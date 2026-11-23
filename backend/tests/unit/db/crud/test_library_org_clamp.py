"""FAR-2692: the library reads must clamp to the caller's org.

FAR-2684/FAR-2686/FAR-2689 removed the RLS-only isolation from the other list
helpers. Three instances remained in the library layer: both contribution
listers held ``org_id`` and never passed it, ``list_primitives_by_version_group``
was parent-id only, and ``list_library_primitives`` accepted the org argument as
optional.

Every case below runs against a plain SQLite engine that has neither Postgres
RLS nor an active tenant-filter listener (no ``set_rls_org`` call), so it is an
explicit RLS-bypass path: the ONLY thing that can narrow the result is the
predicate the helper itself builds. Without the predicate the behavioural
assertion fails, and the statement-level assertion fails with it.

Built-in visibility is pinned in both directions. Native built-ins
(``source='modulo'``) reach a customer from the in-memory registry merge in
``core.library_service.list_primitives`` — they are NOT DB rows in the caller's
slice — so a plain ``organisation_id =`` clamp cannot hide them. The RLS policy
on ``library_primitives`` already restricts rows to
``organisation_id = current_setting('app.organisation_id')``, so a sentinel-org
(``MODULO_REGISTRY_ORG_ID``) row is invisible today and must stay invisible.
These tests fail if a future clamp hides the built-ins, and they fail if the
clamp is weakened enough to expose the sentinel partition.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.core.library_service import (
    _MODULO_PRIMITIVES,
    list_contribution_versions,
    list_contributions,
    list_org_contributions,
    list_primitives,
)
from farna.core.library_service._seed_data import MODULO_ORG_ID
from farna.db.crud.base import PageResult
from farna.db.crud.library_primitive import list_library_primitives, list_primitives_by_version_group
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive

_SERVICE = "farna.core.library_service"

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000e2")

_CLAMPED_TABLES = [LibraryPrimitive.__table__]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_CLAMPED_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@asynccontextmanager
async def _recording(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Record every SELECT statement text the helper issues."""
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


def _assert_scoped(statement_texts: list[str], expected: int) -> None:
    assert len(statement_texts) == expected
    for sql in statement_texts:
        # ``organisation_id`` alone is NOT a discriminator: an entity SELECT
        # projects the column, so it appears even with no predicate. Require the
        # WHERE-clause comparison itself, which only a bound predicate renders.
        assert "organisation_id = :" in sql


def _primitive(
    org_id: uuid.UUID,
    name: str,
    *,
    source: str = "local",
    slug: str | None = None,
    version_group_id: uuid.UUID | None = None,
    primitive_type: str = "schema",
    version: str = "1.0",
) -> LibraryPrimitive:
    row_slug = slug or name.lower().replace(" ", "-")
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=org_id,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=row_slug,
        author="tester",
        version=version,
        tags=[],
        content_json={},
        # ``ck_library_primitives_source_fields`` requires every registry-style
        # field to be NULL for local and modulo sources.
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
        version_group_id=version_group_id,
    )


# ---------------------------------------------------------------------------
# list_library_primitives
# ---------------------------------------------------------------------------


