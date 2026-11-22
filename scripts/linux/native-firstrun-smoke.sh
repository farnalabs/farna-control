#!/usr/bin/env bash
# native-firstrun-smoke.sh — FAR-2581 regression gate for the native bundle.
#
# Usage (as a NON-ROOT user, on a clean Linux host/container):
#   scripts/linux/native-firstrun-smoke.sh <farna-control-X.Y.Z-linux-ARCH.tar.gz>
#
# RELEASE_MANIFEST.json and RELEASE_MANIFEST.json.sig must sit beside the
# tarball: the install goes through the real `install.sh --from-file` path,
# signature verification included.
#
# What it proves, in order (each step fails the run loudly):
#   1. install.sh --from-file installs the bundle into a FRESH home, run from
#      a cwd the smoke user cannot search (FAR-2589: GNU find used to abort
#      with "Failed to restore initial working directory" there)
#   2. `farna-control start --detach` boots
#   3-7 run scripts/native-stack-assertions.sh (shared with the macOS smoke):
#   3. after a >= SMOKE_SETTLE_SECONDS (default 180) settle window, `status
#      --json` shows api, postgres, redis, saq-runs and saq-system alive and
#      the stack is NOT degraded (a crash-looping child trips the crash cap
#      and degrades the stack well inside that window)
#   4. GET /healthz/ready answers 200 and reports the bundle's version
#   5. `farna-control users add` creates an admin, and the admin's org is
#      not the orphan "Orphan (unattributed errors)" sentinel org
#   6. POST /api/v1/auth/login answers 200 with an access token whose org_id
#      claim is not the nil-UUID orphan org
#   7. an authenticated GET /api/v1/pipelines answers 200
#   8. `farna-control --version` names farna-control and the bundle's version
#   9. `farna-control stop` stops the stack
#
# Requirements: bash, python3, openssl (>= 3.0), curl, tar, gzip, sha256sum.
# Environment knobs:
#   SMOKE_WORKDIR          work dir (default: a fresh mktemp -d); the fresh
#                          HOME, install root and data dir all live under it
#   SMOKE_SETTLE_SECONDS   settle window before asserting (default 180, min 180)
#   SMOKE_KEEP_RUNNING=1   skip the final stop (debugging only)
#   SMOKE_START_CWD        run `start --detach` from this directory (e.g. one
#                          the smoke user can enter but neither read nor
#                          write) — proves the launcher never depends on its
#                          cwd (Redis used to write into it)

set -euo pipefail

MIN_SETTLE_SECONDS=180

log() { printf '[smoke %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() {
  printf '[smoke] FAIL: %s\n' "$*" >&2
  dump_diagnostics
  exit 1
}

dump_diagnostics() {
  [[ -n "${CLI:-}" && -x "${CLI:-/nonexistent}" ]] || return 0
  printf '[smoke] ---- diagnostics: farna-control status ----\n' >&2
  "${CLI}" status 2>&1 | sed 's/^/[smoke]   /' >&2 || true
  if [[ -n "${DATA_DIR:-}" && -d "${DATA_DIR}" ]]; then
    local f
    while IFS= read -r -d '' f; do
      printf '[smoke] ---- tail %s ----\n' "${f}" >&2
      tail -n 40 -- "${f}" 2>/dev/null | sed 's/^/[smoke]   /' >&2 || true
    done < <(find "${DATA_DIR}" -maxdepth 3 -type f -name '*.log' -print0 2>/dev/null)
  fi
}

