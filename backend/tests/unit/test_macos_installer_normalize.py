"""FAR-2327: the macOS installer normalize step must honour reusable-workflow call inputs
even when the caller (release-unified) was itself started by workflow_dispatch."""

import hashlib
import http.server
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest
import yaml

from farna.launcher import entry as entry_module
from farna.launcher import env_contract
from farna.launcher import upgrade as upgrade_module
from farna.launcher.platform import binary_name

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_bash(script: str, *, cwd: Path, env: dict[str, str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    bash = r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute workflow scripts")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _macos_normalize_script() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["normalize-inputs"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "normalize")
    assert isinstance(run, str)
    return run


def _run_macos_normalize(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CALL_", "DISPATCH_", "GITHUB_"))}
    # Actions always defines every mapped env var (empty when unset), so default them all to "".
    for name in ("VERSION", "RC_TAG", "REF", "MODE"):
        env[f"CALL_{name}"] = ""
        env[f"DISPATCH_{name}"] = ""
    env["DISPATCH_BUILD_NATIVE"] = ""
    env.update({"GITHUB_OUTPUT": str(out), "GITHUB_REF": "refs/heads/main"})
    env.update(overrides)
    env["WSLENV"] = ":".join(name for name in env if name.startswith(("CALL_", "DISPATCH_", "GITHUB_")))
    result = _run_bash(_macos_normalize_script(), cwd=tmp_path, env=env)

    outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
    return result, outputs


def test_macos_normalize_uses_call_inputs_when_caller_was_dispatched(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="v0.3.324-rc5",
        CALL_REF="v0.3.324-rc5",
        CALL_MODE="recut",
        DISPATCH_VERSION="",
        DISPATCH_RC_TAG="",
        DISPATCH_REF="",
        DISPATCH_MODE="",
        DISPATCH_BUILD_NATIVE="",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["rc_tag"] == "v0.3.324-rc5"
    assert outputs["mode"] == "recut"
    assert outputs["run_native_build"] == "true"


def test_macos_normalize_direct_dispatch_dry_run_still_works(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="manual",
        CALL_MODE="dry-run",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="dry-run",
        DISPATCH_BUILD_NATIVE="true",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["ref"] == "refs/heads/main"
    assert outputs["run_native_build"] == "true"


def _native_installer_step() -> tuple[dict[str, object], str]:
    workflow_path = REPO_ROOT / ".github" / "workflows" / "macos-installer.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    step = next(step for step in steps if step.get("name") == "Sign, notarize and smoke-test native installer")
    run = step.get("run")
    assert isinstance(run, str)
    return step, run


def _line_numbers(script: str, pattern: str) -> list[int]:
    return [index for index, line in enumerate(script.splitlines()) if re.search(pattern, line)]


def test_macos_finder_heredoc_terminator_is_at_column_zero_after_yaml_parsing() -> None:
    """Keep YAML parsing from indenting bash's AppleScript heredoc terminator."""
    _step, build_run = _native_installer_step()
    lines = build_run.splitlines()
    opener_index = next(index for index, line in enumerate(lines) if "<<'APPLESCRIPT'" in line)
    terminator_indexes = [index for index, line in enumerate(lines) if line.strip() == "APPLESCRIPT"]

    assert len(terminator_indexes) == 1
    terminator_index = terminator_indexes[0]
    assert terminator_index > opener_index
    assert lines[terminator_index] == "APPLESCRIPT"


def test_macos_dmg_contract_structurally_verifies_staging_and_post_mount_gates() -> None:
    """Verify the DMG workflow contract without attempting a macOS DMG build on Windows."""
    step, build_run = _native_installer_step()
    plist_script = (REPO_ROOT / "scripts" / "create-macos-info-plist.sh").read_text(encoding="utf-8")
    bundle_script = (REPO_ROOT / "scripts" / "macos" / "build-native-bundle.sh").read_text(encoding="utf-8")
    icon_path = REPO_ROOT / "scripts" / "macos" / "assets" / "Modulo.svg"
    lines = build_run.splitlines()

    assert step["if"] == "${{ env.RUN_NATIVE_BUILD == 'true' }}"
    assert icon_path.is_file()
    assert icon_path.stat().st_size > 0
    assert "CFBundleIconFile</key><string>Modulo.icns" in plist_script
    assert "CFBundleIdentifier</key><string>dev.farna.control<" in plist_script
    assert "CFBundleDisplayName</key><string>Farna Control<" in plist_script
    assert "iconutil --convert icns" in bundle_script

    staging_start = next(index for index, line in enumerate(lines) if 'mkdir -p "$DMG_STAGING"' in line)
    dmg_create = next(index for index, line in enumerate(lines) if "hdiutil_with_resource_busy_retry dmg_create" in line)
    hard_staging_checks = {
        "app": 'test -d "$DMG_STAGING/Farna Control.app"',
        "alias": 'test -L "$DMG_STAGING/Applications"',
    }
    for check in hard_staging_checks.values():
        check_index = next(index for index, line in enumerate(lines) if line.strip() == check)
        assert staging_start < check_index < dmg_create
    alias_create = next(index for index, line in enumerate(lines) if line.strip() == 'ln -s /Applications "$DMG_STAGING/Applications"')
    alias_check = next(index for index, line in enumerate(lines) if line.strip() == hard_staging_checks["alias"])
    assert alias_create < alias_check

    osascript_index = next(index for index, line in enumerate(lines) if 'osascript - "$DMG_STAGING"' in line)
    assert osascript_index > staging_start
    assert any("finder_layout_exit=$?" in line for line in lines[osascript_index:])
    assert any('if [[ "$finder_layout_exit" -eq 0' in line for line in lines[osascript_index:])
    assert any('echo "::warning::Finder automation unavailable' in line for line in lines[osascript_index:])
    assert any("Finder automation completed but produced no .DS_Store" in line for line in lines[osascript_index:])
    assert not any("exit 1" in line for line in lines[osascript_index:dmg_create])

    mounted_checks = {
        "app": r"run_pre_smoke mounted_dmg_app bash -c",
        "alias": r"run_pre_smoke mounted_dmg_applications_alias bash -c",
        "icon": r"run_pre_smoke mounted_dmg_icon bash -c",
        "plist": r"run_pre_smoke mounted_dmg_plist_icon bash -c",
    }
    attach_index = next(index for index, line in enumerate(lines) if "hdiutil_with_resource_busy_retry dmg_attach" in line)
    for name, pattern in mounted_checks.items():
        matches = _line_numbers(build_run, pattern)
        assert len(matches) == 1, name
        assert attach_index < matches[0]
    assert '[[ -L "$1" && "$(readlink "$1")" == "/Applications" ]]' in build_run
    assert 'plutil -extract CFBundleIconFile raw -o - "$1" | grep -Fxq Modulo.icns' in build_run


def test_macos_dmg_contract_structurally_preserves_signing_notarization_and_gatekeeper_order() -> None:
    """Verify command order structurally; execution remains macOS-only."""
    _step, build_run = _native_installer_step()
    order = [
        "run_pre_smoke app_codesign codesign",
        "run_pre_smoke app_codesign_verify codesign",
        "hdiutil_with_resource_busy_retry dmg_create bash -c",
        "run_pre_smoke dmg_codesign codesign",
        "run_pre_smoke dmg_notary_submit bash scripts/macos/notarize-and-verify.sh",
        "run_pre_smoke dmg_codesign_verify codesign",
        "run_pre_smoke dmg_verify hdiutil verify",
        "hdiutil_with_resource_busy_retry dmg_attach hdiutil attach",
        "run_pre_smoke mounted_app_codesign codesign",
        "run_pre_smoke mounted_app_spctl spctl --assess --type execute",
    ]
    positions = [build_run.index(command) for command in order]
    assert positions == sorted(positions)
    assert "run_pre_smoke dmg_staple_validate xcrun stapler validate" in build_run
    assert "run_pre_smoke dmg_spctl spctl --assess --type open" in build_run
    assert "dmg_staple_ticket_unavailable=1" in build_run
    assert "DMG stapling failed for a non-propagation/non-network reason; refusing to defer" in build_run
    assert "Mounted-app codesign and Gatekeeper checks remain hard gates" in build_run


def test_macos_normalize_direct_dispatch_rejects_non_dry_run(tmp_path: Path) -> None:
    result, _outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="recut",
        DISPATCH_BUILD_NATIVE="false",
    )
    assert result.returncode != 0
    assert "unsupported manual dispatch mode" in result.stderr


def _install_sh_script() -> str:
    """Read the install.sh script content."""
    script_path = REPO_ROOT / "scripts" / "macos" / "install.sh"
    return script_path.read_text(encoding="utf-8")


def _run_install_sh(bogus_dmg_path: Path) -> subprocess.CompletedProcess[str]:
    """Run install.sh with a bogus DMG path and return the result."""
    script = _install_sh_script()

    bash = r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute install.sh")

    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned install script
        [bash, "--noprofile", "--norc", "-c", script + '\ninstall.sh "$1"', "install.sh", bogus_dmg_path.as_posix()],
        cwd=bogus_dmg_path.parent,
        env={**os.environ, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_install_sh_normalizes_duplicate_dmg_extension(tmp_path: Path) -> None:
    """Test that install.sh normalizes .dmg.dmg to .dmg and shows the normalized path in the error."""
    bogus_dmg = tmp_path / "foo.dmg.dmg"

    result = _run_install_sh(bogus_dmg)

    assert result.returncode != 0
    assert "install.sh: normalized duplicate .dmg extension -> " in result.stderr
    assert "foo.dmg" in result.stderr
    assert "ERROR: DMG not found" in result.stderr


# ---------------------------------------------------------------------------
# FAR-2478: the native smoke teardown must stop the Postgres/Redis child tree
# the installed launcher started, before asserting that none remain.
# ---------------------------------------------------------------------------

#: Marks a bash block that a test extracts verbatim from the workflow run step.
#: Both markers live in the workflow itself, so the executable regression test
#: runs the exact teardown/poll code the macOS job runs.
_MARKER_TEMPLATE = "# {edge} {marker}"

#: A stand-in for a launcher child that ignores SIGTERM. Postgres answers
#: SIGTERM with a "smart" shutdown that waits for connected clients forever, so
#: a TERM-only teardown leaves it alive; only the SIGKILL escalation stops it.
_TERM_IGNORING_HOLDER = '$SIG{TERM}="IGNORE"; sleep 300;\n'


def _bash_executable() -> str:
    """Resolve bash without silently skipping the regression when it is absent."""
    bash = r"C:\Program Files\Git\bin\bash.exe"
    if Path(bash).is_file():
        return bash
    resolved = shutil.which("bash")
    if resolved is None:
        pytest.skip("bash is required to execute workflow scripts")
    return resolved


def _extract_marked_bash(script: str, marker: str) -> str:
    """Return the marked region (markers included) from a workflow run block."""
    begin = _MARKER_TEMPLATE.format(edge="BEGIN", marker=marker)
    end = _MARKER_TEMPLATE.format(edge="END", marker=marker)
    start = script.index(begin)
    finish = script.index(end, start) + len(end)
    return script[start:finish]


def _run_bash_file(
    script_path: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a repository-owned bash script (written under tmp_path) to completion."""
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [_bash_executable(), "--noprofile", "--norc", script_path.as_posix(), *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_macos_teardown_stops_owned_term_ignoring_processes(tmp_path: Path) -> None:
    """FAR-2478: the teardown fully stops owned Postgres/Redis children.

    Both owned holders ignore SIGTERM (as Postgres' smart shutdown effectively
    does when a client is connected), so this only passes when the teardown
    escalates to SIGKILL. The unowned control process must survive.
    """
    _step, build_run = _native_installer_step()
    teardown = _extract_marked_bash(build_run, "stop_owned_processes")

    data_dir = tmp_path / "modulo-native-smoke-data"
    (data_dir / "pgdata").mkdir(parents=True)
    (data_dir / "redis").mkdir()
    holder = tmp_path / "term_ignoring_holder.pl"
    holder.write_text(_TERM_IGNORING_HOLDER, encoding="utf-8")

    driver = tmp_path / "teardown_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + teardown
        + """
data_dir="$MODULO_TEST_DATA_DIR"
pid_file="$MODULO_TEST_PIDFILE"
holder="$MODULO_TEST_HOLDER"
perl "$holder" "$data_dir/pgdata" &
pg_pid=$!
perl "$holder" "$data_dir/redis/dump.rdb" &
redis_pid=$!
printf '%s\\n%s\\n' "$pg_pid" "$redis_pid" > "$pid_file"
perl "$holder" "unrelated-control" &
control_pid=$!
status=0
stop_owned_processes "$pid_file" "$data_dir" 0 || status=$?
for pid in "$pg_pid" "$redis_pid"; do
  if owned_pid_is_alive "$pid"; then
    printf 'owned process survived teardown: pid=%s\\n' "$pid" >&2
    status=1
  fi
done
if owned_pid_is_alive "$control_pid"; then
  :
else
  printf 'unowned control process was killed: pid=%s\\n' "$control_pid" >&2
  status=1
fi
kill -KILL "$pg_pid" "$redis_pid" "$control_pid" 2>/dev/null || true
wait "$pg_pid" 2>/dev/null || true
wait "$redis_pid" 2>/dev/null || true
wait "$control_pid" 2>/dev/null || true
exit "$status"
""",
        encoding="utf-8",
    )

    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env={
            "MODULO_TEST_DATA_DIR": data_dir.as_posix(),
            "MODULO_TEST_PIDFILE": (tmp_path / "owned-pids").as_posix(),
            "MODULO_TEST_HOLDER": holder.as_posix(),
        },
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "owned process survived teardown" not in result.stderr
    assert "unowned control process was killed" not in result.stderr


def test_macos_teardown_runs_before_the_no_owned_postgres_redis_gate() -> None:
    """FAR-2478: teardown must execute BEFORE the assertion it makes passable."""
    _step, build_run = _native_installer_step()
    teardown_index = build_run.index('run_assertion "stop_owned_processes"')
    assertion_index = build_run.index('run_assertion "no_owned_postgres_redis"')
    assert teardown_index < assertion_index
    assert 'stop_owned_processes "$OWNED_PIDS" "$DATA" 5 || exit 1' in build_run
    # The end-state assertion stays a hard gate.
    assert 'run_assertion "no_owned_postgres_redis"' in build_run


# ---------------------------------------------------------------------------
# FAR-2479: spa_root_html must wait for the SPA entry document, never accept
# the API root, and the Intel Homebrew withdrawal must be explicit.
# ---------------------------------------------------------------------------


def _serve_body(body: str, content_type: str) -> tuple[http.server.HTTPServer, threading.Thread, str]:
    """Serve one fixed response on 127.0.0.1 (real HTTP, no mocking)."""
    payload = body.encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/"


def _stop_server(server: http.server.HTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    thread.join(timeout=10)
    server.server_close()


def _spa_poller_script(tmp_path: Path) -> Path:
    _step, build_run = _native_installer_step()
    poller = _extract_marked_bash(build_run, "spa_root_html_ready")
    script = tmp_path / "spa_root_html_ready.sh"
    script.write_text(poller + '\nspa_root_html_ready "$@"\n', encoding="utf-8")
    return script


def test_macos_spa_root_html_rejects_the_api_root(tmp_path: Path) -> None:
    """FAR-2479: the API root body (`{"status":"ok"}`) must never satisfy the gate."""
    script = _spa_poller_script(tmp_path)
    server, thread, url = _serve_body('{"status":"ok"}', "application/json")
    try:
        result = _run_bash_file(script, [url, "5", "0"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert "is not the SPA entry document" in result.stderr
    assert "SPA entry document not served" in result.stderr


def test_macos_spa_root_html_accepts_a_served_spa_entry(tmp_path: Path) -> None:
    """FAR-2479: the gate passes once the SPA entry document is actually served."""
    script = _spa_poller_script(tmp_path)
    server, thread, url = _serve_body('<!doctype html><html><body><div id="app"></div></body></html>', "text/html")
    try:
        result = _run_bash_file(script, [url, "5", "0"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, result.stderr
    assert "served after" in result.stdout


def test_macos_spa_root_gate_uses_the_readiness_poller() -> None:
    """FAR-2479: the workflow gate calls the bounded poller, not a single curl."""
    _step, build_run = _native_installer_step()
    assert 'spa_root_html_ready "http://127.0.0.1:${API_PORT}/" 60 1 || exit 1' in build_run
    assert "# BEGIN spa_root_html_ready" in build_run


def _build_tools_step() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    step = next(step for step in steps if step.get("name") == "Install build tools")
    run = step.get("run")
    assert isinstance(run, str)
    return run


def test_macos_intel_homebrew_withdrawal_is_explicit_and_actionable() -> None:
    """FAR-2479: the Intel build must state why it no longer uses Homebrew."""
    run = _build_tools_step()
    assert "Intel (x86_64) macOS is a Homebrew Tier-3 configuration with no bottles" in run
    assert "FAR-2486" in run
    assert 'test "$(uname -m)" =' in run


# ---------------------------------------------------------------------------
# FAR-2486: the Intel macOS installer must not depend on Homebrew at all.
# The only Homebrew-installed build tool was shellcheck, so the Intel branch
# installs a pinned, SHA-256-verified upstream release instead. arm64 keeps the
# existing Homebrew path unchanged.
# ---------------------------------------------------------------------------

_X86_64_SHELLCHECK_SHA256 = "c2c15e08df0e8fbc374c335b230a7ee958c313fa5714817a59aa59f1aa594f51"
_ARM64_SHELLCHECK_SHA256 = "339b930feb1ea764467013cc1f72d09cd6b869ebf1013296ba9055ab2ffbd26f"


def test_macos_intel_build_tools_use_pinned_download_and_never_homebrew() -> None:
    """FAR-2486: the x86_64 branch installs a pinned release, not a brew formula."""
    run = _build_tools_step()
    lines = [line.strip() for line in run.splitlines()]

    x86_branch = next(index for index, line in enumerate(lines) if 'if [[ "$MACOS_ARCH" == x86_64 ]]; then' in line)
    arm64_else = next(index for index, line in enumerate(lines) if line == "else" and index > x86_branch)
    intel_branch = "\n".join(lines[x86_branch:arm64_else])

    assert "bash scripts/macos/install-pinned-shellcheck.sh" in intel_branch
    assert '"$MACOS_ARCH" "$SHELLCHECK_BIN_DIR"' in intel_branch
    assert "$GITHUB_PATH" in intel_branch
    assert "brew install" not in intel_branch
    assert "brew update" not in intel_branch


def test_macos_arm64_build_tools_keep_the_homebrew_path() -> None:
    """FAR-2486: arm64 behaviour is unchanged - Homebrew still installs shellcheck."""
    run = _build_tools_step()
    arm64_section = run.split("else\n", 2)[1]
    assert "brew update" in arm64_section
    assert "brew install shellcheck" in arm64_section


def _pinned_shellcheck_script() -> str:
    script_path = REPO_ROOT / "scripts" / "macos" / "install-pinned-shellcheck.sh"
    assert script_path.is_file()
    return script_path.read_text(encoding="utf-8")


def _pinned_shellcheck_case_arm(script: str, arch: str) -> str:
    """Return the body of the `<arch>)` arm of the script's architecture case."""
    lines = script.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == f"{arch})")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == ";;")
    return "\n".join(lines[start:end])


def _sha256_literal(text: str) -> str:
    """Return the single SHA256="..." literal inside a case arm."""
    match = re.search(r'SHA256="([0-9a-f]{64})"', text)
    assert match is not None, f"no SHA256 literal in arm:\n{text}"
    return match.group(1)


def test_pinned_shellcheck_manifest_pins_a_verified_asset_per_architecture() -> None:
    """FAR-2486: each architecture resolves a pinned tarball and a pinned SHA-256."""
    script = _pinned_shellcheck_script()
    assert "https://github.com/koalaman/shellcheck/releases/download" in script
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.x86_64.tar.gz" in script
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.aarch64.tar.gz" in script
    assert _X86_64_SHELLCHECK_SHA256 in script
    assert _ARM64_SHELLCHECK_SHA256 in script
    assert _X86_64_SHELLCHECK_SHA256 != _ARM64_SHELLCHECK_SHA256
    assert "set -euo pipefail" in script
    # The transport stays HTTPS-only by default; nothing here may downgrade it.
    assert "${SHELLCHECK_DOWNLOAD_PROTOCOLS:-https}" in script


def test_pinned_shellcheck_binds_each_digest_to_its_architecture() -> None:
    """FAR-2486: each case arm must carry ITS OWN digest and tarball.

    The manifest test above only proves both digests appear somewhere in the
    file. Swapping the two values between the arms still satisfies that, yet in
    production it downloads the genuine x86_64 tarball, compares it against the
    aarch64 digest, fails verification, and makes the Intel job red on every run
    - exactly the failure FAR-2486 exists to remove. Bind the digest to the arm
    that selects the tarball, not merely to the file.
    """
    script = _pinned_shellcheck_script()
    x86_arm = _pinned_shellcheck_case_arm(script, "x86_64")
    arm_arm = _pinned_shellcheck_case_arm(script, "arm64")

    assert _sha256_literal(x86_arm) == _X86_64_SHELLCHECK_SHA256
    assert _sha256_literal(arm_arm) == _ARM64_SHELLCHECK_SHA256
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.x86_64.tar.gz" in x86_arm
    assert "shellcheck-${SHELLCHECK_VERSION}.darwin.aarch64.tar.gz" in arm_arm


def _pinned_shellcheck_driver(tmp_path: Path) -> Path:
    """Write a driver that runs the workflow script's verify+install block verbatim."""
    script = _pinned_shellcheck_script()
    marked = _extract_marked_bash(script, "shellcheck_verify_install")
    driver = tmp_path / "shellcheck_verify_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        + marked
        + """
status=0
verify_and_install_shellcheck "$1" "$2" "$3" "v0.11.0" "$4" || status=$?
exit "$status"
""",
        encoding="utf-8",
    )
    return driver


def _write_shellcheck_tarball(path: Path, payload: str) -> str:
    """Create a release-shaped tar.gz containing shellcheck-v0.11.0/shellcheck."""
    data = payload.encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("shellcheck-v0.11.0/shellcheck")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_pinned_shellcheck_installs_only_a_checksum_verified_archive(tmp_path: Path) -> None:
    """FAR-2486: a matching digest installs the binary and runs shellcheck --version."""
    driver = _pinned_shellcheck_driver(tmp_path)
    tarball = tmp_path / "shellcheck-v0.11.0.darwin.x86_64.tar.gz"
    digest = _write_shellcheck_tarball(
        tarball,
        "#!/usr/bin/env bash\nprintf 'ShellCheck - shell script analysis tool\\nversion: 0.11.0\\n'\n",
    )
    prefix = tmp_path / "install"

    # Relative paths: GNU tar treats "C:/..." in `-C` as a remote host spec.
    result = _run_bash_file(
        driver,
        [tarball.name, digest, "install", "work"],
        cwd=tmp_path,
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert (prefix / "shellcheck").is_file()
    assert "verified sha256 of" in result.stderr
    assert "version: 0.11.0" in result.stderr


def test_pinned_shellcheck_refuses_a_tampered_archive(tmp_path: Path) -> None:
    """FAR-2486: a digest mismatch fails closed and installs nothing."""
    driver = _pinned_shellcheck_driver(tmp_path)
    tarball = tmp_path / "shellcheck-v0.11.0.darwin.x86_64.tar.gz"
    digest = _write_shellcheck_tarball(tarball, "#!/usr/bin/env bash\nexit 0\n")
    tarball.write_bytes(tarball.read_bytes() + b"tampered")
    prefix = tmp_path / "install"

    result = _run_bash_file(
        driver,
        [tarball.as_posix(), digest, prefix.as_posix(), (tmp_path / "work").as_posix()],
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "checksum verification failed" in result.stderr
    assert not (prefix / "shellcheck").exists()


def test_pinned_shellcheck_refuses_a_missing_archive(tmp_path: Path) -> None:
    """FAR-2486: an absent download is a hard failure, never a skipped check."""
    driver = _pinned_shellcheck_driver(tmp_path)
    prefix = tmp_path / "install"

    result = _run_bash_file(
        driver,
        [
            (tmp_path / "absent.tar.gz").as_posix(),
            _X86_64_SHELLCHECK_SHA256,
            prefix.as_posix(),
            (tmp_path / "work").as_posix(),
        ],
        cwd=tmp_path,
    )

    assert result.returncode != 0
    assert "missing or empty" in result.stderr
    assert not prefix.exists()


def _pinned_shellcheck_download_driver(tmp_path: Path) -> Path:
    """Write a driver that runs the workflow script's download_verified block verbatim."""
    script = _pinned_shellcheck_script()
    marked = _extract_marked_bash(script, "download_verified")
    driver = tmp_path / "shellcheck_download_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        # The loopback fixture is plain HTTP; production keeps the HTTPS default.
        + "SHELLCHECK_DOWNLOAD_PROTOCOLS=http\n"
        + "SHELLCHECK_MAX_ATTEMPTS=5\n"
        + marked
        + """
status=0
download_verified "$1" "$2" "$3" || status=$?
exit "$status"
""",
        encoding="utf-8",
    )
    return driver


def _serve_status_sequence(statuses: list[int], body: bytes = b"verified-asset") -> tuple[http.server.HTTPServer, threading.Thread, str, dict[str, int]]:
    """Serve the given status codes in order (the last repeats) and count requests."""
    lock = threading.Lock()
    counter = {"requests": 0}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with lock:
                index = counter["requests"]
                counter["requests"] += 1
            status = statuses[index] if index < len(statuses) else statuses[-1]
            if status == 200:
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}/shellcheck.tar.gz", counter


def test_pinned_shellcheck_download_retries_transient_http_then_succeeds(tmp_path: Path) -> None:
    """FAR-2486: 503/503/200 is retried, then the asset is saved.

    A regression that classified 503 as permanent would fail this job on the
    first network blip; a regression that never retried would fail on request
    two. Both are invisible to the verify/install tests.
    """
    driver = _pinned_shellcheck_download_driver(tmp_path)
    server, thread, url, counter = _serve_status_sequence([503, 503, 200], b"retried-body")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert counter["requests"] == 3
    assert destination.read_bytes() == b"retried-body"
    assert "transient HTTP 503" in result.stderr


@pytest.mark.parametrize("status", [404, 403])
def test_pinned_shellcheck_download_fails_fast_on_permanent_http_status(tmp_path: Path, status: int) -> None:
    """FAR-2486: 404/403 is a single attempt, non-zero, and installs nothing."""
    driver = _pinned_shellcheck_download_driver(tmp_path)
    # The second entry is 200 so a wrongly-retried permanent failure would
    # "succeed" and fail the assertions below, instead of hiding behind the
    # 5-attempt exhaustion path.
    server, thread, url, counter = _serve_status_sequence([status, 200], b"must-not-be-saved")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode != 0
    assert counter["requests"] == 1
    assert not destination.exists()
    assert not Path(f"{destination.as_posix()}.part").exists()
    assert f"permanent HTTP {status}" in result.stderr


def test_pinned_shellcheck_download_leaves_no_temporary_files_on_success(tmp_path: Path) -> None:
    """FAR-2486: a clean download leaves no .part/.status scratch files behind."""
    driver = _pinned_shellcheck_download_driver(tmp_path)
    server, thread, url, counter = _serve_status_sequence([200], b"clean-body")
    destination = tmp_path / "pinned.tar.gz"
    try:
        result = _run_bash_file(driver, [url, destination.as_posix(), "fixture"], cwd=tmp_path)
    finally:
        _stop_server(server, thread)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert counter["requests"] == 1
    assert destination.read_bytes() == b"clean-body"
    leftovers = sorted(path.name for path in tmp_path.iterdir() if path.name.startswith("pinned.tar.gz."))
    assert not leftovers


# ---------------------------------------------------------------------------
# FAR-2553: every timestamped codesign call depends on Apple's timestamp
# authority (TSA). A TSA outage failed the release recut run 44150528228 on both
# macOS jobs with "A timestamp was expected but was not found." and a valid
# Developer ID identity. The retry block is extracted verbatim from the workflow
# and executed against a stubbed `codesign`, so the classification these tests
# pin is the code the macOS job runs.
# ---------------------------------------------------------------------------

_TSA_TIMESTAMP_MISSING = "A timestamp was expected but was not found."


def _codesign_retry_driver(tmp_path: Path) -> Path:
    """Write a driver that runs the workflow's retry helper against a stub codesign."""
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "codesign_with_timestamp_retry")
    driver = tmp_path / "codesign_retry_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + block
        + """
# Each bounded attempt runs codesign in a background subshell (FAR-2593), so
# the call count lives in a file rather than a shell variable.
fake_codesign_calls_file="$(mktemp)"
printf '0\\n' >"$fake_codesign_calls_file"
codesign() {
  local calls
  calls=$(( $(cat "$fake_codesign_calls_file") + 1 ))
  printf '%d\\n' "$calls" >"$fake_codesign_calls_file"
  if [[ "$calls" -le "${FAKE_CODESIGN_STALLS:-0}" ]]; then
    # A stalled timestamp request: never returns on its own within the bound.
    sleep 6
    return 0
  fi
  if [[ "$calls" -le $(( ${FAKE_CODESIGN_STALLS:-0} + ${FAKE_CODESIGN_FAILURES:-0} )) ]]; then
    printf 'build/FarnaControl.app/Contents/MacOS/modulo: %s\\n' "${FAKE_CODESIGN_MESSAGE:-}" >&2
    return "${FAKE_CODESIGN_STATUS:-1}"
  fi
  printf 'build/FarnaControl.app/Contents/MacOS/modulo: replacing existing signature\\n' >&2
  return 0
}

status=0
codesign_with_timestamp_retry fixture --force --timestamp --sign "Developer ID Application: Fixture (AAAAAAAAAA)" build/FarnaControl.app || status=$?
printf 'fake_codesign_calls=%d\\n' "$(cat "$fake_codesign_calls_file")"
exit "$status"
""",
        encoding="utf-8",
    )
    return driver


def _codesign_retry_env(**overrides: str) -> dict[str, str]:
    """Fast retry timing for tests; production defaults are asserted structurally."""
    env = {
        "CODESIGN_TIMESTAMP_MAX_ATTEMPTS": "3",
        "CODESIGN_TIMESTAMP_BASE_DELAY": "0",
        "CODESIGN_TIMESTAMP_MAX_DELAY": "0",
    }
    env.update(overrides)
    return env


def test_macos_every_timestamped_codesign_call_routes_through_the_retry_helper() -> None:
    """FAR-2553: no timestamped codesign call may bypass the TSA retry."""
    _step, build_run = _native_installer_step()

    assert "# BEGIN codesign_with_timestamp_retry" in build_run
    assert "codesign_with_timestamp_retry() {" in build_run
    assert "codesign_failure_is_transient_timestamp() {" in build_run
    # The nested-binary signer runs in a `bash -c` child, so the helper has to
    # be exported or that call site cannot see it.
    assert "export -f codesign_with_timestamp_retry" in build_run
    assert "export -f codesign_failure_is_transient_timestamp" in build_run

    # Production defaults: 5 attempts, 10s base delay, 120s cap.
    assert "CODESIGN_TIMESTAMP_MAX_ATTEMPTS:-5" in build_run
    assert "CODESIGN_TIMESTAMP_BASE_DELAY:-10" in build_run
    assert "CODESIGN_TIMESTAMP_MAX_DELAY:-120" in build_run

    assert ('run_pre_smoke app_codesign codesign_with_timestamp_retry app_codesign --force --deep --options runtime --timestamp --sign "$SIGNING_IDENTITY" "$APP"') in build_run
    assert ('run_pre_smoke dmg_codesign codesign_with_timestamp_retry dmg_codesign --force --timestamp --sign "$SIGNING_IDENTITY" "$DMG"') in build_run
    assert ('codesign_with_timestamp_retry "nested_binary_$(basename "$binary")" --force --options runtime --timestamp --sign "$identity" "$binary"') in build_run

    # Sweep: EVERY runnable line that requests a secure timestamp goes through
    # the helper, so a future call site cannot quietly regress to a bare
    # codesign, and no call site may opt out of the timestamp.
    bypassing = [line.strip() for line in build_run.splitlines() if "--timestamp" in line and not line.strip().startswith("#") and "codesign_with_timestamp_retry" not in line]
    assert not bypassing, bypassing
    assert "--timestamp=none" not in build_run

    # Verification, notarization and stapling semantics are untouched: only
    # signing gained a retry.
    assert 'run_pre_smoke app_codesign_verify codesign --verify --deep --strict --verbose=2 "$APP"' in build_run
    assert 'run_pre_smoke mounted_app_codesign codesign --verify --deep --strict "$MOUNTED_APP"' in build_run
    assert "run_pre_smoke dmg_notary_submit bash scripts/macos/notarize-and-verify.sh" in build_run
    assert 'xcrun stapler validate "$DMG"' in build_run


def test_macos_codesign_retry_retries_the_observed_tsa_failure_then_succeeds(tmp_path: Path) -> None:
    """FAR-2553: the exact diagnostic from run 44150528228 is retried and signed."""
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(
            FAKE_CODESIGN_FAILURES="1",
            FAKE_CODESIGN_MESSAGE=_TSA_TIMESTAMP_MISSING,
            FAKE_CODESIGN_STATUS="1",
        ),
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "fake_codesign_calls=2" in result.stdout
    assert "class=transient" in result.stderr
    assert "retrying in 0s" in result.stderr
    assert "class=success" in result.stderr
    # Attempt logging carries metadata only - never the signing invocation.
    assert "Developer ID Application" not in result.stdout
    assert "Developer ID Application" not in result.stderr


@pytest.mark.parametrize(
    "message",
    [
        "The timestamp service is not available.",
        "unable to get timestamp: The operation couldn't be completed. (NSURLErrorDomain error -1005.)",
        "The network connection was lost.",
        "codesign: The timestamp server is currently unreachable.",
    ],
)
def test_macos_codesign_retry_retries_other_transient_tsa_diagnostics(tmp_path: Path, message: str) -> None:
    """FAR-2553: TSA unavailability and its network symptoms are all retried."""
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(
            FAKE_CODESIGN_FAILURES="1",
            FAKE_CODESIGN_MESSAGE=message,
            FAKE_CODESIGN_STATUS="1",
        ),
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "fake_codesign_calls=2" in result.stdout
    assert "class=transient" in result.stderr


@pytest.mark.parametrize(
    "message",
    [
        'unable to build chain to self-signed root for signer "Developer ID Application: Fixture (AAAAAAAAAA)"',
        "no identity found",
        "code object is not signed at all",
        "unrecognized option: --deep",
    ],
)
def test_macos_codesign_retry_never_retries_a_non_transient_failure(tmp_path: Path, message: str) -> None:
    """FAR-2553: identity/certificate/option failures fail closed on attempt one."""
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(
            FAKE_CODESIGN_FAILURES="99",
            FAKE_CODESIGN_MESSAGE=message,
            FAKE_CODESIGN_STATUS="42",
        ),
    )

    assert result.returncode == 42
    assert "fake_codesign_calls=1" in result.stdout
    assert "class=non-transient" in result.stderr
    assert "not retried" in result.stderr
    assert "retrying in" not in result.stderr


