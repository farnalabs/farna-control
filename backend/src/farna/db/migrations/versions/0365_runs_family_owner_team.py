"""ADR 070 Phase 1A - runs-family team ownership: child columns + backfill.

Phase 1A is **plumbing**: this migration adds the ownership columns the runs
family needs and fills them, and it changes NO policy. Nothing depends on the new
columns yet, so the migration is safe to ship ahead of the Phase 1B contract
release (ADR 070, "Migration plan" / "Rollback": *added columns and backfills are
inert while the policy is org-only*).

What it adds
------------
1. ``owner_team_id`` (FK -> ``teams``, ``ON DELETE RESTRICT``, indexed) on the six
   runs-family child tables that had no ownership column of their own:
   ``run_attempts``, ``run_node_outputs``, ``run_evidence``, ``node_observations``,
   ``evidence`` and ``nodes``.
2. ``owner_unresolved`` (nullable boolean) on ``runs`` and ``suite_runs`` - the
   LEGACY-UNOWNED marker (see "Three passes" below).

Why the child tables denormalise the parent's team
--------------------------------------------------
The Phase 1B team predicate must be answerable from the row itself. A policy
subquery into ``runs`` would couple every background INSERT to the parent row and
cost a per-row probe on the hottest write path (ADR 070, "Rejected alternatives"),
so each child carries the parent run's team, stamped at write time (the writers
are listed in ``db/run_ownership.py`` and the model docstrings).

Ownership is copied from the parent RUN, never from the live pipeline.
``pipelines.owner_team_id`` can be reassigned after a run was created
(``db/crud/pipeline._enforce_team_ownership_move``), so a live-pipeline copy would
silently move a historical run's visibility - and its logs, node outputs and
evidence - to the reassigned team. This migration honours the same rule: the
``runs``/``suite_runs`` backfill reconstructs the owner **at ``created_at``** from
the ownership audit-event chain, and only then does it copy down into the children.

Three passes for ``runs`` (identical for ``suite_runs``)
-------------------------------------------------------
* **Pass 1 - already stamped.** Rows with ``owner_team_id IS NOT NULL`` are never
  touched (no statement names them).
* **Pass 2 - never moved.** A run whose pipeline has NO
  ``resource_team_ownership_changed`` audit event gets the pipeline's current
  owner. With no recorded move there is exactly one owner in that pipeline's
  history, so the current value IS the historical value.
* **Pass 3 - moved.** A run whose pipeline HAS moved is resolved from the event
  sequence at ``run.created_at``: the ``new_team_id`` of the latest event at or
  before the run, else the ``old_team_id`` of the earliest event after it (the
  owner the first move started from).
* **Legacy marking.** Any run (or suite run) still unowned is left UNOWNED and
  stamped ``owner_unresolved = TRUE`` unless its history PROVES it was org-level
  at ``created_at``. Two arms:
  * the pipeline is team-owned today (so the row must belong to that team's
    private history and the resolution failed), or
  * the pipeline's chain is BROKEN - an event's ``old_team_id`` does not match its
    predecessor's ``new_team_id``, or the last event disagrees with the pipeline's
    current owner (i.e. the pipeline was moved by a path that wrote no event).
    This arm applies even when the pipeline is org-level NOW, which is the
    fail-closed case: an unreadable history cannot show the run was org-level, and
    treating it as such would publish a historical team's runs org-wide.

  "Proved org-level" is therefore either a complete chain whose resolution at
  ``created_at`` is NULL, or no ownership events at all on a pipeline that is
  org-level now (it never moved). Those rows keep ``owner_team_id IS NULL`` and
  ``owner_unresolved IS NULL`` - distinguishable from a legacy row by the marker.
  Legacy rows are admin-only after the Phase 1B flip, never "org-visible"
  (ADR 070 ��4: a NULL owner never means org-visible).

Why a second column rather than a convention
--------------------------------------------
``owner_team_id IS NULL AND pipeline.owner_team_id IS NOT NULL`` cannot express
"legacy" on its own: it is also the temporary state of a row the application has
not stamped yet, so a Phase 1B gate written on it could never distinguish "the
backfill finished" from "the backfill did not run". ``owner_unresolved`` is the
least invasive mechanism that makes the two distinguishable: one nullable boolean
on the two ownership ROOTS (children inherit through their parent, so they need
no marker of their own), no table rewrite, no vocabulary to keep in sync, and no
second spelling of NULL (``FALSE`` is deliberately not used - nothing writes it).
The Phase 1B data precondition is therefore exactly:

    zero rows where ``owner_team_id IS NULL``
              AND the pipeline is team-owned
              AND ``owner_unresolved IS NOT TRUE``

asserted by ``tests/integration/db/test_runs_family_owner_team.py``.

Rejected alternatives
---------------------
* Backfill runs from the live pipeline (wrong for a reassigned pipeline: moves
  historical visibility to the new team).
* A text resolution column (a closed vocabulary that every future reader must
  case on, for one bit of information).
* A trigger-based stamp/backfill (the writers can all resolve the parent inside
  their own transaction, so a trigger would add a hidden write path and a row
  lock on ``runs`` for every child insert).
* Marking children individually (their legacy state IS their parent's - one bit
  per run, not one per node output).

Cost / safety
-------------
* Every column is nullable with no server default, so ``ADD COLUMN`` is a
  metadata-only operation on Postgres - no table rewrite, even on ``runs`` and
  ``run_node_outputs``.
* The index builds and the backfill UPDATEs are ordinary transactional DDL/DML.
  This is a pre-release migration chain (ADR 070: "pre-release, so no migration of
  customer data"); on a large install the child copy-down is the expensive part
  and is bounded by ``WHERE owner_team_id IS NULL`` so a re-run touches nothing.
* ``nodes`` is the deprecated composite-template store: it has no production
  writer, so it gets the column and the pass-2 copy only (no run-based
  resolution exists for a pipeline-scoped row).
* ``journeys`` is deliberately NOT backfilled. A journey row is canonical per
  ``(organisation_id, kind, ref)`` and is shared by every team that ever worked
  that work item, so it has no single owner to resolve - and it has no pipeline
  reference, so the reassignment hazard this migration guards against does not
  apply to it. Stamping the minting run's team would let the first minter narrow
  a row every other team relies on: an application-visibility change, not
  plumbing.

Downgrade
---------
Drops the two ``owner_unresolved`` columns and the six ``owner_team_id`` columns
(with their indexes and FKs), restoring the exact pre-0365 shape.
**This is DATA LOSS**: every owner stamped or backfilled by this migration is
discarded, and re-applying 0365 rebuilds it from the current schema state - the
backfill cannot recover a team that the audit-event chain no longer supports, and
any value written by a post-0365 writer is gone for good. The downgrade is safe
only as an emergency revert before Phase 1B depends on the columns.

Revision ID: 0365_runs_family_owner_team
Revises: 0364_rewrite_legacy_persisted_values
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0365_runs_family_owner_team"
down_revision: str | None = "0364_rewrite_legacy_persisted_values"
branch_labels = None
depends_on = None

#: The six runs-family child tables that gain ``owner_team_id``.
CHILD_TABLES: tuple[str, ...] = (
    "node_observations",
    "nodes",
    "run_attempts",
    "run_evidence",
    "run_node_outputs",
    "evidence",
)

#: The owner roots that gain the legacy-unowned marker.
LEGACY_MARKER_TABLES: tuple[str, ...] = ("runs", "suite_runs")

#: The ownership event the pipeline-move detection keys on. Written by the REST
#: transfer guard (``db/crud/pipeline._enforce_team_ownership_move``) and by the
#: MCP ``set_resource_owner_team`` tool, both with ``resource_type='pipeline'`` and
#: a ``{old_team_id, new_team_id}`` payload.
OWNERSHIP_EVENT_TYPE = "resource_team_ownership_changed"

#: Pass 2 for ``runs``: copy the pipeline's owner when the pipeline never moved.
BACKFILL_RUNS_NEVER_MOVED_SQL = """
UPDATE runs AS r
SET owner_team_id = p.owner_team_id
FROM pipelines AS p
WHERE p.id = r.pipeline_id
  AND r.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM audit_events AS ae
      WHERE ae.event_type = 'resource_team_ownership_changed'
        AND ae.resource_type = 'pipeline'
        AND ae.resource_id = p.id
  )
