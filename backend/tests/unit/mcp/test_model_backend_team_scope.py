"""Team-boundary enforcement for team-scoped API keys on the model-backend MCP tools.

A team-scoped ``mk_`` org API key carries a hard team boundary in
``_ctx_team_id`` (FAR-681, ADR 070 §5). ``model_backends`` carries
``rls_team_isolation`` keyed on the KEY OWNER's memberships, so a key scoped to
team A whose owner also belongs to team B still reaches team B's team-private
backend unless the tool applies the key's own boundary. These cases drive the
real tool handlers and the real CRUD against in-memory SQLite, seeding both
teams inside ONE organisation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.api import mcp_server
from farna.db.models.base import Base
from farna.db.models.connector_instance import ConnectorInstance
from farna.db.models.environment_profile import EnvironmentProfile
from farna.db.models.model_backend import ModelBackend

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000ca")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000cb")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"


@pytest.fixture(autouse=True)
def _mcp_auth_context() -> object:
    """Populate the MCP auth ContextVars with a team-A-scoped API key."""
    from farna.api.mcp_server import (
        _ctx_auth_token,
        _ctx_auth_type,
        _ctx_key_id,
        _ctx_org_id,
        _ctx_role,
        _ctx_team_id,
        _ctx_user_id,
    )

    tokens = [
        (_ctx_org_id, _ORG_ID),
        (_ctx_role, "operator"),
        (_ctx_user_id, _ACCOUNT_ID),
        (_ctx_key_id, uuid.uuid4()),
        (_ctx_auth_token, _API_KEY),
        (_ctx_auth_type, "api_key"),
        (_ctx_team_id, _TEAM_A),
    ]
    set_tokens = [var.set(value) for var, value in tokens]
    yield None
    for (var, _value), token in zip(tokens, set_tokens, strict=True):
        var.reset(token)


@pytest.fixture
async def model_backend_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=[ModelBackend.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


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


def _session_context(session: AsyncSession) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


async def test_list_model_backends_hides_another_teams_private_backend(
    model_backend_session: AsyncSession,
) -> None:
    foreign = _backend(owner_team_id=_TEAM_B, name="team-b-backend")
    own = _backend(owner_team_id=_TEAM_A, name="team-a-backend")
    model_backend_session.add_all([foreign, own])
    await model_backend_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(model_backend_session))),
    ):
        result = await mcp_server.list_model_backends()

    returned = {item["id"] for item in result["data"]}
    assert str(own.id) in returned
    assert str(foreign.id) not in returned


async def test_get_model_backend_refuses_another_teams_backend(
    model_backend_session: AsyncSession,
) -> None:
    foreign = _backend(owner_team_id=_TEAM_B, name="team-b-backend")
    model_backend_session.add(foreign)
    await model_backend_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(model_backend_session))),
    ):
        result = await mcp_server.get_model_backend(str(foreign.id))

    assert result["error"] == "team_boundary_violation"


async def test_update_model_backend_refuses_another_teams_backend(
    model_backend_session: AsyncSession,
) -> None:
    foreign = _backend(owner_team_id=_TEAM_B, name="team-b-backend")
    model_backend_session.add(foreign)
    await model_backend_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(model_backend_session))),
    ):
        result = await mcp_server.update_model_backend(str(foreign.id), name="renamed")

    assert result["error"] == "team_boundary_violation"


# ---------------------------------------------------------------------------
# Connector instances and environment profiles are team-scoped too
# ---------------------------------------------------------------------------


@pytest.fixture
async def connector_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=[ConnectorInstance.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _connector(*, owner_team_id: uuid.UUID | None, name: str) -> ConnectorInstance:
    return ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        connector_type_id="github",
        account_id=_ACCOUNT_ID,
        owner_team_id=owner_team_id,
        visibility="team" if owner_team_id is not None else "org",
        credentials_ciphertext=b"",
        config_json={},
        allowed_operations=[],
    )


async def test_list_connectors_hides_another_teams_private_connector(
    connector_session: AsyncSession,
) -> None:
    foreign = _connector(owner_team_id=_TEAM_B, name="team-b-connector")
    own = _connector(owner_team_id=_TEAM_A, name="team-a-connector")
    connector_session.add_all([foreign, own])
    await connector_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(connector_session))),
    ):
        result = await mcp_server.list_connectors()

    returned = {item["id"] for item in result["data"]}
    assert str(own.id) in returned
    assert str(foreign.id) not in returned


async def test_get_connector_refuses_another_teams_connector(
    connector_session: AsyncSession,
) -> None:
    foreign = _connector(owner_team_id=_TEAM_B, name="team-b-connector")
    connector_session.add(foreign)
    await connector_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(connector_session))),
    ):
        result = await mcp_server.get_connector(str(foreign.id))

    assert result["error"] == "team_boundary_violation"


async def test_delete_connector_refuses_another_teams_connector(
    connector_session: AsyncSession,
) -> None:
    """A team-A key must not DELETE a team-B connector, and the row survives."""
    foreign = _connector(owner_team_id=_TEAM_B, name="team-b-connector")
    connector_session.add(foreign)
    await connector_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(connector_session))),
    ):
        result = await mcp_server.delete_connector(str(foreign.id))

    assert result["error"] == "team_boundary_violation"
    survived = await connector_session.get(ConnectorInstance, foreign.id)
    assert survived is not None


@pytest.fixture
async def environment_profile_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=[EnvironmentProfile.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def _environment_profile(*, owner_team_id: uuid.UUID | None, name: str) -> EnvironmentProfile:
    return EnvironmentProfile(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        name=name,
        provider_type="local_docker",
        account_id=_ACCOUNT_ID,
        owner_team_id=owner_team_id,
        visibility="team" if owner_team_id is not None else "org",
        config_json={},
        capabilities_json=[],
        secret_refs_json=[],
    )


async def test_list_environment_profiles_hides_another_teams_profile(
    environment_profile_session: AsyncSession,
) -> None:
    foreign = _environment_profile(owner_team_id=_TEAM_B, name="team-b-profile")
    own = _environment_profile(owner_team_id=_TEAM_A, name="team-a-profile")
    environment_profile_session.add_all([foreign, own])
    await environment_profile_session.commit()

    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_require_plan_feature", AsyncMock(return_value=None)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(environment_profile_session))),
    ):
        result = await mcp_server.list_environment_profiles()

    returned = {item["id"] for item in result["data"]}
    assert str(own.id) in returned
    assert str(foreign.id) not in returned
