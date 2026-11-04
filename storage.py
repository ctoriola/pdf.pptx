"""
Backblaze B2 storage for large files (bypasses Vercel's 4.5 MB request/response limits).

Flow:
  1. The browser asks /upload-url for a one-time upload link (valid 15 minutes) for a PDF
     of a given size. The size is part of the signature, so B2 rejects any other file.
  2. The browser PUTs the PDF straight to B2 at uploads/<random id>.pdf.
  3. /convert gets that path, downloads the PDF, converts it, uploads the results to
     outputs/<id>/, deletes the PDF, and returns signed download links (valid 1 hour).
  A bucket lifecycle rule (see scripts/b2_setup.py) removes anything left after ~2 days.

Uses B2's S3-compatible API with presigned URLs, signed here with AWS Signature V4,
so no SDK is needed.

Configuration (environment variables):
  B2_KEY_ID            application key ID ("keyID" in the B2 console)
  B2_APPLICATION_KEY   application key (shown once when the key is created)
  B2_BUCKET            bucket name
  B2_ENDPOINT          the bucket's S3 endpoint, e.g. s3.us-east-005.backblazeb2.com
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

MAX_STORAGE_UPLOAD_MB = float(os.environ.get('MAX_STORAGE_UPLOAD_MB', 100))
UPLOAD_URL_TTL = 15 * 60       # seconds the browser has to start its upload
DOWNLOAD_URL_TTL = 60 * 60     # seconds a download link works
UPLOAD_PATH_RE = re.compile(r'^uploads/(?P<id>[0-9a-f]{32})\.pdf$')


class StorageError(Exception):
    """A user-facing problem with an uploaded file (missing, too big, bad path)."""


def enabled():
    return all(os.environ.get(k) for k in ('B2_KEY_ID', 'B2_APPLICATION_KEY', 'B2_BUCKET', 'B2_ENDPOINT'))


def _env(name):
    # Pasted values often carry spaces, line breaks or quotes, which break the signature
    return os.environ.get(name, '').strip().strip('"\'').strip()


def _config():
    endpoint = re.sub(r'^https?://', '', _env('B2_ENDPOINT')).rstrip('/')
    m = re.match(r'^s3\.([a-z0-9-]+)\.backblazeb2\.com$', endpoint)
    region = _env('B2_REGION') or (m.group(1) if m else 'us-east-1')
    return {'host': endpoint, 'region': region, 'bucket': _env('B2_BUCKET'),
            'key_id': _env('B2_KEY_ID'), 'secret': _env('B2_APPLICATION_KEY')}


def s3_error(resp):
    """(code, message) from an S3-style XML error body."""
    body = getattr(resp, 'text', '') or ''
    code = re.search(r'<Code>(.*?)</Code>', body, re.S)
    message = re.search(r'<Message>(.*?)</Message>', body, re.S)
    return (code.group(1).strip() if code else ''), (message.group(1).strip() if message else body[:200].strip())


# ============================================================
# AWS Signature V4 (query-string presigning)
# ============================================================
def _uri_encode(value, safe='-_.~'):
    return quote(str(value), safe=safe)


def presign(method, key, expires, headers=None, now=None, cfg=None):
    """
    Return a presigned URL for METHOD on bucket/key. Any `headers` given are signed,
    so the request must send exactly those header values.
    """
    cfg = cfg or _config()
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    scope = f'{datestamp}/{cfg["region"]}/s3/aws4_request'

    signed = {'host': cfg['host']}
    signed.update({k.lower(): str(v).strip() for k, v in (headers or {}).items()})
    signed_names = ';'.join(sorted(signed))

    canonical_uri = '/' + _uri_encode(cfg['bucket']) + '/' + _uri_encode(key, safe='-_.~/')
    query = {
        'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
        'X-Amz-Credential': f'{cfg["key_id"]}/{scope}',
        'X-Amz-Date': amz_date,
        'X-Amz-Expires': str(int(expires)),
        'X-Amz-SignedHeaders': signed_names,
    }
    canonical_query = '&'.join(f'{_uri_encode(k)}={_uri_encode(v)}' for k, v in sorted(query.items()))
    canonical_headers = ''.join(f'{k}:{signed[k]}\n' for k in sorted(signed))
    canonical_request = '\n'.join([method, canonical_uri, canonical_query, canonical_headers, signed_names,
                                   'UNSIGNED-PAYLOAD'])
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope,
                                hashlib.sha256(canonical_request.encode()).hexdigest()])

    def _hmac(k, msg):
        return hmac.new(k, msg.encode(), hashlib.sha256).digest()
    k = _hmac(('AWS4' + cfg['secret']).encode(), datestamp)
    k = _hmac(k, cfg['region'])
    k = _hmac(k, 's3')
    k = _hmac(k, 'aws4_request')
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f'https://{cfg["host"]}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}'


# ============================================================
# Operations
# ============================================================
def new_upload(size):
    """Return {'url', 'path', 'headers'} for the browser to PUT a PDF of exactly `size` bytes."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise StorageError('Missing file size.')
    if size <= 0:
        raise StorageError('That file is empty.')
    if size > MAX_STORAGE_UPLOAD_MB * 1024 * 1024:
        raise StorageError(f'That PDF is larger than {MAX_STORAGE_UPLOAD_MB:g} MB.')
    path = f'uploads/{secrets.token_hex(16)}.pdf'
    # Content-Length is signed: B2 refuses an upload of any other size
    headers = {'Content-Type': 'application/pdf', 'Content-Length': str(size)}
    url = presign('PUT', path, UPLOAD_URL_TTL, headers)
    return {'url': url, 'path': path, 'headers': {'Content-Type': 'application/pdf'}}


def check_upload_path(path):
    """Only accept uploads/<32 hex>.pdf; returns the upload id."""
    m = UPLOAD_PATH_RE.match(path or '')
    if not m:
        raise StorageError('That upload reference is not valid.')
    return m.group('id')


def download(path, dest):
    head = requests.head(presign('HEAD', path, 300), timeout=30)
    if head.status_code == 404:
        raise StorageError('The uploaded PDF was not found. Please upload it again.')
    head.raise_for_status()
    if int(head.headers.get('Content-Length', 0)) > MAX_STORAGE_UPLOAD_MB * 1024 * 1024:
        raise StorageError(f'That PDF is larger than {MAX_STORAGE_UPLOAD_MB:g} MB.')
    with requests.get(presign('GET', path, 300), stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in resp.iter_content(1 << 20):
                f.write(chunk)


def upload_result(local_path, path, download_name, content_type):
    """Upload a finished file and return a signed link that downloads it as download_name."""
    safe_name = re.sub(r'[^\w.\- ]', '', download_name) or 'download'
    headers = {'Content-Type': content_type, 'Content-Disposition': f'attachment; filename="{safe_name}"'}
    with open(local_path, 'rb') as f:
        resp = requests.put(presign('PUT', path, 300, headers), data=f, headers=headers, timeout=120)
    if resp.status_code >= 300:
        raise RuntimeError(f'B2 upload failed ({resp.status_code}): {resp.text[:300]}')
    return presign('GET', path, DOWNLOAD_URL_TTL)


def delete(path):
    try:
        resp = requests.delete(presign('DELETE', path, 300), timeout=30)
        if resp.status_code >= 300 and resp.status_code != 404:
            log.warning('Could not delete %s: HTTP %s', path, resp.status_code)
    except requests.RequestException as exc:  # the lifecycle rule removes leftovers anyway
        log.warning('Could not delete %s: %s', path, exc)
