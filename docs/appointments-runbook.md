# Appointment upgrade: review, deployment and recovery

**Release gate: review this branch and obtain explicit owner approval before
merge, deployment, restart, migration or expanded access.** A passing test/PR is
not permission to deploy. Real calls/bookings require separate approval.

## Before release

1. Review the exact commit and [test evidence](appointments-review.md). CI must pass.
2. Verify no active calls and preserve unrelated changes. Read the current release,
   user-service PID, environment hash and native subscription binding snapshot.
   Do not print credentials, receiver URLs/secrets, private events or raw prompts.
3. Inspect supported full automation configuration/revision before any prompt edit.
   This task did not have a full-prompt/trigger CAS/readback API. Leave the existing
   supervision automation intact and report that integration gap. Do not replace
   it with polling or edit internal platform stores.
4. Confirm that the existing in-place Google grant really permits the proposed
   reads and writes on the trusted host; metadata declares `calendar.events` and
   persistent refresh, but live capability has not yet been tested here. No new
   credential placement/copy or grant is implicit. If consent fails, stop.
5. Approve/configure Google account namespace and primary write destination;
   required aggregate coverage; verified mirror URL/window; **actual owner-selected
   Office Hours event/series IDs**; optional verified mirror-source namespaces.
   No title search, guessed IDs, new calendar, Graph fallback or changed mirror job.
6. Approve the specific additional MCP read/book/manage scopes as needed, then
   reauthorize only the desired clients. Preserve existing scopes/subscriptions
   and renewals. Private coaching calendar reads are a separate opt-in feature flag.

## Configuration (all new features default off)

See `.env.example`. `APPOINTMENTS_ENABLED` enables incoming/MCP bounded tools;
`APPOINTMENTS_WRITES_ENABLED` separately permits one consented ordinary create.
`OWNER_CALENDAR_COACHING_ENABLED` enables only the private Realtime reader.
`APPOINTMENT_SYNC_ENABLED` enables five-minute private-index ingestion; enable only
after the approved dry-run. Write/index flags require the appointment feature.

`APPOINTMENT_DATABASE_PATH` must differ from the main calls database. It stores
private names, source references, operation journal, receipts, notices and metrics.
`APPOINTMENT_GOOGLE_TOKEN_FILE` references the existing owner-only nonsymlink cache
in place; never put its contents in the repository. Refresh is memory-only, cached
for its actual expiry, and never writes the original credential file.

`APPOINTMENT_MIRROR_WINDOW_VERIFIED=false` is fail-closed. Attest only after verifying
the installed last-success semantics, complete source coverage, 30/365 configured
window and five-minute cadence/jitter. The freshness threshold is 420 seconds;
this still permits the documented eventual-consistency race. Office IDs default
empty, so booking cannot report available until actual occurrences are configured.

## Schema and backfill dry-run

Schema v1 is a separate additive database, with idempotent creation/additive
series identity migration. No existing phone/OAuth/native subscription schema is
changed. Preview on a temporary copy, leaving the original byte-for-byte intact:

```bash
python -m scripts.appointment_registry --dry-run
python -m scripts.appointment_registry --dry-run --database /protected/appointments.sqlite3
```

After separately approved live read access, run the in-process
`gateway.sync.once(dry_run=True)` from the approved server operator context. It
returns counts only, reads at most the configured 60-day future interval, never
writes a calendar/index/cursor, and does not invoke the mirror CLI. Review eligible,
needs-owner and tombstone counts. Most imported work meetings lack participant
metadata and must remain needs-owner; do not import private descriptions or guess
names from titles. After the reviewed index approval, `dry_run=False` commits
only the private index. The automatic sync flag is off until that approval.

## Deploy only the approved revision

Use the existing source-archive deployment mechanism to the existing app directory,
not a new container/service. Preserve protected `.env`, databases, recordings,
cached notices, credentials and runtime files. Back up main/OAuth/private databases
using SQLite backup while quiescent and protect backup permissions; validate the
backups before replacing source. Record source commit, hashes, schema and flags.

Deploy first with appointment writes/indexing disabled; apply only approved
configuration and the separate registry schema. Restart the established **user**
service only after the no-active-call check. Verify health, exact release, feature
flags and schema. Compare native subscription IDs/event scopes/active state and
renewal behavior; read existing mirror/availability timers and approved context.
Do not change reach/office hours, caller contacts, recording, voice, phone routing,
DNS/security settings, or the background Outlook mirror.

Run a separately approved read-only exact-slot/private-coaching canary first. An
available result is not an appointment or a proven live voice test. New paid calls,
calendar writes or invitations are not automatic acceptance steps. Only enable
writes after the scoped grant and reviewed canary approval. Verify recorded audio
and private routing in an owner-authorized handset test.

## Recovery decisions

| Observed state | Safe next step |
| --- | --- |
| Checking/intent, no write attempted | Do not resume creation after restart/hangup; owner reviews unfinished request |
| Committing/uncertain, write attempted | `get_booking_status` reconciles stored deterministic identity; never retry a new create |
| Identity not yet visible / provider outage | Retain uncertain journal, bounded read-only retry or owner review; no blind redial/rebooking |
| Event exists, registry failed | Restore registry access, reconcile same operation; retain original event |
| Persisted fields differ / external conflict | Needs-owner; keep both legitimate events, no overwrite or automatic cancellation |
| Confirmed, injection/receipt missing | Deliver factual result if call remains current, or owner reviews it privately; no second event |
| Call ended during a sent write | Privately reconcile actual outcome; do not treat hangup as rollback |
| Expired sync cursor | Mark index stale; explicit bounded `resync=True` after approval; never infer cancellation from absence |
| Ambiguous alias / lost participant provenance | Suppress caller details; owner resolves mapping, no replacement booking |

Read private journal/outbox state and safe error codes, not public logs containing
names or transcript text. Reconciliation never calls `create`. Do not manually
delete uncertain rows or change idempotency keys to force a retry.

## Retention and rollback

No destructive automated cleanup is enabled. Keep unfinished/sent operations and
deterministic identity receipts until reconciled and through the owner's recovery
window. Suggested owner-reviewed retention: detailed drafts/checks/notices/metrics
30 days after completion/expiry; minimal source identity, payload hash, operation
and result receipts 90 days or the chosen deduplication window. Cancelled records
and aliases must outlive incremental replay cursors. These are proposed settings,
not a background deletion job. Existing call/audio retention remains unchanged.

Rollback is source/flag rollback, **not deleting a calendar event or registry**.
Drain active calls first, disable new writes/indexing, reconcile sent operations,
retain protected journal/backups, then restore the prior reviewed source archive.
The old service ignores the separate DB. A full stale backup restore could erase
legitimate new bookings/receipts and is not an automatic rollback authority.

## Offline checks

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m scripts.audit_release
python -m scripts.benchmark_appointments --iterations 30
```

The benchmark uses only fake providers and temporary databases. Its local disk
timings exclude network, receiver/agent processing, model generation, handset
playback and human response. Telemetry stages cover calendar checks, commit,
readback, registry and injection; actual speech/receipt are separate transcript
observations. A native subscription alone cannot establish timely agent handling.
