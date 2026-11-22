"""Prove-the-fix tests for FAR-2681: an ``mk_`` org API key can READ a composite
template, while authorisation is preserved.

Before FAR-2681 ``GET /api/v1/composite-templates/{id}`` (and its list / editor
siblings) resolved through ``get_current_tenant_user`` — a Bearer JWT only — so
an org API key was rejected 401 *before* the route body ran. A real id and an
invented id produced the identical 401, which made absence unobservable. The
pipeline graph read (``GET /api/v1/pipelines/{id}/graph``) already accepts the
same key via ``require_permission_any_credential``.

Unlike the per-route fixtures that override ``get_current_tenant_user``, these
tests drive the REAL ``require_permission_any_credential`` chain, including
``get_current_tenant_user_or_api_key``'s ``mk_`` key resolution, so the
credential is a genuine ``Authorization: Bearer mk_...`` header.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _make_template(*, organisation_id: uuid.UUID = _ORG_ID) -> MagicMock:
    t = MagicMock()
    t.id = _TEMPLATE_ID
    t.organisation_id = organisation_id
    t.name = "Devil's Advocate"
    t.description = None
    t.sub_pipeline_graph_json = {"nodes": [], "edges": []}
    t.parameter_ports_json = []
    t.input_schema_id = None
    t.output_schema_id = None
    t.parameter_schema_id = None
    t.version = "1.0.0"
    t.created_by = _USER_ID
    t.account_id = _USER_ID
    t.created_at = _NOW
    t.updated_at = _NOW
    return t


def _fake_key(role: str = "operator") -> SimpleNamespace:
    return SimpleNamespace(
        name="composite-read-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=None,
    )


def _make_auth_session() -> AsyncMock:
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
    result.scalar_one_or_none.return_value = _fake_key()
    session.execute = AsyncMock(return_value=result)
    return session


def _make_route_session(template: object | None) -> AsyncMock:
    """Route session: the org-scoped template lookup yields *template*.

    Anything else the route's dependencies issue is served by the shared strict
    contract double, except the authz-enforce kill-switch read (which the
    permission dependency always issues) and the template SELECT.
    """
    session = configure_mock_session(AsyncMock())

    def _dispatch(stmt: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
        result = MagicMock()
        if "authz_enforce" in str(stmt):
            result.scalar_one_or_none.return_value = None
            return result
        result.scalar_one_or_none.return_value = template
        return result

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
            return_value=_FakeFactory(_make_auth_session()),
        ),
        patch("farna.auth.api_key.validate_api_key", return_value=_fake_key(key_role)),
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


class TestCompositeReadAcceptsOrgApiKey:
    def test_org_api_key_reads_a_real_template(self) -> None:
        """(a) org API key + real id = 200 (was 401 before FAR-2681)."""
        route_session = _make_route_session(_make_template())
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(_TEMPLATE_ID)
        assert resp.json()["name"] == "Devil's Advocate"

    def test_org_api_key_gets_404_for_an_unknown_id(self) -> None:
        """(b) org API key + unknown id = 404 (was 401, identical to the real id)."""
        route_session = _make_route_session(None)
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/composite-templates/{uuid.uuid4()}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Composite template not found"

    def test_grant_scoped_key_without_the_read_permission_gets_403(self) -> None:
        """(c) a credential lacking ``pipeline.graph.read`` = 403 (was 401).

        The role floor for the read is ``viewer`` — the lowest org role — so the
        only credential that can lack the permission is a grant-bearing API key
        whose explicit grant-set omits it (FAR-1477: effective = grants
        INTERSECT the live role bundle).
        """
        route_session = _make_route_session(_make_template())
        with _api_key_request(route_session, grants=frozenset({"pipeline.list"})) as client:
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "pipeline.graph.read" in resp.json()["detail"]

    def test_cross_org_id_returns_404_not_the_foreign_template(self) -> None:
        """(d) an id owned by another org = 404, never the foreign row.

        RLS / the tenant filter normally make the lookup miss; the request-time
        org guard (`_template_visible_to`) is the defence-in-depth layer this
        test pins — it fails with 200 if the guard is removed.
        """
        route_session = _make_route_session(_make_template(organisation_id=_OTHER_ORG_ID))
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "Composite template not found"

    def test_editor_read_also_accepts_the_org_api_key(self) -> None:
        """The sibling editor GET is the same read family and follows it."""
        route_session = _make_route_session(_make_template())
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}/editor", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"nodes": [], "edges": []}

    def test_list_read_accepts_the_org_api_key(self) -> None:
        """The list read is the same family; it must not 401 the key either."""
        route_session = configure_mock_session(AsyncMock())
        page_result = MagicMock(items=[_make_template()], total=1, page=1, page_size=20)

        def _dispatch(stmt: Any, *_args: Any, **_kwargs: Any) -> MagicMock:
            result = MagicMock()
            if "authz_enforce" in str(stmt):
                result.scalar_one_or_none.return_value = None
                return result
            result.scalar.return_value = 1
            result.scalars.return_value.all.return_value = [_make_template()]
            return result

        route_session.execute = AsyncMock(side_effect=_dispatch)
        with (
            patch(
                "farna.api.routes.composite_templates.list_composite_templates",
                return_value=page_result,
            ),
            _api_key_request(route_session) as client,
        ):
            resp = client.get("/api/v1/composite-templates", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1


class TestCredentialBoundaryStillEnforced:
    def test_no_bearer_credential_is_still_401(self) -> None:
        """The read is still an authenticated route: no credential = 401.

        The any-credential switch only widens WHICH credential kinds resolve a
        principal (JWT or ``mk_`` key); it does not make the read public, and
        the permission gate still runs for either flavour.
        """
        route_session = _make_route_session(_make_template())
        with _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/composite-templates/{_TEMPLATE_ID}")

        assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("path", ["/api/v1/composite-templates/{id}", "/api/v1/composite-templates/{id}/editor"])
def test_foreign_row_never_serialises(path: str) -> None:
    """Neither by-id read leaks a foreign template body (positive control)."""
    route_session = _make_route_session(_make_template(organisation_id=_OTHER_ORG_ID))
    with _api_key_request(route_session) as client:
        resp = client.get(path.format(id=_TEMPLATE_ID), headers=_AUTH_HEADERS)

    assert resp.status_code == 404
    assert "Devil's Advocate" not in resp.text
