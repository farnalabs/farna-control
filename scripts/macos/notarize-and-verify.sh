#!/usr/bin/env bash
# Submit a signed macOS DMG to Apple's notary service and prove the OUTCOME.
#
# FAR-2574: `xcrun notarytool submit ... --wait` exited 0 on every failing
# release run, yet Apple never issued a ticket: every later `stapler` fetch
# returned `CloudKit query ... "Record not found"` / `Could not find base64
# encoded ticket` for hours. A shell wrapper's exit status is not proof of the
# notarization status, and the run captured no status at all, so neither
# candidate cause could be distinguished.
#
# This wrapper makes the outcome provable:
#   * the DMG's cdhash and SHA-256 are recorded immediately BEFORE the submit,
#     and again immediately after it, and must be identical - so a re-sign or
#     rewrite between the two points fails the build;
#   * the submission is captured with `--output-format json` and its `id`,
#     `status` and `message` are printed and persisted as release evidence;
#   * anything other than `Accepted` is a hard failure, and the notary log is
#     fetched (`notarytool log <id>`) so the rejection issues are surfaced
#     instead of being discovered hours later as a missing ticket.
#
# FAR-2577: Apple can take hours to process a submission (2026-11-19: ~3h for
# 52f6997c). A `--wait` timeout is NOT a verdict - the submission has an id and
# is still In Progress - so it is treated as PENDING: the id is parsed (from
# stdout or stderr), `notarytool info <id>` is polled for up to --poll-budget,
# and an Accepted verdict continues normally. A submission still processing at
# the end of the budget exits with the distinct PENDING code (3), never 0 and
# never the generic failure; Invalid/Rejected stays a hard failure (exit 1).
# A submission record (id, status, cdhash, digest) is written before the submit
# and at every state change, so the caller can preserve it whatever happens.
#
# Exit codes: 0 Accepted, 1 failure (incl. Invalid/Rejected), 2 usage,
#             3 PENDING (Apple is still processing; no ticket yet).
#
# Usage:
#   notarize-and-verify.sh --dmg <path> --key <path> --key-id <id> --issuer <uuid> \
#     [--timeout 90m] [--poll-budget <seconds>] [--poll-interval <seconds>] \
#     [--identity-out <file>] [--evidence <file>] [--submission-out <file>]
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
IDENTITY_TOOL="${SCRIPT_DIR}/artefact-identity.sh"

DMG=""
KEY=""
KEY_ID=""
ISSUER=""
TIMEOUT="90m"
POLL_BUDGET=2700
POLL_INTERVAL=60
IDENTITY_OUT=""
EVIDENCE=""
SUBMISSION_OUT=""
EXIT_PENDING=3

usage() {
  cat >&2 <<'USAGE'
usage: notarize-and-verify.sh --dmg <path> --key <path> --key-id <id> --issuer <uuid>
                              [--timeout 90m] [--poll-budget <seconds>] [--poll-interval <seconds>]
                              [--identity-out <file>] [--evidence <file>] [--submission-out <file>]

  --dmg <path>           signed DMG to submit (required)
  --key <path>           App Store Connect API key (.p8) path (required)
  --key-id <id>          API key id (required)
  --issuer <uuid>        API key issuer id (required)
  --timeout <duration>   notarytool --wait timeout (default 90m)
  --poll-budget <secs>   after a --wait timeout, poll notarytool info this long (default 2700)
  --poll-interval <secs> seconds between notarytool info polls (default 60)
  --identity-out <file>  write the pre-submit artefact identity here
  --evidence <file>      write the submission id/status/message evidence here
  --submission-out <file> write the submission record (id/status/cdhash), from before the submit
USAGE
}

log() {
  printf '%s notarize-and-verify: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

fail() {
  log "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dmg)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      DMG="$2"
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
    --timeout)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      TIMEOUT="$2"
      shift 2
      ;;
    --poll-budget)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      POLL_BUDGET="$2"
      shift 2
      ;;
    --poll-interval)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      POLL_INTERVAL="$2"
      shift 2
      ;;
    --submission-out)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      SUBMISSION_OUT="$2"
      shift 2
      ;;
    --identity-out)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      IDENTITY_OUT="$2"
      shift 2
      ;;
    --evidence)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      EVIDENCE="$2"
      shift 2
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

