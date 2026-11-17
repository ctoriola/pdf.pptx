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
import json
import logging
import os
import re
import time
from pathlib import Path

from PIL import Image, ImageStat

import ocr
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
# Reading takes about 0.15-0.3 s a page. One request reads up to READ_PAGES; a longer book is read in
# parts by several requests at once (read_part), then planned from the parts' text in one more
MAX_PAGES = int(os.environ.get('MAX_PAGES', 3000))
READ_PAGES = int(os.environ.get('READ_PAGES', 300))
# Pages are read in batches; past this many seconds of reading, the conversion stops with a clear
# message instead of being cut off by the host (Vercel ends a function at 300 s)
READ_TIME_LIMIT = float(os.environ.get('READ_TIME_LIMIT', 240))
READ_BATCH = 40


# ---- heading levels across the whole book ----
# pymupdf4llm ranks heading font sizes over the pages of each call (the largest becomes "#"), so a
# book read in batches or in parts would get levels that depend on where it was cut. Each heading
# is tagged with its font size instead, and relevel_headings ranks the sizes once over the whole
# book, exactly as pymupdf4llm would over a single call.
FS_MARK = re.compile(r'^(#{1,6}) <!--fs ([0-9.]+)-->', re.M)


def _tag_heading_sizes():
    from pymupdf4llm.helpers import document_layout as dl
    if getattr(dl, '_pdf2pptx_tagged', False):
        return
    original_update = dl.update_header_tags

    class Level(int):
        fs = None

    def update_header_tags(pages, header_fontsizes):
        original_update(pages, header_fontsizes)
        for page in pages:
            for box in page.boxes:
                if box.boxclass in ('title', 'section-header') and box.header_level:
                    level = Level(box.header_level)
                    level.fs = box.max_fontsize
                    box.header_level = level

    def tagged(convert):
        def to_md(header_level, textlines):
            md = convert(header_level, textlines)
            fs = getattr(header_level, 'fs', None)
            hashes = '#' * int(header_level)
            if fs is None or not md.startswith(hashes + ' '):
                return md
            return f'{hashes} <!--fs {float(fs):g}-->' + md[len(hashes) + 1:]
        return to_md

    dl.update_header_tags = update_header_tags
    dl.section_hdr_to_md = tagged(dl.section_hdr_to_md)
    dl.title_to_md = tagged(dl.title_to_md)
    dl._pdf2pptx_tagged = True


def relevel_headings(page_texts):
    """Heading levels from font sizes ranked over every page, and the size tags removed."""
    sizes = sorted({float(m.group(2)) for t in page_texts for m in FS_MARK.finditer(t)}, reverse=True)[:6]

    def level(m):
        fs = float(m.group(2))
        return '#' * (sizes.index(fs) + 1 if fs in sizes else 6) + ' '
    return [FS_MARK.sub(level, t) for t in page_texts]


class PdfConversionError(Exception):
    """The PDF could not be converted (corrupt, encrypted, too long, no text)."""


class NeedsParts(PdfConversionError):
    """
    Longer than one request can read: the web app has the browser read it in parts (POST /read,
    several at once) and then plans it from them (parts=...).
    """
    def __init__(self, page_count):
        self.ranges = [(first, min(READ_PAGES, page_count - first)) for first in range(0, page_count, READ_PAGES)]
        super().__init__(f'This PDF has {page_count} pages; one request reads up to {READ_PAGES}. '
                         'Split the PDF and convert the parts.')


class NeedsOcr(PdfConversionError):
    """
    More scanned pages than one request can OCR. The web app has the browser send them in batches
    (POST /ocr) and then reads the PDF again with the results (ocr_lines).
    """
    def __init__(self, pages):
        self.pages = pages
        super().__init__(f'This PDF has {len(pages)} scanned pages; one request reads up to '
                         f'{ocr.MAX_OCR_PAGES} with OCR. Split the PDF and convert the parts.')


