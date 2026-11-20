"""The 0357 migration moves ONLY the email/environment-profile catalog rows to community (FAR-2604).

FAR-2604 lowered ``email_config`` and ``environment_profiles`` to the community
tier in ``core.feature_flags._KNOWN_FLAGS``, but that code change is inert on an
existing install: the boot seed inserts catalog rows with
``ON CONFLICT DO NOTHING`` and the DB rows win. Migration 0357 rewrites the two
existing rows once.

These tests pin the exact predicates the migration executes, and EXECUTE its own
captured statement against an in-memory SQLite database (the ``env.py`` target),
because a text-only assertion cannot prove the upgrade runs on a SQLite
deployment. The real-Postgres execution of the whole chain is rehearsed by
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

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0357_community_flag_tiers.py"

_SQLITE_DDL = """
CREATE TABLE feature_flag_catalog (
    name text primary key,
    tier_id text
)
"""

_EMAIL = "email_config"
_ENV = "environment_profiles"
_OTHER_TEAM = "sso"
_OTHER_COMMUNITY = "observability"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0357_community_flag_tiers", _MIGRATION_PATH)
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
    connection.commit()


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0357_community_flag_tiers"
    assert module.down_revision == "0356_soft_delete_community_library_primitives"


def test_upgrade_statement_is_scoped_to_the_two_flags_and_the_team_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_upgrade_statement(monkeypatch)

    # Portable spelling: no Postgres-only schema prefix and no Postgres-only now().
    assert "UPDATE feature_flag_catalog" in statement
    assert "public." not in statement
    assert "now()" not in statement.lower()
    # Only the two named flags, and only rows still on the team tier.
    assert "email_config" in statement
    assert "environment_profiles" in statement
    assert "tier_id = 'team'" in statement
    assert "tier_id = 'community'" in statement


def test_upgrade_converts_exactly_the_two_team_rows_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_upgrade_statement(monkeypatch)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        _seed(
            connection,
            {
                _EMAIL: "team",
                _ENV: "team",
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
    assert second_pass[_EMAIL] == "community"
    assert second_pass[_ENV] == "community"
    assert second_pass[_OTHER_TEAM] == "team"
    assert second_pass[_OTHER_COMMUNITY] == "community"


def test_downgrade_reverses_exactly_the_two_community_rows_on_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = _capture_downgrade_statement(monkeypatch)

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        _seed(
            connection,
            {
                _EMAIL: "community",
                _ENV: "community",
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
    assert second_pass[_EMAIL] == "team"
    assert second_pass[_ENV] == "team"
    assert second_pass[_OTHER_TEAM] == "team"
    assert second_pass[_OTHER_COMMUNITY] == "community"
