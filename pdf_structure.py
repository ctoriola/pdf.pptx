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
TOC_HEADING_RE = re.compile(r'^\W*(table\W*(of\W*)?)?contents\W*$', re.I)
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


def _join_lines(parts):
    """Join the lines of a heading; a line ending in a hyphen continues without a space."""
    text = ''
    for part in parts:
        text += part if text.endswith('-') else (' ' + part if text else part)
    return re.sub(r'\s+', ' ', text).strip()


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
        return {'body_size': 0, 'chapters': {}, 'cover_title': '', 'acronyms': set(), 'page_titles': {}}
    body = sizes.most_common(1)[0][0]

    def display(text, size):
        return size >= body * DISPLAY_RATIO and len(re.findall(r'[A-Za-z]', text)) >= 3

    def runs_at(lines, chapter_size):
        """Runs of adjacent lines at the chapter size on one page, each with any subtitle under it."""
        runs, last_y = [], None
        for text, size, y in lines:
            if not re.search(r'[A-Za-z]{2}', text):
                continue
            near = last_y is not None and 0 <= y - last_y <= chapter_size * 1.6
            if display(text, size) and abs(size - chapter_size) <= chapter_size * 0.15:
                if near:
                    runs[-1].append(text)
                else:
                    runs.append([text])
                last_y = y
            elif near and chapter_size * 0.6 <= size < chapter_size * 0.85:
                runs[-1].append(text)  # a subtitle set just under the title: "(1 TO PUBERTY)"
                last_y = y
        return runs

    # The chapter size: the display size found on the most pages (at least two, at most half),
    # used for exactly one heading on each of them. A size that heads several blocks on one page
    # is a section heading, and one-off cover lettering never recurs.
    pages_by_size = defaultdict(set)
    for n, lines in enumerate(per_page):
        for text, size, _ in lines:
            if display(text, size):
                pages_by_size[round(size)].add(n)
    chapters = {}
    candidates = [(len(pages), size) for size, pages in pages_by_size.items()
                  if 2 <= len(pages) <= max(2, len(per_page) * MAX_CHAPTER_PAGE_SHARE)
                  and all(len(runs_at(per_page[n], size)) == 1 for n in pages)]
    if candidates:
        _, chapter_size = max(candidates)
        for n, lines in enumerate(per_page):
            runs = runs_at(lines, chapter_size)
            title = _join_lines(runs[0]) if len(runs) == 1 else ''
            if 3 <= len(title) <= 120 and not TOC_HEADING_RE.match(title):
                chapters[n] = title

    # The title: the first run of large lines on page 1 (it stops at the first normal-size line,
    # so on a document without a cover page the headings further down are not swept in)
    cover_title = ''
    if per_page and per_page[0]:
        biggest = max(size for _, size, _ in per_page[0])
        if biggest >= body * 1.5:
            threshold, parts, last_y = max(biggest * 0.45, body * 1.5), [], None
            for text, size, y in per_page[0]:
                if size >= threshold and re.search(r'[A-Za-z]{2}', text) \
                        and (last_y is None or y - last_y <= size * 2.2):
                    parts.append(text)
                    last_y = y
                elif parts and len(text) > 3:
                    break
            cover_title = _join_lines(list(dict.fromkeys(parts)))
            if not 3 <= len(cover_title) <= 100:
                cover_title = ''
    # The cover and a title page repeating it are not chapters
    cover_words = set(_words(cover_title))
    for n in [k for k in chapters if k <= 2]:
        if n == 0 or (cover_words and set(_words(chapters[n])) <= cover_words):
            del chapters[n]
    acronyms = {w for w, c in caps_words.items() if c >= 2}
    # Each page's main heading: its largest display line(s), when one size clearly leads
    page_titles = {}
    for n, lines in enumerate(per_page):
        # (short product names count here: "MS-5" has only two letters)
        big = [(size, text) for text, size, _ in lines
               if size >= body * DISPLAY_RATIO and re.search(r'[A-Za-z]', text) and len(text.strip()) >= 2
               and not MONEY_RE.search(text)]
        if big:
            top = max(size for size, _ in big)
            title = _join_lines([text for size, text in big if size >= top * 0.92])
            if 2 <= len(title) <= 90 and not MONEY_RE.search(title):
                page_titles[n] = title
    return {'body_size': body, 'chapters': chapters, 'cover_title': cover_title, 'acronyms': acronyms,
            'page_titles': page_titles}


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
MONEY_RE = re.compile(r'(?:[₦$€£¥₹]|\b(?:NGN|USD|EUR|GBP|KES|GHS|ZAR)\b)\s?\d[\d,.]*|\b\d[\d,.]*\s?(?:NGN|USD|EUR|GBP|naira|dollars)\b', re.I)
PRICE_LABEL_RE = re.compile(r'^\W*(price|pricing|cost|fee|from|starting at|only)\W*$', re.I)
IMAGE_LINE_RE = re.compile(r'^\s*!\[[^\]]*\]\([^)]*\)\s*$')


