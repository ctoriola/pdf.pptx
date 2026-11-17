"""
End-to-end conversion: PDF -> Markdown (+images) -> sections -> [LLM] -> .pptx

    convert(pdf_path, work_dir, mode='standard', theme='coral', title=None, progress=print)
"""
import json
import logging
import os
import re
from pathlib import Path

import condense
import planner
import semantics
import text_repair
from deck_builder import build_deck
from md_parse import parse_markdown, word_count
from slide_plan import get_plan

log = logging.getLogger(__name__)

SUBTITLES = {'condensed': 'Summary', 'standard': 'Presentation', 'detailed': 'In-depth presentation'}


def load_sections(md_path):
    """Parse the Markdown file and resolve image paths relative to it."""
    md_path = Path(md_path)
    title, sections = parse_markdown(md_path.read_text(encoding='utf-8'))
    base = md_path.parent.resolve()
    for _, blocks in sections:
        for b in blocks:
            if b['type'] == 'image':
                p = Path(b['path'])
                p = p if p.is_absolute() else (base / p).resolve()
                # Never read files outside the job folder, whatever the Markdown says
                b['path'] = str(p) if base in p.parents and p.is_file() else ''
        blocks[:] = [b for b in blocks if b['type'] != 'image' or b['path']]
    return title, move_trailing_subheadings(merge_empty_sections(sections))


def merge_empty_sections(sections):
    """
    A heading with no body (e.g. "3 Methods" directly followed by "3.1 Data") becomes a
    sub-heading at the top of the next section instead of an empty slide.
    """
    out, pending = [], []
    for name, blocks in sections:
        if not blocks:
            pending.append(name)
            continue
        if pending:
            name, lead = pending[0], pending[1:] + [name]
            blocks = [{'type': 'sub', 'text': n} for n in lead] + blocks
            pending = []
        out.append((name, blocks))
    if pending and out:
        out[-1][1].extend({'type': 'sub', 'text': n} for n in pending)
    return out


def move_trailing_subheadings(sections):
    """
    A sub-heading with nothing after it (a product name like "DSO-1", or a label such as
    "01 / TACTICAL SHOTGUN" set just above the next heading) introduces the next section, so it
    moves to the start of that section. A section left empty by this is dropped.
    """
    out = [(name, list(blocks)) for name, blocks in sections]
    for i in range(len(out) - 1):
        name, blocks = out[i]
        moved = []
        while blocks and blocks[-1]['type'] == 'sub':
            moved.insert(0, blocks.pop())
        if moved:
            nxt_name, nxt_blocks = out[i + 1]
            at = 1 if nxt_blocks and nxt_blocks[0]['type'] == 'chapter' else 0
            out[i + 1] = (nxt_name, nxt_blocks[:at] + moved + nxt_blocks[at:])
    return [(name, blocks) for name, blocks in out if blocks]


def deck_title(md_title, pdf_path, override=None):
    if override and override.strip():
        return override.strip()
    if md_title:
        return md_title
    stem = Path(pdf_path).stem
    return re.sub(r'[_-]+', ' ', stem).strip().title() or 'Presentation'


PLAN_VERSION = 1
BLOCK_TYPES = {'para', 'quote', 'sub', 'list', 'table', 'image', 'price', 'steps', 'meta', 'chapter'}
MAX_PLAN_SECTIONS = 3000
# Condensed and Detailed send the chosen text to the LLM, whose free tier is rate-limited per minute
MAX_AI_WORDS = int(os.environ.get('MAX_AI_WORDS', 30000))


