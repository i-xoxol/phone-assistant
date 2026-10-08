# Testing and evidence

[← README](../README.md) · [First-call setup](getting-started.md) · [Troubleshooting](troubleshooting.md)

## Offline suite

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Twilio and Live connections are mocked. Tests require no real credentials and
do not place telephone calls or contact paid models. The initial public export
passed **93 tests** on the development host before additional release-audit tests.
CI validates Python 3.11, 3.12, and 3.14 in clean Linux environments.

| Area | Behavior covered |
|---|---|
| Calls | Creation, invalid numbers, provider failure, busy/no-answer, completion, persistence, retrieval |
| Provider callbacks | Signature/account/call checks, ordering, terminal states, signed media diagnostics |
| Voice | Two-way relay, codec binding, transcript timing, finalization, large bursts, stalled playback, failure races |
| Ending | Farewell recognition, final playback acknowledgment before hangup, owner audio-blocking behavior |
| Keypad | Allowed inputs, current ownership, deliberate handoff, reconnection, uncertain-result behavior |
| Updates | Byte limits, queueing, idempotency, acknowledgment, rejection, restored context, authorization limits |
| MCP | Tool inventory/retrieval, owner authentication, OAuth pairing, refresh/resource handling, protocol paths |
| Browser | Pairing, cookie/origin/CSRF, SSE wakeup/replay, duplicate suppression |
| Events | Signed challenge, filtering, expiry, ownership, retries, persistence, delivery counters, DNS pinning |

Tests assert meaningful transport and permission behavior; simulated playback
does not prove how a real phone sounds. Run manual acceptance on your accounts.

## Release checks

```bash
python -m scripts.audit_release
python -m scripts.package
```

The audit checks tracked release paths/credential patterns. Packaging uses a
source allowlist and excludes runtime data. Inspect the generated ZIP members
before sharing a customized build; a local plugin URL can still identify your
instance. Archives are ignored in Git.

The workflow runs only offline tests and the audit. No provider keys are stored
in GitHub Actions, and no network call script is in CI.

## Optional network checks — no telephone dialing

These use your own configured account and may incur OpenAI usage:

| Command | Check |
|---|---|
| `python -m scripts.preflight --network` | Twilio auth/owned number, public health, Live start/finalization |
| `python -m scripts.check_voice_hangup` | Actual Live farewell with simulated Twilio playback/ending |
| `python -m scripts.check_voice_tools --keypad` | Actual delegated keypad request with a simulated telephone gateway |
| `python -m scripts.check_call_updates` | Actual append/acknowledgment modes with simulated audio |
| `python -m scripts.check_live_page` | Public browser login and SSE heartbeat; temporary session revoked |
| `python -m scripts.check_mcp` | Public OAuth MCP catalog and saved retrieval; temporary diagnostic grant revoked |

Run checks **on the service host** when they need to use its database and
pairing state. `check_mcp` reads the latest call if one exists but avoids printing
its transcript; `inspect_call_test` is a local private-data inspection helper
and can print the brief, so do not publish its output.

The voice-hangup check optionally reads a local `data/local-ending-cue.wav`
(8 kHz mono PCM16). No such real recording is distributed. Without it the script
uses its application completion cue. Results are stored in ignored `data/`.

## Manual telephone test

Use the exact steps in [getting started](getting-started.md#7-make-one-consenting-test-call).
The command below **does dial**:

```bash
python -m scripts.manual_call --to +12025550101 --recipient "Consenting test recipient"
```

Replace the fictional number only after agreement from the recipient. The script
places one call, never automatically retries creation, polls provider state,
requests hangup on interruption/timeout where the call ID is known, and saves a
private result. Check both audio and result diagnostics.

For complete acceptance, conduct short separate tests of interruption, assistant
farewell, direct monitor hangup, menu navigation, and a live context update.
Compare injected acknowledgments with actual transcript use. With webhook
monitoring, verify both receipt counters and observed receiver/client handling.

The underlying prototype has been used on real calls, including an existing
appointment confirmation through a business menu. That establishes experience
with the integration, not a guarantee of new account/model/tunnel behavior. No
personal call transcript or recording is included as test evidence.
