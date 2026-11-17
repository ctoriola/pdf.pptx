"""
Tests for everything after Docling: Markdown parsing, image filtering, section handling,
LLM plumbing (with the HTTP call stubbed out) and deck building for every slide type.
"""
import json
import re
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


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Tests never contact a real LLM provider: model discovery is fixed and rate-limit state cleared."""
    monkeypatch.setattr(condense, 'available_models', lambda cfg=None: ['test-model'])
    condense._cooldown.clear()

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
    with pytest.raises(pdf_to_md.PdfConversionError, match='No readable text was found in this PDF'):
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
    monkeypatch.delenv('B2_KEY_ID', raising=False)
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
    assert res.status_code == 400 and 'not switched on' in res.json['error'] and 'GROQ' not in res.json['error']


def test_bold_lines_become_headings():
    out = pdf_to_md.clean_markdown('We combined data. **Regional Gallery**\n\nText.\n\n**Bold sentence.**\n')
    assert '### Regional Gallery' in out and 'We combined data.\n' in out
    assert '**Bold sentence.**' in out  # ends like a sentence, so it stays body text


# ---------- Backblaze B2 route (HTTP to B2 stubbed) ----------
import storage  # noqa: E402

B2_ENV = {'B2_KEY_ID': '005abc', 'B2_APPLICATION_KEY': 'K005secretSECRET/+=', 'B2_BUCKET': 'my-bucket',
          'B2_ENDPOINT': 's3.us-east-005.backblazeb2.com'}


class FakeB2:
    """Stands in for the bucket: 'uploads' maps paths to local files, 'outputs' records results."""
    def __init__(self):
        self.uploads, self.outputs, self.deleted = {}, {}, []

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
def cloud(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    fake = FakeB2()
    monkeypatch.setattr(storage, 'download', fake.download)
    monkeypatch.setattr(storage, 'upload_result', fake.upload_result)
    monkeypatch.setattr(storage, 'delete', fake.delete)
    import app as webapp
    return webapp.app.test_client(), fake


def test_upload_url_signs_size_and_enforces_limit(cloud):
    client, _ = cloud
    res = client.post('/upload-url', json={'size': 60 * 1024 * 1024})
    assert res.status_code == 200
    data = res.json
    assert storage.UPLOAD_PATH_RE.match(data['path'])
    assert data['url'].startswith('https://s3.us-east-005.backblazeb2.com/my-bucket/uploads/')
    assert 'X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost' in data['url']
    assert client.post('/upload-url', json={'size': 101 * 1024 * 1024}).status_code == 400
    assert client.post('/upload-url', json={}).status_code == 400


def test_cloud_convert_returns_links_and_deletes_pdf(cloud):
    client, fake = cloud
    path = 'uploads/' + 'ab' * 16 + '.pdf'
    fake.uploads[path] = SAMPLE_PDF
    res = client.post('/convert', json={'storage_path': path, 'filename': 'report.pdf', 'mode': 'standard'})
    assert res.status_code == 200, res.data[:300]
    data = res.json
    assert data['download_url'].startswith('https://signed.example/outputs/' + 'ab' * 16 + '/')
    assert data['markdown_url'].endswith('_markdown.zip') and data['slides'] >= 5
    deck = next(v for k, v in fake.outputs.items() if k.endswith('.pptx'))
    assert len(pictures(Presentation(__import__('io').BytesIO(deck)))) == 3
    assert fake.deleted == [path]


def test_cloud_rejects_bad_paths(cloud):
    client, fake = cloud
    for bad in ('uploads/../outputs/x.pdf', 'outputs/' + 'ab' * 16 + '.pdf', 'uploads/short.pdf'):
        res = client.post('/convert', json={'storage_path': bad})
        assert res.status_code == 400, bad
    res = client.post('/convert', json={'storage_path': 'uploads/' + 'cd' * 16 + '.pdf'})
    assert res.status_code == 400 and 'not found' in res.json['error']
    assert fake.outputs == {}


def test_cloud_page_mentions_100mb(cloud):
    client, _ = cloud
    html = client.get('/').data.decode()
    assert 'window.cloudUpload' in html and 'Up to 100 MB' in html


def test_page_without_b2_uses_direct_upload(client):
    assert 'cloudUpload =' not in client.get('/').data.decode()


def _botocore_url(method, key, headers, expires):
    """Presign with Amazon's own botocore at a fixed time, for comparison."""
    import datetime as dt
    from unittest import mock
    botocore = pytest.importorskip('botocore')
    import botocore.auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials
    fixed = dt.datetime(2026, 11, 4, 9, 30, 0)

    class FixedDT(dt.datetime):
        @classmethod
        def utcnow(cls):
            return fixed

        @classmethod
        def now(cls, tz=None):
            return fixed.replace(tzinfo=tz) if tz else fixed
    url = f'https://{B2_ENV["B2_ENDPOINT"]}/{B2_ENV["B2_BUCKET"]}/' + storage._uri_encode(key, safe='-_.~/')
    req = AWSRequest(method=method, url=url, headers=headers, data=b'')
    with mock.patch.object(botocore.auth.datetime, 'datetime', FixedDT):
        botocore.auth.S3SigV4QueryAuth(Credentials(B2_ENV['B2_KEY_ID'], B2_ENV['B2_APPLICATION_KEY']), 's3',
                                       'us-east-005', expires=expires).add_auth(req)
    return req.url, fixed.replace(tzinfo=dt.timezone.utc)


@pytest.mark.parametrize('method,key,headers', [
    ('GET', 'outputs/abc/My Deck (v2).pptx', {}),
    ('PUT', 'uploads/' + 'ab' * 16 + '.pdf', {'Content-Type': 'application/pdf', 'Content-Length': '12345'}),
    ('PUT', 'outputs/abc/Deck.zip', {'Content-Type': 'application/zip',
                                     'Content-Disposition': 'attachment; filename="Deck.zip"'}),
])
def test_presign_matches_botocore(monkeypatch, method, key, headers):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    expected, now = _botocore_url(method, key, headers, 900)
    assert storage.presign(method, key, 900, headers, now=now) == expected


# ---------- /setup page (Backblaze API stubbed) ----------
class FakeB2Api:
    def __init__(self, restricted=False, bucket_exists=True):
        self.restricted, self.bucket_exists, self.calls = restricted, bucket_exists, []

    def response(self, status, body):
        class R:
            status_code = status
            text = json.dumps(body)

            def json(self):
                return body
        return R()

    def get(self, url, headers=None, timeout=None):
        self.calls.append(('auth', headers['Authorization']))
        if headers['Authorization'] == 'Basic ' + __import__('base64').b64encode(b'bad:bad').decode():
            return self.response(401, {'message': 'unauthorized'})
        return self.response(200, {'apiUrl': 'https://api005.backblazeb2.com', 'authorizationToken': 'tok',
                                   'accountId': 'acc', 'allowed': {'bucketId': 'b1' if self.restricted else None}})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url.rsplit('/', 1)[-1], json))
        if url.endswith('b2_list_buckets'):
            buckets = [{'bucketId': 'bid', 'bucketName': json['bucketName'],
                        'lifecycleRules': [{'fileNamePrefix': 'keep/', 'daysFromHidingToDeleting': 3}],
                        'corsRules': [{'corsRuleName': 'other'}]}] if self.bucket_exists else []
            return self.response(200, {'buckets': buckets})
        return self.response(200, {'bucketName': 'my-bucket'})


SETUP_TOKEN = 'owner-only-setup-token-1234'


def signed_in(client):
    """Enter the owner's setup password, as the owner would, and keep the cookie it sets."""
    res = client.post('/setup', data={'password': SETUP_TOKEN})
    assert res.status_code == 303
    return client


