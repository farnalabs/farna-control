#!/usr/bin/env bash
# Build a genuine native macOS bundle. Every input is downloaded from an
# upstream source and verified before it is compiled or copied into the app.
#
# The Python runtime is the pinned python-build-standalone CPython distribution,
# not a CPython source build. The third argument selects the native architecture
# (arm64 or x86_64); arm64 remains the default for existing callers.
set -euo pipefail

VERSION=${1:?version required}
BUILD_ROOT=${2:?build root required}
MACOS_ARCH=${3:-arm64}
case "${MACOS_ARCH}" in
  arm64) PBS_ARCH=aarch64; PBS_MACHO_ARCH=arm64 ;;
  x86_64) PBS_ARCH=x86_64; PBS_MACHO_ARCH=x86_64 ;;
  *) echo "unsupported macOS architecture: ${MACOS_ARCH}" >&2; exit 2 ;;
esac
ROOT_DIR="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
APP_NAME="Modulo-${VERSION}.app"
APP_ROOT="${BUILD_ROOT}/${APP_NAME}"
BUNDLE_ROOT="${APP_ROOT}/Contents/Resources/bundle"
DOWNLOADS="${BUILD_ROOT}/downloads"
mkdir -p "${DOWNLOADS}" "${BUNDLE_ROOT}" "${APP_ROOT}/Contents/MacOS" "${APP_ROOT}/Contents/Resources"

# Render the checked-in brand mark into the native icon format before signing.
# qlmanage is part of macOS and preserves the SVG's existing Modulo mark without
# introducing a second logo source. iconutil then embeds the complete @1x/@2x
# icon family in Contents/Resources, where Finder and LaunchServices read it.
ICON_SOURCE="${ROOT_DIR}/scripts/macos/assets/Modulo.svg"
ICONSET_DIR="${BUILD_ROOT}/Modulo.iconset"
[[ -s "${ICON_SOURCE}" ]]
rm -rf "${ICONSET_DIR}"
mkdir -p "${ICONSET_DIR}"
qlmanage -t -s 1024 -o "${BUILD_ROOT}" "${ICON_SOURCE}" >/dev/null
ICON_RENDERED="${BUILD_ROOT}/Modulo.svg.png"
[[ -s "${ICON_RENDERED}" ]]
sips -z 16 16 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_16x16.png" >/dev/null
sips -z 32 32 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_16x16@2x.png" >/dev/null
sips -z 128 128 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_128x128.png" >/dev/null
sips -z 256 256 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_256x256.png" >/dev/null
sips -z 512 512 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_512x512.png" >/dev/null
sips -z 1024 1024 "${ICON_RENDERED}" --out "${ICONSET_DIR}/icon_512x512@2x.png" >/dev/null
iconutil --convert icns --output "${APP_ROOT}/Contents/Resources/Modulo.icns" "${ICONSET_DIR}"
[[ -s "${APP_ROOT}/Contents/Resources/Modulo.icns" ]]

PBS_RELEASE=20260901
PBS_VERSION=3.12.14
PBS_TARBALL="cpython-${PBS_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-apple-darwin-install_only.tar.gz"
if [[ "${MACOS_ARCH}" == arm64 ]]; then
  PBS_SHA256="3ee3ee547cedfeb7c2b16b2b7156039f7b470bb8f857e226fd3d2eb11db83c76"
else
  PBS_SHA256="2e31b23f3f1319f707d0e620b48847a0046577541d357276821f9f1b5492e0ba"
fi
PG_VERSION=16.15
PG_SHA256="4f200ca23dfb120ff9838f13ce06014aad1d3c432d16ee9f93ab2000c0eeef7b"
REDIS_VERSION=8.0.2
REDIS_SHA256="caf3c0069f06fc84c5153bd2a348b204c578de80490c73857bee01d9b5d7401f"

