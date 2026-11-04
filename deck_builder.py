"""
Stage 5: build a .pptx deck from parsed sections (ported from PitchPerfectAI's deck_builder).

Each section gets a layout chosen from its content: table, stat callouts, numbered
cards, lead + body text, text + image, or an image gallery. Body text is sized to
fit; in flowing decks (Standard, Detailed) overflow continues on "(cont.)" slides,
while one-slide-per-section decks (Condensed) shrink the text instead.
"""
import math
import re
import threading
import zipfile
from datetime import date
from io import BytesIO

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

from md_parse import inline_runs, plain


# ---------- Themes ----------
class Theme:
    FONT_KEYS = ('head_font', 'body_font')

    def __init__(self, key, name, **values):
        self.key, self.name = key, name
        for k, v in values.items():
            setattr(self, k, v if k in self.FONT_KEYS else RGBColor.from_string(v))


THEMES = {
    'coral': Theme(
        'coral', 'Coral',
        accent='D44C3F', accent_strong='B73A2F', accent_soft='F19C92', tint='FEF7F6',
        ink='111827', body='374151', muted='6B7280', line='E5E7EB', panel='F9FAFB',
        bg='FFFFFF', on_accent='FFFFFF', cover_bg='1F232B', cover_text='FFFFFF', cover_muted='C9CDD4',
        head_font='Calibri', body_font='Calibri'),
    'green': Theme(
        'green', 'Modern Green',
        accent='2C7A4B', accent_strong='1F5C38', accent_soft='97D4A9', tint='EEF8F1',
        ink='13241A', body='3A4A40', muted='6B7A70', line='DDE7E0', panel='F5F9F6',
        bg='FFFFFF', on_accent='FFFFFF', cover_bg='173D28', cover_text='FFFFFF', cover_muted='B9D3C2',
        head_font='Cambria', body_font='Calibri'),
    'red': Theme(
        'red', 'Bold Red',
        accent='E5383B', accent_strong='FF6B6B', accent_soft='FF9A9C', tint='2A1718',
        ink='FFFFFF', body='D6D8DC', muted='9CA0A8', line='33363D', panel='1E2026',
        bg='131418', on_accent='FFFFFF', cover_bg='0B0B0D', cover_text='FFFFFF', cover_muted='A3A6AD',
        head_font='Arial', body_font='Arial'),
}
CHAR_EM = {'Calibri': 0.47, 'Arial': 0.52}  # average character width (ems), for text fitting

DEFAULT_THEME = 'coral'
T = THEMES[DEFAULT_THEME]  # active theme while a deck is being built (see build_deck)

# ---------- Geometry (16:9 widescreen, inches) ----------
SLIDE_W, SLIDE_H = 13.333, 7.5
MARGIN = 0.6
CONTENT_TOP = 1.65
CONTENT_BOTTOM = 6.75
CONTENT_H = CONTENT_BOTTOM - CONTENT_TOP
CONTENT_W = SLIDE_W - 2 * MARGIN
GAP = 0.35
CAPTION_H = 0.5

MAX_BODY_PT, MIN_BODY_PT = 18, 12
READABLE_PT = 16  # below this, continue on another slide instead
GALLERY_MAX = 4   # images per gallery slide
SIDE_IMAGES_MAX = 2
NUMBER_RE = re.compile(r'(?<![\w.])([$€£₦]?\d[\d,.]*\s?(?:%|[xX]\b|[KkMmBbTt]\b|bn\b|million\b|billion\b)?\+?)')


# ============================================================
# Text measurement (approximate)
# ============================================================
def text_height(blocks, width_in, size_pt):
    """Estimate rendered height (inches) of text blocks at a font size."""
    char_w = size_pt * CHAR_EM.get(T.body_font, 0.52) / 72
    line_h = size_pt * 1.2 / 72
    per_line = max(10, int(width_in / char_w))
    height = 0.0
    for b in blocks:
        if b['type'] == 'para':
            height += math.ceil(len(plain(b['text'])) / per_line) * line_h + size_pt * 0.6 / 72
        elif b['type'] == 'sub':
            height += line_h * 1.15 + size_pt * 0.9 / 72
        elif b['type'] == 'list':
            item_chars = max(10, per_line - 3)
            for item in b['items']:
                height += math.ceil(len(plain(item)) / item_chars) * line_h + size_pt * 0.35 / 72
            height += size_pt * 0.4 / 72
    return height


def fit_size(blocks, width_in, height_in, floor=MIN_BODY_PT):
    for size in range(MAX_BODY_PT, floor - 1, -1):
        if text_height(blocks, width_in, size) <= height_in:
            return size
    return None


def split_blocks(blocks, width_in, height_in):
    """Split blocks into pages that fit at a readable size (splitting long lists and paragraphs)."""
    size = READABLE_PT
    pages, page, expanded = [], [], []
    for b in blocks:
        if b['type'] == 'list' and text_height([b], width_in, size) > height_in * 0.6:
            for k in range(0, len(b['items']), 4):
                expanded.append({**b, 'items': b['items'][k:k + 4]})
        elif b['type'] == 'para' and text_height([b], width_in, size) > height_in * 0.6:
            chunk = ''
            for sentence in re.split(r'(?<=[.!?])\s+', b['text']):
                candidate = (chunk + ' ' + sentence).strip()
                if chunk and text_height([{'type': 'para', 'text': candidate}], width_in, size) > height_in * 0.5:
                    expanded.append({'type': 'para', 'text': chunk})
                    candidate = sentence
                chunk = candidate
            if chunk:
                expanded.append({'type': 'para', 'text': chunk})
        else:
            expanded.append(b)
    for b in expanded:
        if page and text_height(page + [b], width_in, size) > height_in:
            pages.append(page)
            page = []
        page.append(b)
    if page:
        pages.append(page)
    # A subheading shouldn't end a page
    for n in range(len(pages) - 1):
        if pages[n] and pages[n][-1]['type'] == 'sub' and len(pages[n]) > 1:
            pages[n + 1].insert(0, pages[n].pop())
    return pages


