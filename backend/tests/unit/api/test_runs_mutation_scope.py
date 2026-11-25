"""Team-boundary enforcement across the per-run MUTATION family (ADR 070 ��4).

``runs`` carries only the org-isolation RLS policy and no ``visibility``
column, so a run's team boundary is application-layer. #3442 clamped the
per-run reads; the per-run mutations still loaded a run with org scope alone,
so a holder of a team-scoped API key could cancel, observe, recover, override
a guardrail block on, or attach feedback to another team's run.

Every per-run mutation now routes through the same leaf predicate the read
family uses (:func:`farna.api.run_scope.load_run_in_scope` /
``run_visible_to_principal``) BEFORE it acts. A refusal is **404**, never 403,
so a run's existence is not confirmed outside its boundary; an organisation
admin stays exempt.

Each test below pins a refusal AND that the action did not happen: the core
writer is patched with an ``AsyncMock`` and asserted never awaited, so a 404
that still cancelled the run cannot pass.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from farna.api import run_scope
from farna.api.dependencies import _get_engine, _get_session_factory, deny_break_glass_mint, get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes import evals as evals_module
from farna.api.routes import feedback as feedback_module
from farna.api.routes import runs as runs_module
from farna.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from farna.auth.jwt import TenantPrincipal
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

_MSG_RUN_NOT_FOUND = "Run not found"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _blind_scope(owner_team_id: uuid.UUID | None) -> BlindPipelineScope:
    return BlindPipelineScope(
        owner_team_id=owner_team_id,
        visibility="org" if owner_team_id is None else "team",
    )


def _make_run(
    *,
    owner_team_id: uuid.UUID | None = _TEAM_B,
    status: str = "awaiting_human",
    run_id: uuid.UUID = _RUN_ID,
) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.pipeline_id = _PIPELINE_ID
    run.owner_team_id = owner_team_id
    run.status = status
    run.organisation_id = _ORG_ID
    return run


def _team_scoped_key(*, role: str = "operator") -> TenantPrincipal:
    """A team-A-scoped API key: the hard team boundary rides on ``team_id``."""
    return TenantPrincipal(
        username="team-a-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role=role,
        team_id=_TEAM_A,
        via_api_key=True,
    )


def _viewer() -> TenantPrincipal:
    return TenantPrincipal(username="viewer", organisation_id=_ORG_ID, account_id=_ACCOUNT_ID, org_role="viewer")


def _admin() -> TenantPrincipal:
    return TenantPrincipal(username="admin", organisation_id=_ORG_ID, account_id=_ACCOUNT_ID, org_role="admin")


def _result(scalar_one_or_none: object = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    result.first = MagicMock(return_value=None)
    result.scalar = MagicMock(return_value=None)
    result.all = MagicMock(return_value=[])
    result.scalars.return_value.all = MagicMock(return_value=[])
    return result


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


class _Factory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _Factory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


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
    app.dependency_overrides[deny_break_glass_mint] = lambda: None


# ---------------------------------------------------------------------------
# Every per-run mutation surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Writer:
    owner: Any
    attr: str
    return_value: Any = None


@dataclass(frozen=True)
class _Surface:
    name: str
    path: str
    body: dict[str, Any] | None
    expected_status: int
    writers: tuple[_Writer, ...]
    #: A run status that lets the success path complete without extra work.
    success_status: str = "awaiting_human"
    #: Extra patches applied around the request: ``(owner, attr, value)``.
    support: tuple[tuple[Any, str, Any], ...] = field(default_factory=tuple)


def _success_run(status: str) -> MagicMock:
    return _make_run(status=status)


def _feedback_record(run: MagicMock) -> MagicMock:
    record = MagicMock()
    record.id = uuid.uuid4()
    record.run_id = run.id
    record.review_id = "review-1"
    record.account_id = _ACCOUNT_ID
    record.rejection_reason = "wrong"
    record.feedback_status = "pending"
    record.feedback_handler_type = "human_review"
    record.eval_gap = None
    record.correction_run_id = None
    return record


def _rls_support(module: Any) -> tuple[tuple[Any, str, Any], ...]:
    return (
        (module, "set_rls_org", AsyncMock()),
        (module, "set_rls_user_context", AsyncMock()),
    )


def _cancel_surface() -> _Surface:
    return _Surface(
        name="cancel",
        path=f"/api/v1/runs/{_RUN_ID}/cancel",
        body=None,
        expected_status=202,
        writers=(_Writer(runs_module, "request_cancellation"),),
        support=(
            *_rls_support(runs_module),
            (runs_module, "set_mutation_row_lock_timeout", AsyncMock()),
        ),
    )


def _observe_surface() -> _Surface:
    return _Surface(
        name="observe",
        path=f"/api/v1/runs/{_RUN_ID}/nodes/node-a/observe",
        body=None,
        expected_status=200,
        writers=(_Writer(runs_module, "observe_node", MagicMock(human_observed_at=None, account_id=None)),),
        support=_rls_support(runs_module),
    )


def _recover_surface() -> _Surface:
    def _recovered() -> MagicMock:
        return _make_run(status="running")

    return _Surface(
        name="recover",
        path=f"/api/v1/runs/{_RUN_ID}/nodes/node-a/recover",
        body={"input_data": {"value": "x"}},
        expected_status=200,
        writers=(
            _Writer(runs_module, "recover_node", _recovered()),
            _Writer(runs_module, "dispatch_run", ("enqueued", "job-1")),
        ),
        support=_rls_support(runs_module),
    )


def _guardrail_surface() -> _Surface:
    return _Surface(
        name="guardrail-override",
        path=f"/api/v1/runs/{_RUN_ID}/guardrail-override",
        body={"input_data": {"value": "x"}},
        expected_status=200,
        writers=(
            _Writer(runs_module, "guardrail_override", _make_run(status="pending")),
            _Writer(runs_module, "dispatch_run", ("enqueued", "job-1")),
        ),
        support=(
            *_rls_support(runs_module),
            (runs_module._guardrail_override_rate_limiter, "consume", AsyncMock(return_value=True)),
        ),
    )


def _feedback_surface() -> _Surface:
    return _Surface(
        name="feedback",
        path=f"/api/v1/runs/{_RUN_ID}/feedback",
        body={
            "review_id": "review-1",
            "rejection_reason": "wrong output",
            "producing_node_id": "node-a",
            "feedback_handler_type": "human_review",
        },
        expected_status=201,
        writers=(
            _Writer(feedback_module.FeedbackManager, "create_feedback_record", _feedback_record(_make_run())),
            _Writer(feedback_module, "append_audit_event_isolated"),
        ),
        support=_rls_support(feedback_module),
    )


_SURFACE_BUILDERS = [
    _cancel_surface,
    _observe_surface,
    _recover_surface,
    _guardrail_surface,
    _feedback_surface,
]

_SURFACE_PARAMS = pytest.mark.parametrize("surface_builder", _SURFACE_BUILDERS, ids=lambda f: f.__name__)


class _RequestOutcome:
    def __init__(self, response: Any, writers: dict[str, AsyncMock], run_reader: AsyncMock) -> None:
        self.response = response
        self.writers = writers
        self.run_reader = run_reader


def _call_surface(surface: _Surface, principal: TenantPrincipal, run: MagicMock) -> _RequestOutcome:
    """Issue the surface request with the run load answered by *run*.

    The core writer(s) and the run reader are AsyncMocks so a test can prove
    both ``404`` and "the action did not happen". ``get_run`` is patched on the
    route module that owns the shared loader's ``fetch`` seam.
    """
    session = _boundary_session(run)
    _install_overrides(session, principal)
    writers: dict[str, AsyncMock] = {}
    reader_module = feedback_module if surface.name == "feedback" else runs_module
    run_reader = AsyncMock(return_value=run)

    with ExitStack() as stack:
        for writer in surface.writers:
            mock = AsyncMock(return_value=writer.return_value)
            writers[writer.attr] = mock
            stack.enter_context(patch.object(writer.owner, writer.attr, mock))
        for owner, attr, value in surface.support:
            stack.enter_context(patch.object(owner, attr, value))
        # ``create=True`` keeps the same test meaningful against the PRE-FIX
        # code: ``feedback`` had no ``get_run`` seam to patch, so without it the
        # refusal cases would fail on the patch itself rather than on the
        # missing clamp.
        stack.enter_context(patch.object(reader_module, "get_run", run_reader, create=True))
        stack.enter_context(patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))))
        response = TestClient(app).post(surface.path, json=surface.body)

    app.dependency_overrides.clear()
    return _RequestOutcome(response, writers, run_reader)


# ---------------------------------------------------------------------------
# A team-A key acting on team-B's run is NOT FOUND and the action NEVER runs
# ---------------------------------------------------------------------------


@_SURFACE_PARAMS
def test_a_foreign_team_run_is_not_found_and_the_action_never_runs(
    surface_builder: Any,
) -> None:
    """The exploit: a team-A key could cancel/observe/recover/etc. team B's run.

    Before the clamp the run loaded with org scope alone, so every writer below
    was invoked. Now the refusal is NOT FOUND and each writer is never awaited.
    """
    surface = surface_builder()
    run = _make_run(owner_team_id=_TEAM_B, status=surface.success_status)

    outcome = _call_surface(surface, _team_scoped_key(), run)

    assert outcome.response.status_code == 404, outcome.response.text
    assert outcome.response.json()["detail"] == _MSG_RUN_NOT_FOUND
    for attr, writer in outcome.writers.items():
        assert not writer.await_count, f"{surface.name}: {attr} ran for a foreign-team run"


# ---------------------------------------------------------------------------
# In-boundary callers keep working: own team, org-level run, admin
# ---------------------------------------------------------------------------


@_SURFACE_PARAMS
def test_a_team_key_can_act_on_its_own_teams_run(surface_builder: Any) -> None:
    surface = surface_builder()
    run = _make_run(owner_team_id=_TEAM_A, status=surface.success_status)

    outcome = _call_surface(surface, _team_scoped_key(), run)

    assert outcome.response.status_code == surface.expected_status, outcome.response.text
    assert all(writer.await_count for writer in outcome.writers.values())


@_SURFACE_PARAMS
def test_an_org_level_run_is_visible_to_every_team(surface_builder: Any) -> None:
    surface = surface_builder()
    run = _make_run(owner_team_id=None, status=surface.success_status)

    outcome = _call_surface(surface, _team_scoped_key(), run)

    assert outcome.response.status_code == surface.expected_status, outcome.response.text


@_SURFACE_PARAMS
def test_admin_is_exempt_from_the_team_boundary(surface_builder: Any) -> None:
    surface = surface_builder()
    run = _make_run(owner_team_id=_TEAM_B, status=surface.success_status)

    outcome = _call_surface(surface, _admin(), run)

    assert outcome.response.status_code == surface.expected_status, outcome.response.text


# ---------------------------------------------------------------------------
# A viewer cannot reach ANY per-run mutation, and never reads the run
# ---------------------------------------------------------------------------


@_SURFACE_PARAMS
def test_a_viewer_is_refused_by_the_role_gate_before_any_run_read(
    surface_builder: Any,
) -> None:
    """These mutations are runner/operator-level, so a viewer is refused 403.

    The viewer clause of the shared predicate is live on the read family; on
    the mutation family the org-role floor refuses first, and the assertion
    that ``get_run`` was never awaited pins that a viewer (with or without a
    team membership) cannot read, let alone act on, any run here. 403 is not an
    existence oracle: it is returned for every run id.
    """
    surface = surface_builder()
    run = _make_run(owner_team_id=_TEAM_B, status=surface.success_status)

    outcome = _call_surface(surface, _viewer(), run)

    assert outcome.response.status_code == 403, outcome.response.text
    assert not outcome.run_reader.await_count
    for attr, writer in outcome.writers.items():
        assert not writer.await_count, f"{surface.name}: {attr} ran for a viewer"


# ---------------------------------------------------------------------------
# The fix, proven at the loader boundary for the two core-routed mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_route_applies_the_clamp_before_the_core_call() -> None:
    """``recover_node`` resolves the run itself, so the route must clamp first.

    Pinned directly: with a foreign-team run the loader answers ``None`` and the
    core recovery is never entered (the HTTP 404 is the caller's spelling of
    that ``None``).
    """
    from fastapi import HTTPException

    run = _make_run(owner_team_id=_TEAM_B, status="awaiting_human")
    session = _boundary_session(run)
    recover = AsyncMock()
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch.object(runs_module, "recover_node", recover),
        patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as denied,
    ):
        await runs_module.recover_run_node(
            run_id=_RUN_ID,
            node_id="node-a",
            req=runs_module.NodeRecoverRequest(input_data={"value": "x"}),
            session=session,
            principal=_team_scoped_key(),
        )

    assert denied.value.status_code == 404
    recover.assert_not_awaited()


@pytest.mark.asyncio
async def test_guardrail_override_route_applies_the_clamp_before_the_core_call() -> None:
    """``guardrail_override`` resolves the run itself: clamp at the route first."""
    from fastapi import HTTPException

    run = _make_run(owner_team_id=_TEAM_B, status="awaiting_human")
    session = _boundary_session(run)
    override = AsyncMock()
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch.object(runs_module, "guardrail_override", override),
        patch.object(runs_module._guardrail_override_rate_limiter, "consume", AsyncMock(return_value=True)),
        patch.object(run_scope, "viewer_run_scope_visible", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as denied,
    ):
        await runs_module.guardrail_override_run(
            run_id=_RUN_ID,
            req=runs_module.GuardrailOverrideRequest(input_data={"value": "x"}),
            session=session,
            principal=_team_scoped_key(),
        )

    assert denied.value.status_code == 404
    override.assert_not_awaited()


# ---------------------------------------------------------------------------
# A fixture typo guard: the shared predicate is the one under test
# ---------------------------------------------------------------------------


def test_surfaces_do_not_reimplement_the_boundary() -> None:
    """Every per-run mutation routes through the one shared leaf predicate.

    ``runs`` composes it behind ``_load_run_in_scope``; ``feedback`` calls it
    directly. A future sibling that hand-rolls a team comparison instead of
    calling the shared loader fails here before it can drift.
    """
    assert runs_module.load_run_in_scope is run_scope.load_run_in_scope
    assert feedback_module.load_run_in_scope is run_scope.load_run_in_scope


@pytest.fixture(autouse=True)
def _clear_overrides() -> Generator[None, None, None]:
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/v1/evals/from-run — a per-run write, authored FROM another team's run
# ---------------------------------------------------------------------------


def _eval_from_run_body() -> dict[str, Any]:
    return {
        "run_id": str(_RUN_ID),
        "node_id": str(uuid.uuid4()),
        "eval_type": "regex",
        "name": "from-run-eval",
    }


def _post_eval_from_run(principal: TenantPrincipal, run: MagicMock) -> tuple[Any, AsyncMock]:
    """POST the from-run surface, returning the response and the DB writer.

    ``create_or_update_eval`` is the persistence action: a 404 that still
    authored the definition would await it.
    """
    session = _boundary_session(run)
    _install_overrides(session, principal)
    writer = AsyncMock(return_value=MagicMock())
    with (
        patch.object(evals_module, "create_or_update_eval", writer),
        patch.object(evals_module, "_load_node_sample_output", AsyncMock(return_value={"field": "x"})),
        patch.object(evals_module, "_eval_def_to_dict", MagicMock(return_value={"id": "eval-1"})),
        patch.object(evals_module, "set_rls_org", AsyncMock()),
        patch.object(evals_module, "set_rls_user_context", AsyncMock()),
        patch("farna.db.crud.team_scope.pipeline_team_scope_team_blind", AsyncMock(return_value=_blind_scope(None))),
    ):
        response = TestClient(app).post("/api/v1/evals/from-run", json=_eval_from_run_body())

    app.dependency_overrides.clear()
    return response, writer


def test_eval_from_run_refuses_a_foreign_team_run_and_authors_nothing() -> None:
    response, writer = _post_eval_from_run(_team_scoped_key(), _make_run(owner_team_id=_TEAM_B))

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == _MSG_RUN_NOT_FOUND
    assert not writer.await_count


def test_eval_from_run_authors_from_its_own_teams_run() -> None:
    response, writer = _post_eval_from_run(_team_scoped_key(), _make_run(owner_team_id=_TEAM_A))

    assert response.status_code == 201, response.text
    assert writer.await_count == 1


def test_eval_from_run_authors_from_an_org_level_run() -> None:
    response, writer = _post_eval_from_run(_team_scoped_key(), _make_run(owner_team_id=None))

    assert response.status_code == 201, response.text
    assert writer.await_count == 1


def test_eval_from_run_admin_is_exempt() -> None:
    response, writer = _post_eval_from_run(_admin(), _make_run(owner_team_id=_TEAM_B))

    assert response.status_code == 201, response.text
    assert writer.await_count == 1


def test_eval_from_run_viewer_is_refused_before_the_run_read() -> None:
    response, writer = _post_eval_from_run(_viewer(), _make_run(owner_team_id=_TEAM_B))

    assert response.status_code == 403, response.text
    assert not writer.await_count
