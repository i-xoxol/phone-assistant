# Design decisions, costs, and extension points

[← README](../README.md) · [Architecture](architecture.md)

## Direct bridge instead of an agent framework

Twilio's Calls API, `<Connect><Stream>`, and OpenAI's native Live SDK provide the
required primitives. A direct Python bridge keeps codec matching, playback,
authorization, and persistence visible in one small service. Twilio Agent Connect
or direct Live SIP could replace the transport module later, but neither is
required to replicate this MVP.

The call manager is the boundary between controls and transport. REST, MCP,
browser hangup, and voice tools all converge on it. This avoids giving different
interfaces inconsistent ownership of the same call.

## Native audio and a separate tool backend

GPT-Live owns conversation style, listening, and speech. Optional Responses
delegation gives it a focused backend for keypad and ending decisions. That
backend does not need access to the originating chat or its other plugins.
The application executes validated function requests and returns their result.

This reduces dependence on supervisor polling for menu navigation. It does not
make tool latency zero or grant the voice model calendar/email access. Keep facts
and owner authorization in a concise initial brief; send only relevant approved
updates as they become available.

## SQLite and one process

SQLite gives durable call records, ordered events, idempotency, and delivery
cursors without a queue broker or database server. In-process wakeups keep
browser captions and event delivery responsive. The cost is a one-worker
architecture, no distributed recovery, and no multi-tenant isolation.

There is no Redis, Celery, Kafka, Kubernetes, frontend build, or required Docker
deployment. Add infrastructure only after a real scale or durability need is
measured. Multiple call objects can exist, but concurrency, fairness, and
multiple-owner behavior are not comprehensively load-tested or promised.

## Three event channels

SSE gives a private browser live text and direct controls. Native/ordinary MCP
subscriptions give an authenticated client a call-filtered signed callback. A
separate operator-configured webhook feeds custom workflows. These have distinct
delivery guarantees and signature formats; conflating them produces misleading
claims about whether a chat is monitoring a call.

## Deliberate MVP compromises

DTMF uses a TwiML redirect and voice reconnect because Media Streams has no
outbound DTMF message. Farewell detection coordinates final words with playback
marks because the implemented primary Live output does not expose a simple
speech-turn completion event used by this bridge. Captions can arrive late;
transcript acknowledgment is not audio playback proof.

Safety is mostly prompt-based for conversation content. There are narrow allowed
functions, no financial functions, and explicit limits, but no semantic approval
engine. The monitor's direct hangup is a practical human control, subject to
network/provider confirmation.

## Costs

Your costs are the sum of:

```text
Twilio caller-number rental
+ outbound voice minutes for the destination
+ Twilio Media Streams usage
+ GPT-Live session usage
+ Responses backend usage when voice tools are enabled
+ any paid tunnel/server hosting
```

There is no separate always-running model or automatic summary-model charge.
The Python process and SQLite are local; connecting a client or pairing a browser
does not dial a phone. Network preflights still briefly use OpenAI. Set short call
limits and provider alerts, and inspect the actual account usage dashboard.

Rates, destination charges, taxes, billing increments, model access, and account
promotions change. Use current official pricing rather than treating an old
sample call as a price guarantee:

- [Twilio Voice pricing](https://www.twilio.com/en-us/voice/pricing)
- [OpenAI API pricing](https://developers.openai.com/api/docs/pricing)
- [Twilio trial allowances and restrictions](https://www.twilio.com/docs/usage/trials)

Trial credits/free minutes should not be assumed to renew monthly. Separate
personal use from university/research credit accounts and verify each award's
terms before charging it. This public project contains no grant or billing-account
configuration.

## Future extension points

| Future feature | Likely boundary |
|---|---|
| Stronger approval requests | CallManager/VoiceTools before execution, durable approval records, owner UI |
| Configurable profiles | Settings/prompt builder; keep disclosure policy explicit |
| Calendar/contacts/email context | Supervising client lookup, minimal verified `send_call_update` |
| Incoming calls | Separate authenticated Twilio voice route and initial record creation |
| Voicemail handling | Provider detection plus explicit conversation/end policy |
| Scheduled calls | Durable authorized jobs and lifecycle management |
| Automatic structured summaries | Post-finalization worker with evidence-grounded schema and distinct billing |
| Multi-owner support | Real user accounts, per-record authorization, separate grants and isolation |
| More scale | Bridge ownership coordination and durable recovery before extra workers |
| SIP or another voice provider | Transport boundary; preserve controls, records, and authorization |

These features are not implemented. Voice cloning is also not implemented. The
project currently uses a built-in Live voice and outgoing telephone calls only.
