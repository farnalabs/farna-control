#!/usr/bin/env bash
#
# Build the Farna Control .deb from a linux-amd64 native bundle (FAR-2579).
#
# Usage:
#   scripts/linux/build-deb.sh <farna-control-<version>-linux-amd64.tar.gz> <version> <outdir>
#
# Produces <outdir>/farna-control_<version>_amd64.deb and its .sha256.
# Needs only dpkg-deb, readelf (binutils), ldconfig and dpkg -S: run it on an
# Ubuntu 22.04 host or container (the bundle's glibc build baseline) so the
# computed Depends resolve against the oldest supported release.
#
# Package layout (mirrors scripts/install.sh, rooted at /opt instead of ~):
#   /opt/farna-control/versions/<version>/   the verified bundle, unmodified
#                                            apart from precompiled bytecode
#   /opt/farna-control/current               symlink -> versions/<version>
#   /usr/bin/farna-control                   wrapper -> current/launcher
#
# The versioned tree sits under versions/ (as in install.sh) rather than
# directly under /opt/farna-control/ because the bundle launcher derives
# FARNA_CONTROL_INSTALL_ROOT as "<launcher dir>/../..": with this layout a
# direct `/opt/farna-control/current/launcher` call resolves the same root as
# the wrapper, never /opt.
#
# The package installs NO daemon and runs nothing as root. Every user runs the
# CLI for themselves exactly as with the tarball install; the per-user data dir
# (~/.local/share/modulo/data) and its runtime keys are created by the launcher
# on that user's first `farna-control start`. The maintainer scripts never
# touch a user data dir: remove and purge leave every user's data intact.

set -euo pipefail

PACKAGE="farna-control"
INSTALL_PREFIX="/opt/farna-control"
WRAPPER_PATH="/usr/bin/farna-control"
MAINTAINER="Farna Labs <admin@farnalabs.com>"
HOMEPAGE="https://github.com/farnalabs/farna-control"
# Packages every build depends on regardless of the ELF scan below:
#   ca-certificates — outbound TLS (model backends, connectors, webhooks)
#                     trusts the system CA store.
#   openssl         — the same operator tooling contract as scripts/install.sh
#                     (manual key rotation: `openssl rand`; release-manifest
#                     verification for the tarball path).
BASE_DEPENDS=("ca-certificates" "openssl")

die() {
  printf 'build-deb: ERROR: %s\n' "$*" >&2
  exit 1
}

info() {
  printf 'build-deb: %s\n' "$*"
}

usage() {
  printf 'Usage: %s <farna-control-<version>-linux-amd64.tar.gz> <version> <outdir>\n' "${0##*/}" >&2
}

