# shellcheck shell=bash
#
# Shared helpers for the signed apt repository (FAR-2579, FAR-2590).
# Sourced by scripts/linux/plan-apt-repo.sh and scripts/linux/build-apt-repo.sh
# so both apply the SAME rules to a Packages stanza reused from the live index:
# if the planner keeps a stanza, the builder must accept it, and vice versa.

# Constants for the scripts that source this file (shellcheck sees no use here).
# shellcheck disable=SC2034
APT_PACKAGE="farna-control"
APT_SUITE="stable"
APT_COMPONENT="main"
APT_ARCH="amd64"
APT_POOL_DIR="pool/${APT_COMPONENT}/f/${APT_PACKAGE}"

# apt_deb_version <deb-file-name>: print X.Y.Z for farna-control_X.Y.Z_amd64.deb,
# fail for any other name.
apt_deb_version() {
  [[ "$1" =~ ^farna-control_([0-9]+\.[0-9]+\.[0-9]+)_amd64\.deb$ ]] || return 1
  printf '%s\n' "${BASH_REMATCH[1]}"
}

# apt_sums_entry <SHA256SUMS> <deb-file-name>: print the SHA256 a release's
# SHA256SUMS records for the .deb. Fails unless there is exactly one entry.
apt_sums_entry() {
  local sums="$1" deb="$2" entries
  [[ -f "${sums}" ]] || return 1
  entries="$(awk -v f="${deb}" '$2 == f && $1 ~ /^[0-9a-f]+$/ && length($1) == 64 { print $1 }' "${sums}")"
  [[ -n "${entries}" && "$(wc -l <<< "${entries}")" -eq 1 ]] || return 1
  printf '%s\n' "${entries}"
}

# apt_packages_stanza <Packages> <deb-file-name>: print the one stanza whose
# Filename is the .deb's pool path. Prints nothing when there is not exactly one.
apt_packages_stanza() {
  local packages="$1" deb="$2"
  awk -v want="Filename: ${APT_POOL_DIR}/${deb}" '
    BEGIN { RS = ""; FS = "\n" }
    { for (i = 1; i <= NF; i++) if ($i == want) { n++; hit = $0 } }
    END { if (n == 1) print hit }' "${packages}"
}

# apt_stanza_problem <stanza-file> <deb-file-name> <sha256>: validate a stanza
# reused from a previous index. Prints the reason and fails when the stanza is
# not a well-formed farna-control stanza for exactly that .deb, or when its
# SHA256 is not the expected one (the release's SHA256SUMS entry).
apt_stanza_problem() {
  local stanza="$1" deb="$2" want_sha="$3" version field want count value
  version="$(apt_deb_version "${deb}")" || { echo "${deb} is not named ${APT_PACKAGE}_<X.Y.Z>_${APT_ARCH}.deb"; return 1; }
  [[ -s "${stanza}" ]] || { echo "empty stanza"; return 1; }
  if grep -q '^[[:space:]]*$' "${stanza}"; then
    echo "stanza holds a blank line (more than one paragraph)"; return 1
  fi
  if grep -Evq '^([A-Za-z0-9][A-Za-z0-9-]*: .*| .+)$' "${stanza}"; then
    echo "stanza holds a line that is neither a field nor a continuation"; return 1
  fi
  for field in Package Version Architecture Filename Size SHA256; do
    case "${field}" in
      Package) want="${APT_PACKAGE}" ;;
      Version) want="${version}" ;;
      Architecture) want="${APT_ARCH}" ;;
      Filename) want="${APT_POOL_DIR}/${deb}" ;;
      SHA256) want="${want_sha}" ;;
      Size) want="" ;;
    esac
    count="$(grep -c "^${field}: " "${stanza}" || true)"
    [[ "${count}" -eq 1 ]] || { echo "stanza has ${count} ${field} fields"; return 1; }
    value="$(sed -n "s/^${field}: //p" "${stanza}")"
    if [[ "${field}" == Size ]]; then
      [[ "${value}" =~ ^[1-9][0-9]*$ ]] || { echo "stanza Size '${value}' is not a byte count"; return 1; }
    elif [[ "${value}" != "${want}" ]]; then
      echo "stanza ${field} is '${value}', expected '${want}'"; return 1
    fi
  done
  # The other digests apt-ftparchive writes must at least be well formed.
  for field in MD5sum:32 SHA1:40 SHA512:128; do
    count="$(grep -c "^${field%%:*}: " "${stanza}" || true)"
    [[ "${count}" -le 1 ]] || { echo "stanza has ${count} ${field%%:*} fields"; return 1; }
    [[ "${count}" -eq 0 ]] && continue
    value="$(sed -n "s/^${field%%:*}: //p" "${stanza}")"
    [[ "${value}" =~ ^[0-9a-f]+$ && "${#value}" -eq "${field##*:}" ]] || { echo "stanza ${field%%:*} is malformed"; return 1; }
  done
  return 0
}
