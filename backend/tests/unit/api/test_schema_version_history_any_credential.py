"""Prove-the-fix tests for FAR-2729: schema version history accepts an ``mk_`` key.

``GET``/``POST /api/v1/schemas/{schema_id}/versions`` carried an
``any_credential: declarative apply (FAR-681)`` comment and an any-credential
permission gate, but the route-level ``require_feature("schema_version_history")``
resolved its plan through ``get_plan_context`` -> ``get_current_user`` (a Bearer
JWT), so an ``mk_`` org API key was rejected 401 *before* the handler. The
comment claimed a capability the code did not have.

The fix swaps the feature gate for ``require_feature_any_credential``, whose
plan context authenticates through ``get_current_tenant_user_or_api_key``.

These tests drive the REAL plan-context and permission dependencies (only the
plan source and the DB is stubbed), so the credential is a genuine
``Authorization: Bearer mk_...`` header. They fail on the pre-fix route:

* the real id and the unknown id both returned 401 (not 200 / not 404);
* a grant-scoped key missing ``schema.list`` returned 401 (not 403);
* the introspection assertion saw ``get_current_user`` in the route tree.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session
from farna.api.main import app
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.api.plan_stubs import PlanStub
from tests.unit.api.route_introspection import get_all_apiroutes, get_applied_dependency_names

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}
_VERSIONS_PATH = f"/api/v1/schemas/{_SCHEMA_ID}/versions"
_CREATE_BODY = {"version": "1.0", "version_number": 1, "definition_json": {"type": "object"}}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
    )


def _make_schema() -> MagicMock:
    schema = MagicMock()
    schema.id = _SCHEMA_ID
    schema.organisation_id = _ORG_ID
    schema.name = "Test Schema"
    return schema


def _make_schema_version() -> MagicMock:
    sv = MagicMock()
    sv.id = uuid.uuid4()
    sv.organisation_id = _ORG_ID
    sv.schema_id = _SCHEMA_ID
    sv.version = "1.0"
    sv.version_number = 1
    sv.definition_json = {"type": "object"}
    sv.published = False
    sv.account_id = _USER_ID
    sv.created_at = _NOW
    sv.updated_at = _NOW
    return sv


def _fake_key(role: str = "viewer") -> SimpleNamespace:
    return SimpleNamespace(
        name="schema-version-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=None,
    )


def _make_auth_session(*, role: str = "viewer") -> AsyncMock:
    """Session used by ``get_current_tenant_user_or_api_key``'s org lookups."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    session.info = {}
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key(role)
    session.execute = AsyncMock(return_value=result)
    return session


