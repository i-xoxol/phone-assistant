# Telephone answering controls and optional Gemini Live

The authenticated `/live` portal now manages provider-specific answering choices,
contact overrides, audio recording, and cached recording notices. These choices
apply to **new calls**. Each call retains its effective provider, model, voice,
recording flag and announcement flag through keypad redirects and voice reconnects.
Lookup and private owner coaching retain their existing independent configuration.

## Portal

1. Open **Answering settings**. Choose **Provider**, **Model**, and **Voice**, then
   **Save answering settings**. The status distinguishes saved settings from
   unsaved edits. Models and voices come from the server capability registry.
   **Refresh ElevenLabs library voices** fetches up to 500 saved library voices;
   it does not generate speech or place a call. The configured private voice is
   retained if a library refresh fails.
2. Open **Caller book · Language and answering**. Existing names, language,
   nicknames and familiar/formal greetings stay in the same caller book. Each
   saved contact has **Answering provider**, **Model**, **Voice**, and
   **Save contact answering**, plus its effective selection. **Default / inherit**
   follows later global changes. A contact model/voice override requires an
   explicit provider so it cannot become a foreign voice after a global change.
3. Open **Recording and announcement**. Set **Audio recording for new calls** and
   **Play the cached recording announcement**, then **Save recording settings**.
   An active call still has its separate **Stop audio recording** control.
4. In that panel select **Announcement language**, edit **Short announcement
   text**, and explicitly click **Generate replacement**. Preview the draft with
   **Preview replacement**, then click **Activate replacement**. Alternatively,
   choose a mono PCM16 WAV and click **Upload replacement**. Limits are 2 MiB,
   eight seconds, and 8–48 kHz; uploads normalize to 24 kHz mono PCM16 WAV. Check
   that uploaded speech matches the supplied text. English and Ukrainian have
   independent active/draft clips. **Preview active clip** plays the saved notice.

Generation is charged provider usage and occurs only on an explicit generation
request. Neither page load nor normal call playback requests TTS. Generated
speech must have a matching transcript; preparation preambles are rejected.
A failed generation/upload/activation preserves the current clip. New clips are
written atomically; activation changes a SQLite pointer to an immutable asset.
Already-issued signed URLs retain their original bytes after replacement.

## Selection and compatibility

Precedence is explicit per-call provider/model/voice, then a **unique** matched
contact, then provider-local saved defaults, then environment defaults. If a
per-call provider differs from the contact provider, the contact's foreign model
and voice are discarded. Null contact settings are actual inheritance, not copied
values. Ambiguous/unmatched contacts use the global default. Incoming matching
uses the existing presented original caller number and forwarding ambiguity
rules; the forwarding owner's number does not become the caller's identity.
Recognition and voice selection grant no disclosure or booking permissions.

The OpenAI registry retains GPT-Live 1 and the existing documented voice list.
ElevenLabs offers Realtime Mini/Max and the configured/permitted saved voices.
An ElevenLabs voice cannot be selected for OpenAI or Gemini. Model strings are
validated against the registry rather than accepted as arbitrary text.

Existing `/voice`, `/voice/backend`, `get_voice_settings`, `set_voice`,
`get_voice_backend`, `set_voice_backend`, and `place_call` remain compatible.
`set_voice` still updates only OpenAI's default voice. `set_voice_backend` adds
optional `model` and `voice`; the original optional `elevenlabs_model` argument
still works and omission preserves the saved provider-local selection.
`place_call` adds optional `voice_model` and `voice` alongside `voice_provider`.
No provider changes automatically redial or switch an existing call.

Additional owner MCP tools:

- `get_contact_voice` / `set_contact_voice(contact_id, provider?, model?, voice?)`.
- `get_recording_settings` /
  `set_recording_settings(recording_enabled, announcement_enabled)`.
- `get_recording_notices`, `generate_recording_notice(language, text)`, and
  `activate_recording_notice(language, asset_id)`.
- `refresh_voice_library`.

REST equivalents use the existing bearer-authenticated `/voice` and `/concierge`
boundaries: `/voice/recording-settings`, `/voice/recording-notices`,
`/voice/recording-notices/{language}/generate`,
`/voice/recording-notices/{language}/activate`, `/voice/refresh-library`, and
`/concierge/contacts/{contact_id}/voice`. The portal uses session-authenticated
`/live` counterparts with the existing CSRF/origin checks. Owner audio previews
are authenticated; Twilio delivery requires a valid signature covering the whole
path and version query. There is no new public administrative endpoint.

## Recording facts

| Audio recording | Announcement preference | New-call behavior |
| --- | --- | --- |
| On | On | Issue the cached notice once and request recording; if the clip is unavailable, continue without recording. |
| On | Off | Request recording without a notice or generated recording greeting. |
| Off | Either | Request no recording and play no recording notice; preserve the announcement preference. |

Transcription and live speech processing remain distinct from saved audio.
Turning off recording does not delete earlier recordings. A participant opt-out
is saved even before recording starts; retries, transfers and later settings
changes cannot restart it. An unconfirmed provider stop ends the call using the
existing fail-closed behavior.

`recording_requested_at` and nullable `notice_issued_at` are separate facts.
`notice_playback=requested_unconfirmed` means the application issued TwiML to
play it, **not** that a human heard it. No notice or delivery timestamp is
fabricated when announcements are disabled. The model receives this actual state,
including missing clips and recording uncertainty, and must answer direct
recording questions truthfully. It must not add a disabled notice to its greeting.

The legacy recording-policy table is migrated transactionally to allow a null
notice time while retaining every previous request, stop and error. All other
settings tables are additive. No contact, call, recording, message, language,
fact, hours, expected-call note, OAuth grant or calendar credential is replaced.

## Gemini availability and design

