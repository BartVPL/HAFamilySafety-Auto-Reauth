# HAFamilySafety — Fully Unattended Microsoft Reauthentication

**Community testing guide · October 2026 · Unofficial community automation**

> **Status:** The complete reauthentication flow succeeded in a manual end-to-end run, and the first naturally triggered, fully unattended reauthentication also reportedly succeeded. Long-term reliability across multiple Microsoft authentication cycles is still being evaluated. This is **not** an official feature of HAFamilySafety or Home Assistant.
>
> **Important:** This is a setup guide for the **final, tested scripts exported from the working LXC**, not a claim that the original development ZIPs are production-ready. The original drafts were patched repeatedly during testing. Before publishing a downloadable release, export and sanitize the actual deployed versions as explained in §12.

## 1. What does it do?

The watcher runs on a separate Linux host/container and checks Home Assistant regularly. When the HAFamilySafety connection indicates that Microsoft authentication has expired, it:

1. Reads `sensor.microsoft_family_safety_connection` through the HA REST API.
2. Checks that **all** reauth conditions match: `state: degraded`, `reauth_required: true`, `account_interrupt: signin_required`.
3. Sends a notification through Home Assistant.
4. Calls HA service `microsoft_family_safety.request_reauth`.
5. Discovers the new configuration flow through HA WebSocket `config_entries/flow/progress`.
6. Submits `reauth_full_confirm` and receives a fresh short-lived Microsoft authentication proxy URL.
7. Uses headless Playwright/Chromium to open the HA proxy and complete the Microsoft sign-in pages.
8. Selects **Send a code to [the configured email]** when this choice is offered.
9. Polls the Gmail API for a *new* Microsoft one-time-code message, extracts the code, and submits it in the same browser session.
10. Handles the “Stay signed in” prompt if shown, keeps Chromium running through asynchronous HA callback/progress steps, and submits `reauth_success` when available.
11. Verifies `connected`, `family_context: ready`, **and** `reauth_required: false`, then sends the success notification.

If it fails, the watcher reports the failure and uses a cooldown instead of retrying continuously.

### Architecture

```text
HAFamilySafety integration in Home Assistant
        │
        ├── sensor.microsoft_family_safety_connection
        │
        └── config flow / Microsoft auth proxy
                  ▲
                  │ HA REST + WebSocket
                  │
Linux / Proxmox LXC ── systemd timer (every ~1 min)
        │
        ├── familysafety-auto.py (watcher, lock, cooldown, notifications)
        ├── full_reauth.py (HA config flow + Playwright)
        ├── Chromium (Microsoft sign-in)
        └── Gmail API OAuth (read-only OTP retrieval)
```

## 2. Requirements

- A functioning Home Assistant instance with the **custom HAFamilySafety integration**, using **native web authentication** and a recent version that exposes `microsoft_family_safety.request_reauth` and the connection diagnostic sensor. The tested version was **2.0.13**. Verify the service in **Developer Tools → Actions**; do not assume other versions expose the same flow steps.
- Home Assistant reachable from the bot host over LAN or HTTPS. The bot needs to reach HA's **short-lived `/auth/microsoft_family_safety/proxy/...`** URL as well as its REST/WebSocket API.
- A **Home Assistant administrator long-lived access token** used by the bot; keep it secret, restrict filesystem access, and ideally use a dedicated administrator account created only for the bot. HA long-lived access tokens are powerful credentials; use the smallest privileges supported by your HA setup.
- A Debian/Ubuntu Linux VM, LXC, or host. The tested installation used **Debian 13 (Trixie) on Proxmox LXC 106**, Python **3.13**, Python `venv`, and systemd. Proxmox is optional.
- Chromium with Playwright. The tested system used system package `/usr/bin/chromium` (Chromium 155) instead of the Playwright-downloaded browser.
- Google Cloud project with **Gmail API** enabled, OAuth client of type **Desktop app**, and **read-only Gmail scope** `https://www.googleapis.com/auth/gmail.readonly`.
- Access to the mailbox that receives the Microsoft verification codes. This may be a Gmail address used to sign into Microsoft; Gmail API consent is independent of the HA admin token.
- The tested Microsoft account supports **email verification codes**. Microsoft screens change; password-based flows, passkeys, CAPTCHA, device approval or additional security checks are **not guaranteed to be unattended**.
- Optional: working HA notification service (the tested deployment uses `notify.notify`; everyone else must replace this name).
- Outbound HTTPS/DNS to Microsoft, Google, and HA. Reserve sufficient disk space for Chromium. A minimal 8 GB LXC disk became tight during testing; monitor disk usage.

