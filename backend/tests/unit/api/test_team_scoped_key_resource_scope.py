"""Team-boundary enforcement for team-scoped API keys on resource reads.

A team-scoped ``mk_`` org API key carries a hard team boundary in
``TenantPrincipal.team_id`` (FAR-681, ADR 070 §5). For the team-scoped resource
tables (pipelines, library_primitives, triggers-via-pipeline, model_backends)
the database policy ``rls_team_isolation`` filters on the KEY OWNER's team
memberships, not on the key's own team. A key scoped to team A whose owner is
also a member of team B therefore still reads team B's team-private rows unless
the surface applies the key's team boundary explicitly.

These cases seed two teams inside ONE organisation and drive the real route
handler against a plain in-memory SQLite engine with ``set_rls_org`` patched to
a no-op — no PostgreSQL RLS and no team filter listener — so the only thing
that can narrow the read is the explicit ``team_id`` predicate the surface
must apply.

Before each fix the team-A key sees team B's row; after it, the row is either
absent from the page or a 404.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.api.routes import library as library_module
from farna.api.routes import model_backends as model_backends_module
from farna.api.routes import pipelines as pipelines_module
from farna.api.routes import triggers as triggers_module
from farna.auth.jwt import TenantPrincipal
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.models.model_backend import ModelBackend
from farna.db.models.pipeline import Pipeline
from farna.db.models.trigger import Trigger

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


def _team_a_key() -> TenantPrincipal:
    """A team-A-scoped API key (the owner is also a team-B member in the DB)."""
    return TenantPrincipal(
        username="team-a-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="runner",
        team_id=_TEAM_A,
        via_api_key=True,
    )


def _org_wide_key() -> TenantPrincipal:
    """An org-wide API key (``team_id=None``) keeps the org-level view."""
    return TenantPrincipal(
        username="org-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="runner",
        via_api_key=True,
    )


async def _sqlite_session(tables: list[object]) -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _pipeline(*, name: str, owner_team_id: uuid.UUID | None, visibility: str = "team") -> Pipeline:
    return Pipeline(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        visibility=visibility,
        owner_team_id=owner_team_id,
        default_autonomy_level="manual_approval",
        account_id=_ACCOUNT_ID,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/pipelines (list)
# ---------------------------------------------------------------------------


@pytest.fixture
async def pipeline_session() -> AsyncGenerator[AsyncSession, None]:
    async for value in _sqlite_session([Pipeline.__table__]):
        yield value


async def test_pipeline_list_hides_another_teams_private_pipeline(
    pipeline_session: AsyncSession,
) -> None:
    """A team-A key must not list a team-B team-private pipeline (MCP parity)."""
    foreign = _pipeline(name="team-b-pipeline", owner_team_id=_TEAM_B)
    own = _pipeline(name="team-a-pipeline", owner_team_id=_TEAM_A)
    pipeline_session.add_all([foreign, own])
    await pipeline_session.commit()

    with patch.object(pipelines_module, "set_rls_org", AsyncMock()), patch.object(pipelines_module, "set_rls_user_context", AsyncMock()):
        response = await pipelines_module.list_pipelines_endpoint(
            session=pipeline_session,
            page=1,
            page_size=20,
            cursor=None,
            include_archived=False,
            folder_id=None,
            principal=_team_a_key(),
        )

    returned = {item.id for item in response.items}
    assert own.id in returned
    assert foreign.id not in returned


async def test_pipeline_list_keeps_org_level_pipelines_for_a_team_key(
    pipeline_session: AsyncSession,
) -> None:
    """Org-level pipelines (no owner team) stay visible to every team key."""
    org_level = _pipeline(name="org-pipeline", owner_team_id=None, visibility="org")
    pipeline_session.add(org_level)
    await pipeline_session.commit()

    with patch.object(pipelines_module, "set_rls_org", AsyncMock()), patch.object(pipelines_module, "set_rls_user_context", AsyncMock()):
        response = await pipelines_module.list_pipelines_endpoint(
            session=pipeline_session,
            page=1,
            page_size=20,
            cursor=None,
            include_archived=False,
            folder_id=None,
            principal=_team_a_key(),
        )

    assert [item.id for item in response.items] == [org_level.id]


async def test_pipeline_list_org_wide_key_still_sees_every_team(
    pipeline_session: AsyncSession,
) -> None:
    """An org-wide key is team-blind and keeps the pre-existing behaviour."""
    foreign = _pipeline(name="team-b-pipeline", owner_team_id=_TEAM_B)
    pipeline_session.add(foreign)
    await pipeline_session.commit()

    with patch.object(pipelines_module, "set_rls_org", AsyncMock()), patch.object(pipelines_module, "set_rls_user_context", AsyncMock()):
        response = await pipelines_module.list_pipelines_endpoint(
            session=pipeline_session,
            page=1,
            page_size=20,
            cursor=None,
            include_archived=False,
            folder_id=None,
            principal=_org_wide_key(),
        )

    assert [item.id for item in response.items] == [foreign.id]


# ---------------------------------------------------------------------------
# GET /api/v1/triggers (list) — team ownership is the pipeline's
# ---------------------------------------------------------------------------


@pytest.fixture
async def trigger_session() -> AsyncGenerator[AsyncSession, None]:
    async for value in _sqlite_session([Pipeline.__table__, Trigger.__table__]):
        yield value


def _trigger(*, pipeline_id: uuid.UUID, name: str) -> Trigger:
    return Trigger(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        pipeline_id=pipeline_id,
        name=name,
        trigger_type="manual",
        account_id=_ACCOUNT_ID,
        config_json={},
    )


async def test_trigger_list_hides_another_teams_pipeline_triggers(
    trigger_session: AsyncSession,
) -> None:
    """A team-A key must not list triggers of a team-B team-private pipeline."""
    foreign_pipeline = _pipeline(name="team-b-pipeline", owner_team_id=_TEAM_B)
    own_pipeline = _pipeline(name="team-a-pipeline", owner_team_id=_TEAM_A)
    trigger_session.add_all([foreign_pipeline, own_pipeline])
    await trigger_session.flush()
    foreign_trigger = _trigger(pipeline_id=foreign_pipeline.id, name="team-b-trigger")
    own_trigger = _trigger(pipeline_id=own_pipeline.id, name="team-a-trigger")
    trigger_session.add_all([foreign_trigger, own_trigger])
    await trigger_session.commit()

    async def _fake_serialize(_session: AsyncSession, row: Trigger) -> dict[str, object]:
        return {"id": str(row.id)}

    with (
        patch.object(triggers_module, "set_rls_org", AsyncMock()),
        patch.object(triggers_module, "set_rls_user_context", AsyncMock()),
        patch.object(triggers_module, "_serialize_trigger", side_effect=_fake_serialize),
        patch.object(triggers_module, "_read_org_pause_state", AsyncMock(return_value=(False, None))),
    ):
        response = await triggers_module.list_triggers(
            pipeline_id=None,
            trigger_type=None,
            page=1,
            page_size=20,
            session=trigger_session,
            principal=_team_a_key(),
        )

    returned = {item["id"] for item in response["items"]}
    assert str(own_trigger.id) in returned
    assert str(foreign_trigger.id) not in returned


# ---------------------------------------------------------------------------
# GET /api/v1/library/{primitive_id} — library_primitives is team-scoped
# ---------------------------------------------------------------------------


@pytest.fixture
async def library_session() -> AsyncGenerator[AsyncSession, None]:
    async for value in _sqlite_session([LibraryPrimitive.__table__]):
        yield value


def _primitive(*, owner_team_id: uuid.UUID | None, name: str) -> LibraryPrimitive:
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        source="local",
        primitive_type="agent",
        name=name,
        slug=f"{name}-{uuid.uuid4().hex[:8]}",
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
        owner_team_id=owner_team_id,
        visibility="team" if owner_team_id is not None else "org",
        account_id=None,
        tier="native",
        status="published",
        manifest_pins=[],
    )


async def test_library_read_hides_another_teams_private_primitive(
    library_session: AsyncSession,
) -> None:
    """A team-A key gets a 404 for a team-B team-private primitive (IDOR)."""
    foreign = _primitive(owner_team_id=_TEAM_B, name="team-b-primitive")
    library_session.add(foreign)
    await library_session.commit()

    with patch.object(library_module, "set_rls_org", AsyncMock()), patch.object(library_module, "set_rls_user_context", AsyncMock()), pytest.raises(HTTPException) as exc_info:
        await library_module.get_library_primitive_endpoint(
            primitive_id=foreign.id,
            session=library_session,
            principal=_team_a_key(),
        )

    assert exc_info.value.status_code == 404


async def test_library_read_keeps_own_teams_primitive(
    library_session: AsyncSession,
) -> None:
    own = _primitive(owner_team_id=_TEAM_A, name="team-a-primitive")
    library_session.add(own)
    await library_session.commit()

    with patch.object(library_module, "set_rls_org", AsyncMock()), patch.object(library_module, "set_rls_user_context", AsyncMock()):
        response = await library_module.get_library_primitive_endpoint(
            primitive_id=own.id,
            session=library_session,
            principal=_team_a_key(),
        )

    assert response.id == own.id


# ---------------------------------------------------------------------------
# GET /api/v1/model-backends (list)
# ---------------------------------------------------------------------------


@pytest.fixture
async def model_backend_session() -> AsyncGenerator[AsyncSession, None]:
    async for value in _sqlite_session([ModelBackend.__table__]):
        yield value


def _backend(*, owner_team_id: uuid.UUID | None, name: str) -> ModelBackend:
    return ModelBackend(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        display_name=name,
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"",
        default_params={},
        owner_team_id=owner_team_id,
        visibility="team" if owner_team_id is not None else "org",
        account_id=_ACCOUNT_ID,
    )


async def test_model_backend_list_hides_another_teams_private_backend(
    model_backend_session: AsyncSession,
) -> None:
    """A team-A key must not list a team-B team-private model backend."""
    foreign = _backend(owner_team_id=_TEAM_B, name="team-b-backend")
    own = _backend(owner_team_id=_TEAM_A, name="team-a-backend")
    model_backend_session.add_all([foreign, own])
    await model_backend_session.commit()

    with (
        patch.object(model_backends_module, "set_rls_org", AsyncMock()),
        patch.object(model_backends_module, "set_rls_user_context", AsyncMock()),
        patch.object(model_backends_module, "_base_urls_for_backends", AsyncMock(return_value={})),
    ):
        response = await model_backends_module.list_model_backends_endpoint(
            page=1,
            page_size=20,
            include_in_dev=False,
            session=model_backend_session,
            principal=_team_a_key(),
        )

    returned = {item.id for item in response.items}
    assert own.id in returned
    assert foreign.id not in returned


# ---------------------------------------------------------------------------
# PATCH /api/v1/model-backends/{backend_id} — a WRITE aimed at another team
# ---------------------------------------------------------------------------


async def test_model_backend_update_cannot_target_another_teams_backend(
    model_backend_session: AsyncSession,
) -> None:
    """A team-A key must not mutate a team-B team-private backend (404)."""
    foreign = _backend(owner_team_id=_TEAM_B, name="team-b-backend")
    model_backend_session.add(foreign)
    await model_backend_session.commit()

    request = MagicMock()
    request.default_params = None
    request.api_key = None
    request.base_url = None
    request.provider = None
    request.visibility = None
    request.owner_team_id = None
    request.cost_tracking = None
    request.currency = None
    request.tier = None
    request.fallback_backend_ids = None
    request.model_id = None
    request.name = None
    request.display_name = None
    request.extra = None

    secrets_backend = MagicMock()
    secrets_backend.read = AsyncMock(return_value=None)
    settings = MagicMock()
    settings.fernet_key = "a" * 32

    with (
        patch.object(model_backends_module, "set_rls_org", AsyncMock()),
        patch.object(model_backends_module, "set_rls_user_context", AsyncMock()),
        patch.object(model_backends_module, "create_secrets_backend", MagicMock(return_value=secrets_backend)),
        patch.object(model_backends_module, "_read_secret_payload", AsyncMock(return_value={})),
        pytest.raises(HTTPException) as exc_info,
    ):
        await model_backends_module._load_update_state(
            model_backend_session,
            foreign.id,
            _team_a_key(),
            request,
            settings,
            {},
        )

    assert exc_info.value.status_code == 404
