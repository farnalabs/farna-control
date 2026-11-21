#!/usr/bin/env bash
# native-stack-assertions.sh — FAR-2581 post-start stability assertions for a
# RUNNING native install (Linux bundle or macOS app). Shared by the Linux
# first-run gate (scripts/linux/native-firstrun-smoke.sh) and the macOS
# installer smoke (.github/workflows/macos-installer.yml) so both platforms
# assert the same thing.
#
# Usage:
#   scripts/native-stack-assertions.sh <farna-control-cli> <scratch-dir> [<data-dir>]
#
# <farna-control-cli>  the installed CLI (Linux shim / macOS app executable)
# <scratch-dir>        writable dir for response bodies (holds no secrets
#                      beyond the throwaway smoke admin's token)
# <data-dir>           optional --data-dir passed to status / users add
#
# Why a settle window: a single /healthz probe right after boot PASSES while a
# crash-looping child (the FAR-2581 saq-system SAQ_AUTH_PASSWORD loop) is
# still under the supervisor's crash cap; the cap trips and degrades the whole
# stack a little later. Asserting after >= 180s catches that.
#
# Asserts, in order:
#   0. first boot reports /healthz/ready 200 within SMOKE_READY_TIMEOUT_SECONDS
#      (default 900; first boot runs initdb + all migrations)
#   a. SMOKE_SETTLE_SECONDS after that (default 180, min 180): `status --json` shows
#      api, postgres, redis, saq-runs and saq-system alive, stack not degraded
#   b. GET /healthz/ready answers 200 (and, when SMOKE_EXPECTED_VERSION is
#      set, its "version" equals it)
#   c. `users add <admin> --admin` succeeds and does not land in the orphan org
#   d. POST /api/v1/auth/login answers 200 with a JWT whose org_id is not the
#      nil-UUID orphan org
#   e. an authenticated GET /api/v1/pipelines answers 200
#
# Requires bash (3.2+ is fine), python3, curl, openssl. Exit 0 = all passed.

set -euo pipefail

ORPHAN_ORG_ID="00000000-0000-0000-0000-000000000000"
ORPHAN_ORG_NAME="Orphan (unattributed errors)"
MIN_SETTLE_SECONDS=180

