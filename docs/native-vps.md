# Native VPS deployment and migration

This application can run on a small persistent Ubuntu VM using Python, one
Uvicorn process, systemd and Nginx. Docker is another supported packaging option;
see [cloud-deployment.md](cloud-deployment.md). No GPU, Kubernetes, Redis or
separate database service is required. Size the VM for concurrent audio bridges,
resampling and optional lookup processes, rather than the Python process alone.

## Prepare a server

1. Use an existing VM, verify its public address in the provider console, enroll
   a dedicated public SSH key and pin the server host key. Keep private keys on
   their originating machines. Create an administrator and test its key/sudo
   access before disabling password SSH. Keep an out-of-band provider console
   available. Provisioning this project does not purchase a hosting plan.
2. Install Python 3.11 or later, `python3-venv`, Nginx and Certbot from official
   repositories. Use a separate unprivileged application account. This release
   was tested on Python 3.11, 3.12 and 3.14; the native cloud migration used 3.14.
3. Fetch the selected git release into the application account's directory,
   create its `.venv`, then install `requirements.txt`. Keep `.env`, databases,
   authentication caches and recordings out of the source archive and Git.
4. If using subscription-backed lookup, install the pinned official
   `@openai/codex@0.163.0` package with Node 22, and authenticate the application
   account with `codex login --device-auth`. Test using
   `python -m scripts.check_subscription_lookup --fast`. Do not copy a complete
   developer home/config directory into production. Credentials are runtime
   state, never an image layer or build argument. An existing deployment's
   CLI-managed cache may instead be migrated privately with its owner, but its
   use must move to one runner. If the source machine continues using Codex,
   establish an independent sign-in for the new host to avoid refresh-token
   conflicts. Follow the [official authentication lifecycle guidance](https://learn.chatgpt.com/docs/auth/ci-cd-auth).
   Never extract tokens into application code or silently substitute an API key.
5. Enable linger for a systemd user deployment, or use a system unit with an
   explicit unprivileged `User`. Adapt [deploy/phone-agent.service](../deploy/phone-agent.service)
   to the actual working directory and virtual environment. Set `UMask=0077`,
   restart on failure and use **one worker**. Bind Uvicorn to loopback.

Application account example:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766 --workers 1 \
  --no-access-log --forwarded-allow-ips 127.0.0.1
```

Keep all provider and Twilio configuration from the main README. Verified
outbound caller IDs, personalities, contact rules, hours, recording policy and
approval grants are existing runtime state; a hosting move should preserve them.

## HTTPS and streaming proxy

Publish the existing canonical host with a valid certificate. Nginx must forward
the original published Host, set the HTTPS scheme deliberately, handle Upgrade
and Connection headers, disable proxy buffering, and allow long-lived streams:

```nginx
map $http_upgrade $phone_upgrade {
    default upgrade;
    '' close;
}

# Within the intended HTTPS server block:
location / {
    proxy_pass http://127.0.0.1:8766;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $phone_upgrade;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

Reject unexpected hosts. If using Cloudflare, trust its published IP ranges
explicitly before accepting `CF-Connecting-IP`; never trust arbitrary incoming
forwarded headers. Keep the application/status ports private. Publish only the
required SSH/HTTP/HTTPS ports, and preserve SSH when configuring the firewall.

For a migration, obtain the certificate before switching traffic. A DNS challenge
can do this without moving the application first; keep the DNS management token
on its established trusted host when practical. If migrating Certbot's private
configuration, correct absolute paths and explicitly reconfigure renewal for the
new host. A successful initial certificate is not evidence that renewal works.
After DNS cutover, simulate renewal using the intended authenticator, enable the
renewal timer and reload Nginx after a successful renewal. HTTP webroot renewal
does not require storing a DNS API credential on the application VM.

## Preserve the dependencies

The phone process alone is not the complete service when calendar features are
enabled. Move or deliberately connect the existing approved calendar bridge,
owner-only Google credential, account/calendar namespaces and verified freshness
status API. Retain the exact coverage attestation and freshness semantics.
Do not fabricate a healthy mirror response to make appointments available.

Run one calendar mirror scheduler, one availability refresh timer and one phone
instance as primary. Test the copied calendar credentials with a read-only or
dry-run check first. Stop/drain the old scheduler before activating the new one.
Preserve unrelated mail and other services on the source server. Strip event
titles from general service logs; retain safe counts and status fields needed by
the freshness monitor. With stale or unavailable status, the assistant must
continue to withhold availability and use the existing fallback/message policy.

The phone's optional subscription lookup must also work on the new machine.
Verify the actual bounded runner with synthetic facts; checking only whether a
CLI binary exists does not prove subscription authentication or compatibility.

## Stage, drain and switch

1. Keep production live while installing and testing a separate copy. Run the
   existing test suite with fake providers; use a copied SQLite snapshot and fake
   credentials for startup/API checks. Do not start a second real webhook/event
   dispatcher against copied production state.
2. Check stored calls, transcripts, contact assignments, personalities, hours,
   disclosure grants, password hashes, OAuth registrations and provider defaults
   survive the copy. Avoid printing their contents in a deployment report.
3. Optionally run bounded real-provider checks with fictional data, for example
   `scripts.check_answering_providers`. They generate model audio and can incur
   a small provider charge, but their telephone gateway refuses actual dialing.
4. Check active calls twice. Drain in-flight jobs, stop the old phone worker and
   only its associated availability/mirror schedulers, then take final consistent
   SQLite backups of **calls, OAuth and appointment databases**. Copy assets and
   required private runtime configuration. Never make a raw copy of a live WAL
   database without its committed state.
5. Start the new private listener and verify it through the TLS proxy before
   moving DNS/routing. Keep the canonical public URL and OAuth issuer unchanged.
   Update only the intended phone record/route, leaving unrelated tunnel routes
   untouched. Keep the old phone instance stopped and disabled.
6. Verify public HTTPS, password login page, authenticated controls, existing MCP
   authorization, actual SSE connected/heartbeat delivery, WebSocket routing,
   rejection of unsigned Twilio callbacks, provider connectivity and fresh calendar
   status. `/health` alone is only a process/configuration check. A synthetic audio
   check does not prove a real caller heard the audio; label that limitation.
7. Record release hash, runtime, changed routing, private backup locations and
   validation results. Keep deployment-specific host addresses, authentication
   files, screenshots, call records and personal settings outside the public repo.

## Private backups and recovery

Create private application backups as the application account, so SQLite WAL
sidecar files keep the correct ownership:

```sh
.venv/bin/python -m scripts.backup_state --directory /private/phone-backups \
  --cli-auth /private/runtime-home/.codex/auth.json \
  --extra-file /private/calendar/msal_cache.json
```

The command uses SQLite's backup API, checks the three databases, includes
notice/prompt assets, `.env`, the configured Google token and explicitly named
private dependency files, then publishes a completed archive atomically.
`--cli-auth` is optional. The output contains the archive location and size;
it never contains credentials or transcript text. Archive storage must be
owner-only. Test restoration into a separate directory. Backups do not include
provider-hosted recordings, a whole VM, private SSH identities or the TLS setup.

Schedule backups with bounded retention and copy completed archives off the VM
over an authenticated encrypted channel. Check timestamps and checksums. A copy
on the same disk does not protect against losing the VM/disk.

For rollback after cloud traffic has arrived, drain the new instance and move
its **latest** consistent state back to the retained source before switching
routing. Preserve current calls, appointments, OAuth grants and preferences.
Reconcile uncertain external writes. Do not overwrite new data with the old
pre-migration snapshot, or restart both instances against the same traffic.