def test_macos_codesign_retry_stops_at_the_bounded_attempt_cap(tmp_path: Path) -> None:
    """FAR-2553: a persistent TSA outage is bounded, never an unbounded loop."""
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(
            CODESIGN_TIMESTAMP_MAX_ATTEMPTS="3",
            FAKE_CODESIGN_FAILURES="99",
            FAKE_CODESIGN_MESSAGE=_TSA_TIMESTAMP_MISSING,
            FAKE_CODESIGN_STATUS="1",
        ),
    )

    assert result.returncode == 1
    assert "fake_codesign_calls=3" in result.stdout
    assert "persisted through 3 attempt(s)" in result.stderr


def test_macos_codesign_retry_terminates_a_stalled_attempt_and_retries_it(tmp_path: Path) -> None:
    """FAR-2593: run 44743991976 sat in `codesign --timestamp` on the DMG for
    2h16m. A stalled attempt is now terminated at its bound and retried."""
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(CODESIGN_ATTEMPT_TIMEOUT="1", FAKE_CODESIGN_STALLS="1"),
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "fake_codesign_calls=2" in result.stdout
    assert "attempt=1/3 exit=124 class=timeout" in result.stderr
    assert "codesign attempt exceeded 1s and was terminated" in result.stderr
    assert "class=success" in result.stderr


def test_macos_codesign_retry_bounds_a_codesign_that_always_stalls(tmp_path: Path) -> None:
    driver = _codesign_retry_driver(tmp_path)
    result = _run_bash_file(
        driver,
        [],
        cwd=tmp_path,
        env=_codesign_retry_env(CODESIGN_TIMESTAMP_MAX_ATTEMPTS="2", CODESIGN_ATTEMPT_TIMEOUT="1", FAKE_CODESIGN_STALLS="99"),
    )

    assert result.returncode == 124
    assert "fake_codesign_calls=2" in result.stdout
    assert "persisted through 2 attempt(s)" in result.stderr


def test_macos_codesign_attempts_carry_a_production_time_bound() -> None:
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "codesign_with_timestamp_retry")

    assert "CODESIGN_ATTEMPT_TIMEOUT:-300" in block
    assert 'codesign_attempt_bounded "$attempt_timeout" "$output_file" "$@"' in block
    # Inside the helper, codesign itself is only ever invoked by the bounded runner.
    bare_calls = [line.strip() for line in block.splitlines() if re.match(r"\s*codesign\s", line)]
    assert bare_calls == ['codesign "$@" >"$output_file" 2>&1 &']
    # The nested-binary signer runs in a `bash -c` child, so it must be exported.
    assert "export -f codesign_attempt_bounded" in build_run


