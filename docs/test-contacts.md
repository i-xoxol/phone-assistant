# Test contacts

In the private `/live` portal, expand **Caller book**, find a saved contact and
open its card. Check **Trust this contact for cooperative testing**, edit the
optional extra instructions, and press **Save test settings**. Uncheck and save
to restore ordinary behavior for future calls. No contact is enabled automatically.

A test contact can request general conversation, language/style experiments,
feature demonstrations and direct answers using available owner-approved context
and verified tool results. The additional instructions are layered after its
selected personality in the voice and tool-planner prompts. OpenAI, ElevenLabs
and Gemini receive the same test policy. The instructions viewer shows the flag
and effective policy, and a call card identifies an active test conversation.

This is cooperative testing, not a universal authorization override. It does not
expose API credentials, security codes, payment details or unauthorized private
records. Existing booking, spending, transfer, recording and tool permissions
remain enforced. Simulations must be identified as simulations. The flag does
not change who the caller is or pretend that the caller is the owner.

Only the owner portal, authenticated bearer API or approved owner MCP connection
can change the setting. The voice model has no tool to enable its own test mode.
Caller ID is a routing hint and can be spoofed. A unique saved number is required;
withheld numbers, duplicate matches and ambiguous forwarding do not activate it.
The flag is bound to the number present when saved. If a contact number changes,
save the test setting again to explicitly bind the new number.

Changes apply to **new calls**, including outbound calls to that contact. Each
call stores its selected test mode and instructions so redirects/reconnects are
consistent. An already active call retains its original mode; hang it up if an
immediate stop is needed. Other contacts' prompts and all model/voice choices
remain unchanged.

Rocky tools:

- `get_contact_test_mode(contact_id)`
- `set_contact_test_mode(contact_id, enabled, instructions="")`

Empty instructions use the built-in cooperative-testing guidance. REST writes
use `/concierge/contacts/{contact_id}/test-mode`; portal writes use
`/live/contacts/{contact_id}/test-mode` with owner cookie, exact Origin and CSRF.
The setting never places a test call by itself.