[[ -n "$DMG" ]] || { log "--dmg is required" >&2; usage; exit 2; }
[[ -n "$KEY" ]] || { log "--key is required" >&2; usage; exit 2; }
[[ -n "$KEY_ID" ]] || { log "--key-id is required" >&2; usage; exit 2; }
[[ -n "$ISSUER" ]] || { log "--issuer is required" >&2; usage; exit 2; }
[[ "$POLL_BUDGET" =~ ^[0-9]+$ ]] || { log "--poll-budget must be whole seconds" >&2; usage; exit 2; }
[[ "$POLL_INTERVAL" =~ ^[1-9][0-9]*$ ]] || { log "--poll-interval must be a positive number of seconds" >&2; usage; exit 2; }
[[ -f "$DMG" ]] || fail "DMG not found: $DMG"
[[ -f "$KEY" ]] || fail "notary API key not found"
[[ -f "$IDENTITY_TOOL" ]] || fail "artefact identity helper not found: $IDENTITY_TOOL"
for tool in xcrun python3 codesign; do
  command -v "$tool" >/dev/null 2>&1 || fail "required tool not on PATH: $tool"
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/notarize-and-verify.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

read_field() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" | head -n 1
}

# notarytool prints one JSON object for --output-format json, but progress or
# warning lines can share the stream on some versions. This extracts the last
# balanced top-level object, so a stray line never turns the status into a
# silent empty value, and normalises it to one `key=value` per line.
normalise_notary_json() {
  local input="$1" output="$2"
  python3 - "$input" "$output" <<'PY'
import json
import sys
from pathlib import Path

source, destination = sys.argv[1], sys.argv[2]
raw = Path(source).read_text(encoding="utf-8", errors="replace")
payload = None
try:
    payload = json.loads(raw)
except ValueError:
    decoder = json.JSONDecoder()
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
if not isinstance(payload, dict):
    print(f"no parseable JSON object in {source}", file=sys.stderr)
    raise SystemExit(1)


def flatten(value: object, limit: int) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.split())[:limit]


lines = [
    "id=" + flatten(payload.get("id"), 200),
    "status=" + flatten(payload.get("status"), 80),
    "message=" + flatten(payload.get("message"), 1000),
]
issues = payload.get("issues")
if isinstance(issues, list):
    for issue in issues:
        if isinstance(issue, dict):
            lines.append(
                "issue="
                + flatten(issue.get("severity"), 40)
                + "|"
                + flatten(issue.get("path"), 300)
                + "|"
                + flatten(issue.get("message"), 400)
            )
        else:
            lines.append("issue=" + flatten(issue, 400))
Path(destination).write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"normalised notary output: id/status/message + {len(lines) - 3} issue(s)")
PY
}

# 1. Identity immediately BEFORE the submit. Everything Apple signs a ticket
#    for is derived from these bytes.
IDENTITY_OUT="${IDENTITY_OUT:-$WORK/pre-submit-identity.env}"
bash "$IDENTITY_TOOL" record --path "$DMG" --out "$IDENTITY_OUT" ||
  fail "could not record the artefact identity before submission"
cdhash_before="$(read_field "$IDENTITY_OUT" cdhash)"
sha256_before="$(read_field "$IDENTITY_OUT" sha256)"

