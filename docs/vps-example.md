# VPS example: how this project is deployed

[README](../README.md) · [Native deployment](native-vps.md) · [Docker deployment](cloud-deployment.md)

The owner's running installation uses a persistent Bluehost Ubuntu VPS,
Python 3.14, one Uvicorn worker under a dedicated user systemd service, and Nginx
behind Cloudflare on a stable HTTPS hostname. The phone process listens only on
`127.0.0.1:8766`. Browser control, SSE, owner audio WebSockets, MCP and signed
Twilio callbacks all share that HTTPS origin. A separate private calendar bridge
is used when appointment features are enabled. Model inference and the telephone
network are provided by external services; no local GPU is needed.

The paths and hostname below are a reproducible **anonymized example** of that
layout. They do not expose the owner's account, private endpoint or credentials.
Use your own DNS, account, provider configuration and grants.

| Component | Example placement | Purpose |
|---|---|---|
| Phone source and virtualenv | `/home/phoneapp/apps/phone-agent` | One application release |
| Protected configuration | `phone-agent/.env` (mode 600) | Provider, OAuth and feature settings |
| Persistent state | `phone-agent/data` (directory mode 700) | SQLite, sessions and configured media |
| User service | `~/.config/systemd/user/phone-agent.service` | Starts after reboot using linger |
| Local app socket | `127.0.0.1:8766` | Private reverse-proxy upstream |
| HTTPS edge | `https://phone.example.com` | Nginx TLS and Cloudflare DNS/proxy |
| Private backups | `/home/phoneapp/backups/phone-assistant` | Restricted verified SQLite archives |

## 1. Install an isolated release

Provision the VM and account as described in [native-vps.md](native-vps.md).
As the application account, clone the repository and check out the release
commit you have reviewed (the repository default branch contains the documented
release). Keep runtime data outside Git tracking.

```sh
mkdir -p "$HOME/apps"
git clone https://github.com/i-xoxol/phone-assistant.git "$HOME/apps/phone-agent"
cd "$HOME/apps/phone-agent"
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
umask 077
cp .env.example .env
mkdir -p data
chmod 600 .env
chmod 700 data
```

Edit `.env` privately, following [configuration.md](configuration.md). Configure
the stable public URL, REST token, telephone provider and selected voice backend.
For browser joining, also configure the OpenAI transcription key/model. Configure
incoming handling, personalities, contacts, scopes and enabled tools explicitly.
The application does not acquire phone numbers or grant calendar access for you.
Run `.venv/bin/python -m scripts.preflight` before enabling the service.

## 2. Run one worker under systemd

Install [the example unit](../deploy/phone-agent.service), adapting its two paths
and port to the layout above. For this VPS layout use:

```ini
WorkingDirectory=%h/apps/phone-agent
ExecStart=%h/apps/phone-agent/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766 --workers 1 --no-access-log --timeout-graceful-shutdown 10
```

Retain the unit's `UMask=0077`, restart policy, shutdown timeout and unprivileged
account. Then:

```sh
mkdir -p "$HOME/.config/systemd/user"
cp deploy/phone-agent.service "$HOME/.config/systemd/user/phone-agent.service"
# Edit the installed unit's paths and port before starting.
systemctl --user daemon-reload
systemctl --user enable --now phone-agent.service
```

An administrator enables linger for the application user so it survives logout:
`sudo loginctl enable-linger phoneapp`. One worker is required because live media
sessions, event delivery, locks and browser audio hubs are in process.

## 3. Publish HTTPS and connect providers

Use the [Nginx WebSocket/SSE configuration](native-vps.md#https-and-streaming-proxy)
with upstream port 8766. Obtain a valid certificate, configure renewal and reject
unexpected hosts. If Cloudflare is used, use a valid origin certificate/TLS
configuration and deliberate trusted-proxy settings. No direct public access to
8766 or the calendar bridge is needed. Preserve SSH access when setting firewall
rules.

Set the incoming Twilio webhook to your own HTTPS routes documented in
[incoming calls](incoming-calls.md). Outbound calls use the same configured origin
for their signed callbacks and media stream. Verify signature validation instead
of relaxing it to work around proxy mistakes.

Pair the owner browser using [the monitor guide](live-monitor.md), then verify
contacts, profile names, voice selection and recording policy. Connect optional
ChatGPT/Codex supervision using [supervising-agent.md](supervising-agent.md).
Calendar use additionally needs the existing private bridge, fresh mirror and
grants described in [the appointment runbook](appointments-runbook.md).

## 4. Verify without dialing first

Run offline tests and the release audit in a development checkout with dev
requirements installed. Then verify production readiness and a fictional browser
call. Test direct, forwarded and outgoing scenarios, recent forwarding choices,
profile-specific guidance, live captions and listening/joining from a second
paired tab. Browser tests use the real configured voice/tools but do not contact
the telephone gateway; paid model usage still applies. Use fictional contacts and
disable or explicitly scope side-effecting tools when testing them.

A real phone call is a separate check of carrier timing, audio and callbacks.
Keep provider results distinct from offline mocks and browser transport checks.

## 5. Update with a backup and a quiet line

The running installation uses staged, reviewed source releases rather than
editing the active application directory while calls are running:

1. Create a feature branch from the currently deployed branch. Implement and
   review changes. Pass the complete provider-mocked suite, release audit and
   browser checks; validate dependencies and migrations against private copies.
2. Verify there are no active/ringing calls. Create a private backup using
   `scripts.backup_state`, which snapshots the configured SQLite files and checks
   their integrity. Include `.env` and explicitly selected dependency credentials
   only in the protected backup, never the source artifact:

   ```sh
   .venv/bin/python -m scripts.backup_state \
     --directory "$HOME/backups/phone-assistant" --env-file .env
   ```

3. Recheck active calls immediately before restarting. Keep the previous source
   and release manifest. Stop the user service, install the verified source
   artifact atomically, preserve `.env`, virtualenv and persistent state, and
   start the service. A restart cannot preserve an in-flight AI conversation.
4. Check local and public `/health` (including its `ready` field), service state, authenticated
   browser/MCP access and a bounded fictional browser call. Confirm the intended
   release is running. Keep rollback source available; do not overwrite fresh
   production data with an old database merely to revert code.

Use [native-vps.md](native-vps.md) for migration, certificate renewal, dependency
checks and backup restoration details. A GitHub merge alone does not deploy this
VPS, and a green health endpoint alone does not verify a full telephone call.
