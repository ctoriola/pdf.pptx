"""
The meaning layer: work out what kind of document this is and what each piece of a section
*is*, so the deck builder can choose a layout that suits it instead of treating everything
as headings and paragraphs.

Recognised here (rules only, no AI; planner.py can refine the result):
  - the document kind: 'brochure', 'handbook' or 'document'
  - a kicker: the small label above a heading ("01 / TACTICAL SHOTGUN")
  - a tagline: the one-line description under a product name
  - a price: "PRICE / ₦4,000,000 per unit / Exclusive of VAT" -> {'type': 'price', ...}
  - a spec sheet: a two-column label/value table -> table['kv'] = True
  - a process: numbered step headings with a line each -> {'type': 'steps', 'items': [...]}
  - a product section: image + spec sheet + short copy -> layout 'product'

A section's findings travel in a {'type': 'meta', 'layout', 'kicker', 'tagline'} block placed
at the front of its blocks (after a chapter marker).
"""
import json
import re

from md_parse import plain, word_count
from pdf_structure import MONEY_RE, PRICE_LABEL_RE

LAYOUTS = ('product', 'steps')
KICKER_RE = re.compile(r'^\d{1,2}\s*[/|·]\s*\S')
PRICE_NOTE_RE = re.compile(r'^(excl|incl|plus\b|vat\b|tax|subject to|per\s|from\s|ex\.?\s|rrp)', re.I)
UNIT_RE = re.compile(r'\s*(?:(per\s+[A-Za-z]+(?:\s+[A-Za-z]+)?)|/\s*([A-Za-z]+))\s*$', re.I)
META_RE = re.compile(r'<!--\s*pdf2pptx\s+(\{.*?\})\s*-->', re.S)

BROCHURE_MAX_PAGES = 16
BROCHURE_MAX_WORDS_PER_PAGE = 260
PRODUCT_MAX_WORDS = 150
PRODUCT_MAX_SPEC_ROWS = 14
STEP_MAX_CHARS = 280


# ============================================================
# Meta block helpers
# ============================================================
def get_meta(blocks):
    return next((b for b in blocks[:2] if b['type'] == 'meta'), None)


def layout_of(blocks):
    meta = get_meta(blocks)
    return meta.get('layout') if meta else None


def set_meta(blocks, **values):
    """Return blocks with the meta block updated (created after a chapter marker if absent)."""
    values = {k: v for k, v in values.items() if v}
    meta = get_meta(blocks)
    if meta is not None:
        return [({**b, **values} if b is meta else b) for b in blocks]
    if not values:
        return list(blocks)
    at = 1 if blocks and blocks[0]['type'] == 'chapter' else 0
    return blocks[:at] + [{'type': 'meta', **values}] + blocks[at:]


def content(blocks):
    return [b for b in blocks if b['type'] not in ('meta', 'chapter')]


def read_doc_meta(markdown):
    """The {'pages', 'chapters', 'subtitle', 'kicker'} line pdf_to_md writes at the top of the Markdown."""
    m = META_RE.search(markdown or '')
    try:
        meta = json.loads(m.group(1)) if m else {}
    except ValueError:
        meta = {}
    return meta if isinstance(meta, dict) else {}


# ============================================================
# Document kind
# ============================================================
def classify(doc_meta, sections):
    """'brochure' (few designed pages: pictures, specs, little prose), 'handbook' (chapters) or 'document'."""
    pages = int(doc_meta.get('pages') or 0)
    if int(doc_meta.get('chapters') or 0) >= 2:
        return 'handbook'
    blocks = [b for _, bl in sections for b in bl]
    visuals = sum(b['type'] in ('image', 'table', 'price', 'steps') for b in blocks)
    words = sum(word_count(bl) for _, bl in sections)
    if 0 < pages <= BROCHURE_MAX_PAGES and words <= pages * BROCHURE_MAX_WORDS_PER_PAGE and visuals >= pages * 0.5:
        return 'brochure'
    return 'document'