async def test_list_library_primitives_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _primitive(_ORG_A, "Org A Schema")
    session.add_all([own, _primitive(_ORG_B, "Org B Schema")])
    await session.flush()

    page = await list_library_primitives(session, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_library_primitives_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    session.add_all([_primitive(_ORG_A, "Org A Schema"), _primitive(_ORG_B, "Org B Schema")])
    await session.flush()

    async with _recording(session) as captured:
        await list_library_primitives(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


async def test_list_library_primitives_hides_the_sentinel_org_partition(session: AsyncSession) -> None:
    # A registry shadow row materialised under the built-in sentinel org. RLS
    # hides this partition from a tenant session; the explicit clamp must too,
    # so a BYPASSRLS connection cannot read another org's built-in shadow rows.
    sentinel = _primitive(MODULO_ORG_ID, "Sentinel Shadow", source="modulo", slug="sentinel-shadow")
    own = _primitive(_ORG_A, "Org A Schema")
    session.add_all([own, sentinel])
    await session.flush()

    page = await list_library_primitives(session, org_id=_ORG_A)

    assert [item.id for item in page.items] == [own.id]
    assert sentinel.id not in [item.id for item in page.items]


async def test_list_library_primitives_requires_an_org(session: AsyncSession) -> None:
    # The argument was optional; an optional argument lets a future caller fall
    # back to an RLS-only read. Omitting it must now fail loudly.
    with pytest.raises(TypeError):
        await list_library_primitives(session)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# built-in visibility (the clamp must not hide the Native library)
# ---------------------------------------------------------------------------


async def test_list_primitives_still_serves_the_in_memory_builtins(session: AsyncSession) -> None:
    # Built-ins are not rows in the caller's slice: they are merged in from the
    # in-code registry by ``list_primitives``. The clamp must therefore leave
    # them visible while hiding the sentinel-org shadow row.
    sentinel = _primitive(MODULO_ORG_ID, "Sentinel Shadow", source="modulo", slug="sentinel-shadow")
    own = _primitive(_ORG_A, "Org A Schema")
    session.add_all([own, sentinel])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_primitives(session, _ORG_A)

    item_ids = {item.id for item in result.items}
    # The org's own row came back from the clamped DB query (a degraded,
    # exception-swallowed query would drop it and fail here).
    assert own.id in item_ids
    # Every Native built-in is still served to the customer.
    assert {p.id for p in _MODULO_PRIMITIVES} <= item_ids
    # The sentinel partition stays invisible.
    assert sentinel.id not in item_ids
    assert result.total == 1 + len(_MODULO_PRIMITIVES)


# ---------------------------------------------------------------------------
# list_primitives_by_version_group
# ---------------------------------------------------------------------------


async def test_list_primitives_by_version_group_returns_only_the_callers_org(session: AsyncSession) -> None:
    # Same version group id in both orgs: the parent id alone cannot separate
    # them.
    group_id = uuid.uuid4()
    own = _primitive(_ORG_A, "Org A V1", version_group_id=group_id)
    session.add_all([own, _primitive(_ORG_B, "Org B V1", version_group_id=group_id)])
    await session.flush()

    rows = await list_primitives_by_version_group(session, group_id, org_id=_ORG_A)

    assert [row.id for row in rows] == [own.id]


async def test_list_primitives_by_version_group_binds_the_org_predicate(session: AsyncSession) -> None:
    group_id = uuid.uuid4()
    session.add_all(
        [
            _primitive(_ORG_A, "Org A V1", version_group_id=group_id),
            _primitive(_ORG_B, "Org B V1", version_group_id=group_id),
        ]
    )
    await session.flush()

    async with _recording(session) as captured:
        await list_primitives_by_version_group(session, group_id, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


async def test_list_primitives_by_version_group_requires_an_org(session: AsyncSession) -> None:
    with pytest.raises(TypeError):
        await list_primitives_by_version_group(session, uuid.uuid4())  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# call sites: the service helpers must PASS the org they already hold
# ---------------------------------------------------------------------------


def _page(items: list[LibraryPrimitive] | None = None) -> PageResult[LibraryPrimitive]:
    return PageResult(items=items or [], total=len(items or []), page=1, page_size=20)


def _service_session() -> MagicMock:
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=True)
    return session


async def test_list_contributions_passes_the_callers_org() -> None:
    org_id = uuid.uuid4()
    lister = AsyncMock(return_value=_page())

    with (
        patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()),
        patch(f"{_SERVICE}.list_library_primitives", new=lister),
    ):
        await list_contributions(_service_session(), org_id)

    assert lister.await_args.kwargs["org_id"] == org_id
    assert lister.await_args.kwargs["primitive_type"] == "test_fixture"


async def test_list_org_contributions_passes_the_callers_org() -> None:
    org_id = uuid.uuid4()
    lister = AsyncMock(return_value=_page())

    with (
        patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()),
        patch(f"{_SERVICE}.list_library_primitives", new=lister),
    ):
        await list_org_contributions(_service_session(), org_id)

    assert lister.await_args.kwargs["org_id"] == org_id


async def test_list_contribution_versions_passes_the_callers_org() -> None:
    org_id = uuid.uuid4()
    group_id = uuid.uuid4()
    prim = _primitive(_ORG_A, "Contribution", primitive_type="test_fixture", version_group_id=group_id)
    resolver = AsyncMock(return_value=prim)
    version_lister = AsyncMock(return_value=[prim])

    with (
        patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()),
        patch(f"{_SERVICE}.get_primitive", new=resolver),
        patch(f"{_SERVICE}.list_primitives_by_version_group", new=version_lister),
    ):
        await list_contribution_versions(_service_session(), org_id, prim.id)

    assert resolver.await_args.kwargs["explicit_org_filter"] == org_id
    assert resolver.await_args.args[1] == org_id
    assert version_lister.await_args.kwargs["org_id"] == org_id
    assert version_lister.await_args.args[1] == group_id


# ---------------------------------------------------------------------------
# call sites: behaviour through the real helpers on an RLS-bypass session
# ---------------------------------------------------------------------------


async def test_list_contributions_does_not_leak_another_orgs_fixtures(session: AsyncSession) -> None:
    own = _primitive(_ORG_A, "Org A Fixture", primitive_type="test_fixture")
    session.add_all([own, _primitive(_ORG_B, "Org B Fixture", primitive_type="test_fixture")])
    await session.flush()

    # ``set_rls_org`` is stubbed out: this is a BYPASSRLS session, so only the
    # helper's own predicate can narrow the read.
    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_contributions(session, _ORG_A)

    assert [item.id for item in result.items] == [own.id]


async def test_list_org_contributions_does_not_leak_another_orgs_rows(session: AsyncSession) -> None:
    own = _primitive(_ORG_A, "Org A Row", primitive_type="test_fixture")
    session.add_all([own, _primitive(_ORG_B, "Org B Row", primitive_type="test_fixture")])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_org_contributions(session, _ORG_A)

    assert [item.id for item in result.items] == [own.id]


async def test_list_contribution_versions_does_not_leak_another_orgs_versions(session: AsyncSession) -> None:
    group_id = uuid.uuid4()
    seed = _primitive(_ORG_A, "Org A V1", primitive_type="test_fixture", version_group_id=group_id)
    v2 = _primitive(_ORG_A, "Org A V2", primitive_type="test_fixture", version_group_id=group_id, version="1.1")
    foreign = _primitive(_ORG_B, "Org B V1", primitive_type="test_fixture", version_group_id=group_id)
    session.add_all([seed, v2, foreign])
    await session.flush()

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_contribution_versions(session, _ORG_A, seed.id)

    assert {item.id for item in result} == {seed.id, v2.id}
    assert foreign.id not in {item.id for item in result}