def plan_from_markdown(md_path, mode='standard', title=None, pdf_path='document.pdf', progress=None):
    """
    Everything before slides are made: parse, repair spacing, recognise what each piece is and let
    the planner arrange it. Returns a JSON-safe plan: the deck title, the document kind and the
    sections (image paths relative to the Markdown folder). Nothing here depends on the theme.
    """
    progress = progress or (lambda msg: None)
    md_path = Path(md_path)
    md_title, sections = load_sections(md_path)
    # Letter-spaced / run-together words from the PDF ("P R O D U C T", "BUILTFORASTRONGER")
    md_title, sections = text_repair.repair_sections(md_title, sections, progress)
    title = deck_title(md_title, pdf_path, title)
    if len(sections) == 1 and sections[0][0] == 'Overview':
        sections = [(title, sections[0][1])]  # a document with no headings: its slides carry its title
    # What kind of document is this, and what is each piece (a product, a price, a process)?
    doc_meta = semantics.read_doc_meta(md_path.read_text(encoding='utf-8'))
    sections = semantics.structure(sections)
    kind = semantics.classify(doc_meta, sections)
    # With an LLM available, let it review the arrangement; on any failure the rules' plan stands
    kind, sections, planned_by = planner.refine(title, doc_meta, sections, kind, progress)
    try:  # a small record of how the deck was planned, for the app to report
        (md_path.parent / 'plan.json').write_text(json.dumps({'kind': kind, 'planner': planned_by}))
    except OSError:
        pass
    log.info('%s: %d sections, %d words', title, len(sections), sum(word_count(b) for _, b in sections))
    base = md_path.parent.resolve()
    boxes = _image_boxes(base)
    return {
        'v': PLAN_VERSION, 'title': title, 'kind': kind, 'planner': planned_by,
        'meta': {k: doc_meta.get(k, '') for k in ('kicker', 'subtitle', 'pages', 'ocr_pages')},
        'sections': [{'name': name, 'blocks': [_portable(b, base, boxes) for b in blocks]} for name, blocks in sections],
    }


