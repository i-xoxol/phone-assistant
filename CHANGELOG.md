# Changelog

## Unreleased

- Per-contact spoken nicknames and familiar/formal conversation styles; unknown callers use the configured office greeting.
- Restore immediate incoming greetings after caller personalization, including a real-provider silent-caller check in English and Ukrainian.
- Prepare caller context with Luna during pre-answer ringing, without extending the pickup delay.
- Persist per-contact greeting languages and personalize known callers while preserving owner/caller roles.

- On-demand natural OpenAI private speech with subscription Luna, owner-leg request handling and single-use announcements.
- Prevent raw historical speech from being selected as coaching; revalidate source facts through audio delivery.
- Optional trusted saved-number recognition with explicit owner/caller roles.

- Pass presented incoming caller ID to both voice and tool-backend context, including message-only calls.
- Let callers confirm using that number for callbacks; preserve unavailable-number handling and identity/disclosure limits.

## 0.7.0 — 2026-10-16

- Exact-number history recall across prior incoming/outgoing calls, including unanswered attempts and confirmed messages.
- Automatic bounded subscription Luna recall, historical-context injection, and an owner MCP/REST history reader.
- Identity/disclosure limits, sensitive-detail filtering, source freshness checks and no inherited permissions from old speech.
- Configurable pre-answer TwiML pause to approximate a chosen number of rings, including closed-hours responses.

## 0.6.0 — 2026-10-16

- Optional conference handoff with 1 for private subscription Luna coaching, 3 for transcription only, and 2 to decline.
- Dedicated streaming OpenAI transcription of both human microphones; revised captions, separate owner labels and failure diagnostics.
- Bounded read-only factual coaching through owner-only Twilio announcements, with no Luna in transcription-only mode.
- Agent-managed office/mobile-reach schedules, IANA time zones, closed-hours announcements and optional message-taking.
- Two owner MCP tools for reading/updating hours, with persisted policies and server-enforced transfer restrictions.

## 0.5.1 — 2026-10-16

- Fix incoming GPT-Live refusing a request to reach the configured owner as if it were an arbitrary outbound call.
- Advertise screened owner contact in the voice delegation list and explicitly distinguish the fixed target from caller-supplied numbers.
- Add prompt regression coverage and a natural caller-phrasing provider check.

## 0.5.0 — 2026-10-16

- Persistent GPT-Live voice selection through MCP, REST and the paired mobile monitor; active calls keep their voice.
- One screened owner-contact attempt for a complex active call: fixed mobile, private briefing and press-1 acceptance.
- No-answer/decline returns to GPT-Live with retained history, voice and remaining duration; human conversation is not recorded/transcribed.
- Voice/Rocky initiation and bounded Luna recommendations; pause pending supervision and automated bookings during handoff.
- Signed parent/child callbacks, loop prevention, limits, ambiguity handling and transfer-state diagnostics.

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