The optional adapter uses `google-genai==2.34.0`, server-to-server Live API, and
`gemini-3.8-live`. It starts **disabled** until a suitable private Gemini API key
and provider verification are available. Calendar OAuth is not a Gemini API key;
no billing project or calendar configuration is changed.

Required private setup: `GEMINI_API_KEY`, access to `gemini-3.8-live` in that
account, and a supported prebuilt voice (`GEMINI_VOICE`, default `Kore`). Run the
bounded synthetic check below before setting `GEMINI_LIVE_VERIFIED=true` in the
service environment. That flag is an operator verification gate, not an automatic
claim of provider success. The UI rejects Gemini selections while the gate/key is
missing. The initial implementation intentionally exposes only the 30 documented
prebuilt voices, with no cross-provider cloning or imports.

The adapter reuses the call manager, persistence, event delivery, bridge,
message/language/transfer/opt-out controls, and server-enforced tool executor.
Booking still requires the existing readback, caller assent, revision binding and
uncertain-write reconciliation. It declares functions `BLOCKING` explicitly;
model-default asynchronous scheduling is not assumed. Function responses resume
the model without an extra response trigger. Native cancellation suppresses
pending tools and duplicate IDs. Already-dispatched writes retain their existing
uncertainty and idempotency protections; cancellation cannot undo a completed
operation.

Audio is statefully resampled with libsoxr: 8 kHz G.711 μ-law telephone input to
16 kHz little-endian PCM16, and 24 kHz PCM16 output back to 8 kHz μ-law. Conversion
has bounded chunks, carries partial samples and filter state across frames, and
resets output on native interruption. It uses neither `audioop` nor frame-level
subprocesses. Every audio part and both transcription fields in an event are
processed independently. Playback is bounded and interruption clears queued
telephone audio. Connection/session duration and cancellation are bounded;
provider failures end the telephone leg through the shared cleanup path.

Startup signals are claimed durably once per call for ElevenLabs/Gemini. A
replayed signal or resumed session cannot create another opening. Startup-only
instructions are not repeatedly inserted into ElevenLabs permanent context.

Gemini history-seeding mode is enabled only when restoring an earlier transcript.
The history upload ends with `turn_complete=true`, which exits that history-only
phase without generating speech. Fresh calls omit history mode so their one-time
opening can immediately generate audio instead of being silently consumed as history.

Gemini mid-session `send_client_content` has explicit user/model roles.
Application context/instruction updates are labelled server supervision, with no
expanded authority; spoken updates direct a question **to the caller**. A context
write does not complete a model turn; a spoken update does. The current protocol
exposes no matching client-content acknowledgment, so these updates are reported
`unconfirmed` rather than falsely acknowledged. Transcript/audio evidence is
needed to establish subsequent use, and real human hearing needs a telephone
check. Reconnects retain durable call settings and previous speaker roles.

Official references reviewed November 4, 2026:
[model](https://aistudio.google.com/docs/models/gemini-3.8-live),
[SDK startup](https://aistudio.google.com/docs/live-api/get-started-sdk),
[audio, voices, updates and interruption](https://aistudio.google.com/docs/live-api/capabilities),
[function calling](https://aistudio.google.com/docs/live-api/tools),
[prebuilt voices](https://aistudio.google.com/docs/speech-generation#prebuilt-voices),
[ElevenLabs library catalog](https://elevenlabs.io/docs/api-reference/voices/search).

## Validation and release gates

Run `python -m pytest -q` and `python -m scripts.audit_release` in the isolated
checkout. GitHub CI covers Linux Python 3.11, 3.12 and 3.14. Tests cover registry
validation, inheritance, forwarding, migration and reconnect pinning, the recording
matrix and opt-out, immutable caching, authenticated previews and signed delivery,
GenAI SDK configuration, audio golden values/chunk continuity/alias rejection,
all event parts, transcription, cancellation, duplicate writes, and existing
booking/coaching/subscription boundaries.

The bounded real-provider check uses fictional storage and simulated playback:

```sh
python -m scripts.check_answering_providers --provider openai --env-file /path/to/private.env
python -m scripts.check_answering_providers --provider elevenlabs --env-file /path/to/private.env
python -m scripts.check_answering_providers --provider elevenlabs --model eleven_realtime_v1_max --env-file /path/to/private.env
python -m scripts.check_answering_providers --provider gemini --env-file /path/to/private.env
```

This never dials or accesses calendar APIs and does not enable a provider in the
running service. It verifies startup with a silent simulated caller, generated
audio/transcript, an injected fictional fact used in an answer, and a spoken
owner question directed to the caller. It does not prove telephone codecs,
real caller ASR, recording audibility, carrier interruption timing or human
hearing. No actual telephone test is authorized until the owner confirms readiness.

Before deployment, require clean tests, CI, isolated mobile/desktop browser checks,
and relevant configured-provider checks. Recheck the running release/source hashes
and active call count. Back up changed source, private configuration, existing
service configuration and all SQLite databases. Drain/recheck before stopping;
do not restart during an active call. Install only the reviewed runtime delta,
retain OpenAI and the existing recording/announcement defaults, then verify public
health, login authentication, existing authorized MCP access, subscription
bindings, and unchanged calendar/booking configuration.

Rollback restores source/runtime configuration only after another zero-active-call
check. **Never copy a database backup over current call/message/calendar records.**
The migrated schema is compatible with the previous adapter's explicit recording
policy inserts and unchanged call-provider table. Before a later manual rollback,
check whether the owner has since selected Gemini or changed recording policy:
the older source cannot honor the new controls, so do not silently translate those
choices into another provider or silently enable recording. Preserve current data
and resolve that configuration explicitly. Keep the isolated branch/PR and private
release manifest/backup for recovery; do not publish private IDs or audio.
