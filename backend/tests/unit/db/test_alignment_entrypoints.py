"""FAR-2659: every startup path runs the shared alignment guard before migrating.

The guard itself is ``farna.db.alignment`` (unit-tested in ``test_alignment.py``).
These tests pin the WIRING: each entrypoint calls the shared module exactly once,
and does so before it runs any migration, so a database from a newer release is
refused legibly instead of reaching alembic's "Can't locate revision" retry loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GUARD_CALL = "farna.db.alignment"

_ENTRYPOINTS = {
    "backend/entrypoint.sh": ("python -m farna.db.alignment", "alembic upgrade 0001_v2_identity_org"),
    "deploy/supervisor/entrypoint.sh": ("python -m farna.db.alignment", "alembic upgrade heads"),
    "deploy/fly/entrypoint.sh": ("python3 -m farna.db.alignment", "alembic upgrade heads"),
    # The Fly [release] command is the AUTHORITATIVE migrator (it runs before
    # any machine boots), so it needs the guard too — the entrypoint above is
    # only reached on the later machine boot.
    "deploy/fly/release.sh": ("python3 -m farna.db.alignment", "alembic upgrade heads"),
}


def _executable_lines(text: str) -> list[str]:
    """Non-comment, non-blank lines (the explanatory comments name alembic too)."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


@pytest.mark.parametrize(("relative_path", "commands"), sorted(_ENTRYPOINTS.items()))
def test_entrypoint_guards_before_running_migrations(relative_path: str, commands: tuple[str, str]) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    guard_command, migrate_command = commands
    lines = _executable_lines(text)

    guard_indexes = [index for index, line in enumerate(lines) if guard_command in line]
    migrate_indexes = [index for index, line in enumerate(lines) if migrate_command in line]

    assert guard_indexes, f"{relative_path} must run the shared alignment guard ({guard_command})"
    assert migrate_indexes, f"{relative_path} must still run its migration step ({migrate_command})"
    assert guard_indexes[0] < migrate_indexes[0], f"{relative_path} must run the alignment guard BEFORE migrating"


@pytest.mark.parametrize("relative_path", sorted(_ENTRYPOINTS))
def test_entrypoint_calls_the_shared_guard_exactly_once(relative_path: str) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert text.count(_GUARD_CALL) == 1, f"{relative_path} must contain exactly one shared-guard call, not duplicated logic"
