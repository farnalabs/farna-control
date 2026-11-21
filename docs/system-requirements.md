# System Requirements

Supported platforms, minimum resources, and database backends for running Farna Control in production. Farna Control v1 supports Linux hosts and native macOS arm64 and x86_64 single-install artifacts.

---

## Supported Platforms

Farna Control v1 targets Linux hosts and native macOS for the native single-install
artifact. Windows native support is Beta scope, opt-in only: the native installer (delivered
by FAR-2337 / FAR-2338) is built, signed and attested by the unified release pipeline
on a `cut`/`recut` release only when the Beta-only `include_windows` input is enabled,
alongside the Linux bundle and the macOS `.dmg`. Its
publication depends on a release running (no signed Windows installer is published
yet), and end-to-end validation on the real Windows installer host is still
outstanding under FAR-2250.

### Platform support matrix (v1)

This matrix is about the **server host** for a production or evaluation
deployment, not about developer workstations (see Development / Evaluation
below).

| Server host platform | v1 status |
|----------------------|-----------|
| **Linux** | Supported (native single-install and container paths) |
| **macOS arm64** | Supported native single-install host (`FarnaControl-<version>-macos-arm64.dmg`, signed/notarized, macos-15 validation; Rosetta not required) |
| **macOS x86_64 (Intel)** | Supported native single-install host (`FarnaControl-<version>-macos-x86_64.dmg`, signed/notarized, macos-15-intel validation; Rosetta not required) |
| **Windows** | Beta scope, opt-in only: native installer built, signed (Azure Trusted Signing) and attested by the release pipeline when the Beta-only `include_windows` input is enabled (FAR-2337 / FAR-2338). Publication depends on a release run (no signed installer published yet); real-host validation (FAR-2250) is outstanding |

Docker Desktop (see Development / Evaluation below) is a developer
convenience on macOS and Windows: it runs the Linux containers inside a Linux
VM, so the deployed stack is still Linux. It is not a supported production
host.

macOS arm64 and x86_64 are native release targets. Separate signed and notarized
`.dmg` artifacts are built on `macos-15` and `macos-15-intel` with pinned native
runtimes and validated separately from the Linux bundle. Intel Macs use the x86_64
artifact; Apple Silicon Macs use arm64. Rosetta is not required. The
macOS installer's trust boundary is Apple Developer ID signing/notarization and
stapling, verified with `codesign`, `spctl` and `hdiutil verify`; it does not
consume the Linux signed release manifest.

Windows native support is in Beta scope and is opt-in only. The native Windows
installer is delivered by FAR-2337 / FAR-2338 (see
[`deploy/windows/BUNDLE-BUILD.md`](../deploy/windows/BUNDLE-BUILD.md)): the release
pipeline builds the native payload (`windows-artifacts.yml`), compiles the Inno
Setup installer (`windows-installer.yml`), signs it with Azure Trusted Signing,
and attaches a build provenance attestation. Signing and attestation happen on
`cut` and `recut` only; `dry-run` compiles the installer contract and skips them.
`release-unified.yml` includes the installer in `SHA256SUMS` and the signed release
manifest only when the Beta-only `include_windows` input is explicitly enabled (it
defaults to false). Because publication depends
on a release running, no signed Windows installer is published yet, and end-to-end
validation on the real installer host (FAR-2250) is still outstanding. Until the
installer is published and that validation is recorded, a Windows-hosted deployment
means Windows Server running the Linux stack in a VM or container host, not native
Windows binaries.

### Install paths