@pytest.fixture
def setup_client(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv('SETUP_TOKEN', SETUP_TOKEN)
    import app as webapp
    import b2_admin
    api = FakeB2Api()
    monkeypatch.setattr(b2_admin.requests, 'get', api.get)
    monkeypatch.setattr(b2_admin.requests, 'post', api.post)
    monkeypatch.setattr(b2_admin, 'check_app_key', lambda: None)
    monkeypatch.setattr(webapp.time, 'sleep', lambda s: None)
    return signed_in(webapp.app.test_client()), api


def test_setup_page_lists_missing_settings(monkeypatch):
    for k in B2_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('SETUP_TOKEN', SETUP_TOKEN)
    import app as webapp
    html = signed_in(webapp.app.test_client()).get('/setup').data.decode()
    assert 'B2_BUCKET</code>&nbsp;is missing' in html and '<form' not in html


def test_setup_configures_cors_and_lifecycle_without_echoing_key(setup_client):
    client, api = setup_client
    res = client.post('/setup', data={'key_id': 'master', 'key': 'SUPERSECRET', 'origins': ''})
    html = res.data.decode()
    assert res.status_code == 200 and 'is ready' in html and 'App key can write' in html
    assert 'SUPERSECRET' not in html and res.headers['Cache-Control'] == 'no-store'
    update = next(p for name, p in api.calls if name == 'b2_update_bucket')
    assert update['bucketType'] == 'allPrivate'
    assert [r['corsRuleName'] for r in update['corsRules']] == ['other', 'pdfToSlidesUploads']
    assert update['corsRules'][1]['allowedOrigins'] == ['*'] and 's3_put' in update['corsRules'][1]['allowedOperations']
    assert [r['fileNamePrefix'] for r in update['lifecycleRules']] == ['keep/', 'uploads/', 'outputs/']


def test_setup_restricts_origins(setup_client):
    client, api = setup_client
    client.post('/setup', data={'key_id': 'm', 'key': 'k', 'origins': 'https://a.vercel.app/, https://b.com'})
    update = next(p for name, p in api.calls if name == 'b2_update_bucket')
    assert update['corsRules'][-1]['allowedOrigins'] == ['https://a.vercel.app', 'https://b.com']


def test_setup_reports_wrong_or_restricted_key(setup_client):
    client, api = setup_client
    assert 'rejected the key' in client.post('/setup', data={'key_id': 'bad', 'key': 'bad'}).data.decode()
    api.restricted = True
    assert 'MASTER application key' in client.post('/setup', data={'key_id': 'm', 'key': 'k'}).data.decode()
    api.restricted, api.bucket_exists = False, False
    assert 'no bucket named' in client.post('/setup', data={'key_id': 'm', 'key': 'k'}).data.decode()


def test_check_app_key_reports_write_failure(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    import b2_admin

    class R:
        def __init__(self, code, text=''):
            self.status_code, self.text = code, text
    body = '<Error><Code>SignatureDoesNotMatch</Code><Message>Signature validation failed</Message></Error>'
    monkeypatch.setattr(b2_admin.requests, 'put', lambda *a, **k: R(403, body))
    msg = b2_admin.check_app_key()
    assert 'could not write' in msg and 'does not belong to B2_KEY_ID' in msg and 'Signature validation failed' in msg
    assert 'region us-east-005' in msg and B2_ENV['B2_APPLICATION_KEY'] not in msg
    monkeypatch.setattr(b2_admin.requests, 'put', lambda *a, **k: R(200))
    monkeypatch.setattr(b2_admin.requests, 'delete', lambda *a, **k: R(204))
    assert b2_admin.check_app_key() is None


def test_config_strips_pasted_whitespace_and_quotes(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, f' "{v}"\n')
    cfg = storage._config()
    assert cfg['key_id'] == B2_ENV['B2_KEY_ID'] and cfg['secret'] == B2_ENV['B2_APPLICATION_KEY']
    assert cfg['host'] == B2_ENV['B2_ENDPOINT'] and cfg['region'] == 'us-east-005'


# ---------- word-spacing repair (LLM stubbed) ----------
import text_repair  # noqa: E402


def test_needs_repair_detection():
    assert text_repair.needs_repair('P R O D U C T C O D E')
    assert text_repair.needs_repair('BUILTFORASTRONGERTOMORROW')
    assert text_repair.needs_repair('O F F I C E A D D R E S S [Street, city, state]')
    assert not text_repair.needs_repair('Multi-purpose Water resistant extreme duty')
    assert not text_repair.needs_repair('DG-2')
    assert not text_repair.needs_repair('See https://example.com/averyveryverylongpathsegmenthere')
    assert not text_repair.needs_repair('Hours: 9 to 5 on weekdays, a b c')  # only 3 single letters


def test_repair_keeps_only_space_changes(monkeypatch):
    sent = []

    def fake(system, user, max_tokens=1500, retries=5):
        sent.append(user)
        return {'fixed': ['PRODUCT CODE', 'BUILT FOR A STRONGER TOMORROW', 'DEFENSE INDUSTRIES']}
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    monkeypatch.setattr(text_repair.condense, 'chat_json', fake)
    fixes = text_repair.repair_strings(['P R O D U C T C O D E', 'BUILTFORASTRONGERTOMORROW', 'DEFENCEINDUSTRIES',
                                        'normal text'])
    assert fixes == {'P R O D U C T C O D E': 'PRODUCT CODE',
                     'BUILTFORASTRONGERTOMORROW': 'BUILT FOR A STRONGER TOMORROW'}  # spelling change rejected
    assert len(sent) == 1 and 'normal text' not in sent[0]


def test_repair_sections_applies_everywhere(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    mapping = {'S A L E S D E S K': 'SALES DESK', 'EMAILADDRESSOFFICE': 'EMAIL ADDRESS OFFICE',
               'P A C K': 'PACK', 'D I C O N F I P C O': 'DICON FIPCO'}
    monkeypatch.setattr(text_repair.condense, 'chat_json', lambda s, u, **k: {
        'fixed': [mapping[line.split('. ', 1)[1]] for line in u.split('ITEMS:\n', 1)[1].splitlines()]})
    title, sections = text_repair.repair_sections('D I C O N F I P C O', [
        ('S A L E S D E S K', [{'type': 'para', 'text': 'EMAILADDRESSOFFICE'},
                               {'type': 'list', 'items': ['P A C K', 'fine'], 'ordered': False},
                               {'type': 'table', 'rows': [['P A C K', '400 g']]}])])
    assert title == 'DICON FIPCO' and sections[0][0] == 'SALES DESK'
    blocks = sections[0][1]
    assert blocks[0]['text'] == 'EMAIL ADDRESS OFFICE' and blocks[1]['items'] == ['PACK', 'fine']
    assert blocks[2]['rows'] == [['PACK', '400 g']]


def test_repair_without_key_is_a_no_op(monkeypatch):
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.setattr(text_repair.condense, 'chat_json', lambda *a, **k: pytest.fail('should not call the LLM'))
    assert text_repair.repair_strings(['P R O D U C T']) == {}


# ---------- table layout ----------
def _text_boxes_fit(prs):
    """Every text box's estimated content height stays inside the slide."""
    from deck_builder import SLIDE_H
    for n, slide in enumerate(prs.slides, 1):
        for sh in slide.shapes:
            if sh.shape_type in (17, 19):  # text boxes and tables (decorative circles bleed on purpose)
                assert Emu(sh.top + sh.height).inches <= SLIDE_H + 0.01, f'slide {n}: content runs off the slide'


def test_long_text_with_table_flows_onto_its_own_slides():
    from deck_builder import build_deck
    long_text = [{'type': 'sub', 'text': f'Point {i}'} if i % 2 else
                 {'type': 'para', 'text': 'Extreme pressure performance protects against metal contact. ' * 4}
                 for i in range(14)]
    table = {'type': 'table', 'rows': [['Product code', 'Class', 'Pack']] +
             [[f'DG-{i}', 'Multi-purpose water resistant extreme duty grease for chassis points', '400 g cartridge']
              for i in range(20)]}
    buffer, slides = build_deck('Brochure', [('Built for a stronger tomorrow', long_text + [table])])
    prs = Presentation(buffer)
    tables = [sh.table for s in prs.slides for sh in s.shapes if sh.has_table]
    assert len(tables) >= 2  # the 21-row table was split
    assert sum(len(t.rows) - 1 for t in tables) == 20  # every data row kept once, header repeated
    assert all(t.cell(0, 0).text == 'Product code' for t in tables)
    text = ' '.join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert 'Point 13' in text and '(table)' in text
    _text_boxes_fit(prs)


def test_short_text_sits_beside_small_table():
    from deck_builder import build_deck
    blocks = [{'type': 'para', 'text': 'Nigeria leads by capacity.'},
              {'type': 'table', 'rows': [['Country', 'MW'], ['Nigeria', '1,850'], ['Ghana', '980']]}]
    buffer, slides = build_deck('T', [('Market', blocks)])
    assert slides == 3  # cover, one table slide, closing


def test_every_table_in_a_section_is_rendered():
    from deck_builder import build_deck
    t = lambda name: {'type': 'table', 'rows': [['Name', 'Value'], [name, '1']]}  # noqa: E731
    buffer, _ = build_deck('T', [('Two tables', [{'type': 'para', 'text': 'Intro.'}, t('first'), t('second')])])
    cells = [sh.table.cell(1, 0).text for s in Presentation(buffer).slides for sh in s.shapes if sh.has_table]
    assert cells == ['first', 'second']


# ---------- HTML from PyMuPDF and resized duplicate images ----------
def test_inline_html_becomes_plain_text():
    from md_parse import clean_inline
    assert clean_inline('S A E<br>15W-40<BR/>API CI-4 / SL') == 'S A E 15W-40 API CI-4 / SL'
    assert clean_inline('04<sup>READINESS DEPENDS ON</sup> AVAILABILITY') == '04 READINESS DEPENDS ON AVAILABILITY'
    assert clean_inline('a <b>bold</b>, ok') == 'a bold, ok'
    assert clean_inline('Use <email address> here') == 'Use <email address> here'  # not a known tag
    rows = parse_blocks('| a<br>b | c |\n|---|---|\n| 1 L<br>4 L | x |')[0]['rows']
    assert rows == [['a b', 'c'], ['1 L 4 L', 'x']]


def test_resized_copies_of_a_picture_are_deduplicated(tmp_path):
    (tmp_path / 'images').mkdir()
    big = Image.open(FIXTURES / 'images' / 'photo2.png')
    big.save(tmp_path / 'images' / 'a.png')
    big.resize((600, 450)).save(tmp_path / 'images' / 'b.png')
    big.resize((420, 315)).convert('RGB').save(tmp_path / 'images' / 'c.jpg', quality=80)
    shutil.copy(FIXTURES / 'images' / 'photo3.png', tmp_path / 'images' / 'd.png')
    md = '\n\n'.join(f'![Image](images/{n})' for n in ('a.png', 'b.png', 'c.jpg', 'd.png'))
    kept = pdf_to_md.filter_images(md, tmp_path)
    assert 'a.png' in kept and 'd.png' in kept  # first copy and a different picture stay
    assert 'b.png' not in kept and 'c.jpg' not in kept  # a large picture repeated is kept once, not dropped


# ---------- deck structure: tagline headings, long sections, small sections ----------
def test_one_off_tagline_heading_does_not_swallow_the_document():
    md = ('## **DICON-FIPCO**\n\nIntro.\n\n# **BUILT FOR A STRONGER TOMORROW.**\n\nEngine oil.\n\n'
          '## **WELCOME**\n\nText.\n\n## **WHY DEFENCE**\n\nText.\n\n### **D S O - 1**\n\nText.')
    out = pdf_to_md.normalize_headings(md)
    assert '## Built for a Stronger Tomorrow.' in out and '## Welcome' in out and '## Why Defence' in out
    assert '### D S O - 1' in out


def test_long_section_splits_at_its_subheadings():
    from deck_builder import split_long_sections, build_deck
    para = {'type': 'para', 'text': 'Extreme pressure performance protects against metal contact under load. ' * 5}
    blocks = []
    for h in ('WELCOME TO DICON-FIPCO.', 'LUBRICANTS FROM NIGERIA', 'WHY DEFENCE NEEDS A TRUSTED OIL SOURCE.',
              'FIVE GRADES. EVERY ENGINE IN THE FLEET.', 'READING THE API CATEGORY', 'SELECTION AND USE'):
        blocks += [{'type': 'sub', 'text': h}, para, para]
    buffer, _ = build_deck('T', [('BUILT FOR A STRONGER TOMORROW.', blocks)])
    titles = [s.shapes[1].text_frame.text for s in Presentation(buffer).slides
              if len(s.shapes) > 1 and s.shapes[1].has_text_frame][1:-1]
    assert sum(t.startswith('BUILT FOR A STRONGER TOMORROW. (cont.)') for t in titles) <= 1  # only its own overflow
    for heading in ('LUBRICANTS FROM NIGERIA', 'FIVE GRADES. EVERY ENGINE IN THE FLEET', 'SELECTION AND USE'):
        assert heading in titles  # each later group is titled by its own sub-heading


def test_numbered_items_pack_together_and_lists_end_groups(monkeypatch):
    import deck_builder
    from deck_builder import pack_small_sections
    monkeypatch.setattr(deck_builder, 'TEXT_BUDGET', deck_builder.CONTENT_H)  # test the grouping rules alone
    p = lambda: [{'type': 'para', 'text': 'Short line of text here.'}]  # noqa: E731
    out = pack_small_sections([('Why defence needs a trusted source', p()), ('01 Unverified oil', p()),
                               ('02 Defence is strategic', p()), ('Five grades', p()), ('Reading the grade', p())])
    assert [n for n, _ in out] == ['Why defence needs a trusted source', 'Five grades']
    assert [b['text'] for b in out[0][1] if b['type'] == 'sub'] == ['01 Unverified oil', '02 Defence is strategic']


def test_trailing_subheading_moves_to_next_section():
    from pipeline import move_trailing_subheadings
    out = move_trailing_subheadings([('Grades', [{'type': 'para', 'text': 'x'}, {'type': 'sub', 'text': 'DSO-1'}]),
                                     ('Full synthetic engine oil', [{'type': 'para', 'text': 'y'}])])
    assert out[0][1] == [{'type': 'para', 'text': 'x'}]
    assert out[1][1][0] == {'type': 'sub', 'text': 'DSO-1'}


def test_fragment_is_not_used_as_lead():
    from deck_builder import split_lead
    blocks = [{'type': 'para', 'text': 'Engine oil. Radiator coolant. Multi-purpose grease.'},
              {'type': 'para', 'text': 'More.'}]
    assert split_lead(blocks) == (None, blocks)
    lead, rest = split_lead([{'type': 'para', 'text': 'Oil is a small line in any budget and a decisive one. Rest.'}])
    assert lead == 'Oil is a small line in any budget and a decisive one.'


def test_brochure_end_to_end(tmp_path):
    from pipeline import convert
    pptx, md, title, slides = convert(FIXTURES / 'brochure.pdf', tmp_path, 'standard', source_name='brochure.pdf')
    prs = Presentation(str(pptx))
    text = ' '.join(sh.text_frame.text for s in prs.slides for sh in s.shapes if sh.has_text_frame)
    assert '<br>' not in text and '<sup>' not in text
    assert text.count('(cont.)') <= 1
    assert 'Why Defence Needs a Trusted Oil Source' in text
    picture_slides = [i for i, s in enumerate(prs.slides) if any(sh.shape_type == 13 for sh in s.shapes)]
    assert len(picture_slides) >= 3  # images sit with their sections, not piled at the end
    assert len(pictures(prs)) == 3  # the coolant picture appears three times in the PDF, once in the deck


def test_text_per_slide_is_halved():
    """A section that would fill one slide edge to edge now spreads over two, in larger type."""
    from deck_builder import CONTENT_H, CONTENT_W, READABLE_PT, TEXT_BUDGET, build_deck, text_height
    sentence = 'Extreme pressure performance protects against metal contact under heavy load and shock. '
    para = {'type': 'para', 'text': sentence * 3}
    blocks = []
    while text_height(blocks + [para], CONTENT_W, READABLE_PT) <= CONTENT_H * 0.95:
        blocks.append(dict(para))
    assert text_height(blocks, CONTENT_W, READABLE_PT) > TEXT_BUDGET
    buffer, slides = build_deck('T', [('Full slide of text', blocks)])
    assert slides >= 4  # cover, at least two content slides, closing
    sizes = {r.font.size.pt for s in Presentation(buffer).slides for sh in s.shapes if sh.has_text_frame
             for p in sh.text_frame.paragraphs for r in p.runs if r.text.startswith('Extreme')}
    assert min(sizes) >= READABLE_PT


# ---------- handbook-style documents: chapters, front matter, lists ----------
import pdf_structure  # noqa: E402

HANDBOOK = FIXTURES / 'handbook.pdf'


def test_chapters_and_title_are_read_from_font_sizes():
    import pymupdf
    info = pdf_structure.analyze(pymupdf.open(HANDBOOK))
    assert list(info['chapters'].values()) == ['GENERAL CONCEPTS OF FIRST AID', 'CARE FOR ADULTS (18 AND OVER)']
    assert info['cover_title'] == 'FAH First Aid Handbook'  # contents heading and giant numerals ignored
    assert 'FAH' in info['acronyms']


def test_front_matter_is_dropped_and_chapters_marked(tmp_path):
    text = pdf_to_md.convert_pdf(HANDBOOK, tmp_path).read_text()
    assert text.startswith('<!-- pdf2pptx {"pages": 7, "chapters": 2') and '\n# FAH First Aid Handbook\n' in text
    assert 'All rights reserved' not in text and 'CONTENTS' not in text.upper() and '. . . .' not in text
    assert '## General Concepts of First Aid {.chapter}' in text
    assert '## Care for Adults (18 and Over) {.chapter}' in text
    assert text.count('General Concepts of First Aid') == 1  # running header on the next page removed
    assert '## Step by Step' in text and 'CHAPTER' not in text.replace('{.chapter}', '')


def test_handbook_deck_has_chapter_slides_and_numbered_steps(tmp_path):
    from pipeline import convert
    pptx, _, title, slides = convert(HANDBOOK, tmp_path, 'standard', source_name='handbook.pdf')
    prs = Presentation(str(pptx))
    texts = [' | '.join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert title == 'FAH First Aid Handbook'
    assert sum('CHAPTER 1' in t for t in texts) == 1 and sum('CHAPTER 2' in t for t in texts) == 1
    assert 'next 2 chapters' in texts[1]  # the agenda lists chapters, not every section
    steps = next(t for t in texts if 'Step by Step' in t and 'Check that the person' in t)
    assert '3.\tOpen the airway' in steps and 'a.\tPut your hand' in steps and '4.\tGive care' in steps
    wide = next(s for s in prs.slides if any(sh.shape_type == 13 and sh.width > sh.height * 2.2 for sh in s.shapes))
    pic = next(sh for sh in wide.shapes if sh.shape_type == 13)
    assert Emu(pic.width).inches > 8  # the wide diagram gets the slide's full width
    assert_on_slide(prs)


def test_picture_text_toc_and_legal_detection():
    text = 'Before\n\n<!-- Start of picture text -->\nA B C<br>D E F<br><!-- End of picture text -->\n\nAfter'
    assert pdf_structure.strip_picture_text(text).split() == ['Before', 'After']
    toc = '##### **TABLE**<sup>_of_</sup> **CONTENTS**\n\nIntro . . . . . . 4\n\nAED Steps – 15'
    assert pdf_structure.is_toc_page(toc)
    assert pdf_structure.is_toc_page('\n'.join(f'Section {n} . . . . . {n}' for n in range(8)))
    assert not pdf_structure.is_toc_page('## Real section\n\nA paragraph of normal text about first aid.\n\n- one\n- two')
    assert pdf_structure.is_legal_page('Copyright © 2023 Some Publisher\n\nAll rights reserved.')


def test_smart_title_keeps_acronyms():
    assert pdf_structure.smart_title('USE OF AUTOMATED EXTERNAL DEFIBRILLATOR') == 'Use of Automated External Defibrillator'
    assert pdf_structure.smart_title('ONE-RESCUER BLS/CPR FOR ADULTS', {'BLS', 'CPR'}) == 'One-Rescuer BLS/CPR for Adults'
    assert pdf_structure.smart_title('A I RWAY') == 'Airway'
    assert pdf_structure.smart_title('Already Mixed Case') == 'Already Mixed Case'


def test_list_parsing_handles_pdf_quirks():
    md = ('- The importance of early CPR has been re-emphasized. The\n\n'
          '- risk of harm to the patient is low.\n\n'
          '   - A sixth link, Recovery, was added.\n')
    block = parse_blocks(md)[0]
    assert block['items'] == ['The importance of early CPR has been re-emphasized. The risk of harm to the patient is low.',
                              'A sixth link, Recovery, was added.']
    assert block['levels'] == [0, 0]  # stray indentation in a bullet list is not nesting
    steps = parse_blocks('5. Stop pressing.\n\n6. Open the airway.\n\n   - a. Tilt the head.\n\n   - b. Lift the jaw.\n\n7. Give a breath.')[0]
    assert steps['labels'] == ['5.', '6.', 'a.', 'b.', '7.'] and steps['levels'] == [0, 0, 1, 1, 0]
    glued = parse_blocks('- 3.Assess breathing.\n\n- 4.If you cannot feel a pulse, begin CPR.')[0]
    assert glued['labels'] == ['3.', '4.'] and glued['items'][1].startswith('If you cannot')


def test_pull_quote_and_broken_paragraph_are_repaired():
    blocks = parse_blocks('The blood returns to the heart and is pumped to the organs—as\n\n'
                          '_**Taking the right action quickly can make the difference between life and death.**_\n\n'
                          'well as the rest of the body.')
    assert [b['type'] for b in blocks] == ['quote', 'para']
    assert blocks[1]['text'].endswith('organs—as well as the rest of the body.')
    spliced = pdf_to_md.repair_spliced_pullquotes(
        'Once in place, the compression to breath **_The compression rate for_** ratio should be adjusted. '
        '**_all persons is always 100-120 per minute._**')
    assert 'compression to breath ratio should be adjusted.' in spliced
    assert '_**The compression rate for all persons is always 100-120 per minute.**_' in spliced


def test_page_breaks_keep_sub_points_and_avoid_orphans():
    from deck_builder import CONTENT_W, TEXT_BUDGET, split_blocks
    quiz = {'type': 'list', 'ordered': True, 'items': [], 'labels': [], 'levels': []}
    for n in range(1, 6):
        quiz['items'] += [f'Question number {n} asks what the rescuer should do next in this situation?'] + \
                         [f'Option {x} for question {n}' for x in 'abcd']
        quiz['labels'] += [f'{n}.', 'a.', 'b.', 'c.', 'd.']
        quiz['levels'] += [0, 1, 1, 1, 1]
    pages = split_blocks([quiz], CONTENT_W, TEXT_BUDGET)
    assert len(pages) >= 2
    for page in pages:
        assert page[0]['levels'][0] == 0  # no slide opens with a stranded "b." option
        assert page[0]['labels'].count('d.') == sum(lv == 0 for lv in page[0]['levels'])  # whole questions only
    intro = [{'type': 'para', 'text': 'Keys for first aid:'},
             {'type': 'list', 'ordered': False, 'items': ['Short point about care.'] * 9}]
    pages = split_blocks(intro, CONTENT_W, TEXT_BUDGET)
    assert pages[0][0]['type'] == 'para' and pages[0][1]['type'] == 'list'  # the intro line stays with its list
    assert all(sum(len(b.get('items', [1])) for b in pg) >= 2 for pg in pages)  # no single-item last page


def test_long_documents_use_fuller_slides():
    import deck_builder
    para = {'type': 'para', 'text': 'Extreme pressure performance protects against metal contact under load. ' * 6}
    sections = [(f'Topic {n}', [dict(para), dict(para), dict(para)]) for n in range(60)]
    _, dense = deck_builder.build_deck('Long', sections)
    half = deck_builder._build('Long', sections, 'Presentation', False, True).page  # at the default budget
    assert half > deck_builder.DENSE_AFTER and dense < half * 0.8
    assert deck_builder.TEXT_BUDGET == deck_builder.CONTENT_H * deck_builder.TEXT_FILL  # budget restored afterwards


def test_repeated_section_headings_are_not_chapters(tmp_path):
    """A heading size used several times per page is a section heading, however large."""
    import pymupdf
    doc = pymupdf.open()
    for n in range(4):
        page = doc.new_page()
        if n == 0:
            page.insert_text((60, 80), 'Quarterly Operations Report', fontsize=22)
        for k, y in enumerate((150, 420)):
            page.insert_text((60, y), f'Section {n}-{k} Heading', fontsize=20)
            page.insert_textbox((60, y + 15, 520, y + 200), 'Body text about operations and results. ' * 25, fontsize=10)
    info = pdf_structure.analyze(doc)
    assert info['chapters'] == {}
    assert info['cover_title'] == 'Quarterly Operations Report'  # the heading further down is not swept in


def test_headingless_document_uses_its_title_on_slides(tmp_path):
    md = tmp_path / 'document.md'
    md.write_text('\n\n'.join(['A plain paragraph of body text about automation for small businesses. ' * 4] * 12))
    buffer, title, _ = build_from_markdown(md, 'standard', pdf_path='field_notes.pdf')
    text = ' '.join(sh.text_frame.text for s in Presentation(buffer).slides for sh in s.shapes if sh.has_text_frame)
    assert title == 'Field Notes' and 'Overview' not in text and 'Field Notes (cont.)' in text
    assert not pdf_to_md._good_title('(anonymous)')


def test_repeated_banner_graphics_are_dropped(tmp_path):
    """A running header drawn as artwork repeats on every page of its chapter."""
    (tmp_path / 'images').mkdir()
    banner = Image.new('RGB', (900, 220), 'white')
    banner.paste((30, 60, 140), (40, 60, 620, 160))
    for n in range(3):
        banner.save(tmp_path / 'images' / f'h{n}.png')
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'photo.png')
    md = '\n\n'.join(f'![](images/{n})' for n in ('h0.png', 'photo.png', 'h1.png', 'h2.png'))
    assert pdf_to_md.filter_images(md, tmp_path).strip() == '![](images/photo.png)'


def test_page_limits(tmp_path, monkeypatch):
    """Standard takes long PDFs; the AI slide types have a lower cap with a message pointing to Standard."""
    import pymupdf
    assert pdf_to_md.MAX_PAGES >= 300
    doc = pymupdf.open()
    for n in range(6):
        doc.new_page().insert_textbox((60, 60, 520, 700), f'Page {n} body text about first aid and care. ' * 20, fontsize=10)
    doc.save(tmp_path / 'six.pdf')
    monkeypatch.setattr(pdf_to_md, 'MAX_AI_PAGES', 5)
    assert pdf_to_md.convert_pdf(tmp_path / 'six.pdf', tmp_path / 'a').exists()  # Standard: fine
    with pytest.raises(pdf_to_md.PdfConversionError, match='choose Standard'):
        pdf_to_md.convert_pdf(tmp_path / 'six.pdf', tmp_path / 'b', uses_llm=True)
    monkeypatch.setattr(pdf_to_md, 'MAX_PAGES', 5)
    with pytest.raises(pdf_to_md.PdfConversionError, match='the limit is 5'):
        pdf_to_md.convert_pdf(tmp_path / 'six.pdf', tmp_path / 'c')


# ---------- LLM client: retired models, rotation ----------
class _Resp:
    def __init__(self, code, body=None, headers=None):
        self.status_code, self._body, self.headers = code, body or {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def _ok(obj):
    return _Resp(200, {'choices': [{'message': {'content': json.dumps(obj)}}]})


def test_models_are_discovered_and_retired_ones_skipped(monkeypatch):
    monkeypatch.undo()  # use the real available_models for this test
    condense._models.update({'for': None, 'list': None})
    listing = {'data': [{'id': 'whisper-large-v3', 'context_window': 448}, {'id': 'openai/gpt-oss-20b', 'context_window': 131072},
                        {'id': 'qwen/qwen3.8-27b', 'context_window': 131072}, {'id': 'tiny-chat', 'context_window': 4096},
                        {'id': 'meta-llama/llama-prompt-guard-2-22m', 'context_window': 512}]}
    monkeypatch.setattr(condense.requests, 'get', lambda *a, **k: _Resp(200, listing))
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    monkeypatch.setenv('LLM_MODEL', 'llama-3.3-70b-versatile')  # configured but retired
    assert condense.available_models() == ['qwen/qwen3.8-27b', 'openai/gpt-oss-20b']
    condense._models.update({'for': None, 'list': None})
    monkeypatch.setattr(condense.requests, 'get', lambda *a, **k: (_ for _ in ()).throw(condense.requests.ConnectionError()))
    assert condense.available_models() == ['llama-3.3-70b-versatile']  # list unavailable: trust the setting
    condense._models.update({'for': None, 'list': None})


def test_rate_limited_model_is_skipped_without_waiting(monkeypatch):
    monkeypatch.setattr(condense, 'available_models', lambda cfg=None: ['model-a', 'model-b'])
    used, slept = [], []

    def post(url, headers=None, json=None, timeout=None):
        used.append(json['model'])
        return _Resp(429, headers={'retry-after': '30'}) if json['model'] == 'model-a' else _ok({'title': 'ok'})
    monkeypatch.setattr(condense.requests, 'post', post)
    monkeypatch.setattr(condense.time, 'sleep', slept.append)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    assert condense.chat_json('s', 'u') == {'title': 'ok'}
    assert used == ['model-a', 'model-b'] and slept == []
    assert condense.chat_json('s', 'u') == {'title': 'ok'} and used[-1] == 'model-b'  # model-a still cooling down


def test_retired_model_falls_through_to_the_next(monkeypatch):
    models = ['old-model', 'new-model']
    monkeypatch.setattr(condense, 'available_models', lambda cfg=None: list(models))
    monkeypatch.setattr(condense, '_drop_model', lambda m: models.remove(m))

    def post(url, headers=None, json=None, timeout=None):
        if json['model'] == 'old-model':
            return _Resp(404, {'error': {'message': 'The model `old-model` does not exist', 'code': 'model_not_found'}})
        return _ok({'fixed': ['x']})
    monkeypatch.setattr(condense.requests, 'post', post)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    assert condense.chat_json('s', 'u') == {'fixed': ['x']} and models == ['new-model']


def test_json_wrapped_in_prose_is_still_read():
    assert condense._parse_json('Here you go:\n```json\n{"a": 1}\n```') == {'a': 1}


# ---------- the meaning layer: products, prices, processes ----------
import semantics  # noqa: E402

BROCHURE_MD = '''<!-- pdf2pptx {"pages": 5, "chapters": 0, "subtitle": "Field radios and accessories for licensed operators.", "kicker": "PRODUCT BROCHURE | 2026"} -->

# Field Radios & Accessories

### 01 / Handheld Radio

## TR-5

Rugged handheld radio with long-life battery

![](images/photo2.png)

The TR-5 is a rugged handheld radio built for security teams. It pairs a long-life battery with a sealed housing and simple controls.

### Technical Data

|Model|**Handheld, sealed**|
|---|---|
|Range|**8 km**|
|Battery|**18 hours**|
|Weight|**310 g**|

PRICE

₦ 400,000 per unit

Exclusive of VAT and applicable taxes

**Documentation required:** a valid operator licence before an invoice is issued.

## How to order

### Send an enquiry

**1**

Tell us the products and quantities you need.

**2**

### Submit documentation

Provide a valid operator licence.

### Receive your invoice

**3**

Once documents are approved, an invoice is issued.

### Price Summary

|TR-5 Handheld Radio|**₦400,000 / unit**|
|---|---|
|Spare battery|**₦35,000 / piece**|

Prices are exclusive of VAT. Quotations are valid for 30 days.
'''


@pytest.fixture
def brochure_md(tmp_path):
    (tmp_path / 'images').mkdir()
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'photo2.png')
    md = tmp_path / 'document.md'
    md.write_text(BROCHURE_MD)
    return md


def test_brochure_pieces_are_recognised(brochure_md):
    _, sections = load_sections(brochure_md)
    doc_meta = semantics.read_doc_meta(brochure_md.read_text())
    structured = semantics.structure(sections)
    assert semantics.classify(doc_meta, structured) == 'brochure'
    assert [n for n, _ in structured] == ['TR-5', 'How to order', 'Price Summary']
    product = dict(structured)['TR-5']
    meta = semantics.get_meta(product)
    assert meta['layout'] == 'product' and meta['kicker'] == '01 / Handheld Radio'
    assert meta['tagline'] == 'Rugged handheld radio with long-life battery'
    price = next(b for b in product if b['type'] == 'price')
    assert (price['amount'], price['unit'], price['note']) == ('₦400,000', 'per unit', 'Exclusive of VAT and applicable taxes')
    table = next(b for b in product if b['type'] == 'table')
    assert table['kv'] and table['label'] == 'Technical Data'
    steps = next(b for b in dict(structured)['How to order'] if b['type'] == 'steps')
    assert [i['title'] for i in steps['items']] == ['Send an enquiry', 'Submit documentation', 'Receive your invoice']
    assert not any(b['type'] == 'para' and b['text'].strip('*') in '123' for _, bl in structured for b in bl)


def test_brochure_deck_is_one_slide_per_unit(brochure_md):
    buffer, title, slides = build_from_markdown(brochure_md, 'standard')
    prs = Presentation(buffer)
    texts = [' | '.join(sh.text_frame.text for sh in s.shapes if sh.has_text_frame) for s in prs.slides]
    assert slides == 5  # cover, product, process, price summary, closing: no agenda for a short brochure
    assert 'PRODUCT BROCHURE | 2026' in texts[0] and 'Field radios and accessories' in texts[0]
    product = next(t for t in texts if 'TR-5' in t and '₦400,000' in t)
    for piece in ('01 / HANDHELD RADIO', 'Rugged handheld radio', 'TECHNICAL DATA', 'per unit', 'Documentation required'):
        assert piece in product
    product_slide = prs.slides[texts.index(product)]
    assert any(sh.shape_type == 13 for sh in product_slide.shapes) and any(sh.has_table for sh in product_slide.shapes)
    spec = next(sh.table for sh in product_slide.shapes if sh.has_table)
    assert spec.cell(0, 0).text == 'Model' and not spec.first_row  # a spec sheet: no header row styling
    process = next(t for t in texts if 'How to order' in t)
    assert all(step in process for step in ('Send an enquiry', 'Submit documentation', 'Receive your invoice'))
    assert_on_slide(prs)


def test_prose_documents_are_not_mistaken_for_brochures():
    _, sections = load_sections(SAMPLE)
    structured = semantics.structure(sections)
    assert semantics.classify({'pages': 40, 'chapters': 0}, structured) == 'document'
    assert semantics.classify({'pages': 40, 'chapters': 5}, structured) == 'handbook'
    assert not any(semantics.layout_of(b) for _, b in structured)
    # ordinary sub-headings with a paragraph each are not a numbered process
    blocks = parse_blocks('### Be Safe\n\nMake sure the scene is safe.\n\n### Call EMS\n\nSend someone.\n\n### CPR\n\nBegin.')
    assert semantics.find_steps(blocks) == blocks


def test_price_boxes_survive_picture_text_stripping():
    page = ('![](img/p-11.png)\n\n<!-- Start of picture text -->\nPRICE<br><!-- End of picture text -->\n\n'
            '![](img/p-12.png)\n\n<!-- Start of picture text -->\n₦ 4,000,000<br>per unit<br><!-- End of picture text -->\n\n'
            '![](img/fig.png)\n\n<!-- Start of picture text -->\nA B C<br>D E F<br><!-- End of picture text -->\n')
    out = pdf_structure.strip_picture_text(page)
    assert 'PRICE' in out and '₦ 4,000,000 per unit' in out   # real content drawn as a box: kept as text
    assert 'p-11.png' not in out and 'p-12.png' not in out      # ...and the pictures of those boxes dropped
    assert 'fig.png' in out and 'A B C' not in out              # a figure keeps its image, loses its labels


def test_cover_tagline_and_label_are_kept():
    cover = '**E I G E R** S E C U R I T Y\n\n**PRODUCT BROCHURE | 2026**\n\n# **Tactical Kit**\n\nPlatforms and accessories for licensed security and military clients.'
    assert pdf_structure.cover_extras(cover, 'Tactical Kit') == (
        'Platforms and accessories for licensed security and military clients.', 'PRODUCT BROCHURE | 2026')


def test_missed_images_are_recovered(tmp_path):
    """A picture the layout model did not mark is rendered and referenced at its reading position."""
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(60, 200, 500, 420), filename=str(FIXTURES / 'images' / 'photo2.png'))
    chunks = [{'text': 'HEADING\n\nBody text.', 'page_boxes': [
        {'class': 'section-header', 'bbox': (60, 100, 300, 130), 'pos': (0, 7)},
        {'class': 'text', 'bbox': (60, 500, 500, 520), 'pos': (9, 19)}]}]
    pdf_structure.recover_missed_images(doc, chunks, str(tmp_path))
    text = chunks[0]['text']
    assert text.index('HEADING') < text.index('![](') < text.index('Body text.')
    assert (tmp_path / 'missed-0001-00.png').is_file()
    # a region the model already captured as a picture or table is left alone
    seen = [{'text': 'x', 'page_boxes': [{'class': 'table', 'bbox': (60, 200, 500, 420), 'pos': (0, 1)}]}]
    assert '![](' not in pdf_structure.recover_missed_images(doc, seen, str(tmp_path))[0]['text']


def test_ai_modes_leave_product_and_process_slides_alone(brochure_md, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(condense, 'chat_json', fake)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    for mode in ('condensed', 'detailed'):
        buffer, _, _ = build_from_markdown(brochure_md, mode)
        text = ' '.join(sh.text_frame.text for s in Presentation(buffer).slides for sh in s.shapes if sh.has_text_frame)
        assert '₦400,000' in text and 'TECHNICAL DATA' in text and 'Submit documentation' in text
    rewrites = [c for c in fake.calls if 'Summarise the text' in c or 'Expand the text' in c]
    assert not any('TR-5 is a rugged' in call for call in rewrites)  # the product copy was never sent for rewriting


# ---------- the AI planner (LLM stubbed): validated suggestions, rules as the fallback ----------
import planner  # noqa: E402

HARD_BROCHURE_MD = re.sub(r'\n\*\*\d\*\*\n', '\n', BROCHURE_MD).replace('### Technical Data', '## Technical Data')


@pytest.fixture
def hard_sections(tmp_path):
    """A brochure the rules cannot fully resolve: no step numbers, the spec table as its own section."""
    (tmp_path / 'images').mkdir()
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'photo2.png')
    md = tmp_path / 'document.md'
    md.write_text(HARD_BROCHURE_MD)
    return semantics.structure(load_sections(md)[1])


def test_rules_alone_leave_the_hard_brochure_fragmented(hard_sections):
    assert [n for n, _ in hard_sections] == ['TR-5', 'Technical Data', 'How to order']
    assert not any(semantics.layout_of(b) for _, b in hard_sections)


def test_planner_merges_fragments_and_promotes_layouts(hard_sections):
    plan = {'kind': 'brochure', 'sections': [{'id': 0, 'layout': 'product'}, {'id': 1, 'merge_into_previous': True},
                                             {'id': 2, 'layout': 'steps'}]}
    kind, sections, changes = planner.apply_plan(plan, hard_sections, 'document')
    assert kind == 'brochure' and changes >= 2
    layouts = {n: semantics.layout_of(b) for n, b in sections}
    assert layouts['TR-5'] == 'product' and layouts['How to order'] == 'steps' and 'Technical Data' not in layouts
    product = dict(sections)['TR-5']
    assert any(b['type'] == 'price' for b in product) and next(b for b in product if b['type'] == 'table')['kv']
    assert planner._total_words(sections) >= planner._total_words(hard_sections) * 0.95  # nothing lost


def test_planner_suggestions_that_cannot_be_carried_out_are_ignored():
    sections = [('Overview', [{'type': 'para', 'text': 'Plain text about first aid kits and gloves.'}]),
                ('Details', [{'type': 'para', 'text': 'More plain text about bandages.'}]),
                ('Care', [{'type': 'para', 'text': 'Text.'}])]
    plan = {'kind': 'spaceship', 'sections': [
        {'id': 0, 'layout': 'product'},                 # no picture, no spec sheet
        {'id': 1, 'layout': 'steps'},                   # no step headings
        {'id': 2, 'title': 'Completely Invented Marketing Headline'},   # 'Care' is not generic
        {'id': 0, 'title': 'Quantum Blockchain Synergy'},               # generic title, but invented words
        {'id': 99, 'layout': 'steps'}, 'nonsense', {'id': 'x'}]}
    kind, out, changes = planner.apply_plan(plan, sections, 'document')
    assert (kind, out, changes) == ('document', sections, 0)
    kind, out, changes = planner.apply_plan({'sections': [{'id': 0, 'title': 'First Aid Kits and Gloves'}]}, sections, 'document')
    assert out[0][0] == 'First Aid Kits and Gloves' and changes == 1  # generic title, the section's own words
    merged_all = {'sections': [{'id': 1, 'merge_into_previous': True}, {'id': 2, 'merge_into_previous': True}]}
    assert planner.apply_plan(merged_all, sections, 'document')[1] == sections  # merging most sections: not trusted
    assert planner.apply_plan(['not', 'a', 'dict'], sections, 'document') == ('document', sections, 0)


def test_planner_falls_back_to_rules_when_the_llm_fails(hard_sections, monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'test')

    def boom(*a, **k):
        raise condense.LLMError('The AI service is busy (rate limit).')
    monkeypatch.setattr(condense, 'chat_json', boom)
    assert planner.refine('T', {'pages': 5}, hard_sections, 'brochure') == ('brochure', hard_sections, 'rules')
    # a plan that would lose content is rejected as a whole
    monkeypatch.setattr(condense, 'chat_json', lambda *a, **k: {'kind': 'brochure', 'sections': []})
    monkeypatch.setattr(planner, 'apply_plan', lambda plan, s, k: (k, s[:1], 1))
    assert planner.refine('T', {'pages': 5}, hard_sections, 'brochure')[2] == 'rules'
    monkeypatch.undo()
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    assert planner.refine('T', {'pages': 5}, hard_sections, 'brochure')[2] == 'rules'  # no key: rules, no request


def test_standard_mode_uses_the_planner_when_a_key_is_set(tmp_path, monkeypatch):
    (tmp_path / 'images').mkdir()
    shutil.copy(FIXTURES / 'images' / 'photo2.png', tmp_path / 'images' / 'photo2.png')
    md = tmp_path / 'document.md'
    md.write_text(HARD_BROCHURE_MD)
    monkeypatch.setenv('GROQ_API_KEY', 'test')
    seen = []

    def fake(system, user, max_tokens=1500, retries=5):
        seen.append(user)
        return {'kind': 'brochure', 'sections': [{'id': 0, 'layout': 'product'}, {'id': 1, 'merge_into_previous': True},
                                                 {'id': 2, 'layout': 'steps'}]}
    monkeypatch.setattr(condense, 'chat_json', fake)
    buffer, _, slides = build_from_markdown(md, 'standard')
    assert slides == 5 and json.loads((tmp_path / 'plan.json').read_text()) == {'kind': 'brochure', 'planner': 'ai'}
    outline = seen[0]
    assert '"TR-5"' in outline and 'table 4x2 (label/value)' in outline
    assert 'sealed housing and simple controls' not in outline  # an outline, not the document's full text
    monkeypatch.setattr(condense, 'chat_json', lambda *a, **k: (_ for _ in ()).throw(condense.LLMError('down')))
    buffer, _, slides = build_from_markdown(md, 'standard')
    assert slides >= 5 and json.loads((tmp_path / 'plan.json').read_text())['planner'] == 'rules'  # still a deck


def test_app_reports_how_the_deck_was_planned(client):
    with open(SAMPLE_PDF, 'rb') as f:
        res = client.post('/convert', data={'pdf': (f, 'sample.pdf'), 'mode': 'standard'})
    assert res.status_code == 200 and res.headers['X-Planner'] == 'rules' and res.headers['X-Kind'] in planner.KINDS


# ---------- the setup page is for the owner only ----------
@pytest.fixture
def open_web(monkeypatch):
    """The app with storage configured and a setup token set, visited by someone who is NOT signed in."""
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv('SETUP_TOKEN', SETUP_TOKEN)
    import app as webapp
    import b2_admin
    calls = []
    monkeypatch.setattr(b2_admin, 'configure_bucket', lambda *a, **k: calls.append(a) or {'bucket': 'x', 'origins': ['*']})
    monkeypatch.setattr(b2_admin, 'check_app_key', lambda: None)
    monkeypatch.setattr(webapp.time, 'sleep', lambda s: None)
    return webapp.app.test_client(), calls


SECRETS_ON_THE_PAGE = ('B2_KEY_ID', 'B2_BUCKET', 'my-bucket', 'Master', 'Vercel settings', 'Backblaze')


def test_setup_does_not_exist_without_a_token(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    import app as webapp
    client = webapp.app.test_client()
    for token in (None, '', 'short', 'x' * 15):  # unset, empty, or too short to resist guessing
        monkeypatch.delenv('SETUP_TOKEN', raising=False) if token is None else monkeypatch.setenv('SETUP_TOKEN', token)
        for res in (client.get('/setup'), client.post('/setup', data={'password': token or ''}),
                    client.post('/setup', data={'key_id': 'm', 'key': 'k'})):
            assert res.status_code == 404
            assert not any(word in res.data.decode() for word in SECRETS_ON_THE_PAGE)
    assert client.get('/setup').data == client.get('/no-such-page').data  # indistinguishable from any unknown address


def test_setup_shows_nothing_until_the_password_is_entered(open_web):
    client, calls = open_web
    res = client.get('/setup')
    html = res.data.decode()
    assert res.status_code == 401 and 'name="password"' in html
    assert not any(word in html for word in SECRETS_ON_THE_PAGE)
    assert res.headers['Cache-Control'] == 'no-store' and 'noindex' in res.headers['X-Robots-Tag']
    for wrong in ('', 'guess', SETUP_TOKEN[:-1], SETUP_TOKEN + 'x', SETUP_TOKEN.upper()):
        res = client.post('/setup', data={'password': wrong})
        assert res.status_code == 401 and 'Set-Cookie' not in res.headers
        assert not any(word in res.data.decode() for word in SECRETS_ON_THE_PAGE)
    assert client.get('/setup').status_code == 401  # still locked after the failed attempts


def test_setup_cannot_be_run_without_signing_in(open_web):
    client, calls = open_web
    res = client.post('/setup', data={'key_id': 'master', 'key': 'SECRET', 'origins': ''})
    assert res.status_code == 401 and calls == []  # a master key posted by a stranger goes nowhere
    client.set_cookie('setup_auth', 'forged-value', path='/setup')
    assert client.post('/setup', data={'key_id': 'master', 'key': 'SECRET'}).status_code == 401 and calls == []
    client.set_cookie('setup_auth', '', path='/setup')
    assert client.get('/setup').status_code == 401


def test_owner_signs_in_with_the_token(open_web):
    client, calls = open_web
    res = client.post('/setup', data={'password': SETUP_TOKEN})
    cookie = res.headers['Set-Cookie']
    assert res.status_code == 303 and 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Path=/setup' in cookie
    assert SETUP_TOKEN not in cookie  # the browser never holds the password itself
    html = client.get('/setup').data.decode()
    assert 'Vercel settings' in html and 'my-bucket' in html
    assert client.post('/setup', data={'key_id': 'master', 'key': 'SECRET'}).status_code == 200 and len(calls) == 1


def test_a_changed_token_signs_everyone_out(open_web, monkeypatch):
    client, _ = open_web
    signed_in(client)
    assert client.get('/setup').status_code == 200
    monkeypatch.setenv('SETUP_TOKEN', 'a-different-owner-token-5678')
    assert client.get('/setup').status_code == 401
    monkeypatch.delenv('SETUP_TOKEN')
    assert client.get('/setup').status_code == 404  # removing the token removes the page


def test_nothing_public_mentions_the_setup_page(client, cloud):
    for c in (client, cloud[0]):
        html = c.get('/').data.decode()
        assert '/setup' not in html and 'Storage setup' not in html


# ---------- choosing sections: plan first, build after ----------
from pipeline import build_from_plan, outline, plan_from_markdown, sections_from_plan  # noqa: E402


@pytest.fixture
def handbook_plan(tmp_path):
    md = pdf_to_md.convert_pdf(HANDBOOK, tmp_path / 'markdown')
    plan = plan_from_markdown(md, 'standard', pdf_path='handbook.pdf')
    return plan, md.parent


def test_plan_is_portable_json(handbook_plan):
    plan, md_dir = handbook_plan
    restored = json.loads(json.dumps(plan))  # what the browser holds and sends back
    assert restored['v'] == 1 and restored['title'] == 'FAH First Aid Handbook'
    images = [b for s in restored['sections'] for b in s['blocks'] if b['type'] == 'image']
    assert images and all(not Path(b['path']).is_absolute() for b in images)  # no server paths leave the server
    rows = outline(restored, md_dir)
    chapters = [r for r in rows if r['chapter']]
    assert [r['name'] for r in chapters] == ['General Concepts of First Aid', 'Care for Adults (18 and Over)']
    assert all(r['slides'] >= 1 for r in rows) and all(r['group'] is not None for r in rows)
    steps = next(r for r in rows if r['name'] == 'Step by Step')
    assert steps['group'] == chapters[1]['id'] and 'Check that the person' in steps['preview']


def test_only_chosen_sections_are_built(handbook_plan):
    plan, md_dir = handbook_plan
    rows = outline(plan, md_dir)
    pick = next(r for r in rows if r['name'] == 'Step by Step')
    buffer, _, slides = build_from_plan(plan, md_dir, 'standard', 'mint', [pick['id']])
    text = ' '.join(sh.text_frame.text for s in Presentation(buffer).slides for sh in s.shapes if sh.has_text_frame)
    assert 'Check that the person' in text and 'Open the airway' in text
    assert 'First aid refers to' not in text and 'What is the first thing to check' not in text
    # its chapter's opening section was not chosen, but the chapter still gets its divider
    assert 'CHAPTER 1' in text and 'Care for Adults (18 and Over)' in text and 'General Concepts' not in text


def test_choosing_nothing_or_bad_plans_are_refused(handbook_plan, tmp_path):
    plan, md_dir = handbook_plan
    with pytest.raises(ValueError, match='at least one'):
        build_from_plan(plan, md_dir, 'standard', 'coral', [9999])
    for bad in ({}, {'v': 2, 'sections': []}, {'v': 1, 'sections': 'nope'}, [1, 2]):
        with pytest.raises(ValueError):
            sections_from_plan(bad, md_dir)


def test_a_tampered_plan_cannot_reach_other_files(handbook_plan, tmp_path):
    plan, md_dir = handbook_plan
    secret = tmp_path / 'secret.png'
    shutil.copy(FIXTURES / 'images' / 'photo2.png', secret)
    evil = json.loads(json.dumps(plan))
    evil['sections'][0]['blocks'] += [{'type': 'image', 'path': '../secret.png', 'caption': ''},
                                      {'type': 'image', 'path': str(secret), 'caption': ''},
                                      {'type': 'image', 'path': '/etc/passwd', 'caption': ''},
                                      {'type': 'script', 'text': 'x'}, 'not a block']
    sections = sections_from_plan(evil, md_dir)
    paths = [b['path'] for _, bl in sections for b in bl if b['type'] == 'image']
    assert paths and all(Path(p).resolve().is_relative_to(Path(md_dir).resolve()) for p in paths)
    assert not any(b['type'] == 'script' for _, bl in sections for b in bl)


def test_plan_then_build_routes(client):
    import io
    import gzip
    with open(HANDBOOK, 'rb') as f:
        res = client.post('/plan', data={'pdf': (f, 'handbook.pdf'), 'mode': 'standard'})
    assert res.status_code == 200, res.data[:300]
    data = res.json
    assert data['title'] == 'FAH First Aid Handbook' and data['sections'] and data['plan']['v'] == 1
    quiz = next(r for r in data['sections'] if r['name'] == 'Self-Assessment')
    packed = gzip.compress(json.dumps(data['plan']).encode())  # as the browser sends it
    with open(HANDBOOK, 'rb') as f:
        res = client.post('/build', data={'pdf': (f, 'handbook.pdf'), 'mode': 'standard', 'theme': 'mint',
                                          'selected': str(quiz['id']), 'plan': (io.BytesIO(packed), 'plan.json.gz')})
    assert res.status_code == 200, res.data[:300]
    text = ' '.join(sh.text_frame.text for s in Presentation(io.BytesIO(res.data)).slides
                    for sh in s.shapes if sh.has_text_frame)
    assert 'What is the first thing to check' in text and 'Open the airway' not in text
    with open(HANDBOOK, 'rb') as f:  # nothing chosen
        res = client.post('/build', data={'pdf': (f, 'handbook.pdf'), 'selected': '',
                                          'plan': (io.BytesIO(packed), 'plan.json.gz')})
    assert res.status_code == 400 and 'at least one' in res.json['error']
    with open(HANDBOOK, 'rb') as f:  # a damaged section list
        res = client.post('/build', data={'pdf': (f, 'handbook.pdf'), 'selected': '1',
                                          'plan': (io.BytesIO(b'\x1f\x8bnot gzip'), 'plan.json.gz')})
    assert res.status_code == 400 and 'upload the PDF again' in res.json['error']


def test_cloud_upload_is_kept_until_the_deck_is_built(cloud):
    import io
    client, fake = cloud
    path = 'uploads/' + 'ef' * 16 + '.pdf'
    fake.uploads[path] = HANDBOOK
    res = client.post('/plan', data={'storage_path': path, 'filename': 'handbook.pdf', 'mode': 'standard'})
    assert res.status_code == 200 and fake.deleted == []  # still needed for step 2
    plan = res.json['plan']
    res = client.post('/build', data={'storage_path': path, 'filename': 'handbook.pdf', 'mode': 'standard',
                                      'selected': '0,1', 'plan': (io.BytesIO(json.dumps(plan).encode()), 'plan.json')})
    assert res.status_code == 200 and res.json['download_url'] and fake.deleted == [path]


# ---------- OCR for scanned PDFs ----------
import ocr  # noqa: E402


def scanned_copy(src, dest, pages=None, dpi=150):
    """A PDF whose pages are only pictures of the source pages: no text layer, like a scan."""
    import pymupdf
    doc, out = pymupdf.open(src), pymupdf.open()
    for n, page in enumerate(doc):
        if pages is None or n in pages:
            pix = page.get_pixmap(dpi=dpi)
            q = out.new_page(width=page.rect.width, height=page.rect.height)
            q.insert_image(q.rect, pixmap=pix)
        else:
            out.insert_pdf(doc, from_page=n, to_page=n)
    out.save(dest)
    return dest


def test_ocr_language_data_ships_with_the_app():
    assert ocr.available() and (ocr.TESSDATA / 'eng.traineddata').stat().st_size > 1_000_000


def test_scanned_pdf_is_read_with_ocr(tmp_path):
    import pymupdf
    scan = scanned_copy(HANDBOOK, tmp_path / 'scan.pdf')
    assert sum(len(p.get_text()) for p in pymupdf.open(scan)) == 0
    assert ocr.pages_needing_ocr(pymupdf.open(scan)) == list(range(7))
    md = pdf_to_md.convert_pdf(scan, tmp_path / 'out').read_text()
    assert '"ocr_pages": 7' in md
    assert 'Check that the person responds' in md and 'Put your hand on the forehead' in md
    # display-size chapter titles are found even though OCR reads them from a picture
    assert '## General Concepts of First Aid {.chapter}' in md and '## Care for Adults (18 and Over) {.chapter}' in md
    assert 'All rights reserved' not in md  # front matter still recognised and dropped


def test_only_scanned_pages_are_ocred(tmp_path):
    import pymupdf
    mixed = scanned_copy(HANDBOOK, tmp_path / 'mixed.pdf', pages={5})
    assert ocr.pages_needing_ocr(pymupdf.open(mixed)) == [5]
    md = pdf_to_md.convert_pdf(mixed, tmp_path / 'out').read_text()
    assert '"ocr_pages": 1' in md and 'Check that the person responds' in md and 'First aid refers to' in md


def test_scanned_deck_end_to_end(tmp_path):
    from pipeline import convert
    pptx, _, title, slides = convert(scanned_copy(HANDBOOK, tmp_path / 'scan.pdf'), tmp_path, 'standard')
    text = ' '.join(sh.text_frame.text for s in Presentation(str(pptx)).slides for sh in s.shapes if sh.has_text_frame)
    assert slides >= 6 and 'CHAPTER 1' in text and 'Open the airway' in text


def test_ocr_limits_and_missing_language_data(tmp_path, monkeypatch):
    scan = scanned_copy(HANDBOOK, tmp_path / 'scan.pdf')
    monkeypatch.setattr(ocr, 'MAX_OCR_PAGES', 3)
    with pytest.raises(pdf_to_md.PdfConversionError, match='7 scanned pages.*up to 3 at a time'):
        pdf_to_md.convert_pdf(scan, tmp_path / 'a')
    monkeypatch.setattr(ocr, 'TESSDATA', tmp_path / 'no-tessdata')
    with pytest.raises(pdf_to_md.PdfConversionError, match='No readable text was found in this PDF'):
        pdf_to_md.convert_pdf(scan, tmp_path / 'b')


def test_ocr_noise_is_dropped():
    for junk in ('y', 'yy', '= | ~', '@#', '—'):
        assert ocr._is_noise(junk)
    for keep in ('1', '02', 'MS-5', 'Send an enquiry', 'Be Safe'):
        assert not ocr._is_noise(keep)
