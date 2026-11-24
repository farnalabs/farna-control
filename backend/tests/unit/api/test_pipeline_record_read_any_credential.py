"""Prove-the-fix tests for FAR-2726: an ``mk_`` org API key can READ the
pipeline RECORD (``GET /api/v1/pipelines/{id}``), with authorisation intact.

Before FAR-2726 the route resolved through ``require_permission`` and
``require_team_membership_or_admin`` — Bearer JWT only — so an org API key was
rejected 401 *before* the team gate or handler ran. A real id and an invented
id produced the identical 401, which made absence unobservable. The pipeline
GRAPH read (``GET /api/v1/pipelines/{id}/graph``) already accepts the same key,
so the sensitivity boundary was inverted: the graph (prompts, connector
bindings, model pins) was readable by a credential the record was not.

These tests drive the REAL ``require_permission_any_credential`` +
``require_team_membership_or_admin_any_credential`` chain, including
``get_current_tenant_user_or_api_key``'s ``mk_`` key resolution, so the
credential is a genuine ``Authorization: Bearer mk_...`` header. Both gates
must be the any-credential flavour together: a half-change (one gate on the JWT
dependency) would 401 the key again or skip the team boundary.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session, configure_rls_preamble

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_OTHER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
    )


def _make_pipeline(
    *,
    organisation_id: uuid.UUID = _ORG_ID,
    name: str = "Deploy Pipeline",
) -> MagicMock:
    """A partial ORM stand-in the PipelineResponse validators accept."""
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = organisation_id
    p.name = name
    p.description = None
    p.visibility = "org"
    p.owner_team_id = None
    p.environment_profile_id = None
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.graph_nodes_json = []
    p.rate_limit_config = None
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.created_by = _USER_ID
    p.account_id = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _fake_key(role: str = "operator", *, team_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="pipeline-record-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=team_id,
    )


def _make_auth_session(*, role: str = "operator", team_id: uuid.UUID | None = None) -> AsyncMock:
    """Session used by ``get_current_tenant_user_or_api_key``'s org lookups."""
    session = AsyncMock()
    configure_rls_preamble(session)
    session.info = {}
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    # ``_lookup_api_key_org`` resolves the key's org off the key row.
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key(role, team_id=team_id)
    session.execute = AsyncMock(return_value=result)
    return session