# ---------------------------------------------------------------------------
# FAR-2554: signing and notarization succeeded, then the post-install smoke
# test failed because the installed app could not start its bundled services:
#
#   bundled binaries not found at
#   .../Modulo-0.3.328.app/Contents/Resources/bundle/runtime/bundled/bin
#   (missing: initdb, postgres, pg_isready, redis-server, redis-cli)
#
# The build installed the binaries in <bundle>/bin (in-bundle relative
# symlinks) and the compiled launcher exported that directory, but the resolver
# read a DIFFERENT env name, so the export was ignored and the boot fell
# through to the <sys.prefix>/bundled/bin default that no bundle populates.
# These tests pin one env-var name across every producer and consumer, pin the
# macOS launcher's exported directory to the layout the build populates, pin
# the Linux installer's exported directory to the layout that bundle
# populates, and execute the build gate against the shipped layout plus three
# mutations.
#
# Windows is NAME-ONLY on purpose: scripts/windows/build-launcher.ps1 stages
# only bin\modulo.exe and no PostgreSQL/Redis payload, so the Windows boot gate
# cannot pass whatever the variable is called. Its payload layout is a separate
# follow-up, so nothing here asserts Windows is functionally aligned - only
# that its launcher uses the shared variable name.
# ---------------------------------------------------------------------------

_PRODUCER_CONSUMER_CONTRACT = [
    # producer: the macOS bundle's compiled launcher.
    ("scripts/macos/build-native-bundle.sh", r'setenv\("([A-Za-z_][A-Za-z0-9_]*)", bin_dir, 1\)'),
    # producer: the Linux tarball installer's pre-upgrade dump invocation.
    ("scripts/install.sh", r'^\s+([A-Za-z_][A-Za-z0-9_]*BUNDLED_BIN_DIR)="'),
    # producer: the Linux tarball bundle's launcher wrapper.
    (".github/workflows/bundle-release.yml", r"export ([A-Za-z_][A-Za-z0-9_]*BUNDLED_BIN_DIR)="),
    # consumer: the launcher entry point's resolver.
    ("backend/src/farna/launcher/entry.py", r'BUNDLED_BIN_DIR_ENV = "([A-Z_][A-Z0-9_]*)"'),
    # consumer: the upgrade helper's resolver (same contract, own literal).
    ("backend/src/farna/launcher/upgrade.py", r'os\.environ\.get\("([A-Z_][A-Z0-9_]*BUNDLED_BIN_DIR)"\)'),
]

#: The Windows launcher's env-name literal. It is asserted separately (name
#: only) because its bundle stages no PostgreSQL/Redis payload yet.
_WINDOWS_LAUNCHER_ENV_NAME = r'L"([A-Za-z_][A-Za-z0-9_]*BUNDLED_BIN_DIR)"'


def _bundle_build_script() -> str:
    script_path = REPO_ROOT / "scripts" / "macos" / "build-native-bundle.sh"
    assert script_path.is_file()
    return script_path.read_text(encoding="utf-8")


def _launcher_c_source(script: str) -> str:
    """Return the launcher C heredoc body exactly as the build compiles it."""
    lines = script.splitlines()
    opener = next(line for line in lines if "launcher.c" in line and "<<'EOF'" in line)
    start = script.index(opener) + len(opener) + 1
    end = script.index("\nEOF\n", start)
    return script[start:end] + "\n"


def _launcher_bin_dir_env_name(c_source: str) -> str:
    match = re.search(r'setenv\("([A-Za-z_][A-Za-z0-9_]*)", bin_dir, 1\)', c_source)
    assert match is not None, c_source
    return match.group(1)


def _launcher_bin_dir_format(c_source: str) -> str:
    match = re.search(r'snprintf\(bin_dir, sizeof\(bin_dir\), "([^"]*)", bundle\)', c_source)
    assert match is not None, c_source
    return match.group(1)


def _launcher_exported_bin_dir(c_source: str, bundle_root: Path) -> Path:
    """Apply the launcher's own format string to a bundle root (as C would)."""
    return Path(_launcher_bin_dir_format(c_source) % str(bundle_root))


@pytest.mark.parametrize(("relative_path", "pattern"), _PRODUCER_CONSUMER_CONTRACT)
def test_bundled_bin_dir_env_name_agrees_across_producers_and_consumers(relative_path: str, pattern: str) -> None:
    """FAR-2554: the export and the read must be the SAME name on every platform.

    The name was renamed on the consumer side only, so every native launcher
    exported a variable nothing read and the boot resolved
    ``<sys.prefix>/bundled/bin`` instead of the directory the bundle populates.
    """
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    names = set(re.findall(pattern, text, re.MULTILINE))

    assert names, f"no bundled-bin-dir env name found in {relative_path}"
    assert names == {entry_module.BUNDLED_BIN_DIR_ENV}, relative_path


def test_windows_launcher_uses_the_shared_bundled_bin_dir_name_only() -> None:
    """FAR-2554: the Windows launcher's env NAME matches; nothing more.

    Deliberately name-only. ``scripts/windows/build-launcher.ps1`` stages
    ``bin\\modulo.exe`` and no PostgreSQL/Redis payload, so the Windows boot
    gate cannot pass whatever the variable is called; its bundled payload
    layout is a separate follow-up. Asserting functional alignment here would
    pin behaviour the Windows bundle does not have.
    """
    text = (REPO_ROOT / "scripts" / "windows" / "modulo-launcher.c").read_text(encoding="utf-8")
    names = set(re.findall(_WINDOWS_LAUNCHER_ENV_NAME, text))

    assert names == {entry_module.BUNDLED_BIN_DIR_ENV}


def test_macos_launcher_bin_dir_expression_is_the_directory_the_bundle_populates() -> None:
    """FAR-2554: the exported directory is the one the layout block creates."""
    script = _bundle_build_script()
    c_source = _launcher_c_source(script)
    bundle_root = Path("/tmp/modulo-bundle")

    assert _launcher_bin_dir_format(c_source) == "%s/bin"
    assert _launcher_exported_bin_dir(c_source, bundle_root) == bundle_root / "bin"

    layout = _extract_marked_bash(script, "bundled_bin_layout")
    assert 'mkdir -p "${BUNDLE_ROOT}/redis" "${BUNDLE_ROOT}/bin"' in layout
    assert 'ln -s "../pg/bin/${binary}" "${BUNDLE_ROOT}/bin/${binary}"' in layout
    assert 'ln -s "../redis/${binary}" "${BUNDLE_ROOT}/bin/${binary}"' in layout


def _install_sh_exported_bin_dir(tmp_path: Path, old_root: Path) -> Path:
    """Resolve install.sh's pre-upgrade export exactly as the shell would.

    The assignment is lifted verbatim from ``run_pre_upgrade_dump`` and expanded
    by bash against a seeded install root, so the test pins the VALUE the
    installer hands the upgrade helper - not just the variable's name.
    """
    script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    matches = re.findall(
        r'^\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*BUNDLED_BIN_DIR)="(?P<value>[^"]*)"',
        script,
        re.MULTILINE,
    )
    assert len(matches) == 1, matches
    name, value = matches[0]
    driver = tmp_path / "resolve_export.sh"
    driver.write_text(
        f'#!/usr/bin/env bash\nset -euo pipefail\nold_root="$1"\n{name}="{value}"\nprintf "%s\\n" "${{{name}}}"\n',
        encoding="utf-8",
    )
    result = _run_bash_file(driver, [old_root.as_posix()], cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return Path(result.stdout.strip())


def test_linux_pre_upgrade_export_names_the_directory_that_holds_pg_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-2554: the install.sh value must be ``<bundle>/pg/bin``, not ``<bundle>/pg``.

    The parametrised name test captures only the identifier, so reverting the
    value to the pre-fix ``${old_root}/pg`` - where pg_dump is one level deeper
    and therefore not found - would leave the suite green. Seed the layout the
    Linux tarball actually builds, expand the shipped value with bash, then let
    the upgrade helper's own resolver look for the dump tools in it.
    """
    old_root = tmp_path / "current"
    pg_bin = old_root / "pg" / "bin"
    # bundle-release.yml builds PostgreSQL into <bundle>/pg/bin and copies the
    # redis binaries in beside them.
    for name in ("initdb", "postgres", "pg_isready", "pg_dump", "psql", "pg_config", "redis-server", "redis-cli"):
        _write_executable(pg_bin / binary_name(name))

    exported = _install_sh_exported_bin_dir(tmp_path, old_root)

    assert exported == pg_bin
    monkeypatch.setenv(entry_module.BUNDLED_BIN_DIR_ENV, str(exported))
    resolved = upgrade_module._resolve_pg_bin_dir(None)
    assert resolved == pg_bin
    for name in ("pg_dump", "pg_isready"):
        assert (resolved / binary_name(name)).exists(), name


def _write_executable(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _seed_bundle_binaries(tmp_path: Path) -> tuple[Path, Path]:
    """Create the pg/ and redis/ payload trees the real build produces."""
    bundle_root = tmp_path / "bundle"
    for name in ("initdb", "postgres", "pg_isready", "pg_dump", "psql"):
        _write_executable(bundle_root / "pg" / "bin" / name)
    redis_src = tmp_path / "redis-src"
    for name in ("redis-server", "redis-cli"):
        _write_executable(redis_src / "src" / name)
    return bundle_root, redis_src


def _stub_bundle_python(bundle_root: Path) -> None:
    """Stand in for the bundle venv interpreter the build gate invokes.

    The gate sets PYTHONPATH to the *bundle's* backend source copy; the stub
    points the import path at this checkout so the gate's real resolver runs.
    """
    repo_src = (REPO_ROOT / "backend" / "src").as_posix()
    interpreter = Path(sys.executable).as_posix()
    _write_executable(
        bundle_root / "backend" / ".venv" / "bin" / "python",
        "#!/usr/bin/env bash\n" + f'export PYTHONPATH="{repo_src}"\n' + f'exec "{interpreter}" "$@"\n',
    )


def _bundle_contract_driver(tmp_path: Path, c_source: str, layout: str) -> Path:
    """A driver that runs the extracted layout block, then the build gate."""
    gate = _extract_marked_bash(_bundle_build_script(), "bundled_bin_dir_contract_gate")
    build_root = tmp_path / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "launcher.c").write_text(c_source, encoding="utf-8")
    driver = tmp_path / "bundle_contract_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + 'BUNDLE_ROOT="$1"\nREDIS_SRC="$2"\nBUILD_ROOT="$3"\n' + layout + "\n" + gate + "\n",
        encoding="utf-8",
    )
    return driver


def _run_bundle_contract(tmp_path: Path, c_source: str, layout: str) -> subprocess.CompletedProcess[str]:
    bundle_root, redis_src = _seed_bundle_binaries(tmp_path)
    _stub_bundle_python(bundle_root)
    driver = _bundle_contract_driver(tmp_path, c_source, layout)
    return _run_bash_file(
        driver,
        [bundle_root.as_posix(), redis_src.as_posix(), (tmp_path / "build").as_posix()],
        cwd=tmp_path,
    )


def test_macos_bundle_layout_binaries_resolve_at_the_launcher_exported_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-2554: the produced layout and the resolver agree, binary for binary."""
    script = _bundle_build_script()
    layout = _extract_marked_bash(script, "bundled_bin_layout")
    bundle_root, redis_src = _seed_bundle_binaries(tmp_path)
    driver = tmp_path / "layout_only_driver.sh"
    driver.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nBUNDLE_ROOT="$1"\nREDIS_SRC="$2"\n' + layout + "\n",
        encoding="utf-8",
    )

    result = _run_bash_file(driver, [bundle_root.as_posix(), redis_src.as_posix()], cwd=tmp_path)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    c_source = _launcher_c_source(script)
    env_name = _launcher_bin_dir_env_name(c_source)
    exported = _launcher_exported_bin_dir(c_source, bundle_root)

    # The launcher exports this value into the child environment; the resolver
    # must return exactly that directory. The incident was a value the resolver
    # never read, so it fell through to <sys.prefix>/bundled/bin.
    monkeypatch.setenv(env_name, str(exported))
    assert entry_module.resolve_bin_dir() == exported
    assert exported == bundle_root / "bin"

    # The launcher's own boot gate must accept the produced layout. It appends
    # ".exe" on Windows, so the macOS layout's extensionless names are only
    # that gate's contract on POSIX (CI runs this test on Linux).
    if os.name != "nt":
        entry_module._verify_bundled_binaries(exported)

    symlinked = 0
    for name in entry_module.REQUIRED_BUNDLED_BINARIES:
        binary = exported / name
        assert binary.exists(), name
        if not binary.is_symlink():
            continue
        symlinked += 1
        target = binary.readlink()
        assert not Path(target).is_absolute(), (name, target)
        assert binary.resolve().is_relative_to(bundle_root.resolve()), (name, target)

    # POSIX creates the real relative symlinks the bundle ships; Windows may
    # materialise the same layout as copies, so only assert the shape there.
    if os.name != "nt":
        assert symlinked == len(entry_module.REQUIRED_BUNDLED_BINARIES)


def test_macos_bundle_build_gate_passes_for_the_shipped_producer_contract(tmp_path: Path) -> None:
    """FAR-2554: the shipped launcher + shipped layout satisfy the build gate."""
    script = _bundle_build_script()
    result = _run_bundle_contract(tmp_path, _launcher_c_source(script), _extract_marked_bash(script, "bundled_bin_layout"))

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"bundled bin dir contract: env={entry_module.BUNDLED_BIN_DIR_ENV}" in result.stdout
    assert "dir=" in result.stdout


def test_macos_bundle_build_gate_fails_on_an_env_name_drift(tmp_path: Path) -> None:
    """FAR-2554 mutation: the pre-rebrand export name must fail the build."""
    script = _bundle_build_script()
    shipped = _launcher_c_source(script)
    drifted = shipped.replace(
        f'setenv("{entry_module.BUNDLED_BIN_DIR_ENV}"',
        'setenv("MODULO_BUNDLED_BIN_DIR"',
        1,
    )
    assert drifted != shipped

    result = _run_bundle_contract(tmp_path, drifted, _extract_marked_bash(script, "bundled_bin_layout"))

    assert result.returncode != 0
    assert "but the resolver reads" in result.stderr


def test_macos_bundle_build_gate_fails_when_the_launcher_resolves_another_directory(tmp_path: Path) -> None:
    """FAR-2554 mutation: the observed ``runtime/bundled/bin`` path must fail the build."""
    script = _bundle_build_script()
    shipped = _launcher_c_source(script)
    drifted = shipped.replace('"%s/bin", bundle', '"%s/runtime/bundled/bin", bundle', 1)
    assert drifted != shipped

    result = _run_bundle_contract(tmp_path, drifted, _extract_marked_bash(script, "bundled_bin_layout"))

    assert result.returncode != 0
    assert "but the launcher resolves" in result.stderr
    assert "missing from the launcher-resolved bin dir" in result.stderr


def test_macos_bundle_build_gate_fails_when_a_required_binary_is_missing(tmp_path: Path) -> None:
    """FAR-2554 mutation: a dropped binary reproduces the incident's missing list."""
    script = _bundle_build_script()
    layout = _extract_marked_bash(script, "bundled_bin_layout")
    shipped_loop = "for binary in initdb postgres pg_isready pg_dump psql; do"
    dropped = layout.replace(shipped_loop, "for binary in initdb postgres pg_dump psql; do")
    assert dropped != layout

    result = _run_bundle_contract(tmp_path, _launcher_c_source(script), dropped)

    assert result.returncode != 0
    assert "missing from the launcher-resolved bin dir" in result.stderr
    assert "pg_isready" in result.stderr


# ---------------------------------------------------------------------------
# FAR-2563: the SPA env contract (the third rebrand drift of the same class)
# ---------------------------------------------------------------------------
#
# The launcher exported MODULO_SERVE_SPA / MODULO_FRONTEND_DIST while
# farna.api.main read FARNA_CONTROL_SERVE_SPA / FARNA_CONTROL_FRONTEND_DIST, so
# the built SPA was never mounted and every installed app answered "/" with the
# API's 404. The macOS build now reads the exported names back out of the
# compiled source and holds them against the shared registry, exactly as the
# FAR-2554 gate does for the bundled bin dir.

_LAUNCHER_SERVE_SPA_ENV = r'setenv\("([A-Za-z_][A-Za-z0-9_]*)", "1", 1\)'
_LAUNCHER_FRONTEND_DIST_ENV = r'setenv\("([A-Za-z_][A-Za-z0-9_]*)", frontend_dist, 1\)'
_LAUNCHER_FRONTEND_DIST_FORMAT = r'snprintf\(frontend_dist, sizeof\(frontend_dist\), "([^"]*)", bundle\)'

_SPA_PRODUCER_CONSUMER_CONTRACT = [
    # producer: the macOS bundle's compiled launcher.
    ("scripts/macos/build-native-bundle.sh", _LAUNCHER_SERVE_SPA_ENV, env_contract.SERVE_SPA_ENV),
    ("scripts/macos/build-native-bundle.sh", _LAUNCHER_FRONTEND_DIST_ENV, env_contract.FRONTEND_DIST_ENV),
]


def _run_spa_env_contract(tmp_path: Path, c_source: str) -> subprocess.CompletedProcess[str]:
    """Run the shipped SPA env gate against ``c_source`` (the compiled launcher)."""
    gate = _extract_marked_bash(_bundle_build_script(), "spa_env_contract_gate")
    bundle_root = tmp_path / "bundle"
    build_root = tmp_path / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    (build_root / "launcher.c").write_text(c_source, encoding="utf-8")
    _stub_bundle_python(bundle_root)
    driver = tmp_path / "spa_env_driver.sh"
    driver.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + 'BUNDLE_ROOT="$1"\nBUILD_ROOT="$2"\n' + gate + "\n",
        encoding="utf-8",
    )
    return _run_bash_file(driver, [bundle_root.as_posix(), build_root.as_posix()], cwd=tmp_path)


