# Farna Control Helm Chart

Vendor-neutral Helm chart for the Farna Control stack. Deploys backend API, SAQ workers, frontend SPA, and Redis. Postgres is external/optional.

## Prerequisites

- Kubernetes 1.25+
- Helm 3.10+
- A Postgres 16 database (external or embedded)
- Redis 7+ (external or embedded)

## Quick Start

```bash
# Add the chart (requires two Postgres roles: admin + app)
helm install farna-control ./deploy/helm/farna-control \
  --set postgres.host=your-rds-host \
  --set postgres.password=your-password \
  --set postgres.username=farna_control_app \
  --set postgres.adminUsername=modulo \
  --set backend.env.SECRET_KEY=$(openssl rand -hex 32) \
  --set backend.env.FERNET_KEY=$(openssl rand -hex 32) \
  --set backend.env.FARNA_CONTROL_USERS="admin:$(openssl passwd -6 your-admin-password)" \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=$(openssl rand -hex 16)
```

## Configuration

### External Postgres (Default)

The chart does NOT deploy Postgres by default. Farna Control requires **two distinct Postgres roles**:

| Role | Purpose | Used by |
|------|---------|---------|
| `farna_control_app` | Restricted runtime role — DML only, no superuser, no BYPASSRLS | `DATABASE_URL` (backend, SAQ workers) |
| `modulo` | Admin/superuser role — migrations, role bootstrap, DDL | `DATABASE_ADMIN_URL` (entrypoint only) |

The `farna_control_app` role is **created automatically** by `bootstrap_role` on startup — the operator only needs to provision the admin role with sufficient privileges (superuser or CREATEROLE + CREATEDB). A third role, `farna_control_system` (LOGIN, BYPASSRLS — see `postgres.systemUsername`), is created by the same bootstrap with the shared password; the chart wires it into `FARNA_CONTROL_SYSTEM_DATABASE_URL`, without which the `dispatcher_reconcile` system cron refuses to run and backend readiness stays 503 forever.

Set these values:

```yaml
postgres:
  host: your-rds-host.amazonaws.com
  port: 5432
  database: modulo
  username: farna_control_app       # Restricted runtime role (DATABASE_URL)
  adminUsername: modulo      # Admin/superuser role (DATABASE_ADMIN_URL)
  existingSecret: farna-control-secrets  # Secret with "password" key
```

Or set `postgres.password` directly (rendered into a Secret). The password is shared between both roles unless your setup requires separate credentials.

### External Redis (Default)

```yaml
redis:
  embedded: false  # Use external Redis
  host: your-redis-host.cache.amazonaws.com
  port: 6379
  db: 0
  existingSecret: farna-control-secrets  # Secret with "password" key
```

### Embedded Redis (Development)

Embedded Redis requires authentication. Set either `redis.password` or
`redis.existingSecret`; the chart fails at render time when neither is set.
The password is kept in the Kubernetes Secret and passed to Redis through
`REDIS_PASSWORD`, so it is not rendered into the Deployment command. The
same credential is embedded in `REDIS_URL`, and the probes authenticate with
that environment variable.

```yaml
redis:
  embedded: true
  password: replace-with-a-development-password
  # Or use a pre-created Secret containing a `password` key:
  # existingSecret: farna-control-secrets
```

`redis.embedded` is the only switch that controls the embedded Redis Deployment.
(`postgres.enabled`, `redis.enabled` and the former `redisChart.enabled` were
dead config and have been removed. Postgres is always external.)

<a id="required-values"></a>

### Required Values

This section is the canonical reference for the chart's mandatory values; other
files point here instead of restating the failure modes.

