"""
Stage 1: PDF -> Markdown + extracted images, using Docling.

Docling detects the layout (headings, paragraphs, lists, tables, figures), drops
page headers/footers, and writes each figure to an image file referenced from the
Markdown at the position it appeared, so every image lands in the section it
belongs to. Junk images (tiny icons, thin rules, blank boxes, logos repeated on
every page) are filtered out afterwards.
"""
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

from PIL import Image, ImageStat

from md_parse import IMAGE_RE

log = logging.getLogger(__name__)

IMAGES_SCALE = 2.0          # render figures at 2x (144 dpi) so they stay sharp on slides
MIN_IMAGE_PX = 120          # at 2x scale, ~60pt; smaller is an icon or bullet glyph
MAX_ASPECT = 8.0            # wider/taller than this is a rule or banner
REPEAT_LIMIT = 3            # an identical image on 3+ pages is a logo/header graphic

_converters = {}
_lock = threading.Lock()    # Docling models are heavy; build once, convert one PDF at a time


class PdfConversionError(Exception):
    """The PDF itself could not be converted (corrupt, encrypted, empty)."""


class EngineUnavailable(PdfConversionError):
    """Docling could not load its models: a server setup problem, not the user's PDF."""


def _converter(ocr):
    if ocr not in _converters:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        # Pre-downloaded models (`docling-tools models download -o <dir>`) for offline servers
        if os.environ.get('DOCLING_ARTIFACTS_PATH'):
            opts.artifacts_path = os.environ['DOCLING_ARTIFACTS_PATH']
        opts.do_ocr = ocr
        opts.do_table_structure = True
        opts.generate_picture_images = True
        opts.images_scale = IMAGES_SCALE
        converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
        try:
            converter.initialize_pipeline(InputFormat.PDF)  # loads (and on first run downloads) the models
        except Exception as exc:
            log.exception('Docling failed to initialise')
            raise EngineUnavailable(
                'The PDF reader could not load its models, so no PDF can be converted right now. '
                'This is a server setup problem (see README: "Docling models").') from exc
        _converters[ocr] = converter
    return _converters[ocr]


def warm_up():
    """Load the Docling models ahead of the first upload (raises EngineUnavailable)."""
    with _lock:
        _converter(False)


def convert_pdf(pdf_path, out_dir, progress=None):
    """
    Convert a PDF. Returns the path of the written Markdown file; images are saved
    under out_dir/images and referenced with paths relative to the Markdown file.
    Text-free (scanned) PDFs are retried with OCR.
    """
    from docling_core.types.doc import ImageRefMode

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / 'document.md'
    progress = progress or (lambda msg: None)

    with _lock:
        for ocr in (False, True):
            if ocr:
                progress('Little text found; running OCR on the pages…')
            converter = _converter(ocr)
            try:
                result = converter.convert(str(pdf_path))
            except Exception as exc:  # docling raises a range of errors for bad input
                raise PdfConversionError(f'Could not read this PDF: {exc}') from exc
            doc = result.document
            doc.save_as_markdown(md_path, artifacts_dir=Path('images'), image_mode=ImageRefMode.REFERENCED,
                                 escaping_underscores=False)
            text = md_path.read_text(encoding='utf-8')
            words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
            if words >= 30 or ocr:
                break

    text = clean_markdown(filter_images(text, md_path.parent))
    if len(text.split()) < 5:
        raise PdfConversionError('No readable text or images were found in this PDF.')
    md_path.write_text(text, encoding='utf-8')
    return md_path


def filter_images(markdown, base_dir):
    """Drop image references that are icons, rules, blank boxes or repeated logos."""
    lines = markdown.split('\n')
    refs = []  # (line index, path, digest)
    for n, line in enumerate(lines):
        m = IMAGE_RE.match(line.strip())
        if not m:
            continue
        path = resolve(base_dir, m.group('path'))
        refs.append((n, path, image_digest(path)))

    counts = {}
    for _, _, digest in refs:
        if digest:
            counts[digest] = counts.get(digest, 0) + 1

    seen, drop = set(), set()
    for n, path, digest in refs:
        keep = digest is not None and counts[digest] < REPEAT_LIMIT and digest not in seen and usable_image(path)
        if digest:
            seen.add(digest)
        if not keep:
            drop.add(n)
    if drop:
        log.info('Dropped %d of %d extracted images', len(drop), len(refs))
    return '\n'.join(line for n, line in enumerate(lines) if n not in drop)


def resolve(base_dir, ref):
    path = Path(ref)
    return path if path.is_absolute() else Path(base_dir) / path


def image_digest(path):
    try:
        with Image.open(path) as im:
            small = im.convert('L').resize((16, 16))
            return hashlib.md5(small.tobytes()).hexdigest()
    except (OSError, ValueError):
        return None


def usable_image(path):
    try:
        with Image.open(path) as im:
            w, h = im.size
            if min(w, h) < MIN_IMAGE_PX or max(w, h) / max(min(w, h), 1) > MAX_ASPECT:
                return False
            stat = ImageStat.Stat(im.convert('L').resize((64, 64)))
            return stat.stddev[0] > 4  # near-uniform = blank box or background
    except (OSError, ValueError):
        return False


def clean_markdown(text):
    """Remove leftovers Docling can leave behind: bare page numbers, hyphenation, blank runs."""
    text = re.sub(r'(?m)^\s*(page\s+)?\d{1,3}(\s*(/|of)\s*\d{1,3})?\s*$', '', text, flags=re.I)
    text = re.sub(r'(\w)-\s*\n\s*(\w)', r'\1\2', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() + '\n'


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    print(convert_pdf(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else 'out', print))
