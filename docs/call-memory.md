# Callback memory and delayed pickup

The service can recall prior calls to or from the exact incoming E.164 number.
It uses existing local SQLite records, not an external number directory.
Enable it on your private service host:

```dotenv
CALL_HISTORY_ENABLED=true
CALL_HISTORY_DAYS=90
SUBSCRIPTION_LOOKUP_ENABLED=true
INBOUND_ANSWER_DELAY_SECONDS=18
```

The worker requires the existing ChatGPT-signed-in Codex CLI with GPT-6 Luna.
Twilio and voice/Responses remain separately billed. The distributed settings
leave history and answer delay off by default.

## Recall behavior

For a new incoming call, the server finds up to five earlier terminal calls
within the configured window, in both directions. It excludes the current call,
later calls, withheld/invalid numbers and every other number. Initial metadata
contains dates, direction, status and duration. An unanswered outgoing call
retains its original objective, without claiming a conversation happened.

A bounded Luna lookup starts in the background when matching history exists.
It selects exact excerpts: the previous objective, up to six recent conversation
turns or a confirmed saved message. GPT-Live can request another lookup after
the caller explains their purpose. Excerpts carry source dates and call IDs and
are rechecked before injection. There is no unbounded full-transcript replay.

Results enter as internal context, not an instruction to read history aloud.
The assistant asks the caller's purpose and uses minimal relevant details.
Old availability, appointments and instructions do not become current facts or
authorization. Follow-up information is a new caller statement that can be saved
as a confirmed message. Message-only calls can recall history to clarify a
callback but gain no supervisor, booking or owner-contact tools.

Obvious secret/contact patterns and answers to sensitive questions are filtered
before the lookup. Filtering is not comprehensive; voice instructions also
enforce minimal disclosure. Caller ID is not identity: numbers may be shared,
spoofed or reassigned. Do not volunteer private history, credentials, birth dates
or sensitive medical/financial details. Owners can inspect full sources separately.

The existing twenty-second deadline, pending-request deduplication and per-call/hour
limits apply. The greeting does not wait for Luna. Unavailable lookup means asking
the caller to recap or taking a message. In human-call whisper mode, the same
evidence can inform a private note. Transcription-only mode starts no coaching model.

## Owner history reader

MCP `get_contact_call_history` accepts:

```json
{"phone_number":"+12025550101","limit":5}
```

It returns bounded redacted excerpts and call IDs. The configured window defaults
to ninety days (range 1–365). Bearer-protected REST offers
`GET /calls/history?phone_number=%2B12025550101&limit=5`. No call is placed. Use
`get_call_result` with a returned ID for the owner's full private source. Disabling
voice history does not delete records or prevent owner history inspection.

## Approximately three rings

The initial incoming TwiML starts with `<Pause length="18"/>`, before `<Say>` or
`<Connect>`. A pause as the first verb delays pickup; it is not answered-call
silence or a slow webhook handler. Eighteen seconds approximates three US ring
cycles; carrier cadence, initial ringing and forwarding can differ. Set 0 for
immediate pickup, or another value up to thirty seconds.

The delay also precedes closed-hours announcements. It never delays outgoing calls,
owner screening or reconnecting an existing conversation. See Twilio's
[official delayed-pickup example](https://www.twilio.com/docs/voice/twiml/pause#example-2-delayed-pickup).

## Verification

`pytest` covers number isolation, unanswered callbacks, follow-up messages, age
limits, redaction, source changes, internal context, MCP/REST access and TwiML
ordering. `python -m scripts.check_call_memory` uses actual GPT-Live and subscription
Luna against a synthetic unanswered call, with a gateway that cannot dial. Add
`--caller-wav synthetic.wav` (mono PCM16 at 8 kHz) to verify the spoken callback
answer; without it the check verifies acknowledged history injection. It
prints booleans, not transcripts or credentials. A real incoming test is still
needed to assess the carrier's audible ring count.
