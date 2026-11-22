"""The 0361 migration lowers ONLY the five operational catalog rows (FAR-2665).

FAR-2665 moved ``runtime_config``, ``rate_limits``, ``plugin_management``,
``error_tracking`` and ``error_forwarders`` to Community in the code registry.
The boot seed now reconciles ``tier_id`` from code on every boot, but an install
that upgrades before its next boot must still receive the change (and the admin
Feature Flags surface must stop advertising a free capability as paid).
Migration 0361 rewrites those rows once.

These tests pin the exact predicates the migration executes, and EXECUTE its own
captured statement against an in-memory SQLite database (the ``env.py`` target).
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

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0361_lower_operational_flag_tiers.py"

_SQLITE_DDL = """
CREATE TABLE feature_flag_catalog (
    name text primary key,
    tier_id text
)
"""

_LOWERED = (
    "runtime_config",
    "rate_limits",
    "plugin_management",
    "error_tracking",
    "error_forwarders",
)
_OTHER_TEAM = "sso"
_OTHER_COMMUNITY = "observability"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0361_lower_operational_flag_tiers", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture_statement(monkeypatch: pytest.MonkeyPatch, run: Callable[[ModuleType], None]) -> str:
    """Run *run* with ``op.execute`` captured and return its one statement."""
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)

    run(module)

    assert len(executed) == 1
    return executed[0]


def _capture_upgrade_statement(monkeypatch: pytest.MonkeyPatch) -> str:
    return _capture_statement(monkeypatch, lambda module: module.upgrade())


def _capture_downgrade_statement(monkeypatch: pytest.MonkeyPatch) -> str:
    return _capture_statement(monkeypatch, lambda module: module.downgrade())


def _tier_id_by_name(connection: sqlite3.Connection) -> dict[str, str | None]:
    rows = connection.execute("SELECT name, tier_id FROM feature_flag_catalog").fetchall()
    return {row[0]: row[1] for row in rows}


def _seed(connection: sqlite3.Connection, tiers: dict[str, str]) -> None:
    connection.executemany(
        "INSERT INTO feature_flag_catalog (name, tier_id) VALUES (?, ?)",
        list(tiers.items()),
    )


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0361_lower_operational_flag_tiers"
    assert module.down_revision == "0360_free_schema_history_retire_pipeline_delete"


def test_upgrade_statement_is_scoped_to_the_five_flags_and_the_team_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_upgrade_statement(monkeypatch)

    # Portable spelling: no Postgres-only schema prefix and no Postgres-only function.
    assert "UPDATE feature_flag_catalog" in statement
    assert "public." not in statement
    assert "now()" not in statement.lower()
    for name in _LOWERED:
        assert name in statement
    # Exactly one of each unrelated tier must not be touched.
    assert "sso" not in statement
    assert "tier_id = 'team'" in statement
    assert "tier_id = 'community'" in statement


def test_upgrade_converts_exactly_the_five_team_rows_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_upgrade_statement(monkeypatch)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        _seed(
            connection,
            {
                **dict.fromkeys(_LOWERED, "team"),
                _OTHER_TEAM: "team",
                _OTHER_COMMUNITY: "community",
            },
        )

        connection.execute(statement)
        connection.commit()
        first_pass = _tier_id_by_name(connection)

        # Re-running the upgrade must be a no-op: the team guard skips converted rows.
        connection.execute(statement)
        connection.commit()
        second_pass = _tier_id_by_name(connection)
    finally:
        connection.close()

    assert first_pass == second_pass
    for name in _LOWERED:
        assert second_pass[name] == "community", f"{name} must be lowered to community"
    assert second_pass[_OTHER_TEAM] == "team"
    assert second_pass[_OTHER_COMMUNITY] == "community"


def test_downgrade_reverses_exactly_the_five_community_rows_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_downgrade_statement(monkeypatch)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        _seed(
            connection,
            {
                **dict.fromkeys(_LOWERED, "community"),
                _OTHER_TEAM: "team",
                _OTHER_COMMUNITY: "community",
            },
        )

        connection.execute(statement)
        connection.commit()
        first_pass = _tier_id_by_name(connection)

        # Re-running the downgrade must be a no-op: the community guard skips reverted rows.
        connection.execute(statement)
        connection.commit()
        second_pass = _tier_id_by_name(connection)
    finally:
        connection.close()

    assert first_pass == second_pass
    for name in _LOWERED:
        assert second_pass[name] == "team", f"{name} must be restored to team"
    assert second_pass[_OTHER_TEAM] == "team"
    assert second_pass[_OTHER_COMMUNITY] == "community"
