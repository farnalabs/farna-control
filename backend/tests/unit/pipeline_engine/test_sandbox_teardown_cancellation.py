"""Cancellation during the sandbox teardown must not skip the rest of teardown.

``_sandbox_agent_impl``'s terminal ``finally`` tears down, in order: the
loop-intercept bridge server, the (already-finalised) streaming writer, the
sandbox workspace (``destroy_workspace_by_ref``), the per-dispatch provider
(``close``), the route hub (``aclose``), and finally the FENCED dispatch-marker
clear. Every one of those steps used to guard only ``except Exception`` (or
re-raised ``CancelledError`` immediately), so a cancellation landing on ANY of
them unwound the ``finally`` on the spot and silently skipped every later step.

The leak that matters: a cancellation in the bridge close skipped the sandbox
kill (a live workspace left running) and the marker clear (a stale capacity
reservation held against an already-cancelled run).

The fix applies the discipline the receipt harvest immediately above already
uses: record the FIRST ``CancelledError`` in ``_gh_settle_cancelled``, continue
to the next teardown step, and re-raise that recorded cancellation once at the
end of the ``finally`` — so teardown is unconditional and the caller still
observes a cancelled task, carrying the FIRST cancellation rather than a later
one.
"""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from farna.core.pipeline_engine import node_runner as nr
from farna.core.pipeline_engine.node_runner import make_sandbox_agent_fn
from tests.unit.pipeline_engine.conftest import FakeFileIOProvider, install_fake_dispatch

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"
_COMPLETED_OUTPUT = '{"status": "completed", "summary": "done"}'


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-teardown",
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _node_def() -> dict[str, Any]:
    return {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_commands": [_AGENT_COMMAND],
        "timeout_seconds": 30,
    }


def _install_log_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the R1 log-tail seam to a fake so the dispatch never hits urllib."""
    builder = MagicMock(return_value=MagicMock(read_log_tail=AsyncMock(return_value=b"teardown-tail")))
    monkeypatch.setattr("farna.core.pipeline_engine.node_runner._build_log_tail_provider", builder)


def _install_bridge(monkeypatch: pytest.MonkeyPatch, close_side_effect: BaseException) -> MagicMock:
    """Publish a bridge server into the dispatch state whose ``close()`` cancels.

    ``_sandbox_wrap_command_with_bridge`` is the seam that owns
    ``_state.bridge_server``; stubbing it keeps this a teardown test (the real
    bridge start is covered by ``test_node_runner_residual.py``).
    """
    bridge = MagicMock()
    bridge.close = AsyncMock(side_effect=close_side_effect)

    async def _fake_wrap(_state: Any, *, rendered_agent_command: str, **_kwargs: Any) -> str:
        _state.bridge_server = bridge
        return rendered_agent_command

    monkeypatch.setattr(nr, "_sandbox_wrap_command_with_bridge", _fake_wrap)
    return bridge


def _spy_marker_clear(monkeypatch: pytest.MonkeyPatch, *, raise_exc: BaseException | None = None) -> list[str]:
    """Record every reached fenced dispatch-marker clear.

    With no ``raise_exc`` the real clear runs (a no-op without a DB session,
    exactly as on this path); with one, the clear raises it — how a LATER
    teardown cancellation is staged.
    """
    cleared: list[str] = []
    original = nr._exec_clear_dispatch_marker

    async def _spy(ctx: Any) -> None:
        cleared.append("cleared")
        if raise_exc is not None:
            raise raise_exc
        await original(ctx)

    monkeypatch.setattr(nr, "_exec_clear_dispatch_marker", _spy)
    return cleared


async def test_cancellation_in_bridge_close_still_kills_the_sandbox_and_clears_the_marker(monkeypatch: pytest.MonkeyPatch, fake_file_io: FakeFileIOProvider) -> None:
    """A cancel on the FIRST teardown await must not skip the kill or the clear.

    Pre-fix this fails on the two assertions below: ``asyncio.CancelledError``
    is a ``BaseException`` that the bridge close's ``except Exception`` never
    caught, so the ``finally`` unwound there and neither the sandbox kill nor
    the dispatch-marker clear ever ran (leaked workspace, stale reservation).
    """
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-teardown-cancel", exit_code=0)
    bridge = _install_bridge(monkeypatch, asyncio.CancelledError("cancelled during teardown"))
    cleared = _spy_marker_clear(monkeypatch)

    fn = make_sandbox_agent_fn(_node_def())
    with pytest.raises(asyncio.CancelledError):
        await fn(_run_state())

    # The cancellation point really was the bridge close...
    bridge.close.assert_awaited_once()
    # ...yet teardown continued: the workspace was killed, the provider was
    # closed, and the fenced marker clear ran.
    assert "destroy_by_ref" in dispatch.events, dispatch.events
    assert "close" in dispatch.events, dispatch.events
    assert cleared == ["cleared"]


async def test_first_cancellation_is_the_one_reraised_when_a_later_step_also_cancels(monkeypatch: pytest.MonkeyPatch, fake_file_io: FakeFileIOProvider) -> None:
    """The FIRST teardown cancellation wins; a later one never replaces it.

    Matches the receipt-harvest rule (``_gh_settle_cancelled`` is only written
    while it is still ``None``). The first cancellation comes from the provider
    close and the second from the marker clear; an implementation that recorded
    the LAST cancellation instead — or that re-raised the second — fails the
    identity assertion, and pre-fix the run never reaches the second step at
    all (``cleared`` stays empty).
    """
    _install_log_tail(monkeypatch)
    fake_file_io.files["/home/user/output.json"] = _COMPLETED_OUTPUT.encode()
    dispatch = install_fake_dispatch(monkeypatch, ref="sbx-teardown-double-cancel", exit_code=0)
    first_cancellation = asyncio.CancelledError("first: provider close")
    second_cancellation = asyncio.CancelledError("second: marker clear")
    # The provider close is a plain (unshielded) bounded await, so the instance
    # staged here is the one the teardown observes — identity is preserved.
    dispatch.close = AsyncMock(side_effect=first_cancellation)  # type: ignore[method-assign]
    cleared = _spy_marker_clear(monkeypatch, raise_exc=second_cancellation)

    fn = make_sandbox_agent_fn(_node_def())
    with pytest.raises(asyncio.CancelledError) as excinfo:
        await fn(_run_state())

    # The cancellation from the close is the one delivered...
    assert excinfo.value is first_cancellation
    # ...and it did not stop the teardown reaching the later step that cancelled.
    assert "destroy_by_ref" in dispatch.events, dispatch.events
    assert cleared == ["cleared"]
