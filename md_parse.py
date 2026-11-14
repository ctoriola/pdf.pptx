"""
Markdown -> sections of typed blocks (adapted from PitchPerfectAI's deck_builder parser).

The document is split on '## ' headings. A heading written '## Title {.chapter}' starts a
chapter: its section begins with a {'type': 'chapter'} marker and gets a divider slide.
Each section body becomes a list of blocks:
  {'type': 'para',  'text': str}
  {'type': 'quote', 'text': str}                       a fully emphasised pull-quote
  {'type': 'sub',   'text': str}                       ### to ###### headings
  {'type': 'list',  'items': [str], 'ordered': bool,
                    'labels': [str|None], 'levels': [int]}   '6.' / 'a.' / None (bullet); 0 or 1
  {'type': 'table', 'rows': [[str]]}
  {'type': 'image', 'path': str, 'caption': str}       ![alt](path)
"""
import html
import re

IMAGE_RE = re.compile(r'^!\[(?P<alt>[^\]]*)\]\((?P<path>[^)\s]+)(?:\s+"[^"]*")?\)$')
LIST_RE = re.compile(r'^\s*([-*+•▪●]|\d+[.)])\s+')
CAPTION_RE = re.compile(r'^(fig(ure)?\.?|image|chart|photo|diagram|graph|exhibit|plate)\s*[\dA-Z]', re.I)
CHAPTER_MARK = '{.chapter}'
GLUED_NUMBER_RE = re.compile(r'^(\d{1,2})[.)]\s*(?=[A-Z])')
SUB_LABEL_RE = re.compile(r'^([a-zA-Z]|[ivx]{2,4})[.)]\s+(?=\S)')
TERMINAL_RE = re.compile(r'[.!?:;)\]”"’\']$')


def parse_markdown(markdown):
    """Return (document_title, [(section_title, [blocks])])."""
    text = html.unescape((markdown or '').replace('\r', ''))
    text = re.sub(r'<!--.*?-->', '', text, flags=re.S)  # image placeholders, HTML comments
    text = re.sub(r'(?m)^[ \t]*>[ \t]?', '', text)      # blockquote markers
    title = None
    m = re.search(r'^#\s+(.+?)\s*#*\s*$', text, re.M)
    if m:
        title = plain(clean_inline(m.group(1)))
        text = text[:m.start()] + text[m.end():]

    sections, current = [], None
    for line in text.split('\n'):
        m = re.match(r'^##\s+(.+?)\s*#*\s*$', line)
        if m:
            name = m.group(1)
            chapter = name.endswith(CHAPTER_MARK)
            name = plain(clean_inline(name[:-len(CHAPTER_MARK)] if chapter else name))
            current = [name, [], chapter]
            sections.append(current)
        elif current is not None:
            current[1].append(line)
        elif line.strip():
            if not sections or sections[0][0] != 'Overview':
                sections.insert(0, ['Overview', [], False])
                current = sections[0]
            sections[0][1].append(line)
    out = []
    for name, lines, chapter in sections:
        if name:
            blocks = parse_blocks('\n'.join(lines))
            out.append((name, ([{'type': 'chapter'}] if chapter else []) + blocks))
    return title, out


def parse_blocks(body):
    blocks, para = [], []

    def flush():
        if para:
            blocks.append(paragraph_block(' '.join(para)))
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
            text = plain(clean_inline(re.sub(r'^#{3,6}\s+', '', stripped)))
            if text and not STRAY_LABEL_RE.match(text):
                blocks.append({'type': 'sub', 'text': text})
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
            block, i = parse_list(lines, i)
            blocks.append(block)
            continue
        else:
            para.append(clean_inline(stripped))
        i += 1
    flush()
    return drop_stray_labels(attach_captions(merge_broken_paragraphs(blocks)))


