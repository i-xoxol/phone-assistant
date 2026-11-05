# Place calls from the control page

Sign in at `/live` and expand **Place a call**. Select a saved contact or enter a
full international number, such as `+12025550101`. Supply the objective, relevant
context and optional additional limits. Choose the maximum duration and whether
to listen for a phone menu before introducing the assistant.

**Place call** starts one real paid Twilio call, selects it in the existing call
monitor and shows its transcript/recording and Hang up control. It uses the
contact's personality, model and voice overrides, or the current saved defaults.
Presentation mode and caller ID are independent choices.

## Office number or verified mobile caller ID

**Show my caller ID as** offers:

- **Default**: the saved outbound caller-ID choice, initially the office number.
- **Office / Twilio number**: `TWILIO_PHONE_NUMBER`, preserving existing behavior.
- **My mobile**: `OUTBOUND_MOBILE_NUMBER`, or `OWNER_TRANSFER_NUMBER` when the former
  is empty. Complete verification in the same Twilio account under Verified
  Caller IDs first. The service rechecks the exact configured number against
  Twilio's OutgoingCallerIds resource before dialing. A lookup failure or absent
  verification stops the request before any call is created.

The app sends the selected number as Twilio's `From` parameter. Calls still use
Twilio and its normal voice billing; this does not use the mobile's cellular
minutes or make the app receive calls to the mobile. Return calls to the displayed
mobile follow that mobile's normal routing/forwarding. The destination carrier
controls displayed caller name and spam labeling. See the official
[Call resource](https://www.twilio.com/docs/voice/api/call-resource) and
[OutgoingCallerIds resource](https://www.twilio.com/docs/voice/api/outgoing-caller-ids).
Owner-transfer/screening calls continue using the Twilio office number to avoid
ringing the owner's mobile from its own number.

## Reusable brief and defaults

Expand **Reusable call defaults** in the form. **Save form as defaults** saves the
objective, context, additional constraints, duration, phone-menu setting and
caller-ID choice. It never saves the destination/recipient or places a call.
**Use saved defaults** loads that template into the draft for editing. The number
and recipient stay as entered. Defaults are private to the authenticated owner.
Concurrent saves require the current revision.

The caller-ID default also applies to MCP/REST calls that omit `caller_id`.
Saved brief text is a portal template only: it is not secretly injected into
existing agent requests. Existing authorization, recording and disclosure limits
remain enforced by their existing server checks.

## MCP and REST

`place_call` now accepts `caller_id: "default" | "twilio" | "mobile"` and an optional
UUID `request_id`. Existing callers that omit both retain compatibility. Refresh
the MCP client's tool schema if it does not show these new arguments.

```json
{
  "phone_number": "+12025550101",
  "objective": "Confirm the date of my existing appointment; do not change it.",
  "context": "The appointment was arranged last week.",
  "constraints": ["Do not approve charges or schedule a new appointment"],
  "caller_id": "mobile",
  "request_id": "5691f0ce-e7bb-4bd0-a8e8-216c095c8064"
}
```

Bearer-authenticated `POST /calls` accepts the same fields. Owner-only
`GET/PUT /voice/outbound-settings` reads/updates the reusable defaults and configured
caller-ID choices. Browser endpoints are `GET/PUT /live/outbound/settings`,
`POST /live/outbound/calls`, and `GET /live/outbound/requests/{request_id}`. All
browser writes use the existing owner session, exact Origin and CSRF checks on
both public HTTPS and the explicitly configured private overlay portal.

Each new outbound call stores its resolved caller-ID source and number separately
in `call_origination`; call results expose this as `origination`. Existing calls
and incoming routing are unchanged. Changing defaults never changes a queued or
connected call.

## Interrupted submissions

The portal generates a unique request ID per submission. SQLite atomically binds
it to one call and a hash of the brief. Repeating the same ID/brief returns the
original call without redialing; reusing it with changed content is rejected.
There is no automatic retry after an ambiguous Twilio create failure.

If the browser loses the response, **Check submitted call** retrieves that exact
request without dialing. The pending ID survives a page reload in this browser
tab; the brief is not saved to browser storage. Reconcile history/Twilio before
starting another call if creation is unconfirmed. Do not invent a fresh request
ID merely to retry an uncertain attempt.

Most tests use fake Twilio and no real calls. For a manual integration test, use
a consenting recipient/second phone, enter a harmless objective, explicitly
select **My mobile**, and place one call. Check the displayed caller number,
speech/transcript, pinned `origination`, and Hang up. Do not automatically redial
after a timeout; use the recorded request/call ID to reconcile first.
