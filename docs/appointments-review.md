# Appointment upgrade: review evidence

Branch: **`codex/appointments-v1`**, based on main merge `9433da5`.
The reviewed commit and CI checks are attached to the pull request. **Not deployed.**

## Implementation delivered

- Versioned provider-neutral contracts and privacy-filtered call/MCP schemas.
- Exact 15-minute reads, current academic-office occurrences, mirror health/coverage
  gates, consent/audio interruption checks and actual request deadlines.
- Durable single-create saga, deterministic identity, pre-transport authorization,
  source readback, external-race detection, registry/outbox repair and separate
  caller delivery evidence.
- Bounded private repeat-caller registry, verified aliases, incremental cursor CAS,
  tombstones/series invalidation and explicit expired-cursor resync.
- Curated relationship/status controls with separate grant, current audience-scoped
  notices, no global/private context mixing, no candidate enumeration.
- Private Realtime `get_owner_schedule`: seven-day bounded reads and fresh results
  only to the accepted owner coaching channel; no action tools.
- Shared speech policy, unchanged configured voice, four coaching regressions.

## Capability verification and production boundary

Read-only installed source/metadata inspection confirmed the Python one-way
Outlook → Google-primary mirror, five-minute timer/jitter, 30/365-day coverage,
existing `calendar.events` persistent Google grant metadata, and time-only
downstream exporter limits. No token values were returned or copied.

An isolated in-memory fixture executed the installed mirror's pure planner with
one synthetic native phone-owned event and no source events: **zero actions**, the
phone event preserved, zero calendar network calls. The default-writing CLI was
never invoked. Installed phone release source hash/configuration and the running
user-service PID remained unchanged during development. Five native subscriptions
remained active: incoming, supervisor requests, messages, status and transcript.

No production source/configuration/database migration, service restart, active
call update, real telephone call, real calendar event, contact/hour/voice/routing/
recording change, credential/grant change or native subscription change was made.
Tests use a separate worktree and temporary SQLite databases with synthetic data.

## Test evidence

Run from the feature worktree with the installed Python 3.14 development runtime:

```bash
python -m pytest -q
python -m scripts.audit_release
python -m scripts.appointment_registry --dry-run
python -m scripts.benchmark_appointments --iterations 30
```

The PR records the final exact test count and CI status; CI independently runs
Python 3.11, 3.12 and 3.14 with pinned dependencies. The local full suite passed
531 tests; added credential, audience-update and lookup cases also passed focused
checks. The complete final suite contains 536 tests for CI verification.

Exercised failure cases include missing/truncated exporter coverage; early busy;
whole-interval boundaries, office exceptions/buffers, all-day DST and ambiguous
local time; mirror outage/staleness/partial coverage/pagination; lost writes;
crash/restart repair; registry failure; external edit races; changed payload,
withdrawal/barge-in, expired request and hangup during provider preparation;
duplicate identity/delivery/cursors, recurrence cancellation and source alias
ambiguity; missing/sensitive/duplicate/contradictory caller data; current private
notices; injection versus full speech/receipt; separate grants and schema version;
private schedule tool loop, cancellation, pause and routing; partial human bridge,
normal caption-reader exit, paused TTS revocation and zero DB opens in 100 mixer frames.

Both the unrelated fake adapter and HTTP-mocked Google run the same create/readback/
reconciliation/privacy/unsupported-capability and pre-transport hangup contract tests.
Core files are checked for vendor imports/shapes. No second real provider was connected.

Schema preview: version **1**, zero operations/appointments/outbox records in the
new synthetic DB, source unchanged, **zero provider calls**. Backfill dry-run
does not change the index or cursor. Expired cursors require explicit resync.

### Synthetic timing sample

Thirty local Windows fake-provider samples; **milliseconds**, p50 / p95:

| Stage | p50 | p95 |
| --- | ---: | ---: |
| Exact check | 12.052 | 16.234 |
| Commit including recheck/readback | 44.488 | 62.757 |
| Registry persistence | 5.895 | 8.916 |
| Synthetic injection acknowledgment | 5.370 | 9.601 |

These are backend/disk fixtures, **not real API, model, webhook receiver, speech or
handset latency**. They establish no two-second live-call guarantee. Production
telemetry separates reads/commit/registry/injection; spoken confirmation and actual
receipt use transcript evidence. Actual receiver/agent timings require approved
client instrumentation and a later acceptance test.

## Still required before release

1. Owner review/explicit approval of exact commit, deployment and feature flags.
2. Approved Google-boundary configuration and actual Office Hours IDs; aggregate
   and mirror coverage attestation. Live Google read/write capability has not been
   tested in this task. No new grant is silently acquired if current access fails.
3. Separate desired MCP appointment grants/client reauthorization. Calendar writes,
   indexing and private coaching reads are independently opt-in.
4. Imported Outlook/Book With Me participant identity is absent in the installed
   mirror. Those records stay needs-owner without authoritative minimal metadata;
   no private descriptions, inferred participants or direct Graph fallback.
5. Supported full supervision automation prompt/trigger revision/CAS/readback was
   not available here. Existing automation was preserved; its policy reconciliation
   remains an owner-controlled integration step, not a guessed internal edit.
6. Separately authorized read-only live canary, then any desired paid call/real
   booking/handset acceptance test. Synthetic results do not claim these passed.

Use the [deployment/recovery/rollback runbook](appointments-runbook.md). Preserve
legitimate new events and journal receipts during rollback; do not restore stale
data or delete a booking to compensate for a notification failure.
