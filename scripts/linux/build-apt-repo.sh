#!/usr/bin/env bash
#
# Build the signed Farna Control apt repository site (FAR-2579 phase 2).
#
# Usage:
#   scripts/linux/build-apt-repo.sh <out-site-dir> <manifest>
#
# <manifest> lists every package version the repository publishes, one per
# line, as "<release-tag><TAB><path to farna-control_<version>_amd64.deb>".
# Blank lines and lines starting with '#' are ignored. Each .deb must be the
# EXACT bytes of the asset attached to that GitHub release: the Packages index
# records their size and hashes, and apt downloads the release asset itself.
#
# The output is a static site for Cloudflare Pages (https://apt.farna.dev):
#
#   dists/stable/Release, InRelease (clearsigned), Release.gpg (detached)
#   dists/stable/main/binary-amd64/Packages, Packages.gz
#   farna-control-archive-keyring.gpg   binary (dearmored) public keyring for
#                                       signed-by=/usr/share/keyrings/...
#   farna-control-archive-keyring.asc   the same key, armored
#   _redirects                          302 /pool/main/f/farna-control/<deb>
#                                       -> the GitHub release download URL
#   _headers                            short cache lifetime for the index
#   index.html                          install instructions
#
# The .deb files are NOT part of the site (Cloudflare Pages caps a file at
# 25 MiB): the Packages "Filename" is a pool path that the _redirects file
# sends to the release asset, and apt follows the redirect.
#
# Every listed version is kept in the index (newest first), so users can pin
# or downgrade with `apt install farna-control=<version>`.
#
# Environment:
#   APT_REPO_GPG_PRIVATE_KEY        (required) armored private signing key
#   APT_REPO_GPG_PASSPHRASE         (optional) its passphrase
#   APT_REPO_PUBLIC_KEY_FILE        (optional) armored public key the signing
#                                   key MUST match by fingerprint (CI passes the
#                                   reviewed deploy/apt/farna-control-archive-keyring.asc)
#   APT_REPO_PUBLIC_URL             (optional) public repo URL shown in
#                                   index.html (default https://apt.farna.dev)
#   APT_REPO_RELEASE_DOWNLOAD_BASE  (optional) release download base the
#                                   redirects point at (default
#                                   https://github.com/farnalabs/farna-control/releases/download)
#
# Needs apt-ftparchive (apt-utils), gpg, gpgv, dpkg-deb, gzip and coreutils:
# run it on Ubuntu or in an ubuntu container.

set -euo pipefail

PACKAGE="farna-control"
SUITE="stable"
COMPONENT="main"
ARCH="amd64"
POOL_DIR="pool/${COMPONENT}/f/${PACKAGE}"
KEYRING_NAME="${PACKAGE}-archive-keyring"
PUBLIC_URL="${APT_REPO_PUBLIC_URL:-https://apt.farna.dev}"
RELEASE_DOWNLOAD_BASE="${APT_REPO_RELEASE_DOWNLOAD_BASE:-https://github.com/farnalabs/farna-control/releases/download}"

die() {
  printf 'build-apt-repo: ERROR: %s\n' "$*" >&2
  exit 1
}

info() {
  printf 'build-apt-repo: %s\n' "$*"
}

usage() {
  printf 'Usage: %s <out-site-dir> <manifest: release-tag<TAB>deb-path per line>\n' "${0##*/}" >&2
}

