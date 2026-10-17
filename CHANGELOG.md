# Changelog

## 0.4.1 — 2026-10-16

- Fix incoming calls being rejected before GPT-Live could start: apply the duration cap after the signed media start, when Twilio is in progress.
- Preserve number-level status callbacks rather than attempting a per-call callback update without a redirect URL.
- Record safe numeric Twilio failure codes and test call-state ordering, provider request fields and fail-closed setup.

## 0.4.0 — 2026-10-16

- Optional GPT-6 Luna quick lookup through a private ChatGPT-signed-in Codex CLI.
- Owner-curated contacts and expiring phone-safe facts, with four MCP tools and authenticated REST.
- Evidence-ID selection only: server injects exact current stored facts, never model-authored commitments.
- Disabled executable capabilities, strict deadlines, concurrency/usage caps and no API-key fallback.
- Durable lookup diagnostics; retired/changed/expired facts and late answers are withheld.
- Incoming greeting seeded at session start and explicit voice delegation rules.
- Availability values fetched through the fresh local reader before being offered.

## 0.3.0 — 2026-10-16

- Persistent agent-updatable incoming role, phone-safe context and anticipated-call notes.
- Freshness-bounded free windows and a read-only live context tool that works without Rocky.
- Five additional owner MCP tools and authenticated REST context endpoints.
- Optional automatic Google calendar availability refresh, using existing event-read permissions.
- In-memory credential refresh, time-only event fields, DST/all-day handling and stale-mirror gating.
- Useful clarification while awaiting supervisor help; bookings still require verified supervisor completion.

## 0.2.0 — 2026-10-16

- Incoming GPT-Live calls with a private message inbox and Rocky/MCP access.
- Supervised concierge requests, verified replies, deadlines, and message fallback.
- Three line-wide MCP events for future incoming calls, help requests and messages.
- Five additional MCP tools; optional ordinary-booking authorization, off by default.
- Mobile inbox and supervisor connection/request status alongside live captions.
- Signed provider binding, incoming rate/concurrency limits and provider duration caps.
- Dry-run Twilio routing helper with private backups, offline tests and replication guide.

An actual subscribed client is required for automatic supervision. The voice
model has no direct calendar or booking credentials. No private deployment
configuration or caller data is included.

## 0.1.0 — 2026-10-08

Initial public source release of the working self-hosted prototype.

- Outbound Twilio calling bridged to OpenAI GPT-Live.
- Authenticated REST and owner-paired OAuth MCP with nine tools.
- Persistent SQLite call records and transcript fragments.
- Direct DTMF navigation with voice-session handoff and context restoration.
- Optional Responses delegation for in-service voice tools.
- Idempotent mid-call context, spoken-request, and instruction updates.
- Private mobile monitor with SSE captions and direct hangup.
- Verified, signed MCP event subscriptions and an optional operator-configured webhook.
- Playback backpressure, stream diagnostics, and assistant-initiated ending.
- Sanitized examples, complete replication documentation, MIT license, and CI.

This release does not include private deployment configuration or real call data.
