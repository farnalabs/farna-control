"""Prove-the-fix tests for FAR-2682: an ``mk_`` org API key can READ the library
and pipeline-template resources its role permits, while authorisation is
preserved.

Before FAR-2682 these reads resolved through ``get_current_tenant_user`` (a
Bearer JWT only) — ``GET /api/v1/templates`` and the library rating reads had no
permission dependency at all, and ``GET /api/v1/libraries/{id}`` carried a
tenant-only ``library.search`` gate — so an org API key was rejected 401 *before*
the route body ran. A real id and an invented id produced the identical 401,
which made absence unobservable, and a composite primitive's masked sub-pipeline
graph was readable by a key through the composite-template route (FAR-2681) but
not through the library by-id route.

Unlike the per-route fixtures that override ``get_current_tenant_user``, these
tests drive the REAL ``require_permission_any_credential`` chain, including
``get_current_tenant_user_or_api_key``'s ``mk_`` key resolution, so the
credential is a genuine ``Authorization: Bearer mk_...`` header. Only the CRUD
boundary of each route is patched (there is no database in the unit tier).
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.core.library_service import BUILTIN_ORG_ID
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session, configure_rls_preamble

_FERNET_KEY = Fernet.generate_key().decode()
_VALID_32 = "a" * 32
# Deliberately distinct from ``BUILTIN_ORG_ID`` (the built-in sentinel org,
# ``...0001``): the caller org must differ from the sentinel so the
# ``_BUILTIN_ORG_ID`` arm of ``_primitive_visible_to`` is genuinely exercised.
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_OTHER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PRIMITIVE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_TEMPLATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
        dev_mode=True,
    )


def _make_primitive(
    *,
    pid: uuid.UUID = _PRIMITIVE_ID,
    organisation_id: uuid.UUID = _ORG_ID,
    primitive_type: str = "composite",
) -> MagicMock:
    """A response-valid library primitive (composite carries a sub-graph)."""
    p = MagicMock()
    p.id = pid
    p.organisation_id = organisation_id
    p.source = "local"
    p.primitive_type = primitive_type
    p.name = "Devil's Advocate"
    p.slug = "devils-advocate"
    p.description = "A masked sub-pipeline graph"
    p.author = _USER_ID.hex
    p.version = "1.0"
    p.tags = ["composite"]
    p.content_json = {"nodes": [], "edges": []}
    p.source_url = None
    p.forked_from = None
    p.checksum = None
    p.ed25519_signature = None
    p.verified = None
    p.trust_tier = None
    p.tier = "native"
    p.download_count = 0
    p.average_rating = None
    p.review_count = 0
    p.owner_team_id = None
    p.visibility = "org"
    p.account_id = _USER_ID
    p.auto_update = True
    p.status = None
    p.manifest_pins = None
    p.trust_header = None
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_template(*, organisation_id: uuid.UUID = _ORG_ID) -> MagicMock:
    p = _make_primitive(pid=_TEMPLATE_ID, organisation_id=organisation_id, primitive_type="pipeline_template")
    p.name = "PR Review Template"
    p.slug = "pr-review-template"
    p.category = "code-review"
    p.tags = ["pipeline_template"]
    p.content_json = {
        "agents": [{"name": "Agent One"}],
        "graph_nodes": [{"id": "node-1", "node_type": "agent", "label": "Agent One"}],
        "edges": [],
    }
    return p


def _fake_key(role: str = "operator") -> SimpleNamespace:
    return SimpleNamespace(
        name="library-read-key",
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


def _make_route_session() -> AsyncMock:
    """Route session: the strict double stubs the authz kill-switch read.

    Every route CRUD call is patched at the route-module boundary, so the only
    query the session sees is the permission dependency's ``authz_enforce``
    read, which ``configure_mock_session`` serves by default (enforce=True).
    """
    return configure_mock_session(AsyncMock())


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
    route_session: AsyncMock | None = None,
    *,
    key_role: str = "operator",
    grants: frozenset[str] | None = None,
) -> Generator[TestClient, None, None]:
    """Drive the real any-credential stack with an ``mk_`` bearer header."""
    session = route_session if route_session is not None else _make_route_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

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


def _template_page(template: MagicMock) -> MagicMock:
    page = MagicMock()
    page.items = [template]
    page.total = 1
    page.page = 1
    page.page_size = 20
    page.has_more = False
    return page


def _rating_page() -> MagicMock:
    rating = MagicMock()
    rating.id = uuid.uuid4()
    rating.primitive_id = _PRIMITIVE_ID
    rating.account_id = _USER_ID
    rating.thumbs_up = True
    rating.comment = "Great"
    rating.created_at = _NOW
    page = MagicMock()
    page.items = [rating]
    page.total = 1
    return page


# ---------------------------------------------------------------------------
# GET /api/v1/templates
# ---------------------------------------------------------------------------


class TestTemplatesListAcceptsOrgApiKey:
    def test_org_api_key_lists_the_catalogue(self) -> None:
        """(a) org API key + a real template = 200 (was 401 before FAR-2682)."""
        template = _make_template()
        with (
            patch("farna.api.routes.templates.list_templates", new_callable=AsyncMock, return_value=_template_page(template)),
            _api_key_request() as client,
        ):
            resp = client.get("/api/v1/templates", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["id"] == str(_TEMPLATE_ID)

    def test_viewer_role_key_lists_the_catalogue(self) -> None:
        """``pipeline.list`` is viewer-level; a viewer-role key reads it."""
        template = _make_template()
        with (
            patch("farna.api.routes.templates.list_templates", new_callable=AsyncMock, return_value=_template_page(template)),
            _api_key_request(key_role="viewer") as client,
        ):
            resp = client.get("/api/v1/templates", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text

    def test_grant_scoped_key_without_pipeline_list_gets_403(self) -> None:
        """(c) a credential lacking ``pipeline.list`` = 403 (was 401).

        The role floor is ``viewer`` — the lowest org role — so only a
        grant-bearing key whose explicit grant-set omits the permission can
        lack it (FAR-1477: effective = grants INTERSECT the live role bundle).
        """
        template = _make_template()
        with (
            patch("farna.api.routes.templates.list_templates", new_callable=AsyncMock, return_value=_template_page(template)),
            _api_key_request(grants=frozenset({"schema.list"})) as client,
        ):
            resp = client.get("/api/v1/templates", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "pipeline.list" in resp.json()["detail"]

    def test_no_bearer_credential_is_still_401(self) -> None:
        """The any-credential switch widens WHICH kinds resolve, not whether."""
        with (
            patch("farna.api.routes.templates.list_templates", new_callable=AsyncMock, return_value=_template_page(_make_template())),
            _api_key_request() as client,
        ):
            resp = client.get("/api/v1/templates")

        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/libraries/{primitive_id}
# ---------------------------------------------------------------------------


class TestLibraryPrimitiveReadAcceptsOrgApiKey:
    def test_org_api_key_reads_a_real_primitive(self) -> None:
        """(a) org API key + real id = 200 (was 401 before FAR-2682)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            _api_key_request() as client,
        ):
            resp = client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == str(_PRIMITIVE_ID)
        assert resp.json()["name"] == "Devil's Advocate"

    def test_org_api_key_reads_a_builtin_primitive(self) -> None:
        """A built-in lives in the modulo sentinel org and is globally visible."""
        builtin = _make_primitive(organisation_id=BUILTIN_ORG_ID)
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=builtin),
            _api_key_request() as client,
        ):
            resp = client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text

    def test_org_api_key_gets_404_for_an_unknown_id(self) -> None:
        """(b) org API key + unknown id = 404 (was 401, identical to the real id)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=None),
            _api_key_request() as client,
        ):
            resp = client.get(f"/api/v1/libraries/{uuid.uuid4()}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text

    def test_grant_scoped_key_without_the_read_permission_gets_403(self) -> None:
        """(c) a credential lacking ``library.search`` = 403 (was 401)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            _api_key_request(grants=frozenset({"pipeline.list"})) as client,
        ):
            resp = client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "library.search" in resp.json()["detail"]

    def test_cross_org_id_returns_404_not_the_foreign_primitive(self) -> None:
        """(d) an id owned by another org = 404, never the foreign row.

        RLS / the tenant filter normally make the lookup miss; the request-time
        org guard (``_primitive_visible_to``) is the defence-in-depth layer this
        test pins — it fails with 200 if the guard is removed.
        """
        with (
            patch(
                "farna.api.routes.library.get_primitive",
                new_callable=AsyncMock,
                return_value=_make_primitive(organisation_id=_OTHER_ORG_ID),
            ),
            _api_key_request() as client,
        ):
            resp = client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text
        assert "Devil's Advocate" not in resp.text

    def test_no_bearer_credential_is_still_401(self) -> None:
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            _api_key_request() as client,
        ):
            resp = client.get(f"/api/v1/libraries/{_PRIMITIVE_ID}")

        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# GET /api/v1/libraries/{primitive_id}/ratings and /ratings/aggregate
