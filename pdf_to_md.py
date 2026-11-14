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

import pdf_structure
from md_parse import IMAGE_RE

log = logging.getLogger(__name__)

RENDER_DPI = 150            # figure resolution; sharp on a 13.3in slide without bloating the deck
MIN_IMAGE_PX = 100          # at 150 dpi, ~0.7in; smaller is an icon or bullet glyph
MAX_ASPECT = 8.0            # wider/taller than this is a rule or banner
REPEAT_LIMIT = 3            # a small picture on 3+ pages is a logo/header graphic
LOGO_MAX_PX = 400           # at 150 dpi, under ~2.7in: small enough to be a header logo
BANNER_ASPECT = 2.5         # this much wider than tall (or taller than wide) reads as a banner
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
            info = pdf_structure.analyze(doc)
            chunks = pymupdf4llm.to_markdown(
                doc, write_images=True, image_path=str(img_dir), image_format='png', dpi=RENDER_DPI,
                header=False, footer=False, use_ocr=False, show_progress=False, page_chunks=True)
        except Exception as exc:
            log.exception('pymupdf4llm failed')
            raise PdfConversionError(f'Could not read this PDF: {exc}') from exc

    text = pdf_structure.assemble([c.get('text', '') for c in chunks], info)
    text = relative_image_refs(text, img_dir)
    text = filter_images(text, out_dir)
    text = optimize_images(text, out_dir)
    text = normalize_headings(clean_markdown(text), choose_title(meta_title, info.get('cover_title', '')),
                              info.get('acronyms', ()))
    words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
    if words < 20:
        raise PdfConversionError('No readable text was found. This looks like a scanned PDF; '
                                 'run it through OCR first, then upload it again.')
    md_path.write_text(text, encoding='utf-8')
    return md_path


def choose_title(meta_title, cover_title):
    """The PDF's metadata title when it reads like one; otherwise the large type on the cover."""
    meta_title = (meta_title or '').strip()
    if _good_title(meta_title) and ' ' in meta_title and '_' not in meta_title:
        return meta_title
    return cover_title or (meta_title if _good_title(meta_title) and '_' not in meta_title else '')


# ============================================================
# Headings
# ============================================================
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')