def _open(pdf_path):
    import pymupdf
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PdfConversionError('This file could not be opened as a PDF.') from exc
    if doc.needs_pass:
        doc.close()
        raise PdfConversionError('This PDF is password-protected. Remove the password and try again.')
    return doc


def _with_ocr(doc, ocr_lines, progress, may_ocr_here=True):
    """
    (src, scanned pages): src is doc, or a copy whose scanned pages carry the OCR text layer.
    Pages already read (ocr_lines) are used as they are; NeedsOcr when the rest are too many for
    this request (or when none may be read here: a long book is read in parts, which need the text).
    """
    scanned = ocr.pages_needing_ocr(doc)
    if not scanned:
        return doc, scanned
    if not ocr.available():
        log.warning('%d scanned pages, but no OCR language data in %s', len(scanned), ocr.TESSDATA)
        return doc, scanned
    lines = ocr.clean_lines(ocr_lines, doc, set(scanned)) if ocr_lines else {}
    todo = [n for n in scanned if n not in lines]
    if len(todo) > ocr.MAX_OCR_PAGES or (todo and not may_ocr_here):
        raise NeedsOcr(todo)
    try:
        if todo:
            progress('This PDF is scanned. Reading its pages with OCR…')
            lines.update(ocr.read_pages(doc, todo, progress))
        return ocr.apply_lines(doc, lines), scanned
    except Exception as exc:
        log.exception('OCR failed')
        raise PdfConversionError('The scanned pages of this PDF could not be read.') from exc


def _read_pages(src, pages, img_dir, progress, started):
    """
    pymupdf4llm over `pages`, READ_BATCH at a time and within READ_TIME_LIMIT. Returns (page chunks,
    {picture file: (page index, bbox)}).
    """
    import pymupdf4llm
    _tag_heading_sizes()
    chunks = []
    try:
        for i in range(0, len(pages), READ_BATCH):
            if chunks and time.monotonic() - started > READ_TIME_LIMIT:
                raise PdfConversionError(
                    f'This PDF is too long to read in one go here: {i} of its {len(pages)} pages were read '
                    f'in the time allowed. Split it (for example by part or chapter) and convert the parts.')
            batch = pages[i:i + READ_BATCH]
            if len(pages) > READ_BATCH:
                progress(f'Reading pages {i + 1}-{i + len(batch)} of {len(pages)}…')
            chunks += pymupdf4llm.to_markdown(
                src, pages=batch, write_images=True, image_path=str(img_dir), image_format='png', dpi=RENDER_DPI,
                header=False, footer=False, use_ocr=False, show_progress=False, page_chunks=True)
    except PdfConversionError:
        raise
    except Exception as exc:
        log.exception('pymupdf4llm failed')
        raise PdfConversionError(f'Could not read this PDF: {exc}') from exc
    try:
        saved = pdf_structure.recover_missed_images(src, chunks, str(img_dir), RENDER_DPI)
    except Exception:  # a safety net only: never fail a conversion over it
        log.exception('Recovering missed images failed')
        saved = {}
    return chunks, pdf_structure.image_boxes(chunks, img_dir, saved)