[[ $# -eq 3 ]] || { usage; exit 2; }
BUNDLE_TARBALL="$1"
VERSION="$2"
OUTDIR="$3"

[[ -f "${BUNDLE_TARBALL}" ]] || die "bundle tarball not found: ${BUNDLE_TARBALL}"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version must be X.Y.Z (got '${VERSION}')"
for tool in dpkg-deb dpkg readelf ldconfig sha256sum tar gzip find sort; do
  command -v "${tool}" >/dev/null 2>&1 || die "required tool missing: ${tool} (run this on Ubuntu with dpkg + binutils)"
done

BUNDLE_NAME="farna-control-${VERSION}-linux-amd64"
EXPECTED_TARBALL="${BUNDLE_NAME}.tar.gz"
[[ "$(basename -- "${BUNDLE_TARBALL}")" = "${EXPECTED_TARBALL}" ]] || \
  die "bundle file name must be ${EXPECTED_TARBALL} (got $(basename -- "${BUNDLE_TARBALL}"))"

mkdir -p -- "${OUTDIR}"
OUTDIR="$(cd -- "${OUTDIR}" && pwd)"
WORK="$(mktemp -d)"
cleanup() {
  rm -rf -- "${WORK}"
}
trap cleanup EXIT

# --- extract + verify the bundle --------------------------------------------

info "extracting ${BUNDLE_TARBALL}"
tar -xzf "${BUNDLE_TARBALL}" -C "${WORK}"
entries=("${WORK}"/*)
[[ "${#entries[@]}" -eq 1 && -d "${entries[0]}" && ! -L "${entries[0]}" ]] || \
  die "unexpected archive layout: expected exactly one top-level directory ${BUNDLE_NAME}"
[[ "$(basename -- "${entries[0]}")" = "${BUNDLE_NAME}" ]] || \
  die "archive top-level directory $(basename -- "${entries[0]}") does not match ${BUNDLE_NAME}"
BUNDLE="${entries[0]}"

[[ "$(cat -- "${BUNDLE}/VERSION")" = "bundle-v${VERSION}" ]] || \
  die "bundle VERSION '$(cat -- "${BUNDLE}/VERSION")' does not match bundle-v${VERSION}"
for required in launcher SHA256SUMS runtime/bin/python3 pg/bin/postgres redis/redis-server backend/src frontend-dist/index.html; do
  [[ -e "${BUNDLE}/${required}" ]] || die "malformed bundle: ${required} is missing"
done
info "verifying the bundle's internal SHA256SUMS"
(cd -- "${BUNDLE}" && sha256sum --check --strict --quiet SHA256SUMS) || die "bundle internal SHA256SUMS verification FAILED"

# --- stage the package tree -------------------------------------------------

PKGROOT="${WORK}/pkgroot"
VERSIONED="${PKGROOT}${INSTALL_PREFIX}/versions/${VERSION}"
mkdir -p -- "${PKGROOT}${INSTALL_PREFIX}/versions" "${PKGROOT}/usr/bin" "${PKGROOT}/DEBIAN" \
  "${PKGROOT}/usr/share/doc/${PACKAGE}" "${PKGROOT}/usr/share/man/man1" "${PKGROOT}/usr/share/lintian/overrides"
mv -- "${BUNDLE}" "${VERSIONED}"
ln -s "versions/${VERSION}" "${PKGROOT}${INSTALL_PREFIX}/current"

# Precompile bytecode at build time. Every user runs from a root-owned,
# read-only tree, so Python can never cache bytecode there at runtime: without
# this, every process start recompiles the whole app in memory. checked-hash
# pycs stay valid regardless of file mtimes and are owned by the package, so
# dpkg removes them with everything else.
info "precompiling Python bytecode"
"${VERSIONED}/runtime/bin/python3" -W ignore -m compileall -q -j 0 --invalidation-mode checked-hash \
  "${VERSIONED}/backend/src" "${VERSIONED}/backend/.venv/lib/python3.12/site-packages" >/dev/null || \
  die "bytecode precompilation failed"

cat > "${PKGROOT}${WRAPPER_PATH}" <<'WRAPPER'
#!/bin/sh
# Farna Control CLI wrapper, installed by the farna-control .deb (FAR-2579).
# Runs the packaged bundle for the CALLING user; nothing here needs root.
set -eu

INSTALL_ROOT=/opt/farna-control

# Always state the package's own install root. A user who also has the
# per-user tarball install may have sourced its env.sh, which exports
# FARNA_CONTROL_INSTALL_ROOT=~/.local/opt/farna-control; the bundle launcher
# keeps an inherited value, so without this the packaged CLI would act on the
# tarball install's tree.
FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"
export FARNA_CONTROL_INSTALL_ROOT
# The packaged tree is read-only and ships precompiled bytecode; never try to
# write caches into it (a root-run CLI would otherwise leave files dpkg does
# not own behind in /opt).
PYTHONDONTWRITEBYTECODE=1
export PYTHONDONTWRITEBYTECODE

# The package is upgraded by apt, never by the in-app upgrader: it would try to
# rewrite the root-owned /opt tree behind dpkg's back.
for arg in "$@"; do
  case "${arg}" in
    -*) continue ;;
    upgrade)
      echo "farna-control: this install is managed by apt (the farna-control .deb)." >&2
      echo "Upgrade it with the package manager instead, e.g.:" >&2
      echo "  sudo apt install ./farna-control_<version>_amd64.deb" >&2
      exit 2
      ;;
    *) break ;;
  esac
done

# Coexistence with the per-user tarball install (scripts/install.sh). Both use
# the SAME per-user data dir; the data-dir lock stops them running at once.
# Say so on `start` so a mixed setup is never silent.
if [ "${1:-}" = "start" ] && [ -n "${HOME:-}" ] && [ -e "${HOME}/.local/opt/farna-control/current/launcher" ]; then
  echo "farna-control: note: a per-user tarball install also exists at ${HOME}/.local/opt/farna-control; both use the same data dir. Running the apt-installed version (${INSTALL_ROOT}/current)." >&2
fi

exec "${INSTALL_ROOT}/current/launcher" "$@"
WRAPPER
chmod 0755 "${PKGROOT}${WRAPPER_PATH}"

# --- runtime dependencies (computed from the shipped ELF files) ------------
#
# Every NEEDED soname that the bundle does not ship itself must come from the
# system. Resolve each through ldconfig to the owning Debian package, fail on
# anything unresolvable, and pin libc6 to the highest GLIBC_ symbol version any
# shipped file references.

list_elf_files() {
  # list_elf_files <dir> — NUL-separated regular files starting with the ELF
  # magic. One bundled-Python pass instead of a process per file: the bundle
  # holds tens of thousands of source/data files and a few hundred ELFs.
  "${VERSIONED}/runtime/bin/python3" -I -c '
import os, sys
ELF_MAGIC = bytes([0x7F, 0x45, 0x4C, 0x46])
for root, _dirs, files in os.walk(sys.argv[1]):
    for name in files:
        path = os.path.join(root, name)
        if os.path.islink(path):
            continue
        try:
            with open(path, "rb") as handle:
                if handle.read(4) == ELF_MAGIC:
                    sys.stdout.write(path)
                    sys.stdout.write(chr(0))
        except OSError:
            pass
' "$1"
}

info "computing runtime dependencies from shipped ELF files"
declare -A bundled_sonames=()
declare -A needed_sonames=()
glibc_floor="2.17"
elf_count=0
while IFS= read -r -d '' file; do
  bundled_sonames["$(basename -- "${file}")"]=1
done < <(find "${VERSIONED}" -name '*.so*' \( -type f -o -type l \) -print0)
while IFS= read -r -d '' file; do
  elf_count=$((elf_count + 1))
  while IFS= read -r soname; do
    [[ -n "${soname}" ]] && needed_sonames["${soname}"]=1
  done < <(readelf -d -- "${file}" 2>/dev/null | sed -nE '/\(NEEDED\)/s/.*\[([^]]*)\].*/\1/p')
  while IFS= read -r glibc; do
    [[ -n "${glibc}" ]] || continue
    if [[ "$(printf '%s\n%s\n' "${glibc_floor}" "${glibc}" | sort -V | tail -n 1)" = "${glibc}" ]]; then
      glibc_floor="${glibc}"
    fi
  done < <(readelf -V -- "${file}" 2>/dev/null | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)+' | sed 's/^GLIBC_//' | sort -Vu)
done < <(list_elf_files "${VERSIONED}")
[[ "${elf_count}" -gt 0 ]] || die "no ELF files found in the bundle"

declare -A packages=()
unresolved=()
for soname in "${!needed_sonames[@]}"; do
  # A NEEDED entry may be a path (python-build-standalone links
  # "$ORIGIN/../lib/libpython3.12.so.1.0"); match the bundled file by name.
  [[ -n "${bundled_sonames[${soname##*/}]:-}" ]] && continue
  lib_path="$(ldconfig -p | awk -v so="${soname}" '$1 == so && /x86-64/ { print $NF; exit }')"
  if [[ -z "${lib_path}" ]]; then
    unresolved+=("${soname}")
    continue
  fi
  owner="$(dpkg -S "$(readlink -f -- "${lib_path}")" 2>/dev/null || dpkg -S "${lib_path}" 2>/dev/null || true)"
  owner="${owner%%:*}"
  if [[ -z "${owner}" ]]; then
    unresolved+=("${soname} (${lib_path}: no owning package)")
    continue
  fi
  packages["${owner}"]=1
done
if [[ "${#unresolved[@]}" -gt 0 ]]; then
  die "shipped ELF files need system libraries this host cannot resolve to a package: ${unresolved[*]}"
fi
packages["libc6"]=1
for base in "${BASE_DEPENDS[@]}"; do
  packages["${base}"]=1
done

depends=()
while IFS= read -r pkg; do
  if [[ "${pkg}" = "libc6" ]]; then
    depends+=("libc6 (>= ${glibc_floor})")
  else
    depends+=("${pkg}")
  fi
done < <(printf '%s\n' "${!packages[@]}" | LC_ALL=C sort)
DEPENDS="$(IFS=','; printf '%s' "${depends[*]}" | sed 's/,/, /g')"
info "scanned ${elf_count} ELF files; Depends: ${DEPENDS}"

# --- docs, man page, lintian overrides --------------------------------------

cat > "${PKGROOT}/usr/share/doc/${PACKAGE}/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: farna-control
Source: ${HOMEPAGE}

Files: *
Copyright: Farna Labs Ltd
License: BUSL-1.1
 Farna Control is source-available under the Business Source License 1.1.
 The full license text ships in the upstream repository (LICENSE) at
 ${HOMEPAGE}.
 The bundled third-party runtimes (CPython, PostgreSQL, Redis and the Python
 dependencies under /opt/farna-control/versions/${VERSION}) keep their own
 upstream licenses.
EOF

changelog_date="$(date -u -R)"
printf '%s (%s) stable; urgency=medium\n\n  * Farna Control %s native bundle.\n\n -- %s  %s\n' \
  "${PACKAGE}" "${VERSION}" "${VERSION}" "${MAINTAINER}" "${changelog_date}" \
  | gzip -9n > "${PKGROOT}/usr/share/doc/${PACKAGE}/changelog.gz"

cat <<'MANPAGE' | gzip -9n > "${PKGROOT}/usr/share/man/man1/farna-control.1.gz"
.TH FARNA-CONTROL 1 "" "Farna Control" "User Commands"
.SH NAME
farna-control \- self-hosted agent governance platform (native launcher CLI)
.SH SYNOPSIS
.B farna-control
.I command
.RI [ options ]
.SH DESCRIPTION
Runs the bundled Farna Control stack (API, workers, PostgreSQL and Redis) for
the calling user. Data lives in ~/.local/share/modulo/data.
Common commands: start, start \-\-detach, status, stop, doctor, logs, backup,
restore. Run
.B farna-control \-\-help
for the full list.
.SH NOTES
This package is upgraded with apt; the in-app
.B upgrade
command is disabled for package installs.
MANPAGE

# The bundle is a self-contained vendor runtime: it intentionally lives in
# /opt (FHS add-on software), ships its own interpreter, PostgreSQL, Redis and
# prebuilt wheels, and is not rebuilt with Debian's toolchain flags.
cat > "${PKGROOT}/usr/share/lintian/overrides/${PACKAGE}" <<'EOF'
farna-control: dir-or-file-in-opt *
farna-control: embedded-library *
farna-control: hardening-no-* *
farna-control: unstripped-binary-or-object *
farna-control: shared-library-lacks-prerequisites *
farna-control: library-not-linked-against-libc *
farna-control: binary-or-shlib-defines-rpath *
farna-control: custom-library-search-path *
farna-control: package-installs-python-pycache-dir *
farna-control: package-installs-python-bytecode *
farna-control: script-not-executable *
farna-control: executable-not-elf-or-script *
farna-control: non-standard-executable-perm *
farna-control: non-standard-file-perm *
farna-control: repeated-path-segment *
farna-control: extra-license-file *
farna-control: package-contains-documentation-outside-usr-share-doc *
farna-control: python-script-but-no-python-dep *
farna-control: unusual-interpreter *
farna-control: interpreter-in-usr-local *
farna-control: wrong-path-for-interpreter *
farna-control: shell-script-fails-syntax-check *
farna-control: package-contains-vcs-control-file *
farna-control: package-contains-empty-directory *
farna-control: statically-linked-binary *
farna-control: shared-library-is-executable *
farna-control: executable-in-usr-lib *
farna-control: national-encoding *
farna-control: zero-byte-file-in-doc-directory *
farna-control: package-contains-compiled-glib-schema *
EOF

# --- maintainer scripts -----------------------------------------------------
#
# HARD RULE: none of these may reference a per-user data dir. Remove and purge
# leave every user's ~/.local/share/modulo/data untouched; the only paths they
# act on are the package's own tree under /opt/farna-control.

cat > "${PKGROOT}/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  configure)
    echo "Farna Control installed. As your normal (non-root) user run:"
    echo "  farna-control start --detach"
    echo "then 'farna-control status'. Each user gets their own data dir."
    ;;
  abort-upgrade|abort-remove|abort-deconfigure)
    ;;
  *)
    echo "postinst called with unknown argument '$1'" >&2
    exit 1
    ;;
esac
exit 0
EOF

cat > "${PKGROOT}/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  remove|upgrade|deconfigure)
    # Per-user launchers are never stopped here (they belong to their users,
    # and stopping one is that user's call). Warn so a removal or upgrade
    # under a running stack is not silent.
    if command -v pgrep >/dev/null 2>&1 && pgrep -f '^/opt/farna-control/' >/dev/null 2>&1; then
      echo "warning: Farna Control processes are still running from /opt/farna-control." >&2
      echo "warning: each user should run 'farna-control stop' (their data is kept)." >&2
    fi
    ;;
  failed-upgrade)
    ;;
  *)
    echo "prerm called with unknown argument '$1'" >&2
    exit 1
    ;;
