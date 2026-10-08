# Run it on your own server

[← README](../README.md) · [Configuration](configuration.md) · [Security](security.md)

The same small application can run on a Linux home server or VPS. A persistent
HTTPS tunnel avoids router port forwarding. This guide uses a user-level systemd
unit, a loopback listener, and your own public origin. It does not modify any
existing deployment.

## Install as the service owner

Log into your server as the account that will own the service:

```bash
mkdir -p "$HOME/apps"
cd "$HOME/apps"
git clone https://github.com/i-xoxol/phone-assistant.git
cd phone-assistant
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
mkdir -p data
chmod 600 .env
chmod 700 data
```

Populate `.env` locally with your provider credentials, generated REST token,
identity, and stable `PUBLIC_BASE_URL`. Run the offline tests and preflight:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.preflight
```

The unit expects `$HOME/apps/phone-assistant`. If you choose a different path,
edit `WorkingDirectory` and `ExecStart` in your installed unit. `%h` is expanded
by systemd to the service user's home. Never add credentials to the unit file.

## Install the user service

```bash
mkdir -p "$HOME/.config/systemd/user"
cp deploy/phone-agent.service "$HOME/.config/systemd/user/phone-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now phone-agent.service
systemctl --user status phone-agent.service
journalctl --user -u phone-agent.service --since '10 minutes ago'
```

If it must start after reboot without an interactive login, an administrator can
enable user lingering:

```bash
sudo loginctl enable-linger "$USER"
```

The example unit binds `127.0.0.1:8000`, runs one worker, disables access logging,
uses `UMask=0077`, and restarts on failure. Loopback is appropriate when the
tunnel or reverse proxy runs on the same machine. If a separate machine or
container must reach the app, choose the necessary listener deliberately and
restrict it with your network/firewall. Do not copy another operator's LAN IP.

## Persistent tunnel

Quick tunnels can test audio connectivity, but Cloudflare Quick Tunnels do not
support SSE and cannot serve this live monitor. Use ngrok or a named Cloudflare
Tunnel for the complete application. [Quick Tunnel limitations](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
A stable hostname also prevents
broken Twilio callbacks, OAuth resource URLs, and browser origins after restart.
Use a named Cloudflare Tunnel or a reserved ngrok domain under your account.

For a locally managed Cloudflare Tunnel, install `cloudflared`, authenticate
your account, create a tunnel, and route your own DNS hostname:

```bash
cloudflared tunnel login
cloudflared tunnel create phone-assistant
cloudflared tunnel route dns phone-assistant phone.example.com
```

The create command reports a tunnel UUID and stores a credential JSON locally.
Use those **local** values in `$HOME/.cloudflared/config.yml`; never commit that
file or credential JSON. Example configuration:

```yaml
tunnel: YOUR-TUNNEL-UUID
credentials-file: /home/YOUR-USER/.cloudflared/YOUR-TUNNEL-UUID.json
ingress:
  - hostname: phone.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Then run it in a separate terminal, or install a persistent tunnel service using
Cloudflare's guide:

```bash
cloudflared tunnel --config "$HOME/.cloudflared/config.yml" run phone-assistant
```

Replace the hostname and configure the tunnel with your own account credentials.
Do not commit tunnel tokens or credential JSON. If cloudflared runs in a
container, container loopback is not necessarily host loopback; use a verified
host route and matching app listener instead.

Set `PUBLIC_BASE_URL=https://phone.example.com`, restart the service when no calls
are active, and run:

```bash
.venv/bin/python -m scripts.preflight --network
.venv/bin/python -m scripts.check_live_page
```

The latter creates and revokes a temporary browser session to check public
authentication and SSE heartbeat without printing transcripts or dialing.
Provider preflight can incur a small OpenAI charge.

References: [Cloudflare tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
and [ngrok documentation](https://ngrok.com/docs/).

## Optional reverse proxy

A direct tunnel to Uvicorn is enough. If you already operate nginx, preserve
WebSocket upgrades and disable SSE buffering. Inside the intended server block:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_buffering off;
    access_log off;
}
```

This is a focused fragment, not a complete TLS configuration. Keep TLS and DNS
under your existing setup. Avoid URL rewriting, path prefixes, or login
interstitials on `/twilio/*`. Twilio signatures are checked against the configured
public URL, not arbitrary forwarded headers. Proxy buffering can make a connected
monitor appear to deliver captions in batches.

## Pair on the server

From the actual service directory and environment:

```bash
.venv/bin/python -m scripts.web_pairing
.venv/bin/python -m scripts.mcp_pairing
```

These generate **different** codes. Browser pairing uses the calls database;
OAuth pairing uses the OAuth database. Running the command on an unrelated
laptop checkout creates a code in that laptop's database, which will not work
on the server.

## Updates and active calls

An ordinary `git pull` while the process is running does not hot-replace its
loaded code, but may change files it reads at runtime. Avoid any source update
during active calls. Query state before updating:

```bash
.venv/bin/python -m scripts.deployment_check
```

Ensure all calls are terminal (`completed`, `failed`, `busy`, `no_answer`,
`cancelled`) and prevent new calls during maintenance. Prepare/test the new
release in a separate checkout before stopping a working service.

For a simple update:

1. Record the current Git revision and back up the databases as described below.
2. Stop the user service.
3. Pull the intended revision and install its pinned dependencies.
4. Run the offline tests and preflight.
5. Start the service and verify public health, paired monitor, and MCP retrieval.
6. If validation fails, restore the previous revision/dependencies and restart.

The included archive path provides another operator workflow:

- `python -m scripts.package` builds source/plugin ZIPs under ignored `data/`.
- `python -m scripts.test_release` checks a staged source ZIP in a temporary Linux
  virtual environment without stopping the running service.
- `python -m scripts.update_service` refuses nonterminal calls, backs up SQLite
  and source, stops the user service, extracts an allowlisted ZIP, installs/test
  checks, restores source on failure, and restarts in a `finally` block.

These scripts assume the example unit name and default `data/*.sqlite3` paths.
For custom database paths or service names, adapt and review them before use.
The active-call check is an MVP maintenance guard, not a transactional distributed
lock; prevent new call requests throughout the update. Backups remain private.

## Backups and retention

Call data and OAuth credentials persist in `data/`. Back them up only to private
storage. Do not put them in Git, releases, or a public sync folder. During a
running SQLite WAL database, copying only the `.sqlite3` file can omit recent
transactions. Prefer the SQLite backup API or stop the service and safely copy
the complete database state.

Example online backup, run from the project root, writes to ignored `data/backup/`:

```bash
python - <<'PY'
import sqlite3
from pathlib import Path
from app.config import Settings

s = Settings()
folder = Path('data/backup')
folder.mkdir(mode=0o700, parents=True, exist_ok=True)
for source_path in (s.database_path, s.oauth_database_path):
    if source_path.exists():
        destination = folder / source_path.name
        with sqlite3.connect(source_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
        destination.chmod(0o600)
PY
```

Set your own data-retention policy. There is no scheduled deletion or encrypted
storage layer in this MVP. Losing/restoring old OAuth state can require clients
to reconnect. Restoring call data does not restore a live phone conversation.

## Windows development controls

`scripts/local_service.ps1` can start/status/stop/restart the local port-8000
process and leaves its tunnel running. It verifies that the listener belongs to
this checkout before stopping it. Use it only with no active calls. Its process
IDs/logs are stored under ignored `data/`; it does not install a Windows service
or scheduled task.