# ============================================================
# Drawing helpers
# ============================================================
def _rgb_hex(color):
    return '%02X%02X%02X' % (color[0], color[1], color[2])


def fill_background(slide, color):
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = color


def add_box(slide, x, y, w, h, color, shape=MSO_SHAPE.RECTANGLE, radius=None):
    shp = slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = color
    shp.line.fill.background()
    shp.shadow.inherit = False
    if radius is not None and shape == MSO_SHAPE.ROUNDED_RECTANGLE:
        shp.adjustments[0] = radius
    return shp


def _style(run, size, color, bold=False, italic=False, font=None):
    run.font.name = font or T.body_font
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    run.font.color.rgb = color


def add_text(slide, x, y, w, h, text, size, color=None, bold=False, italic=False,
             align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, font=None, markdown=False, line_spacing=None):
    color = color or T.body
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    p = tf.paragraphs[0]
    p.alignment = align
    if line_spacing:
        p.line_spacing = line_spacing
    for run_text, run_bold, run_italic in (inline_runs(text) if markdown else [(text, False, False)]):
        r = p.add_run()
        r.text = run_text
        _style(r, size, color, bold or run_bold, italic or run_italic, font)
    return box


def _bullet(paragraph, autonum=False, indent_in=0.3):
    """Give a paragraph a real bullet (hanging indent) in the accent colour."""
    pPr = paragraph._p.get_or_add_pPr()
    pPr.set('marL', str(Emu(Inches(indent_in))))
    pPr.set('indent', str(-Emu(Inches(indent_in))))
    for tag in ('a:buClr', 'a:buChar', 'a:buAutoNum', 'a:buNone', 'a:buFont'):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    clr = pPr.makeelement(qn('a:buClr'), {})
    clr.append(clr.makeelement(qn('a:srgbClr'), {'val': _rgb_hex(T.accent)}))
    pPr.append(clr)
    if autonum:
        pPr.append(pPr.makeelement(qn('a:buAutoNum'), {'type': 'arabicPeriod'}))
    else:
        pPr.append(pPr.makeelement(qn('a:buFont'), {'typeface': 'Arial'}))
        pPr.append(pPr.makeelement(qn('a:buChar'), {'char': '•'}))


def add_rich_text(slide, x, y, w, h, blocks, size):
    """Render para/sub/list blocks into one text box at the given size."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    first = True

    def new_par():
        nonlocal first
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        first = False
        p.line_spacing = 1.1
        return p

    for b in blocks:
        if b['type'] == 'para':
            p = new_par()
            p.space_after = Pt(size * 0.6)
            for t, bold, italic in inline_runs(b['text']):
                r = p.add_run(); r.text = t
                _style(r, size, T.ink if bold else T.body, bold, italic)
        elif b['type'] == 'sub':
            p = new_par()
            p.space_before = Pt(size * 0.4)
            p.space_after = Pt(size * 0.3)
            r = p.add_run(); r.text = plain(b['text'])
            _style(r, size + 2, T.accent_strong, True, font=T.head_font)
        elif b['type'] == 'list':
            for item in b['items']:
                p = new_par()
                p.space_after = Pt(size * 0.35)
                _bullet(p, autonum=b['ordered'])
                for t, bold, italic in inline_runs(item):
                    r = p.add_run(); r.text = t
                    _style(r, size, T.ink if bold else T.body, bold, italic)
            p.space_after = Pt(size * 0.75)
    return box


def _cell_borders(cell, color, width_pt=0.75):
    tcPr = cell._tc.get_or_add_tcPr()
    for tag in ('a:lnL', 'a:lnR', 'a:lnT', 'a:lnB'):
        for el in tcPr.findall(qn(tag)):
            tcPr.remove(el)
        ln = tcPr.makeelement(qn(tag), {'w': str(int(width_pt * 12700)), 'cap': 'flat', 'cmpd': 'sng', 'algn': 'ctr'})
        fill = ln.makeelement(qn('a:solidFill'), {})
        fill.append(fill.makeelement(qn('a:srgbClr'), {'val': _rgb_hex(color)}))
        ln.append(fill)
        ln.append(ln.makeelement(qn('a:prstDash'), {'val': 'solid'}))
        tcPr.insert(['a:lnL', 'a:lnR', 'a:lnT', 'a:lnB'].index(tag), ln)  # borders precede the fill


def _shrink_on_overflow(box):
    """Ask PowerPoint to shrink text that still doesn't fit its box."""
    bodyPr = box.text_frame._txBody.bodyPr
    for tag in ('a:noAutofit', 'a:spAutoFit', 'a:normAutofit'):
        for el in bodyPr.findall(qn(tag)):
            bodyPr.remove(el)
    bodyPr.append(bodyPr.makeelement(qn('a:normAutofit'), {}))