# ---------------------------------------------------------------------------

_RATING_PATHS = [
    f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings",
    f"/api/v1/libraries/{_PRIMITIVE_ID}/ratings/aggregate",
]


class TestLibraryRatingsAcceptOrgApiKey:
    @pytest.mark.parametrize("path", _RATING_PATHS)
    def test_org_api_key_reads_ratings_for_a_real_primitive(self, path: str) -> None:
        """(a) org API key + real id = 200 (was 401 before FAR-2682)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            patch("farna.api.routes.library.list_ratings_for_primitive", new_callable=AsyncMock, return_value=_rating_page()),
            patch("farna.api.routes.library.get_rating_aggregate", new_callable=AsyncMock, return_value=(4.5, 2)),
            _api_key_request() as client,
        ):
            resp = client.get(path, headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize("path", _RATING_PATHS)
    def test_org_api_key_gets_404_for_an_unknown_id(self, path: str) -> None:
        """(b) unknown id = 404 (was 200 with an empty page)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=None),
            patch("farna.api.routes.library.list_ratings_for_primitive", new_callable=AsyncMock, return_value=_rating_page()),
            patch("farna.api.routes.library.get_rating_aggregate", new_callable=AsyncMock, return_value=(4.5, 2)),
            _api_key_request() as client,
        ):
            resp = client.get(path, headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text

    @pytest.mark.parametrize("path", _RATING_PATHS)
    def test_cross_org_id_returns_404_with_no_foreign_body(self, path: str) -> None:
        """(d) a foreign primitive's ratings are never served (was a 200 leak)."""
        with (
            patch(
                "farna.api.routes.library.get_primitive",
                new_callable=AsyncMock,
                return_value=_make_primitive(organisation_id=_OTHER_ORG_ID),
            ),
            patch("farna.api.routes.library.list_ratings_for_primitive", new_callable=AsyncMock, return_value=_rating_page()),
            patch("farna.api.routes.library.get_rating_aggregate", new_callable=AsyncMock, return_value=(4.5, 2)),
            _api_key_request() as client,
        ):
            resp = client.get(path, headers=_AUTH_HEADERS)

        assert resp.status_code == 404, resp.text
        assert "Great" not in resp.text

    @pytest.mark.parametrize("path", _RATING_PATHS)
    def test_grant_scoped_key_without_the_read_permission_gets_403(self, path: str) -> None:
        """(c) a credential lacking ``library.search`` = 403 (was 401)."""
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            patch("farna.api.routes.library.list_ratings_for_primitive", new_callable=AsyncMock, return_value=_rating_page()),
            patch("farna.api.routes.library.get_rating_aggregate", new_callable=AsyncMock, return_value=(4.5, 2)),
            _api_key_request(grants=frozenset({"pipeline.list"})) as client,
        ):
            resp = client.get(path, headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "library.search" in resp.json()["detail"]

    @pytest.mark.parametrize("path", _RATING_PATHS)
    def test_no_bearer_credential_is_still_401(self, path: str) -> None:
        with (
            patch("farna.api.routes.library.get_primitive", new_callable=AsyncMock, return_value=_make_primitive()),
            _api_key_request() as client,
        ):
            resp = client.get(path)

        assert resp.status_code == 401, resp.text