@pytest.mark.parametrize(
    ("relative_path", "pattern", "expected"),
    _SPA_PRODUCER_CONSUMER_CONTRACT,
    ids=[f"{relative_path}::{expected}" for relative_path, _pattern, expected in _SPA_PRODUCER_CONSUMER_CONTRACT],
)
def test_spa_env_name_agrees_across_producers_and_consumers(relative_path: str, pattern: str, expected: str) -> None:
    """FAR-2563: the export and the read must be the SAME name."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    names = set(re.findall(pattern, text, re.MULTILINE))

    assert names, f"no SPA env name found in {relative_path}"
    assert names == {expected}, relative_path


def test_the_spa_consumer_reads_the_shared_registry_not_its_own_literals() -> None:
    """FAR-2563 consumer half: ``main`` resolves the SPA names from the registry.

    Together with the producer assertions above (producer == registry) and the
    runtime identity check in ``test_spa_serving`` (consumer == registry), this
    closes the chain producer == registry == consumer without restating values.
    """
    text = (REPO_ROOT / "backend" / "src" / "farna" / "api" / "main.py").read_text(encoding="utf-8")

    assert "from farna.launcher.env_contract import FRONTEND_DIST_ENV, SERVE_SPA_ENV, read_env" in text
    assert "_SERVE_SPA_ENV = SERVE_SPA_ENV" in text
    assert "_FRONTEND_DIST_ENV = FRONTEND_DIST_ENV" in text
    # A private literal is what drifted in the first place.
    assert '_SERVE_SPA_ENV = "FARNA' not in text
    assert '_FRONTEND_DIST_ENV = "FARNA' not in text


def test_macos_launcher_exports_the_spa_dist_directory_the_bundle_populates() -> None:
    """FAR-2563: the exported dist path is the directory the layout block fills."""
    script = _bundle_build_script()
    c_source = _launcher_c_source(script)
    match = re.search(_LAUNCHER_FRONTEND_DIST_FORMAT, c_source)
    assert match is not None, c_source
    assert match.group(1) == "%s/frontend-dist"
    assert 'mkdir -p "${BUNDLE_ROOT}/frontend-dist"' in script
    assert 'cp -a "${ROOT_DIR}/frontend/dist/." "${BUNDLE_ROOT}/frontend-dist/"' in script


def test_macos_bundle_spa_env_gate_passes_for_the_shipped_producer_contract(tmp_path: Path) -> None:
    """FAR-2563: the shipped launcher satisfies the shipped build gate."""
    result = _run_spa_env_contract(tmp_path, _launcher_c_source(_bundle_build_script()))

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"spa env contract: serve={env_contract.SERVE_SPA_ENV}" in result.stdout
    assert f"dist={env_contract.FRONTEND_DIST_ENV}" in result.stdout


def test_macos_bundle_spa_env_gate_fails_on_a_pre_rebrand_export_name(tmp_path: Path) -> None:
    """FAR-2563 mutation: reverting the export to MODULO_* must fail the build."""
    shipped = _launcher_c_source(_bundle_build_script())
    drifted = shipped.replace(
        f'setenv("{env_contract.SERVE_SPA_ENV}", "1", 1)',
        'setenv("MODULO_SERVE_SPA", "1", 1)',
        1,
    )
    assert drifted != shipped

    result = _run_spa_env_contract(tmp_path, drifted)

    assert result.returncode != 0
    assert "but the API reads" in result.stderr
    assert env_contract.SERVE_SPA_ENV in result.stderr


def test_macos_bundle_spa_env_gate_fails_when_the_dist_directory_moves(tmp_path: Path) -> None:
    """FAR-2563 mutation: exporting a dist dir the layout never fills must fail."""
    shipped = _launcher_c_source(_bundle_build_script())
    drifted = shipped.replace('"%s/frontend-dist", bundle', '"%s/dist", bundle', 1)
    assert drifted != shipped

    result = _run_spa_env_contract(tmp_path, drifted)

    assert result.returncode != 0
    assert "but the layout populates" in result.stderr


# ---------------------------------------------------------------------------
# FAR-2574: notarization outcome and artefact identity
#
# `notarytool submit ... --wait` exited 0 on every failing release run while
# Apple never issued a ticket, so the two candidate causes (a post-notarization
# rewrite of the artefact, or a submission that never reached `Accepted`) were
# indistinguishable. These tests pin the diagnostics that distinguish them.
# ---------------------------------------------------------------------------

_NOTARY_TOOL = REPO_ROOT / "scripts" / "macos" / "notarize-and-verify.sh"
_IDENTITY_TOOL = REPO_ROOT / "scripts" / "macos" / "artefact-identity.sh"

#: Stand-in `codesign` for the identity helpers. It reports CDHash=/digest the
#: same way `codesign --display --verbose=4` does, and can emit a sequence of
#: hashes so "the artefact changed between two checks" is reproducible.
_FAKE_CODESIGN = """#!/usr/bin/env bash
set -u
path=""
for candidate in "$@"; do path="$candidate"; done
calls_file="${FAKE_CODESIGN_CALLS:-}"
count=1
if [[ -n "$calls_file" ]]; then
  [[ -f "$calls_file" ]] && count="$(cat "$calls_file")"
  count=$((count + 1))
  printf '%s' "$count" >"$calls_file"
fi
hash="${FAKE_CDHASH:-}"
if [[ -n "${FAKE_CDHASH_SEQUENCE:-}" ]]; then
  IFS=',' read -r -a hashes <<<"$FAKE_CDHASH_SEQUENCE"
  hash="${hashes[$((count - 1))]:-${hashes[0]}}"
fi
if [[ -z "$hash" ]]; then
  printf 'code object is not signed at all\nIn subcomponent: %s\n' "$path" >&2
  exit 1
fi
printf 'Executable=%s\nFormat=disk image\n' "$path" >&2
printf 'CDHash=%s\n' "$hash" >&2
exit 0
"""

#: Stand-in `xcrun`. `notarytool submit`, `notarytool log` and (FAR-2577)
#: `notarytool info` are the only invocations the helpers are allowed to make.
_FAKE_XCRUN = """#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >>"${FAKE_NOTARY_CALLS:-/dev/null}"
if [[ "${1:-}" == "notarytool" && "${2:-}" == "submit" ]]; then
  if [[ -n "${FAKE_NOTARY_SUBMIT_STDERR:-}" ]]; then
    printf '%s\n' "$FAKE_NOTARY_SUBMIT_STDERR" >&2
  fi
  printf '%s\n' "${FAKE_NOTARY_SUBMIT_JSON:-}"
  exit "${FAKE_NOTARY_SUBMIT_EXIT:-0}"
fi
if [[ "${1:-}" == "notarytool" && "${2:-}" == "log" ]]; then
  printf '%s\n' "${FAKE_NOTARY_LOG_JSON:-}"
  exit "${FAKE_NOTARY_LOG_EXIT:-0}"
fi
if [[ "${1:-}" == "notarytool" && "${2:-}" == "info" ]]; then
  count=1
  if [[ -n "${FAKE_NOTARY_INFO_COUNTER:-}" ]]; then
    if [[ -f "$FAKE_NOTARY_INFO_COUNTER" ]]; then
      count="$(cat "$FAKE_NOTARY_INFO_COUNTER")"
      count=$((count + 1))
    fi
    printf '%s' "$count" >"$FAKE_NOTARY_INFO_COUNTER"
  fi
  IFS=',' read -r -a statuses <<<"${FAKE_NOTARY_INFO_SEQUENCE:-In Progress}"
  last=$((${#statuses[@]} - 1))
  info_status="${statuses[$((count - 1))]:-${statuses[$last]}}"
  printf '{"id": "%s", "status": "%s", "message": "fixture"}\n' "${3:-}" "$info_status"
  exit "${FAKE_NOTARY_INFO_EXIT:-0}"
fi
printf 'unexpected xcrun invocation: %s\n' "$*" >&2
exit 1
"""


def _posix_path(path: Path) -> str:
    """Render a path for a bash PATH entry.

    Git Bash does not translate a PATH value that bash itself sets, so a
    Windows `C:/...` entry would split at the drive colon and silently drop the
    stub directory. `/c/...` is the form the shell searches.
    """
    text = path.as_posix()
    if len(text) > 2 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def _stub_bin(tmp_path: Path) -> Path:
    """A PATH prefix holding the fake macOS tools plus a real python3."""
    bin_dir = tmp_path / "stub-bin"
    _write_executable(bin_dir / "codesign", _FAKE_CODESIGN)
    _write_executable(bin_dir / "xcrun", _FAKE_XCRUN)
    _write_executable(
        bin_dir / "python3",
        "#!/usr/bin/env bash\nexec " + shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n',
    )
    return bin_dir


def _run_tool(
    tmp_path: Path,
    script: Path,
    args: list[str],
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a repository-owned macOS helper with the stubbed toolchain on PATH."""
    bin_dir = _stub_bin(tmp_path)
    env = {key: value for key, value in os.environ.items() if not key.startswith("FAKE_")}
    env.update(extra_env or {})
    command = "\n".join(
        [
            'export PATH="' + _posix_path(bin_dir) + ':$PATH"',
            " ".join(["bash", shlex.quote(script.as_posix()), *(shlex.quote(arg) for arg in args)]),
        ]
    )
    # These helpers spawn many short-lived tools; a Windows host pays a
    # process-creation cost per call that a macOS/Linux runner does not.
    return _run_bash(command, cwd=tmp_path, env=env, timeout=180)


def _notary_dmg(tmp_path: Path) -> Path:
    dmg = tmp_path / "FarnaControl-0.3.328-macos-arm64.dmg"
    dmg.write_bytes(b"fixture disk image bytes")
    return dmg


def _notary_args(tmp_path: Path, dmg: Path) -> list[str]:
    key = tmp_path / "notary.p8"
    key.write_text("fixture", encoding="utf-8")
    return [
        "--dmg",
        dmg.as_posix(),
        "--key",
        key.as_posix(),
        "--key-id",
        "FAKEKEYID",
        "--issuer",
        "00000000-0000-0000-0000-000000000000",
        "--identity-out",
        (tmp_path / "identity.env").as_posix(),
        "--evidence",
        (tmp_path / "evidence.env").as_posix(),
    ]


def test_macos_notarization_records_an_accepted_submission_as_evidence(tmp_path: Path) -> None:
    """FAR-2574: the submission status is captured and surfaced, not discarded."""
    dmg = _notary_dmg(tmp_path)
    calls = tmp_path / "notary-calls.log"
    result = _run_tool(
        tmp_path,
        _NOTARY_TOOL,
        _notary_args(tmp_path, dmg),
        {
            "FAKE_CDHASH": "1111111111111111111111111111111111111111",
            "FAKE_NOTARY_SUBMIT_JSON": json.dumps({"id": "abc-123", "status": "Accepted", "message": "Processing complete"}),
            "FAKE_NOTARY_CALLS": calls.as_posix(),
        },
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "submission_id=abc-123 status=Accepted" in result.stdout
    evidence = (tmp_path / "evidence.env").read_text(encoding="utf-8")
    assert "submission_id=abc-123" in evidence
    assert "status=Accepted" in evidence
    # The artefact identity is recorded before submission and re-checked after.
    assert "cdhash=1111111111111111111111111111111111111111" in evidence
    assert len(re.findall(r"^sha256_submitted=[0-9a-f]{64}$", evidence, re.MULTILINE)) == 1
    invocation = calls.read_text(encoding="utf-8")
    assert "--output-format json" in invocation
    assert "notarytool log" not in invocation


def test_macos_notarization_fails_loudly_on_a_non_accepted_status(tmp_path: Path) -> None:
    """FAR-2574: a non-Accepted submission issues no ticket, so it must fail."""
    dmg = _notary_dmg(tmp_path)
    calls = tmp_path / "notary-calls.log"
    result = _run_tool(
        tmp_path,
        _NOTARY_TOOL,
        _notary_args(tmp_path, dmg),
        {
            "FAKE_CDHASH": "2222222222222222222222222222222222222222",
            "FAKE_NOTARY_SUBMIT_JSON": json.dumps({"id": "inv-1", "status": "Invalid", "message": "Archive contains critical validation errors"}),
            "FAKE_NOTARY_LOG_JSON": json.dumps(
                {
                    "status": "Invalid",
                    "message": "Archive contains critical validation errors",
                    "issues": [
                        {
                            "severity": "error",
                            "path": "Farna Control.app/Contents/MacOS/modulo",
                            "message": "The binary is not signed with a valid Developer ID certificate.",
                        }
                    ],
                }
            ),
            "FAKE_NOTARY_CALLS": calls.as_posix(),
        },
    )

    assert result.returncode != 0
    assert "status=Invalid" in result.stdout
    assert "no ticket will be issued" in result.stderr
    # The rejection reason is fetched and surfaced instead of guessed at later.
    invocation = calls.read_text(encoding="utf-8")
    assert "notarytool log inv-1" in invocation
    surfaced = result.stdout + result.stderr
    assert "not signed with a valid Developer ID certificate" in surfaced
    assert not (tmp_path / "evidence.env").exists()


def test_macos_notarization_fails_when_the_submit_result_is_not_parseable(tmp_path: Path) -> None:
    """FAR-2574: exit 0 with unparseable output still leaves the status unknown."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(
        tmp_path,
        _NOTARY_TOOL,
        _notary_args(tmp_path, dmg),
        {
            "FAKE_CDHASH": "3333333333333333333333333333333333333333",
            "FAKE_NOTARY_SUBMIT_JSON": "Conducting pre-submission checks for Modulo.dmg",
            "FAKE_NOTARY_CALLS": (tmp_path / "notary-calls.log").as_posix(),
        },
    )

    assert result.returncode != 0
    assert "notarization status is unknown" in result.stderr


def test_macos_notarization_fails_when_the_artefact_changed_after_the_submit(tmp_path: Path) -> None:
    """FAR-2574 candidate 1: a re-signed artefact must fail the identity check."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(
        tmp_path,
        _NOTARY_TOOL,
        _notary_args(tmp_path, dmg),
        {
            "FAKE_CDHASH_SEQUENCE": "4444444444444444444444444444444444444444,5555555555555555555555555555555555555555",
            "FAKE_NOTARY_SUBMIT_JSON": json.dumps({"id": "abc-999", "status": "Accepted", "message": "Processing complete"}),
            "FAKE_CODESIGN_CALLS": (tmp_path / "codesign-calls").as_posix(),
            "FAKE_NOTARY_CALLS": (tmp_path / "notary-calls.log").as_posix(),
        },
    )

    assert result.returncode != 0
    assert "cdhash changed after notarization" in result.stderr
    assert "not the one Apple notarized" in result.stderr
    assert not (tmp_path / "evidence.env").exists()


def test_macos_artefact_identity_detects_a_rewritten_artefact(tmp_path: Path) -> None:
    """FAR-2574: digest drift fails unless a staple explains it; cdhash never does."""
    dmg = _notary_dmg(tmp_path)
    recorded = tmp_path / "recorded.env"
    common_env = {"FAKE_CDHASH": "6666666666666666666666666666666666666666"}
    record = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["record", "--path", dmg.as_posix(), "--out", recorded.as_posix()],
        common_env,
    )
    assert record.returncode == 0, record.stderr
    recorded_text = recorded.read_text(encoding="utf-8")
    assert "cdhash=6666666666666666666666666666666666666666" in recorded_text
    assert len(re.findall(r"^sha256=[0-9a-f]{64}$", recorded_text, re.MULTILINE)) == 1

    unchanged = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["verify", "--expected", recorded.as_posix(), "--path", dmg.as_posix()],
        common_env,
    )
    assert unchanged.returncode == 0, unchanged.stderr

    # Stapling legitimately rewrites the file: digest drift is tolerated only
    # when the caller says a staple happened.
    dmg.write_bytes(b"fixture disk image bytes with a stapled ticket")
    rewritten = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["verify", "--expected", recorded.as_posix(), "--path", dmg.as_posix()],
        common_env,
    )
    assert rewritten.returncode != 0
    assert "bytes changed with no staple in between" in rewritten.stderr

    stapled = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["verify", "--expected", recorded.as_posix(), "--path", dmg.as_posix(), "--after-staple"],
        common_env,
    )
    assert stapled.returncode == 0, stapled.stderr

    # A different cdhash is a different notarized artefact, staple or not.
    resigned = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["verify", "--expected", recorded.as_posix(), "--path", dmg.as_posix(), "--after-staple"],
        {"FAKE_CDHASH": "7777777777777777777777777777777777777777"},
    )
    assert resigned.returncode != 0
    assert "cdhash changed after notarization" in resigned.stderr


def test_macos_artefact_identity_refuses_an_unsigned_artefact(tmp_path: Path) -> None:
    """FAR-2574: no CDHash means the identity cannot be established at all."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(
        tmp_path,
        _IDENTITY_TOOL,
        ["record", "--path", dmg.as_posix(), "--out", (tmp_path / "recorded.env").as_posix()],
        {"FAKE_CDHASH": ""},
    )

    assert result.returncode != 0
    assert "reported no CDHash" in result.stderr
    assert not (tmp_path / "recorded.env").exists()


def test_macos_notarization_submits_through_the_status_proving_wrapper() -> None:
    """FAR-2574: no bare notarytool call may bypass the status/identity proof."""
    _step, build_run = _native_installer_step()

    assert "xcrun notarytool submit" not in build_run
    assert "run_pre_smoke dmg_notary_submit bash scripts/macos/notarize-and-verify.sh" in build_run
    assert "--output-format json" in _NOTARY_TOOL.read_text(encoding="utf-8")
    # Evidence is printed on the success path too, not only in a failure branch.
    assert 'cat "$DATA/notarization-evidence.env"' in build_run
    assert "the notarization submission evidence file was not written" in build_run

    submit_index = build_run.index("run_pre_smoke dmg_notary_submit")
    gate_index = build_run.index("run_pre_smoke dmg_identity_before_upload")
    assert submit_index < gate_index
    # The identity gate is the last thing the step does before the upload step.
    assert 'run_assertion "no_owned_postgres_redis"' in build_run
    assert build_run.index('run_assertion "no_owned_postgres_redis"') < gate_index
    # Digest drift is only tolerated when the staple actually succeeded.
    assert 'dmg_identity_after_staple_arg="--after-staple"' in build_run
    assert 'if [[ "$dmg_staple_ticket_unavailable" -eq 0 ]]; then' in build_run


def test_macos_nothing_resigns_or_rewrites_the_dmg_after_notarization() -> None:
    """FAR-2574 ordering invariant: the notarized bytes are the uploaded bytes."""
    _step, build_run = _native_installer_step()
    lines = build_run.splitlines()
    submit_index = next(index for index, line in enumerate(lines) if "dmg_notary_submit" in line)

    resigning = [line.strip() for line in lines[submit_index:] if not line.strip().startswith("#") and "codesign" in line and "--sign" in line]
    recreating = [line.strip() for line in lines[submit_index:] if not line.strip().startswith("#") and "hdiutil create" in line]
    rewriting = [line.strip() for line in lines[submit_index:] if not line.strip().startswith("#") and (re.search(r'>>?\s*"\$DMG"', line) or re.search(r'\b(cp|mv)\b[^|]*"\$DMG"\s*$', line))]

    assert not resigning, resigning
    assert not recreating, recreating
    assert not rewriting, rewriting

    # And the signed order that makes that possible: app signed, DMG built, DMG
    # signed, then submitted.
    order = ["run_pre_smoke app_codesign codesign", "hdiutil_with_resource_busy_retry dmg_create", "run_pre_smoke dmg_codesign", "run_pre_smoke dmg_notary_submit"]
    positions = [build_run.index(command) for command in order]
    assert positions == sorted(positions)


def test_macos_upload_step_follows_the_notarizing_step_and_the_contract_checks_the_helpers() -> None:
    """FAR-2574: the artefact is uploaded after the gate, and the helpers are gated."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Sign, notarize and smoke-test native installer") < names.index("Upload real macOS installer")

    upload = steps[names.index("Upload real macOS installer")]
    assert upload["with"]["path"] == "build/FarnaControl-${{ needs.normalize-inputs.outputs.version }}-macos-${{ needs.normalize-inputs.outputs.architecture }}.dmg"

    contract = next(step["run"] for step in steps if step.get("name") == "Validate workflow contract")
    assert "shellcheck scripts/macos/notarize-and-verify.sh" in contract
    assert "shellcheck scripts/macos/artefact-identity.sh" in contract
    assert "bash -n scripts/macos/notarize-and-verify.sh" in contract
    assert "bash -n scripts/macos/artefact-identity.sh" in contract