def strip_picture_text(text):
    """
    Remove the text the converter found inside pictures: it repeats what the image shows
    (diagram labels, "A B C" photo tags). The exception is a text box drawn as artwork that
    carries real content, which no image can replace: a price panel ("PRICE", "₦4,000,000 per
    unit"). That text is kept as a paragraph and the picture of the box is dropped instead.
    """
    lines = text.split('\n')

    def repl(m):
        inner = re.sub(r'<!--.*?-->|\*\*-{3,}[^*]*-{3,}\*\*', '', m.group(0), flags=re.S)
        inner = re.sub(r'\s+', ' ', re.sub(r'<br\s*/?>', ' ', inner)).strip()
        if len(inner) <= 60 and (MONEY_RE.search(inner) or PRICE_LABEL_RE.match(inner)):
            return f'\n\n{KEPT_MARK}{inner}\n\n'
        return ''
    text = PICTURE_TEXT_RE.sub(repl, text)
    # the image just above a kept text box is a picture of that same box
    out = []
    for line in text.split('\n'):
        if line.startswith(KEPT_MARK):
            k = len(out) - 1
            while k >= 0 and not out[k].strip():
                k -= 1
            if k >= 0 and IMAGE_LINE_RE.match(out[k]):
                del out[k]
            line = line[len(KEPT_MARK):]
        out.append(line)
    return '\n'.join(out)


KEPT_MARK = '\x00kept:'


def cover_extras(text, title):
    """(subtitle, kicker) from a cover page's text: its descriptive sentence and its small label."""
    title_key = re.sub(r'\W+', '', title or '').lower()
    subtitle = kicker = ''
    for line in text.split('\n'):
        raw = _strip_markup(HEADING_RE.sub(lambda m: m.group(2), line.strip()))
        if not raw or IMAGE_LINE_RE.match(line) or re.sub(r'\W+', '', raw).lower() == title_key:
            continue
        words = raw.split()
        if sum(len(w) == 1 for w in words) > len(words) / 2:
            continue  # letter-spaced logo lettering: "E I G E R  S E C U R I T Y"
        if len(words) >= 6 and len(raw) <= 220 and len(raw) > len(subtitle):
            subtitle = raw
        elif not kicker and 2 <= len(words) <= 6 and len(raw) <= 48:
            kicker = raw
    return subtitle, kicker


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


DESIGNED_MAX_PAGES = 16


def _promote_page_title(text, title):
    """Make the heading that matches the page's main title a top-level heading."""
    want = _words(title)
    lines = text.split('\n')
    for k, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and _words(m.group(2)) == want:
            lines[k] = '# ' + m.group(2)
            break
    return '\n'.join(lines)


def recover_missed_images(doc, chunks, img_dir, dpi=150):
    """
    The layout model sometimes fails to see a picture (a product photo inside a framed panel).
    Any sizeable region of embedded images that it did not mark as a picture is rendered from
    the page and referenced in that page's text at its reading position.
    """
    import pymupdf
    for n, chunk in enumerate(chunks):
        page = doc[n]
        area = abs(page.rect)
        rects = []
        for info in page.get_image_info():
            r = pymupdf.Rect(info['bbox']) & page.rect
            if r.width >= 24 and r.height >= 24 and abs(r) < area * 0.85:
                rects.append(r)
        regions = []
        for r in sorted(rects, key=lambda r: -abs(r)):  # merge overlapping placements into regions
            for k, reg in enumerate(regions):
                if abs(reg & r) > 0:
                    regions[k] = reg | r
                    break
            else:
                regions.append(r)
        boxes = chunk.get('page_boxes') or []
        # already captured: as pictures (one, or several that together cover the region) or as a table
        seen = [pymupdf.Rect(b['bbox']) for b in boxes if b.get('class') in ('picture', 'table', 'formula')]
        text, inserts = chunk.get('text', ''), []
        for k, reg in enumerate(regions):
            if abs(reg) < area * 0.03 or sum(abs(reg & p) for p in seen) >= abs(reg) * 0.4:
                continue
            above = [b for b in boxes if b.get('pos') and b['bbox'][3] <= reg.y0 + 4
                     and b.get('class') not in ('page-header', 'page-footer')]
            at = max((b['pos'][1] for b in above), default=0)
            path = f'{img_dir}/missed-{n + 1:04d}-{k:02d}.png'
            page.get_pixmap(clip=reg, dpi=dpi).save(path)
            inserts.append((at, f'\n\n![]({path})\n\n'))
        for at, ref in sorted(inserts, reverse=True):
            text = text[:at] + ref + text[at:]
        chunk['text'] = text
    return chunks


def assemble(page_texts, info):
    """
    Join per-page Markdown into one document: figures' inner text removed, contents/legal/cover
    pages dropped, and each chapter opened by a clean '## Title {.chapter}' heading.
    """
    chapters = info.get('chapters', {})
    acronyms = info.get('acronyms', set())
    first_chapter = min(chapters) if chapters else None
    # A short document without chapters is laid out page by page (a brochure, a one-pager): each
    # page's main heading opens a section, whatever heading level the converter gave it
    page_titles = info.get('page_titles', {})
    designed = not chapters and len(page_texts) <= DESIGNED_MAX_PAGES
    out, current_words = [], set()
    for n, raw in enumerate(page_texts):
        text = _drop_number_only_headings(strip_picture_text(raw))
        words = len(re.sub(r'!\[[^\]]*\]\([^)]*\)', '', text).split())
        if is_toc_page(text) or is_legal_page(text):
            continue
        bare_cover = (first_chapter is not None and n < first_chapter and words < 60) or \
            (first_chapter is None and n == 0 and len(page_texts) > 3 and words < 40)
        if bare_cover:
            # cover and title pages; a real preface (60+ words) is kept. Their tagline is saved.
            if n == 0:
                info['cover_subtitle'], info['cover_kicker'] = cover_extras(text, info.get('cover_title', ''))
            continue
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
        elif designed and n in page_titles:
            text = _promote_page_title(text, page_titles[n])
        out.append(text)
    return '\n\n'.join(out)
