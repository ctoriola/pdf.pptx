"""
The AI planner: a second opinion on how the document should be arranged into slides.

semantics.py decides by rules; this module shows an LLM a compact outline (section titles and
what each block is, with only its first few words) and lets it adjust the arrangement:

  - the document kind
  - a layout for a section the rules left alone ('steps', 'product'), or 'text' to undo one
  - merging a fragment section into the one before it
  - a title for a section whose title is generic ("Overview") or repeated

The LLM arranges; it never writes or edits content. Every suggestion is validated against
the section's actual blocks and ignored if it cannot be carried out. If the LLM is missing,
slow, rate-limited or answers nonsense, the rules' result is used unchanged.
"""
import logging
import re

import condense
import semantics
from md_parse import plain, word_count

log = logging.getLogger(__name__)

KINDS = ('brochure', 'handbook', 'report', 'paper', 'slides', 'document')
LAYOUT_CHOICES = ('steps', 'product', 'text')
MAX_OUTLINE_CHARS = 14000   # keeps one request well inside free-tier per-minute token limits
MAX_BLOCKS_SHOWN = 9
MAX_MERGE_SHARE = 0.4       # a plan that merges more than this share of sections is not trusted
GENERIC_TITLES = {'overview', 'introduction', 'untitled', 'section', 'technical data', 'details', 'notes'}

SYSTEM = (
    'You are a presentation designer. You are given the outline of a document that is being turned into '
    'slides, and you decide how its existing pieces are arranged. You never write, summarise or change the '
    'content itself. Reply with a single JSON object and nothing else.'
)

PROMPT = """Document title: {title}
Pages: {pages}. The rules-based planner classified it as: {kind}.

Below is the outline. Each section will become one or more slides. For every section you see its id,
its title, the layout the rules chose (if any) and its blocks in order, described by type.

{outline}

Decide the following and return JSON:
{{"kind": "...", "sections": [{{"id": 0, ...}}]}}

"kind": one of brochure (a few designed pages selling products or services), handbook (chapters of
instructions), report, paper, slides (already a slide deck), document (anything else).

"sections": include ONLY sections that need a change. For each, any of:
- "layout": "steps"   when the section is a sequence of steps or stages: several sub-headings, each
                      followed by a short paragraph. Not for ordinary sub-topics that are not a sequence.
- "layout": "product" when the section presents one product or service with a picture and a
                      label/value table.
- "layout": "text"    when the rules chose a layout that does not suit the section.
- "merge_into_previous": true   when the section is only a fragment of the section before it (for
                      example a "Technical Data" or "Specifications" table that belongs to the product
                      above it). Never merge sections that are separate topics.
- "title": "..."      ONLY when the current title is generic or repeated (such as "Overview"). At most
                      8 words, describing what the section's own text is about.

If the rules' arrangement is already right, return {{"kind": "...", "sections": []}}."""


# ============================================================
# Outline
# ============================================================
def _first_words(text, n=9):
    words = plain(text).split()
    return ' '.join(words[:n]) + ('…' if len(words) > n else '')


def describe_block(block):
    kind = block['type']
    if kind in ('para', 'quote'):
        return f'{kind} ({len(block["text"].split())} words): {_first_words(block["text"])}'
    if kind == 'sub':
        return f'sub-heading: {plain(block["text"])[:70]}'
    if kind == 'list':
        style = 'numbered' if block.get('ordered') else 'bullet'
        return f'{style} list ({len(block["items"])} items): {_first_words(block["items"][0], 7) if block["items"] else ""}'
    if kind == 'table':
        rows = block['rows']
        shape = 'label/value' if block.get('kv') else 'data'
        return f'table {len(rows)}x{max(len(r) for r in rows)} ({shape}): {" | ".join(plain(c)[:24] for c in rows[0][:4])}'
    if kind == 'image':
        return 'image' + (f': {block["caption"][:60]}' if block.get('caption') else '')
    if kind == 'price':
        return f'price: {block["amount"]} {block.get("unit", "")}'.strip()
    if kind == 'steps':
        return f'steps ({len(block["items"])}): ' + '; '.join(plain(i['title'])[:30] for i in block['items'][:6])
    return kind