[[ $# -eq 1 ]] || { printf 'usage: %s <bundle.tar.gz>\n' "$0" >&2; exit 2; }
[[ "$(id -u)" -ne 0 ]] || { printf 'refusing to run as root: the gate proves a NON-ROOT first run\n' >&2; exit 2; }

ASSERTIONS="$(cd "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/native-stack-assertions.sh"
[[ -f "${ASSERTIONS}" ]] || { printf 'missing %s (run from a checkout)
' "${ASSERTIONS}" >&2; exit 2; }
TARBALL="$(cd "$(dirname -- "$1")" && pwd)/$(basename -- "$1")"
[[ -f "${TARBALL}" ]] || { printf 'no such tarball: %s\n' "$1" >&2; exit 2; }
TARBALL_DIR="$(dirname -- "${TARBALL}")"
for required in RELEASE_MANIFEST.json RELEASE_MANIFEST.json.sig; do
  [[ -f "${TARBALL_DIR}/${required}" ]] || { printf 'missing %s beside the tarball\n' "${required}" >&2; exit 2; }
done
for tool in python3 openssl curl tar gzip sha256sum; do
  command -v "${tool}" >/dev/null 2>&1 || { printf 'required tool missing: %s\n' "${tool}" >&2; exit 2; }
done

SETTLE="${SMOKE_SETTLE_SECONDS:-${MIN_SETTLE_SECONDS}}"
[[ "${SETTLE}" =~ ^[0-9]+$ ]] || { printf 'SMOKE_SETTLE_SECONDS must be an integer\n' >&2; exit 2; }
(( SETTLE >= MIN_SETTLE_SECONDS )) || { printf 'SMOKE_SETTLE_SECONDS must be >= %s\n' "${MIN_SETTLE_SECONDS}" >&2; exit 2; }

WORKDIR="${SMOKE_WORKDIR:-$(mktemp -d)}"
mkdir -p -- "${WORKDIR}"
# Everything this script does itself runs from the work dir; only the
# installer (an inaccessible cwd, see step 1) and `start` (SMOKE_START_CWD when
# given) run elsewhere.
cd -- "${WORKDIR}"
# A FRESH home: install root, bin dir and data dir all derive from it, so no
# earlier install, config.env or data dir on this machine can leak in.
export HOME="${WORKDIR}/home"
mkdir -p -- "${HOME}"
unset XDG_DATA_HOME FARNA_CONTROL_INSTALL_ROOT FARNA_CONTROL_BIN_DIR FARNA_CONTROL_DATA_DIR \
  FARNA_CONTROL_INSTALL_ROOT FARNA_CONTROL_BIN_DIR FARNA_CONTROL_DATA_DIR DATABASE_URL REDIS_URL SECRET_KEY FERNET_KEY \
  SAQ_AUTH_USERNAME SAQ_AUTH_PASSWORD
CLI="${HOME}/.local/bin/farna-control"
DATA_DIR="${HOME}/.local/share/modulo/data"
STARTED=0

cleanup() {
  local rc=$?
  if [[ "${STARTED}" -eq 1 && "${SMOKE_KEEP_RUNNING:-0}" != "1" ]]; then
    "${CLI}" stop >/dev/null 2>&1 || true
  fi
  exit "${rc}"
}
trap cleanup EXIT

log "workdir=${WORKDIR} user=$(id -un) tarball=${TARBALL}"

# --- 1. install ---------------------------------------------------------------
log "step 1: install.sh --from-file"
install_dir="${WORKDIR}/installer"
mkdir -p -- "${install_dir}"
tar -xzf "${TARBALL}" -C "${install_dir}" --wildcards '*/install.sh'
installer="$(find "${install_dir}" -name install.sh -type f | head -n 1)"
[[ -n "${installer}" ]] || fail "the tarball does not ship install.sh"
# Run the installer from a cwd the user cannot search (mode 0000, entered
# before the chmod - the shape of `sudo -u` from another user's 0700 home):
# GNU find aborted there with "Failed to restore initial working directory"
# until the installer stopped depending on its cwd (FAR-2589). An enter-only
# (0311) cwd does not reproduce it.
install_cwd="${WORKDIR}/inaccessible-install-cwd"
mkdir -p -- "${install_cwd}"
log "  running install.sh from inaccessible (mode 0000) cwd ${install_cwd}"
install_rc=0
( cd -- "${install_cwd}" && chmod 0000 . && bash "${installer}" --from-file "${TARBALL}" ) || install_rc=$?
chmod 0755 -- "${install_cwd}"
[[ "${install_rc}" -eq 0 ]] || \
  fail "install.sh --from-file (cwd ${install_cwd}, mode 0000) exited ${install_rc}"
[[ -x "${CLI}" ]] || fail "install did not create the shim ${CLI}"

# --- 2. start -------------------------------------------------------------------
log "step 2: farna-control start --detach"
if [[ -n "${SMOKE_START_CWD:-}" ]]; then
  log "  starting from cwd ${SMOKE_START_CWD}"
  ( cd -- "${SMOKE_START_CWD}" && "${CLI}" start --detach ) || fail "start --detach (cwd ${SMOKE_START_CWD}) exited non-zero"
else
  "${CLI}" start --detach || fail "start --detach exited non-zero"
fi
STARTED=1

# --- 3-7. shared post-start stability assertions -------------------------------
# settle >= 180s, all 5 components alive + not degraded, /healthz/ready 200,
# users add (not the orphan org), login 200 (token org != orphan), an
# authenticated GET 200. Shared with the macOS installer smoke.
log "steps 3-7: scripts/native-stack-assertions.sh"
expected_version="$(basename -- "${TARBALL}" | sed -nE 's/^farna-control-([0-9]+\.[0-9]+\.[0-9]+)-linux-[a-z0-9]+\.tar\.gz$/\1/p')"
SMOKE_SETTLE_SECONDS="${SETTLE}" SMOKE_EXPECTED_VERSION="${expected_version}" \
  bash "${ASSERTIONS}" "${CLI}" "${WORKDIR}/assertions" || fail "post-start assertions failed"

# --- 8. version ------------------------------------------------------------------
log "step 8: farna-control --version"
version_out="$("${CLI}" --version)" || fail "--version exited non-zero"
log "  ${version_out}"
[[ "${version_out}" == "farna-control ${expected_version}" ]] || \
  fail "--version printed '${version_out}', expected 'farna-control ${expected_version}'"

# --- 9. stop ---------------------------------------------------------------------
if [[ "${SMOKE_KEEP_RUNNING:-0}" == "1" ]]; then
  log "step 9: skipped (SMOKE_KEEP_RUNNING=1)"
else
  log "step 9: farna-control stop"
  # `stop` itself polls (up to 120s) for the ordered teardown to release the
  # data-dir lock and exits 0 only once the launcher is gone (FAR-2581).
  "${CLI}" stop || fail "stop could not confirm the launcher stopped"
  log "  launcher stopped"
  STARTED=0
fi

log "PASS: native first run is healthy"
