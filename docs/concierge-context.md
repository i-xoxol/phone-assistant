# Saved context and automatic availability

[← README](../README.md) · [Incoming calls](incoming-calls.md) · [MCP](mcp.md)

The incoming voice assistant can answer from an owner-maintained local cache
without waiting for a supervising chat. Rocky or another authenticated agent
can update its role, phone-safe facts, verified free windows and anticipated-call
notes. These persist in SQLite across calls and restarts. They are available in
concierge mode only; outbound briefs and message-only calls remain separate.

Greeting and tone preferences in the saved profile override the sample opening
phrases. A recognized owner receives a short casual greeting without a repeated
name/assistant/office introduction, including the silent-start fallback. Recognition
requires the configured saved-number trust policy, one contact matching the
presented caller number and a match to the configured owner mobile. A forwarding
destination or someone merely saying the owner's name does not select this greeting.
This changes tone only; recording notices, honesty and action limits still apply.
`python -m scripts.check_owner_greeting` verifies the real voice model with a
silent synthetic owner, both with and without the fallback (`--language uk` for
Ukrainian). It uses the configured OpenAI API and incurs brief voice usage, but
never creates a Twilio call or changes the real directory/context.

The live backend has a read-only `get_call_context` function. It refreshes from
SQLite before offering availability. Context changes notify active incoming
sessions to fetch again, using the existing Live update channel. No voice-session
restart is needed. When required information is missing or expired, it requests
supervisor help, asks useful clarifying questions while waiting, and takes a
message if help is unavailable. It must not fabricate progress or repeat filler.

`get_call_context(question)` also retrieves up to six current, owner-approved
knowledge facts for a caller's actual topic. This local read does not invoke
Luna. A blank question reads general context. Exact-number expected-call notes
are prioritized, followed by topic relevance, with three notes returned. For
teaching/advising workflows, see the [student-call strategy](caller-memory-and-student-context.md).

## Owner tools

| Tool | Purpose |
|---|---|
| `get_inbound_context` | Read saved role/facts, availability freshness and active anticipated-call notes |
| `set_inbound_context(role, context)` | Replace the default role and phone-safe facts |
| `set_inbound_availability(slots, time_zone, observed_at, expires_at, source)` | Replace verified free windows with an explicit freshness limit |
| `expect_incoming_call(title, context, expires_at, phone_number, context_id)` | Add/update an expiring phone-safe note for an anticipated call |
| `retire_expected_call(context_id)` | Stop exposing a note, retaining its private record |

Equivalent bearer-authenticated routes:

- `GET /concierge/context`
- `PUT /concierge/context`, body `{ "role": "...", "context": "..." }`
- `PUT /concierge/availability`, same fields as the availability tool
- `PUT /concierge/expected-calls`, same fields as the anticipated-call tool
- `POST /concierge/expected-calls/{context_id}/retire`

These are owner endpoints. A caller can only read the filtered phone-safe
snapshot through the live backend; it has no context-writing tools. General
context is at most 2,000 characters; role is at most 200. Fixed policies about
honesty, unverified callers, payments/contracts and booking verification remain
above the configurable role. Context cannot grant a caller new authority.

Only store information the owner approves for disclosure to unverified incoming
callers. Do not put event titles, private meeting descriptions, full calendars,
passwords, access tokens or unrelated personal records in this cache.

## Availability and later hours for friends

Each slot is a free **window**, `{ "start": "<offset-aware ISO timestamp>",
"end": "<offset-aware ISO timestamp>" }`. Send up to forty non-overlapping
windows with a real IANA `time_zone`, such as `America/New_York`. They must be
verified, not guessed from incomplete calendar results. Observation and expiry
times require explicit offsets. Expiry must be in the future and no more than
one hour after observation; ten minutes is the recommended automatic TTL.
Older observations cannot overwrite a newer snapshot. An empty list clears
offers. Expired data has `fresh=false` and no slots in live context reads.

The live assistant can offer fresh listed windows without Rocky. **Offering is
not reserving.** A chosen slot still requires Rocky to recheck availability,
complete the authorized booking and return `appointment_confirmed`. The cache
does not hold slots or prevent a calendar changing during a conversation.

Routine hours are an offer policy, not a complete definition of the owner's
availability. A caller asking for an evening, weekend, different duration or
travel buffer can be referred to Rocky. Friends may have later hours when the
owner/supervisor approves them. A claim of friendship is not authentication or
permission to expose other schedule data.