# The submission record is written BEFORE the submit and rewritten at every
# state change, so the workflow can upload it (with the submitted DMG) whatever
# happens next - including a job that dies inside the notary wait. stapler
# fetches a ticket by cdhash, so a preserved record + DMG is always stapleable
# once Apple accepts the submission. (FAR-2577)
write_submission_record() {
  local phase="$1" id="$2" state="$3"
  [[ -n "$SUBMISSION_OUT" ]] || return 0
  mkdir -p "$(dirname -- "$SUBMISSION_OUT")"
  {
    printf 'phase=%s\n' "$phase"
    printf 'submission_id=%s\n' "$id"
    printf 'status=%s\n' "$state"
    printf 'dmg=%s\n' "$(basename -- "$DMG")"
    printf 'cdhash=%s\n' "$cdhash_before"
    printf 'sha256_submitted=%s\n' "$sha256_before"
    printf 'recorded_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$SUBMISSION_OUT"
}

write_submission_record submitting "" ""

# 2. Submit, capturing the structured result. The exit status is checked, but it
#    is NOT treated as the notarization status.
log "xcrun notarytool submit <path> --wait --timeout $TIMEOUT --output-format json"
set +e
xcrun notarytool submit "$DMG" \
  --key "$KEY" \
  --key-id "$KEY_ID" \
  --issuer "$ISSUER" \
  --wait \
  --timeout "$TIMEOUT" \
  --output-format json >"$WORK/submit.json" 2>"$WORK/submit.stderr"
submit_exit=$?
set -e
printf 'notary: submit command_exit=%d\n' "$submit_exit"

if [[ -s "$WORK/submit.stderr" ]]; then
  printf 'notary submit stderr:\n'
  head -n 40 "$WORK/submit.stderr"
fi

# notarytool can report a `--wait` timeout as a JSON object on STDERR with an
# empty stdout (2026-11-19, submission 52f6997c: exit 124 and "no parseable JSON
# result"), so stderr is the second place a structured result can be.
if normalise_notary_json "$WORK/submit.json" "$WORK/submit.env"; then
  :
elif [[ -s "$WORK/submit.stderr" ]] && normalise_notary_json "$WORK/submit.stderr" "$WORK/submit.env"; then
  printf 'notary: the structured submit result was read from stderr\n'
else
  write_submission_record unknown "" ""
  fail "notarytool submit produced no parseable JSON result (command_exit=$submit_exit); the notarization status is unknown"
fi

submission_id="$(read_field "$WORK/submit.env" id)"
status="$(read_field "$WORK/submit.env" status)"
message="$(read_field "$WORK/submit.env" message)"

printf 'notary: submission_id=%s status=%s\n' "${submission_id:-<none>}" "${status:-<none>}"
if [[ -n "$message" ]]; then
  printf 'notary: message=%s\n' "$message"
fi

# A `--wait` timeout is not a verdict: Apple is still processing a submission it
# has an id for (exit 124, or "Timeout of N second(s) was reached before
# processing completed.").
wait_timed_out() {
  [[ "$submit_exit" -eq 124 ]] && return 0
  [[ "$message" == *"Timeout of"*"reached before processing completed"* ]]
}

# Poll `notarytool info` until Apple returns a verdict or the budget runs out. A
# failed or unparseable query is transient and is simply polled again.
poll_submission() {
  local deadline remaining nap info_exit polled
  deadline=$(($(date -u +%s) + POLL_BUDGET))
  while [[ -z "$status" || "$status" == "In Progress" ]]; do
    remaining=$((deadline - $(date -u +%s)))
    if [[ "$remaining" -le 0 ]]; then
      return 0
    fi
    nap="$POLL_INTERVAL"
    [[ "$nap" -gt "$remaining" ]] && nap="$remaining"
    sleep "$nap"
    set +e
    xcrun notarytool info "$submission_id" \
      --key "$KEY" \
      --key-id "$KEY_ID" \
      --issuer "$ISSUER" \
      --output-format json >"$WORK/info.json" 2>"$WORK/info.stderr"
    info_exit=$?
    set -e
    polled=""
    if normalise_notary_json "$WORK/info.json" "$WORK/info.env" >/dev/null 2>&1 ||
      normalise_notary_json "$WORK/info.stderr" "$WORK/info.env" >/dev/null 2>&1; then
      polled="$(read_field "$WORK/info.env" status)"
    fi
    printf 'notary: info poll command_exit=%d status=%s\n' "$info_exit" "${polled:-<none>}"
    if [[ "$info_exit" -eq 0 && -n "$polled" ]]; then
      status="$polled"
      message="$(read_field "$WORK/info.env" message)"
    fi
  done
}

cdhash_after=""
sha256_after=""

# Identity immediately AFTER Apple's processing. No staple has happened yet, so
# both the cdhash and the digest must be byte-identical to the submitted bytes.
verify_post_submit_identity() {
  bash "$IDENTITY_TOOL" verify --expected "$IDENTITY_OUT" --path "$DMG" --record-out "$WORK/post-submit-identity.env" ||
    fail "the DMG changed between submission and the post-submit check; Apple notarized different bytes"
  cdhash_after="$(read_field "$WORK/post-submit-identity.env" cdhash)"
  sha256_after="$(read_field "$WORK/post-submit-identity.env" sha256)"
}

write_evidence() {
  local outcome="$1"
  [[ -n "$EVIDENCE" ]] || return 0
  mkdir -p "$(dirname -- "$EVIDENCE")"
  {
    printf 'outcome=%s\n' "$outcome"
    printf 'submission_id=%s\n' "$submission_id"
    printf 'status=%s\n' "$status"
    printf 'message=%s\n' "$message"
    printf 'dmg=%s\n' "$DMG"
    printf 'cdhash=%s\n' "$cdhash_before"
    printf 'sha256_submitted=%s\n' "$sha256_before"
    printf 'sha256_submit_check=%s\n' "$sha256_after"
    printf 'cdhash_submit_check=%s\n' "$cdhash_after"
  } >"$EVIDENCE"
  log "wrote notarization evidence to $EVIDENCE"
}

write_step_summary() {
  local outcome="$1"
  [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] || return 0
  {
    printf '### Apple notarization\n\n'
    printf '| field | value |\n| --- | --- |\n'
    printf '| outcome | %s |\n' "$outcome"
    printf '| submission id | %s |\n' "$submission_id"
    printf '| status | %s |\n' "$status"
    printf '| cdhash | %s |\n' "$cdhash_before"
  } >>"$GITHUB_STEP_SUMMARY"
}

if [[ "$submit_exit" -ne 0 ]]; then
  if wait_timed_out && [[ -n "$submission_id" ]]; then
    # The status is PENDING, not unknown: Apple holds the upload under this id
    # and is still processing it. Poll it towards a verdict within the budget.
    [[ -n "$status" && "$status" != "In Progress" ]] || status="In Progress"
    write_submission_record pending "$submission_id" "$status"
    log "notarytool --wait timed out with submission $submission_id still processing; polling notarytool info for up to ${POLL_BUDGET}s"
    poll_submission
  else
    write_submission_record failed "$submission_id" "$status"
    fail "notarytool submit exited $submit_exit (status=${status:-unknown}); the artefact was not notarized"
  fi
fi

# 3. Still processing when the budget ran out: a distinct PENDING outcome. It is
#    not a success (no ticket exists yet) and not a failure (Apple may still
#    accept it). The caller decides; the submission id is the hand-off.
if [[ -z "$status" || "$status" == "In Progress" ]]; then
  status="In Progress"
  verify_post_submit_identity
  write_submission_record pending "$submission_id" "$status"
  write_evidence pending
  write_step_summary pending
  printf 'notary: outcome=pending submission_id=%s\n' "$submission_id"
  printf '::warning::Apple notarization of submission %s is still In Progress after the notary wait and a %ss poll; the outcome is PENDING (exit %d). No ticket exists yet.\n' \
    "$submission_id" "$POLL_BUDGET" "$EXIT_PENDING" >&2
  exit "$EXIT_PENDING"
fi

# 4. Anything other than Accepted is terminal: no ticket will ever be issued
#    for this submission. Fetch the notary log so the rejection reason is in the
#    build log instead of being guessed at from a missing ticket.
if [[ "$status" != "Accepted" ]]; then
  write_submission_record rejected "$submission_id" "$status"
  if [[ -n "$submission_id" ]]; then
    printf 'notary: fetching the rejection log for %s\n' "$submission_id"
    set +e
    xcrun notarytool log "$submission_id" \
      --key "$KEY" \
      --key-id "$KEY_ID" \
      --issuer "$ISSUER" >"$WORK/log.json" 2>"$WORK/log.stderr"
    log_exit=$?
    set -e
    printf 'notary: log command_exit=%d\n' "$log_exit"
    if [[ -s "$WORK/log.stderr" ]]; then
      head -n 40 "$WORK/log.stderr"
    fi
    if [[ "$log_exit" -eq 0 ]] && normalise_notary_json "$WORK/log.json" "$WORK/log.env" >/dev/null; then
      issue_count="$(grep -c '^issue=' "$WORK/log.env" || true)"
      printf 'notary: log_status=%s issues=%s\n' "$(read_field "$WORK/log.env" status)" "${issue_count:-0}"
      grep '^issue=' "$WORK/log.env" | head -n 20 || true
    else
      printf 'notary: the notary log could not be retrieved for %s\n' "$submission_id"
    fi
  fi
  printf '::error::Apple notarization finished with status %s (submission %s); no ticket will be issued for this artefact.\n' \
    "${status:-<none>}" "${submission_id:-<none>}" >&2
  exit 1
fi

# 5. Accepted. Re-prove the identity, then persist the evidence the delivery
#    needs and surface it in the step summary so a later missing ticket can be
#    explained from the run itself.
verify_post_submit_identity
write_submission_record accepted "$submission_id" "$status"
write_evidence accepted
write_step_summary accepted

log "Apple accepted the submission: id=$submission_id cdhash=$cdhash_before"
