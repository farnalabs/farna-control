# Upgrade Process

How to upgrade an existing Farna Control deployment with minimal downtime. Covers version upgrades, database migrations, and rollback procedures.

---

## Project Version Convention

The **single source of truth** for the product version is `backend/pyproject.toml` (`[project].version`). It is mirrored into:

| File | Field |
|------|-------|
| `backend/uv.lock` | `version` on the editable `farna-control` package entry |
| `frontend/package.json` | `"version"` |
| `deploy/helm/farna-control/Chart.yaml` | `version` and `appVersion` |

All of these files carry a plain numeric `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the **git tag only** (e.g. `v0.4.0-rc1`) and is never written into the files.

Bump with `scripts/bump-version.py` (reads the current version from `backend/pyproject.toml`, writes every mirror):

```bash
python scripts/bump-version.py patch        # or: minor | major
python scripts/bump-version.py --set 1.2.3  # explicit version
```

The script fails loudly (non-zero exit, nothing written) if any mirror has drifted away from the source of truth or is already at the target version - it never reports a change it did not make. It writes `backend/uv.lock` directly (the editable `farna-control` package entry), so no separate `uv lock` run is needed after a bump. The `check-uv-lock` pre-commit hook and the CI `UV lockfile freshness` job fail closed when `backend/uv.lock` is stale against `backend/pyproject.toml`; if the resolution itself changed, run `uv lock` in `backend/` and commit the result.

---

## Before You Start

1. **Read the release notes** for the target version – check for breaking changes, new required env vars, and deprecated features.
2. **Check the Alembic migration chain** – review what schema changes will be applied:
   ```bash
   uv run alembic history
   uv run alembic upgrade heads --sql  # preview SQL without applying
   ```
   Run these from `backend/` with `DATABASE_URL` (or the owner-role override
   `DATABASE_ADMIN_URL`) set in the process environment or in the `.env` file
   in `backend/`. `backend/alembic.ini` deliberately carries no default URL, so
   Alembic refuses with an explicit error when neither is configured rather
   than silently targeting a local database (FAR-2183).
3. **Back up the database** – always take a backup before upgrading
   (from the repository root - `scripts/` lives at the repo root, not in
   `backend/`):
   ```bash
   uv run scripts/backup.py --output /backups/pre-upgrade-$(date +%Y%m%d).tar.gz.enc
   ```
4. **Test in staging first** – apply the upgrade to a staging environment with a copy of production data.

---

## Upgrade Paths

### Native single-install (Linux and macOS)

The native single-install path supports the published Linux bundle and the
signed, notarized native macOS arm64 and x86_64 `.dmg` artifacts. Windows native installer support is Beta
scope, opt-in only (FAR-2337 / FAR-2338): the release pipeline builds the native
payload, compiles, signs (Azure Trusted Signing) and attests the installer, and
includes it in `SHA256SUMS` and the release manifest only when the Beta-only
`include_windows` input is explicitly enabled (it defaults to false). Publication only happens when a release runs, so no signed Windows
installer is published yet: there is no published native Windows upgrade path
today, and real-host validation is outstanding under FAR-2250. Linux uses
`scripts/install.sh`; macOS uses `scripts/macos/install.sh` and installs the
LaunchAgent seam from the app bundle. See the platform support matrix in
[`docs/system-requirements.md`](./system-requirements.md).

The native upgrade swaps the `current` symlink after a verified data
snapshot; migrations run through the new bundle's boot lifespan. On macOS, install the signed/notarized DMG matching the host architecture:

| Mac | Native artifact | Rosetta |
|-----|-----------------|---------|
| Apple Silicon | `FarnaControl-<version>-macos-arm64.dmg` | Not required |
| Intel | `FarnaControl-<version>-macos-x86_64.dmg` | Not required |

These are separate native bundles, not a universal artifact. The installer validates the mounted app's launcher architecture as well as the host architecture, so renaming a DMG cannot bypass the check. The Linux installer verifies the signed release manifest/checksum
chain before copying the app; the macOS installer does not read the release
manifest and instead verifies the DMG and app bundle through `hdiutil verify`
plus Apple Developer ID signing/notarization. Two entry points perform the same
swap:

- the published installer (`bash scripts/install.sh`), and
- the launcher command (`farna-control upgrade <target-version>`).

The Linux bundle also carries the exact checked-out `install.sh` as an executable
file. After extracting a verified bundle, run `install.sh --from-bundle
/path/to/farna-control-<version>-linux-<arch>` from the bundle directory's parent. The
installer requires the adjacent tarball plus its signed release manifest and
signature, then verifies both the signed artifact and the bundle's internal
`SHA256SUMS`; it never guesses a download or accepts an unsigned extracted tree.
It writes a path-only activation helper to `~/.local/opt/farna-control/env.sh` and
prints the exact `source <path>` command at the end. The helper exports only the
canonical `FARNA_CONTROL_INSTALL_ROOT` and `FARNA_CONTROL_BIN_DIR` names; the
pre-rebrand compatibility aliases are gone. That command must be run by the caller
because an installer child process cannot modify its parent's shell.

The installed command is `farna-control` (`~/.local/bin/farna-control`), the
install root is `~/.local/opt/farna-control`, and the bundle artifact is
`farna-control-<version>-linux-<arch>.tar.gz`. A machine still carrying a
pre-rename install (`~/.local/opt/modulo` and `~/.local/bin/modulo`) is migrated
automatically on the next install run: the root is renamed, the marker file
becomes `.farna-control-native-install`, and the old shim is removed. The
installer refuses – rather than guessing – when both an old and a new install
root exist. A RUNNING pre-rebrand install is migrated in two passes, because the
root rename needs it stopped while the enforced dump needs a live stack: take an
out-of-band database backup first (see
[backup](./operations/backup.md) – a `--data-dir` backup cannot run while the
launcher is live, so use its `--db-url`/`pg_dump` form), stop it
(`farna-control stop`), then re-run this installer with `--skip-backup` – the loud
acknowledgement that the out-of-band backup is the authoritative snapshot (a
stopped stack cannot be dumped). The bundled data dir keeps its
`~/.local/share/modulo/data` location so an upgrade never strands existing data.

Both entrypoints to that install export `FARNA_CONTROL_INSTALL_ROOT`: the
`~/.local/bin/farna-control` shim states the root it was generated for, and the
bundle's own `current/launcher` derives it from its location (it sits at
`<root>/versions/<version>/launcher`). So `farna-control upgrade` - including
one run through `current/launcher` directly - targets the install that is
actually on disk, even when `FARNA_CONTROL_INSTALL_ROOT` put it somewhere other
than the default `~/.local/opt/farna-control`. The built-in default and
`install.sh`'s default are additionally pinned together by a test.

`farna-control upgrade` flow:

1. **Enforced pre-upgrade dump — only when a migration is pending.** The
   bundled pg_dump writes a versioned snapshot dir beside the cluster
   (`pre-upgrade-dump-<version>-<ts>/`, a sibling of `pgdata/`, never inside
   it) with a restore manifest. The dump is proven complete at dump time
   (pg_dump's completion marker as the final non-empty line), checksummed, and
   the database's revision(s) are recorded as the snapshot's schema. The
   pending decision is made INSIDE the shared helper and compares the live
   database with the NEW bundle's migrations (`--target-bundle`), never with
   the old installation that runs the helper — so a database already at the
   new bundle's head takes no snapshot (one line says so) and an installer
   re-run cannot rotate the recovery points for nothing. Retention keeps the
   two newest verified snapshots plus the newest one recording the database's
   current schema — a bounded set, so the disk guard cannot eventually refuse
   every upgrade. The disk guard itself refuses when the output filesystem has
   less than `max(2 GB, 1.25 x database size)` free plus the snapshots
   retention keeps. A failed or unverified dump aborts the upgrade - NO binary
   swap happens without a verified snapshot. The dump needs the bundled
   Postgres reachable, so run the upgrade command while the stack is running.
2. Stop + swap. The data dir must not be locked for the swap; the command
   stops the stack, re-points `current` to the new bundle, then boots the
   new bundle's migrations and gates on health.
3. Failure output carries the snapshot path and the EXACT manual restore
   command - v1 does NOT auto-restore on a failed upgrade. When no snapshot was
   needed, the guidance says so and re-points the binary only.

`--skip-backup` skips the enforced dump, not the recovery point. This flag is
the loud, explicit acknowledgement that a verified snapshot already exists (the
stop-then-rerun flow used by the published installer); the upgrade **refuses
when no intact snapshot of the CURRENT schema is present** under the data dir —
the snapshot's recorded revisions must equal the live database's, so an archive
several migrations stale cannot satisfy it — and it probes the LIVE database for
its revision(s) rather than the old bundle's script heads, so a database
already migrated ahead is still detected.

### Signed-manifest provisioning state (honest note)

`farna-control upgrade` and `scripts/install.sh` both refuse to install from an
unsigned or unverifiable release manifest (an ed25519 signature over the
manifest's exact bytes). The current production key, `modulo-2026-a`, is
provisioned in the shipped verifier and the release pipeline's
`BUNDLE_MANIFEST_SIGNING_KEY` GitHub Actions secret. The next rotation slot is
reserved and remains empty in both trust-store assemblies:
`backend/src/farna/launcher/manifest.py` `_TRUST_ROWS` and the installer's
`TRUST_KEY_CURRENT_B64` / `TRUST_KEY_NEXT_B64` values in `scripts/install.sh`.

No `bundle-v*` release has been published yet, so this does not claim that a
signed bundle is already available or that the installer is currently usable.
When rotation begins, use this two-stage protocol; do not change only the
secret. First, provision the replacement public key under a new key id in the
reserved NEXT slot and ship that overlap while the old key remains in CURRENT.
For the promotion release, make one coordinated change before producing any
artifact: move the old public key to NEXT under its old id, move the replacement
public key to CURRENT under its new id, update the checked-in trust store and
current-key id (in both the verifier and installer), replace the
``BUNDLE_MANIFEST_SIGNING_KEY`` GitHub Actions secret with the matching new
private key, and update the release workflow's expected-key-id contract to the
new current id. Release signing always derives the envelope key id from the
checked-in CURRENT slot. Verify that promotion release before publishing it:
old releases continue to verify via NEXT and the new release verifies via
CURRENT. Merely changing the secret while CURRENT remains the old key creates
a mismatched envelope and is invalid. After the compatibility window, remove
the old key from NEXT in both trust-store assemblies. Keep private signing key
material in the GitHub Actions secret only; never commit it. These are manual,
keys-in-vault operations tracked as needs-human.


### Docker Compose

The production compose file pins the image through `TAG`; never upgrade by
silently changing a floating tag. Before touching a real deployment, rehearse
the complete matrix on a disposable host with the same release compose asset.
Run the harness from a Farna Control checkout, pointing it at the extracted
`docker-compose.prod.yml` and its generated `.env`:

```bash
python3 scripts/verify_compose_upgrade_rollback.py \
  --compose-file /path/to/compose-bundle/docker-compose.prod.yml \
  --env-file /path/to/compose-bundle/.env \
  --old-tag v0.4.0 \
  --new-tag v0.5.0 \
  --evidence-dir evidence/compose-upgrade
