#!/usr/bin/env bash
# Report Apple's current verdict on a recorded notarization submission.
#
# FAR-2577: a release whose notarization was still PENDING when its build job
# finished is HELD (FAR-2573) and handed to macos-staple-retry.yml, which keeps
# trying to staple until the ticket exists. Stapling alone cannot tell "Apple is
# still processing" from "Apple rejected it": both look like a missing ticket,
# so a rejected submission would be retried until the 24h deadline. This helper
# asks Apple directly (`notarytool info <id>`) so a terminal rejection fails the
# retry loudly instead.
#
# The submission record is the `--submission-out` file written by
# notarize-and-verify.sh (`submission_id=`, `status=`, `cdhash=` ...).
#
# Prints `submission_id=<id>` and `notary_status=<status>` on stdout.
# Exit codes:
#   0  Accepted, still In Progress, or the verdict could not be read (transient;
#      stapling stays the proof of a ticket)
#   2  usage error
#   4  terminal rejection (Invalid, Rejected or any other non-pending verdict):
#      no ticket will ever exist for this submission
#
# Usage:
#   notary-submission-status.sh --record <file> --key <path> --key-id <id> --issuer <uuid>
set -euo pipefail

RECORD=""
KEY=""
KEY_ID=""
ISSUER=""
EXIT_REJECTED=4

usage() {
  cat >&2 <<'USAGE'
usage: notary-submission-status.sh --record <file> --key <path> --key-id <id> --issuer <uuid>

  --record <file>   submission record written by notarize-and-verify.sh --submission-out
  --key <path>      App Store Connect API key (.p8) path
  --key-id <id>     API key id
  --issuer <uuid>   API key issuer id
USAGE
}

log() {
  printf '%s notary-submission-status: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --record)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      RECORD="$2"
      shift 2
      ;;
    --key)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      KEY="$2"
      shift 2
      ;;
    --key-id)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      KEY_ID="$2"
      shift 2
      ;;
    --issuer)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      ISSUER="$2"
      shift 2
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      log "unknown argument: $1"
      usage
      exit 2
      ;;
  esac
done

[[ -n "$RECORD" && -n "$KEY" && -n "$KEY_ID" && -n "$ISSUER" ]] || { usage; exit 2; }
[[ -f "$RECORD" ]] || { log "submission record not found: $RECORD"; exit 2; }
[[ -f "$KEY" ]] || { log "notary API key not found"; exit 2; }
for tool in xcrun python3; do
  command -v "$tool" >/dev/null 2>&1 || { log "required tool not on PATH: $tool"; exit 2; }
done

submission_id="$(sed -n 's/^submission_id=//p' "$RECORD" | head -n 1)"
printf 'submission_id=%s\n' "$submission_id"
if [[ -z "$submission_id" ]]; then
  log "the submission record carries no submission id; Apple's verdict cannot be queried"
  printf 'notary_status=unknown\n'
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/notary-submission-status.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

set +e
xcrun notarytool info "$submission_id" \
  --key "$KEY" \
  --key-id "$KEY_ID" \
  --issuer "$ISSUER" \
  --output-format json >"$WORK/info.json" 2>"$WORK/info.stderr"
info_exit=$?
set -e

# The last balanced JSON object on stdout, else stderr, carries the status.
status="$(python3 - "$WORK/info.json" "$WORK/info.stderr" <<'PY'
import json
import sys
from pathlib import Path

for source in sys.argv[1:]:
    raw = Path(source).read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    payload = None
    position = 0
    while True:
        position = raw.find("{", position)
        if position < 0:
            break
        try:
            candidate, end = decoder.raw_decode(raw, position)
        except ValueError:
            position += 1
            continue
        payload = candidate
        position = end
    if isinstance(payload, dict) and payload.get("status"):
        print(" ".join(str(payload["status"]).split())[:80])
        break
PY
)"

if [[ "$info_exit" -ne 0 || -z "$status" ]]; then
  log "notarytool info for ${submission_id} returned no verdict (command_exit=${info_exit}); treating it as unknown"
  head -n 20 "$WORK/info.stderr" >&2 || true
  printf 'notary_status=unknown\n'
  exit 0
fi

printf 'notary_status=%s\n' "$status"
case "$status" in
  Accepted | "In Progress")
    exit 0
    ;;
  *)
    log "submission ${submission_id} ended ${status}: Apple will never issue a ticket for it"
    exit "$EXIT_REJECTED"
    ;;
esac
