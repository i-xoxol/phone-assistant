# Events and webhook delivery

[← README](../README.md) · [MCP setup](mcp.md) · [Live monitor](live-monitor.md)

There are three different event paths. Their authentication and delivery
semantics are deliberately separate:

| Path | Destination | Format / setup |
|---|---|---|
| Browser SSE | Paired `/live` browser | Cookie-authenticated event stream; no webhook receiver |
| MCP subscriptions | Client-supplied public HTTPS receiver | Standard Webhooks; per-call filter; OAuth ownership |
| Generic outgoing webhook | Operator-configured `WEBHOOK_URL` | Custom `X-Phone-*` HMAC; service-wide new-event feed |

## MCP event subscriptions

Two events are available: `transcript.delta` and `call.status`. Both require a
specific call UUID. New subscriptions begin at the current event head; they
do not export historical transcripts. Read `get_call_result` for saved text.

### For ordinary tool clients

Create a receiver you control and a separate random signing secret:

```bash
python -c "import base64,secrets; print('whsec_'+base64.b64encode(secrets.token_bytes(32)).decode())"
```

Keep the result private. Start the included receiver in a separate terminal.
On macOS/Linux:

```bash
export MCP_WEBHOOK_SECRET='YOUR-GENERATED-whsec_-SECRET'
python -m uvicorn scripts.mcp_webhook_receiver:app --host 127.0.0.1 --port 8781 --no-access-log
```

On PowerShell:

```powershell
$env:MCP_WEBHOOK_SECRET = 'YOUR-GENERATED-whsec_-SECRET'
python -m uvicorn scripts.mcp_webhook_receiver:app --host 127.0.0.1 --port 8781 --no-access-log
```

The strings above are placeholders. For a real secret, enter it through a local
protected configuration method rather than saving it in shell history. Expose
port 8781 with a **separate** HTTPS tunnel and use its `/events` path:

```json
{
  "call_id": "1b375389-a97b-41fb-a70e-6f65b19f625a",
  "callback_url": "https://receiver.example.com/events",
  "signing_secret": "whsec_<base64-encoded-random-secret>",
  "event_name": "transcript.delta",
  "ttl_minutes": 60
}
```

Pass these to `subscribe_call_events` through your private authenticated MCP
client. Repeat for `call.status` if desired. The service sends a **signed
verification challenge** first and requires the receiver to echo it. Only then
is the subscription active.

Inspect `get_call_event_subscriptions(call_id)`: it exposes active state,
expiration/refresh metadata, cumulative acknowledged `delivered_events`, and
`last_delivery_at`, without exposing URLs or secrets. Verification is not actual
event receipt, and receipt is not proof another model read or acted on the text.
Stop with `unsubscribe_call_events(subscription_id)`.

A subscription tool cannot create a callback address for an arbitrary ChatGPT
conversation. Supply a real receiver or use native support where the client
provides one. Do not invent URLs. To send information back to the live agent,
use `send_call_update`; incoming webhook receipt is not a reply channel.

### For native MCP Events clients

The server advertises events and implements these protocol methods:

- `events/list`
- `events/subscribe`
- `events/unsubscribe`