esac
exit 0
EOF

cat > "${PKGROOT}/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  remove|purge)
    # Only the package's own tree: bytecode a root-run CLI may have cached
    # outside the shipped files would otherwise keep /opt/farna-control alive.
    # User data dirs (~/.local/share/modulo/data) are NEVER touched, on remove
    # or on purge.
    if [ -d /opt/farna-control ]; then
      find /opt/farna-control -depth -type d -name __pycache__ -empty -delete 2>/dev/null || true
      rmdir --ignore-fail-on-non-empty /opt/farna-control/versions /opt/farna-control 2>/dev/null || true
    fi
    ;;
  upgrade|failed-upgrade|abort-install|abort-upgrade|disappear)
    ;;
  *)
    echo "postrm called with unknown argument '$1'" >&2
    exit 1
    ;;
esac
exit 0
EOF
chmod 0755 "${PKGROOT}/DEBIAN/postinst" "${PKGROOT}/DEBIAN/prerm" "${PKGROOT}/DEBIAN/postrm"

INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "${PKGROOT}" | awk '{print $1}')"
cat > "${PKGROOT}/DEBIAN/control" <<EOF
Package: ${PACKAGE}
Version: ${VERSION}
Architecture: amd64
Maintainer: ${MAINTAINER}
Installed-Size: ${INSTALLED_SIZE}
Depends: ${DEPENDS}
Section: admin
Priority: optional
Homepage: ${HOMEPAGE}
Description: self-hosted agent governance platform for agentic SDLC pipelines
 Farna Control runs a visual, composable pipeline of agent nodes that
 automate work between tools such as GitHub and Notion, with governance,
 human-in-the-loop review and audit built in.
 .
 This package ships the self-contained native bundle (Python runtime,
 PostgreSQL, Redis and the web UI) under /opt/farna-control and the
 farna-control command. It starts no system service: each user runs
 'farna-control start' and keeps their own data in their home directory.
EOF

# Root-owned, world-readable package tree regardless of the build umask.
find "${PKGROOT}" -path "${PKGROOT}/DEBIAN" -prune -o -type d -exec chmod 0755 {} +
find "${PKGROOT}${INSTALL_PREFIX}" -type f -perm /u+x -exec chmod 0755 {} +
find "${PKGROOT}${INSTALL_PREFIX}" -type f ! -perm /u+x -exec chmod 0644 {} +
find "${PKGROOT}/usr/share" -type f -exec chmod 0644 {} +

DEB="${OUTDIR}/${PACKAGE}_${VERSION}_amd64.deb"
info "building ${DEB}"
dpkg-deb --root-owner-group -Zxz --build "${PKGROOT}" "${DEB}" >/dev/null
(cd -- "${OUTDIR}" && sha256sum -- "$(basename -- "${DEB}")" > "$(basename -- "${DEB}").sha256")
dpkg-deb --info "${DEB}" | sed -n '/Package:/,$p'
info "wrote ${DEB}"
info "wrote ${DEB}.sha256: $(cat -- "${DEB}.sha256")"
