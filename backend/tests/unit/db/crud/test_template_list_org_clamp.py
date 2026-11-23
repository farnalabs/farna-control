"""FAR-2684: the template list must clamp to the caller's org in the query.

Every other list route carries an explicit ``organisation_id ==`` predicate;
``GET /api/v1/templates`` previously relied on row-level security alone, so a
``BYPASSRLS`` connection (or a misconfigured RLS policy) would list every org's
templates.

These tests run against a plain SQLite engine that has neither Postgres RLS nor
the SQLite tenant-filter listener active (no ``set_rls_org`` call), so they are
an explicit RLS-bypass path: the only thing that can narrow the result is the
predicate ``list_templates`` itself builds. Without that predicate the
two-org fixture returns both orgs' rows and the behavioural test fails.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.db.crud.template import list_templates
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[LibraryPrimitive.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _template(*, org_id: uuid.UUID, name: str) -> LibraryPrimitive:
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=org_id,
        source="local",
        primitive_type="pipeline_template",
        name=name,
        slug=name.lower().replace(" ", "-"),
        author="tester",
        version="1.0.0",
        content_json={},
    )


async def _seed_two_orgs(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    org_a_template = _template(org_id=_ORG_A, name="Org A Template")
    org_b_template = _template(org_id=_ORG_B, name="Org B Template")
    session.add_all([org_a_template, org_b_template])
    await session.flush()
    return org_a_template.id, org_b_template.id


@pytest.mark.anyio
async def test_list_templates_returns_only_the_callers_org_on_a_bypass_path(session: AsyncSession) -> None:
    org_a_template_id, org_b_template_id = await _seed_two_orgs(session)

    page = await list_templates(session, org_id=_ORG_A)

    assert page.total == 1
    assert len(page.items) == 1
    assert page.items[0].id == org_a_template_id
    assert page.items[0].organisation_id == _ORG_A
    assert all(item.id != org_b_template_id for item in page.items)


@pytest.mark.anyio
async def test_list_templates_binds_the_caller_org_into_both_statements(session: AsyncSession) -> None:
    await _seed_two_orgs(session)

    captured: list[str] = []
    real_execute = session.execute

    async def recording_execute(statement: object, *args: object, **kwargs: object) -> object:
        if isinstance(statement, Select):
            captured.append(str(statement))
        return await real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    session.execute = recording_execute  # type: ignore[method-assign]
    try:
        await list_templates(session, org_id=_ORG_A, page=1, page_size=20)
    finally:
        session.execute = real_execute  # type: ignore[method-assign]

    # Both the COUNT and the item page must carry the org predicate; a
    # predicate on only one would let ``total`` and the rows disagree.
    assert len(captured) == 2
    for sql in captured:
        assert "organisation_id" in sql
