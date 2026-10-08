# From clone to first call

[← README](../README.md) · [Configuration](configuration.md) · [Troubleshooting](troubleshooting.md)

This guide sets up one private instance. No software in this repository gives
you access to someone else's phone service. You supply your own accounts and
pay for their usage.

## 1. Prepare your accounts

### Twilio

1. Create or use a [Twilio account](https://console.twilio.com/).
2. Find your **Account SID** and **Auth Token** in account details. The SID starts
   with `AC`; the Auth Token must belong to that same account.
3. Buy or select a number with **Voice** capability under **Phone Numbers →
   Manage → Active Numbers**. Put this number in `TWILIO_PHONE_NUMBER`, with
   country code, for example `+12025550100`. This is the caller number, not your
   personal destination number.
4. On a trial account, verify the destination under **Verified Caller IDs**.
   A trial may play its own announcement and restrict which destinations you can
   call. Check [Twilio's current trial rules](https://www.twilio.com/docs/usage/trials).
5. Enable the destination country in **Voice Geographic Permissions** if needed.
   See [Twilio's geographic permissions guide](https://www.twilio.com/docs/voice/geo-permissions).
6. Configure account usage alerts appropriate to your testing budget.

For these outbound calls, **leave your number's incoming-call webhook alone**.
The application sets its own voice and status URLs on each Calls API request.
You do not need a SIP trunk, Twilio Agent Connect credentials, or a Twilio
Functions deployment.

### OpenAI

1. Use [the API platform](https://platform.openai.com/), enable API billing, and
   select a project where `gpt-live-1` is available.
2. Create a server-side [project API key](https://platform.openai.com/api-keys).
   A ChatGPT subscription is separate from API billing.
3. Give the key access to the Live API. For `LIVE_TOOLS_ENABLED=true`, it must
   also allow **Responses write** and the configured backend model, default
   `gpt-6-luna`.
4. Keep `OPENAI_LIVE_MODEL=gpt-live-1` and `OPENAI_VOICE=marin` for the initial
   test. There is no automatic fallback to another API or model.
5. No OpenAI webhook, SIP number, or browser-side API key is needed for this
   server-to-server WebSocket bridge.

If Live access is unavailable in your project, the network preflight fails;
account creation alone is not proof of access. A restricted Live-only key may
receive an optional history-compaction advisory. The bridge tolerates that
specific advisory, while other initialization failures remain fatal.

Reference: [GPT-Live getting started](https://developers.openai.com/api/docs/guides/live)
and [delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation).

## 2. Install locally

Clone the project and open a terminal in its root:

```bash
git clone https://github.com/i-xoxol/phone-assistant.git
cd phone-assistant
```

On macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
cp .env.example .env
chmod 600 .env
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

If PowerShell blocks activation, call `.\.venv\Scripts\python.exe` directly in
place of `python`. You do not need to change a system-wide execution policy.
For running the service without test dependencies, install `requirements.txt`.
The exact dependency versions are pinned there.

## 3. Configure the instance

Generate a local REST token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `.env` locally. Fill these values:

```dotenv
OPENAI_API_KEY=your-project-api-key
TWILIO_ACCOUNT_SID=your-account-sid
TWILIO_AUTH_TOKEN=your-account-auth-token
TWILIO_PHONE_NUMBER=your-owned-voice-number-in-E164
PUBLIC_BASE_URL=https://YOUR-TUNNEL-HOST
LOCAL_API_TOKEN=your-generated-random-token
CALLER_NAME=your-preferred-name
```

Those strings are placeholders, not valid credentials. `.env.example` intentionally
leaves the actual fields empty. Leave `CALLBACK_NUMBER` empty unless you authorize
the assistant to disclose that number to recipients. Do not paste keys into an
issue, screenshot, chat, or public repository.

Start with `MCP_ENABLED=false` and `LIVE_TOOLS_ENABLED=false` to validate the
audio path first. You can enable voice tools before the manual menu test if
your key has Responses access. [All configuration fields →](configuration.md)

## 4. Start the service and tunnel

In terminal A:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

In terminal B, use ngrok for the complete local prototype:

```bash
ngrok http 8000
```

For an audio-only connectivity experiment you can also use a temporary
Cloudflare Quick Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

**Cloudflare Quick Tunnels do not support SSE**, so they cannot provide the live
transcript monitor. Use ngrok or a named Cloudflare Tunnel for the complete
application. [Cloudflare limitations](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
The [deployment guide](deployment.md#persistent-tunnel) includes named-tunnel setup.

Use the HTTPS origin printed by the tunnel, with no trailing route, as
`PUBLIC_BASE_URL`. Restart terminal A after changing `.env`. Keep both processes
running. Temporary tunnel origins change when the tunnel restarts.

For source development you can add `--reload`; avoid reloads or edits during a
call. One worker is required. `python run.py` is a shorthand for a local server
on port 8000 without reload or access logging.

### Required public routes

| Method and route | Used by | Protection |
|---|---|---|
| `POST /twilio/voice/{call_id}` | Twilio asks for TwiML | Twilio signature and account/call binding |
| `POST /twilio/status/{call_id}` | Twilio call state | Twilio signature and account/call binding |
| `POST /twilio/stream-status/{call_id}` | Media lifecycle/errors | Twilio signature and account/call binding |
| `WSS /twilio/media/{call_id}` | Bidirectional phone audio | Signed upgrade and matching stream format/account/call |
| `GET /health` | Preflight/operator | Public readiness; no configuration values |
| `/mcp` and OAuth discovery/approval/token routes | MCP clients, when enabled | Owner-paired OAuth |
| `/live` and `/live/*` | Browser monitor | One-use pairing, Secure cookie, CSRF on writes |
| `/calls` and `/calls/*` | REST operator/client | Separate local bearer token |

Expose the whole FastAPI origin for the simplest setup. If you route only selected
paths, preserve all OAuth discovery and token routes as well as `/mcp`. A tunnel
must support HTTPS, WebSocket upgrades, and long-lived SSE streams. Do not put an
interactive tunnel login page in front of Twilio.

## 5. Check before dialing

```bash
python -m scripts.preflight
python -m pytest -q
```

These checks are offline. The preflight reports missing setting **names**, never
secret values. A configured `.env` still does not prove the account keys work.

Then run:

```bash
python -m scripts.preflight --network
```

This confirms Twilio authentication and the owned voice number, public `/health`,
and an actual Live session start/finalization. It sends a short silence sample;
it does not call any phone, but it can incur a small OpenAI charge. It cannot
prove the public Twilio media WebSocket works—that needs the next step.

## 6. Pair the monitor

On the service host, in the project directory:

```bash
python -m scripts.web_pairing
```

Open `https://YOUR-TUNNEL-HOST/live` on your phone or laptop and enter the printed
code. It works once for 15 minutes. Use another code for another browser. The
Secure cookie requires **HTTPS**; use the tunnel origin rather than local HTTP
for the monitor. [Monitor guide →](live-monitor.md)

## 7. Make one consenting test call

Ask the person first. This command places **one real, paid outbound call**:

```bash
python -m scripts.manual_call --to +12025550101 --recipient "Consenting test recipient"
```

Replace the fictional number. The script uses the local REST API at
`http://127.0.0.1:8000` and a two-minute limit. If the service is elsewhere, pass
`--base-url` for that service. Its credentials come from the local `.env`.
Creation is never retried automatically. It waits for final status and writes
the private result under ignored `data/`. Ctrl+C requests hangup if the call ID
is already known; if creation timed out, inspect Twilio before trying again.

Verify:

- The phone rings and a trial announcement, if any, completes.
- The assistant introduces itself as the configured caller's personal assistant.
- Both sides can hear one another. Interrupt while it counts and assess the response.
- The monitor displays both speakers and remains connected.
- A supplied constraint is honored; no invented facts or unauthorized commitments appear.
- The assistant says its closing phrase and ends the call after playback.
- The saved record has an OpenAI session ID, `voice_finalized=1`, and no
  `error_message`. A Twilio `completed` status alone is insufficient.

In a separate short test, use the monitor's direct **Hang up call** button and
verify that audio stops and the phone disconnects. With voice tools enabled,
test a navigation-key request and inspect `voice_tool_operations` rather than
assuming the originating chat sent the key.

## 8. Enable the agent interface

Once the first call works, set `MCP_ENABLED=true`, restart with no active calls,
and follow [the MCP guide](mcp.md). Enabling an interface does not place a call.

For real business objectives, give a narrowly defined brief, short time limit,
and explicit disclosure/authorization boundaries. Add availability using
`send_call_update`; if something looks wrong, use direct hangup rather than
waiting for an instruction update to take effect.
