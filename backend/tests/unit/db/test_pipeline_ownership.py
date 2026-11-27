"""Ownership stamping for the pipeline family (ADR 070 Phase 1A).

The stamp has two halves and both are exercised here against a real SQLite
session (no Docker needed):

* the model-level ``before_insert`` guard, which stamps ``owner_team_id`` from the
  parent pipeline for EVERY ORM insert - including the writers this change does
  not edit (the MCP trigger builder) and any future writer that forgets. The
  insert-without-an-owner tests below are what fail if that listener is removed;
* :func:`resolve_pipeline_owner_team_id`, the explicit path used by writers that
  need the value in Python.

The rule under test is the pipeline-family rule, not the runs-family one: the
value is the pipeline's CURRENT owner, so an org-level pipeline leaves the child
unowned and an explicitly stamped row is never overwritten.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.db.models.base import Base
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_edge import PipelineEdge
from farna.db.models.trigger import Trigger
from farna.db.pipeline_ownership import resolve_pipeline_owner_team_id

_TABLE_NAMES = {
    "organisations",
    "accounts",
    "teams",
    "pipelines",
    "triggers",
    "pipeline_edges",
}


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        tables = [t for t in Base.metadata.sorted_tables if t.name in _TABLE_NAMES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables))
        # The guard reads ``pipelines`` on the INSERT's connection; FK enforcement
        # is irrelevant to it, and off keeps the fixture to the tables under test.
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()


async def _pipeline(session: AsyncSession, owner_team_id: uuid.UUID | None) -> Pipeline:
    pipeline = Pipeline(
        organisation_id=uuid.uuid4(),
        name=f"pipeline-{uuid.uuid4().hex[:8]}",
        account_id=uuid.uuid4(),
        visibility="org" if owner_team_id is None else "team",
        owner_team_id=owner_team_id,
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


def _trigger(session: AsyncSession, pipeline: Pipeline, owner_team_id: uuid.UUID | None = None) -> Trigger:
    trigger = Trigger(
        organisation_id=pipeline.organisation_id,
        pipeline_id=pipeline.id,
        name=f"trigger-{uuid.uuid4().hex[:8]}",
        trigger_type="manual",
        account_id=uuid.uuid4(),
        owner_team_id=owner_team_id,
    )
    session.add(trigger)
    return trigger


def _edge(session: AsyncSession, pipeline: Pipeline, owner_team_id: uuid.UUID | None = None) -> PipelineEdge:
    edge = PipelineEdge(
        organisation_id=pipeline.organisation_id,
        pipeline_id=pipeline.id,
        source_node_id=uuid.uuid4(),
        target_node_id=uuid.uuid4(),
        owner_team_id=owner_team_id,
    )
    session.add(edge)
    return edge


async def test_trigger_insert_without_an_owner_inherits_the_pipeline_team(session: AsyncSession) -> None:
    team = uuid.uuid4()
    pipeline = await _pipeline(session, team)
    trigger = _trigger(session, pipeline)
    await session.flush()
    assert trigger.owner_team_id == team


async def test_edge_insert_without_an_owner_inherits_the_pipeline_team(session: AsyncSession) -> None:
    team = uuid.uuid4()
    pipeline = await _pipeline(session, team)
    edge = _edge(session, pipeline)
    await session.flush()
    assert edge.owner_team_id == team


async def test_an_org_level_pipeline_leaves_its_children_unowned(session: AsyncSession) -> None:
    """NULL owner is a legitimate value, not a stamping failure."""
    pipeline = await _pipeline(session, None)
    trigger = _trigger(session, pipeline)
    edge = _edge(session, pipeline)
    await session.flush()
    assert trigger.owner_team_id is None
    assert edge.owner_team_id is None


async def test_an_explicitly_stamped_child_is_never_overwritten(session: AsyncSession) -> None:
    """The writers that hold the pipeline in memory set the value themselves; the
    guard must respect that rather than issuing a second read."""
    pipeline = await _pipeline(session, uuid.uuid4())
    explicit = uuid.uuid4()
    trigger = _trigger(session, pipeline, owner_team_id=explicit)
    edge = _edge(session, pipeline, owner_team_id=explicit)
    await session.flush()
    assert trigger.owner_team_id == explicit
    assert edge.owner_team_id == explicit


async def test_resolve_pipeline_owner_team_id_reads_the_current_owner(session: AsyncSession) -> None:
    team = uuid.uuid4()
    team_pipeline = await _pipeline(session, team)
    org_pipeline = await _pipeline(session, None)

    assert await resolve_pipeline_owner_team_id(session, team_pipeline.id) == team
    assert await resolve_pipeline_owner_team_id(session, org_pipeline.id) is None
    assert await resolve_pipeline_owner_team_id(session, uuid.uuid4()) is None
