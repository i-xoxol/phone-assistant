# Voice selection and reaching the owner

For natural private notes after accepting a transfer, see
[private voice coaching](private-voice-coaching.md).

The private monitor and MCP can change the default GPT-Live voice. A complex
call can also make one screened attempt to connect the owner personally.

## Select a voice

Open **Voice and owner contact** on `/live`, select a voice, and save. Rocky or
another authorized MCP client can use `get_voice_settings` and `set_voice`:

```json
{"voice":"meridian"}
```

Meridian has a masculine North American presentation; Gleam has a feminine
North American presentation. The catalog also includes Marin and documented
regional/language options. Test pronunciation in the languages you use.
The full catalog comes from the
[official GPT-Live voice guide](https://developers.openai.com/api/docs/guides/live-conversations#voice-options).

The choice persists in SQLite and overrides `OPENAI_VOICE` for new calls. A call
pins its voice when the first Live session starts. Existing calls, including an
assistant resumed after a failed owner connection, keep that voice. GPT-Live
[requires a new session to change voice](https://developers.openai.com/api/docs/guides/live-conversations#configuration-fields),
so this release does not switch voices during a conversation. It does not clone
or import voices. Twilio's short transfer/screening announcements use its own
standard speech voice; they are separate from the chosen conversation voice.

Authenticated REST equivalents are `GET /voice` and `PUT /voice`. Browser
`PUT /live/voice` requires its paired cookie, same origin and CSRF token.
Callers and voice-model tools cannot change the default.

## Enable owner contact

Configure the private service host, never a public plugin manifest:

```dotenv
OWNER_TRANSFER_ENABLED=true
OWNER_TRANSFER_NUMBER=<your verified mobile in E.164 format>
OWNER_TRANSFER_RING_SECONDS=20
```

The feature is disabled in the distributed configuration. The destination is
fixed by the server environment and is never an argument supplied by a caller,
model or remote MCP client. The target must differ from both the Twilio number
and the phone already participating in the call. Keep the number private and
verify Twilio outbound permissions for that destination.

The voice backend may select `reach_owner` for a complex issue needing the owner,
after explaining the transition and obtaining the caller's agreement to hold.
An explicit request to speak to the owner can also start this screened flow.
Saying "call the owner on their phone" uses the configured target; it does not
allow a caller to supply a destination or disclose the owner's number.
An optional subscription-backed Luna lookup can recommend owner involvement;
its recommendation alone cannot dial. The voice still follows the consent and
server policy. A supervising agent such as Rocky can also call:

```json
{"call_id":"<active call UUID>","reason":"The question needs the owner personally."}
```

Use MCP `reach_owner` or owner REST `POST /calls/{call_id}/reach-owner`. The mobile
monitor has **Ring my mobile and connect me** for an owner-requested handoff.
The voice tool is available in incoming concierge mode and outbound calls with
voice tools enabled. No emergency-response, payment, contractual or sensitive
disclosure authority is created by escalation.

## Screened transfer flow

### With continuous transcription and optional coaching

Set `OWNER_OBSERVATION_ENABLED=true` to use the conference flow. Supply API access
to `OPENAI_TRANSCRIPTION_MODEL=gpt-live-transcribe`. For coaching also enable the
existing subscription lookup worker and sign Codex in with ChatGPT on the service host.

The private screening prompt offers **1: accept with Luna coaching**, **3: accept
with Luna off**, and **2: decline**. Both accepted modes stream both people's
microphones to dedicated OpenAI transcription sessions. The mobile monitor and
MCP transcript events continue after handoff, labeling the owner separately.
Partial captions replace earlier text with the same source event ID; the saved
record contains the latest revision. These streams create no audio recordings;
the separate recording policy still applies.

Only whisper mode starts GPT-6 Luna. It selects one useful current phone-safe
fact, anticipated-call note or fresh availability window from owner-maintained
context. The server rechecks the fact before a private Twilio participant
announcement to the owner's leg. The caller cannot hear that announcement.
Luna cannot use arbitrary tools, spend money, book anything or choose recipients.
It uses the existing subscription CLI with no API-key fallback. There are at
most twelve lookups and eight unique suggestions per call, with a twenty-second
lookup cooldown, twenty-five-second announcement cooldown and a quiet-gap check.
Suggestions are not instant; a delayed result is discarded when the call ends.
This is bounded factual coaching, not unrestricted continuous reasoning or
an independent calendar/booking agent. There is no phone-page button required.

Twilio conference and media-stream charges, two phone legs and OpenAI transcription
charges apply. Luna uses subscription limits; Twilio supplies the announcement
voice independently of the GPT-Live voice setting unless natural speech is enabled.
The optional [hybrid flow](hybrid-owner-coaching.md) uses cached concise screening,
TTS hints and brief caption-fed Live coaching. Your original duration cap
still applies to both phones. Transcription is enabled in both modes, but network
or provider failures can create gaps; the monitor reports `degraded`. Such a
failure does not disconnect the human conversation or start Luna in captions-only mode.
There is currently no automatic transcription reconnection after a provider failure.

Expose the same HTTPS origin including `/twilio/owner-media/{transfer_id}/{caller|owner}`
as WSS and all signed `/twilio/owner-transfer/{transfer_id}/*` callbacks. No new
public owner-control endpoint or credential is needed. Conference callbacks bind
the account, room and participant; microphone streams additionally bind the exact
call SID. Callers hear that the conversation will be transcribed before joining.

Manual acceptance test: call from a second phone, request the owner, and select 3.
Speak distinct phrases on both phones and confirm two labeled live captions and
zero coaching. Repeat with 1 after setting a short synthetic fact through
`set_call_fact`; ask about it and confirm only the owner hears the suggestion.
Also test 2/no answer, direct monitor hangup, and both phones disconnecting.
`python -m scripts.check_owner_transcription --caller-wav synthetic.wav` verifies
actual OpenAI transcription of synthetic mono PCM16/8k audio without dialing.
`python -m scripts.check_owner_coaching` verifies actual subscription Luna selection
against a simulated telephone announcement. Neither proves handset isolation;
the two-phone test is still required on your deployment.

### Legacy bridge (observation disabled)

1. The server checks the current call/bridge, remaining call time, fixed target,
   one-attempt-per-call rule and maximum ten attempts per hour.
2. It expires pending supervisor requests and pauses the voice bridge. Automated
   bookings and supervisor replies must stop while the owner takes over.
3. Twilio tells the caller it is trying the owner, then rings the configured
   mobile, using the owned Twilio number as caller ID.
4. If answered, the owner hears the concise reason privately and has eight
   seconds to press **1 to accept** or **2 to decline**. No key also declines.
   Ordinary voicemail cannot connect the original caller without acceptance.
5. After acceptance, Twilio connects the two people directly. The AI session is
   closed. Their conversation is **not recorded or transcribed** by this service.
6. No answer, busy, declined or failed dialing reconnects GPT-Live with the same
   call ID, voice, transcript history and remaining time budget. It explains the
   owner was unavailable and offers message-taking. It never redials automatically.

This uses Twilio's official
[`Dial`/`Number` screening flow](https://www.twilio.com/docs/voice/twiml/number#url),
with an [`action` callback](https://www.twilio.com/docs/voice/twiml/dial#action)
to return to the assistant. It is simpler than a conference and does not keep
the AI listening during the human conversation. A separate owner notification
call would not connect the people; this flow handles both ringing and connection.

Twilio bills an additional telephone leg while reaching/connecting the owner.
The existing parent duration cap remains; the new leg is also bounded by the
remaining call time. A provider request with an ambiguous result is never
retried: the service requests a parent-call hangup to avoid an uncontrolled paid leg. Network
failure therefore can still end a call. These limits are not a carrier spending cap.

An owner mobile that forwards busy calls back to Twilio must not create a loop.
Incoming calls with this instance's own Twilio caller ID are rejected. Existing
single-incoming-call admission also remains. Do not configure the Twilio number
as its own target.

## Endpoints and state

Expose these signed Twilio POST endpoints through the same HTTPS tunnel:

- `/twilio/owner-transfer/{transfer_id}/screen`
- `/twilio/owner-transfer/{transfer_id}/accept`
- `/twilio/owner-transfer/{transfer_id}/leg-status`
- `/twilio/owner-transfer/{transfer_id}/finished`

They validate account and exact parent/child call binding. Screening uses the
server's Twilio fetch when a child callback omits its parent SID. No credentials
or destination numbers are placed in callback URLs.

`get_call_result.owner_transfer` contains the attempt, source, timestamps,
acceptance, state and sanitized failure code, excluding the private destination.
`dialing`, `ringing`, `screening` and `accepted` are progress, not proof a human
conversation completed. `completed` requires owner acceptance and Twilio's
`DialBridged=true`; `returned_to_assistant` means no human connection was verified.
The monitor receives transfer-state updates on its existing SSE stream.

Rocky must check the current call, request deadline and owner-transfer state
immediately before any booking/action. Stop automated work during a handoff.
Never repeat a booking or dial because its notification was uncertain.

## Test

```bash
python -m pytest -q
python -m scripts.check_owner_transfer_voice
```

The second command uses the real GPT-Live/Responses service with Meridian and
a fake telephone gateway. No PSTN call is placed. Optionally supply
`--caller-wav /private/synthetic-caller.wav` for synthetic mono PCM16 8 kHz speech
asking to reach the owner and agreeing to hold. Without it, the caller request
is seeded as text. This does not test Twilio's real dialing or mobile acceptance.

Most coverage uses fake Twilio/Live connections: voice persistence, caller/owner
binding, one attempt, accept/decline/no-answer, provider ambiguity, timeout budget,
history-preserving resume, loop prevention and conflicting supervisor work.

For a real test, call the Twilio number **from a second phone** while the owner's
configured mobile is free. Ask to speak to the owner and agree to hold. Verify:

1. The owner mobile rings and gives the private briefing.
2. Pressing 1 connects the caller; pressing 2/no key returns the caller to GPT-Live.
3. Not answering also returns to GPT-Live, which can save a confirmed message.
4. The original monitor hangup still ends the parent call and its attached dial leg.

Calling from the owner mobile itself cannot test ringing that same number; the
server blocks that self-dial. A real PSTN acceptance test is separate from a
mocked/provider-model-only test. Do not claim it passed merely because the
application generated valid TwiML.
