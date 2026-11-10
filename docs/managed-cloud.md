# Optional managed cloud deployment — UNVERIFIED

**Experimental second deployment option. Azure has not been deployed or tested
with a real call.** Passing local tests or compiling infrastructure does not
verify cloud networking, persistent mounts, audio latency or carrier behavior.
The native/VPS and existing Docker SQLite options remain the defaults.

This option uses **Azure Container Apps Consumption**, **managed PostgreSQL**
and **Azure Files**. Azure manages the host OS, container restarts, HTTPS ingress
and database backups. You still maintain application images, secrets, grants,
data retention, restore procedures and billing. It is not a zero-maintenance or
high-availability promise. AWS Fargate is a viable alternative, compared below;
an AWS infrastructure template is not included in this first managed option.

```mermaid
flowchart LR
  T[Twilio signed webhooks and audio WebSockets] <--> A[Azure HTTPS ingress]
  M[MCP clients / private browser portal] <--> A
  A <--> P[One Python container / one worker]
  P <--> V[Configured voice provider]
  P <--> D[Private managed PostgreSQL]
  P <--> F[Azure Files: notices, prompts, optional auth caches]
  P <--> C[Optional external calendar freshness bridge]
```

## What changes

- Set `DEPLOYMENT_MODE=managed` and a secret PostgreSQL `DATABASE_URL`.
  All three stores use it, in separate schemas: `phone_calls`, `phone_oauth`,
  `phone_appointments`. The prefix is configurable. No URL means ordinary local
  SQLite unless managed mode was explicitly requested, in which case startup
  fails. A PostgreSQL failure never silently falls back to SQLite.
- Install `requirements-managed.txt` or build Docker target `managed`.
  The default `runtime` target does not require PostgreSQL dependencies.
- The database compatibility layer supports the SQL forms used by this
  application, with bound values and transaction-scoped locks for existing
  single-writer critical sections. It is not a general SQLite translator.
