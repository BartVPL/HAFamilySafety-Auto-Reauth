#!/usr/bin/env python3
"""Read-only Gmail Microsoft OTP reader. No email modification, tokens never logged."""
import argparse
import base64
import datetime as dt
import email
import json
import re
import sys
import time
from html import unescape
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent
TOKEN = ROOT / 'gmail_token.json'
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
# Edit only after confirming the real FROM address of your Microsoft emails.
TRUSTED_FROM = (
    'accountprotection.microsoft.com',
    'account.microsoft.com',
    'microsoft.com',
)
CODE = re.compile(r'(?<!\d)(\d{6})(?!\d)')


def gmail():
    if not TOKEN.exists():
        raise RuntimeError('Missing gmail_token.json: authorize Google on a desktop and copy the token to the Linux bot host')
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json(), encoding='utf-8')
            TOKEN.chmod(0o600)
        else:
            raise RuntimeError('Invalid OAuth token; authorize Google again')
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def flatten_body(part):
    result = []
    data = part.get('body', {}).get('data')
    typ = part.get('mimeType', '')
    if data and typ in ('text/plain', 'text/html'):
        raw = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4))
        value = raw.decode('utf-8', errors='replace')
        if typ == 'text/html':
            value = re.sub(r'(?s)<[^>]+>', ' ', value)
            value = unescape(value)
        result.append(value)
    for child in part.get('parts', []):
        result.extend(flatten_body(child))
    return result



def extract_security_codes(text):
    """Extract only Microsoft OTP codes explicitly labelled as such."""
    patterns = [
        r"twój\s+jednorazowy\s+kod\s*:\s*(\d{6})(?!\d)",
        r"twoj\s+jednorazowy\s+kod\s*:\s*(\d{6})(?!\d)",
        r"your\s+single.use\s+code\s*:\s*(\d{6})(?!\d)",
        r"your\s+security\s+code\s*(?:is|:)\s*(\d{6})(?!\d)",
        r"your\s+verification\s+code\s*(?:is|:)\s*(\d{6})(?!\d)",
    ]

    found = set()
    for pattern in patterns:
        found.update(re.findall(pattern, text, re.I))

    return sorted(found)


def find_messages(service, since_epoch, limit=30):
    # Gmail's after: is in days/epoch seconds; server-side filtering limits access.
    q = 'after:%d (from:microsoft.com OR from:accountprotection.microsoft.com)' % since_epoch
    listing = service.users().messages().list(userId='me', q=q, maxResults=limit).execute()
    for item in listing.get('messages', []):
        msg = service.users().messages().get(userId='me', id=item['id'], format='full').execute()
        headers = {x['name'].lower(): x['value'] for x in msg.get('payload', {}).get('headers', [])}
        sender = email.utils.parseaddr(headers.get('from', ''))[1].lower()
        if not sender or not any(sender == d or sender.endswith('@'+d) for d in TRUSTED_FROM):
            continue
        ts = int(msg.get('internalDate', '0')) / 1000
        if ts < since_epoch:
            continue
        subject = headers.get('subject', '')
        bodies = flatten_body(msg.get('payload', {}))
        text = '\n'.join(bodies) if bodies else msg.get('snippet', '')
        codes = extract_security_codes(text)
        yield {'id': msg['id'], 'time': ts, 'sender': sender, 'subject': subject,
               'codes': codes}


def main():
    p = argparse.ArgumentParser(description='Test Microsoft verification email retrieval using Gmail API')
    p.add_argument('--since-minutes', type=int, default=1440,
                   help='Search messages from the past N minutes; default 24 hours')
    p.add_argument('--since-epoch', type=int, help='Exact sign-in start time (UNIX epoch)')
    p.add_argument('--show-code', action='store_true',
                   help='Display verification code in local terminal ONLY (do not share it)')
    p.add_argument('--wait', type=int, default=0, metavar='SEKUNDY',
                   help='Wait for new code for N seconds, polling every 10 seconds')
    args = p.parse_args()
    since = args.since_epoch or int(time.time() - args.since_minutes * 60)
    service = gmail()
    profile = service.users().getProfile(userId='me').execute()
    print('Gmail API: OK, account:', profile.get('emailAddress', '?'))
    end = time.monotonic() + args.wait
    while True:
        matches = sorted(find_messages(service, since), key=lambda m: m['time'], reverse=True)
        print('Matching Microsoft emails:', len(matches))
        for m in matches[:5]:
            stamp = dt.datetime.fromtimestamp(m['time'], dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            print(' -', stamp, '|', m['sender'], '|', m['subject'][:90], '| 6-digit code:', bool(m['codes']))
        candidate = next((m for m in matches if len(set(m['codes'])) == 1), None)
        if candidate:
            if args.show_code:
                print('CODE (local terminal only):', candidate['codes'][0])
            else:
                print('Code: detected (hidden; use --show-code for local testing only)')
            return
        if time.monotonic() >= end:
            print('No unambiguous verification code in the specified period.')
            return
        time.sleep(min(10, max(0, end - time.monotonic())))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Gmail API error:', type(exc).__name__, str(exc)[:240], file=sys.stderr)
        sys.exit(1)
