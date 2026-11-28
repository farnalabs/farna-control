"""FAR-2820 - the Costs overview's "Total Runs" must count every terminal run.

Regression for the impossible "Total Runs 0 / 226 successful runs" pair on the
``/admin/costs`` overview. "Total Runs" was derived from the spend ledger
(``org_daily_run_counts.run_count``), which only records a run with a POSITIVE
persisted cost (``cost_controller.finalize._ledger_run_date`` skips a zero-cost
terminal, and the per-day backfill mirrors that rule), while "successful runs"
on the SAME card is read from ``/analytics/query`` - i.e. ``run_daily_facts``,
one row per terminal run. On a zero-cost model backend the ledger is empty, so
the card claimed 0 runs next to hundreds of successes.

This seeds ``run_daily_facts`` rows ONLY (no ledger rows - the exact staging
shape) and calls the real ``get_costs`` route function. The returned
``org_run_count`` must be the fact count for the caller's organisation and the
report period window, so ``total >= successful`` holds by construction, and
both must exclude an out-of-window run and another organisation's run.

Requires Docker (testcontainers Postgres with the real migration chain) - runs
in CI's changed-integration job; collection + style are verified locally.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from farna.api.routes.costs import get_costs
from farna.auth.jwt import TenantPrincipal

pytestmark = pytest.mark.integration


def _month_start(today: date) -> date:
    return date(today.year, today.month, 1)


async def test_total_runs_counts_every_terminal_run_and_never_undercuts_successes(db_engine: AsyncEngine) -> None:
    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    today = datetime.now(UTC).date()
    out_of_window = _month_start(today) - timedelta(days=1)

    # In-window mixture for OUR org: 2 complete + 1 failed => successful = 2,
    # total = 3. The previous-month run and the other org's run must not count.
    fact_rows: list[tuple[uuid.UUID, date, str]] = [
        (org_id, today, "complete"),
        (org_id, today, "complete"),
        (org_id, today, "failed"),
        (org_id, out_of_window, "complete"),
        (other_org_id, today, "complete"),
    ]

    async with db_engine.connect() as conn, conn.begin():
        for oid, name in ((org_id, "Costs Overview Org"), (other_org_id, "Costs Overview Other Org")):
            await conn.execute(
                text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
                {"id": str(oid), "name": name, "slug": f"costs-overview-{oid.hex[:8]}"},
            )
        await conn.execute(
            text("INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"),
            {"id": str(account_id), "email": f"costs-overview-{account_id.hex[:8]}@integration.test", "name": "Costs Overview User"},
        )
        for fact_org_id, run_date, status in fact_rows:
            await conn.execute(
                text("INSERT INTO run_daily_facts (organisation_id, run_id, run_date, status, trigger_type) VALUES (:oid, :rid, :run_date, :status, 'manual')"),
                {"oid": str(fact_org_id), "rid": str(uuid.uuid4()), "run_date": run_date, "status": status},
            )

        successful = (
            await conn.execute(
                text("SELECT count(*) FROM run_daily_facts WHERE organisation_id = :oid AND run_date >= :since AND status = 'complete'"),
                {"oid": str(org_id), "since": _month_start(today)},
            )
        ).scalar_one()

    assert successful == 2

    principal = TenantPrincipal(
        username="costs-overview@integration.test",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    try:
        async with factory() as session:
            report = await get_costs(group_by="team", period="month", current_user=principal, session=session)

        # No ledger rows exist for this org (the zero-cost shape), so the
        # pre-FAR-2820 figure would have been 0.
        async with factory() as session:
            ledger_rows = (
                await session.execute(
                    text("SELECT count(*) FROM org_daily_run_counts WHERE organisation_id = :oid"),
                    {"oid": str(org_id)},
                )
            ).scalar_one()
        assert ledger_rows == 0

        # Both figures count only the caller's org and only the period window:
        # 3 in-window facts (2 complete + 1 failed), not the 5 seeded rows.
        assert report.org_run_count == 3
        # The successful count is the SAME window's complete subset (2), so it
        # also excludes the previous-month complete fact - the exact window
        # mismatch that produced "0 total / hundreds successful".
        assert report.org_successful_run_count == 2
        # The invariant that could not hold before the fix.
        assert report.org_run_count >= successful
        assert report.org_run_count >= (report.org_successful_run_count or 0)
        # Spend really is zero here - a $0.00 total with 3 runs is correct.
        assert float(report.org_total or "0") == 0.0
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM run_daily_facts WHERE organisation_id IN (:a, :b)"),
                {"a": str(org_id), "b": str(other_org_id)},
            )
            await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(account_id)})
            await conn.execute(
                text("DELETE FROM organisations WHERE id IN (:a, :b)"),
                {"a": str(org_id), "b": str(other_org_id)},
            )


async def test_total_and_successful_share_one_window_across_a_month_boundary(db_engine: AsyncEngine) -> None:
    """FAR-2820: total and successful must be scoped to the SAME window.

    The original defect let the two figures be computed over different windows:
    "Total Runs" came from the UTC month while "successful runs" came from a
    browser-local month start, so at the start of a UTC month a browser west of
    UTC counted the PREVIOUS month's successes against the current month's
    (small) total. This seeds a success on the last day of the previous month
    and successes/failures on the first day of the current month and today, then
    proves the route returns both figures from the single UTC month-to-date
    window: the previous-month success is excluded from BOTH, so total >=
    successful always holds.
    """
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()
    today = datetime.now(UTC).date()
    month_start = _month_start(today)
    previous_month_last_day = month_start - timedelta(days=1)

    # Previous-month success (must be excluded from BOTH figures) + in-window
    # mixture. The month_start and today rows are distinct rows even when
    # today == month_start, so the expected totals are stable either way.
    fact_rows: list[tuple[date, str]] = [
        (previous_month_last_day, "complete"),
        (month_start, "complete"),
        (month_start, "failed"),
        (today, "complete"),
        (today, "failed"),
    ]

    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": "Costs Boundary Org", "slug": f"costs-boundary-{org_id.hex[:8]}"},
        )
        await conn.execute(
            text("INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"),
            {"id": str(account_id), "email": f"costs-boundary-{account_id.hex[:8]}@integration.test", "name": "Costs Boundary User"},
        )
        for run_date, status in fact_rows:
            await conn.execute(
                text("INSERT INTO run_daily_facts (organisation_id, run_id, run_date, status, trigger_type) VALUES (:oid, :rid, :run_date, :status, 'manual')"),
                {"oid": str(org_id), "rid": str(uuid.uuid4()), "run_date": run_date, "status": status},
            )

    principal = TenantPrincipal(
        username="costs-boundary@integration.test",
        organisation_id=org_id,
        account_id=account_id,
        org_role="admin",
    )
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    try:
        async with factory() as session:
            report = await get_costs(group_by="team", period="month", current_user=principal, session=session)

        # Four in-window terminal runs (2 complete + 2 failed), one success per
        # period day, NOT the 5 seeded rows: the previous-month success is out
        # of the shared window for BOTH figures.
        assert report.org_run_count == 4
        assert report.org_successful_run_count == 2
        assert report.org_run_count >= (report.org_successful_run_count or 0)
    finally:
        async with db_engine.connect() as conn, conn.begin():
            await conn.execute(
                text("DELETE FROM run_daily_facts WHERE organisation_id = :oid"),
                {"oid": str(org_id)},
            )
            await conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": str(account_id)})
            await conn.execute(text("DELETE FROM organisations WHERE id = :id"), {"id": str(org_id)})