def convert_pdf(pdf_path, out_dir, progress=None, ocr_lines=None, parts=None):
    """
    Convert a PDF. Returns the path of the written Markdown file; images are saved
    under out_dir/images and referenced as images/<name> relative to the Markdown file.
    ocr_lines: OCR results already read for some scanned pages (ocr.read_pages, e.g. in batches).
    parts: a book longer than READ_PAGES, read in parts by read_part (in parallel requests). Its
    pictures are then known by size and position only (nothing is saved under images/): the plan
    needs no more, and the build step cuts the chosen ones from the PDF.
    """
    out_dir = Path(out_dir)
    img_dir = out_dir / 'images'
    img_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / 'document.md'
    progress = progress or (lambda msg: None)
    started = time.monotonic()

    with _open(pdf_path) as doc:
        if doc.page_count > MAX_PAGES:
            raise PdfConversionError(f'This PDF has {doc.page_count} pages; the limit is {MAX_PAGES}.')
        meta_title = (doc.metadata or {}).get('title', '')
        long_book = doc.page_count > READ_PAGES
        # Scanned pages (pictures of text) get an invisible text layer first, so everything below
        # treats them like any other page
        src, scanned = _with_ocr(doc, ocr_lines, progress, may_ocr_here=not long_book)
        if long_book and parts is None:
            raise NeedsParts(doc.page_count)
        try:
            info = pdf_structure.analyze(src)
        except Exception as exc:
            log.exception('Reading the layout failed')
            raise PdfConversionError(f'Could not read this PDF: {exc}') from exc
        if parts is None:
            chunks, boxes = _read_pages(src, list(range(src.page_count)), img_dir, progress, started)
            page_texts, facts = [c.get('text', '') for c in chunks], None
        else:
            progress('Putting the parts together…')
            page_texts, facts = join_parts(parts, doc.page_count)
        page_texts = relevel_headings(page_texts)
        if src is not doc:
            src.close()
        page_count = doc.page_count

    text = pdf_structure.assemble(page_texts, info)
    if facts is None:
        text = relative_image_refs(text, img_dir)
        text = filter_images(text, out_dir)
        text = optimize_images(text, out_dir)
        positions = {Path(name).stem: [page, *[round(v, 2) for v in bbox]] for name, (page, bbox) in boxes.items()}
        sizes = {}
    else:
        text = filter_images(text, out_dir, facts)
        positions = {Path(name).stem: f['box'] for name, f in facts.items()}
        sizes = {name: f['size'] for name, f in facts.items()}
    text = normalize_headings(clean_markdown(text), choose_title(meta_title, info.get('cover_title', '')),
                              info.get('acronyms', ()))
    meta = {'pages': page_count, 'chapters': len(info.get('chapters', {})), 'ocr_pages': len(scanned),
            'subtitle': info.get('cover_subtitle', ''), 'kicker': info.get('cover_kicker', '')}
    text = f'<!-- pdf2pptx {json.dumps(meta, ensure_ascii=False)} -->\n\n' + text
    words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
    if words < 20:
        if scanned and ocr.available():
            raise PdfConversionError('No readable text was found, even after reading the scanned pages with OCR. '
                                     'The scan may be too faint or blurred.')
        raise PdfConversionError('No readable text was found in this PDF.')
    md_path.write_text(text, encoding='utf-8')
    # where each picture still in use came from, so the build step can cut it again (render_images)
    used = {Path(m.group('path')).name for m in map(IMAGE_RE.match, map(str.strip, text.split('\n'))) if m}
    stems = {Path(n).stem for n in used}
    (out_dir / BOXES_FILE).write_text(json.dumps({k: v for k, v in positions.items() if k in stems}))
    if sizes:
        (out_dir / SIZES_FILE).write_text(json.dumps({k: v for k, v in sizes.items() if k in used}))
    return md_path


