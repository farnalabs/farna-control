"""Prove-the-fix tests for FAR-2726: an ``mk_`` org API key can READ the run
LIST (``GET /api/v1/runs``), with tenant isolation and team scoping intact.

Before FAR-2726 the list resolved through ``require_permission("run.list")`` —
Bearer JWT only — so an org API key was rejected 401 *before* the handler ran.
The run DETAIL route (``GET /api/v1/runs/{id}``) already accepts the same key
via ``require_permission_any_credential("run.status")``; the list is the same
read family, so the boundary was inverted.

A LIST has no path id, so the ticket's real/unknown-id pair maps onto its
natural equivalent here: a real ``pipeline_id`` filter is 200 (not 401) and an
unknown one is an EMPTY 200 (not 401) — the route's contract has no 404. The
record-route module (``test_pipeline_record_read_any_credential.py``) pins the
404 cases exactly.

These tests drive the REAL ``require_permission_any_credential`` chain,
including ``get_current_tenant_user_or_api_key``'s ``mk_`` key resolution, so
the credential is a genuine ``Authorization: Bearer mk_...`` header.

They also PIN the scoping the swap must not bypass:

* the organisation predicate is always the caller's org (cross-tenant reads
  impossible);
* the viewer/account clause is ROLE-based, so a viewer-level key is bounded to
  its owner's runs exactly as a viewer JWT is;
* ``_do_list_runs`` now threads the principal's ``team_id`` into the shared
  team filter. Without that threading a team-scoped key would have read every
  team's runs — a boundary every other run surface already enforces.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
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

_KEY = "mk_12345678_" + "x" * 32
_AUTH_HEADERS = {"Authorization": f"Bearer {_KEY}"}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
    )


def _fake_key(role: str = "operator", *, team_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        name="run-list-key",
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
    result = MagicMock()
    result.scalar_one_or_none.return_value = _fake_key(role, team_id=team_id)
    session.execute = AsyncMock(return_value=result)
    return session


def _make_route_session() -> AsyncMock:
    """Route session: every query answers the empty shape (an empty run page).

    The auth chain and the handler's scope resolution run for real; only the
    row store is empty, so the tests assert the SCOPE the handler asked for
    rather than seeded rows (``db_list_runs`` is patched and inspected).
    """
    return configure_mock_session(AsyncMock(), allow_empty_execute=True)


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


class _RouteFactory:
    """``async_sessionmaker`` stand-in for the LIST handler's own session.

    ``list_runs_endpoint`` opens its session from ``_get_session_factory`` (not
    the request-scoped ``get_db_session``), so the route double must be handed
    over through that dependency.
    """

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> "_RouteFactory":
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *_args: object) -> bool:
        return False


class _EmptyPage:
    """Minimal ``PageResult`` stand-in: an empty runs page."""

    def __init__(self) -> None:
        self.items: list[Any] = []
        self.total = 0
        self.page = 1
        self.page_size = 20
        self.next_cursor = None
        self.has_more = False


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
    app.dependency_overrides[_get_session_factory] = lambda: _RouteFactory(route_session)
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


@contextmanager
def _patched_list_runs() -> Generator[AsyncMock, None, None]:
    """Patch the route's CRUD call so the test can inspect the scope it got."""
    with patch("farna.api.routes.runs.db_list_runs", new=AsyncMock(return_value=_EmptyPage())) as list_mock:
        yield list_mock


