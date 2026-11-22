#!/bin/sh
# Farna Control all-in-one container entrypoint.
#
# FAR-2667: the pre-upgrade database snapshot is taken HERE, immediately
# before the migrations run, because at this point
#   - the application is stopped (supervisord has not started anything),
#   - the database is up (compose gates on its healthcheck), and
#   - the operator cannot forget it.
# A snapshot directory the operator has to remember to create is weaker than
# one the container cannot skip: if this step fails, `set -e` aborts the boot
# and NO migration is applied.
#
# The snapshot lives on the /backups bind mount, never inside the
# postgres_data volume: a host path survives image replacement and
# `compose down`, does not double the volume the 98%-capacity monitor watches,
# and is reachable while Postgres is stopped (which is exactly when a restore
# runs).
set -eu

snapshot_dir="${FARNA_CONTROL_SNAPSHOT_DIR:-/backups}"
# pg_dump must run as the OWNER role: farna_control_app is NOBYPASSRLS, so a
# dump through it would read only the rows its session can see.
database_url="${DATABASE_ADMIN_URL:-$DATABASE_URL}"

echo "Pre-upgrade database snapshot (FAR-2667)..."
if [ -n "${FARNA_CONTROL_VERSION:-}" ]; then
  snapshot="$(python -m farna.launcher.upgrade \
    --db-url "$database_url" \
    --output-dir "$snapshot_dir" \
    --release "$FARNA_CONTROL_VERSION" \
    --skip-if-pristine)"
else
  snapshot="$(python -m farna.launcher.upgrade \
    --db-url "$database_url" \
    --output-dir "$snapshot_dir" \
    --skip-if-pristine)"
fi

if [ -n "$snapshot" ]; then
  echo "Pre-upgrade snapshot verified: $snapshot"
  echo "  Restore a bad release with: scripts/compose-restore.sh $snapshot"
else
  echo "Fresh database (no application tables): no pre-upgrade snapshot needed."
fi

echo "Running database migrations..."
# FAR-2659: refuse a database from a newer release BEFORE running migrations.
# One shared Python check (also used by the native launcher and HA later); it
# exits non-zero with one legible line when the database is ahead of this
# release, which `set -e` turns into a refusal instead of an alembic traceback.
python -m farna.db.alignment
alembic upgrade heads

echo "Starting supervisord..."
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
