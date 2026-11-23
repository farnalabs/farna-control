"""FAR-2696: the update fan-out and the contribution-version seed clamp to the org.

What this pins
--------------

``_mark_update_recipients`` (behind ``notify_importers_of_update``) SELECTS every
``forked_from`` row and every installed-collection row in a version group and
WRITES ``update_available_version_id`` on each. Before FAR-2696 none of those
three reads — nor the write — carried an ``organisation_id`` predicate, so the
code relied for isolation on PostgreSQL row-level security *and* on the
same-organisation triggers on ``forked_from`` / ``version_group_id`` /
``update_available_version_id``.

``list_contribution_versions`` resolved its seed with the id-only
``get_library_primitive``; it now goes through
``get_primitive(..., explicit_org_filter=org_id)``.

Why SQLite here
---------------

Every case runs against a plain in-memory SQLite engine with
``set_rls_org`` patched to a no-op. That engine has no PostgreSQL RLS, no
tenant-filter listener (nothing bound ``session.info``) and — because
``create_all`` does not create triggers — neither of the same-organisation
triggers. It is therefore the concrete environment the code was silently
relying on and the one where a missing predicate bites: the ONLY thing that can
narrow the read/write is the predicate the code builds itself. On a fully
triggered Postgres the cross-organisation *shape* would raise 23503 instead of
being silently skipped, which is why the fix is defence in depth rather than a
live data-loss bug — but a restored dump, a replication apply session or the
test backend has no such trigger.

Prove-the-fix: strip the ``organisation_id`` predicates from
``_mark_update_recipients`` and from the seed resolver and the foreign-row
assertions below fail (the foreign fork / installed row is stamped, and the
foreign contribution seed is returned).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.core.library_service import (
    ContributionNotFoundError,
    list_contribution_versions,
    notify_importers_of_update,
)
from farna.db.crud.library_primitive import mark_update_available
from farna.db.models.base import Base
from farna.db.models.collection_install import CollectionInstall
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.soft_delete import register_soft_delete_filter

# This module asserts that a SOFT-DELETED recipient is never stamped. The
# global soft-delete filter is installed by an import side effect, so register
# it explicitly here — otherwise the assertion would be evaluated with the
# opposite semantics depending on which imports happened to run first.
register_soft_delete_filter()

_SERVICE = "farna.core.library_service"

_ORG_A = uuid.UUID("00000000-0000-0000-0000-00000000e601")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-00000000e602")

_TABLES = [LibraryPrimitive.__table__, CollectionInstall.__table__]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@asynccontextmanager
async def _recording(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Record every SELECT statement text the fan-out issues."""
    captured: list[str] = []
    real_execute = session.execute

    async def recording_execute(statement: object, *args: object, **kwargs: object) -> Any:
        if isinstance(statement, Select):
            captured.append(str(statement))
        return await real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    session.execute = recording_execute  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        session.execute = real_execute  # type: ignore[method-assign]


def _primitive(
    org_id: uuid.UUID,
    name: str,
    *,
    source: str = "local",
    primitive_type: str = "schema",
    version_group_id: uuid.UUID | None = None,
    forked_from: uuid.UUID | None = None,
    auto_update: bool = True,
) -> LibraryPrimitive:
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=org_id,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=name.lower().replace(" ", "-"),
        author="tester",
        version="1.0",
        tags=[],
        content_json={},
        # ``ck_library_primitives_source_fields`` requires every registry-style
        # field to be NULL for the local source.
        source_url=None,
        forked_from=forked_from,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=None,
        visibility="org",
        account_id=None,
        auto_update=auto_update,
        version_group_id=version_group_id,
        tier="native",
    )


def _install(org_id: uuid.UUID, collection_id: uuid.UUID) -> CollectionInstall:
    return CollectionInstall(
        install_id=uuid.uuid4(),
        collection_id=collection_id,
        collection_version="1.0",
        organisation_id=org_id,
        status="installed",
        registry_sourced=False,
        agents_granted=False,
    )


async def _stamp_of(session: AsyncSession, row_id: uuid.UUID) -> uuid.UUID | None:
    """Read the stamp, treating an invisible (soft-deleted) row as unstamped.

    ``scalar_one_or_none`` (not ``scalar_one``) so the registered soft-delete
    filter hiding the deleted row reads as ``None`` rather than raising.
    """
    result = await session.execute(select(LibraryPrimitive.update_available_version_id).where(LibraryPrimitive.id == row_id))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# the fan-out: forks
# ---------------------------------------------------------------------------


async def test_in_org_fork_is_stamped(session: AsyncSession) -> None:
    group_id = uuid.uuid4()
    published = _primitive(_ORG_A, "Org A V1", version_group_id=group_id)
    fork = _primitive(_ORG_A, "Org A Fork", forked_from=published.id)
    session.add_all([published, fork])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        await notify_importers_of_update(session, _ORG_A, published.id)

    assert await _stamp_of(session, fork.id) == published.id


async def test_foreign_org_fork_is_not_stamped(session: AsyncSession) -> None:
    """A fork in another org must never be stamped (RLS-bypass session)."""
    group_id = uuid.uuid4()
    published = _primitive(_ORG_A, "Org A V1", version_group_id=group_id)
    own_fork = _primitive(_ORG_A, "Org A Fork", forked_from=published.id)
    foreign_fork = _primitive(_ORG_B, "Org B Fork", forked_from=published.id)
    session.add_all([published, own_fork, foreign_fork])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        await notify_importers_of_update(session, _ORG_A, published.id)

    assert await _stamp_of(session, own_fork.id) == published.id
    assert await _stamp_of(session, foreign_fork.id) is None