"""

#: Pass 2 for ``suite_runs``: the pipeline id lives in ``extra`` (JSON object), not
#: in a column (see ``eval_suite_run.build_suite_run``).
BACKFILL_SUITE_RUNS_NEVER_MOVED_SQL = """
UPDATE suite_runs AS sr
SET owner_team_id = p.owner_team_id
FROM pipelines AS p
WHERE p.id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
  AND sr.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM audit_events AS ae
      WHERE ae.event_type = 'resource_team_ownership_changed'
        AND ae.resource_type = 'pipeline'
        AND ae.resource_id = p.id
  )
"""

#: The shared ownership-event CTE. ``seq`` numbers each pipeline's events in
#: ``(created_at, id)`` order; ``broken`` is the set of pipelines whose chain
#: contradicts itself or its own current owner, i.e. pipelines a resolved answer
#: cannot be trusted for.
#:
#: The tail check (``last.new_team_id IS DISTINCT FROM p.owner_team_id``) is what
#: catches a move written by a path that recorded no event: the chain then ends on
#: a team the pipeline no longer has. Without it, a reassigned pipeline with a
#: stale prefix would resolve runs to the wrong team - the exact failure the
#: three-pass design exists to prevent.

#: Pass 3 for ``runs``: resolve the owner at ``created_at`` from the event chain.
#: Only a non-NULL resolution is written; a NULL one is left for the legacy marker
#: below (so "resolved to no team" and "could not resolve" share one marker).
BACKFILL_RUNS_MOVED_SQL = """WITH ev AS (
    SELECT ae.resource_id AS pipeline_id,
           ae.id AS event_id,
           ae.created_at,
           NULLIF(ae.payload_json->>'old_team_id', '')::uuid AS old_team_id,
           NULLIF(ae.payload_json->>'new_team_id', '')::uuid AS new_team_id
    FROM audit_events AS ae
    WHERE ae.event_type = 'resource_team_ownership_changed'
      AND ae.resource_type = 'pipeline'
      AND ae.resource_id IS NOT NULL
),
seq AS (
    SELECT ev.pipeline_id,
           ev.event_id,
           ev.created_at,
           ev.old_team_id,
           ev.new_team_id,
           row_number() OVER (
               PARTITION BY ev.pipeline_id
               ORDER BY ev.created_at, ev.event_id
           ) AS rn
    FROM ev
),
broken AS (
    SELECT DISTINCT s.pipeline_id
    FROM seq AS s
    JOIN seq AS prev
      ON prev.pipeline_id = s.pipeline_id
     AND prev.rn = s.rn - 1
    WHERE s.old_team_id IS DISTINCT FROM prev.new_team_id
    UNION
    SELECT DISTINCT last.pipeline_id
    FROM seq AS last
    JOIN pipelines AS p ON p.id = last.pipeline_id
    WHERE last.rn = (
        SELECT max(x.rn) FROM seq AS x WHERE x.pipeline_id = last.pipeline_id
    )
      AND last.new_team_id IS DISTINCT FROM p.owner_team_id
)
,
resolved AS (
    SELECT r.id AS run_id,
           CASE
               WHEN b.pipeline_id IS NOT NULL THEN NULL
               ELSE COALESCE(
                   (
                       SELECT s.new_team_id
                       FROM seq AS s
                       WHERE s.pipeline_id = r.pipeline_id
                         AND s.created_at <= r.created_at
                       ORDER BY s.created_at DESC, s.rn DESC
                       LIMIT 1
                   ),
                   (
                       SELECT s.old_team_id
                       FROM seq AS s
                       WHERE s.pipeline_id = r.pipeline_id
                         AND s.created_at > r.created_at
                       ORDER BY s.created_at ASC, s.rn ASC
                       LIMIT 1
                   )
               )
           END AS owner_team_id
    FROM runs AS r
    JOIN pipelines AS p ON p.id = r.pipeline_id
    LEFT JOIN broken AS b ON b.pipeline_id = r.pipeline_id
    WHERE r.owner_team_id IS NULL
      AND EXISTS (
          SELECT 1 FROM seq AS s WHERE s.pipeline_id = r.pipeline_id
      )
)
UPDATE runs AS r
SET owner_team_id = resolved.owner_team_id
FROM resolved
WHERE r.id = resolved.run_id
  AND resolved.owner_team_id IS NOT NULL
