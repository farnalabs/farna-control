"""HITL / interrupt-gate group of :class:`PipelineExecutor`.

Step 4 of the split described in ``GAPS-harden-w2-executor.md`` §5, group 3.7:
"interrupt gate creation + HITL notification dispatch" moves out of
``executor.py`` as a mixin. The group owns the failure-isolated nested-savepoint
readers that build a gate's briefing / review config / review window, the
coalescing-aware gate row creation, the interrupt handler that parks the run in
``awaiting_human`` and publishes ``hitl_awaiting``, and the notifier dispatch
for that event.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``uuid`` / ``datetime`` / ``UTC`` /
``timedelta`` / ``AsyncSession`` are imported directly: they are shared objects,
so a patch on ``executor.<name>`` either mutates the same object or would have
no meaning.

The function-local ``from farna.core.runner_capacity import
mark_runner_dispatch_cleared_at_hitl`` inside ``_handle_graph_interrupt`` STAYS a
local import. It was one before the move, and a test patches the DEFINING
module (``farna.core.runner_capacity``), which only a call-time import observes;
routing it through ``_ref`` would resolve an import-time binding instead.

Host contract
-------------
This mixin calls one method from a group that has not been split yet
(``_frozen_gated_eval_modes``, governance) plus the three instance attributes it
reads. They are declared on :class:`_ExecutorHost` so the type checker sees
them; the declaration is TYPE_CHECKING-only, so at runtime that base is an empty
placeholder and the real implementation is found on ``PipelineExecutor``
through the MRO.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .support import _log, _ref, _StreamContext, _StreamState

if TYPE_CHECKING:
    from farna.core.hitl_manager import HITLManager
    from farna.core.pipeline_engine.eval_persist_order import EvalDefDTO
    from farna.core.pipeline_engine.hitl_context import HitlReviewContext

    class _ExecutorHost:
        """Collaborators this mixin calls that the host class provides.

        TYPE-CHECKING ONLY. At runtime the mixin's base is the placeholder
        below; the real implementation is found on ``PipelineExecutor`` through
        the MRO. The signature must match the host's exactly — a drift is a
        mypy error.
        """

        @staticmethod
        def _frozen_gated_eval_modes(
            eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None,
            review_id: str,
        ) -> list[dict[str, Any]]: ...

else:

    class _ExecutorHost:
        """Runtime placeholder for the TYPE_CHECKING-only contract above."""

        __slots__ = ()


class HitlMixin(_ExecutorHost):
    """Interrupt-gate creation and HITL notification dispatch."""

    # Declared (assigned on the host) so the type checker can see them.
    _session_factory: async_sessionmaker[AsyncSession]
    _claim_token: str | None
    _notifier: Any | None

    # ---- Helpers for _create_interrupt_gate() --------------------------------
    # Extracted to reduce cognitive complexity (S3776).

    async def _create_interrupt_gate_resolve_pipeline_name(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
        _run_id: uuid.UUID,
    ) -> str | None:
        """Resolve pipeline name in a nested savepoint (failure-isolated)."""
        try:
            async with session.begin_nested():
                pipeline = await _ref("get_pipeline")(session, pipeline_id, organisation_id=org_id)
                return pipeline.name if pipeline is not None else None
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl_review.pipeline_name_lookup_failed",
                extra={"pipeline_id": str(pipeline_id)},
                exc_info=True,
            )
            return None

    async def _create_interrupt_gate_capture_context(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        review_id: str,
        org_id: uuid.UUID,
        pipeline_name: str | None,
        completed_node_outputs: dict[str, Any] | None,
        condition_result: dict[str, Any] | None,
        subject: str | None,
        subject_parent: dict[str, Any] | None,
        subject_leaf_key: str | None,
    ) -> HitlReviewContext | None:
        """Capture HITL review context in a nested savepoint (failure-isolated)."""
        try:
            async with session.begin_nested():
                context: HitlReviewContext | None = await _ref("build_hitl_review_context")(
                    session,
                    run_id=run_id,
                    review_id=review_id,
                    org_id=org_id,
                    pipeline_name=pipeline_name,
                    completed_node_outputs=completed_node_outputs,
                    condition_result=condition_result,
                    subject=subject,
                    subject_parent=subject_parent,
                    subject_leaf_key=subject_leaf_key,
                )
                return context
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl_review.context_capture_failed",
                extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
                exc_info=True,
            )
            return None

    async def _create_interrupt_gate_resolve_config(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
        review_id: str,
        org_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Resolve HITL review config in a nested savepoint (failure-isolated)."""
        try:
            async with session.begin_nested():
                config: dict[str, Any] | None = await _ref("resolve_hitl_review_config")(
                    session,
                    run_id=run_id,
                    review_id=review_id,
                    org_id=org_id,
                )
                return config
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl_review.config_stamp_failed",
                extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
                exc_info=True,
            )
            return None

    async def _create_interrupt_gate_resolve_window(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        review_id: str,
    ) -> datetime | None:
        """Resolve review window deadline in a nested savepoint (failure-isolated)."""
        try:
            async with session.begin_nested():
                window_seconds = await _ref("resolve_review_window_for_gate")(
                    session,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                )
                return datetime.now(UTC) + timedelta(seconds=window_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "hitl_review.window_resolution_failed",
                extra={"run_id": str(run_id), "review_id": review_id, "org_id": str(org_id)},
                exc_info=True,
            )
            return None

    async def _create_interrupt_gate(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        review_id: str,
        pipeline_id: uuid.UUID,
        required_team_id: uuid.UUID | None,
        completed_node_outputs: dict[str, Any] | None = None,
        condition_result: dict[str, Any] | None = None,
        subject: str | None = None,
        subject_parent: dict[str, Any] | None = None,
        subject_leaf_key: str | None = None,
        frozen_gated_eval_modes: list[dict[str, Any]] | None = None,
    ) -> tuple[str | None, bool]:
        """Create the HITL gate row (or reuse a coalescing open gate).

        FAR-604 D4 gate coalescing: when an OPEN gate already covers this work
        item, the duplicate run never raises a second gate. Unchanged SHA →
        "reuse" (the existing gate decides; this run is terminalised
        superseded by the caller); changed SHA → the old gate was
        auto-superseded in the same transaction and this run raises fresh.

        FAR-613: the decision briefing is captured at fire time and attached to
        the gate (``context_json``), and the pipeline name is resolved on the
        SHARED session inside the interrupt handler's outer transaction. Both
        reads run inside a nested savepoint (the established best-effort
        idiom): a DB-level failure there would otherwise leave the shared
        transaction in pending-rollback while the error is swallowed, and the
        next statement (``create_gate``) would raise ``PendingRollbackError``
        and fail the whole interrupt. A savepoint-scoped failure rolls back
        only the savepoint; the briefing degrades (name/context -> None) but
        the interrupt always proceeds.

        FAR-688: the interrupt payload's matched ``condition_result`` (the
        gate node's JMESPath evaluation) is threaded into the briefing capture
        so the bundle records the matched value as PRIMARY evidence. The
        FAR-604 coalescing hash is over ``Run.input_hash`` — never this
        payload — so the extra member cannot change reuse/supersede outcomes.

        FAR-939 chunk 12 (M4): ``frozen_gated_eval_modes`` is the source
        node's evaluation-mode snapshot as of THIS fire — stamped onto
        ``gate_config_json`` so a resume re-freezes the DTO universe
        (§3.3 run-frozen).

        Returns ``(pipeline_name, coalesce_reused)`` — the name is ``None``
        when the gate was reused or the lookup failed (logged, best-effort).
        """
        mgr = _ref("HITLManager")()
        pipeline_name: str | None = None
        coalesce_reused = False
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            outcome = await _ref("evaluate_gate_coalescing")(
                session,
                run_id=run_id,
                review_id=review_id,
                pipeline_id=pipeline_id,
                org_id=org_id,
            )
            if outcome == "reuse":
                coalesce_reused = True
            else:
                pipeline_name = await self._create_interrupt_gate_row(
                    session,
                    mgr,
                    org_id=org_id,
                    run_id=run_id,
                    review_id=review_id,
                    pipeline_id=pipeline_id,
                    required_team_id=required_team_id,
                    completed_node_outputs=completed_node_outputs,
                    condition_result=condition_result,
                    subject=subject,
                    subject_parent=subject_parent,
                    subject_leaf_key=subject_leaf_key,
                    frozen_gated_eval_modes=frozen_gated_eval_modes,
                )
        return pipeline_name, coalesce_reused

    async def _create_interrupt_gate_row(
        self,
        session: AsyncSession,
        mgr: HITLManager,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        review_id: str,
        pipeline_id: uuid.UUID,
        required_team_id: uuid.UUID | None,
        completed_node_outputs: dict[str, Any] | None,
        condition_result: dict[str, Any] | None,
        subject: str | None,
        subject_parent: dict[str, Any] | None,
        subject_leaf_key: str | None,
        frozen_gated_eval_modes: list[dict[str, Any]] | None,
    ) -> str | None:
        """Resolve the gate's briefing + config + window and create the row.

        Runs inside ``_create_interrupt_gate``'s already-open transaction (the
        caller owns the session, RLS, the manager instance and the coalescing
        decision). Returns the resolved pipeline name (``None`` when the lookup
        failed).
        """
        # Resolve pipeline name (failure-isolated)
        pipeline_name = await self._create_interrupt_gate_resolve_pipeline_name(
            session=session,
            pipeline_id=pipeline_id,
            org_id=org_id,
            _run_id=run_id,
        )

        # Capture HITL review context (failure-isolated)
        gate_context = await self._create_interrupt_gate_capture_context(
            session=session,
            run_id=run_id,
            review_id=review_id,
            org_id=org_id,
            pipeline_name=pipeline_name,
            completed_node_outputs=completed_node_outputs,
            condition_result=condition_result,
            subject=subject,
            subject_parent=subject_parent,
            subject_leaf_key=subject_leaf_key,
        )

        # Resolve HITL review config (failure-isolated)
        gate_config = await self._create_interrupt_gate_resolve_config(
            session=session,
            run_id=run_id,
            review_id=review_id,
            org_id=org_id,
        )

        # Resolve review window deadline (failure-isolated)
        terminalize_at = await self._create_interrupt_gate_resolve_window(
            session=session,
            pipeline_id=pipeline_id,
            org_id=org_id,
            run_id=run_id,
            review_id=review_id,
        )

        # FAR-939 chunk 12 (M4): stamp the fire-time evaluation modes
        # onto the fire-time review-config column (the same
        # failure-isolated stamp FAR-634 established). Only merged
        # onto a RESOLVED config — a NULL stamp stays NULL so the
        # resolver's snapshot/live walk fallback is untouched, and
        # every reader of gate_config_json reads members by key, so
        # the extra member is inert to them.
        if frozen_gated_eval_modes and isinstance(gate_config, dict):
            gate_config = {**gate_config, _ref("_GATED_EVAL_MODES_KEY"): frozen_gated_eval_modes}

        await mgr.create_gate(
            session,
            run_id=run_id,
            review_id=review_id,
            pipeline_id=pipeline_id,
            org_id=org_id,
            required_team_id=required_team_id,
            context_json=gate_context,
            gate_config_json=gate_config,
            terminalize_at=terminalize_at,
        )
        return pipeline_name

    async def _handle_graph_interrupt(
        self,
        interrupts: Any,
        state: _StreamState,
        ctx: _StreamContext,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Create the HITL gate and publish the awaiting event for an interrupt.

        PR A signature change (§4.2): the handler ACCEPTS the accumulated
        ``node_token_usage`` / ``completed_node_outputs`` (via ``state`` /
        ``ctx``, which live in ``_stream_graph``'s scope) and RETURNS them in
        the ``awaiting_human`` 4-tuple, so the pause persists the CURRENT
        segment's sets MERGED into the stored cumulative set — NOT ``None``,
        NOT segment-only. The empty-accumulator case (``{}`` → ``None``)
        normalizes so ``finalize_cost``'s merge leaves the stored set untouched.
        """
        gate_payload = _ref("_interrupt_gate_payload")(interrupts)
        review_id = gate_payload.get("review_id", "")
        required_team_id = _ref("_interrupt_required_team_id")(gate_payload)
        condition_result = _ref("_interrupt_condition_result")(gate_payload)
        subject = _ref("_interrupt_subject")(gate_payload)
        subject_parent = _ref("_interrupt_subject_parent")(gate_payload)
        subject_leaf_key = _ref("_interrupt_subject_leaf_key")(gate_payload)
        node_token_usage = state.node_token_usage
        broker = ctx.broker
        run_id = ctx.run_id
        pipeline_id = ctx.pipeline_id
        org_id = ctx.org_id

        # D8 (FAR-594) HITL boundary: BEFORE the run enters ``awaiting_human``,
        # any remaining dispatch marker becomes the capacity-neutral
        # ``cleared_at_hitl`` tombstone — a parked run holds no slot (the
        # marker count is running-only AND tombstone-excluded), and an
        # abandoned awaiting_human run can never starve the org. Best-effort,
        # claim-token-fenced, and guarded on marker presence (never
        # resurrects a marker on a run that dispatched nothing); the resume
        # re-acquires a slot when the HITL approval re-dispatches through the
        # decided-gate auto-resume loop.
        if org_id is not None and self._claim_token:
            from farna.core.runner_capacity import mark_runner_dispatch_cleared_at_hitl

            await mark_runner_dispatch_cleared_at_hitl(
                self._session_factory,
                org_id=org_id,
                run_id=str(run_id),
                claim_token=self._claim_token,
            )

        if pipeline_id is not None and org_id is not None:
            # FAR-939 chunk 12 (M4, §3.3): capture the source node's
            # evaluation modes AS OF THIS FIRE — stamped onto the claim row
            # inside _create_interrupt_gate so resume() can re-freeze the
            # DTO universe even when a mode flips mid-run.
            frozen_gated_eval_modes = self._frozen_gated_eval_modes(
                ctx.eval_definitions_by_node,
                review_id,
            )
            pipeline_name, coalesce_reused = await self._create_interrupt_gate(
                org_id=org_id,
                run_id=run_id,
                review_id=review_id,
                pipeline_id=pipeline_id,
                required_team_id=required_team_id,
                completed_node_outputs=ctx.completed_node_outputs,
                condition_result=condition_result,
                subject=subject,
                subject_parent=subject_parent,
                subject_leaf_key=subject_leaf_key,
                frozen_gated_eval_modes=frozen_gated_eval_modes,
            )
            if coalesce_reused:
                detail = f"HITL gate coalesced (FAR-604 D4): an open gate already covers this work item (unchanged payload hash); the existing gate decides. run={run_id} gate={review_id}"
                _log.info(
                    "pipeline.gate_coalesced",
                    extra={"run_id": str(run_id), "review_id": review_id, "pipeline_id": str(pipeline_id)},
                )
                superseded: tuple[str, str | None, str | None, dict[str, Any] | None] = _ref("_terminal_failure")(broker, "failed", "executor_superseded", detail, node_token_usage)
                return superseded
            broker.publish(
                "hitl_awaiting",
                {
                    "gate_payload": gate_payload,
                    "team_id": str(required_team_id) if required_team_id else None,
                },
            )
            await self._dispatch_hitl_awaiting(
                org_id=org_id,
                run_id=run_id,
                review_id=review_id,
                pipeline_name=pipeline_name,
                team_id=required_team_id,
            )
            return "awaiting_human", None, None, node_token_usage or None

        _log.warning(
            "hitl_review.cannot_create",
            extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id), "org_id": str(org_id)},
        )
        broker.publish("run_failed", {"error": "gate_creation_failed", "detail": "Pipeline or org ID is None"})
        return (
            "failed",
            "configuration_error",
            "Missing pipeline_id or org_id for HITL gate creation",
            node_token_usage or None,
        )

    async def _dispatch_hitl_awaiting(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        review_id: str,
        pipeline_name: str | None,
        team_id: uuid.UUID | None,
    ) -> None:
        """Dispatch the ``hitl_awaiting`` webhook/in-app notification.

        Closes the team-hitl-gates Known Gap (PRD §8.8 flow step 2): previously
        the run lifecycle only emitted the WebSocket broker ``hitl_awaiting``
        event — the HMAC-signed webhook / in-app notification path was never
        triggered from the executor. Routed through the injected Notifier with
        ``team_id`` so team-scoped gates reach team notification endpoints
        first (falling back to org-wide). Failure-isolated: a broken notifier
        or dispatch is logged and never blocks the run pause.
        """
        if self._notifier is None:
            return
        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "review_id": review_id,
            "team_id": str(team_id) if team_id else None,
        }
        if pipeline_name is not None:
            payload["pipeline_name"] = pipeline_name
        try:
            await self._notifier.dispatch_event(
                org_id=org_id,
                event_type=_ref("EVENT_HITL_AWAITING"),
                payload=payload,
                run_id=run_id,
                team_id=team_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "hitl_review.awaiting_notification_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id), "review_id": review_id},
            )
