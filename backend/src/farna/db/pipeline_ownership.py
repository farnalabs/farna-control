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
trigger, a graph edge, a snapshot or a schema pin is part of the pipeline's
**live definition**: it has no meaning apart from the pipeline's present state.
When the pipeline moves to another team the definition moves with it, so its
children follow - a team that did not receive the pipeline must not be able to
read or rewrite its live triggers and graph.

Two shapes, one rule
--------------------
* :func:`resolve_pipeline_owner_team_id` - one ``SELECT`` for an async writer
  that already has a session but not the pipeline row in memory.
* :func:`stamp_owner_from_pipeline` - a mapper ``before_insert`` listener for the
  children, so EVERY ORM insert is stamped by construction. It only runs when the
  writer left ``owner_team_id`` unset, which makes it a safety net rather than a
  second source of truth: the writers that hold the pipeline in memory set the
  value explicitly (no extra round trip) and the listener covers the writers that
  cannot - today the MCP trigger builder, which must not reach into a session
  itself - plus any future writer that forgets.

Both are transaction-scoped reads of ``pipelines`` and therefore see the row
under the caller's RLS organisation context, exactly like the write they feed: a
pipeline the caller cannot see resolves to ``NULL``, and a genuinely org-level
pipeline (``owner_team_id IS NULL``) also resolves to ``NULL``. Neither case
fabricates an owner.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["resolve_pipeline_owner_team_id", "stamp_owner_from_pipeline"]


async def resolve_pipeline_owner_team_id(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID | None:
    """Return the parent pipeline's ``owner_team_id`` (``None`` when org-level or absent).

    ``pipelines.owner_team_id IS NULL`` is a legitimate value: an org-level
    pipeline's children are org-level too, and their owner column stays NULL
    (ADR 070 §4: a NULL owner never means "org-visible" for the runs family, and
    the same rule applies here - the child simply has no team to inherit).
    """
    from farna.db.models.pipeline import Pipeline

    return await session.scalar(select(Pipeline.owner_team_id).where(Pipeline.id == pipeline_id))


def stamp_owner_from_pipeline() -> Any:
    """Build a mapper ``before_insert`` listener that stamps ``owner_team_id``.

    Returns a listener for ``event.listen(Model, "before_insert", ...)``. It reads
    ``target.pipeline_id`` and copies the pipeline's current owner onto the row
    through the SAME connection as the INSERT, so the stamp commits or rolls back
    with the row.

    The guard is a no-op when the writer already set ``owner_team_id`` (the
    explicit path used by the writers that hold the pipeline in memory) and when
    the row has no pipeline reference yet. A pipeline that cannot be read - absent,
    or hidden by the caller's RLS organisation context - leaves the column NULL
    rather than inventing a team; the ``pipeline_id`` foreign key then rejects the
    row exactly as it does today.
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
