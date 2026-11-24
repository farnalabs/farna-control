"""Team-boundary enforcement across the REST run-detail family (ADR 070 §4).

``runs`` carries only the org-isolation RLS policy and no ``visibility``
column, so a run's team boundary is application-layer. The list and stats
surfaces clamp in SQL; before this change the per-run reads did not, so a
holder of a team-scoped API key — or a viewer — could read another team's run
detail, IO, logs, node outputs, reconstructed prompts, artifact pointers and
side-car transcripts, its live events, and subscribe to its WebSocket stream.

Every per-run read now routes through one shared clamp
(:func:`farna.api.run_scope.run_visible_to_principal`) applying the two ADR 070
clauses — team-scoped credential boundary and viewer membership — with the
organisation admin exempt. A refusal is **404**, never 403, so a run's
existence is not confirmed outside its boundary.

These tests pin, per surface, that a foreign-team run is reported NOT FOUND,
and pin the shared decision matrix (own team, org-level run, pre-stamp
fallback, viewer with/without membership, admin) at the loader boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from farna.api import run_scope
from farna.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes import run_ws
from farna.api.routes import runs as runs_module
from farna.api.routes.runs import RunNotFoundError
from farna.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.db.crud.team_scope import BlindPipelineScope
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_RUN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _blind_scope(owner_team_id: uuid.UUID | None) -> BlindPipelineScope | None:
    """The team-blind read's answer for an EXISTING pipeline row.

    A pipeline with no owner team is ORG-LEVEL (``owner_team_id=None``,
    ``visibility='org'``), which is a different thing from "no pipeline row".
    The unresolved case is fail-closed and is exercised explicitly by the tests
    that patch the resolver to return ``None``.
    """
    return BlindPipelineScope(
        owner_team_id=owner_team_id,
        visibility="org" if owner_team_id is None else "team",
    )


def _make_run(*, owner_team_id: uuid.UUID | None = _TEAM_B, run_id: uuid.UUID = _RUN_ID) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.pipeline_id = _PIPELINE_ID
    run.owner_team_id = owner_team_id
    run.status = "complete"
    run.organisation_id = _ORG_ID
    return run


def _team_scoped_key(*, role: str = "runner") -> TenantPrincipal:
    """A team-A-scoped API key: the hard team boundary rides on ``team_id``."""
    return TenantPrincipal(
        username="team-a-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role=role,
        team_id=_TEAM_A,
        via_api_key=True,
    )


def _org_wide_key(*, role: str = "runner") -> TenantPrincipal:
    """An org-wide API key (``team_id=None``) keeps its org-level reach."""
    return TenantPrincipal(
        username="org-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role=role,
        via_api_key=True,
    )


def _viewer() -> TenantPrincipal:
    return TenantPrincipal(username="viewer", organisation_id=_ORG_ID, account_id=_ACCOUNT_ID, org_role="viewer")


def _admin() -> TenantPrincipal:
    return TenantPrincipal(username="admin", organisation_id=_ORG_ID, account_id=_ACCOUNT_ID, org_role="admin")


def _result(scalar_one_or_none: object = None, rows: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    result.first = MagicMock(return_value=None)
    result.scalar = MagicMock(return_value=None)
    result.all = MagicMock(return_value=rows if rows is not None else [])
    result.scalars.return_value.all = MagicMock(return_value=rows if rows is not None else [])
    return result


def _decision_session() -> AsyncMock:
    """A session double for the decision matrix (no query is issued for it)."""
    return configure_mock_session(AsyncMock(), allow_empty_execute=True)


class _Factory:
    """``async_sessionmaker`` stand-in: calling it yields one fixed session."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _Factory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


# ---------------------------------------------------------------------------
# The shared predicate
# ---------------------------------------------------------------------------


