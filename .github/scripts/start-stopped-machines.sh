#!/usr/bin/env bash
# Start stopped app/worker-process machines to recover a full stop.
#
# Why this exists (2026-11-24 outage)
# -----------------------------------
# Production went fully down because BOTH machines (process groups "app" and
# "worker") were in state "stopped" (a stop requested by a user-source action,
# exit 0). The web-watchdog's restart pass only looks at STARTED machines, and
# its deploy guard also skips on a queued deploy run, so nothing recovered prod
# until a human ran `fly machine start`.
#
# This script is the recovery half: it STARTS stopped machines in the "app" or
# "worker" process group. It never restarts or stops anything.
#
# Why a per-group expected-started-count (not "start every stopped machine")
# --------------------------------------------------------------------------
# "Start every stopped machine" is wrong: it cannot tell an accidental stop
# from
#   * the documented deliberate prod cold-standby worker (fly.toml: one worker
#     is started and live, a second is a stopped cold standby), or
#   * a grouped stray left by a failed rolling deploy (fly.toml: group-less
#     strays are pruned by deploy.yml, but a GROUPED stray is never
#     auto-cleaned).
# Starting the standby moves prod from 2 to 3 started machines and consumes
# the rolling-deploy N+1 headroom; starting a stray keeps a stale recorded
# config/image alive under the worker [[restart]] policy "always".
#
# So recovery fills only the DEFICIT between an expected number of STARTED
# machines and the number actually started, per process group:
#   * app group:    EXPECTED_APP_STARTED    (default 1)
#   * worker group: EXPECTED_WORKER_STARTED (default 1)
# A group already at (or above) its expected started count starts nothing - so
# the cold standby and any extra stray stay stopped. Only a genuinely
# under-started group is recovered, and only up to its deficit. When several
# stopped machines are eligible for a deficit, the most recently updated is
# preferred (a deploy touches the canonical machine; a cold standby keeps its
# older recorded config), though any started worker restores worker capacity.
#
# Guards (each eligible machine must pass both)
# ---------------------------------------------
# 1. Freshness: the machine's updated_at must be older than
#    STOPPED_FRESHNESS_SECS (default 600s). A deploy or restart may have just
#    stopped it; an unparseable timestamp is treated as too fresh (skip; the
#    next scheduled run retries).
# 2. Deploy: no start while any deploy run is in_progress. A queued/pending run
#    deliberately does NOT block recovery (a run stuck pending must not keep
#    prod down); a failed deploy-run query skips, fail-closed.
#
# At most MAX_START_ATTEMPTS (default 5) start ATTEMPTS are made per run,
# counted on attempt (not on success) so a fleet of failing `fly machine start`
# calls cannot loop unbounded. The matrix runs prod and staging as separate
# jobs, so the whole workflow can attempt up to twice this.
#
# Contract
# --------
# - Usage: start-stopped-machines.sh <app>
# - Best-effort by design: a missing flyctl, an unreadable/unparseable machine
#   list, or a failed start emits a GitHub warning and exits 0. The next
#   scheduled run retries; CI is never blocked by a transient Fly/API error.
# - Writes the number of successful starts to ${STARTS_RESULT_FILE:-/tmp/
#   wd-started.count} and its human/machine-readable lines to
#   ${GITHUB_STEP_SUMMARY:-/dev/null}.
# - Exits 0 when there is nothing to do (the common case).
set -euo pipefail

APP="${1:?usage: start-stopped-machines.sh <app>}"

STOPPED_FRESHNESS_SECS="${STOPPED_FRESHNESS_SECS:-600}"
MAX_START_ATTEMPTS="${MAX_START_ATTEMPTS:-5}"
EXPECTED_APP_STARTED="${EXPECTED_APP_STARTED:-1}"
EXPECTED_WORKER_STARTED="${EXPECTED_WORKER_STARTED:-1}"
RESULT_FILE="${STARTS_RESULT_FILE:-/tmp/wd-started.count}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

# Default the reported start count to 0 so every early exit still leaves a
# readable result for the calling workflow.
printf '0\n' > "${RESULT_FILE}" 2>/dev/null || true

