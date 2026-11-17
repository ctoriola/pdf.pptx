"""
OCR for scanned PDFs: pages that are pictures of text get an invisible text layer, so the rest of
the pipeline (chapter detection from font sizes, the layout model, image extraction) runs on them
exactly as on a normal PDF.

Uses the Tesseract engine built into PyMuPDF, so there is nothing to install: it only needs
Tesseract's language data, shipped in ./tessdata (English, "fast" model, Apache 2.0). More
languages: add their .traineddata files there and list them in OCR_LANGUAGES, e.g. "eng+fra".
"""
import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

TESSDATA = Path(os.environ.get('OCR_TESSDATA') or Path(__file__).resolve().parent / 'tessdata')
LANGUAGES = os.environ.get('OCR_LANGUAGES', 'eng')
OCR_DPI = int(os.environ.get('OCR_DPI', 200))           # 200 reads printed text well and is quick
MAX_OCR_PAGES = int(os.environ.get('MAX_OCR_PAGES', 40))  # read in one request (~1-3 s a page; Vercel allows 300 s)
OCR_BATCH = int(os.environ.get('OCR_BATCH', 30))         # more than that: the browser sends batches of this many
MIN_TEXT_CHARS = 40          # a page with less real text than this...
MIN_IMAGE_SHARE = 0.4        # ...and a picture covering this much of it is a scan


def available():
    """True when the language data for every language in OCR_LANGUAGES is present."""
    return all((TESSDATA / f'{lang}.traineddata').is_file() for lang in LANGUAGES.split('+') if lang)


def page_needs_ocr(page):
    """A page that is a picture of text: almost no text layer, and mostly covered by image."""
    if len(page.get_text('text').strip()) >= MIN_TEXT_CHARS:
        return False
    area = abs(page.rect) or 1
    covered = 0.0
    for info in page.get_image_info():
        r = page.rect & info['bbox']
        covered += abs(r)
    return covered / area >= MIN_IMAGE_SHARE


def pages_needing_ocr(doc):
    return [n for n, page in enumerate(doc) if page_needs_ocr(page)]


LARGE_TEXT_DPI = 50      # a second, coarse pass: Tesseract misses display type (40pt+) at OCR_DPI
LARGE_TEXT_MIN_PT = 20   # from that pass, keep only lines at least this tall...
LARGE_TEXT_RATIO = 2.2   # ...and this many times the page's usual line height...
OVERLAP_SHARE = 0.3      # ...that the fine pass did not already read


def _lines(page, dpi):
    """[(bbox, text, size, baseline)] for each line Tesseract reads on the page at this resolution."""
    import pymupdf
    tp = page.get_textpage_ocr(dpi=dpi, full=True, language=LANGUAGES, tessdata=str(TESSDATA))
    out = []
    for block in page.get_text('dict', textpage=tp).get('blocks', []):
        for line in block.get('lines', []):
            spans = [sp for sp in line['spans'] if sp['text'].strip()]
            text = ' '.join(''.join(sp['text'] for sp in line['spans']).split())
            if not spans or not text:
                continue
            if _is_noise(text):
                continue
            sizes = sorted(sp['size'] for sp in spans)
            out.append((pymupdf.Rect(line['bbox']), text, sizes[len(sizes) // 2], spans[0]['origin'][1]))
    return out


def _is_noise(text):
    """Fragments OCR reads from logos, icons and rules: "y", "= | ~", "@#". Numbers ("1", "02") are kept."""
    chars = text.replace(' ', '')
    alnum = sum(c.isalnum() for c in chars)
    if chars.isdigit():
        return False
    return len(chars) <= 2 or alnum < len(chars) * 0.5


def ocr_page_lines(page):
    """
    Body text from a fine pass, plus large headings from a coarse pass. Where both passes read the
    same large text, the coarse reading wins: Tesseract reads display type better at low resolution.
    """
    fine = _lines(page, OCR_DPI)
    heights = sorted(f[0].height for f in fine)
    usual = heights[len(heights) // 2] if heights else 10.0
    large_min = max(LARGE_TEXT_MIN_PT, usual * LARGE_TEXT_RATIO)
    large = []
    for bbox, text, size, base in _lines(page, LARGE_TEXT_DPI):
        chars = text.replace(' ', '')
        alnum = sum(c.isalnum() for c in chars)
        # short product names count ("MS-5"): two letters or digits, at least one letter, mostly alphanumeric
        if bbox.height >= large_min and alnum >= 2 and any(c.isalpha() for c in chars) and alnum >= len(chars) * 0.6:
            large.append((bbox, text, size, base))

    def overlaps(a, b):
        return abs(a & b) > min(abs(a), abs(b)) * OVERLAP_SHARE

    lines = []
    for f in fine:
        replaced = f[0].height >= large_min * 0.8 and any(overlaps(f[0], g[0]) for g in large)
        if not replaced:
            lines.append(f)
    for g in large:
        if not any(overlaps(g[0], f[0]) for f in lines):
            lines.append(g)
    return sorted(lines, key=lambda line: (line[3], line[0].x0))


def read_pages(doc, pages, progress=None):
    """
    OCR the given pages: {page index: [[x0, baseline, size, text], ...]}, one entry per line. Plain
    JSON, so pages read in separate requests (a scanned textbook, in batches) can be put together.
    """
    progress = progress or (lambda msg: None)
    out = {}
    for k, n in enumerate(pages, 1):
        progress(f'Reading scanned page {k} of {len(pages)} (OCR)…')
        out[n] = [[round(bbox.x0, 2), round(base, 2), round(size, 2), text]
                  for bbox, text, size, base in ocr_page_lines(doc[n])]
    return out


def apply_lines(doc, lines):
    """
    A copy of doc in which each page in `lines` carries an invisible text layer with the recognised
    text, one line at a time at that line's size, over the unchanged picture.
    """
    import pymupdf
    out = pymupdf.open()
    out.insert_pdf(doc)
    for n, page_lines in lines.items():
        page = out[n]
        for x0, base, size, text in page_lines:
            # invisible text (render mode 3): searchable and extractable, nothing drawn
            page.insert_text((x0, base), text, fontsize=max(4.0, size), fontname='helv', render_mode=3)
    log.info('OCR text layer on %d of %d pages', len(lines), doc.page_count)
    return out


def make_searchable(doc, pages, progress=None):
    """OCR `pages` and return a copy of doc with their text layer."""
    return apply_lines(doc, read_pages(doc, pages, progress))


MAX_LINES_PER_PAGE = 600
MAX_LINE_CHARS = 1000


def clean_lines(data, doc, allowed):
    """
    OCR results sent back by the browser, checked: only pages in `allowed` (the scanned pages),
    numbers clamped to the page, text capped. Anything malformed is dropped (that page is read again).
    """
    out = {}
    if not isinstance(data, dict):
        return out
    for key, page_lines in list(data.items())[:5000]:
        try:
            n = int(key)
        except (TypeError, ValueError):
            continue
        if n not in allowed or not isinstance(page_lines, list):
            continue
        rect = doc[n].rect
        good = []
        for line in page_lines[:MAX_LINES_PER_PAGE]:
            if not (isinstance(line, list) and len(line) == 4 and isinstance(line[3], str)
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                            for v in line[:3])):
                good = None
                break
            x0, base, size, text = line
            text = ' '.join(text.split())[:MAX_LINE_CHARS]
            if text:
                good.append([min(max(x0, 0), rect.width), min(max(base, 0), rect.height),
                             min(max(size, 4), 200), text])
        if good is not None:
            out[n] = good
    return out