# A placeholder digest is a deliberate build-stop, never a release path.
case "${PBS_SHA256}" in
  REPLACE_*|'' ) echo "::error::python-build-standalone macOS ${MACOS_ARCH} SHA-256 has not been provisioned" >&2; exit 1 ;;
  * ) ;;
esac

download_verify() {
  local url=$1 output=$2 expected=$3
  curl --fail --location --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 --connect-timeout 20 -o "${output}" "${url}"
  printf '%s  %s\n' "${expected}" "${output}" | shasum -a 256 --check --strict
}

download_verify "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_TARBALL}" "${DOWNLOADS}/${PBS_TARBALL}" "${PBS_SHA256}"
download_verify "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.gz" "${DOWNLOADS}/postgresql-${PG_VERSION}.tar.gz" "${PG_SHA256}"
download_verify "https://github.com/redis/redis/archive/refs/tags/${REDIS_VERSION}.tar.gz" "${DOWNLOADS}/redis-${REDIS_VERSION}.tar.gz" "${REDIS_SHA256}"

tar -xzf "${DOWNLOADS}/${PBS_TARBALL}" -C "${BUNDLE_ROOT}"
mv "${BUNDLE_ROOT}/python" "${BUNDLE_ROOT}/runtime"
# Verify the trusted runtime input before it becomes part of the release bundle.
file "${BUNDLE_ROOT}/runtime/bin/python3" | grep -E "Mach-O 64-bit executable ${PBS_MACHO_ARCH}" >/dev/null
otool -L "${BUNDLE_ROOT}/runtime/bin/python3" | grep -q '/usr/lib/libSystem.B.dylib'
"${BUNDLE_ROOT}/runtime/bin/python3" --version | grep -Fqx "Python ${PBS_VERSION}"

mkdir -p "${BUILD_ROOT}/src"
tar -xzf "${DOWNLOADS}/postgresql-${PG_VERSION}.tar.gz" -C "${BUILD_ROOT}/src"
PG_SRC="${BUILD_ROOT}/src/postgresql-${PG_VERSION}"
mkdir -p "${BUILD_ROOT}/pg-build" "${BUNDLE_ROOT}/pg"
(
  cd "${BUILD_ROOT}/pg-build"
  "${PG_SRC}/configure" --prefix="${BUNDLE_ROOT}/pg" --without-readline --without-zlib --without-icu
  make -j"$(sysctl -n hw.ncpu)"
  make install
)

tar -xzf "${DOWNLOADS}/redis-${REDIS_VERSION}.tar.gz" -C "${BUILD_ROOT}/src"
REDIS_SRC="${BUILD_ROOT}/src/redis-${REDIS_VERSION}"
(
  cd "${REDIS_SRC}"
  make -j"$(sysctl -n hw.ncpu)" BUILD_TLS=no
)
# BEGIN bundled_bin_layout
# The launcher exports this directory (Contents/Resources/bundle/bin) as
# FARNA_CONTROL_BUNDLED_BIN_DIR and the supervisor resolves every bundled
# binary out of it, so this is the one authoritative location: real binaries
# stay in pg/ and redis/, and bin/ holds only in-bundle relative symlinks. A
# copy in a second directory would drift; do not add one. The build-time gate
# below resolves the directory the compiled launcher actually exports and
# verifies every name in farna.launcher.entry.REQUIRED_BUNDLED_BINARIES there.
# BEGIN markers are executed verbatim by
# backend/tests/unit/test_macos_installer_normalize.py.
mkdir -p "${BUNDLE_ROOT}/redis" "${BUNDLE_ROOT}/bin"
cp "${REDIS_SRC}/src/redis-server" "${BUNDLE_ROOT}/redis/redis-server"
cp "${REDIS_SRC}/src/redis-cli" "${BUNDLE_ROOT}/redis/redis-cli"
for binary in initdb postgres pg_isready pg_dump psql; do
  ln -s "../pg/bin/${binary}" "${BUNDLE_ROOT}/bin/${binary}"
done
for binary in redis-server redis-cli; do
  ln -s "../redis/${binary}" "${BUNDLE_ROOT}/bin/${binary}"
