# Contact instructions, facts and spoken names

## Spoken names

In **Settings → Personalities**, the profile **Name** is its display label.
**Spoken name** is the name the voice uses. It is editable for both presentations:

- **Assistant:** blank inherits the configured assistant name.
- **Me · first person:** blank inherits the configured owner name.

The inherited name is shown beside the field. Saving a different spoken name
does not rename the profile or change its voice or permissions. New calls pin
the resolved name. Legacy assistant call snapshots sometimes contain the owner
name from an old fallback; those names remain ignored for assistant speech
instead of being reinterpreted as custom assistant identities.

## Guidance for one contact

Open a saved contact in **Contacts**, then expand **Instructions and facts**.
The instruction and fact editors each support **all profiles** or **selected
profiles**. All-profile entries include future profiles but stay limited to that
contact. Each entry defaults to incoming and outgoing calls; incoming-only and
outgoing-only options are available.

Instructions control behavior. An optional **Opening words** field provides
the initial greeting for that scope. Facts contain information approved to
share with this contact. Each fact fits 250 UTF-8 bytes, like existing stored
facts; split longer notes into separate entries. Facts are durable unless you
edit, remove or disable them, and may have an optional expiry. Temporary facts
expire in the browser's local time. Existing shared facts retain their original
audience and expiry rules.

Fictional example for Riley Example:

| Scope | Instruction | Approved fact |
|---|---|---|
| All profiles | Keep answers friendly and brief. | The owner-approved explanation of occasional household noise. |
| Assistant profile | Warm, playful delivery. Opening: “Hi Riley, it's Rocky.” | Information approved for this presentation only. |
| First-person profile | Use the owner's preferred first-person style and quieter tone. | Different information approved for this presentation. |

These examples do not create contacts or facts. Only saved owner entries apply.
Profile choice and guidance never grant new tools, calendar authority, disclosure
of unrelated stored information, or permission to pass human verification.

## Resolution and call continuity

The existing explicit call choice, routing rule, contact assignment and default
select the profile first. Contact entries then apply from general to specific:

1. Shared contact guidance for both directions.
2. Shared contact guidance for the current direction.
3. Selected-profile contact guidance for both directions.
4. Selected-profile contact guidance for the current direction.

More-specific instructions override general style. Entries with the same title
have one effective version: the most-specific matching entry wins. Different
titles combine; for equally specific instructions, later-listed instructions
win explicit conflicts. Overlapping duplicate titles at the same scope must
be edited rather than saved as competing versions. Opening words follow the
same specificity and apply once, not after updates or reconnects.

A single exact saved-number match is required. Unknown, withheld, duplicated
or ambiguous incoming numbers receive no contact guidance. Forwarding metadata
does not substitute a different contact. Caller ID remains a routing hint.

Instructions and the initial fact selection are pinned for each new call.
Instruction edits apply to future calls. Later fact reads withdraw facts that
were edited, disabled, removed, expired or lost their contact binding; they do
not silently replace them with new values in an already-started call. A new call
loads the updated facts. Future tool reads and selected lookup evidence validate
the current fact again before use. Historical recall withholds a prior call if
its contact facts are not available to the current contact/profile/direction.

Guidance applies to real incoming/outgoing calls and browser calls through the
same prompt builders. **Preview saved guidance** shows the effective profile,
spoken name, instructions, facts and opening without placing a call. Save edits
first. The extended prompt viewer also accepts direction and forwarding metadata.

## Authenticated interfaces

Paired-owner UI routes:

- `GET /live/contacts/{contact_id}/guidance`
- `PUT /live/contacts/{contact_id}/guidance`
- `GET /live/browser-tests/recent-numbers`

The guidance read/write routes also exist at
`/concierge/contacts/{contact_id}/guidance` under existing bearer authentication.
Browser writes retain same-origin and CSRF checks. No new MCP grant is implied.

Guidance responses contain `revision` and `entries`. A PUT replaces that contact's
entries atomically using the current revision; a stale revision returns 409.
Entries contain `entry_id`, `kind` (`instruction` or `fact`), `title`, `text`,
`enabled`, `profile_scope` (`all` or `selected`), `profile_ids`, `direction`
(`both`, `inbound`, `outbound`), optional instruction `greeting`, and nullable
fact `expires_at`. Omitted optional fields use the editor defaults. Selected
profiles must exist. Empty optional content does not enable a feature by itself.

`POST /live/instructions-preview` and its bearer counterpart accept the existing
contact/profile selectors plus optional `direction` and `forwarded_from` fields.
They return `contact_guidance` alongside the presentation and assembled prompts.
Recent-number responses contain `forwarding_sources` and `call_numbers`; each
suggestion includes `number`, `contact_name`, and UTC `last_seen`.
