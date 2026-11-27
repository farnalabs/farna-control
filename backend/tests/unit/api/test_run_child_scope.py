"""Team-boundary enforcement for the run family's CHILD surfaces (ADR 070 §4).

#3442 clamped every per-run read whose path names the run, and #3512 clamped the
per-run mutations. The class left open is the run's **child row** reached by a
run id the handler never loads, or by the child's own id: the route reads the
child table under the organisation predicate alone, so a viewer in team A reads
team B's evidence and eval results.

The actor that matters on these surfaces is the **viewer membership** clamp
(clause 2 of :mod:`farna.api.run_scope`): ``evidence.read`` and ``eval.list`` are
viewer-level floors served by the JWT-only ``require_permission`` dependency, so
a team-scoped ``mk_`` key cannot reach them while a browser viewer can. A
viewer's visibility is their team memberships, so another team's run children
must be refused exactly as the run-detail family refuses the run itself.

Every test drives the REAL shared predicate (``run_scope``); only the two DB
reads behind it are doubled - the parent-run SELECT and the membership SELECT.
That is what makes these tests fail against the pre-fix code: there was no
boundary to stub, because the route never asked.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes import evals as evals_module
from farna.api.routes import evidence as evidence_module
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_RUN_A_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")  # owned by team A
_RUN_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c2")  # owned by team B
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c3")
_EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c4")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_MSG_RUN_NOT_FOUND = "Run not found"


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
    )


def _principal(*, role: str = "viewer", team_id: uuid.UUID | None = None) -> TenantPrincipal:
    """A browser principal. ``team_id`` is only set for a team-scoped key."""
    return TenantPrincipal(
        username=f"{role}@test",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role=role,
        team_id=team_id,
        via_api_key=team_id is not None,
    )


def _run(*, run_id: uuid.UUID, owner_team_id: uuid.UUID | None) -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.pipeline_id = _PIPELINE_ID
    run.owner_team_id = owner_team_id
    run.organisation_id = _ORG_ID
    run.status = "complete"
    run.created_at = None
    return run


def _db_result(
    *,
    scalar_one_or_none: Any = None,
    scalar: Any = None,
    rows: list[Any] | None = None,
    first: Any = None,
) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    result.scalar = MagicMock(return_value=scalar)
    result.first = MagicMock(return_value=first)
    result.all = MagicMock(return_value=rows if rows is not None else [])
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=rows if rows is not None else [])
    result.scalars.return_value = scalars
    return result


def _session(queue: list[MagicMock], *, pipeline_scope: tuple[Any, Any] = (None, "org")) -> AsyncMock:
    """A session whose non-RLS SELECTs are answered from *queue*, in call order."""
    session = configure_mock_session(AsyncMock())
    authz = _db_result()

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        sql = str(stmt)
        if "authz_enforce" in sql:
            return authz
        if "FROM pipelines" in sql:
            return _db_result(first=pipeline_scope)
        if not queue:
            raise AssertionError(f"unexpected SQL issued: {sql}")
        return queue.pop(0)

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _install_overrides(session: AsyncMock, principal: TenantPrincipal) -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=principal.username,
        organisation_id=principal.organisation_id,
        account_id=principal.account_id,
        org_role=principal.org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


@contextmanager
def _client(session: AsyncMock, principal: TenantPrincipal) -> Iterator[TestClient]:
    _install_overrides(session, principal)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _rls(module: Any) -> Iterator[None]:
    """Patch the RLS session setters the route module calls directly."""
    module_set_user_context = getattr(module, "set_rls_user_context", None)
    with patch.object(module, "set_rls_org", AsyncMock()):
        if module_set_user_context is None:
            yield
            return
        with patch.object(module, "set_rls_user_context", AsyncMock()):
            yield


@contextmanager
def _membership(visible_run_ids: set[uuid.UUID]) -> Iterator[None]:
    """Double the viewer-membership DB read behind the shared run predicate."""

    async def _visible(_session: Any, run_id: uuid.UUID, _account_id: uuid.UUID) -> bool:
        return run_id in visible_run_ids

    with patch("farna.api.run_scope.viewer_run_scope_visible", new=AsyncMock(side_effect=_visible)):
        yield


def _eval_result_row() -> MagicMock:
    """A concrete eval-result row so the pre-fix leak serialises (not a 500)."""
    row = MagicMock()
    row.id = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
    row.run_id = _RUN_B_ID
    row.node_id = None
    row.eval_id = uuid.UUID("00000000-0000-0000-0000-0000000000d2")
    row.outcome = "pass"
    row.passed = True
    row.score = 0.9
    row.detail = "team-b result"
    row.error_code = None
    row.judge_confidence_kind = None
    row.evaluated_at = _NOW
    row.resolution = "pending"
    row.resolution_reason = None
    row.resolved_at = None
    row.resolved_by = None
    return row


def _evidence_row(*, subject_type: str = "run", subject_id: str | None = None) -> MagicMock:
    row = MagicMock()
    row.id = _EVIDENCE_ID
    row.organisation_id = _ORG_ID
    row.key = "guardrail.result"
    row.subject_type = subject_type
    row.subject_id = str(_RUN_B_ID) if subject_id is None else subject_id
    # A team-B fact: the ownership a correct boundary must key on.
    row.owner_team_id = _TEAM_B
    row.value = {"raw_output": "team-b-secret"}
    row.observed_at = _NOW
    row.producer_type = "eval"
    row.producer_id = None
    row.created_at = _NOW
    row.updated_at = _NOW
    return row


# ---------------------------------------------------------------------------
# GET /api/v1/evidence — a run's evidence, by run id
# ---------------------------------------------------------------------------


class TestRunEvidenceList:
    URL = f"/api/v1/evidence?run_id={_RUN_B_ID}"

    def test_viewer_cannot_list_another_teams_run_evidence(self) -> None:
        """The parent run is loaded and refused, so no child row is ever read."""
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B))])
        loader = AsyncMock(return_value=[_evidence_row()])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership({_RUN_A_ID}),
            patch.object(evidence_module, "_load_evidence_rows", new=loader),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 200
        assert resp.json() == [], "team B's evidence must not be returned to a team-A viewer"
        loader.assert_not_awaited()

    def test_viewer_can_list_own_teams_run_evidence(self) -> None:
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A))])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership({_RUN_A_ID}),
            patch.object(
                evidence_module,
                "_load_evidence_rows",
                new=AsyncMock(return_value=[_evidence_row(subject_id=str(_RUN_A_ID))]),
            ),
            patch.object(evidence_module, "load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = http.get(f"/api/v1/evidence?run_id={_RUN_A_ID}")

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_viewer_can_list_an_org_level_runs_evidence(self) -> None:
        """An org-level run (no owning team) stays visible to every viewer."""
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=None))])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership(set()),
            patch.object(
                evidence_module,
                "_load_evidence_rows",
                new=AsyncMock(return_value=[_evidence_row(subject_id=str(_RUN_A_ID))]),
            ),
            patch.object(evidence_module, "load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = http.get(f"/api/v1/evidence?run_id={_RUN_A_ID}")

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_admin_can_list_another_teams_run_evidence(self) -> None:
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B))])

        with (
            _client(session, _principal(role="admin")) as http,
            _rls(evidence_module),
            patch.object(evidence_module, "_load_evidence_rows", new=AsyncMock(return_value=[_evidence_row()])),
            patch.object(evidence_module, "load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# GET /api/v1/evidence/{evidence_id} — a run child reached by its own id
# ---------------------------------------------------------------------------


class TestRunEvidenceById:
    URL = f"/api/v1/evidence/{_EVIDENCE_ID}"

    def test_viewer_cannot_read_another_teams_evidence_row(self) -> None:
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B))])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership({_RUN_A_ID}),
            patch.object(evidence_module, "_load_evidence_row", new=AsyncMock(return_value=_evidence_row())),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 404
        assert resp.json()["detail"] == evidence_module._MSG_EVIDENCE_NOT_FOUND

    def test_viewer_can_read_own_teams_evidence_row(self) -> None:
        session = _session([_db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A))])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership({_RUN_A_ID}),
            patch.object(
                evidence_module,
                "_load_evidence_row",
                new=AsyncMock(return_value=_evidence_row(subject_id=str(_RUN_A_ID))),
            ),
            patch.object(evidence_module, "load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 200
        assert resp.json()["id"] == str(_EVIDENCE_ID)

    def test_viewer_can_read_a_non_run_subject_row(self) -> None:
        """A ``trigger_event`` fact has no run and is org-level by declaration."""
        row = _evidence_row(subject_type="trigger_event", subject_id=str(uuid.uuid4()))
        session = _session([])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership(set()),
            patch.object(evidence_module, "_load_evidence_row", new=AsyncMock(return_value=row)),
            patch.object(evidence_module, "load_redaction_rules", new=AsyncMock(return_value=[])),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 200

    def test_malformed_run_subject_fails_closed(self) -> None:
        row = _evidence_row(subject_type="run", subject_id="not-a-run-id")
        session = _session([])

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evidence_module),
            _membership({_RUN_A_ID}),
            patch.object(evidence_module, "_load_evidence_row", new=AsyncMock(return_value=row)),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/runs/{run_id}/evals — a run's eval results, by run id
# ---------------------------------------------------------------------------


class TestRunEvalResults:
    URL = f"/api/v1/runs/{_RUN_B_ID}/evals"

    def test_viewer_cannot_list_another_teams_run_eval_results(self) -> None:
        # The count/page results are queued so the PRE-FIX path completes with
        # 200 + rows: the failure is the leak itself, not a stubbed-out query.
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B)),
                _db_result(scalar=1),
                _db_result(rows=[_eval_result_row()]),
            ]
        )

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evals_module),
            _membership({_RUN_A_ID}),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 404
        assert resp.json()["detail"] == _MSG_RUN_NOT_FOUND

    def test_viewer_can_list_own_teams_run_eval_results(self) -> None:
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A)),
                _db_result(scalar=0),
                _db_result(rows=[]),
            ]
        )

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evals_module),
            _membership({_RUN_A_ID}),
        ):
            resp = http.get(f"/api/v1/runs/{_RUN_A_ID}/evals")

        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_admin_can_list_another_teams_run_eval_results(self) -> None:
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B)),
                _db_result(scalar=0),
                _db_result(rows=[]),
            ]
        )

        with (
            _client(session, _principal(role="admin")) as http,
            _rls(evals_module),
        ):
            resp = http.get(self.URL)

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/v1/evals/compare — two run children reached by run id
# ---------------------------------------------------------------------------


class TestCompareRunEvals:
    URL = "/api/v1/evals/compare"

    def test_viewer_cannot_compare_another_teams_run(self) -> None:
        # The result pages are queued so the pre-fix path completes with 200:
        # the failure under test is the leak, not an exhausted stub.
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A)),
                _db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B)),
                _db_result(rows=[]),
                _db_result(rows=[]),
            ]
        )

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evals_module),
            _membership({_RUN_A_ID}),
        ):
            resp = http.post(self.URL, json={"run_id_a": str(_RUN_A_ID), "run_id_b": str(_RUN_B_ID)})

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run B not found"

    def test_viewer_cannot_compare_when_run_a_is_foreign(self) -> None:
        # Run B is queued too, so the pre-fix path completes the comparison of
        # a foreign run's results instead of failing on an exhausted stub.
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_B)),
                _db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A)),
                _db_result(rows=[]),
                _db_result(rows=[]),
            ]
        )

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evals_module),
            _membership({_RUN_A_ID}),
        ):
            resp = http.post(self.URL, json={"run_id_a": str(_RUN_B_ID), "run_id_b": str(_RUN_A_ID)})

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Run A not found"

    def test_viewer_can_compare_own_teams_runs(self) -> None:
        session = _session(
            [
                _db_result(scalar_one_or_none=_run(run_id=_RUN_A_ID, owner_team_id=_TEAM_A)),
                _db_result(scalar_one_or_none=_run(run_id=_RUN_B_ID, owner_team_id=_TEAM_A)),
                _db_result(rows=[]),
                _db_result(rows=[]),
            ]
        )

        with (
            _client(session, _principal(role="viewer")) as http,
            _rls(evals_module),
            _membership({_RUN_A_ID, _RUN_B_ID}),
        ):
            resp = http.post(self.URL, json={"run_id_a": str(_RUN_A_ID), "run_id_b": str(_RUN_B_ID)})

        assert resp.status_code == 200
        assert resp.json()["run_a"]["id"] == str(_RUN_A_ID)