"""

#: Pass 3 for ``suite_runs``: same rule, keyed on ``created_at`` and the pipeline id
#: carried in ``extra``.
BACKFILL_SUITE_RUNS_MOVED_SQL = """WITH ev AS (
    SELECT ae.resource_id AS pipeline_id,
           ae.id AS event_id,
           ae.created_at,
           NULLIF(ae.payload_json->>'old_team_id', '')::uuid AS old_team_id,
           NULLIF(ae.payload_json->>'new_team_id', '')::uuid AS new_team_id
    FROM audit_events AS ae
    WHERE ae.event_type = 'resource_team_ownership_changed'
      AND ae.resource_type = 'pipeline'
      AND ae.resource_id IS NOT NULL
),
seq AS (
    SELECT ev.pipeline_id,
           ev.event_id,
           ev.created_at,
           ev.old_team_id,
           ev.new_team_id,
           row_number() OVER (
               PARTITION BY ev.pipeline_id
               ORDER BY ev.created_at, ev.event_id
           ) AS rn
    FROM ev
),
broken AS (
    SELECT DISTINCT s.pipeline_id
    FROM seq AS s
    JOIN seq AS prev
      ON prev.pipeline_id = s.pipeline_id
     AND prev.rn = s.rn - 1
    WHERE s.old_team_id IS DISTINCT FROM prev.new_team_id
    UNION
    SELECT DISTINCT last.pipeline_id
    FROM seq AS last
    JOIN pipelines AS p ON p.id = last.pipeline_id
    WHERE last.rn = (
        SELECT max(x.rn) FROM seq AS x WHERE x.pipeline_id = last.pipeline_id
    )
      AND last.new_team_id IS DISTINCT FROM p.owner_team_id
)
,
resolved AS (
    SELECT sr.id AS suite_run_id,
           CASE
               WHEN b.pipeline_id IS NOT NULL THEN NULL
               ELSE COALESCE(
                   (
                       SELECT s.new_team_id
                       FROM seq AS s
                       WHERE s.pipeline_id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
                         AND s.created_at <= sr.created_at
                       ORDER BY s.created_at DESC, s.rn DESC
                       LIMIT 1
                   ),
                   (
                       SELECT s.old_team_id
                       FROM seq AS s
                       WHERE s.pipeline_id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
                         AND s.created_at > sr.created_at
                       ORDER BY s.created_at ASC, s.rn ASC
                       LIMIT 1
                   )
               )
           END AS owner_team_id
    FROM suite_runs AS sr
    JOIN pipelines AS p ON p.id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
    LEFT JOIN broken AS b ON b.pipeline_id = p.id
    WHERE sr.owner_team_id IS NULL
      AND EXISTS (
          SELECT 1 FROM seq AS s WHERE s.pipeline_id = p.id
      )
)
UPDATE suite_runs AS sr
SET owner_team_id = resolved.owner_team_id
FROM resolved
WHERE sr.id = resolved.suite_run_id
  AND resolved.owner_team_id IS NOT NULL
