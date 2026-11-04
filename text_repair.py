"""
Repair broken word spacing from PDF extraction, using the LLM.

PDFs with letter-spaced or tightly kerned type extract badly:
  "P R O D U C T C O D E"              (letters spread apart)
  "BUILTFORASTRONGERTOMORROW"          (words run together)
Only fragments that look broken are sent, in batches, and the LLM may only add or
remove spaces: any answer that changes a single other character is discarded and
the original kept. Without an LLM key the text is left as it is.
"""
import logging
import re

import condense

log = logging.getLogger(__name__)

# 4+ single characters separated by single spaces: "P R O D U C T"
LETTER_SPACED_RE = re.compile(r'(?<!\S)(?:[^\s] ){3,}[^\s](?!\S)')
# a run of 12+ capitals, or 20+ letters of any case, with no space: "BUILTFORASTRONGER"
GLUED_RE = re.compile(r'[A-Z]{12,}|[A-Za-z]{20,}')
URLISH_RE = re.compile(r'https?://\S+|www\.\S+|\S+@\S+\.\S+')

BATCH_CHARS = 3000
BATCH_ITEMS = 60

SYSTEM = ('You fix word spacing in text extracted from PDFs. You never change spelling, case, punctuation, '
          'numbers or word order. Reply with a single JSON object and nothing else.')

PROMPT = """Each item below is text from a PDF whose word spacing broke during extraction.
Some have letters spread apart ("P R O D U C T C O D E"), some have words run together
("BUILTFORASTRONGERTOMORROW"), some have both.

Rewrite each item with correct spacing between words. Rules:
- Only add or remove spaces. Every other character must stay exactly as it is, in the same order,
  including capitals, punctuation, brackets and **bold markers**.
- Keep product codes and model numbers together (DG-2, DHO-1, 5W-30, DSO-1).
- If an item is already correct, return it unchanged.

Return JSON: {{"fixed": ["...", "..."]}} with exactly {n} strings, in the same order.

ITEMS:
{items}"""


def needs_repair(text):
    if not text or len(text) < 4:
        return False
    stripped = URLISH_RE.sub('', text)
    return bool(LETTER_SPACED_RE.search(stripped) or GLUED_RE.search(stripped))


def _same_letters(a, b):
    return re.sub(r'\s+', '', a) == re.sub(r'\s+', '', b)


def repair_strings(strings, progress=None):
    """Return {original: repaired} for the strings that needed and passed repair."""
    todo = list(dict.fromkeys(s for s in strings if needs_repair(s)))
    if not todo or not condense.llm_available():
        return {}
    if progress:
        progress(f'Fixing word spacing in {len(todo)} lines…')
    fixes, batch, size = {}, [], 0

    def flush():
        if not batch:
            return
        items = '\n'.join(f'{n}. {s}' for n, s in enumerate(batch, 1))
        try:
            reply = condense.chat_json(SYSTEM, PROMPT.format(n=len(batch), items=items),
                                       max_tokens=min(4000, sum(len(s) for s in batch) + 400))
        except condense.LLMError as exc:
            log.warning('Spacing repair skipped for %d lines: %s', len(batch), exc)
            return
        fixed = reply.get('fixed') if isinstance(reply, dict) else None
        if not isinstance(fixed, list) or len(fixed) != len(batch):
            log.warning('Spacing repair returned %s items for %d', len(fixed or []), len(batch))
            return
        for original, new in zip(batch, fixed):
            new = re.sub(r'[ \t]+', ' ', str(new)).strip()
            if new and new != original and _same_letters(original, new):
                fixes[original] = new
            elif new != original:
                log.info('Rejected spacing repair that changed characters: %r -> %r', original[:60], new[:60])

    for s in todo:
        if batch and (size + len(s) > BATCH_CHARS or len(batch) >= BATCH_ITEMS):
            flush()
            batch, size = [], 0
        batch.append(s)
        size += len(s)
    flush()
    return fixes


def repair_sections(title, sections, progress=None):
    """Apply spacing repairs to the title, section names and every text field of every block."""
    strings = [title or ''] + [name for name, _ in sections]
    for _, blocks in sections:
        for b in blocks:
            if b['type'] in ('para', 'sub'):
                strings.append(b['text'])
            elif b['type'] == 'list':
                strings += b['items']
            elif b['type'] == 'table':
                strings += [c for row in b['rows'] for c in row]
            elif b['type'] == 'image':
                strings.append(b.get('caption', ''))
    fixes = repair_strings(strings, progress)
    if not fixes:
        return title, sections
    log.info('Repaired spacing in %d text fragments', len(fixes))
    fix = lambda s: fixes.get(s, s)  # noqa: E731
    out = []
    for name, blocks in sections:
        new_blocks = []
        for b in blocks:
            b = dict(b)
            if b['type'] in ('para', 'sub'):
                b['text'] = fix(b['text'])
            elif b['type'] == 'list':
                b['items'] = [fix(i) for i in b['items']]
            elif b['type'] == 'table':
                b['rows'] = [[fix(c) for c in row] for row in b['rows']]
            elif b['type'] == 'image':
                b['caption'] = fix(b.get('caption', ''))
            new_blocks.append(b)
        out.append((fix(name), new_blocks))
    return fix(title) if title else title, out
