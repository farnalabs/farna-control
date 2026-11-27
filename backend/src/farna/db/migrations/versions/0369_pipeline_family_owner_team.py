"""ADR 070 Phase 1A - pipeline-family team ownership: child columns + backfill.

Phase 1A is **plumbing**: this migration adds the ownership columns the pipeline
family needs and fills them, and it changes NO policy. ``triggers`` and
``pipeline_edges`` are still ``org_global`` in ``db/tenant_scope.py`` and stay
org-gated until the Phase 1B contract release, so the new columns are inert
(ADR 070, "Migration plan" / "Rollback": *added columns and backfills are inert
while the policy is org-only*).

What it adds
------------
``owner_team_id`` (FK -> ``teams``, ``ON DELETE RESTRICT``, indexed) on the two
pipeline children whose ownership follows exactly one pipeline: ``triggers`` and
``pipeline_edges``. Both carry a NOT NULL ``pipeline_id`` FK, so every row has
exactly one parent and the parent's team is the row's team.

Ownership follows the pipeline's CURRENT owner - the opposite of the runs family
-------------------------------------------------------------------------------
0365 resolves a run's owner AT ``created_at`` from the ownership audit-event
chain, because a run is a **historical fact**: reassigning the pipeline must not
publish a previous team's execution history to the new team.

A trigger or a graph edge is not a historical fact. It is part of the pipeline's
**live definition**: it has no meaning apart from the pipeline's present state,
and when the pipeline moves to another team the definition moves with it - a
team that did not receive the pipeline must not be able to read or rewrite its
triggers and graph. So this migration copies ``pipelines.owner_team_id`` as it
is, with no event-chain reconstruction. New rows are stamped at insert in the
same transaction (explicitly by the writers that hold the pipeline in memory -
the REST trigger route, graph save, clone, snapshot rollback, template install,
library install and workflow import - and by a model-level ``before_insert``
guard in ``db/pipeline_ownership.py`` for every other ORM writer).

**Flagged, not implemented: a reassignment does not re-stamp existing children.**
Moving a pipeline (``db/crud/pipeline.update_pipeline`` ->
``_enforce_team_ownership_move``, and the bulk
``db/crud/team.reassign_team_resources_to_org`` on team deletion) changes only
``pipelines.owner_team_id``. Nothing reads these child columns before Phase 1B,
and a half-applied propagation (one of the two move paths) would be worse than a
documented gap, so propagation is left as a Phase 1B entry condition: before the
policy flips, the move paths must re-stamp (or the Phase 1B policy must read a
column that is refreshed at move time), otherwise a reassigned pipeline's
triggers and edges keep the previous team's owner.

Excluded tables (deliberately no column)
----------------------------------------
* ``pipeline_folders`` - org-level navigation, not a child of a pipeline. It has
  no ``pipeline_id``; it is a self-referencing tree (``parent_id``) with an
  ``account_id`` recording the creator, and every team browses the same tree
  (``db/tenant_scope.py`` declares it ``org_global`` for that reason).
* ``pipeline_snapshots`` - ownership does NOT follow exactly one pipeline in the
  way this migration can express. A snapshot is an immutable version of the
  definition at a point in time, and the two kinds pull in opposite directions:
  a ``version_kind='edit'`` snapshot is live version history and belongs with the
  pipeline's current team, while a ``version_kind='run'`` snapshot is the frozen
  evidence a historical run replays - the same historical-fact problem 0365
  solved with the audit-event chain, which a single live-owner column cannot
  express. Stamping the live owner would let team B read a team A run's frozen
  graph and deny team A its own evidence. Deferred to Phase 2, where the
  visibility/binding pattern can distinguish the two kinds.
* ``snapshot_schema_pins`` - the parent relationship is to a *snapshot*
  (``snapshot_id``), not to a pipeline: there is no ``pipeline_id`` to read, and
  its scope must follow whatever decision ``pipeline_snapshots`` gets. Deferred
  with it.

Backfill
--------
One pass: copy the parent pipeline's owner onto every unowned child. It is
idempotent (``WHERE owner_team_id IS NULL``) and an org-level pipeline
(``owner_team_id IS NULL``) legitimately leaves its children NULL - a NULL owner
never means "org-visible" (ADR 070 §4), it means "no team to inherit". The
Phase 1B data precondition is therefore exactly:

    zero rows where ``owner_team_id IS NULL`` AND the pipeline is team-owned

asserted by ``tests/integration/db/test_pipeline_family_owner_team.py``.

Cost / safety
-------------
* Every column is nullable with no server default, so ``ADD COLUMN`` is
  metadata-only on Postgres - no table rewrite.
* The index builds and the backfill UPDATEs are ordinary transactional DDL/DML,
  bounded by ``WHERE owner_team_id IS NULL`` so a re-run touches nothing.
* Pre-release migration chain (ADR 070: "pre-release, so no migration of
  customer data"); unlike 0365 there is no audit-event resolution pass, so the
  backfill is one statement per table.

Downgrade
---------
Drops the two ``owner_team_id`` columns with their indexes and FKs, restoring the
exact pre-0369 shape. **This is DATA LOSS**: every owner stamped or backfilled by
this migration - and every value written by a post-0369 writer - is discarded.
Re-applying 0369 rebuilds the value from the pipeline's CURRENT owner, which is
not necessarily the owner a child had when it was written (a pipeline may have
moved in between), so a round trip is lossy even when no rows are added.

Revision ID: 0369_pipeline_family_owner_team
Revises: 0368_invitations_quarantine_rls
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0369_pipeline_family_owner_team"
down_revision: str | None = "0368_invitations_quarantine_rls"
branch_labels = None
depends_on = None

#: The pipeline children that gain ``owner_team_id``: each has exactly one
#: parent (NOT NULL ``pipeline_id``) and its ownership is the pipeline's.
CHILD_TABLES: tuple[str, ...] = ("pipeline_edges", "triggers")

#: One pass per table: the pipeline's CURRENT owner (see the module docstring -
#: a trigger/edge is part of the live definition, so no ``created_at``
#: reconstruction is wanted here), bounded so a re-run is a no-op and an
#: org-level pipeline correctly leaves its children unowned.
BACKFILL_CHILD_SQL_TEMPLATE = """
UPDATE {table} AS c
SET owner_team_id = p.owner_team_id
FROM pipelines AS p
WHERE p.id = c.pipeline_id
  AND c.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
