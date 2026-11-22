#!/bin/sh
set -e

echo "=== Bootstrapping modulo_app role ==="
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
  snapshot="$(python -m farna.launcher.upgrade \
    --db-url "$DATABASE_ADMIN_URL" \
    --output-dir "$FARNA_CONTROL_SNAPSHOT_DIR" \
    --skip-if-pristine)"
  if [ -n "$snapshot" ]; then
    echo "Pre-upgrade snapshot verified: $snapshot"
    echo "  Restore a bad release with: scripts/compose-restore.sh --confirm $snapshot"
  else
    echo "No pre-upgrade snapshot was needed (fresh database or no migration pending) — see the line above."
  fi
fi

echo "Running database migrations..."

# Phase 1: apply the initial schema (creates version table with VARCHAR(32))
alembic upgrade 0001_v2_identity_org

# Widen the version_num column to VARCHAR(64) — long revision IDs like
# 0005_library_community_visibility (34 chars) exceed the default 32.
PGPASSWORD=${POSTGRES_PASSWORD:-changeme} psql -X -h db -U modulo -d modulo -c \
  "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);" 2>/dev/null || true

# Phase 2: apply remaining migrations
alembic upgrade heads

echo "Migrations complete. Starting uvicorn..."

exec uvicorn farna.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
