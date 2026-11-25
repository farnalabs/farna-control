"""FAR-2786: the entrypoint's real migration sequence must migrate an EMPTY database.

``backend/entrypoint.sh`` upgraded to ``0001_v2_identity_org`` — a revision the
2026-11 squash deleted. On a fresh database alembic exited with ``Can't locate
revision identified by '0001_v2_identity_org'``, ``set -e`` killed the entrypoint
before uvicorn started, and every new Docker/Helm install CrashLoopBackOff'd.

The fix (FAR-2777) leaves a single ``alembic upgrade heads``, and its unit-level
regression executes the script with STUBBED python/alembic binaries. That proves
which command the script runs, not that alembic can resolve it and migrate: a
target the graph no longer contains, a revision module that fails to import, or a
version column too narrow for the ids only fail against a real database. So these
tests execute the script's ACTUAL migration commands, in order, under the
script's own ``set -e``, against a pristine Postgres 16 and assert the schema
reaches head.

The second test is the no-regression case: a database already at head (what
every existing install has) must run the same sequence clean, as a real no-op.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NamedTuple

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

BACKEND_ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = BACKEND_ROOT / "entrypoint.sh"

#: The executable lines between these two markers are the migration sequence —
#: everything after the pre-upgrade snapshot and before uvicorn is exec'd.
_MIGRATIONS_START = 'echo "Running database migrations..."'
_MIGRATIONS_END = "exec uvicorn"

#: The `alembic` and `python` the entrypoint invokes must be this test's
#: interpreter, not whatever the host PATH happens to find.
_VENV_BIN = Path(sys.executable).parent


def _posix_shell() -> str | None:
    """Return a POSIX shell able to execute the entrypoint block on this host."""
    if sys.platform == "win32":
        # ``shutil.which("bash")`` finds WSL's bash on Windows, which cannot
        # launch this checkout's Windows ``alembic.exe``; Git Bash can.
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\sh.exe",
        ):
            if Path(candidate).is_file():
                return candidate
        return None
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found:
            return found
    return None


SHELL = _posix_shell()

pytestmark = [pytest.mark.integration, pytest.mark.posix_only]


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    # script_location is CWD-relative in alembic.ini; pin the absolute path (as
    # the integration conftest does) so the test also runs from the repo root.
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "farna" / "db" / "migrations"))
    return ScriptDirectory.from_config(config)


def _head_revision() -> str:
    head = _script_directory().get_current_head()
    assert head is not None, "the revision graph has no head"
    return head


def _migration_block() -> list[str]:
    """The executable lines the entrypoint runs after the snapshot, before uvicorn."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    start = text.index(_MIGRATIONS_START)
    end = text.index(_MIGRATIONS_END, start)
    lines = [line.strip() for line in text[start:end].splitlines() if line.strip() and not line.lstrip().startswith("#")]

    upgrades = [line for line in lines if line.startswith("alembic upgrade")]
    # Guard against a vacuous pass: whatever the sequence becomes, it must still
    # run alembic upgrades and finish by applying `heads`.
    assert upgrades, f"{ENTRYPOINT.name} must still run alembic upgrades between {_MIGRATIONS_START!r} and {_MIGRATIONS_END!r}"
    assert upgrades[-1] == "alembic upgrade heads", f"the migration sequence must end by applying heads, found {upgrades!r}"
    return lines


def _migration_env(database_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(_VENV_BIN), env.get("PATH", "")])
    # DATABASE_ADMIN_URL is the owner-role URL migrations/env.py prefers; both
    # spellings point at the throwaway database so the entrypoint can never
    # drift onto a developer's or the shared testcontainer's database.
    env["DATABASE_URL"] = database_url
    env["DATABASE_ADMIN_URL"] = database_url
    return env


def _run(script: str, database_url: str) -> subprocess.CompletedProcess[str]:
    assert SHELL is not None
    # argv is this test's own shell plus the block extracted from entrypoint.sh;
    # nothing here comes from an untrusted caller.
    return subprocess.run(  # noqa: S603
        [SHELL, "-c", script],
        cwd=str(BACKEND_ROOT),
        env=_migration_env(database_url),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800,
        check=False,
    )


