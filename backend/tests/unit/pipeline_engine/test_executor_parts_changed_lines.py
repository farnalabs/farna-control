"""Changed-lines coverage for the executor helper extraction.

The extraction moved pre-existing helpers out of ``executor.py`` into
``executor_parts/`` and reshaped a handful of executor methods.  Every moved
line counts as a changed line for the changed-lines coverage gate, so the
defensive/error branches those helpers carry must be exercised explicitly.

Each test drives the REAL function/method with its collaborators mocked at the
module boundary and asserts the observable result, so a deleted or altered
branch fails the test rather than silently lowering coverage.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.core.pipeline_engine import executor as executor_mod
from farna.core.pipeline_engine.executor import (
    PipelineExecutor,
    RouterNoMatchError,
    RunNotFoundError,
    _accumulate_chat_model_tokens,
    _accumulate_llm_tokens,
    _checkpointer_scope,
    _connector_failure_views,
    _reclassify_after_work_intact,
    _resolve_post_node_eval_target,
    _safe_pipeline_name,
    _script_lease_probe_ok,
    _teardown_hub,
    org_sandbox_capacity_free,
)
from farna.core.pipeline_engine.executor_parts import run_shape as run_shape_mod

# ---------------------------------------------------------------------------
# Shared async-context-manager fakes
# ---------------------------------------------------------------------------


class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value: Any = None) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _session_factory(session: Any) -> MagicMock:
    @asynccontextmanager
    async def _ctx() -> Any:
        yield session

    return MagicMock(side_effect=lambda: _ctx())


def _fake_session() -> MagicMock:
    session = MagicMock()
    session.begin = MagicMock(return_value=_AsyncCM())
    return session


# ---------------------------------------------------------------------------
# executor._teardown_resume_runtime
# ---------------------------------------------------------------------------


async def test_teardown_resume_runtime_tears_down_a_present_hub_and_keeps_registry_on_complete():
    executor = PipelineExecutor.__new__(PipelineExecutor)
    hub = object()
    with (
        patch.object(executor_mod, "_teardown_hub", new=AsyncMock()) as teardown,
        patch.object(executor_mod, "set_cancellation_check"),
        patch.object(executor_mod, "set_audit_hook"),
        patch.object(executor_mod, "set_model_backend_hub"),
        patch.object(executor_mod, "get_registry") as registry,
    ):
        await executor._teardown_resume_runtime(
            model_backend_hub=hub,
            final_status="complete",
            run_id=uuid.uuid4(),
        )

    teardown.assert_awaited_once_with(hub)
    registry.return_value.close.assert_not_called()


# ---------------------------------------------------------------------------
# executor._finalize_run_after_stream — missing final row
# ---------------------------------------------------------------------------


async def test_finalize_run_after_stream_raises_when_final_run_is_missing():
    executor = PipelineExecutor.__new__(PipelineExecutor)
    session = _fake_session()
    executor._session_factory = _session_factory(session)
    executor._downgrade_final_status_after_stream = AsyncMock(return_value=("complete", None, None))
    executor._compute_run_work_intact = MagicMock(return_value=True)
    executor._run_finalize_cost_transaction = AsyncMock()

    with (
        patch.object(executor_mod, "get_run", new=AsyncMock(return_value=None)),
        patch.object(executor_mod, "set_rls_org", new=AsyncMock()),
        patch.object(executor_mod, "set_rls_execution_context", new=AsyncMock()),
        pytest.raises(RunNotFoundError),
    ):
        await executor._finalize_run_after_stream(
            run_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            node_type_map={},
            final_status="complete",
            error_code=None,
            error_detail=None,
            node_token_usage={},
            completed_node_outputs={},
            node_ids=set(),
        )


# ---------------------------------------------------------------------------
# executor._execute_stream_phase — router no-match translation
# ---------------------------------------------------------------------------


async def test_execute_stream_phase_translates_router_no_match():
    executor = PipelineExecutor.__new__(PipelineExecutor)
    executor._prepare_and_stream = AsyncMock(side_effect=RouterNoMatchError("no matching route"))
    scope = SimpleNamespace(run_id=uuid.uuid4(), org_id=uuid.uuid4())
    stream_config = SimpleNamespace(graph_json={}, broker=MagicMock(), completed_node_outputs={})

    result = await executor._execute_stream_phase(
        scope=scope,
        stream_config=stream_config,
        graph_idempotent=True,
        single_sandbox_node=False,
        model_backend_hub=None,
        connector_hub=None,
    )

    final_status, error_code, error_detail, _node_token_usage, _retry_eligible = result
    assert final_status == "router_no_match"
    assert error_code == "router.no_match"
    assert "no matching route" in (error_detail or "")


# ---------------------------------------------------------------------------
# executor._prepare_and_stream — checkpointer phases
# ---------------------------------------------------------------------------


def _preparable_executor(*, tracker: Any, checkpointer_conn_string: str | None) -> tuple[PipelineExecutor, Any]:
    executor = PipelineExecutor.__new__(PipelineExecutor)
    compiled = MagicMock()
    executor._dispatch_phase_tracker = tracker
    executor._checkpointer_conn_string = checkpointer_conn_string
    executor._compile_scoped_graph = MagicMock(return_value=compiled)
    executor._build_initial_stream_state = AsyncMock(return_value={})
    executor._prepare_stream_runtime = MagicMock(return_value=({}, {}))
    executor._seed_run_scoped_context = AsyncMock()
    executor._stream_with_config = AsyncMock(return_value=("complete", None, None, {}))
    return executor, compiled


def _scope_and_config() -> tuple[SimpleNamespace, SimpleNamespace]:
    scope = SimpleNamespace(run_id=uuid.uuid4(), org_id=uuid.uuid4(), thread_id="thread-1", pipeline_id=uuid.uuid4())
    config = SimpleNamespace(graph_json={})
    return scope, config


async def test_prepare_and_stream_attaches_checkpointer_when_interactive():
    tracker = MagicMock()
    executor, compiled = _preparable_executor(tracker=tracker, checkpointer_conn_string="postgresql://db")
    scope, config = _scope_and_config()
    saver = object()

    @asynccontextmanager
    async def _fake_scope(*_args: Any, **_kwargs: Any):
        yield saver

    with (
        patch.object(executor_mod, "_graph_is_interactive", return_value=True),
        patch.object(executor_mod, "_checkpointer_scope", new=_fake_scope),
        patch("farna.settings.get_settings", return_value=SimpleNamespace(fernet_key="k")),
    ):
        result = await executor._prepare_and_stream(scope=scope, config=config)

    assert result == ("complete", None, None, {})
    assert compiled.checkpointer is saver
    tracker.enter_phase.assert_any_call("graph_compile")
    tracker.enter_phase.assert_any_call("streaming")


async def test_prepare_and_stream_clears_checkpointer_and_enters_streaming_when_not_interactive():
    tracker = MagicMock()
    executor, compiled = _preparable_executor(tracker=tracker, checkpointer_conn_string=None)
    scope, config = _scope_and_config()

    result = await executor._prepare_and_stream(scope=scope, config=config)

    assert result == ("complete", None, None, {})
    assert compiled.checkpointer is None
    tracker.enter_phase.assert_any_call("streaming")


# ---------------------------------------------------------------------------
# executor_parts.outcomes._connector_failure_views
# ---------------------------------------------------------------------------


def test_connector_failure_views_includes_a_stored_dict_view():
    stored = {"node": "n1", "connector_failure": True}
    with patch("farna.core.pipeline_engine.executor_parts.outcomes.node_telemetry", return_value=stored):
        views = _connector_failure_views("n1", {}, {}, {})

    assert views == [stored]


# ---------------------------------------------------------------------------
# executor_parts.support._safe_pipeline_name
# ---------------------------------------------------------------------------


async def test_safe_pipeline_name_reraises_cancelled_lookup():
    async def _cancelled(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError

    with patch("farna.core.pipeline_engine.executor_parts.support._ref") as ref:
        ref.return_value = _cancelled
        with pytest.raises(asyncio.CancelledError):
            await _safe_pipeline_name(MagicMock(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


# ---------------------------------------------------------------------------
# executor_parts.run_shape._teardown_hub
# ---------------------------------------------------------------------------


class _CancelsOnceThenCompletes:
    """Awaitable that raises CancelledError on the first await, then returns."""

    def __init__(self) -> None:
        self.awaits = 0

    def __await__(self) -> Any:
        self.awaits += 1

        async def _first() -> bool:
            raise asyncio.CancelledError

        async def _second() -> bool:
            return False

        return (_first() if self.awaits == 1 else _second()).__await__()


async def test_teardown_hub_completes_shielded_cleanup_then_reraises_cancellation():
    shielded = _CancelsOnceThenCompletes()

    class _Hub:
        async def __aexit__(self, *_exc: object) -> bool:
            return False

    def _fake_shield(awaitable: Any) -> Any:
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        return shielded

    with (
        patch.object(run_shape_mod, "asyncio", SimpleNamespace(shield=_fake_shield, CancelledError=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await _teardown_hub(_Hub())

    assert shielded.awaits == 2


async def test_teardown_hub_reraises_cancellation_from_runtime_hub_aclose():
    class _RuntimeHub:
        async def aclose(self) -> None:
            raise asyncio.CancelledError

    class _Hub:
        _runtime_provider = _RuntimeHub()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    with pytest.raises(asyncio.CancelledError):
        await _teardown_hub(_Hub())


async def test_checkpointer_scope_yields_the_saver_from_the_saver_context():
    ap = _AsyncCM("saver")

    class _Saver:
        @staticmethod
        def from_conn_string(*_args: Any, **_kwargs: Any) -> Any:
            return ap

    with patch.object(run_shape_mod, "ModuloPostgresSaver", _Saver):
        async with _checkpointer_scope("postgresql://db", uuid.uuid4(), None) as saver:
            assert saver == "saver"


# ---------------------------------------------------------------------------
# executor_parts.run_shape._script_lease_probe_ok
# ---------------------------------------------------------------------------


class _LeaseSession:
    def __init__(self, *, row: Any = None, exc: BaseException | None = None) -> None:
        self._row = row
        self._exc = exc

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def begin(self) -> _AsyncCM:
        return _AsyncCM()

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return MagicMock(fetchone=MagicMock(return_value=self._row))


class _LeaseFactory:
    def __init__(self, *, row: Any = None, exc: BaseException | None = None) -> None:
        self._session = _LeaseSession(row=row, exc=exc)

    def __call__(self) -> _LeaseSession:
        return self._session


_LEASE_CLAIM = "claim-1"
_LEASE_RUN = "run-1"
_LEASE_ORG = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))


def _patch_rls() -> Any:
    return (
        patch("farna.db.rls.set_rls_org", new=AsyncMock()),
        patch("farna.db.rls.set_rls_execution_context", new=AsyncMock()),
    )


async def test_lease_probe_invalid_org_id_fails_open():
    ok = await _script_lease_probe_ok(_LeaseFactory(row=("x",)), _LEASE_RUN, "not-a-uuid", _LEASE_CLAIM)
    assert ok is True


async def test_lease_probe_missing_row_fails_open():
    rls_org, rls_ctx = _patch_rls()
    with rls_org, rls_ctx:
        ok = await _script_lease_probe_ok(_LeaseFactory(row=None), _LEASE_RUN, _LEASE_ORG, _LEASE_CLAIM)
    assert ok is True


async def test_lease_probe_empty_dispatch_state_fails_open():
    rls_org, rls_ctx = _patch_rls()
    with rls_org, rls_ctx:
        ok = await _script_lease_probe_ok(_LeaseFactory(row=(None,)), _LEASE_RUN, _LEASE_ORG, _LEASE_CLAIM)
    assert ok is True


async def test_lease_probe_unparseable_dispatch_state_fails_open():
    rls_org, rls_ctx = _patch_rls()
    with rls_org, rls_ctx:
        ok = await _script_lease_probe_ok(_LeaseFactory(row=("-not-json",)), _LEASE_RUN, _LEASE_ORG, _LEASE_CLAIM)
    assert ok is True


async def test_lease_probe_generic_error_fails_closed():
    rls_org, rls_ctx = _patch_rls()
    with rls_org, rls_ctx:
        ok = await _script_lease_probe_ok(_LeaseFactory(exc=RuntimeError("db down")), _LEASE_RUN, _LEASE_ORG, _LEASE_CLAIM)
    assert ok is False


async def test_lease_probe_reraises_cancellation():
    rls_org, rls_ctx = _patch_rls()
    with rls_org, rls_ctx, pytest.raises(asyncio.CancelledError):
        await _script_lease_probe_ok(_LeaseFactory(exc=asyncio.CancelledError()), _LEASE_RUN, _LEASE_ORG, _LEASE_CLAIM)


# ---------------------------------------------------------------------------
# executor_parts.run_shape._resolve_post_node_eval_target
# ---------------------------------------------------------------------------


def test_resolve_post_node_eval_target_returns_envelope_for_non_splittable_non_dict_output():
    envelope = {"output": "just-a-string"}
    with patch.object(executor_mod, "resolve_node_contract_output", return_value=(False, {})):
        result = _resolve_post_node_eval_target("n1", envelope, {"n1": "condition"})
    assert result is envelope


# ---------------------------------------------------------------------------
# executor_parts.run_shape._reclassify_after_work_intact
# ---------------------------------------------------------------------------


async def test_reclassify_after_work_intact_reraises_cancellation_from_the_reread():
    session = MagicMock()
    session.get = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await _reclassify_after_work_intact(session, uuid.uuid4())


# ---------------------------------------------------------------------------
# executor_parts.run_shape.org_sandbox_capacity_free
# ---------------------------------------------------------------------------


def _capacity_refs(*, run: Any, graph_contains_sandbox_agent: bool, cap: Any = None, active: int = 0) -> Any:
    async def _get_run(_session: Any, _run_id: Any) -> Any:
        return run

    async def _get_cap(_session: Any, _org_id: Any) -> Any:
        return cap

    async def _count(_session: Any, _org_id: Any, exclude_run_id: Any = None) -> int:
        return active

    mapping = {
        "get_run": _get_run,
        "get_sandbox_concurrency_limit": _get_cap,
        "count_active_sandbox_runs_for_org": _count,
        "_graph_contains_sandbox_agent": lambda _graph_json: graph_contains_sandbox_agent,
    }
    return lambda name: mapping[name]


def _capacity_session(graph_json: Any = None) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=graph_json)))
    return session


async def test_org_sandbox_capacity_free_when_run_missing():
    session = _capacity_session()
    with patch.object(run_shape_mod, "_ref", _capacity_refs(run=None, graph_contains_sandbox_agent=False)):
        assert await org_sandbox_capacity_free(session, uuid.uuid4(), uuid.uuid4()) is True


async def test_org_sandbox_capacity_free_flag_on_uses_shared_capacity_reader():
    run = SimpleNamespace(snapshot_id=uuid.uuid4())
    session = _capacity_session({"nodes": [{"node_type": "sandbox_agent"}]})
    decision = SimpleNamespace(cap=5, active=2)
    with (
        patch.object(run_shape_mod, "_ref", _capacity_refs(run=run, graph_contains_sandbox_agent=True)),
        patch("farna.settings.get_settings", return_value=SimpleNamespace(runner_capacity_gate_enabled=True)),
        patch("farna.core.runner_capacity.resolve_runner_capacity_decision", new=AsyncMock(return_value=decision)) as resolve,
    ):
        assert await org_sandbox_capacity_free(session, uuid.uuid4(), uuid.uuid4()) is True

    resolve.assert_awaited_once()


async def test_org_sandbox_capacity_free_flag_off_without_a_cap_admits():
    run = SimpleNamespace(snapshot_id=uuid.uuid4())
    session = _capacity_session({"nodes": [{"node_type": "sandbox_agent"}]})
    with (
        patch.object(run_shape_mod, "_ref", _capacity_refs(run=run, graph_contains_sandbox_agent=True, cap=SimpleNamespace(enforced_cap=None))),
        patch("farna.settings.get_settings", return_value=SimpleNamespace(runner_capacity_gate_enabled=False)),
    ):
        assert await org_sandbox_capacity_free(session, uuid.uuid4(), uuid.uuid4()) is True


async def test_org_sandbox_capacity_free_reraises_cancellation():
    session = _capacity_session()

    async def _cancelled(*_args: Any, **_kwargs: Any) -> Any:
        raise asyncio.CancelledError

    with (
        patch.object(run_shape_mod, "_ref", lambda _name: _cancelled),
        patch("farna.settings.get_settings", return_value=SimpleNamespace(runner_capacity_gate_enabled=False)),
        pytest.raises(asyncio.CancelledError),
    ):
        await org_sandbox_capacity_free(session, uuid.uuid4(), uuid.uuid4())


async def test_org_sandbox_capacity_free_flag_off_counts_active_against_cap():
    run = SimpleNamespace(snapshot_id=uuid.uuid4())
    session = _capacity_session({"nodes": [{"node_type": "sandbox_agent"}]})
    with (
        patch.object(
            run_shape_mod,
            "_ref",
            _capacity_refs(run=run, graph_contains_sandbox_agent=True, cap=SimpleNamespace(enforced_cap=4), active=2),
        ),
        patch("farna.settings.get_settings", return_value=SimpleNamespace(runner_capacity_gate_enabled=False)),
    ):
        assert await org_sandbox_capacity_free(session, uuid.uuid4(), uuid.uuid4()) is True


# ---------------------------------------------------------------------------
# executor_parts.run_shape token accumulators — non-dict usage fall-through
# ---------------------------------------------------------------------------


def test_accumulate_chat_model_tokens_ignores_non_dict_usage():
    event = {"metadata": {"langgraph_node": "n1"}, "data": {"output": {"llm_output": {"token_usage": "nope"}}}}
    node_token_usage: dict[str, dict[str, int]] = {}
    _accumulate_chat_model_tokens(event, node_token_usage, None, None)
    assert node_token_usage == {}


def test_accumulate_llm_tokens_ignores_non_dict_usage():
    event = {"metadata": {"langgraph_node": "n1"}, "data": {"output": {"llm_output": {"token_usage": "nope"}}}}
    node_token_usage: dict[str, dict[str, int]] = {}
    _accumulate_llm_tokens(event, node_token_usage, None, None)
    assert node_token_usage == {}