summary() {
  printf '%s\n' "$1" >> "${SUMMARY}"
}

if ! command -v fly >/dev/null 2>&1; then
  echo "::warning::flyctl not on PATH - cannot start stopped machines in ${APP}; next run retries."
  summary "- start pass: flyctl missing; no action (next run retries)."
  exit 0
fi

MACHINES_JSON="$(fly machines list --app "${APP}" --json 2>/dev/null || true)"
if [[ -z "${MACHINES_JSON}" ]]; then
  echo "::warning::fly machines list failed for ${APP} - machine state unknown; no stopped-machine action taken (next run retries)."
  summary "- start pass: machine state unknown (flyctl query failed); retrying next run."
  exit 0
fi

# Select the stopped machines to start: only under-started app/worker groups,
# only stopped machines, ranked most-recently-updated first, each carrying its
# group's deficit. Any other process group is ignored. A parse failure is
# treated as "unknown" (warn, no action) rather than "nothing stopped".
SEL_ERR="$(mktemp)"
SEL_RC=0
PLAN="$(printf '%s' "${MACHINES_JSON}" | \
  EXPECTED_APP_STARTED="${EXPECTED_APP_STARTED}" \
  EXPECTED_WORKER_STARTED="${EXPECTED_WORKER_STARTED}" \
  python3 -c '
import json, os, sys

try:
    machines = json.load(sys.stdin)
except Exception as exc:  # noqa: BLE001 - any parse failure means "unknown"
    print(f"::warning::could not parse the flyctl machines output: {exc}", file=sys.stderr)
    sys.exit(1)

expected = {
    "app": int(os.environ["EXPECTED_APP_STARTED"]),
    "worker": int(os.environ["EXPECTED_WORKER_STARTED"]),
}
started = {group: 0 for group in expected}
stopped = {group: [] for group in expected}

for machine in machines or []:
    config = machine.get("config") or {}
    group = str(
        (config.get("metadata") or {}).get("fly_process_group")
        or config.get("env", {}).get("FLY_PROCESS_GROUP")
        or ""
    )
    if group not in expected:
        continue
    state = str(machine.get("state", ""))
    if state == "started":
        started[group] += 1
    elif state == "stopped":
        stopped[group].append((str(machine.get("updated_at", "")), str(machine.get("id", ""))))

for group in ("app", "worker"):
    deficit = max(0, expected[group] - started[group])
    if deficit == 0:
        continue
    for updated_at, machine_id in sorted(stopped[group], key=lambda row: row[0], reverse=True):
        print(f"{machine_id}\t{group}\t{updated_at}\t{deficit}")
' 2>"${SEL_ERR}")" || SEL_RC=$?

if [[ "${SEL_RC}" -ne 0 ]]; then
  [[ -s "${SEL_ERR}" ]] && cat "${SEL_ERR}"
  echo "::warning::could not select stopped machines for ${APP} - machine state unknown; no action taken (next run retries)."
  summary "- start pass: could not select stopped machines (parse failure); retrying next run."
  rm -f "${SEL_ERR}"
  exit 0
fi
rm -f "${SEL_ERR}"

if [[ -z "${PLAN}" ]]; then
  echo "No stopped machine needs starting in ${APP} (each process group already meets its expected started count)."
  summary "- start pass: no stopped app/worker machine to start"
  exit 0
fi

# Deploy-guard query: number of deploy runs in progress, or "unknown" on a
# failed/non-numeric query (fail-closed). Queued/pending runs are excluded on
# purpose - a run stuck pending must not block recovery.
in_progress_deploy_runs() {
  local count
  count="$(gh run list --repo "${GITHUB_REPOSITORY:-farnalabs/farna-control}" \
    --workflow deploy.yml \
    --json databaseId,status \
    --jq '[.[] | select(.status == "in_progress")] | length' \
    </dev/null 2>/dev/null || true)"
  case "${count}" in
    '' | *[!0-9]*) echo "unknown" ;;
    *) echo "${count}" ;;
  esac
}

NOW_TS="$(date -u +%s)"
DEPLOY_STATE="$(in_progress_deploy_runs)"