def _run_entrypoint_migrations(database_url: str) -> subprocess.CompletedProcess[str]:
    # `set -e` mirrors the entrypoint: a failing phase aborts the sequence, which
    # is exactly what killed the container boot.
    return _run("\n".join(["set -e", *_migration_block()]), database_url)


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit={result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


class _Database(NamedTuple):
    """One throwaway database on the shared Postgres 16 testcontainer."""

    name: str
    #: Plain `postgresql://` URL for the assertions (psycopg, the driver the
    #: migrations themselves use).
    url: str
    #: The asyncpg URL the application and alembic are handed.
    migration_url: str


def _create_database(postgres: PostgresContainer, suffix: str) -> _Database:
    admin_url = postgres.get_connection_url(driver=None)
    name = f"far2786_{suffix}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    migration_url = make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
    return _Database(name=name, url=url, migration_url=migration_url)


def _tables(database: _Database) -> set[str]:
    with psycopg.connect(database.url) as connection:
        rows = connection.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'").fetchall()
    return {row[0] for row in rows}


def _revisions(database: _Database) -> set[str]:
    """The revisions stamped in ``alembic_version`` (empty when never migrated)."""
    with psycopg.connect(database.url) as connection:
        registered = connection.execute("SELECT to_regclass('public.alembic_version')").fetchone()
        if registered is None or registered[0] is None:
            return set()
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return {row[0] for row in rows}


def _version_num_width(database: _Database) -> int | None:
    with psycopg.connect(database.url) as connection:
        row = connection.execute(
            "SELECT character_maximum_length FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'alembic_version' AND column_name = 'version_num'",
        ).fetchone()
    return None if row is None else row[0]


@pytest.fixture(scope="module")
def fresh_database(postgres_container: PostgresContainer) -> _Database:
    """An EMPTY database: the fresh-install shape that CrashLoopBackOff'd."""
    return _create_database(postgres_container, "fresh")


@pytest.fixture(scope="module")
def database_at_head(postgres_container: PostgresContainer) -> _Database:
    """A database an EXISTING install has: already migrated by `upgrade heads`."""
    database = _create_database(postgres_container, "head")
    result = _run("set -e\nalembic upgrade heads", database.migration_url)
    assert result.returncode == 0, f"preparing the already-migrated database failed:\n{_output(result)}"
    assert _revisions(database) == {_head_revision()}
    return database


def test_entrypoint_migrations_create_a_fresh_database(fresh_database: _Database) -> None:
    """FAR-2786: a fresh database must reach head, not die on a deleted revision id."""
    assert _tables(fresh_database) == set(), "the fresh database must start empty"

    result = _run_entrypoint_migrations(fresh_database.migration_url)
    output = _output(result)

    assert "Can't locate revision" not in output, output
    assert result.returncode == 0, output
    assert _revisions(fresh_database) == {_head_revision()}

    # The removed `psql ... TYPE VARCHAR(64)` step claimed to guarantee the
    # version column can hold every revision id. Keep the guarantee, not the
    # step: migrations/env.py pre-creates the column wide enough.
    longest = max(len(revision.revision) for revision in _script_directory().walk_revisions())
    width = _version_num_width(fresh_database)
    assert width is None or width >= longest, f"alembic_version.version_num is VARCHAR({width}) but the longest revision id is {longest} chars"


def test_entrypoint_migrations_are_a_noop_on_a_database_at_head(database_at_head: _Database) -> None:
    """No regression: an already-migrated database must stay put and exit clean."""
    assert _revisions(database_at_head) == {_head_revision()}

    result = _run_entrypoint_migrations(database_at_head.migration_url)
    output = _output(result)

    assert "Can't locate revision" not in output, output
    assert result.returncode == 0, output
    assert _revisions(database_at_head) == {_head_revision()}
    assert "Running upgrade" not in output, f"nothing should have been applied to a database already at head:\n{output}"
