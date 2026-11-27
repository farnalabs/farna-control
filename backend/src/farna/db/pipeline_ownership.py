"""Parent-pipeline team-ownership stamping for the pipeline family (ADR 070 Phase 1A).

Phase 1A denormalises the parent's ``owner_team_id`` onto the children of a
``pipelines`` row so the Phase 1B team predicate is answerable from the child row
itself (ADR 070, "Rejected alternatives": a policy subquery against the parent
couples every write to it and costs a per-row probe).

Ownership follows the pipeline's CURRENT owner
---------------------------------------------
This is deliberately the opposite of the runs family
(:mod:`farna.db.run_ownership`). A run is a **historical fact**: its owner is the
team that owned the pipeline when the run was created, so copying the live
pipeline owner would publish a previous team's execution history to the new team
after a reassignment (``db/crud/pipeline._enforce_team_ownership_move``). A
trigger or a graph edge is part of the pipeline's **live definition**: it has no
meaning apart from the pipeline's present state. When the pipeline moves to
another team the definition moves with it, so its children follow - a team that
did not receive the pipeline must not be able to read or rewrite its live
triggers and graph.

Deliberately out of scope (Phase 1A): ``pipeline_snapshots`` and
``snapshot_schema_pins``. A run-kind snapshot is historical evidence, not live
definition, so it must not follow the live owner; those tables are deferred to
Phase 2 and migration 0369 adds no column to them.

One shape, one rule
-------------------
:func:`stamp_owner_from_pipeline` is a mapper ``before_insert`` listener for the
children, so EVERY ORM insert is stamped by construction. It only runs when the
writer left ``owner_team_id`` unset, which makes it a safety net rather than a
second source of truth: the writers that hold the pipeline in memory set the
value explicitly for team-owned pipelines and the listener covers the writers
that cannot - today the MCP trigger builder, which must not reach into a session
itself - plus any future writer that forgets.

The listener is a transaction-scoped read of ``pipelines`` and therefore sees the
row under the caller's RLS organisation context, exactly like the write it feeds:
a pipeline the caller cannot see resolves to ``NULL``, and a genuinely org-level
pipeline (``owner_team_id IS NULL``) also resolves to ``NULL``. Neither case
fabricates an owner. Because an explicit ``NULL`` is indistinguishable from an
unset value, an org-level pipeline's writer still triggers one read per child;
the no-extra-read benefit holds only for team-owned rows the writer stamps itself.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

__all__ = ["stamp_owner_from_pipeline"]


def stamp_owner_from_pipeline() -> Any:
    """Build a mapper ``before_insert`` listener that stamps ``owner_team_id``.

    Returns a listener for ``event.listen(Model, "before_insert", ...)``. It reads
    ``target.pipeline_id`` and copies the pipeline's current owner onto the row
    through the SAME connection as the INSERT, so the stamp commits or rolls back
    with the row.

    The guard is a no-op when the writer already stamped a team (``owner_team_id``
    non-NULL) and when the row has no pipeline reference yet. An explicit ``NULL``
    is indistinguishable from unset, so an org-level pipeline's writer still pays
    one read per child. A pipeline that cannot be read - absent, or hidden by the
    caller's RLS organisation context - leaves the column NULL rather than
    inventing a team; the ``pipeline_id`` foreign key then rejects the row exactly
    as it does today.
    """

    def _listener(_mapper: Any, connection: Any, target: Any) -> None:
        if target.owner_team_id is not None:
            return
        pipeline_id = target.pipeline_id
        if pipeline_id is None:
            return
        # Imported here, not at module scope: the model modules register this
        # listener at import time and a module-level import of Pipeline would
        # cycle through farna.db.models.
        from farna.db.models.pipeline import Pipeline

        target.owner_team_id = connection.execute(select(Pipeline.owner_team_id).where(Pipeline.id == pipeline_id)).scalar_one_or_none()

    return _listener