"""

#: Operator-facing summary emitted into the deploy log. A NOTICE, not a failure:
#: only rows under a TEAM-owned pipeline are a gap, and the Phase 1B gate is what
#: refuses to proceed if the resolved state is not what was expected.
_REPORT_SQL = """
DO $$
DECLARE
    unowned_edges bigint;
    unowned_triggers bigint;
BEGIN
    SELECT count(*) INTO unowned_edges
    FROM pipeline_edges AS c JOIN pipelines AS p ON p.id = c.pipeline_id
    WHERE c.owner_team_id IS NULL AND p.owner_team_id IS NOT NULL;
    SELECT count(*) INTO unowned_triggers
    FROM triggers AS c JOIN pipelines AS p ON p.id = c.pipeline_id
    WHERE c.owner_team_id IS NULL AND p.owner_team_id IS NOT NULL;
    RAISE NOTICE '0369 pipeline-family ownership: % unowned pipeline_edge(s), % unowned trigger(s) under a team-owned pipeline', unowned_edges, unowned_triggers;
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

    for table in CHILD_TABLES:
        op.execute(BACKFILL_CHILD_SQL_TEMPLATE.format(table=table))

    op.execute(_REPORT_SQL)


def downgrade() -> None:
    # DATA LOSS (see the module docstring): every owner stamped by this migration
    # - or written by a post-0369 writer - is discarded, and re-applying 0369
    # rebuilds it from the pipeline's CURRENT owner, which may have changed since.
    for table in CHILD_TABLES:
        op.drop_constraint(f"{table}_owner_team_id_fkey", table, type_="foreignkey")
        op.drop_index(f"ix_{table}_owner_team_id", table_name=table)
        op.drop_column(table, "owner_team_id")