class TestTeamBoundaryPredicate:
    """``team_boundary_blocked`` — the one predicate both surfaces share."""

    def test_foreign_team_owner_blocks_a_team_bound_credential(self) -> None:
        assert run_scope.team_boundary_blocked(_TEAM_A, _TEAM_B)

    def test_own_team_owner_is_allowed(self) -> None:
        assert not run_scope.team_boundary_blocked(_TEAM_A, _TEAM_A)

    def test_org_level_resource_is_visible_to_every_team(self) -> None:
        assert not run_scope.team_boundary_blocked(_TEAM_A, None)

    def test_unbound_credential_is_never_blocked(self) -> None:
        assert not run_scope.team_boundary_blocked(None, _TEAM_B)


# ---------------------------------------------------------------------------
# The two-clause decision
# ---------------------------------------------------------------------------


class TestRunScopeDecision:
    """``run_visible_to_principal`` on the run family."""

    @pytest.mark.asyncio
    async def test_team_scoped_key_cannot_see_another_teams_run(self) -> None:
        visible = await run_scope.run_visible_to_principal(_decision_session(), _team_scoped_key(), _make_run(owner_team_id=_TEAM_B))

        assert visible is False

    @pytest.mark.asyncio
    async def test_team_scoped_key_sees_its_own_teams_run(self) -> None:
        visible = await run_scope.run_visible_to_principal(_decision_session(), _team_scoped_key(), _make_run(owner_team_id=_TEAM_A))

        assert visible is True

    @pytest.mark.asyncio
    async def test_team_scoped_key_sees_an_org_level_run(self) -> None:
        with patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))):
            visible = await run_scope.run_visible_to_principal(_decision_session(), _team_scoped_key(), _make_run(owner_team_id=None))

        assert visible is True

    @pytest.mark.asyncio
    async def test_pre_stamp_run_falls_back_to_the_pipeline_owner(self) -> None:
        """A NULL run stamp resolves from the pipeline, never to org-level."""
        with patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=BlindPipelineScope(owner_team_id=_TEAM_B, visibility="team"))) as owner:
            visible = await run_scope.run_visible_to_principal(_decision_session(), _team_scoped_key(), _make_run(owner_team_id=None))

        assert visible is False
        owner.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stamped_owner_wins_over_the_pipeline_owner(self) -> None:
        """The run's stamped owner is authoritative — no fallback read at all."""
        with patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=BlindPipelineScope(owner_team_id=_TEAM_B, visibility="team"))) as owner:
            visible = await run_scope.run_visible_to_principal(_decision_session(), _team_scoped_key(), _make_run(owner_team_id=_TEAM_A))

        assert visible is True
        owner.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_org_wide_key_is_unaffected_by_the_team_clause(self) -> None:
        visible = await run_scope.run_visible_to_principal(_decision_session(), _org_wide_key(), _make_run(owner_team_id=_TEAM_B))

        assert visible is True

    @pytest.mark.asyncio
    async def test_viewer_without_membership_cannot_see_the_run(self) -> None:
        with patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)) as membership:
            visible = await run_scope.run_visible_to_principal(_decision_session(), _viewer(), _make_run(owner_team_id=_TEAM_B))

        assert visible is False
        membership.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_viewer_with_membership_sees_the_run(self) -> None:
        with patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=True)):
            visible = await run_scope.run_visible_to_principal(_decision_session(), _viewer(), _make_run(owner_team_id=_TEAM_B))

        assert visible is True

    @pytest.mark.asyncio
    async def test_viewer_sees_an_org_level_run_without_a_membership_read(self) -> None:
        with (
            patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))),
            patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)) as membership,
        ):
            visible = await run_scope.run_visible_to_principal(_decision_session(), _viewer(), _make_run(owner_team_id=None))

        assert visible is True
        membership.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_is_exempt_from_both_clauses(self) -> None:
        visible = await run_scope.run_visible_to_principal(_decision_session(), _admin(), _make_run(owner_team_id=_TEAM_B))

        assert visible is True


# ---------------------------------------------------------------------------
# The loaders every surface routes through
# ---------------------------------------------------------------------------


def _loader_session(run: Any) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=run))
    return session


async def _load_with_gate(run: Any, principal: AuthenticatedPrincipal) -> tuple[Any, bool]:
    session = _loader_session(run)
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch.object(runs_module, "_run_gate_fired", AsyncMock(return_value=False)),
    ):
        return await runs_module._do_get_run_with_gate(_Factory(session), principal, _RUN_ID)  # type: ignore[arg-type]


