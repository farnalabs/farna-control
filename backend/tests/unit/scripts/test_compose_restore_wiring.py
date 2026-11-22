"""FAR-2667 review: structural wiring tests for the compose recovery helper.

The restore path shipped non-functional twice because nothing exercised the
helper or the entrypoint wiring. These are fast, Docker-free static checks that
pin the three things that make the recovery work:

* the admin URL is expanded IN-CONTAINER (the operator shell runs under
  ``set -u`` and the name only exists in the compose service environment);
* the installed ``farna-control`` executable runs behind an explicit
  ``--entrypoint`` (the all-in-one image ENTRYPOINT ignores its arguments);
* both entrypoints print the helper path the shipped bundle actually contains
  (``./compose-restore.sh``, not ``scripts/compose-restore.sh``).
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "compose-restore.sh").exists():
            return parent
    raise RuntimeError("Could not find repo root (scripts/compose-restore.sh)")


_REPO_ROOT = _repo_root()
_HELPER = _REPO_ROOT / "scripts" / "compose-restore.sh"


def test_helper_runs_under_strict_shell_mode() -> None:
    assert "set -Eeuo pipefail" in _HELPER.read_text(encoding="utf-8")


def test_restore_runs_the_installed_executable_behind_an_explicit_entrypoint() -> None:
    text = _HELPER.read_text(encoding="utf-8")
    # The all-in-one image ENTRYPOINT (/entrypoint.sh) ignores its arguments and
    # execs supervisord; the installed executable is `farna-control`, not
    # `restore`, so every restore must name it behind an explicit entrypoint.
    assert "--entrypoint sh" in text
    assert "exec farna-control restore" in text
    # A bare `restore ...` container invocation (relying on the image
    # ENTRYPOINT) must not survive: that is the shape that silently started
    # supervisord instead of restoring.
    assert "\n  restore " not in text


def test_admin_url_is_expanded_in_container_not_the_operator_shell() -> None:
    text = _HELPER.read_text(encoding="utf-8")
    admin_lines = [line for line in text.splitlines() if "$DATABASE_ADMIN_URL" in line and not line.lstrip().startswith("#")]
    assert admin_lines, "compose-restore.sh must pass the admin URL to the restore"
    for line in admin_lines:
        # Each admin URL must be inside a single-quoted `sh -c` script so the
        # operator shell (under `set -u`) never expands the compose-only name.
        assert "-c '" in line, f"$DATABASE_ADMIN_URL must be expanded in-container, not in the operator shell: {line!r}"


@pytest.mark.parametrize("relative_path", ["backend/entrypoint.sh", "deploy/supervisor/entrypoint.sh"])
def test_entrypoints_print_the_shipped_helper_path(relative_path: str) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "./compose-restore.sh --confirm" in text
    assert "scripts/compose-restore.sh" not in text, f"{relative_path} must not print a path the shipped bundle does not contain"


def test_supervisor_snapshot_is_gated_on_the_snapshot_dir() -> None:
    """A non-compose all-in-one boot without a snapshot dir must not require
    DATABASE_ADMIN_URL on every (pristine / already-at-head) boot."""
    text = (_REPO_ROOT / "deploy" / "supervisor" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${FARNA_CONTROL_SNAPSHOT_DIR:-}" ]' in text
    assert "${FARNA_CONTROL_SNAPSHOT_DIR:-/backups}" not in text
