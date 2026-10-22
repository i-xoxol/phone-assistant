# Appointment upgrade: development and release boundary

This feature is disabled by default. Development uses synthetic calls, contacts,
calendar responses and a separate SQLite database. Nothing in this branch authorizes
a production restart, a calendar write, a telephone test or a new credential grant.

## Verified starting point (October 22, 2026)

| Capability | Installed service | Proposed addition |
| --- | --- | --- |
| Phone source | Realtime bridge release `f1d086a`; main contains merge `9433da5` | Isolated `codex/appointments-v1` |
| Work calendar | One-way Outlook → Google primary, five-minute timer with jitter | Preserve unchanged; no Graph calls |
| Mirror provenance | `bridgeName=shu-outlook`, source event ID, iCalUID, last modification | Verify aliases, never deduplicate by title |
| Google access | Existing owner-only nonsymlink credential; declares `calendar.events` and persistent refresh | Reuse in place only after release approval; no copied cache |
| Bridge status | Healthy successful complete run, 30-day past / 365-day future configured coverage | Exact-range freshness gate; residual five-minute race remains |
| Phone availability | Ten-minute TTL, first 40 windows, default minimum 30 minutes | Direct exact-interval reads; missing cached window means unknown |
| Calendar writes | Mirror has unbounded reconciliation writes, no booking RPC | Separate bounded create/readback adapter; never invoke mirror CLI |
| Imported identity | Mirror intentionally omits attendees | Missing participant identity requires owner review |
| Supervision | Existing native subscriptions and asynchronous request deadlines | Preserve delivery; add revisioned progress separately from terminal replies |

The installed bridge is Python, not a PowerShell or MCP booking service. Its main
command writes by default and was not invoked. Scope metadata was inspected on its
existing host without returning credentials. A live authorization/booking test has
not been performed. No new credentials or permissions have been acquired.

## Implementation order

1. Versioned provider-neutral types and deterministic office-hours/coverage policy.
2. Private operation journal, consent/check revisions, registry, outbox and uncertain-write recovery.
3. Google adapter with mocked HTTP, existing in-place token boundary and separate mirror-health reads.
4. Narrow call-bound voice/MCP integration, bounded appointment/family disclosures and shared style.
5. Coaching regressions, failure fixtures, synthetic latency measurements and CI.
6. Review exact commit, migration/backfill dry run, missing integration capabilities and rollback plan.

Booking hours are academic hours, distinct from phone reach hours: Monday/Thursday
10–12 and Friday 14–16 America/New_York. Whole intervals, current owner-designated
office-hour occurrences, exceptions, buffers and complete aggregate coverage must
pass. Calendar titles cannot establish an availability block. Ordinary callers
cannot cancel/reschedule events, change policy, select calendars or enable invitations.

The core has no vendor SDK imports. It persists an intent before a single external
create, uses a provider-qualified deterministic identity, independently reads back
the event, checks for external races, then commits registry and result outbox together.
A lost response or a registry failure is reconciliation work, never permission for
another event or an automatic deletion. Injection acknowledgment is not spoken receipt.

Google primary is the initial configured destination, not core business logic.
There is no second calendar write and no direct SHU/mail lookup. Approved availability,
relationship recognition and phone-safe status notices cannot broaden booking authority.

Release requires Igor's separate explicit review and approval. Platform supervision
automation must not be edited without supported full configuration/revision readback;
an inaccessible configuration is an integration gap, not permission to patch internals.
