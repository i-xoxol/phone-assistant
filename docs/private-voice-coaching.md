# Natural private voice coaching

After accepting a transfer with **1**, both human microphones are transcribed.
Subscription Luna selects up to two current facts, including fresh cached owner
availability. OpenAI generates a short clip only when needed. Twilio plays it to
the owner's participant only. Press **3** for captions without Luna or speech.

```dotenv
OWNER_SPEECH_ENABLED=true
OWNER_SPEECH_MODEL=gpt-realtime-2.1-mini
OWNER_SPEECH_VOICE=cedar
CALLER_ID_TRUST_SAVED_CONTACTS=true
```

This uses the supported token-billed Realtime model for text-to-speech. It receives
only selected text, no microphone, tools or call history, and closes after generating
audio. No GPT-Live session stays open during the human call. The old
`gpt-4o-mini-tts` retires January 6, 2027 and is not used here. Owner acceptance
announces that private AI voice coaching is on.

Address **Assistant**, **Rocky** or **Luna**, for example: “Assistant, what is my
Monday availability?” Only completed captions from the accepted owner leg trigger
explicit owner requests. The OWNER is the configured account owner; the incoming
CALLER is a different participant. Owner “my schedule” means owner availability.
The caller can still hear your spoken requests; only the assistant reply is private.

Luna can also select an occasional useful current fact. Raw previous-call speech
is never directly spoken as a coaching answer. Evidence is rechecked after
selection, after generation, and before serving audio. Repeated announcement
callbacks are claimed once. A newer owner question supersedes an answer still
being prepared. Missing information produces an honest short notice, not an old
transcript excerpt. This worker reads cached context; it cannot perform fresh
calendar searches, bookings or wake Rocky after transfer. Rocky can refresh the
existing cached context through owner tools.

At most 12 selection attempts and 8 notes per transfer are permitted. Automatic
notes have a 20-second attempt cooldown and 25-second announcement spacing;
explicit owner requests can follow after five seconds. Speech is bounded to 30
seconds per clip. WAV data is held in memory for at most 60 seconds and removed
when the call ends. A restart loses pending audio safely.

Expose `/twilio/owner-transfer/{transfer_id}/audio/{whisper_id}.wav` with the other
Twilio routes. Its GET requires a Twilio signature and checks the current transfer,
accepted coaching mode, claimed announcement and current evidence. Responses use
`Cache-Control: private, no-store`. Audio is not saved as a recording. Failed
generation reports unavailable coaching while leaving the human call connected.

## Saved-number recognition

`CALLER_ID_TRUST_SAVED_CONTACTS=true` makes an exact unique saved-number match
the caller identity for ordinary recognition. Owner and caller names stay separate.
With it off, saved names are possible matches. Unknown, missing and ambiguous
numbers do not become known identities. This policy grants no additional payment,
booking or private-record disclosure authority.

Contacts can also have a `preferred_language`, such as `uk` for Ukrainian or `en`
for English, through `set_call_contact` or the existing contact REST endpoint.
Omitting it preserves the saved preference; an empty string clears it. A trusted
known caller gets a greeting in that language from the first word and can
switch languages during the conversation. Relationship labels such as Mama refer
to the owner's family; the assistant does not impersonate a family member.

Use `preferred_name` for a spoken nickname and `conversation_style="familiar"`
for the owner, friends and family. Familiar callers get warm first-name greetings
and relaxed conversation throughout the call. `conversation_style="formal"`
uses a professional office greeting; this is the default for other saved contacts
and unfamiliar callers. Familiar style requires a unique trusted saved-number
match. A saved business is not automatically treated as a friend.

For example, update an existing contact through MCP (reuse its ID and number):

```json
{"contact_id":"existing-contact-uuid","name":"Anne Example","phone_number":"+12025550101","preferred_name":"Annie","conversation_style":"familiar"}
```

Omitted preferences are preserved; an empty `preferred_name` clears the nickname.
Rocky can read/update these through `search_call_knowledge` / `set_call_contact`;
REST uses `PUT /concierge/contacts`. Preferences apply at the next voice-session
startup. Refresh a client's tool catalog if the new optional arguments are missing.
The formal opening uses `office_name` from `set_call_hours`, shared with the closed
announcement; set your own office title there. The generic default adds no degree
or professional title. Returning from handoff keeps the tone without repeating the
introduction. Closed-hours message-taking and action permissions still apply.

During a configured pre-answer delay, a best-effort Luna task selects up to two
relevant context items before the voice session starts. It shares the existing
lookup concurrency/hourly allowance and finishes before the delay deadline.
Pickup is never delayed for that task. If it times out, the saved caller identity
and language still personalize the greeting. Historical speech remains internal
background and is not recited in a greeting. Zero-delay calls skip this preparation.

## Cost and testing

At the October 17, 2026 prices, OpenAI's selected model costs $20 per million output
audio tokens plus text usage. Three ten-second notes cost about $0.012 for generated
audio at roughly 20 tokens per second. Twilio/transcription remain separate;
Luna consumes the signed-in subscription allowance.

Google Gemini 3.8 Flash-Lite TTS lists $6 per million audio tokens and 25 tokens per
second: three ten-second clips are about $0.0045 before text charges. That promotional
price ends December 31, 2026. This implementation uses OpenAI; Google account access
does not imply API billing access, and no Google integration is enabled automatically.

Sources: [OpenAI prices](https://developers.openai.com/api/docs/pricing),
[audio accounting](https://developers.openai.com/api/docs/guides/voice-latency-cost),
[deprecation](https://developers.openai.com/api/docs/deprecations#2026-10-01-text-to-speech-models),
[Google prices](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.8-flash-lite-tts),
[Twilio participant announcements](https://www.twilio.com/docs/voice/api/conference-participant-resource).

Offline checks: `pytest -q`. Provider-only check:
`python -m scripts.check_owner_coaching --speech` uses actual subscription Luna and
paid OpenAI speech with a synthetic fact and a fake phone. It never dials.
For real playback, call from a second phone, accept with 1, request owner availability,
and confirm only the owner hears the note. Repeat with 3 to verify coaching stays off.
A fake telephone check does not prove real audio isolation.
