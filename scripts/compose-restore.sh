#!/usr/bin/env bash
# Restore a pre-upgrade database snapshot into a docker-compose deployment
# (FAR-2667). This is the ONE documented recovery command for the compose
# path:
#
#   scripts/compose-restore.sh /backups/pre-upgrade-dump-<version>-<timestamp>
#
# It stops the writers itself (the `modulo` container), restores the snapshot
# through a one-off container, and LEAVES EVERYTHING STOPPED, printing the
# exact start command. It never starts the application: a restart would hand
# a half-verified database to an app that then refuses to boot.
set -euo pipefail

usage() {
  printf 'usage: %s <snapshot-dir> [compose-file]\n' "$(basename "$0")" >&2
  printf '  <snapshot-dir> is the path the upgrade printed, e.g.\n' >&2
  printf '  /backups/pre-upgrade-dump-0.5.0-20261122-101500 (container path),\n' >&2
  printf '  the same path on the host, or just the directory name.\n' >&2
  printf '  compose-file defaults to the COMPOSE_FILE env var, then\n' >&2
  printf '  deploy/compose/docker-compose.prod.yml, then ./docker-compose.prod.yml\n' >&2
}

snapshot="${1:-}"
if [ -n "${2:-}" ]; then
  compose_file="$2"
elif [ -n "${COMPOSE_FILE:-}" ]; then
  compose_file="$COMPOSE_FILE"
elif [ -f deploy/compose/docker-compose.prod.yml ]; then
  compose_file=deploy/compose/docker-compose.prod.yml
else
  # Extracted compose release bundle layout: the helper sits beside it.
  compose_file=docker-compose.prod.yml
fi

if [ -z "$snapshot" ]; then
  usage
  exit 2
fi
if [ ! -f "$compose_file" ]; then
  printf 'compose file not found: %s\n' "$compose_file" >&2
  exit 2
fi

# The snapshot lives on the host bind mount that the compose file mounts at
# /backups, so accept a container path, a host path, or a bare name and
# translate all three to the container path.
snapshot_name="$(basename "$snapshot")"
container_snapshot="/backups/${snapshot_name}"
host_backups_dir="$(cd "$(dirname "$compose_file")" && pwd)/backups"

if [ ! -d "$host_backups_dir/${snapshot_name}" ]; then
  printf 'refusing to restore: no snapshot directory at %s/%s\n' \
    "$host_backups_dir" "$snapshot_name" >&2
  printf 'list the available snapshots with: ls -1 %s\n' "$host_backups_dir" >&2
  exit 1
fi

printf 'Stopping the writers (compose service "modulo")...\n'
docker compose -f "$compose_file" stop modulo

printf 'Restoring %s ...\n' "$container_snapshot"
# --no-deps: the database is already running; do not restart it. The one-off
# container inherits the service environment (DATABASE_URL / DATABASE_ADMIN_URL
# / DATABASE_ADMIN_URL's owner role is what pg_dump and psql need).
# --safety-dump-dir /backups keeps the pre-restore safety dump of the CURRENT
# database on the host instead of inside the disposable container.
docker compose -f "$compose_file" run --rm --no-deps --entrypoint sh modulo -c \
  'farna-control restore "$1" --db-url "$DATABASE_ADMIN_URL" --yes --safety-dump-dir /backups' \
  sh "$container_snapshot"

printf '\nWriters stay stopped. Start the previous release with:\n'
printf '  TAG=<previous-version> docker compose -f %s up -d modulo\n' "$compose_file"
printf 'No automatic restart was performed.\n'
