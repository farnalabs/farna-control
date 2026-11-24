"""Backfill the brand-free ``synthetic_failure`` node-telemetry marker.

The rebrand added a brand-free machine marker (``synthetic_failure``) for the
sandbox runner's synthetic-failure envelope, written alongside the legacy marker
(dual-write) and read alongside it (dual-read). That keeps cross-release
finalization correct on its own: a run that outlives a rolling deploy is scanned
from ``run_node_outputs`` and the predicate accepts either key.

This migration additionally normalises the CURRENT storage surface
(``run_node_outputs.node_telemetry_json``) so historical rows carry the new key
as well. It is purely ADDITIVE: the legacy key is left in place, because those
rows are evidence and the drop of the old read path is a later release.

What it touches
---------------
Only ``run_node_outputs.node_telemetry_json`` — the current single telemetry
surface. Pre-P1b rows whose marker survives only in the legacy mixed
``outputs_json`` shape are left untouched: the finalize reader already scans that
shape (``_sandbox_failure_scan_views``) and dual-reads both keys, so a rewrite of
that hot, mixed-shape column would add churn and lock time for no convergence.

Safety
------
* Every statement is idempotent: the ``NOT jsonb_exists(..., 'synthetic_failure')``
  guard means a re-run, or a row already written by the new writer, matches
  nothing.
* The statements run in bounded batches on a DEDICATED autocommit connection, so
  a large table is not rewritten under one long lock and an interrupted run
  simply resumes on the next ``upgrade``. Alembic's own transaction is left
  intact (so the version row is still recorded).
* ``jsonb_typeof(...) = 'object'`` keeps the jsonb ``||`` merge well-typed;
  ``jsonb_exists`` (the function form of the ``?`` operator) is used so the
  marker is never mistaken for a bind parameter.

Downgrade: a deliberate no-op. The change is additive, and removing the new key
could destroy a value a post-R1 writer legitimately stamped. Reverting the
writer (and keeping the dual-read) is the supported rollback; no data has to
move.

Revision ID: 0365_backfill_synthetic_failure_marker
Revises: 0364_rewrite_legacy_persisted_values
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "0365_backfill_synthetic_failure_marker"
down_revision: str | None = "0364_rewrite_legacy_persisted_values"
branch_labels = None
depends_on = None

#: The brand-free marker this release writes and reads (see ``node_runner``).
_CURRENT_MARKER = "synthetic_failure"
#: The legacy marker written by earlier releases. Fixed, code-owned literal.
_LEGACY_MARKER = "modulo_synthetic_failure"

#: Bounded batch so a large ``run_node_outputs`` table is never rewritten under
#: one lock; each batch is committed independently (autocommit connection).
_BATCH_SIZE = 1000

_BACKFILL_SQL = """
UPDATE public.run_node_outputs
SET node_telemetry_json = node_telemetry_json || jsonb_build_object(:current, true)
WHERE ctid IN (
    SELECT ctid
    FROM public.run_node_outputs
    WHERE node_telemetry_json IS NOT NULL
      AND jsonb_typeof(node_telemetry_json) = 'object'
      AND jsonb_exists(node_telemetry_json, :legacy)
      AND NOT jsonb_exists(node_telemetry_json, :current)
    LIMIT :batch
)
"""


def _backfill(bind: Connection) -> None:
    """Add the current marker to every telemetry row that carries the legacy one."""
    engine = bind.engine
    with engine.connect() as raw_conn:
        conn = raw_conn.execution_options(isolation_level="AUTOCOMMIT")
        params = {"current": _CURRENT_MARKER, "legacy": _LEGACY_MARKER, "batch": _BATCH_SIZE}
        while True:
            result = conn.execute(sa.text(_BACKFILL_SQL), params)
            if result.rowcount == 0:
                break


def upgrade() -> None:
    _backfill(op.get_bind())


def downgrade() -> None:
    """No-op: additive backfill; see the module docstring for the rollback."""
