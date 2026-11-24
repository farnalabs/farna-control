#!/usr/bin/env bash
# Proof test for start-stopped-machines.sh (2026-11-24 full-stop outage).
#
# Usage: bash .github/scripts/test-start-stopped-machines.sh
#
# Runs the real script against a fake `fly` and a fake `gh` on PATH. No Fly API,
# no GitHub API, no network. Pins the invariants that matter:
#   1. an under-started app/worker group has stopped machines started;
#   2. a group ALREADY at its expected started count starts NOTHING - so the
#      documented cold-standby worker and a grouped stray stay stopped;
#   3. machines in any other process group are ignored;
#   4. the most-recently-updated stopped machine is preferred when a deficit
#      allows only one start;
#   5. an unparseable updated_at, a too-fresh machine, or an in_progress deploy
#      skips the start;
#   6. a QUEUED deploy does not block (a run stuck pending must not keep prod
#      down);
#   7. an unreadable or unparseable machine list takes no action and exits 0;
#   8. a failed `fly machine start` is best-effort (warning, exit 0) and still
#      counts towards the per-run attempt cap, so failing starts cannot loop.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/start-stopped-machines.sh"
APP="farna-control"

APP_MACHINE_ID="1111aaaa0001"
WORKER_MACHINE_ID="2222bbbb0002"
STANDBY_MACHINE_ID="3333cccc0003"
STRAY_MACHINE_ID="4444dddd0004"
OTHER_GROUP_MACHINE_ID="5555eeee0005"
OLD_UPDATED="2020-01-01T00:00:00Z"
NEWER_UPDATED="2021-01-01T00:00:00Z"
FRESH_UPDATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT
BIN_DIR="${WORK_DIR}/bin"
mkdir -p "${BIN_DIR}"
START_LOG="${WORK_DIR}/starts.log"
GH_LOG="${WORK_DIR}/gh.log"
RESULT_FILE="${WORK_DIR}/started.count"
SUMMARY_FILE="${WORK_DIR}/summary.md"

cat > "${BIN_DIR}/fly" <<'FAKE_FLY'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  machines)
    if [[ "${2:-}" == "list" ]]; then
      if [[ "${FLY_MACHINES_JSON:-}" == "__FAIL__" ]]; then
        exit 1
      fi
      printf '%s' "${FLY_MACHINES_JSON:-[]}"
      exit 0
    fi
    ;;
  machine)
    if [[ "${2:-}" == "start" ]]; then
      echo "${3:-UNKNOWN}" >> "${FLY_START_LOG:?}"
      exit "${FLY_START_EXIT:-0}"
    fi
    ;;
esac
echo "fake fly: unexpected args: $*" >&2
exit 64
FAKE_FLY
chmod +x "${BIN_DIR}/fly"

# Faithful gh emulation: emits raw deploy-run JSON and, when `--jq` is passed,
# applies the REAL filter the script ships (so "in_progress only" is tested,
# not merely asserted). A queued-only payload therefore yields 0.
cat > "${BIN_DIR}/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
echo "gh $*" >> "${GH_LOG:?}"
if [[ "${GH_JSON:-}" == "__FAIL__" ]]; then
  exit 1
