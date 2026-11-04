# Caller book and personalities

Open the private `/live` control page. These controls use the same owner login,
CSRF checks and database as the existing transcript monitor.

## Save and customize a caller

Select a call and choose **Save caller to phone book**, or open **Caller book**
and choose **Add contact**. Enter a name and full international number, then save.
The page does not guess an incoming caller's name. An existing number opens its
saved contact; ambiguous or withheld caller ID cannot be saved from a call.
Duplicate numbers are rejected by the portal rather than silently creating an
ambiguous match. Existing ambiguous directory records remain unchanged.

Each saved contact has:

- Name, spoken nickname, language and formal/familiar relationship tone.
- Personality: **Default / inherit**, or one specific saved profile.
- Answering provider, model and voice, with the existing default inheritance.

A single exact presented-number match selects the contact. Forwarded calls use
the original presented caller, not the owner's forwarding number. Missing or
ambiguous identity uses the default profile. Personality selection does not
authenticate a caller or change disclosure permissions.

## Personalities

**Default** is initialized once from the existing conversational speech style,
including its light hesitations and accent guidance. The migration keeps the
current default provider/voice, caller languages, owner instructions and contacts.
**Office** and **Friend** are optional starting profiles; they are not assigned
automatically. Existing formal/familiar and owner-recognition rules still apply.

Each personality has an editable name, instructions, greeting guidance and
caller-safe background. To make a variation, select a profile, choose
**Duplicate saved profile**, edit the independent copy and save. Duplication
copies saved content; it neither changes the default nor moves contacts.
Alternatively choose **New personality**. Up to fifty profiles are supported.
Concurrent edits require the current revision; a stale save is rejected.

Use **Default for new calls** to choose which profile unassigned callers inherit.
An explicit contact assignment stays attached to its selected profile. Editing
that profile affects its future calls. Every call pins the full profile text and
revision at creation, so an edit, rename, reassignment or restart does not change
an existing conversation. Use the existing live-update tool for an intentional
change during an active call.

Personality is behavior, independently of the audio engine. The same selected
instructions reach OpenAI, ElevenLabs and Gemini adapters; their ability to follow
accent, expressiveness and greeting guidance varies. Model/voice precedence stays
explicit call choice → unique contact override → saved provider defaults. There
is no automatic provider switch or new API charge for editing/previewing profiles.

## Instructions and context

**Owner context** edits the same shared role and current activities that Rocky
updates through `set_inbound_context`. Anticipated-call notes and approved facts
can be added, edited and retired manually. They retain their existing expiry and
disclosure limits. Facts are caller-safe information, not a private notebook.
Calendar free/busy remains managed by the calendar bridge; expired slots are
withheld, and no editor can turn old availability into verified current evidence.

**Inspect assembled instructions and context** runs the actual application prompt
builders locally. Preview a new incoming caller/profile combination, or inspect
the selected call's pinned personality and voice. Expand the voice prompt, tool
planner prompt, startup input, tool definitions and context sections. Preview does
not dial, connect to an AI provider, query a live calendar, change settings or pin
an imaginary call. A selected call preview rebuilds current safe context; it is
not a historical recording of the exact startup prompt.

Editable personality instructions replace the old shared speech-style block.
Other sections compose the application identity and action policy, global role,
contact preferences, routing, fresh context, call history and live owner updates.
The fixed identity, authorization, tool, recording and continuity policies are
visible but remain controlled by the server. Profiles cannot grant new tools,
financial authority, sensitive disclosure or booking permission. The viewer shows
application prompts, not private model reasoning or provider credentials.

## Rocky / MCP

New tools reuse the existing `phone.calls` connection:

- `get_call_personalities()`
- `set_call_personality(name, instructions, context, greeting, personality_id?, revision?)`
- `duplicate_call_personality(personality_id, name)`
- `set_default_call_personality(personality_id)`
- `set_contact_personality(contact_id, personality_id?)` — omit the profile to inherit.
- `get_call_instructions(call_id?, contact_id?, personality_id?)` — private read-only preview.

Read the current profile before updating it, retain its fields, and supply its
revision. Caller requests never authorize personality changes. Existing contact,
language, context, model/voice and live-update tools continue to work.

Owner bearer REST equivalents live under `/voice/personalities`,
`/voice/default-personality`, `/voice/instructions-preview` and
`/concierge/contacts/{contact_id}/personality`. The browser uses `/live/...`
routes requiring an owner session, exact Origin and CSRF for writes/previews.

## Verification and rollout

Run `python -m pytest -q` and `python -m scripts.audit_release`. Tests use fictional
contacts and mocked telephony. Check default preservation, independent copies,
revision conflicts, contact inheritance, pinning, privacy/authentication and
provider prompt composition. Browser-check adding/editing contacts, assignment,
duplication, context editing and prompt previews at mobile and desktop widths.
Deploy a reviewed branch only after isolated checks pass, with source/database
backups and zero active calls. Database changes are additive; rollback source
without restoring an old business database over newer calls or messages.
