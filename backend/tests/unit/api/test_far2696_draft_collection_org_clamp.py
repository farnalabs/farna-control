"""FAR-2696: the draft-collection resolver clamps to the caller's org.

``_load_draft_collection`` backs the collection UPDATE and PUBLISH endpoints,
both of which WRITE to the row they load (rewrite its pins, or flip it to
``published``). It resolved the id through the id-only ``get_library_primitive``
(RLS only), so on a ``BYPASSRLS`` connection a caller could mutate or publish
another organisation's draft.

The case below runs against a plain in-memory SQLite engine with ``set_rls_org``
patched to a no-op — no PostgreSQL RLS and no tenant-filter listener — so the
only thing that can narrow the read is the ``explicit_org_filter`` the helper
now passes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.api.routes.library import _load_draft_collection
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000e611")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000e612")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=[LibraryPrimitive.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _draft_collection(org_id: uuid.UUID) -> LibraryPrimitive:
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=org_id,
        source="local",
        primitive_type="library_collection",
        name="Draft Collection",
        slug=f"draft-collection-{org_id.int}",
        author="tester",
        version="1.0",
        tags=[],
        content_json={},
        source_url=None,
        forked_from=None,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=None,
        visibility="org",
        account_id=None,
        tier="native",
        status="draft",
        manifest_pins=[],
    )


async def test_foreign_org_draft_collection_is_not_loadable(session: AsyncSession) -> None:
    foreign = _draft_collection(_ORG_B)
    session.add(foreign)
    await session.flush()

    with patch("farna.api.routes.library.set_rls_org", new=AsyncMock()), pytest.raises(HTTPException) as exc:
        await _load_draft_collection(session, _ORG_A, foreign.id)

    assert exc.value.status_code == 404


async def test_in_org_draft_collection_is_loadable(session: AsyncSession) -> None:
    own = _draft_collection(_ORG_A)
    session.add(own)
    await session.flush()

    with patch("farna.api.routes.library.set_rls_org", new=AsyncMock()):
        loaded = await _load_draft_collection(session, _ORG_A, own.id)

    assert loaded.id == own.id