async def _load_events(run: Any, principal: AuthenticatedPrincipal) -> Any:
    session = _loader_session(run)
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
    ):
        return await runs_module._do_get_run(_Factory(session), principal, _RUN_ID)  # type: ignore[arg-type]


async def _load_in_scope(run: Any, principal: AuthenticatedPrincipal) -> Any:
    session = _loader_session(run)
    return await runs_module._load_run_in_scope(session, principal, _RUN_ID)  # type: ignore[arg-type]


class TestDetailLoadersApplyTheClamp:
    """The three loaders the surface handlers use apply the same decision."""

    @pytest.mark.asyncio
    async def test_detail_loader_refuses_a_foreign_team_run(self) -> None:
        with pytest.raises(RunNotFoundError):
            await _load_with_gate(_make_run(owner_team_id=_TEAM_B), _team_scoped_key())

    @pytest.mark.asyncio
    async def test_detail_loader_returns_its_own_teams_run(self) -> None:
        run = _make_run(owner_team_id=_TEAM_A)

        loaded, gate_fired = await _load_with_gate(run, _team_scoped_key())

        assert loaded is run
        assert gate_fired is False

    @pytest.mark.asyncio
    async def test_detail_loader_refuses_an_out_of_scope_viewer(self) -> None:
        with (
            patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)),
            pytest.raises(RunNotFoundError),
        ):
            await _load_with_gate(_make_run(owner_team_id=_TEAM_B), _viewer())

    @pytest.mark.asyncio
    async def test_events_loader_refuses_a_foreign_team_run(self) -> None:
        with pytest.raises(RunNotFoundError):
            await _load_events(_make_run(owner_team_id=_TEAM_B), _team_scoped_key())

    @pytest.mark.asyncio
    async def test_events_loader_returns_an_org_level_run(self) -> None:
        run = _make_run(owner_team_id=None)
        with patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))):
            loaded = await _load_events(run, _team_scoped_key())

        assert loaded is run

    @pytest.mark.asyncio
    async def test_child_reader_loader_refuses_a_foreign_team_run(self) -> None:
        loaded = await _load_in_scope(_make_run(owner_team_id=_TEAM_B), _team_scoped_key())

        assert loaded is None

    @pytest.mark.asyncio
    async def test_child_reader_loader_returns_its_own_teams_run(self) -> None:
        run = _make_run(owner_team_id=_TEAM_A)

        loaded = await _load_in_scope(run, _team_scoped_key())

        assert loaded is run

    @pytest.mark.asyncio
    async def test_child_reader_loader_returns_an_org_level_run(self) -> None:
        run = _make_run(owner_team_id=None)
        with patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))):
            loaded = await _load_in_scope(run, _team_scoped_key())

        assert loaded is run


# ---------------------------------------------------------------------------
# Every surface refuses a foreign-team run (route level, 404)
# ---------------------------------------------------------------------------

