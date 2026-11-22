#!/bin/sh
set -e

echo "Running database migrations..."
# FAR-2659: refuse a database from a newer release BEFORE running migrations.
# One shared Python check (also used by the native launcher and HA later); it
# exits non-zero with one legible line when the database is ahead of this
# release, which `set -e` turns into a refusal instead of an alembic traceback.
python -m farna.db.alignment
alembic upgrade heads

echo "Starting supervisord..."
exec supervisord -c /etc/supervisor/conf.d/supervisord.conf