def _make_route_session(
    *,
    pipeline: object | None,
    team_row: tuple[uuid.UUID | None, str | None] | None,
) -> AsyncMock:
    """One session shared by both gates and the handler.

    The team resolver reads via ``result.first()``; the handler's org-scoped
    ``get_pipeline`` reads via ``result.scalar_one_or_none()``. Serving both off
    one ``FROM pipelines`` result lets each case pin exactly what the gate sees
    versus what the handler sees (e.g. a resolvable team row with no
    handler-visible row = the org-filter defence-in-depth case).
    """
    session = configure_mock_session(AsyncMock())

    def _dispatch(stmt: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
        result = MagicMock()
        text = str(stmt)
        if "authz_enforce" in text:
            result.scalar_one_or_none.return_value = None
            result.first.return_value = None
            return result
        if "FROM pipelines" in text:
            result.first.return_value = team_row
            result.scalar_one_or_none.return_value = pipeline
            return result
        raise AssertionError(f"Unexpected pipeline-record query: {text}")

    session.execute = AsyncMock(side_effect=_dispatch)
    return session


class _FakeFactory:
    """``async_sessionmaker`` stand-in for the API-key auth path."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> "_FakeFactory":
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *_args: object) -> bool:
        return False


@contextmanager
def _api_key_request(
    route_session: AsyncMock,
    *,
    key_role: str = "operator",
    team_id: uuid.UUID | None = None,
    grants: frozenset[str] | None = None,
) -> Generator[TestClient, None, None]:
    """Drive the real any-credential stack with an ``mk_`` bearer header."""

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield route_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    patches = [
        patch("farna.api.dependencies.get_or_create_engine", return_value=MagicMock()),
        patch(
            "farna.api.dependencies.get_or_create_session_factory",
            return_value=_FakeFactory(_make_auth_session(role=key_role, team_id=team_id)),
        ),
        patch("farna.auth.api_key.validate_api_key", return_value=_fake_key(key_role, team_id=team_id)),
        patch(
            "farna.db.crud.org_membership.resolve_live_role_and_kind",
            new=AsyncMock(return_value=(key_role, "human")),
        ),
    ]
    if grants is not None:
        patches.append(patch("farna.auth.api_key.resolve_key_grants", new=AsyncMock(return_value=grants)))
    try:
        for active in patches:
            active.start()
        yield TestClient(app)
    finally:
        for active in patches:
            active.stop()
        app.dependency_overrides.clear()


class TestPipelineRecordReadAcceptsOrgApiKey:
    def test_org_api_key_reads_a_real_pipeline(self) -> None:
        """(a) org API key + real id = 200 (was 401 before FAR-2726)."""
        route_session = _make_route_session(pipeline=_make_pipeline(), team_row=(None, "org"))
        with _api_key_request(route_session, key_role="viewer") as client:
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(_PIPELINE_ID)
        assert resp.json()["name"] == "Deploy Pipeline"

    def test_org_api_key_gets_404_for_an_unknown_id(self) -> None:
        """(b) org API key + unknown id = 404 (was 401, identical to the real id)."""
        route_session = _make_route_session(pipeline=None, team_row=None)
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/pipelines/{uuid.uuid4()}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text

    def test_grant_scoped_key_without_the_read_permission_gets_403(self) -> None:
        """(c) a credential lacking ``pipeline.list`` = 403 (was 401).

        ``pipeline.list`` resolves to ``viewer`` — the lowest org role — so the
        only credential that can lack it is a grant-bearing API key whose
        explicit grant-set omits it (FAR-1477: effective = grants INTERSECT the
        live role bundle).
        """
        route_session = _make_route_session(pipeline=_make_pipeline(), team_row=(None, "org"))
        with _api_key_request(route_session, grants=frozenset({"pipeline.graph.read"})) as client:
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "pipeline.list" in resp.json()["detail"]

    def test_cross_org_id_returns_404_not_the_foreign_pipeline(self) -> None:
        """(d) an id owned by another org = 404, never the foreign row.

        The team resolver sees an org-visible row (so the gate passes) while
        the handler's org-scoped ``get_pipeline`` finds nothing — the
        defence-in-depth tenant filter, which fails with a 200/foreign body if
        the ``organisation_id`` predicate is dropped.
        """
        route_session = _make_route_session(pipeline=None, team_row=(None, "org"))
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text
        assert "Deploy Pipeline" not in resp.text

    def test_team_scoped_key_cannot_read_another_teams_private_pipeline(self) -> None:
        """The any-credential team gate keeps a team-scoped key's hard boundary.

        A team-A-scoped key reading a team-private pipeline owned by team B is
        a 403 boundary denial — the same matrix the JWT path applied. Without
        the any-credential TEAM gate (the half-change) this would reach the
        handler and leak the record.
        """
        route_session = _make_route_session(
            pipeline=_make_pipeline(name="Team B Pipeline"),
            team_row=(_TEAM_B, "team"),
        )
        with _api_key_request(route_session, key_role="operator", team_id=_TEAM_A) as client:
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert str(_TEAM_A) in resp.json()["detail"]
        assert "Team B Pipeline" not in resp.text


class TestCredentialBoundaryStillEnforced:
    def test_no_bearer_credential_is_still_401(self) -> None:
        """The record read is still authenticated: no credential = 401.

        The any-credential switch only widens WHICH credential kinds resolve a
        principal (JWT or ``mk_`` key); it does not make the record public, and
        the permission gate still runs for either flavour.
        """
        route_session = _make_route_session(pipeline=_make_pipeline(), team_row=(None, "org"))
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")

        assert resp.status_code == 401, resp.text