[[ $# -eq 2 ]] || { usage; exit 2; }
SITE="$1"
MANIFEST="$2"

[[ -f "${MANIFEST}" ]] || die "manifest not found: ${MANIFEST}"
[[ -n "${APT_REPO_GPG_PRIVATE_KEY:-}" ]] || die "APT_REPO_GPG_PRIVATE_KEY is not set (armored private signing key)"
[[ "${PUBLIC_URL}" =~ ^https?://[A-Za-z0-9.:-]+$ ]] || die "APT_REPO_PUBLIC_URL must be a bare http(s) origin (got '${PUBLIC_URL}')"
[[ "${RELEASE_DOWNLOAD_BASE}" =~ ^https://[A-Za-z0-9./_-]+[A-Za-z0-9]$ ]] || \
  die "APT_REPO_RELEASE_DOWNLOAD_BASE must be an https URL without a trailing slash (got '${RELEASE_DOWNLOAD_BASE}')"
for tool in apt-ftparchive gpg gpgv gpgconf dpkg-deb sha256sum gzip awk sort; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool missing: ${tool} (apt-get install apt-utils gnupg)"
done

if [[ -e "${SITE}" ]]; then
  [[ -d "${SITE}" ]] || die "${SITE} exists and is not a directory"
  [[ -z "$(ls -A -- "${SITE}")" ]] || die "${SITE} is not empty; refusing to mix a fresh build with stale files"
fi
mkdir -p -- "${SITE}"
SITE="$(cd -- "${SITE}" && pwd)"

WORK="$(mktemp -d)"
GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
chmod 0700 "${GNUPGHOME}"
cleanup() {
  gpgconf --kill gpg-agent >/dev/null 2>&1 || true
  rm -rf -- "${WORK}" "${GNUPGHOME}"
}
trap cleanup EXIT

# --- read and check the manifest --------------------------------------------

mkdir -p -- "${WORK}/${POOL_DIR}"
declare -A seen=()
redirects=()
versions=()
line_no=0
while IFS= read -r line || [[ -n "${line}" ]]; do
  line_no=$((line_no + 1))
  line="${line%$'\r'}"
  [[ -z "${line}" || "${line}" == \#* ]] && continue
  [[ "${line}" == *$'\t'* ]] || die "manifest line ${line_no}: expected '<release-tag><TAB><deb-path>'"
  tag="${line%%$'\t'*}"
  deb="${line#*$'\t'}"
  [[ "${tag}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "manifest line ${line_no}: invalid release tag '${tag}'"
  [[ -f "${deb}" ]] || die "manifest line ${line_no}: no such file: ${deb}"
  file="$(basename -- "${deb}")"
  [[ "${file}" =~ ^${PACKAGE}_([0-9]+\.[0-9]+\.[0-9]+)_${ARCH}\.deb$ ]] || \
    die "manifest line ${line_no}: ${file} is not named ${PACKAGE}_<X.Y.Z>_${ARCH}.deb"
  version="${BASH_REMATCH[1]}"
  [[ -z "${seen[${file}]:-}" ]] || die "manifest line ${line_no}: ${file} is listed twice (${seen[${file}]} and ${tag})"
  seen["${file}"]="${tag}"
  [[ "$(dpkg-deb -f "${deb}" Package)" = "${PACKAGE}" ]] || die "${file}: control Package is not ${PACKAGE}"
  [[ "$(dpkg-deb -f "${deb}" Version)" = "${version}" ]] || die "${file}: control Version does not match the file name"
  [[ "$(dpkg-deb -f "${deb}" Architecture)" = "${ARCH}" ]] || die "${file}: control Architecture is not ${ARCH}"
  # A symlink, not a copy: release .debs are hundreds of MB each.
  ln -s -- "$(cd -- "$(dirname -- "${deb}")" && pwd)/${file}" "${WORK}/${POOL_DIR}/${file}"
  redirects+=("/${POOL_DIR}/${file} ${RELEASE_DOWNLOAD_BASE}/${tag}/${file} 302")
  versions+=("${version}"$'\t'"${tag}")
  info "package ${version} <- ${tag} (${file})"
done < "${MANIFEST}"
[[ "${#versions[@]}" -gt 0 ]] || die "the manifest lists no packages"

# --- Packages ---------------------------------------------------------------

BINARY_DIR="dists/${SUITE}/${COMPONENT}/binary-${ARCH}"
mkdir -p -- "${SITE}/${BINARY_DIR}"
info "generating ${BINARY_DIR}/Packages"
(cd -- "${WORK}" && apt-ftparchive --md5=yes --sha1=yes --sha256=yes --sha512=yes packages "${POOL_DIR}") > "${WORK}/Packages.raw"

# Order the stanzas newest version first (versions are X.Y.Z, so sort -V is
# exact). Stanzas are paragraphs; join each onto one line with \001 to sort.
awk 'BEGIN { RS = ""; FS = "\n" }
  {
    version = ""
    for (i = 1; i <= NF; i++) if ($i ~ /^Version: /) version = substr($i, 10)
    record = $0
    gsub(/\n/, "\001", record)
    printf "%s\t%s\n", version, record
  }' "${WORK}/Packages.raw" \
  | LC_ALL=C sort -t $'\t' -k1,1rV \
  | awk -F '\t' '{ gsub(/\001/, "\n", $2); printf "%s\n\n", $2 }' \
  | awk -v pool="${POOL_DIR}" '
      # apt-ftparchive resolves the pool symlinks and records the real path;
      # rewrite every Filename to its pool path (the _redirects source).
      /^Filename: / { n = split($2, parts, "/"); print "Filename: " pool "/" parts[n]; next }
      { print }' > "${SITE}/${BINARY_DIR}/Packages"

stanzas="$(grep -c '^Package: ' "${SITE}/${BINARY_DIR}/Packages" || true)"
[[ "${stanzas}" -eq "${#versions[@]}" ]] || die "Packages has ${stanzas} stanzas for ${#versions[@]} manifest entries"
# Self-check: every stanza must carry the size and SHA256 of the exact bytes.
for file in "${!seen[@]}"; do
  real="$(readlink -f -- "${WORK}/${POOL_DIR}/${file}")"
  want_sha="$(sha256sum -- "${real}" | awk '{print $1}')"
  want_size="$(stat -c %s -- "${real}")"
  stanza="$(awk -v f="Filename: ${POOL_DIR}/${file}" 'BEGIN { RS = "" } index($0, f "\n") { print }' "${SITE}/${BINARY_DIR}/Packages")"
  grep -qx "SHA256: ${want_sha}" <<< "${stanza}" || die "Packages SHA256 for ${file} does not match the file"
  grep -qx "Size: ${want_size}" <<< "${stanza}" || die "Packages Size for ${file} does not match the file"
done
gzip -9nc "${SITE}/${BINARY_DIR}/Packages" > "${SITE}/${BINARY_DIR}/Packages.gz"

# --- Release ----------------------------------------------------------------

info "generating dists/${SUITE}/Release"
apt-ftparchive \
  -o "APT::FTPArchive::Release::Origin=Farna Labs" \
  -o "APT::FTPArchive::Release::Label=Farna Control" \
  -o "APT::FTPArchive::Release::Suite=${SUITE}" \
  -o "APT::FTPArchive::Release::Codename=${SUITE}" \
  -o "APT::FTPArchive::Release::Architectures=${ARCH}" \
  -o "APT::FTPArchive::Release::Components=${COMPONENT}" \
  -o "APT::FTPArchive::Release::Description=Farna Control self-hosted agent governance platform" \
  release "${SITE}/dists/${SUITE}" > "${WORK}/Release"
mv -- "${WORK}/Release" "${SITE}/dists/${SUITE}/Release"

# --- signing ----------------------------------------------------------------

printf 'allow-loopback-pinentry\n' > "${GNUPGHOME}/gpg-agent.conf"
gpg_args=(--batch --yes --no-tty --pinentry-mode loopback)
if [[ -n "${APT_REPO_GPG_PASSPHRASE:-}" ]]; then
  printf '%s' "${APT_REPO_GPG_PASSPHRASE}" > "${GNUPGHOME}/passphrase"
  chmod 0600 "${GNUPGHOME}/passphrase"
  gpg_args+=(--passphrase-file "${GNUPGHOME}/passphrase")
else
  gpg_args+=(--passphrase "")
fi

printf '%s\n' "${APT_REPO_GPG_PRIVATE_KEY}" | gpg "${gpg_args[@]}" --import 2>"${WORK}/gpg-import.log" || {
  cat -- "${WORK}/gpg-import.log" >&2
  die "could not import APT_REPO_GPG_PRIVATE_KEY"
}
mapfile -t secret_fprs < <(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "sec" { want = 1; next } want && $1 == "fpr" { print $10; want = 0 }')
[[ "${#secret_fprs[@]}" -eq 1 ]] || die "APT_REPO_GPG_PRIVATE_KEY must hold exactly one secret primary key (found ${#secret_fprs[@]})"
FPR="${secret_fprs[0]}"
info "signing key ${FPR}"

if [[ -n "${APT_REPO_PUBLIC_KEY_FILE:-}" ]]; then
  [[ -f "${APT_REPO_PUBLIC_KEY_FILE}" ]] || die "APT_REPO_PUBLIC_KEY_FILE not found: ${APT_REPO_PUBLIC_KEY_FILE}"
  mapfile -t expected_fprs < <(gpg --batch --with-colons --show-keys -- "${APT_REPO_PUBLIC_KEY_FILE}" | awk -F: '$1 == "pub" { want = 1; next } want && $1 == "fpr" { print $10; want = 0 }')
  [[ "${#expected_fprs[@]}" -eq 1 ]] || die "${APT_REPO_PUBLIC_KEY_FILE} must hold exactly one public primary key"
  [[ "${expected_fprs[0]}" = "${FPR}" ]] || \
    die "signing key ${FPR} does not match the reviewed public key ${expected_fprs[0]} (${APT_REPO_PUBLIC_KEY_FILE})"
  info "signing key matches ${APT_REPO_PUBLIC_KEY_FILE}"
fi

RELEASE="${SITE}/dists/${SUITE}/Release"
gpg "${gpg_args[@]}" --local-user "${FPR}!" --digest-algo SHA512 --clearsign \
  --output "${SITE}/dists/${SUITE}/InRelease" "${RELEASE}"
gpg "${gpg_args[@]}" --local-user "${FPR}!" --digest-algo SHA512 --armor --detach-sign \
  --output "${SITE}/dists/${SUITE}/Release.gpg" "${RELEASE}"

# The published keyring: the signing key's PUBLIC part only.
gpg --batch --export "${FPR}" > "${SITE}/${KEYRING_NAME}.gpg"
gpg --batch --armor --export "${FPR}" > "${SITE}/${KEYRING_NAME}.asc"
[[ -s "${SITE}/${KEYRING_NAME}.gpg" ]] || die "exporting the public keyring failed"
if grep -q 'PRIVATE KEY' "${SITE}/${KEYRING_NAME}.asc"; then
  die "refusing to publish: the exported keyring contains private key material"
fi

# Prove both signatures verify against the keyring exactly as apt will use it.
gpgv --keyring "${SITE}/${KEYRING_NAME}.gpg" "${SITE}/dists/${SUITE}/InRelease" 2>"${WORK}/gpgv.log" || {
  cat -- "${WORK}/gpgv.log" >&2
  die "InRelease does not verify against the published keyring"
}
gpgv --keyring "${SITE}/${KEYRING_NAME}.gpg" "${SITE}/dists/${SUITE}/Release.gpg" "${RELEASE}" 2>"${WORK}/gpgv.log" || {
  cat -- "${WORK}/gpgv.log" >&2
  die "Release.gpg does not verify against the published keyring"
}
info "InRelease and Release.gpg verify against ${KEYRING_NAME}.gpg"

# --- Cloudflare Pages: redirects + headers ----------------------------------

{
  printf '# Generated by scripts/linux/build-apt-repo.sh (FAR-2579).\n'
  printf '# The .deb files live on the GitHub releases; apt follows these 302s.\n'
  printf '%s\n' "${redirects[@]}"
} > "${SITE}/_redirects"

cat > "${SITE}/_headers" <<'EOF'
# Generated by scripts/linux/build-apt-repo.sh (FAR-2579).
# The index changes on every release: keep caches short so Release, InRelease
# and Packages never disagree for long.
/dists/*
  Cache-Control: public, max-age=60, must-revalidate
/farna-control-archive-keyring.*
  Cache-Control: public, max-age=300
EOF

# --- index.html -------------------------------------------------------------

newest="$(printf '%s\n' "${versions[@]}" | LC_ALL=C sort -t $'\t' -k1,1rV | head -n 1 | cut -f1)"
version_rows="$(printf '%s\n' "${versions[@]}" | LC_ALL=C sort -t $'\t' -k1,1rV \
  | awk -F '\t' '{ printf "        <li><code>%s</code> (release <code>%s</code>)</li>\n", $1, $2 }')"
cat > "${SITE}/index.html" <<EOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Farna Control apt repository</title>
  <style>
    :root { color-scheme: light dark; --bg: #ffffff; --fg: #1d1d1f; --muted: #5f6368; --code: #f3f4f6; }
    @media (prefers-color-scheme: dark) { :root { --bg: #111214; --fg: #e8e8ea; --muted: #a0a4ab; --code: #1e2024; } }
    body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.55 system-ui, sans-serif; }
    main { max-width: 46rem; margin: 0 auto; padding: 2rem 1rem; }
    pre { background: var(--code); padding: 0.9rem 1rem; overflow-x: auto; border-radius: 6px; }
    code { font: 0.9em ui-monospace, SFMono-Regular, Menlo, monospace; }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <main>
    <h1>Farna Control apt repository</h1>
    <p>Signed apt repository for the self-hosted Farna Control native package on
    Ubuntu 22.04 and 24.04 (amd64). Newest version: <code>${newest}</code>.</p>
    <h2>Install</h2>
    <pre><code>sudo apt-get install -y ca-certificates curl
curl -fsSL ${PUBLIC_URL}/${KEYRING_NAME}.gpg \\
  | sudo tee /usr/share/keyrings/${KEYRING_NAME}.gpg &gt;/dev/null
echo "deb [signed-by=/usr/share/keyrings/${KEYRING_NAME}.gpg] ${PUBLIC_URL} ${SUITE} ${COMPONENT}" \\
  | sudo tee /etc/apt/sources.list.d/${PACKAGE}.list
sudo apt update
sudo apt install ${PACKAGE}</code></pre>
    <p>Then, as your normal (non-root) user: <code>farna-control start --detach</code>.</p>
    <h2>Upgrade and pin</h2>
    <pre><code>sudo apt update &amp;&amp; sudo apt upgrade              # newest release
apt list -a ${PACKAGE}                              # every published version
sudo apt install ${PACKAGE}=${newest}                 # a specific version
sudo apt-mark hold ${PACKAGE}                        # stay on it</code></pre>
    <p>Signing key fingerprint: <code>${FPR}</code>
    (<a href="${KEYRING_NAME}.asc">armored</a>).</p>
    <h2>Published versions</h2>
    <ul>
${version_rows}
    </ul>
    <p class="muted">The packages are the release assets of
    <a href="https://github.com/farnalabs/farna-control/releases">github.com/farnalabs/farna-control</a>;
    this site serves only the signed index.</p>
  </main>
</body>
</html>
EOF

info "site written to ${SITE} (${#versions[@]} version(s), newest ${newest})"
find "${SITE}" -type f | sed "s|^${SITE}/|  |" | LC_ALL=C sort
