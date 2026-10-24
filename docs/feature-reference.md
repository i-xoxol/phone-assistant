# Calling, context, appointments, and coaching

[← Project overview](../README.md) · [Documentation](../README.md#documentation)

Detailed behavior and boundaries of the optional workflows in the current source.

## Incoming calls and concierge mode

Call your Twilio number or configure conditional forwarding with your carrier.
The service keeps presented caller ID and forwarding metadata separate; absent
metadata cannot prove a direct call or distinguish declined from unanswered.
Optional delayed pickup and missed-call context change the greeting without
automatically ringing the owner again. [Forwarding behavior →](recordings.md)

In concierge mode, the live assistant can save a caller-confirmed message or ask
a supervising agent such as Rocky for verified help. Your paired monitor shows
captions, inbox messages, pending requests, and direct hangup. Automatic
supervision requires an actual renewed client event subscription; installing the
plugin alone does not keep an agent listening. Missing or delayed help falls
back to message-taking. A supervisor can confirm a booking only after an
authorized action and verification. [Incoming setup and permissions →](incoming-calls.md)

Office hours govern whether the line offers normal service, closure, or
message-taking only. Owner-reach hours separately restrict new screened mobile
attempts. Neither free/busy nor a caller's claim of urgency overrides those
controls; accepted human conversations are not disconnected by a later hours
change. [Routing and hours →](operating-hours.md)

### Context, memory, and expiry

Owner tools maintain a saved role, phone-safe facts, verified free windows, and
anticipated-call notes. The voice reads current local context without waiting
for a supervisor; changes notify active incoming sessions. Optional Luna lookup
selects evidence IDs from approved stored facts, and the server revalidates the
sources before injecting their exact text.

Availability expires and is withheld when stale; expected-call notes expire
within seven days, and curated facts have explicit expiry. Optional exact-number
call recall uses bounded historical excerpts, with source dates, as background.
Old speech never becomes fresh availability or new authorization. Caller ID,
saved-number recognition, names, and forwarding hints do **not** establish
verified identity. Store only facts approved for the intended disclosure.
[Saved context](concierge-context.md) · [Lookup](subscription-lookup.md) ·
[Call memory](call-memory.md)

An optional Google job refreshes cached free windows every five minutes.
**Offering a cached window is not reserving it**, and an absent cached window
does not prove busy. The guarded appointment path below performs its own reads.

## Guarded appointments and calendar adapters

This is an implemented **opt-in workflow**, disabled by default. It needs separate
feature/write settings, standing ordinary-booking authority, configured calendar
and mirror evidence, and additional MCP grants for a supervising client.
Enabling calling does not enable booking. [Setup, policy, and recovery →](appointments.md)

- **Discovery:** `find_first_available_slot` searches the next fourteen days,
  honoring optional caller-stated bounds and collecting missing name, purpose,
  duration, and arrangement. It verifies the whole search window, then runs the
  ordinary exact check. A result is a proposal, never a reservation or consent.
- **Eligibility:** the current implementation is an ordinary academic-office
  workflow. The whole meeting must fit its academic-hours policy and an
  owner-designated Office Hours occurrence, with fresh complete coverage,
  recurrence/all-day handling, buffers, and the actual live request deadline.
  Broad cached windows and event titles cannot designate booking authority.
- **Consent and verification:** one server-owned concrete readback must be
  observed, followed by fresh unambiguous caller agreement. Corrections,
  withdrawal, pending audio, and expiry block a write. For routine near-term
  local appointments, already established details allow a shorter confirmation;
  flexible timing alone never approves an unseen slot.
- **One durable operation:** one deterministic private create per call is followed
  by independent source readback, an external-conflict check, and private registry
  commit. Ambiguous writes are reconciled against the original identity, never
  blindly created again. Calendar confirmation and whether the caller actually
  heard it are tracked separately. No invitations, cancellation, or rescheduling
  API is provided.
- **Minimal disclosure:** private appointment records and bounded owner-provided
  notices stay out of shared facts. Native callers can confirm meetings whose
  titles plausibly match a name mentioned anywhere in the call; current Google
  evidence is required. This is not proof of caller identity. Existing curated-contact status is audience
  restricted and expiring, not inferred from private event titles.

The core uses a **provider-neutral `CalendarProviderAdapter` v1 contract** with
canonical identities, intervals, coverage, revisions, and declared capabilities.
**Google is the currently implemented real adapter**; another provider is exercised
through a fake contract-test adapter, not a second live integration. Existing
upstream calendar mirroring remains a separate dependency; this project adds no
direct Microsoft Graph calendar or email access.
[Contract](../app/appointments/contracts.py) · [Google adapter](../app/appointments/google.py)

Fresh Google reads and mirror-health checks fail closed on incomplete or stale
coverage. They cannot eliminate the upstream mirror's five-minute lag or lock
external calendar editors; Google has no atomic free/busy-and-create operation.
“No verified slot” means none was verified inside the bounded search window.
The separately enabled private Realtime `get_owner_schedule` tool is read-only,
owner-only, and bounded to seven days per lookup; it offers no booking tools.

## Private owner-coaching options

Screened handoff always uses a fixed configured destination. With observation
enabled, **1** accepts with configured coaching, **2** declines, and **3** accepts
with captions only and no coaching model. Basic transfer can also run without
observation. These optional paths share current approved context and preserve
the distinction between owner and caller.

| Optional mode | Input and private delivery | Boundary |
|---|---|---|
| [TTS coaching](private-voice-coaching.md) | Subscription Luna selects current evidence from captions/context; OpenAI generates a short clip for an owner-participant announcement. | No fresh calendar search or action tools in this worker. |
| [Hybrid coaching](hybrid-owner-coaching.md) | Luna chooses a factual TTS hint or a temporary GPT-Live coach fed selected facts and labelled captions. | Caption-fed, buffered short replies; no direct microphone input or action tools. |
| [Native Realtime coaching](realtime-owner-coaching.md) | Full GPT-Realtime 2.1 receives separately labelled raw owner/caller audio and streams brief hints to the owner channel, bypassing Luna. | Optional read-only schedule tool; no booking, dialing, payment, or permission-changing tools. |

Native Realtime offers an opt-in **two-leg local bridge** that relays both humans
and mixes hints only into audio sent to the owner's earpiece. Model failure leaves
human forwarding running; media loss closes the streams into a preloaded ordinary
Twilio conference with coaching off. Recovery may interrupt audio. The alternative
**managed conference** transport needs a same-account TwiML App and an extra
application connection, with corresponding concurrency and cost limits.

Owner pause/resume and changed/expired evidence suppress late coaching; captions
remain separate. Private output routing is implemented and documented handset
checks exist, but a new deployment still needs its own consenting acceptance
test. Speakerphone echo, caption errors, model interpretation, carrier behavior,
and end-to-end latency remain practical limits.

## Your phone as a live control panel

The **Caller book · Languages** panel shows saved language preferences and lets
the owner edit them. Explicit caller requests are remembered for the next call;
unknown callers default to English. Rocky can manage preferences and approved
teaching/activity context. [Language memory, student-call strategy and Luna Fast →](caller-memory-and-student-context.md)

```bash
python -m scripts.web_pairing
```

Open `https://YOUR-PUBLIC-HOST/live` and enter that code. Generate a separate
one-use code for each browser, or use **Pair mobile** from a paired laptop.
Codes expire after 15 minutes; browser sessions last seven days. Browser pairing
and MCP OAuth pairing are separate. Once paired, choose **Password** to create
a password (username **owner**). Save it in your password manager and use it on
either device whenever a session expires. Pairing codes remain available for
bootstrap and host-controlled recovery. [Sign-in and recovery →](live-monitor.md)



Captions are pushed as they arrive, with replay after reconnect. They can trail
speech or contain recognition errors. The monitor uses **SSE**, not an outgoing
webhook, and its hangup button talks directly to the service.
[Monitor and emergency control details →](live-monitor.md)
