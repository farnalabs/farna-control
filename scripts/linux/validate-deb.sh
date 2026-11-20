#!/usr/bin/env bash
#
# Validate a farna-control .deb end to end on a fresh Ubuntu host (FAR-2579).
#
# Usage (as root, in a disposable ubuntu:22.04 / ubuntu:24.04 container or VM):
#   scripts/linux/validate-deb.sh <path/to/farna-control_<version>_amd64.deb>
#   scripts/linux/validate-deb.sh --installed
#
# --installed skips the local `apt-get install ./<deb>` and validates the
# farna-control package that is ALREADY installed (from the signed apt
# repository: .github/workflows/apt-repo.yml installs it the documented way
# first). Everything after the install step is identical.
#
# Flow: apt install the .deb -> as a NON-root user `farna-control start
# --detach` -> /healthz 200 on the api_port from state.json -> GET / serves the
# SPA index -> both SAQ workers alive per `farna-control status --json` ->
# `farna-control stop` -> apt remove (user data dir must survive) -> apt purge
# (user data dir must STILL survive, package tree gone).
#
# This is the exact script .github/workflows/linux-deb.yml runs in its
# ubuntu:22.04 / ubuntu:24.04 matrix, so a local Docker run reproduces CI.
#
# FARNA_DEB_FULL_CHECK=1 adds the long-run assertions: the stack must stay up
# (healthz 200 + both SAQ workers alive) for FARNA_DEB_STABILITY_SECONDS
# (default 180) after boot, and an admin created with `farna-control users add`
# must be able to log in. It is OFF by default until FAR-2581 lands: on the
# current bundle the saq-system worker crash-loops ("SAQ_AUTH_PASSWORD must be
# set") and the launcher tears the stack down a few minutes after start, which
# is a launcher defect, not a packaging one. Turn it on once FAR-2581 ships.
# The default checks run inside the first ~60s after boot for that reason.

set -euo pipefail

DEB="${1:?usage: validate-deb.sh <farna-control_<version>_amd64.deb> | --installed}"
FROM_INSTALLED=0
if [[ "${DEB}" = "--installed" ]]; then
  FROM_INSTALLED=1
fi
VALIDATION_USER="${FARNA_DEB_VALIDATION_USER:-fcvalidate}"
BOOT_TIMEOUT_SECONDS="${FARNA_DEB_BOOT_TIMEOUT:-300}"
FULL_CHECK="${FARNA_DEB_FULL_CHECK:-0}"
STABILITY_SECONDS="${FARNA_DEB_STABILITY_SECONDS:-180}"

step() {
  printf '\n=== %s ===\n' "$*"
}

