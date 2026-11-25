"""FAR-2659: every startup path runs the shared alignment guard before migrating.

The guard itself is ``farna.db.alignment`` (unit-tested in ``test_alignment.py``).
These tests pin the WIRING: each entrypoint calls the shared module exactly once,
and does so before it runs any migration, so a database from a newer release is
refused legibly instead of reaching alembic's "Can't locate revision" retry loop.
"""

from __future__ import annotations

import os
import subprocess
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


# ---------------------------------------------------------------------------
# FAR-2777: a fresh boot applies the ONE migration command the chain needs
# ---------------------------------------------------------------------------

_ENTRYPOINT = _REPO_ROOT / "backend" / "entrypoint.sh"

#: Each stub records "<name> <args>" then succeeds, so the test can see exactly
#: which migration / psql / uvicorn commands entrypoint.sh actually ran.
_STUB_TEMPLATE = '#!/bin/sh\nprintf \'%s %s\\n\' "{name}" "$*" >> "${{STUB_LOG:?}}"\nexit 0\n'


@pytest.mark.posix_only
def test_fresh_boot_runs_exactly_one_alembic_upgrade_heads(tmp_path: Path) -> None:
    """EXECUTE backend/entrypoint.sh with stubbed binaries (FAR-2777).

    A fresh Compose install could not boot because the entrypoint still ran
    ``alembic upgrade 0001_v2_identity_org`` - a revision that no longer exists -
    plus a psql ``alembic_version`` widening against the retired ``modulo``
    role/db. Asserting the shipped text is not enough: the broken phase-2
    ``alembic upgrade heads`` line passes a substring check, so the old pins kept
    passing after ``entrypoint.sh`` was reverted to ``origin/main``. Running the
    real script proves the fresh-boot path reaches uvicorn with exactly one
    migration command and no psql step.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    log_path = tmp_path / "calls.log"
    for name in ("python", "alembic", "uvicorn", "psql"):
        stub = stub_bin / name
        stub.write_text(_STUB_TEMPLATE.format(name=name), encoding="utf-8", newline="\n")
        stub.chmod(0o755)

    env = {
        "PATH": os.pathsep.join((str(stub_bin), "/usr/bin", "/bin")),
        "STUB_LOG": str(log_path),
        "HOME": str(tmp_path / "home"),
        # FARNA_CONTROL_SNAPSHOT_DIR is left unset on purpose: the pre-upgrade
        # snapshot adds a ``python -m farna.launcher.upgrade`` call and is not
        # part of the fresh-boot migration contract under test.
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, the shipped entrypoint under test
        ["/bin/sh", str(_ENTRYPOINT)],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"backend/entrypoint.sh must boot a fresh database; rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    calls = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    alembic_upgrades = [call for call in calls if call.startswith("alembic upgrade")]

    assert alembic_upgrades == ["alembic upgrade heads"], (
        f"backend/entrypoint.sh must run exactly ONE migration command, `alembic upgrade heads`: the pre-FAR-2777 phase-1 `alembic upgrade 0001_v2_identity_org` names a revision that no longer exists and aborts a fresh boot. Observed: {alembic_upgrades!r}"
    )
    assert not [call for call in calls if call.startswith("psql ")], f"backend/entrypoint.sh must not run the retired psql alembic_version widening: migrations/env.py widens version_num itself and the removed step targeted the retired modulo role/db. Observed: {calls!r}"
    assert any(call.startswith("uvicorn ") for call in calls), f"backend/entrypoint.sh must reach `exec uvicorn` after migrating; observed {calls!r}"