| Platform | Status | Documentation |
|----------|--------|---------------|
| **Docker Compose** | Production-ready | [Deployment guide](./deployment.md) |
| **Self-hosted (bare metal / VM)** | Production-ready | [Self-hosted administration](./operations/self-hosted-admin.md) |
| **Fly.io** | Production-ready | [Deployment journey](./deployment-journey.md) |
| **Railway** | Production-ready | [Deployment journey](./deployment-journey.md) |
| **Native install (Linux, macOS)** | Workstation / evaluation (ADR 031) | [Native single-install bundle](#native-single-install-bundle-linux) |

SQLite is not an install path: it is used only by the automated test suite.

---

## Native Single-Install Bundle (Linux)

The native installer (`scripts/install.sh`) distributes a self-contained
bundle: the backend, a bundled CPython runtime, PostgreSQL 16, Redis 8, and
the built SPA, laid out under `~/.local/opt/farna-control/` with the
`farna-control` command on `PATH`. The table below is
the honest v1 envelope (ADR 031; recenter on what `bundle-v*` artifacts
actually ship - do not widen these rows until the packaging tickets land):

| OS | glibc | Status |
|----|-------|--------|
| Ubuntu 22.04 LTS+ | 2.35+ | Supported |
| Debian 12+ | 2.36+ | Supported |
| Ubuntu 20.04 and earlier | 2.31 and below | Not supported (below the bundled service-binary floor) |
| macOS arm64 | Apple Silicon | Supported native `FarnaControl-<version>-macos-arm64.dmg`; Rosetta not required |
| macOS x86_64 | Intel | Supported native `FarnaControl-<version>-macos-x86_64.dmg`; Rosetta not required |
| Windows | - | Beta scope, opt-in only; native installer built, signed and attested by the release pipeline when the Beta-only `include_windows` input is enabled (FAR-2337 / FAR-2338), published only by a release run (no installer published yet); real-host validation outstanding (FAR-2250) |

The hard requirement is **glibc 2.35 or newer**. The bundle is assembled on an
Ubuntu 22.04 (jammy) build baseline: the bundled PostgreSQL 16 binaries come
from the jammy PGDG pool, Redis 8 is compiled from the pinned upstream tarball
on that same runner, and the Python venv is created there too, so every shipped
binary stays within the glibc 2.35 floor (`scripts/bundle-versions.env`;
`bundle-release.yml` asserts the floor on every build). This covers all
currently-supported Ubuntu LTS releases (22.04 and 24.04) and Debian 12 or
newer. Ubuntu 20.04 (glibc 2.31) and older are below the floor and are not
supported native-install targets.

### Installing with apt (.deb, Ubuntu 22.04 / 24.04)

The same linux-amd64 bundle also ships as a Debian package,
`farna-control_<version>_amd64.deb`, built and validated on Ubuntu 22.04 and
24.04 by `.github/workflows/linux-deb.yml` on every release (and rehearsed on
every dry-run). The release `SHA256SUMS`, the signed `RELEASE_MANIFEST.json`
and the build-provenance attestation all cover it.

The recommended way to install it is the signed apt repository at
`https://apt.farna.dev`, so `apt upgrade` picks up new releases:

```bash
sudo apt-get install -y ca-certificates curl
curl -fsSL https://apt.farna.dev/farna-control-archive-keyring.gpg   | sudo tee /usr/share/keyrings/farna-control-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/farna-control-archive-keyring.gpg] https://apt.farna.dev stable main"   | sudo tee /etc/apt/sources.list.d/farna-control.list
sudo apt update
sudo apt install farna-control

# then, as your normal (non-root) user:
farna-control start --detach
farna-control status
```

The repository is signed with the Farna Control archive key, fingerprint
`CD1B AC68 5400 EAF6 D27C  4D82 5FF1 C9A5 156D 71E1`
(`Farna Control Archive Signing Key <packages@farna.dev>`). The same public key
is committed in the source repository at
`deploy/apt/farna-control-archive-keyring.asc`; you can check the downloaded
keyring with
`gpg --show-keys /usr/share/keyrings/farna-control-archive-keyring.gpg`. The
`signed-by` option limits that key to this one repository, and `apt update`
refuses the repository if its `InRelease` signature does not verify. The
package files themselves are the GitHub release assets: the repository index
records their sizes and hashes, and apt fetches them from the release.

**Upgrading.** Stop the stack first (`farna-control stop`), then:

```bash
sudo apt update
sudo apt upgrade            # or: sudo apt install --only-upgrade farna-control
```

**Pinning a version.** The repository keeps every released version, so you
can stay on one or go back:

```bash
apt list -a farna-control                       # every published version
sudo apt install farna-control=<version>        # add --allow-downgrades to go back
sudo apt-mark hold farna-control                # apt upgrade leaves it alone
sudo apt-mark unhold farna-control              # follow new releases again
```

To follow only one release line instead of holding, add an apt preference,
for example `/etc/apt/preferences.d/farna-control`:

```text
Package: farna-control
Pin: version 0.3.*
Pin-Priority: 1001
```

**Without the repository** (air-gapped hosts, or to install one release by
hand), download the `.deb` and `SHA256SUMS` from the release page instead:

```bash
grep " farna-control_<version>_amd64.deb$" SHA256SUMS | sha256sum --check -
sudo apt install ./farna-control_<version>_amd64.deb
```

What the package does and does not do:

- It installs the bundle under `/opt/farna-control/versions/<version>/` with a
  `/opt/farna-control/current` symlink, and the `farna-control` command at
  `/usr/bin/farna-control`. Its dependencies are computed at build time from
  the system libraries the bundled binaries actually link, plus
  `ca-certificates` and `openssl`; for 0.3.329 that is `libc6 (>= 2.34)`,
  `libcrypt1`, `libgcc-s1`, `libstdc++6`, `zlib1g`, `ca-certificates` and
  `openssl`, all present on a stock Ubuntu 22.04 / 24.04.
- It starts **no** system service and runs nothing as root. Each user runs
  `farna-control start` for themselves, exactly like the tarball install. The
  data dir (`~/.local/share/modulo/data`) and its runtime keys are created on
  that user's first start.
- `sudo apt remove farna-control` and `sudo apt purge farna-control` remove the
  program only. They never touch any user's data dir; delete
  `~/.local/share/modulo/data` yourself if you want the data gone. Stop the
  stack (`farna-control stop`) before removing or upgrading the package.
- Upgrade with apt (`sudo apt upgrade` from the repository, or by installing
  the newer `.deb` by hand). The in-app `farna-control upgrade` command is
  disabled for the package install (it would rewrite files apt owns) and tells
  you to use apt instead.

**If you also have the tarball install** (`scripts/install.sh`): the two
coexist without overwriting each other (`~/.local/opt/farna-control` +
`~/.local/bin/farna-control` vs `/opt/farna-control` + `/usr/bin/farna-control`),
and they share the same per-user data dir; the data-dir lock stops both running
at once. Ubuntu puts `~/.local/bin` ahead of `/usr/bin` on `PATH`, so a plain
`farna-control` keeps running the **tarball** install for that user. To switch
to the package, stop the stack, then remove `~/.local/bin/farna-control` (and,
once you no longer need it, `~/.local/opt/farna-control`), or call
`/usr/bin/farna-control` explicitly. The packaged command always uses its own
install root, even if the tarball install's `env.sh` is sourced, and prints a
note on `start` when a tarball install is also present.

### Supported architectures

| Architecture | Status |
|--------------|--------|
| linux-amd64 (x86_64) | Supported |
| linux-arm64 | Planned (pins exist; no artifact published yet) |
| macOS arm64 | Supported native artifact; Apple Silicon (`FarnaControl-<version>-macos-arm64.dmg`) |
| macOS x86_64 | Supported native artifact; Intel (`FarnaControl-<version>-macos-x86_64.dmg`) |

`linux-arm64` is not installable yet: the bundle build matrix carries only
`amd64`, so no `linux-arm64` tarball is published even though the arm64 pins
already exist in `scripts/bundle-versions.env` and the matrix is structured to
accept them. `scripts/install.sh` preflight accepts `aarch64`/`arm64` for the
architecture case, but there is no `linux-arm64` bundle to download.

### Disk and RAM envelope

The native bundle is roughly the same footprint as Compose, minus Docker
itself: ~2 GB for the install roots, and the bundled Postgres data dir
grows with run history.

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| RAM | 4 GB | 8 GB |
| Disk (install + data dir) | 10 GB | 20 GB |
| Swap | Required (Postgres needs commit headroom) | - |

---

## Minimum Resources

### Development / Evaluation

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 10 GB SSD | 20 GB SSD |
| Docker | Docker Desktop 24+ | Docker Desktop 24+ |
| Python | 3.12-3.14 | 3.12-3.14 |

### Single-Server Production (Docker Compose)

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| CPU | 4 vCPU | 8 vCPU | LLM calls are I/O-bound, not CPU-bound |
| RAM | 8 GB | 16 GB | More RAM for larger pipeline states |
| Disk | 20 GB SSD | 50 GB SSD | Database grows with run history |
| Network | 100 Mbps | 1 Gbps | Webhook delivery, connector calls |

---

## Supported Databases

| Database | Version | Status | Production Ready | Notes |
|----------|---------|--------|-----------------|-------|
| **PostgreSQL** | 16+ | **Supported** | **Yes** | The only supported runtime and production database |
| **SQLite** | 3.x | Test-only | **No** | Used by the automated test suite (in-memory `create_all`), behind the test-only `FARNA_CONTROL_ALLOW_SQLITE=1` opt-in; not deployable |
| **MySQL** | 8+ | Not supported | **No** | `FARNA_CONTROL_DB=mysql` is rejected at startup; there is no production path |
| **MariaDB** | 11+ | Not supported | **No** | `FARNA_CONTROL_DB=mariadb` is rejected at startup; there is no production path |

### Why only PostgreSQL

PostgreSQL 16 is the only supported runtime database (ADR 068).

- **The tenant boundary is PostgreSQL row-level security.** RLS is what isolates
  one organisation's rows from another's. SQLite (test-only) has no equivalent, so
  a non-Postgres deployment could only offer weaker application-level filtering.
- **The Alembic migration chain is PostgreSQL-only.** The `0350` baseline is
  PostgreSQL DDL, so a fresh SQLite `alembic upgrade heads` fails at the first
  migration with a SQL syntax error. Migrations are only ever exercised against
  Postgres.
- **The native installer bundles PostgreSQL 16**, so a separate database server is
  not a packaging reason to offer SQLite (ADR 031).

SQLite remains in the automated test suite because in-memory `create_all` is
fast and needs no database server; that is a test harness, not a deployment
backend. The settings validator accepts only `postgres` and `sqlite`:
`mariadb` and `mysql` are rejected outright, and `sqlite` is accepted only when
the test-only `FARNA_CONTROL_ALLOW_SQLITE=1` opt-in is set alongside a SQLite
`DATABASE_URL`, so nothing but `postgres` reaches a deployment.

### PostgreSQL Requirements

- **Version**: 16 or later
- **Extensions**: none required; `gen_random_uuid()` is built into PostgreSQL 16+ (core since PG 13)
- **Connection**: Async via `asyncpg` driver
- **TLS**: `sslmode=require` (or stronger) recommended for production; the
  `sslmode` in the database URL is honored. Accepted values: `disable`,
  `require`, `verify-ca`, `verify-full`; `prefer`/`allow` are rejected at
  startup (they silently downgrade to plaintext). No `sslmode` = plaintext.
- **Schema**: Alembic-managed migrations run on startup

---

## Required Services

| Service | Version | Required | Purpose |
|---------|---------|----------|---------|
| PostgreSQL | 16+ | **Yes** (production) | Primary data store |
| Redis | 8+ | **Yes** (production) | SAQ task queue, rate limiting, event broker |
| Python | 3.12-3.14 | Yes | Application runtime (`requires-python = ">=3.12,<3.15"`; the Docker images build on 3.14) |
| `uv` | 0.11.13 | Yes | Python package manager (exact pin in `backend/Dockerfile` and `deploy/docker/Dockerfile.all-in-one`) |
| Node.js | 20+ | For frontend dev | Frontend build toolchain; CI pins Node 22. Use `pnpm` (the repo pins `pnpm@11.28.13` via `packageManager`), not npm |
| Docker | 24+ | For Docker Compose | Container runtime |

### Redis Requirement Table

| Deployment Type | Redis Required? | Reason |
|----------------|-----------------|--------|
| Single replica, single process | **Yes** | The dispatcher enqueues every run to SAQ's Redis queue (`core/dispatch.py`) and cron/polling triggers fire via Redis-backed SAQ system crons (`fire_due_triggers`, `fire_cron_trigger`, `fire_polling_trigger` in `core/saq_worker.py`); `api/main.py` refuses to boot without `REDIS_URL`. This reconciles with the "Redis \| Yes (production)" prerequisite row above and [`docs/quickstart.md`](./quickstart.md) §3b. |
| Multiple replicas | **Yes** | SAQ worker coordination, distributed rate limiting |
| Horizontal scaling | **Yes** | Cross-replica event broker, cron triggers |
| Production with 2+ backend pods | **Yes** | See [`docs/deployment.md`](./deployment.md) §Scaling |

---

## Network Requirements

### Outbound (optional, per configuration)

| Destination | Port | Protocol | Purpose |
|-------------|------|----------|---------|
| LLM API endpoints | 443 | HTTPS | Model backend calls (Anthropic, OpenAI, etc.) |
| Connector API endpoints | 443 | HTTPS | GitHub, GitLab, Linear, etc. |
| OIDC/SAML provider | 443 | HTTPS | SSO authentication |
| OTel collector | 4317 | gRPC | Telemetry export (when enabled) |
| E2B API | 443 | HTTPS | Sandboxed agent runtime |

### Inbound

| Port | Protocol | Purpose |
|------|----------|---------|
| 443 | HTTPS | API + Web UI (via reverse proxy) |
| 80 | HTTP | Redirect to HTTPS |

With default settings and no connectors configured, Farna Control makes **zero external network calls**. See [`docs/operations/network-egress.md`](./operations/network-egress.md) for the full egress audit.

---

## Browser Support

| Browser | Supported | Notes |
|---------|-----------|-------|
| Chromium 120+ | Yes | The only browser the E2E suite installs (`playwright install chromium` in CI and in the deploy workflow) |

Firefox, Safari, and Edge are **not** covered by the automated E2E matrix; the
Playwright config (`frontend/playwright.config.ts`) declares no `projects` block,
so a single default Chromium browser runs. Treat the other three as untested
here.

---

## Farna Control Website Version Display (Scope-Out Decision)

**Decision recorded per FAR-1872 (d):** A grep of `modulo-website/src/**` found ZERO version strings and ZERO ghcr tags.

**Explicit scope-out:** The marketing website (farna.dev, served from `modulo-website` repo) does not display version badges, changelog mirrors, or a downloads page in this release. This is a deliberate deferral: the website surfaces product capabilities and documentation; version-specific download/install links live in the product docs (`docs/deployment.md`, `docs/upgrade-process.md`, `docs/release-process.md`) and the GHCR package page.

If a version badge, changelog mirror, or downloads page is desired for the website, it should be tracked as a separate feature ticket in the `modulo-website` repo.

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Quickstart | [`docs/quickstart.md`](./quickstart.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment journeys | [`docs/deployment-journey.md`](./deployment-journey.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Configuration precedence (native installs) | [`docs/configuration-precedence.md`](./configuration-precedence.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
| Upgrade process | [`docs/upgrade-process.md`](./upgrade-process.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Network egress | [`docs/operations/network-egress.md`](./operations/network-egress.md) |