def build_outline(sections):
    """(text, ids shown): one entry per section, truncated to MAX_OUTLINE_CHARS."""
    lines, shown, size = [], [], 0
    for n, (name, blocks) in enumerate(sections):
        body = semantics.content(blocks)
        head = f'[{n}] "{name}"'
        if blocks and blocks[0]['type'] == 'chapter':
            head += ' (chapter opener)'
        if semantics.layout_of(blocks):
            head += f' layout={semantics.layout_of(blocks)}'
        entry = [head] + [f'    - {describe_block(b)}' for b in body[:MAX_BLOCKS_SHOWN]]
        if len(body) > MAX_BLOCKS_SHOWN:
            entry.append(f'    - … {len(body) - MAX_BLOCKS_SHOWN} more blocks')
        text = '\n'.join(entry)
        if size + len(text) > MAX_OUTLINE_CHARS:
            break
        lines.append(text)
        shown.append(n)
        size += len(text) + 1
    return '\n'.join(lines), shown


# ============================================================
# Applying a plan
# ============================================================
def _is_generic(name, all_names):
    key = re.sub(r'\s*\((cont\.|figures|table)\)$', '', name.strip().lower())
    return key in GENERIC_TITLES or sum(1 for n in all_names if n.strip().lower() == name.strip().lower()) > 1


def _clean_title(text, blocks, vocabulary=''):
    """
    Accept a suggested title only if it is short and made of the document's own vocabulary:
    the section's text plus the document's other headings ("Answers" under the chapter
    "BLS for Adults" may become "Adult BLS Answers", but not something invented).
    """
    title = re.sub(r'\s+', ' ', str(text or '')).strip().strip('"')
    if not 3 <= len(title) <= 70 or len(title.split()) > 8:
        return None
    source = set(re.findall(r'[a-z0-9]{3,}', (vocabulary + ' ' + ' '.join(
        plain(b.get('text', '')) + ' ' + ' '.join(b.get('items', []) if b['type'] == 'list' else [])
        for b in blocks)).lower()))
    source |= {w[:-1] for w in source if w.endswith('s')} | {w + 's' for w in source}  # adults ~ adult
    words = re.findall(r'[a-z0-9]{4,}', title.lower())
    if words and sum(w in source for w in words) < len(words) * 0.5:
        return None  # mostly words that do not occur in the section: an invented title
    return title


def _apply_layout(name, blocks, layout):
    """Return re-laid-out sections for (name, blocks), or None if the layout cannot be carried out."""
    head = [b for b in blocks if b['type'] == 'chapter']
    meta = semantics.get_meta(blocks) or {}
    body = semantics.content(blocks)
    if layout == 'text':
        if not meta.get('layout'):
            return None
        body = [({'type': 'para', 'text': meta['tagline']}) for _ in [0] if meta.get('tagline')] + body
        flat = []
        for b in body:
            if b['type'] == 'steps':  # back to sub-headings and paragraphs
                for item in b['items']:
                    flat += [{'type': 'sub', 'text': item['title']}, {'type': 'para', 'text': item['text']}]
            else:
                flat.append(b)
        return [(name, semantics.set_meta(head + flat, kicker=meta.get('kicker', '')))]
    if meta.get('layout'):
        return None  # the rules already chose; only 'text' overrides that
    if layout == 'steps':
        converted = semantics.find_steps(body, need_numbers=False)
        if not any(b['type'] == 'steps' for b in converted):
            return None
        parts = semantics._split_around_steps(name, converted)
        parts[0] = (parts[0][0], head + parts[0][1])
        return parts
    if layout == 'product':
        marked = [({**b, 'kv': True} if b['type'] == 'table' and max(len(r) for r in b['rows']) == 2 else b)
                  for b in body]
        images = [b for b in marked if b['type'] == 'image']
        tables = [b for b in marked if b['type'] == 'table']
        if not images or len(tables) != 1 or not tables[0].get('kv') \
                or len(tables[0]['rows']) > semantics.PRODUCT_MAX_SPEC_ROWS \
                or any(b['type'] in ('steps', 'list') for b in marked):
            return None
        return [(name, semantics._lift_kicker_and_tagline(head + marked))]
    return None