#: ``(method, path, json body, expected refusal detail)`` for every run-detail
#: read surface. The detail pins that the refusal came from the run's own
#: not-found path, not from an incidental missing sub-resource.
_SURFACES: list[tuple[str, str, dict[str, Any] | None, str]] = [
    ("GET", f"/api/v1/runs/{_RUN_ID}", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/io", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/work-items/enrichment", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/export-fixture", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/workspace-events", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/logs", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/nodes/node-a/output", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/attempts", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/events", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/schema-enforcement", None, "Run not found"),
    ("POST", f"/api/v1/runs/{_RUN_ID}/nodes/node-a/prompt/reveal", None, "Run not found"),
    ("GET", f"/api/v1/runs/{_RUN_ID}/nodes/node-a/artifacts", None, "Run not found"),
    (
        "GET",
        f"/api/v1/runs/{_RUN_ID}/nodes/node-a/attempts/1/artifacts/stdout",
        None,
        "Artifact stream not found",
    ),
    (
        "POST",
        "/api/v1/runs/diff",
        {
            "run_id_a": str(_RUN_ID),
            "node_id_a": "node-a",
            "run_id_b": str(_RUN_ID),
            "node_id_b": "node-b",
        },
        f"Run {_RUN_ID} not found",
    ),
]


def _boundary_session(run: MagicMock) -> AsyncMock:
    """A session that answers the run load with *run* and everything else empty."""
    session = configure_mock_session(AsyncMock())
    authz = _result()
    run_result = _result(scalar_one_or_none=run)

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        if "authz_enforce" in str(stmt):
            return authz
        return run_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _artifact_session(run: MagicMock, artifact_row: MagicMock) -> AsyncMock:
    """Answer the run load with *run* and any run_node_outputs read with *row*.

    Order-independent on purpose: the fixed code loads the run first (and the
    clamp refuses before the artifact read), while the pre-fix code issues only
    the artifact read — so the same session proves either behaviour.
    """
    session = configure_mock_session(AsyncMock())
    authz = _result()
    run_result = _result(scalar_one_or_none=run)
    artifact_result = _result(scalar_one_or_none=artifact_row, rows=[artifact_row])

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        text = str(stmt).lower()
        if "authz_enforce" in text:
            return authz
        if "from run_node_outputs" in text:
            return artifact_result
        return run_result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _artifact_pointer() -> dict[str, Any]:
    return {
        "stream": "stdout",
        "rel_path": "org/run/node/attempt.stdout.zst",
        "size_bytes": 5,
        "sha256": "x",
        "compression": "zstd",
    }


def _install_overrides(session: AsyncMock, principal: TenantPrincipal) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: _Factory(session)
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


@pytest.fixture
def foreign_team_client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _boundary_session(_make_run(owner_team_id=_TEAM_B))
    _install_overrides(session, _team_scoped_key())
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
    ):
        yield TestClient(app), session
    app.dependency_overrides.clear()


@pytest.mark.parametrize(("method", "path", "body", "detail"), _SURFACES)
def test_every_surface_refuses_a_foreign_team_run(
    foreign_team_client: tuple[TestClient, AsyncMock],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    detail: str,
) -> None:
    """A team-A key reading team-B's run gets 404 on every surface.

    The refusal is NOT FOUND rather than a boundary error, so a caller cannot
    distinguish "another team's run" from "no such run". The detail pins that
    the refusal came from the run itself (not from a missing sub-resource).
    """
    http, _session = foreign_team_client
    response = http.request(method, path, json=body)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == detail


def test_artifact_listing_returns_no_pointers_for_a_foreign_team_run() -> None:
    """The listing must not serve another team's pointers when rows exist.

    Before the clamp this surface answered 200 with the foreign run's artifact
    pointers: it never loaded the run at all, so org-only RLS was the only
    boundary. Serving an artifact row proves the refusal is the run clamp, not
    an empty result.
    """
    artifact_row = MagicMock(attempt_key="run:x:node:node-a:1", artifacts_json=[_artifact_pointer()])
    session = _artifact_session(_make_run(owner_team_id=_TEAM_B), artifact_row)
    _install_overrides(session, _team_scoped_key())
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
    ):
        response = TestClient(app).get(f"/api/v1/runs/{_RUN_ID}/nodes/node-a/artifacts")
    app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Run not found"


def test_artifact_download_never_serves_a_foreign_teams_transcript() -> None:
    """The side-car download must not stream another team's stdout.

    Before the clamp the pointer row was read and the transcript streamed back
    as 200; now the run clamp refuses before any pointer is touched.
    """
    artifact_row = MagicMock(attempt_key="1", artifacts_json=[_artifact_pointer()])
    session = _artifact_session(_make_run(owner_team_id=_TEAM_B), artifact_row)
    _install_overrides(session, _team_scoped_key())
    store = MagicMock()
    store.read_bytes = MagicMock(return_value=b"secret transcript")
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch("farna.core.artifacts.store.get_store", return_value=store),
    ):
        response = TestClient(app).get(f"/api/v1/runs/{_RUN_ID}/nodes/node-a/attempts/1/artifacts/stdout")
    app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert store.read_bytes.call_count == 0