```

The harness records host/OS/Python versions, every command and exit code,
stdout/stderr, image tags, readiness assertions, and final `RESULT`. It owns a
unique Compose project name and always runs `down --volumes --remove-orphans`
in a `finally` block. The release workflow runs this same command against the
extracted RC compose asset after the clean-host check, using the previous
published `bundle-v*` release as `--old-tag`; it uploads the evidence directory
as a workflow artifact and fails closed when no previous release exists. The
matrix is:

1. clean install with the old immutable image tag;
2. upgrade to the new immutable image tag and readiness check, asserting the
   new release's verified pre-upgrade snapshot and that the database revision
   moved forward;
3. intentionally failed upgrade by replacing the app with an invalid
   `SECRET_KEY`, asserting readiness is no longer HTTP 200 and retaining the
   app logs;
4. the previous image started on the forward-migrated database, which the
   startup guard refuses with exit 78 and one `REFUSED:` line naming the
   database revision, the running version and the documented restore command;
5. restore the verified pre-upgrade snapshot with the shipped
   `compose-restore.sh`; and
6. start the previous release with `compose-rollback.sh` and verify readiness,
   the reverted database revisions, and that the row written before the upgrade
   is still present.

The failed-upgrade leg is deliberately a real container replacement, not a
mocked command. A run is not evidence unless the evidence directory contains
`metadata.json`, `commands.jsonl`, the referenced stdout/stderr files, and a
`RESULT` line. Do not report Windows or macOS coverage from this Linux/Docker
matrix; macOS validation is recorded separately and Windows remains deferred.

For the operator upgrade itself:

```bash
# 1. Pull the exact new image named by TAG.
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml pull farna-control

