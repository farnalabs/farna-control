"""Boot seeder reconciliation (FAR-1875).

``farna.api.main._seed_tier_catalog`` reconciles the code registry onto
``feature_flag_catalog`` with ``INSERT ... ON CONFLICT (name) DO UPDATE`` (the
code ``description``/``tier_id``/``depends_on`` win on every boot, while the
operator's ``is_active`` kill switch is left untouched) and prunes every catalog
row whose ``name`` is not in the current code registry. Without the prune,
retiring a flag is a code-only change that leaves the retired flag's row behind
forever, and the DB-backed admin Feature Flags listing keeps surfacing a flag
that gates nothing — the defect that required a one-off migration for every
retirement.

This proves, against REAL Postgres (the fully migrated testcontainer), that a
stale row present before boot is GONE after the seeder runs, that a live flag is
properly seeded, that a stale projection is reconciled without clearing the
operator's kill switch, and that a second boot is idempotent. It is the standing
root-cause fix, so a future retirement never needs another migration.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from farna.api import main
from farna.core.seed_data.catalog import FLAGS

pytestmark = [pytest.mark.integration]

#: A name absent from the code registry — the row a real boot must prune.
STALE_SENTINEL = "zzz_far1875_boot_prune_sentinel"
#: A live registry flag the seeder must (re)seed.
LIVE_FLAG = "email_config"


async def _catalog_names(url: str) -> set[str]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT name FROM feature_flag_catalog"))).scalars().all()
        return set(rows)
    finally:
        await engine.dispose()


async def _remove_sentinel(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM feature_flag_catalog WHERE name = :n"), {"n": STALE_SENTINEL})
    finally:
        await engine.dispose()


async def _flag_row(url: str, name: str) -> dict[str, object] | None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text("SELECT description, tier_id, depends_on, is_active FROM feature_flag_catalog WHERE name = :n"),
                        {"n": name},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row is not None else None
    finally:
        await engine.dispose()


async def test_seed_tier_catalog_reconciles_a_stale_row_without_clearing_the_kill_switch(migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog INSERT is ``ON CONFLICT DO UPDATE``, not ``DO NOTHING``.

    A code change to a flag's ``description``/``tier_id``/``depends_on`` must reach
    an EXISTING install's row on boot (the FAR-2604 defect was the opposite: a code
    tier change never reached the DB catalogue). The per-install operator kill
    switch (``is_active``) is deliberately NOT in the SET list and must survive the
    boot even when it is false.
    """
    expected = next(flag for flag in FLAGS if flag["name"] == LIVE_FLAG)
    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description) VALUES ('community', 'Community', 0, false, 'Free tier'), ('team', 'Team', 1, true, 'Paid tier') ON CONFLICT (tier_id) DO NOTHING"))
            # A stale projection (wrong description, wrong tier) that the operator
            # has switched OFF. The seeder must reconcile the projection and leave
            # is_active false.
            await conn.execute(
                text("""
                    INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active)
                    VALUES (:n, 'stale description', 'team', NULL, false)
                    ON CONFLICT (name) DO UPDATE SET
                        description = EXCLUDED.description,
                        tier_id = EXCLUDED.tier_id,
                        depends_on = EXCLUDED.depends_on,
                        is_active = false
                """),
                {"n": LIVE_FLAG},
            )

        # Point the seeder's engine seam at this testcontainer for this call.
        monkeypatch.setattr("farna.db.session.engine", engine)

        seeded = await _flag_row(migrated_db_url, LIVE_FLAG)
        assert seeded is not None
        assert seeded["is_active"] is False

        await main._seed_tier_catalog()

        row = await _flag_row(migrated_db_url, LIVE_FLAG)
        assert row is not None
        assert row["description"] == expected["description"], "boot seeder must reconcile a stale description onto the code declaration"
        assert row["tier_id"] == expected["tier_id"], "boot seeder must reconcile a stale tier_id onto the code declaration"
        assert row["depends_on"] == expected["depends_on"], "boot seeder must reconcile stale depends_on onto the code declaration"
        assert row["is_active"] is False, "boot seeder must NOT clear the operator kill switch"
    finally:
        # Restore the seeded default (active) so the session-scoped migrated DB is
        # unchanged for the other tests in this module.
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE feature_flag_catalog SET is_active = true WHERE name = :n"), {"n": LIVE_FLAG})
        await engine.dispose()


async def test_seed_tier_catalog_prunes_stale_rows(migrated_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # The two FK targets the seeder and the sentinel need.
            await conn.execute(text("INSERT INTO tier_catalog (tier_id, label, rank, requires_license, description) VALUES ('community', 'Community', 0, false, 'Free tier'), ('team', 'Team', 1, true, 'Paid tier') ON CONFLICT (tier_id) DO NOTHING"))
            await conn.execute(
                text("INSERT INTO feature_flag_catalog (name, description, tier_id, depends_on, is_active) VALUES (:n, :n, 'team', NULL, true) ON CONFLICT (name) DO NOTHING"),
                {"n": STALE_SENTINEL},
            )
        # Point the seeder's engine seam at this testcontainer for this call.
        monkeypatch.setattr("farna.db.session.engine", engine)

        assert STALE_SENTINEL in await _catalog_names(migrated_db_url)

        await main._seed_tier_catalog()

        names = await _catalog_names(migrated_db_url)
        assert STALE_SENTINEL not in names, "boot seeder must prune a catalog row absent from the code registry"
        assert LIVE_FLAG in names, "boot seeder must still seed a live flag"

        # Idempotent: a second boot changes nothing.
        await main._seed_tier_catalog()
        assert await _catalog_names(migrated_db_url) == names
    finally:
        await engine.dispose()
        await _remove_sentinel(migrated_db_url)