done
# END bundled_bin_layout

# Build and copy the real Vue/Vite distribution. The macOS workflow pins Node
# 22 and pnpm 11 before invoking this script; never ship an API-only bundle.
(
  cd "${ROOT_DIR}/frontend"
  pnpm install --frozen-lockfile --ignore-scripts
  pnpm run build
)
mkdir -p "${BUNDLE_ROOT}/frontend-dist"
cp -a "${ROOT_DIR}/frontend/dist/." "${BUNDLE_ROOT}/frontend-dist/"

mkdir -p "${BUNDLE_ROOT}/backend"
cp -a "${ROOT_DIR}/backend/src" "${BUNDLE_ROOT}/backend/src"
cp "${ROOT_DIR}/backend/alembic.ini" "${ROOT_DIR}/backend/pyproject.toml" "${ROOT_DIR}/backend/uv.lock" "${BUNDLE_ROOT}/backend/"
[[ -f "${BUNDLE_ROOT}/backend/alembic.ini" ]]
[[ -d "${BUNDLE_ROOT}/backend/src/farna/db/migrations" ]]
# Keep Alembic's config beside the bundled backend source. The app starts from
# launchd's data directory, so the relative script_location in alembic.ini must
# be resolved from this installed bundle rather than the build checkout.
# Build the venv against the bundled interpreter with copies, rather than copying
# the developer venv. This keeps the signed app relocatable: venv entry points
# and interpreter links resolve inside the app instead of to build-time paths.
"${BUNDLE_ROOT}/runtime/bin/python3.12" -m venv --copies "${BUNDLE_ROOT}/backend/.venv"
uv sync --frozen --no-dev --no-install-project --project "${BUNDLE_ROOT}/backend" --python "${BUNDLE_ROOT}/backend/.venv/bin/python"
# The native launcher starts the bundled interpreter directly. Its child
# supervisor processes deliberately receive an allowlisted environment that
# excludes PYTHONPATH, so make the application source importable without an
# inherited environment variable. A relative .pth keeps the installed app
# relocatable after it is copied out of the build workspace.
RUNTIME_SITE_PACKAGES="${BUNDLE_ROOT}/runtime/lib/python3.12/site-packages"
mkdir -p "${RUNTIME_SITE_PACKAGES}"
printf '%s\n' '../../../../backend/src' > "${RUNTIME_SITE_PACKAGES}/modulo-bundle.pth"
env -u PYTHONPATH -u PYTHONHOME "${BUNDLE_ROOT}/runtime/bin/python3" -c 'import farna.launcher.supervisor'

# Assert the installed bundle's actual launcher module emits PostgreSQL's
# server argv contract as separate elements. A shell command string would be
# the wrong contract here: the supervisor passes an argv list directly.
PYTHONPATH="${BUNDLE_ROOT}/backend/src:${BUNDLE_ROOT}/backend/.venv/lib/python3.12/site-packages" \
  "${BUNDLE_ROOT}/backend/.venv/bin/python" - "${BUNDLE_ROOT}" <<'PY'
from pathlib import Path
import sys

from farna.launcher.initdb import postgres_server_argv

bundle = Path(sys.argv[1])
pgdata = bundle / "runtime-contract-pgdata"
argv = postgres_server_argv(pgdata, "127.0.0.1", 18000, bundle / "bin")
assert argv == [
    str(bundle / "bin" / "postgres"),
    "-D",
    str(pgdata),
    "-h",
    "127.0.0.1",
    "-p",
    "18000",
], argv
assert "--host" not in argv and "--port" not in argv, argv
assert f"--pgdata={pgdata}" not in argv, argv
print(f"bundled postgres argv contract: {argv}")
PY

