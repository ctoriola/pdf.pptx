"""
One-time Backblaze B2 bucket setup for PDF to Slides.

Sets two things the B2 website can't fully configure:
  - a CORS rule that lets browsers upload (PUT) to the bucket through presigned links
  - lifecycle rules that delete uploads/ and outputs/ files about 2 days after upload

Needs your MASTER application key (the app's own key can't change bucket settings).
The key is typed at a hidden prompt and is not saved anywhere.

    python scripts/b2_setup.py --bucket my-bucket-name
    python scripts/b2_setup.py --bucket my-bucket-name --origin https://my-app.vercel.app

Standard library only: no pip install needed.
"""
import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.request

AUTH_URL = 'https://api.backblazeb2.com/b2api/v2/b2_authorize_account'


def call(url, token, payload=None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={'Authorization': token, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors='replace')
        try:
            body = json.loads(body).get('message', body)
        except ValueError:
            pass
        sys.exit(f'B2 error ({exc.code}): {body}')


def main():
    ap = argparse.ArgumentParser(description='Configure a B2 bucket for PDF to Slides.')
    ap.add_argument('--bucket', required=True, help='bucket name')
    ap.add_argument('--origin', action='append',
                    help='site allowed to upload, e.g. https://my-app.vercel.app (repeatable; default: any site)')
    ap.add_argument('--key-id', help='master application key ID (asked for if omitted)')
    args = ap.parse_args()

    key_id = args.key_id or input('Master application key ID: ').strip()
    key = getpass.getpass('Master application key (hidden as you type): ').strip()
    basic = 'Basic ' + base64.b64encode(f'{key_id}:{key}'.encode()).decode()

    auth = call(AUTH_URL, basic)
    api, token, account = auth['apiUrl'], auth['authorizationToken'], auth['accountId']

    buckets = call(f'{api}/b2api/v2/b2_list_buckets', token,
                   {'accountId': account, 'bucketName': args.bucket})['buckets']
    if not buckets:
        sys.exit(f'No bucket named "{args.bucket}" in this account. Check the spelling.')
    bucket = buckets[0]

    # Upload links are secret and short-lived, so allowing any site is safe; --origin narrows it
    origins = (args.origin + ['http://localhost:5000']) if args.origin else ['*']
    cors = [{
        'corsRuleName': 'pdfToSlidesUploads',
        'allowedOrigins': origins,
        'allowedOperations': ['s3_put', 's3_get', 's3_head'],
        'allowedHeaders': ['content-type'],
        'exposeHeaders': ['ETag'],
        'maxAgeSeconds': 3600,
    }]
    lifecycle = [{'fileNamePrefix': prefix, 'daysFromUploadingToHiding': 1, 'daysFromHidingToDeleting': 1}
                 for prefix in ('uploads/', 'outputs/')]
    keep = [r for r in bucket.get('lifecycleRules', []) if r['fileNamePrefix'] not in ('uploads/', 'outputs/')]

    updated = call(f'{api}/b2api/v2/b2_update_bucket', token, {
        'accountId': account,
        'bucketId': bucket['bucketId'],
        'bucketType': 'allPrivate',
        'corsRules': cors,
        'lifecycleRules': keep + lifecycle,
    })
    print(f'\nBucket "{updated["bucketName"]}" is ready:')
    print('  Private:   yes')
    print(f'  Uploads allowed from: {", ".join(cors[0]["allowedOrigins"])}')
    print('  Auto-delete: uploads/ and outputs/ files about 2 days after upload')
    print('\nNext: add the B2_ settings to Vercel (setup guide, Part 6).')


if __name__ == '__main__':
    main()
