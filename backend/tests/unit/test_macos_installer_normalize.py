"""FAR-2327: the macOS installer normalize step must honour reusable-workflow call inputs
even when the caller (release-unified) was itself started by workflow_dispatch."""

import hashlib
import http.server
import io
import os
import re
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


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = r"C:\Program Files\Git\bin\bash.exe" if Path(r"C:\Program Files\Git\bin\bash.exe").is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute workflow scripts")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
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
    assert "iconutil --convert icns" in bundle_script

    staging_start = next(index for index, line in enumerate(lines) if 'mkdir -p "$DMG_STAGING"' in line)
    dmg_create = next(index for index, line in enumerate(lines) if "run_pre_smoke dmg_create" in line)
    hard_staging_checks = {
        "app": 'test -d "$DMG_STAGING/Modulo-${VERSION_NUM}.app"',
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
    attach_index = next(index for index, line in enumerate(lines) if "run_pre_smoke dmg_attach" in line)
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
        "run_pre_smoke dmg_create bash -c",
        "run_pre_smoke dmg_codesign codesign",
        "run_pre_smoke dmg_notary_submit xcrun notarytool submit",
        "run_pre_smoke dmg_codesign_verify codesign",
        "run_pre_smoke dmg_verify hdiutil verify",
        "run_pre_smoke dmg_attach hdiutil attach",
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
fake_codesign_calls=0
codesign() {
  fake_codesign_calls=$((fake_codesign_calls + 1))
  if [[ "$fake_codesign_calls" -le "${FAKE_CODESIGN_FAILURES:-0}" ]]; then
    printf 'build/Modulo.app/Contents/MacOS/modulo: %s\\n' "${FAKE_CODESIGN_MESSAGE:-}" >&2
    return "${FAKE_CODESIGN_STATUS:-1}"
  fi
  printf 'build/Modulo.app/Contents/MacOS/modulo: replacing existing signature\\n' >&2
  return 0
}

status=0
codesign_with_timestamp_retry fixture --force --timestamp --sign "Developer ID Application: Fixture (AAAAAAAAAA)" build/Modulo.app || status=$?
printf 'fake_codesign_calls=%d\\n' "$fake_codesign_calls"
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
    assert "run_pre_smoke dmg_notary_submit xcrun notarytool submit" in build_run
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