def number_badge(slide, x, y, d, label, fill=None, color=None, size=None):
    circle = add_box(slide, x, y, d, d, fill or T.accent, MSO_SHAPE.OVAL)
    tf = circle.text_frame
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    r = p.add_run(); r.text = label
    _style(r, size or d * 30, color or T.on_accent, True)
    return circle


def image_size(path):
    try:
        with Image.open(path) as im:
            return im.size
    except (OSError, ValueError):
        return None


def place_image(slide, path, x, y, w, h, caption=''):
    """Fit an image inside the box (keeping its proportions), centred, with an optional caption below."""
    size = image_size(path)
    if not size:
        return False
    img_h_avail = h - (CAPTION_H if caption else 0)
    iw, ih = size
    scale = min(w / iw, img_h_avail / ih)
    dw, dh = iw * scale, ih * scale
    dx = x + (w - dw) / 2
    dy = y + (img_h_avail - dh) / 2
    slide.shapes.add_picture(str(path), Inches(dx), Inches(dy), Inches(dw), Inches(dh))
    if caption:
        text = caption if len(caption) <= 160 else caption[:157].rstrip() + '…'
        add_text(slide, x, dy + dh + 0.1, w, CAPTION_H - 0.1, text, 11, T.muted, italic=True,
                 align=PP_ALIGN.CENTER)
    return True


def is_portrait(path):
    size = image_size(path)
    return bool(size) and size[1] > size[0] * 1.15


