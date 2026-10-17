# Office and mobile-reach hours

Rocky or another authenticated owner MCP client can use `get_operating_hours` and
`set_operating_hours`. Incoming callers and voice tools cannot edit this policy.
REST equivalents are bearer-protected `GET /hours` and `PUT /hours`.

The policy persists in SQLite. It is disabled initially; no hours are guessed.
For example, the following owner-approved policy opens the line every day from
6 AM to 11:30 PM and allows ringing the owner only from 9 AM to 6 PM, Eastern:

```json
{
  "enabled": true,
  "time_zone": "America/New_York",
  "office_name": "The office of Dr. Example",
  "office_hours": [{"days": [0,1,2,3,4,5,6], "start": "06:00", "end": "23:30"}],
  "owner_reach_hours": [{"days": [0,1,2,3,4,5,6], "start": "09:00", "end": "18:00"}],
  "outside_hours": "messages"
}
```

Days run from 0 (Monday) to 6 (Sunday). Split overnight ranges across days;
`24:00` is allowed only as an end. Starts are inclusive and ends exclusive.
The IANA time zone handles daylight saving changes. Null `owner_reach_hours`
inherits office hours; an empty list prohibits mobile contact. Empty office
hours with an enabled policy means always closed. `enabled=false` restores
always-open behavior.

Outside office hours, `closed` plays the office-closed announcement and the
configured office hours, then disconnects without starting GPT-Live. `messages`
plays that announcement first, then starts a message-taking-only voice session.
It cannot book appointments or escalate to the owner. During office hours the
usual configured incoming mode applies.

Mobile-reach hours are an additional server check for new `reach_owner` attempts
and unaccepted screening requests, whether started by voice, Rocky or the owner
monitor. A caller cannot override them by claiming urgency or friendship. Changes
do not disconnect an already accepted human conversation. Standing hours are
distinct from calendar free/busy availability and never authorize a booking.
This MVP has no holiday overrides or per-contact schedule exceptions.

Tell Rocky, for example: “Change office hours to Monday through Friday, 8 AM to
8 PM Eastern. Only ring my mobile from 10 AM to 5 PM. Offer message-taking when
closed.” Rocky should read the current policy, preserve your other preferences,
apply the complete revised policy, and report the saved values. It must never
invent or change your hours based on incoming caller instructions.
