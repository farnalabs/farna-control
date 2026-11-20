#!/usr/bin/env bash
#
# Decide which released .debs the apt repository refresh must download
# (FAR-2590). Pure local logic: no network access.
#
# Usage:
#   scripts/linux/plan-apt-repo.sh <candidates.tsv> <previous-dir> <out-dir>
#
# <candidates.tsv>  every version the new index must list, one per line, as
#                   "<release-tag><TAB><deb-file-name><TAB><path to that
#                   release's SHA256SUMS>". The SHA256SUMS files are small and
#                   always downloaded fresh; the .debs are ~224 MB each.
# <previous-dir>    the index currently published, as fetched from the live
#                   site: "InRelease" and "Packages" (from
#                   dists/stable/main/binary-amd64/). Missing or empty files
#                   mean there is no previous index (first run): every
#                   candidate is downloaded.
# <out-dir>         must not exist or be empty. Receives:
#                     reuse.tsv     "<tag><TAB><stanza-file><TAB><SHA256SUMS>"
#                                   -> the reuse manifest of build-apt-repo.sh
#                     download.tsv  "<tag><TAB><deb-file-name>": fetch these
#                     stanzas/<deb>.stanza
#
# The live site is never trusted blindly:
#   1. InRelease must verify (gpgv) against APT_REPO_PUBLIC_KEY_FILE, the
#      reviewed public key, and the SHA256 and size it signs for
#      main/binary-amd64/Packages must match the fetched Packages. Otherwise
#      the whole previous index is ignored (full rebuild, with a warning).
#   2. A version's stanza is reused only if it is well formed for exactly
#      that .deb AND its SHA256 equals the release's SHA256SUMS entry for the
#      .deb. Otherwise that one version is downloaded and recomputed.
# Versions whose release no longer exists simply drop out: only candidates
# are considered.
#
# Environment:
#   APT_REPO_PUBLIC_KEY_FILE  armored public key the previous InRelease must be
#                             signed by (required whenever a previous index is
#                             present).

set -euo pipefail

# shellcheck source=scripts/linux/apt-repo-lib.sh
source "$(dirname -- "${BASH_SOURCE[0]}")/apt-repo-lib.sh"

die() {
  printf 'plan-apt-repo: ERROR: %s\n' "$*" >&2
  exit 1
}

info() {
  printf 'plan-apt-repo: %s\n' "$*"
}

warn() {
  printf 'plan-apt-repo: WARNING: %s\n' "$*" >&2
}