# 2. Restart with that immutable image (takes the pre-upgrade snapshot, then
#    runs migrations on startup).
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml up -d farna-control

# 3. Verify migration completed and readiness is healthy.
TAG=<new-version> docker compose -f deploy/compose/docker-compose.prod.yml logs farna-control | grep -i alembic
curl http://localhost:80/healthz/ready
```

#### The pre-upgrade snapshot is automatic (FAR-2667)

The container entrypoint takes a verified `pg_dump` snapshot of the database
**immediately before** `alembic upgrade heads` runs, but only when a migration
is actually pending. Order is fixed: the release-alignment guard
(`python -m farna.db.alignment`, FAR-2659) runs first, then the snapshot, then
the migrations.

- **Only when a migration is pending.** The database's recorded revision(s)
  are compared with the revisions this image ships. A database already at head
  is not changed by this boot, so no snapshot is taken and one line says so.
  That is what stops a release that migrates and then crash-loops from
  rotating its own recovery point away on every restart.
- the snapshot lands in the `snapshot_data` volume at
  `/backups/pre-upgrade-dump-<TAG>-<UTC timestamp>/`, never inside the
  `postgres_data` volume — so it survives image replacement and
  `docker compose down`, and it is readable while Postgres is stopped. The
  volume is created from the image, so it is owned by the container user; only
  `docker compose down -v` deletes it (which also deletes the database);
- the dump is proven complete at dump time (pg_dump's completion marker as the
  final non-empty line), checksummed, and recorded in both `manifest.json` and
  the restore-compatible `backup-info.json`;
- retention keeps the two newest verified snapshots **plus the newest snapshot
  recording the database's current schema** — the recovery point a database
  that did not move since (a failed migration, a rollback) depends on. At most
  three directories survive: older generations are superseded and deleted, so
  snapshots cannot accumulate until the disk guard refuses every upgrade.
  Nothing is deleted before its replacement verifies;
- the guard refuses to run the upgrade at all when the output filesystem has
  less than `max(2 GB, 1.25 x database size)` free **plus the snapshots
  retention keeps**, naming the required space, the available space and that
  nothing has been changed. A refused snapshot aborts the boot, so no migration
  is applied;
- a first start against an empty database is not an upgrade: nothing is
  dumped and the boot proceeds;
- `DATABASE_ADMIN_URL` is required for the dump. There is no fallback to
  `DATABASE_URL`: the application role is `NOBYPASSRLS`, so a dump through it
  would be trailer-complete but row-partial.

The entrypoint prints the snapshot path it took. Record it.

#### If the new release fails: restore the snapshot

Run the recovery helper that ships beside the compose file. It requires an
explicit `--confirm` (it never confirms itself) and verifies before it
destroys:

```bash
./compose-restore.sh --confirm /backups/pre-upgrade-dump-<TAG>-<UTC timestamp>
```

1. **Verify the archive** — `restore --dry-run` in a one-off container checks
   the manifest, checksum coverage, every checksum and the dump's completeness.
   Each one-off container runs `--entrypoint farna-control … --no-deps`: the
   image's own `ENTRYPOINT` is `/entrypoint.sh`, which ignores its arguments,
   takes a snapshot, migrates and execs supervisord, so it must be overridden or
   the "dry run" would MIGRATE the live database.
2. **Verify the target** — `--verify-target-schema` refuses unless the target
   database records exactly the schema revisions the snapshot was taken from,
   so a wrong target cannot wipe an unrelated cluster.
3. **No URL, no password.** The helper never reads or passes a database URL: the
   one-off container inherits `DATABASE_ADMIN_URL` from the service environment
   and the CLI resolves it there. Nothing needs exporting in the operator's
   shell, so `set -u` cannot abort the recovery and the superuser password never
   appears on a host command line.
4. **Stop, restore, stop.** It stops the application, restores the dump,
   re-applies the application role's grants, and then **stops** — it prints the
   exact start command (on failure as well as success) and never restarts the
   stack:

```bash
./compose-rollback.sh <previous-version>
```

The bundle's `docker-compose.prod.yml` pins the image to a literal release tag
(so the file can carry a `SHA256SUMS` entry), which is why `TAG=<previous>`
`docker compose up -d` does not start anything different — `TAG` only sets
`FARNA_CONTROL_VERSION`. `compose-rollback.sh` merges a one-line image override
for that one invocation and leaves the pinned file, and its checksum, untouched.

Start the previous release only after reading the restore output. Rolling the
database backwards (`alembic downgrade`) is not supported; the snapshot is the
way back. The pre-restore safety dump of the database you are replacing is
written into the same snapshot volume, next to the snapshot.

**The RC matrix now asserts this leg (FAR-2674).**
`scripts/verify_compose_upgrade_rollback.py` runs the documented recovery end to
end: it verifies the new release's pre-upgrade snapshot, stops the app on the
forward-migrated database, asserts the previous image is refused by the startup
guard (exit 78 and the one `REFUSED:` line), restores the snapshot with the
shipped `compose-restore.sh`, and starts the previous release with
`compose-rollback.sh` on the restored database with the row written before the
upgrade still present. The green path through that restore is still gated on
FAR-2763, so a green matrix is evidence the leg is asserted and exercised, not
that the restore has yet completed successfully against a real published pair.

### Fly.io

**Fly takes no pre-upgrade snapshot, and there is no switch to enable one.**
The Fly `[release]` command (`deploy/fly/release.sh`) is the normal migrator: it
runs once per deploy, before any machine boots, and applies the migrations
there. In the normal path the boot entrypoint (`deploy/fly/entrypoint.sh`)
therefore finds the schema already at head and skips its migration work.

A boot-time migration path still exists, though, and it takes no snapshot. The
entrypoint keeps a 10-attempt `alembic upgrade heads` fallback that runs
whenever its at-head probe reports the database behind — and, because that
probe is fail-safe, on ANY probe error. When that fallback applies a schema
change it does so unguarded, so Fly is still the documented exception to "every
upgrade path keeps a recovery point": a schema-changing Fly deploy needs an
out-of-band `farna-control backup` (or `pg_dump`) taken before you deploy. That
remains a known gap (see
[`docs/product-map/infra/backup-restore.md`](./product-map/infra/backup-restore.md)).
Closing it means moving the snapshot into the release stage, where the normal
migration runs; it is a separate change, not something the boot entrypoint can
fix without migrating outside the release stage.

The release-alignment guard (`python3 -m farna.db.alignment`) still runs in
both `release.sh` and the boot entrypoint, so a database from a newer release
is refused legibly on Fly too.

### Self-Hosted (Bare Metal / VM)

```bash
# 1. Pull latest code
git pull origin main

