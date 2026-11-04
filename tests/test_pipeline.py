"""
Tests for everything after Docling: Markdown parsing, image filtering, section handling,
LLM plumbing (with the HTTP call stubbed out) and deck building for every slide type.
"""
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Emu

import condense
import pdf_to_md
from deck_builder import SLIDE_H, SLIDE_W, THEMES
from md_parse import parse_blocks, parse_markdown
from pipeline import build_from_markdown, load_sections, merge_empty_sections

FIXTURES = Path(__file__).parent / 'fixtures'
SAMPLE = FIXTURES / 'sample.md'


def pictures(prs):
    return [sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13]


def open_deck(buffer):
    return Presentation(buffer)


def assert_on_slide(prs):
    for n, slide in enumerate(prs.slides, 1):
        for sh in slide.shapes:
            if sh.shape_type == 13:  # pictures must stay inside the slide
                assert sh.left >= 0 and sh.top >= 0, f'slide {n}'
                assert Emu(sh.left + sh.width).inches <= SLIDE_W + 0.01, f'slide {n}'
                assert Emu(sh.top + sh.height).inches <= SLIDE_H + 0.01, f'slide {n}'


# ---------- parsing ----------
def test_parse_sections_images_and_captions():
    title, sections = parse_markdown(SAMPLE.read_text())
    assert title == 'Renewable Energy Outlook for West Africa 2026'
    names = [n for n, _ in sections]
    assert names[0] == 'Overview' and 'Key Figures' in names
    summary = dict(sections)['Executive Summary']
    image = next(b for b in summary if b['type'] == 'image')
    assert image['path'] == 'images/chart_wide.png'
    assert image['caption'].startswith('Figure 1')
    assert not any(b['type'] == 'para' and b['text'].startswith('Figure 1') for b in summary)


def test_parse_undoes_docling_escaping_and_placeholders():
    _, sections = parse_markdown('## S\n\nsnake\\_case &amp; more\n\n<!-- image -->\n\n- item one\n  wrapped')
    blocks = sections[0][1]
    assert blocks[0] == {'type': 'para', 'text': 'snake_case & more'}
    assert len(blocks) == 2
    lst = next(b for b in blocks if b['type'] == 'list')
    assert lst['items'] == ['item one wrapped']


def test_merge_empty_sections():
    merged = merge_empty_sections([('3 Methods', []), ('3.1 Data', [{'type': 'para', 'text': 'x'}])])
    assert merged == [('3 Methods', [{'type': 'sub', 'text': '3.1 Data'}, {'type': 'para', 'text': 'x'}])]


def test_load_sections_rejects_paths_outside_job_folder(tmp_path):
    md = tmp_path / 'doc.md'
    md.write_text('## A\n\nText here.\n\n![Image](../../etc/passwd)\n\n![Image](missing.png)\n')
    _, sections = load_sections(md)
    assert all(b['type'] != 'image' for _, blocks in sections for b in blocks)


# ---------- image filtering ----------
def test_filter_images_drops_icons_blanks_and_repeated_logos(tmp_path):
    img = tmp_path / 'images'
    img.mkdir()
    for name in ('chart_wide.png', 'icon.png', 'blank.png'):
        shutil.copy(FIXTURES / 'images' / name, img / name)
    Image.new('RGB', (2000, 100), 'black').save(img / 'rule.png')
    logo = Image.new('RGB', (300, 300), 'white')
    logo.paste((200, 0, 0), (50, 50, 250, 250))
    logo.save(img / 'logo.png')
    md = '\n\n'.join(['![Image](images/chart_wide.png)', '![Image](images/icon.png)', '![Image](images/blank.png)',
                      '![Image](images/rule.png)'] + ['![Image](images/logo.png)'] * 3)
    kept = pdf_to_md.filter_images(md, tmp_path)
    assert kept.strip() == '![Image](images/chart_wide.png)'


def test_filter_images_keeps_first_of_a_duplicate_pair(tmp_path):
    (tmp_path / 'images').mkdir()
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'a.png')
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'b.png')
    kept = pdf_to_md.filter_images('![Image](images/a.png)\n\n![Image](images/b.png)', tmp_path)
    assert 'a.png' in kept and 'b.png' not in kept


def test_clean_markdown_removes_page_numbers_keeps_years():
    out = pdf_to_md.clean_markdown('Intro text\n\n12\n\nPage 3 of 10\n\n2024\n')
    assert '12' not in out.split() and 'Page' not in out and '2024' in out


