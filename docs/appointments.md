# Appointments and private calendar coaching · v1

[README](../README.md) · [Verified inventory](appointments-development.md) · [Release/recovery](appointments-runbook.md) · [Evidence](appointments-review.md)

This upgrade is **disabled by default**. It adds exact-slot checking, one ordinary
appointment booking per call, minimal repeat-caller confirmation, bounded private
status notices, and read-only calendar access for private Realtime coaching. It does
not authorize a deployment, new grant, real appointment or test call.

## Data flow

```mermaid
flowchart TD
  V[Incoming GPT-Live / delegated backend] --> T[Call-bound appointment tools]
  M[Authorized MCP supervisor] --> T
  T --> P[Consent + academic hours + deadline + privacy policy]
  P --> J[Private SQLite journal / registry / outbox]
  P --> A[CalendarProviderAdapter v1]
  A --> G[Google primary: exact read / one private create / readback]
  B[Existing Outlook to Google mirror: every 5 minutes] --> G
  B --> H[Existing read-only health / coverage metadata]
  A --> H
  R[Private Realtime 2.1 coaching] --> O[Owner-only schedule read · no booking]
  O --> A
  O --> E[Owner earpiece only]
```

Only the Google transport understands Google dictionaries, token files or HTTP
endpoints. Core policy, private registry and orchestration use canonical immutable
types. Calendar role/destination is configured independently of provider/account
identity. The second fake provider deliberately uses different opaque IDs and
revisions; the same contract tests exercise both providers.

Google primary remains the first write destination and existing aggregate. No
direct Graph/SHU lookup, mail lookup, second calendar, invitation, cancellation or
rescheduling API is added. The installed mirror continues unchanged. Native
phone-booked events use `phoneAgentOwner=phone-assistant`, never the mirror's
`bridgeName=shu-outlook` ownership tag.

## Tool schemas and grants

Schemas are version **1.0**, defined in `app/appointments/schemas.py`. Unknown
arguments are rejected. Dates require offsets and an IANA timezone; naive dates
are rejected. Core local-time conversion rejects DST gaps and requires explicit
fold selection for ambiguous times.

| Tool | Main input | Result / authority |
| --- | --- | --- |
| `find_first_available_slot` | Live call; first/last name; purpose; duration in minutes; arrangement; optional caller-stated date bounds | Earliest verified minute-aligned slot within the next 14 days, followed by the ordinary exact check and one readback; no reservation |
| `check_appointment_slot` | Live call; optional pending request UUID; first/last name; purpose/course; start/end/time_zone; arrangement | Exact decision, check/draft/request IDs, TTL, policy version, completeness, no reservation |
| `book_appointment` | Call-bound draft/check IDs and stable idempotency key | Revisioned state; confirmed only after provider readback and private registry commit |
| `get_booking_status` | Call and operation IDs | Read/reconcile original event; never creates an event |
| `get_my_appointment` | Call and caller's stated first/last name | Minimal date/time/zone/duration/arrangement; no candidate list |
| `report_call_request_progress` | Existing native request; received/checking/conflict; conflict check ID when needed | Nonterminal progress; a verified conflict can be injected promptly |
| `set_caller_relationship` | Existing curated contact ID; wife/mother/friend/none | Owner-authorized relationship, no new contact or widened booking authority |
| `set_private_call_notice` | Existing contact/appointment audience; approved category/value; effective/expiry; optional existing notice ID | Revisioned private owner statement, maximum seven-day lifetime |

MCP tools require `phone.calls` plus the applicable **additional explicit grant**:
`phone.appointments.read`, `phone.appointments.book` or
`phone.appointments.manage`. Existing tokens/default scopes do not acquire these.
Management permits only owner-provided status and classification of existing
contacts. It is unavailable to caller tools. OAuth explains the requested expansion
before approval; this branch changes no existing grant.

