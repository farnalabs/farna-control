#!/usr/bin/env bash
# Name the file behind a Gatekeeper "sealed resource is missing or invalid"
# rejection (FAR-2593).
#
# Release cut run 44698118733 notarized the x86_64 DMG (status=Accepted), and
# `codesign --verify --deep --strict` passed on the mounted app, yet
# `spctl --assess --type execute` rejected it with
#   Farna Control.app: a sealed resource is missing or invalid
# without naming a file. spctl validates with stricter Security framework flags
# than codesign's command line exposes (symlink, app-like and sideband-data
# restrictions), so this helper reproduces each flag individually through
# SecStaticCodeCheckValidityWithErrors and prints the CFError user info, which
# names the altered / missing / added resource.
#
# Diagnostics only: this helper never fails the caller. It always exits 0 so a
# diagnostic gap can never mask, or replace, the hard gate that invoked it.
#
# Usage:
#   signature-diagnostics.sh <label> <app> [<reference-app>]
#       <label>          free-text tag printed on every line (e.g. pre_dmg)
#       <app>            the bundle to diagnose
#       <reference-app>  optional bundle <app> was copied from; when given the
#                        two trees are compared (content, symlinks, names).
set -uo pipefail

label="${1:-}"
app="${2:-}"
reference="${3:-}"

if [[ -z "$label" || -z "$app" ]]; then
  echo 'usage: signature-diagnostics.sh <label> <app> [<reference-app>]' >&2
  exit 0
fi

section() {
  printf '\n=== signature-diagnostics[%s]: %s ===\n' "$label" "$1"
}

section "codesign --verify --deep --strict --verbose=4"
codesign --verify --deep --strict --verbose=4 "$app" 2>&1 | tail -n 60
printf 'codesign_verify_exit=%s\n' "${PIPESTATUS[0]}"

section "spctl -a -vvv -t exec"
spctl -a -vvv -t exec "$app" 2>&1 | tail -n 40
printf 'spctl_exit=%s\n' "${PIPESTATUS[0]}"

# Reproduce Gatekeeper's validation flags one at a time. Raw values are the
# public SecCSFlags constants from <Security/CSCommon.h>.
section "Security framework flag probes"
probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/sigdiag.XXXXXX")"
cat >"$probe_dir/probe.swift" <<'SWIFT'
import Foundation
import Security

let path = CommandLine.arguments[1]
var staticCode: SecStaticCode?
let created = SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, SecCSFlags(rawValue: 0), &staticCode)
guard created == errSecSuccess, let code = staticCode else {
    print("probe: SecStaticCodeCreateWithPath status=\(created)")
    exit(0)
}
let checkAllArchitectures: UInt32 = 1 << 0
let checkNestedCode: UInt32 = 1 << 3
let strictValidate: UInt32 = 1 << 4
let restrictSymlinks: UInt32 = 1 << 7
let restrictToAppLike: UInt32 = 1 << 8
let restrictSidebandData: UInt32 = 1 << 9
let base = checkAllArchitectures | checkNestedCode | strictValidate
let probes: [(String, UInt32)] = [
    ("strict_nested", base),
    ("restrict_symlinks", base | restrictSymlinks),
    ("restrict_to_app_like", base | restrictToAppLike),
    ("restrict_sideband_data", base | restrictSidebandData),
    ("gatekeeper_flags", base | restrictSymlinks | restrictToAppLike | restrictSidebandData),
]
for (name, flags) in probes {
    var unmanagedError: Unmanaged<CFError>?
    let status = SecStaticCodeCheckValidityWithErrors(code, SecCSFlags(rawValue: flags), nil, &unmanagedError)
    print("probe \(name): status=\(status)")
    if let error = unmanagedError?.takeRetainedValue() {
        print("  error: \(error)")
        if let info = CFErrorCopyUserInfo(error) as? [String: Any] {
            for key in info.keys.sorted() {
                print("  \(key): \(String(describing: info[key]!))")
            }
        }
    }
}
SWIFT
if command -v xcrun >/dev/null 2>&1; then
  xcrun swiftc -O -o "$probe_dir/probe" "$probe_dir/probe.swift" >"$probe_dir/build.log" 2>&1
  build_exit=$?
  if [[ "$build_exit" -eq 0 ]]; then
    "$probe_dir/probe" "$app" 2>&1 | head -n 400
  else
    printf 'probe build failed (exit=%d)\n' "$build_exit"
    tail -n 30 "$probe_dir/build.log"
  fi
