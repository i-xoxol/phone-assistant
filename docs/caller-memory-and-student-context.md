# Caller language, useful context and model choices

[← README](../README.md) · [Saved context](concierge-context.md) · [Subscription lookup](subscription-lookup.md)

## Remembering a language

English is the default. Existing owner-set contact preferences remain in effect.
When the actual caller explicitly asks for another language, the voice switches
and delegates `remember_caller_language(language, request_quote)`. The server checks
the quote against recent caller captions, binds the change to this incoming call,
and saves the language against its presented number. It never accepts a different
phone number from the voice backend. A later request replaces the choice.

Language memory works for an unknown number without inventing a contact name or
granting identity/disclosure rights. It survives restarts, call transfers and later
calls. A blocked number or ambiguous forwarding identity gets a preference for
this call only. Shared/recycled numbers share a preference until it is changed.
If captions cannot confirm the request, the voice can still switch but cannot
claim to have remembered it; it can ask for the request again.

The caller book has a language code and source (`caller` or `owner`). At most
1,000 number preferences are stored; the portal shows the first 200 directory rows.
Curated contact names, nicknames and familiar/formal styles stay separate.
Prepared recording notices still use cached files: Ukrainian for a saved Ukrainian
preference, otherwise the prepared English notice. There is no per-call TTS notice.
Short local startup greetings cover English, Ukrainian, Spanish, French, German,
Polish, Russian, Portuguese, Italian, Arabic, Chinese, Japanese, Korean and Hindi.
Other codes retain the requested conversation language but use a brief English
startup fallback so silent callers still hear a greeting. No translation API is
called to build these fixed phrases.

Rocky and other owner-authorized agents can:

- Read `search_call_knowledge()` and its `caller_preferences` directory.
- Call `set_caller_language(phone_number, language)` to edit a preference directly;
  `en` restores English, `uk` selects Ukrainian, `es` selects Spanish, etc.
- Continue using `set_call_contact(..., preferred_language=...)` for curated contacts,
  preserving the existing contact ID/name/number and other preferences.

The owner can also edit a language at **Caller book · Languages** in `/live`.
REST uses `PUT /concierge/caller-language` with the owner token. Portal writes use
`PUT /live/caller-language` with the existing cookie, configured Origin and CSRF
token. Owner edits notify a matching current voice call. MCP clients that cache
tool discovery may need a refresh to expose `set_caller_language`; the existing
contact tool remains usable for known contacts.

## Strategy for activities and student callers

Keep a small phone-safe working brief, rather than copying private inboxes,
calendars or student records into the voice prompt.

| Layer | Rocky's tool | What belongs here | Freshness |
|---|---|---|---|
| General role and workflow | `set_inbound_context` | Approved activities, how to handle advising/course calls, whom to ask | Review when role/workflow changes |
| Reusable facts | `set_call_fact` | Verified public course FAQs, approved contact instructions, published event details | Explicit source and expiry, at most 90 days |
| Anticipated calls | `expect_incoming_call` | Expected topic, relevant approved background, questions to ask, optional number hint | At most 7 days; retire when resolved |
| Available times | `set_inbound_availability` | Verified free windows, time zone and observation time | At most 1 hour; existing exporter can refresh |
| Personalized action | `request_supervisor` → Rocky | Missing information, actual booking, exceptions needing the owner | Verify against the active request/deadline |

For example, an owner-approved general brief might say: “For student calls, ask
which course or section and what help is needed. Use current approved FAQs. If
the answer is unavailable, take a concise message for the instructor.” A short
fact could be: “For advising calls, have your proposed course plan ready.” These
are workflow examples, not claims about the deployment owner's current classes.

Rocky should first read the current context, preserve unrelated instructions,
and ask the owner or consult an already authorized source before adding actual
courses, academic office hours, dates, deadlines or activity facts. Update the
brief at the beginning of a teaching period and whenever details change. Keep
short-lived expected-call notes for an advising period, an event or a specific
callback; reuse IDs when updating and retire obsolete notes. Automatic free/busy
refreshes remain independent and must not overwrite role/FAQ/call notes.

During a call the assistant asks the purpose, then uses local
`get_call_context(question)` to retrieve relevant approved facts and anticipated
notes without waiting for Luna. It answers from those facts, asks a useful
clarifying question if necessary, and requests Luna/Rocky for an uncertain match
or missing answer. It does not invent a reason for a delay or repeatedly stall.

The caller's number is not evidence that they are a student. The phone brief
does not contain grades, attendance, accommodation details, other students'
information or private calendar titles. Course changes, grading decisions,
extensions and exceptions are not authorized by caller speech. The assistant
can take a message and request verified owner/supervisor guidance. Phone-line
operating hours are distinct from academic office hours.

## Luna Fast and the voice model

Official documentation checked October 19, 2026:

- **Voice stays `gpt-live-1`.** It is OpenAI's current premier full-duplex voice
  model, with natural interruption handling and independent backend delegation.
  Keeping it is a fit/compatibility choice, not a claim that every other voice
  model has been benchmarked. [Model reference](https://developers.openai.com/api/docs/models/gpt-live-1)
- **Fast is a processing mode for the same `gpt-6-luna`.**
  `OPENAI_BACKEND_FAST=true` requests `delegation.responses.service_tier="priority"`
  and Luna uses low reasoning effort. Availability depends on the API project.
  [Live delegation](https://developers.openai.com/api/docs/guides/live-delegation#configure-responses-delegation)
- **`SUBSCRIPTION_LOOKUP_FAST=true`** requests Fast for all existing subscription
  Luna uses: pre-answer preparation, history/fact lookup and private coaching.
  Each CLI invocation remains isolated, ChatGPT-authenticated, read-only, with
  executable tools disabled and existing deadlines/usage caps. There is no silent
  API-key fallback or purchase of credits. Fast uses **2.5× included subscription
  allowance**, while purchased/Enterprise pay-as-you-go credits use 2×.
  [Speed and billing](https://learn.chatgpt.com/docs/agent-configuration/speed#fast-mode)

For short-context API Luna, Standard is $0.10/M input and $0.50/M output; Fast is
$0.20/M input and $1.00/M output. A hypothetical 3,000 input + 200 output token
request therefore costs approximately **$0.0004 Standard or $0.0008 Fast**,
excluding cache writes/tools and other usage. These are API examples, not charges
for the subscription-backed worker. Live voice is separately $0.05/minute,
billed per second; Twilio remains separate. [Official pricing](https://developers.openai.com/api/docs/pricing)

Fast can reduce processing/queue delay; it does not eliminate CLI startup,
transcription, speech-generation, network or playback delays. Local factual reads
avoid that model trip entirely. Measure on the installed account with:

```bash
python -m scripts.check_subscription_lookup --compare
python -m scripts.check_voice_tools --language --fast
```

Both use synthetic data and never dial a telephone. The first compares one
Standard and one Fast lookup, not a statistical latency guarantee. The second
executes the real API-delegated language tool against an isolated database.
For a telephone acceptance test, call from a consenting test phone, request a
language, hang up and call again. Check the greeting and caller-book preference,
then use `set_caller_language(..., "en")` to test the owner override.
