#!/usr/bin/env bash
# Name the file behind a Gatekeeper "sealed resource is missing or invalid"
# rejection (FAR-2593).
#
# Release cut run 44698118733 notarized the x86_64 DMG (status=Accepted), and
# `codesign --verify --deep --strict` passed on the mounted app, yet
# `spctl --assess --type execute` rejected it with
#   Farna Control.app: a sealed resource is missing or invalid
# without naming a file. spctl validates with stricter Security framework flags
# than codesign's command line exposes, so the full mode reproduces each flag
# through SecStaticCodeCheckValidityWithErrors, re-derives every CodeResources
# hash independently and scans the bundle for symlink, name, hard-link,
# multi-architecture and extended-attribute oddities.
#
# Diagnostics only: this helper never fails the caller. It always exits 0 so a
# diagnostic gap can never mask, or replace, the hard gate that invoked it.
# Every external call is time-bounded (run 44743991976 showed how long an
# unbounded whole-bundle scan can take), so a stalled tool cannot hang the job.
#
# Usage:
#   signature-diagnostics.sh [--quick] <label> <app> [<reference-app>]
#       --quick          codesign verify plus one Gatekeeper assessment only;
#                        used on the success path. Without it the full (slow)
#                        diagnostics run, which belongs on failure paths.
#       <label>          free-text tag printed on every section header
#       <app>            the bundle to diagnose
#       <reference-app>  optional bundle <app> was copied from; when given the
#                        two trees are compared.
set -uo pipefail

mode=full
if [[ "${1:-}" == "--quick" ]]; then
  mode=quick
  shift
fi
label="${1:-}"
app="${2:-}"
reference="${3:-}"

if [[ -z "$label" || -z "$app" ]]; then
  echo 'usage: signature-diagnostics.sh [--quick] <label> <app> [<reference-app>]' >&2
  exit 0
fi

section() {
  printf '\n=== signature-diagnostics[%s]: %s ===\n' "$label" "$1"
}

# Run "$@" for at most $1 seconds (bash 3.2-safe watchdog; macOS ships no
# timeout(1)). Prints a marker and returns 124 when the bound is hit.
run_bounded() {
  local timeout_secs="$1" pid status started
  shift
  "$@" &
  pid=$!
  started=$SECONDS
  while kill -0 "$pid" 2>/dev/null; do
    if [[ $(( SECONDS - started )) -ge "$timeout_secs" ]]; then
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      printf 'signature-diagnostics: %s exceeded %ss and was terminated\n' "$1" "$timeout_secs"
      return 124
    fi
    sleep 0.2
  done
  status=0
  wait "$pid" || status=$?
  return "$status"
}

work_dir="$(mktemp -d "${TMPDIR:-/tmp}/sigdiag.XXXXXX")"
trap 'rm -rf "$work_dir"' EXIT

section "mode=$mode"

section "codesign --verify --deep --strict --verbose=4"
run_bounded 180 codesign --verify --deep --strict --verbose=4 "$app" >"$work_dir/codesign.log" 2>&1
printf 'codesign_verify_exit=%s\n' "$?"
tail -n 40 "$work_dir/codesign.log"

if [[ "$mode" == quick ]]; then
  section "spctl -a -vvv -t exec"
  run_bounded 120 spctl -a -vvv -t exec "$app" >"$work_dir/spctl.log" 2>&1
  printf 'spctl_exit=%s\n' "$?"
  tail -n 20 "$work_dir/spctl.log"
  section "end"
  exit 0
fi

# Repeat the assessment: a verdict that changes between identical calls
# points at the assessment service, not at the bundle contents.
assessment_started="$(date '+%Y-%m-%d %H:%M:%S')"
for attempt in 1 2 3; do
  section "spctl -a -vvv -t exec (attempt $attempt)"
  run_bounded 120 spctl -a -vvv -t exec "$app" >"$work_dir/spctl.log" 2>&1
  printf 'spctl_exit=%s\n' "$?"
  tail -n 20 "$work_dir/spctl.log"
done

if command -v syspolicy_check >/dev/null 2>&1; then
  section "syspolicy_check distribution"
  run_bounded 180 syspolicy_check distribution "$app" --verbose >"$work_dir/syspolicy_check.log" 2>&1
  printf 'syspolicy_check_exit=%s\n' "$?"
  tail -n 60 "$work_dir/syspolicy_check.log"
fi

# Reproduce Gatekeeper's validation flags one at a time. Raw values are the
# public SecCSFlags constants from <Security/CSCommon.h>.
section "Security framework flag probes"
cat >"$work_dir/probe.swift" <<'SWIFT'
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
  if run_bounded 240 xcrun swiftc -O -o "$work_dir/probe" "$work_dir/probe.swift" >"$work_dir/build.log" 2>&1; then
    run_bounded 240 "$work_dir/probe" "$app" >"$work_dir/probe.log" 2>&1
    printf 'probe_exit=%s\n' "$?"
    head -n 200 "$work_dir/probe.log"
  else
    echo 'probe build failed or timed out'
    tail -n 20 "$work_dir/build.log"
  fi
else
  echo 'probe skipped: xcrun unavailable'
fi

section "syspolicyd log since the first assessment (info + debug)"
run_bounded 90 log show --info --debug --style compact --start "$assessment_started" \
  --predicate 'process == "syspolicyd" OR process == "spctl" OR subsystem == "com.apple.syspolicy"' \
  >"$work_dir/syspolicyd.log" 2>/dev/null
printf 'log_show_exit=%s\n' "$?"
grep -Ev 'CSSMERR_CSP_INVALID_KEYATTR_MASK|SecKeyVerifySignature|SecTrustEvaluate|SecCertificate|SecTrustCopy|SecPolicyCreate|com.apple.network' \
  "$work_dir/syspolicyd.log" | tail -n 150