log() { printf '[assert %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }

[[ $# -ge 2 && $# -le 3 ]] || { printf 'usage: %s <cli> <scratch-dir> [<data-dir>]\n' "$0" >&2; exit 2; }
CLI="$1"
SCRATCH="$2"
DATA_DIR_ARGS=()
if [[ $# -eq 3 ]]; then
  DATA_DIR_ARGS=(--data-dir "$3")
fi
[[ -x "${CLI}" ]] || { printf 'not an executable CLI: %s\n' "${CLI}" >&2; exit 2; }
mkdir -p -- "${SCRATCH}"
SETTLE="${SMOKE_SETTLE_SECONDS:-${MIN_SETTLE_SECONDS}}"
[[ "${SETTLE}" =~ ^[0-9]+$ ]] || { printf 'SMOKE_SETTLE_SECONDS must be an integer\n' >&2; exit 2; }
(( SETTLE >= MIN_SETTLE_SECONDS )) || { printf 'SMOKE_SETTLE_SECONDS must be >= %s\n' "${MIN_SETTLE_SECONDS}" >&2; exit 2; }

fail() {
  printf '[assert] FAIL: %s\n' "$*" >&2
  printf '[assert] ---- diagnostics: status ----\n' >&2
  "${CLI}" status ${DATA_DIR_ARGS[@]+"${DATA_DIR_ARGS[@]}"} 2>&1 | sed 's/^/[assert]   /' >&2 || true
  exit 1
}

# --- 0. first boot finishes: /healthz/ready answers 200 -------------------------
# First boot runs initdb + every migration before the SAQ workers may start
# (they are gated on migration head), and /healthz/ready only answers 200 once
# the SAQ system worker's reconcile has run. On a slow host that takes
# minutes, so the settle window below starts AFTER the stack first reports
# ready: the crash-cap check is "still healthy >= SETTLE seconds after it came
# up", not "healthy SETTLE seconds after start". A stack that never becomes
# ready (the FAR-2581 saq-system crash loop) fails here.
READY_TIMEOUT="${SMOKE_READY_TIMEOUT_SECONDS:-900}"
[[ "${READY_TIMEOUT}" =~ ^[0-9]+$ ]] || { printf 'SMOKE_READY_TIMEOUT_SECONDS must be an integer\n' >&2; exit 2; }
log "0: waiting up to ${READY_TIMEOUT}s for first boot to report /healthz/ready 200"
ready_deadline=$(( $(date +%s) + READY_TIMEOUT ))
first_ready=""
while (( $(date +%s) < ready_deadline )); do
  boot_port="$("${CLI}" status ${DATA_DIR_ARGS[@]+"${DATA_DIR_ARGS[@]}"} --json 2>/dev/null \
    | python3 -c 'import json,sys; print((json.load(sys.stdin).get("components", {}).get("api") or {}).get("port") or "")' 2>/dev/null || true)"
  if [[ "${boot_port}" =~ ^[0-9]+$ ]] && \
    [[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "http://127.0.0.1:${boot_port}/healthz/ready" 2>/dev/null || true)" == "200" ]]; then
    first_ready="$(date -u +%H:%M:%S)"
    break
  fi
  sleep 5
done
[[ -n "${first_ready}" ]] || fail "the stack never reported /healthz/ready 200 within ${READY_TIMEOUT}s"
log "  first /healthz/ready 200 at ${first_ready}"

# --- a. settle, then every component alive and not degraded -------------------
log "a: settling ${SETTLE}s after first readiness before asserting component health"
sleep "${SETTLE}"
"${CLI}" status ${DATA_DIR_ARGS[@]+"${DATA_DIR_ARGS[@]}"} --json > "${SCRATCH}/status.json" || fail "status --json exited non-zero"
python3 - "${SCRATCH}/status.json" <<'PY' || fail "component health assertion failed (see above)"
import json
import sys

REQUIRED = ("api", "postgres", "redis", "saq-runs", "saq-system")
payload = json.load(open(sys.argv[1], encoding="utf-8"))
problems = []
if payload.get("degraded"):
    problems.append(f"stack is DEGRADED: {payload['degraded']}")
if payload.get("error"):
    problems.append(f"status error: {payload['error']}")
components = payload.get("components") or {}
for name in REQUIRED:
    component = components.get(name)
    if not isinstance(component, dict):
        problems.append(f"component {name} missing from status")
        continue
    print(f"[assert]   {name:<11} pid={component.get('pid')} port={component.get('port')} alive={component.get('alive')}")
    if not component.get("alive"):
        problems.append(f"component {name} not alive")
for problem in problems:
    print(f"[assert]   PROBLEM: {problem}", file=sys.stderr)
sys.exit(1 if problems else 0)
PY
api_port="$(python3 -c 'import json,sys; print((json.load(open(sys.argv[1]))["components"].get("api") or {}).get("port") or "")' "${SCRATCH}/status.json")"
[[ "${api_port}" =~ ^[0-9]+$ ]] || fail "could not read the api port from status (got '${api_port}')"
BASE="http://127.0.0.1:${api_port}"

# --- b. readiness ---------------------------------------------------------------
log "b: GET ${BASE}/healthz/ready"
ready_meta="$(curl -sS -o "${SCRATCH}/ready.json" -w '%{http_code} %{time_total}' --max-time 60 "${BASE}/healthz/ready")" || \
  fail "/healthz/ready request failed"
ready_code="${ready_meta%% *}"
log "  /healthz/ready -> ${ready_code} in ${ready_meta#* }s $(head -c 400 "${SCRATCH}/ready.json")"
[[ "${ready_code}" == "200" ]] || fail "/healthz/ready answered ${ready_code}, expected 200"
# FAR-2589: when the caller knows the build version, the running app must
# report it (the macOS app reported "0.0.0-dev" while the Linux bundle carried
# its VERSION stamp).
if [[ -n "${SMOKE_EXPECTED_VERSION:-}" ]]; then
  reported_version="$(python3 -c 'import json,sys; v=json.load(open(sys.argv[1], encoding="utf-8")).get("version"); print(v if isinstance(v, str) else "")' "${SCRATCH}/ready.json" 2>/dev/null || true)"
  log "  /healthz/ready version=${reported_version:-<none>} expected=${SMOKE_EXPECTED_VERSION}"
  [[ "${reported_version}" == "${SMOKE_EXPECTED_VERSION}" ]] || \
    fail "/healthz/ready reports version '${reported_version}', expected the build version '${SMOKE_EXPECTED_VERSION}'"
fi

# --- c. first admin -------------------------------------------------------------
log "c: users add (first admin)"
admin_email="smoke-admin@example.com"
admin_password="$(openssl rand -hex 24)Aa1-"
add_out="$("${CLI}" users add "${admin_email}" --admin --password "${admin_password}" ${DATA_DIR_ARGS[@]+"${DATA_DIR_ARGS[@]}"} 2>&1)" || \
  fail "users add exited non-zero: ${add_out}"
log "  ${add_out}"
[[ "${add_out}" != *"${ORPHAN_ORG_NAME}"* ]] || fail "the first admin landed in the orphan org: ${add_out}"
[[ "${add_out}" == *"admin in organisation"* ]] || fail "users add did not report an admin membership: ${add_out}"

# --- d. login -------------------------------------------------------------------
log "d: POST ${BASE}/api/v1/auth/login"
python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": sys.argv[2]}))' \
  "${admin_email}" "${admin_password}" > "${SCRATCH}/login-body.json"
login_code="$(curl -sS -o "${SCRATCH}/login.json" -w '%{http_code}' --max-time 60 \
  -H 'Content-Type: application/json' --data-binary "@${SCRATCH}/login-body.json" "${BASE}/api/v1/auth/login")" || \
  fail "login request failed"
rm -f -- "${SCRATCH}/login-body.json"
log "  login -> ${login_code}"
[[ "${login_code}" == "200" ]] || fail "login answered ${login_code}: $(head -c 400 "${SCRATCH}/login.json")"
token_org="$(python3 - "${SCRATCH}/login.json" <<'PY'
import base64
import json
import sys

token = json.load(open(sys.argv[1], encoding="utf-8")).get("access_token") or ""
parts = token.split(".")
if len(parts) != 3:
    raise SystemExit("no JWT access_token in the login response")
body = parts[1] + "=" * (-len(parts[1]) % 4)
print(json.loads(base64.urlsafe_b64decode(body)).get("org_id") or "")
PY
)" || fail "login response carried no usable access token"
log "  token org_id=${token_org}"
[[ -n "${token_org}" ]] || fail "access token has no org_id claim"
[[ "${token_org}" != "${ORPHAN_ORG_ID}" ]] || fail "the admin's token is scoped to the orphan org"
token="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["access_token"])' "${SCRATCH}/login.json")"

# --- e. authenticated read ------------------------------------------------------
log "e: GET ${BASE}/api/v1/pipelines (authenticated)"
pipelines_code="$(curl -sS -o "${SCRATCH}/pipelines.json" -w '%{http_code}' --max-time 60 \
  -H "Authorization: Bearer ${token}" "${BASE}/api/v1/pipelines")" || fail "pipelines request failed"
log "  /api/v1/pipelines -> ${pipelines_code}"
[[ "${pipelines_code}" == "200" ]] || fail "authenticated GET answered ${pipelines_code}: $(head -c 400 "${SCRATCH}/pipelines.json")"

log "PASS: stack stable, first admin can log in and read"
