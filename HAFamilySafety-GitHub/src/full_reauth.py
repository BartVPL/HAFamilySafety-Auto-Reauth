#!/usr/bin/env python3
"""Experimental end-to-end native HAFamilySafety reauth, driven entirely from LXC.
Does not alter the existing monitoring service. Never prints URLs/tokens/codes.
Requires HA admin long-lived token, gmail_reader.py and Gmail OAuth token, Chromium.
"""
import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
import websockets

BASE = Path('/opt/familysafety-bot')
GMAIL_PATH = BASE / 'gmail_setup/familysafety_gmail'
sys.path.insert(0, str(GMAIL_PATH))
from gmail_reader import gmail, find_messages  # type: ignore


def load_env():
    for line in Path('/etc/familysafety-bot.env').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, val = line.split('=', 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

load_env()
HA = os.environ['HA_URL'].rstrip('/')
TOKEN = os.environ['HA_TOKEN']
SENSOR = 'sensor.microsoft_family_safety_connection'
DOMAIN = 'microsoft_family_safety'
H = {'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'}


def ha(method, path, body=None, timeout=30):
    encoded = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(HA + path, data=encoded, headers=H, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        # Never output full response: HA can include sensitive flow URLs.
        raise RuntimeError(f'HA {method} {path.split("?")[0]}: HTTP {exc.code}') from exc


def sensor():
    d = ha('GET', '/api/states/' + SENSOR)
    a = d.get('attributes', {})
    return d.get('state'), a.get('family_context'), a.get('reauth_required'), a.get('account_interrupt')


async def flows():
    url = HA.replace('https://', 'wss://').replace('http://', 'ws://') + '/api/websocket'
    async with websockets.connect(url, open_timeout=15) as ws:
        greeting = json.loads(await ws.recv())
        if greeting.get('type') != 'auth_required':
            raise RuntimeError('Unexpected HA WebSocket greeting')
        await ws.send(json.dumps({'type': 'auth', 'access_token': TOKEN}))
        auth = json.loads(await ws.recv())
        if auth.get('type') != 'auth_ok':
            raise RuntimeError('HA WebSocket authentication failed; admin token required')
        await ws.send(json.dumps({'id': 1, 'type': 'config_entries/flow/progress'}))
        res = json.loads(await ws.recv())
        if not res.get('success'):
            raise RuntimeError('HA flow/progress rejected request')
        return [f for f in res.get('result', []) if f.get('handler') == DOMAIN and f.get('context', {}).get('source') == 'reauth']


def locate_flow():
    for i in range(15):
        f = asyncio.run(flows())
        if len(f) == 1:
            print('HA: found one reauthentication flow')
            return f[0]['flow_id']
        if len(f) > 1:
            raise RuntimeError('More than one FamilySafety reauth flow; refusing to guess')
        time.sleep(2)
    raise RuntimeError('HA accepted request but no reauth flow appeared in 30s')


def ensure_step(flow_id, step):
    d = ha('GET', '/api/config/config_entries/flow/' + flow_id)
    print('HA flow step:', d.get('step_id'), '/ type:', d.get('type'))
    if d.get('step_id') != step:
        raise RuntimeError('Expected HA step ' + step + ', received ' + str(d.get('step_id')))
    return d


def pick_first(page, choices):
    for selector in choices:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=800):
                return loc
        except Exception:
            pass
    return None


def click_first(page, choices):
    loc = pick_first(page, choices)
    if loc:
        loc.click(timeout=8000)
        return True
    return False


def poll_gmail_code(service, started, used_ids, max_wait=120):
    end = time.monotonic() + max_wait
    while time.monotonic() < end:
        for m in find_messages(service, started - 3, limit=40):
            if m['id'] in used_ids:
                continue
            codes = set(m['codes'])
            if len(codes) == 1:
                used_ids.add(m['id'])
                print('Gmail: fresh verification code found (hidden)')
                return next(iter(codes))
        time.sleep(8)
    raise RuntimeError('No fresh unambiguous Microsoft email code within 120s')


def progress_login(page, gmail_service, login_email, password, max_seconds=300):
    start = time.monotonic()
    used_ids = set()
    sent_at = time.time()
    last_action = ''
    print('Playwright: starting Microsoft authentication proxy')
    while time.monotonic() - start < max_seconds:
        try:
            page.wait_for_load_state('domcontentloaded', timeout=5000)
        except PlaywrightTimeout:
            pass
        try:
            body = page.locator('body').inner_text(timeout=5000).lower()[:6000]
        except Exception:
            body = ''
        url = page.url.lower()
        if 'authentication completed' in body or 'uwierzytelnianie zakończone' in body or 'sign-in complete' in body:
            print('Playwright: page confirms completed authentication')
            return
        # Microsoft Account landing page
        if "it's all here with microsoft" in body or "it’s all here with microsoft" in body:
            print("Microsoft: detected account landing page", flush=True)

            try:
                reject = page.get_by_text("Reject", exact=True)
                if reject.count() and reject.first.is_visible():
                    reject.first.click(timeout=5000)
                    print("Microsoft: rejected optional cookies", flush=True)
            except Exception as exc:
                print("Cookies:", type(exc).__name__, flush=True)

            candidates = [
                page.get_by_text("Sign in", exact=True),
                page.locator("a").filter(has_text=__import__("re").compile(r"^\\s*Sign in\\s*$", __import__("re").I)),
                page.locator('input[value="Sign in"]'),
                page.locator('button:has-text("Sign in")'),
            ]

            found = False

            for locator in candidates:
                count = locator.count()
                print("Microsoft: Sign in candidates:", count, flush=True)

                if not count:
                    continue

                for index in reversed(range(count)):
                    item = locator.nth(index)

                    if not item.is_visible():
                        continue

                    found = True

                    try:
                        item.click(timeout=10000)
                        print("Microsoft: clicked SIGN IN", flush=True)
                        time.sleep(3)
                        break
                    except Exception as exc:
                        print(
                            "Microsoft: click failed:",
                            str(exc).splitlines()[0],
                            flush=True
                        )
                        raise

                if found:
                    break

            if not found:
                print("Microsoft: no visible Sign in control", flush=True)
                time.sleep(2)

            continue

        # Microsoft: "Sign in another way" -> email verification
        if "sign in another way" in body and "send a code to" in body:
            print("Microsoft: detected authentication method selector", flush=True)

            import re
            option = page.get_by_text(
                re.compile(r"Send a code to", re.I)
            ).last

            try:
                option.wait_for(state="visible", timeout=10000)
                sent_at = time.time()
                option.click(timeout=15000)
                last_action = "send_code"
                stall_count = 0
                print("Microsoft: selected email verification code", flush=True)
                time.sleep(3)
                continue
            except Exception as exc:
                print(
                    "Microsoft: could not select email verification:",
                    str(exc).splitlines()[0],
                    flush=True
                )
                raise

        # Keep the authentication tab alive while HA performs Family SSO.
        if ('please wait' in body or 'poczekaj' in body or 'signing you in' in body or 'loading' in body) and not any(x in body for x in ('enter code', 'wprowadź kod', 'enter your password')):
            time.sleep(3)
            continue
        email = pick_first(page, ['input[type="email"]', 'input[name="loginfmt"]', 'input[name="username"]', 'input[autocomplete="username"]'])
        if email and not email.input_value():
            email.fill(login_email)
            click_first(page, ['input[type="submit"]', 'button[type="submit"]', '#idSIButton9', 'button:has-text("Next")', 'button:has-text("Dalej")'])
            last_action = 'email'
            stall_count = 0
            print('Playwright: submitted account email')
            time.sleep(3)
            continue
        pwd = pick_first(page, ['input[type="password"]', 'input[name="passwd"]'])
        if pwd and pwd.is_visible():
            if not password:
                raise RuntimeError('Microsoft requests a password: set MS_PASSWORD in /etc/familysafety-bot.microsoft.env')
            if last_action != 'password':
                pwd.fill(password)
                click_first(page, ['input[type="submit"]', 'button[type="submit"]', '#idSIButton9', 'button:has-text("Sign in")', 'button:has-text("Zaloguj")'])
                last_action = 'password'
                print('Playwright: submitted password (hidden)')
                time.sleep(3)
                continue
        otp = pick_first(page, ['input[autocomplete="one-time-code"]', 'input[name="otc"]', 'input[name="code"]', 'input[id="idTxtBx_SAOTCC_OTC"]', 'input[aria-label*="code" i]', 'input[placeholder*="code" i]', 'input[placeholder*="kod" i]'])
        if otp and otp.is_visible():
            if last_action != 'otp':
                print('Microsoft: waiting for fresh email verification code...')
                code = poll_gmail_code(gmail_service, sent_at, used_ids)
                otp.fill(code)
                click_first(page, ['input[type="submit"]', 'button[type="submit"]', '#idSubmit_SAOTCC_Continue', '#idSIButton9', 'button:has-text("Verify")', 'button:has-text("Weryfikuj")', 'button:has-text("Continue")', 'button:has-text("Dalej")'])
                last_action = 'otp'
                time.sleep(3)
                continue
        if ('stay signed in' in body or 'pozostać zalogowanym' in body or 'nie wylogowuj mnie' in body):
            if click_first(page, ['#idSIButton9', 'input[value="Yes"]', 'button:has-text("Yes")', 'button:has-text("Tak")']):
                print('Playwright: Stay signed in = YES')
                time.sleep(3)
                continue
        if last_action not in ('send_code', 'otp') and ('send a code' in body or 'send code' in body or 'wyślij kod' in body or 'email a code' in body or 'email code' in body):
            if click_first(page, ['button:has-text("Send code")', 'button:has-text("Send a code")', 'button:has-text("Wyślij kod")', 'input[value*="Send"]', '#idSIButton9']):
                sent_at = time.time()
                print('Microsoft: requested email verification code')
                last_action = 'send_code'
                time.sleep(3)
                continue
        if 'captcha' in body or 'prove you are human' in body or 'potwierdź, że jesteś człowiekiem' in body:
            raise RuntimeError('Microsoft CAPTCHA/manual verification required')
        # Save diagnostic screenshot once per run.
        diagnostic = Path('/opt/familysafety-bot/microsoft_screen.png')
        if not diagnostic.exists():
            try:
                page.screenshot(path=str(diagnostic), full_page=True, timeout=10000)
                diagnostic.chmod(0o600)
                print('DIAGNOSTICS: saved microsoft_screen.png', flush=True)
                print('DIAGNOSTICS: input fields:', page.locator('input').count(), flush=True)
                print('DIAGNOSTICS: iframe count:', page.locator('iframe').count(), flush=True)
            except Exception as exc:
                print('DIAGNOSTICS:', type(exc).__name__, flush=True)
        # Diagnose a stalled login without exposing credentials.
        stall_count = locals().get('stall_count', 0) + 1

        if stall_count >= 5:
            diagnostic = Path('/opt/familysafety-bot/microsoft_stalled.png')

            try:
                page.screenshot(
                    path=str(diagnostic),
                    full_page=True,
                    timeout=10000
                )
                diagnostic.chmod(0o600)

                print('DIAGNOSTICS: saved microsoft_stalled.png', flush=True)
                print('DIAGNOSTICS: page title:', page.title()[:100], flush=True)
                print('DIAGNOSTICS: iframe count:', page.locator('iframe').count(), flush=True)

                inputs = page.locator('input')
                for i in range(min(inputs.count(), 20)):
                    element = inputs.nth(i)
                    print(
                        'INPUT', i,
                        'type:', element.get_attribute('type'),
                        'name:', element.get_attribute('name'),
                        'visible:', element.is_visible(),
                        flush=True
                    )

            except Exception as exc:
                print('DIAGNOSTICS:', type(exc).__name__, flush=True)

            # After submitting OTP, Microsoft may display a blank page
            # while Home Assistant processes the authentication callback.
            if last_action == 'otp' and page.title() == '':
                print(
                    'Microsoft: code submitted, HA completion screen. '
                    'Waiting for reauth_success.',
                    flush=True
                )
                return

            raise RuntimeError('Microsoft login stalled; screenshot saved')

        print(
            'Playwright: waiting for Microsoft screen:',
            stall_count, '/5',
            flush=True
        )
        time.sleep(5)
    raise RuntimeError('Playwright login timed out; unknown Microsoft screen or flow incomplete')


def settle(flow_id, seconds=240):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            d = ha('GET', '/api/config/config_entries/flow/' + flow_id)
        except RuntimeError as e:
            if 'HTTP 404' in str(e):
                st = sensor()
                if st[0] == 'connected' and st[1] == 'ready' and not st[2]:
                    return True
            raise
        step, typ = d.get('step_id'), d.get('type')
        if (step, typ) != last:
            print('HA finalization:', step, typ)
            last = (step, typ)
        if step == 'reauth_success' and typ == 'form':
            result = ha('POST', '/api/config/config_entries/flow/' + flow_id, {})
            if result.get('type') not in ('abort', 'create_entry'):
                print('HA final submit type:', result.get('type'))
            for _ in range(35):
                state = sensor()
                if state[0] == 'connected' and state[1] == 'ready' and not state[2]:
                    return True
                time.sleep(3)
            raise RuntimeError('Submitted reauth_success but sensor did not return connected/ready')
        if typ == 'abort':
            raise RuntimeError('HA reauth flow aborted (reason=' + str(d.get('reason')) + ')')
        if typ in ('external', 'external_done', 'progress', 'progress_done'):
            # GET follows HA's flow step; for external+progress HA often transitions
            # through its own callback and progress task. Avoid POSTing empty data
            # at these steps: that can finish too early.
            time.sleep(4)
            continue
        time.sleep(4)
    raise RuntimeError('HA did not reach reauth_success within 240s')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', action='store_true', help='Explicit consent to actually trigger reauth')
    ap.add_argument('--email', help='Microsoft login email (not Gmail mailbox unless same account)')
    args = ap.parse_args()
    if not args.run:
        ap.error('Pass --run to execute real Microsoft reauth. No HA changes without it.')
    email_addr = args.email or os.environ.get('MS_LOGIN_EMAIL')
    if not email_addr:
        email_addr = input('Microsoft parent login email: ').strip()
    password_path = Path('/etc/familysafety-bot.microsoft.env')
    password = None
    if password_path.exists():
        for s in password_path.read_text().splitlines():
            if s.startswith('MS_PASSWORD='):
                password = s.partition('=')[2].strip().strip('"').strip("'")
    print('Gmail: checking OAuth token and API...')
    service = gmail()
    service.users().getProfile(userId='me').execute()
    print('Gmail API: OK')
    print('HA state before:', sensor())
    existing = asyncio.run(flows())
    if len(existing) > 1:
        raise RuntimeError('Multiple reauth flows: refusing to guess')
    if existing:
        fid = existing[0]['flow_id']
        print('HA: reusing existing reauth flow')
    else:
        ha('POST', '/api/services/microsoft_family_safety/request_reauth', {})
        print('HA: request_reauth requested')
        fid = locate_flow()
    first = ensure_step(fid, 'reauth_full_confirm')
    # Preserve integration option; config flow form returns suggested boolean schema.
    # When HTTPS is configured, default false is safe; do not silently enable local HTTP.
    result = ha('POST', '/api/config/config_entries/flow/' + fid, {})
    if result.get('type') != 'external' or not result.get('url'):
        raise RuntimeError('Expected HA external proxy URL after reauth_full_confirm; received ' + str(result.get('type')) + '/' + str(result.get('step_id')))
    proxy = result['url']
    if '/auth/microsoft_family_safety/proxy/' not in proxy:
        raise RuntimeError('Unexpected auth URL; refusing to browse it')
    print('HA: fresh authentication proxy URL generated (hidden)')
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        try:
            page = browser.new_page(
                locale='en-US',
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            )
            print('Playwright: opening Microsoft proxy (commit, timeout 60s)', flush=True)
            try:
                page.goto(proxy, wait_until='commit', timeout=60000)
                print('Playwright: received document response', flush=True)
            except Exception as exc:
                from urllib.parse import urlsplit
                print('Playwright: navigation issue:', type(exc).__name__, flush=True)
                print('Playwright: current domain:', urlsplit(page.url).hostname, flush=True)
                raise
            # Microsoft Account landing page
            print('Microsoft: inspecting landing page', flush=True)

            try:
                reject = page.get_by_role('button', name='Reject', exact=True)
                if reject.count() and reject.first.is_visible():
                    reject.first.click(timeout=5000)
                    print('Microsoft: rejected optional cookies', flush=True)
            except Exception:
                print('Microsoft: cookie banner skipped', flush=True)

            sign_in = None
            for locator in [
                page.get_by_role('link', name='Sign in', exact=True),
                page.get_by_role('button', name='Sign in', exact=True),
                page.get_by_text('Sign in', exact=True),
            ]:
                try:
                    if locator.count() and locator.first.is_visible():
                        sign_in = locator.first
                        break
                except Exception:
                    pass

            if sign_in is not None:
                print('Microsoft: clicking Sign in', flush=True)
                sign_in.click(timeout=15000)
                print('Microsoft: Sign in clicked', flush=True)

            progress_login(page, service, email_addr, password)
            # Keep page and browser alive while Home Assistant finishes Family SSO.
            if not settle(fid, 360):
                raise RuntimeError('HA final confirmation failed')
        finally:
            browser.close()
    print('SUCCESS: Home Assistant FamilySafety connected / family_context ready')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted; HA flow may remain unfinished.', file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print('STOP:', str(exc), file=sys.stderr)
        sys.exit(1)
