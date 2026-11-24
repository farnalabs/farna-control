#!/usr/bin/env bash
# Install a signed, notarized Farna Control native .dmg for the current macOS user.
# The app bundle owns the service contract; this script only verifies/copies it.
#
# Usage: install.sh DMG_PATH [--no-telemetry-prompt]
#
# Product analytics (FAR-2741): the first-run install asks ONCE whether to send
# anonymous aggregate analytics, defaulting to OFF. Accepting writes the three
# FARNA_CONTROL_PRODUCT_ANALYTICS_* settings into the native config file
# (<data dir>/config.env, mode 600); declining writes nothing at all, so "no
# telemetry by default" stays true. The prompt is skipped when stdin is not a
# terminal or --no-telemetry-prompt is passed, so scripted installs never block.
set -euo pipefail

DMG=""
NO_TELEMETRY_PROMPT=0
PRODUCT_ANALYTICS_ENDPOINT_URL="https://telemetry.farna.dev"
for argument in "$@"; do
  case "$argument" in
    --no-telemetry-prompt) NO_TELEMETRY_PROMPT=1 ;;
    -*) printf 'install.sh: unknown option: %s\n' "$argument" >&2; exit 1 ;;
    *)
      if [[ -n "$DMG" ]]; then
        printf 'usage: install.sh DMG_PATH [--no-telemetry-prompt]\n' >&2
        exit 1
      fi
      DMG="$argument"
      ;;
  esac
done
[[ -n "$DMG" ]] || { printf 'usage: install.sh DMG_PATH [--no-telemetry-prompt]\n' >&2; exit 1; }
# Defensive: normalize path by stripping any duplicate .dmg extension (guard against .dmg.dmg bug)
while [[ "$DMG" == *.dmg.dmg ]]; do
  DMG="${DMG%.dmg}"
  printf 'install.sh: normalized duplicate .dmg extension -> %s\n' "$DMG" >&2
done
[[ -f "$DMG" ]] || { echo "ERROR: DMG not found: $DMG" >&2; exit 1; }
[[ "$(uname -s)" == Darwin ]] || { echo 'ERROR: this installer requires macOS' >&2; exit 1; }
HOST_ARCH="$(uname -m)"
case "$HOST_ARCH" in
  arm64|x86_64) ;;
  *) echo "ERROR: unsupported macOS architecture: $HOST_ARCH" >&2; exit 1 ;;
esac
case "$(basename "$DMG")" in
  *-macos-arm64.dmg) EXPECTED_ARCH=arm64 ;;
  *-macos-x86_64.dmg) EXPECTED_ARCH=x86_64 ;;
  *) EXPECTED_ARCH='' ;;
esac
if [[ -n "$EXPECTED_ARCH" && "$HOST_ARCH" != "$EXPECTED_ARCH" ]]; then
  echo "ERROR: native installer filename architecture mismatch: host=$HOST_ARCH artifact=$EXPECTED_ARCH" >&2
  exit 1
fi
MOUNT=$(mktemp -d)
MOUNT_ATTACHED=0
cleanup() {
  local status=$?
  set +e
  if [[ "$MOUNT_ATTACHED" -eq 1 ]]; then
    detach_status=1
    for _attempt in 1 2 3; do
      if hdiutil detach "$MOUNT" >/dev/null 2>&1; then
        detach_status=0
      fi
      if [[ "$detach_status" -eq 0 ]] && ! hdiutil info | grep -Fq "${MOUNT}"; then
        break
      fi
      sleep 1
    done
    if [[ "$detach_status" -ne 0 ]] || hdiutil info | grep -Fq "${MOUNT}"; then
      echo "ERROR: cleanup could not detach mounted volume ${MOUNT}" >&2
      [[ "$status" -eq 0 ]] && status=1
    fi
  fi
  rmdir "$MOUNT" 2>/dev/null || {
    echo "ERROR: cleanup could not remove temporary mount directory ${MOUNT}" >&2
    [[ "$status" -eq 0 ]] && status=1
  }
  return "$status"
}
trap cleanup EXIT
hdiutil verify "$DMG" >/dev/null
codesign --verify --verbose=2 "$DMG"
hdiutil attach "$DMG" -mountpoint "$MOUNT" -nobrowse -readonly >/dev/null
MOUNT_ATTACHED=1
# The mounted volume ships "Farna Control.app", but the DMG itself may be
# renamed (the installer accepts any DMG path, including one with spaces), so
# derive APP from the bundle actually present on the volume rather than from
# the DMG filename.
app_count=0
APP=''
while IFS= read -r candidate; do
  APP="$candidate"
  app_count=$((app_count + 1))
