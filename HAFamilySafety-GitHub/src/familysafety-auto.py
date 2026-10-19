#!/usr/bin/env python3
"""Systemd timer launcher for existing tested full_reauth.py (no changes to that script)."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, urlopen

BASE = Path('/opt/familysafety-bot')
DATA = Path('/var/lib/familysafety-auto')
STATE = DATA / 'state.json'
LOCK = DATA / 'run.lock'
SENSOR = 'sensor.microsoft_family_safety_connection'
LOGGER = logging.getLogger('familysafety-auto')


def ha(method, endpoint, payload=None):
    base = os.environ['HA_URL'].rstrip('/')
    token = os.environ['HA_TOKEN']
    content = json.dumps(payload).encode() if payload is not None else None
    req = Request(base + endpoint, data=content, method=method,
                  headers={'Authorization': 'Bearer ' + token,
                           'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as res:
        raw = res.read()
        return json.loads(raw) if raw else None


def sensor():
    item = ha('GET', '/api/states/' + SENSOR)
    attrs = item.get('attributes') or {}
    return {'state': item.get('state'),
            'required': attrs.get('reauth_required') is True,
            'interrupt': attrs.get('account_interrupt'),
            'family_context': attrs.get('family_context')}


def notify(message):
    # A call failure must never abort the auth process.
    try:
        ha('POST', '/api/services/notify/' + os.getenv('HA_NOTIFY_SERVICE', 'notify.notify').removeprefix('notify.'), {'message': message,
                'title': 'Microsoft Family Safety'})
        LOGGER.info('HA notification submitted')
    except Exception as exc:
        LOGGER.error('Notification failed: %s: %s', type(exc).__name__, str(exc)[:120])


def load_state():
    if not STATE.exists():
        return {}
    return json.loads(STATE.read_text(encoding='utf-8'))


def save_state(obj):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj), encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(STATE)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='read-only check, never start reauth')
    args = parser.parse_args()
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.INFO)
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open('a+') as fd:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOGGER.info('Another reauth attempt already running; skipping')
            return

        current = sensor()
        LOGGER.info('Sensor state=%s required=%s interrupt=%s family_context=%s',
                    current['state'], current['required'], current['interrupt'], current['family_context'])
        if args.check:
            LOGGER.info('CHECK ONLY: no reauth or notifications executed')
            return
        # Do not react to degraded for unrelated reasons or connected+stale required.
        if not (current['state'] == 'degraded' and current['required'] and
                current['interrupt'] == 'signin_required'):
            return

        state = load_state()
        now = time.time()
        cool_hours = max(1, int(os.getenv('REAUTH_COOLDOWN_HOURS', '6')))
        last = float(state.get('last_attempt', 0))
        if now - last < cool_hours * 3600:
            LOGGER.info('Cooldown active: wait another %d minutes', int((cool_hours * 3600 - (now - last)) / 60))
            return
        login = os.getenv('MS_LOGIN_EMAIL', '').strip()
        if not login:
            LOGGER.error('Missing MS_LOGIN_EMAIL in /etc/familysafety-auto.env')
            return
        script = BASE / 'full_reauth.py'
        if not script.is_file():
            LOGGER.error('Missing %s', script)
            return

        # Mark attempt before starting: survives VM/LXC restarts, avoids retry storms.
        state['last_attempt'] = now
        state['last_result'] = 'started'
        save_state(state)
        notify('MS Family disconnected - starting reauthentication')
        cmd = [str(BASE / 'venv/bin/python'), '-u', str(script), '--run', '--email', login]
        LOGGER.info('Starting automated reauth; timeout 900 seconds')
        try:
            completed = subprocess.run(cmd, cwd=str(BASE), timeout=900, check=False)
            after = sensor()
            ok = (completed.returncode == 0 and after['state'] == 'connected'
                  and after['family_context'] == 'ready' and not after['required'])
            if ok:
                state['last_result'] = 'success'
                state['last_success'] = time.time()
                save_state(state)
                notify('MS Family connected - reauthentication successful')
                LOGGER.info('SUCCESS: reauth completed and sensor verified')
            else:
                state['last_result'] = 'failed'
                save_state(state)
                notify('MS Family reauthentication failed - check the service journal')
                LOGGER.error('Reauth failed: exit_code=%s after=%s', completed.returncode, after)
                sys.exit(1)
        except subprocess.TimeoutExpired:
            state['last_result'] = 'timeout'
            save_state(state)
            notify('MS Family reauthentication timed out after 15 minutes - check the service journal')
            LOGGER.error('Reauth timeout after 900s')
            sys.exit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        LOGGER.exception('Unexpected auto-runner error: %s', type(exc).__name__)
        sys.exit(1)
