"""Prove-the-fix tests for FAR-2748: the apply CLI's system-map workflow accepts an ``mk_`` key.

The ``farna-control apply`` CLI manages system maps as a declared entity kind
(``backend/src/farna/cli/apply/system_map_apply.py``):

* ``GET  /api/v1/system-maps``                       — plan current state
* ``POST /api/v1/system-maps``                       — create a declared map
* ``PUT  /api/v1/system-maps/{map_id}``              — update a declared map
* ``POST /api/v1/system-maps/{map_id}/revisions``    — write the create-only draft revision

The CLI authenticates with its documented ``mk_`` org key. Before FAR-2748 the
whole system-map family was JWT-only (a JWT-only ``require_permission`` AND a
JWT-only plan feature gate), so every one of those calls 401'd the key.

These tests drive the REAL any-credential permission + plan-context stack (only
the plan source and the DB are stubbed), so the credential is a genuine
``Authorization: Bearer mk_...`` header. They fail on the pre-fix routes:

* the real id and the unknown id both returned 401 (not 200 / not 404);
* a grant-scoped key missing the route permission returned 401 (not 403);
* the structural assertion saw ``get_current_user`` in the route tree.

The four cases the ticket asks for are covered per route: a key with the real
id, an unknown id (404, not 401 — on the id-bearing routes), a key missing the
route permission (403 naming it), and no credential (401). The collection read
has no id, so it carries the three that apply plus the entitlement check.
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
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_REV_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}

_COLLECTION = "/api/v1/system-maps"
_MAP = f"/api/v1/system-maps/{_MAP_ID}"
_REVISIONS = f"/api/v1/system-maps/{_MAP_ID}/revisions"
_CREATE_BODY = {"name": "Apply Map", "visibility": "org"}
_UPDATE_BODY = {"description": "updated by apply"}
_REVISION_BODY = {"nodes": [], "relationships": [], "loop_configs": [], "notes": ""}

#: The apply CLI's system-map workflow: exactly the four routes FAR-2748 widened.
#: Keyed by the route TEMPLATE (the structural test walks the router).
_WIDENED = (
    ("GET", _COLLECTION),
    ("POST", _COLLECTION),
    ("PUT", "/api/v1/system-maps/{map_id}"),
    ("POST", "/api/v1/system-maps/{map_id}/revisions"),
)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
    )


def _map_row(**overrides: Any) -> SimpleNamespace:
    row = SimpleNamespace(
        id=_MAP_ID,
        organisation_id=_ORG_ID,
        name="Apply Map",
        description="a declared map",
        owner_team_id=None,
        visibility="org",
        default_map=False,
        published_revision_id=_REV_ID,
        account_id=_USER_ID,
        updated_by=_USER_ID,
        deleted_at=None,
        deleted_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _revision_row() -> SimpleNamespace:
    return SimpleNamespace(
        id=_REV_ID,
        organisation_id=_ORG_ID,
        system_map_id=_MAP_ID,
        revision_number=1,
        status="draft",
        content_json={},
        published_at=None,
        effective_from=None,
        effective_to=None,
        deleted_at=None,
        deleted_by=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _fake_key(role: str = "operator", *, team_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="apply-key",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        role=role,
        team_id=team_id,
    )


def _make_auth_session(*, role: str = "operator", key_team_id: uuid.UUID | None = None) -> AsyncMock:
    """Session used by ``get_current_tenant_user_or_api_key``'s org lookups."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    session.info = {}
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key(role, team_id=key_team_id)
    session.execute = AsyncMock(return_value=result)
    return session


