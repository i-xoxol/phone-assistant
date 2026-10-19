# Live transcript monitor

[← README](../README.md) · [Architecture](architecture.md) · [Security](security.md)

The monitor is a plain HTML/CSS/JS page at `/live`, designed for both phone and
laptop screens. It displays recent calls, state, duration, and two-speaker
captions, with a large fixed **Hang up call** button.

## Sign in on a phone and laptop

The portal supports a reusable password and one-time pairing codes. To create
the first password, pair one browser using the instructions below, then open
**Password** in the monitor header. Choose a unique password of 12–256 characters
and save it in your password manager. The username is **owner**. No email or
provider account password is needed.

On another device, or after cookies/session storage reset, open `/live/login`
and sign in with **owner** and your saved password. Login and password settings
use ordinary HTML forms with password-manager autocomplete fields. Password
sign-in issues the same seven-day private session as pairing. It does not change
MCP OAuth or REST authentication.

To change your password, open **Password** and enter the current password plus
the new one. Creating/changing a password revokes all earlier browser sessions
and unused pairing codes; the saving browser receives a new session. Other
browsers must sign in again. An open event stream checks session revocation on
its next event/heartbeat.

### Pair the first browser or another device

On the machine running the service:

```bash
python -m scripts.web_pairing
```

Open `https://YOUR-PUBLIC-HOST/live` in the first browser and enter the code.
Generate another code for the second device. Codes are single-use and expire in
15 minutes. Successful pairing creates a browser session valid for seven days.

Alternatively, after pairing your laptop, press **Pair mobile**. The page displays
a new one-use code. Open the same HTTPS site on your phone and enter it; do not
put the code into a URL or send it through a public channel.

Codes from `scripts.mcp_pairing` are for OAuth and do not work here. Monitor
codes from `scripts.web_pairing` do not work on the MCP approval page.

Use the configured HTTPS origin. The `__Host-` session cookie is Secure,
HttpOnly, SameSite=Strict, and scoped to `/`. Local plain HTTP cannot provide
the same login. Private browsing, clearing cookies, switching browsers, or an
expired session requires signing in with your saved password or a new pairing
code. **Sign out** revokes that browser session.

### Forgotten password

From your private terminal on the service host, run:

```bash
python -m scripts.web_pairing --reset-password
```

This clears the portal password, revokes all browser sessions and unused codes,
and prints a fresh one-time pairing code. Pair once, then create a new password.
It leaves call history, recordings, MCP connections and provider keys intact.
There is no public password-reset endpoint. Ordinary pairing does not override
an existing password's current-password requirement.

## Select and follow a call

The selector lists recent records. Choose a call, or open the `live_url`
returned by `place_call`/`get_call_status` to select that call. **Follow live**
keeps the transcript scrolled to recent content; uncheck it to review earlier
text while new fragments continue arriving.

Transcript fragments preserve source IDs and speaker timing. Late fragments can
update an earlier speaker block. The UI escapes text through DOM text handling;
transcript content is data, not executable markup or instructions.

## Push mechanism and latency

The page reads initial state from `/live/bootstrap`, then opens an authenticated
**Server-Sent Events** connection at `/live/events`. It does **not** depend on an
outgoing webhook or repeated `get_call_result` polling.

Each durable call event wakes the stream. SQLite IDs and `Last-Event-ID` let the
browser replay missed events after reconnect, with source IDs preventing duplicate
caption fragments. Five-second heartbeats show transport liveness. A warning
appears when the stream is disconnected or stale.

“Live” means text is pushed as GPT-Live produces it. It does not remove model
transcription delay, mobile network latency, browser suspension, or recognition
errors. Captions may trail speech and should not be treated as an exact record
of all audio already played. A mobile background tab may pause and recover
missed text when reopened.

Reverse proxies must allow long-lived responses and disable buffering for SSE.
See [deployment](deployment.md) for a proxy example.
Cloudflare Quick Tunnels do not support SSE; use ngrok or a named Cloudflare
Tunnel for this page. [Provider limitation](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

## Direct hangup

The button sends `POST /live/calls/{call_id}/hangup` directly to the service with
the paired cookie and CSRF token. It does not wait for an MCP client or chat
to notice an event.

The manager seals outgoing audio and clears queued Twilio playback before
requesting provider hangup. Network/provider confirmation still takes time; if
it cannot be confirmed, the page reports uncertainty. Outgoing audio remains
blocked locally. Retry hangup or inspect Twilio's active call if the telephone
is still connected. Avoid sending a context update as an emergency stop—updates
are model input and cannot cancel an action already in progress.

The button is disabled for terminal calls and while an action is pending. Test
this control on a short consenting call before relying on a fresh deployment.

## Privacy

Every paired browser can see the owner's recent call records; this is a
single-owner monitor, not per-call sharing. Only pair devices you control. No
provider API key, static REST token, or permanent access token is embedded in
the monitor URL. Runtime cookies and codes are never included in the public
repository or its illustrative preview.