Incoming voice additionally has `get_caller_status` and
`get_family_availability`; the backend derives the current caller and cannot accept
model-selected contact identities. MCP inputs cannot select arbitrary calendars,
SQL, event IDs for disclosure, credentials, or an `authorized=true` bypass.

Example slot request (synthetic):

```json
{
  "first_name": "Casey", "last_name": "Example", "purpose": "course guidance",
  "start": "2026-10-26T10:00:00-04:00", "end": "2026-10-26T10:15:00-04:00",
  "time_zone": "America/New_York", "arrangement": "Office A"
}
```

MCP wraps this as `{"call_id":"<live call UUID>","request":{...}}`. The voice
runtime binds the call internally. Reuse the returned request ID for corrections;
then use the returned draft/check IDs and a stable key for booking. Never replay
this historical example as a real appointment.

For “first available,” “earliest” or “any time,” collect only missing name,
purpose, duration and arrangement; do not demand an exact date/time. The bounded
search requires complete fresh coverage of its entire window, including every
page of conflicts, and the existing academic-hours and Office Hours occurrence
policy. It uses office time and honors optional caller-stated bounds within the
next fourteen days. Incomplete coverage cannot establish the earliest slot.
No result means no slot was verified in that window, not that all future dates
are unavailable. The caller must hear and agree to the concrete proposal before
booking; a flexible timing preference alone never authorizes a write. This tool
uses the existing appointment read scope and preserves the request deadline.

## Calendar and consent rules

Academic meetings must fit entirely within Monday/Thursday **10–12**, or Friday
**14–16 America/New_York**, and a current owner-designated Office Hours occurrence.
An event title cannot designate availability. Canceled/moved occurrences, actual
appointments inside an availability block, all-day commitments and configured
buffers are respected. Friends' later free time never expands booking hours.
Phone operation/reach hours are independent and unchanged.

The exporter cache has a 30-minute minimum and first-40 truncation; absence cannot
prove busy. This path queries the exact interval directly, including 15-minute
requests. One verified conflict can return early. Available requires complete,
fresh Google coverage **and** verified recent complete upstream-mirror coverage.
Unhealthy/stale/partial/out-of-window/unattested mirror state returns unknown.

The mirror still runs every five minutes with jitter. A work event created after
the last sync can be missing even while health is green. Immediate Google
pre-commit/post-write checks narrow other races but **cannot eliminate mirror lag
or lock external calendar editors**. This is explicit in internal results.

Booking requires a live incoming concierge call with standing ordinary-booking
authority, an unexpired server request, full agreed payload, complete observed
assistant readback, then unambiguous fresh caller assent. Model assertions are
not consent evidence. Transcript/audio activity detects corrections, withdrawal
and interruptions during I/O. Only affirmative assent after readback works; other
utterances fail closed. An existing native request retains its actual deadline
(the inspected service uses 90 seconds), rather than assuming 45 seconds.

Reads are budgeted to six seconds, the single create to five, readback/reconciliation
to six; live work is additionally bounded by the actual request deadline. The model
can continue listening while the delegated operation runs. No operation starts
after call end/expiry. A write already sent is reconciled privately even after
hangup. If time runs out, save an **unconfirmed request**, never promise a booking.
The provider invokes a server-owned authorization guard again immediately before
POST dispatch, after credential preparation, so a hangup during token refresh cannot
start a later write. An in-flight network write remains an uncertain operation to reconcile.

## Durable state and recovery

```text
details + exact check + spoken readback + fresh assent
  → durable intent → checking → committing (one deterministic create)
  → independent source readback → external-conflict check
  → private registry + durable result outbox → confirmed
```

Failures return conflict, needs_owner or uncertain. Needs-details happens before
write. Uncertain status reads the stored provider-qualified deterministic event
identity; it never repeats creation. Registry failure retains the event and
repairs its original record. No automatic delete/compensation occurs. One write
per live call is an intentionally conservative MVP limit.