else
  echo 'probe skipped: xcrun unavailable'
fi
rm -rf "$probe_dir"

section "syspolicyd log (last 3 minutes)"
log show --last 3m --style compact \
  --predicate 'process == "syspolicyd" OR process == "spctl" OR subsystem == "com.apple.securityd"' 2>/dev/null \
  | grep -Ei 'sealed|resource|symlink|invalid|-670[0-9][0-9]|file://' | tail -n 120

section "symbolic links (absolute or escaping the bundle are flagged)"
python3 -I - "$app" <<'PY'
import os
import sys

root = os.path.realpath(sys.argv[1])
count = 0
for dirpath, dirnames, filenames in os.walk(sys.argv[1]):
    for name in dirnames + filenames:
        path = os.path.join(dirpath, name)
        if not os.path.islink(path):
            continue
        count += 1
        target = os.readlink(path)
        resolved = os.path.realpath(path)
        flags = []
        if os.path.isabs(target):
            flags.append("ABSOLUTE")
        if not (resolved == root or resolved.startswith(root + os.sep)):
            flags.append("ESCAPES_BUNDLE")
        if not os.path.exists(path):
            flags.append("DANGLING")
        rel = os.path.relpath(path, sys.argv[1])
        print(f"symlink {rel} -> {target} {' '.join(flags)}".rstrip())
print(f"symlink_count={count}")
PY

section "file names that are not plain ASCII or collide case-insensitively"
python3 -I - "$app" <<'PY'
import os
import sys
import unicodedata

seen = {}
for dirpath, dirnames, filenames in os.walk(sys.argv[1]):
    for name in dirnames + filenames:
        rel = os.path.relpath(os.path.join(dirpath, name), sys.argv[1])
        if not name.isascii():
            nfc = unicodedata.normalize("NFC", name) == name
            nfd = unicodedata.normalize("NFD", name) == name
            print(f"non_ascii {rel!r} nfc={nfc} nfd={nfd}")
        key = os.path.join(dirpath, name).casefold()
        if key in seen and seen[key] != rel:
            print(f"case_collision {seen[key]!r} {rel!r}")
        seen[key] = rel
PY

section "files carrying extended attributes"
find "$app" -print0 2>/dev/null | xargs -0 xattr 2>/dev/null | grep -v 'com.apple.cs.Code' | head -n 80

section "files that are not readable or are special"
find "$app" \( ! -type f ! -type d ! -type l \) -print 2>/dev/null | head -n 40
find "$app" ! -perm -u+r -print 2>/dev/null | head -n 40

if [[ -n "$reference" && -d "$reference" ]]; then
  section "comparison with reference $reference"
  diff -rq "$reference" "$app" 2>&1 | head -n 80
  printf 'diff_exit=%s\n' "${PIPESTATUS[0]}"
  (cd "$reference" && find . -type l -print | sort) >"${TMPDIR:-/tmp}/sigdiag-ref-links.$$"
  (cd "$app" && find . -type l -print | sort) >"${TMPDIR:-/tmp}/sigdiag-app-links.$$"
  diff "${TMPDIR:-/tmp}/sigdiag-ref-links.$$" "${TMPDIR:-/tmp}/sigdiag-app-links.$$" | head -n 40
  rm -f "${TMPDIR:-/tmp}/sigdiag-ref-links.$$" "${TMPDIR:-/tmp}/sigdiag-app-links.$$"
fi

section "end"
exit 0
