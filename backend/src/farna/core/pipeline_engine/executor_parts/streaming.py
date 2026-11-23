"""Stream-phase group of :class:`PipelineExecutor`.

Step 4 of the split described in ``GAPS-harden-w2-executor.md`` §5, group 3.8:
"translate an ``astream_events`` stream into run status + broker events" moves
out of ``executor.py`` as a mixin. The group owns the per-event dispatch
(mapping, node lifecycle publishing, chain-end capture, token accumulation), the
exception-to-terminal-outcome classification, the terminal-status notifications
and the ``_stream_graph`` driver itself.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``uuid`` / ``inspect.isawaitable`` and the
exception classes used in ``isinstance`` checks are imported directly: they are
shared objects, so a patch on ``executor.<name>`` either mutates the same object
or would have no meaning.

Host contract
-------------
This mixin calls methods from groups that have not been split, or that are split
into sibling mixins it does not inherit (``_intercept_node_output_and_run_evals``
in governance, ``_log_accumulation_state`` in telemetry, ``_handle_graph_interrupt``
in ``.hitl``), plus the seven instance attributes it reads. They are declared on
:class:`_ExecutorHost` so the type checker sees them; the declaration is
TYPE_CHECKING-only and the runtime base is a placeholder, so the real
implementations are found on ``PipelineExecutor`` through the MRO.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, cast

from langgraph.errors import GraphInterrupt, NodeCancelledError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from farna.core.eval_engine import EvalBlockedError
from farna.core.pipeline_engine.decorator import RunCancelledError
from farna.core.pipeline_engine.errors import RouterNoMatchError
from farna.core.pipeline_engine.eval_persist_order import EvalDefDTO
from farna.core.pipeline_engine.event_broker import RunEventBroker
from farna.core.pipeline_engine.node_runner import (
    ConnectorNodeFailedError,
    OutputSchemaValidationError,
    SandboxNodeFailedError,
    SupersededNodeError,
)
from farna.core.pipeline_engine.output_filter import OutputRejectedError
from farna.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from farna.core.pipeline_engine.runtime_retry import CompensationFailedError
from farna.db.models.run import Run
from farna.otel_bridge import LangGraphOtelBridge

from .support import _log, _ref, _StreamContext, _StreamState

if TYPE_CHECKING:
    from collections.abc import Callable

    class _ExecutorHost:
        """Collaborators this mixin calls that the host class provides.

        TYPE-CHECKING ONLY. At runtime the mixin's base is the placeholder
        below; the real implementations are found on ``PipelineExecutor``
        through the MRO. The signatures must match the host's exactly — a drift
        is a mypy error.
        """

        _otel_bridge: LangGraphOtelBridge
        _session_factory: async_sessionmaker[AsyncSession]
        _dispatch_phase_tracker: Any | None
        _notifier: Any | None
        _terminating_rejected_gates: set[str]
        on_first_progress: Callable[[], None] | None
        on_node_started: Callable[[str], None] | None
        on_node_completed: Callable[[str], Awaitable[Any] | None] | None

        async def _handle_graph_interrupt(
            self,
            interrupts: Any,
            state: _StreamState,
            ctx: _StreamContext,
        ) -> tuple[str, str | None, str | None, dict[str, Any] | None]: ...

        async def _intercept_node_output_and_run_evals(
            self,
            *,
            name: str,
            output: dict[str, Any],
            ctx: _StreamContext,
        ) -> dict[str, Any]: ...

        def _log_accumulation_state(
            self,
            run_id: uuid.UUID,
            segments_completed: int,
            node_token_usage: dict[str, dict[str, int]] | None,
        ) -> None: ...

else:

    class _ExecutorHost:
        """Runtime placeholder for the TYPE_CHECKING-only contract above."""

        __slots__ = ()


_TerminalOutcome = tuple[str, str | None, str | None, dict[str, Any] | None]


def _terminal_outcome(
    broker: RunEventBroker,
    status: str,
    code: str,
    detail: str,
    node_token_usage: dict[str, Any] | None,
) -> _TerminalOutcome:
    """Typed face of ``executor._terminal_failure``, resolved at CALL time.

    The group's bodies used to call the module-level helper directly. It now
    lives in ``executor_parts.outcomes`` and is re-exported from ``executor``,
    so the call is routed through :func:`~.support._ref` to keep
    ``mock.patch("...executor._terminal_failure")`` intercepting. A local
    wrapper rather than a bare ``_ref(...)(...)`` keeps a declared return type
    for mypy (``_ref`` is ``Any``).
    """
    return cast(_TerminalOutcome, _ref("_terminal_failure")(broker, status, code, detail, node_token_usage))


class StreamingMixin(_ExecutorHost):
    """``astream_events`` consumption, event mapping and stream outcomes."""

    async def _dispatch_run_failed(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        error_code: str | None,
    ) -> None:
        """Dispatch the ``run_failed`` webhook/in-app notification.

        Until now ``EVENT_RUN_FAILED`` was declared and fully rendered by the
        notifier's event mapper but had NO dispatch call site, so a terminal run
        failure produced no notification at all — a billing/credential fault
        that broke two pipelines went unnoticed for six days.

        DELIBERATELY NARROW: the gate is :func:`should_notify_terminal_failure`
        (non-retryable AND ``alert_severity == "critical"``), so routine
        retryable failures and non-critical terminalisations stay silent. The
        payload supplies exactly the fields the mapper's ``EVENT_RUN_FAILED``
        title/body/deep-link templates render: ``pipeline_name``, ``error_code``
        and the run-scoped ``run_id`` (the action URL is ``/runs/{run_id}``).

        The non-retryable provider-refusal codes are EXCLUDED: FAR-2006 already
        dispatches a DEDICATED ``EVENT_PROVIDER_FAILURE`` alert for exactly
        those, from the same finalization tail, so sending the generic
        ``EVENT_RUN_FAILED`` as well would page an operator twice for one
        refusal.

        Failure-isolated: a missing notifier, a name-lookup failure, or a
        dispatch exception is logged and never blocks or alters the terminal
        write (mirrors ``_dispatch_hitl_awaiting``).
        """
        if self._notifier is None:
            return
        if not _ref("should_notify_terminal_failure")(error_code):
            return
        canonical_code = _ref("map_legacy_code")(error_code)
        if canonical_code in _ref("NON_RETRYABLE_PROVIDER_CODES"):
            return
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "error_code": canonical_code,
        }
        pipeline_name = await self._notification_pipeline_name(
            org_id=org_id,
            pipeline_id=pipeline_id,
            run_id=run_id,
        )
        if pipeline_name is not None:
            payload["pipeline_name"] = pipeline_name
        try:
            await self._notifier.dispatch_event(
                org_id=org_id,
                event_type=_ref("EVENT_RUN_FAILED"),
                payload=payload,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "pipeline.run_failed_notification_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id), "error_code": canonical_code},
            )

    async def _dispatch_terminal_status_notification(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_run: Run,
    ) -> None:
        """Dispatch the notification for a terminal status that carries its own event.

        ``run_stalled``, ``budget_exceeded`` and ``eval_blocked`` were declared
        constants with full in-app render configs but NO ``dispatch_event`` call
        site, so an operator could subscribe to (``run_stalled`` /
        ``budget_exceeded`` are webhook-subscribable) or opt out of them and no
        notification could ever arrive (FAR-2138).

        The payload supplies exactly the fields the mapper's title/body/deep-link
        templates render: ``pipeline_name`` (best-effort; the mapper degrades to
        ``[unknown]``), ``error_code`` and the run-scoped ``run_id`` (the action
        URL is ``/runs/{run_id}``).

        Failure-isolated: a missing notifier, a name-lookup failure or a
        dispatch exception is logged and never blocks or alters the terminal
        write (mirrors ``_dispatch_run_failed`` / ``_dispatch_hitl_awaiting``).
        """
        if self._notifier is None:
            return
        status = getattr(final_run, "status", None)
        if not isinstance(status, str):
            return
        error_code = getattr(final_run, "error_code", None)
        event_type = _ref("_TERMINAL_STATUS_EVENTS").get(status)
        if event_type is None and status == "eval_failed" and error_code == "eval_blocked":
            event_type = _ref("EVENT_EVAL_BLOCKED")
        if event_type is None:
            return
        payload: dict[str, Any] = {"run_id": str(run_id)}
        if error_code:
            payload["error_code"] = str(error_code)
        pipeline_name = await self._notification_pipeline_name(
            org_id=org_id,
            pipeline_id=pipeline_id,
            run_id=run_id,
        )
        if pipeline_name is not None:
            payload["pipeline_name"] = pipeline_name
        try:
            await self._notifier.dispatch_event(
                org_id=org_id,
                event_type=event_type,
                payload=payload,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "pipeline.terminal_status_notification_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id), "event_type": event_type},
            )

    async def _notification_pipeline_name(
        self,
        *,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> str | None:
        """Best-effort pipeline name for a notification payload (never raises)."""
        if self._session_factory is None:
            return None
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                pipeline_name: str | None = await _ref("_safe_pipeline_name")(session, pipeline_id, org_id, run_id)
                return pipeline_name
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.notification_pipeline_name_lookup_failed",
                extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id)},
            )
            return None

    async def _handle_chain_end_event(
        self,
        *,
        state: _StreamState,
        ctx: _StreamContext,
        lg_event: Any,
    ) -> None:
        """Handle one ``on_chain_end`` event: capture + stamp the completed
        node's output and latch its stall / agent-failure / session-lost markers.

        Extracted from ``_stream_graph``'s event loop (pure refactor) so the
        loop body stays small and single-responsibility. Also fires the
        FAR-305 standalone post-node eval path against the node's contract
        output. ``ctx.completed_node_outputs`` / ``state`` are mutated in place.
        """
        name = lg_event.get("name", "")
        if name not in ctx.node_ids:
            # HITL gate nodes are synthetic (not in ``node_ids``); latch a
            # stamped terminate-rejection so finalize can downgrade the run.
            if _ref("_is_terminating_rejection_output")(name, (lg_event.get("data") or {}).get("output")):
                state.terminating_rejected_gates.add(name)
            return
        state.segments_completed += 1
        if ctx.guard is not None:
            ctx.guard.record_step()
        output = _ref("_record_chain_end_output")(
            ctx.completed_node_outputs,
            name,
            lg_event.get("data", {}),
            ctx.run_trace_id,
            self._otel_bridge,
            lg_event.get("run_id"),
        )
        if output is None:
            return
        # Markers are extracted from the RAW output BEFORE interception — a
        # FAR-209 Track B redaction (transform/policy) must never blank the
        # stall / agent-failure / session-lost evidence path (these publish to
        # the event broker on the surviving detail; the raw payload itself is
        # only ever framed here).
        stall_reason, agent_failure, session_lost = _ref("_record_node_markers")(
            output,
            ctx.broker,
            name,
        )
        # FAR-1800: capture the session-lost envelope's RETAINED text from the
        # RAW output (same object the markers were read from, BEFORE any FAR-209
        # interception rewrites it) — the wrapper's ``no_output_reason`` is the
        # only place the killing provider error (e.g. HTTP 402) survives.
        session_lost_text = _ref("_node_output_sandbox_session_lost_text")(output) if session_lost else None
        # FAR-305: standalone post-node eval path — run any node-scoped evals
        # for this node against its inner output dict. This is independent of
        # HITL gates: a plain node (no gate) now gets its node-scoped evals
        # evaluated too. If a ``block`` eval fails, ``EvalBlockedError``
        # propagates and the existing ``except EvalBlockedError`` below
        # transitions the run to ``eval_failed`` with ``error_code="eval_blocked"``.
        # If this node ALSO feeds a HITL gate with eval-before-interrupt,
        # the evals run twice (once here post-node, once in the gate) —
        # acceptable for now, the gate's eval is a separate node.
        #
        # FAR-209 Track B: guardrail-typed defs bound to a node are SPLIT out
        # here — they are interception-owned, not eval-owned. Output-point
        # ones intercept THIS envelope; anything else would raise
        # GuardrailMisroutedError inside the eval engine and crash the run.
        if ctx.eval_definitions_by_node:
            output = await self._intercept_node_output_and_run_evals(name=name, output=output, ctx=ctx)
        if stall_reason:
            state.stall_reason = stall_reason
        if agent_failure:
            state.agent_failure_reason = agent_failure
        _ref("_record_connector_failure")(state, ctx, name, output)
        if session_lost:
            state.session_lost_reason = session_lost
            state.session_lost_retained_text = session_lost_text

    async def _handle_stream_event(
        self,
        state: _StreamState,
        ctx: _StreamContext,
        lg_event: Any,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
        """Handle one ``astream_events`` event; returns a terminal 4-tuple or None.

        Extracted from ``_stream_graph``'s event loop (pure refactor). Maps a
        HITL interrupt to the awaiting_human 4-tuple, publishes the node
        lifecycle event (with first-node zombie-watchdog signalling), fires the
        chain-end capture handler, and accumulates token usage. Returns None
        when the loop must keep streaming.
        """
        interrupts = _ref("_streamed_interrupts")(lg_event)
        if interrupts:
            return await self._handle_graph_interrupt(interrupts, state, ctx)
        mapped = _ref("_map_lg_event")(lg_event, ctx.node_ids)
        if mapped is not None:
            completion = self._publish_mapped_stream_event(mapped, state, ctx)
            if completion is not None:
                await completion
        event_kind = lg_event.get("event", "")
        # Capture node output for agent_signal trigger firing.
        if event_kind == "on_chain_end":
            await self._handle_chain_end_event(state=state, ctx=ctx, lg_event=lg_event)
        if event_kind == "on_chat_model_end":
            _ref("_accumulate_chat_model_tokens")(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
        elif event_kind == "on_llm_end":
            _ref("_accumulate_llm_tokens")(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
        return None

    def _publish_mapped_stream_event(
        self,
        mapped: tuple[str, dict[str, Any]],
        state: _StreamState,
        ctx: _StreamContext,
    ) -> Awaitable[Any] | None:
        event_type, payload = mapped
        if not state.first_node_signalled:
            state.first_node_signalled = True
            tracker = self._dispatch_phase_tracker
            if tracker is not None:
                tracker.enter_phase(PHASE_FIRST_NODE_DISPATCHED)
            if self.on_first_progress is not None:
                self.on_first_progress()
        if event_type == "node_started" and self.on_node_started is not None:
            self.on_node_started(str(payload.get("node_id", "")))
        elif event_type == "node_completed" and self.on_node_completed is not None:
            completion = self.on_node_completed(str(payload.get("node_id", "")))
            if isawaitable(completion):
                # Lifecycle hooks historically accepted synchronous callbacks.
                # Await only coroutine-returning hooks so durable completion
                # writes finish before the node event is published, without
                # creating un-awaited coroutine warnings for the async hook.
                async def _finish_completion() -> None:
                    await completion
                    ctx.broker.publish(event_type, payload)

                return _finish_completion()
        ctx.broker.publish(event_type, payload)
        return None

    def _stream_policy_outcome(
        self,
        exc: BaseException,
        broker: RunEventBroker,
        run_id: uuid.UUID,
        state: _StreamState,
        node_token_usage: dict[str, Any] | None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
        """Terminal mappings for eval / output / cancellation / compensation errors.

        Tried BEFORE ``_stream_operational_outcome``; ``None`` = not this
        exception. Order within each helper mirrors the original isinstance
        chain — first match wins.
        """
        if isinstance(exc, EvalBlockedError):
            return _terminal_outcome(
                broker,
                "eval_failed",
                "eval_blocked",
                _ref("_sanitize_detail")(exc, limit=None),
                node_token_usage,
            )
        if isinstance(exc, OutputRejectedError):
            # C4: ``output_rejected`` violates the ``ck_runs_status`` CHECK
            # constraint as a STATUS — it is an error CODE on a ``failed`` run.
            return _terminal_outcome(
                broker,
                "failed",
                "output_rejected",
                _ref("_sanitize_detail")(exc, limit=None),
                node_token_usage,
            )
        if isinstance(exc, RunCancelledError):
            broker.publish("run_cancelled", {})
            self._log_accumulation_state(run_id, state.segments_completed, state.node_token_usage)
            return "cancelled", None, None, node_token_usage
        if isinstance(exc, ConnectorNodeFailedError):
            # Preserve the typed connector envelope when P5 has no retry or
            # compensation route left to resolve the failure.
            envelope = exc.envelope
            return _terminal_outcome(
                broker,
                "failed",
                envelope.get("error_code", _ref("_CODE_CONNECTOR_FAILED")),
                _ref("_sanitize_detail")(str(exc.cause), limit=None),
                node_token_usage,
            )
        if isinstance(exc, CompensationFailedError):
            # FAR-402 P5 (§E): a watched node's compensation target itself failed.
            # The node wrapper wrapped the watched-node terminal failure into a
            # forward compensation execution, which then failed — terminalize the
            # run as COMPENSATION_FAILED (never retried; the run already left the
            # batch pipeline's normal failure path via the compensation edge).
            scrubbed = _ref("_sanitize_detail")(str(exc), limit=None)
            _log.warning(
                "pipeline.compensation_failed",
                extra={
                    "run_id": str(run_id),
                    "node_id": getattr(exc, "node_id", None),
                    "compensation_target": getattr(exc, "compensation_target", None),
                },
            )
            return _terminal_outcome(
                broker,
                "compensation_failed",
                _ref("COMPENSATION_FAILED_CODE"),
                scrubbed,
                node_token_usage,
            )
        return None

    def _stream_operational_outcome(
        self,
        exc: BaseException,
        broker: RunEventBroker,
        run_id: uuid.UUID,
        node_token_usage: dict[str, Any] | None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
        """Terminal mappings for runaway / timeout / superseded / validation / routing errors.

        Tried AFTER ``_stream_policy_outcome``; ``None`` = not this exception
        (the caller falls back to the generic ``type(exc).__name__`` failure).
        Order within the helper mirrors the original isinstance chain — first
        match wins.
        """
        if isinstance(exc, RunawayRunError):
            error_detail = _ref("_sanitize_detail")(exc, limit=None)
            _log.warning(
                "runaway.terminated",
                extra={
                    "run_id": str(run_id),
                    "guard": exc.guard,
                    "current": exc.current,
                    "limit": exc.limit,
                },
            )
            return _terminal_outcome(broker, "failed", "runaway", error_detail, node_token_usage)
        if isinstance(exc, TimeoutError):
            error_detail = _ref("_sanitize_detail")(exc, limit=None)
            _log.warning(
                _ref("_ERROR_CODE_NODE_TIMEOUT"),
                extra={"run_id": str(run_id), "detail": error_detail},
            )
            return _terminal_outcome(broker, "failed", "node_timeout", error_detail, node_token_usage)
        if isinstance(exc, SupersededNodeError):
            # A6: the sandbox dispatch marker was denied — a superseded claim
            # or a run no longer running. Terminal ``superseded`` failure; the
            # token-guarded finalize write is a no-op if a successor already
            # owns the run. NEVER a completed run with zero work.
            scrubbed = _ref("_sanitize_detail")(exc, limit=None)
            _log.warning(
                "pipeline.node_superseded",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_outcome(
                broker,
                "failed",
                "executor_superseded",
                scrubbed,
                node_token_usage,
            )
        if isinstance(exc, OutputSchemaValidationError):
            # Manual-node resume output (or agent output) failed validation
            # against output_schema_json. Domain-specific error code per §8.9 —
            # never a raw ``ValueError``.
            scrubbed = _ref("_sanitize_detail")(exc, limit=None)
            _log.warning(
                "pipeline.output_schema_validation_failed",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_outcome(
                broker,
                "failed",
                "schema_validation_failure",
                scrubbed,
                node_token_usage,
            )
        if isinstance(exc, RouterNoMatchError):
            # FAR-402 P1 (F2-A): a Router node had no matching rule and no
            # default — terminalize the run with the dedicated router_no_match
            # status (terminal, non-failure; classified as excluded/notify).
            # NOT retryable: a no-match is a definitive outcome, not a transient
            # infra failure. ``_stream_graph`` catches the exception BEFORE it
            # reaches execute()'s dedicated except, so the mapping lives here.
            error_detail = _ref("_sanitize_detail")(exc, limit=None)
            _log.info("pipeline.router_no_match", extra={"run_id": str(run_id), "detail": error_detail})
            return _terminal_outcome(
                broker,
                "router_no_match",
                "router.no_match",
                error_detail,
                node_token_usage,
            )
        return None

    async def _stream_exception_outcome(
        self,
        exc: BaseException,
        *,
        state: _StreamState,
        ctx: _StreamContext,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Map a stream exception to a terminal 4-tuple, or re-raise it.

        Extracted from ``_stream_graph``'s exception chain (pure refactor) so
        the stream method keeps a single ``except BaseException`` clause. Every
        mapped outcome is identical to the original chain; cancellation and
        transient node failures are re-raised unchanged. The isinstance checks
        are split across ``_stream_policy_outcome`` and
        ``_stream_operational_outcome`` (tried in that order) — first match
        wins, exactly as the original inline chain.
        """
        broker = ctx.broker
        run_id = ctx.run_id
        node_token_usage = state.node_token_usage or None
        if isinstance(exc, ConnectorNodeFailedError) and ctx.completed_node_outputs is not None:
            ctx.completed_node_outputs[exc.node_id] = exc.envelope
        if isinstance(exc, GraphInterrupt):
            interrupts = exc.args[0] if exc.args else []
            return await self._handle_graph_interrupt(interrupts, state, ctx)
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, (NodeCancelledError, SandboxNodeFailedError)):
            # Transient node cancellation / sandbox-infra failure (langgraph
            # wraps a node body's asyncio.CancelledError; a stall or command
            # timeout raises SandboxNodeFailedError). Do NOT swallow into a
            # terminal failure tuple here — propagate so execute() can decide:
            # retry (fenced reset to pending + re-raise) or terminal-fail once
            # retries are exhausted.
            raise
        policy_outcome = self._stream_policy_outcome(exc, broker, run_id, state, node_token_usage)
        if policy_outcome is not None:
            return policy_outcome
        operational_outcome = self._stream_operational_outcome(exc, broker, run_id, node_token_usage)
        if operational_outcome is not None:
            return operational_outcome
        _tb = _ref("_traceback_detail")(exc, limit=5000)
        return _terminal_outcome(
            broker,
            "failed",
            type(exc).__name__,
            _tb,
            state.node_token_usage or None,
        )

    async def _stream_graph(
        self,
        compiled: Any,
        initial_state: dict[str, Any] | None,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        *,
        pipeline_id: uuid.UUID | None = None,
        org_id: uuid.UUID | None = None,
        completed_node_outputs: dict[str, Any] | None = None,
        guard: RunawayGuard | None = None,
        node_token_budgets: dict[str, int] | None = None,
        eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None = None,
        node_type_map: dict[str, str] | None = None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Stream graph execution, mapping events to broker publishes.

        If *completed_node_outputs* is provided (a mutable dict), it will be
        populated with ``{node_id: output_data}`` for each completed node.

        If *guard* is provided, runaway run protection checks are enforced
        before each event and on node completion / token usage.

        If *node_token_budgets* is provided (``{node_id: token_budget}``),
        per-node token budgets are enforced after each LLM call — if a node's
        cumulative tokens exceed its budget a ``RunawayRunError`` is raised.

        Returns (final_status, error_code, error_detail, node_token_usage).
        """
        state = _StreamState()
        self._terminating_rejected_gates = set()
        ctx = _StreamContext(
            node_ids=node_ids,
            guard=guard,
            completed_node_outputs=completed_node_outputs,
            run_trace_id=None,
            broker=broker,
            run_id=run_id,
            pipeline_id=pipeline_id,
            org_id=org_id,
            node_token_budgets=node_token_budgets,
            eval_definitions_by_node=eval_definitions_by_node,
            node_type_map=node_type_map,
        )
        lg_config = {**config, "callbacks": [self._otel_bridge]}
        # FAR-198: seed the OTel context with the run's deterministic trace id
        # so every span exported during graph execution carries the SAME
        # trace_id the API reports on RunResponse. The root span is stored on
        # the bridge (its spans inherit it) AND attached to the current
        # context (spans created outside the bridge inherit it too). Both are
        # cleaned up in the finally block below.
        run_trace_id, run_root_span = _ref("_compute_otel_run_context")(
            config,
            self._otel_bridge,
        )
        ctx.run_trace_id = run_trace_id
        run_root_token = _ref("context_api").attach(_ref("set_span_in_context")(run_root_span)) if run_root_span is not None else None
        try:
            async for lg_event in compiled.astream_events(initial_state, lg_config, version="v2"):
                if ctx.guard is not None:
                    ctx.guard.check_duration()
                outcome = await self._handle_stream_event(state, ctx, lg_event)
                if outcome is not None:
                    return outcome

            self._terminating_rejected_gates = set(state.terminating_rejected_gates)
            terminal: _TerminalOutcome | None = _ref("_stream_terminal_reason")(state, broker, run_id)
            if terminal is not None:
                return terminal
            # FAR-1534: ``run_completed`` is NOT published here. The finalize
            # step can still downgrade this would-be ``complete`` (HITL
            # terminate-rejection -> ``rejected``, masked sandbox failure ->
            # ``failed``), so the event is published by
            # ``_finalize_then_publish_completed`` AFTER the terminal status
            # commit, carrying the final status.
            return "complete", None, None, state.node_token_usage or None
        except Exception as exc:
            # CancelledError / NodeCancelledError / SandboxNodeFailedError
            # propagate via _stream_exception_outcome's re-raise branches so
            # execute()'s retry machinery still decides.
            return await self._stream_exception_outcome(exc, state=state, ctx=ctx)
        finally:
            # FAR-198: tear down the seeded OTel context + run root span on
            # every exit path (returns, exceptions, cancellation).
            if run_root_token is not None:
                _ref("context_api").detach(run_root_token)
            self._otel_bridge.end_run_root()
