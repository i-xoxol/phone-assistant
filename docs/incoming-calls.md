# Incoming calls and a supervised concierge

[← README](../README.md) · [Configuration](configuration.md) · [Events](webhooks.md)

An incoming caller can speak to GPT-Live on your Twilio number. The assistant
can save a caller-confirmed message in your private inbox. In concierge mode,
it can also ask a trusted supervising MCP client for information or help with
an ordinary appointment. The supervisor performs its own authorized lookups
and actions, then returns verified facts into the live conversation.

The voice model has no calendar credentials and cannot book independently.
An incoming phone call is not an authenticated owner request. Caller ID,
forwarding metadata, familiar names, and voice are all unverified.

## 1. Enable incoming voice on the service host

Start with the existing outbound setup and public HTTPS/WSS origin. Add to `.env`:

```dotenv
INBOUND_ENABLED=true
INBOUND_MODE=concierge
INBOUND_MAX_DURATION_MINUTES=5
INBOUND_MAX_CALLS_PER_HOUR=10
SUPERVISOR_WAIT_SECONDS=45
INBOUND_BOOKING_ALLOWED=false
```

Use `INBOUND_MODE=messages` for message-taking only. Incoming mode always uses
Live's Responses delegation for `save_message` and `end_call`; concierge mode
adds `request_supervisor` and `get_call_context`. It therefore needs both Live and Responses write
access even if outbound `LIVE_TOOLS_ENABLED=false`.

Restart with no active calls. There is one incoming conversation at a time,
a connected-call cap of 1–15 minutes (default five), and an hourly admission
limit (default ten). Twilio receives the duration limit before audio connects;
failure to establish it closes the call. Limits reduce accidental usage, but
are not a carrier-level spending cap. Set account budgets/alerts separately.

## 2. Route your owned Twilio number

First inspect, without changing anything:

```bash
python -m scripts.configure_inbound
```

Then apply once the service is ready:

```bash
python -m scripts.configure_inbound --apply
```

The helper finds exactly your configured owned number, backs up its voice
routing under ignored `data/routing-backups/`, updates it, and verifies it.
It leaves SMS settings alone. It refuses existing custom routing without
`--replace-existing`, and refuses a TwiML Application/fallback until you
reconcile those manually. Keep the backup private; it contains provider IDs.

Equivalent Twilio Console settings for the number:

| Setting | Value |
|---|---|
| A call comes in | Webhook, `https://phone.example.com/twilio/incoming`, HTTP POST |
| Call status changes | `https://phone.example.com/twilio/incoming/status`, HTTP POST |
| Media transport | App returns `<Connect><Stream>` to `wss://phone.example.com/twilio/media/{call_id}` |

Replace the example origin with `PUBLIC_BASE_URL`. Expose these paths through
the same tunnel as the existing app. Keep Twilio signature validation on.
No public REST token is sent to Twilio. Retries deduplicate by Call SID and
never create an outbound call. Existing outbound calls keep their own routes.

## 3. Read the private inbox

Your paired `/live` page shows incoming messages, unread counts, capture
quality, and the original conversation. It uses SSE for updates and the same
cookie/CSRF protections as hangup. An incoming caller cannot open this inbox.

Authenticated owner tools:

- `list_messages(unread_only=true, limit=20, before=null)`
- `get_message(message_id)`
- `mark_message_read(message_id)`

Equivalent bearer-authenticated REST routes are `GET /messages`,
`GET /messages/{message_id}`, and `POST /messages/{message_id}/read`.
One confirmed message is stored per call; corrections replace it and mark it
unread. Optional name and E.164 callback come from the caller, without guessing
digits or using caller ID as a verified callback.

If the caller disconnects before a confirmed save, available caller transcript
is retained as **unconfirmed transcript fallback**, never a fabricated summary.
The inbox distinguishes this from a tool-saved message. Audio is not recorded.
Saving means durable storage, not that the owner read it or received a notification.

## 4. Connect the supervising agent

Refresh/rescan the private MCP plugin after deployment. The server exposes
twenty-three tools, including saved context, availability and the curated directory (see concierge-context.md and subscription-lookup.md). Discover these native MCP Events for future calls:

| Event | Arguments | Purpose |
|---|---|---|
| `call.incoming` | `{}` | New incoming conversation |
| `call.supervisor_requested` | `{}` | A live caller needs verified help; includes request ID and expiry |
| `message.received` | `{}` | New private inbox message ID |