# ============================================================
# Pieces
# ============================================================
def is_kv_table(block):
    """A spec sheet: two columns of label / value (no header row to style)."""
    rows = block.get('rows') or []
    if len(rows) < 2 or max(len(r) for r in rows) != 2:
        return False
    labels = [plain(r[0]) for r in rows if r]
    return all(len(l) <= 48 for l in labels) and sum(len(l) for l in labels) / len(labels) <= 34


def _is_price_text(text):
    text = plain(text).strip()
    return bool(MONEY_RE.search(text)) and len(text) <= 44 and len(text.split()) <= 7 and not text.endswith('.')


def find_prices(blocks):
    """Fold "PRICE" / "₦4,000,000 per unit" / "Exclusive of VAT..." paragraphs into price blocks."""
    out, i = [], 0
    while i < len(blocks):
        b = blocks[i]
        if b['type'] == 'para' and _is_price_text(b['text']):
            label = 'Price'
            if out and out[-1]['type'] == 'para' and PRICE_LABEL_RE.match(plain(out[-1]['text'])):
                label = plain(out.pop()['text']).strip().title()
            amount = re.sub(r'([₦$€£¥₹])\s+(?=\d)', r'\1', plain(b['text']).strip())
            unit = ''
            m = UNIT_RE.search(amount)
            if m and MONEY_RE.search(amount[:m.start()]):
                unit, amount = (m.group(1) or f'per {m.group(2)}').lower(), amount[:m.start()].strip()
            note = ''
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt and nxt['type'] == 'para' and len(plain(nxt['text'])) <= 30 and UNIT_RE.fullmatch(' ' + plain(nxt['text'])):
                unit, i = plain(nxt['text']).strip().lower(), i + 1  # "per unit" on its own line
                nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt and nxt['type'] == 'para' and len(plain(nxt['text'])) <= 90 and PRICE_NOTE_RE.match(plain(nxt['text'])):
                note, i = plain(nxt['text']).strip(), i + 1
            out.append({'type': 'price', 'label': label, 'amount': amount, 'unit': unit, 'note': note,
                        **({'page': b['page']} if 'page' in b else {})})
        elif b['type'] == 'para' and PRICE_LABEL_RE.match(plain(b['text'])) and not (
                i + 1 < len(blocks) and blocks[i + 1]['type'] == 'para' and _is_price_text(blocks[i + 1]['text'])):
            pass  # a "PRICE" label whose amount was not found says nothing on its own
        else:
            out.append(b)
        i += 1
    return out


def _bare_number(block):
    return block['type'] == 'para' and re.fullmatch(r'\d{1,2}[.)]?', plain(block['text']).strip())


def find_steps(blocks, need_numbers=True):
    """
    Turn a run of step headings, each with a line or two of text, into one steps block. Designed
    pages draw the step numbers as badges, which extract as stray "1", "2" paragraphs in no
    reliable position; they are the evidence that this is a numbered process, then discarded.
    """
    numbers = sum(1 for b in blocks if _bare_number(b))
    seq = [b for b in blocks if not _bare_number(b)]
    best = None  # (start, end, items)
    i = 0
    while i < len(seq):
        if seq[i]['type'] != 'sub':
            i += 1
            continue
        items, j = [], i
        while j < len(seq) and seq[j]['type'] == 'sub':
            k = j + 1
            texts = []
            while k < len(seq) and seq[k]['type'] == 'para' and len(texts) < 2:
                texts.append(seq[k]['text'])
                k += 1
            if not texts or len(' '.join(texts)) > STEP_MAX_CHARS or len(seq[j]['text']) > 70:
                break
            items.append({'title': seq[j]['text'], 'text': ' '.join(texts)})
            j = k
        if len(items) >= 3 and (best is None or len(items) > len(best[2])):
            best = (i, j, items)
        i = max(j, i + 1)
    if not best or (need_numbers and numbers < len(best[2]) - 1):
        return blocks
    start, end, items = best
    return seq[:start] + [{'type': 'steps', 'items': items}] + seq[end:]