def apply_plan(plan, sections, kind):
    """Carry out the validated parts of a plan. Returns (kind, sections, number of changes applied)."""
    if not isinstance(plan, dict):
        return kind, sections, 0
    new_kind = plan.get('kind') if plan.get('kind') in KINDS else kind
    wanted = {}
    for item in plan.get('sections') or []:
        if isinstance(item, dict) and isinstance(item.get('id'), int) and 0 <= item['id'] < len(sections):
            wanted[item['id']] = item
    merges = [i for i, w in wanted.items() if w.get('merge_into_previous') is True]
    if len(merges) > max(1, len(sections) * MAX_MERGE_SHARE):
        log.warning('Planner wanted to merge %d of %d sections; ignoring its merges', len(merges), len(sections))
        merges = []
    names = [n for n, _ in sections]
    out, changes = [], 0
    for i, (name, blocks) in enumerate(sections):
        want = wanted.get(i, {})
        body = semantics.content(blocks)
        if i in merges and out and not (blocks and blocks[0]['type'] == 'chapter') \
                and not semantics.layout_of(blocks):
            prev_name, prev_blocks = out[-1]
            prev_meta = semantics.get_meta(prev_blocks) or {}
            if prev_meta.get('layout') in (None, 'product'):
                # re-run the rules on the joined section: a product may now be complete
                joined = [b for b in prev_blocks if b['type'] != 'meta']
                if prev_meta.get('tagline'):
                    at = 1 if joined and joined[0]['type'] == 'chapter' else 0
                    joined.insert(at, {'type': 'para', 'text': prev_meta['tagline']})
                if prev_meta.get('kicker'):
                    at = 1 if joined and joined[0]['type'] == 'chapter' else 0
                    joined.insert(at, {'type': 'sub', 'text': prev_meta['kicker']})
                joined += [{'type': 'sub', 'text': name}] + body
                out[-1:] = semantics.structure([(prev_name, joined)])
                changes += 1
                continue
        if want.get('title') and _is_generic(name, names):
            title = _clean_title(want['title'], body, ' '.join(names))
            if title:
                name, changes = title, changes + 1
        if want.get('layout') in LAYOUT_CHOICES:
            parts = _apply_layout(name, blocks, want['layout'])
            if parts:
                out += parts
                changes += 1
                continue
        out.append((name, blocks))
    if new_kind != kind:
        changes += 1
    return new_kind, out, changes


def _total_words(sections):
    """Words in every block, including the kicker and tagline a meta block carries."""
    total = 0
    for _, blocks in sections:
        total += word_count(blocks)
        meta = semantics.get_meta(blocks) or {}
        total += len(str(meta.get('kicker', '')).split()) + len(str(meta.get('tagline', '')).split())
    return total


# ============================================================
# Entry point
# ============================================================
def refine(title, doc_meta, sections, kind, progress=None):
    """
    Ask the LLM to review the arrangement. Returns (kind, sections, source) where source is
    'ai' if its plan was applied (even with no changes) or 'rules' if the rules' result stands
    because no LLM is configured or the request failed.
    """
    if not condense.llm_available() or not sections:
        return kind, sections, 'rules'
    try:
        outline, shown = build_outline(sections)
        if not shown:
            return kind, sections, 'rules'
        if progress:
            progress('Planning the slide structure…')
        reply = condense.chat_json(SYSTEM, PROMPT.format(title=title or 'Untitled', pages=doc_meta.get('pages') or '?',
                                                         kind=kind, outline=outline), max_tokens=900, retries=2)
        new_kind, new_sections, changes = apply_plan(reply, sections, kind)
        if not new_sections or _total_words(new_sections) < _total_words(sections) * 0.95:
            raise ValueError('the plan lost content')  # belt and braces: arranging never removes text
        log.info('AI planner: kind=%s, %d change(s) over %d of %d sections', new_kind, changes, len(shown),
                 len(sections))
        return new_kind, new_sections, 'ai'
    except Exception as exc:  # any failure: the rules' plan stands
        log.warning('AI planner unavailable (%s); using the rules-based plan', exc)
        return kind, sections, 'rules'