### Security notes

This automation handles an HA admin bearer token, a Google OAuth refresh token, and time-sensitive Microsoft sign-in codes. It is **only appropriate for accounts you own or administer with authorization**. Do not publish token files, authentication proxy URLs, active codes, password files, browser profiles, raw email exports, or screenshots containing them. Never bypass CAPTCHA or other explicitly interactive account security challenges; raise an alert and use a manual fallback.

## 3. Get the **tested** source files

You need the final working versions of:

```text
familysafety-bot/
├── full_reauth.py             # final script from working host, NOT initial development draft
├── familysafety-auto.py       # watcher / notifier
├── familysafety-auto.service  # systemd oneshot unit
├── familysafety-auto.timer    # systemd timer
└── gmail_setup/
    └── familysafety_gmail/
        ├── gmail_reader.py         # FINAL corrected OTP parser
        ├── authorize_gmail_windows.py
        └── requirements.txt
```

The separate read-only `familysafety-bot.py` / `familysafety-bot.service` monitor is **optional**; the `familysafety-auto.timer` performs its own checks and does not depend on it.

**DO NOT distribute:** `gmail_token.json`, `credentials.json` downloaded from Google, `/etc/familysafety-bot.env`, `/etc/familysafety-auto.env`, `/etc/familysafety-bot.microsoft.env`, screenshots, debug outputs, backup files, and `/var/lib/familysafety-auto/state.json`.

**Crucial tested fixes that need to exist in the published `full_reauth.py`:**

- Browser `user_agent` compatible with the Microsoft/HA proxy (in testing a Chrome 131-style UA fixed a request that otherwise stalled).
- `page.goto(proxy, wait_until='commit', timeout=60000)` and explicitly configured Chromium path.
- Handling the Microsoft account landing page and its **Sign in** button.
- Handling the **Sign in another way → Send a code to email** screen.
- Gmail parser that selects the OTP from **`Twój jednorazowy kod: NNNNNN`** (and appropriate English phrases), ignoring unrelated six-digit numbers in URLs, such as `LinkId=...`.
- Waiting through the blank Home Assistant callback screen **while keeping the Chromium session alive**, rather than treating the blank page as successful authentication.
- Final flow transitions `check_mobile_proxy` → `wait_family_proxy` → `finish_proxy` → `reauth_success`, with confirmation in HA and verification of `reauth_required: false`.

The **first versions of the generated ZIP/script files did not include all these fixes**; using those versions unchanged is expected to fail. Obtain the final deployed files before trying the guide.

## 4. Prepare the Linux host (Debian 13 example)

The examples below use `/opt/familysafety-bot`, but any directory can work after updating paths in the scripts and unit files.

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip chromium ca-certificates
sudo mkdir -p /opt/familysafety-bot
sudo python3 -m venv /opt/familysafety-bot/venv
sudo /opt/familysafety-bot/venv/bin/pip install playwright websockets httpx \
  google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

Copy the **tested and sanitized** scripts into `/opt/familysafety-bot`, preserving the layout shown in §3.

Test Chromium **without starting a Microsoft reauth**:

```bash
/opt/familysafety-bot/venv/bin/python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox']); print('CHROMIUM OK'); b.close(); p.stop()"
```

Expected: `CHROMIUM OK`. System Chromium may differ from the Playwright-pinned build: test compatibility locally. The `--no-sandbox` flag was used in the root-run LXC prototype; a dedicated unprivileged user and browser sandbox are recommended for a hardened release.

## 5. Configure Home Assistant access

Create an HA long-lived access token via your profile and store it **locally on the Linux bot host**:

```bash
sudo install -m 600 /dev/null /etc/familysafety-bot.env
sudo nano /etc/familysafety-bot.env
```

Example (replace placeholders; do not paste real values into GitHub):

```dotenv
HA_URL=http://HOME_ASSISTANT_LAN_IP:8123
HA_TOKEN=REPLACE_WITH_PRIVATE_HA_LONG_LIVED_TOKEN
POLL_SECONDS=60
```

Restrict file permissions:

```bash
sudo chmod 600 /etc/familysafety-bot.env
```

Use the correct reachable HA URL. An HA public proxy link generated during reauth also must be reachable by Chromium; successful access to the HA homepage does **not** prove that the Microsoft proxy endpoint can complete its request.

Check that the integration exposes:

```text
sensor.microsoft_family_safety_connection
microsoft_family_safety.request_reauth
```

When healthy, check for attributes such as:

```yaml
state: connected
family_context: ready
reauth_required: false
```

