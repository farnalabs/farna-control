#!/usr/bin/env bash
# Restore a pre-upgrade database snapshot into a docker-compose deployment
# (FAR-2667). This is the ONE documented recovery command for the compose
# path:
#
#   scripts/compose-restore.sh --confirm /backups/pre-upgrade-dump-<version>-<timestamp>
#
# It VERIFIES before it destroys: the snapshot's manifest, checksums and dump
# completeness are checked in a one-off container (restore --dry-run), and the
# TARGET database must record the schema the snapshot was taken from
# (--verify-target-schema). A snapshot that is corrupt, foreign, missing or
# aimed at the wrong cluster is refused BEFORE anything is stopped or wiped.
#
# EVERY one-off container overrides the image ENTRYPOINT with
# `--entrypoint farna-control` (or `sh` for a filesystem check). The all-in-one
# image's ENTRYPOINT is /entrypoint.sh, which IGNORES its arguments, runs the
# alignment guard, takes a fresh snapshot, migrates and execs supervisord:
# appending `restore ...` to it would MIGRATE the live database and hang, while
# restoring nothing. The CLI is `farna-control` (on PATH in the image); there
# is no bare `restore` binary.
#
# This script NEVER reads or passes the database URL. The one-off container
# inherits DATABASE_ADMIN_URL from the service environment, and the CLI
# resolves it there — so the superuser password never appears in a host argv
# (world-readable via /proc/<pid>/cmdline) and `set -u` cannot abort on a
# variable only the container has.
#
# The confirmation is an explicit ARGUMENT (--confirm), never a --yes baked in
# by a wrapper: a recovery command that confirms itself is a command that wipes
# the wrong database without anyone saying so.
#
# It stops the writers itself (the `modulo` container), restores through a
# one-off container, and LEAVES EVERYTHING STOPPED. It never starts the
# application: a restart would hand a half-verified database to an app that
# then refuses to boot. The exact start command is printed on failure as well
# as success.
# -E (errtrace) is REQUIRED: the ERR trap below must fire for a failing command
# inside a shell FUNCTION (compose()/print_start_command()), and bash does not
# inherit the ERR trap into functions without it — without -E a failed
# `docker compose run` exits without printing the start command the operator
# needs at exactly the moment they need it.
set -Eeuo pipefail

# The compose service that runs the application (and the only writer).
app_service="${FARNA_CONTROL_APP_SERVICE:-modulo}"

usage() {
  printf 'usage: %s --confirm <snapshot-dir> [compose-file]\n' "$(basename "$0")" >&2
  printf '  <snapshot-dir> is the path the upgrade printed, e.g.\n' >&2
  printf '  /backups/pre-upgrade-dump-0.5.0-20261122-101500 (a path inside the\n' >&2
  printf '  snapshot volume) or just the directory name.\n' >&2
  printf '  --confirm  required: acknowledges that this OVERWRITES the current database\n' >&2
  printf '  compose-file defaults to the COMPOSE_FILE env var, then\n' >&2
  printf '  deploy/compose/docker-compose.prod.yml, then ./docker-compose.prod.yml\n' >&2
}

confirm=""
snapshot=""
compose_file=""

while [ $# -gt 0 ]; do
  case "$1" in
    --confirm | -c)
      confirm=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    -*)
      printf 'unknown option: %s\n' "$1" >&2
      usage
      exit 2
      ;;
    *)
      if [ -z "$snapshot" ]; then
        snapshot="$1"
      elif [ -z "$compose_file" ]; then
        compose_file="$1"
      else
        printf 'unexpected extra argument: %s\n' "$1" >&2
        usage
        exit 2
      fi
      shift
      ;;
  esac
done

if [ -z "$compose_file" ]; then
  if [ -n "${COMPOSE_FILE:-}" ]; then
    compose_file="$COMPOSE_FILE"
  elif [ -f deploy/compose/docker-compose.prod.yml ]; then
    compose_file=deploy/compose/docker-compose.prod.yml
  else
    # Extracted compose release bundle layout: the helper sits beside it.
    compose_file=docker-compose.prod.yml
  fi