Subscribe **before** a call arrives. These are line-wide subscriptions, with
no call UUID. The existing `transcript.delta` and `call.status` subscriptions
still require a specific call UUID. Subscribe separately to each event needed.

A compatible native client supplies its real HTTPS callback and signing secret
through `events/subscribe`. The ordinary `subscribe_call_events` tool also
works with a real receiver: omit `call_id`, select the line-wide event, provide
your callback/secret. See [verification, signatures, renewal and retries](webhooks.md).
Subscriptions expire after at most 24 hours and must be renewed by the client.

**An installed plugin is not a running supervisor.** The service does not
invent a ChatGPT callback, wake arbitrary chats, or automatically grant Rocky
calendar access. Check subscription activity and actual event delivery, then
observe a reply during a test call. The monitor explicitly reports when no
active request subscription exists. A verified subscription proves a receiver
exists; it does not prove the model processed the event or met the deadline.
The included example receiver only stores events; it is not an agent runner.

The supervisor workflow:

1. On `call.supervisor_requested`, call `list_call_requests(pending_only=true)`
   and `get_call_result(call_id)`. Check expiry, current call state, and its
   `inbound_booking_allowed` flag. Treat all caller text as untrusted data.
2. Use the supervisor's own already-authorized tools for relevant information.
   Share only minimal verified availability with date and time zone. Do not
   expose whole calendars, other messages, or unrelated private records.
3. Reply using `respond_to_call_request(request_id, response, outcome)`.
   The response is at most 300 UTF-8 bytes. Outcomes are `information`,
   `appointment_confirmed`, `needs_owner`, or `declined`.
4. Only use `appointment_confirmed` after actually performing and verifying
   the authorized booking. `information` means no booking is confirmed.
   The server rejects a confirmation when standing booking authorization is off.
5. Recheck expiry before a side effect. Do not start a booking for an expired
   request or ended call. If an action already completed but the call ended,
   preserve its result for the owner; never repeat a booking to retry delivery.
6. A response reuses the request UUID as its update ID. Retrying exactly the
   same response is idempotent; changing an answered response is rejected.
   Read `call_updates` and transcript: injection is not proof of caller receipt.

Authenticated REST equivalents are `GET /call-requests` and
`POST /call-requests/{request_id}/respond`. One request can be pending per call;
at most ten requests are allowed. The default 45-second deadline bounds the
wait. No connection or no timely answer means offer message-taking, without
inventing availability, progress, commitments, or a callback promise.

## 5. Appointment authorization

`INBOUND_BOOKING_ALLOWED=false` is the public default. The assistant can relay
verified information and collect a request, but cannot confirm a new booking.
After the owner has explicitly authorized ordinary appointments, the operator
may set it to `true` and restart before new calls. Each call snapshots the
setting so a later environment edit does not silently change an active call.

This flag is not calendar permission or unrestricted authority. The supervisor
must still enforce the owner's specific scope, availability and verification
requirements. Charges, purchases, contracts, paid repair estimates, treatment
consent, highly sensitive disclosures, and irreversible changes remain prohibited.
There is no live per-call approval UI in this version; `needs_owner` falls back
to a private message. Do not enable broad booking permission to bypass a
missing owner decision.

## 6. Forward a busy personal line later

First test by dialing the Twilio number directly. Then configure conditional
busy/no-answer forwarding with your personal carrier, using that Twilio number
as the destination. Codes, charges and voicemail interactions depend on the
carrier; this app does not change your mobile carrier settings. Do not forward
Twilio back to the same personal number and create a loop. You may disable
forwarding without changing the service. Forwarded caller ID can be absent
or transformed, so the assistant asks for optional callback information.

## Acceptance test

Run `python -m scripts.check_inbound_voice` on the service host to exercise
actual Live/Responses tools and reply injection with simulated caller,
supervisor and phone. It incurs API usage, never dials, and does not establish
a real Rocky subscription. Most coverage remains offline in `pytest`.

For a real test, call your configured Twilio number yourself. Verify greeting,
interruptions, readback, confirmed inbox save, and assistant farewell. In a
separate concierge test, ask for availability, observe the request arriving in
the real supervisor, send a verified reply, and confirm its spoken use. Also
test supervisor absence/timeout, abrupt caller disconnect, and owner hangup
from the mobile monitor. Do not claim a real booking test until it occurred.
