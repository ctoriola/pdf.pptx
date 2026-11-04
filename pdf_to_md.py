"""
Stage 1: PDF -> Markdown + extracted images, using PyMuPDF (pymupdf4llm + its layout model).

pymupdf4llm detects the page layout (headings, paragraphs, lists, tables, figures),
drops page headers/footers, and writes each figure to an image file referenced from
the Markdown at the position it appeared, so every image lands in the section it
belongs to. Afterwards:
  - heading levels are normalised to one '# Title' and '## Section' / '### Sub' headings
  - junk images (tiny icons, thin rules, blank boxes, repeated logos) are dropped
  - photos are recompressed so the finished deck stays small
"""
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageStat

from md_parse import IMAGE_RE

log = logging.getLogger(__name__)

RENDER_DPI = 150            # figure resolution; sharp on a 13.3in slide without bloating the deck
MIN_IMAGE_PX = 100          # at 150 dpi, ~0.7in; smaller is an icon or bullet glyph
MAX_ASPECT = 8.0            # wider/taller than this is a rule or banner
REPEAT_LIMIT = 3            # a small picture on 3+ pages is a logo/header graphic
LOGO_MAX_PX = 400           # at 150 dpi, under ~2.7in: small enough to be a header logo
DUPLICATE_DISTANCE = 6      # perceptual-hash bits that may differ between copies of one picture
MAX_IMAGE_PX = 1600         # longest side after recompression
MAX_PAGES = int(os.environ.get('MAX_PAGES', 80))


class PdfConversionError(Exception):
    """The PDF could not be converted (corrupt, encrypted, too long, no text)."""


def convert_pdf(pdf_path, out_dir, progress=None):
    """
    Convert a PDF. Returns the path of the written Markdown file; images are saved
    under out_dir/images and referenced as images/<name> relative to the Markdown file.
    """
    import pymupdf
    import pymupdf4llm

    out_dir = Path(out_dir)
    img_dir = out_dir / 'images'
    img_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / 'document.md'
    progress = progress or (lambda msg: None)

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PdfConversionError('This file could not be opened as a PDF.') from exc
    with doc:
        if doc.needs_pass:
            raise PdfConversionError('This PDF is password-protected. Remove the password and try again.')
        if doc.page_count > MAX_PAGES:
            raise PdfConversionError(f'This PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.')
        meta_title = (doc.metadata or {}).get('title', '')
        try:
            text = pymupdf4llm.to_markdown(
                doc, write_images=True, image_path=str(img_dir), image_format='png', dpi=RENDER_DPI,
                header=False, footer=False, use_ocr=False, show_progress=False)
        except Exception as exc:
            log.exception('pymupdf4llm failed')
            raise PdfConversionError(f'Could not read this PDF: {exc}') from exc

    text = relative_image_refs(text, img_dir)
    text = filter_images(text, out_dir)
    text = optimize_images(text, out_dir)
    text = normalize_headings(clean_markdown(text), meta_title)
    words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
    if words < 20:
        raise PdfConversionError('No readable text was found. This looks like a scanned PDF; '
                                 'run it through OCR first, then upload it again.')
    md_path.write_text(text, encoding='utf-8')
    return md_path


# ============================================================
# Headings
# ============================================================
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def _heading_text(text):
    text = re.sub(r'\*\*|__', '', text)
    text = re.sub(r'^[*_](.+)[*_]$', r'\1', text.strip())
    return re.sub(r'\s+', ' ', text).strip()


def normalize_headings(markdown, meta_title=''):
    """
    pymupdf4llm marks heading levels by font size, so a document can have many '#'
    headings. Rewrite to: one '# Title', top-level headings as '##', deeper as '###'.
    """
    lines = markdown.split('\n')
    heads = [(n, len(m.group(1)), _heading_text(m.group(2))) for n, line in enumerate(lines)
             if (m := HEADING_RE.match(line)) and _heading_text(m.group(2))]
    if not heads:
        return (f'# {meta_title.strip()}\n\n' if _good_title(meta_title) else '') + markdown

    # A heading at the top level that opens the document is its title
    first_content = next((n for n, line in enumerate(lines) if line.strip()), 0)
    top = min(lvl for _, lvl, _ in heads)
    title_line = heads[0][0] if heads[0][0] == first_content and heads[0][1] == top else None
    section_levels = sorted({lvl for n, lvl, _ in heads if n != title_line})

    out_levels = {lvl: min(6, 2 + i) for i, lvl in enumerate(section_levels)}
    out = list(lines)
    for n, lvl, text in heads:
        out[n] = f'# {text}' if n == title_line else f'{"#" * out_levels[lvl]} {text}'
    result = '\n'.join(out)
    if title_line is None and _good_title(meta_title):
        result = f'# {meta_title.strip()}\n\n' + result
    return result


def _good_title(title):
    title = (title or '').strip()
    # Skip generator defaults such as "untitled", "Microsoft Word - doc1.docx", "report.pdf"
    return (4 <= len(title) <= 150 and not re.search(r'\.(docx?|pdf|pptx?|indd)$|^untitled|^microsoft', title, re.I))


