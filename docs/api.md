# API and tool reference

[← README](../README.md) · [MCP connection](mcp.md) · [Webhooks](webhooks.md)

Examples use fictional numbers and UUIDs. Replace them with an explicitly
authorized destination and IDs returned by your own service. Documentation
examples do not authorize calls or payments.

## Authentication

REST `/calls` routes require `Authorization: Bearer <LOCAL_API_TOKEN>`. Use a
client-side environment variable rather than placing the value in a script or
shell history. MCP uses owner-paired OAuth and the `phone.calls` scope; the REST
token does **not** authenticate MCP. Monitor controls use a paired Secure cookie,
origin verification, and a CSRF header. Twilio routes use provider signatures.

FastAPI serves interactive REST schemas at `/docs`. That page describes the API;
using “Try it out” on call creation makes a real call.

## Create a call

**REST:** `POST /calls` → HTTP 201 with a call result
**MCP:** `place_call`

| Argument | Required | Default / restriction |
|---|---|---|
| `phone_number` | Yes | E.164: `+` and 8–15 digits, first digit nonzero |
| `objective` | Yes | 1–4,000 characters |
| `recipient` | No | Empty, at most 200 characters |
| `context` | No | Empty, at most 8,000 characters |
| `constraints` | No | Empty list, at most 30 items |
| `max_duration_minutes` | No | 15; allowed 1–15 |
| `ivr_mode` | No | `false`; defer the immediate greeting for automated menus |

```json
{
  "phone_number": "+12025550101",
  "recipient": "Example service desk",
  "objective": "Ask when my existing request will be ready. Do not change it.",
  "context": "The request is under Alex.",
  "constraints": ["Do not authorize charges", "Do not provide payment information"],
  "max_duration_minutes": 3,
  "ivr_mode": true
}
```

The returned dictionary contains `call_id`, current `status`, `duration_seconds`,
`live_url`, `voice_tools_enabled`, and update-support metadata. Its state may
already have changed through callbacks; do not assume it always returns dialing.
Twilio creation failure is stored as a failed record and returned; check the
state even if the HTTP request itself succeeded. Missing configuration raises a
service error before dialing.

```bash
# Save the fictional example as examples/call.json and edit its destination first.
curl --fail-with-body -X POST http://127.0.0.1:8000/calls \
  -H "Authorization: Bearer $LOCAL_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/call.json
```

Do not automatically retry call creation after a timeout. Check Twilio and the
persisted record to avoid a duplicate paid call.

## Status

**REST:** `GET /calls/{call_id}`
**MCP:** `get_call_status(call_id)`

REST accepts `?refresh=true` to fetch current Twilio metadata and reconcile state.
The MCP status tool reads persisted state; ordinary signed callbacks keep it
current. Elapsed duration during an active call is computed from its start time.

States:

```text
queued → dialing → ringing → in_progress → completed
                                      ↘ failed
             ↘ busy / no_answer / cancelled
```

Not every call visits every state. `completed`, `failed`, `busy`, `no_answer`,
and `cancelled` are terminal. Callback order is guarded by provider sequence
numbers; terminal states cannot be regressed by a late callback.

## Result and transcript

**REST:** `GET /calls/{call_id}/result`
**MCP:** `get_call_result(call_id)`

Results include the original brief, timestamps, provider IDs, state, duration,
`transcript`, `keypad_events`, `voice_tool_operations`, `call_updates`,
`voice_events`, `error_message`, and `voice_finalized`. MCP also merges current
status/capability fields. Records are available during the call; text can still
arrive during finalization after Twilio reports completion.

An illustrative transcript fragment:

```json
{
  "speaker": "callee",
  "timestamp": 8.7,
  "end_timestamp": 10.1,
  "text": "The request is ready for pickup."
}
```

Fragments may split words, overlap across speakers, or contain recognition errors.
These are model captions, not recordings or certified verbatim evidence. Read
`voice_events` and `error_message` even when status is completed.

The service does **not** return generated `summary`, `outcome`,
`person_spoken_to`, or follow-up analysis. The supervising client can derive a
structured summary from the saved transcript, distinguishing stated facts from
uncertainty. It should not invent facts absent from the conversation.

## Hangup

**REST:** `POST /calls/{call_id}/hangup`
**MCP:** `hangup_call(call_id)`

