"""
Flask web app: upload a PDF, choose a slide type and theme, download the .pptx.

Stateless, so it runs on serverless hosts (Vercel) as well as a normal server: each
request converts in its own temporary folder.

Two upload routes:
  - Backblaze B2 (when configured): the browser gets a one-time upload link from
    /upload-url, uploads the PDF straight to B2 (up to 100 MB) and sends its path; the
    result comes back as a signed download link. No size limit from Vercel.
  - Direct: the PDF is posted in the request and the file is returned in the response.
    On Vercel this is capped at 4.5 MB each way.
"""
import io
import logging
import os
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file

load_dotenv()

import condense  # noqa: E402  (reads LLM settings from the environment loaded above)
import storage  # noqa: E402
from deck_builder import DEFAULT_THEME, THEMES, shrink_pptx  # noqa: E402
from pdf_to_md import PdfConversionError  # noqa: E402
from slide_plan import DEFAULT_MODE, PLANS, normalize_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('app')

ON_VERCEL = bool(os.environ.get('VERCEL'))
# Vercel functions accept at most 4.5 MB per request and per response
MAX_UPLOAD_MB = float(os.environ.get('MAX_UPLOAD_MB', 4.4 if ON_VERCEL else 50))
MAX_RESPONSE_MB = float(os.environ.get('MAX_RESPONSE_MB', 4.4 if ON_VERCEL else 0))  # 0 = no limit

PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = int(MAX_UPLOAD_MB * 1024 * 1024)


class InputError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@app.get('/')
def index():
    use_storage = storage.enabled()
    return render_template('index.html', plans=PLANS, default_mode=DEFAULT_MODE, themes=THEMES,
                           default_theme=DEFAULT_THEME, llm_ready=condense.llm_available(),
                           max_upload_mb=f'{storage.MAX_STORAGE_UPLOAD_MB if use_storage else MAX_UPLOAD_MB:g}',
                           cloud_upload=use_storage)


B2_SETTINGS = ('B2_KEY_ID', 'B2_APPLICATION_KEY', 'B2_BUCKET', 'B2_ENDPOINT')


@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """One-time B2 bucket setup (CORS for browser uploads + auto-delete), using the master key."""
    import b2_admin

    result = None
    if request.method == 'POST' and storage.enabled():
        origins = (request.form.get('origins') or '').replace(',', ' ').split()
        try:
            result = b2_admin.configure_bucket(request.form.get('key_id', ''), request.form.get('key', ''),
                                               os.environ['B2_BUCKET'], origins)
            result['app_key_problem'] = b2_admin.check_app_key()
        except b2_admin.SetupError as exc:
            result = {'error': str(exc)}
    resp = app.make_response(render_template(
        'setup.html', env_checks=[(k, bool(os.environ.get(k))) for k in B2_SETTINGS], ready=storage.enabled(),
        bucket=os.environ.get('B2_BUCKET', ''), result=result, max_mb=f'{storage.MAX_STORAGE_UPLOAD_MB:g}'))
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.post('/upload-url')
def upload_url():
    """A one-time B2 upload link for a PDF of the stated size."""
    if not storage.enabled():
        return jsonify(error='Cloud uploads are not configured on this server.'), 404
    try:
        return jsonify(storage.new_upload((request.get_json(silent=True) or {}).get('size')))
    except storage.StorageError as exc:
        return jsonify(error=str(exc)), 400


def _options():
    """Form fields from either a multipart form or a JSON body."""
    return request.get_json(silent=True) or request.form


def _receive_pdf(work):
    """
    Put the uploaded PDF at work/input.pdf.
    Returns (pdf_path, filename, ctx); ctx is None for direct uploads, or a dict with the
    upload id and storage path for B2 uploads.
    """
    pdf_path = Path(work) / 'input.pdf'
    opts = _options()
    if opts.get('storage_path'):
        if not storage.enabled():
            raise InputError('Cloud uploads are not configured on this server.')
        try:
            upload_id = storage.check_upload_path(opts['storage_path'])
            storage.download(opts['storage_path'], pdf_path)
        except storage.StorageError as exc:
            raise InputError(str(exc))
        ctx = {'id': upload_id, 'path': opts['storage_path']}
        filename = (opts.get('filename') or 'document.pdf')[:200]
    else:
        upload = request.files.get('pdf')
        if not upload or not upload.filename:
            raise InputError('Choose a PDF to upload.')
        upload.save(pdf_path)
        ctx, filename = None, upload.filename
    with open(pdf_path, 'rb') as f:
        if f.read(5) != b'%PDF-':
            raise InputError('That file is not a PDF.')
    return pdf_path, filename, ctx