# ============================================================
# Sections
# ============================================================
def _split_around_steps(name, blocks):
    """
    A steps block gets a slide of its own, titled by the sub-heading just above it if there is
    one. What comes before stays with the section; what follows under a new sub-heading becomes
    its own section.
    """
    idx = next((n for n, b in enumerate(blocks) if b['type'] == 'steps'), None)
    if idx is None:
        return [(name, blocks)]
    before, after = blocks[:idx], blocks[idx + 1:]
    out, steps_name = [], name
    if before and before[-1]['type'] == 'sub' and content(before[:-1]):
        steps_name, before = plain(before[-1]['text']), before[:-1]
        out.append((name, before))
        before = []
    out.append((steps_name, set_meta(before + [blocks[idx]], layout='steps')))
    if after:
        if after[0]['type'] == 'sub':
            out.append((plain(after[0]['text']), after[1:] or after))
        else:
            out.append((f'{name} (cont.)', after))
    return out


def _product_parts(blocks):
    body = content(blocks)
    images = [b for b in body if b['type'] == 'image']
    tables = [b for b in body if b['type'] == 'table']
    prose = [b for b in body if b['type'] in ('para', 'quote', 'list')]
    return body, images, tables, prose


def is_product(blocks):
    """A product page: a picture, one spec sheet and a little copy (optionally a price)."""
    body, images, tables, prose = _product_parts(blocks)
    return (len(images) >= 1 and len(tables) == 1 and tables[0].get('kv')
            and 3 <= len(tables[0]['rows']) <= PRODUCT_MAX_SPEC_ROWS
            and word_count(prose) <= PRODUCT_MAX_WORDS
            and not any(b['type'] in ('steps', 'list') for b in body))


def _lift_kicker_and_tagline(blocks):
    """For a product section: a leading label becomes the kicker, a short first line the tagline."""
    body = content(blocks)
    head = [b for b in blocks if b['type'] in ('meta', 'chapter')]
    kicker = tagline = ''
    if body and body[0]['type'] == 'sub' and (KICKER_RE.match(body[0]['text']) or len(body[0]['text']) <= 40):
        kicker, body = plain(body[0]['text']), body[1:]
    # the tagline: a short first line, which may sit just after the photo
    first = next((b for b in body[:2] if b['type'] == 'para'), None)
    if first is not None and len(body) > 1:
        text = plain(first['text']).strip()
        if len(text) <= 110 and len(text.split()) <= 16 and not text.endswith(('.', ':')):
            tagline, body = text, [b for b in body if b is not first]
    # the spec sheet's own heading ("TECHNICAL DATA") is a label for the table, not a new topic
    for n, b in enumerate(body[:-1]):
        if b['type'] == 'sub' and body[n + 1]['type'] == 'table':
            body[n + 1] = {**body[n + 1], 'label': plain(b['text'])}
            body = body[:n] + body[n + 1:]
            break
    return set_meta(head + body, kicker=kicker, tagline=tagline, layout='product')


def structure(sections, kind='document'):
    """Apply the rules to every section. Returns new sections; some are split or re-labelled."""
    out = []
    for name, blocks in sections:
        blocks = [({**b, 'kv': True} if b['type'] == 'table' and is_kv_table(b) else b) for b in blocks]
        blocks = find_steps(find_prices(blocks))
        for part_name, part in _split_around_steps(name, blocks):
            if layout_of(part) != 'steps' and is_product(part):
                part = _lift_kicker_and_tagline(part)
            elif content(part) and content(part)[0]['type'] == 'sub' and KICKER_RE.match(content(part)[0]['text']):
                first = content(part)[0]
                part = set_meta([b for b in part if b is not first], kicker=plain(first['text']))
            out.append((part_name, part))
    return out
