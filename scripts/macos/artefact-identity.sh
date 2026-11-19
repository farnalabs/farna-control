#!/usr/bin/env bash
# Record and verify the code-signing identity of a macOS artefact (FAR-2574).
#
# Apple issues a notarization ticket for the CDHash of the bytes it was given.
# If the published artefact is re-signed, re-created or rewritten after the
# submission, its CDHash no longer matches the one the ticket was bound to, and
# `stapler` can never fetch a ticket - it reports `Record not found` forever.
# This helper records that identity at the two points that matter (immediately
# before `notarytool submit` and immediately before upload) so a mismatch is a
# loud build failure instead of an unexplained missing ticket.
#
# CDHash is the identity notarization is bound to. The SHA-256 digest is
# recorded alongside it as evidence: stapling legitimately rewrites a DMG, so a
# digest change is only tolerated when the caller states a staple happened, and
# even then the CDHash must still be identical.
#
# Usage:
#   artefact-identity.sh record --path <path> --out <file>
#   artefact-identity.sh verify --expected <file> --path <path> [--after-staple] [--record-out <file>]
#
# `record` writes a key=value file (path, cdhash, sha256, size).
# `verify` records the current identity and fails unless it matches `expected`;
# --record-out keeps that fresh record for the caller's evidence file.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: artefact-identity.sh record --path <path> --out <file>
       artefact-identity.sh verify --expected <file> --path <path> [--after-staple] [--record-out <file>]

  record            write the artefact's path, cdhash, sha256 and size to <file>
  verify            re-record the artefact and fail unless it matches <file>
  --after-staple    tolerate a sha256 change (stapling rewrites the file); the
                    cdhash must still be identical
  --record-out      where verify writes the record it just observed
USAGE
}

log() {
  printf '%s artefact-identity: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

read_field() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" | head -n 1
}

# macOS ships shasum; the GNU coreutils spelling keeps the helper runnable in a
# Linux test harness. Neither present is a hard failure, never a silent skip.
sha256_of() {
  local path="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return 0
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return 0
  fi
  fail "no SHA-256 tool on PATH (shasum or sha256sum)"
}

record_identity() {
  local path="$1" out="$2" cdhash sha256
  [[ -f "$path" ]] || fail "artefact not found: $path"
  # codesign writes the display output to stderr; an unsigned artefact prints a
  # diagnostic and no CDHash, which must fail closed rather than record "".
  cdhash="$(codesign --display --verbose=4 "$path" 2>&1 | sed -n 's/^[[:space:]]*CDHash=//p' | head -n 1 || true)"
  [[ -n "$cdhash" ]] || fail "codesign reported no CDHash for $path; an unsigned artefact cannot carry a notarization ticket"
  sha256="$(sha256_of "$path")"
  {
    printf 'path=%s\n' "$path"
    printf 'cdhash=%s\n' "$cdhash"
    printf 'sha256=%s\n' "$sha256"
    printf 'size=%s\n' "$(wc -c <"$path" | tr -d '[:space:]')"
  } >"$out"
  log "recorded path=$path cdhash=$cdhash sha256=$sha256"
}

verify_identity() {
  local expected="$1" path="$2" after_staple="$3" record_out="$4"
  local actual_cdhash expected_cdhash actual_sha256 expected_sha256
  [[ -f "$expected" ]] || fail "expected identity record not found: $expected"
  record_identity "$path" "$record_out"
  expected_cdhash="$(read_field "$expected" cdhash)"
  actual_cdhash="$(read_field "$record_out" cdhash)"
  expected_sha256="$(read_field "$expected" sha256)"
  actual_sha256="$(read_field "$record_out" sha256)"
  [[ -n "$expected_cdhash" ]] || fail "identity record $expected has no cdhash"
  if [[ "$expected_cdhash" != "$actual_cdhash" ]]; then
    printf '::error::artefact cdhash changed after notarization: notarized=%s current=%s path=%s\n' \
      "$expected_cdhash" "$actual_cdhash" "$path" >&2
    fail "the published artefact is not the one Apple notarized; a ticket can never be fetchable for it"
  fi
  if [[ "$expected_sha256" != "$actual_sha256" ]]; then
    if [[ "$after_staple" == "true" ]]; then
      log "sha256 changed (expected): stapling rewrote $path; cdhash is identical"
    else
      printf '::error::artefact bytes changed with no staple in between: before=%s after=%s path=%s\n' \
        "$expected_sha256" "$actual_sha256" "$path" >&2
      fail "the artefact was rewritten after notarization and the ticket no longer describes these bytes"
    fi
  fi
  log "identity verified path=$path cdhash=$actual_cdhash sha256=$actual_sha256"
}

action="${1:-}"
[[ -n "$action" ]] || { usage; exit 2; }
shift

PATH_ARG=""
OUT_ARG=""
EXPECTED_ARG=""
RECORD_OUT_ARG=""
AFTER_STAPLE="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      PATH_ARG="$2"
      shift 2
      ;;
    --out)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      OUT_ARG="$2"
      shift 2
      ;;
    --expected)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      EXPECTED_ARG="$2"
      shift 2
      ;;
    --record-out)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      RECORD_OUT_ARG="$2"
      shift 2
      ;;
    --after-staple)
      AFTER_STAPLE="true"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      log "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

command -v codesign >/dev/null 2>&1 || fail "required tool not on PATH: codesign"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/artefact-identity.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

case "$action" in
  record)
    [[ -n "$PATH_ARG" ]] || { log "--path is required" >&2; usage; exit 2; }
    [[ -n "$OUT_ARG" ]] || { log "--out is required" >&2; usage; exit 2; }
    record_identity "$PATH_ARG" "$OUT_ARG"
    ;;
  verify)
    [[ -n "$PATH_ARG" ]] || { log "--path is required" >&2; usage; exit 2; }
    [[ -n "$EXPECTED_ARG" ]] || { log "--expected is required" >&2; usage; exit 2; }
    verify_identity "$EXPECTED_ARG" "$PATH_ARG" "$AFTER_STAPLE" "${RECORD_OUT_ARG:-$WORK/actual-identity.env}"
    ;;
  *)
    log "unknown action: $action" >&2
    usage
    exit 2
    ;;
esac
