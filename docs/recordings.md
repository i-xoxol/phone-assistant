# Forwarded calls and private recordings

The assistant can answer mobile calls forwarded by your carrier when you decline
or do not answer. Configure conditional forwarding on your phone/carrier to your
Twilio number. This application does not change the carrier settings.

## Forwarding and caller identity

Twilio's signed incoming webhook supplies `From` (presented caller ID) and may
supply `ForwardedFrom` (the number that forwarded the call). Carrier behavior
varies: the original caller may be preserved, replaced or withheld. The service
keeps both fields separate. An absent forwarding field means **unknown**, not a
verified direct call. It cannot determine whether the owner declined or simply
did not answer. If `From` equals the reported forwarding number, contact recognition
and number-based historical recall are withheld; the voice asks for a name and
preferred callback rather than assuming the caller is the owner.

`get_call_result.forwarding` exposes private routing metadata to the owner.
The forwarding number is excluded from voice/backend prompts. Normal caller ID
remains visible and follows the owner's saved-contact policy. Your carrier might
omit forwarding metadata, so verify with a consenting test caller.

Set `INBOUND_MISSED_CALL_CONTEXT=true` to tell the voice this line also receives
missed/declined mobile calls. It keeps the configured office/familiar tone, helps
within concierge limits, and can save a message for the owner and supervising
agent. It does not automatically ring the owner back. Ordinary supervision,
booking limits, contact nicknames, office hours and language preferences remain.
This is general line context, not an expiring anticipated-call note.

## Enable recording

Recording is opt-in for each deployment. Prepare reusable English/Ukrainian
OpenAI speech notices once on the service host (incurs speech API usage):

```bash
python -m scripts.prepare_recording_notices
```

Then set and restart the single worker while no calls are active:

```dotenv
CALL_RECORDING_ENABLED=true
INBOUND_MISSED_CALL_CONTEXT=true
```

Notices live in ignored `data/recording-notices/`, or the absolute directory in
`RECORDING_NOTICE_DIRECTORY`. They contain your configured caller name, not phone
numbers or secrets. Existing files are preserved; regenerate deliberately after
changing the owner name or notice text. Do not commit generated audio.

The pre-answer ring delay runs first. The cached notice then plays, and
`<Start><Recording>` records both audio tracks before connecting the voice model.
There is no upfront AI label; the assistant answers honestly if asked. Ukrainian
contacts get a Ukrainian notice. A missing notice disables recording for that
call and emits an unavailable event rather than recording silently or breaking
the call. Duplicate webhooks and keypad/handoff reconnects do not restart recording.
Enabling the feature does not recover audio from earlier calls.

Callers are told the call will be recorded and may ask the assistant to stop.
`stop_call_recording` is a narrow privacy control, not authority to delete prior
audio or restart recording. The monitor and owner API also have a stop control,
including during human handoff. If the provider stop fails, the service attempts
to end the call and reports uncertainty if that also fails. Audio opt-out does
not stop speech processing/live captions; decline of those requires ending the call.
Review consent obligations for your participants and jurisdictions before use.
The recorded audio begins after the notice; SQLite stores when notice TwiML was
issued, not proof the caller heard it or a recording of the notice itself.

The parent caller leg continues recording through owner handoff. The owner's
private briefing/coaching leg is not independently recorded; owner-only whispers
should not be present in the caller-leg audio. Verify this isolation in your
manual conference test before relying on it.

## Retrieve audio

The paired `/live` site shows recording status and a mobile audio player after
Twilio reports `completed`. Its audio routes require the paired browser cookie;
the stop button additionally requires same-origin CSRF protection.

Owner REST routes use the existing bearer token:

| Route | Purpose |
|---|---|
| `GET /calls/{call_id}/recordings` | Policy and recording metadata |
| `GET /calls/{call_id}/recordings/{recording_id}/audio` | Private MP3 proxy |
| `POST /calls/{call_id}/recordings/stop` | Stop audio for this call |

Authenticated MCP clients use `get_call_recordings(call_id)`,
`get_call_recording_audio(call_id, recording_id)` and `stop_call_recording(call_id)`.
Audio uses an MCP audio content block; client support varies. Rocky can always
read the saved transcript and recording metadata, but do not assume the client
listened to audio unless it actually processed it. Refresh the client's tool list
if these tools are missing. Audio retrieval is capped at 8 MiB per request.
For completion notifications, subscribe to line-wide `call.recording` with no
`call_id`, using the existing verified webhook subscription tools or MCP Events.
The event contains state/recording ID, not audio. No Rocky subscription is created
automatically; verify the connection's delivered subscriptions before promising alerts.
Recordings are private: no public signed/share URLs or provider credentials are
returned. Retrieval fetches by validated recording SID from Twilio, ignores any
callback-supplied media URL, checks call ownership, bounds redirects/downloads,
and returns `Cache-Control: no-store`.

Expose signed Twilio endpoints `POST /twilio/recordings/{call_id}` and
`GET /twilio/recording-notice/{en|uk}.wav` through the same public HTTPS origin.
The application supplies their URLs automatically. No extra Console recording
setting is needed. Status callbacks may arrive after hangup and out of order;
completed recordings cannot regress to in-progress. Missing/absent audio is never
reported as a completed recording.

## Storage, cost and acceptance test

Twilio stores the recordings; the local database stores metadata only. Recording
and provider storage have additional charges. There is no automatic retention
purge in this MVP: manage provider retention/deletion deliberately. Transcript
retention and audio retention are separate. Keep backups and browser/MCP access
private; do not publish callers' audio.

Most tests use synthetic audio and mocked Twilio. For real acceptance, have a
consenting person call your mobile, leave it unanswered or decline it, and verify
forwarding, the announced recording, caller identity, personal/formal greeting,
message save, completed callback and private playback. Test direct Twilio calls,
outgoing calls, recording opt-out and a screened handoff separately. Confirm
private owner whispers are absent from caller-leg recordings. No real call is
placed automatically by this feature's tests.

Sources: [Twilio forwarding fields](https://www.twilio.com/docs/voice/twiml),
[recording TwiML](https://www.twilio.com/docs/voice/twiml/recording),
[recording API/media](https://www.twilio.com/docs/voice/api/recording),
[recording consent guidance](https://help.twilio.com/articles/360011522553).
