# Portable single-server deployment

The current Lenovo service remains a native systemd service. The Docker files
are an additional deployment option, not an automatic migration or a cloud
account setup. No AWS/Azure resources or paid subscriptions are created.

Use one persistent Linux VM/container host with HTTPS and WebSocket support,
one process, one replica and a persistent disk. SQLite is appropriate for this
deployment; multiple replicas sharing a database file are not supported.
Avoid ephemeral/serverless filesystems for the databases and credentials.

## Build and run

Copy `.env.example` to `.env`, supply the documented provider/Twilio secrets and
PUBLIC_BASE_URL, then:

```sh
docker compose build
docker compose up -d
docker compose exec phone python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8766/health').status)"
```

The default `runtime` target includes Python dependencies, not a Codex sign-in.
For optional subscription-backed lookups and rule review:

```sh
PHONE_IMAGE_TARGET=subscription docker compose build
PHONE_IMAGE_TARGET=subscription docker compose up -d
docker compose exec phone codex login --device-auth
```

Complete your own device authorization. It stays in the private data volume at
`/data/runtime-home/.codex`. It is never copied into the image. This optional
target pins Codex CLI 0.163.0, matching the validated server runner; upgrades
need a new bounded-runner compatibility test. Usage follows your subscription's
limits. API-backed review requires an API key and explicit API selection; it is
billed separately. Without CLI authentication, subscription review/lookups fail
closed. Do not enable subscription lookup in a runtime-only image.

The build context allows only app source and requirements. The image runs as
UID/GID 10001 with a read-only root, no extra capabilities and a temporary /tmp.
Compose publishes only `127.0.0.1:8766`. Place your existing HTTPS proxy/tunnel in
front of it and support long-lived WebSockets/SSE. Configure the proxy's trusted
forwarding behavior deliberately; do not expose an unauthenticated internal
endpoint or trust arbitrary X-Forwarded headers. A separate proxy container
needs its own intentional routing configuration; loopback here is on the host.

## Persistent state and migration

The `phone-data` volume holds calls.sqlite3, oauth.sqlite3,
appointments.sqlite3, recording-notices/, owner-prompts/ and optional runtime
CLI auth. API/Twilio secrets enter at runtime through `.env` or your cloud's
secret injection. Never bake these into image layers, build arguments or git.
Protect disk snapshots/backups as private, since transcripts, approved details
and credentials can be present. Bind mounts must be writable by UID 10001.

For migration, drain calls first, stop the old instance, take consistent SQLite
backups of all three databases and copy those plus media/prompt directories into
the new persistent volume. Keep the same canonical public URL and OAuth issuer
when moving an existing installation; changing them requires deliberate MCP and
Twilio reconfiguration. Do not run old and new instances against the same call
traffic simultaneously. Test a separate number/hostname or mocked gateway first.

Calendar access also needs its existing Google token, account namespace and
fresh mirror-status service. Credentials are not automatically portable just
because the app is containerized. Mount the token privately at a configured
`APPOINTMENT_GOOGLE_TOKEN_FILE` path (the library may refresh it, so retain the
existing read/write behavior), and ensure the configured mirror-status URL is
reachable. Move/configure the bridge separately; stale/missing status continues
to withhold availability. Keep appointment writes disabled until this boundary
is verified. No existing Lenovo calendar credentials are included in this repo.

Twilio must reach the same public routes described in the main README, including
its voice/status/recording webhooks and media WebSockets. Configure inbound voice
on the intended number and validate signed requests; credentials and base URLs
must match. Public MCP OAuth and the owner portal remain authenticated.

Before an update, build/test a separate image, back up state, check for active
calls twice, then replace the process. `/health` is a process health check, not
a proof that provider billing, Twilio dialing or calendar freshness work. Verify
those separately without unintended calls. Roll back source/image if needed;
do not overwrite newer call data with an old database snapshot. Record the
deployed git hash/image ID. Keep credentials/logs out of public build output.