# --- FAR-2575: every Mach-O under Contents is signed and audited ------------

_MACHO_TOOL = REPO_ROOT / "scripts" / "macos" / "macho-inventory.sh"
_THIN_MACHO_ARM64 = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"\x00" * 24
_FAT_MACHO = b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 24
_JAVA_CLASS = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\x00" * 24

#: Stand-in `codesign` for the Mach-O audit. The verdict is keyed off the file
#: name so one fixture tree can hold every signature state Apple distinguishes.
_FAKE_AUDIT_CODESIGN = """#!/usr/bin/env bash
set -u
path=""
for candidate in "$@"; do path="$candidate"; done
name="$(basename "$path")"
if [[ "$1" == "--verify" ]]; then
  case "$name" in
    *unsigned*) printf '%s: code object is not signed at all\\n' "$path" >&2; exit 1 ;;
  esac
  exit 0
fi
printf 'Executable=%s\\n' "$path" >&2
case "$name" in
  *adhoc*) printf 'Signature=adhoc\\n' >&2 ;;
  *notimestamp*)
    printf 'Authority=Developer ID Application: Farna Labs Ltd (TEAMID1234)\\n' >&2
    printf 'Signed Time=19 Nov 2026 at 08:19:21\\n' >&2
    ;;
  *)
    printf 'Authority=Developer ID Application: Farna Labs Ltd (TEAMID1234)\\n' >&2
    printf 'Authority=Developer ID Certification Authority\\n' >&2
    printf 'Timestamp=19 Nov 2026 at 08:19:21\\n' >&2
    ;;
esac
exit 0
"""


