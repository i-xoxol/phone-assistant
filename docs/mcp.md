# Connect an MCP client

[← README](../README.md) · [API reference](api.md) · [Webhooks](webhooks.md)

The server uses the official MCP Python SDK and **Streamable HTTP** at `/mcp`.
It shares the FastAPI process and call manager. There is no separate stdio
process in this release.

The server currently exposes 29 owner tools. `get_contact_call_history` reads
[number-matched earlier calls](call-memory.md). `get_operating_hours` and
`set_operating_hours` manage [office and mobile-reach schedules](operating-hours.md).
Voice selection and screened owner
contact are documented in [voice and owner contact](voice-and-owner-contact.md).
The four curated-directory tools
are documented in [subscription lookup](subscription-lookup.md). Enabling that
worker adds a voice-internal reader, not a public agent execution tool.

## Enable and pair

1. Complete the first-call setup and verify the public HTTPS origin.
2. Set `MCP_ENABLED=true` in `.env`.
3. Restart with no active calls. Continue using one worker.
4. Generate a fresh OAuth pairing code **on the service host**:

```bash
python -m scripts.mcp_pairing
```

The code works once and expires after 15 minutes. It is for MCP authorization,
not the transcript browser. Generate a separate code for each client connection.

The OAuth provider supports dynamic client registration, PKCE, `phone.calls`,
resource-bound access tokens, one-hour access tokens, and rotating 90-day refresh
tokens. The owner approval page requires the local code before issuing a grant.
Provider keys and the REST bearer token are never needed by the MCP client.

This provider is deliberately **single-owner**. Every approved client can access
the owner's calls and invoke the granted tools. It is not a tenant account system
or a fine-grained per-call authorization service. Keep connected instances private.

## Codex CLI

Use your own public URL:

```bash
codex mcp add phone_assistant --url https://phone.example.com/mcp
codex mcp login phone_assistant
```

The login flow opens the service's approval page. Enter a freshly generated
code, confirm the callback host and requested capabilities, and finish login.
Check the tool inventory in Codex. Read-only status/result operations are a
useful connection test before any call request.

Use the same server from the desktop app's MCP settings if your installed app
offers remote HTTP servers. Configuration labels may change; the endpoint and
OAuth flow remain the same. [Official MCP guidance](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)

## ChatGPT