done < <(for candidate in "$MOUNT"/*.app; do [[ -d "$candidate" && ! -L "$candidate" ]] && printf '%s\n' "$candidate"; done)
[[ "$app_count" -eq 1 ]] || { echo "ERROR: expected exactly one non-symlink .app bundle on the DMG, found ${app_count}" >&2; exit 1; }
[[ -d "$APP" && ! -L "$APP" ]] || { echo 'ERROR: DMG does not contain the expected non-symlink .app bundle' >&2; exit 1; }
codesign --verify --deep --strict "$APP"
LAUNCHER="$APP/Contents/MacOS/modulo"
[[ -f "$LAUNCHER" && ! -L "$LAUNCHER" ]] || { echo "ERROR: app bundle is missing its native launcher: $LAUNCHER" >&2; exit 1; }
LAUNCHER_FILE_OUTPUT="$(file "$LAUNCHER" 2>&1)"
case "$LAUNCHER_FILE_OUTPUT" in
  *'Mach-O 64-bit executable arm64'*) APP_ARCH=arm64 ;;
  *'Mach-O 64-bit executable x86_64'*) APP_ARCH=x86_64 ;;
  *) echo "ERROR: app launcher is not a supported 64-bit Mach-O executable: $LAUNCHER_FILE_OUTPUT" >&2; exit 1 ;;
esac
[[ "$HOST_ARCH" == "$APP_ARCH" ]] || {
  echo "ERROR: mounted app architecture mismatch: host=$HOST_ARCH app=$APP_ARCH" >&2
  exit 1
}
if [[ "${FARNA_CONTROL_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET:-}" == '1' && "${FARNA_CONTROL_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET_REASON:-}" == 'apple_cloudkit_ticket_unavailable' ]]; then
  printf '%s\n' '::warning::Rehearsal-only: deferring mounted-app Gatekeeper assessment because the Apple CloudKit notarization ticket is unavailable.' >&2
elif [[ "${FARNA_CONTROL_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET:-}" == '1' && "${FARNA_CONTROL_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET_REASON:-}" == 'apple_notarization_pending' ]]; then
  # FAR-2577: Apple is still processing the submission, so no ticket exists yet.
  printf '%s\n' '::warning::Rehearsal-only: deferring mounted-app Gatekeeper assessment because Apple notarization is still pending.' >&2
else
  spctl --assess --type execute "$APP"
fi
INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT:-$HOME/Library/Application Support/Modulo}"
mkdir -p "$INSTALL_ROOT"
TARGET="$INSTALL_ROOT/$(basename "$APP")"
rm -rf "$TARGET"
cp -R "$APP" "$TARGET"
# Verify the copied, relocatable app—not the mounted source bundle—can import
# the supervisor with the same clean environment used by launchd children.
env -u PYTHONPATH -u PYTHONHOME \
  "$TARGET/Contents/Resources/bundle/runtime/bin/python3" \
  -c 'import farna.launcher.supervisor'
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/run.modulo.launcher.plist"
DATA_DIR="${FARNA_CONTROL_DATA_DIR:-$HOME/Library/Application Support/Modulo/data}"

configure_product_analytics() {
  # Ask ONCE (first run, interactive terminal) whether to send anonymous
  # aggregate analytics. Default OFF; decline writes nothing.
  local config_path existing endpoint secret enabled answer instance_secret
  config_path="$DATA_DIR/config.env"
  if [[ -n "${FARNA_CONTROL_PRODUCT_ANALYTICS_ENDPOINT_URL:-}" \
     || -n "${FARNA_CONTROL_PRODUCT_ANALYTICS_INSTANCE_SECRET:-}" \
     || -n "${FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED:-}" ]]; then
    printf 'product analytics: configured through the environment; leaving %s unchanged\n' "$config_path"
    return 0
  fi
  existing=""
  if [[ -f "$config_path" ]]; then
    existing="$(cat "$config_path")"
  fi
  endpoint="$(printf '%s\n' "$existing" | sed -n 's/^FARNA_CONTROL_PRODUCT_ANALYTICS_ENDPOINT_URL=//p' | head -n 1)"
  secret="$(printf '%s\n' "$existing" | sed -n 's/^FARNA_CONTROL_PRODUCT_ANALYTICS_INSTANCE_SECRET=//p' | head -n 1)"
  enabled="$(printf '%s\n' "$existing" | sed -n 's/^FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED=//p' | head -n 1)"
  if [[ -n "$endpoint" || -n "$secret" || -n "$enabled" ]] || [[ -e "$config_path" ]]; then
    if [[ -n "$endpoint$secret$enabled" ]]; then
      printf 'product analytics: keeping the existing opt-in settings in %s\n' "$config_path"
    fi
    return 0
  fi
  if [[ "$NO_TELEMETRY_PROMPT" -eq 1 ]]; then
    printf 'product analytics: off (--no-telemetry-prompt; the default)\n'
    return 0
  fi
  if [[ ! -t 0 ]]; then
    printf 'product analytics: off (non-interactive install; the default)\n'
    return 0
  fi

  printf '\nFarna Control can send anonymous product analytics to Farnalabs.\n'
  printf 'It is OFF unless you choose to turn it on now. You can turn it off\n'
  printf 'later in the app (Administration, Product analytics); turning it on\n'
  printf 'after declining requires updating the instance configuration.\n\n'
  printf 'If you turn it on, once a day this instance sends aggregate counts only:\n'
  printf '  - how many organisations, pipelines, agents, schemas, teams, model\n'
  printf '    backends, connector instances and triggers exist\n'
  printf '  - run statistics (counts, durations and outcomes)\n'
  printf '  - counts of error families\n'
  printf '  - an inventory of which provider and connector types are in use\n'
  printf 'No prompts, pipeline outputs, credentials or customer content are sent.\n'
  printf 'It goes to the Farnalabs telemetry service at %s.\n\n' "$PRODUCT_ANALYTICS_ENDPOINT_URL"
  printf 'Send anonymous aggregate product analytics? [y/N] '
  answer=""
  read -r answer || answer=""
  case "$answer" in
    [Yy] | [Yy][Ee][Ss]) ;;
    *)
      printf 'product analytics: off (the default)\n'
      return 0
      ;;
  esac

  instance_secret=""
  if command -v openssl >/dev/null 2>&1; then
    instance_secret="$(openssl rand -hex 32)" || instance_secret=""
  fi
  [[ -n "$instance_secret" ]] || { printf 'ERROR: could not generate the product-analytics instance secret with openssl\n' >&2; exit 1; }
  mkdir -p "$DATA_DIR"
  chmod 700 "$DATA_DIR"
  ( umask 077
    {
      printf 'FARNA_CONTROL_PRODUCT_ANALYTICS_ENDPOINT_URL=%s\n' "$PRODUCT_ANALYTICS_ENDPOINT_URL"
      printf 'FARNA_CONTROL_PRODUCT_ANALYTICS_INSTANCE_SECRET=%s\n' "$instance_secret"
      printf 'FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED=true\n'
    } > "$config_path"
  )
  chmod 600 "$config_path"
  printf 'product analytics: on, recorded in %s (mode 600; never commit this file)\n' "$config_path"
}

configure_product_analytics
PYTHONPATH="$TARGET/Contents/Resources/bundle/backend/src:$TARGET/Contents/Resources/bundle/backend/.venv/lib/python3.12/site-packages" \
  "$TARGET/Contents/Resources/bundle/runtime/bin/python3" - "$TARGET" "$PLIST" "$DATA_DIR" <<'PY'
import sys
from pathlib import Path
from farna.launcher.service import render_launchd_plist
app = Path(sys.argv[1])
plist_path = Path(sys.argv[2])
data_dir = Path(sys.argv[3])
plist_path.write_text(render_launchd_plist(app / 'Contents/MacOS/modulo', data_dir=data_dir), encoding='utf-8')
PY
launchctl unload -w "$PLIST" >/dev/null 2>&1 || true
launchctl load -w "$PLIST"
printf 'Farna Control installed at %s\n' "$TARGET"
