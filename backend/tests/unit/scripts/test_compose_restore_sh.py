"""Execute ``scripts/compose-restore.sh`` for real (FAR-2667).

``compose-restore.sh`` is the ONE documented recovery command for the compose
path, and the branch's own ``AGENTS.md`` lesson says a documented recovery
command is verified by EXECUTING the shipped file, not by asserting its text.
``test_compose_restore_wiring.py`` pins the wiring statically; this module runs
the helper under ``bash`` (its shebang is bash: ``set -Eeuo pipefail``) with a stubbed
``docker`` first on PATH, the
way ``test_compose_rollback_sh.py`` runs the rollback helper.

The stub records every invocation and distinguishes the three one-off
containers the helper starts:

* the presence check runs ``--entrypoint sh`` (the snapshot volume is not a host
  directory);
* the two verification steps pass ``--dry-run`` and must touch nothing;
* the real replay is the only ``run`` WITHOUT ``--dry-run`` and must carry
  ``--yes`` and ``--safety-dump-dir /backups``.

Each case would fail on the shipped-broken shapes this helper family has
shipped twice: a ``compose run`` that relies on the image ENTRYPOINT, a real
replay that starts the app, or a failure that leaves the operator without the
start command.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.posix_only


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "compose-restore.sh").is_file():
            return parent
    raise RuntimeError("Could not find repo root (scripts/compose-restore.sh)")


REPO_ROOT = _repo_root()
REPO_HELPER = REPO_ROOT / "scripts" / "compose-restore.sh"

_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from package_compose_release import build_compose_release  # noqa: E402

#: The service the shipped compose names the application.
APP_SERVICE = "farna-control"

SNAPSHOT = "/backups/pre-upgrade-dump-0.4.0-20261122-101500"

DOCKER_STUB = r"""\
#!/usr/bin/env bash
# Test stub for compose-restore.sh: never touches a container daemon.
set -u
printf '%s\n' "$*" >> "${STUB_LOG}"

mode=""
for a in "$@"; do
  case "$a" in
    run|stop) [ -z "${mode}" ] && mode="$a" ;;
    *) ;;
  esac
done

case "${mode}" in
  run)
    is_sh=0
    is_dry=0
    for a in "$@"; do
      [ "$a" = "sh" ] && is_sh=1
      [ "$a" = "--dry-run" ] && is_dry=1
    done
    if [ "${is_sh}" = 1 ] || [ "${is_dry}" = 1 ]; then
      exit 0
    fi
    # The real replay: STUB_RESTORE_RC lets a case prove the failure path.
    exit "${STUB_RESTORE_RC:-0}"
    ;;
  stop)
    exit 0
    ;;
  *)
    printf 'unexpected docker invocation: %s\n' "$*" >&2
    exit 3
    ;;
esac
"""


def _make_env(tmp_path: Path, *, restore_rc: int = 0) -> dict[str, str]:
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    stub.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    return {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_dir),
        "STUB_LOG": str(tmp_path / "docker.log"),
        "STUB_RESTORE_RC": str(restore_rc),
    }


def _extract_bundle(tmp_path: Path) -> Path:
    """Build and unpack a real release bundle (the shipped layout)."""
    tarball = build_compose_release(version="0.4.0", output_dir=tmp_path / "dist")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with tarfile.open(tarball, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(bundle, filter="data")
        else:
            tar.extractall(bundle)  # noqa: S202
    return bundle


def _run_helper(helper: Path, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, repo script under test
        ["bash", str(helper), *args],  # noqa: S607
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _invocations(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _index_of(invocations: list[str], needle: str) -> int:
    for index, line in enumerate(invocations):
        if needle in line:
            return index
    raise AssertionError(f"no docker invocation containing {needle!r} in {invocations!r}")


def test_packaged_helper_verifies_then_stops_then_replays_without_restarting(tmp_path: Path) -> None:
    """The documented recovery, executed end to end against a real bundle."""
    bundle = _extract_bundle(tmp_path)
    env = _make_env(tmp_path)

    proc = _run_helper(bundle / "compose-restore.sh", bundle, env, "--confirm", SNAPSHOT)

    assert proc.returncode == 0, proc.stderr
    assert "Restore complete. Writers stay stopped." in proc.stdout
    assert "Start the previous release with:" in proc.stdout
    assert "compose-rollback.sh" in proc.stdout

    invocations = _invocations(tmp_path)
    # Every one-off container overrides the image ENTRYPOINT.
    runs = [line for line in invocations if "run" in line.split()]
    assert runs, invocations
    assert all("--entrypoint" in line for line in runs), runs
    assert all("--no-deps" in line for line in runs), runs

    presence = _index_of(invocations, "--entrypoint sh")
    dry_run = _index_of(invocations, "--dry-run")
    verify = _index_of(invocations, "--verify-target-schema")
    stop = _index_of(invocations, "stop " + APP_SERVICE)
    replay = _index_of(invocations, "--yes")
    # Verify BEFORE destroy, stop BEFORE replay.
    assert dry_run < stop
    assert verify < stop
    assert stop < replay
    assert presence < replay

    replay_line = invocations[replay]
    assert "--dry-run" not in replay_line
    assert "--safety-dump-dir /backups" in replay_line
    assert "--entrypoint farna-control" in replay_line
    # It never starts the application.
    assert not any(" up " in line for line in invocations), invocations


def test_without_confirm_it_refuses_before_touching_anything(tmp_path: Path) -> None:
    bundle = _extract_bundle(tmp_path)
    env = _make_env(tmp_path)

    proc = _run_helper(bundle / "compose-restore.sh", bundle, env, SNAPSHOT)

    assert proc.returncode == 2
    assert "refusing to restore without --confirm" in proc.stderr
    assert _invocations(tmp_path) == [], "a refused restore must not run any container"


def test_missing_snapshot_argument_is_a_usage_error(tmp_path: Path) -> None:
    bundle = _extract_bundle(tmp_path)
    env = _make_env(tmp_path)

    proc = _run_helper(bundle / "compose-restore.sh", bundle, env)

    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert _invocations(tmp_path) == []


def test_a_failed_replay_prints_the_start_command_and_never_restarts(tmp_path: Path) -> None:
    """An error after the stop must hand the operator the way back."""
    bundle = _extract_bundle(tmp_path)
    env = _make_env(tmp_path, restore_rc=7)

    proc = _run_helper(bundle / "compose-restore.sh", bundle, env, "--confirm", SNAPSHOT)

    assert proc.returncode == 7, proc.stderr
    assert "RESTORE FAILED" in proc.stderr
    assert "compose-rollback.sh" in proc.stderr
    assert "No automatic restart was performed." in proc.stderr
    invocations = _invocations(tmp_path)
    assert not any(" up " in line for line in invocations), "the helper must never start the application itself"


def test_the_repo_scripts_are_committed_executable() -> None:
    """A direct ``./compose-restore.sh`` must work in a git checkout."""
    for helper in (REPO_ROOT / "scripts" / "compose-restore.sh", REPO_ROOT / "scripts" / "compose-rollback.sh"):
        mode = helper.stat().st_mode
        assert mode & 0o111, f"{helper.name} must be executable in the checkout"