[[ $# -eq 3 ]] || { printf 'Usage: %s <candidates.tsv> <previous-dir> <out-dir>\n' "${0##*/}" >&2; exit 2; }
CANDIDATES="$1"
PREVIOUS="$2"
OUT="$3"

[[ -f "${CANDIDATES}" ]] || die "candidates not found: ${CANDIDATES}"
for tool in gpg gpgv sha256sum awk stat; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool missing: ${tool}"
done
if [[ -e "${OUT}" ]]; then
  [[ -d "${OUT}" && -z "$(ls -A -- "${OUT}")" ]] || die "${OUT} must be an empty directory"
fi
mkdir -p -- "${OUT}/stanzas"
: > "${OUT}/reuse.tsv"
: > "${OUT}/download.tsv"

WORK="$(mktemp -d)"
cleanup() {
  GNUPGHOME="${WORK}/gnupg" gpgconf --kill gpg-agent >/dev/null 2>&1 || true
  rm -rf -- "${WORK}"
}
trap cleanup EXIT

# --- trust the previous index, or ignore it ----------------------------------

# previous_problem: print why the fetched previous index cannot be trusted;
# succeed silently when it can.
previous_problem() {
  local key="${APT_REPO_PUBLIC_KEY_FILE:-}" signed want got_sha got_size
  [[ -n "${key}" && -f "${key}" ]] || { echo "APT_REPO_PUBLIC_KEY_FILE is not set to the reviewed public key"; return 1; }
  mkdir -m 0700 -- "${WORK}/gnupg"
  GNUPGHOME="${WORK}/gnupg" gpg --batch --quiet --dearmor --output "${WORK}/keyring.gpg" "${key}" 2>/dev/null \
    || { echo "cannot read ${key}"; return 1; }
  signed="${WORK}/Release.verified"
  # --output keeps only the bytes the signature covers.
  gpgv --keyring "${WORK}/keyring.gpg" --output "${signed}" "${PREVIOUS}/InRelease" 2>"${WORK}/gpgv.log" \
    || { echo "InRelease does not verify against the reviewed key: $(tr '\n' ' ' < "${WORK}/gpgv.log")"; return 1; }
  grep -qx "Suite: ${APT_SUITE}" "${signed}" || { echo "InRelease is not for suite ${APT_SUITE}"; return 1; }
  want="$(awk -v p="${APT_COMPONENT}/binary-${APT_ARCH}/Packages" '
    /^[^ ]/ { section = $0; next }
    section == "SHA256:" && $3 == p { print $1 " " $2 }' "${signed}")"
  [[ -n "${want}" && "$(wc -l <<< "${want}")" -eq 1 ]] || { echo "InRelease signs no single SHA256 for Packages"; return 1; }
  got_sha="$(sha256sum -- "${PREVIOUS}/Packages" | awk '{print $1}')"
  got_size="$(stat -c %s -- "${PREVIOUS}/Packages")"
  [[ "${want}" == "${got_sha} ${got_size}" ]] \
    || { echo "Packages (${got_sha} ${got_size}) is not the one InRelease signs (${want})"; return 1; }
}

PACKAGES=""
if [[ -s "${PREVIOUS}/InRelease" && -s "${PREVIOUS}/Packages" ]]; then
  if reason="$(previous_problem)"; then
    PACKAGES="${PREVIOUS}/Packages"
    info "previous index verified: $(grep -c '^Package: ' "${PACKAGES}" || true) stanza(s) available for reuse"
  else
    warn "ignoring the previous index (full rebuild): ${reason}"
  fi
else
  info "no previous index (first run or site not reachable): every version is downloaded"
fi

# --- per version: reuse or download ------------------------------------------

declare -A seen=()
reused=0
downloads=0
line_no=0
while IFS= read -r line || [[ -n "${line}" ]]; do
  line_no=$((line_no + 1))
  line="${line%$'\r'}"
  [[ -z "${line}" || "${line}" == \#* ]] && continue
  IFS=$'\t' read -r tag deb sums extra <<< "${line}"
  [[ -n "${sums:-}" && -z "${extra:-}" ]] || die "candidates line ${line_no}: expected '<tag><TAB><deb><TAB><SHA256SUMS>'"
  [[ "${tag}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "candidates line ${line_no}: invalid release tag '${tag}'"
  apt_deb_version "${deb}" >/dev/null || die "candidates line ${line_no}: ${deb} is not named ${APT_PACKAGE}_<X.Y.Z>_${APT_ARCH}.deb"
  [[ -z "${seen[${deb}]:-}" ]] || die "candidates line ${line_no}: ${deb} is listed twice"
  seen["${deb}"]=1
  want_sha="$(apt_sums_entry "${sums}" "${deb}")" || die "${tag}: SHA256SUMS (${sums}) has no single entry for ${deb}"

  why="not in the previous index"
  if [[ -n "${PACKAGES}" ]]; then
    stanza_file="${OUT}/stanzas/${deb}.stanza"
    apt_packages_stanza "${PACKAGES}" "${deb}" > "${stanza_file}"
    if [[ ! -s "${stanza_file}" ]]; then
      rm -f -- "${stanza_file}"
    elif why="$(apt_stanza_problem "${stanza_file}" "${deb}" "${want_sha}")"; then
      printf '%s\t%s\t%s\n' "${tag}" "$(cd -- "${OUT}/stanzas" && pwd)/${deb}.stanza" "$(cd -- "$(dirname -- "${sums}")" && pwd)/$(basename -- "${sums}")" >> "${OUT}/reuse.tsv"
      reused=$((reused + 1))
      info "reuse    ${deb} (${tag}): indexed SHA256 matches the release SHA256SUMS"
      continue
    else
      rm -f -- "${stanza_file}"
      why="indexed stanza discarded: ${why}"
    fi
  fi
  printf '%s\t%s\n' "${tag}" "${deb}" >> "${OUT}/download.tsv"
  downloads=$((downloads + 1))
  info "download ${deb} (${tag}): ${why}"
done < "${CANDIDATES}"

info "plan: reuse ${reused}, download ${downloads}"