STARTED=0
ATTEMPTS=0
declare -A GROUP_STARTED=()

while IFS=$'\t' read -r MID GROUP UPDATED_AT DEFICIT; do
  [[ -z "${MID}" ]] && continue

  # The group's deficit may already have been filled (an earlier eligible
  # machine started successfully, or the ranking continued past a skip).
  if [[ "${GROUP_STARTED[${GROUP}]:-0}" -ge "${DEFICIT}" ]]; then
    continue
  fi

  # Guard 1: freshness (a deploy/restart may have just stopped it).
  UPDATED_TS="$(date -u -d "${UPDATED_AT}" +%s 2>/dev/null || true)"
  if [[ -z "${UPDATED_TS}" ]]; then
    echo "::warning::Stopped machine ${MID} (${GROUP}) has an unparseable updated_at ('${UPDATED_AT}') - skipping start."
    summary "- Skipped start of \`${MID}\` (${GROUP}): machine timestamp unparseable; retrying next run."
    continue
  fi
  AGE=$(( NOW_TS - UPDATED_TS ))
  if [[ "${AGE}" -lt "${STOPPED_FRESHNESS_SECS}" ]]; then
    echo "::warning::Stopped machine ${MID} (${GROUP}) was updated ${AGE}s ago (< ${STOPPED_FRESHNESS_SECS}s) - skipping start."
    summary "- Skipped start of \`${MID}\` (${GROUP}): stopped only ${AGE}s ago (< $(( STOPPED_FRESHNESS_SECS / 60 )) min); retrying next run."
    continue
  fi

  # Guard 2: no start while a deploy is in_progress (fail-closed).
  if [[ "${DEPLOY_STATE}" == "unknown" ]]; then
    echo "::warning::Stopped machine ${MID} (${GROUP}): deploy-run status could not be queried - skipping start (fail-closed; retrying next run)."
    summary "- Skipped start of \`${MID}\` (${GROUP}): deploy-run status unknown (query failed); retrying next run."
    continue
  fi
  if [[ "${DEPLOY_STATE}" -gt 0 ]] 2>/dev/null; then
    echo "::warning::Stopped machine ${MID} (${GROUP}): ${DEPLOY_STATE} deploy run(s) in progress - skipping start."
    summary "- Skipped start of \`${MID}\` (${GROUP}): deploy run(s) in progress (${DEPLOY_STATE}); retrying next run."
    continue
  fi

  # Attempt cap counts ATTEMPTS, not successes, so a fleet of failing start
  # calls cannot loop unbounded within one run.
  if [[ "${ATTEMPTS}" -ge "${MAX_START_ATTEMPTS}" ]]; then
    echo "::warning::Reached the per-run start-attempt cap (${MAX_START_ATTEMPTS}) for ${APP} - remaining stopped machines retry next run."
    summary "- Start-attempt cap (${MAX_START_ATTEMPTS}) reached; remaining stopped machines retry next run."
    break
  fi
  ATTEMPTS=$(( ATTEMPTS + 1 ))

  echo "::notice::Starting stopped machine ${MID} (${GROUP}) in ${APP}."
  if fly machine start "${MID}" --app "${APP}" </dev/null; then
    STARTED=$(( STARTED + 1 ))
    GROUP_STARTED[${GROUP}]="$(( ${GROUP_STARTED[${GROUP}]:-0} + 1 ))"
    summary "- **Started \`${MID}\`** (${GROUP}) at $(date -u '+%Y-%m-%dT%H:%M:%SZ'): stopped for more than $(( STOPPED_FRESHNESS_SECS / 60 )) minutes, no deploy in progress."
  else
    echo "::error::fly machine start failed for ${MID}."
    summary "- ERROR: start command failed for \`${MID}\` (${GROUP}) - manual attention may be required."
  fi
done <<< "${PLAN}"

printf '%s\n' "${STARTED}" > "${RESULT_FILE}" 2>/dev/null || true
echo "Stopped-machine pass complete for ${APP} (started: ${STARTED}, attempts: ${ATTEMPTS})."

exit 0