# 2. Update dependencies
cd backend
uv sync

# 3. Run migrations
uv run alembic upgrade heads

# 4. Restart the service
sudo systemctl restart modulo

# 5. Verify
sudo journalctl -u modulo -n 50 --no-pager | grep alembic
curl http://localhost:8000/healthz
```

---

## Migration Behavior

Migrations run automatically on backend startup:

1. The backend pod/process starts
2. It acquires a two-key PostgreSQL advisory lock `(72001, 1)` via `pg_try_advisory_lock`
3. It runs `alembic upgrade heads` (idempotent)
4. On success, it proceeds to serve traffic
5. On failure, it retries up to 5 times with backoff, then fails boot

The advisory lock prevents concurrent migrations across multiple replicas. The lock key (`72001, 1`) must not conflict with other applications sharing the same Postgres instance.

### The legacy `owner` org role on an upgraded database

Current releases have no `owner` org role: the CHECK constraint on
`org_memberships.role` admits only `admin`, `operator`, `runner` and `viewer`,
and older releases converted `owner` rows to `admin` in migration 0030, so a
database that has been upgraded continuously already carries none.

A database that still has `owner` rows - restored from a pre-0030 backup, or
carried over from a release that predates the conversion - is not renamed
automatically. On the first boot after the upgrade a startup guard
(`_assert_no_owner_rows`) refuses to boot, logs
`infra_blocked=owner_rows_present` with the affected account ids, and prints
the exact fix:

```sql
UPDATE org_memberships SET role='admin' WHERE role='owner';
```

Run that once against the database, then start the new version again. A boot
that stops here is the guard doing its job, not a migration failure: the data
is intact and the correction is one statement.

---

## Zero-Downtime Requirements

| Requirement | Docker Compose |
|-------------|---------------|
| Minimum replicas | 1 (brief downtime on restart) |
| Readiness probe | Manual check |
| Rolling update | Not supported (stop + start) |

Docker Compose self-hosted deployments stop the old container before
starting the new one, so a brief downtime occurs during upgrades. For
true zero-downtime, deploy via Fly.io, which performs rolling deployments
between machine groups.

---

## Rollback

### Native single-install rollback playbook

The manual playbook. Run it when either trigger fires:

**Triggers**
1. A reproducible data-loss report against a released `bundle-vX.Y.Z`
   (failed upgrade, dropped data after restore, anything where the
   release artifact is the proximate cause).
2. A failed release - the nightly canary red for that release, or the
   release smoke failing on the tagged artifact.

**Actions (in order)**
1. **Unlist the failing release.** `gh release delete <tag> --yes` (or
   make it a draft). Published release assets are immutable, so a bad
   artifact must be removed from the download surface, not replaced.
2. **Re-point the install one-liner.** The installer resolves
   `releases/latest`; unlisting the bad release is usually sufficient.
   When you need an EXPLICIT re-point, run the rollback-verify target:
   `gh workflow run "Native: release smoke + canary (bundle-v*)" -f rollback_version=bundle-v<good>`.
   This verifies the target release exists with its artifact servable.
   A manual dispatch MUST pass one of `rollback_version` or `canary_version`:
   an argument-free dispatch would run only the tracking-housekeeping job and
   verify nothing, so the workflow rejects it with an explicit error naming
   both inputs (FAR-2161). To smoke a published release rather than verify a
   rollback target, pass `-f canary_version=bundle-v<good>` instead.
3. **Cut a patch.** Branch, fix, tag the next `bundle-v*` patch version; the release
   smoke runs against the exact artifact before Merge Queue can bless it.
4. **Operator-side recovery.** Any operator already holding the bad
   bundle restores from their pre-upgrade snapshot: the upgrade refusal
   output prints the snapshot dir and the exact `farna-control restore` command.

**Triggers rule:** if the canary is red and no customer report exists,
treat 1-3 as REQUIRED, not optional - an endangered release stays listed
only while it is being rolled forward.

**Version pin reference (Docker Compose production):**

| Variable | Purpose | Example |
|----------|---------|---------|
| `TAG` | Farna Control image version (required, no default) | `v0.4.0` |

### Application Rollback

```bash
# Docker Compose – pin the previous image version and restart
docker compose -f deploy/compose/docker-compose.prod.yml stop farna-control
docker tag ghcr.io/farnalabs/farna-control:old ghcr.io/farnalabs/farna-control:<previous-version>
TAG=<previous-version> docker compose -f deploy/compose/docker-compose.prod.yml up -d