def _heading_text(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\*\*|__', '', text)
    text = re.sub(r'(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _is_italic_heading(raw):
    """'_Be Safe_' (italic only): the converter's mark of a lower-rank heading."""
    raw = re.sub(r'<[^>]+>', '', raw).strip()
    return bool(re.fullmatch(r'[_*](?![_*]).+(?<![_*])[_*]', raw))


def normalize_headings(markdown, title='', acronyms=()):
    """
    pymupdf4llm marks heading levels by font size, so a document can have many '#'
    headings on erratic levels. Rewrite to: one '# Title', sections as '##', deeper or
    italic-only headings as '###'. Chapter headings ('## X {.chapter}') pass through.
    """
    lines = markdown.split('\n')
    heads = []  # (line index, level, text, italic)
    for n, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and not line.rstrip().endswith(pdf_structure.CHAPTER_MARK) and _heading_text(m.group(2)):
            heads.append((n, len(m.group(1)), _heading_text(m.group(2)), _is_italic_heading(m.group(2))))
    title = (title or '').strip()
    has_title = _good_title(title)
    if not heads:
        return (f'# {title}\n\n' if has_title else '') + markdown

    # The document title: the given one, else a top-level heading that opens the document
    first_content = next((n for n, line in enumerate(lines) if line.strip()), 0)
    top = min(lvl for _, lvl, _, _ in heads)
    title_line = None
    if heads[0][0] == first_content and heads[0][1] == top:
        same = re.sub(r'\W+', '', heads[0][2]).lower() == re.sub(r'\W+', '', title).lower()
        if not has_title or same:
            title_line = heads[0][0]
    section_levels = sorted({lvl for n, lvl, _, _ in heads if n != title_line})
    # Sections use the highest level that occurs at least twice: a one-off giant tagline
    # ("BUILT FOR A STRONGER TOMORROW.") must not turn every real heading into a sub-heading
    counts = {lvl: sum(1 for n, l, _, _ in heads if l == lvl and n != title_line) for lvl in section_levels}
    section_level = next((lvl for lvl in section_levels if counts[lvl] >= 2), section_levels[0] if section_levels else 1)
    at_level = [h for h in heads if h[1] == section_level and h[0] != title_line]
    # Italic-only headings rank below upright ones when the section level mixes both
    italic_subs = 0 < sum(h[3] for h in at_level) < len(at_level)
    out = list(lines)
    for n, lvl, text, italic in heads:
        text = pdf_structure.smart_title(text, acronyms)
        if n == title_line:
            out[n] = f'# {text}'
        elif lvl > section_level or (italic and italic_subs and lvl == section_level):
            out[n] = f'### {text}'
        else:
            out[n] = f'## {text}'
    result = '\n'.join(out)
    if title_line is None and has_title:
        result = f'# {title}\n\n' + result
    return result


def _good_title(title):
    title = (title or '').strip()
    # Skip generator defaults such as "untitled", "Microsoft Word - doc1.docx", "report.pdf"
    return (4 <= len(title) <= 150
            and not re.search(r'\.(docx?|pdf|pptx?|indd)$|^untitled|^microsoft|^\(?anonymous\)?$|^document\d*$',
                              title, re.I))


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
        # Repeated on 3+ pages and small or banner-shaped: a logo or a running header drawn as art
        repeated_logo = k is not None and clusters[k][1] >= REPEAT_LIMIT \
            and (side < LOGO_MAX_PX or image_aspect(path) >= BANNER_ASPECT)
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
            px = list(g.tobytes())
    except (OSError, ValueError):
        return None
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def hamming(a, b):
    return bin(a ^ b).count('1')


def image_aspect(path):
    try:
        with Image.open(path) as im:
            return max(im.size) / max(1, min(im.size))
    except (OSError, ValueError):
        return 1.0


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
    text = repair_spliced_pullquotes(text)
    # Headings the layout model missed: a bold phrase glued to the end of a paragraph,
    # or a bold line on its own without closing punctuation
    # (a heading never starts in lower case, so those are left alone)
    text = re.sub(r'(?m)^(.*[.!?:])[ \t]+\*\*([A-Z0-9][^*\n]{1,79})\*\*[ \t]*$', r'\1\n\n**\2**', text)
    text = re.sub(r'(?m)^\*\*([A-Z0-9][^*\n]{1,79}[^.!?:,;*\n])\*\*[ \t]*$', r'### \1', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() + '\n'


SPLICED_RE = re.compile(r'(?<=[a-z,]) (?:\*\*_|_\*\*|\*\*\*|\*\*|_)([^*_\n]{6,90}?)(?:_\*\*|\*\*_|\*\*\*|\*\*|_) (?=[a-z])')
LOWER_HEAD_RE = re.compile(r'^(?:#{1,6}\s+)?(?:\*\*_|_\*\*|\*\*\*|\*\*|_)?([a-z][^*_\n]{3,200}?)(?:_\*\*|\*\*_|\*\*\*|\*\*|_)?\s*$')


TAIL_RE = re.compile(r'\s(?:\*\*_|_\*\*|\*\*\*|\*\*)([a-z][^*_\n]{3,200}?)(?:_\*\*|\*\*_|\*\*\*|\*\*)\s*$')


def repair_spliced_pullquotes(text):
    """
    Text that wraps around a pull-quote is extracted with the quote's first line spliced into
    the middle of a sentence and its remaining lines set apart (often as a "heading" that starts
    in lower case). Lift the fragment out and reunite the quote; a heading that starts in lower
    case is never a heading, so it becomes body text either way.
    """
    lines = text.split('\n')
    for n, line in enumerate(lines):
        if line.lstrip().startswith(('#', '|', '!', '-')):
            continue
        m = SPLICED_RE.search(line)
        if not m:
            continue
        same = TAIL_RE.search(line, m.end())
        if same:  # the rest of the quote sits at the end of the same line
            lines[n] = (line[:m.start()] + ' ' + line[m.end():same.start()]).rstrip() \
                + f'\n\n_**{m.group(1).strip()} {same.group(1).strip()}**_'
            continue
        for k in range(n + 1, min(n + 8, len(lines))):
            tail = LOWER_HEAD_RE.match(lines[k].strip())
            emphasised = lines[k].lstrip().startswith(('#', '*', '_'))
            if tail and emphasised:
                lines[n] = line[:m.start()] + ' ' + line[m.end():]
                lines[k] = f'_**{m.group(1).strip()} {tail.group(1).strip()}**_'
                break
    out = []
    for line in lines:
        m = re.match(r'^#{1,6}\s+(.*)$', line)
        if m and re.match(r'^[*_]*[a-z]', m.group(1)):
            line = m.group(1)  # a "heading" in lower case is a stray continuation line
        out.append(line)
    return '\n'.join(out)


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    print(convert_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'out', print))