fi
JQ_EXPR=""
ARGS=("$@")
for ((i = 0; i < ${#ARGS[@]}; i++)); do
  if [[ "${ARGS[$i]}" == "--jq" ]]; then
    JQ_EXPR="${ARGS[$((i + 1))]:-}"
  fi
done
if [[ -n "${JQ_EXPR}" ]]; then
  printf '%s' "${GH_JSON:-[]}" | jq -r "${JQ_EXPR}"
else
  printf '%s' "${GH_JSON:-[]}"
fi
FAKE_GH
chmod +x "${BIN_DIR}/gh"

PASS=0
FAIL=0
EXIT_CODE_LABEL="exit code"
NO_START_LABEL="no start"

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [[ "${expected}" == "${actual}" ]]; then
    echo "  PASS: ${label}"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: ${label} (expected='${expected}', got='${actual}')"
    FAIL=$((FAIL + 1))
  fi
}

# machine_specs is "<id>,<state>,<group>,<updated_at>" per argument.
build_machines() {
  python3 - "$@" <<'PY'
import json, sys
machines = []
for spec in sys.argv[1:]:
    mid, state, group, updated = spec.split(",", 3)
    machines.append({
        "id": mid,
        "state": state,
        "config": {"metadata": {"fly_process_group": group}},
        "updated_at": updated,
    })
print(json.dumps(machines))
PY
}

# run_case <name> <machines-json> [gh-json] [start-exit] [exp-app] [exp-worker]
run_case() {
  local name="$1" json="$2" ghjson="${3:-[]}" start_exit="${4:-0}"
  local exp_app="${5:-1}" exp_worker="${6:-1}"
  echo "=== ${name} ==="
  : > "${START_LOG}"
  : > "${GH_LOG}"
  : > "${SUMMARY_FILE}"
  rm -f "${RESULT_FILE}"
  set +e
  OUTPUT="$(PATH="${BIN_DIR}:${PATH}" \
    FLY_MACHINES_JSON="${json}" \
    FLY_START_LOG="${START_LOG}" \
    FLY_START_EXIT="${start_exit}" \
    GH_JSON="${ghjson}" \
    GH_LOG="${GH_LOG}" \
    EXPECTED_APP_STARTED="${exp_app}" \
    EXPECTED_WORKER_STARTED="${exp_worker}" \
    STARTS_RESULT_FILE="${RESULT_FILE}" \
    GITHUB_STEP_SUMMARY="${SUMMARY_FILE}" \
    GITHUB_REPOSITORY="farnalabs/farna-control" \
    bash "${TARGET}" "${APP}" 2>&1)"
  RC=$?
  set -e
  STARTED_IDS="$(tr '\n' ' ' < "${START_LOG}" | sed 's/ *$//')"
  RESULT_COUNT="$(cat "${RESULT_FILE}" 2>/dev/null || echo 0)"
  echo "  stdout: $(printf '%s' "${OUTPUT}" | tr '\n' ' ')"
}

# --- 1. Both groups fully stopped: both are started -------------------------
run_case "both app and worker stopped are started" \
  "$(build_machines "${APP_MACHINE_ID},stopped,app,${OLD_UPDATED}" "${WORKER_MACHINE_ID},stopped,worker,${OLD_UPDATED}")"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "started both machines" "${APP_MACHINE_ID} ${WORKER_MACHINE_ID}" "${STARTED_IDS}"
assert_eq "reported start count" "2" "${RESULT_COUNT}"

# --- 2. Cold standby: worker group already has its started worker -----------
run_case "cold-standby worker is not started when the group is already started" \
  "$(build_machines "${APP_MACHINE_ID},started,app,${OLD_UPDATED}" "${WORKER_MACHINE_ID},started,worker,${OLD_UPDATED}" "${STANDBY_MACHINE_ID},stopped,worker,${OLD_UPDATED}")"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "no cold-standby start" "" "${STARTED_IDS}"
assert_eq "reported start count" "0" "${RESULT_COUNT}"

# --- 3. Grouped stray: app group already at its expected started count ------
run_case "grouped stray in a started group is not started" \
  "$(build_machines "${APP_MACHINE_ID},started,app,${OLD_UPDATED}" "${STRAY_MACHINE_ID},stopped,app,${OLD_UPDATED}")"
assert_eq "no stray start" "" "${STARTED_IDS}"

# --- 4. Another process group is ignored ------------------------------------
run_case "a stopped machine in another process group is ignored" \
  "$(build_machines "${OTHER_GROUP_MACHINE_ID},stopped,release,${OLD_UPDATED}")"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "no other-group start" "" "${STARTED_IDS}"

# --- 5. Most-recently-updated stopped machine is preferred ------------------
run_case "deficit start prefers the most recently updated stopped machine" \
  "$(build_machines "${STRAY_MACHINE_ID},stopped,app,${OLD_UPDATED}" "${APP_MACHINE_ID},stopped,app,${NEWER_UPDATED}")"
assert_eq "started one machine" "${APP_MACHINE_ID}" "${STARTED_IDS}"
assert_eq "reported start count" "1" "${RESULT_COUNT}"

# --- 6. Unparseable updated_at is skipped -----------------------------------
run_case "unparseable updated_at skips the start" \
  "$(build_machines "${APP_MACHINE_ID},stopped,app,not-a-timestamp")"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "no start with unparseable timestamp" "" "${STARTED_IDS}"
assert_eq "warns about the unparseable timestamp" "1" \
  "$(printf '%s' "${OUTPUT}" | grep -c 'unparseable updated_at' || true)"

# --- 7. Too-fresh machine is skipped ----------------------------------------
run_case "a machine stopped only moments ago is skipped" \
  "$(build_machines "${APP_MACHINE_ID},stopped,app,${FRESH_UPDATED}")"
assert_eq "no start for a too-fresh machine" "" "${STARTED_IDS}"

# --- 8. in_progress deploy blocks the start ---------------------------------
run_case "an in_progress deploy blocks the start" \
  "$(build_machines "${APP_MACHINE_ID},stopped,app,${OLD_UPDATED}")" \
  '[{"databaseId":1,"status":"in_progress"}]'
assert_eq "no start during an in_progress deploy" "" "${STARTED_IDS}"

# --- 9. queued deploy does NOT block the start ------------------------------
run_case "a queued deploy does not block the start" \
  "$(build_machines "${APP_MACHINE_ID},stopped,app,${OLD_UPDATED}")" \
  '[{"databaseId":1,"status":"queued"}]'
assert_eq "start proceeds while a deploy is only queued" "${APP_MACHINE_ID}" "${STARTED_IDS}"

# --- 10. Unreadable machine list: warn, no action, exit 0 --------------------
run_case "failed machine list is best-effort no-op" "__FAIL__"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "no start when the list is unreadable" "" "${STARTED_IDS}"
assert_eq "warns when the list is unreadable" "1" \
  "$(printf '%s' "${OUTPUT}" | grep -c 'machines list failed' || true)"

# --- 11. Unparseable machine list: warn, no action, exit 0 -------------------
run_case "unparseable machine list is best-effort no-op" "not-json"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "no start when the list is unparseable" "" "${STARTED_IDS}"

# --- 12. Failed start is best-effort, and the attempt cap counts attempts ----
MANY=()
for i in 1 2 3 4 5 6; do
  MANY+=("capmachine000${i},stopped,app,${OLD_UPDATED}")
done
run_case "failed starts are best-effort and the attempt cap counts attempts" \
  "$(build_machines "${MANY[@]}")" "[]" "1" "10" "1"
assert_eq "${EXIT_CODE_LABEL}" "0" "${RC}"
assert_eq "attempts capped at 5 even though every start fails" "5" \
  "$(wc -l < "${START_LOG}" | tr -d ' ')"
assert_eq "no successful start reported" "0" "${RESULT_COUNT}"

echo ""
echo "=== summary: ${PASS} passed, ${FAIL} failed ==="
[[ "${FAIL}" -eq 0 ]]