fi

if [ -z "$snapshot" ]; then
  usage
  exit 2
fi
if [ -z "$confirm" ]; then
  printf 'refusing to restore without --confirm: this OVERWRITES the current database.\n' >&2
  printf 're-run as: %s --confirm %s\n' "$(basename "$0")" "$snapshot" >&2
  exit 2
fi
if [ ! -f "$compose_file" ]; then
  printf 'compose file not found: %s\n' "$compose_file" >&2
  exit 2
fi

print_start_command() {
  # compose-rollback.sh, NEVER `TAG=<previous> docker compose up -d`: the
  # packaged compose hard-pins a literal image tag (so the file can carry a
  # checksum), and TAG only sets FARNA_CONTROL_VERSION - so re-running compose
  # would restart the release that just failed. The helper merges a one-line
  # image override for that invocation only. `dirname "$0"` resolves both
  # layouts: `./compose-rollback.sh` in the bundle, `scripts/compose-rollback.sh`
  # in the repo.
  rollback_helper="$(dirname "$0")/compose-rollback.sh"
  printf '\nStart the previous release with:\n'
  printf '  %s <previous-version>   (e.g. %s v0.3.0)\n' "$rollback_helper" "$rollback_helper"
  printf 'No automatic restart was performed.\n'
}

on_error() {
  status=$?
  printf '\nRESTORE FAILED (exit %s) — the writers are still stopped; do NOT start the new release.\n' "$status" >&2
  print_start_command >&2
  exit "$status"
}
trap on_error ERR

# The snapshot lives on the compose deployment's snapshot volume, mounted at
# /backups, so accept a container path or a bare name and translate both to the
# in-container path. Presence is verified INSIDE the container (the volume is
# not a host directory).
snapshot_name="$(basename "$snapshot")"
container_snapshot="/backups/${snapshot_name}"

compose() {
  docker compose -f "$compose_file" "$@"
}

printf 'Verifying snapshot %s before touching anything...\n' "$container_snapshot"
# --dry-run performs every archive check (manifest shape/version, checksum
# coverage, checksums, dump completeness, JSON exports) and previews the steps
# WITHOUT touching the database, stopping the writers or taking the data-dir
# lock. A corrupt, foreign or missing snapshot dies here.
# shellcheck disable=SC2016  # the single-quoted script is executed INSIDE the container
compose run --rm --no-deps --entrypoint sh "$app_service" -c \
  'test -d "$1" || { echo "refusing to restore: no snapshot directory at $1" >&2; exit 1; }' \
  sh "$container_snapshot"
# --entrypoint farna-control: run the CLI, and NOTHING else. Without it the
# image's /entrypoint.sh swallows these arguments, migrates the live database
# and starts supervisord — the exact opposite of a dry run.
compose run --rm --no-deps --entrypoint farna-control "$app_service" \
  restore "$container_snapshot" --dry-run >/dev/null

printf 'Verifying the TARGET database matches the snapshot...\n'
# Without --data-dir there is no state.json identity to compare, so the
# equivalent guard verifies the TARGET's recorded schema against the snapshot's:
# a database that is not the one this snapshot came from (different revision,
# or not a bootstrapped Farna Control database at all) is refused.
compose run --rm --no-deps --entrypoint farna-control "$app_service" \
  restore "$container_snapshot" --dry-run \
  --verify-target-schema >/dev/null

printf 'Stopping the writers (compose service "%s")...\n' "$app_service"
compose stop "$app_service"

printf 'Restoring %s ...\n' "$container_snapshot"
# --yes here is the INNER confirmation; the operator's confirmation is the
# --confirm argument this script refuses to run without. --no-deps: the database
# is already running; do not restart it. --safety-dump-dir /backups keeps the
# pre-restore safety dump of the CURRENT database on the volume instead of
# inside the disposable container.
compose run --rm --no-deps --entrypoint farna-control "$app_service" \
  restore "$container_snapshot" \
  --yes --verify-target-schema --safety-dump-dir /backups

printf '\nRestore complete. Writers stay stopped.\n'
print_start_command