def _deliver(local_path, download_name, mimetype, ctx, extra=None):
    """Signed Storage link (JSON) for cloud uploads; the file itself for direct uploads."""
    extra = extra or {}
    if ctx:
        url = storage.upload_result(local_path, f'outputs/{ctx["id"]}/{download_name}',
                                    download_name, mimetype)
        return jsonify(download_url=url, filename=download_name, **extra)
    out = io.BytesIO(Path(local_path).read_bytes())
    if MAX_RESPONSE_MB and mimetype == PPTX_MIME:
        out = shrink_pptx(out, int(MAX_RESPONSE_MB * 1024 * 1024))
    if MAX_RESPONSE_MB and out.getbuffer().nbytes > MAX_RESPONSE_MB * 1024 * 1024:
        raise InputError('The finished file is too large to download from this server. '
                         'Try the Condensed slide type, or a PDF with fewer images.', 413)
    resp = send_file(out, as_attachment=True, download_name=download_name, mimetype=mimetype)
    for key, value in extra.items():
        resp.headers[f'X-{key.title()}'] = str(value).encode('ascii', 'ignore').decode()
    resp.headers['Access-Control-Expose-Headers'] = 'X-Title, X-Slides, Content-Disposition'
    return resp


@app.post('/convert')
def convert_route():
    from pipeline import convert

    opts = _options()
    mode = normalize_mode(opts.get('mode'))
    if PLANS[mode]['uses_llm'] and not condense.llm_available():
        return jsonify(error=f'{PLANS[mode]["label"]} slides need an AI key. Set GROQ_API_KEY on the server, '
                             'or choose Standard.'), 400
    theme = opts.get('theme') if opts.get('theme') in THEMES else DEFAULT_THEME
    title = (opts.get('title') or '').strip()[:120] or None

    ctx = None
    with tempfile.TemporaryDirectory(prefix='pdf2pptx-') as work:
        try:
            pdf_path, filename, ctx = _receive_pdf(work)
            pptx_path, md_path, deck_title, slide_total = convert(pdf_path, work, mode, theme, title,
                                                                  source_name=filename)
            extra = {'title': deck_title, 'slides': slide_total}
            if ctx:
                # The PDF is deleted after this request, so hand back the Markdown link now too
                zip_path = _zip_markdown(md_path, Path(work) / (pptx_path.stem + '_markdown.zip'))
                extra['markdown_url'] = storage.upload_result(
                    zip_path, f'outputs/{ctx["id"]}/{zip_path.name}', zip_path.name, 'application/zip')
            return _deliver(pptx_path, pptx_path.name, PPTX_MIME, ctx, extra)
        except InputError as exc:
            return jsonify(error=str(exc)), exc.status
        except (PdfConversionError, condense.LLMError) as exc:
            return jsonify(error=str(exc)), 422
        except Exception:
            log.exception('Conversion failed')
            return jsonify(error='Something went wrong while converting this PDF.'), 500
        finally:
            if ctx:
                storage.delete(ctx['path'])  # the PDF is no longer needed


def _zip_markdown(md_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in Path(md_path).parent.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(Path(md_path).parent))
    return zip_path


@app.post('/markdown')
def markdown_route():
    """The intermediate Markdown and its images, zipped (no LLM involved)."""
    from pdf_to_md import convert_pdf

    ctx = None
    with tempfile.TemporaryDirectory(prefix='pdf2md-') as work:
        try:
            pdf_path, filename, ctx = _receive_pdf(work)
            md_path = convert_pdf(pdf_path, Path(work) / 'markdown')
            zip_path = _zip_markdown(md_path, Path(work) / (Path(filename).stem + '_markdown.zip'))
            return _deliver(zip_path, zip_path.name, 'application/zip', ctx)
        except InputError as exc:
            return jsonify(error=str(exc)), exc.status
        except PdfConversionError as exc:
            return jsonify(error=str(exc)), 422
        finally:
            if ctx:
                storage.delete(ctx['path'])


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f'That PDF is larger than {MAX_UPLOAD_MB:g} MB.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