# ---------- Standard (no LLM) ----------
@pytest.mark.parametrize('theme', list(THEMES))
def test_standard_deck_every_theme(theme):
    buffer, title, slides = build_from_markdown(SAMPLE, 'standard', theme)
    prs = open_deck(buffer)
    assert len(prs.slides) == slides
    # cover + agenda + 8 sections (+ figures/continuations) + closing
    assert slides >= 11
    # 1 image beside the summary text, 3 in the gallery: each extracted image appears exactly once
    assert len(pictures(prs)) == 4
    assert_on_slide(prs)


def test_standard_deck_keeps_all_text():
    buffer, _, _ = build_from_markdown(SAMPLE, 'standard')
    prs = open_deck(buffer)
    text = ' '.join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert 'without affordable credit' in text  # end of the long section, on its continuation slide
    assert '(cont.)' in text


# ---------- LLM modes (HTTP stubbed) ----------
class FakeLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, system, user, max_tokens=1500, retries=5):
        self.calls.append(user)
        if '"notes"' in user:
            return {'notes': 'Dense notes.'}
        if 'Summarise' in user:
            return {'title': 'Summary slide', 'body': 'One framing sentence.\n\n- **Point** one\n- Point two\n- Point three'}
        return {'title': 'Expanded', 'body': 'A longer explanation of the section.\n\n### Key takeaways\n\n- First\n- Second'}