Stops an active call or cancels a ringing one. Terminal calls are unchanged.
Outgoing speech is blocked and buffered Twilio audio cleared before the
provider disconnect request. A 502 means provider confirmation failed; it does
not mean the call definitely stopped. Inspect Twilio or retry the hangup control.

## Keypad

**REST:** `POST /calls/{call_id}/digits`
**MCP:** `press_digits(call_id, digits, reason)`

```json
{
  "digits": "2",
  "reason": "The fully heard menu says 2 reaches the existing-request desk."
}
```

REST/MCP allow 1–20 characters from `0-9`, `*`, `#`, `w`, `W`. `w` pauses half
a second; `W` pauses one second. The in-service voice function is narrower:
1–4 characters from `0-9`, `*`, `#`, with a nonempty reason up to 500 characters.

A connected bridge is required. Another handoff or ending blocks submission.
Digits redirect the Twilio Call and briefly restart the voice stream. A successful
result includes `keypad_status=submitted_and_reconnected`. This confirms the
transport action and reconnection, not menu acceptance. The next prompt is the
evidence. See [architecture](architecture.md#why-keypad-presses-reconnect-the-stream).

## Update the conversation

**REST:** `POST /calls/{call_id}/updates`
**MCP:** `send_call_update(call_id, content, mode, update_id)`

```json
{
  "content": "New availability: Thursday 2–4 PM America/New_York. The earlier morning slot is unavailable.",
  "mode": "context",
  "update_id": "1d2f5b98-c97f-4fb0-86dd-345b19f14cab"
}
```

| Mode | Use |
|---|---|
| `context` (default) | Verified facts or availability |
| `say` | A short message for the assistant to convey aloud |
| `instructions` | Adjust conversation behavior within existing permissions |

Content is stripped and must fit **1–400 UTF-8 bytes**; non-ASCII text reaches
the byte limit earlier than 400 characters. There are at most 20 updates per call.
Provide a fresh UUID for a new update and reuse that UUID/content/mode on a retry.
If omitted, the service creates an ID; save it before retrying.

Delivery states: `queued`, `sent`, `acknowledged`, `rejected`, `unconfirmed`.
Updates can queue during dialing and keypad reconnect. Acknowledged confirms
context injection, not speech or action. A new ID can cause a duplicate request;
do not use one merely to retry an uncertain delivery. Read the result and verify
the transcript. An update cannot undo call ending or expand spending authority.

## Event tools

| MCP tool | Inputs | Result |
|---|---|---|
| `subscribe_call_events` | UUID `call_id`, HTTPS `callback_url`, `signing_secret`, `event_name`, `ttl_minutes` | Verified subscription metadata |
| `get_call_event_subscriptions` | UUID `call_id` | This OAuth connection's subscriptions and acknowledged delivery counts |
| `unsubscribe_call_events` | String `subscription_id` | Stops the owned subscription; repeat-safe |

`event_name` is `transcript.delta` (default) or `call.status`; TTL is 1–1,440
minutes, default 60. The signing secret must be `whsec_` followed by base64 for
24–64 random bytes. Subscribe once per desired event type.

Native MCP Events also provides `events/list`, `events/subscribe`, and
`events/unsubscribe` protocol methods. These are not ordinary tools. See
[webhooks](webhooks.md) for schemas, signatures, verification, and receivers.

## Browser routes

The browser bootstraps with `GET /live/bootstrap`, then listens to
`GET /live/events`. Writes use the paired cookie, configured origin, and
`X-CSRF-Token` from the paired page. The dedicated hangup route is
`POST /live/calls/{call_id}/hangup`. Browser pairing and signout are covered in
[the monitor guide](live-monitor.md).

## Common errors

| HTTP status | Meaning |
|---|---|
| 401 | Missing REST token or expired/unpaired browser session |
| 403 | Invalid Twilio signature/account binding, origin, or CSRF token |
| 404 | Unknown call UUID |
| 409 | Current call state cannot accept digits/update, or conflicting update ID |
| 422 | Invalid input schema, UUID, number, digits, or duration |
| 502 | Provider refresh/hangup/keypad outcome could not be confirmed |
| 503 | Missing setup or required configuration |

MCP returns SDK tool/protocol errors rather than the REST HTTP vocabulary. Treat
uncertain side effects carefully and read the persisted operation history.
