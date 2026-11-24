"""0365 - runs-family ownership columns + three-pass backfill (ADR 070 Phase 1A).

Two layers of proof, both runnable without Docker:

* **Structural** - the migration adds exactly the intended columns (nullable, FK
  ``ON DELETE RESTRICT``, indexed), runs the passes in the order the contract
  requires, and its downgrade removes everything it added.
* **Executable** - the dialect-portable statements (pass 2, the legacy marker and
  the child copy-down) are EXECUTED against an in-memory SQLite database seeded
  with the same shapes the real-backfill test uses, so a broken predicate fails
  here in the fast suite.

The full upgrade/downgrade against real Postgres (including the audit-event
resolution pass, which needs ``jsonb`` and ``::uuid``) is
``tests/integration/db/test_runs_family_owner_team.py``.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0365_runs_family_owner_team.py"

_CHILD_TABLES = {
    "evidence",
    "node_observations",
    "nodes",
    "run_attempts",
    "run_evidence",
    "run_node_outputs",
}


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0365_runs_family_owner_team", _MIGRATION_PATH)
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


def test_upgrade_adds_nullable_restrict_fk_columns_to_every_child_table(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "upgrade")
    added = {(c[1], c[2]) for c in calls if c[0] == "add_column"}
    for table in module.CHILD_TABLES:
        assert (table, "owner_team_id") in added
    assert {c[1] for c in calls if c[0] == "add_column" and c[2] == "owner_team_id"} == _CHILD_TABLES

    # Nullable everywhere: ADD COLUMN must stay metadata-only (no rewrite of the
    # largest tables, and no fabricated owner for an org-level run).
    assert {c[3] for c in calls if c[0] == "add_column" and c[2] == "owner_team_id"} == {True}

    fks = {(c[1], c[3]) for c in calls if c[0] == "create_foreign_key"}
    assert fks == {(table, "RESTRICT") for table in module.CHILD_TABLES}

    indexed = {c[1] for c in calls if c[0] == "create_index"}
    assert indexed == _CHILD_TABLES
    for call in calls:
        if call[0] == "create_index":
            assert call[2] == f"ix_{call[1]}_owner_team_id"


def test_upgrade_marks_legacy_on_the_two_ownership_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "upgrade")
    added = {(c[1], c[2]) for c in calls if c[0] == "add_column"}
    assert {(table, "owner_unresolved") for table in module.LEGACY_MARKER_TABLES} <= added
    assert module.LEGACY_MARKER_TABLES == ("runs", "suite_runs")


def test_upgrade_runs_the_three_passes_in_order_before_the_children(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "upgrade")
    executed = [c[1] for c in calls if c[0] == "execute"]
    expected = [
        module.BACKFILL_RUNS_NEVER_MOVED_SQL,
        module.BACKFILL_RUNS_MOVED_SQL,
        module.MARK_LEGACY_RUNS_SQL,
        module.MARK_LEGACY_BROKEN_CHAIN_RUNS_SQL,
        module.BACKFILL_SUITE_RUNS_NEVER_MOVED_SQL,
        module.BACKFILL_SUITE_RUNS_MOVED_SQL,
        module.MARK_LEGACY_SUITE_RUNS_SQL,
        module.MARK_LEGACY_BROKEN_CHAIN_SUITE_RUNS_SQL,
    ]
    assert executed[: len(expected)] == expected
    # Children are copied AFTER the parent runs are resolved, never before.
    for table in ("node_observations", "run_attempts", "run_evidence", "run_node_outputs"):
        child_sql = module.BACKFILL_RUN_CHILD_SQL_TEMPLATE.format(table=table)
        assert executed.index(child_sql) > executed.index(module.BACKFILL_RUNS_MOVED_SQL)
    assert module.BACKFILL_EVIDENCE_SQL in executed
    assert module.BACKFILL_NODES_SQL in executed


def test_the_moved_pass_never_copies_the_live_pipeline_owner() -> None:
    """The whole point of pass 3: the answer comes from the audit-event chain at
    ``created_at``, never from ``pipelines.owner_team_id``."""
    module = _load_migration()
    sql = module.BACKFILL_RUNS_MOVED_SQL
    assert "SET owner_team_id = p.owner_team_id" not in sql
    assert "audit_events" in sql
    assert "s.created_at <= r.created_at" in sql
    assert "old_team_id" in sql
    # The tail corroboration: a chain that disagrees with the pipeline's current
    # owner is untrustworthy (a later move that wrote no event).
    assert "last.new_team_id IS DISTINCT FROM p.owner_team_id" in sql


def test_downgrade_drops_every_added_column_index_and_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    module, calls = _capture(monkeypatch, "downgrade")
    dropped_columns = {(c[1], c[2]) for c in calls if c[0] == "drop_column"}
    for table in module.CHILD_TABLES:
        assert (table, "owner_team_id") in dropped_columns
        assert ("drop_constraint", table, f"{table}_owner_team_id_fkey") in calls
        assert ("drop_index", table, f"ix_{table}_owner_team_id") in calls
    for table in module.LEGACY_MARKER_TABLES:
        assert (table, "owner_unresolved") in dropped_columns


_SQLITE_DDL = """
CREATE TABLE pipelines (id text primary key, owner_team_id text);
CREATE TABLE audit_events (event_type text, resource_type text, resource_id text);
CREATE TABLE runs (
    id text primary key,
    pipeline_id text,
    owner_team_id text,
    owner_unresolved boolean
);
CREATE TABLE suite_runs (id text primary key, owner_team_id text, owner_unresolved boolean, extra text);
CREATE TABLE run_attempts (run_id text, owner_team_id text);
CREATE TABLE run_node_outputs (run_id text, node_id text, owner_team_id text);
"""


def test_portable_passes_resolve_and_mark_against_real_rows() -> None:
    """Execute pass 2, the legacy marker and the child copy-down on seeded rows.

    Shapes: a never-moved team pipeline, an org-level pipeline, a moved pipeline
    (the moved run must NOT be touched by pass 2) and a legacy-unowned run.
    """
    module = _load_migration()
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_SQLITE_DDL)
        conn.executescript(
            """
            INSERT INTO pipelines VALUES ('p_team', 'team-a'), ('p_org', NULL), ('p_moved', 'team-b');
            INSERT INTO audit_events VALUES
                ('resource_team_ownership_changed', 'pipeline', 'p_moved');
            INSERT INTO runs VALUES
                ('r_never', 'p_team', NULL, NULL),
                ('r_org', 'p_org', NULL, NULL),
                ('r_moved', 'p_moved', NULL, NULL),
                ('r_stamped', 'p_team', 'team-z', NULL);
            INSERT INTO suite_runs VALUES
                ('s_never', NULL, NULL, '{"pipeline_id": "p_team"}'),
                ('s_moved', NULL, NULL, '{"pipeline_id": "p_moved"}');
            INSERT INTO run_attempts VALUES ('r_never', NULL), ('r_moved', NULL);
            """
        )
        conn.execute(module.BACKFILL_RUNS_NEVER_MOVED_SQL)
        conn.execute(module.MARK_LEGACY_RUNS_SQL)
        conn.execute(module.BACKFILL_RUN_CHILD_SQL_TEMPLATE.format(table="run_attempts"))

        owners = dict(conn.execute("SELECT id, owner_team_id FROM runs"))
        assert owners["r_never"] == "team-a"  # pass 2 copied the pipeline owner
        assert owners["r_org"] is None  # org-level pipeline stays unowned
        assert owners["r_moved"] is None  # moved pipeline is NOT copied
        assert owners["r_stamped"] == "team-z"  # pass 1 untouched

        legacy = dict(conn.execute("SELECT id, owner_unresolved FROM runs"))
        assert legacy["r_moved"] == 1  # unowned + team-owned pipeline => legacy
        assert legacy["r_org"] is None  # genuinely org-level is NOT legacy
        assert legacy["r_never"] is None  # resolved => not legacy

        assert dict(conn.execute("SELECT run_id, owner_team_id FROM run_attempts")) == {"r_never": "team-a", "r_moved": None}
    finally:
        conn.close()