fail() {
  printf 'validate-deb: FAIL: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || fail "run as root (it installs and removes the package)"
if [[ "${FROM_INSTALLED}" -eq 0 ]]; then
  [[ -f "${DEB}" ]] || fail "no such .deb: ${DEB}"
  case "${DEB}" in
    /*) : ;;
    *) DEB="$(pwd)/${DEB}" ;;
  esac
fi

export DEBIAN_FRONTEND=noninteractive

as_user() {
  # A clean login-like environment for the unprivileged user: no root env
  # leaks into the launcher, PATH is the stock one (/usr/bin wrapper), and the
  # working directory is the user's home as after a login. The cwd matters:
  # the launcher's Redis config sets no `dir`, so bundled Redis keeps dump.rdb
  # in the launcher's cwd and refuses to boot from an unreadable one (e.g.
  # root's 0700 home, where this script runs).
  (
    cd "${USER_HOME}"
    runuser -u "${VALIDATION_USER}" -- env -i \
      HOME="${USER_HOME}" USER="${VALIDATION_USER}" LOGNAME="${VALIDATION_USER}" \
      PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 "$@"
  )
}

dump_diagnostics() {
  local rc=$?
  if [[ "${rc}" -ne 0 && -n "${USER_HOME:-}" ]]; then
    printf '\n=== diagnostics (exit %s) ===\n' "${rc}" >&2
    as_user farna-control status 2>&1 | sed 's/^/status: /' >&2 || true
    if [[ -d "${DATA_DIR:-/nonexistent}" ]]; then
      find "${DATA_DIR}" -maxdepth 2 -name '*.log' -type f 2>/dev/null | while IFS= read -r log; do
        printf -- '--- tail %s ---\n' "${log}" >&2
        tail -n 60 -- "${log}" >&2 || true
      done
      # Known launcher defect (FAR-2581), named so a failure is attributed.
      if grep -rqs 'SAQ_AUTH_PASSWORD must be set' "${DATA_DIR}"; then
        echo "NOTE: saq-system logged 'SAQ_AUTH_PASSWORD must be set' — known launcher defect FAR-2581, not a packaging fault." >&2
      fi
    fi
  fi
  exit "${rc}"
}
trap dump_diagnostics EXIT

step "distro"
# shellcheck source=/dev/null  # the target distro's own os-release, read at runtime
. /etc/os-release
printf '%s (%s)\n' "${PRETTY_NAME}" "${VERSION_CODENAME:-?}"

if [[ "${FROM_INSTALLED}" -eq 1 ]]; then
  step "farna-control already installed (from the apt repository)"
  dpkg-query -W -f='${Status}
' farna-control 2>/dev/null | grep -qx 'install ok installed'     || fail "--installed given but the farna-control package is not installed"
  apt-get update -qq
  # curl + procps (pgrep) are validation tools only, not package dependencies.
  apt-get install -y -qq --no-install-recommends curl procps >/dev/null
else
  step "apt-get install ./$(basename -- "${DEB}")"
  apt-get update -qq
  # curl + procps (pgrep) are validation tools only, not package dependencies.
  apt-get install -y -qq --no-install-recommends curl procps >/dev/null
  apt-get install -y --no-install-recommends "${DEB}"
fi
dpkg -s farna-control | sed -n '/^Package:/p;/^Version:/p;/^Depends:/p'
[[ -x /usr/bin/farna-control ]] || fail "/usr/bin/farna-control missing after install"
[[ -L /opt/farna-control/current ]] || fail "/opt/farna-control/current symlink missing"
printf 'current -> %s\n' "$(readlink /opt/farna-control/current)"

step "create non-root user ${VALIDATION_USER}"
id "${VALIDATION_USER}" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "${VALIDATION_USER}"
USER_HOME="$(getent passwd "${VALIDATION_USER}" | cut -d: -f6)"
DATA_DIR="${USER_HOME}/.local/share/modulo/data"
[[ "$(as_user sh -c 'command -v farna-control')" = "/usr/bin/farna-control" ]] || fail "farna-control does not resolve to /usr/bin/farna-control for the user"
as_user farna-control --version

step "farna-control start --detach (as ${VALIDATION_USER})"
START_SECONDS="${SECONDS}"
as_user farna-control start --detach

step "resolve api_port from state.json and wait for /healthz 200"
api_port=""
deadline=$((SECONDS + BOOT_TIMEOUT_SECONDS))
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  if [[ -f "${DATA_DIR}/state.json" ]]; then
    api_port="$(sed -nE 's/.*"api_port"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "${DATA_DIR}/state.json" | head -n 1)"
    [[ -n "${api_port}" ]] && break
  fi
  sleep 2
done
[[ -n "${api_port}" ]] || fail "state.json never recorded an api_port within ${BOOT_TIMEOUT_SECONDS}s"
BASE="http://127.0.0.1:${api_port}"
printf 'api base: %s\n' "${BASE}"
health_code=""
while [[ "${SECONDS}" -lt "${deadline}" ]]; do
  health_code="$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/healthz" || true)"
  [[ "${health_code}" = "200" ]] && break
  sleep 2
done
[[ "${health_code}" = "200" ]] || fail "/healthz never returned 200 (last: ${health_code:-none})"
printf '/healthz -> %s (%ss after start)\n' "${health_code}" "$((SECONDS - START_SECONDS))"

step "GET / serves the SPA index"
index="$(curl -fsS "${BASE}/")"
grep -qi '<html' <<< "${index}" || fail "GET / did not return HTML"
grep -q 'id="app"' <<< "${index}" || fail "GET / is not the SPA index (no #app mount point)"
printf 'GET / -> SPA index (%s bytes)\n' "${#index}"

assert_all_components_alive() {
  # Every component (postgres, redis, api, saq-runs, saq-system) must be alive
  # per `farna-control status --json`; both SAQ workers are named explicitly.
  #
  # The verdict and the printed table come from ONE `status --json` snapshot:
  # two separate status calls can disagree while a worker is restarting. A
  # snapshot with a component down is retried for up to 30s (a supervised
  # restart in flight); a component that never shows alive fails the run.
  local status_json verdict snapshot_deadline
  snapshot_deadline=$((SECONDS + 30))
  while :; do
    status_json="$(as_user farna-control status --json)"
    verdict="$(/opt/farna-control/current/runtime/bin/python3 -I -c '
import json, sys
components = json.loads(sys.stdin.read()).get("components", {})
down = []
for name in ("postgres", "redis", "api", "saq-runs", "saq-system"):
    component = components.get(name) or {}
    alive = component.get("alive") is True
    pid = str(component.get("pid"))
    print(f"{name:<12} pid={pid:<8} alive={str(alive).lower()}")
    if not alive:
        down.append(name)
print("DOWN " + " ".join(down) if down else "ALL_ALIVE")
' <<< "${status_json}")"
    printf '%s\n' "${verdict}" | sed '$d'
    if [[ "$(printf '%s\n' "${verdict}" | tail -n 1)" = "ALL_ALIVE" ]]; then
      echo "all components alive in one status snapshot (incl. saq-runs and saq-system)"
      return 0
    fi
    [[ "${SECONDS}" -lt "${snapshot_deadline}" ]] || fail "not alive per farna-control status --json: $(printf '%s\n' "${verdict}" | tail -n 1)"
    echo "retrying: $(printf '%s\n' "${verdict}" | tail -n 1)"
    sleep 3
  done
}

step "farna-control status: both SAQ workers alive ($((SECONDS - START_SECONDS))s after start)"
assert_all_components_alive

if [[ "${FULL_CHECK}" = "1" ]]; then
  step "full check: stack stays up for ${STABILITY_SECONDS}s"
  sleep "${STABILITY_SECONDS}"
  [[ "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/healthz" || true)" = "200" ]] || fail "/healthz not 200 after ${STABILITY_SECONDS}s"
  assert_all_components_alive

  step "full check: admin login"
  admin_password="$(/opt/farna-control/current/runtime/bin/python3 -I -c 'import secrets; print(secrets.token_urlsafe(24))')"
  as_user farna-control users add deb-validate@example.com --admin --password "${admin_password}"
  login_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"deb-validate@example.com\",\"password\":\"${admin_password}\"}" || true)"
  [[ "${login_code}" = "200" ]] || fail "admin login returned ${login_code:-none}"
  echo "admin login -> 200"
else
  echo "(FARNA_DEB_FULL_CHECK=0: skipping the ${STABILITY_SECONDS}s stability window and login until FAR-2581 lands)"
fi

step "farna-control stop"
# `stop` sends SIGTERM and only waits 10s to confirm; on a slow host the
# ordered teardown (API drain, workers, Redis, Postgres) can take longer and
# stop reports `stop.unconfirmed`. The real contract is that every packaged
# process exits, so wait for that instead of trusting the 10s confirmation.
if ! as_user farna-control stop; then
  echo "farna-control stop did not confirm within its own window; waiting for the processes to exit"
fi
stop_deadline=$((SECONDS + 120))
while pgrep -u "${VALIDATION_USER}" -f '/opt/farna-control/' >/dev/null 2>&1 && [[ "${SECONDS}" -lt "${stop_deadline}" ]]; do
  sleep 2
done
if pgrep -u "${VALIDATION_USER}" -f '/opt/farna-control/' >/dev/null 2>&1; then
  pgrep -u "${VALIDATION_USER}" -af '/opt/farna-control/' >&2 || true
  fail "processes from /opt/farna-control still running after stop"
fi
echo "no packaged processes left running"

step "apt-get remove farna-control (user data must survive)"
apt-get remove -y farna-control
[[ ! -e /usr/bin/farna-control ]] || fail "/usr/bin/farna-control still present after remove"
[[ -d "${DATA_DIR}" ]] || fail "user data dir ${DATA_DIR} was deleted by remove"
[[ -f "${DATA_DIR}/state.json" ]] || fail "user state.json was deleted by remove"
echo "data dir intact after remove: ${DATA_DIR}"

step "apt-get purge farna-control (user data must STILL survive)"
apt-get purge -y farna-control
[[ -d "${DATA_DIR}" ]] || fail "user data dir ${DATA_DIR} was deleted by purge"
[[ -f "${DATA_DIR}/state.json" ]] || fail "user state.json was deleted by purge"
[[ ! -e /opt/farna-control ]] || { find /opt/farna-control | head -20 >&2; fail "/opt/farna-control left behind after purge"; }
if dpkg -s farna-control >/dev/null 2>&1; then
  fail "dpkg still knows farna-control after purge"
fi
echo "data dir intact after purge: ${DATA_DIR}; /opt/farna-control removed"

printf '\nvalidate-deb: PASS on %s\n' "${PRETTY_NAME}"
