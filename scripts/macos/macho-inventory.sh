#!/usr/bin/env bash
# Inventory and audit every Mach-O file inside a macOS app bundle (FAR-2575).
#
# Apple marked every DMG submission `Invalid` because four bundled
# python-build-standalone Tcl/Itcl dylibs under
# Contents/Resources/bundle/runtime/lib/{thread3.0.6,itcl4.3.8}/ were never
# signed with the Developer ID identity or a secure timestamp. The old nested
# signer only considered files with an executable permission bit
# (`find -perm -111`), and those dylibs ship mode 0644, so they were skipped;
# `codesign --deep` does not reach a non-standard location like
# Contents/Resources/bundle either. Notarization inspects every Mach-O in the
# archive regardless of permission bits, so selection must too.
#
# Usage:
#   macho-inventory.sh discover <app>
#       Print every regular (non-symlink) Mach-O file under <app>/Contents,
#       one path per line, deepest paths first so a caller signing in that
#       order signs inside-out. Mach-O is identified by its magic bytes, never
#       by permission bits or file name. Fails if none is found.
#   macho-inventory.sh audit <app>
#       Fail unless every Mach-O under <app>/Contents passes
#       `codesign --verify --strict` AND is signed by a Developer ID
#       Application authority AND carries a secure timestamp. Every offending
#       file is named. Run it before `notarytool submit` so a known-bad
#       artefact never costs a notary round trip.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: macho-inventory.sh discover <app>
       macho-inventory.sh audit <app>
USAGE
}

die() {
  printf 'macho-inventory: %s\n' "$*" >&2
  exit 1
}

discover() {
  local app="$1"
  [[ -d "$app/Contents" ]] || die "no Contents directory under: $app"
  # Magic-byte detection in Python: one process for the whole tree instead of
  # one `file` spawn per file, independent of permission bits and extensions.
  # Fat headers share 0xcafebabe with Java class files, so a fat header is only
  # accepted with a plausible architecture count (the same test `file` uses).
  python3 -I - "$app/Contents" <<'PY'
import os
import struct
import sys

THIN = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}
FAT = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}


def is_macho(path):
    try:
        with open(path, "rb") as handle:
            header = handle.read(8)
    except OSError as exc:
        sys.stderr.write("macho-inventory: cannot read %s: %s\n" % (path, exc))
        raise SystemExit(1)
    magic = header[:4]
    if magic in THIN:
        return True
    if magic in FAT and len(header) == 8:
        return 0 < struct.unpack(">I", header[4:8])[0] < 20
    return False


root = sys.argv[1]
found = []
for directory, _subdirs, files in os.walk(root, followlinks=False):
    for name in files:
        path = os.path.join(directory, name)
        if os.sep != "/":
            path = path.replace(os.sep, "/")
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        if is_macho(path):
            found.append(path)
# Deepest first (inside-out), then by path for a stable order.
found.sort(key=lambda p: (-p.count("/"), p))
for path in found:
    sys.stdout.write(path + "\n")
if not found:
    sys.stderr.write("macho-inventory: no Mach-O file found under %s\n" % root)
    raise SystemExit(1)
PY
}

audit() {
  local app="$1"
  local inventory details path failures=0 checked=0
  inventory="$(mktemp "${TMPDIR:-/tmp}/macho-inventory.XXXXXX")"
  details="$(mktemp "${TMPDIR:-/tmp}/macho-codesign.XXXXXX")"
  # shellcheck disable=SC2064 # expand now: the paths are fixed for this call.
  trap "rm -f '$inventory' '$details'" EXIT
  discover "$app" >"$inventory"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    checked=$((checked + 1))
    if ! codesign --verify --strict "$path" >"$details" 2>&1; then
      printf 'unsigned or invalid signature: %s (%s)\n' "$path" "$(tr '\n' ' ' <"$details")" >&2
      failures=$((failures + 1))
      continue
    fi
    # `codesign --display` writes its report to stderr.
    if ! codesign --display --verbose=4 "$path" >"$details" 2>&1; then
      printf 'cannot read signature: %s (%s)\n' "$path" "$(tr '\n' ' ' <"$details")" >&2
      failures=$((failures + 1))
      continue
    fi
    if ! grep -q '^Authority=Developer ID Application: ' "$details"; then
      printf 'not signed with a Developer ID Application certificate: %s\n' "$path" >&2
      failures=$((failures + 1))
      continue
    fi
    # A secure (TSA) timestamp is reported as `Timestamp=`; a signature
    # without one reports only `Signed Time=` or nothing at all.
    if ! grep -q '^Timestamp=' "$details"; then
      printf 'signature has no secure timestamp: %s\n' "$path" >&2
      failures=$((failures + 1))
      continue
    fi
    printf 'ok: %s\n' "$path"
  done <"$inventory"
  if [[ "$failures" -ne 0 ]]; then
    printf 'macho-inventory audit: %d of %d Mach-O file(s) failed the Developer ID + secure timestamp check\n' "$failures" "$checked" >&2
    return 1
  fi
  printf 'macho-inventory audit: all %d Mach-O file(s) carry a Developer ID signature with a secure timestamp\n' "$checked"
}

[[ $# -eq 2 ]] || {
  usage
  exit 2
}
case "$1" in
  discover) discover "$2" ;;
  audit) audit "$2" ;;
  *)
    usage
    exit 2
    ;;
esac