def _macho_app(tmp_path: Path) -> Path:
    """An app tree mirroring the bundled runtime layout Apple rejected."""
    app = tmp_path / "Farna Control.app"
    contents = app / "Contents"
    runtime = contents / "Resources" / "bundle" / "runtime"
    lib = runtime / "lib"
    files: dict[Path, bytes] = {
        contents / "MacOS" / "modulo": _THIN_MACHO_ARM64,
        lib / "thread3.0.6" / "libtcl9thread3.0.6.dylib": _THIN_MACHO_ARM64,
        lib / "itcl4.3.8" / "libitcl4.3.8.dylib": _THIN_MACHO_ARM64,
        lib / "python3.12" / "lib-dynload" / "_ssl.cpython-312-darwin.so": _THIN_MACHO_ARM64,
        runtime / "bin" / "fat-tool": _FAT_MACHO,
        runtime / "bin" / "pip3": b"#!/bin/sh\nexec python3 -m pip\n",
        lib / "python3.12" / "empty.py": b"",
        lib / "java" / "Example.class": _JAVA_CLASS,
        contents / "Info.plist": b"<plist/>",
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    # The four Apple-rejected dylibs shipped mode 0644: no executable bit.
    for path in files:
        if path.suffix in {".dylib", ".so"}:
            path.chmod(0o644)
    return app


def _run_macho_tool(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "macho-stub-bin"
    _write_executable(bin_dir / "codesign", _FAKE_AUDIT_CODESIGN)
    _write_executable(
        bin_dir / "python3",
        "#!/usr/bin/env bash\nexec " + shlex.quote(Path(sys.executable).as_posix()) + ' "$@"\n',
    )
    command = "\n".join(
        [
            'export PATH="' + _posix_path(bin_dir) + ':$PATH"',
            " ".join(["bash", shlex.quote(_MACHO_TOOL.as_posix()), *(shlex.quote(arg) for arg in args)]),
        ]
    )
    return _run_bash(command, cwd=tmp_path, env=dict(os.environ), timeout=180)


def _relative_lines(output: str, app: Path) -> list[str]:
    prefix = app.as_posix() + "/"
    return [line.removeprefix(prefix) for line in output.splitlines() if line.strip()]


def test_macos_macho_discovery_selects_non_executable_dylibs_and_extension_modules(tmp_path: Path) -> None:
    """FAR-2575: mode-0644 dylibs and .so modules are Mach-O and must be signed."""
    app = _macho_app(tmp_path)

    result = _run_macho_tool(tmp_path, ["discover", app.as_posix()])

    assert result.returncode == 0, result.stderr
    selected = set(_relative_lines(result.stdout, app))
    assert selected == {
        "Contents/MacOS/modulo",
        "Contents/Resources/bundle/runtime/lib/thread3.0.6/libtcl9thread3.0.6.dylib",
        "Contents/Resources/bundle/runtime/lib/itcl4.3.8/libitcl4.3.8.dylib",
        "Contents/Resources/bundle/runtime/lib/python3.12/lib-dynload/_ssl.cpython-312-darwin.so",
        "Contents/Resources/bundle/runtime/bin/fat-tool",
    }


def test_macos_macho_discovery_lists_deepest_files_first_for_inside_out_signing(tmp_path: Path) -> None:
    app = _macho_app(tmp_path)

    result = _run_macho_tool(tmp_path, ["discover", app.as_posix()])

    assert result.returncode == 0, result.stderr
    depths = [line.count("/") for line in _relative_lines(result.stdout, app)]
    assert depths == sorted(depths, reverse=True)


def test_macos_macho_discovery_fails_when_the_bundle_holds_no_macho(tmp_path: Path) -> None:
    app = tmp_path / "Empty.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(b"<plist/>")

    result = _run_macho_tool(tmp_path, ["discover", app.as_posix()])

    assert result.returncode != 0
    assert "no Mach-O file found" in result.stderr


def test_macos_macho_audit_passes_when_every_macho_is_developer_id_timestamped(tmp_path: Path) -> None:
    app = _macho_app(tmp_path)

    result = _run_macho_tool(tmp_path, ["audit", app.as_posix()])

    assert result.returncode == 0, result.stderr
    assert "all 5 Mach-O file(s) carry a Developer ID signature with a secure timestamp" in result.stdout


@pytest.mark.parametrize(
    ("file_name", "diagnostic"),
    [
        ("libtcl9thread3.0.6-unsigned.dylib", "unsigned or invalid signature"),
        ("libitcl4.3.8-adhoc.dylib", "not signed with a Developer ID Application certificate"),
        ("libthread3.0.6-notimestamp.dylib", "signature has no secure timestamp"),
    ],
)
def test_macos_macho_audit_names_every_file_apple_would_reject(tmp_path: Path, file_name: str, diagnostic: str) -> None:
    """FAR-2575: the audit reproduces each of Apple's per-file rejections."""
    app = _macho_app(tmp_path)
    bad = app / "Contents" / "Resources" / "bundle" / "runtime" / "lib" / "thread3.0.6" / file_name
    bad.write_bytes(_THIN_MACHO_ARM64)
    bad.chmod(0o644)

    result = _run_macho_tool(tmp_path, ["audit", app.as_posix()])

    assert result.returncode != 0
    assert f"{diagnostic}: {bad.as_posix()}" in result.stderr
    assert "1 of 6 Mach-O file(s) failed" in result.stderr


def test_macos_signer_selects_every_macho_and_audits_before_submit() -> None:
    """FAR-2575: selection ignores permission bits; the audit precedes the notary round trip."""
    _step, build_run = _native_installer_step()

    assert 'run_pre_smoke nested_binary_discovery bash scripts/macos/macho-inventory.sh discover "$APP"' in build_run
    discovery_line = next(line for line in build_run.splitlines() if "run_pre_smoke nested_binary_discovery" in line)
    assert "-perm" not in discovery_line
    # The signer still consumes exactly the discovery output.
    assert 'done < "$DATA/pre-smoke-nested_binary_discovery.log"' in build_run

    order = [
        "run_pre_smoke nested_binary_discovery",
        'run_pre_smoke "nested_binary_${binary_index}"',
        "run_pre_smoke app_codesign codesign",
        "run_pre_smoke app_codesign_verify codesign",
        'run_pre_smoke nested_macho_signature_audit bash scripts/macos/macho-inventory.sh audit "$APP"',
        "hdiutil_with_resource_busy_retry dmg_create bash -c",
        "run_pre_smoke dmg_codesign codesign",
        "run_pre_smoke dmg_notary_submit bash scripts/macos/notarize-and-verify.sh",
    ]
    positions = [build_run.index(command) for command in order]
    assert positions == sorted(positions)


def test_macos_nested_signer_keeps_its_fail_closed_classification() -> None:
    """FAR-2575 must not loosen the per-file classification of selected binaries."""
    _step, build_run = _native_installer_step()
    start = build_run.index('run_pre_smoke "nested_binary_${binary_index}"')
    signer = build_run[start : build_run.index("run_pre_smoke app_codesign codesign")]

    assert '*"Mach-O 64-bit dynamically linked shared library $MACOS_ARCH"*' in signer
    assert '*"Mach-O 64-bit bundle $MACOS_ARCH"*' in signer
    assert "--force --options runtime --timestamp --sign" in signer
    assert re.search(r'printf "unexpected Mach-O architecture: %s \(%s\)\\n" "\$binary" "\$kind" >&2\s+exit 1', signer)
    assert re.search(r'printf "unexpected executable type: %s \(%s\)\\n" "\$binary" "\$kind" >&2\s+exit 1', signer)


def test_macos_contract_gates_the_macho_inventory_helper() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    contract = next(step["run"] for step in steps if step.get("name") == "Validate workflow contract")

    assert "test -x scripts/macos/macho-inventory.sh" in contract
    assert "shellcheck scripts/macos/macho-inventory.sh" in contract
    assert "bash -n scripts/macos/macho-inventory.sh" in contract


def test_macos_notary_wait_fits_inside_the_job_timeout() -> None:
    """FAR-2575: run 44525902038 timed out a 30m wait while Apple was still processing."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    _step, build_run = _native_installer_step()

    submit = build_run[build_run.index("run_pre_smoke dmg_notary_submit") :]
    submit = submit[: submit.index("--evidence")]
    wait = re.search(r"--timeout (\d+)m", submit)
    assert wait is not None
    wait_minutes = int(wait.group(1))
    assert wait_minutes >= 90
    poll = re.search(r"--poll-budget (\d+)", submit)
    assert poll is not None
    poll_minutes = int(poll.group(1)) // 60
    assert poll_minutes >= 30
    # Retry sleeps (~63m worst case) plus build/sign/smoke work must still fit
    # alongside the wait AND the post-timeout poll (FAR-2577), or the job dies
    # before the notary verdict arrives.
    assert workflow["jobs"]["build"]["timeout-minutes"] >= wait_minutes + poll_minutes + 90
    assert workflow["jobs"]["build"]["timeout-minutes"] <= 360
    notary_tool = _NOTARY_TOOL.read_text(encoding="utf-8")
    assert 'TIMEOUT="90m"' in notary_tool
    # The status gate is unchanged: only an Accepted submission passes.
    assert 'if [[ "$status" != "Accepted" ]]; then' in notary_tool


# --- FAR-2577: a slow Apple verdict is PENDING, not a failure ---------------
#
# On 2026-11-19 Apple took ~3h to accept submission 52f6997c. notarytool's
# `--wait --timeout 90m` exited 124 with the timeout reported as JSON on stderr
# (stdout empty), the wrapper reported "no parseable JSON result", the job
# failed, and the submitted DMG was never uploaded - so the later Accepted
# ticket was unusable. These tests pin the pending outcome and its hand-off.

_NOTARY_STATUS_TOOL = REPO_ROOT / "scripts" / "macos" / "notary-submission-status.sh"
_TIMEOUT_SUBMISSION_ID = "52f6997c-0000-4000-8000-000000000000"
_TIMEOUT_JSON = json.dumps(
    {
        "message": "Timeout of 5400 second(s) was reached before processing completed.",
        "id": _TIMEOUT_SUBMISSION_ID,
    }
)
_NOTARY_PENDING_EXIT = 3


def _timed_out_submit_env(tmp_path: Path, info_sequence: str) -> dict[str, str]:
    """The observed 2026-11-19 shape: exit 124, the timeout JSON only on stderr."""
    return {
        "FAKE_CDHASH": "8888888888888888888888888888888888888888",
        "FAKE_NOTARY_SUBMIT_JSON": "",
        "FAKE_NOTARY_SUBMIT_STDERR": _TIMEOUT_JSON,
        "FAKE_NOTARY_SUBMIT_EXIT": "124",
        "FAKE_NOTARY_INFO_SEQUENCE": info_sequence,
        "FAKE_NOTARY_INFO_COUNTER": (tmp_path / "info-count").as_posix(),
        "FAKE_NOTARY_CALLS": (tmp_path / "notary-calls.log").as_posix(),
        "FAKE_NOTARY_LOG_JSON": json.dumps({"status": "Invalid", "issues": []}),
    }


def _pending_args(tmp_path: Path, dmg: Path, budget: str) -> list[str]:
    return [
        *_notary_args(tmp_path, dmg),
        "--poll-budget",
        budget,
        "--poll-interval",
        "1",
        "--submission-out",
        (tmp_path / "submission" / "submission.env").as_posix(),
    ]


def _env_file(path: Path) -> dict[str, str]:
    pairs = (line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)
    return dict(pairs)


def test_macos_notarization_wait_timeout_is_pending_with_the_submission_id(tmp_path: Path) -> None:
    """FAR-2577: a --wait timeout parses the id and exits with the distinct PENDING code."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(tmp_path, _NOTARY_TOOL, _pending_args(tmp_path, dmg, "2"), _timed_out_submit_env(tmp_path, "In Progress"))

    assert result.returncode == _NOTARY_PENDING_EXIT, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "notarization status is unknown" not in result.stderr
    assert f"outcome=pending submission_id={_TIMEOUT_SUBMISSION_ID}" in result.stdout
    evidence = _env_file(tmp_path / "evidence.env")
    assert evidence["outcome"] == "pending"
    assert evidence["submission_id"] == _TIMEOUT_SUBMISSION_ID
    assert evidence["status"] == "In Progress"
    record = _env_file(tmp_path / "submission" / "submission.env")
    assert record["phase"] == "pending"
    assert record["submission_id"] == _TIMEOUT_SUBMISSION_ID
    assert record["cdhash"] == "8888888888888888888888888888888888888888"
    # Apple was actively polled with the parsed id, not just waited on.
    assert f"notarytool info {_TIMEOUT_SUBMISSION_ID}" in (tmp_path / "notary-calls.log").read_text(encoding="utf-8")


def test_macos_notarization_pending_then_accepted_continues_normally(tmp_path: Path) -> None:
    """FAR-2577: Accepted during the poll is an ordinary success."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(
        tmp_path,
        _NOTARY_TOOL,
        _pending_args(tmp_path, dmg, "30"),
        _timed_out_submit_env(tmp_path, "In Progress,Accepted"),
    )

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    evidence = _env_file(tmp_path / "evidence.env")
    assert evidence["outcome"] == "accepted"
    assert evidence["status"] == "Accepted"
    assert evidence["submission_id"] == _TIMEOUT_SUBMISSION_ID
    assert _env_file(tmp_path / "submission" / "submission.env")["phase"] == "accepted"
    assert (tmp_path / "info-count").read_text(encoding="utf-8") == "2"


def test_macos_notarization_pending_then_invalid_still_hard_fails(tmp_path: Path) -> None:
    """FAR-2577: Invalid stays a hard failure even when it arrives after a timeout."""
    dmg = _notary_dmg(tmp_path)
    result = _run_tool(tmp_path, _NOTARY_TOOL, _pending_args(tmp_path, dmg, "30"), _timed_out_submit_env(tmp_path, "Invalid"))

    assert result.returncode == 1, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "no ticket will be issued" in result.stderr
    assert f"notarytool log {_TIMEOUT_SUBMISSION_ID}" in (tmp_path / "notary-calls.log").read_text(encoding="utf-8")
    assert _env_file(tmp_path / "submission" / "submission.env")["phase"] == "rejected"
    assert not (tmp_path / "evidence.env").exists()


def test_macos_notarization_non_timeout_failure_is_not_pending(tmp_path: Path) -> None:
    """FAR-2577: only a --wait timeout is pending; any other non-zero exit fails."""
    dmg = _notary_dmg(tmp_path)
    env = _timed_out_submit_env(tmp_path, "In Progress")
    env["FAKE_NOTARY_SUBMIT_STDERR"] = json.dumps({"message": "HTTP status code: 401. Unable to authenticate.", "id": ""})
    env["FAKE_NOTARY_SUBMIT_EXIT"] = "69"
    result = _run_tool(tmp_path, _NOTARY_TOOL, _pending_args(tmp_path, dmg, "30"), env)

    assert result.returncode == 1
    assert "notarytool submit exited 69" in result.stderr
    assert not (tmp_path / "info-count").exists()


def test_macos_notarization_records_the_submission_before_the_verdict(tmp_path: Path) -> None:
    """FAR-2577: the record exists even when the submit result is unusable."""
    dmg = _notary_dmg(tmp_path)
    env = _timed_out_submit_env(tmp_path, "In Progress")
    env["FAKE_NOTARY_SUBMIT_STDERR"] = "notarytool: connection reset"
    result = _run_tool(tmp_path, _NOTARY_TOOL, _pending_args(tmp_path, dmg, "30"), env)

    assert result.returncode == 1
    assert "notarization status is unknown" in result.stderr
    record = _env_file(tmp_path / "submission" / "submission.env")
    assert record["phase"] == "unknown"
    assert len(record["sha256_submitted"]) == 64


def _status_record(tmp_path: Path, submission_id: str) -> Path:
    record = tmp_path / "submission.env"
    record.write_text(f"phase=pending\nsubmission_id={submission_id}\nstatus=In Progress\n", encoding="utf-8")
    return record


def _status_args(tmp_path: Path, record: Path) -> list[str]:
    key = tmp_path / "notary.p8"
    key.write_text("fixture", encoding="utf-8")
    return ["--record", record.as_posix(), "--key", key.as_posix(), "--key-id", "FAKEKEYID", "--issuer", "00000000-0000-0000-0000-000000000000"]


@pytest.mark.parametrize(("apple_status", "expected_exit"), [("Accepted", 0), ("In Progress", 0), ("Invalid", 4), ("Rejected", 4)])
def test_macos_notary_status_helper_flags_only_a_terminal_rejection(tmp_path: Path, apple_status: str, expected_exit: int) -> None:
    """FAR-2577: the staple retry can tell a still-pending submission from a rejected one."""
    record = _status_record(tmp_path, _TIMEOUT_SUBMISSION_ID)
    result = _run_tool(
        tmp_path,
        _NOTARY_STATUS_TOOL,
        _status_args(tmp_path, record),
        {"FAKE_NOTARY_INFO_SEQUENCE": apple_status, "FAKE_NOTARY_CALLS": (tmp_path / "calls.log").as_posix()},
    )

    assert result.returncode == expected_exit, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"notary_status={apple_status}" in result.stdout
    assert f"submission_id={_TIMEOUT_SUBMISSION_ID}" in result.stdout


def test_macos_notary_status_helper_treats_an_unreadable_verdict_as_unknown(tmp_path: Path) -> None:
    """FAR-2577: a failed query is transient and must never be read as a rejection."""
    record = _status_record(tmp_path, _TIMEOUT_SUBMISSION_ID)
    result = _run_tool(
        tmp_path,
        _NOTARY_STATUS_TOOL,
        _status_args(tmp_path, record),
        {"FAKE_NOTARY_INFO_SEQUENCE": "Invalid", "FAKE_NOTARY_INFO_EXIT": "1"},
    )

    assert result.returncode == 0
    assert "notary_status=unknown" in result.stdout


def test_macos_notary_status_helper_without_an_id_is_unknown(tmp_path: Path) -> None:
    record = _status_record(tmp_path, "")
    result = _run_tool(tmp_path, _NOTARY_STATUS_TOOL, _status_args(tmp_path, record), {"FAKE_NOTARY_CALLS": (tmp_path / "calls.log").as_posix()})

    assert result.returncode == 0
    assert "notary_status=unknown" in result.stdout
    assert not (tmp_path / "calls.log").exists()


def test_macos_workflow_treats_a_pending_notarization_as_a_held_success() -> None:
    """FAR-2577: exit 3 continues unstapled; every other non-zero exit still fails."""
    step, build_run = _native_installer_step()

    assert step.get("id") == "native"
    assert '--submission-out "$GITHUB_WORKSPACE/build/notary-submission/submission.env"' in build_run
    assert '--evidence "$DATA/notarization-evidence.env" || notary_exit=$?' in build_run
    case_block = build_run[build_run.index('case "$notary_exit" in') :]
    case_block = case_block[: case_block.index("esac")]
    assert "0) notarization_outcome=accepted ;;" in case_block
    assert "notarization_outcome=pending" in case_block
    assert 'exit "$notary_exit"' in case_block
    assert "printf 'notarization_submission_id=%s\\n'" in build_run

    # Pending skips stapling (no ticket can exist) and defers only the ticket
    # gates, with a reason install.sh recognises.
    pending_branch = build_run[build_run.index("dmg_ticket_deferral_reason=apple_cloudkit_ticket_unavailable") :]
    pending_branch = pending_branch[: pending_branch.index("\nelse\n")]
    assert "dmg_staple_ticket_unavailable=1" in pending_branch
    assert "dmg_ticket_deferral_reason=apple_notarization_pending" in pending_branch
    assert "staple_dmg_with_retry" not in pending_branch
    assert 'MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET_REASON="$dmg_ticket_deferral_reason"' in build_run
    install_sh = (REPO_ROOT / "scripts" / "macos" / "install.sh").read_text(encoding="utf-8")
    assert "'apple_notarization_pending'" in install_sh
    # Hard gates that do not depend on a ticket are still unconditional.
    assert "run_pre_smoke dmg_codesign_verify codesign --verify" in build_run
    assert "run_pre_smoke dmg_verify hdiutil verify" in build_run

    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    outputs = workflow["jobs"]["build"]["outputs"]
    assert outputs["notarization_outcome"] == "${{ steps.native.outputs.notarization_outcome }}"
    assert outputs["notarization_submission_id"] == "${{ steps.native.outputs.notarization_submission_id }}"
    triggers = workflow.get("on", workflow.get(True))
    assert "notarization_submission_id" in triggers["workflow_call"]["outputs"]


def test_macos_workflow_preserves_the_submission_whatever_the_verdict() -> None:
    """FAR-2577: the submission record and submitted DMG survive any outcome."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    by_name = {step.get("name"): step for step in steps}
    names = [step.get("name") for step in steps]

    record = by_name["Upload the notarization submission record"]
    assert record["if"].startswith("${{ always() && ")
    assert "hashFiles('build/notary-submission/submission.env')" in record["if"]
    assert record["with"]["path"] == "build/notary-submission/submission.env"
    assert record["with"]["name"].startswith("macos-notary-submission-")

    preserved = by_name["Preserve the submitted DMG after an unsuccessful run"]
    assert "(failure() || cancelled())" in preserved["if"]
    assert preserved["with"]["path"] == by_name["Upload real macOS installer"]["with"]["path"]
    assert preserved["with"]["name"] != by_name["Upload real macOS installer"]["with"]["name"]

    native = names.index("Sign, notarize and smoke-test native installer")
    assert native < names.index("Upload the notarization submission record")
    assert native < names.index("Preserve the submitted DMG after an unsuccessful run")


# ---------------------------------------------------------------------------
# FAR-2588: macOS ships /bin/bash 3.2. Under `set -u` it rejects an EMPTY
# "${ARR[@]}" as an unbound variable AND exits 0 (the EXIT trap also sees
# $? == 0). Run 44680480922 died that way right after
# "pre-install: detached mount", skipped every post-install assertion, and
# reported success. These tests run the extracted blocks under the host bash
# and, when Docker is available, under the real bash 3.2.57 (official
# `bash:3.2` image).
# ---------------------------------------------------------------------------

_BASH32_IMAGE = "bash:3.2"


def _bash32_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    for args in (["image", "inspect", _BASH32_IMAGE], ["pull", "-q", _BASH32_IMAGE]):
        try:
            result = subprocess.run(  # noqa: S603 - fixed docker CLI and pinned public image
                [docker, *args], capture_output=True, text=True, timeout=180, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode == 0:
            return True
    return False


def _run_bash_stdin(flavour: str, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``script`` from stdin under the host bash or the real bash 3.2."""
    if flavour == "host":
        command = [_bash_executable(), "--noprofile", "--norc", "-s", "--", *args]
    else:
        if not _bash32_available():
            pytest.skip("docker with the bash:3.2 image is required for the bash 3.2 variant")
        docker = shutil.which("docker")
        assert docker is not None
        command = [docker, "run", "--rm", "-i", _BASH32_IMAGE, "bash", "--noprofile", "--norc", "-s", "--", *args]
    # Bytes, not text mode: on Windows text mode turns every newline into CRLF
    # and bash then reads "pipefail\r" as an option name.
    raw = subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned workflow text
        command,
        input=script.encode("utf-8"),
        capture_output=True,
        timeout=180,
        check=False,
    )
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
        raw.stderr.decode("utf-8", "replace").replace("\r\n", "\n"),
    )


_BASH_FLAVOURS = ["host", "bash-3.2"]


def _install_sh_harness(block: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            'dmg_staple_ticket_unavailable="$1"',
            "dmg_ticket_deferral_reason=apple_cloudkit_ticket_unavailable",
            'INSTALL_ROOT="/tmp/modulo install"',
            "DATA=/tmp/modulo-native-smoke-data",
            'DMG_WITH_SPACES="/tmp/Farna Control 0.0.0 macos arm64.dmg"',
            "run_assertion() { printf 'argv:%s\\n' \"$@\"; }",
            block,
            "echo REACHED_AFTER_INSTALL_ASSERTION",
            "",
        ]
    )


def _argv_lines(stdout: str) -> list[str]:
    return [line.removeprefix("argv:") for line in stdout.splitlines() if line.startswith("argv:")]


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_install_sh_assertion_runs_with_an_empty_install_env(flavour: str) -> None:
    """FAR-2588: the stapled (normal) path leaves INSTALL_ENV empty; the install
    assertion must still run instead of dying on an unbound-variable error."""
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "install_sh_assertion")

    result = _run_bash_stdin(flavour, _install_sh_harness(block), ["0"])

    assert result.returncode == 0, result.stderr
    assert "unbound variable" not in result.stderr
    assert "REACHED_AFTER_INSTALL_ASSERTION" in result.stdout
    argv = _argv_lines(result.stdout)
    assert argv[:3] == ["install_sh", "bash", "-c"]
    assert argv[-3:] == ["/tmp/modulo install", "/tmp/modulo-native-smoke-data", "/tmp/Farna Control 0.0.0 macos arm64.dmg"]


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_install_sh_assertion_passes_the_deferral_env_when_the_ticket_is_unavailable(flavour: str) -> None:
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "install_sh_assertion")

    result = _run_bash_stdin(flavour, _install_sh_harness(block), ["1"])

    assert result.returncode == 0, result.stderr
    argv = _argv_lines(result.stdout)
    assert argv[:4] == [
        "install_sh",
        "env",
        "MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET=1",
        "MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET_REASON=apple_cloudkit_ticket_unavailable",
    ]
    assert argv[4] == "bash"


def test_macos_install_sh_harness_detects_the_unguarded_expansion_under_bash32() -> None:
    """Negative control: the pre-FAR-2588 unguarded expansion really is fatal
    under bash 3.2, so the harness above would have caught run 44680480922."""
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "install_sh_assertion")
    unguarded = block.replace('${INSTALL_ENV[@]+"${INSTALL_ENV[@]}"}', '"${INSTALL_ENV[@]}"')
    assert unguarded != block

    result = _run_bash_stdin("bash-3.2", _install_sh_harness(unguarded), ["0"])

    assert "INSTALL_ENV[@]: unbound variable" in result.stderr
    assert "REACHED_AFTER_INSTALL_ASSERTION" not in result.stdout


def _exit_status_harness(block: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            block,
            "cleanup() {",
            '  local reported_status="${1:-0}" original_status',
            '  original_status="$(smoke_effective_exit_status "$reported_status")"',
            '  printf \'cleanup result: reported_status=%s original_status=%s\\n\' "$reported_status" "$original_status"',
            '  exit "$original_status"',
            "}",
            "trap 'cleanup \"$?\"' EXIT",
            "EMPTY=()",
            'case "$1" in',
            # bash 3.2 aborts on the empty array (and exits 0); bash 4.4+
            # accepts it and aborts on the unset variable instead.
            '  crash) printf \'%s\\n\' "${EMPTY[@]}" "$UNSET_SMOKE_VARIABLE" ;;',
            "  early_exit) exit 0 ;;",
            "  fail) false ;;",
            "  ok) : ;;",
            "esac",
            "SMOKE_BODY_COMPLETED=1",
            "",
        ]
    )


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
@pytest.mark.parametrize("mode", ["crash", "fail", "early_exit"])
def test_macos_smoke_trap_fails_when_the_body_dies_before_cleanup(flavour: str, mode: str) -> None:
    """FAR-2588: an exit before the body's last statement turns the job red,
    including bash 3.2's unbound-variable abort that reports status 0."""
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "smoke_effective_exit_status")

    result = _run_bash_stdin(flavour, _exit_status_harness(block), [mode])

    assert result.returncode != 0, result.stdout + result.stderr
    assert "original_status=0" not in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_smoke_trap_succeeds_when_the_body_completes(flavour: str) -> None:
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "smoke_effective_exit_status")

    result = _run_bash_stdin(flavour, _exit_status_harness(block), ["ok"])

    assert result.returncode == 0, result.stderr
    assert "original_status=0" in result.stdout


def _shell_statements(script: str) -> list[str]:
    return [line.strip() for line in script.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_macos_smoke_cleanup_derives_its_status_from_the_completion_sentinel() -> None:
    """FAR-2588: the real cleanup trap captures $? first, routes it through the
    sentinel check, and the sentinel is set only by the step's last statement."""
    _step, build_run = _native_installer_step()

    assert "trap 'cleanup \"$?\"' EXIT" in build_run
    cleanup_statements = _shell_statements(build_run[build_run.index("cleanup() {") :])
    assert cleanup_statements[1] == 'local reported_status="${1:-0}"'
    assert 'original_status="$(smoke_effective_exit_status "$reported_status")"' in cleanup_statements[:5]
    assert _shell_statements(build_run)[-1] == "SMOKE_BODY_COMPLETED=1"
    assert _shell_statements(build_run).count("SMOKE_BODY_COMPLETED=1") == 1
    assert build_run.index("SMOKE_BODY_COMPLETED=0") < build_run.index("trap 'cleanup \"$?\"' EXIT")


def _macos_bash_sources() -> dict[str, str]:
    """Every bash text that runs under macOS /bin/bash 3.2: each run block of
    the macOS build job plus the repository scripts those blocks execute."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    sources: dict[str, str] = {}
    for step in workflow["jobs"]["build"]["steps"]:
        run = step.get("run")
        if isinstance(run, str):
            sources[f"macos-installer.yml::{step.get('name')}"] = run
    scripts = sorted((REPO_ROOT / "scripts" / "macos").glob("*.sh"))
    scripts += [REPO_ROOT / "scripts" / "native-stack-assertions.sh", REPO_ROOT / "scripts" / "create-macos-info-plist.sh"]
    for path in scripts:
        sources[path.relative_to(REPO_ROOT).as_posix()] = path.read_text(encoding="utf-8")
    return sources


_GUARDED_ARRAY = re.compile(r'\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[@\]\+"\$\{(?P=name)\[@\]\}"\}')
_ANY_ARRAY_EXPANSION = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[[@*]\]\}")

#: Constructs that do not exist (or behave differently) in bash 3.2.
_BASH4_ONLY = {
    "mapfile/readarray": re.compile(r"\b(mapfile|readarray)\b"),
    "case-modification expansion": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?(,,?|\^\^?)\}"),
    "associative array": re.compile(r"\b(declare|local|typeset)\s+-[a-zA-Z]*A"),
    "nameref": re.compile(r"\b(declare|local|typeset)\s+-[a-zA-Z]*n\b"),
    "append-both redirection &>>": re.compile(r"&>>"),
    "pipe-stderr |&": re.compile(r"\|&"),
    "wait -n": re.compile(r"\bwait\s+-n\b"),
    "[[ -v ]] test": re.compile(r"\[\[\s+-v\s"),
    "transformation expansion ${x@Q}": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*@[QEPAaKk]\}"),
    "negative array index": re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\[-[0-9]+\]\}"),
    "case fallthrough ;& / ;;&": re.compile(r";;&|;&\s*$", re.MULTILINE),
    "coproc": re.compile(r"\bcoproc\b"),
    "globstar": re.compile(r"\bglobstar\b"),
    "dynamic fd {var}>": re.compile(r"\bexec\s+\{[A-Za-z_]+\}[<>]"),
    "EPOCHSECONDS/EPOCHREALTIME": re.compile(r"\bEPOCH(SECONDS|REALTIME)\b"),
}


def _code_only(text: str) -> str:
    """Drop whole-line comments so prose that names a construct is not flagged."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _unguarded_array_expansions(text: str) -> list[str]:
    code = _GUARDED_ARRAY.sub("<guarded>", _code_only(text))
    return [match.group("name") for match in _ANY_ARRAY_EXPANSION.finditer(code)]


def test_macos_bash_never_expands_a_possibly_empty_array_unguarded() -> None:
    """FAR-2588: under bash 3.2 + set -u an empty "${X[@]}" aborts the script
    with exit 0. Every element expansion must use ${X[@]+"${X[@]}"}."""
    offenders = [f"{label}: {name}" for label, text in _macos_bash_sources().items() for name in _unguarded_array_expansions(text)]
    assert not offenders, offenders


def test_macos_bash_uses_no_bash4_only_constructs() -> None:
    offenders: list[str] = []
    for label, text in _macos_bash_sources().items():
        code = _code_only(text)
        offenders.extend(f"{label}: {construct}" for construct, pattern in _BASH4_ONLY.items() if pattern.search(code))
    assert not offenders, offenders


def test_macos_bash_scanner_flags_the_run_44680480922_expansion() -> None:
    """The scanner must catch the exact expansion that masked the crash."""
    assert _unguarded_array_expansions('run_assertion "install_sh" "${INSTALL_ENV[@]}" bash -c true') == ["INSTALL_ENV"]
    assert _BASH4_ONLY["mapfile/readarray"].search("mapfile -t LINES < file")


def test_macos_gatekeeper_rejection_prints_signature_diagnostics_before_failing() -> None:
    """FAR-2593: a mounted-app spctl rejection names the offending file instead
    of only "a sealed resource is missing or invalid"."""
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "mounted_app_spctl_with_retry")

    assert 'run_pre_smoke mounted_app_spctl spctl --assess --type execute "$MOUNTED_APP"' in block
    assert 'bash scripts/macos/signature-diagnostics.sh mounted_app "$MOUNTED_APP" "$APP"' in block
    assert re.search(r"signature-diagnostics\.sh mounted_app[^\n]*\n[^\n]*endgroup[^\n]*\n\s+exit 1\n", block)
    # The retry block is the only Gatekeeper assessment of the mounted app.
    assert build_run.count("run_pre_smoke mounted_app_spctl spctl --assess --type execute") == 1


def _spctl_retry_harness(block: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            'DATA="$(mktemp -d)"',
            "MOUNTED_APP=/mnt/app",
            "APP=build/app",
            'VERDICTS="$1"',
            # Stub the pause and the diagnostics helper so the harness only
            # observes what the retry block asks for.
            "sleep() { printf 'SLEPT %s\\n' \"$1\"; }",
            "bash() { printf 'DIAGNOSTICS %s\\n' \"$*\"; }",
            "run_pre_smoke() {",
            '  local name="$1" verdict',
            '  verdict="${VERDICTS%%,*}"',
            '  if [[ "$VERDICTS" == *,* ]]; then VERDICTS="${VERDICTS#*,}"; else VERDICTS=""; fi',
            "  printf 'CALL %s\\n' \"$name\"",
            '  case "$verdict" in',
            "    ok) printf 'accepted\\n' >\"$DATA/pre-smoke-$name.log\"; return 0 ;;",
            '    sealed) printf \'%s: a sealed resource is missing or invalid\\n\' "$MOUNTED_APP" >"$DATA/pre-smoke-$name.log"; return 1 ;;',
            '    unnotarized) printf \'%s: rejected\\nsource=Unnotarized Developer ID\\n\' "$MOUNTED_APP" >"$DATA/pre-smoke-$name.log"; return 3 ;;',
            "    *) printf 'harness: no verdict left\\n' >&2; return 99 ;;",
            "  esac",
            "}",
            block,
            "printf 'REACHED_AFTER_GATE\\n'",
            "",
        ]
    )


# --- FAR-2589: the macOS app reports its build version ------------------------


def _fake_macos_app(tmp_path: Path, *, info_plist: bool = True) -> Path:
    """A macOS app layout holding the real farna.version, returning its bundle root.

    The stub interpreter stands in for the bundled runtime + its relative .pth:
    the gate strips PYTHONPATH, and the stub points the import path at the
    bundle's own backend/src copy, which is what the .pth does in the app.
    """
    contents = tmp_path / "FarnaControl.app" / "Contents"
    bundle_root = contents / "Resources" / "bundle"
    package = bundle_root / "backend" / "src" / "farna"
    package.mkdir(parents=True)
    for name in ("__init__.py", "version.py"):
        shutil.copyfile(REPO_ROOT / "backend" / "src" / "farna" / name, package / name)
    if info_plist:
        (contents / "Info.plist").write_text("<plist/>\n", encoding="utf-8")
    _write_executable(
        bundle_root / "runtime" / "bin" / "python3",
        "#!/usr/bin/env bash\n" + f'export PYTHONPATH="{(bundle_root / "backend" / "src").as_posix()}"\n' + f'exec "{Path(sys.executable).as_posix()}" "$@"\n',
    )
    return bundle_root


def _run_version_stamp(tmp_path: Path, bundle_root: Path, version: str) -> subprocess.CompletedProcess[str]:
    block = _extract_marked_bash(_bundle_build_script(), "bundle_version_stamp")
    driver = tmp_path / "version_stamp_driver.sh"
    driver.write_text('#!/usr/bin/env bash\nset -euo pipefail\nVERSION="$1"\nBUNDLE_ROOT="$2"\n' + block + "\n", encoding="utf-8")
    return _run_bash_file(driver, [version, bundle_root.as_posix()], cwd=tmp_path)


def test_macos_bundle_version_stamp_is_what_the_bundled_app_reports(tmp_path: Path) -> None:
    """FAR-2589: the stamp the build writes is the one farna.version resolves
    inside the app layout, so /healthz/ready reports the build version."""
    bundle_root = _fake_macos_app(tmp_path)

    result = _run_version_stamp(tmp_path, bundle_root, "9.8.7")

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "bundle version stamp: 9.8.7" in result.stdout
    assert (bundle_root / "VERSION").read_text(encoding="utf-8") == "bundle-v9.8.7\n"


def test_macos_bundle_version_gate_fails_when_the_app_cannot_resolve_the_stamp(tmp_path: Path) -> None:
    """Without the app marker the stamp is ignored; the build gate must fail
    rather than ship an app that reports another version."""
    bundle_root = _fake_macos_app(tmp_path, info_plist=False)

    result = _run_version_stamp(tmp_path, bundle_root, "9.8.7")

    assert result.returncode != 0, result.stdout
    assert "expected the build version '9.8.7'" in result.stderr


def test_macos_smoke_asserts_the_running_app_reports_the_build_version() -> None:
    _step, build_run = _native_installer_step()

    assert 'SMOKE_EXPECTED_VERSION="$3" bash scripts/native-stack-assertions.sh' in build_run
    assert '\' bash "$INSTALL_ROOT" "$DATA" "$VERSION_NUM" || exit 1' in build_run
    assertions = (REPO_ROOT / "scripts" / "native-stack-assertions.sh").read_text(encoding="utf-8")
    assert '[[ "${reported_version}" == "${SMOKE_EXPECTED_VERSION}" ]]' in assertions


# --- FAR-2589: every smoke assertion's own line reaches the log ---------------

_PIPED_RUN_ASSERTION = re.compile(r"^\s*run_assertion\b[^'\n]*\|", re.MULTILINE)


def _logical_lines(script: str) -> str:
    return script.replace("\\n", " ")


def test_macos_smoke_never_pipes_run_assertion_output_away() -> None:
    """Piping run_assertion into grep hides its `assertion:` line from the log."""
    _step, build_run = _native_installer_step()

    assert not _PIPED_RUN_ASSERTION.findall(_logical_lines(build_run))
    historical = 'run_assertion "service_binary_macho" \\n  file "$SERVICE_BINARY" | grep -E "Mach-O" || exit 1\n'
    assert _PIPED_RUN_ASSERTION.findall(_logical_lines(historical))


def _macho_harness(run_assertion_block: str, macho_block: str) -> str:
    return "\n".join(
        [
            "set -euo pipefail",
            'stub="$(mktemp -d)"',
            'DATA="$(mktemp -d)"',
            "cat > \"$stub/file\" <<'STUB'",
            "#!/bin/sh",
            'echo "$1: Mach-O 64-bit executable arm64"',
            "STUB",
            'chmod +x "$stub/file"',
            'PATH="$stub:$PATH"',
            "redact_pre_smoke_output() { cat; }",
            run_assertion_block,
            'SERVICE_BINARY="/opt/Farna Control.app/Contents/Resources/bundle/runtime/bin/python3"',
            'MACOS_ARCH="$1"',
            macho_block,
            "echo REACHED_AFTER_MACHO_ASSERTION",
            "",
        ]
    )


def _run_spctl_retry(flavour: str, verdicts: str) -> subprocess.CompletedProcess[str]:
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "mounted_app_spctl_with_retry")
    return _run_bash_stdin(flavour, _spctl_retry_harness(block), [verdicts])


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_mounted_app_spctl_retries_the_sealed_resource_flake_then_passes(flavour: str) -> None:
    """FAR-2593: the Intel first-assessment flake (run 44735390410) is re-assessed and the gate passes."""
    result = _run_spctl_retry(flavour, "sealed,ok")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("CALL mounted_app_spctl") == 2
    assert "mounted_app_spctl: attempt=2/3 verdict=accepted" in result.stdout
    assert "attempt=1/3 verdict=sealed-resource rejection" in result.stderr
    assert "SLEPT 10" in result.stdout
    assert "DIAGNOSTICS" not in result.stdout
    assert "REACHED_AFTER_GATE" in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_mounted_app_spctl_accepts_on_the_first_attempt_without_pausing(flavour: str) -> None:
    result = _run_spctl_retry(flavour, "ok")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("CALL mounted_app_spctl") == 1
    assert "mounted_app_spctl: attempt=1/3 verdict=accepted" in result.stdout
    assert "SLEPT" not in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_mounted_app_spctl_still_fails_hard_when_every_attempt_is_rejected(flavour: str) -> None:
    """The retry is bounded: three sealed-resource verdicts fail the job, and a fourth is never requested."""
    result = _run_spctl_retry(flavour, "sealed,sealed,sealed,ok")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.count("CALL mounted_app_spctl") == 3
    assert result.stdout.count("SLEPT 10") == 2
    assert "DIAGNOSTICS scripts/macos/signature-diagnostics.sh mounted_app /mnt/app build/app" in result.stdout
    assert "REACHED_AFTER_GATE" not in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_mounted_app_spctl_never_retries_any_other_rejection(flavour: str) -> None:
    """Only the known sealed-resource verdict is re-assessed; an unnotarized verdict fails at once."""
    result = _run_spctl_retry(flavour, "unnotarized,ok")

    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.count("CALL mounted_app_spctl") == 1
    assert "not the known sealed-resource flake; not retried" in result.stderr
    assert "SLEPT" not in result.stdout
    assert "DIAGNOSTICS" in result.stdout
    assert "REACHED_AFTER_GATE" not in result.stdout


