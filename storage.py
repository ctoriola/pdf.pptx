"""
Firebase Storage for large files (bypasses Vercel's 4.5 MB request/response limits).

Flow:
  1. The browser signs in anonymously and uploads the PDF straight to
     uploads/<uid>/<id>.pdf in Firebase Storage (storage.rules restricts this).
  2. It calls /convert with that path and its Firebase ID token.
  3. The server verifies the token, checks the path belongs to that user, downloads
     the PDF, converts it, uploads the result to outputs/<uid>/<id>/, deletes the
     PDF, and returns a signed download link that expires after LINK_TTL.

Configuration (environment variables):
  FIREBASE_SERVICE_ACCOUNT   the service-account key JSON (raw, or base64-encoded)
  FIREBASE_STORAGE_BUCKET    e.g. my-project.firebasestorage.app
  FIREBASE_PROJECT_ID        e.g. my-project
  FIREBASE_API_KEY, FIREBASE_AUTH_DOMAIN, FIREBASE_APP_ID   web config for the browser
"""
import base64
import json
import logging
import os
import re
from datetime import timedelta

log = logging.getLogger(__name__)

LINK_TTL = timedelta(hours=1)
MAX_STORAGE_UPLOAD_MB = float(os.environ.get('MAX_STORAGE_UPLOAD_MB', 50))
UPLOAD_PATH_RE = re.compile(r'^uploads/(?P<uid>[A-Za-z0-9_-]{1,128})/(?P<id>[A-Za-z0-9_-]{8,64})\.pdf$')

_bucket = None


class StorageError(Exception):
    """A user-facing problem with an uploaded file (missing, too big, not yours)."""


class AuthError(Exception):
    pass


def enabled():
    return all(os.environ.get(k) for k in ('FIREBASE_SERVICE_ACCOUNT', 'FIREBASE_STORAGE_BUCKET',
                                           'FIREBASE_PROJECT_ID', 'FIREBASE_API_KEY', 'FIREBASE_APP_ID'))


def web_config():
    """The Firebase config the browser needs (all public values)."""
    project = os.environ.get('FIREBASE_PROJECT_ID', '')
    return {
        'apiKey': os.environ.get('FIREBASE_API_KEY', ''),
        'authDomain': os.environ.get('FIREBASE_AUTH_DOMAIN') or f'{project}.firebaseapp.com',
        'projectId': project,
        'storageBucket': os.environ.get('FIREBASE_STORAGE_BUCKET', ''),
        'appId': os.environ.get('FIREBASE_APP_ID', ''),
    }


def _service_account_info():
    raw = os.environ['FIREBASE_SERVICE_ACCOUNT'].strip()
    if not raw.startswith('{'):
        raw = base64.b64decode(raw).decode('utf-8')
    return json.loads(raw)


def _get_bucket():
    global _bucket
    if _bucket is None:
        from google.cloud import storage as gcs
        from google.oauth2 import service_account

        info = _service_account_info()
        creds = service_account.Credentials.from_service_account_info(info)
        client = gcs.Client(project=info.get('project_id'), credentials=creds)
        _bucket = client.bucket(os.environ['FIREBASE_STORAGE_BUCKET'])
    return _bucket


def verify_user(auth_header):
    """Return the Firebase uid from an 'Authorization: Bearer <ID token>' header."""
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token

    token = (auth_header or '').removeprefix('Bearer ').strip()
    if not token:
        raise AuthError('Missing sign-in token.')
    try:
        claims = id_token.verify_firebase_token(token, ga_requests.Request(),
                                                audience=os.environ['FIREBASE_PROJECT_ID'])
    except Exception as exc:
        raise AuthError('Your session has expired. Reload the page and try again.') from exc
    uid = (claims or {}).get('sub') or (claims or {}).get('user_id')
    if not uid:
        raise AuthError('Invalid sign-in token.')
    return uid


def check_upload_path(path, uid):
    """Only accept uploads/<this user's uid>/<id>.pdf; returns the upload id."""
    m = UPLOAD_PATH_RE.match(path or '')
    if not m or m.group('uid') != uid:
        raise StorageError('That upload does not belong to this session.')
    return m.group('id')


def download(path, dest):
    blob = _get_bucket().blob(path)
    if not blob.exists():
        raise StorageError('The uploaded PDF was not found. Please upload it again.')
    blob.reload()
    if blob.size and blob.size > MAX_STORAGE_UPLOAD_MB * 1024 * 1024:
        raise StorageError(f'That PDF is larger than {MAX_STORAGE_UPLOAD_MB:g} MB.')
    blob.download_to_filename(str(dest))


def upload_result(local_path, path, download_name, content_type):
    """Upload a finished file and return a signed link that downloads it under download_name."""
    blob = _get_bucket().blob(path)
    blob.upload_from_filename(str(local_path), content_type=content_type)
    safe_name = re.sub(r'[^\w.\- ]', '', download_name) or 'download'
    return blob.generate_signed_url(version='v4', expiration=LINK_TTL, method='GET',
                                    response_disposition=f'attachment; filename="{safe_name}"')


def delete(path):
    try:
        _get_bucket().blob(path).delete()
    except Exception as exc:  # the bucket's lifecycle rule removes leftovers anyway
        log.warning('Could not delete %s: %s', path, exc)