def parse_list(lines, i):
    """
    Parse one list starting at lines[i]; returns (block, next index).
    PDF converters put blank lines between items, indent them unreliably and break long
    items in two, so: blank lines don't end the list, an item that stops mid-sentence
    absorbs the next one, and only "a." / "b." sub-points (or bullets under a numbered
    item) count as nested.
    """
    items, labels, levels = [], [], []
    ordered = None
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and j - i <= 3 and LIST_RE.match(lines[j]):
                i = j
                continue
            break
        m = LIST_RE.match(line)
        if not m:
            if items and (line.startswith((' ', '\t')) or not TERMINAL_RE.search(items[-1])):
                items[-1] += ' ' + clean_inline(line)  # wrapped continuation of an item
                i += 1
                continue
            break
        marker, text = m.group(1), clean_inline(line[m.end():])
        numeric = marker[0].isdigit()
        if ordered is None:
            ordered = numeric
        label, level = (marker[:-1] + '.' if numeric else None), 0
        glued = None if numeric else GLUED_NUMBER_RE.match(text)
        if glued:  # "- 4.If you cannot..." is item 4 of a numbered list
            numeric, label, text = True, glued.group(1) + '.', text[glued.end():]
            if not items:
                ordered = True
        if not numeric:
            sub = SUB_LABEL_RE.match(text)
            if sub:
                label, text, level = sub.group(1) + '.', text[sub.end():], 1
            elif ordered:
                level = 1  # a bullet under a numbered item
            elif items and not TERMINAL_RE.search(items[-1]) and text[:1].islower():
                items[-1] += ' ' + text  # the previous bullet was cut mid-sentence
                i += 1
                continue
        elif not ordered:
            break  # a numbered item ends a bullet list
        if text:
            items.append(text)
            labels.append(label)
            levels.append(level)
        i += 1
    return {'type': 'list', 'items': items, 'ordered': bool(ordered), 'labels': labels, 'levels': levels}, i


QUOTE_RE = re.compile(r'^(?:\*{1,3}|_{1,3}){1,2}(?P<text>[^*_].*?[^*_])(?:\*{1,3}|_{1,3}){1,2}$')


def paragraph_block(text):
    """A paragraph, or a pull-quote when the whole paragraph is one emphasised sentence."""
    m = QUOTE_RE.match(text)
    if m and not CAPTION_RE.match(m.group('text')) and '**' not in m.group('text') and 8 <= len(m.group('text').split()) <= 45 \
            and m.group('text')[-1] in '.!?”"':
        return {'type': 'quote', 'text': m.group('text')}
    return {'type': 'para', 'text': text}


def _unfinished(block):
    return block['type'] == 'para' and len(block['text']) > 40 and not TERMINAL_RE.search(plain(block['text']))


def _continues(block):
    return block['type'] == 'para' and plain(block['text'])[:1].islower()


def merge_broken_paragraphs(blocks):
    """
    Rejoin a paragraph that was cut in two (by a page break, or by a pull-quote or figure the
    text wraps around): the first half stops mid-sentence and the second starts in lower case.
    """
    out, i = [], 0
    while i < len(blocks):
        b = blocks[i]
        if _unfinished(b):
            if i + 1 < len(blocks) and _continues(blocks[i + 1]):
                blocks[i + 1] = {'type': 'para', 'text': b['text'] + ' ' + blocks[i + 1]['text']}
                i += 1
                continue
            if i + 2 < len(blocks) and blocks[i + 1]['type'] in ('quote', 'image') and _continues(blocks[i + 2]):
                blocks[i + 2] = {'type': 'para', 'text': b['text'] + ' ' + blocks[i + 2]['text']}
                blocks[i], blocks[i + 1] = blocks[i + 1], None  # the quote/figure now leads
                out.append(blocks[i])
                i += 2
                continue
        out.append(b)
        i += 1
    return out


def _caption(block):
    """The caption text if this paragraph looks like 'Figure 3: ...' (italics allowed), else None."""
    if block['type'] != 'para':
        return None
    text = plain(block['text']).strip('_* ')
    return text if CAPTION_RE.match(text) and len(text) <= 300 else None


STRAY_LABEL_RE = re.compile(r'^(chapter\s*\d*|(fig(ure)?\.?|table)\s*\d+\w?\.?)$', re.I)


def drop_stray_labels(blocks):
    """Drop leftover labels: "CHAPTER 1" beside a chapter title, a "Figure 3" whose image is gone."""
    return [b for b in blocks if not (b['type'] == 'para' and STRAY_LABEL_RE.match(plain(b['text']).strip()))]


def _title_line(block):
    """A short, fully italic line such as "_Adult Chain of Survival_" set above a figure."""
    if block['type'] != 'para':
        return None
    m = re.fullmatch(r'[_*](?![_*])(.{3,80}?)[_*]', block['text'].strip())
    return m.group(1).strip() if m and m.group(1)[-1] not in '.!?' else None


