"""
One-time Backblaze B2 bucket setup, run from the app's /setup page.

Sets the two bucket settings the B2 website can't fully configure:
  - a CORS rule that lets browsers upload (PUT) through the app's presigned links
  - lifecycle rules that delete uploads/ and outputs/ files about 2 days after upload
and then checks that the app's own key (B2_KEY_ID / B2_APPLICATION_KEY) can write and
delete in the bucket.

Bucket settings need the account's MASTER key, which the app's bucket-limited key can't
do. The master key is used for these calls only and never stored or logged.
"""
import base64
import logging

import requests

import storage

log = logging.getLogger(__name__)

AUTH_URL = 'https://api.backblazeb2.com/b2api/v2/b2_authorize_account'
APP_PREFIXES = ('uploads/', 'outputs/')


class SetupError(Exception):
    pass


def _call(url, token, payload=None):
    try:
        if payload is None:
            resp = requests.get(url, headers={'Authorization': token}, timeout=30)
        else:
            resp = requests.post(url, headers={'Authorization': token}, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise SetupError(f'Could not reach Backblaze: {exc}') from exc
    if resp.status_code >= 300:
        try:
            message = resp.json().get('message') or resp.text
        except ValueError:
            message = resp.text
        if resp.status_code == 401:
            message = 'Backblaze rejected the key. Check you pasted the MASTER keyID and applicationKey.'
        raise SetupError(f'{message} (HTTP {resp.status_code})')
    return resp.json()


def configure_bucket(key_id, key, bucket_name, origins=None):
    """Apply the CORS and lifecycle rules. Returns a summary dict."""
    if not (key_id and key):
        raise SetupError('Paste both the master keyID and the applicationKey.')
    basic = 'Basic ' + base64.b64encode(f'{key_id.strip()}:{key.strip()}'.encode()).decode()
    auth = _call(AUTH_URL, basic)
    if auth.get('allowed', {}).get('bucketId'):
        raise SetupError('That key is limited to one bucket. Use the MASTER application key instead.')
    api, token, account = auth['apiUrl'], auth['authorizationToken'], auth['accountId']

    buckets = _call(f'{api}/b2api/v2/b2_list_buckets', token,
                    {'accountId': account, 'bucketName': bucket_name})['buckets']
    if not buckets:
        raise SetupError(f'This account has no bucket named "{bucket_name}". Check B2_BUCKET in Vercel.')
    bucket = buckets[0]

    # Upload links are secret and short-lived, so allowing any site is safe; origins narrow it
    allowed = [o.strip().rstrip('/') for o in (origins or []) if o.strip()] or ['*']
    cors = [{
        'corsRuleName': 'pdfToSlidesUploads',
        'allowedOrigins': allowed,
        'allowedOperations': ['s3_put', 's3_get', 's3_head'],
        'allowedHeaders': ['content-type'],
        'exposeHeaders': ['ETag'],
        'maxAgeSeconds': 3600,
    }]
    lifecycle = [{'fileNamePrefix': p, 'daysFromUploadingToHiding': 1, 'daysFromHidingToDeleting': 1}
                 for p in APP_PREFIXES]
    keep = [r for r in bucket.get('lifecycleRules', []) if r.get('fileNamePrefix') not in APP_PREFIXES]
    other_cors = [r for r in bucket.get('corsRules', []) if r.get('corsRuleName') != 'pdfToSlidesUploads']

    _call(f'{api}/b2api/v2/b2_update_bucket', token, {
        'accountId': account,
        'bucketId': bucket['bucketId'],
        'bucketType': 'allPrivate',
        'corsRules': other_cors + cors,
        'lifecycleRules': keep + lifecycle,
    })
    log.info('Configured B2 bucket %s', bucket_name)
    return {'bucket': bucket_name, 'origins': allowed}


APP_KEY_HINTS = {
    'InvalidAccessKeyId': 'B2_KEY_ID is not a valid key ID. Copy the app key\'s keyID again (not the master key).',
    'SignatureDoesNotMatch': 'B2_APPLICATION_KEY does not belong to B2_KEY_ID. Re-copy it; it is shown only once, '
                             'so if you no longer have it, create a new app key and update both settings.',
    'AccessDenied': 'The key is not allowed to write to this bucket. Create an app key for bucket "{bucket}" with '
                    'Read and Write access and update both settings.',
    'AuthorizationHeaderMalformed': 'B2_ENDPOINT does not match the bucket\'s region. Copy the Endpoint from the '
                                    'bucket\'s card again.',
    'NoSuchBucket': 'B2_BUCKET does not match a bucket this key can see.',
}


def describe_failure(action, resp):
    cfg = storage._config()
    code, message = storage.s3_error(resp)
    hint = APP_KEY_HINTS.get(code, 'Check B2_KEY_ID, B2_APPLICATION_KEY and B2_ENDPOINT.').format(bucket=cfg['bucket'])
    detail = f'B2 said: {code or "HTTP " + str(resp.status_code)}' + (f' ({message})' if message else '')
    return (f'The app key could not {action} (HTTP {resp.status_code}). {hint} {detail}. '
            f'[key ID starts {cfg["key_id"][:6] or "(empty)"}, region {cfg["region"]}, bucket {cfg["bucket"]}]')


def check_app_key():
    """Write and delete a small file with the app's own key. Returns None if fine, else a message."""
    path = 'uploads/setup-check.txt'
    headers = {'Content-Type': 'text/plain'}
    try:
        put = requests.put(storage.presign('PUT', path, 300, headers), data=b'ok', headers=headers, timeout=30)
        if put.status_code >= 300:
            return describe_failure('write to the bucket', put)
        delete = requests.delete(storage.presign('DELETE', path, 300), timeout=30)
        if delete.status_code >= 300:
            return describe_failure('delete files', delete)
    except requests.RequestException as exc:
        return f'Could not reach the bucket endpoint ({exc}). Check B2_ENDPOINT.'
    return None