- `/data` holds ordinary files, not managed-mode SQLite databases. Do not put
  SQLite WAL databases on Azure Files/EFS/network filesystems; see
  [SQLite's restrictions](https://www.sqlite.org/wal.html).
- `/health` is a liveness check. `/readyz` also checks database access and
  required configuration, returning only a readiness boolean.

**One replica, one Uvicorn worker, no scale-to-zero.** Audio bridges and live
supervisor connections are process-local. Multiple workers/revisions can route
a call to the wrong process. A single-revision setting does not prevent rollout
overlap: deactivate the old revision after draining, before starting a new one.
Provider restarts can still interrupt active calls; replicated databases do not
make live audio connections durable.

## Test locally without telephone charges

Prerequisites: Git, Docker Engine/Desktop with Compose v2, Python 3.11+ for the
optional smoke script. No cloud account or provider keys are needed.

```sh
git clone https://github.com/i-xoxol/phone-assistant.git
cd phone-assistant
# Until merged, check out the experimental branch:
git checkout codex/managed-cloud-option
docker compose -f deploy/managed/compose.yaml up --build -d --wait
python -m scripts.managed_smoke
```

Open `https://localhost:18767/live`. Caddy uses a local self-signed certificate;
accept its browser warning only for this localhost demo. The separate loopback
HTTP port 18766 is for the synthetic API smoke test. Generate a **local-demo** pairing code with:

```sh
docker compose -f deploy/managed/compose.yaml exec phone python -m scripts.web_pairing
```

The demo injects a fake telephone gateway and invalid voice credentials, blocks
provider media endpoints, and creates synthetic call records. The fixed demo
password/token is deliberately public and local-only. It does not load `.env`,
expose a public webhook, or dial any number. Do not expose this demo to a LAN or
the internet. A simulated completion endpoint is absent from the real app.

The smoke script prints a synthetic `call_id`. Test persistence:

```sh
docker compose -f deploy/managed/compose.yaml restart phone
python -m scripts.managed_smoke --call-id REPLACE_WITH_SYNTHETIC_CALL_ID
docker compose -f deploy/managed/compose.yaml down
```

`down` keeps the database, file and local-certificate named volumes; `down --volumes` deliberately deletes this
demo's state. It does not touch the default VPS compose project.

For automated HTTPS pairing/password and file persistence checks, leave the
stack running and use `python -m scripts.managed_portal_smoke` (`--sudo` on a
Linux development host if required). It resets **only this local demo's** test
password, trusts its local CA only for that process, restarts the demo phone
container, and checks both the database and a synthetic file afterward.

For database integration tests, start an isolated PostgreSQL 17 instance and
set `TEST_DATABASE_URL` to its **disposable, dedicated** database. The explicit
`--postgres-tests` fixture creates and drops `test_*` schemas there. Never point
it at a production database. Example, in a POSIX shell:

```sh
pip install -r requirements-dev.txt -r requirements-managed.txt
export TEST_DATABASE_URL='postgresql://phone:local-demo-only@127.0.0.1:25432/phone?sslmode=disable'
python -m pytest --postgres-tests tests/test_calls.py tests/test_personalities.py tests/test_appointments_core.py tests/test_web_password.py tests/test_rules_control.py tests/test_appointments_oauth.py tests/test_managed_cloud.py
```

The compose database deliberately has no published port. To run this example,
use another disposable container bound to `127.0.0.1:25432`, or run pytest within
its Docker network with the appropriate hostname. SSL disabled here is for
local containers only; the Azure template verifies PostgreSQL's TLS certificate.

## Prepare an Azure sandbox

Use a **new resource group and hostname**, not the existing production service.
Read the template at `deploy/managed/azure/main.bicep` before deploying. It creates:

| Resource | Initial configuration |
|---|---|
| Container Apps | Consumption profile, 0.5 vCPU / 1 GiB, minimum=maximum=1 |
| PostgreSQL Flexible Server | PostgreSQL 17, B1ms, 32 GiB, private network, 7-day backups, no HA |
| Azure Files | Dedicated LRS SMB share, quota 5 GiB, VNet access, UID/GID 10001 |
| Networking | Dedicated VNet/subnets, private PostgreSQL DNS, HTTPS public app ingress |
| Logs | Log Analytics, 30-day retention and a 0.1 GiB/day ingestion cap |
| Application secrets | Encrypted Container Apps secrets from a secure parameter object |

The dedicated database administrator is the initial application owner. This
is a prototype convenience, not a least-privilege production role setup. Review
and restrict database grants before a production rollout. Storage account-key
mounting uses the stable SMB approach; managed-identity SMB is currently a
separate [preview option](https://learn.microsoft.com/en-us/azure/container-apps/storage-mounts).

1. Install Azure CLI and Bicep, sign into your chosen subscription, register
   `Microsoft.App`, `Microsoft.DBforPostgreSQL`, `Microsoft.Storage`,
   `Microsoft.Network` and `Microsoft.OperationalInsights`. Confirm regional
   availability/quotas for PostgreSQL 17 and B1ms. Create a budget alert first.
2. Build and push a Linux **amd64** image to a registry accessible by Azure.
   The initial template expects a publicly readable image containing source
   only. Credentials and data must never enter the image. Pin its digest:

   ```sh
   docker build --target managed -t YOUR_REGISTRY/phone:YOUR_RELEASE .
   docker push YOUR_REGISTRY/phone:YOUR_RELEASE
   ```

3. Copy `deploy/managed/azure/parameters.example.json` to a private location
   outside Git (for example `phone.parameters.private.json`). Fill its empty
   values; the example deliberately cannot deploy as supplied. Supply:
   `prefix`, `image` (`registry/name@sha256:...`), a random 24+ character
   `databasePassword`, `deploymentAcknowledgement` equal to
   `I_ACKNOWLEDGE_CLOUD_UNVERIFIED`, and `appSettings` as a string-valued object.
   At minimum include your `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, and random `LOCAL_API_TOKEN`.
   Copy only the optional settings you actually use from `.env.example`.
   Do not put secrets directly on a shell command line or in Git.
4. Compile locally first, then validate/preview against your sandbox account.
   The commands below do **not** authorize a production cutover:

   ```sh
   az bicep build --file deploy/managed/azure/main.bicep --outfile /tmp/phone-template.json
   az group create --name phone-sandbox --location eastus
   az deployment group validate --resource-group phone-sandbox --template-file deploy/managed/azure/main.bicep --parameters @/PRIVATE/PATH/parameters.json
   az deployment group what-if --resource-group phone-sandbox --template-file deploy/managed/azure/main.bicep --parameters @/PRIVATE/PATH/parameters.json
   ```

5. **Creating resources incurs charges.** After reviewing the preview, deploy
   with `az deployment group create` using the same arguments. `enableApp`
   defaults to false so this first step creates infrastructure without a live
   phone worker. Provisioning, logs and databases still cost money.
6. Review the Azure Files network/mount permissions, secrets and PostgreSQL
   TLS/grants. Redeploy with `--parameters enableApp=true`. The output `publicUrl`
   is the managed HTTPS hostname; visit `/health` and `/readyz`.
7. Generate a portal code using the container console:
   `python -m scripts.web_pairing`. Configure the owner's profile/contacts/rules.
   Connect a **sandbox** MCP client to `publicUrl/mcp` and complete pairing.
   A new hostname/new database requires a new authorization; existing grants
   are not automatically copied.
8. Only then configure a **separate test Twilio number** with the paths in
   [getting started](getting-started.md), replacing its base URL with the sandbox
   URL. No automatic Twilio or DNS changes are performed by this template.

Custom `publicBaseUrl` is optional. It must match the actual HTTPS origin and
its separately configured DNS/certificate before signed callbacks or same-origin
portal forms can work. Do not disable signatures or origin checks to fix a URL
mismatch. The managed ingress supports WebSockets; its actual idle behavior,
long calls and cancellation paths still require cloud integration tests.

### Optional dependencies and data

- **Voice providers and recording:** existing adapters/tools remain in use;
  cloud tests must verify each selected provider. Audio recordings remain in
  Twilio unless your deployment explicitly exports them. Test notice playback,
  recording callbacks and transferred-call recording before relying on them.
- **Subscription lookups:** use image target `managed-subscription` if desired.
  Authenticate the CLI in the new container environment as the nonroot app user,
  keeping its cache under `/data/runtime-home`. Do not copy laptop/VPS private
  caches into an image. Subscription eligibility, refresh and share permissions
  need a cloud test; API lookup billing is separate. Missing subscription auth
  is not permission to switch to an API that incurs charges.
- **Calendar:** Google token files can live on the private share, but the external
  calendar-mirror/freshness bridge is a separate dependency, not bundled here.
  Keep appointments/coaching disabled until its authenticated endpoint is
  reachable and fresh. Stale data must remain withheld; never set a fake healthy
  mirror status. The template does not move private homelab bridge credentials.
- **Existing data:** this is a fresh-install option. Automatic SQLite-to-PostgreSQL
  migration is **not implemented**. Do not cut over an existing service and expect
  its contacts, tokens, messages or appointment journal to appear. A later migration
  needs a backed-up offline import and row/constraint reconciliation.

## Safe updates, backups and rollback

Do not run `scripts/update_service.py` or `scripts/backup_state.py` as managed
database maintenance tools. The former is the native systemd/SQLite workflow;
the latter refuses PostgreSQL to avoid reporting a misleading empty backup.
Use PostgreSQL PITR/`pg_dump` and separate Azure Files snapshots/backups. Share
soft-delete is not a complete backup strategy. Practice a restore in a separate
resource group, including authentication and appointment idempotency journals.

For an update: prevent new admissions using your planned maintenance routing,
wait for calls/transfers to finish, reconcile pending calendar writes, take
database and file backups, deactivate the old Container Apps revision, verify
there is no running old replica, then deploy/activate the pinned new image.
Do not send live traffic to two revisions. Expect brief downtime. Keep the old
digest and a schema-compatible restore point; never erase a journal to retry an
uncertain call or calendar write. Provider-driven restarts need reconciliation
too: check Twilio before repeating any uncertain outbound request.

The existing production hostname, VPS service and Twilio routing are untouched
by local validation. An eventual cutover is a separate deliberate operation.
Removing a sandbox resource group deletes its database/files too: export any
data you intend to keep before cleanup.

## Projected cost

Planning rates checked **November 9, 2026**, USD, 730 hours/month, East US /
N. Virginia, one always-ready app and a small single-instance managed database.
No signup credits, reservations or free trial assumed. These are infrastructure
estimates, **not measured bills or quotes**:

| Deployment | Expected planning budget | What drives it |
|---|---:|---|
| Azure option implemented here | **about $60–105/month** | App idle/active billing, $16.09 DB+32 GiB, ~$25.55 managed network resources, file/log costs |
| Comparable AWS design, not implemented | **about $75–95/month** | Fargate ~$18, RDS+20 GiB ~$25.66, ALB ~$17, three public IPv4s ~$10.95, EFS/logs/secrets |
| Local managed demo | **No cloud/provider charges** | Runs on your own Docker host with synthetic calls |

Azure app compute alone is ~$11.83/month if eligible for idle billing all month
or ~$39.42 active all month, before its subscription-wide free grants. Background
activity or sustained CPU/network use can prevent idle pricing, so budget for
the active end until measured. At 30 active hours and a **$10 planning allowance**
for ancillary services, the included calculator gives Azure ~$64.60 and AWS
~$81.64. The Azure estimate explicitly includes one managed standard load balancer and
two public IPv4s for the external VNet environment, in addition to the allowance.
The allowance is not a fixed provider tariff; logs, mounts,
transactions and backups must be checked in the actual account.

```sh
python -m scripts.managed_cost --active-hours 30 --ancillary 10
python -m scripts.managed_cost --active-hours 730 --ancillary 20
```

The calculator and [dated rates](../deploy/managed/rates.json) show assumptions
instead of claiming a guaranteed bill. Sources: [Azure Container Apps pricing](https://azure.microsoft.com/en-us/pricing/details/container-apps/),
[Azure billing rules](https://learn.microsoft.com/en-us/azure/container-apps/billing),
[Azure PostgreSQL pricing](https://azure.microsoft.com/en-us/pricing/details/postgresql/flexible-server/),
[official Azure retail rates API](https://prices.azure.com/api/retail/prices),
[AWS Fargate](https://aws.amazon.com/fargate/pricing/),
[RDS](https://aws.amazon.com/rds/postgresql/pricing/),
[ALB](https://aws.amazon.com/elasticloadbalancing/pricing/) and
[public IPv4 pricing](https://aws.amazon.com/vpc/pricing/).

**Excluded:** Twilio numbers/minutes/media/recording, OpenAI/Gemini/ElevenLabs,
subscription fees, substantial internet/cross-zone egress, taxes, HA and NAT
gateways. AWS comparison assumes security-group-restricted public Fargate tasks
to avoid a NAT gateway; private tasks add NAT/endpoints costs. Extra replicas,
larger images/CLI tasks, private endpoints, dedicated profiles and HA can raise
these estimates substantially. Managed hosting removes OS administration, not
the cost or need to design reliable call handling.

## What remains unverified in Azure

- Resource provisioning/quota availability and private DNS connectivity.
- File-share mount ownership, atomic updates, notice serving and CLI auth refresh.
- Real signed Twilio callbacks, WebSockets, long calls, interruption and hangup.
- Transfers/coaching/microphone audio and recording across all call legs.
- MCP persistent event connections and portal behavior through managed ingress.
- Calendar freshness bridge access, booking reconciliation, backup/restore and
  controlled revision updates without routing active calls to another process.

Do not remove the **UNVERIFIED** label until these have dated evidence from the
cloud sandbox. Local PostgreSQL tests and Bicep compilation cover only part of
that checklist.
