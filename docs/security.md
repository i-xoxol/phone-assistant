# Security, authorization, and privacy

[← README](../README.md) · [SECURITY.md](../SECURITY.md) · [Configuration](configuration.md)

This project is designed as a **private single-owner deployment**. The source
is public; your working instance, credentials, and call history should remain
private. These are implementation boundaries, not a guarantee against every
model mistake or a replacement for reviewing a call objective.

## Authorization model

The initial voice prompt carries identity, objective, relevant context, approved
disclosure, restrictive constraints, uncertainty handling, and ending behavior.
It permits information gathering and approved basic caller information. It
prohibits payments, charges, purchases, contracts, irreversible cancellation,
credentials, and highly sensitive disclosures. “Handle this for me” does not
grant financial or contractual authority.

An ordinary appointment can be confirmed only when the **initial brief** explicitly
allows booking within its supplied limits and involves no new charges, contractual
terms, or treatment consent. Rescheduling likewise needs initial authorization.
Mid-call updates refine facts or availability; they do not expand permissions.

The assistant introduces itself as a personal assistant, does not impersonate
the owner or claim to be human, and answers honestly if asked whether it is AI.
Adapt identification/disclosure language for your use case and applicable rules.

### What is enforced by code

- Signed Twilio callbacks/upgrades with account and call binding.
- REST bearer authentication and owner-paired OAuth/browser access.
- UUID/input validation, allowed keypad characters, duration/update/tool limits.
- Current bridge ownership and guards against competing handoffs or stopping calls.
- Atomic duplicate tool claims and idempotent context update IDs.
- Only two voice-backend functions: navigation digits and ending this same call.
- Provider duration limit and bounded playback/finalization waits.

### What is primarily prompt-based

- Whether speech reveals an inappropriate fact supplied in context.
- Whether a spoken commitment stays within authorization.
- Whether the model interprets a menu accurately or chooses the correct key.
- Whether a text update is useful, understood, or communicated correctly.

There are no payment/contract tools, but the content of an ordinary conversation
can still matter. No live approval queue or deterministic semantic checker is
implemented. Stronger approval mechanisms can be added in `CallManager` and
`VoiceTools.operation` before side effects, with a separate UI/client workflow.

## Data flow

| Data | Where it goes |
|---|---|
| Destination/caller numbers and telephone metadata | Twilio and local call records |
| Voice audio | Twilio and OpenAI over their network connections; no local recording |
| Objective, context, constraints, approved identity | OpenAI Live prompt and local SQLite |
| Delegated task context | Configured OpenAI Responses backend when enabled |
| Transcript fragments | Local SQLite, paired monitor, authorized MCP/REST clients |
| Subscribed transcript/status events | Only the verified receiver for that subscription |
| Generic event feed | Operator's configured webhook, if enabled |
| OAuth/browser state and signing secrets | Private local SQLite databases |

`store=False` is supplied to the Live session. It is **not** a promise of zero
provider retention under all account policies. Review provider terms/data controls
for your account. The repository contains no Twilio audio recording configuration;
Calls API requests set `record=False`.

Transcripts, reasons, and updates may contain personal information. Keep context
minimal. Do not add passwords, payment details, whole email threads, full calendar
entries, or unrelated personal facts merely because they are available elsewhere.
Only send facts authorized for that conversation.

## Access controls

REST compares a secret bearer token in constant time. MCP uses an OAuth grant
with `phone.calls` and an exact resource URL. Its approval page requires a code
generated on the host. Approved clients have broad access to the one owner's
calls; there are no per-client/per-call data partitions.

Browser codes are one-use and short-lived. Sessions use Secure, HttpOnly,
SameSite=Strict cookies. Writes verify the configured origin and CSRF token.
The monitor sends a restrictive CSP, no-store caching, no-referrer, and anti-frame
headers. A paired browser can generate a code for another device.

The app has a small browser-login failure throttle and high-entropy pairing
codes; it does not implement a full edge abuse/WAF/rate-limiting service. Only
approve trusted clients. Dynamic registration itself is not a call grant.

Twilio callback signatures are checked against `PUBLIC_BASE_URL`, and media
start messages must match the account, call, mono μ-law codec, and sample rate.
Arbitrary proxy headers do not become trusted signature origins.

## Storage and logs

SQLite databases are **not encrypted** by this app. Pairing/session lookup
values use hashes, and OAuth pairing codes use a salted slow hash. OAuth database
payloads and webhook signing secrets remain sensitive and may contain recoverable
credential material. Do not assume all database contents are irreversibly hashed.
Use owner-only filesystem permissions and private/encrypted backups as needed.

Structured logs contain identifiers, state transitions, safe error codes, exception
types, and stack locations. They intentionally omit transcript bodies, SDK raw
messages, credentials, and callback URLs. Access logging is disabled in the
documented server commands to avoid unnecessary URLs. Do not turn on SDK debug
logging or post entire private runtime logs to a public issue.

Records currently persist until the operator removes/rotates them. There is no
automated retention scheduler. Restarts do not recover a conversation or remove
old credentials. Plan storage protection and retention for your deployment.

## Webhook trust

MCP user-supplied callback URLs are verified with a signed challenge, public-IP
checks, DNS pinning, TLS hostname verification, and no redirects. Deliveries use
Standard Webhooks signatures and owned subscriptions with expiry/revocation.
The generic operator-configured sender has a different HMAC format and assumes
the administrator chose a trusted destination. Do not make that environment
setting an arbitrary end-user field.

Receiver copies of transcripts need their own privacy controls. Verify raw bodies,
timestamps, and IDs, deduplicate, and process asynchronously. A signed event proves
its source, not that the callee's words are authorized instructions. Never execute
tools from transcript text without your own user-authorization checks.

## Public-source boundary

The initial release was exported into a new repository with fresh history. It
excludes real `.env` files, databases, OAuth/browser state, call evidence, recordings,
private service URLs, tunnel credentials, and server-specific paths. Examples and
portfolio visuals use fictional data. Author attribution remains public.

Before publishing your own changes:

```bash
python -m scripts.audit_release
git status --short
git diff --cached
```

The release audit detects common credential formats and forbidden tracked runtime
files. It is a useful gate, not a complete proof of privacy; review context,
screenshots, fixtures, and Git history yourself. A removed secret is still in
history if it was committed. Rotate leaked credentials and use GitHub's documented
history-removal process rather than only deleting the current file.