# ============================================================
# Slide builders
# ============================================================
class DeckBuilder:
    def __init__(self, deck_title, subtitle='Presentation', one_slide_per_section=False):
        self.prs = Presentation()
        self.prs.slide_width = Inches(SLIDE_W)
        self.prs.slide_height = Inches(SLIDE_H)
        self.blank = self.prs.slide_layouts[6]
        self.title = deck_title
        self.subtitle = subtitle
        self.page = 0
        self.one_slide_per_section = one_slide_per_section

    def _new_slide(self, notes=None):
        slide = self.prs.slides.add_slide(self.blank)
        self.page += 1
        if notes:
            slide.notes_slide.notes_text_frame.text = notes
        return slide

    def _chrome(self, slide, number, title):
        """Section badge + title + footer on a content slide."""
        fill_background(slide, T.bg)
        number_badge(slide, MARGIN, 0.62, 0.62, f'{number:02d}', size=15)
        size = 30 if len(title) <= 55 else 24 if len(title) <= 80 else 20
        add_text(slide, MARGIN + 0.85, 0.5, CONTENT_W - 0.85, 0.85, title, size, T.ink, bold=True,
                 anchor=MSO_ANCHOR.MIDDLE, font=T.head_font)
        add_text(slide, MARGIN, 7.0, 8, 0.3, self.title, 10, T.muted)
        add_text(slide, SLIDE_W - MARGIN - 1, 7.0, 1, 0.3, str(self.page), 10, T.muted, align=PP_ALIGN.RIGHT)

    # ---- cover / agenda / closing ----
    def cover(self):
        slide = self._new_slide()
        fill_background(slide, T.cover_bg)
        add_box(slide, 8.9, -1.2, 6.2, 6.2, T.accent, MSO_SHAPE.OVAL)
        add_box(slide, 10.6, 3.9, 4.2, 4.2, T.accent_strong, MSO_SHAPE.OVAL)
        add_box(slide, 8.2, 4.7, 1.3, 1.3, T.accent_soft, MSO_SHAPE.OVAL)
        add_text(slide, MARGIN + 0.1, 1.2, 7.6, 0.4, self.subtitle.upper(), 14, T.accent_soft, bold=True)
        n = len(self.title)
        size = 54 if n <= 22 else 44 if n <= 36 else 36 if n <= 60 else 28
        add_text(slide, MARGIN + 0.1, 1.75, 7.6, 4.2, self.title, size, T.cover_text, bold=True, font=T.head_font,
                 anchor=MSO_ANCHOR.TOP, line_spacing=0.95)
        add_text(slide, MARGIN + 0.1, 6.35, 7, 0.4, date.today().strftime('%B %Y'), 14, T.cover_muted)

    def agenda(self, section_names):
        slide = self._new_slide()
        fill_background(slide, T.bg)
        add_text(slide, MARGIN, 0.55, 4.4, 0.9, 'Agenda', 36, T.ink, bold=True, font=T.head_font)
        add_text(slide, MARGIN, 1.4, 4.4, 1.2, f'What we\'ll cover in the next {len(section_names)} sections.', 16,
                 T.muted)
        names = section_names
        per_col = math.ceil(len(names) / 2) if len(names) > 5 else len(names)
        col_w = 3.7 if len(names) > 5 else 6.6
        row_h = min(0.95, 5.6 / max(per_col, 1))
        badge = min(0.55, row_h - 0.1)
        name_size = 18 if row_h >= 0.8 else 15 if row_h >= 0.6 else 13
        for i, name in enumerate(names):
            col, row = divmod(i, per_col)
            x = 5.35 + col * (col_w + 0.35)
            y = 0.95 + row * row_h
            number_badge(slide, x, y + (row_h - badge) / 2, badge, f'{i + 1:02d}', fill=T.tint, color=T.accent_strong,
                         size=14 if badge >= 0.5 else 11)
            label = name if len(name) <= 48 else name[:45].rstrip() + '…'
            add_text(slide, x + badge + 0.2, y, col_w - badge - 0.25, row_h, label, name_size, T.ink, bold=True,
                     anchor=MSO_ANCHOR.MIDDLE)
        add_text(slide, MARGIN, 7.0, 8, 0.3, self.title, 10, T.muted)
        add_text(slide, SLIDE_W - MARGIN - 1, 7.0, 1, 0.3, str(self.page), 10, T.muted, align=PP_ALIGN.RIGHT)

    def closing(self):
        slide = self._new_slide()
        fill_background(slide, T.cover_bg)
        add_box(slide, -1.6, 3.6, 5.2, 5.2, T.accent, MSO_SHAPE.OVAL)
        add_box(slide, 11.4, -1.0, 3.0, 3.0, T.accent_strong, MSO_SHAPE.OVAL)
        add_text(slide, 3.0, 2.35, 7.33, 1.2, 'Thank you', 54, T.cover_text, bold=True, font=T.head_font,
                 align=PP_ALIGN.CENTER)
        name_size = 22 if len(self.title) <= 40 else 18
        add_text(slide, 2.5, 3.6, 8.33, 0.9, self.title, name_size, T.accent_soft, align=PP_ALIGN.CENTER)
        add_text(slide, 3.0, 4.65, 7.33, 0.5, 'Questions & discussion', 16, T.cover_muted, align=PP_ALIGN.CENTER)

    # ---- section content ----
    def section(self, number, name, blocks):
        notes = section_notes(name, blocks)
        images = [b for b in blocks if b['type'] == 'image' and image_size(b['path'])]
        text = [b for b in blocks if b['type'] != 'image']
        if not images:
            return self._text_section(number, name, text, notes)
        if not has_text(text):
            return self._gallery_slides(number, name, images, notes, continued=False)
        layout = choose_layout(text)
        if layout == 'table':
            self._table_slide(number, name, text, notes)
            return self._gallery_slides(number, name, images, notes)
        side = images[:SIDE_IMAGES_MAX] if self._side_fits(images[:SIDE_IMAGES_MAX], text) else images[:1]
        self._text_image_slides(number, name, text, side, notes)
        if images[len(side):]:
            self._gallery_slides(number, name, images[len(side):], notes)

    def _text_section(self, number, name, blocks, notes):
        layout = choose_layout(blocks)
        if layout == 'table':
            return self._table_slide(number, name, blocks, notes)
        if layout == 'stats':
            return self._stats_slide(number, name, blocks, notes)
        if layout == 'cards':
            return self._cards_slide(number, name, blocks, notes)
        return self._text_slides(number, name, blocks, notes)

    def _text_slides(self, number, name, blocks, notes):
        """Lead statement on the left, body on the right; continues if too long."""
        lead, rest = split_lead(blocks)
        height = CONTENT_H
        if self.one_slide_per_section and lead and not fit_size(rest, CONTENT_W - 4.55, height):
            lead, rest = None, blocks  # give the body the full width rather than overflow
        body_x = MARGIN + 4.55 if lead else MARGIN
        body_w = SLIDE_W - MARGIN - body_x
        if self.one_slide_per_section:
            size = (fit_size(rest, body_w, height) or MIN_BODY_PT) if rest else MAX_BODY_PT
            pages = [rest]
        else:
            size = fit_size(rest, body_w, height, READABLE_PT) if rest else MAX_BODY_PT
            pages = [rest] if size else split_blocks(rest, body_w, height)
        if not self.one_slide_per_section and len(pages) == 2 and \
                text_height(pages[1], body_w, READABLE_PT) < height * 0.3:
            tighter = fit_size(rest, body_w, height, READABLE_PT - 1)  # avoid a near-empty continuation
            if tighter:
                size, pages = tighter, [rest]
        if len(pages) > 1 and lead:
            # Continuation slides have no lead box, so re-flow them at full width
            pages = pages[:1] + split_blocks([b for pg in pages[1:] for b in pg], CONTENT_W, height)
        for n, page in enumerate(pages):
            slide = self._new_slide(notes)
            self._chrome(slide, number, name if n == 0 else f'{name} (cont.)')
            if lead and n == 0:
                add_box(slide, MARGIN, CONTENT_TOP, 4.15, height, T.tint, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
                lead_size = 26 if len(lead) < 110 else 22 if len(lead) < 180 else 19
                add_text(slide, MARGIN + 0.35, CONTENT_TOP + 0.35, 3.45, height - 0.7, lead, lead_size, T.ink,
                         bold=True, font=T.head_font, anchor=MSO_ANCHOR.MIDDLE, markdown=True, line_spacing=1.1)
            elif lead:
                body_x, body_w = MARGIN, CONTENT_W
            if page:
                box = add_rich_text(slide, body_x, CONTENT_TOP + 0.05, body_w, height, page, size or READABLE_PT)
                if self.one_slide_per_section and not fit_size(page, body_w, height):
                    _shrink_on_overflow(box)

    # ---- image layouts ----
    def _side_width(self, images):
        return 4.4 if all(is_portrait(i['path']) for i in images) else 5.6

    def _side_fits(self, images, text):
        """Two stacked images only when both are landscape and the text is short."""
        if len(images) < 2 or any(is_portrait(i['path']) for i in images):
            return False
        text_w = CONTENT_W - self._side_width(images) - GAP
        return fit_size(text, text_w, CONTENT_H, READABLE_PT) is not None

    def _text_image_slides(self, number, name, text, images, notes):
        """Text on the left, image(s) on the right; overflowing text continues at full width."""
        side_w = self._side_width(images)
        text_w = CONTENT_W - side_w - GAP
        side_x = MARGIN + text_w + GAP
        if self.one_slide_per_section:
            size = fit_size(text, text_w, CONTENT_H) or MIN_BODY_PT
            pages = [text]
        else:
            size = fit_size(text, text_w, CONTENT_H, READABLE_PT)
            pages = [text] if size else split_blocks(text, text_w, CONTENT_H)
            if len(pages) > 1:
                pages = pages[:1] + split_blocks([b for pg in pages[1:] for b in pg], CONTENT_W, CONTENT_H)
        for n, page in enumerate(pages):
            slide = self._new_slide(notes)
            self._chrome(slide, number, name if n == 0 else f'{name} (cont.)')
            if n == 0:
                slot_h = (CONTENT_H - GAP * (len(images) - 1)) / len(images)
                for k, img in enumerate(images):
                    place_image(slide, img['path'], side_x, CONTENT_TOP + k * (slot_h + GAP), side_w, slot_h,
                                img.get('caption', ''))
                width = text_w
            else:
                width = CONTENT_W
            box = add_rich_text(slide, MARGIN, CONTENT_TOP + 0.05, width, CONTENT_H, page, size or READABLE_PT)
            if self.one_slide_per_section and not fit_size(page, width, CONTENT_H):
                _shrink_on_overflow(box)

    def _gallery_slides(self, number, name, images, notes, continued=True):
        """Images in a grid: 1 full, 2 or 3 side by side, 4 as 2x2."""
        for start in range(0, len(images), GALLERY_MAX):
            group = images[start:start + GALLERY_MAX]
            slide = self._new_slide(notes)
            self._chrome(slide, number, f'{name} (figures)' if continued or start else name)
            cols = 2 if len(group) == 4 else len(group)
            rows = math.ceil(len(group) / cols)
            cell_w = (CONTENT_W - GAP * (cols - 1)) / cols
            cell_h = (CONTENT_H - GAP * (rows - 1)) / rows
            for k, img in enumerate(group):
                r, c = divmod(k, cols)
                place_image(slide, img['path'], MARGIN + c * (cell_w + GAP), CONTENT_TOP + r * (cell_h + GAP),
                            cell_w, cell_h, img.get('caption', ''))

    # ---- structured layouts ----
    def _cards_slide(self, number, name, blocks, notes):
        intro = [b for b in blocks if b['type'] in ('para', 'sub')]
        items = next(b for b in blocks if b['type'] == 'list')['items']
        slide = self._new_slide(notes)
        self._chrome(slide, number, name)
        top = CONTENT_TOP
        if intro:
            size = fit_size(intro, CONTENT_W, 1.3) or 14
            h = min(1.3, text_height(intro, CONTENT_W, size) + 0.05)
            add_rich_text(slide, MARGIN, top, CONTENT_W, h, intro, size)
            top += h + 0.3
        cols = len(items) if len(items) <= 3 else 2 if len(items) == 4 else 3
        rows = math.ceil(len(items) / cols)
        card_w = (CONTENT_W - GAP * (cols - 1)) / cols
        max_card_h = (CONTENT_BOTTOM - top - GAP * (rows - 1)) / rows
        text_w = card_w - 0.7
        size = min(fit_size([{'type': 'para', 'text': it}], text_w, max_card_h - 1.4) or MIN_BODY_PT for it in items)
        size = min(size, 20)
        needed = max(text_height([{'type': 'para', 'text': it}], text_w, size) for it in items)
        card_h = min(max_card_h, max(2.0, needed + 1.5))
        text_h = card_h - 1.15
        block_h = rows * card_h + GAP * (rows - 1)
        top += max(0, (CONTENT_BOTTOM - top - block_h) / 2 - 0.25)
        for i, item in enumerate(items):
            r, c = divmod(i, cols)
            x = MARGIN + c * (card_w + GAP)
            y = top + r * (card_h + GAP)
            add_box(slide, x, y, card_w, card_h, T.panel, MSO_SHAPE.ROUNDED_RECTANGLE, 0.08)
            number_badge(slide, x + 0.35, y + 0.35, 0.5, str(i + 1), size=14)
            add_text(slide, x + 0.35, y + 1.05, text_w, text_h, item, size, T.body, markdown=True, line_spacing=1.1)

    def _stats_slide(self, number, name, blocks, notes):
        intro = [b for b in blocks if b['type'] in ('para', 'sub')]
        items = next(b for b in blocks if b['type'] == 'list')['items']
        slide = self._new_slide(notes)
        self._chrome(slide, number, name)
        top = CONTENT_TOP
        if intro:
            size = fit_size(intro, CONTENT_W, 1.4) or 14
            h = min(1.4, text_height(intro, CONTENT_W, size) + 0.05)
            add_rich_text(slide, MARGIN, top, CONTENT_W, h, intro, size)
            top += h + 0.35
        cols = len(items)
        card_w = (CONTENT_W - GAP * (cols - 1)) / cols
        labels = [extract_stat(it)[1] for it in items]
        label_size = min(16, min(fit_size([{'type': 'para', 'text': l}], card_w - 0.7, CONTENT_BOTTOM - top - 1.9)
                                 or MIN_BODY_PT for l in labels))
        needed = max(text_height([{'type': 'para', 'text': l}], card_w - 0.7, label_size) for l in labels)
        card_h = min(CONTENT_BOTTOM - top, max(2.8, needed + 2.0))
        top += max(0, (CONTENT_BOTTOM - top - card_h) / 2 - 0.25)
        for i, item in enumerate(items):
            figure, label = extract_stat(item)
            x = MARGIN + i * (card_w + GAP)
            add_box(slide, x, top, card_w, card_h, T.tint, MSO_SHAPE.ROUNDED_RECTANGLE, 0.06)
            fig_size = 44 if len(figure) <= 6 else 36 if len(figure) <= 9 else 28
            add_text(slide, x + 0.35, top + 0.35, card_w - 0.7, 1.0, figure, fig_size, T.accent_strong, bold=True,
                     font=T.head_font, anchor=MSO_ANCHOR.BOTTOM)
            add_text(slide, x + 0.35, top + 1.5, card_w - 0.7, card_h - 1.65, label, label_size, T.body,
                     markdown=True, line_spacing=1.1)

    def _table_slide(self, number, name, blocks, notes):
        """
        Table sections. Text sits beside the table only when both fit; otherwise the text
        flows onto its own slides and each table follows at full width, split across
        slides (header repeated) when it is too tall.
        """
        tables = [b for b in blocks if b['type'] == 'table']
        others = [b for b in blocks if b['type'] not in ('table', 'image')]
        side_w = 4.3
        table_w = CONTENT_W - side_w - 0.5
        if len(tables) == 1 and others:
            side_size = fit_size(others, side_w, CONTENT_H, READABLE_PT - 2)
            fit = table_fit(tables[0]['rows'], table_w, CONTENT_H)
            if side_size and fit:
                slide = self._new_slide(notes)
                self._chrome(slide, number, name)
                add_rich_text(slide, MARGIN, CONTENT_TOP + 0.05, side_w, CONTENT_H, others, side_size)
                self._draw_table(slide, MARGIN + side_w + 0.5, table_w, tables[0]['rows'], *fit)
                return
        first = True
        if has_text(others):
            self._text_slides(number, name, others, notes)
            first = False
        for table in tables:
            for size, rows, heights in paginate_table(table['rows'], CONTENT_W, CONTENT_H):
                slide = self._new_slide(notes)
                self._chrome(slide, number, name if first else f'{name} (table)')
                self._draw_table(slide, MARGIN, CONTENT_W, rows, size, heights)
                first = False

    def _draw_table(self, slide, x, width, rows, size, heights):
        ncols = table_cols(rows)
        shape = slide.shapes.add_table(len(rows), ncols, Inches(x), Inches(CONTENT_TOP),
                                       Inches(width), Inches(sum(heights)))
        tbl = shape.table
        tblPr = tbl._tbl.tblPr
        tblPr.set('bandRow', '0')
        tblPr.set('firstRow', '1')
        for r, row in enumerate(rows):
            tbl.rows[r].height = Inches(heights[r])
            for c in range(ncols):
                cell = tbl.cell(r, c)
                cell.fill.solid()
                cell.fill.fore_color.rgb = T.accent if r == 0 else (T.bg if r % 2 else T.panel)
                _cell_borders(cell, T.line)
                cell.margin_left = cell.margin_right = Inches(CELL_PAD_X)
                cell.margin_top = cell.margin_bottom = Inches(CELL_PAD_Y)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                tf = cell.text_frame
                tf.word_wrap = True
                text = row[c] if c < len(row) else ''
                for t, bold, italic in inline_runs(text):
                    run = tf.paragraphs[0].add_run(); run.text = t
                    _style(run, size, T.on_accent if r == 0 else (T.ink if c == 0 or bold else T.body),
                           r == 0 or c == 0 or bold, italic)


# ============================================================
# Table measurement
# ============================================================
TABLE_SIZES = (16, 14, 13, 12, 11, 10)
MAX_TABLE_COLS = 8
CELL_PAD_X, CELL_PAD_Y = 0.1, 0.05


def table_cols(rows):
    return min(MAX_TABLE_COLS, max(len(r) for r in rows))


def table_row_heights(rows, width, size):
    """Estimated height (inches) of each row at a font size, with equal column widths."""
    ncols = table_cols(rows)
    inner = width / ncols - 2 * CELL_PAD_X
    per_line = max(4, int(inner / (size * CHAR_EM.get(T.body_font, 0.52) / 72)))
    line_h = size * 1.2 / 72
    heights = []
    for row in rows:
        lines = max([math.ceil(len(plain(c)) / per_line) for c in row[:ncols] if c] or [1])
        heights.append(max(0.4, lines * line_h + 2 * CELL_PAD_Y + 0.08))
    return heights


def table_fit(rows, width, height, sizes=TABLE_SIZES):
    """(size, row heights) for the largest font at which the whole table fits, else None."""
    for size in sizes:
        heights = table_row_heights(rows, width, size)
        if sum(heights) <= height:
            return size, heights
    return None


def paginate_table(rows, width, height):
    """[(size, rows, heights)]: the whole table if it fits, else pages at 11pt with the header repeated."""
    fit = table_fit(rows, width, height)
    if fit:
        return [(fit[0], rows, fit[1])]
    size = 11
    heights = table_row_heights(rows, width, size)
    header, header_h = rows[0], heights[0]
    pages, cur, cur_h = [], [], []
    for row, h in zip(rows[1:], heights[1:]):
        if cur and header_h + sum(cur_h) + h > height:
            pages.append((size, [header] + cur, [header_h] + cur_h))
            cur, cur_h = [], []
        cur.append(row)
        cur_h.append(h)
    if cur or not pages:
        pages.append((size, [header] + cur, [header_h] + cur_h))
    return pages


# ============================================================
# Layout selection
# ============================================================
def has_text(blocks):
    return any(b['type'] in ('para', 'list', 'table') for b in blocks)


def choose_layout(blocks):
    types = [b['type'] for b in blocks]
    lists = [b for b in blocks if b['type'] == 'list']
    paras = [b for b in blocks if b['type'] == 'para']
    intro_chars = sum(len(plain(b['text'])) for b in paras)
    if 'table' in types:
        return 'table'
    if 'sub' in types or len(lists) != 1:
        return 'text'
    items = lists[0]['items']
    if intro_chars > 320:
        return 'text'
    if 2 <= len(items) <= 4 and all(extract_stat(i)[0] for i in items) and all(len(plain(i)) <= 110 for i in items):
        return 'stats'
    if 2 <= len(items) <= 6 and all(len(plain(i)) <= 170 for i in items):
        return 'cards'
    return 'text'


def extract_stat(item):
    """Return (figure, label) when a list item leads with / contains a key number."""
    text = plain(item)
    m = re.match(r'^\s*([^:–—-]{1,40}?)\s*[:–—-]\s*(.+)$', text)
    if m:
        num = NUMBER_RE.search(m.group(2))
        if num and num.start() == 0:
            figure = num.group(1).strip()
            rest = m.group(2)[num.end():].strip(' ,.;')
            return figure, (m.group(1) + (' — ' + rest if rest else '')).strip()
    num = NUMBER_RE.search(text)
    if num and num.start() <= 3 and re.search(r'[%$€£₦KkMmBbxX]|\d{2,}', num.group(1)):
        figure = num.group(1).strip()
        label = (text[:num.start()] + text[num.end():]).strip(' ,.;:-–—')
        label = re.sub(r'^(of|in|to)\s+', '', label)
        return figure, label[:1].upper() + label[1:]
    return '', text


LEAD_MIN_WORDS, LEAD_MAX_CHARS = 6, 150


def split_lead(blocks):
    """
    Use the first sentence of the first paragraph as a lead statement, but only when it is
    a real sentence: a fragment such as "Engine oil. Radiator coolant." stays body text.
    """
    if not blocks or blocks[0]['type'] != 'para':
        return None, blocks
    text = blocks[0]['text']
    parts = re.split(r'(?<=[.!?])\s+', text, maxsplit=1)
    lead, remainder = parts[0].strip(), (parts[1].strip() if len(parts) > 1 else '')
    if len(plain(lead).split()) < LEAD_MIN_WORDS or len(lead) > LEAD_MAX_CHARS:
        return None, blocks
    rest = ([{'type': 'para', 'text': remainder}] if remainder else []) + blocks[1:]
    if not rest:
        return None, blocks  # a single short sentence reads better as body text
    return lead, rest


# ============================================================
# Packing small sections
# ============================================================
SMALL_SECTION = 0.5   # a section using less than half a slide may join the one before it
PACK_FILL = 0.9       # ...if together they fill at most 90% of the slide


def section_height(blocks):
    width = CONTENT_W - 5.6 - GAP if any(b['type'] == 'image' for b in blocks) else CONTENT_W - 4.55
    images = sum(1 for b in blocks if b['type'] == 'image')
    if images > SIDE_IMAGES_MAX:
        return float('inf')  # it needs gallery slides anyway
    return sum(block_height(b, width) for b in blocks) + 0.45  # + its heading as a sub-heading


NUMBERED_RE = re.compile(r'^\s*(\d{1,2})(?:[.)]|\s)\s*\S')


def heading_number(title):
    m = NUMBERED_RE.match(plain(title))
    return int(m.group(1)) if m else None


def can_follow(group_numbers, number):
    """Numbered lists stay together: 1 may join its intro, n joins n-1, and a list ends the group."""
    last = group_numbers[-1]
    if number is None:
        return all(n is None for n in group_numbers)
    if number == 1:
        return last is None
    return last == number - 1


def pack_small_sections(sections):
    """
    Short consecutive sections ("01 Defence heritage", "02 Focused range"...) share a slide
    when they fit: the later section's title becomes a sub-heading under the first.
    """
    out, numbers = [], []
    for name, blocks in sections:
        number = heading_number(name)
        if out:
            prev_name, prev_blocks = out[-1]
            joined = prev_blocks + [{'type': 'sub', 'text': name}] + list(blocks)
            if (can_follow(numbers[-1], number) and section_height(blocks) <= CONTENT_H * SMALL_SECTION
                    and not any(b['type'] == 'table' for b in joined)
                    and section_height(joined) <= CONTENT_H * PACK_FILL):
                out[-1] = (prev_name, joined)
                numbers[-1].append(number)
                continue
        out.append((name, list(blocks)))
        numbers.append([number])
    return out


# ============================================================
# Splitting long sections
# ============================================================
TITLE_MAX_CHARS = 70


def block_height(block, width):
    """Estimated height (inches) of one block at the readable size."""
    if block['type'] == 'table':
        return sum(table_row_heights(block['rows'], CONTENT_W, 12)) + 0.3
    if block['type'] == 'image':
        return 0.0
    return text_height([block], width, READABLE_PT)


def section_title_from(text):
    title = plain(text).strip().rstrip('.:;,').strip()
    return title if 2 <= len(title) <= TITLE_MAX_CHARS else None


def split_long_sections(sections):
    """
    A section too long for one slide is split at its own sub-headings: sub-heading runs are
    packed into slide-sized groups, and each later group is titled by the sub-heading it
    starts with, instead of every slide repeating "<section> (cont.)". Images stay with
    the text they appeared next to.
    """
    out = []
    for name, blocks in sections:
        width = CONTENT_W - 5.6 - GAP if any(b['type'] == 'image' for b in blocks) else CONTENT_W - 4.55
        if sum(block_height(b, width) for b in blocks) <= CONTENT_H:
            out.append((name, blocks))
            continue
        runs, run = [], []
        for b in blocks:
            if b['type'] == 'sub' and run:
                runs.append(run)
                run = []
            run.append(b)
        if run:
            runs.append(run)
        if len(runs) < 2:
            out.append((name, blocks))
            continue
        groups, group, height = [], [], 0.0
        for run in runs:
            h = sum(block_height(b, width) for b in run)
            if group and height + h > CONTENT_H:
                groups.append(group)
                group, height = [], 0.0
            group += run
            height += h
        groups.append(group)
        for i, group in enumerate(groups):
            title = section_title_from(group[0]['text']) if i and group[0]['type'] == 'sub' else None
            if title and len(group) > 1:
                out.append((title, group[1:]))
            else:
                out.append((name if i == 0 else f'{name} (cont.)', group))
    return out


def section_notes(name, blocks):
    lines = [name]
    for b in blocks:
        if b['type'] in ('para', 'sub'):
            lines.append(plain(b['text']))
        elif b['type'] == 'list':
            lines.extend('- ' + plain(i) for i in b['items'])
        elif b['type'] == 'table':
            lines.extend(' | '.join(plain(c) for c in r) for r in b['rows'])
        elif b['type'] == 'image' and b.get('caption'):
            lines.append(f'[Figure: {b["caption"]}]')
    return '\n'.join(lines)


# ============================================================
# Size control
# ============================================================
SHRINK_STEPS = ((1400, 80), (1100, 70), (850, 60), (650, 50))


def shrink_pptx(buffer, target_bytes):
    """
    Recompress the pictures inside a finished .pptx until it is under target_bytes.
    Files keep their names and formats, so no slide XML needs to change.
    """
    data = buffer.getvalue()
    if len(data) <= target_bytes:
        return BytesIO(data)
    for max_px, quality in SHRINK_STEPS:
        out = BytesIO()
        with zipfile.ZipFile(BytesIO(data)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                content = src.read(item.filename)
                if item.filename.startswith('ppt/media/'):
                    content = _recompress(content, max_px, quality)
                dst.writestr(item, content)
        if out.getbuffer().nbytes <= target_bytes:
            break
    out.seek(0)
    return out


def _recompress(content, max_px, quality):
    try:
        with Image.open(BytesIO(content)) as im:
            fmt = im.format
            img = im.copy()
    except (OSError, ValueError):
        return content
    if fmt not in ('JPEG', 'PNG'):
        return content
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = BytesIO()
    if fmt == 'JPEG':
        img.convert('RGB').save(buf, 'JPEG', quality=quality, optimize=True)
    else:
        img.save(buf, 'PNG', optimize=True)
    return buf.getvalue() if buf.tell() < len(content) else content


# ============================================================
# Public API
# ============================================================
_theme_lock = threading.Lock()
AGENDA_RANGE = (3, 14)


def build_deck(title, sections, theme=DEFAULT_THEME, subtitle='Presentation', one_slide_per_section=False,
               agenda=True):
    """
    Return a BytesIO containing the finished .pptx.
    sections: [(section_title, [blocks])] with image paths already resolved.
    """
    global T
    with _theme_lock:  # T is module state; keep concurrent builds from mixing themes
        T = THEMES.get(theme, THEMES[DEFAULT_THEME])
        try:
            builder = DeckBuilder(title, subtitle, one_slide_per_section)
            sections = [(n, b) for n, b in sections if b] or \
                [('Overview', [{'type': 'para', 'text': 'No content was found in this document.'}])]
            if not one_slide_per_section:
                sections = pack_small_sections(split_long_sections(sections))
            builder.cover()
            if agenda and AGENDA_RANGE[0] <= len(sections) <= AGENDA_RANGE[1]:
                builder.agenda([n for n, _ in sections])
            for i, (name, blocks) in enumerate(sections, start=1):
                builder.section(i, name, blocks)
            builder.closing()
            buffer = BytesIO()
            builder.prs.save(buffer)
            buffer.seek(0)
            return buffer, builder.page
        finally:
            T = THEMES[DEFAULT_THEME]
