"""Focused changed-lines branch coverage for the wave-1 sandbox decomposition.

The FAR-310 hardening waves extracted ``_sandbox_agent_impl`` into phase
helpers. The wider suite only ever enters one arm of several guard branches on
these helpers, which leaves the changed-lines branch gate short. These tests
drive the extracted helpers directly to exercise the other arm: missing SDK
stdout, over-cap stderr on the stall/finalise paths, no bridge definitions, an
attempt-keyed streaming writer, and the artifact-writer short-circuit.

No DB, no Docker, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import farna.core.pipeline_engine.node_runner as nr

_ORG_ID = "11111111-2222-3333-4444-555555555555"
_RUN_ID = "run-1"


def _state(**overrides: Any) -> nr._SandboxDispatchState:
    state = nr._SandboxDispatchState(
        exec_ctx=nr._SandboxExecContext(
            session_factory=None,
            org_id=_ORG_ID,
            run_id=_RUN_ID,
            node_id="n1",
            claim_lease=None,
            sandbox_timeout=60,
            provider="e2b",
        )
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_init_exec_artifact_writer_skips_broker_and_writer_without_run_id() -> None:
    """No run id short-circuits both the broker lookup and the writer build."""
    writer, broker = nr._sandbox_init_exec_artifact_writer(run_id="", org_id=_ORG_ID, node_id="n1", attempt_key=None)
    assert writer is None
    assert broker is None


async def test_retain_stall_evidence_prefers_longer_sdk_stdout_and_over_cap_stderr() -> None:
    """An SDK cmd_result with longer stdout wins, cleans a live writer, and
    persists over-cap stderr (the non-None cmd_result arm)."""
    cleanup = MagicMock()
    writer = SimpleNamespace(finalized=False, cleanup=cleanup)
    sandbox = MagicMock()
    sandbox.kill = AsyncMock()
    state = _state(
        sandbox=sandbox,
        sandbox_id="sb-1",
        dispatch_provider=None,
        attempt_key="ak-1",
        streaming_writer_instance=writer,
        exit_code=1,
        stdout_len=10,
        stderr_len=999,
        stdout_truncated=True,
        agent_stdout_raw="short",
        agent_stderr_raw="e" * 999,
        stall_reason="stalled",
    )
    cmd_result = SimpleNamespace(stdout="a much longer sdk stdout payload")
    persist_out = MagicMock(return_value={"stream": "stdout"})
    persist_err = MagicMock(return_value={"stream": "stderr"})
    with (
        patch.object(nr, "_read_log_tail_via_provider", new=AsyncMock(return_value="")),
        patch.object(nr, "_persist_full_stdout_artifact", new=persist_out),
        patch.object(nr, "_persist_full_stderr_artifact", new=persist_err),
        patch.object(nr, "_retain_raw_output_marker", new=AsyncMock(return_value=None)),
    ):
        _command_error, source = await nr._sandbox_retain_stall_evidence(
            state,
            cmd_result=cmd_result,
            node_id="n1",
            run_id=_RUN_ID,
            org_id=_ORG_ID,
            session_factory=None,
            sandbox_timeout=60,
            delivery_sentinel=None,
            stdout_cap=4,
            _resolved_provider="e2b",
        )
    assert source == cmd_result.stdout
    cleanup.assert_called_once()
    persist_out.assert_called_once()
    persist_err.assert_called_once()


async def test_retain_stall_evidence_keeps_raw_stdout_when_sdk_stdout_is_shorter() -> None:
    """A shorter SDK stdout never replaces the captured raw stdout."""
    sandbox = MagicMock()
    sandbox.kill = AsyncMock()
    state = _state(
        sandbox=sandbox,
        sandbox_id="sb-1",
        attempt_key="ak-1",
        exit_code=0,
        agent_stdout_raw="a longer raw stdout",
        agent_stderr_raw="",
        stdout_truncated=False,
        stderr_len=0,
    )
    with (
        patch.object(nr, "_read_log_tail_via_provider", new=AsyncMock(return_value="")),
        patch.object(nr, "_retain_raw_output_marker", new=AsyncMock(return_value=None)),
    ):
        _command_error, source = await nr._sandbox_retain_stall_evidence(
            state,
            cmd_result=SimpleNamespace(stdout="x"),
            node_id="n1",
            run_id=_RUN_ID,
            org_id=_ORG_ID,
            session_factory=None,
            sandbox_timeout=60,
            delivery_sentinel=None,
            stdout_cap=4,
            _resolved_provider="e2b",
        )
    assert source == "a longer raw stdout"


async def test_wrap_command_with_bridge_without_definitions_keeps_command() -> None:
    """An enabled config with zero bound guardrails is inert: no env wiring."""
    envs: dict[str, str] = {}
    with patch(
        "farna.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
        new=AsyncMock(return_value=[]),
    ):
        result = await nr._sandbox_wrap_command_with_bridge(
            _state(),
            node_id="n1",
            run_id=_RUN_ID,
            org_id=_ORG_ID,
            session_factory=None,
            state={},
            rendered_agent_command="python3 /home/user/main.py",
            loop_intercept_config=SimpleNamespace(enabled=True),
            sandbox_envs=envs,
            _resolved_provider="e2b",
        )
    assert result == "python3 /home/user/main.py"
    assert envs == {}


async def test_finalise_envelope_persists_over_cap_stderr() -> None:
    """Over-cap stderr is persisted to the artifact store on the success path."""
    state = _state(
        attempt_key="ak-1",
        agent_stdout_raw="out",
        agent_stderr_raw="e" * 30,
        agent_stdout="out",
        agent_stderr="e" * 30,
        output_json={"status": "completed"},
        pr_url="",
        exit_code=0,
        elapsed=1.0,
        cost_estimate_usd=0.0,
        stdout_len=3,
        stderr_len=30,
        stdout_truncated=False,
        drift_results=[],
        stall_reason=None,
        artifact_writer=None,
        streaming_writer_instance=None,
    )
    interpreted = nr._SandboxInterpretation(
        status="completed",
        result_summary="ok",
        changed_files=[],
        agent_status=None,
        agent_outcome=None,
        sandbox_session_lost=False,
    )
    persist_err = MagicMock(return_value={"stream": "stderr"})
    envelope = {"telemetry": {"status": "completed"}}
    with (
        patch.object(nr, "_finalize_artifact_writer", new=AsyncMock(return_value=None)),
        patch.object(nr, "_persist_full_stderr_artifact", new=persist_err),
        patch.object(nr, "_build_sandbox_node_envelope", new=MagicMock(return_value=envelope)),
    ):
        result = await nr._sandbox_finalise_envelope(
            state,
            interpreted,
            node_id="n1",
            run_id=_RUN_ID,
            org_id=_ORG_ID,
            session_factory=None,
            sandbox_mode="script",
            delivery_sentinel=None,
            stdout_cap=4,
            _resolved_provider="e2b",
        )
    assert result is envelope
    persist_err.assert_called_once()


async def _build_exec_runtime(state: nr._SandboxDispatchState) -> nr._SandboxExecRuntime:
    return await nr._sandbox_build_exec_runtime(
        state,
        node_id="n1",
        run_id=_RUN_ID,
        org_id=_ORG_ID,
        sandbox_mode="script",
        stdout_retention_mode="tail",
        resource_limits=None,
        enable_heartbeat=False,
        watch_log_path=None,
        stdout_percentage_delta=None,
        watch_globs=[],
        wallclock_budget_seconds=None,
        stall_timeout_override=None,
        start_time=0.0,
        stdout_cap=1024,
        drained_chunks=[],
        _resolved_provider="e2b",
    )


async def test_build_exec_runtime_initialises_streaming_writer_for_attempt_key() -> None:
    """A set attempt key builds the uncapped streaming stdout writer."""
    state = _state(attempt_key="ak-1", sandbox=MagicMock())
    with (
        patch.object(nr, "_sandbox_resolve_stall_window", return_value=(SimpleNamespace(), 10)),
        patch.object(nr, "_SandboxWatchdog", return_value=MagicMock()),
        patch("farna.core.artifacts.store.get_store", return_value=MagicMock()),
        patch("farna.core.artifacts.streaming.StreamingArtifactWriter", return_value=MagicMock()),
    ):
        runtime = await _build_exec_runtime(state)
    assert state.streaming_writer_instance is not None
    assert runtime.watchdog is not None


async def test_build_exec_runtime_skips_streaming_writer_without_attempt_key() -> None:
    """No attempt key leaves the streaming writer unbound."""
    state = _state(attempt_key=None, sandbox=MagicMock())
    with (
        patch.object(nr, "_sandbox_resolve_stall_window", return_value=(SimpleNamespace(), 10)),
        patch.object(nr, "_SandboxWatchdog", return_value=MagicMock()),
    ):
        runtime = await _build_exec_runtime(state)
    assert state.streaming_writer_instance is None
    assert runtime.watchdog is not None