class TestRunListReadAcceptsOrgApiKey:
    def test_org_api_key_lists_runs(self) -> None:
        """(a) org API key + list request = 200 (was 401 before FAR-2726)."""
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session) as client:
            resp = client.get("/api/v1/runs", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 0
        kwargs = list_mock.await_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID

    def test_org_api_key_pipeline_filter_with_a_real_id_is_200(self) -> None:
        """A real ``pipeline_id`` filter is 200, not 401."""
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/runs?pipeline_id={_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert list_mock.await_args.kwargs["pipeline_id"] == _PIPELINE_ID

    def test_org_api_key_unknown_pipeline_filter_is_empty_200_not_401(self) -> None:
        """An unknown id is an empty 200 — absence is observable, unlike a 401.

        (A list has no by-id 404; 401 for every filter value is exactly the
        confusion FAR-2726 removes. The record route's 404 case is pinned in the
        sibling module.)
        """
        route_session = _make_route_session()
        unknown = uuid.uuid4()
        with _patched_list_runs() as list_mock, _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/runs?pipeline_id={unknown}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 0
        assert list_mock.await_args.kwargs["pipeline_id"] == unknown

    def test_grant_scoped_key_without_the_read_permission_gets_403(self) -> None:
        """(c) a credential lacking ``run.list`` = 403 (was 401).

        ``run.list`` resolves to ``viewer`` — the lowest org role — so the only
        credential that can lack it is a grant-bearing API key whose explicit
        grant-set omits it.
        """
        route_session = _make_route_session()
        with _patched_list_runs(), _api_key_request(route_session, grants=frozenset({"run.status"})) as client:
            resp = client.get("/api/v1/runs", headers=_AUTH_HEADERS)

        assert resp.status_code == 403, resp.text
        assert "run.list" in resp.json()["detail"]

    def test_cross_org_pipeline_filter_cannot_return_foreign_runs(self) -> None:
        """A foreign-org pipeline filter still reads the CALLER's org only.

        The organisation predicate is mandatory in the shared CRUD filter and
        the route passes the principal's org unconditionally, so a foreign id
        cannot match a row even if RLS were misconfigured. Here the page is
        empty and the scope the handler passed names the caller's org — never
        the foreign one.
        """
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session) as client:
            resp = client.get(f"/api/v1/runs?pipeline_id={_PIPELINE_ID}", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert not resp.json()["items"]
        kwargs = list_mock.await_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID
        assert kwargs["organisation_id"] != _OTHER_ORG_ID


class TestRunListScopingSurvivesTheSwap:
    def test_team_scoped_key_is_bounded_to_its_own_team(self) -> None:
        """A team-scoped key's hard boundary reaches the shared team filter.

        This is the security half of the swap: accepting mk_ keys WITHOUT
        threading ``team_id`` would let a team-scoped key enumerate every
        team's runs (runs carry only org-level RLS). ``_runs_list_scope_filters``
        turns this into ``team_scope_clause(effective_owner, team_id)``.
        """
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session, key_role="operator", team_id=_TEAM_A) as client:
            resp = client.get("/api/v1/runs", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert list_mock.await_args.kwargs["team_id"] == _TEAM_A

    def test_org_wide_key_carries_no_team_filter(self) -> None:
        """An org-wide key (team_id=None) keeps the pre-existing org-wide read."""
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session, key_role="operator") as client:
            resp = client.get("/api/v1/runs", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert list_mock.await_args.kwargs["team_id"] is None
        assert list_mock.await_args.kwargs["account_id"] is None

    def test_viewer_level_key_keeps_the_account_clause(self) -> None:
        """The viewer/account clause is ROLE-based, so a viewer key is bounded.

        ``_do_list_runs`` derives ``account_id`` from ``org_role``, not from the
        principal's class — an API-key principal with the viewer role gets the
        same own-runs restriction as a viewer JWT. This pins that the swap does
        not widen a viewer credential.
        """
        route_session = _make_route_session()
        with _patched_list_runs() as list_mock, _api_key_request(route_session, key_role="viewer") as client:
            resp = client.get("/api/v1/runs", headers=_AUTH_HEADERS)

        assert resp.status_code == 200, resp.text
        assert list_mock.await_args.kwargs["account_id"] == _USER_ID
        assert list_mock.await_args.kwargs["team_id"] is None


class TestCredentialBoundaryStillEnforced:
    def test_no_bearer_credential_is_still_401(self) -> None:
        """The list is still authenticated: no credential = 401."""
        route_session = _make_route_session()
        with _patched_list_runs(), _api_key_request(route_session) as client:
            resp = client.get("/api/v1/runs")

        assert resp.status_code == 401, resp.text