For example, tell Rocky:

> Set the incoming role to my personal concierge. Use fresh routine free
> windows for ordinary scheduling. For friends who need later hours, ask me or
> use an explicitly approved anticipated-call note. Do not confirm a booking
> until you have actually made and verified it.

Anticipated-call notes can supply a specific approved evening option without
changing the general routine-hours cache. Keep the note accurate and retire
it when no longer needed. It still does not prove a calendar reservation.

## Anticipated calls

Tell Rocky to expect a particular office or caller and provide just the relevant
phone-safe facts. An optional E.164 `phone_number` filters the note by incoming
caller ID; a blank number applies to all incoming callers. Caller ID is a routing
hint, **never identity verification**. It can be withheld, transformed or spoofed.
Only disclose information safe even if that hint is wrong.

Notes must expire within seven days; at most twenty may be active. The live
snapshot includes up to three matching notes, newest first. Reuse a `context_id`
when updating or retrying a note; read current IDs with `get_inbound_context`.
Retiring is idempotent. No note can authorize spending, medical consent,
contracts or another prohibited action.

## Optional automatic Google availability refresh

The included script uses an existing Google authorized-user token file, deriving
free windows from event times and transparency only. It requests no event titles,
descriptions, attendees or locations, and makes no calendar writes. It expands
recurring events, follows bounded pagination, respects all-day events and DST,
unions busy intervals across explicitly selected calendars, and fails closed
on incomplete/error responses.

The existing `calendar.events` grant can read event times through
[Google's Events list API](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).
This avoids requesting a different FreeBusy scope solely for this feature.
The credential file must be owner-only and not a symlink on Linux. Expired
access credentials are refreshed in memory against Google's fixed token endpoint;
the shared file is never rewritten. This does not bypass revoked consent or MFA.
The script needs no additional Python packages beyond `requirements.txt`.

First inspect counts without changing the phone cache:

```bash
python -m scripts.sync_google_availability \
  --token-file "$HOME/private/google_token.json" \
  --calendar primary --time-zone America/New_York --days 14 --dry-run
```

After checking the selected calendar and offer policy, omit `--dry-run` to
update the authenticated phone service over its configured HTTPS origin.
The script uses the service's local `.env`; it does not expose its bearer token
to the model or put credentials in command arguments.

Defaults: weekdays, 9 AM–5 PM, next fourteen days, at least thirty minutes per
free window and thirty minutes' lead time. Override `--start-hour`, `--end-hour`,
`--weekdays` (Monday=0), `--days`, `--minimum-minutes` and `--lead-minutes` to match
your own policy. Multiple `--calendar` arguments union busy times. Only those
calendars are checked; do not claim availability covers unchecked calendars.
At most the earliest forty windows are exported; absence of a listed window
does not establish the owner is busy. Friends' later options can remain a
supervisor decision rather than globally widening routine hours.

If a calendar mirrors another source, its own freshness matters. The optional
`--upstream-health-url` reads a trusted operator-configured status endpoint and
requires `state` healthy/syncing plus `lastSuccess.timestamp` within fifteen
minutes. A stale mirror blocks refresh; the old phone cache expires naturally.
Do not use this option with arbitrary caller-supplied URLs. A failed/unconfirmed
update must not be reported as fresh availability.

To refresh every five minutes on a Linux host, customize the paths and hours in
`deploy/phone-availability.service`, then install only these two new user units:

```bash
install -m 600 deploy/phone-availability.service deploy/phone-availability.timer \
  "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now phone-availability.timer
systemctl --user start phone-availability.service
systemctl --user status phone-availability.service
```

This is an optional bounded systemd job, with a two-minute timeout. It does not
replace an existing calendar bridge or change its timer. Do not point the example
unit at another person's credentials. Keep actual unit paths and credentials
private; the committed template contains neither.

## Validation

Offline tests cover owner-only writes, persisted context, caller filtering,
expiry, old-snapshot rejection, IANA zones, offset-aware intervals, live refresh
notifications, MCP tools, overlap/working-hour calculations, DST/all-day events,
pagination, title-free fields, in-memory token refresh and stale upstreams.
The optional `scripts.check_inbound_voice` probe exercises the actual Live
context reader with a synthetic profile, alongside simulated supervisor/message
tools. A real incoming call and actual Rocky subscription still need their
separate end-to-end test.
