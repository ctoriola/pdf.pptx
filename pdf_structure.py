"""
Document structure that the Markdown converter gets wrong or drops, recovered from the
PDF itself (font sizes and page contents):

  - chapter titles: the display-size headings (e.g. 46pt on a 10pt body) that open chapters.
    The layout model often scrambles them ("CHAPTER USE OF 3 AUTOMATED ...") or skips them.
  - the document title, from the cover page
  - pages that should not become slides: table of contents, copyright/legal, bare covers
  - text inside figures ("picture text"), which duplicates what the image already shows
"""
import re
from collections import Counter, defaultdict

CHAPTER_MARK = '{.chapter}'
DISPLAY_RATIO = 1.8        # a line this many times the body size is a display heading
MAX_CHAPTER_PAGE_SHARE = 0.5

PICTURE_TEXT_RE = re.compile(
    r'<!--\s*Start of picture text\s*-->.*?<!--\s*End of picture text\s*-->'
    r'|\*\*-{3,}\s*Start of picture text\s*-{3,}\*\*.*?\*\*-{3,}\s*End of picture text\s*-{3,}\*\*', re.S | re.I)
TOC_HEADING_RE = re.compile(r'^\W*(table\W*of\W*)?contents\W*$', re.I)
TOC_LINE_RE = re.compile(r'(\.\s?){3,}\s*\d{1,4}\s*\**\s*$|\s[–—-]\s*\d{1,4}\s*$')
LEGAL_RE = re.compile(r'all rights reserved|copyright\s*(©|\(c\))|©\s*(19|20)\d\d|no part of this (publication|book)', re.I)
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*#*\s*$')
SMALL_WORDS = {'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in', 'into', 'nor', 'of', 'on', 'or',
               'the', 'to', 'vs', 'via', 'with'}


def _words(text):
    return re.findall(r'[A-Za-z0-9]+', re.sub(r'<[^>]+>', ' ', text).lower())