"""

#: The legacy marker, arm 1: a run still unowned whose pipeline is team-owned
#: today. Runs LAST, after both ownership passes, so it can only ever mark a row
#: the resolution genuinely could not stamp. This arm is dialect-portable and the
#: unit test EXECUTES it; arm 2 (below) covers the PG-only broken-chain case.
MARK_LEGACY_RUNS_SQL = """
UPDATE runs AS r
SET owner_unresolved = TRUE
FROM pipelines AS p
WHERE p.id = r.pipeline_id
  AND r.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
"""

MARK_LEGACY_SUITE_RUNS_SQL = """
UPDATE suite_runs AS sr
SET owner_unresolved = TRUE
FROM pipelines AS p
WHERE p.id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
  AND sr.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
"""

#: The legacy marker, arm 2 (fail-closed): an unowned run whose pipeline's
#: ownership chain is BROKEN cannot be shown to have been org-level at creation
#: either. Without this arm such a run would stay unowned and unmarked, which
#: after the Phase 1B flip means "org-level" - i.e. a pipeline that was silently
#: moved to org scope (or moved by a path that wrote no event) would publish its
#: historical team's runs org-wide. Marking it legacy keeps it admin-only.
#:
#: This arm covers pipelines whose CURRENT owner is NULL too, so it deliberately
#: does not repeat the ``p.owner_team_id IS NOT NULL`` predicate of arm 1.
MARK_LEGACY_BROKEN_CHAIN_RUNS_SQL = """WITH ev AS (
    SELECT ae.resource_id AS pipeline_id,
           ae.id AS event_id,
           ae.created_at,
           NULLIF(ae.payload_json->>'old_team_id', '')::uuid AS old_team_id,
           NULLIF(ae.payload_json->>'new_team_id', '')::uuid AS new_team_id
    FROM audit_events AS ae
    WHERE ae.event_type = 'resource_team_ownership_changed'
      AND ae.resource_type = 'pipeline'
      AND ae.resource_id IS NOT NULL
),
seq AS (
    SELECT ev.pipeline_id,
           ev.event_id,
           ev.created_at,
           ev.old_team_id,
           ev.new_team_id,
           row_number() OVER (
               PARTITION BY ev.pipeline_id
               ORDER BY ev.created_at, ev.event_id
           ) AS rn
    FROM ev
),
broken AS (
    SELECT DISTINCT s.pipeline_id
    FROM seq AS s
    JOIN seq AS prev
      ON prev.pipeline_id = s.pipeline_id
     AND prev.rn = s.rn - 1
    WHERE s.old_team_id IS DISTINCT FROM prev.new_team_id
    UNION
    SELECT DISTINCT last.pipeline_id
    FROM seq AS last
    JOIN pipelines AS p ON p.id = last.pipeline_id
    WHERE last.rn = (
        SELECT max(x.rn) FROM seq AS x WHERE x.pipeline_id = last.pipeline_id
    )
      AND last.new_team_id IS DISTINCT FROM p.owner_team_id
)

