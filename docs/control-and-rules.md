# Contacts, routing and live control

The portal has five sections: **Calls**, **New call**, **Contacts**, **Rules**, and
**Settings**. On phones the navigation stays at the bottom; an active call has a
separate Hang up button above it. Transcript updates do not move you away from an
editor or a pending approval. Settings are grouped into expandable panels.

## Contacts and personalities

Choose Contacts → Add contact, enter an international-format number, and save.
Expand the saved contact to choose its personality, language, provider, model,
voice, and test settings. Personalities are edited/duplicated in Settings.
"Default" inherits the configured default; voice and presentation are separate.
Selecting a first-person personality does not select a cloned voice or expand
permissions. Existing profiles and assignments retain their IDs and behavior.

Each contact has an **information level**, separate from relationship tone:
General; Friends (also General); Family (also Friends and General). Existing
contacts begin at General. Assign these levels deliberately. Recognition uses
the service's existing saved-number policy; forwarding with an ambiguous caller
identity does not inherit the forwarding owner's group. It is recognition, not
strong authentication. A test flag never grants additional disclosures.

Advanced routing data includes an optional owner-assigned score, 0–100. This is
just a classification you choose, not a model's estimate of identity or trust.
No score means score-based conditions cannot match.

## Review and apply routing rules

1. Describe the desired routing in Rules. For example: "For incoming calls
   reported as forwarded from +12025550109, use Office and the configured OpenAI
   voice. For Sam's saved number use Friends. Otherwise keep defaults."
2. Choose **Codex subscription** (existing server CLI sign-in) or **OpenAI API**
   (separately billed, explicitly selected). Click Review with AI.
3. Read the proposed conditions, choices, explanation and unresolved requests.
   No rules have changed yet. Unsupported requests block applying the proposal.
4. Apply reviewed rules. If another editor changed them, reload/review again.
5. Use Advanced → routing preview to simulate a number, direction and reported
   forwarding source. This does not dial or change saved settings.

Only configured, supported providers/models/voices and existing personality IDs
are accepted. No executable code is generated. There is no silent paid API
fallback. The reviewer receives the owner-authored routing request, current rule
configuration and the contact/profile/provider catalog; it receives no private
fact values, transcripts or credentials. A proposal may take up to the configured
review timeout. Model/CLI failures leave the active rules untouched.

Rules run deterministically when a call is created. The first matching enabled
rule wins (lower priority first; UUID breaks ties). Other rules are not combined.
For each setting the precedence is:

**Explicit per-call choice → matching rule → saved contact → default.**

Unset fields inherit. Rule conditions include inbound/outbound, exact numbers,
reported forwarding source, contact group and score bounds. Missing forwarding
metadata means **unreported**, not proof that a call was direct. Forwarding data
comes from the signed Twilio webhook, subject to what the carrier supplies.
Caller-ID actions must be outbound; mobile caller ID still requires successful
Twilio verification. Test rules require exact, uniquely saved contact numbers.

Advanced JSON editing supports precise changes and removing rules. Disable a rule
to stop applying it. Save/apply uses a revision guard to prevent overwriting other
editors. Routing/profile/provider choices are snapshotted per call; edits apply to
future calls, not a call already underway. Rules cannot add tools, authorize
money/contracts, change disclosure groups or bypass booking/transfer limits.

## Call monitoring and approvals

In Calls, select an active call. Live controls shows **Information requests**
first. Expand **Send new information** to send a short context update, ask a
question, or request that the voice say something. Delivery is asynchronous;
confirm what was actually said in the transcript. Repeating an uncertain send
with unchanged text reuses its request ID. Updates preserve the call's identity
and permissions. Hang up sends a direct termination command to the server.

Facts in Settings → caller knowledge have an audience: General, Friends, Family,
or **Ask me for this call**. Private values are filtered out before voice and
lookup models receive context. A voice tool can request an exact fact without
seeing its value. You see the fact, reason and call; approve or deny it there.
Approving one detail does not approve emailing, spending, booking, other facts or
other calls. A stale/edited/expired fact or ended call cannot be newly approved.
Changing a fact invalidates its old grants. A contact downgrade takes effect on
subsequent lookups in an active call. Previously spoken words cannot be recalled.
Calls with a one-call disclosure are withheld from automatic conversational
history replay; recordings/transcripts remain in the private owner portal.

Shared free-form owner context is still caller-safe reference information. Keep
restricted details in audience-labelled facts, not in shared role/style/context
text. Information explicitly supplied for an outbound call is that call's
authorized task context. Restrictions do not automatically classify arbitrary
text or erase information that was already sent.

## Rocky / other MCP supervisors

All controls use the existing owner authentication and phone tool permissions.
Refresh the MCP tool catalog/reconnect the client if new tools are not visible.
No new OAuth permission is needed. The new tools are:

- `get_call_rules`, `set_call_rules`, `review_call_rules`,
  `apply_reviewed_call_rules`, `preview_call_rule`;
- `get_outbound_settings`, `set_outbound_defaults`;
- `get_call_approvals`, `request_call_disclosure`, `resolve_call_approval`.

`set_call_contact` adds/edits numbers, language, personality, model/voice,
information level and routing score. `set_call_fact` accepts `audience`.
`place_call` accepts `personality_id` and `caller_id: "mobile" | "twilio"` in
addition to provider/model/voice. Read actual profile IDs before using one; use
`request_id` to reconcile uncertain dialing rather than blindly redialing.
Existing personality tools edit/duplicate profiles and preserve omitted fields.

For live supervision, subscribe an actual receiver URL to
`call.approval_requested` as well as the existing status/transcript events.
This event includes the call ID, approval ID and topic, never the private value.
Retrieve pending approvals through the authenticated tool and decide only under
the owner's specific instruction; a caller asking is not owner authorization.
Then use `resolve_call_approval`. The website receives these events through its
existing authenticated SSE connection; it does not need an external webhook.
MCP clients do not wake arbitrary chats automatically without a configured
subscription/receiver. Existing subscriptions are left untouched.

The rule schemas are in `app/calling/rules.py`; REST endpoints mirror them at
`/voice/rules`, `/voice/rules/review`, `/voice/rules/review/{id}/apply`, and
`/voice/rules/preview`. Portal writes use authenticated `/live/...` endpoints and
the existing exact-origin/CSRF checks. Neither public static assets nor webhook
events expose private approval values.
