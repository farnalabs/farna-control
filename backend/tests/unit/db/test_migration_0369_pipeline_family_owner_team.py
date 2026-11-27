"""0369 - pipeline-family ownership columns + backfill (ADR 070 Phase 1A).

Two layers of proof, both runnable without Docker:

* **Structural** - the migration adds exactly the intended columns (nullable, FK
  ``ON DELETE RESTRICT``, indexed) to exactly the two unambiguous pipeline
  children, and its downgrade removes everything it added.
* **Executable** - the backfill statement is EXECUTED against an in-memory
  SQLite database seeded with a team-owned pipeline, an org-level pipeline, an
  already-stamped child and an unowned child, so a broken predicate fails here in
  the fast suite.

The full upgrade from 0368 / downgrade to 0368 against real Postgres (columns,
indexes, FKs, backfill and the Phase 1B precondition, including the non-vacuity
seed) is ``tests/integration/db/test_pipeline_family_owner_team.py``.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0369_pipeline_family_owner_team.py"

_CHILD_TABLES = {"pipeline_edges", "triggers"}

#: Deliberately NOT stamped by this migration (see the migration docstring):
#: folders are org-level navigation, a snapshot mixes live version history with
#: frozen run evidence, and a schema pin owns no pipeline reference at all.
_EXCLUDED_TABLES = {"pipeline_folders", "pipeline_snapshots", "snapshot_schema_pins"}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0369_pipeline_family_owner_team", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(monkeypatch: pytest.MonkeyPatch, run_name: str) -> tuple[ModuleType, list[tuple[object, ...]]]:
    module = _load_migration()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(op, "add_column", lambda table, column: calls.append(("add_column", table, column.name, column.nullable)))
    monkeypatch.setattr(op, "create_index", lambda name, table, columns, **kw: calls.append(("create_index", table, name, tuple(columns))))
    monkeypatch.setattr(
        op,
        "create_foreign_key",
        lambda name, table, referent, columns, referent_columns, **kw: calls.append(("create_foreign_key", table, name, kw.get("ondelete"))),
    )
    monkeypatch.setattr(op, "drop_column", lambda table, name: calls.append(("drop_column", table, name)))
    monkeypatch.setattr(op, "drop_index", lambda name, table_name=None: calls.append(("drop_index", table_name, name)))
    monkeypatch.setattr(op, "drop_constraint", lambda name, table, type_=None: calls.append(("drop_constraint", table, name)))
    monkeypatch.setattr(op, "execute", lambda sql: calls.append(("execute", sql)))
    getattr(module, run_name)()
    return module, calls


def test_the_child_set_is_the_two_unambiguous_pipeline_children() -> None:
    """Pins the scope decision: a future snapshot/pin column needs its own ADR
    answer for the run-kind-vs-edit-kind split, not an extra entry here."""
    module = _load_migration()
    assert set(module.CHILD_TABLES) == _CHILD_TABLES
    assert set(module.CHILD_TABLES).isdisjoint(_EXCLUDED_TABLES)


def test_upgrade_adds_nullable_restrict_fk_columns_to_every_child_table(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "upgrade")
    added = {(c[1], c[2]) for c in calls if c[0] == "add_column"}
    assert added == {(table, "owner_team_id") for table in module.CHILD_TABLES}

    # Nullable everywhere: ADD COLUMN must stay metadata-only, and a child of an
    # org-level pipeline legitimately has no team to inherit.
    assert {c[3] for c in calls if c[0] == "add_column"} == {True}

    fks = {(c[1], c[3]) for c in calls if c[0] == "create_foreign_key"}
    assert fks == {(table, "RESTRICT") for table in module.CHILD_TABLES}

    indexes = [c for c in calls if c[0] == "create_index"]
    assert {c[1] for c in indexes} == _CHILD_TABLES
    for call in indexes:
        assert call[2] == f"ix_{call[1]}_owner_team_id"
        assert call[3] == ("owner_team_id",)


def test_upgrade_backfills_every_child_table_after_the_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "upgrade")
    executed = [c[1] for c in calls if c[0] == "execute"]
    last_add = max(i for i, c in enumerate(calls) if c[0] == "add_column")
    first_execute = min(i for i, c in enumerate(calls) if c[0] == "execute")
    assert first_execute > last_add, "a backfill must never run before the column exists"
    for table in module.CHILD_TABLES:
        assert module.BACKFILL_CHILD_SQL_TEMPLATE.format(table=table) in executed


def test_the_backfill_copies_the_live_pipeline_owner_not_an_event_chain() -> None:
    """The whole point of the pipeline family: a trigger/edge is part of the
    pipeline's live definition, so it follows the CURRENT owner - the opposite of
    the runs family's ``created_at`` resolution."""
    module = _load_migration()
    sql = module.BACKFILL_CHILD_SQL_TEMPLATE.format(table="triggers")
    assert "SET owner_team_id = p.owner_team_id" in sql
    assert "audit_events" not in sql
    assert "created_at" not in sql
    # Idempotent, and an org-level pipeline leaves its children unowned.
    assert "c.owner_team_id IS NULL" in sql
    assert "p.owner_team_id IS NOT NULL" in sql


def test_downgrade_drops_every_added_column_index_and_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "downgrade")
    dropped_columns = {(c[1], c[2]) for c in calls if c[0] == "drop_column"}
    for table in module.CHILD_TABLES:
        assert (table, "owner_team_id") in dropped_columns
        assert ("drop_constraint", table, f"{table}_owner_team_id_fkey") in calls
        assert ("drop_index", table, f"ix_{table}_owner_team_id") in calls


_SQLITE_DDL = """
CREATE TABLE pipelines (id text primary key, owner_team_id text);
CREATE TABLE triggers (id text primary key, pipeline_id text, owner_team_id text);
CREATE TABLE pipeline_edges (id text primary key, pipeline_id text, owner_team_id text);
"""


def test_the_backfill_resolves_and_preserves_against_real_rows() -> None:
    """Execute the backfill on the four shapes that matter.

    * a team-owned pipeline's unowned child inherits the team;
    * an ORG-level pipeline's child stays unowned (NULL owner is not an error);
    * an already-stamped child is never rewritten (idempotence + pass-1 rule);
    * a child of an absent pipeline is left alone (the FK owns that error).
    """
    module = _load_migration()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SQLITE_DDL)
        conn.executescript(
            """
            INSERT INTO pipelines VALUES ('p_team', 'team-a'), ('p_org', NULL);
            INSERT INTO triggers VALUES
                ('t_team', 'p_team', NULL),
                ('t_org', 'p_org', NULL),
                ('t_stamped', 'p_team', 'team-z'),
                ('t_orphan', 'p_missing', NULL);
            INSERT INTO pipeline_edges VALUES
                ('e_team', 'p_team', NULL),
                ('e_org', 'p_org', NULL);
            """
        )
        for table in module.CHILD_TABLES:
            conn.execute(module.BACKFILL_CHILD_SQL_TEMPLATE.format(table=table))

        assert dict(conn.execute("SELECT id, owner_team_id FROM triggers")) == {
            "t_team": "team-a",
            "t_org": None,
            "t_stamped": "team-z",
            "t_orphan": None,
        }
        assert dict(conn.execute("SELECT id, owner_team_id FROM pipeline_edges")) == {"e_team": "team-a", "e_org": None}
    finally:
        conn.close()
