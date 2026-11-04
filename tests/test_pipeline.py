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
    assert res.status_code == 400 and 'AI key' in res.json['error']


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


@pytest.fixture
def setup_client(monkeypatch):
    for k, v in B2_ENV.items():
        monkeypatch.setenv(k, v)
    import app as webapp
    import b2_admin
    api = FakeB2Api()
    monkeypatch.setattr(b2_admin.requests, 'get', api.get)
    monkeypatch.setattr(b2_admin.requests, 'post', api.post)
    monkeypatch.setattr(b2_admin, 'check_app_key', lambda: None)
    return webapp.app.test_client(), api


def test_setup_page_lists_missing_settings(monkeypatch):
    for k in B2_ENV:
        monkeypatch.delenv(k, raising=False)
    import app as webapp
    html = webapp.app.test_client().get('/setup').data.decode()
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
    assert '## BUILT FOR A STRONGER TOMORROW.' in out and '## WELCOME' in out and '## WHY DEFENCE' in out
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
    assert sum('(cont.)' in t for t in titles) == 0
    assert 'FIVE GRADES. EVERY ENGINE IN THE FLEET' in titles  # a later group titled by its sub-heading


def test_numbered_items_pack_together_and_lists_end_groups():
    from deck_builder import pack_small_sections
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
    assert 'WHY DEFENCE NEEDS A TRUSTED OIL SOURCE' in text
    picture_slides = [i for i, s in enumerate(prs.slides) if any(sh.shape_type == 13 for sh in s.shapes)]
    assert len(picture_slides) >= 3  # images sit with their sections, not piled at the end
    assert len(pictures(prs)) == 3  # the coolant picture appears three times in the PDF, once in the deck