def _strip_markup(text):
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', re.sub(r'[*_`]+', '', text)).strip()


# ============================================================
# Font analysis
# ============================================================
def page_lines(page):
    """
    [(text, size, y)] for the text lines of a page, top to bottom. A line mixing sizes (a
    chapter title sharing its line with a giant decorative numeral) is reported at the size
    most of its characters use, without the odd-sized spans.
    """
    out = []
    for block in page.get_text('dict').get('blocks', []):
        for line in block.get('lines', []):
            spans = [s for s in line.get('spans', []) if s.get('text', '').strip()]
            if not spans:
                continue
            by_size = Counter()
            for s in spans:
                by_size[round(s['size'])] += len(s['text'].strip())
            size = by_size.most_common(1)[0][0]
            main = [s for s in spans if abs(s['size'] - size) <= size * 0.15]
            text = re.sub(r'\s+', ' ', ' '.join(s['text'].strip() for s in main)).strip()
            out.append((text, float(size), min(s['origin'][1] for s in main)))
    return sorted(out, key=lambda item: item[2])


def analyze(doc):
    """
    Returns {'body_size', 'chapters': {page index: title}, 'cover_title', 'acronyms'}.
    Chapter titles are the display-size lines whose size recurs on the most pages (at least
    two, at most half the document, so slide-like PDFs with a big title on every page and
    one-off cover lettering are both ignored).
    """
    sizes, per_page, caps_words = Counter(), [], Counter()
    for page in doc:
        lines = page_lines(page)
        per_page.append(lines)
        for text, size, _ in lines:
            sizes[round(size)] += len(text)
            if not text.isupper():  # acronyms are capitals that appear inside normal sentences
                caps_words.update(re.findall(r'\b[A-Z][A-Z0-9]{1,5}\b', text))
    if not sizes:
        return {'body_size': 0, 'chapters': {}, 'cover_title': '', 'acronyms': set()}
    body = sizes.most_common(1)[0][0]

    def display(text, size):
        return size >= body * DISPLAY_RATIO and len(re.findall(r'[A-Za-z]', text)) >= 3

    pages_by_size = defaultdict(set)
    for n, lines in enumerate(per_page):
        for text, size, _ in lines:
            if display(text, size):
                pages_by_size[round(size)].add(n)
    chapters = {}
    candidates = [(len(pages), size) for size, pages in pages_by_size.items()
                  if 2 <= len(pages) <= max(2, len(per_page) * MAX_CHAPTER_PAGE_SHARE)]
    if candidates:
        _, chapter_size = max(candidates)
        for n, lines in enumerate(per_page):
            parts, last_y = [], None
            for text, size, y in lines:
                if display(text, size) and abs(size - chapter_size) <= chapter_size * 0.15:
                    parts.append(text)
                    last_y = y
                elif last_y is not None and 0 < y - last_y <= chapter_size * 1.6 \
                        and chapter_size * 0.6 <= size < chapter_size * 0.85 and re.search(r'[A-Za-z]{2}', text):
                    parts.append(text)  # a subtitle line set just under the title: "(1 TO PUBERTY)"
                    last_y = y
            title = re.sub(r'\s+', ' ', ' '.join(parts)).strip()
            if 3 <= len(title) <= 120 and not TOC_HEADING_RE.match(title):
                chapters[n] = title

    # The title: the first run of large lines on page 1 (it stops at the first normal-size line,
    # so on a document without a cover page the headings further down are not swept in)
    cover_title = ''
    if per_page and per_page[0]:
        biggest = max(size for _, size, _ in per_page[0])
        if biggest >= body * 1.5:
            threshold, parts = max(biggest * 0.45, body * 1.5), []
            for text, size, _ in per_page[0]:
                if size >= threshold and re.search(r'[A-Za-z]{2}', text):
                    parts.append(text)
                elif parts and len(text) > 3:
                    break
            cover_title = re.sub(r'\s+', ' ', ' '.join(dict.fromkeys(parts))).strip()
            if not 3 <= len(cover_title) <= 100:
                cover_title = ''
    acronyms = {w for w, c in caps_words.items() if c >= 2}
    return {'body_size': body, 'chapters': chapters, 'cover_title': cover_title, 'acronyms': acronyms}


def smart_title(text, acronyms=()):
    """Turn an ALL-CAPS heading into Title Case, keeping the document's own acronyms."""
    letters = re.sub(r'[^A-Za-z]', '', text)
    if len(letters) < 4 or not letters.isupper():
        return text
    # Loose kerning splits a word's first letters off: "A I RWAY" -> "AIRWAY"
    text = re.sub(r'\b((?:[A-Z] ){2,})([A-Z]{2,})\b', lambda m: m.group(1).replace(' ', '') + m.group(2), text)
    out = []
    for n, word in enumerate(text.split(' ')):
        core = re.sub(r'[^A-Za-z0-9]', '', word)
        if core in acronyms or (re.search(r'\d', core) and len(core) <= 6):
            out.append(word)
        elif n and core.lower() in SMALL_WORDS:
            out.append(word.lower())
        else:
            out.append('-'.join('/'.join(q[:1].upper() + q[1:].lower() if re.sub(r'[^A-Za-z0-9]', '', q) not in acronyms
                                         else q for q in part.split('/')) for part in word.split('-')))
    return ' '.join(out)


# ============================================================
# Page clean-up
# ============================================================
def strip_picture_text(text):
    return PICTURE_TEXT_RE.sub('', text)


def is_toc_page(text):
    lines = [l.strip() for l in text.split('\n') if l.strip() and not l.strip().startswith('![')]
    if not lines:
        return False
    if any((m := HEADING_RE.match(l)) and TOC_HEADING_RE.match(_strip_markup(m.group(2))) for l in lines[:6]):
        return True
    if TOC_HEADING_RE.match(_strip_markup(lines[0])):
        return True
    hits = sum(1 for l in lines if TOC_LINE_RE.search(_strip_markup(l)))
    return len(lines) >= 5 and hits >= len(lines) * 0.4


def is_legal_page(text):
    return bool(LEGAL_RE.search(text)) and len(text.split()) < 350


def _covered_by(heading, title_words, share=1.0):
    """True when a heading only repeats (part of) the chapter title, 'CHAPTER' and digits."""
    words = [w for w in _words(heading) if w != 'chapter' and not w.isdigit()]
    return bool(words) and sum(w in title_words for w in words) >= len(words) * share


def _drop_number_only_headings(text):
    return '\n'.join(l for l in text.split('\n')
                     if not ((m := HEADING_RE.match(l)) and not re.search(r'[A-Za-z]{2}', _strip_markup(m.group(2)))))


def assemble(page_texts, info):
    """
    Join per-page Markdown into one document: figures' inner text removed, contents/legal/cover
    pages dropped, and each chapter opened by a clean '## Title {.chapter}' heading.
    """
    chapters = info.get('chapters', {})
    acronyms = info.get('acronyms', set())
    first_chapter = min(chapters) if chapters else None
    out, current_words = [], set()
    for n, raw in enumerate(page_texts):
        text = _drop_number_only_headings(strip_picture_text(raw))
        words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
        if is_toc_page(text) or is_legal_page(text):
            continue
        if first_chapter is not None and n < first_chapter and words < 60:
            continue  # cover and title pages; a real preface (60+ words) is kept
        if first_chapter is None and n == 0 and len(page_texts) > 3 and words < 40:
            continue  # a bare cover page
        if n in chapters:
            current_words = set(_words(chapters[n]))
        if current_words:
            # Scrambled copies of the chapter title, and its running header on later pages
            kept = []
            for line in text.split('\n'):
                m = HEADING_RE.match(line)
                if m and n in chapters and _covered_by(m.group(2), current_words, 0.5):
                    continue  # a (possibly scrambled) copy of the title on the chapter's own page
                if m and _covered_by(m.group(2), current_words) \
                        and len(_words(m.group(2))) >= max(1, len(current_words) * 0.6):
                    continue  # the chapter's running header on a later page
                kept.append(line)
            text = '\n'.join(kept)
        if n in chapters:
            text = f'## {smart_title(chapters[n], acronyms)} {CHAPTER_MARK}\n\n' + text
        out.append(text)
    return '\n\n'.join(out)
