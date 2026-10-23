# Appointment verification without a telephone call

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

`--save-audio` retains both generated raw `.pcmu` streams for diagnosis. Those are
private test artifacts, not repository fixtures. Keep scenarios, audio, SQLite
files and reports out of Git. Use synthetic names in public regression tests.

This exercises real speech recognition, full-duplex interruptions and tool use.
The simulated caller has a generated voice; it does not reproduce the owner's
accent or a handset/Twilio network path. Results can vary between model runs.
Inspect all four transcript streams when diagnosing a failure, especially caller
output versus assistant input and assistant output versus caller input.
