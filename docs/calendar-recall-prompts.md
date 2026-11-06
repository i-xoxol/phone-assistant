# Calendar caption timing, return calls and prompt visibility

This change follows private inspection of the owner's test calls. No caller
identities, transcripts, recordings, voice identifiers or credentials are published.

## Calendar diagnosis

A subsequent new-booking test exposed another Mini failure: it described a slot
without executing any calendar operation and restarted confirmation repeatedly.
Its unmapped caller captions received zero display timestamps, sorting caller
speech ahead of assistant turns. No event had been created by that call.

Mini concierge sessions with appointments enabled now start with manual audio
commits. Each committed ASR item is bound to its captured telephone span. Luna
collects one missing detail at a time and chooses the existing slot/lookup tools.
Mini speaks server-issued questions, verified offers and confirmed results,
without independently choosing a time. The shared workflow still owns proposal
identity, fresh availability, current assent and single writes. Manual speech
interrupts playback. Message-only calls and other backends retain their controls.

The provider rejects changes to turn detection after conversation start, so this
mode is selected at session creation. Earlier instruction-ACK and conversation-
item barriers improved isolated checks but did not constrain every real call.
The verified speech boundary described below supersedes them for managed Mini
calendar output. Silence-only ASR is ignored.
Legacy zero-time native captions are displayed in receipt order with estimated
timing metadata; original database rows remain intact. Memory uses the same order.

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
Ordinary ElevenLabs instruction updates require an actual instruction ACK;
managed Mini calendar utterances instead acknowledge completed text-checked
generation. Gemini has no corresponding ACK and remains marked unconfirmed.
Neither socket writes nor ACKs prove playback.
Native booking intent continues to require actual offer/assent evidence. Current
audio, refusal, changed proposals, idempotency, fresh calendar checks and
uncertain-write reconciliation remain server enforced.

Mini's bounded Luna turn handler covers existing-meeting lookup and new-booking
intake through the same protected voice-tool facade. It requires a live concierge
call and settled current captions. Changed audio, unproven names and existing
dispatch suppress stale or duplicate work. Ordinary conversation before calendar
handling retains the original call policy and tools; calendar output uses the
verified boundary below. This uses the existing low-latency Luna API
client separately from subscription-backed recall. Failure gets one honest
unconfirmed explanation. There are at most twenty intent checks per call, with an
eight-second request deadline. The intent helper never invokes a calendar write;
new bookings still require the existing verified proposal and caller assent.
Manually committed Mini captions normally need no independent ASR. Unmappable
automatic ElevenLabs captions have a twelve-check fallback budget; Gemini retains
its four-check budget.

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
`call_updates.response_required` and nullable `spoken_text` columns, plus a
recall-context table. Rollback
restores source, retaining additive schema and all newer business records.
Actual telephone testing remains a separate owner-ready check.

`python -m scripts.check_mini_booking --env-file /private/service.env --audio-dir /private/fictional-clips`
replays fictional request/name/details/assent WAV clips through real Mini, ASR and
Luna. It requires one verified future offer, no write before assent, exactly one
fictional-calendar write and an audible confirmed result. Its calendar is entirely
local and its telephone gateway cannot dial. The required mono PCM16 24 kHz files
are `booking-request.wav`, `booking-name.wav`, `booking-details.wav` and
`booking-yes.wav`; private recordings must not be published as test fixtures.

## Verified Mini calendar speech

A later owner test showed that accepting instructions and even a current
application conversation item did not reliably constrain Mini's actual speech.
It announced an unchecked time as booked while the operation journal contained
no draft, availability check or create. There was therefore no booking to verify.
Instruction acknowledgments alone are insufficient for this provider path.

During managed Mini calendar/return-call turns, native speculative speech is now
withheld. The server supplies the missing-detail question or the actual shared
calendar result as bounded `spoken_text`. A short isolated session using the
**same pinned ElevenLabs model and voice** renders that quotation. Its complete
output transcript must match the supplied words, allowing number/ordinal and
punctuation differences, before any audio reaches telephone playback. Changed
words, additional confirmation claims, missing audio or failed generation cannot
be played. A text mismatch permits one retry; other failures close the bridge
rather than leaving a paid silent call running. This uses existing Realtime
permission, with no new TTS scope, key, vendor or voice selection.

A newer caller revision invalidates a pending render. Playback remains
interruptible. The transcript uses the actual generated words and the telephone
clock at enqueue; completion is evidence of generation, never proof that a
participant heard the complete response. Explicit owner spoken updates retain
their existing authorized path. After calendar handling, a general offer of
further help cannot add a new confirmation claim. Gemini's calendar adapter and
shared availability/consent/write protections are unchanged.

Return-call guidance keeps a provided first name and asks only for the missing
surname when lookup needs one. An earlier confirmed intake can supply a tentative
first-name hint even when historical ASR was phonetic. The assistant may check
that hint gently; a current caller affirmation followed by a fresh surname can
establish the lookup name. A refusal, correction or third-party name cannot.
History alone never provides current lookup identity or booking consent.

The migration adds nullable `call_updates.spoken_text`; existing rows, clients and
settings retain their previous behavior. The new renderer prompt is included in
the authenticated all-prompts inventory. The occupied-slot provider replay uses
`check_mini_booking --busy-first-slot`: the first office hour is fictional busy
state, the next free slot must be offered, and exactly one fictional write may
occur after current assent. The harness waits for the offered audio's duration
before sending agreement. It never accesses the real calendar or dials.