They are separate from the ordinary tool inventory. Protocol `2026-07-28`
clients can discover event definitions. A subscribe request has this shape:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "events/subscribe",
  "params": {
    "name": "transcript.delta",
    "arguments": {"call_id": "1b375389-a97b-41fb-a70e-6f65b19f625a"},
    "delivery": {
      "mode": "webhook",
      "url": "https://receiver.example.com/events",
      "secret": "whsec_<base64-encoded-random-secret>"
    },
    "ttlMs": 3600000
  }
}
```

The ordinary tools adapt to this same implementation. Native default/maximum TTL
is 24 hours; ordinary tools default to one hour. A shorter TTL has a 60-second
minimum. Refresh is idempotent; signing-key rotation has a five-minute overlap.
The protocol cursor is null and there is no protocol history replay after expiry.

Consult the [official MCP Events guide](https://developers.openai.com/plugins/build/mcp-events)
for the client's current capability requirements. In a supporting ChatGPT Work
or dot flow, verify discovery, subscription, challenge, delivery, and observed
client handling individually. An advertised event catalog alone does not prove
the client subscribed. Events can be batched or processed asynchronously.

### Signatures, delivery, and receiver behavior

MCP subscriptions use `webhook-id`, `webhook-timestamp`, and `webhook-signature`
headers from **Standard Webhooks**, plus `X-MCP-Subscription-Id`. The included
receiver verifies the raw request body with `standardwebhooks.Webhook`, validates
the event name and ID, echoes verification challenges, and stores real events
with durable deduplication in ignored `data/received-mcp-events.sqlite3`.

Delivery is at least once with bounded retries and exponential backoff. A `410`
disables the subscription; `413` or exhausted attempts drops that event and logs
only identifiers. Return 2xx after durable receipt, then process asynchronously.
Do not trigger actions twice on a duplicate delivery.

Public callback URLs are validated for HTTPS, allowed form, and public IPs.
Connections resolve DNS at connection time, reject non-global destinations,
pin the selected IP, preserve TLS hostname verification, and refuse redirects.
This prevents user-supplied callbacks from reaching private services through
the subscription path.

Subscriptions are owned by their OAuth connection and stop after expiry,
unsubscribe, or grant revocation. Signing secrets must remain available for
delivery and are stored in the private calls database, not merely hashed.
Protect that database as a credential store as well as a transcript store.

## Generic operator-configured outgoing webhook

This is optional and independent of MCP subscriptions. Set these on the **main
service**:

```dotenv
WEBHOOK_URL=https://receiver.example.com/events
WEBHOOK_SECRET=your-separate-random-HMAC-secret
```

Restart with no active calls. First configuration starts at the current event
head, so old calls are not unsolicited exports. The generic destination receives
new service events, including creation, status, transcript, keypad, updates,
context-delivery state, and media diagnostics. It is not restricted to one call.

Illustrative payload:

```json
{
  "id": 42,
  "call_id": "1b375389-a97b-41fb-a70e-6f65b19f625a",
  "type": "transcript.delta",
  "created_at": "2026-01-01T12:00:00+00:00",
  "data": {
    "speaker": "callee",
    "timestamp": 4.2,
    "end_timestamp": 4.5,
    "text": "The request is ready.",
    "source_event_id": "example-fragment"
  }
}
```

The **custom** signature is:

```text
X-Phone-Signature = sha256=HMAC_SHA256(secret, timestamp + "." + raw_body)
X-Phone-Timestamp = Unix seconds
X-Phone-Event-Id = decimal durable event ID
```

Reject timestamps more than five minutes from your clock, compare signatures in
constant time, match the header ID to the body, and deduplicate before acting.
`scripts/webhook_receiver.py` implements this format. Start it separately with
its matching `WEBHOOK_SECRET` process environment and:

```bash
python -m uvicorn scripts.webhook_receiver:app --host 127.0.0.1 --port 8780 --no-access-log
```

Expose `/events` through another HTTPS tunnel. This receiver cannot verify the
Standard Webhooks format used by MCP; use the correct script for each path.

The generic dispatcher has a five-second timeout, does not follow redirects,
preserves its ordered destination cursor, and retries with a delay capped at
60 seconds. A slow destination creates a backlog without blocking voice/SSE.
Changing to a new URL starts a new cursor; returning to a previously used URL
resumes that URL's saved cursor.

Unlike user-supplied MCP callbacks, this generic URL is an **operator-trusted
configuration** and is not protected by the public-IP-pinning callback wrapper.
Never expose it as an unrestricted user-editable field. Use a receiver you control.

## Privacy and trust

Events can contain private conversation text. A receiver that accepts them is
another copy of that data, with its own access controls and retention policy.
Use independent signing secrets, not provider keys. Logs should omit payloads.
Transcript text is untrusted conversation data and must not become instructions
authorizing calendar changes, purchases, or other tools.