def test_macos_signed_bundle_is_diagnosed_before_it_is_staged() -> None:
    _step, build_run = _native_installer_step()

    order = [
        'run_pre_smoke nested_macho_signature_audit bash scripts/macos/macho-inventory.sh audit "$APP"',
        'bash scripts/macos/signature-diagnostics.sh --quick pre_dmg "$APP"',
        "hdiutil_with_resource_busy_retry dmg_create bash -c",
    ]
    positions = [build_run.index(command) for command in order]
    assert positions == sorted(positions)


def test_macos_mounted_app_codesign_agrees_with_gatekeeper_strictness() -> None:
    _step, build_run = _native_installer_step()

    assert 'run_pre_smoke mounted_app_codesign codesign --verify --deep --strict "$MOUNTED_APP"' in build_run
    assert 'run_pre_smoke app_codesign_verify codesign --verify --deep --strict --verbose=2 "$APP"' in build_run


def test_macos_signature_diagnostics_helper_never_fails_its_caller() -> None:
    helper = (REPO_ROOT / "scripts" / "macos" / "signature-diagnostics.sh").read_text(encoding="utf-8")
    code = _code_only(helper)

    assert "set -e" not in code.replace("set -uo pipefail", "")
    assert code.rstrip().endswith("exit 0")
    assert "codesign --verify --deep --strict --verbose=4" in code
    assert "spctl -a -vvv -t exec" in code
    assert "SecStaticCodeCheckValidityWithErrors" in code


def _signature_diagnostics_helper() -> str:
    return (REPO_ROOT / "scripts" / "macos" / "signature-diagnostics.sh").read_text(encoding="utf-8")


_DIAGNOSTIC_TOOLS = ("codesign", "spctl", "syspolicy_check", "xcrun", "log", "python3", "xattr", "diff", "find", "lipo")


def test_macos_signature_diagnostics_bound_every_external_tool_call() -> None:
    """FAR-2593: a stalled tool must never hang the job, so no diagnostic tool
    is invoked except through the time-bounded runner."""
    code = _code_only(_signature_diagnostics_helper())
    tool = "|".join(_DIAGNOSTIC_TOOLS)
    unbounded = [line.strip() for line in code.splitlines() if re.match(rf"\s*(?:if\s+!?\s*)?(?:{tool})\s", line)]
    assert not unbounded, unbounded
    assert re.search(r"run_bounded \d+ codesign --verify --deep --strict --verbose=4", code)
    assert re.search(r"run_bounded \d+ spctl -a -vvv -t exec", code)
    assert re.search(r'run_bounded \d+ python3 -I "\$work_dir/scan.py"', code)
    # The per-file lipo loop took 20 minutes in run 44743991976; fat Mach-O
    # detection is now part of the single-pass scan.
    assert "lipo" not in code


def test_macos_signature_diagnostics_quick_mode_skips_the_whole_bundle_scans() -> None:
    code = _signature_diagnostics_helper()
    quick_branch = code.index('if [[ "$mode" == quick ]]; then')
    quick_exit = code.index("exit 0", quick_branch)

    for heavy in ("for attempt in 1 2 3", "syspolicy_check distribution", "xcrun swiftc", "log show", 'scan.py"', "xattr -r", "diff -rq"):
        assert code.index(heavy) > quick_exit, heavy


def _embedded_bundle_scan() -> str:
    """Return the Python scanner embedded in signature-diagnostics.sh."""
    helper = _signature_diagnostics_helper()
    opener = "cat >\"$work_dir/scan.py\" <<'PY'\n"
    start = helper.index(opener) + len(opener)
    end = helper.index("\nPY\n", start)
    return helper[start:end] + "\n"


def _run_embedded_bundle_scan(app: Path) -> subprocess.CompletedProcess[str]:
    scan = app.parent / "scan.py"
    scan.write_text(_embedded_bundle_scan(), encoding="utf-8")
    return subprocess.run(  # noqa: S603 - repository-owned scanner and a temp bundle
        [sys.executable, "-I", scan.as_posix(), app.as_posix()],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _sealed_bundle(tmp_path: Path, *, sealed_bytes: bytes, stored_bytes: bytes) -> Path:
    app = tmp_path / "Farna Control.app"
    contents = app / "Contents"
    (contents / "_CodeSignature").mkdir(parents=True)
    (contents / "Info.plist").write_bytes(sealed_bytes)
    (contents / "_CodeSignature" / "CodeResources").write_bytes(plistlib.dumps({"files2": {"Info.plist": stored_bytes}}))
    return app


@pytest.mark.parametrize("shape", ["raw-bytes", "dict-hash2"])
def test_macos_bundle_scan_flags_a_tampered_sealed_file(tmp_path: Path, shape: str) -> None:
    """FAR-2593 review: Apple stores an ordinary sealed file as a raw <data>
    digest directly under files2[path] (plistlib returns bytes), not only the
    dict hash2 form. Both shapes must be hashed and compared, naming a file
    whose contents no longer match its seal."""
    info = b"sealed contents\n"
    expected = hashlib.sha256(info).digest()
    stored = expected if shape == "raw-bytes" else {"hash2": expected}
    app = _sealed_bundle(tmp_path, sealed_bytes=info + b"tampered\n", stored_bytes=stored)

    result = _run_embedded_bundle_scan(app)

    assert result.returncode == 0, result.stderr
    assert "seal_hash_mismatch Info.plist" in result.stdout, result.stdout
    assert "seal_entries=1 seal_problems=1" in result.stdout


def test_macos_bundle_scan_accepts_a_matching_raw_digest_sealed_file(tmp_path: Path) -> None:
    info = b"sealed contents\n"
    app = _sealed_bundle(tmp_path, sealed_bytes=info, stored_bytes=hashlib.sha256(info).digest())

    result = _run_embedded_bundle_scan(app)

    assert result.returncode == 0, result.stderr
    assert "seal_hash_mismatch" not in result.stdout, result.stdout
    assert "seal_entries=1 seal_problems=0" in result.stdout


@pytest.mark.parametrize("tampered", [False, True])
def test_macos_bundle_scan_checks_a_legacy_sha1_raw_digest_with_sha1(tmp_path: Path, tampered: bool) -> None:
    """A raw <data> digest is the legacy 20-byte SHA-1 form; hashing it as
    SHA-256 would report every such file as tampered."""
    info = b"sealed contents\n"
    on_disk = info + b"tampered\n" if tampered else info
    app = _sealed_bundle(tmp_path, sealed_bytes=on_disk, stored_bytes=hashlib.sha1(info).digest())  # noqa: S324 - mirrors Apple's legacy seal digest

    result = _run_embedded_bundle_scan(app)

    assert result.returncode == 0, result.stderr
    assert ("seal_hash_mismatch Info.plist" in result.stdout) is tampered, result.stdout
    assert f"seal_entries=1 seal_problems={int(tampered)}" in result.stdout


def test_macos_bundle_scan_reports_a_digest_it_cannot_check(tmp_path: Path) -> None:
    app = _sealed_bundle(tmp_path, sealed_bytes=b"sealed contents\n", stored_bytes=b"\x00" * 7)

    result = _run_embedded_bundle_scan(app)

    assert result.returncode == 0, result.stderr
    assert "seal_unrecognised_digest Info.plist" in result.stdout, result.stdout
    assert "seal_entries=1 seal_problems=1" in result.stdout


def _run_bounded_harness(helper: str) -> str:
    start = helper.index("run_bounded() {")
    function = helper[start : helper.index("\n}\n", start) + 3]
    return "\n".join(
        [
            "set -uo pipefail",
            function,
            "started=$SECONDS",
            'run_bounded "$1" sleep 8',
            'printf "rc=%d elapsed=%d\\n" "$?" "$(( SECONDS - started ))"',
            'run_bounded 5 sh -c "exit 7"',
            'printf "fast_rc=%d\\n" "$?"',
            "",
        ]
    )


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_signature_diagnostics_runner_terminates_a_stalled_tool(flavour: str) -> None:
    result = _run_bash_stdin(flavour, _run_bounded_harness(_signature_diagnostics_helper()), ["1"])

    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(r"rc=(\d+) elapsed=(\d+)", result.stdout)
    assert match, result.stdout
    assert match.group(1) == "124"
    assert int(match.group(2)) < 6
    assert "sleep exceeded 1s and was terminated" in result.stdout
    # A tool that finishes inside its bound keeps its own exit status.
    assert "fast_rc=7" in result.stdout


def test_macos_contract_gates_the_signature_diagnostics_helper() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["build"]["steps"]
    contract = next(step["run"] for step in steps if step.get("name") == "Validate workflow contract")

    assert "test -x scripts/macos/signature-diagnostics.sh" in contract
    assert "shellcheck scripts/macos/signature-diagnostics.sh" in contract
    assert "bash -n scripts/macos/signature-diagnostics.sh" in contract


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_macho_assertion_logs_its_line_and_passes_on_a_match(flavour: str) -> None:
    _step, build_run = _native_installer_step()
    harness = _macho_harness(_extract_marked_bash(build_run, "run_assertion"), _extract_marked_bash(build_run, "service_binary_macho_assertion"))

    result = _run_bash_stdin(flavour, harness, ["arm64"])

    assert result.returncode == 0, result.stdout + result.stderr
    assert "assertion: service_binary_macho=ok (exit=0)" in result.stdout
    assert "Mach-O 64-bit executable arm64" in result.stdout
    assert "REACHED_AFTER_MACHO_ASSERTION" in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_macho_assertion_logs_its_line_and_fails_on_an_architecture_mismatch(flavour: str) -> None:
    _step, build_run = _native_installer_step()
    harness = _macho_harness(_extract_marked_bash(build_run, "run_assertion"), _extract_marked_bash(build_run, "service_binary_macho_assertion"))

    result = _run_bash_stdin(flavour, harness, ["x86_64"])

    assert result.returncode != 0, result.stdout
    assert "assertion: service_binary_macho=FAILED (exit=1)" in result.stdout
    assert "REACHED_AFTER_MACHO_ASSERTION" not in result.stdout


# --- FAR-2589 review: a command word glued to its quoted argument ------------
#
# `file"${APP_ROOT}/Contents/MacOS/modulo"` (a lost separator) is ONE valid
# word, so bash -n and shellcheck both accept it; on the macOS runner it runs a
# command named `file/.../modulo` and the build dies at its final check. Only a
# lexical scan catches the class before a runner does.

_HEREDOC_OPENER = re.compile(r"""<<-?\s*(['"]?)([A-Za-z_][A-Za-z0-9_]*)\1""")
_GLUED_COMMAND_WORD = re.compile(r"""^\s*([A-Za-z_][A-Za-z0-9_.-]+)["']""")


def _bash_code_lines(text: str) -> list[str]:
    """Non-comment shell lines, with heredoc bodies (C, Python, plist) skipped."""
    lines: list[str] = []
    terminator: str | None = None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        if line.lstrip().startswith("#"):
            continue
        opener = _HEREDOC_OPENER.search(line)
        if opener:
            terminator = opener.group(2)
        lines.append(line)
    return lines


def _glued_command_words(text: str) -> list[str]:
    return [line.strip() for line in _bash_code_lines(text) if _GLUED_COMMAND_WORD.match(line)]


def test_installer_bash_never_glues_a_command_word_to_its_quoted_argument() -> None:
    sources = _macos_bash_sources()
    for relative in ("scripts/install.sh", "scripts/linux/native-firstrun-smoke.sh"):
        sources[relative] = (REPO_ROOT / relative).read_text(encoding="utf-8")

    offenders = [f"{label}: {line}" for label, text in sources.items() for line in _glued_command_words(text)]

    assert not offenders, offenders


def test_glued_command_word_scan_flags_the_reviewed_build_script_typo() -> None:
    """Negative control: the exact PR #3291 line is caught; heredoc bodies are not."""
    script = _bundle_build_script()
    typo = script.replace('file "${APP_ROOT}/Contents/MacOS/modulo"', 'file"${APP_ROOT}/Contents/MacOS/modulo"')
    assert typo != script

    assert _glued_command_words(typo) == ['file"${APP_ROOT}/Contents/MacOS/modulo"']
    assert not _glued_command_words("cat <<'PY'\nprint\"x\"\nPY\n")


def test_macos_bundle_build_ends_with_the_launcher_identity_checks() -> None:
    """The build's last checks run right after the version stamp, verbatim."""
    script = _bundle_build_script()
    tail = script[script.index("# END bundle_version_stamp") :].splitlines()[1:5]

    assert tail == [
        'file "${APP_ROOT}/Contents/MacOS/modulo"',
        '"${BUNDLE_ROOT}/pg/bin/postgres" --version',
        '"${BUNDLE_ROOT}/redis/redis-server" --version',
        '"${APP_ROOT}/Contents/MacOS/modulo" --help >/dev/null',
    ]


# ---------------------------------------------------------------------------
# FAR-2629 / FAR-2631: `hdiutil` intermittently fails on the GitHub macOS
# runners with a transient "Resource busy" (EBUSY). FAR-2629 bounded `hdiutil
# create`; FAR-2631 found the same verdict at `pre_install_detach` (run
# 44923958746, the failure that blocked the 0.3.329-rc2 cut) and moved the retry
# into one shared helper that every EBUSY-capable call site routes through. The
# blocks are extracted verbatim from the workflow and executed against a stubbed
# hdiutil, so the classification these tests pin is the code the macOS job runs.
# ---------------------------------------------------------------------------

_HDIUTIL_RESOURCE_BUSY = "hdiutil: create failed - Resource busy"

#: The detach diagnostic observed in run 44923958746.
_HDIUTIL_DETACH_RESOURCE_BUSY = 'hdiutil: couldn\'t eject "disk5" - Resource busy'

#: Stand-in `hdiutil`. It fails with a configurable message/status for the
#: first FAKE_HDIUTIL_FAILURES calls, then creates its output target (the last
#: argument) so the workflow's `mv -f` has something to move. Call counts live
#: in a file because the workflow invokes it from a child `bash -c`.
_FAKE_HDIUTIL = """#!/usr/bin/env bash
set -u
target=""
for candidate in "$@"; do target="$candidate"; done
calls_file="${FAKE_HDIUTIL_CALLS:?}"
count=1
if [[ -f "$calls_file" ]]; then
  count=$(( $(cat "$calls_file") + 1 ))
fi
printf '%s\\n' "$count" >"$calls_file"
if [[ "$count" -le "${FAKE_HDIUTIL_FAILURES:-0}" ]]; then
  printf 'hdiutil: create failed - %s\\n' "${FAKE_HDIUTIL_MESSAGE:-Resource busy}" >&2
  exit "${FAKE_HDIUTIL_STATUS:-1}"
fi
printf 'created dmg\\n' >"$target"
exit 0
"""


def _workflow_bash_function(build_run: str, name: str) -> str:
    """Slice one workflow-defined bash function (``name() {`` through its ``}``)."""
    lines = build_run.splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == f"{name}() {{")
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == "}")
    return "\n".join(lines[start : end + 1])