def _make_route_session() -> AsyncMock:
    """Permissive session shared by the plan context, the gates and the handler."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    session.info = {}
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
    *,
    feature_enabled: bool = True,
    grants: frozenset[str] | None = None,
    key_role: str = "viewer",
    handler_patches: list[Any] | None = None,
) -> Generator[TestClient, None, None]:
    """Drive the real any-credential plan + permission stack with an ``mk_`` key.

    ``get_organisation`` and ``resolve_plan_context`` are stubbed so the test
    does not need a live database, but the dependency chain between the
    request and those calls is the production one: a JWT-only
    ``get_current_user`` anywhere in it still 401s the key.
    """
    route_session = _make_route_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield route_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    with ExitStack() as stack:
        stack.enter_context(patch("farna.api.dependencies.get_or_create_engine", return_value=MagicMock()))
        stack.enter_context(
            patch(
                "farna.api.dependencies.get_or_create_session_factory",
                return_value=_FakeFactory(_make_auth_session(role=key_role)),
            )
        )
        stack.enter_context(patch("farna.auth.api_key.validate_api_key", return_value=_fake_key(key_role)))
        stack.enter_context(
            patch(
                "farna.db.crud.org_membership.resolve_live_role_and_kind",
                new=AsyncMock(return_value=(key_role, "human")),
            )
        )
        stack.enter_context(
            patch(
                "farna.db.crud.organisation.get_organisation",
                new=AsyncMock(return_value=SimpleNamespace(settings_json={}, plan_id=None)),
            )
        )
        stack.enter_context(
            patch(
                "farna.core.feature_flags.resolve_plan_context",
                new=AsyncMock(return_value=PlanStub(enabled=feature_enabled, tier="community")),
            )
        )
        if grants is not None:
            stack.enter_context(patch("farna.auth.api_key.resolve_key_grants", new=AsyncMock(return_value=grants)))
        for handler_patch in handler_patches or []:
            stack.enter_context(handler_patch)
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()


class TestSchemaVersionHistoryAcceptsOrgApiKey:
    def test_org_api_key_reads_a_real_schema_version_history(self) -> None:
        """(a) org API key + real id = 200 (was 401 before FAR-2729)."""
        page_result = MagicMock(items=[_make_schema_version()], total=1, page=1, page_size=20)
        patches = [
            patch("farna.api.routes.schemas.get_schema", return_value=_make_schema()),
            patch("farna.api.routes.schemas.list_schema_versions", return_value=page_result),
            patch("farna.api.routes.schemas.set_rls_org"),
        ]
        with _api_key_request(handler_patches=patches) as client:
            resp = client.get(_VERSIONS_PATH, headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["version"] == "1.0"

    def test_org_api_key_gets_404_for_an_unknown_schema(self) -> None:
        """(b) org API key + unknown id = 404 (was 401, identical to the real id)."""
        patches = [
            patch("farna.api.routes.schemas.get_schema", return_value=None),
            patch("farna.api.routes.schemas.set_rls_org"),
        ]
        with _api_key_request(handler_patches=patches) as client:
            resp = client.get(f"/api/v1/schemas/{uuid.uuid4()}/versions", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text

    def test_grant_scoped_key_without_schema_list_gets_403(self) -> None:
        """(c) a credential lacking ``schema.list`` = 403 naming it (was 401)."""
        patches = [
            patch("farna.api.routes.schemas.get_schema", return_value=_make_schema()),
            patch("farna.api.routes.schemas.set_rls_org"),
        ]
        with _api_key_request(grants=frozenset({"schema.update"}), handler_patches=patches) as client:
            resp = client.get(_VERSIONS_PATH, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "schema.list" in resp.json()["detail"]

    def test_no_credential_is_still_401(self) -> None:
        """(d) the read is still authenticated: no credential = 401."""
        with _api_key_request() as client:
            resp = client.get(_VERSIONS_PATH)

        assert resp.status_code == 401, resp.text

    def test_feature_gate_still_refuses_when_the_feature_is_off(self) -> None:
        """Widening the credential does NOT skip the entitlement: off = 402."""
        with _api_key_request(feature_enabled=False) as client:
            resp = client.get(_VERSIONS_PATH, headers=_AUTH_HEADERS)

        assert resp.status_code == 402, resp.text
        assert "schema_version_history" in resp.text


class TestSchemaVersionCreateAcceptsOrgApiKey:
    def test_org_api_key_creates_a_schema_version(self) -> None:
        """The sibling POST route carries the same FAR-681 comment and contract."""
        patches = [
            patch("farna.api.routes.schemas.get_schema", return_value=_make_schema()),
            patch("farna.api.routes.schemas.create_schema_version", return_value=_make_schema_version()),
            patch("farna.api.routes.schemas.set_rls_org"),
        ]
        with _api_key_request(key_role="operator", handler_patches=patches) as client:
            resp = client.post(_VERSIONS_PATH, json=_CREATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 201, resp.text
        assert resp.json()["version"] == "1.0"

    def test_create_without_a_credential_is_still_401(self) -> None:
        with _api_key_request() as client:
            resp = client.post(_VERSIONS_PATH, json=_CREATE_BODY)

        assert resp.status_code == 401, resp.text


class TestCredentialContractIsPinned:
    """Fails on the pre-fix route even before the behaviour tests run."""

    def _route(self, method: str, path: str) -> Any:
        for route in get_all_apiroutes(app):
            if path == route.path and method in (route.methods or set()):
                return route
        raise AssertionError(f"route not found: {method} {path}")

    def test_version_history_depends_on_the_any_credential_principal(self) -> None:
        names = get_applied_dependency_names(self._route("GET", "/api/v1/schemas/{schema_id}/versions"))

        assert "get_current_tenant_user_or_api_key" in names
        assert "get_current_user" not in names
        assert "get_current_tenant_user" not in names

    def test_create_version_depends_on_the_any_credential_principal(self) -> None:
        names = get_applied_dependency_names(self._route("POST", "/api/v1/schemas/{schema_id}/versions"))

        assert "get_current_tenant_user_or_api_key" in names
        assert "get_current_user" not in names
        assert "get_current_tenant_user" not in names