Canonical identity contains provider/tenant/calendar/event/opaque occurrence;
source revision and verified mirror aliases are separate. Core never parses
vendor IDs. Google supports deterministic identity, exact reads, recurrence
expansion, private events and invitation suppression. It does **not** provide an
atomic free/busy-and-create transaction. No attendees or conference creation are
sent; `sendUpdates=none` alone is not treated as a no-mail guarantee.

Operation state and caller delivery are independent. Injection accepted, complete
spoken confirmation and actual caller receipt are separately observed/persisted.
A delivery error or ended call does not undo a verified booking or permit another.
Progress never answers/fulfills the existing native supervisor request.

## Repeat callers, indexing and private status

The private registry is separate from general context/facts. Names, purposes,
source IDs, aliases and notices never become global FAQ entries. Operational logs
contain safe codes/counts or opaque IDs, not transcript/name/token contents.

Caller self-identification/full-name answer permits minimal confirmation of one
unambiguous current ordinary meeting. The backend independently verifies the
source revision and complete coverage before disclosure. Duplicate names,
contradiction, missing participant provenance, sensitive/unknown meetings, stale
data or ambiguous aliases reveal no candidates.

Initial backfill reads a bounded upcoming interval, defaults to dry-run, and
indexes only supported ordinary records with verified participant metadata.
The installed Outlook mirror omits attendees: those imported meetings generally
need owner review and are **not** inferred from titles. Verified mirror aliases
require both trusted bridge metadata and configured source namespaces. Source
authority stays with the original provider; no replacement event is created.

Serial incremental ingestion uses expected-cursor compare-and-swap, ignores
duplicate deliveries, applies explicit tombstones/series invalidation, and never
interprets absence as cancellation. A 410 expired cursor marks the index stale
and requires explicit bounded resync. No calendar webhook or new native event
subscription is registered; unsupported webhook delivery is declared unsupported.

Only one exact existing curated wife/mother/friend number with owner-established
relationship and no contradictory evidence can receive current free/busy and
current owner-provided broad statuses. Unknown/hidden/shared numbers need owner
review. No event titles, attendees, sensitive reasons or inferred locations are
returned. Appointment delay notices are audience-specific, current and never
invented. Notice ID/category/audience cannot be rebound.

## Private Realtime calendar tools

`OWNER_CALENDAR_COACHING_ENABLED=true` offers **`get_owner_schedule`** directly to
the already accepted Realtime coaching session. Inputs: timezone-aware start/end,
IANA `time_zone`, and `view=schedule|free_busy`. Reads are limited to seven days
per lookup within a one-day lookback / 90-day horizon, four seconds, and two
lookups per coaching cue. Incomplete/expired/large results return unknown.

The model hears both microphones with application-provided speaker labels, can
look up your schedule when useful, then gives one short private hint. A caller's
own schedule question is not your calendar request. Owner schedule labels are
untrusted data; no descriptions/attendees are requested. Fresh results live only
in the private out-of-band response and owner hint history, not shared facts or
the incoming model. Cancellation/pause/changed context blocks late output. No
booking, invitation, payment, forwarding or other action tool is offered.

The hot 20-ms mixer validates in memory; generation/context changes revoke its
short-lived permission. Caption failure does not stop native microphone audio.
Partial attachment independently enters the existing conference fallback. Pausing
TTS revokes queued callbacks/assets; a clip already downloaded by Twilio can finish
its remaining bounded playback. These are synthetic regression results, not a
claim about new handset latency.

Shared `conversational-v1` speech guidance uses short natural sentences and one
question at a time, preserves honest identity/notices/exact farewell, and keeps
the configured masculine voice unchanged.

## API references

- [Google Calendar events insert](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)
- [Google Calendar incremental synchronization](https://developers.google.com/workspace/calendar/api/guides/sync)
- [OpenAI Realtime function calling](https://developers.openai.com/api/docs/guides/realtime-conversations#function-calling)
- [OpenAI Live backend delegation](https://developers.openai.com/api/docs/guides/live-delegation)
