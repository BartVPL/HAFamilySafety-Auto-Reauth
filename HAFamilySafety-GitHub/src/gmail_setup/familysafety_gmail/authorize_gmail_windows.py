"""Run ONCE on Windows with a browser. Produces gmail_token.json."""
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

ROOT = Path(__file__).resolve().parent
CLIENT = ROOT / 'credentials.json'
TOKEN = ROOT / 'gmail_token.json'
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
if not CLIENT.exists():
    raise SystemExit('Brak credentials.json obok skryptu. Pobierz klienta OAuth typu Desktop app.')
flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
creds = flow.run_local_server(host='localhost', port=0, open_browser=True,
                              access_type='offline', prompt='consent')
if not creds.refresh_token:
    raise SystemExit('Missing refresh_token. Check Google consent and authorize again.')
TOKEN.write_text(creds.to_json(), encoding='utf-8')
print('OK: gmail_token.json created. Never publish or share it.')