# Self-hosted
git checkout <previous-tag>
uv sync
sudo systemctl restart modulo
```

### Database Rollback (Downgrade)

**Warning:** Rolling back the application does NOT revert database migrations. If the previous code expects an older schema:

#### Irreversible data-purge migrations

A database restore alone is not a durable rollback for a migration that permanently deletes data. Stop the purge image and hold any release or startup path that could boot it. Restore the database from the pre-purge backup, then roll the application image back to an image built before the purge migration before starting the application again. Otherwise, startup or release migration logic can apply the purge again as soon as it sees the restored pre-purge revision.

The order is mandatory: **stop or hold the purge image, restore the database, roll back the application image, then boot it**. Do not boot the purge image between the restore and the application rollback.

```bash
# Check current Alembic version
docker compose -f deploy/compose/docker-compose.prod.yml exec farna-control uv run alembic current

# Preview the downgrade SQL
docker compose -f deploy/compose/docker-compose.prod.yml exec farna-control uv run alembic downgrade --sql -1

# Downgrade (use with extreme caution – data loss possible)
docker compose -f deploy/compose/docker-compose.prod.yml exec farna-control uv run alembic downgrade -1
```

**Prefer a forward-fix over downgrading.** Write a new migration that reverts the schema change rather than using `alembic downgrade`. Not all migrations include a `downgrade()` function.

If downgrade fails, restore from the pre-upgrade backup instead
(from the repository root):

```bash
uv run scripts/restore.py --input /backups/pre-upgrade-<date>.tar.gz.enc --full
```

---

## Config Changes Between Versions

When upgrading, check for changes to:

1. **New required environment variables** – the application refuses to start if missing
2. **Deprecated environment variables** – log warnings indicate removal in a future version
3. **Changed defaults** – review [`docs/configuration-reference.md`](./configuration-reference.md) for current defaults
4. **New service dependencies** – e.g., Redis becoming required for new features
5. **API changes** – breaking endpoint changes are documented in release notes
6. **Provider registration changes (FAR-996)** – Docker providers are now registered exclusively via `FARNA_CONTROL_DOCKER_HOST` or `DOCKER_HOST`. An unrelated `FARNA_CONTROL_RUNNER_*` variable (e.g. `FARNA_CONTROL_RUNNER_MACHINE_ID`) no longer registers the Docker provider. If your `runner_docker` pipelines were relying on a `FARNA_CONTROL_RUNNER_*` variable to register Docker, you must now set `FARNA_CONTROL_DOCKER_HOST` (or `DOCKER_HOST`) and restart. The error message on `ProviderNotConfiguredError` already names the required env var.

---

## Post-Upgrade Verification

- [ ] Backend `/healthz` returns 200
- [ ] API responds to authenticated requests
- [ ] Existing pipeline runs appear in the UI
- [ ] WebSocket connections establish successfully
- [ ] Rate limiting is functional
- [ ] Audit log chain is intact (verified via `GET /api/v1/admin/audit/verify`; requires the Team-tier `audit_viewer` feature plus `audit.manage`. The chain is recorded on every tier, but on Community the product offers no route to verify it)
- [ ] All env vars are set correctly (no deprecation warnings in logs)
- [ ] Frontend loads without errors (check browser console)
- [ ] Cross-origin requests work (CORS)

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| System requirements (platform support matrix) | [`docs/system-requirements.md`](./system-requirements.md) |
| Deployment guide | [`docs/deployment.md`](./deployment.md) |
| Deployment security | [`docs/deployment-security.md`](./deployment-security.md) §7 |
| Backup & restore | [`docs/operations/backup.md`](./operations/backup.md) |
| Configuration reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Troubleshooting | [`docs/troubleshooting.md`](./troubleshooting.md) |
| Public launch checklist | [`docs/public-launch-checklist.md`](./public-launch-checklist.md) |
