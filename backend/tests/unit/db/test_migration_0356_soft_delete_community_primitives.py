"""The 0356 migration soft-deletes ONLY live community-sourced library rows (FAR-2592).

The migration is a data-only UPDATE, so these tests pin the exact predicate it
executes: only ``source = 'community'`` rows that are still live are stamped,
which is what makes the upgrade idempotent and keeps every other row
(``local`` / ``registry`` / ``modulo``, and already-deleted rows) untouched.
The real-Postgres execution of the whole chain is rehearsed by
``tests/integration/db/test_migration_rehearsal.py`` in CI (this workstation
has no Docker), so the unit tier pins the statement and the graph shape.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0356_soft_delete_community_library_primitives.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0356_soft_delete_community", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0356_soft_delete_community_library_primitives"
    assert module.down_revision == "0355_drop_library_sync_state"


def test_upgrade_soft_deletes_only_live_community_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)

    module.upgrade()

    assert len(executed) == 1
    statement = executed[0]
    assert "UPDATE public.library_primitives" in statement
    assert "SET deleted_at = now()" in statement
    assert "source = 'community'" in statement
    # The live-row guard is what keeps the upgrade idempotent.
    assert "deleted_at IS NULL" in statement
    # Soft delete only: the row IS preserved and its provenance is NOT rewritten.
    assert "DELETE FROM" not in statement.upper()
    assert "TRUNCATE" not in statement.upper()
    assert "source = 'local'" not in statement


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