def test_condensed_groups_sections_and_keeps_images(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(condense, 'chat_json', fake)
    monkeypatch.setitem(condense.__dict__, 'CONDENSED_MAX_IMAGES', 2)
    from slide_plan import PLANS
    monkeypatch.setitem(PLANS['condensed'], 'max_sections', 4)
    buffer, _, slides = build_from_markdown(SAMPLE, 'condensed')
    prs = open_deck(buffer)
    assert len(fake.calls) == 4  # one request per slide group
    assert not any('images/' in c for c in fake.calls)  # image files never go to the LLM
    assert any('[Figure: Figure 2' in c for c in fake.calls)  # but their captions do, as context
    assert 1 <= len(pictures(prs)) <= 4 * 2
    assert_on_slide(prs)


def test_detailed_reattaches_tables_and_images(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(condense, 'chat_json', fake)
    buffer, _, _ = build_from_markdown(SAMPLE, 'detailed')
    prs = open_deck(buffer)
    tables = [sh for s in prs.slides for sh in s.shapes if sh.has_table]
    assert len(tables) == 1 and tables[0].table.cell(1, 0).text == 'Nigeria'
    assert len(pictures(prs)) == 4
    assert any('| Nigeria |' in c for c in fake.calls)  # the LLM saw the table to explain it


def test_long_sections_are_noted_before_rewriting(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(condense, 'chat_json', fake)
    blocks = [{'type': 'para', 'text': 'word ' * 3000}]
    condense.expand_sections('Doc', [('Long', blocks)], {'words': 200})
    assert sum('"notes"' in c for c in fake.calls) == 2 and len(fake.calls) == 3


def test_group_sections_merges_smallest_neighbours():
    p = lambda n: [{'type': 'para', 'text': 'w ' * n}]
    groups = condense.group_sections([('a', p(500)), ('b', p(10)), ('c', p(10)), ('d', p(500))], 3)
    assert [[n for n, _ in g] for g in groups] == [['a'], ['b', 'c'], ['d']]


def test_chat_json_retries_on_rate_limit(monkeypatch):
    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._body, self.headers, self.text = code, body, {'retry-after': '0'}, ''

        def json(self):
            return self._body

    replies = [Resp(429), Resp(200, {'choices': [{'message': {'content': json.dumps({'title': 'ok'})}}]})]
    monkeypatch.setattr(condense.requests, 'post', lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(condense.time, 'sleep', lambda s: None)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    assert condense.chat_json('s', 'u') == {'title': 'ok'}


def test_chat_json_reports_rejected_key(monkeypatch):
    class Resp:
        status_code, headers, text = 401, {}, 'invalid api key'
    monkeypatch.setattr(condense.requests, 'post', lambda *a, **k: Resp())
    with pytest.raises(condense.LLMError, match='401'):
        condense.chat_json('s', 'u')


# ---------- PDF -> Markdown (real PyMuPDF run on tests/fixtures/sample.pdf) ----------
SAMPLE_PDF = FIXTURES / 'sample.pdf'


def test_normalize_headings_title_and_levels():
    md = '# **Report Title**\n\nIntro.\n\n# **Summary**\n\nText.\n\n## **Detail**\n\nMore.'
    out = pdf_to_md.normalize_headings(md)
    assert out.split('\n')[0] == '# Report Title'
    assert '## Summary' in out and '### Detail' in out


def test_normalize_headings_uses_metadata_title_when_doc_has_none():
    out = pdf_to_md.normalize_headings('Some intro text.\n\n## Part one\n\nBody.', 'Annual Review')
    assert out.startswith('# Annual Review') and '## Part one' in out
    assert not pdf_to_md.normalize_headings('Text.\n\n## A\n\nB', 'Microsoft Word - draft.docx').startswith('#')


def test_italic_caption_is_attached():
    blocks = parse_blocks('![](images/a.png)\n\n_Figure 4: Results._')
    assert blocks == [{'type': 'image', 'path': 'images/a.png', 'caption': 'Figure 4: Results.'}]


def test_convert_pdf_end_to_end(tmp_path):
    md_path = pdf_to_md.convert_pdf(SAMPLE_PDF, tmp_path)
    text = md_path.read_text()
    assert 'Confidential draft' not in text and 'Page 1' not in text  # running header/footer removed
    title, sections = load_sections(md_path)
    assert title == 'Renewable Energy Outlook for West Africa 2026'
    names = [n for n, _ in sections]
    assert 'Executive Summary' in names and 'Market Size by Country' in names
    method = dict(sections)['Methodology']  # bold-only heading recovered (as a sub-heading)
    assert {'type': 'sub', 'text': 'Regional Gallery'} in method
    images = {n: [b for b in bl if b['type'] == 'image'] for n, bl in sections}
    assert len(images['Executive Summary']) == 1  # the figure stays in its own section
    assert images['Executive Summary'][0]['caption'].startswith('Figure 1')
    assert sum(len(v) for v in images.values()) == 3
    assert any(b['type'] == 'table' for b in dict(sections)['Market Size by Country'])
    assert all(Path(b['path']).is_file() for v in images.values() for b in v)


def test_convert_rejects_text_free_pdf(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    doc.save(tmp_path / 'blank.pdf')
    with pytest.raises(pdf_to_md.PdfConversionError, match='scanned'):
        pdf_to_md.convert_pdf(tmp_path / 'blank.pdf', tmp_path / 'out')


# ---------- size control ----------
def test_shrink_pptx_recompresses_pictures(tmp_path):
    from io import BytesIO
    import random
    from deck_builder import build_deck, shrink_pptx
    random.seed(0)
    noisy = Image.effect_noise((1600, 1200), 80).convert('RGB')
    noisy.save(tmp_path / 'p.jpg', quality=95)
    sections = [('Photo', [{'type': 'image', 'path': str(tmp_path / 'p.jpg'), 'caption': ''}])]
    buffer, _ = build_deck('T', sections)
    size = buffer.getbuffer().nbytes
    small = shrink_pptx(buffer, size // 3)
    assert small.getbuffer().nbytes <= size // 3
    assert len(pictures(Presentation(small))) == 1


# ---------- web app ----------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv('FIREBASE_SERVICE_ACCOUNT', raising=False)
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    import app as webapp
    return webapp.app.test_client()


def test_app_converts_pdf(client):
    with open(SAMPLE_PDF, 'rb') as f:
        res = client.post('/convert', data={'pdf': (f, 'sample.pdf'), 'mode': 'standard', 'theme': 'green'})
    assert res.status_code == 200, res.data[:300]
    assert res.mimetype.endswith('presentationml.presentation')
    prs = Presentation(__import__('io').BytesIO(res.data))
    assert len(prs.slides) == int(res.headers['X-Slides'])
    assert len(pictures(prs)) == 3


def test_app_markdown_zip(client):
    import io
    import zipfile
    with open(SAMPLE_PDF, 'rb') as f:
        res = client.post('/markdown', data={'pdf': (f, 'sample.pdf')})
    assert res.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(res.data)).namelist()
    assert 'document.md' in names and sum(n.startswith('images/') for n in names) == 3


def test_app_rejects_bad_input(client):
    import io
    assert client.post('/convert', data={'pdf': (io.BytesIO(b'hello'), 'x.pdf')}).status_code == 400
    with open(SAMPLE_PDF, 'rb') as f:
        res = client.post('/convert', data={'pdf': (f, 'sample.pdf'), 'mode': 'condensed'})
    assert res.status_code == 400 and 'AI key' in res.json['error']


def test_bold_lines_become_headings():
    out = pdf_to_md.clean_markdown('We combined data. **Regional Gallery**\n\nText.\n\n**Bold sentence.**\n')
    assert '### Regional Gallery' in out and 'We combined data.\n' in out
    assert '**Bold sentence.**' in out  # ends like a sentence, so it stays body text


# ---------- Firebase Storage route (Google APIs stubbed) ----------
class FakeStorage:
    """Stands in for the bucket: 'uploads' maps paths to local files, 'outputs' records results."""
    def __init__(self, tmp_path):
        self.tmp, self.uploads, self.outputs, self.deleted = tmp_path, {}, {}, []

    def download(self, path, dest):
        if path not in self.uploads:
            raise storage.StorageError('The uploaded PDF was not found. Please upload it again.')
        shutil.copy(self.uploads[path], dest)

    def upload_result(self, local, path, name, content_type):
        self.outputs[path] = Path(local).read_bytes()
        return f'https://signed.example/{path}'

    def delete(self, path):
        self.deleted.append(path)


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    import storage as storage_mod
    for key in ('FIREBASE_SERVICE_ACCOUNT', 'FIREBASE_STORAGE_BUCKET', 'FIREBASE_PROJECT_ID',
                'FIREBASE_API_KEY', 'FIREBASE_APP_ID'):
        monkeypatch.setenv(key, 'x')
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    fake = FakeStorage(tmp_path)
    monkeypatch.setattr(storage_mod, 'download', fake.download)
    monkeypatch.setattr(storage_mod, 'upload_result', fake.upload_result)
    monkeypatch.setattr(storage_mod, 'delete', fake.delete)
    monkeypatch.setattr(storage_mod, 'verify_user',
                        lambda h: 'user1' if h == 'Bearer good' else (_ for _ in ()).throw(storage_mod.AuthError('bad')))
    import app as webapp
    return webapp.app.test_client(), fake


import storage  # noqa: E402


def test_cloud_convert_returns_signed_links_and_deletes_pdf(cloud):
    client, fake = cloud
    path = 'uploads/user1/abcdef12-3456.pdf'
    fake.uploads[path] = SAMPLE_PDF
    res = client.post('/convert', json={'storage_path': path, 'filename': 'report.pdf', 'mode': 'standard'},
                      headers={'Authorization': 'Bearer good'})
    assert res.status_code == 200, res.data[:300]
    data = res.json
    assert data['download_url'].startswith('https://signed.example/outputs/user1/abcdef12-3456/')
    assert data['markdown_url'].endswith('_markdown.zip') and data['slides'] >= 5
    deck = next(v for k, v in fake.outputs.items() if k.endswith('.pptx'))
    assert len(pictures(Presentation(__import__('io').BytesIO(deck)))) == 3
    assert fake.deleted == [path]


def test_cloud_rejects_other_users_paths_and_bad_tokens(cloud):
    client, fake = cloud
    fake.uploads['uploads/user2/abcdef12-3456.pdf'] = SAMPLE_PDF
    res = client.post('/convert', json={'storage_path': 'uploads/user2/abcdef12-3456.pdf'},
                      headers={'Authorization': 'Bearer good'})
    assert res.status_code == 403
    res = client.post('/convert', json={'storage_path': 'uploads/user1/../../secret.pdf'},
                      headers={'Authorization': 'Bearer good'})
    assert res.status_code == 403
    res = client.post('/convert', json={'storage_path': 'uploads/user1/abcdef12-3456.pdf'},
                      headers={'Authorization': 'Bearer forged'})
    assert res.status_code == 401
    assert fake.outputs == {}


def test_cloud_page_includes_firebase_config(cloud):
    client, _ = cloud
    html = client.get('/').data.decode()
    assert 'firebase-storage.js' in html and 'Up to 50 MB' in html


def test_page_without_firebase_uses_direct_upload(client):
    html = client.get('/').data.decode()
    assert 'firebase' not in html


def test_service_account_accepts_raw_or_base64(monkeypatch):
    import base64 as b64
    raw = '{"project_id": "p", "type": "service_account"}'
    monkeypatch.setenv('FIREBASE_SERVICE_ACCOUNT', raw)
    assert storage._service_account_info()['project_id'] == 'p'
    monkeypatch.setenv('FIREBASE_SERVICE_ACCOUNT', b64.b64encode(raw.encode()).decode())
    assert storage._service_account_info()['project_id'] == 'p'