# ============================================================
# Long books: read in parts, in parallel requests
# ============================================================
def read_part(pdf_path, first, count, out_dir, ocr_lines=None, is_part_file=False, progress=None):
    """
    Read pages first..first+count-1 of a book: from the whole PDF, or from a PDF holding just those
    pages (is_part_file, cut by the web app so a request downloads only its part). Returns plain
    JSON for convert_pdf(parts=...): the Markdown of each page, and for each picture what filtering
    and planning need (fingerprint, size, kind) and where it is, so the picture itself is not kept.
    ocr_lines: OCR text for this part's scanned pages, by page number in the whole book.
    """
    out_dir = Path(out_dir)
    img_dir = out_dir / 'images'
    img_dir.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda msg: None)
    started = time.monotonic()
    with _open(pdf_path) as doc:
        if is_part_file:
            if doc.page_count != count:
                raise PdfConversionError('That part of the PDF does not match its page numbers.')
            pages, offset = list(range(count)), first
        else:
            if not (0 <= first < doc.page_count and 0 < count <= doc.page_count - first):
                raise PdfConversionError('Those pages are not in this PDF.')
            pages, offset = list(range(first, first + count)), 0
        src = doc
        if ocr_lines:
            local = {}
            for key, value in ocr_lines.items():
                try:
                    local[int(key) - offset] = value
                except (TypeError, ValueError):
                    continue
            scanned = set(ocr.pages_needing_ocr(doc)) & set(pages)
            lines = ocr.clean_lines(local, doc, scanned)
            if lines:
                src = ocr.apply_lines(doc, lines)
        chunks, boxes = _read_pages(src, pages, img_dir, progress, started)
        if src is not doc:
            src.close()

    rename, images = {}, {}
    for name, (page, bbox) in boxes.items():
        facts = image_facts(img_dir / name)
        if facts is None:
            continue
        k = int(name[-6:-4]) if name[-6:-4].isdigit() else 0
        kind = 'missed' if name.startswith('missed-') else 'pic'
        new = f'{kind}-{page + offset + 1:04d}-{k:02d}.{"jpg" if facts.pop("photo") else "png"}'
        rename[name] = new
        images[new] = {**facts, 'box': [page + offset, *[round(v, 2) for v in bbox]]}

    def name_ref(m):
        new = rename.get(m.group(2))
        return f'![{m.group(1)}](images/{new})' if new else ''
    texts = [re.sub(r'!\[([^\]]*)\]\(images/([^)\s]+)\)', name_ref, relative_image_refs(c.get('text', ''), img_dir))
             for c in chunks]
    return {'first': first, 'pages': texts, 'images': images}


PART_IMAGE_NAME = re.compile(r'^(pic|missed)-\d{4,5}-\d{2}\.(png|jpg)$')
MAX_PAGE_CHARS = 200_000


def _number(v, lo, hi):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


def join_parts(parts, page_count):
    """
    The parts read by read_part, checked and put in page order: (page texts, picture facts). They
    come back through the browser, so every value is checked; a part that does not fit is refused.
    """
    bad = PdfConversionError('The parts of this PDF did not fit together. Please convert it again.')
    if not isinstance(parts, list) or not parts:
        raise bad
    texts, facts = [], {}
    for part in sorted((p for p in parts if isinstance(p, dict)), key=lambda p: p.get('first', -1)):
        pages = part.get('pages')
        if part.get('first') != len(texts) or not isinstance(pages, list) or \
                not all(isinstance(t, str) for t in pages):
            raise bad
        texts += [t[:MAX_PAGE_CHARS] for t in pages]
        for name, f in (part.get('images') or {}).items() if isinstance(part.get('images'), dict) else ():
            if not (isinstance(name, str) and PART_IMAGE_NAME.match(name) and isinstance(f, dict)):
                continue
            box, size = f.get('box'), f.get('size')
            if not (isinstance(box, list) and len(box) == 5 and isinstance(box[0], int) and 0 <= box[0] < page_count
                    and all(_number(v, -1e5, 1e5) for v in box[1:])
                    and isinstance(size, list) and len(size) == 2 and all(isinstance(v, int) and 0 < v <= 20000 for v in size)
                    and (f.get('hash') is None or isinstance(f.get('hash'), int) and 0 <= f['hash'] < 1 << 64)
                    and isinstance(f.get('side'), int) and _number(f.get('aspect'), 0, 1e6)
                    and isinstance(f.get('usable'), bool)):
                continue
            facts[name] = {k: f[k] for k in ('box', 'size', 'hash', 'side', 'aspect', 'usable')}
    if len(texts) != page_count:
        raise bad
    return texts, facts


SIZES_FILE = 'image_sizes.json'


