#!/usr/bin/env bash
set -euo pipefail
if [[ "${EUID}" -ne 0 ]]; then echo 'Run with sudo/root'; exit 1; fi
cd "$(dirname "$(readlink -f "$0")")"
apt-get update
apt-get install -y python3-venv python3-pip chromium ca-certificates
install -d -m 700 /opt/familysafety-bot /var/lib/familysafety-auto
python3 -m venv /opt/familysafety-bot/venv
/opt/familysafety-bot/venv/bin/pip install -r requirements.txt
install -m 600 src/full_reauth.py /opt/familysafety-bot/full_reauth.py
install -m 600 src/familysafety-auto.py /opt/familysafety-bot/familysafety-auto.py
install -d -m 700 /opt/familysafety-bot/gmail_setup/familysafety_gmail
install -m 600 src/gmail_setup/familysafety_gmail/gmail_reader.py /opt/familysafety-bot/gmail_setup/familysafety_gmail/gmail_reader.py
install -m 600 src/gmail_setup/familysafety_gmail/authorize_gmail_windows.py /opt/familysafety-bot/gmail_setup/familysafety_gmail/authorize_gmail_windows.py
install -m 600 config/familysafety-bot.env.example /etc/familysafety-bot.env.example
install -m 600 config/familysafety-auto.env.example /etc/familysafety-auto.env.example
install -m 644 src/familysafety-auto.service /etc/systemd/system/familysafety-auto.service
install -m 644 src/familysafety-auto.timer /etc/systemd/system/familysafety-auto.timer
systemctl daemon-reload
printf '\nInstalled. Configure /etc/familysafety-bot.env and /etc/familysafety-auto.env,\n'
printf 'authorize Gmail, then run --check before enabling timer. See README.md.\n'