Follow the current [custom MCP connection guide](https://developers.openai.com/api/docs/guides/custom-mcp-server).
As documented for this release:

1. Open [ChatGPT Plugins](https://chatgpt.com/plugins) on the web.
2. Use the add button and **Add custom MCP server**.
3. Give it a name such as **Phone Assistant**, enter your
   `https://phone.example.com/mcp` URL, and choose **OAuth** authentication.
4. Create the plugin, install it, and connect through its owner approval page.
5. Enter the service-host OAuth code. Enable the installed plugin in the chat.
6. Refresh/rescan its tools after changing server code or tool definitions.

Available options depend on your account, workspace policy, and current product
interface. Keep this connected plugin private to your account. Publishing the
source repository does not publish your instance or make it a reviewed public
ChatGPT marketplace app.

A compatible ChatGPT agent or dot can use the installed plugin where enabled.
Do not assume it has a callback endpoint or native event subscription until the
subscription and delivery are verified. [Event setup →](webhooks.md)

## Other clients and protocol versions

The release supports MCP protocol `2026-07-28` and the SDK's retained legacy
initialization path, tested against `2025-11-25`. Ordinary tools do not require
native MCP Events support. A client that implements only tools can use the three
subscription tools with an actual HTTPS receiver.

The OAuth redirect policy currently allows **HTTPS `chatgpt.com`** callbacks and
**HTTP `localhost`/`127.0.0.1`** callbacks for local clients. An otherwise compatible
hosted client with a different callback origin will be rejected. Do not disable
the check indiscriminately; deliberately add and test only the origin you intend
to trust in `PairingOAuthProvider.authorize` and the approval page CSP.

The transport also validates the configured public host plus loopback/test hosts.
If you expose another origin, update `PUBLIC_BASE_URL` and restart rather than
allowing arbitrary Host headers.

## Tool inventory

| Tool | Behavior |
|---|---|
| `place_call` | One real paid outbound call; returns while the conversation continues |
| `get_call_status` | Persisted state, elapsed duration, monitor URL, capability flags |
| `get_call_result` | Saved brief, transcript, updates, keypad/tool operations, diagnostics |
| `hangup_call` | Disconnects active or cancels ringing call |
| `press_digits` | Sends real menu navigation tones and reconnects voice |
| `send_call_update` | Injects concise facts/requests into the current conversation |
| `subscribe_call_events` | Verifies a signed per-call or line-wide webhook subscription |
| `get_call_event_subscriptions` | Checks the current OAuth connection's subscriptions/delivery counts |
| `unsubscribe_call_events` | Stops an owned subscription |
| `list_messages` | Lists private incoming messages with unread count and pagination |
| `get_message` | Reads one caller message and its capture quality |
| `mark_message_read` | Marks one message read; safe to repeat |
| `list_call_requests` | Lists pending, unexpired concierge requests across incoming calls |
| `respond_to_call_request` | Returns verified information or an authorized booking result into the live call |
| `get_inbound_context` | Reads phone-safe role/facts, availability freshness and anticipated-call notes |
| `set_caller_language` | Sets a number's language preference without changing identity; English is `en` |
| `set_inbound_context` | Updates the default role and phone-safe facts |
| `set_inbound_availability` | Replaces verified free windows with observation time and expiry |
| `expect_incoming_call` | Adds/updates an expiring phone-safe anticipated-call note |
| `retire_expected_call` | Stops exposing an anticipated-call note |

Tool annotations identify read-only versus side-effecting actions. Client
confirmation settings remain important: authentication proves client access,
not user authorization for an arbitrary number or transaction.

## Give the supervising agent a useful brief

Example user instruction:

> Call the example service desk at my explicitly supplied number. Ask whether
> my existing request is ready and what pickup times are available. Do not
> change the request, approve charges, or give payment information. Use a
> three-minute limit and send me the live monitor link.

The client should inspect `voice_tools_enabled` immediately. If true, Live's
in-service backend handles navigation; do not submit competing chat-side digits.
If false, the supervising client may inspect the completed menu text and use
`press_digits` one choice at a time.

For a live availability update, call `send_call_update` with concise, verified
dates/times and a time zone. Do not send a full calendar or claim the voice model
accessed it. Read the update acknowledgment, then verify its actual use in the
transcript.

Read results carefully: a completed telephone call is not necessarily a
successful objective. Summaries should state only confirmed facts and flag
uncertainty, voice errors, or decisions still requiring the owner.

## Reusable plugin template

The disabled-by-default [appointment tools](appointments.md) use schema version
`1.0` and separate `phone.appointments.read`, `phone.appointments.book` and
`phone.appointments.manage` grants. Existing `phone.calls` tokens and native event
subscriptions do not acquire these permissions automatically. Private Realtime
`get_owner_schedule` is an in-process owner-only capability, not an incoming-caller
or generic MCP calendar search tool.

### Approve the added appointment permissions

Refresh/rescan the existing Phone Assistant plugin's tools after deploying this
version. Ask your supervising agent: **"Run get_appointment_permissions so I can
approve the new appointment tools. Do not create any appointment."** This setup
tool only verifies authorization. When permission is missing, it returns the
documented OAuth challenge so ChatGPT can show its Connect/Authorize action.
Follow that action on the **existing connection**, enter a fresh service-host
MCP pairing code (`python -m scripts.mcp_pairing`), and approve the appointment
read/book/manage scopes shown on the service's approval page. The code is
single-use and expires after 15 minutes. A transcript-portal code will not work.

Do not delete the plugin or recreate event subscriptions. The OAuth client
identity is reused; existing `phone.calls` grants continue working until and
after reauthorization. Refreshing an old token alone cannot add permissions.
Ordinary appointment tools request only their own scope, retaining any
appointment scopes already granted. No grant is expanded automatically.
The owner-only in-process coaching calendar reader needs no extra MCP consent
and reuses the already configured Google login.

See the [official OAuth UI contract](https://developers.openai.com/plugins/build/auth#triggering-authentication-ui).

`plugin/` contains a generic manifest, server configuration, and a supervising
agent skill. The committed URL is **`https://phone.example.com/mcp`**, an
intentional placeholder. Customize a local copy for your own service before
importing it. Do not commit an authenticated instance's real endpoint, tokens,
or codes into a public fork.

```bash
python -m scripts.package
```

This creates ignored source and plugin archives under `data/`. It does not
publish a plugin, configure your ChatGPT account, or authorize any phone call.
The public repository includes the template; your connected private plugin
still requires your own OAuth pairing.
