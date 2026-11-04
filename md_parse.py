"""
Markdown -> sections of typed blocks (adapted from PitchPerfectAI's deck_builder parser).

The document is split on '## ' headings. Each section body becomes a list of blocks:
  {'type': 'para',  'text': str}
  {'type': 'sub',   'text': str}                       ### to ###### headings
  {'type': 'list',  'items': [str], 'ordered': bool}
  {'type': 'table', 'rows': [[str]]}
  {'type': 'image', 'path': str, 'caption': str}       ![alt](path)
"""
import html
import re

IMAGE_RE = re.compile(r'^!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\s]+)(?:\s+"[^"]*")?\)$')
LIST_RE = re.compile(r'^\s*([-*+•▪●]|\d+[.)])\s+')
CAPTION_RE = re.compile(r'^(fig(ure)?\.?|image|chart|photo|diagram|graph|exhibit|plate)\s*[\dA-Z]', re.I)


def parse_markdown(markdown):
    """Return (document_title, [(section_title, [blocks])])."""
    text = html.unescape((markdown or '').replace('\r', ''))
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)  # docling image placeholders, comments
    title = None
    m = re.search(r'^#\s+(.+?)\s*#*\s*$', text, re.M)
    if m:
        title = clean_inline(m.group(1))
        text = text[:m.start()] + text[m.end():]

    sections, current = [], None
    for line in text.split('\n'):
        m = re.match(r'^##\s+(.+?)\s*#*\s*$', line)
        if m:
            current = [clean_inline(m.group(1)), []]
            sections.append(current)
        elif current is not None:
            current[1].append(line)
        elif line.strip():
            if not sections or sections[0][0] != 'Overview':
                sections.insert(0, ['Overview', []])
                current = sections[0]
            sections[0][1].append(line)
    return title, [(name, parse_blocks('\n'.join(lines))) for name, lines in sections if name]


def parse_blocks(body):
    blocks, para = [], []

    def flush():
        if para:
            blocks.append({'type': 'para', 'text': ' '.join(para)})
            para.clear()

    lines = body.split('\n')
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        image = IMAGE_RE.match(stripped)
        if not stripped:
            flush()
        elif image:
            flush()
            alt = image.group('alt').strip()
            blocks.append({'type': 'image', 'path': image.group('path'),
                           'caption': '' if alt.lower() in ('', 'image', 'picture') else alt})
        elif re.match(r'^#{3,6}\s+', stripped):
            flush()
            blocks.append({'type': 'sub', 'text': clean_inline(re.sub(r'^#{3,6}\s+', '', stripped))})
        elif stripped.startswith('|'):
            flush()
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                cells = [clean_inline(c.strip()) for c in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                blocks.append({'type': 'table', 'rows': rows})
            continue
        elif LIST_RE.match(stripped):
            flush()
            ordered = bool(re.match(r'^\d+[.)]\s+', stripped))
            items = []
            while i < len(lines) and lines[i].strip():
                line = lines[i]
                if LIST_RE.match(line):
                    items.append(clean_inline(LIST_RE.sub('', line)))
                elif items and line.startswith((' ', '\t')):
                    items[-1] += ' ' + clean_inline(line)  # wrapped continuation of an item
                else:
                    break
                i += 1
            blocks.append({'type': 'list', 'items': items, 'ordered': ordered})
            continue
        else:
            para.append(clean_inline(stripped))
        i += 1
    flush()
    return attach_captions(blocks)


def attach_captions(blocks):
    """Move a 'Figure 3: ...' paragraph next to an image into that image's caption."""
    out = []
    for b in blocks:
        prev = out[-1] if out else None
        if (b['type'] == 'para' and prev and prev['type'] == 'image' and not prev['caption']
                and CAPTION_RE.match(b['text']) and len(b['text']) <= 300):
            prev['caption'] = b['text']
            continue
        if (b['type'] == 'image' and not b['caption'] and prev and prev['type'] == 'para'
                and CAPTION_RE.match(prev['text']) and len(prev['text']) <= 300):
            b = {**b, 'caption': out.pop()['text']}
        out.append(b)
    return out


def clean_inline(text):
    """Undo Markdown escaping that docling adds (\\_ \\* \\#) and collapse whitespace."""
    text = re.sub(r'\\([_*#\[\]()|`>~-])', r'\1', text or '')
    return re.sub(r'\s+', ' ', text).strip()


def plain(text):
    return re.sub(r'\*\*(.+?)\*\*|\*(.+?)\*|__(.+?)__', lambda m: next(g for g in m.groups() if g), text or '')


def inline_runs(text):
    """Split Markdown inline text into [(text, bold, italic)]."""
    runs, pos = [], 0
    for m in re.finditer(r'\*\*(.+?)\*\*|__(.+?)__|(?<![*\w])\*(?!\s)(.+?)\*(?![*\w])', text or ''):
        if m.start() > pos:
            runs.append((text[pos:m.start()], False, False))
        runs.append((m.group(1) or m.group(2) or m.group(3), bool(m.group(1) or m.group(2)), bool(m.group(3))))
        pos = m.end()
    if pos < len(text or ''):
        runs.append((text[pos:], False, False))
    return runs or [('', False, False)]


def word_count(blocks):
    n = 0
    for b in blocks:
        if b['type'] in ('para', 'sub'):
            n += len(b['text'].split())
        elif b['type'] == 'list':
            n += sum(len(i.split()) for i in b['items'])
        elif b['type'] == 'table':
            n += sum(len(c.split()) for r in b['rows'] for c in r)
    return n


def blocks_to_markdown(blocks):
    """Render blocks back to Markdown (images omitted; they are re-attached after LLM calls)."""
    out = []
    for b in blocks:
        if b['type'] == 'para':
            out.append(b['text'])
        elif b['type'] == 'sub':
            out.append('### ' + b['text'])
        elif b['type'] == 'list':
            out.append('\n'.join((f'{n}. ' if b['ordered'] else '- ') + it for n, it in enumerate(b['items'], 1)))
        elif b['type'] == 'table':
            rows = b['rows']
            ncols = max(len(r) for r in rows)
            lines = ['| ' + ' | '.join(r + [''] * (ncols - len(r))) + ' |' for r in rows]
            lines.insert(1, '|' + '---|' * ncols)
            out.append('\n'.join(lines))
        elif b['type'] == 'image' and b.get('caption'):
            out.append(f'[Figure: {b["caption"]}]')
    return '\n\n'.join(out)