async def test_orphaned_cross_org_fork_is_not_stamped(session: AsyncSession) -> None:
    """A caller-org fork whose ``forked_from`` points at a FOREIGN group member.

    The outer org clamp alone would stamp it (the row is in-org); the clamped
    version-group subquery is what excludes the foreign group member, so this
    pins the inner clamp specifically.
    """
    group_id = uuid.uuid4()
    foreign_member = _primitive(_ORG_B, "Org B V1", version_group_id=group_id)
    orphan = _primitive(_ORG_A, "Org A Orphan", forked_from=foreign_member.id)
    session.add_all([foreign_member, orphan])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        await notify_importers_of_update(session, _ORG_A, foreign_member.id)

    assert await _stamp_of(session, orphan.id) is None


# ---------------------------------------------------------------------------
# the fan-out: installed collection rows
# ---------------------------------------------------------------------------


async def test_in_org_installed_collection_is_stamped(session: AsyncSession) -> None:
    group_id = uuid.uuid4()
    published = _primitive(_ORG_A, "Org A Collection V1", version_group_id=group_id)
    installed = _primitive(_ORG_A, "Org A Installed Collection", version_group_id=group_id)
    session.add_all([published, installed, _install(_ORG_A, installed.id)])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        await notify_importers_of_update(session, _ORG_A, published.id)

    assert await _stamp_of(session, installed.id) == published.id


async def test_foreign_org_installed_collection_is_not_stamped(session: AsyncSession) -> None:
    """An installed collection row in another org must never be stamped."""
    group_id = uuid.uuid4()
    published = _primitive(_ORG_A, "Org A Collection V1", version_group_id=group_id)
    foreign_installed = _primitive(_ORG_B, "Org B Installed Collection", version_group_id=group_id)
    session.add_all([published, foreign_installed, _install(_ORG_B, foreign_installed.id)])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        await notify_importers_of_update(session, _ORG_A, published.id)

    assert await _stamp_of(session, foreign_installed.id) is None


# ---------------------------------------------------------------------------
# the fan-out: both recipient SELECTs carry the org predicate
# ---------------------------------------------------------------------------


async def test_fanout_selects_carry_the_org_predicate(session: AsyncSession) -> None:
    group_id = uuid.uuid4()
    published = _primitive(_ORG_A, "Org A V1", version_group_id=group_id)
    session.add(published)
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        async with _recording(session) as captured:
            await notify_importers_of_update(session, _ORG_A, published.id)

    # Three SELECTs: the seed point read (id-only by design) plus the two
    # recipient SELECTs. ``organisation_id`` is projected by every entity
    # SELECT, so require the bound comparison itself; each recipient SELECT
    # must carry it on BOTH its outer row and its correlated subquery.
    scoped = [sql for sql in captured if "organisation_id = :" in sql]
    fork_sql = next(sql for sql in scoped if "forked_from" in sql)
    installed_sql = next(sql for sql in scoped if "collection_install" in sql)
    assert fork_sql.count("organisation_id = :") == 2
    assert installed_sql.count("organisation_id = :") == 2


# ---------------------------------------------------------------------------
# the clamped write helper in isolation
# ---------------------------------------------------------------------------


async def test_mark_update_available_stamps_only_a_live_in_org_row(session: AsyncSession) -> None:
    org_row = _primitive(_ORG_A, "Org A Row")
    foreign_row = _primitive(_ORG_B, "Org B Row")
    deleted_row = _primitive(_ORG_A, "Org A Deleted")
    deleted_row.deleted_at = datetime(2024, 1, 1, tzinfo=UTC)
    version = uuid.uuid4()
    session.add_all([org_row, foreign_row, deleted_row])
    await session.flush()

    stamped = await mark_update_available(session, org_id=_ORG_A, recipient_id=org_row.id, version_id=version)
    refused_foreign = await mark_update_available(session, org_id=_ORG_A, recipient_id=foreign_row.id, version_id=version)
    refused_deleted = await mark_update_available(session, org_id=_ORG_A, recipient_id=deleted_row.id, version_id=version)

    assert stamped is not None
    assert await _stamp_of(session, org_row.id) == version
    assert refused_foreign is None
    assert await _stamp_of(session, foreign_row.id) is None
    assert refused_deleted is None
    assert await _stamp_of(session, deleted_row.id) is None


# ---------------------------------------------------------------------------
# the contribution-version seed resolver
# ---------------------------------------------------------------------------


async def test_foreign_org_contribution_seed_is_not_resolved(session: AsyncSession) -> None:
    foreign_seed = _primitive(_ORG_B, "Org B Contribution", primitive_type="test_fixture")
    session.add(foreign_seed)
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()), pytest.raises(ContributionNotFoundError):
        await list_contribution_versions(session, _ORG_A, foreign_seed.id)


async def test_in_org_contribution_seed_is_resolved(session: AsyncSession) -> None:
    own_seed = _primitive(_ORG_A, "Org A Contribution", primitive_type="test_fixture")
    session.add(own_seed)
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        versions = await list_contribution_versions(session, _ORG_A, own_seed.id)

    assert [row.id for row in versions] == [own_seed.id]
