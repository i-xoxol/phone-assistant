# Test calls in the browser

Open the private monitor, sign in, choose **New call**, and expand **Test in
browser · no phone call**. Allow microphone access and use headphones if possible.
The public HTTPS portal and localhost support browser microphone access. An HTTP
portal on a private network address may still support other controls, but needs
HTTPS for microphone testing.

Choose a scenario:

| Scenario | What the assistant receives |
|---|---|
| Incoming · direct | The caller ID you enter, or hidden caller ID if blank. Forwarding metadata is absent, as with a carrier that does not report forwarding. |
| Incoming · forwarded | Separate caller ID and forwarded-from numbers. Owner-mobile forwarding and ambiguous caller identity follow normal routing rules. |
| Outgoing | The entered recipient number, objective, context, constraints and simulated outgoing caller ID. You speak as the person receiving the assistant's call. |

Select a saved contact to populate the number, or enter any international-format
number such as `+12025550101`. Selecting a number does not dial it. Contact
preferences, routing rules, information levels, personalities and provider/model/
voice defaults apply normally. Optional explicit choices override those defaults
for this test only. Outgoing tests can copy the existing outbound composer's
brief. Mobile caller ID is simulated without a Twilio verification request.

For forwarded tests, **Choose from recent history** groups recorded forwarding
sources separately from other caller/recipient numbers. Suggestions come from
the latest 100 real calls, with up to 20 unique numbers per group and saved-contact
names where unambiguous. Browser test records are excluded. Selecting a source
changes only **Forwarded from**; manual caller ID stays separate. Refreshing the
suggestions preserves a manually typed forwarding number.

The selected profile also resolves any [contact instructions and approved
facts](contact-guidance.md). This lets you compare assistant and first-person
presentations for the same fictional contact without changing global settings.

After **Start browser test**, talk normally. The existing call monitor displays
captions and saved call state. The blue browser-test panel stays available while
you navigate, with **Mute microphone** and **End test**. The ordinary selected-call
Hang up control also ends a browser test. Closing or reloading the browser closes
the voice session; reconnect by starting a new test.

## What is shared with a real call

Browser calls use the same call manager, pinned settings, assembled prompts,
provider adapters, media bridge, interruption/playback controls, tools, live
updates, disclosure approvals, captions and duration limits. Browser audio is
converted to the same mono 8 kHz G.711 mu-law used by the telephone bridge.
Provider credentials stay on the server. The browser uses the existing paired
owner session, same-origin checks, and CSRF authorization; sockets are bound to
the browser that created the test and cannot attach to real phone calls.

Enabled non-telephone tools remain real. Saved messages appear in the inbox;
approved context/language changes can persist; enabled appointment tools can
write to the actual configured calendar. Voice-provider usage is billed normally.
This is a test transport for the actual assistant, not an isolated calendar
sandbox. Browser tests remain labeled in call history and are excluded from
automatic number-matched recall in later calls.

## Telephone behavior

No telephone account operation is needed to create, connect, refresh or end a
browser test. There is no dial, ring, carrier callback, provider duration update,
verified-caller-ID lookup or telephone recording. Keypad requests are displayed
as simulated digits without sending telephone tones or reconnecting the voice
session. Owner-transfer requests simulate an unavailable owner and leave the
assistant connected for its normal fallback. Carrier audio quality, ring timing,
successful human handoffs and telephone recording/announcements still need real
telephone checks.

Office-hours routing remains active. A closed-office announcement is displayed
as text in the browser. When the configured policy ends closed-office calls, the
test ends without starting a voice-provider session. Message-taking policy uses
the normal message-taking assistant instead.

Only one browser test runs at a time; it does not occupy the real incoming-call
admission slot or hourly allowance. An unconnected test expires after 30 seconds.
A service restart cancels stale tests instead of trying to resume browser audio.

## Verification

`python -m pytest -q tests/test_browser_tests.py` exercises the three scenarios,
anonymous/ambiguous caller ID, forwarding rules, idempotent creation, paired
session isolation, origin/CSRF checks, audio/caption playback, malformed frames,
phone-action simulation, private-portal authentication, and recall/admission
separation using mocked providers and fictional records. The full regression
suite and release audit remain required before deployment.

To listen to a test or join it as the owner, open that call in a second paired tab and follow [browser live audio](browser-live-audio.md).