def _image_boxes(base):
    from pdf_to_md import BOXES_FILE
    try:
        return json.loads((Path(base) / BOXES_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _portable(block, base, boxes=None):
    """A block for the plan: image paths relative to the Markdown folder, with where on which page the
    picture is (so the build step can cut it from the PDF without reading the whole PDF again)."""
    if block['type'] == 'image':
        try:
            rel = Path(block['path']).resolve().relative_to(base).as_posix()
        except ValueError:
            return {**block, 'path': ''}
        box = (boxes or {}).get(Path(rel).stem)
        return {**block, 'path': rel, **({'box': box} if box else {})}
    return block


def _clean(value, depth=0):
    """Plain JSON data only (str, number, bool, list, dict), size-capped: a plan comes back from the browser."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return value[:20000]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_clean(v, depth + 1) for v in value[:2000]]
    if isinstance(value, dict):
        return {str(k)[:40]: _clean(v, depth + 1) for k, v in list(value.items())[:40]}
    return None


def sections_from_plan(plan, md_dir, selected=None):
    """
    The plan's sections, ready to build: checked, with image paths resolved inside md_dir only,
    and limited to the selected section ids (None = all). A chapter whose opening section is left
    out but whose later sections are kept still gets its divider slide.
    """
    if not isinstance(plan, dict) or plan.get('v') != PLAN_VERSION or not isinstance(plan.get('sections'), list):
        raise ValueError('This section list is out of date. Please upload the PDF again.')
    base = Path(md_dir).resolve()
    raw = plan['sections'][:MAX_PLAN_SECTIONS]
    sections = []
    for s in raw:
        if not isinstance(s, dict) or not isinstance(s.get('blocks'), list):
            raise ValueError('This section list could not be read. Please upload the PDF again.')
        blocks = []
        for b in _clean(s['blocks']):
            if not isinstance(b, dict) or b.get('type') not in BLOCK_TYPES:
                continue
            if b['type'] == 'image':
                p = (base / str(b.get('path') or '')).resolve()
                if base not in p.parents or not p.is_file():
                    continue  # never read outside this conversion's own files
                b = {**b, 'path': str(p)}
            blocks.append(b)
        sections.append((str(s.get('name') or 'Section')[:200], blocks))
    if selected is None:
        return [(n, b) for n, b in sections if b]
    keep = {i for i in selected if isinstance(i, int)}
    out, chapter_at = [], None
    for i, (name, blocks) in enumerate(sections):
        opens_chapter = bool(blocks) and blocks[0]['type'] == 'chapter'
        if opens_chapter:
            chapter_at = i
        if i in keep:
            if chapter_at is not None and chapter_at != i and chapter_at not in keep and \
                    not any(n == sections[chapter_at][0] for n, _ in out):
                out.append((sections[chapter_at][0], [sections[chapter_at][1][0]]))  # divider only
            out.append((name, blocks))
    return [(n, b) for n, b in out if b]


def outline(plan, md_dir):
    """What the section picker shows: per section its title, chapter, layout, a preview and a slide estimate."""
    from deck_builder import estimate_slides
    sections = sections_from_plan(plan, md_dir)
    by_name = {}
    for n, b in sections:
        by_name.setdefault(n, b)
    counts = estimate_slides(sections)
    rows, group = [], None
    for i, ((name, blocks), slides) in enumerate(zip(sections, counts)):
        opens = bool(blocks) and blocks[0]['type'] == 'chapter'
        if opens:
            group = i
        body = semantics.content(blocks)
        meta = semantics.get_meta(blocks) or {}
        rows.append({
            'id': i, 'name': name, 'chapter': opens, 'group': group,
            'layout': _layout_label(meta.get('layout'), body),
            'preview': _preview(meta, body), 'words': word_count(body),
            'images': sum(1 for b in body if b['type'] == 'image'), 'slides': slides,
        })
    return rows


def _layout_label(layout, body):
    if layout == 'product':
        return 'Product'
    if layout == 'steps':
        return 'Steps'
    if any(b['type'] == 'table' for b in body):
        return 'Table'
    if body and all(b['type'] == 'image' for b in body):
        return 'Pictures'
    return ''


def _preview(meta, body, words=22):
    from md_parse import plain
    if meta.get('tagline'):
        return meta['tagline']
    for b in body:
        if b['type'] in ('para', 'quote'):
            text = plain(b['text'])
        elif b['type'] == 'list' and b['items']:
            text = '; '.join(plain(i) for i in b['items'][:3])
        elif b['type'] == 'steps':
            text = '; '.join(plain(i['title']) for i in b['items'][:4])
        elif b['type'] == 'price':
            text = f'{b.get("label") or "Price"}: {b["amount"]} {b.get("unit", "")}'.strip()
        else:
            continue
        cut = text.split()
        return ' '.join(cut[:words]) + ('…' if len(cut) > words else '')
    return ''


def build_from_plan(plan, md_dir, mode='standard', theme='coral', selected=None, progress=None):
    """Make the deck from a plan, keeping only the selected section ids (None = all)."""
    progress = progress or (lambda msg: None)
    slide_plan = get_plan(mode)
    sections = sections_from_plan(plan, md_dir, selected)
    if not sections:
        raise ValueError('Choose at least one section.')
    if slide_plan['uses_llm']:
        words = sum(word_count(b) for _, b in sections)
        if words > MAX_AI_WORDS:
            raise ValueError(
                f'{slide_plan["label"]} slides rewrite up to {MAX_AI_WORDS:,} words at a time (roughly '
                f'{MAX_AI_WORDS // 400}-{MAX_AI_WORDS // 250} pages), and the chosen sections have {words:,}. '
                'Untick some sections, or choose Standard, which has no such limit.')
    title, meta, kind = plan.get('title') or 'Presentation', plan.get('meta') or {}, plan.get('kind') or 'document'
    if slide_plan['mode'] == 'condensed':
        sections = condense.condense_sections(title, sections, slide_plan, progress)
    elif slide_plan['mode'] == 'detailed':
        sections = condense.expand_sections(title, sections, slide_plan, progress)
    progress('Building slides…')
    buffer, slide_total = build_deck(title, sections, theme=theme,
                                     subtitle=meta.get('kicker') or SUBTITLES[slide_plan['mode']],
                                     one_slide_per_section=slide_plan['one_slide_per_section'],
                                     agenda=slide_plan['agenda'], tagline=meta.get('subtitle', ''), kind=kind)
    return buffer, title, slide_total


def build_from_markdown(md_path, mode='standard', theme='coral', title=None, pdf_path='document.pdf',
                        progress=None):
    """Plan and build in one go, with every section."""
    plan = plan_from_markdown(md_path, mode, title, pdf_path, progress)
    return build_from_plan(plan, Path(md_path).parent, mode, theme, None, progress)


def read_pdf(pdf_path, work_dir, mode='standard', progress=None):
    """PDF -> Markdown and images in work_dir/markdown. Returns the Markdown path."""
    from pdf_to_md import convert_pdf
    progress = progress or (lambda msg: None)
    progress('Reading the PDF (layout, text, tables and images)…')
    return convert_pdf(pdf_path, Path(work_dir) / 'markdown', progress)


IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg'}


def _image_jobs(plan, md_dir, selected):
    """
    [(page, bbox, destination)] for every picture in the selected sections, or None when any of them
    cannot be cut from its page (no box recorded), in which case the PDF is read in full instead.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get('sections'), list):
        return None
    base = Path(md_dir).resolve()
    keep = None if selected is None else {i for i in selected if isinstance(i, int)}
    jobs = {}
    for i, s in enumerate(plan['sections'][:MAX_PLAN_SECTIONS]):
        if keep is not None and i not in keep or not isinstance(s, dict) or not isinstance(s.get('blocks'), list):
            continue
        for b in s['blocks']:
            if not isinstance(b, dict) or b.get('type') != 'image' or not b.get('path'):
                continue
            dest = (base / str(b['path'])).resolve()
            box = b.get('box')
            if base not in dest.parents or dest.suffix.lower() not in IMAGE_SUFFIXES:
                continue  # sections_from_plan drops it too
            if not (isinstance(box, list) and len(box) == 5 and isinstance(box[0], int)
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box[1:])):
                return None
            jobs[dest] = (box[0], tuple(box[1:]), dest)
    return list(jobs.values())