def image_facts(path):
    """What filtering and planning need to know about a picture, so the picture need not be kept."""
    try:
        with Image.open(path) as im:
            im.load()
            img = im.copy()
    except (OSError, ValueError):
        return None
    w, h = img.size
    if max(w, h) > MAX_IMAGE_PX:  # as shrink_image does
        img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX), Image.LANCZOS)
    size = list(img.size)
    return {'hash': image_hash(path), 'side': max(w, h), 'aspect': round(max(w, h) / max(1, min(w, h)), 3),
            'usable': usable_image(path), 'photo': _is_photo(img), 'size': size}


BOXES_FILE = 'image_boxes.json'


def render_images(pdf_path, jobs):
    """
    Cut pictures from their pages again: jobs is [(page index, bbox, destination path)]. The same
    render as the first read (RENDER_DPI), resized and saved as the destination's type, so the
    build step needs neither the layout model nor OCR. Returns how many were written.
    """
    import pymupdf
    done = 0
    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PdfConversionError('This file could not be opened as a PDF.') from exc
    with doc:
        for page_no, bbox, dest in jobs:
            if not (isinstance(page_no, int) and 0 <= page_no < doc.page_count):
                continue
            page = doc[page_no]
            clip = pymupdf.Rect(bbox) & page.rect
            if clip.is_empty or clip.width < 2 or clip.height < 2:
                continue
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            img = Image.frombytes('RGB', *_pixmap(page, clip))
            if max(img.size) > MAX_IMAGE_PX:
                img.thumbnail((MAX_IMAGE_PX, MAX_IMAGE_PX), Image.LANCZOS)
            if dest.suffix.lower() in ('.jpg', '.jpeg'):
                img.save(dest, 'JPEG', quality=85, optimize=True)
            else:
                img.save(dest, 'PNG', optimize=True)
            done += 1
    return done


def _pixmap(page, clip):
    pix = page.get_pixmap(clip=clip, dpi=RENDER_DPI, alpha=False)
    return (pix.width, pix.height), pix.samples


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


def filter_images(markdown, base_dir, facts=None):
    """
    Drop image references that are icons, rules or blank boxes, and repeats of the same
    picture (matched by perceptual hash, so resized copies count). The first copy of a
    picture is kept, unless it is a small graphic repeated on many pages (a logo in the
    page header), in which case every copy goes. facts: {file name: image_facts} for pictures
    known without their files (a book read in parts); a reference with no facts is dropped.
    """
    lines = markdown.split('\n')
    refs = []  # (line index, hash, longest side, aspect, usable)
    for n, line in enumerate(lines):
        m = IMAGE_RE.match(line.strip())
        if not m:
            continue
        if facts is not None:
            f = facts.get(Path(m.group('path')).name)
            refs.append((n, f['hash'], f['side'], f['aspect'], f['usable']) if f else (n, None, 0, 1.0, False))
        else:
            path = resolve(base_dir, m.group('path'))
            refs.append((n, image_hash(path), image_side(path), image_aspect(path), usable_image(path)))

    clusters = []  # [representative hash, count]
    cluster_of = {}
    for n, h, *_ in refs:
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
    for n, h, side, aspect, usable in refs:
        k = cluster_of.get(n)
        # Repeated on 3+ pages and small or banner-shaped: a logo or a running header drawn as art
        repeated_logo = k is not None and clusters[k][1] >= REPEAT_LIMIT \
            and (side < LOGO_MAX_PX or aspect >= BANNER_ASPECT)
        if k is None or k in seen or repeated_logo or not usable:
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
    if _is_photo(img):
        new = path.with_suffix('.jpg')
        img.convert('RGB').save(new, 'JPEG', quality=quality, optimize=True)
        if new != path:
            path.unlink(missing_ok=True)
        return new
    img.save(path, optimize=True)
    return path


def _is_photo(img):
    """Photos have thousands of distinct colours; charts and diagrams have few and compress better as PNG."""
    has_alpha = img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
    return not has_alpha and len(img.convert('RGB').resize((96, 96)).getcolors(96 * 96)) > 2000


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
