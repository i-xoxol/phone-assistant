# Appointment verification without a telephone call

Booking uses a separate module, `python -m scripts.simulate_appointment_booking`.
The Live model speaks naturally; priority fast Luna reads the conversation,
chooses from fourteen days of verified free office-hours blocks and interprets
agreement. Python handles the calendar request, current availability, live-call
authority, duplicate prevention and provider result. It does not compare a full
recital or require exact caller wording. Ordinary defaults and vague positive
agreement are accepted; explicit refusal or a changed time is respected.

The booking harness connects the same two real Live peers through the production
bridge. By default it reads the real calendar and simulates one create in private
scratch state. Supply a private scenario with `source_call_id`, `appointment`
(the exact-slot request fields) and caller `script`, then pass `--env-file`,
`--scenario` and a new `--output`. A busy slot remains busy. Results contain the
actual tool calls, Luna action, calendar timings and independently received audio.

One real booking requires explicit owner authorization and backups first, plus
`--execute-approved-booking --owner-grant /private/grant.json`. The owner-controlled
mode-600 grant contains action `one_real_booking_without_telephone`, that
`source_call_id`, the approved `app.appointments.registry.payload_hash`,
`max_events: 1` and a future ISO `expires_at`. It requires zero active calls and a
completed source call. It creates a fresh request; the old call is never reopened
and its expired request is never extended. A write cap accepts the same approved
room with ordinary punctuation/“in person at” variants, and rejects a different
time/name/purpose/room or a second create. Rejected pre-dispatch writes are known
not to have been sent. Any existing operation must be reconciled instead.

Replay speech stays in a private scratch database. The real operation journal and
an honest owner-replay audit event reference the original call; no fake production
call is inserted. Real-mode success also requires an independent Google readback.
No telephone adapter is available in either mode.

For an explicitly authorized first-available replay, set `first_available: true`
and aware `not_before`/`not_after` bounds in both scenario and owner grant. The
calendar cap then accepts the selected free slot within that window at the same
duration; the scenario's historical start/end are a hint rather than a fixed
time. The real booking still performs its fresh office-hours/conflict checks.
This prevents a stale test hint from rejecting a newly available earlier slot.

`python -m scripts.simulate_appointment_lookup` connects two independent real
GPT-Live sessions. One plays the caller from a script; the other uses the normal
`session_config`, Responses backend, `MediaBridge`, caption processing, raw-speech
tracking and native appointment tools. Both exchange G.711 mu-law audio at 8 kHz
in real time, including silence and playback acknowledgments. No Twilio client,
telephone number dial or public webhook is involved.

Use this when the owner asks to simulate an appointment-verification call instead
of making another call. It is an opt-in, billable OpenAI API integration test;
ordinary pytest and CI do not open Live sessions.

Create a **private JSON scenario outside the checkout**:

```json
{
  "first_name": "Casey",
  "last_name": "Example",
  "script": [
    "Hi, my name is Casey Example. I called earlier. I want to verify my appointment.",
    "Could you check again? It's for Casey Example."
  ],
  "timeout_seconds": 120
}
```

To replay the owner's actual script, supply `source_call_id` instead of `script`.
Only that call's **caller** utterances are read. Earlier assistant responses and
calendar results are never fed to the caller model. Supply the caller's correct
first/last spelling separately so it can answer spelling questions naturally.
The caller does not otherwise know the expected date, time or location.

From a checkout of the deployed source, using the existing service virtualenv:

```bash
python -m scripts.simulate_appointment_lookup \
  --env-file /path/to/existing-service/.env \
  --scenario /private/verification-scenario.json \
  --output /private/new-run-directory
```

Each run requires a new output directory. The environment and Google credential
are read in place. The source calls and appointment databases are opened read-only;
all simulated call rows go into separate SQLite files under the private output
directory. The current owner profile, approved facts, matching caller contact,
language, office hours and voice preference are copied into that scratch state.
OAuth records, subscriptions and previous call history are not copied. No service
restart or production configuration change is needed.

The assistant keeps its normal prompt and tool definitions. The harness blocks
booking, message, transfer and other action tools before execution; a separate
HTTP guard rejects calendar mutations. Successful lookups use the real provider
and the normal exact-name, disclosure and fresh-read checks. Nothing inserts a
test appointment, rewrites a name, extends index freshness or resumes an old call.

`report.json` contains both models' input/output transcripts, actual tool arguments
and results, model configuration hash, audio frame count and final usage. Success
requires a verified backend match **and** its date/time/location in the assistant's
speech and the caller's independently transcribed received audio. A tool match,
an unheard response, or an invented spoken confirmation cannot pass alone.
Failures exit nonzero and retain evidence for diagnosis. Caller/assistant session
finalization is also checked. Runs last at most 180 seconds plus bounded cleanup.

Verification that succeeds only after a name loop is a failure too. Reports count
full-name and component/spelling questions and reject repeated spelling cycles or
a full-name question after the requested spellings were already supplied. A normal
initial name intake plus one necessary spelling clarification remains allowed.

`--save-audio` retains both generated raw `.pcmu` streams for diagnosis. Those are
private test artifacts, not repository fixtures. Keep scenarios, audio, SQLite
files and reports out of Git. Use synthetic names in public regression tests.

This exercises real speech recognition, full-duplex interruptions and tool use.
The simulated caller has a generated voice; it does not reproduce the owner's
accent or a handset/Twilio network path. Results can vary between model runs.
Inspect all four transcript streams when diagnosing a failure, especially caller
output versus assistant input and assistant output versus caller input.

The simulated caller may hang up after independently hearing every verified detail
and saying thanks or goodbye. The harness waits for two seconds of caller quiet
and empty audio playback queues, then sends an in-memory media stop and finalizes
both sessions. This models a caller hangup without a telephone action. Missing or
unheard details, blocked actions and repeated name loops cannot trigger it.
