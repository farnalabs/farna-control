"""The 0360 migration frees schema version history and drops the retired pipeline-delete flag.

FAR-2663 lowered ``schema_version_history`` from ``team`` to ``community`` in
``core.feature_flags._KNOWN_FLAGS`` and removed ``pipeline_delete`` from the
catalog entirely. Neither code change reaches an existing install: the boot seed
inserts catalog rows with ``ON CONFLICT (name) DO NOTHING`` and the DB rows win,
so an upgraded install keeps the stale ``tier_id='team'`` row and re-advertises
a flag that gates nothing. Migration 0360 rewrites the one row once and deletes
the other.

These tests pin the exact statements the migration executes, and EXECUTE them
against an in-memory SQLite database (the ``env.py`` target) to simulate an
existing install, because a text-only assertion cannot prove the upgrade runs.
The real-Postgres execution of the whole chain is rehearsed by
``tests/integration/db/test_migration_rehearsal.py`` in CI.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0360_free_schema_history_retire_pipeline_delete.py"

# The columns the migration reads/writes, mirroring db/models/tier_catalog.py.
_SQLITE_DDL = """
CREATE TABLE feature_flag_catalog (
    name text primary key,
    description text,
    tier_id text not null,
    depends_on text,
    is_active boolean default true
)
"""

_SCHEMA_HISTORY = "schema_version_history"
_PIPELINE_DELETE = "pipeline_delete"
_OTHER_TEAM = "sso"
_OTHER_COMMUNITY = "observability"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0360_free_schema_history_retire_pipeline_delete", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_statements(monkeypatch: pytest.MonkeyPatch, run: Callable[[ModuleType], None]) -> list[str]:
    """Run *run* with ``op.execute`` captured and return its statements in order."""
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)

    run(module)

    assert len(executed) == 2
    return executed


def _open_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(_SQLITE_DDL)
    return connection


def _seed(connection: sqlite3.Connection, rows: dict[str, dict[str, object]]) -> None:
    connection.executemany(
        "INSERT INTO feature_flag_catalog (name, description, tier_id, is_active) VALUES (?, ?, ?, ?)",
        [(name, values["description"], values["tier_id"], values["is_active"]) for name, values in rows.items()],
    )
    connection.commit()


def _rows_by_name(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows = connection.execute("SELECT name, description, tier_id, is_active FROM feature_flag_catalog").fetchall()
    return {row[0]: {"description": row[1], "tier_id": row[2], "is_active": row[3]} for row in rows}


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0360_free_schema_history_retire_pipeline_delete"
    assert module.down_revision == "0359_registry_sourced_drop_index"


def test_upgrade_statements_are_scoped_and_dialect_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    statements = _capture_statements(monkeypatch, lambda module: module.upgrade())

    update_statement, delete_statement = statements
    # Portable spelling: no Postgres-only schema prefix and no Postgres-only now().
    for statement in statements:
        assert "public." not in statement
        assert "now()" not in statement.lower()
    # The tier rewrite touches only schema_version_history, and only a team row.
    assert update_statement.startswith("UPDATE feature_flag_catalog")
    assert _SCHEMA_HISTORY in update_statement
    assert "tier_id = 'community'" in update_statement
    assert "tier_id = 'team'" in update_statement
    # The retirement deletes only the pipeline-delete row.
    assert delete_statement.startswith("DELETE FROM feature_flag_catalog")
    assert _PIPELINE_DELETE in delete_statement


def test_upgrade_simulates_an_existing_install_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statements = _capture_statements(monkeypatch, lambda module: module.upgrade())

    connection = _open_connection()
    try:
        _seed(
            connection,
            {
                _SCHEMA_HISTORY: {"description": "Version history and diff", "tier_id": "team", "is_active": True},
                _PIPELINE_DELETE: {"description": "Allow hard-deleting pipelines from the UI", "tier_id": "team", "is_active": True},
                _OTHER_TEAM: {"description": "SSO", "tier_id": "team", "is_active": True},
                _OTHER_COMMUNITY: {"description": "Observability", "tier_id": "community", "is_active": True},
            },
        )

        for statement in statements:
            connection.execute(statement)
        connection.commit()
        first_pass = _rows_by_name(connection)

        # Re-running the upgrade must be a no-op.
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        second_pass = _rows_by_name(connection)
    finally:
        connection.close()

    assert first_pass == second_pass
    # The freed capability is community on the upgraded install.
    assert second_pass[_SCHEMA_HISTORY]["tier_id"] == "community"
    # The retired flag row is gone, so the admin surface cannot advertise it.
    assert _PIPELINE_DELETE not in second_pass
    # No other flag's tier changes.
    assert second_pass[_OTHER_TEAM]["tier_id"] == "team"
    assert second_pass[_OTHER_COMMUNITY]["tier_id"] == "community"


def test_downgrade_restores_the_team_row_and_the_retired_flag_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statements = _capture_statements(monkeypatch, lambda module: module.downgrade())

    connection = _open_connection()
    try:
        _seed(
            connection,
            {
                _SCHEMA_HISTORY: {"description": "Version history and diff", "tier_id": "community", "is_active": True},
                _OTHER_TEAM: {"description": "SSO", "tier_id": "team", "is_active": True},
                _OTHER_COMMUNITY: {"description": "Observability", "tier_id": "community", "is_active": True},
            },
        )

        for statement in statements:
            connection.execute(statement)
        connection.commit()
        first_pass = _rows_by_name(connection)

        # Re-running the downgrade must be a no-op (the re-insert is guarded).
        for statement in statements:
            connection.execute(statement)
        connection.commit()
        second_pass = _rows_by_name(connection)
    finally:
        connection.close()

    assert first_pass == second_pass
    assert second_pass[_SCHEMA_HISTORY]["tier_id"] == "team"
    restored = second_pass[_PIPELINE_DELETE]
    assert restored["tier_id"] == "team"
    assert restored["description"] == "Allow hard-deleting pipelines from the UI"
    assert restored["is_active"] == 1
    assert second_pass[_OTHER_TEAM]["tier_id"] == "team"
    assert second_pass[_OTHER_COMMUNITY]["tier_id"] == "community"
