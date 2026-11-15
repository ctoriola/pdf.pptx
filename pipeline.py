"""
End-to-end conversion: PDF -> Markdown (+images) -> sections -> [LLM] -> .pptx

    convert(pdf_path, work_dir, mode='standard', theme='coral', title=None, progress=print)
"""
import json
import logging
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


def build_from_markdown(md_path, mode='standard', theme='coral', title=None, pdf_path='document.pdf',
                        progress=None):
    progress = progress or (lambda msg: None)
    plan = get_plan(mode)
    md_title, sections = load_sections(md_path)
    # Letter-spaced / run-together words from the PDF ("P R O D U C T", "BUILTFORASTRONGER")
    md_title, sections = text_repair.repair_sections(md_title, sections, progress)
    title = deck_title(md_title, pdf_path, title)
    if len(sections) == 1 and sections[0][0] == 'Overview':
        sections = [(title, sections[0][1])]  # a document with no headings: its slides carry its title
    # What kind of document is this, and what is each piece (a product, a price, a process)?
    doc_meta = semantics.read_doc_meta(Path(md_path).read_text(encoding='utf-8'))
    sections = semantics.structure(sections)
    kind = semantics.classify(doc_meta, sections)
    # With an LLM available, let it review the arrangement; on any failure the rules' plan stands
    kind, sections, planned_by = planner.refine(title, doc_meta, sections, kind, progress)
    try:  # a small record of how the deck was planned, for the app to report
        (Path(md_path).parent / 'plan.json').write_text(json.dumps({'kind': kind, 'planner': planned_by}))
    except OSError:
        pass
    log.info('%s: %d sections, %d words', title, len(sections), sum(word_count(b) for _, b in sections))

    if plan['mode'] == 'condensed':
        sections = condense.condense_sections(title, sections, plan, progress)
    elif plan['mode'] == 'detailed':
        sections = condense.expand_sections(title, sections, plan, progress)

    progress('Building slides…')
    buffer, slide_total = build_deck(title, sections, theme=theme,
                                     subtitle=doc_meta.get('kicker') or SUBTITLES[plan['mode']],
                                     one_slide_per_section=plan['one_slide_per_section'], agenda=plan['agenda'],
                                     tagline=doc_meta.get('subtitle', ''), kind=kind)
    return buffer, title, slide_total


def convert(pdf_path, work_dir, mode='standard', theme='coral', title=None, progress=None, source_name=None):
    """Run every stage. Returns (pptx_path, md_path, title, slide_total)."""
    from pdf_to_md import convert_pdf

    progress = progress or (lambda msg: None)
    work_dir = Path(work_dir)
    progress('Reading the PDF (layout, text, tables and images)…')
    md_path = convert_pdf(pdf_path, work_dir / 'markdown', progress, uses_llm=get_plan(mode)['uses_llm'])
    buffer, title, slide_total = build_from_markdown(md_path, mode, theme, title, source_name or pdf_path, progress)
    out = work_dir / (safe_filename(title) + '.pptx')
    out.write_bytes(buffer.getvalue())
    return out, md_path, title, slide_total


def safe_filename(name):
    name = re.sub(r'[^\w\s-]', '', name).strip()
    return re.sub(r'\s+', '_', name)[:80] or 'presentation'
