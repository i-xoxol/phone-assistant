# Calendar caption timing, return calls and prompt visibility

This change follows private inspection of the owner's test calls. No caller
identities, transcripts, recordings, voice identifiers or credentials are published.

## Calendar diagnosis

Calendar lookup was waiting for current caller captions. Twilio speech activity
uses its stream clock; native adapters had used connection wall time or provider
timestamps with a different origin/rate. The safety gate therefore kept returning
`caller_captions_pending` even after the name was transcribed. Holding phrases
did not represent a successful calendar read.

The adapters now map captions to the actual sent 8 kHz audio and Twilio frames.
Gemini's supported conversation configuration does not provide usable timestamp
offsets in the verified Developer API session, so committed ASR is bound
conservatively to captured speech spans. When one Gemini caption spans multiple
audio utterances, a bounded independent transcription check can prove the known
captured clip. It reuses the configured input-only OpenAI transcription path,
requires its text to match the native caller transcript, and refuses to advance
coverage after newer speech, hangup or a mismatch. It runs only for calendar
discussion/proposals in a live concierge call, at most four times per connection.
This incurs transcription usage only when needed; a fifteen-second RAM buffer is
cleared on close and does not create a recording. Failure leaves the check pending
for clarification/owner review. Ordinary timestamped and already-covered captions
need no extra ASR. ElevenLabs' native rate is calibrated
against captured speech onset; delayed captions cannot credit a newer answer.
Unknown coverage stays pending. Display timestamps are never proof of current
caller coverage. Reconnect offsets are applied once by the shared bridge.

Booking offers, clarification and verified completion updates explicitly request
a spoken response on native adapters. Context-only updates remain context.
ElevenLabs requires an actual instruction ACK; Gemini has no corresponding ACK
and remains marked unconfirmed. Neither socket writes nor ACKs prove playback.
Native booking intent continues to require actual offer/assent evidence. Current
audio, refusal, changed proposals, idempotency, fresh calendar checks and
uncertain-write reconciliation remain server enforced.

ElevenLabs also gets an existing-appointment routing reminder. Mini uses a shorter
calendar policy to reduce confusion with new-booking intake. When Mini still
omits a lookup, a bounded Luna intent check can extract the current caller's
request/name and dispatch the same read-only gateway. It requires a live concierge
call, settled current captions and no active new-booking proposal; changed audio,
unproven names and existing voice dispatch suppress stale or duplicate work.
It never creates or changes an event. This uses the existing low-latency Luna API
client separately from subscription-backed recall; failure leaves ordinary voice
handling available. There are at most twelve intent checks per call.

Real-provider fictional-calendar checks passed for Gemini, ElevenLabs Max and
ElevenLabs Mini (including Luna assistance). A real Luna Fast recall selector also
selected coherent fictional name/topic/outcome evidence. No telephone was dialed
and no real calendar was accessed by these synthetic checks.

Provider references: [Google Live server messages](https://ai.google.dev/api/live)
and [Google's maintained message reference](https://github.com/google/skills/blob/main/skills/cloud/gemini-live-api/references/client_server_messages.md).
Shared SDK fields do not establish support in a particular Live model; the actual
account/session configuration was tested before choosing the fallback.

## Return calls

See [call memory](call-memory.md). Within ten minutes, “it's me again” can load
the earlier name, purpose and unfinished question without a full intake restart.
The live model chooses its wording. Older references require topic context;
Luna selects relevant excerpts. History does not authenticate a caller, confirm
a booking, expand disclosure or inherit consent from another call.

## All assistant prompts

In the authenticated portal open **Extended settings · All assistant prompts**.
Use **Preview new incoming call** for the assembled current configuration, or
**Inspect selected call** for pinned settings and retained live server/owner
updates. **Show all system prompt templates** displays the source-labelled
inventory, including booking intent, name/title matching, Luna lookup/recall,
presentation, recording, transfer and private owner coaching.

The inventory reads source syntax without evaluating templates, calling a
provider or reading credentials. Templates contain runtime placeholders; the
assembled preview shows effective call-specific instructions. Provider-internal
prompts are not available through provider APIs. Application prompts are
read-only here; existing personality/context editors remain editable.

Owner-cookie REST: `GET /live/system-prompts`. Existing bearer REST:
`GET /voice/system-prompts`. Existing MCP clients can use `get_system_prompts`.
These retain existing authentication and create no call.

## Verification and rollout

`python -m scripts.check_calendar_adapter --provider gemini --env-file /private/service.env`
or `--provider elevenlabs` exercises real ASR, tool dispatch and the spoken result
with a fictional read-only calendar and a deliberately shifted telephone clock.
Optional `--caller-audio` accepts mono PCM16 WAV at 24 kHz; `--model` selects an
already supported model for this check. It cannot dial or write a calendar.
Fictional diagnostics remain in the ignored local data folder.

Targeted tests cover clocks, delayed captions, new speech, explicit spoken
updates, return cues, number isolation, older topics, sensitive/owner filtering,
current calendar identity, persistence and authenticated prompt reads. Mobile
and desktop browser checks exercise the actual UI without providers. Full
Python 3.11/3.12/3.14 CI and the release-content audit gate deployment.

Rollout changes reviewed source after a zero-active-call check, with source,
configuration and consistent SQLite backups. It preserves saved providers,
personalities, test contacts, recording/coaching preferences, sessions, OAuth,
subscriptions and calendar credentials. Migrations add the defaulted
`call_updates.response_required` column and a recall-context table. Rollback
restores source, retaining additive schema and all newer business records.
Actual telephone testing remains a separate owner-ready check.
