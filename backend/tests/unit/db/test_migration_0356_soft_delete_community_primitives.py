"""The 0356 migration soft-deletes ONLY live community-sourced library rows (FAR-2592).

The migration is a data-only UPDATE, so these tests pin the exact predicate it
executes: only ``source = 'community'`` rows that are still live are stamped,
which is what makes the upgrade idempotent and keeps every other row
(``local`` / ``registry`` / ``modulo``, and already-deleted rows) untouched.

The statement must also RUN on SQLite, not merely read as portable: ``env.py``
selects the sqlite dialect with ``render_as_batch``, and a ``public.`` schema
prefix or a Postgres-only ``now()`` would fail the upgrade there. One test
therefore executes the migration's own captured statement against an in-memory
SQLite database. The real-Postgres execution of the whole chain is rehearsed by
``tests/integration/db/test_migration_rehearsal.py`` in CI (this workstation has
no Docker), so the unit tier pins the statement, its portable execution, and the
graph shape.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0356_soft_delete_community_library_primitives.py"

#: Seeded rows: one live community row (must be stamped), one live ``local`` row
#: (must be left alone) and one ALREADY soft-deleted community row (must keep its
#: original timestamp, which is what proves the predicate is idempotent).
_SQLITE_DDL = """
CREATE TABLE library_primitives (
    id integer primary key,
    source text,
    deleted_at timestamp
)
"""

_LIVE_COMMUNITY_ID = 1
_LIVE_LOCAL_ID = 2
_PRE_DELETED_COMMUNITY_ID = 3
_PRE_EXISTING_DELETED_AT = "2026-01-01 00:00:00"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0356_soft_delete_community", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_upgrade_statement(monkeypatch: pytest.MonkeyPatch) -> str:
    """Run ``upgrade()`` with ``op.execute`` captured and return its one statement."""
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)

    module.upgrade()

    assert len(executed) == 1
    return executed[0]


def _deleted_at_by_id(connection: sqlite3.Connection) -> dict[int, str | None]:
    rows = connection.execute("SELECT id, deleted_at FROM library_primitives").fetchall()
    return {row[0]: row[1] for row in rows}


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0356_soft_delete_community_library_primitives"
    assert module.down_revision == "0355_drop_library_sync_state"


def test_upgrade_statement_is_soft_delete_only_and_live_row_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_upgrade_statement(monkeypatch)

    # Portable spelling: no Postgres-only schema prefix and no Postgres-only now().
    assert "UPDATE library_primitives" in statement
    assert "public." not in statement
    assert "CURRENT_TIMESTAMP" in statement.upper()
    assert "now()" not in statement.lower()
    assert "source = 'community'" in statement
    # The live-row guard is what keeps the upgrade idempotent.
    assert "deleted_at IS NULL" in statement
    # Soft delete only: the row IS preserved and its provenance is NOT rewritten.
    assert "DELETE FROM" not in statement.upper()
    assert "TRUNCATE" not in statement.upper()
    assert "source = 'local'" not in statement


def test_upgrade_statement_soft_deletes_only_live_community_rows_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The captured statement must EXECUTE on SQLite and touch only the right row.

    A text assertion cannot prove the migration runs on a SQLite deployment, so
    the migration's own statement is executed against an in-memory SQLite
    database and the seeded rows are read back.
    """
    statement = _capture_upgrade_statement(monkeypatch)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        connection.executemany(
            "INSERT INTO library_primitives (id, source, deleted_at) VALUES (?, ?, ?)",
            [
                (_LIVE_COMMUNITY_ID, "community", None),
                (_LIVE_LOCAL_ID, "local", None),
                (_PRE_DELETED_COMMUNITY_ID, "community", _PRE_EXISTING_DELETED_AT),
            ],
        )

        connection.execute(statement)
        connection.commit()
        first_pass = _deleted_at_by_id(connection)

        # Re-running the upgrade must be a no-op: the guard skips stamped rows.
        connection.execute(statement)
        connection.commit()
        second_pass = _deleted_at_by_id(connection)
    finally:
        connection.close()

    assert first_pass == second_pass
    assert second_pass[_LIVE_COMMUNITY_ID] is not None
    assert second_pass[_LIVE_LOCAL_ID] is None
    assert second_pass[_PRE_DELETED_COMMUNITY_ID] == _PRE_EXISTING_DELETED_AT


def test_downgrade_does_not_mutate_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exact reversal is unsafe, so the downgrade is a deliberate no-op.

    ``deleted_at`` carries no provenance identifying which path stamped it, so
    clearing it for every community row would resurrect rows removed for other
    reasons. The module docstring records this decision; the test pins that the
    downgrade executes no statement at all.
    """
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)

    module.downgrade()

    assert not executed
