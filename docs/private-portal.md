# Portal access through an existing private overlay

The public portal uses `PUBLIC_BASE_URL`, an HTTPS origin, and a Secure
`__Host-phone-owner` cookie. Opening the raw HTTP server address is a different
origin: public-origin form checks reject it, and browsers cannot store that
Secure cookie there. Changing the public URL would also break telephone callbacks
and MCP/OAuth, so keep it unchanged.

For an existing encrypted private connection such as Twingate, optionally enable
one separate private-IP portal origin:

```dotenv
PRIVATE_PORTAL_ORIGIN=http://192.168.20.10:8766
PRIVATE_PORTAL_TRUSTED_PEERS=["172.20.0.2","172.20.0.3"]
```

These are fictional addresses. Verify the actual connector TCP peer addresses on
your server. Only the exact listed private peers may use this private portal;
do not configure an entire subnet, wildcard, or arbitrary public proxy. The app
does not trust `X-Forwarded-For`, `X-Forwarded-Host` or `X-Forwarded-Proto` to grant
private access. Keep the ASGI server's proxy trust limited to explicitly trusted
infrastructure, never `--forwarded-allow-ips='*'`.

Connect your private overlay, then open the configured address followed by
`/live/login`. Use username **owner** and your existing portal password. Pairing
codes also work. The two sites sign in separately; the private login does not
reset your password or sign out existing public browsers. Save the private
address in your password manager if you use it regularly. A bookmark to
`/live/password/login` recovers to the normal sign-in page.

The HTTP private portal relies on the existing encrypted overlay rather than
browser HTTPS, so the browser still labels the address as HTTP/“Not secure.” It
uses a separate host-only, HttpOnly, SameSite Strict cookie, without Secure for
that HTTP origin. A private HTTPS origin uses Secure. Public HTTPS retains its
original Secure cookie. Tokens are bound to their exact origin in SQLite and
cannot be replayed between the private/public surfaces by changing cookie names.
Older public sessions remain valid; older unbound tokens cannot become private
sessions. All writes still require the exact selected Origin and CSRF protection.
Live captions, recording playback, controls, logout and password settings use the
same selected private session. Logout revokes only that session; a password
change retains its existing all-session revocation behavior.

This option is disabled by default. Configure both settings together. It adds no
router port, public route, Twingate resource, provider credential or MCP grant.
Private access is for the already configured owner, not a new authentication role.
The public Twilio and MCP URLs remain unchanged. Clearing both settings disables
private portal access; retain HTTPS for access outside the private overlay.

Before deployment, run the private/public origin, peer, cookie-replay, CSRF,
password and pairing tests. Back up source, configuration and databases, preserve
existing credentials/sessions, and restart only with no active calls. A rollback
must remove any new private sessions before using older code that lacks origin
binding; never restore an old call database over newer call records.