@pytest.mark.asyncio
async def test_rerun_refuses_a_foreign_team_run() -> None:
    """``POST /runs/{id}/rerun`` must not clone a run outside the key's team."""
    from fastapi import HTTPException

    session = _boundary_session(_make_run(owner_team_id=_TEAM_B))
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        pytest.raises(HTTPException) as denied,
    ):
        await runs_module.trigger_rerun(
            run_id=_RUN_ID,
            session=session,
            _engine=MagicMock(),
            principal=_team_scoped_key(),
        )

    assert denied.value.status_code == 404


# ---------------------------------------------------------------------------
# An in-scope run stays readable (the clamp must open, not just close)
# ---------------------------------------------------------------------------


def _get_attempts(
    principal: TenantPrincipal,
    run: MagicMock,
    *,
    pipeline_owner: uuid.UUID | None = None,
    membership: bool = False,
) -> Any:
    """GET the attempts surface with the clamp's inputs pinned."""
    session = _boundary_session(run)
    _install_overrides(session, principal)
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(pipeline_owner))),
        patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=membership)),
    ):
        response = TestClient(app).get(f"/api/v1/runs/{_RUN_ID}/attempts")
    app.dependency_overrides.clear()
    return response


def test_key_sees_its_own_teams_run() -> None:
    """Same team-scoped key, own team's run: the surface answers 200."""
    response = _get_attempts(_team_scoped_key(), _make_run(owner_team_id=_TEAM_A))

    assert response.status_code == 200, response.text


def test_key_sees_an_org_level_run() -> None:
    """Org-level runs (no effective owner) stay visible to every team key."""
    response = _get_attempts(_team_scoped_key(), _make_run(owner_team_id=None), pipeline_owner=None)

    assert response.status_code == 200, response.text


def test_viewer_with_membership_sees_the_run() -> None:
    response = _get_attempts(_viewer(), _make_run(owner_team_id=_TEAM_B), membership=True)

    assert response.status_code == 200, response.text


def test_viewer_without_membership_gets_a_404() -> None:
    response = _get_attempts(_viewer(), _make_run(owner_team_id=_TEAM_B), membership=False)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Run not found"


def test_admin_sees_any_run_in_the_organisation() -> None:
    response = _get_attempts(_admin(), _make_run(owner_team_id=_TEAM_B))

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# The WebSocket stream applies the same clamp
# ---------------------------------------------------------------------------


def _ws_principal() -> AuthenticatedPrincipal:
    """The ws-token principal shape (browser JWT: no team binding)."""
    return AuthenticatedPrincipal(
        username="viewer",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="viewer",
    )


def _ws_session(run: Any) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=run))
    return session


class TestRunWebSocketAppliesTheClamp:
    """``_load_run_with_rls`` bounds the stream exactly like the REST readers.

    A ws-token is minted only from a browser-JWT session, so the credential
    clause cannot reach this surface; the viewer membership clause is the live
    one, and an out-of-scope viewer must never subscribe to the run's events.
    """

    @pytest.mark.asyncio
    async def test_viewer_outside_the_owning_team_is_not_loaded(self) -> None:
        session = _ws_session(_make_run(owner_team_id=_TEAM_B))
        with (
            patch.object(run_ws, "set_rls_org", AsyncMock()),
            patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)),
        ):
            loaded = await run_ws._load_run_with_rls(_Factory(session), _ws_principal(), _RUN_ID)  # type: ignore[arg-type]

        assert loaded is None

    @pytest.mark.asyncio
    async def test_viewer_inside_the_owning_team_is_loaded(self) -> None:
        run = _make_run(owner_team_id=_TEAM_A)
        session = _ws_session(run)
        with (
            patch.object(run_ws, "set_rls_org", AsyncMock()),
            patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=True)),
        ):
            loaded = await run_ws._load_run_with_rls(_Factory(session), _ws_principal(), _RUN_ID)  # type: ignore[arg-type]

        assert loaded is run
