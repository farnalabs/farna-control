"""Parent-run team-ownership resolution for the runs family (ADR 070 Phase 1A).

Every runs-family child row carries ``owner_team_id``, stamped at write time from
its parent **run** so the Phase 1B team predicate can key on the child row itself
instead of joining to ``runs`` (or ``pipelines``) on a hot path.

The parent run is the only source of truth here. A child row must never derive
its team from the live pipeline: ``pipelines.owner_team_id`` can be reassigned
after a run was created (``db/crud/pipeline._enforce_team_ownership_move``), so
copying it would silently move a historical run's visibility - and therefore its
logs, node outputs and evidence - to the reassigned team.

Two shapes, one rule:

* :func:`resolve_run_owner_team_id` - one ``SELECT`` for writers that need the
  value in Python (ORM inserts, executemany parameter dicts).
* :func:`run_owner_team_id_subquery` - a scalar subquery for writers that build a
  Core ``INSERT`` and must not spend a round trip (``run_node_outputs`` is
  written on every node completion).

Both are transaction-scoped reads of ``runs`` and therefore see the row under the
caller's RLS organisation context, exactly like the write they feed. A missing
run yields ``NULL`` (the FK on the child then rejects the insert, as it does
today), never a fabricated owner.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from farna.db.models.run import Run

__all__ = ["resolve_run_owner_team_id", "run_owner_team_id_subquery"]


async def resolve_run_owner_team_id(session: AsyncSession, run_id: uuid.UUID) -> uuid.UUID | None:
    """Return the parent run's ``owner_team_id`` (``None`` when unowned or absent).

    ``runs.owner_team_id`` IS NULL for a run of an org-level pipeline and for a
    legacy run whose owner could not be resolved from the audit history; both are
    legitimate values, and the child row inherits whichever applies.
    """
    return await session.scalar(select(Run.owner_team_id).where(Run.id == run_id))


def run_owner_team_id_subquery(run_id: uuid.UUID) -> Any:
    """Scalar subquery yielding the parent run's ``owner_team_id``.

    Used as an ``INSERT ... VALUES (..., (SELECT runs.owner_team_id ...))``
    column expression so a Core writer stamps the owner without a second round
    trip. It is statement-scoped (not a bind parameter), so it composes with
    ``executemany`` and with ``ON CONFLICT DO UPDATE`` unchanged.
    """
    return select(Run.owner_team_id).where(Run.id == run_id).scalar_subquery()
