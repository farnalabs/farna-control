"""FAR-2781: a team-scoped credential is confined to the team it is scoped to.

The owner-team validators answer "is the key OWNER a member of the target
team?". They do not answer "is the CREDENTIAL confined to that team?". A
team-A-scoped API key whose owner also belongs to team B could therefore create
or re-scope a team-B resource.

The reachable REST write path is ``POST /pipelines`` and ``PATCH
/pipelines/{id}`` (both accept ``mk_`` keys). The primary HTTP test below drives
the real ``require_permission_any_credential`` chain with a team-scoped
principal, so it fails (201 / create called) without the boundary and passes
(403 with the typed ``team_boundary_violation`` code) with it. The pipeline
transition helper is exercised directly for the PATCH half, which shares the
same guard. The MCP ``set_resource_owner_team`` half lives in
``test_mcp_set_resource_owner_team.py``.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes.pipelines import _assert_team_transition_allowed
from farna.api.team_scope import TEAM_BOUNDARY_VIOLATION_CODE
from farna.auth.dependencies import get_current_tenant_user_or_api_key
from farna.auth.jwt import TenantPrincipal
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_CREATE_BODY = {
    "name": "Team-A Pipeline",
    "visibility": "team",
    "owner_team_id": str(_TEAM_B),
}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
    )


def _make_team_visible_session(team_found: bool = True) -> AsyncMock:
    """Session whose ``SELECT ... FROM teams`` result reflects ``team_found``."""
    session = configure_mock_session(AsyncMock())
    base_effect = session.execute.side_effect

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select) and "FROM teams" in str(stmt):
            result = MagicMock()
            result.first.return_value = (_TEAM_A,) if team_found else None
            return result
        return base_effect(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _team_scoped_key_principal(*, team_id: uuid.UUID, org_role: str = "operator") -> TenantPrincipal:
    return TenantPrincipal(
        username="apply-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
        via_api_key=True,
        team_id=team_id,
    )


@pytest.fixture
def pipeline_create_client() -> Generator[Any, None, None]:
    """TestClient whose any-credential dependency resolves a team-scoped key."""

    def _make(team_id: uuid.UUID, *, org_role: str = "operator", team_found: bool = True) -> TestClient:
        session = _make_team_visible_session(team_found=team_found)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: _team_scoped_key_principal(
            team_id=team_id,
            org_role=org_role,
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


class TestPipelineCreatePrincipalScope:
    """POST /pipelines: a team-A key must not create a team-B pipeline."""

    def test_team_a_key_cannot_create_team_b_pipeline(self, pipeline_create_client: Any) -> None:
        client = pipeline_create_client(_TEAM_A)
        with (
            patch("farna.api.team_scope.team_membership_exists", new=AsyncMock(return_value=True)),
            patch("farna.api.routes.pipelines.create_pipeline", new=AsyncMock()) as create_pipeline,
        ):
            resp = client.post("/api/v1/pipelines", json=_CREATE_BODY)
        assert resp.status_code == 403
        assert resp.json()["code"] == TEAM_BOUNDARY_VIOLATION_CODE
        create_pipeline.assert_not_called()

    def test_team_a_admin_role_key_cannot_create_team_b_pipeline(self, pipeline_create_client: Any) -> None:
        """The scope check runs BEFORE the org-role admin bypass."""
        client = pipeline_create_client(_TEAM_A, org_role="admin")
        with (
            patch("farna.api.team_scope.team_membership_exists", new=AsyncMock(return_value=True)),
            patch("farna.api.routes.pipelines.create_pipeline", new=AsyncMock()) as create_pipeline,
        ):
            resp = client.post("/api/v1/pipelines", json=_CREATE_BODY)
        assert resp.status_code == 403
        assert resp.json()["code"] == TEAM_BOUNDARY_VIOLATION_CODE
        create_pipeline.assert_not_called()

    def test_team_a_key_can_create_team_a_pipeline(self, pipeline_create_client: Any) -> None:
        """Positive: the key's own team is still writable without owner membership."""
        client = pipeline_create_client(_TEAM_A)
        body = {**_CREATE_BODY, "owner_team_id": str(_TEAM_A)}
        with (
            patch("farna.api.team_scope.team_membership_exists", new=AsyncMock(return_value=False)),
            patch("farna.api.routes.pipelines._pipeline_response", new=MagicMock(return_value=MagicMock())),
            patch(
                "farna.api.routes.pipelines.create_pipeline",
                new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
            ) as create_pipeline,
        ):
            client.post("/api/v1/pipelines", json=body)
        create_pipeline.assert_called_once()


def _current_pipeline(*, owner_team_id: uuid.UUID | None, visibility: str = "team") -> MagicMock:
    current = MagicMock()
    current.owner_team_id = owner_team_id
    current.visibility = visibility
    return current


class TestPipelineTransitionPrincipalScope:
    """The PATCH transition guard shares the FAR-2781 boundary."""

    async def test_team_a_key_cannot_move_pipeline_to_team_b(self) -> None:
        session = configure_mock_session(AsyncMock())
        with pytest.raises(HTTPException) as exc_info:
            await _assert_team_transition_allowed(
                session,
                _team_scoped_key_principal(team_id=_TEAM_A),
                _current_pipeline(owner_team_id=_TEAM_A),
                {"owner_team_id": _TEAM_B},
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == TEAM_BOUNDARY_VIOLATION_CODE

    async def test_team_a_admin_role_key_cannot_move_pipeline_to_team_b(self) -> None:
        session = configure_mock_session(AsyncMock())
        with pytest.raises(HTTPException) as exc_info:
            await _assert_team_transition_allowed(
                session,
                _team_scoped_key_principal(team_id=_TEAM_A, org_role="admin"),
                _current_pipeline(owner_team_id=_TEAM_A),
                {"owner_team_id": _TEAM_B},
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == TEAM_BOUNDARY_VIOLATION_CODE

    async def test_team_a_key_cannot_touch_a_team_b_pipeline(self) -> None:
        session = configure_mock_session(AsyncMock())
        with pytest.raises(HTTPException) as exc_info:
            await _assert_team_transition_allowed(
                session,
                _team_scoped_key_principal(team_id=_TEAM_A),
                _current_pipeline(owner_team_id=_TEAM_B),
                {"visibility": "team"},
            )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error"] == TEAM_BOUNDARY_VIOLATION_CODE

    async def test_team_a_key_can_update_its_own_pipeline(self) -> None:
        session = configure_mock_session(AsyncMock())
        with patch("farna.api.routes.pipelines.team_membership_exists", new=AsyncMock(return_value=False)) as membership:
            await _assert_team_transition_allowed(
                session,
                _team_scoped_key_principal(team_id=_TEAM_A),
                _current_pipeline(owner_team_id=_TEAM_A),
                {"owner_team_id": _TEAM_A},
            )
        membership.assert_not_awaited()
