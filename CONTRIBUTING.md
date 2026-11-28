# Contributing to Farna Control

- [Welcome](#welcome)
- [Development Setup](#development-setup)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [CI/CD](#cicd)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)
- [Security](#security)

---

## Welcome

Farna Control is a self-hosted Delivery Governance Platform for governing the
software development lifecycle as AI and automation take on more of the work. It
provides composable pipelines of atomic AI agents that record, evaluate, control,
and improve automated work between existing tools like GitHub, GitLab, and Slack.

We're glad you're here. Be respectful, constructive, and assume good faith.

By contributing, you agree that your contributions are licensed under the
project's [Business Source License 1.1](LICENSE).

Use the [bug report](.github/ISSUE_TEMPLATE/bug_report.yml) and [feature
request](.github/ISSUE_TEMPLATE/feature_request.yml) templates when opening
issues.

All participants are expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Be respectful, constructive, and assume
good faith.

---

## Development Setup

### Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | >= 3.12 | Managed via `uv` |
| Node.js | >= 20 | Frontend tooling |
| Docker | Latest | Postgres, Redis |
| uv | Latest | [Install uv](https://docs.astral.sh/uv/getting-started/installation/) |

### Required services

The project needs a running PostgreSQL 16 and Redis 8 instance. Use Docker
Compose to start them.

Both compose files **require** a real `FERNET_KEY` and refuse to start without
one: a fixed placeholder cannot encrypt a stored credential, and a stack that
starts without a valid key accepts a login and then fails the first time you
save one (FAR-2181). Compose interpolates the whole file, so this applies even
when you ask for only `db-local`/`redis-local`. Create `.env` once at the repo
root:

```bash
printf 'FERNET_KEY=%s\n' "$(openssl rand -base64 32)" > .env
```

PowerShell equivalent — write it BOM-free, because a BOM breaks compose's
`.env` parsing:

```powershell
[IO.File]::WriteAllText("$PWD\.env", "FERNET_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')")
```

Then start them:

```powershell
docker compose -f docker-compose.local.yml up -d db-local redis-local
```

This starts PostgreSQL on `localhost:5434` and Redis on `localhost:6380`
with the following defaults:

| Variable | Default |
|---|---|
| `POSTGRES_USER` | `farna_control` |
| `POSTGRES_PASSWORD` | `changeme` |
| `POSTGRES_DB` | `farna_control` |
| `DATABASE_URL` | `postgresql+asyncpg://farna_control:changeme@localhost:5434/farna_control` |
| `REDIS_URL` | `redis://localhost:6380/0` |

`POSTGRES_PASSWORD` defaults to `changeme`. `docs/quickstart.md` exports
`POSTGRES_PASSWORD=farna_control` instead and pairs it with the matching
`DATABASE_URL`, so keep the password and the URL in step with each other.

PostgreSQL 16 is the only supported runtime database (ADR 068). There is no
MariaDB/MySQL option: `FARNA_CONTROL_DB` rejects those values at startup. SQLite
is used only by the automated test suite and CI tooling, behind the test-only
`FARNA_CONTROL_ALLOW_SQLITE=1` opt-in (see "Running the test suite" below).

---

## Quick Start

```powershell
# 1. Clone the repository
git clone https://github.com/farnalabs/farna-control.git farna-control
cd farna-control

# 2. Start infrastructure
#    Compose refuses to start without a valid FERNET_KEY — generate one once
#    into .env (see "Required services" above for the PowerShell form).
printf 'FERNET_KEY=%s\n' "$(openssl rand -base64 32)" > .env
docker compose -f docker-compose.local.yml up -d db-local redis-local

# 3. Install backend dependencies
cd backend
uv sync --frozen
cd ..

# 4. Run database migrations
cd backend
uv run alembic upgrade heads
cd ..

# 5. Install frontend dependencies
cd frontend
pnpm install --frozen-lockfile
cd ..

# 6. Configure environment
$env:SECRET_KEY = "dev-secret-key-32-bytes-at-least-here!"
# Generate your own key, e.g.: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
$env:FERNET_KEY = "<generate-your-own-fernet-key>"
$env:DATABASE_URL = "postgresql+asyncpg://farna_control:changeme@localhost:5434/farna_control"
$env:REDIS_URL = "redis://localhost:6380/0"
$env:FARNA_CONTROL_USERS = "admin:admin"
```

**Terminal 1 — backend**

```powershell
cd backend
uv run uvicorn farna.api.main:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 — frontend**

```powershell
cd frontend
pnpm run dev
```

The backend is available at `http://localhost:8000` and the frontend at
`http://localhost:5173`.

**Background workers (required for pipeline execution and cron)**

Pipeline runs and scheduled triggers execute through background SAQ workers.
When running the API outside containers you must start them yourself:

```powershell
# Runs worker — executes run jobs
uv run python -m saq farna.core.saq_worker.runs_settings

# System worker — scheduler + reconcile + crons (requires SAQ_AUTH_USERNAME/SAQ_AUTH_PASSWORD)
$env:SAQ_AUTH_USERNAME = "admin"; $env:SAQ_AUTH_PASSWORD = "admin"
uv run python -m farna.core.saq_worker
```

The `python -m saq` argument is the settings module (no `worker` subcommand in
SAQ 0.26.4). With only Postgres + Redis + uvicorn running, no pipeline run
executes and no trigger fires — the workers are mandatory. If you used
`docker compose -f docker-compose.local.yml up -d` with the full local stack,
`saq-runner` and `saq-system` services launch both for you.

### Quick setup with Docker Compose (all services)

```powershell
docker compose up -d
```

This starts the database, Redis, backend, and frontend together. It needs the
`.env` from "Required services" above (`FERNET_KEY`) — compose fails loudly
without it. The backend auto-seeds an admin user based on the `FARNA_CONTROL_USERS`
environment variable.

### Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SECRET_KEY` | Yes | — | JWT signing key (min 32 bytes) |
| `FERNET_KEY` | Yes | — | Fernet encryption key for credentials (valid urlsafe-base64 key, min 32 bytes) |
| `DATABASE_URL` | Yes | — | Database connection string (required, no static default); a SQLite URL is accepted only when `FARNA_CONTROL_DB=sqlite` and `FARNA_CONTROL_ALLOW_SQLITE=1`, both test-only |
| `FARNA_CONTROL_DB` | No | `postgres` | Database dialect (`postgres`; `sqlite` is test-only and also requires `FARNA_CONTROL_ALLOW_SQLITE=1`) |
| `REDIS_URL` | No | `redis://localhost:6379/0` | Redis connection for the SAQ broker (required in production) |
| `FARNA_CONTROL_PUBLIC_URL` | No | `http://localhost:8000` | Public-facing URL for OIDC/SAML callbacks |
| `FARNA_CONTROL_USERS` | No | — | Seed admin users (`username:password`) |

---

## Project Structure

```
farna-control/
├── backend/
│   ├── src/
│   │   └── farna/
│   │       ├── api/              # FastAPI routes, middleware, DI
│   │       ├── auth/             # JWT, OIDC, SAML, API keys
│   │       ├── cli/              # Click-based CLI tools
│   │       │   ├── backup.py     #   farna-control backup / restore
│   │       │   └── migrate.py    #   farna-control migrate export / import / verify
│   │       ├── connectors/       # External tool integrations
│   │       ├── core/             # Pipeline engine, eval, HITL, triggers
│   │       ├── db/               # SQLAlchemy models, CRUD, migrations, RLS
│   │       │   └── migrations/    #   Alembic migration scripts
│   │       ├── model_backends/   # LLM provider wrappers
│   │       └── otel_bridge/      # OpenTelemetry ↔ LangGraph bridge
│   ├── tests/
│   │   ├── unit/                 # Unit tests (fast, no DB)
│   │   ├── integration/          # Integration tests (real Postgres)
│   │   └── bdd/                  # BDD tests (pytest-bdd + Playwright)
│   ├── pyproject.toml            # Python deps, tool configs
│   ├── alembic.ini
│   └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── stores/               # Pinia stores
│   │   ├── composables/          # Vue composables
│   │   ├── views/                # Route-level pages
│   │   ├── components/           # Reusable components (shadcn-vue)
│   │   └── __tests__/            # Vitest unit tests
│   ├── tests/                    # Playwright E2E tests
│   ├── package.json
│   └── Dockerfile
├── .github/
│   └── workflows/                # CI/CD pipeline definitions
├── docs/
│   ├── product-map/              # Feature graph entries (see product-map/README.md)
│   ├── security/                 # Security documentation
│   └── deployment/               # Deployment guides
      (Architecture decision records migrated to private tooling repo, 2026-09-02 — FAR-434)
      (product map: frontend/src/manifest.yaml — feature registry + per-route refs, ADR 008)
├── deploy/
│   ├── compose/                  # Non-default compose files: docker-compose.{prod,test}.yml
│   ├── docker/                   # Dockerfile.all-in-one
│   └── fly/                      # fly.staging.toml + Fly helper scripts
├── docker-compose.yml            # Full stack: Postgres + Redis + backend + workers + frontend
├── docker-compose.local.yml      # Local dev infra: Postgres + Redis + observability
├── fly.toml                      # Fly config for control.farna.dev
└── ...                           # dotfile config (.gitignore, ruff.toml, .pre-commit-config.yaml, ...)
```

CLI tools are registered as console scripts in `pyproject.toml`:

| Command | Entry point | Purpose |
|---|---|---|
| `farna-control` | `farna.cli.main:cli` | Native launcher (start/stop/status/env) + backup/restore/apply |
| `farna-control migrate` | `farna.cli.migrate:cli` | Export/import/verify org data |
| `farna-control break-glass` | `farna.cli.break_glass:cli` | Emergency break-glass operations |

---

## Coding Standards

### Python (backend)

All Python code is checked with the following tools. Run them before pushing:

```powershell
cd backend

# Lint and format
uv run ruff check .
uv run ruff format --check .

# Type checking (strict mode)
uv run mypy src/farna/

# Security scanning
uv run bandit -r src/farna/ -ll
uv run semgrep --config=../.semgrep/ .

# Dependency audit
uv run pip-audit

# Import architecture enforcement
uv run lint-imports
```

**ruff configuration** (from `pyproject.toml`):
- Line length: 120
- Target: Python 3.12
- Enabled rule sets: pycodestyle (E, W), pyflakes (F), isort (I), pep8-naming (N),
  pyupgrade (UP), flake8-bugbear (B), flake8-bandit (S), flake8-async (ASYNC),
  ruff-specific (RUF)
- Per-file ignores: test files relax security and bugbear rules; specific files
  exempt naming conventions for SCIM and SAML integrations

**mypy configuration**: strict mode with `pydantic.mypy` plugin. LangGraph,
LangChain, testcontainers, and factory-boy imports are allowed untyped. BDD
step modules have relaxed rules.

### Import architecture (enforced by import-linter)

- `farna.api` must not import `langgraph` directly
- `farna.connectors` must not import `farna.api` or `farna.auth`
- `farna.core`, `.api`, `.connectors` must not import `modulo_cloud`
- `farna.otel_bridge` must not import `core.pipeline_engine`, `hitl_manager`, `eval_engine`

### TypeScript / Vue (frontend)

```powershell
cd frontend

# Lint
pnpm run lint                 # eslint src/**/*.{vue,ts,js}

# Type check
pnpm run type-check           # vue-tsc --noEmit

# Format check
pnpm run lint:fix             # auto-fix lint issues
```

### Pre-commit hooks

Install pre-commit hooks to automatically check staged changes. Run from the
repository root (`.pre-commit-config.yaml` lives there; `pre-commit` is
installed as a global uv tool):

```powershell
pre-commit install
# CI-parity pre-push gate (mypy, vue-tsc, schema freshness,
# merge-conflict-with-main check) — must be installed separately:
pre-commit install --hook-type pre-push
```

The pre-push gate mirrors key commands PR CI runs so a branch that fails CI is
caught locally before the push. Tests are not included: the merge train's
validation (`merge-validate.yml`) runs the full suite on the composed branch
properly, and a best-guess pre-push subset duplicates it while risking failures
on files unrelated to the change. (PR CI does run the tests your change affects
— `Test (Backend) affected` — but that is a selection, not a substitute.)

### Commit guidelines

- Use present-tense, imperative-style commit messages
- Prefix with a conventional-commit scope, e.g. `feat(auth): add OIDC refresh token support`
- PR titles that resolve a Linear ticket carry the ticket identifier, e.g. `feat(FAR-123): ...`
- Keep commits focused on a single concern
- Reference issues and PRs where applicable

---

## Testing

We maintain three test layers with increasing fidelity:

### Unit tests

Fast, no database required. Run from `backend/`:

```powershell
cd backend
uv run pytest tests/unit tests/architecture --tb=short -q --timeout=120
```

Tests marked `awaiting-implementation` are excluded by default. Run them
explicitly with:

```powershell
uv run pytest tests/unit/ -m awaiting-implementation
```

### Architecture tests

Dependency/layer-contract enforcement via import-linter. Run from `backend/`:

```powershell
uv run pytest tests/architecture -q
```

### Integration tests

Require a running PostgreSQL instance. Run from `backend/`:

```powershell
docker compose -f docker-compose.local.yml up -d db-local
cd backend
uv run pytest tests/integration/ -m integration -n 2
```

Uses `testcontainers` for isolated database sessions. Concurrent execution
is supported via `pytest-xdist` (`-n` flag).

### BDD / E2E tests

Require PostgreSQL, Redis, a running backend, and a built frontend. Run from
`backend/`:

```powershell
docker compose -f docker-compose.local.yml up -d db-local redis-local
cd backend
uv run alembic upgrade heads
# Terminal 1 — backend:
uv run uvicorn farna.api.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — frontend (build + preview):
cd ../frontend
pnpm run build
pnpm run preview -- --port 4173
cd backend
uv run pytest tests/bdd/ -m e2e --base-url http://localhost:4173 -q
```

Conventions: Playwright tests run against `?theme=agent`; every interactive
element needs a `data-testid`; use `waitForSelector('[data-loading="false"]')`
— never `waitForTimeout()`.

### Frontend unit tests

```powershell
cd frontend
pnpm run test:unit            # vitest run src
```

### Frontend E2E tests

```powershell
cd frontend
pnpm run test:e2e             # playwright test
```

### Coverage thresholds

| Target | Threshold | Measured by |
|---|---|---|
| Overall backend | 80% | pytest-cov |
| `farna.auth` | 90% | pytest-cov |
| `pipeline_engine` | 85% | pytest-cov |
| `db.rls` | 95% | pytest-cov |

Coverage is enforced in CI via the coverage-thresholds script
(`.github/scripts/coverage-thresholds.sh` on Linux runners). The unit suite runs
unsharded in the merge-train validation, and its `Validation: coverage +
changed lines` job calls that script to `coverage combine` the coverage data and
applies every threshold above to the merged report, so a local `coverage
report` after a manual run needs a `coverage combine` first (`parallel = true`
in `backend/pyproject.toml`). Pull-request CI no longer runs the unit suite -
see [CI/CD](#cicd).

### Running the test suite

PostgreSQL 16 is the only supported runtime database (ADR 068). SQLite is the
test-suite backend only, and the Settings guard inspects the DSN: a SQLite
`DATABASE_URL` is refused unless BOTH `FARNA_CONTROL_DB=sqlite` and the
test-only `FARNA_CONTROL_ALLOW_SQLITE=1` are set. Never set that opt-in in a
deployment.

```powershell
$env:DATABASE_URL = "sqlite+aiosqlite:///./test.db"
$env:FARNA_CONTROL_DB = "sqlite"
$env:FARNA_CONTROL_ALLOW_SQLITE = "1"
uv run pytest tests/unit/ -m 'not integration' -q
```

The integration suite runs against real Postgres via testcontainers and needs no
SQLite opt-in.

---

## CI/CD

All CI runs on hosted Ubicloud runners (ubicloud-standard-2). Workflows are defined
in `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | Every PR, manual | The **fast tier**. Deliberately small so a PR gets a verdict in minutes: `Lint (Backend)` (ruff, ruff-format, mypy, bandit, vulture, semgrep and its rule tests, pip-audit, import-linter, Trivy, plus the product-map and behaviour-bindings gates), `UV lockfile freshness`, `Test (Backend) affected` (only the test files the change touches, selected by `scripts/run_affected_tests.py` with its `--prepush` bounded profile, which also defers the integration/docker/bdd/e2e suites to the train rather than running them without their DB/testcontainer/browser setup), `Frontend lint and type-check` (ESLint + vue-tsc), `Schema freshness`, `Secret scan` (gitleaks), `Copy consistency`, `Check migration heads` (one head, plus numeric-prefix collisions against the base), and `E2E route methods`. No push trigger: main changes only through the merge train, which validates the composed tree with the full suite before merging, and the queue dispatches this workflow on main after a merge. Cost controls: a newer push to a PR cancels that PR's in-flight run (dispatch runs are never cancelled), and `Detect changed paths` skips the frontend and affected-test steps when nothing relevant changed (step-level, so the checks still report success) |
| `merge-validate.yml` | Called by `merge-queue.yml` (merge train), manual | The **full suite**, run once per train on the branch composed from main plus the admitted PRs: unsharded backend unit tests with the coverage floors and changed-lines gate, architecture/connector/BDD-module suites, integration shards, Tier 1b testcontainers, frontend (lint, type-check, WCAG contrast, vitest with coverage, production build, the mobile-nav and visual/token Playwright slices, canvas `@visual` snapshots and the E2E route-coverage gate), a frontend unit-parity job that reruns the queue's exact 4-worker `pnpm run test:unit` (FAR-1947), the Bundled Runner docker harness (boots the runner-ci compose rig - socket-proxy + dind - and runs every docker-marked module in `backend/tests/docker/`, path-filtered against main, FAR-773), the backend pytest-bdd BDD suite (pytest-bdd + Playwright against a served frontend and live backend — not the frontend Playwright E2E specs), the SonarCloud scan and its PR-scoped new-issues gate (the train passes no PR number, so that gate reports a notice; the scan's new-code quality gate still applies), and a summary job that names the failing job |
| `bdd.yml` | Push to main, every PR, manual | Backend BDD/E2E suite: Postgres + Redis, Alembic migrations, frontend build, backend + preview, then the pytest-bdd Playwright scenarios (not the frontend Playwright E2E specs under `frontend/tests/e2e/`). Cost controls (FAR-1527): on `pull_request`, a checkout-free, fail-open, step-level changed-path detector (same pattern as `ci.yml`'s `Detect changed paths`, FAR-1427) skips the expensive steps — Playwright browser install and the E2E suite — unless the change touches backend/heavy paths (anything outside `frontend/` and `docs/`); any unreliable read fails open and runs the full suite. Every event other than `pull_request` (push to main, manual) runs everything. Coverage consequence: frontend-only PRs skip the pre-merge E2E run — it still runs on push to main and in the merge train |
| `changelog-classify.yml` | Every PR | Changelog-label gate: PRs touching user-facing paths (`backend/src/**`, `frontend/src/**`) must carry a `changelog:*` label or the check fails. Also hosts the fast-lane classifier merged in from its former standalone workflow (FAR-1527: identical PR triggers, so one runner per event instead of two), which publishes the synthetic `fast-lane-eligible` check run the merge queue reads. Applies path-derived labels (`db: migrations`, `risk: high`, `area: backend` / `frontend` / `ci` / `docs`) for human triage only — no automation reads these labels |
| `deploy.yml` | Push to main, manual | Deploy pipeline: throttle check → pre-deploy full CI → staging deploy + staging E2E → production deploy + prod smoke tests |
| `merge-queue.yml` | Cron hourly (`7 * * * *`), manual | Merge queue: squash-merges approved PRs to main after CI + approval re-verification; closes Linear tickets; dispatches CI/deploy on main |
| `pr-review.yml` | Manual dispatch | Automated PR review |
| `branch-fixer.yml` | Manual/webhook dispatch | Fixes failing or review-blocked PRs in sandboxes |
| `alpha-handover-report.yml` | Manual dispatch | Verifies alpha handover criteria with evidence |
| `schemathesis-nightly.yml` | Cron nightly, manual | Nightly API contract fuzzing against main |

Jobs run in parallel where possible. Stale jobs are cancelled via
concurrency groups keyed on `${{ github.ref }}`.

### PR-based delivery

Farna Control uses PR-based delivery. Push your branch, open a pull request,
and CI (`ci.yml`) validates it automatically; merging is handled by the
`merge-queue.yml` workflow once checks pass and review approves. For
farnalabs-internal delivery, additional automation helpers
exist in the internal tooling repo (not part of the public contribution flow).

---

## Pull Request Process

### Before submitting

1. Ensure your branch is up to date with `main`
2. Run the test suites and lint checks relevant to your change (see
   [Testing](#testing) and [Coding Standards](#coding-standards))
3. Verify coverage thresholds are met
4. Update the product map entry for any feature changes (see the product map — the
   feature graph lives in `docs/product-map/`, keyed by the `features:` registry and
   route `product_map` refs in `frontend/src/manifest.yaml`; see
   `docs/product-map/README.md`)
5. Update the product map (frontend/src/manifest.yaml) and relevant docs if your change introduces new behaviour

### Review requirements

- PRs are reviewed by the automated PR Reviewer (`pr-review.yml`) and, where
  needed, maintainers. The reviewer checks:
  - Correctness: tests pass, coverage met
  - Architecture: follows ADRs and import contracts
  - Security: no leaked secrets, input validation, RLS enforcement
  - Documentation: product map (frontend/src/manifest.yaml) and relevant docs updated if behaviour changed

### Merge policy

**Squash merge** is used for feature branches. Merging is performed by the
`merge-queue.yml` workflow, which squash-merges approved PRs after CI passes
and re-verifies review approval — no direct commits to `main`. All CI checks
must pass before merge.

Most PRs follow this standard review process. However, a PR whose changed
files are **entirely** test code in the dedicated test directories
(`backend/tests/`, `frontend/tests/`, `tests/`), markdown, or non-YAML
GitHub configuration (e.g. `.github/CODEOWNERS`) may qualify for the "fast
lane" and be merged without review. These file types cannot alter product
behaviour, so the risk profile is different.

Changes that always go through the standard reviewed path:

- Product code (backend, frontend, CLI)
- Database migrations
- Deployment configuration
- CI/CD workflow files
- Any `.yml`/`.yaml` file, including non-workflow GitHub configuration such
  as YAML issue templates

Fast-lane merges are capped at 5 per rolling 24 hours and are reviewed after
the fact. If that post-merge review raises a significant finding, the change
follows the normal reviewed path going forward and the fast lane is suspended
for 24 hours; that suspension is maintainer policy.

If you'd prefer your change to be reviewed before merging regardless, say
so in the PR description.

### Branch naming

Use branch names that reflect the work:

- `task-far-<n>-<short-description>` for Linear-ticket work
- `dist/<short-description>` for distribution batches
- `fix/deploy-<sha>` for CI/deploy hotfixes
- `docs/<short-description>` for documentation

---

## Release Process

### Versioning

Farna Control follows [Semantic Versioning 2.0.0](https://semver.org/). During
the alpha phase (`0.x`), breaking changes may occur in minor releases.

### How releases work

Farna Control does not run a manual release workflow. Version bumps happen at publish
time (via `publish.ps1` verify step / CI) — never per merge — and a changelog
is maintained in the product roadmap. Before a release tag is created, maintainers
update both application version files together and set the `LICENSE` Change
Date to three years after the release date.

### Changelog

A changelog is maintained in the product roadmap — each release adds an entry under
the version heading with notable additions, changes, and fixes.

---

## Security

Security is a top priority. Please report vulnerabilities responsibly.

For details on supported versions, disclosure timelines, and our coordinated
disclosure process, see [`SECURITY.md`](SECURITY.md).

### Security contacts

Prefer GitHub's **Report a vulnerability** (private security advisory) on the
repository page, or email `security@farna.dev`.

- **Email**: `security@farna.dev`
- **Do not** open public GitHub issues for security vulnerabilities

### Security documentation

| Document | Location |
|---|---|
| Secret management | `docs/security/secret-management.md` |
| Input validation guide | `docs/security/input-validation-guide.md` |
| Dependency update policy | `docs/security/dependency-policy.md` |
| Penetration test plan | `docs/security/penetration-test-plan.md` |
| Incident response playbook | `docs/security/incident-response-playbook.md` |

### Security best practices for contributors

- Never commit secrets, API keys, or credentials
- Always use Pydantic validation for API inputs — never accept raw request bodies
- Ensure every new API route and MCP tool handler enforces RLS
- Use advisory locks (`farna.db.repositories.locks`) for concurrency-sensitive
  database operations
- Decrypted credentials must never enter LangGraph state, checkpoint blobs,
  OTel spans, or logs

---

*Thank you for contributing to Farna Control.*
