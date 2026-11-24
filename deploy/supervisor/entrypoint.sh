#!/bin/sh
# Farna Control all-in-one container entrypoint.
#
# FAR-2667: the pre-upgrade database snapshot is taken HERE, immediately
# before the migrations run, because at this point
#   - the application is stopped (supervisord has not started anything),
#   - the database is up (compose gates on its healthcheck), and
#   - the operator cannot forget it.
# A snapshot the operator has to remember to create is weaker than one the
# container cannot skip: if this step fails, `set -e` aborts the boot and NO
# migration is applied.
#
# ORDER (FAR-2659 + FAR-2667, pinned by test): alignment guard -> snapshot ->
# migrate. A database from a NEWER release is refused before anything is
# dumped; a migration that is actually PENDING is the only case that takes a
# snapshot, so a release that migrates and then crash-loops cannot rotate its
# own recovery point away on every restart.
#
# The snapshot lives on the /backups mount, never inside the postgres_data
# volume: it survives image replacement and `compose down` (but NOT
# `compose down -v`), does not double the volume the 98%-capacity monitor
# watches, and is reachable while Postgres is stopped (which is exactly when a
# restore runs). The compose file mounts a pre-created, image-owned volume
# there: an operator-supplied bind mount must be writable by the container user
# or the boot aborts with the ownership fix named.
set -eu

echo "Running database migrations..."
# FAR-2659: refuse a database from a newer release BEFORE running migrations.
# One shared Python check (also used by the native launcher and HA later); it
# exits non-zero with one legible line when the database is ahead of this
# release, which `set -e` turns into a refusal instead of an alembic traceback.
python -m farna.db.alignment

# FAR-2667: the snapshot is taken only when a durable snapshot directory is
# wired. The compose deployment sets FARNA_CONTROL_SNAPSHOT_DIR=/backups; a
# non-compose all-in-one deployment that does not set it keeps booting (the
# migrations fall back to DATABASE_URL) instead of hard-requiring
# DATABASE_ADMIN_URL on a pristine or already-at-head database.
if [ -n "${FARNA_CONTROL_SNAPSHOT_DIR:-}" ]; then
  snapshot_dir="$FARNA_CONTROL_SNAPSHOT_DIR"
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
      --output-dir "$snapshot_dir" \
      --release "$FARNA_CONTROL_VERSION" \
      --skip-if-pristine)"
  else
    snapshot="$(python -m farna.launcher.upgrade \
      --db-url "$DATABASE_ADMIN_URL" \
      --output-dir "$snapshot_dir" \
      --skip-if-pristine)"
  fi

  if [ -n "$snapshot" ]; then
    echo "Pre-upgrade snapshot verified: $snapshot"
    echo "  Restore a bad release with: ./compose-restore.sh --confirm $snapshot"
  else
    echo "No pre-upgrade snapshot was needed (fresh database or no migration pending) — see the line above."
  fi
fi

alembic upgrade heads

echo "Starting supervisord..."
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