# ============================================================
# Images
# ============================================================
def relative_image_refs(markdown, img_dir):
    """pymupdf4llm writes absolute paths; make them images/<name> so the folder is portable."""
    img_dir = str(img_dir)

    def repl(m):
        path = m.group(2)
        name = Path(path).name if path.startswith(img_dir) or os.path.isabs(path) else path
        return f'![{m.group(1)}](images/{name})' if (Path(img_dir) / Path(path).name).is_file() else ''
    return re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', repl, markdown)


def filter_images(markdown, base_dir):
    """
    Drop image references that are icons, rules or blank boxes, and repeats of the same
    picture (matched by perceptual hash, so resized copies count). The first copy of a
    picture is kept, unless it is a small graphic repeated on many pages (a logo in the
    page header), in which case every copy goes.
    """
    lines = markdown.split('\n')
    refs = []  # (line index, path, hash, longest side)
    for n, line in enumerate(lines):
        m = IMAGE_RE.match(line.strip())
        if m:
            path = resolve(base_dir, m.group('path'))
            refs.append((n, path, image_hash(path), image_side(path)))

    clusters = []  # [representative hash, count]
    cluster_of = {}
    for n, _, h, _ in refs:
        if h is None:
            continue
        for k, (rep, _) in enumerate(clusters):
            if hamming(rep, h) <= DUPLICATE_DISTANCE:
                clusters[k][1] += 1
                cluster_of[n] = k
                break
        else:
            cluster_of[n] = len(clusters)
            clusters.append([h, 1])

    seen, drop = set(), set()
    for n, path, h, side in refs:
        k = cluster_of.get(n)
        repeated_logo = k is not None and clusters[k][1] >= REPEAT_LIMIT and side < LOGO_MAX_PX
        if k is None or k in seen or repeated_logo or not usable_image(path):
            drop.add(n)
        if k is not None:
            seen.add(k)
    if drop:
        log.info('Dropped %d of %d extracted images', len(drop), len(refs))
    return '\n'.join(line for n, line in enumerate(lines) if n not in drop)


def image_hash(path):
    """64-bit difference hash: survives resizing and recompression."""
    try:
        with Image.open(path) as im:
            g = im.convert('L').resize((9, 8), Image.LANCZOS)
            px = list(g.getdata())
    except (OSError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a, b):
    return bin(a ^ b).count('1')


def image_side(path):
    try:
        with Image.open(path) as im:
            return max(im.size)
    except (OSError, ValueError):
        return 0


def optimize_images(markdown, base_dir):
    """Cap image size and store photos as JPEG (charts and diagrams stay PNG)."""
    def repl(m):
        path = resolve(base_dir, m.group('path'))
        new = shrink_image(path, MAX_IMAGE_PX, 85)
        return m.group(0).replace(m.group('path'), str(Path(m.group('path')).with_name(new.name)))
    return '\n'.join(IMAGE_RE.sub(repl, line.strip()) if IMAGE_RE.match(line.strip()) else line
                     for line in markdown.split('\n'))


def shrink_image(path, max_px, quality):
    """Downscale in place; photo-like images become JPEG. Returns the (possibly renamed) path."""
    path = Path(path)
    try:
        with Image.open(path) as im:
            im.load()
            img = im
    except (OSError, ValueError):
        return path
    if max(img.size) > max_px:
        img = img.copy()
        img.thumbnail((max_px, max_px), Image.LANCZOS)
    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    # Photos have thousands of distinct colours; charts and diagrams have few and compress better as PNG
    photo = not has_alpha and len(img.convert('RGB').resize((96, 96)).getcolors(96 * 96)) > 2000
    if photo:
        new = path.with_suffix('.jpg')
        img.convert('RGB').save(new, 'JPEG', quality=quality, optimize=True)
        if new != path:
            path.unlink(missing_ok=True)
        return new
    img.save(path, optimize=True)
    return path


def resolve(base_dir, ref):
    path = Path(ref)
    return path if path.is_absolute() else Path(base_dir) / path


def usable_image(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
            if min(w, h) < MIN_IMAGE_PX or max(w, h) / max(min(w, h), 1) > MAX_ASPECT:
                return False
            return ImageStat.Stat(im.convert('L').resize((64, 64))).stddev[0] > 4  # near-uniform = blank
    except (OSError, ValueError):
        return False


# ============================================================
# Text clean-up
# ============================================================
def clean_markdown(text):
    """Remove leftovers: bare page numbers, layout-parser notices, hyphenation, blank runs."""
    text = re.sub(r'(?m)^=== Document parser messages ===\s*$', '', text)
    text = re.sub(r'(?m)^\s*(page\s+)?\d{1,3}(\s*(/|of)\s*\d{1,3})?\s*$', '', text, flags=re.I)
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    # Headings the layout model missed: a bold phrase glued to the end of a paragraph,
    # or a bold line on its own without closing punctuation
    text = re.sub(r'(?m)^(.*[.!?:])[ \t]+\*\*([^*\n]{2,80})\*\*[ \t]*$', r'\1\n\n**\2**', text)
    text = re.sub(r'(?m)^\*\*([^*\n]{2,80}[^.!?:,;*\n])\*\*[ \t]*$', r'### \1', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() + '\n'


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    print(convert_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'out', print))
