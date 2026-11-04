"""
Flask web app: upload a PDF, choose a slide type and theme, download the .pptx.

Conversions run in a background thread; the page polls /status/<job_id>.
Each job lives in its own folder under JOBS_DIR and is deleted after JOB_TTL seconds.
"""
import io
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request, send_file

load_dotenv()

import condense  # noqa: E402  (reads LLM settings from the environment loaded above)
from deck_builder import DEFAULT_THEME, THEMES  # noqa: E402
from slide_plan import DEFAULT_MODE, PLANS, normalize_mode  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log = logging.getLogger('app')

JOBS_DIR = Path(os.environ.get('JOBS_DIR') or Path(tempfile.gettempdir()) / 'pdf2pptx-jobs')
JOB_TTL = int(os.environ.get('JOB_TTL_SECONDS', 3600))
MAX_UPLOAD_MB = int(os.environ.get('MAX_UPLOAD_MB', 50))
MAX_ACTIVE_JOBS = int(os.environ.get('MAX_ACTIVE_JOBS', 4))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()


def _set(job_id, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def _run_job(job_id, pdf_path, mode, theme, title, source_name):
    from pipeline import convert
    from pdf_to_md import PdfConversionError

    work_dir = pdf_path.parent
    try:
        pptx_path, md_path, deck_title, slide_total = convert(
            pdf_path, work_dir, mode, theme, title, progress=lambda msg: _set(job_id, message=msg),
            source_name=source_name)
        _set(job_id, state='done', message='Your slides are ready.', pptx=str(pptx_path), markdown=str(md_path),
             title=deck_title, slides=slide_total)
    except (PdfConversionError, condense.LLMError) as exc:
        _set(job_id, state='error', message=str(exc))
    except Exception:
        log.exception('Job %s failed', job_id)
        _set(job_id, state='error', message='Something went wrong while converting this PDF.')


def _cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL
        with jobs_lock:
            expired = [j for j, info in jobs.items() if info['created'] < cutoff and info['state'] != 'running']
            for j in expired:
                jobs.pop(j)
        for j in expired:
            shutil.rmtree(JOBS_DIR / j, ignore_errors=True)


threading.Thread(target=_cleanup_loop, daemon=True).start()


def _warm_up():
    from pdf_to_md import warm_up
    try:
        warm_up()
        log.info('Docling models loaded')
    except Exception as exc:  # reported to users per job; log it here for the operator
        log.error('Docling warm-up failed: %s', exc)


if os.environ.get('DOCLING_WARM_UP', '1') == '1':
    threading.Thread(target=_warm_up, daemon=True).start()


@app.get('/')
def index():
    return render_template('index.html', plans=PLANS, default_mode=DEFAULT_MODE, themes=THEMES,
                           default_theme=DEFAULT_THEME, llm_ready=condense.llm_available(),
                           max_upload_mb=MAX_UPLOAD_MB)


@app.post('/convert')
def start_conversion():
    upload = request.files.get('pdf')
    if not upload or not upload.filename:
        return jsonify(error='Choose a PDF to upload.'), 400
    head = upload.stream.read(5)
    upload.stream.seek(0)
    if head != b'%PDF-':
        return jsonify(error='That file is not a PDF.'), 400

    mode = normalize_mode(request.form.get('mode'))
    if PLANS[mode]['uses_llm'] and not condense.llm_available():
        return jsonify(error=f'{PLANS[mode]["label"]} slides need an AI key. Set GROQ_API_KEY on the server, '
                             'or choose Standard.'), 400
    theme = request.form.get('theme') if request.form.get('theme') in THEMES else DEFAULT_THEME
    title = (request.form.get('title') or '').strip()[:120] or None

    with jobs_lock:
        if sum(1 for j in jobs.values() if j['state'] == 'running') >= MAX_ACTIVE_JOBS:
            return jsonify(error='The converter is busy. Please try again in a minute.'), 503
        job_id = uuid.uuid4().hex
        jobs[job_id] = {'state': 'running', 'message': 'Uploading…', 'created': time.time(),
                        'filename': upload.filename, 'mode': mode}

    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / 'input.pdf'
    upload.save(pdf_path)
    # Without a title, the deck uses the PDF's own title, then the uploaded file's name
    threading.Thread(target=_run_job, args=(job_id, pdf_path, mode, theme, title, upload.filename),
                     daemon=True).start()
    return jsonify(job_id=job_id)


@app.get('/status/<job_id>')
def status(job_id):
    with jobs_lock:
        info = dict(jobs.get(job_id) or {})
    if not info:
        return jsonify(state='missing', message='This conversion has expired. Please upload the PDF again.'), 404
    return jsonify({k: info.get(k) for k in ('state', 'message', 'title', 'slides', 'mode')})


def _job_file(job_id, key):
    with jobs_lock:
        info = dict(jobs.get(job_id) or {})
    if info.get('state') != 'done' or not info.get(key):
        abort(404)
    return info, Path(info[key])


@app.get('/download/<job_id>/pptx')
def download_pptx(job_id):
    info, path = _job_file(job_id, 'pptx')
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')


@app.get('/download/<job_id>/markdown')
def download_markdown(job_id):
    """The intermediate Markdown and its images, zipped."""
    info, md_path = _job_file(job_id, 'markdown')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in md_path.parent.rglob('*'):
            if f.is_file():
                zf.write(f, f.relative_to(md_path.parent))
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=Path(info['pptx']).stem + '_markdown.zip',
                     mimetype='application/zip')


@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f'That PDF is larger than {MAX_UPLOAD_MB} MB.'), 413


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
