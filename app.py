"""
Flask web app: upload a PDF, choose a slide type and theme, download the .pptx.

Stateless, so it runs on serverless hosts (Vercel) as well as a normal server: each
request converts in its own temporary folder and returns the file directly.
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


@app.get('/')
def index():
    return render_template('index.html', plans=PLANS, default_mode=DEFAULT_MODE, themes=THEMES,
                           default_theme=DEFAULT_THEME, llm_ready=condense.llm_available(),
                           max_upload_mb=f'{MAX_UPLOAD_MB:g}')


def _read_upload():
    """Return (pdf bytes, filename) or raise ValueError with a user-facing message."""
    upload = request.files.get('pdf')
    if not upload or not upload.filename:
        raise ValueError('Choose a PDF to upload.')
    data = upload.read()
    if not data.startswith(b'%PDF-'):
        raise ValueError('That file is not a PDF.')
    return data, upload.filename


@app.post('/convert')
def convert_route():
    from pipeline import convert

    try:
        data, filename = _read_upload()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    mode = normalize_mode(request.form.get('mode'))
    if PLANS[mode]['uses_llm'] and not condense.llm_available():
        return jsonify(error=f'{PLANS[mode]["label"]} slides need an AI key. Set GROQ_API_KEY on the server, '
                             'or choose Standard.'), 400
    theme = request.form.get('theme') if request.form.get('theme') in THEMES else DEFAULT_THEME
    title = (request.form.get('title') or '').strip()[:120] or None

    with tempfile.TemporaryDirectory(prefix='pdf2pptx-') as work:
        pdf_path = Path(work) / 'input.pdf'
        pdf_path.write_bytes(data)
        try:
            pptx_path, _, deck_title, slide_total = convert(pdf_path, work, mode, theme, title,
                                                            source_name=filename)
        except (PdfConversionError, condense.LLMError) as exc:
            return jsonify(error=str(exc)), 422
        except Exception:
            log.exception('Conversion failed for %s', filename)
            return jsonify(error='Something went wrong while converting this PDF.'), 500
        out = io.BytesIO(pptx_path.read_bytes())

    if MAX_RESPONSE_MB:
        out = shrink_pptx(out, int(MAX_RESPONSE_MB * 1024 * 1024))
        if out.getbuffer().nbytes > MAX_RESPONSE_MB * 1024 * 1024:
            return jsonify(error='The finished deck is too large to download from this server. '
                                 'Try the Condensed slide type, or a PDF with fewer images.'), 413
    resp = send_file(out, as_attachment=True, download_name=pptx_path.name, mimetype=PPTX_MIME)
    resp.headers['X-Deck-Title'] = deck_title.encode('ascii', 'ignore').decode()
    resp.headers['X-Slide-Count'] = str(slide_total)
    resp.headers['Access-Control-Expose-Headers'] = 'X-Deck-Title, X-Slide-Count, Content-Disposition'
    return resp


@app.post('/markdown')
def markdown_route():
    """The intermediate Markdown and its images, zipped (no LLM involved)."""
    from pdf_to_md import convert_pdf

    try:
        data, filename = _read_upload()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with tempfile.TemporaryDirectory(prefix='pdf2md-') as work:
        pdf_path = Path(work) / 'input.pdf'
        pdf_path.write_bytes(data)
        try:
            md_path = convert_pdf(pdf_path, Path(work) / 'markdown')
        except PdfConversionError as exc:
            return jsonify(error=str(exc)), 422
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for f in md_path.parent.rglob('*'):
                if f.is_file():
                    zf.write(f, f.relative_to(md_path.parent))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=Path(filename).stem + '_markdown.zip',
                     mimetype='application/zip')


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f'That PDF is larger than {MAX_UPLOAD_MB:g} MB.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