The chart **fails at `helm install`** if these values are not supplied (Helm's `required` function enforces this):

| Value | Purpose | Format |
|---|---|---|
| `backend.env.SECRET_KEY` | JWT signing key — all API tokens are signed with this | Any string >= 32 bytes. Generate with: `openssl rand -base64 32` |
| `backend.env.FERNET_KEY` | Connector credential encryption — stored secrets are encrypted at rest with this | URL-safe base64 Fernet key, >= 32 bytes. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `backend.env.SAQ_AUTH_USERNAME` / `SAQ_AUTH_PASSWORD` | SAQ system worker web-UI auth. **The saq-system worker fail-closes without them** — it crash-loops, `dispatcher_reconcile` never runs, and backend `/healthz/ready` stays 503 forever (the backend pod never becomes Ready). | **Non-empty strings.** Generate with: `openssl rand -hex 16`. The fail-closed check (`saq_worker._assert_system_auth_configured`) is truthiness-only, so an **empty string is rejected too** and crash-loops saq-system at boot. |

The two keys in the first two rows are validated at runtime by Pydantic (`_MIN_KEY_LEN = 32` in `settings.py`); the SAQ auth pair is enforced by the worker's fail-closed boot check, not Pydantic. The chart renders all of them into the Kubernetes Secret and all workloads (`backend`, `saq-runner`, `saq-system`) consume them from there.

**Optional but recommended:**

| Value | Purpose |
|---|---|
| `backend.env.FARNA_CONTROL_USERS` | Admin user credentials (`user:password` format). Without this, no login is possible — the app warns at boot but does not crash. |

**Set every environment variable through `backend.env` using the canonical `FARNA_CONTROL_*` name.** That is the only spelling settings reads: no pre-rebrand alias is part of the settings contract, and a bare field-name variable such as `USERS` or `DB` is not read at all. In particular, set the admin user as `backend.env.FARNA_CONTROL_USERS` — with no admin account there is no way to log in — and the system database URL as `backend.env.FARNA_CONTROL_SYSTEM_DATABASE_URL`, which is the only key the chart's helper consults when `postgres.host` is unset.

### Secrets Management

No literal secrets live in `values.yaml`. Use one of:

1. **Inline values** (recommended) — set `postgres.password`, `backend.env.SECRET_KEY`, `backend.env.FERNET_KEY` directly (rendered into a Secret)
2. **existingSecret** — reference a pre-created Kubernetes Secret for Postgres/Redis credentials

### Image Digest Pinning

The chart supports digest-pinned images for reproducibility:

```yaml
backend:
  image:
    repository: ghcr.io/farnalabs/farna-control/backend
    digest: sha256:abc123...  # Preferred: immutable reference
    tag: ""                   # Empty: defaults to the chart's appVersion
```

**Trade-off:** Digest pinning ensures exact image reproducibility but requires
manual updates when new images are published. Tag-based references are simpler
but may pull different images over time.

An empty `tag` resolves to the chart's `appVersion` — the released image tag
that `release-unified.yml` publishes — so the chart version and image version
move together. Set an explicit tag only to pin a different build. The chart
never defaults to the mutable `latest` tag; pass `tag: "latest"` explicitly if
you really want the newest main build.

When both are set the digest wins, and the chart renders a single image
reference (`repository@digest`) — a tag is only used when no digest is set.

### Connection URLs and Secret keys

`DATABASE_URL`, `DATABASE_ADMIN_URL`, and `REDIS_URL` are always present in the
chart Secret, even when empty, because the workloads consume them via
non-optional `secretKeyRef` entries. A missing key would leave pods in
`CreateContainerConfigError`; an empty value surfaces as a normal application
startup error instead. `REDIS_PASSWORD` is rendered only when
`redis.password` (or `redis.existingSecret`) is set.

### Image Pull Secrets (Private GHCR Images)

The GHCR images (`ghcr.io/farnalabs/farna-control/backend`, `.../frontend`) are private.
Set `imagePullSecrets` to pull them:

```yaml
# 1. Create the secret
kubectl create secret docker-registry ghcr-secret \
  --docker-server=ghcr.io \
  --docker-username=<github-username> \
  --docker-password=<github-pat>

# 2. Reference it in values
imagePullSecrets:
  - name: ghcr-secret
```

The secret is rendered into both the ServiceAccount and every pod spec.
Public images would remove the need for this.

### Kubernetes Runtime Provider (service-account token)

By default every pod renders `automountServiceAccountToken: false`: with the
Kubernetes runtime provider off, nothing in the stack calls the Kubernetes
API, and an unmounted token keeps the blast radius of a compromised pod small
(`kubernetes:S6865`).

When you enable the Kubernetes runtime provider (`FARNA_CONTROL_KUBERNETES_ENABLED`)
so agent workspaces run as pods in your cluster, the control-plane workloads
need a namespaced credential to create, exec into and read logs from those
pods. Declare it in values instead of patching the Deployments — a `kubectl
patch` is silently reverted by the next `helm upgrade`:

```yaml
serviceAccount:
  automountServiceAccountToken: true
```

| Workload | Renders | Why |
|---|---|---|
| `backend` | `serviceAccount.automountServiceAccountToken` (default `false`) | profile tests / workspace API calls against the cluster |
| `saq-runner` | `serviceAccount.automountServiceAccountToken` (default `false`) | dispatches workspace pods — create/exec/log |
| `saq-system` | `serviceAccount.automountServiceAccountToken` (default `false`) | workspace sweep + reconciler |
| `frontend` | always `false` | static nginx SPA — never calls the API, knob does not apply |

**Security trade-off:** enabling it hands every backend/SAQ pod a bearer token
whose power is whatever RBAC is bound to the chart ServiceAccount. Keep that
grant a namespaced `Role` scoped to the workspace namespace (pods,
`pods/exec`, `pods/log`) — never a `ClusterRole`. Leave the value `false`
when the provider is off; the default is deliberately the safe one.

The full setup — provider env vars, the least-privilege Role/RoleBinding, and
the workspace-pod ServiceAccount — is in the
[Kubernetes guide](https://farna.dev/control/docs/kubernetes).

### Workspace Egress NetworkPolicy (FAR-1690, FAR-2068)

On the Kubernetes (T3) tier the runtime provider runs agent workspaces as pods.
Egress `deny_all` (profile `network_policy: none`) is **accepted** and resolves
to a **`declared`** egress posture when — and only when —
`workspaceNetworkPolicy.enabled=true`: the chart then authors a `NetworkPolicy`
selecting the workspace pods (`modulo.provider: kubernetes`) with a
deny-by-default egress rule plus an exact-target (CIDR + port) allowlist. The
cluster CNI enforces this policy; Farna Control records the posture as declared (never
an observation — Farna Control does not see network traffic).

`workspaceNetworkPolicy.enabled=false` is the default and the prerequisite in
the other direction: with it off the manifest does not exist, so the chart
injects no enforcement signal and a Kubernetes `deny_all` claim stays
fail-closed (**refused**), never declared but unenforced. Enabling the value
also sets `FARNA_CONTROL_WORKSPACE_NETWORK_POLICY=enabled` in the control-plane
ConfigMap, which is the signal the backend gates the `declared` resolution and
the provider's acceptance of an egress `none` workspace on. A `selected`-mode
egress claim (with or without an allowlist) remains **refused** in every
configuration, with a typed error naming NetworkPolicy as the remediation —
the manifest cannot express a host:port allowlist without a higher-level
controller.

```yaml
workspaceNetworkPolicy:
  enabled: true                # off by default
  allowDns: true               # permit cluster DNS (UDP/TCP 53)
  dnsNamespace: ""             # narrow the DNS rule to one namespace when set
  egressAllowlist:
    - cidr: 10.0.0.0/8
      ports:
        - protocol: TCP
          port: 5432
    - cidr: 0.0.0.0/0
      ports:
        - protocol: TCP
          port: 443
```

| Value | Default | Meaning |
|---|---|---|
| `workspaceNetworkPolicy.enabled` | `false` | Render the workspace egress NetworkPolicy and inject the `FARNA_CONTROL_WORKSPACE_NETWORK_POLICY=enabled` enforcement signal; when off, Kubernetes `deny_all` is refused |
| `workspaceNetworkPolicy.allowDns` | `true` | Permit UDP/TCP 53 so deny-by-default does not break DNS |
| `workspaceNetworkPolicy.dnsNamespace` | `""` | Namespace holding cluster DNS; when set the DNS rule's `namespaceSelector` is narrowed to it, otherwise left cluster-wide (`{}`) |
| `workspaceNetworkPolicy.egressAllowlist` | `[]` | Exact-target egress entries: `{cidr, ports: [{protocol, port}]}` |

Allowlist entries are validated at render: `protocol` must be TCP/UDP/SCTP
(case-insensitive) and `port` must be in 1-65535, so typos fail fast instead of
being silently rejected by the API server.

**Posture and prerequisites.** Farna Control authors the manifest and the cluster CNI
enforces it; Farna Control records that as a **`declared`** egress posture (never an
observation — Farna Control does not see network traffic). This requires a CNI that
enforces NetworkPolicy (e.g. Calico, Cilium); `kindnet` (kind's default) does
not, so a policy applied there proves nothing. A NetworkPolicy cannot express a
hostname — resolve targets to CIDRs before listing them — and some CNIs silently
drop fragmented traffic under NetworkPolicy, a CNI limitation this manifest
cannot express.

## EKS Deployment

See `values.eks.example.yaml` for a validated EKS configuration using AWS managed services (RDS, ElastiCache, ALB).

```bash
# Deploy (SECRET_KEY, FERNET_KEY and SAQ auth are mandatory — see Required Values)
helm install farna-control ./deploy/helm/farna-control \
  -f deploy/helm/farna-control/values.eks.example.yaml \
  --set postgres.password=<rds-password> \
  --set backend.env.SECRET_KEY=$(openssl rand -base64 32) \
  --set backend.env.FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())") \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=<saq-auth-password>
```

## Local Development (kind)

```bash
# Create kind cluster
kind create cluster --name farna-control-dev

# Deploy with embedded Redis (requires two Postgres roles: admin + app)
helm install farna-control ./deploy/helm/farna-control \
  --set redis.embedded=true \
  --set redis.password=replace-with-a-development-password \
  --set postgres.host=host.docker.internal \
  --set postgres.password=changeme \
  --set postgres.username=farna_control_app \
  --set postgres.adminUsername=modulo \
  --set backend.env.SECRET_KEY=dev-secret-key-not-for-production-32b! \
  --set backend.env.FERNET_KEY=dev-fernet-key-not-for-production-32b!! \
  --set backend.env.FARNA_CONTROL_USERS="admin:admin" \
  --set backend.env.SAQ_AUTH_USERNAME=admin \
  --set backend.env.SAQ_AUTH_PASSWORD=admin
```

## Upgrade

```bash
helm upgrade farna-control ./deploy/helm/farna-control \
  -f deploy/helm/farna-control/values.yaml
```

## Uninstall

```bash
helm uninstall farna-control
kubectl delete namespace farna-control  # If namespace.create=true
```

## Architecture

### Services

| Service | Description | Port |
|---------|-------------|------|
| `backend` | FastAPI API server | 8000 |
| `saq-runner` | Pipeline job executor | N/A |
| `saq-system` | Scheduler + system crons | 8081 |
| `frontend` | Vue SPA (nginx) | 80 |
| `redis` | Queue backend (embedded only) | 6379 |

### Security

- **Pod Security Admission:** `restricted` enforcement at namespace level
- **Security Context:** `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: ALL`
- **seccompProfile:** `RuntimeDefault` on all pods
- **readOnlyRootFilesystem:** Enabled on all containers. Frontend nginx uses `command: ["nginx", "-g", "daemon off;"]` to skip the docker-entrypoint (which writes into `/etc/nginx/conf.d/`); the nginx config is provided via a ConfigMap mount. Backend/SAQ runners use writable `/tmp` emptyDir mounts.
- **Service-account token:** `automountServiceAccountToken: false` on every pod by default; opt in per the *Kubernetes Runtime Provider* section above when the Kubernetes runtime provider is enabled.

### Resource Management

- **ResourceQuota:** Namespace-level limits for CPU, memory, and pod count
- **LimitRange:** Default container resource requests and limits
- **HPA:** Optional autoscaling for backend and frontend

## Validation Status

- [x] `helm lint` passes with default values
- [x] `helm lint` passes with EKS example values
- [x] `helm template` renders without errors
- [x] **Live EKS validation (FAR-1052, 2026-09-23):** deployed end-to-end on a
  real EKS cluster (`farna-control-validation`, EKS 1.34, 2× `m7i-flex.large` managed
  nodes, in-cluster embedded Redis, Postgres external in a sibling namespace,
  images `365370368472.dkr.ecr.us-east-1.amazonaws.com/modulo/{backend,frontend}:dev-20260923`).
  All workloads became Ready with backend/worker pods spread across **both**
  nodes (no co-location); backend `/healthz/ready` returned 200 with
  `dispatcher_reconcile` and the full gate list green — proving the FAR-1158
  readiness split (liveness `/healthz`, readiness `/healthz/ready`) holds on
  multi-node Kubernetes. Two gaps found and fixed during this run: SAQ system
  auth was documented as optional (it is fail-closed required — see Required
  Values), and `FARNA_CONTROL_SYSTEM_DATABASE_URL` was not wired by the chart
  (without it `dispatcher_reconcile` never runs and readiness never passes).
- [x] **Live EKS re-validation (FAR-2770, 2026-11-26): PASSED.** A fresh install
  migrated its database, became Ready, served `/healthz/ready`, signed in through
  the UI, created a model backend and ran a pipeline to completion, then survived
  `helm upgrade` and `helm rollback`. This supersedes the blocked 2026-11-25
  attempt: that attempt failed only on the fresh-database migration defect, which
  FAR-2777 fixed (PR #3527) by replacing the retired
  `alembic upgrade 0001_v2_identity_org` bootstrap with a single
  `alembic upgrade heads`.

  Cluster identity: `arn:aws:eks:us-east-1:365370368472:cluster/farna-control-validation`
  (AWS account 365370368472), created 2026-11-26 with eksctl in `us-east-1`,
  EKS 1.34 (Kubernetes `v1.34.12-eks-9adbfea`), 2 x `m7i-flex.large` ON_DEMAND
  managed nodes (2 vCPU / 8 GiB each), chart `farna-control-0.3.330`, embedded
  Redis (`redis.embedded=true` with `redis.password`), Postgres `postgres:16` in
  the sibling namespace `farna-control-db` whose admin role (the chart's default
  superuser) holds CREATEROLE + CREATEDB so `bootstrap_role` can create the app
  and system roles,
  ingress disabled. Images were built from the current tree for `linux/amd64` and
  pushed to ECR in AWS account 365370368472 with tag `helm-validate-20261126`:

  | Image | Tag | Digest |
  |---|---|---|
  | backend | `helm-validate-20261126` | `sha256:cf7da1e43e5d08d9aef45ed0f380aec995d1c94116d02ca47b8085c7e7f1e74a` |
  | frontend | `helm-validate-20261126` | `sha256:441b4990e3ae64d3bbda5c35056e1b0eda66205c263fe8e53f6e1ff18bce0698` |

  Commands:

  ```bash
  helm lint deploy/helm/farna-control -f values-validation.yaml
  helm install farna-control deploy/helm/farna-control \
    -f values-validation.yaml -n farna-control --create-namespace \
    --wait --timeout 15m
  kubectl -n farna-control get pods -o wide
  kubectl -n farna-control logs deployment/farna-control-backend
  ```

  What was proven on 2026-11-26:

  - **Fresh-database migration.** The backend container ran one
    `alembic upgrade heads` and migrated an empty database from
    `0350_baseline_schema` to `0368_invitations_quarantine_rls`, with no revision
    literal and no psql widening step, then logged
    `Migrations complete. Starting uvicorn...`.
  - **All five workloads Ready.** `backend`, `frontend`, `redis`, `saq-runner`
    and `saq-system` each reached `1/1 Running`. The backend, frontend and redis
    pods ran on one node and the two SAQ workers on the other, so the stack is
    not co-located on a single node.
  - **Readiness.** `GET /healthz/ready` returned HTTP 200 with top-level
    `"status":"ok"` and the readiness gates green (`database`, `redis`,
    `checkpointer`, `migrations`, `saq_workers`, `system_crons`,
    `dispatcher_reconcile`, `stale_run_recovery`, `slot_reconciliation`,
    `runner_*`, `decision_record_reconcile`). The advisory `break_glass` check
    reported `degraded` because the optional `FARNA_CONTROL_BREAK_GLASS_*`
    secrets and URL were not set on this validation cluster; that is reported
    separately and does not change the top-level status.
  - **Sign-in through the UI.** A headless Chromium loaded the SPA at `/login`,
    signed in with the seeded admin, and landed on the dashboard with zero console
    errors and zero failed requests.
  - **Model backend and `FERNET_KEY`.** Creating a model backend (built-in stub
    provider, no model spend) stores the credential encrypted; the read-back
    decrypts it, and the run below proves the hub could decrypt it to build the
    adapter.
  - **Pipeline run to completion.** A one-node pipeline ran to terminal status
    `complete` (run id `2384d050-194b-4599-bbf5-0cf235542b29`), producing a run
    record the API and the UI both serve.
  - **Upgrade and rollback.** `helm upgrade` with a changed value moved the release
    to revision 2; the backend pod rolled and became Ready. `helm rollback` returned
    it to revision 1 (recorded as revision 3, "Rollback to 1"). After the rollback
    `/healthz/ready` was 200 `ok` again, a fresh sign-in succeeded, the pipeline
    list returned the pipeline, and the Pipelines page in the UI listed it.

  Observed on this fresh install and owned separately (FAR-2803, not fixed here):
  the boot seed logs
  `[boot] seed environment_profiles: FAILED (ProgrammingError(... new row violates row-level security policy for table "environment_profiles"))`
  and leaves `environment_profiles` **empty** (0 rows); the 2026-11-26 run
  reproduced this on two boots. It did not block readiness or the pipeline run
  above (a plain agent node needs no environment profile), but a fresh install
  ships no default environment profiles until FAR-2803 lands.

  Superseded attempt (2026-11-25, FAR-2770; PR #3525): the chart installed and four
  of the five workloads ran, but the backend crash-looped on
  `ERROR [alembic.util.messaging] Can't locate revision identified by '0001_v2_identity_org'`
  because `backend/entrypoint.sh` still booted that retired revision, so no backend
  pod became Ready and readiness, sign-in, model backends, runs, upgrade and
  rollback were unproven. Fixed by FAR-2777; re-validated by the 2026-11-26 run
  above.
- [ ] **Kubernetes recovery is NOT validated (FAR-2675).** The chart sets no
  `FARNA_CONTROL_SNAPSHOT_DIR` and ships no pre-upgrade hook, so `helm upgrade` on
  this chart takes **no** database snapshot. After a failed migration on the Helm
  path there is nothing to restore, and `helm rollback` reverts Kubernetes objects
  only; it does not restore data. Recovery on Kubernetes has not been proven by
  the 2026-09-23 run or by the 2026-11-26 run. FAR-2675 is open.
- [ ] Live kind validation (FAR-1053)

## Follow-ups

- **FAR-1053:** Live kind validation (EKS done — see Validation Status)
- **Fresh-install migration blocker (found 2026-11-25, FAR-2770; FIXED 2026-11-26):**
  `backend/entrypoint.sh` no longer runs `alembic upgrade 0001_v2_identity_org`.
  FAR-2777 (PR #3527) replaced it with a single `alembic upgrade heads`, and the
  2026-11-26 validation above migrated a fresh database on the chart's own backend
  image. The old behaviour crash-looped every fresh install that boots the API-only
  backend image through `backend/entrypoint.sh` (the Helm chart and the root
  development `docker-compose.yml`); the production Docker Compose install was
  unaffected.
- **FAR-2803:** boot seed for `environment_profiles` fails on a fresh install (RLS
  rejection) and leaves the table empty; observed during the 2026-11-26 run, not
  fixed here.
- **FAR-2675:** pre-upgrade snapshot for the Helm path (documented as a limitation
  in Validation Status; not implemented)
- CI `helm lint` job (requires `workflow`-scoped token)
- Redis persistence (PVC) for embedded mode
