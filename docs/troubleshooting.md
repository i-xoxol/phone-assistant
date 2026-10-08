# Troubleshooting

[← README](../README.md) · [Setup](getting-started.md) · [Testing](testing.md)

Start with `python -m scripts.preflight`, `/health`, and identifier-only logs.
Then run the optional network check. Do not paste `.env`, raw SDK debug output,
real phone numbers, or private call transcripts into a public issue.

## It rings, then disconnects immediately

Check the Twilio debugger and the call record's `error_message`, `voice_events`,
and `voice_finalized`. Common causes include inaccessible Live model/key scope,
incorrect public origin, failed WebSocket upgrade, or media format/account binding.

Confirm the tunnel supports WSS and forwards `/twilio/media/{call_id}` without
rewriting it. Verify `PUBLIC_BASE_URL` matches the URL Twilio called. Run
`preflight --network` for the Live handshake; it does not prove Twilio can reach
the media WebSocket. A real short call is the final media-path test.

## Twilio reports completed, but the conversation failed

`completed` means Twilio ended a connected call. It does not prove the voice
agent completed its objective. Read the stored voice error and transcript. The
service records application-owned failure reasons and Twilio 319xx stream codes
without raw provider messages. Expected DTMF handoffs and requested hangups
should not be interpreted as bridge faults.

`playback_stalled` means a full output queue made no acknowledgment progress in
its bounded wait; inspect network/provider playback rather than increasing the
queue without evidence. `unexpected_error` includes safe type/stack locations
for debugging. Original faults are preserved across later cleanup errors.

## Twilio signature validation fails

Use the same account's Auth Token and Account SID. Match the exact public HTTPS
origin and route. Do not configure a tunnel path prefix or redirect. The validator
uses `PUBLIC_BASE_URL`; it intentionally does not trust an arbitrary proxy Host.
Media upgrade validation accepts the supported WSS/HTTPS forms and trailing-slash
variation, but the signed account/call still must match the saved record.

Reference: [Twilio request validation](https://www.twilio.com/docs/usage/security).

## The trial will not call my destination

Check verified destination restrictions, geographic permissions, account balance,
and caller-number Voice capability. Twilio may play a trial announcement before
connecting. The application cannot bypass provider account restrictions.

## Live starts, but keypad tools do not work

Check `LIVE_TOOLS_ENABLED` and the returned `voice_tools_enabled` flag. With tools
off, the supervising client must send digits; ordinary speech saying “two” is
not a keypad tone. With tools on, verify Responses write permission and backend
model access. Check `voice_tool_operations`, not just the originating chat's text.
Run the optional simulated `check_voice_tools --keypad` before a live menu test.

Do not submit competing keypad requests from the chat while the in-service
backend is acting. A menu must be fully heard; uncertain or partial captions are
not proof of the correct option.

## Audio pauses after a keypad press

This is expected for the MVP: Twilio redirect plays the tones and reconnects
the media/Live stream. The same telephone call continues. Prior transcript,
submitted keypad history, and sent/acknowledged updates seed the replacement
session. If reconnection does not occur within the bounded wait, the operation
is recorded unconfirmed and requests hangup. Do not blindly press the key again.

## Pairing code is invalid

Check which kind of code you generated:

- `python -m scripts.web_pairing` → the `/live` browser login.
- `python -m scripts.mcp_pairing` → OAuth approval when connecting an MCP client.

Both expire in 15 minutes and work once. Generate them on the **actual service
host** using its settings/databases. A code from a separate local checkout will
not pair the server. Phone and laptop need separate browser codes. A paired
laptop can create another code with **Pair mobile**.

## Browser login fails or I immediately lose the session

Use the configured **HTTPS** origin. Cookies are Secure, so local HTTP is not
the documented monitor path. Verify that `PUBLIC_BASE_URL` equals the browser
origin; POST login/write requests intentionally reject a different origin.
Private browsing/cookie clearing or seven-day expiry requires pairing again.

## Captions arrive in batches or seem stuck

Check the connection indicator and five-second SSE heartbeats. Disable reverse
proxy buffering, allow long-lived reads, and avoid sleeping/suspending the
server. A background mobile tab may pause; reopening it reconnects and replays
missed text. Transcript generation and network latency still exist even when
the server pushes every delta immediately.

Run `python -m scripts.check_live_page` on the host to test authentication and
public SSE without outputting transcript text. A successful heartbeat proves
transport liveness, not that the caller's speech was transcribed correctly.

## Hangup reports “unconfirmed”

Outgoing audio is sealed locally before the provider request. If Twilio cannot
confirm disconnect, the call may still be connected. Retry direct hangup or end
the call in Twilio Console. The provider time limit is a final bound, not a
reason to leave an unwanted call running. A context instruction is not an
emergency cancel operation.

## MCP connection or OAuth callback is rejected

Enable `MCP_ENABLED`, restart, use the exact `/mcp` URL, and keep its complete
OAuth discovery/approval/token paths reachable. Use OAuth, not the REST token.
The current callback policy allows HTTPS `chatgpt.com` and HTTP local-loopback
callbacks only. Other hosted clients need a deliberate reviewed policy change.
The service is single-owner; registration is not approval.

For Codex, run `codex mcp login phone_assistant` again with a fresh OAuth code.
For ChatGPT, rescan the server after tools change and check installation/workspace
permissions. Native event methods are not ordinary tools, so their absence from
the tool list does not establish that event support is absent.

## An update is acknowledged, but the assistant did not say it

Acknowledgment proves Live accepted the append event, not speech or action.
Choose `context` for facts, `say` for a request to convey, and `instructions` for
behavior redirection. Check the transcript and current ending/handoff state.
Do not reissue uncertain updates with new IDs; reuse the original UUID/content
on a retry. Keep content within 400 UTF-8 bytes and the 20-update call limit.

## Webhook subscription fails or no events reach my chat

The receiver must be reachable at a public HTTPS URL and verify/echo the signed
challenge. Local/private IPs and redirects are blocked for MCP callbacks. Use
the correct `whsec_` Standard Webhooks secret, not the generic HMAC secret or a
provider key. Check `get_call_event_subscriptions` for active state and counters.
No subscription means no delivery. Delivery receipt still does not prove client
processing, and a tool cannot manufacture a callback URL for an arbitrary chat.

Verify separately: discovery → subscription → challenge → actual 2xx delivery →
observed handling. The browser monitor uses SSE and works without these webhooks.

## Restart left an old call in progress

SQLite records persist; the media socket does not. Refresh the REST status with
`GET /calls/{id}?refresh=true` and inspect Twilio. Hang up if still active. There
is no automatic recovery worker or reconnecting the same conversation after a
process restart. Check readiness again before the next call.