def attach_captions(blocks):
    """Move a 'Figure 3: ...' paragraph, or an italic title line above it, into an image's caption."""
    out = []
    for b in blocks:
        prev = out[-1] if out else None
        if prev and prev['type'] == 'image' and not prev['caption'] and _caption(b):
            prev['caption'] = _caption(b)
            continue
        if b['type'] == 'image' and not b['caption'] and prev:
            caption = _caption(prev) or _title_line(prev)
            if caption:
                out.pop()
                b = {**b, 'caption': caption}
        out.append(b)
    return out


HTML_BREAK_RE = re.compile(r'<br\s*/?>', re.I)
HTML_TAG_RE = re.compile(r'</?(?:sup|sub|b|i|u|em|strong|span|p|div|font|small|mark)(?:\s[^>]*)?>', re.I)


def clean_inline(text):
    """
    Undo Markdown escaping that PDF converters add (\\_ \\* \\#), drop inline HTML they emit
    (<br> inside table cells, <sup>, <span>), and collapse whitespace.
    """
    text = text or ''
    if '<' in text:
        text = HTML_TAG_RE.sub(' ', HTML_BREAK_RE.sub(' ', text))
        text = re.sub(r' +([,.;:!?)])', r'\1', text)
    text = re.sub(r'\\([_*#\[\]()|`>~-])', r'\1', text)
    return re.sub(r'\s+', ' ', text).strip()


EMPHASIS_RE = re.compile(r'\*{1,3}|_{1,3}')


def inline_runs(text):
    """Split Markdown inline text into [(text, bold, italic)]; handles nested _**both**_."""
    text = text or ''
    runs, buf, bold, italic, pos = [], [], False, False, 0

    def emit():
        if buf:
            runs.append((''.join(buf), bold, italic))
            buf.clear()

    for m in EMPHASIS_RE.finditer(text):
        token, start, end = m.group(0), m.start(), m.end()
        before = text[start - 1] if start else ' '
        after = text[end] if end < len(text) else ' '
        underscore = token[0] == '_'
        can_open = not after.isspace() and not (underscore and before.isalnum())
        can_close = not before.isspace() and not (underscore and after.isalnum())
        want_bold, want_italic = len(token) >= 2, len(token) in (1, 3)
        closes = can_close and (not want_bold or bold) and (not want_italic or italic)
        opens = can_open and not (want_bold and bold) and not (want_italic and italic) \
            and text.find(token[0] * len(token), end) != -1
        if not closes and not opens:
            continue  # a literal character: snake_case, 5 * 3
        buf.append(text[pos:start])
        emit()
        if closes:
            bold, italic = bold and not want_bold, italic and not want_italic
        else:
            bold, italic = bold or want_bold, italic or want_italic
        pos = end
    buf.append(text[pos:])
    emit()
    return [r for r in runs if r[0]] or [('', False, False)]


def plain(text):
    return ''.join(t for t, _, _ in inline_runs(text))


def list_labels(block):
    """Per-item labels: the block's own ('6.', 'a.', None) or 1., 2., 3. for a plain ordered list."""
    labels = block.get('labels')
    if labels and len(labels) == len(block['items']):
        return labels
    return [f'{n}.' if block.get('ordered') else None for n in range(1, len(block['items']) + 1)]


def list_levels(block):
    levels = block.get('levels')
    return levels if levels and len(levels) == len(block['items']) else [0] * len(block['items'])


def slice_list(block, start, stop=None):
    """A copy of a list block holding items[start:stop], keeping their labels and levels."""
    return {**block, 'items': block['items'][start:stop], 'labels': list_labels(block)[start:stop],
            'levels': list_levels(block)[start:stop]}


def word_count(blocks):
    n = 0
    for b in blocks:
        if b['type'] in ('para', 'sub', 'quote'):
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
        if b['type'] in ('para', 'quote'):
            out.append(b['text'])
        elif b['type'] == 'sub':
            out.append('### ' + b['text'])
        elif b['type'] == 'list':
            out.append('\n'.join(('   ' * level) + (f'{label} ' if label else '- ') + it
                                 for it, label, level in zip(b['items'], list_labels(b), list_levels(b))))
        elif b['type'] == 'table':
            rows = b['rows']
            ncols = max(len(r) for r in rows)
            lines = ['| ' + ' | '.join(r + [''] * (ncols - len(r))) + ' |' for r in rows]
            lines.insert(1, '|' + '---|' * ncols)
            out.append('\n'.join(lines))
        elif b['type'] == 'image' and b.get('caption'):
            out.append(f'[Figure: {b["caption"]}]')
    return '\n\n'.join(out)