section "main executable signature and entitlements"
run_bounded 60 codesign -dvvv "$app" >"$work_dir/display.log" 2>&1
grep -Ev '^(Authority|Signed Time|Timestamp)=' "$work_dir/display.log" | head -n 30
run_bounded 60 codesign -d --entitlements - "$app" 2>&1 | head -n 30

# One pass over the bundle: re-derive every CodeResources seal, and record
# unsealed files, symlinks, hard links, multi-architecture (fat) Mach-O,
# non-ASCII and case-colliding names. Hashing is sequential and streams each
# file, so the pass is bounded by disk speed, not by process spawning.
section "single-pass bundle scan (CodeResources seal, links, fat Mach-O, names)"
cat >"$work_dir/scan.py" <<'PY'
import hashlib
import os
import plistlib
import re
import struct
import sys
import unicodedata

app = sys.argv[1]
contents = os.path.join(app, "Contents")
root = os.path.realpath(app)
with open(os.path.join(contents, "_CodeSignature", "CodeResources"), "rb") as handle:
    seal = plistlib.load(handle)
files2 = seal.get("files2", {})
rules2 = seal.get("rules2", {})
omit = [re.compile(pattern) for pattern, rule in rules2.items() if isinstance(rule, dict) and rule.get("omit")]
FAT_MAGICS = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
CPU = {7: "i386", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64"}
counts = dict.fromkeys(("seal", "link", "hardlink", "fat", "name"), 0)


def report(kind: str, line: str) -> None:
    counts[kind] += 1
    if counts[kind] <= 40:
        print(line)


def sha256(path: str) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.digest()


for rel, entry in sorted(files2.items()):
    path = os.path.join(contents, rel)
    if isinstance(entry, dict) and "symlink" in entry:
        if not os.path.islink(path) or os.readlink(path) != entry["symlink"]:
            report("seal", f"seal_symlink_mismatch {rel}")
        continue
    if isinstance(entry, dict) and ("cdhash" in entry or "requirement" in entry):
        if not os.path.exists(path):
            report("seal", f"seal_nested_missing {rel}")
        continue
    if not os.path.isfile(path) or os.path.islink(path):
        report("seal", f"seal_missing {rel}")
        continue
    expected = entry.get("hash2") if isinstance(entry, dict) else None
    if expected is not None and sha256(path) != bytes(expected):
        report("seal", f"seal_hash_mismatch {rel}")

seen = {}
for dirpath, dirnames, filenames in os.walk(app):
    for name in dirnames + filenames:
        path = os.path.join(dirpath, name)
        rel_app = os.path.relpath(path, app).replace(os.sep, "/")
        if not name.isascii():
            nfc = unicodedata.normalize("NFC", name) == name
            report("name", f"non_ascii {rel_app!r} nfc={nfc}")
        key = path.casefold()
        if key in seen and seen[key] != rel_app:
            report("name", f"case_collision {seen[key]!r} {rel_app!r}")
        seen[key] = rel_app
        if os.path.islink(path):
            target = os.readlink(path)
            resolved = os.path.realpath(path)
            flags = []
            if os.path.isabs(target):
                flags.append("ABSOLUTE")
            if not (resolved == root or resolved.startswith(root + os.sep)):
                flags.append("ESCAPES_BUNDLE")
            if not os.path.exists(path):
                flags.append("DANGLING")
            if flags:
                report("link", f"symlink {rel_app} -> {target} {' '.join(flags)}")
            continue
        if name in filenames and os.path.isfile(path):
            info = os.lstat(path)
            if info.st_nlink > 1:
                report("hardlink", f"hardlink nlink={info.st_nlink} {rel_app}")
            with open(path, "rb") as handle:
                header = handle.read(8)
            if header[:4] in FAT_MAGICS:
                (nfat,) = struct.unpack(">I", header[4:8])
                if 0 < nfat < 8:
                    with open(path, "rb") as handle:
                        handle.seek(8)
                        archs = []
                        for _ in range(nfat):
                            cputype = struct.unpack(">i", handle.read(4))[0] & 0xFFFFFFFF
                            handle.read(16 if header[3] == 0xBE else 28)
                            archs.append(CPU.get(cputype, hex(cputype)))
                    report("fat", f"fat_macho {rel_app}: {' '.join(archs)}")
        if rel_app.startswith("Contents/") and name in filenames and not os.path.islink(path):
            rel = rel_app[len("Contents/"):]
            if rel in files2 or rel.startswith("_CodeSignature/") or rel in ("MacOS/modulo", "Info.plist", "PkgInfo"):
                continue
            if any(pattern.search(rel) for pattern in omit):
                continue
            report("seal", f"seal_unsealed {rel}")

print(f"seal_entries={len(files2)} seal_problems={counts['seal']}")
print(f"symlink_problems={counts['link']} hardlinks={counts['hardlink']} fat_macho={counts['fat']} name_problems={counts['name']}")
PY
run_bounded 300 python3 -I "$work_dir/scan.py" "$app"
printf 'bundle_scan_exit=%s\n' "$?"

section "files carrying extended attributes (other than code signatures)"
run_bounded 120 xattr -r -l "$app" >"$work_dir/xattr.log" 2>/dev/null
grep -v 'com.apple.cs.Code' "$work_dir/xattr.log" | head -n 40

if [[ -n "$reference" && -d "$reference" ]]; then
  section "comparison with reference $reference"
  run_bounded 300 diff -rq "$reference" "$app" >"$work_dir/diff.log" 2>&1
  printf 'diff_exit=%s\n' "$?"
  head -n 60 "$work_dir/diff.log"
fi

section "end"
exit 0
