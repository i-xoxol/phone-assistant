# Configuration reference

[← README](../README.md) · [Getting started](getting-started.md)

Configuration is read by `app.config.Settings`. It loads the project's `.env`
independently of the current directory; process environment variables take
precedence. Changes take effect after restart. Never restart during an active call.

## Required settings

| Variable | Value | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Your OpenAI project API key | Server-side GPT-Live connection; optional Responses delegation |
| `TWILIO_ACCOUNT_SID` | SID of the account owning the caller number | Twilio SDK authentication and callback binding |
| `TWILIO_AUTH_TOKEN` | Auth Token for that same account | Twilio SDK authentication and signature verification |
| `TWILIO_PHONE_NUMBER` | Your owned, Voice-capable number in E.164 | Outbound caller ID |
| `PUBLIC_BASE_URL` | HTTPS origin such as `https://phone.example.com` | Twilio URLs, signature verification, OAuth resource, monitor origin |
| `LOCAL_API_TOKEN` | A random token, generated from at least 32 random bytes | Authenticates `/calls` REST routes; not the MCP OAuth token |

`PUBLIC_BASE_URL` must be HTTPS and contain no path, credentials, query, or
fragment. A trailing slash is normalized away. Point the entire origin at your
service. A mismatch can fail both Twilio signatures and browser origin checks.
Destination and caller numbers use `+` plus country code and digits; formatting
validation is not a lookup proving a number exists or belongs to a person.

## Optional settings

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_LIVE_MODEL` | `gpt-live-1` | Native Live API model; changing it requires compatible model access |
| `OPENAI_VOICE` | `marin` | Built-in Live voice; no voice-cloning implementation |
| `CALLER_NAME` | `Alex` | Fictional default; change to your approved identity |
| `CALLBACK_NUMBER` | Empty | Included in the voice prompt only if supplied; disclosure is then authorized |
| `DATABASE_PATH` | `data/calls.sqlite3` | Relative paths resolve under the project root |
| `MCP_ENABLED` | `false` | Mounts authenticated Streamable HTTP MCP and OAuth on this FastAPI app |
| `OAUTH_DATABASE_PATH` | `data/oauth.sqlite3` | OAuth client/grant state; use a private writable path |
| `LIVE_TOOLS_ENABLED` | `false` | Lets Live delegate keypad and ending decisions to the in-service Responses backend |
| `OPENAI_BACKEND_MODEL` | `gpt-6-luna` | Used when voice tools are enabled; billed separately from Live |
| `WEBHOOK_URL` | Empty | Optional operator-owned generic HTTPS receiver for new call events |
| `WEBHOOK_SECRET` | Empty | Separate random HMAC secret; required when `WEBHOOK_URL` is set |

`MCP_WEBHOOK_SECRET` is **not** a main-service setting. It is used by the example
Standard Webhooks receiver in `scripts/mcp_webhook_receiver.py`. Native MCP event
subscriptions carry a separate receiver URL and `whsec_` secret per subscription.
See [webhooks](webhooks.md).

## Permissions and model behavior

Basic speech uses Live access. Autonomous voice tools use Responses delegation
and need Responses write permission plus access to the backend model. A
successful Live handshake does not necessarily prove that a later backend
function call can execute; verify that with a short menu test.

When voice tools are off, client delegation requests receive a no-action result;
the assistant has no extra lookup capability. The originating MCP client may
send navigation digits or context independently. With voice tools on, avoid
competing chat-side keypad actions.

Default caller disclosure comes from `CALLER_NAME`, optional `CALLBACK_NUMBER`,
and relevant context in the brief. Do not put secrets, full calendars, patient
records, passwords, or payment data in call context. The model receives that
context; it is not a private note invisible to the voice agent.

## Secret handling

Use the ignored `.env` during local development. On a Linux server set `.env`
and the `data` directory to owner-only permissions. On Windows keep the checkout
and runtime data under your account and avoid shared/synced secret stores.

The app does not print API keys. Generate tokens locally and enter them through
your editor, never through committed shell commands. Use different values for
provider credentials, REST access, and webhook signing.

Never share an authenticated instance publicly just because its **source** is
public. Pairing codes give access to private records and, for OAuth clients,
paid outbound calling. Use a dedicated Twilio account/subaccount and an OpenAI
project if you want separate usage accounting; configure their own credentials.

`python -m scripts.configure_env --field FIELD_NAME` accepts one value on stdin
and stores it without echoing it. The helper supports only its listed provider
fields; editing `.env` remains the simplest complete configuration method.
