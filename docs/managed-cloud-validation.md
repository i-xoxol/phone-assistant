# Managed option validation — cloud status: UNVERIFIED

Local development evidence, November 9, 2026. No Azure/AWS resources were
provisioned, no production configuration changed, and no telephone calls made.

| Check | Evidence |
|---|---|
| Isolated Linux Docker build | `managed` image built from public source; nonroot app user |
| Real PostgreSQL 17 | 172 selected provider-mocked tests passed, covering calls, idempotency, contacts/languages, personalities, live updates, disclosure approvals, passwords, MCP and appointments |
| Local compose | PostgreSQL, app and localhost-only HTTPS proxy became healthy |
| Local HTTPS portal | Pairing and password login passed using the demo CA, without disabling certificate checks or installing a system CA |
| Container restart | Synthetic completed call/transcript, owner password and a synthetic prompt file remained available |
| Azure Bicep | Compiled using Bicep CLI 0.49.2, without account login or provisioning |
| Publication audit | Tracked-source secret/runtime-file scan passed |

These tests use synthetic records and fake telephone/provider gateways. The
local Docker host was an existing Linux development server, not an Azure
emulator. File persistence there uses Docker volumes, not Azure SMB, and the
database runs in a container, not Flexible Server. Thus these results **do not
verify an Azure deployment**. The local Windows Docker engine did not become
available, so it was not used for the container tests.

Run the repeatable local checks described in [the deployment guide](managed-cloud.md).
GitHub's existing SQLite matrix and the managed PostgreSQL/compose workflow
remain separate checks. Their results do not remove the cloud-unverified label.

Before a production cutover, complete the cloud integration checklist in the
guide, a restore drill and a deliberate offline migration of any existing state.
Do not treat a fresh managed database as a migrated copy of the VPS.
