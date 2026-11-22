#!/bin/sh
set -e

echo "=== Bootstrapping modulo_app role ==="
python -m farna.db.bootstrap_role

echo "Running database migrations..."

# FAR-2659: refuse a database from a newer release BEFORE any migration runs.
# One shared Python check (the native launcher calls the same function); a
# non-zero exit is the legible refusal, and `set -e` stops the script there.
python -m farna.db.alignment

# Phase 1: apply the initial schema (creates version table with VARCHAR(32))
alembic upgrade 0001_v2_identity_org

# Widen the version_num column to VARCHAR(64) — long revision IDs like
# 0005_library_community_visibility (34 chars) exceed the default 32.
PGPASSWORD=${POSTGRES_PASSWORD:-changeme} psql -h db -U modulo -d modulo -c \
  "ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64);" 2>/dev/null || true

# Phase 2: apply remaining migrations
alembic upgrade heads

echo "Migrations complete. Starting uvicorn..."

exec uvicorn farna.api.main:app \
    --host 0.0.0.0 --port 8000 \
    --timeout-keep-alive 30 \
    --timeout-graceful-shutdown 30 \
    --limit-concurrency 100
