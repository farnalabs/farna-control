#!/bin/sh
# Start a PREVIOUS release from a packaged compose bundle (FAR-2667).
#
# The ONE documented way back to the previous image for the compose path:
#
#   ./compose-rollback.sh <previous-version>      e.g. ./compose-rollback.sh v0.3.0
#
# Why a helper. The bundle's docker-compose.prod.yml pins the image to THIS
# release's immutable tag on purpose (the file ships with a SHA256SUMS entry),
# so the literal image line IS the release. Two things that look like they
# would move it do not:
#
#   * `TAG=<previous-version> docker compose ...` only sets
#     FARNA_CONTROL_VERSION (the label recorded in the snapshot directory name);
#     the packaged image line carries no ${TAG} placeholder to substitute, so it
#     restarts the release that just failed.
#   * editing the image line in place turns the verified artifact into a
#     different, unverifiable release.
#
# So the rollback merges a one-line override on top of the pinned file for THIS
# invocation only and leaves the shipped file - and its checksum - untouched.
# The override is not persisted: a later `docker compose up -d` goes back to the
# release the bundle was packaged for, so the previous image is never silently
# remembered.
#
# It starts an image; it does NOT restore data. Restore the pre-upgrade snapshot
# first with compose-restore.sh, then start the previous release here.
#
# Order matters: restore the database BEFORE starting the old image (the old
# image must not boot on the newer schema).
set -eu

usage() {
  printf 'usage: %s <previous-version>   (e.g. %s v0.3.0)\n' "$(basename "$0")" "$(basename "$0")" >&2
}

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  usage
  exit 2
fi

tag="$1"
# The tag is interpolated into a YAML document, so it must not carry anything
# that could end the scalar or inject another key. Same shape the packager
# accepts (validate_release_tag).
case "$tag" in
  *[!A-Za-z0-9._-]*)
    printf 'refusing tag %s: use the release version (letters, digits, dot, dash, underscore)\n' "$tag" >&2
    exit 2
    ;;
esac

app_service="${FARNA_CONTROL_APP_SERVICE:-modulo}"

compose_file="${COMPOSE_FILE:-}"
if [ -z "$compose_file" ]; then
  if [ -f deploy/compose/docker-compose.prod.yml ]; then
    compose_file=deploy/compose/docker-compose.prod.yml
  else
    # Extracted compose release bundle layout: the helper sits beside it.
    compose_file=docker-compose.prod.yml
  fi
fi

if [ ! -f "$compose_file" ]; then
  printf 'compose file not found: %s\n' "$compose_file" >&2
  exit 2
fi

override="$(mktemp "${TMPDIR:-/tmp}/farna-rollback.XXXXXX.yml")"
trap 'rm -f "$override"' EXIT INT TERM
cat > "$override" <<EOF
services:
  ${app_service}:
    image: ghcr.io/farnalabs/farna-control:${tag}
EOF

printf 'Starting the previous release: %s from ghcr.io/farnalabs/farna-control:%s\n' "$app_service" "$tag"
# TAG labels FARNA_CONTROL_VERSION (the snapshot directory name) so the running
# release is recorded truthfully; the IMAGE comes from the override above.
TAG="$tag" docker compose -f "$compose_file" -f "$override" up -d "$app_service"