assert_no_escaping_symlinks() {
  local root="$1" link target resolved canonical_root
  canonical_root="$(cd "$root" && pwd -P)"
  while IFS= read -r -d '' link; do
    target="$(readlink "$link")"
    if [[ "$target" = /* ]]; then
      resolved="$target"
    else
      resolved="$(dirname "$link")/$target"
    fi
    resolved="$(python3 - "$resolved" <<'PY'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PY
)"
    case "$resolved/" in
      "$canonical_root/"*) ;;
      *)
        printf '::error::symlink escapes app bundle: link=%s target=%s resolved=%s\n' "$link" "$target" "$resolved" >&2
        return 1
        ;;
    esac
  done < <(find "${root}/Contents" -type l -print0)
}

assert_no_escaping_symlinks "$APP_ROOT"

cat > "${BUILD_ROOT}/launcher.c" <<'EOF'
#include <libproc.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    uint32_t size = 0;
    if (_NSGetExecutablePath(NULL, &size) == 0 || size == 0) return 70;
    char *path = calloc(size + 1, 1);
    if (!path || _NSGetExecutablePath(path, &size) != 0) return 70;
    char *contents = strrchr(path, '/');
    if (!contents) return 70;
    *contents = '\0';
    char *macos = strrchr(path, '/');
    if (!macos) return 70;
    *macos = '\0';
    char bundle[4096];
    snprintf(bundle, sizeof(bundle), "%s/Resources/bundle", path);
    char python[4096];
    snprintf(python, sizeof(python), "%s/runtime/bin/python3", bundle);
    char src[4096];
    snprintf(src, sizeof(src), "%s/backend/src", bundle);
    char site[4096];
    snprintf(site, sizeof(site), "%s/backend/.venv/lib/python3.12/site-packages", bundle);
    setenv("PYTHONPATH", src, 1);
    char path_env[8192];
    snprintf(path_env, sizeof(path_env), "%s/runtime/bin:%s/pg/bin:%s/redis:%s", bundle, bundle, bundle, getenv("PATH") ? getenv("PATH") : "");
    setenv("PATH", path_env, 1);
    char bin_dir[4096];
    snprintf(bin_dir, sizeof(bin_dir), "%s/bin", bundle);
    /* The resolver reads exactly this variable name; the build gate below
       compares it with farna.launcher.entry.BUNDLED_BIN_DIR_ENV. */
    setenv("FARNA_CONTROL_BUNDLED_BIN_DIR", bin_dir, 1);
    char frontend_dist[4096];
    snprintf(frontend_dist, sizeof(frontend_dist), "%s/frontend-dist", bundle);
    /* The single-port SPA seam reads exactly these two names
       (farna.api.main._SERVE_SPA_ENV / _FRONTEND_DIST_ENV). FAR-2564: the
       pre-rebrand MODULO_ spelling exported here was read by nothing, so the
       installed app kept the API mount and "/" returned the API JSON instead
       of the SPA entry document. backend/tests/unit/launcher/
       test_native_shim_env_contract.py derives both sides from the real files
       and fails when a launcher sets a name Python never reads. */
    setenv("FARNA_CONTROL_SERVE_SPA", "1", 1);
    setenv("FARNA_CONTROL_FRONTEND_DIST", frontend_dist, 1);
    char final_pythonpath[12288];
    snprintf(final_pythonpath, sizeof(final_pythonpath), "%s:%s", src, site);
    setenv("PYTHONPATH", final_pythonpath, 1);
    char **child = calloc((size_t)argc + 3, sizeof(char *));
    if (!child) return 70;
    child[0] = python;
    child[1] = "-c";
    child[2] = "from farna.cli.main import cli; cli()";
    for (int i = 1; i < argc; ++i) child[i + 2] = argv[i];
    child[argc + 2] = NULL;
    execv(python, child);
    perror("Modulo launcher execv");
    return 127;
}
EOF
clang -Wall -Wextra -Werror -o "${APP_ROOT}/Contents/MacOS/modulo" "${BUILD_ROOT}/launcher.c"
chmod 755 "${APP_ROOT}/Contents/MacOS/modulo"

# Producer/consumer contract gate (FAR-2554). The launcher exports an env name
# and a bin-dir expression; the packaged resolver reads that name and resolves
# that directory. Both are read back out of the compiled launcher's source and
# fed to the resolver, so a rename on either side, or a layout move, fails THIS
# BUILD instead of the installed app's first boot ("bundled binaries not found
# at .../runtime/bundled/bin", an env value the resolver never read).
# BEGIN bundled_bin_dir_contract_gate
LAUNCHER_BIN_DIR_ENV="$(sed -n 's/.*setenv("\([A-Za-z_][A-Za-z0-9_]*\)", bin_dir, 1).*/\1/p' "${BUILD_ROOT}/launcher.c")"
LAUNCHER_BIN_DIR_FORMAT="$(sed -n 's/.*snprintf(bin_dir, sizeof(bin_dir), "\([^"]*\)", bundle).*/\1/p' "${BUILD_ROOT}/launcher.c")"
if [[ -z "${LAUNCHER_BIN_DIR_ENV}" || -z "${LAUNCHER_BIN_DIR_FORMAT}" ]]; then
  echo "::error::could not read the bundled bin dir contract from ${BUILD_ROOT}/launcher.c (env name or bin dir expression is missing)" >&2
  exit 1
fi
LAUNCHER_BIN_DIR="${LAUNCHER_BIN_DIR_FORMAT/\%s/${BUNDLE_ROOT}}"
PYTHONPATH="${BUNDLE_ROOT}/backend/src:${BUNDLE_ROOT}/backend/.venv/lib/python3.12/site-packages" \
  env "${LAUNCHER_BIN_DIR_ENV}=${LAUNCHER_BIN_DIR}" \
  "${BUNDLE_ROOT}/backend/.venv/bin/python" - "${BUNDLE_ROOT}" "${LAUNCHER_BIN_DIR_ENV}" "${LAUNCHER_BIN_DIR}" <<'PY'
import sys
from pathlib import Path

from farna.launcher.entry import BUNDLED_BIN_DIR_ENV, REQUIRED_BUNDLED_BINARIES, resolve_bin_dir

bundle = Path(sys.argv[1])
exported_env_name = sys.argv[2]
exported_bin_dir = Path(sys.argv[3])
errors = []
if exported_env_name != BUNDLED_BIN_DIR_ENV:
    errors.append(f"launcher exports {exported_env_name!r} but the resolver reads {BUNDLED_BIN_DIR_ENV!r}")
resolved = resolve_bin_dir()
if resolved != exported_bin_dir:
    errors.append(f"launcher exports {exported_bin_dir} but the resolver returns {resolved}")
if resolved != bundle / "bin":
    errors.append(f"the bundle installs binaries into {bundle / 'bin'} but the launcher resolves {resolved}")
missing = [name for name in REQUIRED_BUNDLED_BINARIES if not (resolved / name).exists()]
if missing:
    errors.append(f"missing from the launcher-resolved bin dir {resolved}: {', '.join(missing)}")
if errors:
    for error in errors:
        print(f"::error::{error}", file=sys.stderr)
    raise SystemExit(1)
print(f"bundled bin dir contract: env={BUNDLED_BIN_DIR_ENV} dir={resolved} binaries={len(REQUIRED_BUNDLED_BINARIES)}")
PY
# END bundled_bin_dir_contract_gate

# Single source of truth for the app-bundle metadata: the shared helper in
# scripts/ owns the Info.plist contents (the gate0 filter watches it too).
(
  cd "${APP_ROOT}/Contents"
  "${ROOT_DIR}/scripts/create-macos-info-plist.sh" "${VERSION}" "Info.plist" "${MACOS_ARCH}"
)

echo "${VERSION}" > "${BUNDLE_ROOT}/VERSION"
file "${APP_ROOT}/Contents/MacOS/modulo"
"${BUNDLE_ROOT}/pg/bin/postgres" --version
"${BUNDLE_ROOT}/redis/redis-server" --version
"${APP_ROOT}/Contents/MacOS/modulo" --help >/dev/null