def _dmg_create_harness(build_run: str) -> str:
    """Run the extracted creation block via the workflow's shared retry helper."""
    block = _extract_marked_bash(build_run, "dmg_create_with_resource_busy_retry")
    return "\n".join(
        [
            "set -euo pipefail",
            # The injected stub directory must be a POSIX path: bash does not
            # translate a PATH value it sets itself (see _posix_path).
            'PATH="$1:$PATH"',
            "export PATH",
            'work="$2"',
            'DATA="$(mktemp -d)"',
            'VERSION_NUM="0.3.999"',
            'DMG_STAGING="$work/staging"',
            'DMG_TEMP="$work/dmg-temp.dmg"',
            'DMG="$work/FarnaControl.dmg"',
            'mkdir -p "$DMG_STAGING"',
            "printf 'payload\\n' >\"$DMG_STAGING/placeholder.txt\"",
            _workflow_bash_function(build_run, "redact_pre_smoke_output"),
            _workflow_bash_function(build_run, "run_pre_smoke"),
            _extract_marked_bash(build_run, "hdiutil_with_resource_busy_retry"),
            block,
            "printf 'REACHED_AFTER_DMG_CREATE\\n'",
            "",
        ]
    )


def _run_dmg_create_harness(harness: str, args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run an extracted workflow block from stdin under the host bash."""
    raw = subprocess.run(  # noqa: S603 - fixed interpreter and repository-owned workflow text
        [_bash_executable(), "--noprofile", "--norc", "-s", "--", *args],
        input=harness.encode("utf-8"),
        capture_output=True,
        timeout=120,
        check=False,
        env={**os.environ, **(env or {})},
    )
    return subprocess.CompletedProcess(
        raw.args,
        raw.returncode,
        raw.stdout.decode("utf-8", "replace").replace("\r\n", "\n"),
        raw.stderr.decode("utf-8", "replace").replace("\r\n", "\n"),
    )


def _run_dmg_create(tmp_path: Path, **env_overrides: str) -> tuple[subprocess.CompletedProcess[str], int, Path]:
    """Execute the workflow's DMG creation block against the stub hdiutil."""
    _step, build_run = _native_installer_step()
    stub_dir = tmp_path / "stub-bin"
    calls_file = tmp_path / "hdiutil-calls"
    _write_executable(stub_dir / "hdiutil", _FAKE_HDIUTIL)
    work = tmp_path / "work"
    work.mkdir()
    env = {"FAKE_HDIUTIL_CALLS": _posix_path(calls_file), "HDIUTIL_RESOURCE_BUSY_RETRY_DELAY": "0"}
    env.update(env_overrides)

    result = _run_dmg_create_harness(
        _dmg_create_harness(build_run),
        [_posix_path(stub_dir), work.as_posix()],
        env=env,
    )
    calls = int(calls_file.read_text(encoding="utf-8").strip()) if calls_file.is_file() else 0
    return result, calls, work / "FarnaControl.dmg"


def test_macos_dmg_create_retries_the_resource_busy_failure_then_accepts(tmp_path: Path) -> None:
    """FAR-2629: one Resource busy attempt is retried and the next success ships."""
    result, calls, dmg = _run_dmg_create(tmp_path, FAKE_HDIUTIL_FAILURES="1")

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "REACHED_AFTER_DMG_CREATE" in result.stdout
    assert calls == 2
    assert "dmg_create: attempt=1/3 exit=1" in result.stdout
    assert "first lines of output" in result.stdout
    assert _HDIUTIL_RESOURCE_BUSY in result.stdout
    assert "dmg_create: retrying in 0s (Resource busy)" in result.stderr
    assert "dmg_create: attempt=2/3 exit=0" in result.stdout
    assert dmg.is_file()


def test_macos_dmg_create_never_retries_another_hdiutil_failure(tmp_path: Path) -> None:
    """FAR-2629: only Resource busy is retried; any other verdict fails at once.

    The stub would succeed on a second call, so a regression that retried the
    wrong diagnostic would pass the job instead of failing it here.
    """
    result, calls, dmg = _run_dmg_create(tmp_path, FAKE_HDIUTIL_FAILURES="1", FAKE_HDIUTIL_MESSAGE="No space left on device", FAKE_HDIUTIL_STATUS="42")

    assert result.returncode == 42
    assert calls == 1
    assert "failed for a reason other than Resource busy; not retried" in result.stderr
    assert "retrying in" not in result.stderr
    assert "REACHED_AFTER_DMG_CREATE" not in result.stdout
    assert not dmg.exists()


def test_macos_dmg_create_stops_at_the_bounded_attempt_cap(tmp_path: Path) -> None:
    """FAR-2629: a persistent Resource busy fails the step after three attempts."""
    result, calls, dmg = _run_dmg_create(tmp_path, FAKE_HDIUTIL_FAILURES="99")

    assert result.returncode == 1
    assert calls == 3
    assert "dmg_create: Resource busy persisted through 3 attempt(s)" in result.stderr
    assert "REACHED_AFTER_DMG_CREATE" not in result.stdout
    assert not dmg.is_file()


def test_macos_dmg_create_routes_the_single_creation_through_the_shared_retry_helper() -> None:
    """FAR-2629/FAR-2631: the one DMG creation is retried by the shared helper.

    FAR-2629's retry loop was inlined here; FAR-2631 moved it into
    hdiutil_with_resource_busy_retry so the attach and detach gates reuse it.
    The defaults and the per-attempt log lines are unchanged, so this pins them
    on the helper the call site now delegates to.
    """
    _step, build_run = _native_installer_step()
    block = _extract_marked_bash(build_run, "dmg_create_with_resource_busy_retry")
    helper = _extract_marked_bash(build_run, "hdiutil_with_resource_busy_retry")

    assert "hdiutil_with_resource_busy_retry dmg_create bash -c" in block
    assert build_run.count("# BEGIN dmg_create_with_resource_busy_retry") == 1
    # Exactly one DMG creation in the whole job, and it is this one.
    creation_lines = [line.strip() for line in _code_only(build_run).splitlines() if "hdiutil create" in line]
    assert len(creation_lines) == 1
    assert creation_lines[0].startswith("hdiutil create -volname")
    # Production bounds: 3 attempts, a 5s pause between them.
    assert "HDIUTIL_RESOURCE_BUSY_MAX_ATTEMPTS:-3" in helper
    assert "HDIUTIL_RESOURCE_BUSY_RETRY_DELAY:-5" in helper
    assert '-ge "$max_attempts"' in helper
    # Only the Resource busy verdict is retried.
    assert re.findall(r"grep -Fq '([^']*)'", helper) == ["Resource busy"]
    # The retry reads the log run_pre_smoke writes for the call site's label.
    assert '"$DATA/pre-smoke-${label}.log"' in helper
    assert "pre-smoke-${name}.log" in _workflow_bash_function(build_run, "run_pre_smoke")
    # Bash 3.2 + `set -u` on macOS (FAR-2588): no array expansions, no bash-4
    # constructs.
    assert not _unguarded_array_expansions(helper)
    assert not [name for name, pattern in _BASH4_ONLY.items() if pattern.search(_code_only(helper))]
    # The staging tree is still removed, and only after creation finishes.
    after_retry = build_run[build_run.index("# END dmg_create_with_resource_busy_retry") :]
    assert 'rm -rf "$DMG_STAGING"' in after_retry


# --- FAR-2631: the shared retry covers the detach that blocked a release cut ---


def _pre_install_detach_invocation(build_run: str) -> str:
    """The shipped pre-install detach call site, without its explicit exit guard."""
    guard = " || exit $?"
    for line in _code_only(build_run).splitlines():
        stripped = line.strip()
        if stripped.startswith("hdiutil_with_resource_busy_retry pre_install_detach "):
            assert stripped.endswith(guard), stripped
            return stripped[: -len(guard)]
    raise AssertionError("the workflow has no retried pre-install detach call site")


def _hdiutil_resource_busy_harness(build_run: str, invocation: str) -> str:
    """Run the shipped detach call site against the workflow's shared helper.

    ``hdiutil``, ``sleep`` and the redactor are shell functions, so the harness
    runs unchanged under bash 3.2 in a container where no host stub directory is
    mounted. ``$1`` is the verdict sequence, ``$2`` the Resource busy line.
    """
    return "\n".join(
        [
            "set -euo pipefail",
            'DATA="$(mktemp -d)"',
            'MOUNT="/Volumes/Farna Control"',
            'VERDICTS="$1"',
            'BUSY_LINE="$2"',
            "hdiutil_calls=0",
            "hdiutil() {",
            "  local verdict",
            '  verdict="${VERDICTS%%,*}"',
            '  if [[ "$VERDICTS" == *,* ]]; then VERDICTS="${VERDICTS#*,}"; fi',
            "  hdiutil_calls=$((hdiutil_calls + 1))",
            '  case "$verdict" in',
            "    ok) printf 'hdiutil: detach succeeded\\n'; return 0 ;;",
            "    busy) printf '%s\\n' \"$BUSY_LINE\" >&2; return 16 ;;",
            "    other) printf 'hdiutil: detach failed - No such file or directory\\n' >&2; return 1 ;;",
            "    *) return 99 ;;",
            "  esac",
            "}",
            "sleep() { printf 'SLEPT %s\\n' \"$1\"; }",
            "redact_pre_smoke_output() { cat; }",
            _workflow_bash_function(build_run, "run_pre_smoke"),
            _extract_marked_bash(build_run, "hdiutil_with_resource_busy_retry"),
            "retry_status=0",
            invocation + " || retry_status=$?",
            "printf 'RETRY_STATUS %d\\n' \"$retry_status\"",
            "printf 'HDIUTIL_CALLS %d\\n' \"$hdiutil_calls\"",
            "if [[ \"$retry_status\" -eq 0 ]]; then printf 'REACHED_AFTER_HDIUTIL_RETRY\\n'; fi",
            "",
        ]
    )


def _run_detach_retry(flavour: str, verdicts: str) -> subprocess.CompletedProcess[str]:
    _step, build_run = _native_installer_step()
    harness = _hdiutil_resource_busy_harness(build_run, _pre_install_detach_invocation(build_run))
    return _run_bash_stdin(flavour, harness, [verdicts, _HDIUTIL_DETACH_RESOURCE_BUSY])


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_pre_install_detach_retries_the_resource_busy_failure_then_accepts(flavour: str) -> None:
    """FAR-2631: the EBUSY detach from run 44923958746 is retried, then ships."""
    result = _run_detach_retry(flavour, "busy,ok")

    assert "RETRY_STATUS 0" in result.stdout, result.stdout + result.stderr
    assert "HDIUTIL_CALLS 2" in result.stdout
    assert "pre_install_detach: attempt=1/3 exit=16" in result.stdout
    assert _HDIUTIL_DETACH_RESOURCE_BUSY in result.stdout
    assert "pre_install_detach: retrying in 5s (Resource busy)" in result.stderr
    assert "SLEPT 5" in result.stdout
    assert "pre_smoke: pre_install_detach=ok (exit=0)" in result.stdout
    assert "pre_install_detach: attempt=2/3 exit=0" in result.stdout
    assert "REACHED_AFTER_HDIUTIL_RETRY" in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_pre_install_detach_never_retries_another_hdiutil_failure(flavour: str) -> None:
    """FAR-2631: only Resource busy is retried; any other verdict fails at once.

    The stub would succeed on a second call, so a regression that retried the
    wrong diagnostic would pass the job instead of failing it here.
    """
    result = _run_detach_retry(flavour, "other,ok")

    assert "RETRY_STATUS 1" in result.stdout, result.stdout + result.stderr
    assert "HDIUTIL_CALLS 1" in result.stdout
    assert "failed for a reason other than Resource busy; not retried" in result.stderr
    assert "retrying in" not in result.stderr
    assert "REACHED_AFTER_HDIUTIL_RETRY" not in result.stdout


@pytest.mark.parametrize("flavour", _BASH_FLAVOURS)
def test_macos_pre_install_detach_stops_at_the_bounded_attempt_cap(flavour: str) -> None:
    """FAR-2631: a persistent Resource busy still fails the gate after three tries."""
    result = _run_detach_retry(flavour, "busy")

    assert "RETRY_STATUS 16" in result.stdout, result.stdout + result.stderr
    assert "HDIUTIL_CALLS 3" in result.stdout
    assert "pre_install_detach: Resource busy persisted through 3 attempt(s)" in result.stderr
    assert result.stdout.count("SLEPT 5") == 2
    assert "REACHED_AFTER_HDIUTIL_RETRY" not in result.stdout


# --- FAR-2631: no EBUSY-capable hdiutil call site may bypass the helper -------

#: `hdiutil` subcommands that only inspect an image. None of them mounts, ejects
#: or creates a volume, so the mount-conflict "Resource busy" verdict the wrapper
#: retries cannot arise from them.
_HDIUTIL_READ_ONLY_SUBCOMMANDS = {"info", "verify"}

_HDIUTIL_COMMAND = re.compile(r"(?<![\w-])hdiutil\s+(?P<subcommand>[a-z]+)")
_HDIUTIL_DETACH_COMMAND = re.compile(r"(?<![\w-])hdiutil\s+detach\b")


def _unwrapped_hdiutil_commands(text: str) -> list[str]:
    """hdiutil commands that do not travel through the shared retry helper."""
    unwrapped: list[str] = []
    for line in _code_only(text).splitlines():
        stripped = line.strip()
        for match in _HDIUTIL_COMMAND.finditer(stripped):
            if match.group("subcommand") in _HDIUTIL_READ_ONLY_SUBCOMMANDS:
                continue
            if "hdiutil_with_resource_busy_retry" in stripped:
                continue
            unwrapped.append(stripped)
    return unwrapped


def test_macos_hdiutil_bare_call_scanner_flags_an_unwrapped_detach() -> None:
    """The coverage scan must fail on the bare detach it exists to prevent."""
    bare = 'run_pre_smoke pre_install_detach hdiutil detach "$MOUNT"'

    assert _unwrapped_hdiutil_commands(bare) == [bare]
    assert not _unwrapped_hdiutil_commands('hdiutil_with_resource_busy_retry pre_install_detach hdiutil detach "$MOUNT"')
    assert not _unwrapped_hdiutil_commands("run_pre_smoke pre_install_mount_absent bash -c '! hdiutil info | grep -Fq \"$1\"'")


def test_macos_every_covered_hdiutil_call_site_routes_through_the_retry_helper() -> None:
    """FAR-2631: one retry implementation covers every EBUSY-capable hdiutil call."""
    _step, build_run = _native_installer_step()

    # One implementation, defined once and used by all three call sites
    # (dmg_create, dmg_attach, pre_install_detach).
    assert build_run.count("# BEGIN hdiutil_with_resource_busy_retry") == 1
    assert build_run.count("# END hdiutil_with_resource_busy_retry") == 1
    call_sites = re.findall(r"hdiutil_with_resource_busy_retry(?=\s)", _code_only(build_run))
    assert len(call_sites) == 3
    assert 'hdiutil_with_resource_busy_retry dmg_attach hdiutil attach "$DMG" -mountpoint "$MOUNT" -nobrowse -readonly || exit $?' in build_run
    assert 'hdiutil_with_resource_busy_retry pre_install_detach hdiutil detach "$MOUNT" || exit $?' in build_run

    # The EXIT-cleanup detach loop stays outside the wrapper on purpose: cleanup
    # must never abort, so it keeps its own bounded, re-inspecting loop.
    trap_line = "trap 'cleanup \"$?\"' EXIT"
    cleanup_region = build_run[: build_run.index(trap_line)]
    assert "hdiutil_with_resource_busy_retry" not in cleanup_region
    cleanup_detach_lines = [line.strip() for line in _code_only(cleanup_region).splitlines() if _HDIUTIL_DETACH_COMMAND.search(line)]
    assert cleanup_detach_lines == ['if hdiutil detach "$MOUNT" >/dev/null 2>&1; then']

    # The one DMG creation still happens inside the wrapped dmg_create gate.
    create_block = _extract_marked_bash(build_run, "dmg_create_with_resource_busy_retry")
    create_lines = [line.strip() for line in _code_only(create_block).splitlines() if _HDIUTIL_COMMAND.search(line)]
    assert create_lines == ['hdiutil create -volname "Farna Control $1" -srcfolder "$2" -ov -format UDZO "$3"']
    assert "hdiutil_with_resource_busy_retry dmg_create bash -c '" in create_block

    # Nothing else in the smoke body calls a mutating hdiutil subcommand bare.
    smoke_body = build_run[build_run.index(trap_line) :]
    offenders = _unwrapped_hdiutil_commands(smoke_body.replace(create_block, ""))
    assert not offenders, offenders
