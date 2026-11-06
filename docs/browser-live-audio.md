# Listen live and join from your browser

[README](../README.md) · [Browser test calls](browser-tests.md) · [Live monitor](live-monitor.md)

Open your paired owner control page, select an active call, and use **Listen
live**. You hear both the caller and assistant without granting microphone
access. Select **Join with microphone**, allow the browser permission, and wait
for the **Microphone live** indicator. The caller can now hear you alongside the
assistant. The assistant receives your words as separately labelled owner
captions after each utterance.

- **Mute microphone** keeps listening on. Unmute when ready to speak.
- **Leave microphone** releases the device and returns to listening.
- **Stop listening** releases all browser audio. The telephone call continues.
- **Hang up call** ends the call for everyone.

The audio session card stays visible while navigating Contacts, Rules and
Settings. Selecting a different call does not move an open microphone to it:
stop listening first, then listen to the new call. Closing/reloading the tab,
losing its connection, signing out or losing owner authorization stops its audio.
Use headphones to prevent acoustic echo. Browser microphone and autoplay rules
require an explicit button click and HTTPS (localhost also works).

## What is supported

Live audio works during the assistant's active bidirectional voice bridge, for
incoming, forwarded and outgoing calls with all supported voice backends. It
also works with browser test calls: open the call in a **second paired browser
tab** to listen or join, while the first tab plays the fictional caller. The test
caller tab already owns its microphone, so it cannot simultaneously join itself.

Ringing, announcements before the voice bridge, keypad navigation and screened
telephone owner handoffs have no browser audio bridge. Listening disconnects
when that bridge closes. Telephone owner coaching is a separate feature; browser
joining does not enter its private coaching channel. Up to four owner listening
connections and one browser microphone are allowed per active assistant bridge.

Listening uses the existing voice stream and does not start a recording or a
second phone call. Joining additionally requires `OPENAI_API_KEY` and the existing
`OPENAI_TRANSCRIPTION_MODEL` setting (default `gpt-live-transcribe`) for owner
speech transcription, even if the assistant uses ElevenLabs or Gemini. It incurs
that provider's transcription usage. If transcription cannot start or fails,
the microphone is stopped and listening can continue; rejoin to retry.

## Audio and identity

The server taps inbound caller audio and outbound assistant audio into bounded,
call-specific listener queues. A slow listener disconnects without delaying the
phone. No listening replay/archive is created. Existing configured telephone
recording behavior is independent; browser listening is not a claim that owner
speech appears in every carrier recording.

After the first join, one 20 ms paced writer mixes owner and assistant audio to
the caller. A cleared assistant response drops its queued audio and playback
marks while retaining current owner input. Telephone playback acknowledgments
continue to drive the existing bounded output queue and goodbye drain.

Owner raw audio **does not enter the caller's model-input channel**. It goes to a
separate transcription connection, is saved in the call transcript as **You**,
and is delivered to the assistant as authenticated owner context. This preserves
caller-only audio timing, booking evidence and consent checks. It introduces
caption delay; final words may be incomplete if the microphone is disconnected
before transcription finishes. Calendar-controlled voice paths retain their
existing verified-response rules. Speaking does not grant disclosure or booking
permissions or bypass the existing approval controls.

The joiner hears the caller and assistant, not an electronic echo of their own
microphone. Other paired listeners also hear the joined owner's audio. Live
listening is near real time, not a sample-perfect record of what the telephone
recipient heard.

## Deployment and verification

No additional public port or Twilio route is needed. The HTTPS proxy must allow
long-lived WebSocket upgrades on `/live/calls/{call_id}/live-audio`; the same
origin and cookies as `/live` are used. See the [VPS example](vps-example.md) and
[native proxy configuration](native-vps.md#https-and-streaming-proxy).

`GET /live/calls/{call_id}/live-audio` returns owner-authenticated availability.
The WebSocket requires an origin-bound owner session and an initial
`{"event":"start","csrf":"<current browser CSRF>"}` message. Subsequent `join`,
`leave` and bounded PCMU `media` messages stay bound to that call. Sessions are
rechecked during listening, not only when the socket opens. This is a private
browser transport, not a new public MCP audio tool.

For a deployment check, start a fictional browser test in one tab and use another
paired tab to listen, join, mute, leave and stop. Verify both audio sources,
**You** captions, the microphone indicator after changing pages, and that stopping
listening leaves the test running. Test actual phone audio separately under your
own account before relying on carrier playback quality.

The implementation follows Twilio's supported bidirectional
[media, mark and clear protocol](https://www.twilio.com/docs/voice/media-streams/websocket-messages).
Twilio provides the caller's inbound track; the application already owns the
assistant output and mixes the owner channel before sending it to Twilio.
