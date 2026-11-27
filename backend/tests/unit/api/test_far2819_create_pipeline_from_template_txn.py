"""FAR-2819 regression: create-pipeline-from-template must not read expired
ORM timestamps after its transaction commits.

The route opens ONE ``async with session.begin()``. On the template path the
handler assigns ``graph_nodes_json`` and flushes, which issues an UPDATE whose
server-side ``onupdate`` expires ``updated_at``. The response builder then runs
AFTER the transaction block; on the app's ``autobegin=False`` session that lazy
load is outside any transaction and raises ``InvalidRequestError`` — mapped by
``handle_db_errors`` to the session-contract HTTP 500.

This test drives the REAL endpoint function against a REAL async session (SQLite,
``autobegin=False`` + ``expire_on_commit=False`` matching the application DI
default) so the expiry-then-read behaviour is reproduced faithfully without
Docker. Only the primitive read and the pipeline CRUD are stubbed; the latter
still performs a genuine insert + flush into the session under test.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from farna.api.routes.library import (
    CreatePipelineFromTemplateRequest,
    create_pipeline_from_template_endpoint,
)
from farna.auth.jwt import TenantPrincipal
from farna.db.models.base import Base
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_edge import PipelineEdge

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")


@dataclass
class _FakePrimitive:
    id: uuid.UUID
    organisation_id: uuid.UUID
    primitive_type: str
    name: str
    description: str | None
    content_json: dict[str, Any]


def _fake_primitive(primitive_id: uuid.UUID) -> _FakePrimitive:
    return _FakePrimitive(
        id=primitive_id,
        organisation_id=_ORG_ID,
        primitive_type="pipeline_template",
        name="PR Description Pipeline",
        description="Template description",
        content_json={
            "agents": [{"name": "Agent A"}],
            "graph_nodes": [{"id": "node1", "node_type": "agent", "position": {"x": 0, "y": 0}}],
            "edges": [],
        },
    )


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[Pipeline.__table__, PipelineEdge.__table__],
            )
        )
    # autobegin=False matches the app DI default: the route's
    # ``async with session.begin():`` owns transaction start.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _real_insert_pipeline(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    account_id: uuid.UUID,
    **options: Any,
) -> Pipeline:
    """Mimic ``db.crud.pipeline.create_pipeline``'s insert + flush.

    The real helper also seeds guardrails and writes audit rows; those are
    irrelevant to the session-contract bug under test, so this stub performs the
    one operation that matters: ``session.add`` + ``flush`` of a genuine
    ``Pipeline`` row into the session the route owns.
    """
    pipeline = Pipeline(
        organisation_id=org_id,
        name=name,
        account_id=account_id,
        description=options.get("description"),
        visibility="org",
        run_context_defaults=options.get("run_context_defaults") or {},
    )
    session.add(pipeline)
    await session.flush()
    return pipeline


async def test_create_from_template_reads_timestamps_inside_the_transaction(
    session: AsyncSession,
) -> None:
    primitive_id = uuid.uuid4()
    principal = TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )

    with (
        patch("farna.api.routes.library.get_primitive", return_value=_fake_primitive(primitive_id)),
        patch("farna.api.routes.library.create_pipeline", side_effect=_real_insert_pipeline),
    ):
        response = await create_pipeline_from_template_endpoint(
            primitive_id,
            CreatePipelineFromTemplateRequest(name="PR Review Pipeline"),
            session=session,
            principal=principal,
        )

    # Without the fix the post-commit lazy load of ``updated_at`` raises
    # InvalidRequestError -> handle_db_errors -> HTTPException 500, so reaching
    # this assertion at all proves the transaction contract holds.
    assert response.template_source_id == primitive_id
    assert response.name == "PR Review Pipeline"
    assert response.ready_to_run is True
    assert response.created_at is not None
    assert response.updated_at is not None
    # The endpoint must leave the session idle (no dangling transaction) and the
    # row must be persisted.
    assert session.in_transaction() is False
