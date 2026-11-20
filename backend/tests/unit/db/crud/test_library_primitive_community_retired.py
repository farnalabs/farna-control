"""Community-sourced primitives never surface in the merged library listing (FAR-2592).

The community library surface was removed and every legacy community-sourced
row is soft-deleted by migration 0356, but the retired source must not surface
even if a live row exists (a stale writer, or a deployment that runs the old
code against the new schema). ``_build_list_conditions`` therefore excludes
``source = 'community'`` from the merged listing, and the still-accepted
``?source=community`` filter resolves to an empty page.

These tests run against a real SQLite engine and drive the PUBLIC service
entrypoint (``list_primitives``), so the assertion is about returned rows, not
about compiled SQL text. SQLite is sufficient: the predicate is
dialect-independent and the unit tier already drives other tests from
``sqlite+aiosqlite`` engines.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.core.library_service import list_primitives
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.soft_delete import register_soft_delete_filter

# Production registers the global filter at app startup (via ``farna.db.session``);
# this module must register it explicitly so it asserts the shipped behaviour
# rather than an unfiltered read (see the repository's soft-delete-test lesson).
register_soft_delete_filter()

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")


@pytest.fixture
async def session(tmp_path):
    """A real SQLite session holding only the ``library_primitives`` table."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library_community.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(LibraryPrimitive.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _primitive(*, source: str, slug: str, deleted_at: datetime | None = None) -> LibraryPrimitive:
    """Build a row that satisfies the source-fields CHECK for local/community."""
    return LibraryPrimitive(
        organisation_id=_ORG_ID,
        source=source,
        primitive_type="workflow",
        name=slug.replace("-", " ").title(),
        slug=slug,
        description=None,
        author="tests",
        version="1.0.0",
        tags=[],
        content_json={"steps": []},
        visibility="org",
        tier="native",
        deleted_at=deleted_at,
    )


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            _primitive(source="local", slug="local-workflow"),
            # LIVE community row: nothing has soft-deleted it in this database.
            _primitive(source="community", slug="legacy-community"),
            _primitive(
                source="community",
                slug="already-soft-deleted-community",
                deleted_at=datetime(2026, 11, 1, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()


class TestMergedListingExcludesRetiredCommunitySource:
    async def test_live_community_row_is_excluded_from_merged_listing(self, session: AsyncSession) -> None:
        await _seed(session)

        page = await list_primitives(session, _ORG_ID)

        slugs = {item.slug for item in page.items}
        assert "legacy-community" not in slugs
        assert "already-soft-deleted-community" not in slugs
        # The local row proves the query ran and merged org rows into the page
        # (so the community absence is the filter, not a degraded empty result).
        assert "local-workflow" in slugs

    async def test_live_community_row_still_exists_in_the_database(self, session: AsyncSession) -> None:
        await _seed(session)

        stored = (await session.execute(select(LibraryPrimitive).where(LibraryPrimitive.slug == "legacy-community"))).scalar_one()

        assert stored.deleted_at is None

    async def test_community_source_filter_resolves_to_an_empty_page(self, session: AsyncSession) -> None:
        await _seed(session)

        community = await list_primitives(session, _ORG_ID, source="community")
        local = await list_primitives(session, _ORG_ID, source="local")

        assert not community.items
        assert community.total == 0
        # Non-vacuous control: the same filter path still serves a live source.
        assert [item.slug for item in local.items] == ["local-workflow"]
        assert local.total == 1