def plan_pdf(pdf_path, work_dir, mode='standard', title=None, progress=None, source_name=None):
    """Step 1 of 2: read the PDF and plan its sections. Returns (plan, outline rows, md_path)."""
    md_path = read_pdf(pdf_path, work_dir, mode, progress)
    plan = plan_from_markdown(md_path, mode, title, source_name or pdf_path, progress)
    return plan, outline(plan, md_path.parent), md_path


def build_pdf(pdf_path, work_dir, plan, mode='standard', theme='coral', selected=None, progress=None):
    """
    Step 2 of 2: build the deck from a plan made earlier. The plan holds the text, already repaired
    and arranged, so no reading or AI planning repeats: only the pictures of the chosen sections
    are cut again from their pages (milliseconds each, so a textbook builds as fast as a leaflet).
    A plan without picture positions falls back to reading the whole PDF again.
    Returns (pptx_path, md_path, title, slide_total).
    """
    from pdf_to_md import render_images
    md_dir = Path(work_dir) / 'markdown'
    jobs = _image_jobs(plan, md_dir, selected)
    if jobs is None:
        md_path = read_pdf(pdf_path, work_dir, mode, progress)
    else:
        if jobs:
            (progress or (lambda msg: None))(f'Cutting {len(jobs)} picture(s) from the PDF…')
            render_images(pdf_path, jobs)
        md_dir.mkdir(parents=True, exist_ok=True)
        md_path = md_dir / 'document.md'
    buffer, title, slide_total = build_from_plan(plan, md_path.parent, mode, theme, selected, progress)
    out = Path(work_dir) / (safe_filename(title) + '.pptx')
    out.write_bytes(buffer.getvalue())
    return out, md_path, title, slide_total


def convert(pdf_path, work_dir, mode='standard', theme='coral', title=None, progress=None, source_name=None):
    """Run every stage in one go (every section). Returns (pptx_path, md_path, title, slide_total)."""
    md_path = read_pdf(pdf_path, work_dir, mode, progress)
    buffer, title, slide_total = build_from_markdown(md_path, mode, theme, title, source_name or pdf_path, progress)
    out = Path(work_dir) / (safe_filename(title) + '.pptx')
    out.write_bytes(buffer.getvalue())
    return out, md_path, title, slide_total


def safe_filename(name):
    name = re.sub(r'[^\w\s-]', '', name).strip()
    return re.sub(r'\s+', '_', name)[:80] or 'presentation'