UPDATE runs AS r
SET owner_unresolved = TRUE
FROM broken AS b
WHERE b.pipeline_id = r.pipeline_id
  AND r.owner_team_id IS NULL
"""

MARK_LEGACY_BROKEN_CHAIN_SUITE_RUNS_SQL = """WITH ev AS (
    SELECT ae.resource_id AS pipeline_id,
           ae.id AS event_id,
           ae.created_at,
           NULLIF(ae.payload_json->>'old_team_id', '')::uuid AS old_team_id,
           NULLIF(ae.payload_json->>'new_team_id', '')::uuid AS new_team_id
    FROM audit_events AS ae
    WHERE ae.event_type = 'resource_team_ownership_changed'
      AND ae.resource_type = 'pipeline'
      AND ae.resource_id IS NOT NULL
),
seq AS (
    SELECT ev.pipeline_id,
           ev.event_id,
           ev.created_at,
           ev.old_team_id,
           ev.new_team_id,
           row_number() OVER (
               PARTITION BY ev.pipeline_id
               ORDER BY ev.created_at, ev.event_id
           ) AS rn
    FROM ev
),
broken AS (
    SELECT DISTINCT s.pipeline_id
    FROM seq AS s
    JOIN seq AS prev
      ON prev.pipeline_id = s.pipeline_id
     AND prev.rn = s.rn - 1
    WHERE s.old_team_id IS DISTINCT FROM prev.new_team_id
    UNION
    SELECT DISTINCT last.pipeline_id
    FROM seq AS last
    JOIN pipelines AS p ON p.id = last.pipeline_id
    WHERE last.rn = (
        SELECT max(x.rn) FROM seq AS x WHERE x.pipeline_id = last.pipeline_id
    )
      AND last.new_team_id IS DISTINCT FROM p.owner_team_id
)

UPDATE suite_runs AS sr
SET owner_unresolved = TRUE
FROM broken AS b
WHERE b.pipeline_id = NULLIF(sr.extra->>'pipeline_id', '')::uuid
  AND sr.owner_team_id IS NULL
"""

#: Child copy-down: the parent run's (now backfilled) owner. A plain copy - after
#: the run backfill the parent's value IS the historical answer, so there is no
#: second judgement to make here.
BACKFILL_RUN_CHILD_SQL_TEMPLATE = """
UPDATE {table} AS c
SET owner_team_id = r.owner_team_id
FROM runs AS r
WHERE r.id = c.run_id
  AND c.owner_team_id IS NULL
  AND r.owner_team_id IS NOT NULL
