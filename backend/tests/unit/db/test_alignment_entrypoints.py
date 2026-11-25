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
    "backend/entrypoint.sh": ("python -m farna.db.alignment", "alembic upgrade heads"),
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


# ---------------------------------------------------------------------------
# FAR-2667 C6: guard THEN snapshot THEN migrate
# ---------------------------------------------------------------------------

_SNAPSHOT_ENTRYPOINTS = {
    # The snapshot step must sit between the release-alignment refusal and the
    # migration: a database from a NEWER release is refused before anything is
    # dumped, and a snapshot that cannot be taken aborts before anything is
    # migrated.
    "backend/entrypoint.sh": ("python -m farna.db.alignment", "python -m farna.launcher.upgrade", "alembic upgrade heads"),
    "deploy/supervisor/entrypoint.sh": ("python -m farna.db.alignment", "python -m farna.launcher.upgrade", "alembic upgrade heads"),
    # deploy/fly/entrypoint.sh is deliberately ABSENT: its [release] command
    # (deploy/fly/release.sh) is the single migrator and runs before any
    # machine boots, so a boot-time pending probe is always false and a
    # snapshot there could never fire. Fly is the documented exception; see
    # test_compose_restore_wiring.py::TestTheFlyBootCannotTakeTheSnapshot.
}


@pytest.mark.parametrize(("relative_path", "commands"), sorted(_SNAPSHOT_ENTRYPOINTS.items()))
def test_entrypoint_orders_guard_snapshot_migrate(relative_path: str, commands: tuple[str, str, str]) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    guard_command, snapshot_command, migrate_command = commands
    lines = _executable_lines(text)

    def _first(command: str) -> int:
        return next(index for index, line in enumerate(lines) if command in line)

    assert _first(guard_command) < _first(snapshot_command) < _first(migrate_command), f"{relative_path} must run the alignment guard, then the pre-upgrade snapshot, then the migrations"


@pytest.mark.parametrize("relative_path", sorted(_SNAPSHOT_ENTRYPOINTS))
def test_entrypoint_never_falls_back_to_the_application_role_for_the_dump(relative_path: str) -> None:
    """M11: a dump through DATABASE_URL (NOBYPASSRLS) is row-partial."""
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")

    assert "DATABASE_ADMIN_URL:-$DATABASE_URL" not in text, f"{relative_path} must not fall back to DATABASE_URL for the snapshot"
    assert "\u0024DATABASE_ADMIN_URL" in text
    assert "cannot dump as the owner role" in text