def _make_route_session() -> AsyncMock:
    """Permissive session shared by the plan context, the gates and the handler."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    session.info = {}
    result = MagicMock()
    result.scalar_one.return_value = 1  # the list route's COUNT()
    result.scalar_one_or_none.return_value = None  # authz_enforce default
    result.first.return_value = None
    result.all.return_value = []
    result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=result)
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


def _stub_repo(
    *,
    maps: list[Any] | None = None,
    current_map: Any | None = None,
) -> SimpleNamespace:
    """A ``SystemMapRepository`` double with the methods the four routes call."""
    repo = SimpleNamespace()
    repo.list_maps_by_org = AsyncMock(return_value=maps or [])
    repo.create_map = AsyncMock(return_value=_map_row())
    repo.get_map_by_id = AsyncMock(return_value=current_map)
    repo.validate_bindings = AsyncMock(return_value=[])
    repo.list_revisions = AsyncMock(return_value=[])
    repo.create_revision = AsyncMock(return_value=_revision_row())
    repo.create_node = AsyncMock(return_value=None)
    repo.create_relationship = AsyncMock(return_value=None)
    repo.create_loop_config = AsyncMock(return_value=None)
    return repo


@contextmanager
def _api_key_request(
    *,
    feature_enabled: bool = True,
    grants: frozenset[str] | None = None,
    key_role: str = "operator",
    key_team_id: uuid.UUID | None = None,
    repo: SimpleNamespace | None = None,
    handler_patches: list[Any] | None = None,
) -> Generator[TestClient, None, None]:
    """Drive the real any-credential plan + permission stack with an ``mk_`` key.

    ``get_organisation`` and ``resolve_plan_context`` are stubbed so the test
    does not need a live database, but the dependency chain between the request
    and those calls is the production one: a JWT-only ``get_current_user``
    anywhere in it still 401s the key.
    """
    from farna.api.routes.system_maps import get_repo

    route_session = _make_route_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield route_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_repo] = lambda: repo or _stub_repo()

    with ExitStack() as stack:
        stack.enter_context(patch("farna.api.dependencies.get_or_create_engine", return_value=MagicMock()))
        stack.enter_context(
            patch(
                "farna.api.dependencies.get_or_create_session_factory",
                return_value=_FakeFactory(_make_auth_session(role=key_role, key_team_id=key_team_id)),
            )
        )
        stack.enter_context(patch("farna.auth.api_key.validate_api_key", return_value=_fake_key(key_role, team_id=key_team_id)))
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


def _route(method: str, path: str) -> Any:
    for route in get_all_apiroutes(app):
        if path == route.path and method in (route.methods or set()):
            return route
    raise AssertionError(f"route not found: {method} {path}")


# ---------------------------------------------------------------------------
# Structural: every widened route really resolves an any-credential principal.
# ---------------------------------------------------------------------------


class TestWidenedRoutesResolveAnyCredential:
    def test_each_widened_route_has_no_jwt_only_principal_dependency(self) -> None:
        for method, path in _WIDENED:
            names = get_applied_dependency_names(_route(method, path))
            assert "get_current_tenant_user_or_api_key" in names, f"{method} {path} does not accept an mk_ key"
            assert "get_current_user" not in names, f"{method} {path} still pulls the JWT-only get_current_user"
            assert "get_current_tenant_user" not in names, f"{method} {path} still pulls the JWT-only get_current_tenant_user"


# ---------------------------------------------------------------------------
# (a) key + real id = success.
# ---------------------------------------------------------------------------


class TestOrgApiKeySucceeds:
    def test_key_reads_the_map_collection(self) -> None:
        repo = _stub_repo(maps=[_map_row()])
        with _api_key_request(key_role="viewer", repo=repo) as client:
            resp = client.get(_COLLECTION, headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert [item["name"] for item in resp.json()["items"]] == ["Apply Map"]

    def test_key_creates_a_map(self) -> None:
        repo = _stub_repo()
        with _api_key_request(
            repo=repo,
            handler_patches=[patch("farna.api.routes.system_maps._record_audit", new=AsyncMock())],
        ) as client:
            resp = client.post(_COLLECTION, json=_CREATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "Apply Map"

    def test_key_updates_a_map(self) -> None:
        repo = _stub_repo(current_map=_map_row())
        with _api_key_request(
            repo=repo,
            handler_patches=[patch("farna.api.routes.system_maps._record_audit", new=AsyncMock())],
        ) as client:
            resp = client.put(_MAP, json=_UPDATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "updated by apply"

    def test_key_writes_a_revision(self) -> None:
        repo = _stub_repo(current_map=_map_row())
        with _api_key_request(
            repo=repo,
            handler_patches=[patch("farna.api.routes.system_maps._record_audit", new=AsyncMock())],
        ) as client:
            resp = client.post(_REVISIONS, json=_REVISION_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 201, resp.text
        assert resp.json()["system_map_id"] == str(_MAP_ID)


# ---------------------------------------------------------------------------
# (a2) a TEAM-scoped key (team_id set) is denied on all four routes.
#


class TestTeamScopedKeyIsRejected:
    """A team-scoped ``mk_`` key keeps the pre-widening denial on these routes.

    FAR-2748 widened the four routes to org API keys because the apply CLI uses
    an ORG-WIDE key. A team-scoped key must not ride that widening: the
    request-time owner-team guards check the key OWNER's memberships and RLS is
    set from the owner account, so a team-A key owned by a team-B member could
    reach team-B-private maps (the review's cross-team finding). The collection
    read and create path also have no request-scoped row for the team gate.
    """

    def test_team_scoped_key_cannot_read_the_collection(self) -> None:
        with _api_key_request(key_role="viewer", key_team_id=_TEAM_ID) as client:
            resp = client.get(_COLLECTION, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "team-scoped" in resp.json()["detail"]

    def test_team_scoped_key_cannot_create_a_map(self) -> None:
        repo = _stub_repo()
        with _api_key_request(key_team_id=_TEAM_ID, repo=repo) as client:
            resp = client.post(_COLLECTION, json=_CREATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "team-scoped" in resp.json()["detail"]
        repo.create_map.assert_not_awaited()

    def test_team_scoped_key_cannot_update_a_map(self) -> None:
        repo = _stub_repo(current_map=_map_row())
        with _api_key_request(key_team_id=_TEAM_ID, repo=repo) as client:
            resp = client.put(_MAP, json=_UPDATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "team-scoped" in resp.json()["detail"]

    def test_team_scoped_key_cannot_write_a_revision(self) -> None:
        repo = _stub_repo(current_map=_map_row())
        with _api_key_request(key_team_id=_TEAM_ID, repo=repo) as client:
            resp = client.post(_REVISIONS, json=_REVISION_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "team-scoped" in resp.json()["detail"]
        repo.create_revision.assert_not_awaited()


# ---------------------------------------------------------------------------
# (b) key + unknown id = 404, NOT 401 (the credential is good; the row is not).
# ---------------------------------------------------------------------------


class TestOrgApiKeyUnknownIdIs404:
    def test_key_updating_an_unknown_map_is_404(self) -> None:
        repo = _stub_repo(current_map=None)
        with _api_key_request(repo=repo) as client:
            resp = client.put(f"/api/v1/system-maps/{uuid.uuid4()}", json=_UPDATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text

    def test_key_writing_a_revision_for_an_unknown_map_is_404(self) -> None:
        repo = _stub_repo(current_map=None)
        with _api_key_request(repo=repo) as client:
            resp = client.post(f"/api/v1/system-maps/{uuid.uuid4()}/revisions", json=_REVISION_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# (c) key missing the route permission = 403 naming it (not 401).
# ---------------------------------------------------------------------------


class TestOrgApiKeyMissingPermissionIs403:
    def test_collection_read_requires_system_maps_read(self) -> None:
        with _api_key_request(key_role="viewer", grants=frozenset({"pipeline.list"})) as client:
            resp = client.get(_COLLECTION, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "system_maps.read" in resp.json()["detail"]

    def test_create_requires_system_map_create(self) -> None:
        # ``system_maps_access`` lets the router-level authoring probe pass, so
        # the denial under test is the route's own permission.
        with _api_key_request(grants=frozenset({"system_maps_access"})) as client:
            resp = client.post(_COLLECTION, json=_CREATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "system_map.create" in resp.json()["detail"]

    def test_update_requires_system_map_update(self) -> None:
        with _api_key_request(grants=frozenset({"system_maps_access"})) as client:
            resp = client.put(_MAP, json=_UPDATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "system_map.update" in resp.json()["detail"]

    def test_revision_create_requires_system_map_revision_create(self) -> None:
        with _api_key_request(grants=frozenset({"system_maps_access"})) as client:
            resp = client.post(_REVISIONS, json=_REVISION_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "system_map.revision.create" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# (d) no credential = 401: widening did not make any route anonymous.
# ---------------------------------------------------------------------------


class TestNoCredentialIs401:
    def test_collection_read_without_a_credential_is_401(self) -> None:
        with _api_key_request() as client:
            resp = client.get(_COLLECTION)

        assert resp.status_code == 401, resp.text

    def test_create_without_a_credential_is_401(self) -> None:
        with _api_key_request() as client:
            resp = client.post(_COLLECTION, json=_CREATE_BODY)

        assert resp.status_code == 401, resp.text

    def test_update_without_a_credential_is_401(self) -> None:
        with _api_key_request() as client:
            resp = client.put(_MAP, json=_UPDATE_BODY)

        assert resp.status_code == 401, resp.text

    def test_revision_without_a_credential_is_401(self) -> None:
        with _api_key_request() as client:
            resp = client.post(_REVISIONS, json=_REVISION_BODY)

        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Widening the credential does NOT skip the entitlement.
# ---------------------------------------------------------------------------


class TestFeatureGateStillApplies:
    def test_collection_read_is_402_when_system_maps_read_is_off(self) -> None:
        with _api_key_request(key_role="viewer", feature_enabled=False) as client:
            resp = client.get(_COLLECTION, headers=_AUTH_HEADERS)

        assert resp.status_code == 402, resp.text
        assert "system_maps_read" in resp.text

    def test_create_is_402_when_system_maps_is_off(self) -> None:
        with _api_key_request(feature_enabled=False) as client:
            resp = client.post(_COLLECTION, json=_CREATE_BODY, headers=_AUTH_HEADERS)

        assert resp.status_code == 402, resp.text
        assert "system_maps" in resp.text