When auth is expired, the tested automation triggers only when:

```yaml
state: degraded
account_interrupt: signin_required
reauth_required: true
```

If your integration uses different identifiers, **change them in the scripts**, not in your HA configuration blindly.

## 6. Set up Gmail API (read-only OAuth)

1. Open [Google Cloud Console](https://console.cloud.google.com/), create or choose a dedicated project.
2. Enable [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
3. Configure **Google Auth Platform** → **Audience** (External if using a personal Google account), consent details and test user where needed.
4. Configure **Data access** to request only `https://www.googleapis.com/auth/gmail.readonly`.
5. Create an OAuth client of type **Desktop app** and download its JSON file as `credentials.json`.
6. To avoid the seven-day expiration of refresh tokens that affects external apps in **Testing** status with non-basic Google scopes, switch to **In production**, where appropriate. Production status does not guarantee tokens can never be revoked. Additional Google verification may be required depending on consent screen/scopes and usage. Do not impersonate unrelated brands or publish misleading app information.
7. On a computer with a regular browser, install the Python dependencies from `requirements.txt` and run:

```powershell
py -m pip install -r requirements.txt
py authorize_gmail_windows.py
```

The script opens Google's consent screen and writes `gmail_token.json` with the OAuth refresh token. Authorize the mailbox receiving Microsoft verification messages. **Keep `credentials.json` and `gmail_token.json` off GitHub.**

Securely copy them to this directory on the Linux bot host:

```text
/opt/familysafety-bot/gmail_setup/familysafety_gmail/credentials.json
/opt/familysafety-bot/gmail_setup/familysafety_gmail/gmail_token.json
```

```bash
sudo chmod 700 /opt/familysafety-bot/gmail_setup /opt/familysafety-bot/gmail_setup/familysafety_gmail
sudo chmod 600 /opt/familysafety-bot/gmail_setup/familysafety_gmail/*.json
```

**Gmail API check (no login triggered):**

```bash
cd /opt/familysafety-bot/gmail_setup/familysafety_gmail
/opt/familysafety-bot/venv/bin/python gmail_reader.py --since-minutes 1440
```

Expected: `Gmail API: OK`; an OTP need not be present yet. The final parser must locate a fresh OTP **unambiguously** from the verified Microsoft sender and the surrounding phrase. Microsoft emails sometimes contain unrelated six-digit tracking/link IDs, so **never use the first six-digit match from the whole email**. Treat multiple plausible codes as ambiguous and stop.

## 7. Configure the unattended watcher and notifications

Place `familysafety-auto.py` in `/opt/familysafety-bot/`, and install the provided `.service` and `.timer`:

```bash
sudo cp familysafety-auto.service /etc/systemd/system/
sudo cp familysafety-auto.timer /etc/systemd/system/
sudo install -m 600 /dev/null /etc/familysafety-auto.env
sudo nano /etc/familysafety-auto.env
```

Contents:

```dotenv
MS_LOGIN_EMAIL=YOUR_MICROSOFT_ACCOUNT_EMAIL
REAUTH_COOLDOWN_HOURS=6
```

Set the HA notification target in `familysafety-auto.py` to **your own valid service**, e.g. `notify.mobile_app_your_phone` or an HA notification group. The example implementation had the **hard-coded** service `notify.notify`; **other users must change it**.

The installed wrapper sends the following messages on the configured HA notifier:

```text
MS Family disconnected - starting reauthentication
MS Family connected - reauthentication successful
MS Family - reauth nieudany, sprawdź logi ...
```

Create protected runtime state:

```bash
sudo mkdir -p /var/lib/familysafety-auto
sudo chmod 700 /var/lib/familysafety-auto
sudo chmod 600 /etc/familysafety-auto.env
sudo systemctl daemon-reload
```

Test the watcher in **read-only mode**, without triggering OAuth:

```bash
sudo sh -c 'set -a; . /etc/familysafety-bot.env; . /etc/familysafety-auto.env; set +a; /opt/familysafety-bot/venv/bin/python /opt/familysafety-bot/familysafety-auto.py --check'
```

Expected in the healthy state:

```text
Sensor state=connected required=False interrupt=None family_context=ready
CHECK ONLY: no reauth or notifications executed
```

Enable unattended checks:

```bash
sudo systemctl enable --now familysafety-auto.timer
sudo systemctl status familysafety-auto.timer
sudo systemctl list-timers --all | grep familysafety
```

Expected: timer `active (waiting)`.

**Systemd roles:** `familysafety-auto.timer` activates the `Type=oneshot` `familysafety-auto.service`. Seeing the service as `inactive (dead)` *after a successful quick check* is normal. Do not enable the oneshot service separately as a persistent daemon.

## 8. Test HA push notifications

You can test the selected notification service through Developer Tools → Actions, or through the HA REST API:

```bash
# Read HA_TOKEN securely from your environment; do not put real tokens in shell history.
curl -fsS -H "Authorization: Bearer $HA_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"title":"FamilySafety test","message":"Test alert from reauth bot"}' \
  "$HA_URL/api/services/notify/YOUR_NOTIFICATION_SERVICE"
```

The HA API accepting a notification does not itself prove that the phone received it; check delivery on the device.

## 9. Controlled full end-to-end test

**Only do this during a maintenance window**: it can invalidate the current Microsoft auth session. Ensure Google OAuth is working and the correct scripts are deployed. Stop any pending auth run, then check whether HA already has an active flow.

Use the included/locally written read-only HA flow check, or use HA WebSocket `config_entries/flow/progress`. Do not launch another reauth on top of a flow in `check_mobile_proxy`.

For initial setup, run the finished auth script **once**:

```bash
/opt/familysafety-bot/venv/bin/python -u /opt/familysafety-bot/full_reauth.py \
  --run --email YOUR_MICROSOFT_ACCOUNT_EMAIL
```

The script should detect the `reauth_full_confirm` step, fetch the transient proxy URL, complete email OTP login, keep Chromium open through HA callback processing, submit `reauth_success`, and verify the HA diagnostic sensor. It may take several minutes; **do not press Ctrl+C simply because Chromium shows an empty final callback page**.

**Actual successful flow observed in testing:**

```text
HA: request_reauth wysłany
HA: wykryto jeden proces reauth
HA flow step: reauth_full_confirm / type: form
HA: nowy proxy URL wygenerowany (ukryty)
Microsoft: KLIKNIĘTO SIGN IN
Playwright: wpisano login
Microsoft: wybrano kod wysyłany na Gmail
Gmail: świeży kod rozpoznany (ukryty)
Playwright: pozostawanie zalogowanym = TAK
HA finalizacja: check_mobile_proxy external
HA finalizacja: wait_family_proxy progress
HA finalizacja: finish_proxy progress
HA finalizacja: reauth_success form
SUCCESS: Home Assistant FamilySafety connected / family_context ready
```

**Success criteria (all three):**

```yaml
state: connected
family_context: ready
reauth_required: false
```

The watcher subsequently verifies this final condition. A network HTTP 200 or `state: connected` while `reauth_required: true` is **not** sufficient.

After the manual end-to-end test, keep the timer running until the session naturally expires. In the first reported unattended test, the runner independently detected the problem, finished reauth, and notified the user. Additional cycles are still useful for assessing reliability.

## 10. Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| `Temporary failure resolving ...` | DNS in the container. Check `/etc/resolv.conf`, DNS resolution, and connectivity. Some Proxmox LXCs can inherit nonfunctional Tailscale DNS configuration. |
| `CHROMIUM OK` never appears | Playwright browser not installed or missing OS dependencies. Try Debian `chromium` and set `executable_path='/usr/bin/chromium'`; ensure free disk space. |
| Proxy `page.goto` times out | Check reachability of the specific HA auth proxy, not only the HA homepage. In our test, a browser-like `User-Agent` was needed for Microsoft/Akamai to respond to some requests. |
| Bot stays on Microsoft account landing page | The **Sign in** landing-page button needs a dedicated Playwright handler. |
| Bot stays on **Sign in another way** | Select **Send a code to [email]** rather than assuming an OTP field is immediately visible. |
| Gmail receives mail but bot times out | Inspect message sender, received time, parser, and whether multiple six-digit sequences appear. In our Polish email, a footer URL contained a separate six-digit `LinkId`. |
| Blank page after OTP / “Stay signed in” | It can be HA/Microsoft callback processing. Keep browser alive and poll the HA flow; wait for `reauth_success` instead of raising a false stall error. |
| `connected` but `reauth_required: true` | Authentication has **not** formally completed. Finish the flow and verify all attributes. |
| Active stale `check_mobile_proxy` flow after a failed test | Do **not** re-run the script blindly. First cancel only that stale configuration flow using HA's flow API, and only after confirming its identity. Never delete/re-add the integration to clear a flow. |
| `CAPTCHA`, passkey-only or device-approval prompt | Stop safely and send a failure notification. Do not bypass security challenges. |
| Timer healthy but no unattended reauth | Inspect the exact three watcher conditions; another `degraded` condition may not be `signin_required`. Check journal and latest saved state. |

### Logs / status

```bash
systemctl status familysafety-auto.timer
journalctl -u familysafety-auto.service -n 100 --no-pager
systemctl list-timers --all | grep familysafety
cat /var/lib/familysafety-auto/state.json
```

The watcher uses an `flock` lock to prevent parallel runs, a **6-hour** default cooldown between attempts, a **15-minute** subprocess timeout, and records the last attempt/result in `state.json`. The `.service` has a **20-minute** systemd start timeout. Adjust deliberately for your own setup.

## 11. Operational precautions

- Use **one controller** for auth. The optional read-only monitor can coexist with the watcher, but do not schedule multiple independent runners.
- Keep the bot's local clock correct (NTP). New-code searches use timestamps to reject old email OTPs.
- Avoid printing OTP values, Microsoft proxy URLs, cookies, HA bearer tokens or Gmail OAuth data to journal.
- Browser screenshots of Microsoft sign-in may include sensitive information. Save with mode `0600`, delete after troubleshooting, and never attach unredacted images publicly.
- Store OAuth files and HA token as `0600`; restrict their parent directory. Consider Linux account isolation and secret rotation.
- If your Gmail OAuth app is External/Testing, its refresh token may expire after seven days; use a properly configured production consent screen where appropriate.
- Monitor rate limits and avoid retry storms. The tested runner uses a long cooldown and handles failures via notifications.
- Google and Microsoft can change their auth flows without warning. Treat this as community-maintained browser automation, **not a guaranteed permanent fix**.
- A Microsoft account with additional enforced MFA may require an operator, even if the current passwordless email-code path works.

## 12. How to export/share the tested configuration securely

**Run on the working Linux host only after verifying the final scripts.** The development archive from the initial implementation is **not** the same as the tested, patched version.

Create a sanitized staging directory:

```bash
sudo mkdir -p /root/familysafety-release/familysafety_gmail
sudo cp /opt/familysafety-bot/full_reauth.py /root/familysafety-release/full_reauth.py
sudo cp /opt/familysafety-bot/familysafety-auto.py /root/familysafety-release/
sudo cp /etc/systemd/system/familysafety-auto.service /root/familysafety-release/
sudo cp /etc/systemd/system/familysafety-auto.timer /root/familysafety-release/
sudo cp /opt/familysafety-bot/gmail_setup/familysafety_gmail/gmail_reader.py /root/familysafety-release/familysafety_gmail/
sudo cp /opt/familysafety-bot/gmail_setup/familysafety_gmail/authorize_gmail_windows.py /root/familysafety-release/familysafety_gmail/
sudo cp /opt/familysafety-bot/gmail_setup/familysafety_gmail/requirements.txt /root/familysafety-release/familysafety_gmail/
```

**Manually review these copies before releasing.** At minimum, replace the hard-coded `notify.notify` service with a clearly documented configurable value; confirm no personal addresses, local network IPs, full auth proxy URLs, credentials, or debug output are embedded. The original `full_reauth.py` may include hard-coded paths, which must be documented or parameterized.

Review only the release directory, and package it without sensitive files:

```bash
cd /root
find familysafety-release -type f -maxdepth 3
# Review files, then:
tar -czf familysafety-release-reviewed.tar.gz familysafety-release
```

**Never package the live `/opt/familysafety-bot` directory wholesale**, because it contains `gmail_token.json`, `credentials.json` and potentially diagnostic screenshots, `.bak` copies and sensitive runtime state.

## 13. Example community README status block

> **HAFamilySafety unattended reauth — community proof of concept:** End-to-end Microsoft email-code reauthentication through HA native auth was tested successfully. The first naturally triggered reauth also completed without user intervention, with HA push notifications for start, success, and failure. This uses a separate Linux bot with Playwright and Gmail API read-only OAuth. Long-term testing is ongoing; scripts and setup instructions should be shared only after removing personal credentials and reviewing security implications. Not affiliated with Microsoft, Google, HAFamilySafety, or Home Assistant.

### References

- [HAFamilySafety custom integration](https://github.com/noiwid/HAFamilySafety)
- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)
- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)
- [Google Gmail API](https://developers.google.com/workspace/gmail/api/guides)
- [Google OAuth consent and publishing](https://support.google.com/cloud/answer/10311615)
- [Playwright Python](https://playwright.dev/python/docs/intro)

---

**Document scope:** Reproduces the architecture, expected behavior, tested flow, and deployment configuration as described by the working setup. It is **not** a distributable turnkey package until the final scripts have been exported from the active LXC, sanitized, and independently tested on another user's setup.
