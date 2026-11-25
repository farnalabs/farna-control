#!/bin/sh
set -e

echo "=== Bootstrapping the runtime DB roles (farna_control_app, farna_control_migrate, farna_control_breakglass, farna_control_system) ==="
python -m farna.db.bootstrap_role

# FAR-2659: refuse a database from a newer release BEFORE any migration runs.
# One shared Python check (the native launcher calls the same function); a
# non-zero exit is the legible refusal, and `set -e` stops the script there.
python -m farna.db.alignment

# FAR-2667: optional pre-upgrade database snapshot. Enabled by pointing
# FARNA_CONTROL_SNAPSHOT_DIR at a directory that survives the container
# (deploy/compose/docker-compose.prod.yml does this via its snapshot volume).
# Unset -> skipped, so local development containers stay unchanged.
# ORDER: after the alignment guard (a database from a NEWER release is refused
# before anything is dumped) and before the migrations. If the snapshot cannot
# be taken or verified this exits non-zero and NO migration is applied.
if [ -n "${FARNA_CONTROL_SNAPSHOT_DIR:-}" ]; then
  # pg_dump must run as the OWNER role: farna_control_app is NOBYPASSRLS, so a
  # dump through it would read only the rows its session can see and produce a
  # trailer-complete but ROW-PARTIAL snapshot. Refuse rather than silently fall
  # back to DATABASE_URL (the application role).
  if [ -z "${DATABASE_ADMIN_URL:-}" ]; then
    echo "ERROR: FARNA_CONTROL_SNAPSHOT_DIR is set but DATABASE_ADMIN_URL is not, so the pre-upgrade snapshot cannot dump as the owner role. Set DATABASE_ADMIN_URL (the migration/owner connection URL) — a dump through the NOBYPASSRLS application role would be silently incomplete. No migration has been applied." >&2
    exit 1
  fi
  echo "Pre-upgrade database snapshot (FAR-2667)..."
  if [ -n "${FARNA_CONTROL_VERSION:-}" ]; then
    snapshot="$(python -m farna.launcher.upgrade \
      --db-url "$DATABASE_ADMIN_URL" \
      --output-dir "$FARNA_CONTROL_SNAPSHOT_DIR" \
      --release "$FARNA_CONTROL_VERSION" \
      --skip-if-pristine)"
  else
    snapshot="$(python -m farna.launcher.upgrade \
      --db-url "$DATABASE_ADMIN_URL" \
      --output-dir "$FARNA_CONTROL_SNAPSHOT_DIR" \
      --skip-if-pristine)"
  fi
  if [ -n "$snapshot" ]; then
    echo "Pre-upgrade snapshot verified: $snapshot"
    echo "  Restore a bad release with: ./compose-restore.sh --confirm $snapshot"
  else
    echo "No pre-upgrade snapshot was needed (fresh database or no migration pending) — see the line above."
  fi
fi

echo "Running database migrations..."

# One `alembic upgrade heads` is the whole migration step: 0350_baseline_schema
# is the root of the chain, so there is no separate bootstrap revision to apply
# first, and migrations/env.py owns the version-table width — its pre-flight
# widens alembic_version.version_num to VARCHAR(255) before the first migration
# runs, so revision ids longer than Alembic's 32-character default fit without
# an ALTER run from here.
alembic upgrade heads

echo "Migrations complete. Starting uvicorn..."

exec uvicorn farna.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
