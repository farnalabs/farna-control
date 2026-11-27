"""Shared ``run_daily_facts`` window aggregate (ADR 020).

The single read helper for a plain count/token/duration/success aggregate over
one ``run_date`` window. Both the dashboard summary and the Costs overview use
it so the two surfaces cannot drift apart: a run counted as a "total run" on one
card is the same row a "successful run" is drawn from on the other.

Window bounds are ``run_date`` day boundaries (``[start_date, end_date)``) — the
same day-level bucket key the analytics foundation and the spend ledger use.
Filtering on ``created_at`` (the write/source-run instant) would mis-scope
backfilled rows, whose ``created_at`` is recent but whose ``run_date`` falls in
the target window.

Isolation invariant: the explicit ``organisation_id = :org`` predicate is the
primary control (farna_control_app is NOBYPASSRLS so RLS also enforces org
scoping; the predicate is defence-in-depth-reinforced) — same invariant the
analytics foundation uses.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from farna.db.models.run_daily_facts import RunDailyFact


def _safe_int(value: int | None) -> int:
    return int(value) if value is not None else 0


async def facts_window(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    """Aggregate ``run_daily_facts`` for one window (count/tokens/duration/success).

    Returns ``total_runs`` (every terminal fact in the window) and
    ``successful_runs`` (the ``status == 'complete'`` subset of the SAME rows) so
    a caller that renders both figures is guaranteed ``total_runs >=
    successful_runs`` — a success is by definition a terminal run.
    """
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count(func.distinct(RunDailyFact.pipeline_id)).label("active_pipelines"),
                func.sum(RunDailyFact.total_tokens).label("tokens"),
                func.avg(RunDailyFact.duration_ms).label("avg_duration_ms"),
                func.count().filter(RunDailyFact.status == "complete").label("complete"),
            ).where(
                RunDailyFact.organisation_id == org_id,
                RunDailyFact.run_date >= start_date,
                RunDailyFact.run_date < end_date,
            )
        )
    ).one()
    total = _safe_int(row.total)
    complete = _safe_int(row.complete)
    return {
        "total_runs": total,
        "successful_runs": complete,
        "active_pipelines": _safe_int(row.active_pipelines),
        "tokens": _safe_int(row.tokens),
        "avg_duration_ms": round(float(row.avg_duration_ms), 1) if row.avg_duration_ms is not None else None,
        "success_rate": round(complete / total * 100, 1) if total > 0 else None,
    }