"""

#: ``evidence`` is subject-keyed (``subject_type``/``subject_id`` as TEXT), not
#: run-keyed: only run-subject facts inherit a team, and ``subject_id`` holds
#: ``str(run_id)``.
BACKFILL_EVIDENCE_SQL = """
UPDATE evidence AS e
SET owner_team_id = r.owner_team_id
FROM runs AS r
WHERE e.subject_type = 'run'
  AND e.owner_team_id IS NULL
  AND r.owner_team_id IS NOT NULL
  AND e.subject_id = r.id::text
"""

#: ``nodes`` is pipeline-scoped (deprecated composite-template store, no production
#: writer). Only the unambiguous case is filled: a pipeline that never moved.
BACKFILL_NODES_SQL = """
UPDATE nodes AS n
SET owner_team_id = p.owner_team_id
FROM pipelines AS p
WHERE p.id = n.pipeline_id
  AND n.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM audit_events AS ae
      WHERE ae.event_type = 'resource_team_ownership_changed'
        AND ae.resource_type = 'pipeline'
        AND ae.resource_id = p.id
  )
"""

#: Operator-facing summary of what the backfill could NOT resolve, emitted into the
#: deploy log. A NOTICE is used (not a failure): legacy-unowned history is a
#: documented, expected outcome (ADR 070 ��4/"Phase 1A"), and the Phase 1B gate is
#: what refuses to proceed if the resolved state is not what was expected.
_REPORT_SQL = """
DO $$
DECLARE
    legacy_runs bigint;
    legacy_suite_runs bigint;
BEGIN
    SELECT count(*) INTO legacy_runs FROM runs WHERE owner_unresolved IS TRUE;
    SELECT count(*) INTO legacy_suite_runs FROM suite_runs WHERE owner_unresolved IS TRUE;
    RAISE NOTICE '0365 runs-family ownership: % legacy-unowned run(s), % legacy-unowned suite_run(s)', legacy_runs, legacy_suite_runs;
END
$$
"""


def upgrade() -> None:
    for table in CHILD_TABLES:
        op.add_column(table, sa.Column("owner_team_id", sa.Uuid(), nullable=True))
        op.create_index(f"ix_{table}_owner_team_id", table, ["owner_team_id"])
        op.create_foreign_key(
            f"{table}_owner_team_id_fkey",
            table,
            "teams",
            ["owner_team_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    for table in LEGACY_MARKER_TABLES:
        op.add_column(table, sa.Column("owner_unresolved", sa.Boolean(), nullable=True))

    # Three passes, in order: never-moved, then moved, then the legacy marker
    # (both arms - the second fails closed on an untrustworthy history).
    op.execute(BACKFILL_RUNS_NEVER_MOVED_SQL)
    op.execute(BACKFILL_RUNS_MOVED_SQL)
    op.execute(MARK_LEGACY_RUNS_SQL)
    op.execute(MARK_LEGACY_BROKEN_CHAIN_RUNS_SQL)
    op.execute(BACKFILL_SUITE_RUNS_NEVER_MOVED_SQL)
    op.execute(BACKFILL_SUITE_RUNS_MOVED_SQL)
    op.execute(MARK_LEGACY_SUITE_RUNS_SQL)
    op.execute(MARK_LEGACY_BROKEN_CHAIN_SUITE_RUNS_SQL)

    # Children last, from the resolved parent - never from the live pipeline.
    for table in ("node_observations", "run_attempts", "run_evidence", "run_node_outputs"):
        op.execute(BACKFILL_RUN_CHILD_SQL_TEMPLATE.format(table=table))
    op.execute(BACKFILL_EVIDENCE_SQL)
    op.execute(BACKFILL_NODES_SQL)

    op.execute(_REPORT_SQL)


def downgrade() -> None:
    # DATA LOSS (see the module docstring): every owner stamped by this migration
    # - or written by a post-0365 writer - is discarded, and re-applying 0365 can
    # only rebuild what the audit-event chain still supports.
    for table in LEGACY_MARKER_TABLES:
        op.drop_column(table, "owner_unresolved")

    for table in CHILD_TABLES:
        op.drop_constraint(f"{table}_owner_team_id_fkey", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_owner_team_id", table_name=table)
        op.drop_column(table, "owner_team_id")
