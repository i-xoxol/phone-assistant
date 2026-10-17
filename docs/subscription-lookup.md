# Quick background lookups with GPT-6 Luna

Optional [callback memory](call-memory.md) also lets Luna select exact excerpts
from number-matched prior incoming/outgoing calls. These are historical claims,
not approved current facts; they enter internal context with separate disclosure
and authorization rules. Unrelated numbers' calls are excluded.

The incoming concierge has three ways to obtain information:

| Need | Path | Authority |
|---|---|---|
| Default role, fresh free windows, anticipated-call notes | Local `get_call_context` | Offer verified windows; no reservation |
| Approved background facts about a topic or contact | Optional GPT-6 Luna evidence selector | Read-only facts; no actions |
| Missing/private information, unusual hours, any booking | Connected supervising agent such as Rocky | Verify with authorized tools; complete and verify booking before confirming |

The voice conversation continues while a lookup runs. It can ask a useful
clarification and say it is checking. It must not invent progress or keep callers
waiting indefinitely. A supervisor request can run alongside the quick lookup.
Luna can also recommend owner involvement for a complex issue. That recommendation
does not dial: the voice asks whether the caller wants to hold, then may use the
separate [screened owner-contact tool](voice-and-owner-contact.md). Its fixed-number
server policy still applies, and no booking/payment authority is added.

## Billing and prerequisites

This optional worker uses **`gpt-6-luna` through the Codex CLI signed in with
ChatGPT**, consuming the account's Codex subscription allowance. It does not
reuse a ChatGPT token as an API key and has no API fallback. Twilio, GPT-Live and
the existing Responses delegation backend still incur their normal API charges.
An existing `OPENAI_BACKEND_MODEL=gpt-6-luna` does not make that backend subscription-billed.

Install the official Codex CLI on the private service host, complete `codex login`
as the service account, then check `codex login status`. A tested baseline is
Codex CLI 0.163.0 on Linux, with access to GPT-6 Luna. This integration requires
that version's flags/configuration; failures fall back to the supervisor/message.
Keep the login cache private. No tokens or login caches belong in this repository,
release archives, public CI, callback payloads or browser pages. Do not change an
existing global Codex configuration for this worker.

```dotenv
SUBSCRIPTION_LOOKUP_ENABLED=true
SUBSCRIPTION_LOOKUP_TIMEOUT_SECONDS=20
```

Restart the single service after changing configuration. The worker uses
`codex exec --ephemeral --ignore-user-config --ignore-rules`, ChatGPT-only
authentication, a fresh temporary working directory, read-only sandbox and disabled
shell, apps, plugins, browser/computer, image, subagent, memory, hooks and other
executable capabilities. It excludes API keys, URL overrides and phone-service
credentials from the child environment. Personal/project instruction loading and
bundled skill instructions are disabled. Host-managed policies still apply.
Unexpected executable-tool events cause termination and no answer is delivered.

These runs are private, trusted automation, not a public agent execution endpoint
or an open-source CI workflow using someone's ChatGPT login. See the official
[non-interactive guidance](https://learn.chatgpt.com/docs/non-interactive-mode) and
[authentication guidance](https://learn.chatgpt.com/docs/auth).

## Curated directory and facts

There is no automatic address-book, email or private-file import. The owner or an
authorized agent maintains a small SQLite directory through authenticated MCP/REST:

- `set_call_contact(name, phone_number?, contact_id?, preferred_language?, preferred_name?, conversation_style?)`: the number
  follows the owner's saved-number recognition policy. Reuse IDs for updates and
  retries. A language such as `uk` starts known callers in Ukrainian; omit to preserve
  the current preference, or use an empty string to clear it.
- `set_call_fact(topic, text, source, expires_at, approved_for_callers, contact_id?, fact_id?)`:
  one short verified fact, **at most 250 UTF-8 bytes**, expiring within ninety days.
  Explicit `approved_for_callers=true` is mandatory.
- `search_call_knowledge(query?)`: owner view, including contact IDs and routing
  numbers; up to fifty matching current facts.
- `retire_call_fact(fact_id)`: withhold an obsolete fact, including from work still
  running. The private audit record remains.

**Every fact is approved for disclosure to unverified incoming callers.** Even a
contact-specific fact can be disclosed to someone who simply mentions that name.
Caller ID and claims of friendship are never authentication. Store no sensitive
identifiers, passwords, payment details, private calendar contents or confidential
contact information. Use Rocky's supervised path when identity or permission matters.

Facts are kept separate from profile, anticipated notes and the calendar exporter.
This prevents an automatic availability refresh from replacing the directory.
Store dynamic availability through `set_inbound_availability`, not long-lived facts.
Retire rather than relying on a newer fact to implicitly override an older ID.

Example, using fictional details and a future timestamp within ninety days:

```json
{
  "topic": "Project demonstration",
  "text": "The demonstration keyword is lavender.",
  "source": "Explicit owner instruction",
  "expires_at": "<ISO-8601 timestamp with UTC offset>",
  "approved_for_callers": true
}
```

MCP tool `set_call_fact` accepts that object; use its returned `fact_id` to revise
or retire it. Optional `contact_id` associates it with a curated contact. Names and
numbers do not grant the model access to other tools.

## What happens during a call

1. GPT-Live delegates a missing background question to `request_quick_lookup`.
2. Server code retrieves at most twelve relevant unexpired approved facts using
   bounded literal matching. Contact facts require a name/query or number hint.
   Routing numbers and unrelated transcripts never enter the lookup prompt.
3. Luna returns only up to two existing evidence IDs and a `needs_supervisor` flag.
   The server never accepts model-authored facts, SQL, code or action instructions.
4. The server verifies the IDs, rechecks the records for expiry/change/retirement,
   and injects the **exact stored facts** through the active reply channel into
   the same voice session, so the voice can answer the pending question aloud.
5. No match, uncertainty, capacity, timeout, sign-in or model failure means
   supervisor help or a saved message. No booking is inferred from an answer.

Only one worker runs at a time, with at most six runs per incoming call, twenty
per hour and a twenty-second default deadline. The child process is terminated
on timeout/cancellation. Completed calls or replacement voice sessions reject late
answers. Restarted pending work is marked unavailable. These limits bound usage;
subscription availability is not an unlimited service guarantee.

`get_call_result.lookup_requests` records state, selected evidence IDs, delivery
state and sanitized error codes. `sent`/`acknowledged` does not prove the caller
heard a fact. Transcripts and direct hangup remain on the existing private monitor.
Lookup requested/completed diagnostic events appear on its SSE event stream;
Rocky's native supervision subscription remains `call.supervisor_requested`.

## Verify without dialing

```bash
pytest -q
python -m scripts.check_subscription_lookup
```

The second command uses the real signed-in CLI with a synthetic fact and reports
elapsed time. It never calls Twilio. A local CLI-only probe measured roughly four
seconds; total spoken-answer latency includes Live delegation and audio and must
be checked with an actual incoming call. No real-call latency guarantee is implied.
