"""Execute / resume / transient-recovery group of :class:`PipelineExecutor`.

Step 4 of the split described in ``GAPS-harden-w2-executor.md`` §5, group 3.6:
"drive one run from claim to terminal, and recover it when a node dies
transiently" moves out of ``executor.py`` as a mixin. The group owns the
``execute`` and ``resume`` entry points, their load/validate/admit/prepare and
stream-phase helpers, the whole transient-failure decision tree, the fenced
pending reset and the post-stream tail.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``uuid`` / ``datetime`` and the exception and
value classes used in ``raise`` / ``except`` / ``isinstance`` clauses are
imported directly: they are shared objects, so a patch on ``executor.<name>``
either mutates the same object or would have no meaning. The names a test DOES
patch as objects (``GraphValidator``, ``RunawayGuard``, ``get_or_compile``,
``build_graph_from_json``, ``get_registry``, ``get_run``, ``update_run_status``,
``set_rls_*``, ``set_mutation_row_lock_timeout``, the ``set_*_hook`` seams and
every extracted helper) stay behind ``_ref``.

Host contract
-------------
This mixin calls admission / governance / hub methods that have not been split,
plus the telemetry and finalize mixins' hooks and the stream driver (a sibling
mixin), and reads eight instance attributes. They are declared on
:class:`_ExecutorHost` so the type checker sees them; the declaration is
TYPE_CHECKING-only and the runtime base is a placeholder, so the real
implementations are found on ``PipelineExecutor`` through the MRO.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from langgraph.errors import NodeCancelledError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.core.graph_validator._types import ValidationResult
from farna.core.model_backend_hub import ModelBackendHub
from farna.core.pipeline_engine.errors import RouterNoMatchError
from farna.core.pipeline_engine.eval_persist_order import EvalDefDTO
from farna.core.pipeline_engine.event_broker import RunEventBroker
from farna.core.pipeline_engine.node_runner import SandboxNodeFailedError, SandboxTierRefusedError
from farna.core.pipeline_engine.retry_policy import _can_fenced_requeue
from farna.db.models.eval import Eval
from farna.db.models.hitl_claim import HitlClaim
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.models.policy_gate import PolicyGate
from farna.db.models.run import Run
from farna.otel_bridge import LangGraphOtelBridge

from .support import GraphValidationError, RunNotFoundError, _log, _ref, _RunScope, _StreamConfig

if TYPE_CHECKING:
    from farna.core.pipeline_engine.runaway_protection import RunawayGuard

    class _ExecutorHost:
        """Collaborators this mixin calls that the host class provides.

        TYPE-CHECKING ONLY. At runtime the mixin's base is the placeholder
        below; the real implementations are found on ``PipelineExecutor``
        through the MRO. The signatures must match the host's exactly - a drift
        is a mypy error.
        """

        _session_factory: async_sessionmaker[AsyncSession]
        _claim_token: str | None
        _checkpointer_conn_string: str | None
        _node_timeouts: dict[str, int]
        _registry_gated_agents: set[str]
        _stall_requested: asyncio.Event | None
        _dispatch_phase_tracker: Any | None
        _otel_bridge: LangGraphOtelBridge

        async def _check_capacity(
            self,
            *,
            run_id: uuid.UUID,
            org_id: uuid.UUID,
            pipeline_id: uuid.UUID,
            max_concurrent: int,
            graph_json: dict[str, Any] | None = None,
            snapshot_id: uuid.UUID | None = None,
        ) -> Run: ...

        async def _check_spend_ceiling_gate(
            self,
            *,
            run_id: uuid.UUID,
            org_id: uuid.UUID,
            claim_token: str | None,
        ) -> Run | None: ...

        async def _check_policy_gate_pin(
            self,
            session: AsyncSession,
            *,
            run_id: uuid.UUID,
            org_id: uuid.UUID,
            snapshot: PipelineSnapshot,
            claim_token: str | None,
        ) -> Run | None: ...

        async def _load_claimed_conformance_guardrails(
            self,
            org_id: uuid.UUID,
            pipeline_id: uuid.UUID,
        ) -> tuple[list[Any], bool]: ...

        async def _load_eval_defs_for_pipeline(
            self,
            session: AsyncSession,
            pipeline_id: uuid.UUID,
        ) -> list[tuple[Eval, PolicyGate | None]]: ...

        @staticmethod
        def _build_eval_defs_by_node(
            eval_rows: list[tuple[Eval, PolicyGate | None]],
            org_id: uuid.UUID,
            pipeline_id: uuid.UUID,
            policy_gate_pins: list[dict[str, Any]] | None = None,
        ) -> dict[str, list[EvalDefDTO]]: ...

        @staticmethod
        def _overlay_frozen_gated_eval_modes(
            eval_defs_by_node: dict[str, list[EvalDefDTO]],
            fired_gate_configs: Sequence[dict[str, Any] | None] | None,
        ) -> int: ...

        async def _init_model_backend_hub(self, org_id: uuid.UUID) -> ModelBackendHub | None: ...

        async def _init_connector_hub(
            self,
            org_id: uuid.UUID,
            *,
            graph_json: dict[str, Any] | None = None,
        ) -> Any | None: ...

        async def _enforce_resume_sandbox_capacity(
            self,
            session: AsyncSession,
            *,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
            snapshot_id: uuid.UUID | None,
        ) -> None: ...

        async def _resolve_earned_at_start(
            self,
            pipeline_id: uuid.UUID,
            org_id: uuid.UUID,
            snapshot: Any,
        ) -> str | None: ...

        def _check_db_cancellation(
            self,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
        ) -> Callable[[], Awaitable[bool]]: ...

        def _dispatch_context_write_audit(
            self,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
        ) -> Callable[[dict[str, Any]], Awaitable[None]]: ...

        async def _finalize_then_publish_completed(self, *, defer_close: bool, **finalize_kwargs: Any) -> Run: ...

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
        ) -> tuple[str, str | None, str | None, dict[str, Any] | None]: ...

else:

    class _ExecutorHost:
        """Runtime placeholder for the TYPE_CHECKING-only contract above."""

        __slots__ = ()


class RunExecuteMixin(_ExecutorHost):
    """``execute`` / ``resume`` entry points and transient-failure recovery."""

    # ---- Helpers for resume() -------------------------------------------------
    # Extracted to reduce cognitive complexity (S3776).

    async def _resume_load_and_validate(
        self,
        *,
        session: AsyncSession,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        claim_token: str | None,
        expected_claim_status: str | None,
        check_sandbox_capacity: bool,
    ) -> tuple[Run, PipelineSnapshot, dict[str, Any], dict[str, str]]:
        """First transaction: load run, validate claim, enforce capacity, validate snapshot."""
        await _ref("set_rls_org")(session, org_id)
        await _ref("set_rls_execution_context")(session)
        await _ref("set_mutation_row_lock_timeout")(session)
        run = await _ref("get_run")(session, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        if expected_claim_status is not None:
            if claim_token is None:
                raise ValueError("expected_claim_status requires claim_token")
            run = await _ref("_claim_resume_for_executor")(
                session,
                run_id=run_id,
                org_id=org_id,
                expected_claim_token=claim_token,
                source_status=expected_claim_status,
            )
        else:
            await _ref("update_run_status")(session, run_id, "running", claimed_by=_ref("_WORKER_ID"))
        if check_sandbox_capacity:
            await self._enforce_resume_sandbox_capacity(
                session,
                org_id=org_id,
                run_id=run_id,
                snapshot_id=run.snapshot_id,
            )
        snapshot_result = await session.execute(_ref("select")(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
        snapshot = snapshot_result.scalar_one()
        graph_json: dict[str, Any] = snapshot.graph_json
        node_type_map = _ref("derive_node_type_map")(graph_json)
        validation = await _ref("GraphValidator")().validate_for_run(snapshot, {}, session)
        if not validation.is_valid:
            raise GraphValidationError(validation.issues, run_id)
        pin_run = await self._check_policy_gate_pin(
            session,
            run_id=run_id,
            org_id=org_id,
            snapshot=snapshot,
            claim_token=claim_token,
        )
        if pin_run is not None:
            return pin_run, None, None, None  # type: ignore[return-value]
        return run, snapshot, graph_json, node_type_map

    async def _resume_load_eval_defs(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        policy_gate_pins_json: list[dict[str, Any]] | None,
    ) -> dict[str, list[EvalDefDTO]]:
        """Load eval definitions and apply frozen gated eval modes from fired gates."""
        eval_rows = await self._load_eval_defs_for_pipeline(session, pipeline_id)
        eval_defs_by_node = self._build_eval_defs_by_node(
            eval_rows,
            org_id,
            pipeline_id,
            policy_gate_pins_json,
        )
        fired_gate_configs = list(
            (
                await session.execute(
                    _ref("select")(HitlClaim.gate_config_json).where(
                        HitlClaim.run_id == run_id,
                        HitlClaim.organisation_id == org_id,
                    )
                )
            ).scalars()
        )
        frozen_applied = self._overlay_frozen_gated_eval_modes(eval_defs_by_node, fired_gate_configs)
        if frozen_applied:
            _log.info(
                "eval_defs.frozen_modes_reapplied",
                extra={"run_id": str(run_id), "entries": frozen_applied},
            )
        return eval_defs_by_node

    async def _resume_prepare_second_transaction(
        self,
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> tuple[Pipeline, dict[str, Any]]:
        """Second transaction: load pipeline (including soft-deleted) and capture scalars."""
        await _ref("set_rls_org")(session, org_id)
        await _ref("set_rls_execution_context")(session)
        from farna.db.soft_delete import include_soft_deleted

        pipeline_result = await session.execute(include_soft_deleted(_ref("select")(Pipeline).where(Pipeline.id == pipeline_id)))
        pipeline = pipeline_result.scalar_one_or_none()
        if pipeline is None:
            raise RunNotFoundError(run_id)
        run = await _ref("get_run")(session, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        scalars = self._capture_execution_scalars(pipeline, run)
        return pipeline, scalars

    def _resume_compile_graph(
        self,
        *,
        pipeline: Pipeline,
        pipeline_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        graph_json: dict[str, Any],
        eval_defs_by_node: dict[str, list[EvalDefDTO]],
        pipeline_retry_policy: dict[str, Any] | None,
        scalars: dict[str, Any],
        org_id: uuid.UUID,
    ) -> tuple[Any, Callable[[str, dict[str, Any]], str | None]]:
        """Compile or retrieve the cached graph for resume."""
        run_ref = _ref("rc").build_run_ref(str(pipeline_id), scalars["run_number"])

        def _node_idempotency_key(node_id: str, _state: dict[str, Any]) -> str | None:
            key: str | None = _ref("rc").node_idempotency_key(run_ref=run_ref, node_ref=node_id)
            return key

        compiled = _ref("get_or_compile")(
            pipeline_id,
            snapshot_id,
            lambda: _ref("build_graph_from_json")(
                graph_json,
                eval_definitions_by_node=eval_defs_by_node,
                session_factory=self._session_factory,
                org_id=org_id,
                pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
                pipeline_retry_policy=pipeline_retry_policy,
                node_idempotency_key=_node_idempotency_key,
                pipeline_stdout_retention_config=pipeline.stdout_retention_config,
            ),
            pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            graph_struct_hash=_ref("struct_hash_with_eval_defs")(
                _ref("compute_retry_aware_topology_hash")(graph_json, pipeline_retry_policy),
                eval_defs_by_node,
            ),
        )
        return compiled, _node_idempotency_key

    async def _resume_setup_runtime(
        self,
        *,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        snapshot: PipelineSnapshot,
        run_id: uuid.UUID,
    ) -> tuple[RunEventBroker, ModelBackendHub | None]:
        """Set up runtime environment: conformance context, broker, hooks, model hub."""
        claimed, claims_load_failed = await self._load_claimed_conformance_guardrails(org_id, pipeline_id)
        _ref("set_conformance_ctx")(
            self._session_factory,
            org_id,
            snapshot.environment_profile_id,
            pipeline_id,
            claimed,
            claims_load_failed,
        )
        broker = _ref("get_registry")().get_or_create(run_id)
        _ref("set_cancellation_check")(self._check_db_cancellation(org_id, run_id))
        _ref("set_audit_hook")(self._dispatch_context_write_audit(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))
        model_backend_hub = await self._init_model_backend_hub(org_id)
        return broker, model_backend_hub

    async def _resume_stream_and_handle_errors(
        self,
        *,
        compiled: Any,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
        resume_data: dict[str, Any],
        completed_node_outputs: dict[str, Any],
        guard: Any,
        node_token_budgets: dict[str, int],
        eval_definitions_by_node: dict[str, list[EvalDefDTO]],
        node_type_map: dict[str, str],
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Stream the graph and handle errors."""
        from farna.settings import get_settings

        final_status = "failed"
        error_code = None
        error_detail = None
        node_token_usage = None
        conn_string = self._checkpointer_conn_string
        assert conn_string is not None, "Checkpointer connection string must be set"
        try:
            # The scope open MUST sit inside this try: a checkpointer-connection
            # failure (a transient Postgres blip in ``__aenter__``, an invalid
            # conn string) must be terminalized through the same failure path as
            # a stream error instead of escaping ``resume()`` uncaught and
            # leaving the run row resting in ``running``/claimed. Main caught
            # this region inside ``except Exception`` (FAR-366 review).
            _settings = get_settings()
            async with _ref("_checkpointer_scope")(
                conn_string,
                organisation_id=org_id,
                fernet_key=_settings.fernet_key,
            ) as saver:
                compiled.checkpointer = saver
                # Inject the human's HITL decision into the graph state. A
                # ``None`` here fails closed in ``_hitl_review_resume_result``
                # and re-interrupts the gate, so the decision MUST be threaded
                # through from ``resume()`` (it is not optional).
                await compiled.aupdate_state(
                    config,
                    {"_hitl_decision": resume_data, "_claim_lease": self._claim_token},
                )
                final_status, error_code, error_detail, node_token_usage = await self._stream_graph(
                    compiled,
                    None,
                    config,
                    node_ids,
                    broker,
                    run_id,
                    pipeline_id=pipeline_id,
                    org_id=org_id,
                    completed_node_outputs=completed_node_outputs,
                    guard=guard,
                    node_token_budgets=node_token_budgets,
                    eval_definitions_by_node=eval_definitions_by_node,
                    node_type_map=node_type_map,
                )
        except RuntimeError as exc:
            if "checkpointer" in str(exc):
                _log.warning("pipeline.resume_no_checkpointer", extra={"run_id": str(run_id)})
                final_status = "failed"
                error_code = "configuration_error"
                error_detail = "Pipeline configuration is invalid (checkpointer unavailable)."
            else:
                raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_detail = _ref("_enrich_failure_detail")(_ref("_failure_reason")(exc), exc)
            _log.error(
                "pipeline.resume_error",
                extra={
                    "run_id": str(run_id),
                    **(
                        {
                            "failure_build_sha": _ref("_failure_build_sha")(),
                            "failing_column": _ref("_failure_column")(exc),
                        }
                        if _ref("_ATTRIBUTION_AVAILABLE")
                        else {}
                    ),
                },
                exc_info=exc,
            )
            final_status = "failed"
            error_code = type(exc).__name__
        return final_status, error_code, error_detail, node_token_usage

    async def _resume_cleanup_and_finalize(
        self,
        *,
        final_status: str,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_type_map: dict[str, str],
        error_code: str | None,
        error_detail: str | None,
        node_token_usage: dict[str, Any] | None,
        completed_node_outputs: dict[str, Any],
        node_ids: set[str],
    ) -> Run:
        """Finalize the run after the stream phase (teardown has already run)."""
        return await self._finalize_then_publish_completed(
            defer_close=final_status == "complete",
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_type_map=node_type_map,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_ids=node_ids,
        )

    # ---- Helpers for execute() -----------------------------------------------
    # Extracted to reduce cognitive complexity (S3776).

    async def _execute_load_and_validate(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
    ) -> tuple[Run, Pipeline, PipelineSnapshot, dict[str, Any], dict[str, str]]:
        """Load run, pipeline, snapshot, graph_json, and node_type_map."""
        _tracker = self._dispatch_phase_tracker
        if _tracker is not None:
            _tracker.enter_phase("executor_running")
        if _tracker is not None:
            _tracker.enter_phase("loading_context")
        return await self._load_execution_context(
            run_id=run_id,
            org_id=org_id,
            input_payload=input_payload,
        )

    async def _execute_policy_gate_pin(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        snapshot: PipelineSnapshot,
        claim_token: str | None,
    ) -> Run | None:
        """Check policy gate pin integrity; return terminal run if pin mismatch."""
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            await _ref("set_mutation_row_lock_timeout")(session)
            return await self._check_policy_gate_pin(
                session,
                run_id=run_id,
                org_id=org_id,
                snapshot=snapshot,
                claim_token=claim_token,
            )

    async def _execute_load_eval_defs(
        self,
        *,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        policy_gate_pins_json: list[dict[str, Any]] | None,
    ) -> dict[str, list[EvalDefDTO]]:
        """Load eval definitions for conditional HITL gating."""
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            eval_rows = await self._load_eval_defs_for_pipeline(session, pipeline_id)
        return self._build_eval_defs_by_node(
            eval_rows,
            org_id,
            pipeline_id,
            policy_gate_pins_json,
        )

    async def _execute_pre_stream_checks(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        graph_json: dict[str, Any],
        snapshot_id: uuid.UUID,
        claim_token: str | None,
    ) -> Run | None:
        """Run capacity and spend ceiling checks; return terminal run if blocked."""
        _tracker = self._dispatch_phase_tracker
        if _tracker is not None:
            _tracker.enter_phase("capacity_check")
        capacity_run = await self._check_capacity(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            graph_json=graph_json,
            snapshot_id=snapshot_id,
        )
        if capacity_run.status != "running":
            return capacity_run
        if _tracker is not None:
            _tracker.enter_phase("spend_check")
        ceiling_run = await self._check_spend_ceiling_gate(run_id=run_id, org_id=org_id, claim_token=claim_token)
        if ceiling_run is not None:
            return ceiling_run
        return None

    async def _execute_init_environment(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        graph_json: dict[str, Any],
    ) -> tuple[ModelBackendHub | None, Any, RunEventBroker, bool]:
        """Initialize run environment: model hub, connector hub, broker, sandbox flag."""
        return await self._init_run_environment(
            org_id=org_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            graph_json=graph_json,
        )

    def _execute_handle_stream_exceptions(
        self,
        exc: Exception,
        *,
        run_id: uuid.UUID,
        node_token_usage: dict[str, Any] | None,
        retry_policy_eligible: bool,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None, bool]:
        """Handle exceptions from _prepare_and_stream; return updated status tuple."""
        error_detail = _ref("_enrich_failure_detail")(_ref("_failure_reason")(exc), exc)
        _log.error(
            "pipeline.execution_error",
            extra={
                "run_id": str(run_id),
                **(
                    {
                        "failure_build_sha": _ref("_failure_build_sha")(),
                        "failing_column": _ref("_failure_column")(exc),
                    }
                    if _ref("_ATTRIBUTION_AVAILABLE")
                    else {}
                ),
            },
            exc_info=exc,
        )
        final_status = "failed"
        error_code = type(exc).__name__
        _provider_refusal = _ref("non_retryable_provider_code")(str(exc)) or _ref("non_retryable_provider_code")(error_detail)
        if _provider_refusal is not None:
            error_code = _provider_refusal
        return final_status, error_code, error_detail, node_token_usage, retry_policy_eligible

    async def _execute_retry_policy(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_retry_policy: dict[str, Any] | None,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        is_correction_run: bool,
        graph_idempotent: bool,
        graph_json: dict[str, Any],
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        broker: RunEventBroker,
        retry_policy_eligible: bool,
    ) -> tuple[str, str, str | None, str | None]:
        """Apply retry policy; return potentially updated status tuple."""

        retry_decision = "none"
        if not retry_policy_eligible:
            _log.info(
                "pipeline.retry_policy_skipped_tier_refused",
                extra={"run_id": str(run_id)},
            )
        elif pipeline_retry_policy is not None:
            retry_decision = await _ref("maybe_retry_after_policy")(
                run_id=run_id,
                org_id=org_id,
                pipeline_retry_policy=pipeline_retry_policy,
                final_status=final_status,
                error_code=error_code,
                error_detail=error_detail,
                is_correction_run=is_correction_run,
                graph_idempotent=graph_idempotent,
                graph_json=graph_json,
                model_backend_hub=model_backend_hub,
                connector_hub=connector_hub,
                broker=broker,
                session_factory=self._session_factory,
                claim_token=self._claim_token,
                fenced_pending_reset_fn=self._fenced_pending_reset,
                cleanup_run_resources_fn=self._cleanup_run_resources,
                record_attempt_reset_fn=self._record_attempt_reset,
                probe_script_lease_fn=self._probe_script_lease,
                read_retry_attempt_state_fn=self._read_retry_attempt_state,
                graph_has_script_mode_fn=_ref("_graph_has_script_mode"),
                node_timeouts=self._node_timeouts.values(),
            )
        if retry_decision == "side_effect_unknown":
            final_status = "failed"
            error_code = _ref("_ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN")
            error_detail = _ref("_sanitize_detail")(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); not retried — needs human review.",
                limit=5000,
            )
        return retry_decision, final_status, error_code, error_detail

    async def resume(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        resume_data: dict[str, Any],
        claim_token: str | None = None,
        expected_claim_status: str | None = None,
        check_sandbox_capacity: bool = True,
    ) -> Run:
        """Resume a run that was interrupted for HITL review.

        Loads the checkpointed graph state, injects *resume_data* as
        ``_hitl_decision``, and streams the graph until completion or the
        next interrupt. *claim_token* is the fenced-lease authority captured at
        resume start (see :meth:`execute`).
        """
        self._claim_token = claim_token

        # Phase 1: Load run, validate claim, enforce capacity, validate snapshot, check pin, load eval defs
        async with self._session_factory() as session, session.begin():
            result = await self._resume_load_and_validate(
                session=session,
                run_id=run_id,
                org_id=org_id,
                claim_token=claim_token,
                expected_claim_status=expected_claim_status,
                check_sandbox_capacity=check_sandbox_capacity,
            )
            if result[1] is None:  # pin_run was returned
                return result[0]
            run, snapshot, graph_json, node_type_map = result
            eval_defs_by_node = await self._resume_load_eval_defs(
                session=session,
                org_id=org_id,
                run_id=run_id,
                pipeline_id=run.pipeline_id,
                policy_gate_pins_json=snapshot.policy_gate_pins_json,
            )

        pipeline_id = run.pipeline_id
        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id

        # Phase 2: Load pipeline and capture scalars
        async with self._session_factory() as session, session.begin():
            pipeline, scalars = await self._resume_prepare_second_transaction(
                session=session,
                org_id=org_id,
                pipeline_id=pipeline_id,
                run_id=run_id,
            )
        guard = scalars["guard"]

        if not self._checkpointer_conn_string:
            raise RuntimeError("Cannot resume without a checkpointer configured")

        # Phase 3: Compile graph
        compiled, _node_idempotency_key = self._resume_compile_graph(
            pipeline=pipeline,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            graph_json=graph_json,
            eval_defs_by_node=eval_defs_by_node,
            pipeline_retry_policy=scalars["pipeline_retry_policy"],
            scalars=scalars,
            org_id=org_id,
        )

        # Phase 4: Setup runtime
        broker, model_backend_hub = await self._resume_setup_runtime(
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot=snapshot,
            run_id=run_id,
        )

        # Phase 5: Stream and handle errors
        config, node_ids, node_token_budgets = self._resume_stream_inputs(graph_json=graph_json, thread_id=thread_id)
        # One shared instance threaded into BOTH the stream phase and finalize:
        # ``_stream_graph`` mutates it IN PLACE, so a fresh dict at finalize
        # would lose the resumed segment's outputs (FAR-505 regression).
        completed_node_outputs: dict[str, Any] = {}
        final_status, error_code, error_detail, node_token_usage = await self._resume_stream_phase(
            compiled=compiled,
            config=config,
            node_ids=node_ids,
            broker=broker,
            run_id=run_id,
            pipeline_id=pipeline_id,
            org_id=org_id,
            resume_data=resume_data,
            completed_node_outputs=completed_node_outputs,
            guard=guard,
            node_token_budgets=node_token_budgets,
            eval_defs_by_node=eval_defs_by_node,
            node_type_map=node_type_map,
            model_backend_hub=model_backend_hub,
        )

        # Phase 6: Finalize
        return await self._resume_cleanup_and_finalize(
            final_status=final_status,
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            node_type_map=node_type_map,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_ids=node_ids,
        )

    async def _resume_stream_phase(
        self,
        *,
        compiled: Any,
        config: dict[str, Any],
        node_ids: set[str],
        broker: RunEventBroker,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
        resume_data: dict[str, Any],
        completed_node_outputs: dict[str, Any],
        guard: Any,
        node_token_budgets: dict[str, int],
        eval_defs_by_node: dict[str, list[EvalDefDTO]],
        node_type_map: dict[str, str],
        model_backend_hub: ModelBackendHub | None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Stream the resumed graph, then tear the runtime down exactly once.

        Returns ``(final_status, error_code, error_detail, node_token_usage)``.
        """
        final_status: str = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        try:
            final_status, error_code, error_detail, node_token_usage = await self._resume_stream_and_handle_errors(
                compiled=compiled,
                config=config,
                node_ids=node_ids,
                broker=broker,
                run_id=run_id,
                pipeline_id=pipeline_id,
                org_id=org_id,
                resume_data=resume_data,
                completed_node_outputs=completed_node_outputs,
                guard=guard,
                node_token_budgets=node_token_budgets,
                eval_definitions_by_node=eval_defs_by_node,
                node_type_map=node_type_map,
            )
        finally:
            # Teardown MUST stay exception-safe: a CancelledError or a
            # re-raised non-checkpointer RuntimeError from the stream phase
            # would otherwise leak process-global contextvars and the model
            # hub (FAR-1534).
            await self._teardown_resume_runtime(
                model_backend_hub=model_backend_hub,
                final_status=final_status,
                run_id=run_id,
            )
        return final_status, error_code, error_detail, node_token_usage

    def _resume_stream_inputs(
        self,
        *,
        graph_json: dict[str, Any],
        thread_id: str,
    ) -> tuple[dict[str, Any], set[str], dict[str, int]]:
        """Build the checkpoint config, node-id set and token budgets for a resume."""
        config = {"configurable": {"thread_id": thread_id}}
        node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
        node_token_budgets = {str(n["id"]): n["token_budget"] for n in graph_json.get("nodes", []) if n.get("token_budget") is not None}
        return config, node_ids, node_token_budgets

    async def _teardown_resume_runtime(
        self,
        *,
        model_backend_hub: ModelBackendHub | None,
        final_status: str,
        run_id: uuid.UUID,
    ) -> None:
        """Clear the process-global runtime state a resume installed."""
        _ref("set_cancellation_check")(None)
        _ref("set_audit_hook")(None)
        _ref("set_model_backend_hub")(None)
        if model_backend_hub is not None:
            await _ref("_teardown_hub")(model_backend_hub)
        if final_status not in ("awaiting_human", "complete"):
            _ref("get_registry")().close(run_id)

    async def execute(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
        claim_token: str | None = None,
    ) -> Run:
        """Execute the run to completion. Returns the final Run row.

        *claim_token* is the fenced-lease authority captured at execute start:
        it guards the capacity demotion, the NodeCancelledError pending-reset,
        and is seeded into LangGraph state as ``_claim_lease`` for the sandbox
        dispatch marker. A superseded executor (whose token was rotated by a
        successor) cannot demote/complete the run out from under it.

        A capacity-blocked run is returned ``pending`` (with its reason marker)
        — there is NO in-process retry loop (plan F3b); recovery is owned by
        ``dispatcher_reconcile`` (cron_helpers) and ``stale_run_recovery_sweep``.
        """
        self._claim_token = claim_token

        # Phase 1: Load execution context
        run, pipeline, snapshot, graph_json, node_type_map = await self._execute_load_and_validate(
            run_id=run_id,
            org_id=org_id,
            input_payload=input_payload,
        )

        # Phase 2: Check policy gate pin
        pin_run = await self._execute_policy_gate_pin(
            run_id=run_id,
            org_id=org_id,
            snapshot=snapshot,
            claim_token=claim_token,
        )
        if pin_run is not None:
            return pin_run

        variant_config_snapshot = run.variant_config_snapshot
        scalars = self._capture_execution_scalars(pipeline, run)
        pipeline_id = scalars["pipeline_id"]
        max_concurrent = scalars["max_concurrent"]
        pipeline_retry_policy = scalars["pipeline_retry_policy"]
        guard = scalars["guard"]
        snapshot_id = scalars["snapshot_id"]
        thread_id = scalars["thread_id"]
        is_correction_run = scalars["is_correction_run"]
        run_number = scalars["run_number"]

        # Phases 3-4: Load eval definitions, then run the pre-stream gates
        # (capacity, spend ceiling).
        eval_defs_by_node, pre_stream_run = await self._execute_admit_pre_stream(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            graph_json=graph_json,
            snapshot_id=snapshot_id,
            claim_token=claim_token,
            policy_gate_pins_json=snapshot.policy_gate_pins_json,
        )
        if pre_stream_run is not None:
            return pre_stream_run

        # Phase 5: Initialize environment
        model_backend_hub, connector_hub, broker, single_sandbox_node = await self._execute_init_environment(
            org_id=org_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            graph_json=graph_json,
        )
        graph_idempotent = _ref("_graph_is_idempotent")(graph_json)

        # Phase 6: Prepare stream config
        scope, stream_config = self._build_execute_stream_config(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            thread_id=thread_id,
            snapshot=snapshot,
            input_payload=input_payload,
            variant_config_snapshot=variant_config_snapshot,
            graph_json=graph_json,
            eval_defs_by_node=eval_defs_by_node,
            guard=guard,
            node_type_map=node_type_map,
            pipeline_node_timeout_seconds=pipeline.node_timeout_seconds,
            broker=broker,
            pipeline_retry_policy=pipeline_retry_policy,
            run_number=run_number,
        )

        # Phase 7: Stream and handle exceptions
        final_status, error_code, error_detail, node_token_usage, retry_policy_eligible = await self._execute_stream_phase(
            scope=scope,
            stream_config=stream_config,
            graph_idempotent=graph_idempotent,
            single_sandbox_node=single_sandbox_node,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
        )

        # Phases 8-10: apply the retry policy, run the post-stream tail and
        # teardown, then finalize.
        return await self._finish_execute_after_stream(
            scope=scope,
            stream_config=stream_config,
            pipeline_retry_policy=pipeline_retry_policy,
            is_correction_run=is_correction_run,
            graph_idempotent=graph_idempotent,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            retry_policy_eligible=retry_policy_eligible,
        )

    async def _execute_admit_pre_stream(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        graph_json: dict[str, Any],
        snapshot_id: uuid.UUID,
        claim_token: str | None,
        policy_gate_pins_json: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, list[EvalDefDTO]], Run | None]:
        """Load the run's eval definitions, then run the capacity/spend gates.

        Returns ``(eval_defs_by_node, blocked_run)`` — ``blocked_run`` is the
        terminal/deferred run the caller must return, or ``None`` to proceed.
        """
        eval_defs_by_node = await self._execute_load_eval_defs(
            org_id=org_id,
            pipeline_id=pipeline_id,
            policy_gate_pins_json=policy_gate_pins_json,
        )
        pre_stream_run = await self._execute_pre_stream_checks(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            graph_json=graph_json,
            snapshot_id=snapshot_id,
            claim_token=claim_token,
        )
        return eval_defs_by_node, pre_stream_run

    def _build_execute_stream_config(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        snapshot_id: uuid.UUID,
        thread_id: str,
        snapshot: PipelineSnapshot,
        input_payload: dict[str, Any],
        variant_config_snapshot: dict[str, Any] | None,
        graph_json: dict[str, Any],
        eval_defs_by_node: dict[str, list[EvalDefDTO]],
        guard: Any,
        node_type_map: dict[str, str],
        pipeline_node_timeout_seconds: int,
        broker: RunEventBroker,
        pipeline_retry_policy: dict[str, Any] | None,
        run_number: int,
    ) -> tuple[_RunScope, _StreamConfig]:
        """Build the run scope and stream config for the streaming phases.

        Returns ``(scope, stream_config)``. ``stream_config.completed_node_outputs``
        is ONE shared instance for the whole run: ``_stream_graph`` mutates it
        IN PLACE, so the stream config and the finalize call MUST receive the
        same dict or the accumulated outputs never reach finalize_cost.
        """
        run_ref = _ref("rc").build_run_ref(str(pipeline_id), run_number)

        def _node_idempotency_key(node_id: str, _state: dict[str, Any]) -> str | None:
            key: str | None = _ref("rc").node_idempotency_key(run_ref=run_ref, node_ref=node_id)
            return key

        node_ids = {str(n["id"]) for n in graph_json.get("nodes", [])}
        completed_node_outputs: dict[str, Any] = {}
        scope = _RunScope(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            snapshot_id=snapshot_id,
            thread_id=thread_id,
        )
        stream_config = _StreamConfig(
            snapshot=snapshot,
            input_payload=input_payload,
            variant_config_snapshot=variant_config_snapshot,
            graph_json=graph_json,
            eval_defs_by_node=eval_defs_by_node,
            node_ids=node_ids,
            completed_node_outputs=completed_node_outputs,
            guard=guard,
            node_type_map=node_type_map,
            pipeline_node_timeout_seconds=pipeline_node_timeout_seconds,
            broker=broker,
            pipeline_retry_policy=pipeline_retry_policy,
            idempotency_key=_node_idempotency_key,
        )
        return scope, stream_config

    async def _execute_stream_phase(
        self,
        *,
        scope: _RunScope,
        stream_config: _StreamConfig,
        graph_idempotent: bool,
        single_sandbox_node: bool,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None, bool]:
        """Stream the graph and translate every exit into a status tuple.

        Returns ``(final_status, error_code, error_detail, node_token_usage,
        retry_policy_eligible)``. The ``requeue`` / ``superseded`` transient
        decisions re-raise the original exception (via
        ``_apply_transient_decision``) so the SAQ job retry path re-dispatches.
        """
        graph_json = stream_config.graph_json
        broker = stream_config.broker
        completed_node_outputs = stream_config.completed_node_outputs
        final_status = "failed"
        error_code: str | None = None
        error_detail: str | None = None
        node_token_usage: dict[str, Any] | None = None
        retry_policy_eligible = True
        gate_suppressed = False

        try:
            final_status, error_code, error_detail, node_token_usage = await self._prepare_and_stream(
                scope=scope,
                config=stream_config,
            )
        except asyncio.CancelledError:
            raise
        except RouterNoMatchError as exc:
            _log.info("pipeline.router_no_match", extra={"run_id": str(scope.run_id), "detail": str(exc)})
            final_status = "router_no_match"
            error_code = "router.no_match"
            error_detail = _ref("_sanitize_detail")(str(exc), limit=None)
        except (NodeCancelledError, SandboxNodeFailedError) as exc:
            node_token_usage = _ref("merge_recovered_token_usage")(node_token_usage, exc)
            tier_refused = isinstance(exc, SandboxTierRefusedError)
            if tier_refused:
                _log.warning(
                    "pipeline.tier_refused_terminal",
                    extra={"run_id": str(scope.run_id), "exc_type": type(exc).__name__},
                )
                final_status = "failed"
                error_code = _ref("_ERROR_CODE_SANDBOX_TIER_REFUSED")
                error_detail = _ref("_sanitize_detail")(str(exc), limit=None)
                retry_policy_eligible = False
            else:
                _log.warning(
                    "pipeline.node_cancelled_transient",
                    extra={"run_id": str(scope.run_id), "exc_type": type(exc).__name__},
                )
                transient = await self._decide_transient_failure(
                    exc=exc,
                    run_id=scope.run_id,
                    org_id=scope.org_id,
                    graph_json=graph_json,
                    graph_idempotent=graph_idempotent,
                    single_sandbox_node=single_sandbox_node,
                    model_backend_hub=model_backend_hub,
                    connector_hub=connector_hub,
                    broker=broker,
                    stall_requested=self._stall_requested,
                    pipeline_retry_policy=stream_config.pipeline_retry_policy,
                )
                gate_suppressed, final_status, error_code, error_detail = self._apply_transient_decision(
                    transient,
                    completed_node_outputs=completed_node_outputs,
                    gate_suppressed=gate_suppressed,
                )
        except Exception as exc:
            (
                final_status,
                error_code,
                error_detail,
                node_token_usage,
                retry_policy_eligible,
            ) = self._execute_handle_stream_exceptions(
                exc=exc,
                run_id=scope.run_id,
                node_token_usage=node_token_usage,
                retry_policy_eligible=retry_policy_eligible,
            )
        return final_status, error_code, error_detail, node_token_usage, retry_policy_eligible

    async def _finish_execute_after_stream(
        self,
        *,
        scope: _RunScope,
        stream_config: _StreamConfig,
        pipeline_retry_policy: dict[str, Any] | None,
        is_correction_run: bool,
        graph_idempotent: bool,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        node_token_usage: dict[str, Any] | None,
        retry_policy_eligible: bool,
    ) -> Run:
        """Apply the retry policy, run the post-stream tail, then finalize."""
        graph_json = stream_config.graph_json
        node_type_map = stream_config.node_type_map
        completed_node_outputs = stream_config.completed_node_outputs
        node_ids = stream_config.node_ids
        # Phase 8: Apply retry policy
        _retry_decision, final_status, error_code, error_detail = await self._execute_retry_policy(
            run_id=scope.run_id,
            org_id=scope.org_id,
            pipeline_retry_policy=pipeline_retry_policy,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            is_correction_run=is_correction_run,
            graph_idempotent=graph_idempotent,
            graph_json=graph_json,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
            broker=stream_config.broker,
            retry_policy_eligible=retry_policy_eligible,
        )

        # Phase 9: Post-stream tail and teardown
        signal_node_type_map = _ref("extend_node_type_map_from_edges")(node_type_map, graph_json)
        final_status, error_code, error_detail = await self._run_post_stream_and_teardown(
            run_id=scope.run_id,
            org_id=scope.org_id,
            pipeline_id=scope.pipeline_id,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            completed_node_outputs=completed_node_outputs,
            node_type_map=signal_node_type_map,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
        )

        # Phase 10: Finalize
        return await self._finalize_then_publish_completed(
            defer_close=final_status == "complete",
            run_id=scope.run_id,
            org_id=scope.org_id,
            pipeline_id=scope.pipeline_id,
            node_type_map=node_type_map,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_ids=node_ids,
        )

    async def _decide_transient_failure(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        graph_json: dict[str, Any],
        graph_idempotent: bool,
        single_sandbox_node: bool,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        broker: RunEventBroker,
        stall_requested: asyncio.Event | None,
        pipeline_retry_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Decide how a transient node-cancelled / sandbox-failed run recovers.

        Loads the transient state, computes superseded / stalled / gate_ok /
        script_lease_ok, then returns a decision dict. The caller
        (``_execute_stream_phase``) performs the side effects that must stay in
        its scope — the idempotency-gate envelope write and the bare re-raise
        for requeue / superseded (which propagates out of execute() before the
        post-stream try/finally). Mutations that are safe to perform here (the
        fenced pending-reset + cleanup) happen in this method so the re-raise
        path stays identical to the inline chain.

        Returns one of ``gate`` / ``requeue`` / ``superseded`` / ``terminal``;
        each decision's semantics are documented on the helper that builds it.
        """
        retries = self._transient_retry_budget(pipeline_retry_policy)
        (
            execution_attempt_count,
            current_token,
            run_markers,
            cancellation_requested,
            idempotency_key,
        ) = await self._load_transient_state(run_id=run_id, org_id=org_id)
        # FAR-2076: the ONE shared measure of executions already consumed —
        # never re-derived here, so this path cannot disagree with the policy /
        # watchdog / reconcile ceilings about what a run has spent.
        execution_attempt_count = _ref("retry_executions_consumed")(
            execution_attempt_count=execution_attempt_count,
        )

        superseded = self._claim_token is not None and current_token is not None and current_token != self._claim_token
        stalled = bool(stall_requested is not None and stall_requested.is_set())

        gate_ok, gated_node_id, script_lease_ok = await self._transient_signals(
            exc=exc,
            run_id=run_id,
            org_id=org_id,
            graph_json=graph_json,
            run_markers=run_markers,
            idempotency_key=idempotency_key,
            superseded=superseded,
            stalled=stalled,
            cancellation_requested=cancellation_requested,
            single_sandbox_node=single_sandbox_node,
        )
        if gate_ok and gated_node_id is not None:
            return self._transient_gate_decision(run_id=run_id, gated_node_id=gated_node_id)
        provider_refusal = self._provider_refusal_decision(exc=exc, run_id=run_id, broker=broker)
        if provider_refusal is not None:
            return provider_refusal
        # FAR-1800: classify the transient failure's retained stdout ONCE, so the
        # requeue gate and the terminal code agree. The exception message embeds
        # the retained stdout tail (the no-output diagnostic), which is where a
        # provider refusal (HTTP 402 "Insufficient account funds") appears.
        # Fail open: an unrecognised failure yields None and keeps today's path.
        failure_code = _ref("classify_provider_error")(str(exc))
        # FAR-2562: the failure's OWN canonical class code — the same code the
        # terminal write publishes (``sandbox.no_output_json`` for a base
        # SandboxNodeFailedError, its own code for a mapped subclass). The
        # registry's per-class attempt cap is keyed on it, so a deterministic
        # no-output death may consume at most that many executions. Kept
        # SEPARATE from ``failure_code`` (the provider classification) so the
        # FAR-1800 non-retryable gate keeps judging exactly what it judged
        # before. Deliberately None for:
        #   * ``NodeCancelledError`` — not a SandboxNodeFailedError, so a plain
        #     cancellation carries no class code and its behaviour is unchanged;
        #   * the FAR-136 hang death ("likely hung"), whose terminal code stays
        #     the generic ``node_cancelled`` — capping it under the no-output
        #     class would silently change that code.
        failure_class_code = _ref("_sandbox_failure_class_code")(exc)
        if await self._try_transient_requeue(
            exc=exc,
            run_id=run_id,
            org_id=org_id,
            model_backend_hub=model_backend_hub,
            connector_hub=connector_hub,
            execution_attempt_count=execution_attempt_count,
            retries=retries,
            superseded=superseded,
            stalled=stalled,
            script_lease_ok=script_lease_ok,
            graph_idempotent=graph_idempotent,
            failure_code=failure_code,
            failure_class_code=failure_class_code,
        ):
            return {"decision": "requeue"}
        if superseded or stalled:
            # Superseded or watchdog-stalled: the run is owned by a
            # successor or was already terminal-failed by the zombie
            # watchdog — never reset it to pending and never terminal-fail
            # it here. Clean up so the SAQ job retry re-entry gets a fresh
            # broker and no stale contextvars (the caller re-raises).
            await self._cleanup_run_resources(model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id)
            return {"decision": "superseded"}
        return self._terminal_transient_decision(
            exc=exc,
            script_lease_ok=script_lease_ok,
            graph_idempotent=graph_idempotent,
            execution_attempt_count=execution_attempt_count,
            retries=retries,
            failure_code=failure_code,
            failure_class_code=failure_class_code,
            broker=broker,
        )

    def _transient_retry_budget(self, pipeline_retry_policy: dict[str, Any] | None) -> int:
        """The transient retry allowance, bounded by the pipeline's policy ceiling.

        FAR-2076: the transport allowance (settings.saq_run_retries) is one
        budget; the pipeline's retry_policy is the other. Bound this path by
        the policy ceiling so a transient retry can never grant an execution
        the policy refuses. No policy => unchanged.
        """
        from farna.settings import get_settings

        retries = int(get_settings().saq_run_retries)
        allowance: int = _ref("_transient_retry_allowance")(retries, pipeline_retry_policy)
        return allowance

    async def _transient_signals(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        graph_json: dict[str, Any],
        run_markers: dict[str, Any] | None,
        idempotency_key: str | None,
        superseded: bool,
        stalled: bool,
        cancellation_requested: bool,
        single_sandbox_node: bool,
    ) -> tuple[bool, str | None, bool]:
        """Compute the transient path's read-only signals.

        Returns ``(gate_ok, gated_node_id, script_lease_ok)``.

        FAR-228 guard B (retry-suppression — THE INCIDENT FIX): before any
        state mutation, check whether the run ALREADY delivered (a prior
        attempt's marker carries delivery_done=True) for THIS node. If so,
        a transient retry is suppressed: the run completes COMPLETE with
        error_code harness.idempotency_gate instead of burning the retry
        budget and re-sending the side effect. ORDERING INVARIANT: computed
        AFTER superseded/stalled (the caller's) and BEFORE any mutation —
        final_status/error_code are first mutated only in the
        retries-exhausted branch. ``run_markers`` was loaded by
        ``_load_transient_state`` (repo reader, same transaction) — no extra
        SELECT here.

        FAR-296 Phase 2: before ANY requeue of a script-mode run, prove no
        script process could still be alive (stale-claim lease probe).
        A stale ``script_executing`` lease means a script may have run —
        requeue is forbidden (exactly-once). Only when the probe proves no
        live lease does the run stay eligible for the fenced reset.
        The probe is computed BEFORE the gate/retry chain so
        the idempotency gate stays the FIRST branch (it must suppress the
        pending-reset when a delivery marker is present), and so the
        pending-reset branch remains reachable for BOTH script-mode and
        non-script-mode graphs (a stale lease simply disqualifies it).
        """
        gate_ok = self._idempotency_gate_ok(
            exc=exc,
            run_markers=run_markers,
            run_id=run_id,
            idempotency_key=idempotency_key,
            superseded=superseded,
            stalled=stalled,
            cancellation_requested=cancellation_requested,
            single_sandbox_node=single_sandbox_node,
            # FAR-438: the transient path has no separate fan-out / content-edit
            # context (exc.node_id already encodes parent+index for fan-out
            # children; there is no connector payload to fold), so index/payload
            # are None here — consistent with the marker-write side's defaults.
            index=None,
            payload=None,
        )
        # Only SandboxNodeFailedError carries a node_id — the gate is
        # keyed on it, so a None node_id (plain NodeCancelledError) can
        # never suppress.
        gated_node_id = exc.node_id if isinstance(exc, SandboxNodeFailedError) else None
        script_lease_ok = await self._script_lease_ok(run_id=run_id, org_id=org_id, graph_json=graph_json)
        return gate_ok, gated_node_id, script_lease_ok

    def _transient_gate_decision(self, *, run_id: uuid.UUID, gated_node_id: str) -> dict[str, Any]:
        """The idempotency gate suppressed the retry: the run completes COMPLETE."""
        _log.warning(
            "pipeline.idempotency_gate.suppressed_retry",
            extra={"run_id": str(run_id), "node_id": gated_node_id},
        )
        return {
            "decision": "gate",
            "final_status": "complete",
            "error_code": _ref("_ERROR_CODE_HARNESS_IDEMPOTENCY_GATE"),
            "error_detail": "delivery already sent; transient retry suppressed by idempotency gate",
            "gated_node_id": gated_node_id,
        }

    def _provider_refusal_decision(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_id: uuid.UUID,
        broker: RunEventBroker,
    ) -> dict[str, Any] | None:
        """A provider credential / funding refusal is NON-RETRYABLE (FAR-2006).

        Classify it from the retained sandbox output carried in the exception
        BEFORE the retry gate: re-dispatching a 401/402 can never succeed, so
        retrying only burns a slot (the measured failure mode was three
        ~10-minute attempts per run, a whole night at zero throughput). The
        idempotency gate keeps priority (a delivery already sent is still the
        stronger fact). Returns ``None`` when the exception carries no
        provider refusal.
        """
        provider_code = _ref("non_retryable_provider_code")(str(exc))
        if provider_code is None:
            return None
        error_detail = _ref("_sanitize_detail")(
            f"Model provider refused the run ({provider_code}); not retried — re-dispatching cannot succeed: {exc}",
            limit=5000,
        )
        _log.warning(
            "pipeline.provider_failure_terminal",
            extra={"run_id": str(run_id), "error_code": provider_code, "exc_type": type(exc).__name__},
        )
        broker.publish("run_failed", {"error": provider_code, "detail": error_detail})
        return {
            "decision": "terminal",
            "final_status": "failed",
            "error_code": provider_code,
            "error_detail": error_detail,
        }

    async def _try_transient_requeue(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        execution_attempt_count: int,
        retries: int,
        superseded: bool,
        stalled: bool,
        script_lease_ok: bool,
        graph_idempotent: bool,
        failure_code: str | None,
        failure_class_code: str | None,
    ) -> bool:
        """Fence the run back to ``pending`` when a retry is still allowed.

        Returns ``True`` when the requeue happened (the caller returns the
        ``requeue`` decision and re-raises), ``False`` when the requeue gate
        refused and the caller must continue down the decision chain.
        """
        if _can_fenced_requeue(
            execution_attempt_count,
            retries,
            superseded,
            stalled,
            script_lease_ok,
            graph_idempotent,
            error_code=failure_code,
            failure_class_code=failure_class_code,
        ):
            # Fenced pending-reset: a conditional UPDATE guarded by OUR
            # captured claim token + status='running' so a superseded
            # original cannot demote the successor's running row, a stalled
            # (watchdog-cancelled) executor cannot resurrect a run the
            # watchdog just failed, and a cancellation cannot be reversed.
            reset_rowcount = await self._fenced_pending_reset(run_id=run_id, org_id=org_id)
            if reset_rowcount is None:
                # FAR-2076: the reset TIMED OUT, so the run was NOT handed back
                # to ``pending`` — it is left ``running`` for the age backstop.
                # Re-raising still cannot re-execute it: the SAQ retry's claim
                # only accepts ``pending`` or a STALE running row, and this row
                # was just heartbeating under our token. Never terminalise it.
                _log.warning(
                    "pipeline.transient_requeue_reset_timed_out run=%s — the fenced pending-reset timed out; the row is left running for the nodeless/age backstop (the re-raised retry cannot claim a fresh running row)",
                    run_id,
                )
            elif reset_rowcount:
                # FAR-2078: the attempt ended in a CONFIRMED reset. Close its row
                # with the cause the reset just erased from ``runs.error_code``
                # — this is the history the table exists to retain. Only a
                # confirmed reset (rowcount 1): a fence loss (0) belongs to a
                # successor's row and a timeout (None) is closed by the
                # backstop's terminal write.
                await self._record_attempt_reset(
                    run_id=run_id,
                    org_id=org_id,
                    error_code=failure_code or type(exc).__name__,
                )
            # The caller's re-raise propagates out of execute() BEFORE the
            # post-stream try/finally, so run its cleanup here: clear the
            # cancellation check + hubs and close the run's broker so the
            # retry re-entry gets a fresh broker and no stale contextvars.
            await self._cleanup_run_resources(model_backend_hub=model_backend_hub, connector_hub=connector_hub, run_id=run_id)
            return True
        return False

    def _terminal_transient_decision(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        script_lease_ok: bool,
        graph_idempotent: bool,
        execution_attempt_count: int,
        retries: int,
        failure_code: str | None,
        failure_class_code: str | None,
        broker: RunEventBroker,
    ) -> dict[str, Any]:
        """Terminal failure with a MEANINGFUL code and a published run_failed.

        Retries exhausted — or requeue was REFUSED because the failure class is
        non-retryable (FAR-1800) or has spent its registry execution cap
        (FAR-2562) — so no (further) retry was attempted at all. Publish the
        run_failed event so WS subscribers get a live failure notification,
        consistent with every other terminal-failure path in this file.

        Write cap: 5000, NOT 500 — the transient node-cancelled detail is the
        only place the FAR-197 no-output.json diagnostic (stdout/stderr tails,
        the E2B log tail where the kill reason lives) reaches the user. It is
        bounded by the builder to fit the 5000-char sanitizer/column cap
        (runs.error_detail is Text, widened from String(5000) by migration
        0199), and every detail read surface (run-detail REST + MCP) presents
        at limit=5000; list surfaces truncate to 200 by design. A 500-char
        write cap cut the stderr + log tails entirely for large-output
        failures.
        """
        error_code, error_detail = self._transient_failure_detail(
            exc=exc,
            script_lease_ok=script_lease_ok,
            graph_idempotent=graph_idempotent,
            execution_attempt_count=execution_attempt_count,
            retries=retries,
            non_retryable_code=failure_code if _ref("_is_non_retryable_error_code")(failure_code) else None,
            failure_class_code=failure_class_code,
            attempt_cap=_ref("_failure_class_attempt_cap")(failure_class_code),
        )
        broker.publish("run_failed", {"error": error_code, "detail": error_detail})
        return {
            "decision": "terminal",
            "final_status": "failed",
            "error_code": error_code,
            "error_detail": error_detail,
        }

    def _apply_transient_decision(
        self,
        transient: dict[str, Any],
        *,
        completed_node_outputs: dict[str, Any],
        gate_suppressed: bool,
    ) -> tuple[bool, str, str | None, str | None]:
        """Apply a transient-failure decision dict to execute()'s locals.

        Returns ``(gate_suppressed, final_status, error_code, error_detail)``.
        For ``requeue``/``superseded`` the bare ``raise`` propagates the
        original exception out of ``execute()`` so the SAQ job retry path
        re-dispatches — the fenced pending-reset + cleanup already happened
        inside ``_decide_transient_failure``.
        """
        if transient["decision"] == "gate":
            # SKIP the pending-reset, the re-raise and the run_failed publish
            # — fall through to the existing finalization with
            # final_status="complete". FAR-1534: run_completed is published
            # post-finalize (by ``_finalize_then_publish_completed``), not here.
            completed_node_outputs[transient["gated_node_id"]] = _ref("_idempotency_gate_skipped_envelope")(transient["gated_node_id"])
            return True, transient["final_status"], transient["error_code"], transient["error_detail"]
        if transient["decision"] in ("requeue", "superseded"):
            raise
        # terminal (retries exhausted) — or gate, whose decision dict carries
        # the same final_status / error_code / error_detail it set above.
        return gate_suppressed, transient["final_status"], transient["error_code"], transient["error_detail"]

    async def _load_execution_context(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        input_payload: dict[str, Any],
    ) -> tuple[Run, Pipeline, PipelineSnapshot, dict[str, Any], dict[str, str]]:
        """Load the run + pipeline + snapshot in one short-lived transaction.

        Runs the pre-run GraphValidator (incl. the malformed-retry-policy
        check) and derives the frozen node-type map. Queries directly to avoid
        SQLAlchemy async lazy-load (MissingGreenlet). Raises RunNotFoundError /
        GraphValidationError exactly as the original inline block did.
        """
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            run = await _ref("get_run")(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            # Opt out of the global soft-delete filter -- an in-flight
            # run's pipeline may have been soft-deleted; the pipeline must still
            # be loadable for graph construction.
            from farna.db.soft_delete import include_soft_deleted

            pipeline_result = await session.execute(include_soft_deleted(_ref("select")(Pipeline).where(Pipeline.id == run.pipeline_id)))
            pipeline = pipeline_result.scalar_one()
            snapshot_result = await session.execute(_ref("select")(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
            snapshot = snapshot_result.scalar_one()
            graph_json: dict[str, Any] = snapshot.graph_json

            # FROZEN node-type map — captured ONCE per run at run start (§1.6)
            # and passed into finalize_cost at every pause and resume.
            node_type_map = _ref("derive_node_type_map")(graph_json)

            # Pre-run validation — blocks execution on errors.
            validation = await _ref("GraphValidator")().validate_for_run(snapshot, input_payload, session)
            if not validation.is_valid:
                raise GraphValidationError(validation.issues, run_id)

            # A malformed pipeline retry_policy must not silently disable retries
            # at run time — surface it as a hard validation error (default no-policy
            # is unaffected). Only dict values carry a policy; None/non-dicts (e.g.
            # legacy rows) are the no-policy default and _retry_after_policy
            # already fail-safes them to no retry.
            retry_policy_check = ValidationResult()
            _retry_policy_value = getattr(pipeline, "retry_policy", None)
            if isinstance(_retry_policy_value, dict):
                # Core shape faults (on / max_retries) stay HARD errors at run
                # start — a malformed core policy silently disables retries.
                _ref("GraphValidator").check_retry_policy(_retry_policy_value, retry_policy_check)
                if not retry_policy_check.is_valid:
                    raise GraphValidationError(retry_policy_check.issues, run_id)
                # FAR-525: the OPTIONAL backoff_schedule is validated SEPARATELY
                # and is NON-BLOCKING at run start — the runtime resolver
                # fail-opens to the hardcoded default schedule, so a faulting
                # schedule must warn loudly but never brick the run (direct DB
                # writes and future bounds tightening must not strand runs).
                schedule_check = ValidationResult()
                _ref("GraphValidator").check_retry_policy_schedule(_retry_policy_value, schedule_check)
                for issue in schedule_check.issues:
                    _log.warning(
                        "pipeline.retry_policy_schedule_malformed",
                        extra={"run_id": str(run_id), "code": issue.code, "detail": issue.message},
                    )
        return run, pipeline, snapshot, graph_json, node_type_map

    def _capture_execution_scalars(self, pipeline: Pipeline, run: Run) -> dict[str, Any]:
        """Capture scalar attributes from the run + pipeline before sessions close."""
        pipeline_id = run.pipeline_id
        max_concurrent = pipeline.max_concurrent_runs
        pipeline_retry_policy: dict[str, Any] = {}
        try:
            raw_policy = getattr(pipeline, "retry_policy", None)
            if isinstance(raw_policy, dict):
                pipeline_retry_policy = raw_policy
        except Exception:
            # A malformed/legacy retry_policy must never crash the run —
            # default to no retry.
            pipeline_retry_policy = {}
        guard = _ref("RunawayGuard")(
            max_duration_seconds=pipeline.max_duration_seconds,
            max_steps=pipeline.max_steps,
            token_budget=pipeline.token_budget,
        )

        snapshot_id = run.snapshot_id
        thread_id = run.langgraph_thread_id
        # FAR-210: correction runs are EXCLUDED from retry_policy re-dispatch.
        # A single-node correction has a fixed bounded retry budget owned by the
        # correction path itself; the pipeline retry policy must never
        # re-dispatch a correction run (no chained corrections).
        run_trigger_type = getattr(run, "trigger_type", "") or ""
        is_correction_run = run_trigger_type == "correction"

        return {
            "pipeline_id": pipeline_id,
            "max_concurrent": max_concurrent,
            "pipeline_retry_policy": pipeline_retry_policy,
            "guard": guard,
            "snapshot_id": snapshot_id,
            "thread_id": thread_id,
            "is_correction_run": is_correction_run,
            "run_number": int(getattr(run, "run_number", 0) or 0),
        }

    async def _init_run_environment(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        graph_json: dict[str, Any],
    ) -> tuple[ModelBackendHub | None, Any | None, RunEventBroker, bool]:
        """Set up the run-scoped execution environment (broker + hubs + otel).

        Returns ``(model_backend_hub, connector_hub, broker, single_sandbox_node)``.
        """
        broker = _ref("get_registry")().get_or_create(run_id)
        _ref("set_cancellation_check")(self._check_db_cancellation(org_id, run_id))
        _ref("set_audit_hook")(self._dispatch_context_write_audit(org_id, run_id))
        self._otel_bridge.set_run_context(str(org_id), str(pipeline_id))

        # Load model backends for this run's org — provides LLM access to agent nodes.
        model_backend_hub = await self._init_model_backend_hub(org_id)
        # Load connector hub for this run's org — provides connector access to connector nodes.
        # FAR-418/FAR-435: wire fetch-time capability scoping — the run-level
        # allowed_connectors (node capability_scope union Agent
        # connector_type_refs grants) is computed from the graph at run start so
        # the hub decrypts ONLY those connectors; out-of-scope credentials are
        # never disclosed. The scope is computed by
        # ``farna.core.capability_scope.compute_run_fetch_scope``. CONTRACT
        # CHANGE (FAR-435 tightening vs the old fall-back behaviour): a run that
        # MIXES connector-scoped and connector-unrestricted nodes no longer falls
        # back to fetch-everything — it fetches only the scoped union. The legacy
        # fetch-everything behaviour survives ONLY for fully-unrestricted runs
        # (no node contributes any connector), where the union is empty → None.
        try:
            connector_hub = await self._init_connector_hub(org_id, graph_json=graph_json)
        except (Exception, asyncio.CancelledError):
            # FAR-439: a configured-path connector-hub failure RAISES (fail closed).
            # Catch the run-abort paths (Exception + asyncio.CancelledError) but not
            # KeyboardInterrupt/SystemExit — those terminate the process and must not
            # be swallowed into a teardown-and-reraise.
            # That raise propagates out of execute() BEFORE the post-stream
            # try/finally runs, so the resources acquired above (the model-backend
            # hub and its ContextVar, the run's cancellation-check / audit-hook
            # ContextVars, and the broker) would leak. Tear them down before
            # re-raising so no async client is left dangling and the re-entry gets
            # a fresh broker — mirroring _cleanup_run_resources.
            if model_backend_hub is not None:
                await _ref("_teardown_hub")(model_backend_hub)
            _ref("set_model_backend_hub")(None)
            _ref("set_connector_hub")(None)
            _ref("set_cancellation_check")(None)
            _ref("set_audit_hook")(None)
            _ref("get_registry")().close(run_id)
            raise
        # FAR-228: the idempotency gate is inert on multi-node graphs — it only
        # fires for a SINGLE sandbox_agent node (guard A in the node body and
        # guard B below both require this).
        single_sandbox_node: bool = sum(1 for n in graph_json.get("nodes", []) if str(n.get("node_type", "")).strip() == "sandbox_agent") == 1
        return model_backend_hub, connector_hub, broker, single_sandbox_node

    async def _prepare_and_stream(
        self,
        *,
        scope: _RunScope,
        config: _StreamConfig,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Compile (or retrieve) the StateGraph and stream it to completion.

        Returns ``(final_status, error_code, error_detail, node_token_usage)``.
        ``config.node_ids`` is mutated in place (the caller owns the empty set)
        so the finalization path sees the same ids the original inline rebinding
        produced.
        """
        # FAR-893: enter graph_compile phase.
        _tracker = self._dispatch_phase_tracker
        if _tracker is not None:
            _tracker.enter_phase("graph_compile")
        compiled = self._compile_scoped_graph(scope=scope, config=config)
        initial_state = await self._build_initial_stream_state(scope=scope, config=config)
        runtime_config, node_token_budgets = self._prepare_stream_runtime(config=config, thread_id=scope.thread_id)
        await self._seed_run_scoped_context(scope=scope, config=config)

        # FAR-432 (item 4a): a checkpointer is ONLY attached when the pipeline
        # is HITL/interactive. The decision is DERIVED from the graph at
        # execution time (a HITL gate edge or a ``manual`` input node → resume
        # must work → persist). Batch pipelines never pause, so their
        # per-superstep checkpoints are pure overhead — derive persistence off.
        # ``compiled`` is shared via the graph cache, so the checkpointer is
        # explicitly cleared in the non-interactive branch to avoid leaking a
        # previously-attached saver into a batch run of the same snapshot.
        if self._checkpointer_conn_string and _ref("_graph_is_interactive")(config.graph_json):
            from farna.settings import get_settings

            _settings = get_settings()
            async with _ref("_checkpointer_scope")(
                self._checkpointer_conn_string,
                organisation_id=scope.org_id,
                fernet_key=_settings.fernet_key,
            ) as saver:
                compiled.checkpointer = saver
                # FAR-893: enter streaming phase.
                if _tracker is not None:
                    _tracker.enter_phase("streaming")
                return await self._stream_with_config(
                    compiled,
                    initial_state,
                    runtime_config,
                    node_token_budgets,
                    scope=scope,
                    config=config,
                )
        compiled.checkpointer = None
        # FAR-893: enter streaming phase.
        if _tracker is not None:
            _tracker.enter_phase("streaming")
        return await self._stream_with_config(
            compiled,
            initial_state,
            runtime_config,
            node_token_budgets,
            scope=scope,
            config=config,
        )

    def _compile_scoped_graph(self, *, scope: _RunScope, config: _StreamConfig) -> Any:
        """Compile (or retrieve from cache) the run's StateGraph.

        ``pipeline_retry_policy`` and ``idempotency_key`` are threaded into the
        per-node retry/compensation wrapper (FAR-402 P5) — the latter is
        runtime-derived so a cached graph still dedupes against the run's
        stable identity.

        FAR-502: the eval defs loaded fresh above (execute()) are baked into
        HITL gate closures by the factory below. Folding their content hash
        into the cache key means a replay / variant run whose eval
        definitions changed since the cached compile misses the cache and
        recompiles with the fresh definitions instead of silently reusing
        the first run's closures.
        """
        return _ref("get_or_compile")(
            scope.pipeline_id,
            scope.snapshot_id,
            lambda: _ref("build_graph_from_json")(
                config.graph_json,
                eval_definitions_by_node=config.eval_defs_by_node,
                session_factory=self._session_factory,
                org_id=scope.org_id,
                pipeline_node_timeout_seconds=config.pipeline_node_timeout_seconds,
                pipeline_retry_policy=config.pipeline_retry_policy,
                node_idempotency_key=config.idempotency_key,
                pipeline_stdout_retention_config=config.snapshot.stdout_retention_config,
            ),
            pipeline_node_timeout_seconds=config.pipeline_node_timeout_seconds,
            graph_struct_hash=_ref("struct_hash_with_eval_defs")(
                _ref("compute_retry_aware_topology_hash")(config.graph_json, config.pipeline_retry_policy),
                config.eval_defs_by_node,
            ),
        )

    async def _build_initial_stream_state(self, *, scope: _RunScope, config: _StreamConfig) -> dict[str, Any]:
        """Seed the LangGraph state, pinning the earned autonomy level at start."""
        initial_state: dict[str, Any] = _ref("_seed_state")(config.snapshot, config.input_payload, config.variant_config_snapshot)
        earned_pin = await self._resolve_earned_at_start(scope.pipeline_id, scope.org_id, config.snapshot)
        if earned_pin is not None:
            initial_state["run_context"][_ref("PIPELINE_EARNED_AT_START_KEY")] = earned_pin
        initial_state.update(
            {
                "_run_id": scope.run_id,
                "_org_id": scope.org_id,
                # FAR-1163: seed the pipeline id alongside the other run
                # identity scalars so node-time readers (gate clamp telemetry,
                # _run_identity_strs -> FARNA_CONTROL_PIPELINE_ID, loop-intercept
                # guardrail binding) find it at gate/dispatch time. The state
                # key was READ in those paths but never written before this.
                "_pipeline_id": scope.pipeline_id,
                # FAR-1175 S1: pin the EARNED autonomy level at run start so
                # an in-flight run resolves min(pinned-at-start, live earned) —
                # a demotion bites at the next gate, a promotion never loosens
                # an in-flight run (ADR 062 §3). Read from the live pipeline row
                # (falling back to the pinned default and clamped to the
                # ceiling) on the FIRST execution; the value persists in the run
                # checkpoint so a resume does not re-pin a promoted level.
                "_claim_lease": self._claim_token,
                # FAR-764: registry-gated agent IDs seeded into state so
                # node functions can enforce default-deny without DB access.
                "_registry_gated_agents": self._registry_gated_agents,
            }
        )
        return initial_state

    def _prepare_stream_runtime(
        self,
        *,
        config: _StreamConfig,
        thread_id: str,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Populate the runtime config, node-id set, node timeouts and token budgets.

        Returns ``(runtime_config, node_token_budgets)``.
        """
        runtime_config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        config.node_ids.clear()
        config.node_ids.update({str(n["id"]) for n in config.graph_json.get("nodes", [])})
        # FAR-369: expose each node's configured ``timeout_seconds`` so the
        # absolute node-deadline watchdog (run_executor_with_watchdog) can hold
        # every node to its own hard deadline. Defaults to the pipeline-level
        # node timeout when a node omits it. Mutate in place (NOT reassign) so
        # the dict object passed to the watchdog stays the live reference that
        # the watchdog reads once populated.
        #
        # A node whose timeout is absent AND a node persisted with an explicit
        # ``"timeout_seconds": null`` are the same state: both fall back to the
        # pipeline-level default (see ``_resolved_node_timeouts``).
        self._node_timeouts.clear()
        self._node_timeouts.update(_ref("_resolved_node_timeouts")(config.graph_json, config.pipeline_node_timeout_seconds))
        node_token_budgets: dict[str, int] = {str(n["id"]): n["token_budget"] for n in config.graph_json.get("nodes", []) if n.get("token_budget") is not None}
        return runtime_config, node_token_budgets

    async def _seed_run_scoped_context(self, *, scope: _RunScope, config: _StreamConfig) -> None:
        """Seed the conformance context and count this REAL execution attempt.

        FAR-215: seed the run-scoped conformance context so every node
        re-validates its bound guardrail conformance at node start. The
        claimed guardrail list is hoisted ONCE per run (one query); the
        live capability manifest is still read per node at node start.

        Count this REAL node-execution attempt (post capacity-check,
        post compile, pre-stream). The NodeCancelledError retry budget
        is bounded by node_attempt_count, NOT claim_count —
        claim_count increments on every SAQ claim including
        capacity-deferred / non-executing claims, which would otherwise
        consume the retry budget before any real execution attempt
        (postmortem FAR-121). FAR-2076 keeps this counter as the per-node
        retry budget it always was; the RUN-level execution budget is the
        separate durable ``execution_attempt_count``, stamped on admission
        in ``_claim_under_capacity`` (earlier than this line, so a pre-node
        death still consumes the run-level budget).
        """
        claimed, claims_load_failed = await self._load_claimed_conformance_guardrails(scope.org_id, scope.pipeline_id)
        _ref("set_conformance_ctx")(
            self._session_factory,
            scope.org_id,
            config.snapshot.environment_profile_id,
            scope.pipeline_id,
            claimed,
            claims_load_failed,
        )

        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, scope.org_id)
            await _ref("set_rls_execution_context")(session)
            await session.execute(
                _ref("text")("UPDATE runs SET node_attempt_count = node_attempt_count + 1 WHERE id = :rid"),
                {"rid": str(scope.run_id)},
            )

    async def _stream_with_config(
        self,
        compiled: Any,
        initial_state: dict[str, Any],
        runtime_config: dict[str, Any],
        node_token_budgets: dict[str, int],
        *,
        scope: _RunScope,
        config: _StreamConfig,
    ) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
        """Run ``_stream_graph`` with the run's stream wiring."""
        return await self._stream_graph(
            compiled,
            initial_state,
            runtime_config,
            config.node_ids,
            config.broker,
            scope.run_id,
            pipeline_id=scope.pipeline_id,
            org_id=scope.org_id,
            completed_node_outputs=config.completed_node_outputs,
            guard=config.guard,
            node_token_budgets=node_token_budgets,
            eval_definitions_by_node=config.eval_defs_by_node,
            node_type_map=config.node_type_map,
        )

    async def _load_transient_state(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> tuple[int, str | None, dict[str, Any] | None, bool, str | None]:
        """Reload the run's attempt markers + claim + cancellation state.

        Returns ``(execution_attempt_count, current_token, run_markers,
        cancellation_requested, idempotency_key)``. The attempt count is the
        durable ``runs.execution_attempt_count`` — the ONE retry-budget
        measure (FAR-2076), not ``node_attempt_count`` (which a pre-node death
        never increments) and not ``claim_count`` (which grows on
        capacity-demoted and resume claims). ``idempotency_key`` is the run's
        persisted FAR-438 idempotency identity (``<pipeline_id>:<run_number>``)
        written at create — a re-run reads it back to recompute the SAME
        per-node keys for the read-before-write dedupe.
        """
        execution_attempt_count = 0
        current_token: str | None = None
        run_markers: dict[str, Any] | None = None
        cancellation_requested = False
        idempotency_key: str | None = None
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            current_run = await _ref("get_run")(session, run_id)
            if current_run is not None:
                execution_attempt_count = int(current_run.execution_attempt_count or 0)
                current_token = current_run.claim_token
                # FAR-583 read-switch: the markers reassemble from
                # run_node_outputs (with the empty/mismatch legacy fallback)
                # inside this SAME transaction — one batched repo read, no
                # per-node lazy loads. The run row here is a FRESH load in
                # this transaction, so no in-session unsaved state is relied
                # on.
                run_markers = await _ref("read_run_markers")(session, run_id=run_id, organisation_id=org_id)
                cancellation_requested = bool(current_run.cancellation_requested)
                idempotency_key = current_run.idempotency_key
        return execution_attempt_count, current_token, run_markers, cancellation_requested, idempotency_key

    def _idempotency_gate_ok(
        self,
        *,
        exc: NodeCancelledError | SandboxNodeFailedError,
        run_markers: dict[str, Any] | None,
        run_id: uuid.UUID,
        idempotency_key: str | None,
        superseded: bool,
        stalled: bool,
        cancellation_requested: bool,
        single_sandbox_node: bool,
        index: int | str | None = None,
        payload: str | bytes | None = None,
    ) -> bool:
        """FAR-228 guard B + FAR-438 read-before-write — should a transient retry be suppressed?

        Suppresses when EITHER the run's delivery marker already records
        ``delivery_done`` for the failing node (FAR-228, same-run retry) OR a
        re-run that reused the run's persisted FAR-438 idempotency key records an
        already-applied per-node key (``read_before_write_suppression``). The
        second branch is the UNKNOWN-recovery path: an operator re-run with the
        SAME persisted key must NOT double-submit the write.

        SCOPE (FAR-458 reconciliation): this executor gate stays confined to the
        SANDBOX single-node transient-recovery surface — ``single_sandbox_node``
        below. The CONNECTOR-write UNKNOWN-recovery surface has its OWN decision
        point: the connector node's write boundary (``make_connector_fn`` →
        ``_connector_write_gate``), which consults the SAME
        ``read_before_write_suppression`` before re-sending a previously-delivered
        write and stamps a ``delivery_done`` marker on success. A connector node
        does not (and should not) reach this executor transient path, so the gate
        is intentionally NOT extended to cover connectors here — leaving it
        sandbox-only avoids falsely gating a connector recovery that never passes
        through ``_decide_transient_failure``.
        ``index`` / ``payload`` (FAR-438) are the item-cardinality position and
        content-version payload handed to ``read_before_write_suppression`` so the
        derived per-node key matches the key the marker-write side stamped. They
        must be the SAME arguments the write side used — the transient path has no
        separate fan-out/content-edit context here (``exc.node_id`` already encodes
        ``parent+index`` for fan-out children, and there is no connector content
        payload to fold), so they default to ``None`` and stay consistent with the
        sandbox marker write.

        TOCTOU note (known, documented): ``run_markers`` is the run's
        raw-output markers (reassembled by ``_load_transient_state`` via the
        FAR-583 repo reader) read via plain ``get_run`` — NOT under
        ``SELECT ... FOR UPDATE``. Two executors racing
        on the same run could both read ``delivery_done`` absent and both decide
        to suppress/retry. The marker WRITE side (``_write_raw_output_marker``)
        DOES take ``with_for_update`` on the run row before persisting, so a
        concurrent writer is serialised there; this read is a bounded, best-effort
        gate (guarded by ``pipeline.idempotency_gate.check_failed``). If the
        TOCTOU window becomes a real double-write risk, take ``with_for_update``
        on this read too.
        """
        from farna.settings import get_settings

        gate_ok = False
        try:
            node_id = getattr(exc, "node_id", None)
            delivered = _ref("_should_skip_retry")(node_id, run_markers, str(run_id))
            # read_before_write_suppression is typed ``str`` for both refs and
            # fail-opens (returns False) on a None/empty run_ref or node_ref —
            # guard here so a None idempotency_key (no persisted key) never
            # even attempts the derivation, and to satisfy mypy's arg-types.
            replay_suppressed = False
            if idempotency_key and node_id:
                replay_suppressed = _ref("read_before_write_suppression")(
                    run_markers,
                    run_ref=idempotency_key,
                    node_ref=node_id,
                    index=index,
                    payload=payload,
                )
            gate_ok = (delivered or replay_suppressed) and not superseded and not stalled and not cancellation_requested and getattr(get_settings(), "idempotency_gate_enabled", True) and single_sandbox_node
        except Exception:
            _log.warning("pipeline.idempotency_gate.check_failed", extra={"run_id": str(run_id)}, exc_info=True)
            gate_ok = False
        return gate_ok

    async def _script_lease_ok(self, *, run_id: uuid.UUID, org_id: uuid.UUID, graph_json: dict[str, Any]) -> bool:
        """FAR-296 Phase 2 — prove no script process could still be alive.

        Returns True when the graph has no script mode, or when the stale-claim
        lease probe proves no live lease. The probe-failure and blocked-requeue
        warnings stay here (same messages as the original inline block).
        """
        script_lease_ok = True
        if _ref("_graph_has_script_mode")(graph_json):
            try:
                script_lease_ok = await _ref("_script_lease_probe_ok")(self._session_factory, str(run_id), org_id, self._claim_token)
            except Exception:
                _log.warning(
                    "script.lease_probe_eval_failed",
                    extra={"run_id": str(run_id)},
                    exc_info=True,
                )
                script_lease_ok = False
            if not script_lease_ok:
                _log.warning(
                    "script.lease_probe.blocked_requeue",
                    extra={"run_id": str(run_id), "reason": "stale script_executing lease"},
                )
        return script_lease_ok

    async def _fenced_pending_reset(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> int | None:
        """Fenced pending-reset: a conditional UPDATE guarded by OUR captured
        claim token + status='running' so a superseded original cannot demote
        the successor's running row, a stalled (watchdog-cancelled) executor
        cannot resurrect a run the watchdog just failed, and a cancellation
        cannot be reversed.

        The row keeps ``dispatcher='saq'``/``dispatched_at``/``dispatch_phase``,
        so the resulting ``pending`` row's ONLY durable recovery is
        ``dispatcher_reconcile``'s pending re-enqueue leg (FAR-1649 qa
        iteration 3) — and that leg re-dispatches it as ``execute_run``,
        i.e. re-executes the run. Safe ONLY because every call site of THIS
        method is gated on ``_graph_is_idempotent`` (``_can_fenced_requeue``,
        ``_retry_policy_applies``, watchdog_retry's ``not graph_idempotent``
        stand-down): the reset is never performed on a graph whose re-run is
        unsafe (FAR-295). A new call site MUST keep that gate or a
        non-idempotent node-started run becomes re-dispatchable.

        The gate is a property of EVERY producer of a re-dispatchable
        ``pending`` row, not just this method: the FAR-1627 worker-shutdown
        handback (``worker_handoff``) is a second, independent producer — it
        CLEARS ``dispatched_at``/``dispatcher`` (so its rows are recovered by
        the capacity-deferred leg, not the re-enqueue leg above) and gates
        its own reset on graph idempotency + the run's retry policy at the
        producer (``worker_handoff._handback_decision``), terminal-failing a
        non-idempotent node-started run in place instead of resetting it.

        Returns one of THREE unambiguous outcomes (FAR-2076 — ``0`` previously
        meant both "fence lost" and "the write timed out", and the watchdog
        read both as "a successor owns the run", so its untokened
        ``fail_run_terminal`` killed a run that still held our claim token):

        * ``1`` — the reset landed; the run is ``pending`` and re-dispatchable.
        * ``0`` — FENCE LOST: the row no longer matches
          (``claim_token``/``status``/cancellation), so a successor owns it or
          it already reached a terminal state. Callers stand down to their
          existing no-op/terminal path.
        * ``None`` — the bounded write TIMED OUT (SQLSTATE 55P03). The outcome
          is UNKNOWN and the row is most likely still ``running`` under OUR
          claim token. A caller must treat this as a DEFERRAL: never
          terminalise (an untokened terminal write would kill a run with
          budget remaining), never execute; leave the row for the nodeless/age
          backstop.

        FAR-2076: the reset takes the hot ``runs`` row lock and is
        transaction-scoped BOUNDED (``set_mutation_row_lock_timeout``) before
        its first lock. Without the bound Postgres' default ``lock_timeout``
        (0 = wait forever) applies, so the reset blocks on the very contended
        row that produced the admission timeout it is recovering from — the
        worker then sits until its setup grace expires and the watchdog
        terminalises the never-executed run the deferral exists to save. A
        bounded 55P03 is logged and reported as ``None``; it never propagates
        and never stalls the worker.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                # Transaction-scoped bound BEFORE this transaction's first lock
                # (FAR-1610 recipe; the UPDATE below is that first lock).
                await _ref("set_mutation_row_lock_timeout")(session)
                result = await session.execute(
                    _ref("text")("UPDATE runs SET status='pending', error_code=NULL, error_detail=NULL WHERE id=:rid AND claim_token=:tok AND status='running' AND cancellation_requested = false"),
                    {"rid": str(run_id), "tok": self._claim_token},
                )
                return int(getattr(result, "rowcount", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _ref("sqlstate_of")(exc) != _ref("LOCK_NOT_AVAILABLE_SQLSTATE"):
                raise
            _log.warning(
                "pipeline.fenced_pending_reset_lock_timeout run=%s org=%s — bounded mutation_row_lock_timeout_ms wait expired on the reset write; the row is left as-is for the nodeless/age backstop (no confirmed reset)",
                run_id,
                org_id,
                exc_info=True,
            )
            # ``None`` (NOT 0): the row is likely still ``running`` under our
            # claim token, so a caller must DEFER — never treat this as a
            # fence loss and terminalise.
            return None

    async def _record_attempt_reset(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        error_code: str | None,
    ) -> None:
        """Close the attempt that just ended in a CONFIRMED fenced reset.

        FAR-2078 — this is the load-bearing case the whole table exists for:
        ``_fenced_pending_reset`` nulls the run's ``error_code``, so the trigger
        for a retry used to vanish. The attempt row carries its OWN
        ``terminal_reason`` ("reset") and the pre-reset ``error_code``, and the
        reset never touches it.

        Call ONLY when the reset is confirmed (rowcount 1). A fence loss (0)
        means a successor owns the run and its OWN open attempt row must not be
        closed by us; a timeout (``None``) leaves the run running for the
        nodeless/age backstop, whose terminal write closes the row with the
        truth. FAIL-OPEN — the reset already committed, so diagnostics can
        never undo it.
        """
        from farna.core.pipeline_execution import record_attempt_close

        await record_attempt_close(
            self._session_factory,
            run_id,
            org_id,
            terminal_reason=_ref("ATTEMPT_TERMINAL_REASON_RESET"),
            error_code=error_code,
        )

    async def _cleanup_run_resources(
        self,
        *,
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        run_id: uuid.UUID,
    ) -> None:
        """Clear the contextvars + tear down hubs + close the run's broker.

        Runs BEFORE a re-raise that propagates out of execute() — the
        post-stream try/finally is not entered on that path, so the cleanup
        must happen here for the retry re-entry to get a fresh broker and no
        stale contextvars.
        """
        _ref("set_cancellation_check")(None)
        _ref("set_audit_hook")(None)
        _ref("set_model_backend_hub")(None)
        _ref("set_connector_hub")(None)
        if model_backend_hub is not None:
            await _ref("_teardown_hub")(model_backend_hub)
        if connector_hub is not None:
            await _ref("_teardown_hub")(connector_hub)
        _ref("get_registry")().close(run_id)

    def _transient_failure_detail(
        self,
        *,
        exc: Exception,
        script_lease_ok: bool,
        graph_idempotent: bool,
        execution_attempt_count: int,
        retries: int,
        non_retryable_code: str | None = None,
        failure_class_code: str | None = None,
        attempt_cap: int | None = None,
    ) -> tuple[str, str]:
        """Terminal failure code/detail for a retries-exhausted transient node failure.

        ``non_retryable_code`` (FAR-1800) is the failure's classified code when
        the requeue gate REFUSED to re-dispatch it because that class is
        non-retryable. The retries-exhausted wording would then be a lie (no
        retry was attempted) and would hide the real cause, so it is replaced by
        the registry code plus an explicit non-retryable explanation.

        ``failure_class_code`` / ``attempt_cap`` (FAR-2562) are the failure's own
        canonical code and its registry execution cap. Once the cap is spent the
        run terminalises with the class code and a cap explanation — again never
        the retries-exhausted wording, because the transport budget usually
        still had room (the observed shape was attempt 2 of a permitted 5).
        """
        if not script_lease_ok:
            # FAR-296 Phase 2: a stale script_executing lease blocked the
            # requeue — a script process may have run, so the side-effect
            # state is unknown. Terminal (never retried) with the
            # needs-human ``script.side_effect_unknown`` code.
            return _ref("_ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN"), _ref("_sanitize_detail")(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); not retried — needs human review: " + str(exc),
                limit=5000,
            )
        if non_retryable_code is not None:
            # FAR-1800: a retry cannot succeed for this class (e.g. an HTTP 402
            # billing refusal), so the requeue gate refused it. The WORDING
            # depends on whether any retry budget ever existed: with budget
            # left, the run is terminal on the FIRST attempt (say no retry was
            # attempted); with the budget already exhausted, claiming "not
            # retried" would be false — the retries DID run, and the last
            # failure happens to be non-retryable.
            return non_retryable_code, _ref("_non_retryable_failure_detail")(non_retryable_code, exc, execution_attempt_count, retries)
        if failure_class_code is not None and attempt_cap is not None and execution_attempt_count < retries and _ref("_attempt_cap_reached")(attempt_cap, execution_attempt_count):
            # FAR-2562: the class has spent its registry execution cap while the
            # transport budget still had room. Report the class's own code (the
            # one the run would carry anyway) and an accurate reason — the
            # generic branch below would claim "retries exhausted", which is
            # false here and would hide why the run stopped early. When the
            # budget is ALSO spent the generic wording is already accurate, so
            # that case keeps it unchanged.
            return failure_class_code, _ref("_attempt_capped_failure_detail")(failure_class_code, exc, execution_attempt_count, attempt_cap)
        error_code = "node_cancelled"
        if not graph_idempotent and execution_attempt_count < retries:
            # FAR-295: the run was NOT retried because a node in the
            # graph is non-idempotent (idempotent=false) — a retry
            # would re-execute a side-effecting step. Distinguish
            # this from the retries-exhausted message so the run
            # detail readout explains WHY it terminal-failed on the
            # first attempt.
            error_detail = _ref("_sanitize_detail")(
                "Sandbox node failed (transient); retry suppressed because a node in the graph is non-idempotent (idempotent=false) and re-running could double-execute a side effect: " + str(exc),
                limit=5000,
            )
        elif isinstance(exc, NodeCancelledError):
            error_detail = _ref("_sanitize_detail")("Sandbox node cancelled (transient) after retries exhausted: " + str(exc), limit=5000)
        else:
            # SandboxNodeFailedError: the FAR-197 no-output diagnostic
            # is a fully bounded message designed to survive this
            # surface in full — keep the limit at the sanitizer/column
            # cap (5000), not the 500 used for the short
            # NodeCancelledError string, or the kill-reason log tail
            # would be the first thing truncated (FAR-197 review).
            error_detail = _ref("_sanitize_detail")("Sandbox node failed (transient) after retries exhausted: " + str(exc), limit=5000)
            # FAR-1526: a SandboxNodeFailedError (no-output stall / stream
            # drop) used to terminal-fail under the GENERIC
            # ``node_cancelled`` code — whose registry guidance reads "Node
            # was cancelled." — so a stall was bucketed as a cancellation in
            # the error dashboard and daily facts. Surface the exception's
            # OWN canonical code here instead: one failure class, one code.
            # FAR-1887: that must be the SUBCLASS's code, not the family's
            # generic one — ``SandboxQueueTimeoutError`` (E2B rate-limit
            # retry budget exhausted) and ``SandboxRateLimitedError`` carry
            # their own ``sandbox.queue_timeout`` / ``sandbox.rate_limited``
            # aliases, and hard-setting ``sandbox.no_output_json`` for the
            # whole family made those classes invisible in analytics.
            # EXCEPTION: the FAR-136 hang death carries "likely hung"
            # in the detail and is excluded from ``failure`` retries ONLY
            # while its code resolves to ``node.cancelled`` — that marker
            # keeps the generic code.
            if isinstance(exc, SandboxNodeFailedError) and "likely hung" not in error_detail:
                error_code = _ref("sandbox_failure_error_code")(exc)
            # FAR-734: scan the retained stdout (embedded in the
            # exception message) for terminal provider-error
            # signatures.  When a signature matches, upgrade the
            # generic ``node_cancelled`` code to the specific
            # ``model.*`` code so the Error Dashboard and daily facts
            # bucket it as a first-class failure dimension.  Fail
            # open: when no signature matches the generic code stays.
            # FAR-2006: the 401/402 credential/funding refusal
            # classification runs AFTER the sandbox-code override, so a
            # provider refusal still wins over the sandbox family code.
            _provider_code = _ref("classify_provider_error")(str(exc))
            if _provider_code is not None:
                error_code = _provider_code
        return error_code, error_detail

    async def _read_retry_attempt_state(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> tuple[int, str | None, datetime | None]:
        """Read the run's EXECUTION attempt count, claim token, and THIS attempt's start.

        The first element is the durable ``runs.execution_attempt_count`` — the
        ONE retry-budget measure (FAR-2076). It is what every granting path
        compares against ``1 + max_retries``; ``claim_count`` (capacity-demoted
        / resume claims) and ``node_attempt_count`` (nothing recorded for a
        pre-node death) are NOT execution counters and must never be used here.

        ``attempt_started_at`` is the LATER of ``dispatched_at`` (rewritten on
        every claim / re-claim, so it marks the current attempt's start) and
        ``started_at`` (set once when the first attempt began executing) — see
        ``_attempt_started_at`` for why the max, not a fallback, is correct. It
        is the input to the FAR-2025 over-budget retry guard: an attempt that
        consumed the graph's node-budget allowance must not be re-dispatched.
        """
        execution_attempt_count = 0
        current_claim_token: str | None = None
        attempt_started_at: datetime | None = None
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            current_run = await _ref("get_run")(session, run_id)
            if current_run is not None:
                execution_attempt_count = int(current_run.execution_attempt_count or 0)
                current_claim_token = current_run.claim_token
                attempt_started_at = _ref("_attempt_started_at")(current_run)
        return execution_attempt_count, current_claim_token, attempt_started_at

    async def _probe_script_lease(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """FAR-296 Phase 2 stale-claim lease probe before ANY requeue of script mode.

        Proves no script process could still be alive. A probe evaluation
        error fails CLOSED (returns False, logged) — an unknown side-effect
        state must never be re-dispatched.
        """
        try:
            ok: bool = await _ref("_script_lease_probe_ok")(self._session_factory, str(run_id), org_id, self._claim_token)
        except Exception:
            _log.warning(
                "script.lease_probe_eval_failed_retry_policy",
                extra={"run_id": str(run_id)},
                exc_info=True,
            )
            ok = False
        if not ok:
            _log.warning(
                "script.lease_probe.blocked_retry_policy",
                extra={"run_id": str(run_id), "reason": "stale script_executing lease"},
            )
        return ok

    async def _run_post_stream_tail(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str] | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Post-stream tail: agent_signal firing, gated publish.

        Preserves the outer try/except (``pipeline.post_stream_error``) that
        wrapped the original inline block. Returns the (possibly mutated)
        ``(final_status, error_code, error_detail)``.
        The caller's ``finally`` owns the resource cleanup.
        """
        try:
            # If the run completed, fire agent_signal triggers. FAR-228: a
            # gated run (error_code harness.idempotency_gate) is excluded — the
            # delivery was already made by a PRIOR attempt; firing agent_signal
            # against the skip envelope would be wrong.
            if final_status == "complete" and error_code != _ref("_ERROR_CODE_HARNESS_IDEMPOTENCY_GATE"):
                # Fire agent_signal triggers for each completed node.
                async with self._session_factory() as session, session.begin():
                    await _ref("set_rls_org")(session, org_id)
                    await _ref("set_rls_execution_context")(session)
                    await self._fire_agent_signals(
                        session=session,
                        org_id=org_id,
                        run_id=run_id,
                        pipeline_id=pipeline_id,
                        completed_node_outputs=completed_node_outputs,
                        node_type_map=node_type_map,
                    )
            # FAR-228 / FAR-1534: a gated run completes without re-executing
            # the node; its ``run_completed`` is published post-finalize like
            # every other complete run.
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.post_stream_error", extra={"run_id": str(run_id)})
        return final_status, error_code, error_detail

    async def _run_post_stream_and_teardown(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        model_backend_hub: ModelBackendHub | None,
        connector_hub: Any | None,
        node_type_map: dict[str, str] | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Run the post-stream tail then tear down the run's execution environment.

        The teardown (contextvars, hub teardown, broker registry close) runs in a
        ``finally`` so it fires even when the tail re-raises (e.g. a
        ``CancelledError``), preserving the original inline try/finally
        semantics in ``execute``. Returns the (possibly mutated) triplet from
        the tail.
        """
        tail_ok = False
        try:
            final_status, error_code, error_detail = await self._run_post_stream_tail(
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                final_status=final_status,
                error_code=error_code,
                error_detail=error_detail,
                completed_node_outputs=completed_node_outputs,
                node_type_map=node_type_map,
            )
            tail_ok = True
        finally:
            # Close broker after all post-stream work (suite checks, signals).
            _ref("set_cancellation_check")(None)
            _ref("set_audit_hook")(None)
            _ref("set_model_backend_hub")(None)
            _ref("set_connector_hub")(None)
            if model_backend_hub is not None:
                await _ref("_teardown_hub")(model_backend_hub)
            if connector_hub is not None:
                await _ref("_teardown_hub")(connector_hub)
            # FAR-1534: a clean ``complete`` keeps the broker open — the
            # finalize step may still downgrade it and then publishes
            # ``run_completed`` (final status) and closes the broker.
            if final_status != "awaiting_human" and not (final_status == "complete" and tail_ok):
                _ref("get_registry")().close(run_id)
        return final_status, error_code, error_detail

    async def _fire_agent_signals(
        self,
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str] | None = None,
    ) -> None:
        """Fire agent_signal triggers for each completed node.

        FAR-228: a node whose output_json carries the idempotency_gate marker
        is a SKIPPED delivery (guard A/B) — it must not re-fire child
        pipelines. Keyed ONLY on the marker, never on status == "skipped"
        (template-error skips fire today).

        FAR-131: each node's resolved type is passed through so
        ``fire_agent_signal`` embeds the node's PURE RETURN (not the legacy
        mixed envelope) in the child run's input payload for a SPLITTABLE type
        — whatever the return's shape. The map is EXTENDED from the snapshot
        edges by the caller, so an edge-synthesized HITL gate resolves to
        ``"gate"``. A node absent from the map resolves to ``None`` and keeps
        the legacy verbatim behaviour.
        """
        for node_id, node_output in completed_node_outputs.items():
            if _ref("_node_output_has_idempotency_gate")(node_output):
                continue
            await self._fire_agent_signal_for_node(
                session=session,
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                node_id=node_id,
                node_output=node_output,
                node_type=(node_type_map or {}).get(node_id),
            )

    async def _fire_agent_signal_for_node(
        self,
        *,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_id: str,
        node_output: Any,
        node_type: str | None = None,
    ) -> None:
        """Fire one node's agent_signal triggers, failure-isolated + logged."""
        try:
            signal_results = await _ref("fire_agent_signal")(
                session,
                org_id=org_id,
                source_run_id=run_id,
                source_pipeline_id=pipeline_id,
                completed_node_id=node_id,
                node_output=node_output,
                node_type=node_type,
            )
            for sr in signal_results:
                _log.info(
                    "agent_signal.%s trigger=%s run=%s",
                    sr["status"],
                    sr.get("trigger_id", "?"),
                    sr.get("run_id", "?"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "agent_signal.failed",
                extra={
                    "run_id": str(run_id),
                    "node_id": node_id,
                },
            )
