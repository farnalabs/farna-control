"""FAR-2686: every org-scoped list helper must clamp to the caller's org.

FAR-2684 closed this class on ``GET /api/v1/templates``; its sweep found the
same shape on seven more list routes whose CRUD helpers built a SELECT with no
explicit ``organisation_id`` predicate and relied entirely on PostgreSQL
row-level security. A ``BYPASSRLS`` connection (or a misconfigured/absent RLS
policy) would then list every org's rows.

Each case below runs against a plain SQLite engine that has neither Postgres
RLS nor an active tenant-filter listener (no ``set_rls_org`` call), so it is an
explicit RLS-bypass path: the ONLY thing that can narrow the result is the
predicate the helper itself builds. The two-org fixture seeds one row per org;
without the predicate the behavioural assertion (caller sees only its own row)
fails, and the statement-level assertion (both the COUNT and the item SELECT
carry ``organisation_id``) fails with it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.db.crud.agent import list_agents
from farna.db.crud.cost_component import list_cost_components
from farna.db.crud.environment_profile import list_environment_profiles
from farna.db.crud.pipeline_folder import list_folders as list_pipeline_folders
from farna.db.crud.rating import list_ratings_for_primitive
from farna.db.crud.schema import list_schemas
from farna.db.crud.schema_folder import list_folders as list_schema_folders
from farna.db.crud.variant_group import list_variant_groups
from farna.db.models.agent import Agent
from farna.db.models.base import Base
from farna.db.models.cost_component import CostComponent
from farna.db.models.environment_profile import EnvironmentProfile
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_folder import PipelineFolder
from farna.db.models.primitive_rating import PrimitiveRating
from farna.db.models.schema import Schema, SchemaFolder
from farna.db.models.variant_group import VariantGroup

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a9")

_CLAMPED_TABLES = [
    Agent.__table__,
    CostComponent.__table__,
    EnvironmentProfile.__table__,
    Pipeline.__table__,
    PipelineFolder.__table__,
    SchemaFolder.__table__,
    Schema.__table__,
    VariantGroup.__table__,
    PrimitiveRating.__table__,
]


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


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


def _agent(org_id: uuid.UUID, name: str) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=name,
        prompt_template="Do the thing.",
        account_id=_ACCOUNT,
    )


async def test_list_agents_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _agent(_ORG_A, "Org A Agent")
    session.add_all([own, _agent(_ORG_B, "Org B Agent")])
    await session.flush()

    page = await list_agents(session, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_agents_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    session.add_all([_agent(_ORG_A, "Org A Agent"), _agent(_ORG_B, "Org B Agent")])
    await session.flush()

    async with _recording(session) as captured:
        await list_agents(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# cost components
# ---------------------------------------------------------------------------


def _cost_component(org_id: uuid.UUID, name: str) -> CostComponent:
    return CostComponent(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=name,
        display_name=name.upper(),
        kind="calculated",
    )


async def test_list_cost_components_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _cost_component(_ORG_A, "org-a-component")
    session.add_all([own, _cost_component(_ORG_B, "org-b-component")])
    await session.flush()

    rows = await list_cost_components(session, org_id=_ORG_A)

    assert [row.id for row in rows] == [own.id]


async def test_list_cost_components_binds_the_org_predicate(session: AsyncSession) -> None:
    session.add_all([_cost_component(_ORG_A, "org-a-component"), _cost_component(_ORG_B, "org-b-component")])
    await session.flush()

    async with _recording(session) as captured:
        await list_cost_components(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


# ---------------------------------------------------------------------------
# environment profiles
# ---------------------------------------------------------------------------


def _environment_profile(org_id: uuid.UUID, name: str) -> EnvironmentProfile:
    return EnvironmentProfile(
        id=uuid.uuid4(),
        organisation_id=org_id,
        name=name,
        provider_type="local_docker",
        account_id=_ACCOUNT,
    )


async def test_list_environment_profiles_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _environment_profile(_ORG_A, "org-a-profile")
    session.add_all([own, _environment_profile(_ORG_B, "org-b-profile")])
    await session.flush()

    page = await list_environment_profiles(session, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_environment_profiles_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    session.add_all([_environment_profile(_ORG_A, "org-a-profile"), _environment_profile(_ORG_B, "org-b-profile")])
    await session.flush()

    async with _recording(session) as captured:
        await list_environment_profiles(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# pipeline folders
# ---------------------------------------------------------------------------


def _pipeline_folder(org_id: uuid.UUID, name: str) -> PipelineFolder:
    return PipelineFolder(id=uuid.uuid4(), organisation_id=org_id, name=name, account_id=_ACCOUNT)


async def test_list_pipeline_folders_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _pipeline_folder(_ORG_A, "Org A Folder")
    session.add_all([own, _pipeline_folder(_ORG_B, "Org B Folder")])
    await session.flush()

    folders = await list_pipeline_folders(session, org_id=_ORG_A)

    assert [folder.id for folder in folders] == [own.id]


async def test_list_pipeline_folders_binds_the_org_predicate(session: AsyncSession) -> None:
    session.add_all([_pipeline_folder(_ORG_A, "Org A Folder"), _pipeline_folder(_ORG_B, "Org B Folder")])
    await session.flush()

    async with _recording(session) as captured:
        await list_pipeline_folders(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


# ---------------------------------------------------------------------------
# schema folders
# ---------------------------------------------------------------------------


def _schema_folder(org_id: uuid.UUID, name: str) -> SchemaFolder:
    return SchemaFolder(id=uuid.uuid4(), organisation_id=org_id, name=name, account_id=_ACCOUNT)


async def test_list_schema_folders_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _schema_folder(_ORG_A, "Org A Folder")
    session.add_all([own, _schema_folder(_ORG_B, "Org B Folder")])
    await session.flush()

    folders = await list_schema_folders(session, org_id=_ORG_A)

    assert [folder.id for folder in folders] == [own.id]


async def test_list_schema_folders_binds_the_org_predicate(session: AsyncSession) -> None:
    session.add_all([_schema_folder(_ORG_A, "Org A Folder"), _schema_folder(_ORG_B, "Org B Folder")])
    await session.flush()

    async with _recording(session) as captured:
        await list_schema_folders(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------


def _schema_row(org_id: uuid.UUID, name: str) -> Schema:
    return Schema(id=uuid.uuid4(), organisation_id=org_id, name=name, account_id=_ACCOUNT)


async def test_list_schemas_returns_only_the_callers_org(session: AsyncSession) -> None:
    own = _schema_row(_ORG_A, "org-a-schema")
    session.add_all([own, _schema_row(_ORG_B, "org-b-schema")])
    await session.flush()

    page = await list_schemas(session, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_schemas_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    session.add_all([_schema_row(_ORG_A, "org-a-schema"), _schema_row(_ORG_B, "org-b-schema")])
    await session.flush()

    async with _recording(session) as captured:
        await list_schemas(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# variant groups (inner join to Pipeline for soft delete only)
# ---------------------------------------------------------------------------


def _pipeline(org_id: uuid.UUID, name: str) -> Pipeline:
    return Pipeline(id=uuid.uuid4(), organisation_id=org_id, name=name, account_id=_ACCOUNT)


def _variant_group(org_id: uuid.UUID, pipeline_id: uuid.UUID, name: str) -> VariantGroup:
    return VariantGroup(id=uuid.uuid4(), organisation_id=org_id, pipeline_id=pipeline_id, name=name, variants=[])


async def test_list_variant_groups_returns_only_the_callers_org(session: AsyncSession) -> None:
    pipe_a = _pipeline(_ORG_A, "Org A Pipeline")
    pipe_b = _pipeline(_ORG_B, "Org B Pipeline")
    own = _variant_group(_ORG_A, pipe_a.id, "Org A Group")
    session.add_all([pipe_a, pipe_b, own, _variant_group(_ORG_B, pipe_b.id, "Org B Group")])
    await session.flush()

    items, total = await list_variant_groups(session, org_id=_ORG_A)

    assert total == 1
    assert [item.id for item in items] == [own.id]


async def test_list_variant_groups_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    pipe_a = _pipeline(_ORG_A, "Org A Pipeline")
    pipe_b = _pipeline(_ORG_B, "Org B Pipeline")
    session.add_all(
        [
            pipe_a,
            pipe_b,
            _variant_group(_ORG_A, pipe_a.id, "Org A Group"),
            _variant_group(_ORG_B, pipe_b.id, "Org B Group"),
        ]
    )
    await session.flush()

    async with _recording(session) as captured:
        await list_variant_groups(session, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# primitive ratings (parent primitive id is not an org boundary)


def _rating(org_id: uuid.UUID, primitive_id: uuid.UUID) -> PrimitiveRating:
    return PrimitiveRating(id=uuid.uuid4(), organisation_id=org_id, primitive_id=primitive_id, thumbs_up=True)


async def test_list_ratings_returns_only_the_callers_org(session: AsyncSession) -> None:
    # Same primitive id in both orgs: the parent id alone cannot separate them.
    primitive_id = uuid.uuid4()
    own = _rating(_ORG_A, primitive_id)
    session.add_all([own, _rating(_ORG_B, primitive_id)])
    await session.flush()

    page = await list_ratings_for_primitive(session, primitive_id, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_ratings_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    primitive_id = uuid.uuid4()
    session.add_all([_rating(_ORG_A, primitive_id), _rating(_ORG_B, primitive_id)])
    await session.flush()

    async with _recording(session) as captured:
        await list_ratings_for_primitive(session, primitive_id, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)
