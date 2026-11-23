"""PipelineExecutor — orchestrates a single run end-to-end.

Responsibilities:
  - Seed initial LangGraph state from snapshot.run_context_defaults + input_payload
  - Obtain/compile the StateGraph (via graph_cache)
  - Enforce per-pipeline max_concurrent_runs, the per-org sandbox
    concurrency cap, and the per-org run-concurrency cap via count-based
    capacity checks (no FOR UPDATE; runs declined at capacity are demoted
    back to ``pending`` with a reason marker and recovered by
    ``dispatcher_reconcile`` when a slot frees; stranded re-dispatch is the
    durable liveness backstop — there is NO in-process retry loop)
  - Consume astream_events() and publish to the per-run RunEventBroker
  - Set up AsyncPostgresSaver as LangGraph checkpointer
  - Stream graph execution, updating Run status on transitions
  - Mark run complete/failed/cancelled/awaiting_human/eval_failed in DB

Handles GraphInterrupt by transitioning the run to awaiting_human.
Does NOT handle WebSocket fan-out, HITL claim/approve/reject, or webhook triggers.

Architecture: see docs/architecture.md "Pipeline Executor Patterns" and "Run admission and healing".
"""

from __future__ import annotations

# The ``noqa: F401`` markers in this import block keep names that moved into
# ``executor_parts`` (or lost their last local user) importable FROM THIS MODULE.
# ``executor.<name>`` is a live attribute for `mock.patch` targets and for
# downstream imports, so the bindings must stay even where nothing in this file
# still reads them. Do not "clean them up".
import asyncio
import hashlib  # noqa: F401
import json  # noqa: F401
import logging
import math  # noqa: F401
import os  # noqa: F401
import socket  # noqa: F401
import uuid
from collections import OrderedDict  # noqa: F401
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence  # noqa: F401
from contextlib import asynccontextmanager, suppress  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import UTC, datetime, timedelta  # noqa: F401
from decimal import Decimal  # noqa: F401
from inspect import isawaitable  # noqa: F401
from typing import Any, Literal, NamedTuple, NoReturn  # noqa: F401

from langchain_core.messages import BaseMessage  # noqa: F401
from langgraph.errors import GraphInterrupt, NodeCancelledError  # noqa: F401
from opentelemetry import context as context_api  # noqa: F401
from opentelemetry.trace import set_span_in_context  # noqa: F401
from sqlalchemy import Boolean, Uuid, bindparam, func, select, text, update  # noqa: F401
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from farna.connectors._rate_bucket import SharedBudgetUnavailableError
from farna.connectors.base import node_fires_dispatch_job  # noqa: F401
from farna.core.audit_logger import append_audit_event
from farna.core.audit_logger.labels import (
    SYSTEM_ACTOR,  # noqa: F401
    compose_run_started_summary,
    resolve_run_actor,
    short_id,  # noqa: F401
)
from farna.core.connector_hub.locking import _uuid_to_lock_keys
from farna.core.cost_controller.finalize import (
    FinalizeMetricSink,  # noqa: F401
    derive_node_type_map,  # noqa: F401
    finalize_cost,  # noqa: F401
)
from farna.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED  # noqa: F401
from farna.core.eval_engine import (
    EvalBlockedError,  # noqa: F401
)
from farna.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from farna.core.graph_validator import GraphValidator  # noqa: F401
from farna.core.graph_validator._types import ValidationResult  # noqa: F401
from farna.core.hitl_manager import HITLManager  # noqa: F401
from farna.core.hitl_manager.gate_coalescing import SUPERSEDE_REASON, evaluate_gate_coalescing  # noqa: F401
from farna.core.model_backend_hub import ModelBackendHub
from farna.core.node_output_split import (
    DEFAULT_NODE_TYPE,  # noqa: F401
    SPLITTABLE_NODE_TYPES,  # noqa: F401
    extend_node_type_map_from_edges,  # noqa: F401
    node_telemetry,  # noqa: F401
    resolve_node_contract_output,  # noqa: F401
)
from farna.core.notifier import (
    EVENT_BUDGET_EXCEEDED,
    EVENT_EVAL_BLOCKED,  # noqa: F401
    EVENT_HITL_AWAITING,  # noqa: F401
    EVENT_PROVIDER_FAILURE,  # noqa: F401
    EVENT_RUN_FAILED,  # noqa: F401
    EVENT_RUN_STALLED,
)
from farna.core.pipeline_engine import retry_compensation as rc  # noqa: F401
from farna.core.pipeline_engine import retry_policy as _retry_policy_module
from farna.core.pipeline_engine.decorator import (
    _RESERVED_RUN_CONTEXT_KEYS,  # noqa: F401
    RunCancelledError,  # noqa: F401
    set_audit_hook,  # noqa: F401
    set_cancellation_check,  # noqa: F401
    set_connector_hub,
    set_model_backend_hub,
)
from farna.core.pipeline_engine.error_codes import (
    _CODE_CONNECTOR_FAILED,  # noqa: F401
    _CODE_CONNECTOR_UNKNOWN,  # noqa: F401
    _CODE_HARNESS_RETRY_BUDGET_EXHAUSTED,
    _CODE_SANDBOX_AGENT_FAILED,  # noqa: F401
    _CODE_SANDBOX_NO_OUTPUT_JSON,  # noqa: F401
    map_legacy_code,  # noqa: F401
    sanitize_error_text,  # noqa: F401
    should_notify_terminal_failure,  # noqa: F401
)
from farna.core.pipeline_engine.errors import ResumeClaimSupersededError, RouterNoMatchError  # noqa: F401
from farna.core.pipeline_engine.eval_persist_order import (
    EvalDefDTO,
    run_evals_persist_before_decide,
)
from farna.core.pipeline_engine.event_broker import RunEventBroker, get_registry  # noqa: F401
from farna.core.pipeline_engine.evidence import (
    EvidenceProvider,
    build_default_evidence_provider,  # noqa: F401
    compute_work_intact,  # noqa: F401
    evidence_enabled,  # noqa: F401
    node_declared_success,  # noqa: F401
    run_evidence_probe,  # noqa: F401
)
from farna.core.pipeline_engine.graph_cache import build_graph_from_json, get_or_compile, struct_hash_with_eval_defs  # noqa: F401
from farna.core.pipeline_engine.hitl_context import HitlReviewContext, build_hitl_review_context  # noqa: F401
from farna.core.pipeline_engine.idempotency import read_before_write_suppression  # noqa: F401
from farna.core.pipeline_engine.llm_judge import resolve_model_judge_callable
from farna.core.pipeline_engine.model_backend_errors import (
    NON_RETRYABLE_PROVIDER_CODES,  # noqa: F401
    classify_provider_error,  # noqa: F401
    non_retryable_provider_code,  # noqa: F401
)
from farna.core.pipeline_engine.modulo_saver import ModuloPostgresSaver  # noqa: F401
from farna.core.pipeline_engine.node_runner import (
    MODULO_SYNTHETIC_FAILURE_MARKER,  # noqa: F401
    ConnectorNodeFailedError,  # noqa: F401
    OutputSchemaValidationError,  # noqa: F401
    SandboxNodeFailedError,  # noqa: F401
    SandboxTierRefusedError,  # noqa: F401
    SupersededNodeError,  # noqa: F401
    _idempotency_gate_skipped_envelope,  # noqa: F401
    _marker_delivery_done_for_node,  # noqa: F401
    merge_recovered_token_usage,  # noqa: F401
    set_conformance_ctx,  # noqa: F401
)
from farna.core.pipeline_engine.output_filter import OutputRejectedError  # noqa: F401
from farna.core.pipeline_engine.port_resolver import compute_port_topology_hash  # noqa: F401
from farna.core.pipeline_engine.retry_policy import (
    _ERROR_CODE_AGENT_FAILED,  # noqa: F401
    _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE,  # noqa: F401
    _ERROR_CODE_NODE_TIMEOUT,  # noqa: F401
    _ERROR_CODE_SANDBOX_TIER_REFUSED,  # noqa: F401
    _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN,  # noqa: F401
    _attempt_cap_reached,  # noqa: F401
    _attempt_capped_failure_detail,  # noqa: F401
    _attempt_started_at,  # noqa: F401
    _can_fenced_requeue,  # noqa: F401
    _failure_class_attempt_cap,  # noqa: F401
    _non_retryable_failure_detail,  # noqa: F401
    _transient_retry_allowance,  # noqa: F401
    maybe_retry_after_policy,  # noqa: F401
    sandbox_failure_error_code,  # noqa: F401
)
from farna.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError  # noqa: F401
from farna.core.pipeline_engine.runtime_retry import COMPENSATION_FAILED_CODE, CompensationFailedError  # noqa: F401
from farna.core.retry_budget import (
    # The ONE retry-budget module (zero-import leaf): the bound, the ceiling and
    # the consumed measure all live there so no path can define a second copy.
    retry_budget_exhausted,
    retry_executions_consumed,
    retry_max_executions,
)
from farna.core.run_context.autonomy import (
    PIPELINE_EARNED_AT_START_KEY,  # noqa: F401
    PIPELINE_MAX_AUTONOMY_KEY,  # noqa: F401
    AutonomyLevel,
    autonomy_level_rank,  # noqa: F401
)
from farna.core.run_provenance import run_provenance_fields
from farna.core.spend_ceiling import ORG_CEILING_EXCEEDED, evaluate_org_spend_ceiling
from farna.core.trigger_engine.agent_signal import fire_agent_signal  # noqa: F401
from farna.db.crud.hitl_review_config import (
    parse_hitl_review_id,
    resolve_hitl_review_config,  # noqa: F401
    resolve_review_window_for_gate,  # noqa: F401
)
from farna.db.crud.organisation_cost_state import get_org_cost_state
from farna.db.crud.pipeline import get_pipeline  # noqa: F401
from farna.db.crud.row_lock import set_mutation_row_lock_timeout
from farna.db.crud.run import (
    ERROR_CODE_ORG_CAPACITY_LIMITED,
    ERROR_CODE_PIPELINE_CAPACITY,
    _graph_contains_sandbox_agent,
    _verify_policy_gate_pin_fingerprint,
    count_active_runs_for_org,
    count_active_runs_for_pipeline,
    count_active_sandbox_runs_for_org,
    get_org_run_concurrency_limit,
    get_run,
    get_sandbox_concurrency_limit,
    update_run_status,
)
from farna.db.crud.run_node_outputs import DualWriteError, read_run_blobs, read_run_markers  # noqa: F401
from farna.db.models.eval import Eval
from farna.db.models.hitl_claim import HitlClaim  # noqa: F401
from farna.db.models.model_backend import ModelBackend
from farna.db.models.organisation import Organisation
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.models.policy_gate import PolicyGate
from farna.db.models.run import ACTIVE_RUN_STATUSES, TERMINAL_STATUSES, Run
from farna.db.models.run_attempt import ATTEMPT_TERMINAL_REASON_RESET  # noqa: F401
from farna.db.rls import set_rls_execution_context, set_rls_org
from farna.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, is_lock_abort, sqlstate_of  # noqa: F401
from farna.otel_bridge import LangGraphOtelBridge, trace_id_for_thread  # noqa: F401

# ---------------------------------------------------------------------------
# Module-level helpers, value objects and private exceptions extracted into the
# ``executor_parts`` package (GAPS-harden-w2-executor.md §5 step 1). They are
# re-exported here so every existing import path and every
# ``mock.patch("farna.core.pipeline_engine.executor.<name>")`` target keeps
# resolving. Do not redefine these names in this module.
#
# ``_FinalizeMixin`` / ``_TelemetryMixin`` / ``_HitlMixin`` / ``_StreamingMixin``
# / ``_RunExecuteMixin`` are the step 2-4 class splits: the finalize, telemetry,
# HITL, stream-phase and execute/resume method groups moved into mixin modules,
# imported under private aliases so they do NOT join this module's importable
# surface.
# ---------------------------------------------------------------------------
from .executor_parts.finalize import FinalizeMixin as _FinalizeMixin
from .executor_parts.hitl import HitlMixin as _HitlMixin
from .executor_parts.outcomes import (
    _connector_artifact_error_code,
    _connector_artifact_matches,
    _connector_failure_artifact_reason,
    _connector_failure_for_node,
    _connector_failure_views,
    _is_supersede_rejection,
    _is_terminating_rejection_output,
    _map_lg_event,
    _node_output_agent_failure,
    _node_output_connector_error_code,
    _node_output_connector_failure,
    _node_output_has_idempotency_gate,
    _node_output_sandbox_session_lost,
    _node_output_sandbox_session_lost_text,
    _node_output_stall_reason,
    _node_output_text_surfaces,
    _record_chain_end_output,
    _record_connector_failure,
    _record_node_markers,
    _sandbox_failure_class_code,
    _should_skip_retry,
    _stream_terminal_reason,
    _terminal_failure,
    _try_agent_failure_elevation,
)
from .executor_parts.run_execute import RunExecuteMixin as _RunExecuteMixin
from .executor_parts.run_shape import (
    _ENGINE_MANAGED_SEED_KEYS,
    _GATED_EVAL_MODES_KEY,
    _INTERACTIVE_NODE_TYPES,
    _SANDBOX_AGENT_CACHE,
    _SANDBOX_AGENT_CACHE_MAX,
    _WORKER_ID,
    _accumulate_chat_model_tokens,
    _accumulate_llm_tokens,
    _accumulate_node_token_usage,
    _all_capacities_ok,
    _apply_frozen_eval_mode_entry,
    _apply_work_intact,
    _apply_work_intact_best_effort,
    _checkpointer_scope,
    _claim_resume_for_executor,
    _clamp_to_ceiling,
    _compute_otel_run_context,
    _connector_scope_agent_ids,
    _extract_chat_model_token_usage,
    _extract_llm_token_usage,
    _frozen_eval_mode_entries,
    _governed_evaluation_mode,
    _graph_is_idempotent,
    _graph_is_interactive,
    _index_dtos_by_eval_id,
    _interrupt_condition_result,
    _interrupt_gate_payload,
    _interrupt_required_team_id,
    _interrupt_subject,
    _interrupt_subject_leaf_key,
    _interrupt_subject_parent,
    _reclassify_after_work_intact,
    _resolve_post_node_eval_target,
    _resolved_node_timeouts,
    _sandbox_agent_for_snapshot,
    _script_lease_probe_ok,
    _seed_state,
    _streamed_interrupts,
    _teardown_hub,
    compute_retry_aware_topology_hash,
    org_sandbox_capacity_free,
)
from .executor_parts.streaming import StreamingMixin as _StreamingMixin
from .executor_parts.support import (
    AdmissionLockTimeoutError,
    AdmissionResetState,
    GraphValidationError,
    RetryBudgetGateRefusedError,
    RunNotFoundError,
    SandboxCapacityExceededError,
    _CapacitySnapshot,
    _is_finalize_lock_retryable,
    _RunScope,
    _safe_pipeline_name,
    _sanitize_detail,
    _StreamConfig,
    _StreamContext,
    _StreamState,
    _traceback_detail,
)
from .executor_parts.telemetry import TelemetryMixin as _TelemetryMixin

# NOTE on the two private aliases above: ``PipelineExecutor`` gets its finalize
# and telemetry groups as mixins (steps 2-3 of GAPS-harden-w2-executor.md §5).
# The mixin classes are composition detail, NOT importable surface, so their
# bindings are dropped once the class exists (see the ``del`` at the end of this
# module) — ``dir(farna.core.pipeline_engine.executor)`` stays exactly what it
# was before the split.

# The names above are re-exports, not local definitions. ``__all__`` marks them as
# explicitly exported so ``mypy --no-implicit-reexport`` (part of ``strict``) still
# accepts ``from farna.core.pipeline_engine.executor import <name>`` for consumers
# such as cron_helpers / saq_worker / worker_handoff. ``dir(executor)`` is unchanged
# by ``__all__``; only a ``from executor import *`` would consult it, and nothing in
# this repository star-imports the module.
__all__ = [
    "_ENGINE_MANAGED_SEED_KEYS",
    "_GATED_EVAL_MODES_KEY",
    "_INTERACTIVE_NODE_TYPES",
    "_SANDBOX_AGENT_CACHE",
    "_SANDBOX_AGENT_CACHE_MAX",
    "_WORKER_ID",
    "AdmissionLockTimeoutError",
    "AdmissionResetState",
    "GraphValidationError",
    "RetryBudgetGateRefusedError",
    "RunNotFoundError",
    "SandboxCapacityExceededError",
    "_CapacitySnapshot",
    "_RunScope",
    "_StreamConfig",
    "_StreamContext",
    "_StreamState",
    "_accumulate_chat_model_tokens",
    "_accumulate_llm_tokens",
    "_accumulate_node_token_usage",
    "_all_capacities_ok",
    "_apply_frozen_eval_mode_entry",
    "_apply_work_intact",
    "_apply_work_intact_best_effort",
    "_checkpointer_scope",
    "_claim_resume_for_executor",
    "_clamp_to_ceiling",
    "_compute_otel_run_context",
    "_connector_artifact_error_code",
    "_connector_artifact_matches",
    "_connector_failure_artifact_reason",
    "_connector_failure_for_node",
    "_connector_failure_views",
    "_connector_scope_agent_ids",
    "_extract_chat_model_token_usage",
    "_extract_llm_token_usage",
    "_frozen_eval_mode_entries",
    "_governed_evaluation_mode",
    "_graph_is_idempotent",
    "_graph_is_interactive",
    "_index_dtos_by_eval_id",
    "_interrupt_condition_result",
    "_interrupt_gate_payload",
    "_interrupt_required_team_id",
    "_interrupt_subject",
    "_interrupt_subject_leaf_key",
    "_interrupt_subject_parent",
    "_is_finalize_lock_retryable",
    "_is_supersede_rejection",
    "_is_terminating_rejection_output",
    "_map_lg_event",
    "_node_output_agent_failure",
    "_node_output_connector_error_code",
    "_node_output_connector_failure",
    "_node_output_has_idempotency_gate",
    "_node_output_sandbox_session_lost",
    "_node_output_sandbox_session_lost_text",
    "_node_output_stall_reason",
    "_node_output_text_surfaces",
    "_reclassify_after_work_intact",
    "_record_chain_end_output",
    "_record_connector_failure",
    "_record_node_markers",
    "_resolve_post_node_eval_target",
    "_resolved_node_timeouts",
    "_safe_pipeline_name",
    "_sandbox_agent_for_snapshot",
    "_sandbox_failure_class_code",
    "_sanitize_detail",
    "_script_lease_probe_ok",
    "_seed_state",
    "_should_skip_retry",
    "_stream_terminal_reason",
    "_streamed_interrupts",
    "_teardown_hub",
    "_terminal_failure",
    "_traceback_detail",
    "_try_agent_failure_elevation",
    "compute_retry_aware_topology_hash",
    "org_sandbox_capacity_free",
]

# Backward-compatible re-exports of the retry-policy symbols extracted into
# ``retry_policy``. Untouched consumers (watchdog_retry, cron_helpers,
# worker_handoff, pipeline_execution) and existing tests still import these
# legacy names from ``executor``; keep that public surface intact.
RunRetryPolicyError = _retry_policy_module.RunRetryPolicyError
_retry_after_policy = _retry_policy_module.retry_after_policy
_retry_policy_applies = _retry_policy_module.retry_policy_applies
_can_retry_after_policy = _retry_policy_module.can_retry_after_policy
_retry_backoff_seconds = _retry_policy_module.retry_backoff_seconds
_record_retry_redispatch = _retry_policy_module.record_retry_redispatch
_attempt_consumed_node_budget = _retry_policy_module._attempt_consumed_node_budget
_attempt_elapsed_seconds = _retry_policy_module._attempt_elapsed_seconds
_graph_has_script_mode = _retry_policy_module._graph_has_script_mode
_is_non_retryable_error_code = _retry_policy_module._is_non_retryable_error_code

# Statuses a run may still be admitted from when a retry's backoff elapses.
# Any terminal status (complete/failed/cancelled/eval_failed) means the run
# was already finalised while the retry loop slept — it must never be
# resurrected back to ``running``. Single-sourced from
# ``db.models.run.ACTIVE_RUN_STATUSES``.
_ADMISSIBLE_STATUSES = ACTIVE_RUN_STATUSES

# Terminal statuses. A run in one of these is already finalised; it must never
# be resurrected AND must never spawn (or hold) a retry task. Single-sourced
# from ``db.models.run.TERMINAL_STATUSES`` (includes ``stalled``).
_TERMINAL_STATUSES = TERMINAL_STATUSES

# Terminal run statuses that carry their own notifier event. Read
# off the COMMITTED run row (never the pre-finalize locals) so the per-agent
# token budget override applied inside ``finalize_cost`` is observed.
# The ``eval_blocked`` event is keyed on a status+error_code pair instead of a
# status, so it is resolved separately in
# ``_dispatch_terminal_status_notification``.
_TERMINAL_STATUS_EVENTS: dict[str, str] = {
    "stalled": EVENT_RUN_STALLED,
    "budget_exceeded": EVENT_BUDGET_EXCEEDED,
}

# --- Bounded whole-transaction finalisation retry (money-correctness) ---
# A deadlock (40P01) or the bounded lock_timeout expiry (55P03) taken at the
# org / Run finalisation locks aborts the WHOLE finalisation transaction, so it
# is re-run from its ownership layer (``_run_finalize_cost_transaction``, which
# owns ``session.begin()``). Idempotent-safe: the rolled-back OUTER transaction
# commits none of its own writes — including the org accrual, which lives inside
# the ledger savepoint — so re-running cannot double-count. (``finalize_cost``
# also runs side transactions that COMMIT independently — ``_reduced_escape``'s
# fresh transaction and the journey/facts writes — so the claim is scoped to the
# outer transaction, not to "nothing whatsoever".) A small fixed number of
# attempts rides out transient contention; a persisting failure propagates and
# the executor is terminal-failed truthfully (``executor_failed``), never a raw
# 500 and never a silent swallow.
FINALIZE_LOCK_RETRY_ATTEMPTS = 3
# Small linear backoff between attempts (0.1s, 0.2s) — long enough for the
# contended lock holder to release, short enough to stay well inside the
# finalisation's bounded envelope.
_FINALIZE_LOCK_RETRY_DELAY_SECONDS = 0.1


_log = logging.getLogger(__name__)

# Guardrail eval_type value the executor splits out of the node-scoped eval defs
# (interception-owned, never eval-engine-owned — the eval engine raises
# GuardrailMisroutedError on a guardrail-typed def).
GUARDRAIL_EVAL_TYPE = "guardrail"

# NOTE: the output-point predicate is NOT re-implemented here. The canonical
# ``is_output_interception`` lives in ``farna.core.guardrails.output_intercept``
# and is lazily imported inside ``_handle_chain_end_event`` (a module-level
# import cycle guards against guardrails -> eval_engine -> executor). A local
# copy of a security-seam predicate is exactly the duplication flagged in
# review — never reintroduce one.

# The shared "no gate governs this eval" verdict of the policy-gate resolution
# helpers — warn semantics, no PolicyGateDecision row (``policy_gate_id is None``
# downstream). One name so every ungoverned arm returns an identical, recognisable value.
_NO_GOVERNED_GATE: tuple[str, uuid.UUID | None, int | None, uuid.UUID | None, str | None] = (
    "warn",
    None,
    None,
    None,
    None,
)


# ---------------------------------------------------------------------------
# Failure-context attribution helpers (log-line extras)
# ---------------------------------------------------------------------------
# Single import gate: every helper below delegates to the (itself fail-open)
# failure_context module. A single module-level guard is the only layer
# needed — the per-call re-imports previously duplicated this guard.
_ATTRIBUTION_AVAILABLE = True
try:
    from farna.core.pipeline_engine.failure_context import (
        _extract_column_info,
        _get_build_sha,
        enrich_error_detail,
    )
except Exception:  # pragma: no cover — import failure is the degraded path
    _ATTRIBUTION_AVAILABLE = False


def _failure_build_sha() -> str:
    """Best-effort build SHA for log-line extra (never raises)."""
    if not _ATTRIBUTION_AVAILABLE:
        return "unknown"
    try:
        return _get_build_sha()
    except Exception:
        return "unknown"


def _failure_column(exc: BaseException) -> str | None:
    """Best-effort failing column name for log-line extra (never raises)."""
    if not _ATTRIBUTION_AVAILABLE:
        return None
    try:
        _table, column = _extract_column_info(exc)
        return column
    except Exception:
        return None


def _enrich_failure_detail(raw_detail: str, exc: BaseException) -> str:
    """Append failure attribution to a failure detail.

    Thin executor-side boundary around ``failure_context.enrich_error_detail``
    so both executor failure paths share one call site and a test can prove
    the enrichment is wired. The flag guard is the only executor-side layer:
    ``enrich_error_detail`` is itself fail-open by construction, so returns
    ``raw_detail`` unchanged when the module is unavailable.
    """
    if not _ATTRIBUTION_AVAILABLE:
        return raw_detail
    return enrich_error_detail(raw_detail, exc)


class PipelineExecutor(_HitlMixin, _StreamingMixin, _RunExecuteMixin, _FinalizeMixin, _TelemetryMixin):
    """Execute a single pipeline run (HITL-aware, supports parallel fan-out).

    ``astream_events`` delivers every node's events (including nodes running in
    parallel branches) through ONE async generator in superstep
    completion order, so ``_stream_graph`` needs no per-branch task management:
    ``completed_node_outputs`` is keyed by node_id (no clobbering), token usage
    accumulates per node name, and the RunawayGuard counters are incremented
    once per completed node / token report (each parallel node counts once;
    duration is wall-clock). Parallel fan-out itself is compiled natively by
    ``graph_cache.build_graph_from_json`` (multiple ``add_edge`` calls from one
    source → LangGraph runs them in the same superstep).

    A HITL interrupt raised in ONE parallel branch pauses the whole run; the
    already-completed sibling tasks in that superstep keep their state, and on
    ``resume`` the interrupted gate re-runs and any join nodes downstream of it
    re-run with the gate's decision (see ``_stream_graph`` interrupt handling).

    Args:
        db_engine: SQLAlchemy async engine for run CRUD operations.
        checkpointer_conn_string: psycopg-compatible connection string for
            LangGraph's AsyncPostgresSaver. If None, no checkpointer is used
            (runs will not persist checkpoints and HITL interrupts will not work).

    """

    def __init__(
        self,
        db_engine: AsyncEngine,
        *,
        checkpointer_conn_string: str | None = None,
        evidence_provider: EvidenceProvider | None = None,
        notifier: Any | None = None,
    ) -> None:
        self._engine = db_engine
        self._session_factory = async_sessionmaker(db_engine, expire_on_commit=False, autobegin=False)
        self._checkpointer_conn_string = checkpointer_conn_string
        self._otel_bridge = LangGraphOtelBridge()
        # Evidence seam: injected FakeEvidenceProvider in tests;
        # None selects the production E2B+DB-backed provider per run.
        self._evidence_provider = evidence_provider
        # Zombie-run protection hook (2026-08-05): wired by
        # ``pipeline_execution.run_executor_with_watchdog`` to an asyncio.Event.
        # Called when the FIRST node dispatches so the execute_run watchdog can
        # distinguish "hung in pre-node setup" (no progress) from a legitimate
        # long-running node (progress already signalled → watchdog stands down).
        self.on_first_progress: Callable[[], None] | None = None
        # Absolute node-deadline watchdog hooks (defense-in-depth): wired
        # by ``pipeline_execution.run_executor_with_watchdog`` to asyncio.Events.
        # Called when EACH node starts / completes (by node_id) so the watchdog
        # can measure each node's execution against its own ``timeout_seconds``
        # independently of idle/activity — catching a half-alive SSE stall that
        # defeats the idle-watchdog. Unlike ``on_first_progress`` (fires once),
        # these fire once per node.
        self.on_node_started: Callable[[str], None] | None = None
        self.on_node_completed: Callable[[str], Awaitable[Any] | None] | None = None
        # Per-node configured ``timeout_seconds`` (node_id -> seconds), populated
        # from the graph JSON before streaming so the watchdog can read it as a
        # shared dict reference. Empty until ``_prepare_and_stream`` runs.
        self._node_timeouts: dict[str, int] = {}
        # Fenced-lease authority (dist/runtime-core A1): the claim token
        # captured at execute/resume start. Every terminal/demotion write by
        # this executor is fenced against it so a superseded original cannot
        # write out from under a successor. Seeded into LangGraph state as
        # ``_claim_lease`` for the sandbox dispatch marker.
        self._claim_token: str | None = None
        # Gate IDs whose stamped terminate-rejection ended the last
        # stream (set by ``_stream_graph``, consumed by
        # ``_finalize_run_after_stream``). Per-executor, like ``_claim_token``.
        self._terminating_rejected_gates: set[str] = set()
        # The run-level connector fetch scope (``allowed_connectors``),
        # computed once at run start from the graph (node capability_scope union
        # Agent connector_type_refs grants) and reused by the compensation hub
        # so both run paths apply the same deny-by-default fetch gate.
        self._run_connector_scope: list[str] | None = None
        # Agent IDs whose collection_install is registry-sourced and
        # not-yet-granted.  These agents run under default-deny tool/connector
        # scope (no connectors, read-only tools) until the operator grants them.
        self._registry_gated_agents: set[str] = set()
        # Cancellation-intent signals wired by run_executor_with_watchdog so the
        # NodeCancelledError retry handler can tell a watchdog stall / supersession
        # from a genuine transient node cancellation and skip the pending-reset.
        self._stall_requested: asyncio.Event | None = None
        self._superseded: asyncio.Event | None = None
        # HITL-awaiting notifier seam (team-hitl-gates Known Gap, 2026-08-16):
        # inject the Notifier so the run lifecycle can dispatch the
        # ``hitl_awaiting`` webhook/in-app notification. Injected by the SAQ
        # execute/resume path (which already builds a Notifier for the system
        # worker crons); None skips dispatch (fail-open, never blocks the run).
        self._notifier: Any | None = notifier
        # Dispatch phase tracker (optional, wired by execute_run).
        # The tracker reports which phase the executor is in between claim and
        # first node dispatch.  Read by the zombie watchdog when the grace
        # period fires.
        self._dispatch_phase_tracker: Any | None = None

    async def _check_capacity(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        graph_json: dict[str, Any] | None = None,
        snapshot_id: uuid.UUID | None = None,
    ) -> Run:
        """Non-blocking capacity check using count-based comparison.

        Soft-cap, no advisory lock. If the pipeline's ``max_concurrent_runs``
        limit OR the organisation's sandbox concurrency cap (when the graph
        contains a ``sandbox_agent`` node and a cap is configured) OR the
        organisation's run-concurrency cap (``run_concurrency_limit``, when
        configured) is reached, the run is atomically demoted back to
        ``pending`` with a reason marker on ``error_code``
        (``pipeline_capacity`` / ``org_capacity_limited``) and recovered by
        ``dispatcher_reconcile`` (cron_helpers) / the stale-run sweep
        (pipeline_execution) — plan F3b: no in-process retry loop.

        The org run-concurrency cap is the CLAIM-TIME backstop for the
        dispatch-time admission gate in ``dispatch_run``. Dispatch-time
        admission counts active runs in one transaction but enqueues later;
        newly-enqueued runs stay ``pending`` (invisible to the count) until a
        worker claims them, so a burst of dispatches can each see
        ``active < limit`` and all enqueue — exceeding the org cap by the
        batch size. Re-checking the org run cap here, at claim time, closes
        that TOCTOU window (mirroring the sandbox-cap claim-time check).

        When *max_concurrent* is 0 or negative the pipeline is unlimited, but
        the organisation caps (sandbox + run, if configured) still apply.

        Fail-open, loud: any error reading the org settings, counting org
        runs, or scanning the graph logs a warning and ADMITS the run (treats
        it as no-cap) rather than raising.

        A 55P03 row-lock timeout on the admission transaction is
        retried ONCE. That transaction also spends one execution attempt, so a
        silent recovery would let the graph run uncounted. A second consecutive
        timeout does NOT terminalise: the executor attempts the fenced
        pending-reset and raises :class:`AdmissionLockTimeoutError`, which
        ``run_executor_with_watchdog`` maps to the non-terminal ``deferred``
        outcome. The run consumed zero executions (both admissions rolled
        back). The run's resting state depends on the reset: a CONFIRMED reset
        leaves it ``pending`` for ``dispatcher_reconcile``; a reset that itself
        timed out (``None``) leaves it ``running`` under our claim token for the
        nodeless/age backstop; a reset that RAISED leaves its outcome
        ``unknown``, worded as both possibilities. Either way the next
        admission counts the attempt.
        """
        org_sandbox_cap, org_sandbox_host_only, org_sandbox_population = await self._read_org_sandbox_cap_full(org_id, graph_json, snapshot_id)
        org_run_limit: int | None = await self._read_org_run_concurrency_limit(org_id)

        # The admission transaction ALSO spends one execution against
        # the retry budget (see ``_claim_under_capacity``). A 55P03 row-lock
        # timeout rolls that transaction back WHOLE — including the bump — so
        # proceeding after recovery would execute the graph UNCOUNTED. Retry the
        # admission+bump transaction exactly once.
        #
        # If the second attempt also times out, DEFER rather than terminalise.
        # Both attempts rolled back, so the run has consumed zero executions;
        # killing it ``executor_failed`` would be worse than the capacity-lock
        # behaviour this replaced. The run was already claimed ``running``, so
        # it must be handed back explicitly: the fenced pending-reset (the
        # established non-terminal deferral path used by the retry policy and
        # the watchdogs) leaves it recoverable by ``dispatcher_reconcile``'s
        # re-enqueue leg. Safe for a non-idempotent graph despite
        # ``_fenced_pending_reset``'s idempotency rule: ZERO nodes have
        # executed here (admission precedes streaming), so a re-dispatch cannot
        # repeat a side effect.
        run, first_timeout = await self._claim_or_lock_timeout(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            org_sandbox_cap=org_sandbox_cap,
            org_sandbox_host_only=org_sandbox_host_only,
            org_sandbox_population=org_sandbox_population,
            org_run_limit=org_run_limit,
        )
        if run is not None:
            return run
        _log.warning(
            "pipeline.capacity_check_lock_timeout run=%s — bounded mutation_row_lock_timeout_ms wait expired on the claim-time write; retrying the admission+bump transaction once",
            run_id,
            exc_info=first_timeout,
        )
        run, second_timeout = await self._claim_or_lock_timeout(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            max_concurrent=max_concurrent,
            org_sandbox_cap=org_sandbox_cap,
            org_sandbox_host_only=org_sandbox_host_only,
            org_sandbox_population=org_sandbox_population,
            org_run_limit=org_run_limit,
        )
        if run is not None:
            return run
        return await self._defer_after_admission_lock_timeout(run_id=run_id, org_id=org_id, lock_timeout=second_timeout)

    async def _claim_or_lock_timeout(self, **kwargs: Any) -> tuple[Run | None, Exception | None]:
        """Attempt ONE admission, translating a 55P03 row-lock timeout into data.

        The admission transaction also spends one execution against the retry
        budget, so a lock timeout rolls the bump back WHOLE. Returning the
        timeout (instead of raising) lets the caller retry exactly once and then
        defer; every non-55P03 failure and cancellation propagates unchanged.
        """
        try:
            return await self._claim_under_capacity(**kwargs), None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if sqlstate_of(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
                raise
            return None, exc

    async def _defer_after_admission_lock_timeout(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        lock_timeout: Exception | None,
    ) -> NoReturn:
        """Hand a zero-execution run back to ``pending`` after a second 55P03.

        The reset's OUTCOME is carried as a tri-state (plus "unknown" for an
        unexpected reset failure) — never a bool, because a fence loss (rowcount
        0) and a timeout (``None``) are different facts and the deferral log
        must describe each truthfully. The raised error maps to the non-terminal
        ``deferred`` outcome, so the run is never terminal-failed after consuming
        zero executions.
        """
        reset_state: AdmissionResetState = "unknown"
        try:
            reset = await self._fenced_pending_reset(run_id=run_id, org_id=org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The reset RAISED, so its outcome cannot be determined here.
            # ``unknown`` names BOTH possible resting states and claims neither
            # — a landed reset leaves the row ``pending`` (recovered by
            # dispatcher_reconcile's re-enqueue leg), a reset that did not land
            # leaves it ``running`` (the nodeless/age backstop). Deliberately no
            # re-read: opening a session just to look at the row is more surface
            # than this ambiguity warrants.
            _log.warning(
                "pipeline.capacity_check_lock_timeout run=%s — fenced pending-reset raised after the second admission timeout, so whether it landed is unknown; the deferral names both possible sweeps",
                run_id,
                exc_info=True,
            )
        else:
            if reset is None:
                reset_state = "timeout"
            elif reset:
                reset_state = "confirmed"
            else:
                reset_state = "fence_lost"
        _log.warning(
            "pipeline.capacity_check_lock_timeout run=%s — second bounded "
            "mutation_row_lock_timeout_ms wait expired; the admission and its "
            "execution-budget bump rolled back, so the attempt is NOT executed "
            "uncounted. Deferring (fenced pending-reset=%s) instead of "
            "terminal-failing a run that consumed zero executions",
            run_id,
            reset_state,
            exc_info=True,
        )
        # The deferral's observable contract differs with the reset: a confirmed
        # reset leaves the run ``pending`` (reconciled); a fence loss is owned by
        # a successor / already non-running; a timeout leaves the row most likely
        # running (backstop); a raised reset leaves the outcome ``unknown`` and
        # both possible resting states are named.
        raise AdmissionLockTimeoutError(run_id, attempts=2, reset_state=reset_state) from lock_timeout

    async def _claim_under_capacity(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        org_sandbox_cap: int | None,
        org_sandbox_host_only: bool,
        org_sandbox_population: str,
        org_run_limit: int | None,
    ) -> Run:
        """Claim the run or demote it to pending, in one bounded transaction.

        ``set_mutation_row_lock_timeout`` bounds the hot ``runs``
        row-lock wait BEFORE this transaction's first lock (``get_run`` below
        is a plain read; the first lock is the cancel/claim/demote write). This
        claim-time write is a RECOVERY path — the run was already claimed
        ``running`` by ``claim_run_async``, so a contended row must NOT
        terminal-fail it. Issued in the caller's transaction; the shared
        ``update_run_status`` stays unbounded -.
        """
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await set_mutation_row_lock_timeout(session)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            if run.cancellation_requested:
                await update_run_status(session, run_id, "cancelled")
                cancelled_run = await get_run(session, run_id)
                if cancelled_run is None:
                    raise RunNotFoundError(run_id)
                return cancelled_run

            capacity = await self._capacity_snapshot(
                session,
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
                max_concurrent=max_concurrent,
                org_sandbox_cap=org_sandbox_cap,
                org_sandbox_host_only=org_sandbox_host_only,
                org_sandbox_population=org_sandbox_population,
                org_run_limit=org_run_limit,
            )
            if not _all_capacities_ok(capacity.pipeline_ok, capacity.sandbox_ok, capacity.run_ok):
                return await self._demote_for_capacity(
                    session,
                    run_id=run_id,
                    max_concurrent=max_concurrent,
                    capacity=capacity,
                    org_sandbox_cap=org_sandbox_cap,
                    org_run_limit=org_run_limit,
                )

            if run.status not in _ADMISSIBLE_STATUSES:
                # The run went terminal (or hold) while a retry was backing
                # off — never resurrect it. Return it untouched so the
                # caller does not resume execution.
                return run
            refused = await self._retry_budget_gate(
                session,
                run=run,
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
            )
            if refused is not None:
                return refused
            admitted = await self._claim_run_and_audit(
                session=session,
                run_id=run_id,
                org_id=org_id,
                pipeline_id=pipeline_id,
            )
            # This attempt is EXECUTING — spend ONE execution
            # against the pipeline's retry budget. Placement is the
            # invariant: the capacity check above has passed (a demotion
            # returns above and never reaches this line), so the counter
            # grows if and only if the execution attempt actually begins.
            # Stamped in the SAME transaction as the admission decision, so
            # a crash immediately after this commit still leaves the
            # attempt counted; every pre-node death that follows
            # (checkpointer setup, graph compile, hub init) therefore
            # consumes budget, because the attempt was made. It is NEVER
            # stamped by the SAQ claim (``claim_run_async``) — that runs
            # before capacity and would charge a demoted claim — and never
            # by the resume claim (a checkpoint continuation, not a graph
            # execution).
            await self._spend_execution_attempt(session, run_id=run_id, org_id=org_id)
            return admitted

    async def _spend_execution_attempt(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
    ) -> None:
        """Spend ONE execution and open the matching per-attempt history row.

        Called from ``_claim_under_capacity`` in the SAME transaction as the
        admission decision, so the spend and the attempt row describe one
        event and can never disagree.

        The per-attempt row is FAIL-OPEN, inside a SAVEPOINT: diagnostics must
        never fail the admission. A bare try/except would be wrong on Postgres
        — a failed statement aborts the WHOLE transaction, so the caller's
        admission commit would fail instead of the attempt write alone. The
        savepoint rolls back just this statement. ``model_used`` is unknown at
        admission (nodes have not run).
        """
        from farna.core.pipeline_execution import bump_execution_attempt_count

        counted = await bump_execution_attempt_count(
            session,
            run_id,
            org_id,
            claim_token=self._claim_token,
        )
        if not counted:
            # The token/status fence rejected the bump. The caller's admission
            # branch proved the row was admissible, so this is a concurrent
            # transition (a successor rotated the token, or the row left
            # ``running`` in the same instant). Never let that be
            # indistinguishable from a successful spend: WARN, and proceed
            # only because every later write the attempt makes is itself
            # token-fenced (a successor that took the run spent its own
            # attempt in its own admission).
            _log.warning(
                "pipeline.execution_attempt_not_counted run=%s org=%s — the claim-token/status fence rejected the execution-budget bump; the attempt proceeds against a superseded or no-longer-running row (its writes remain fenced)",
                run_id,
                org_id,
            )
            return
        # Gated on ``counted``: a fence-rejected bump opened no execution, so
        # writing a row would make attempts OUTNUMBER ``execution_attempt_count``.
        try:
            from farna.db.crud.run_attempts import start_attempt

            async with session.begin_nested():
                await start_attempt(session, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.execution_attempt_row_failed run=%s org=%s — the per-attempt history row could not be written; the execution proceeds (attempt history is diagnostics, never a gate)",
                run_id,
                org_id,
                exc_info=True,
            )

    async def _capacity_snapshot(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        max_concurrent: int,
        org_sandbox_cap: int | None,
        org_sandbox_host_only: bool,
        org_sandbox_population: str,
        org_run_limit: int | None,
    ) -> _CapacitySnapshot:
        """Read the three capacity gates once, in the admission transaction."""
        pipeline_ok = True
        active_count = 0
        if max_concurrent > 0:
            active_count = await count_active_runs_for_pipeline(session, pipeline_id, include_pending=False, exclude_run_id=run_id)
            pipeline_ok = active_count < max_concurrent

        sandbox_count = 0
        if org_sandbox_cap is not None:
            sandbox_count = await self._org_sandbox_active_count(
                session,
                org_id,
                run_id,
                population=org_sandbox_population,
                host_resource_only=org_sandbox_host_only,
            )
        sandbox_ok = org_sandbox_cap is None or sandbox_count < org_sandbox_cap

        run_count = 0
        if org_run_limit is not None:
            run_count = await self._org_run_active_count(session, org_id, run_id)
        run_ok = org_run_limit is None or run_count < org_run_limit
        return _CapacitySnapshot(
            pipeline_ok=pipeline_ok,
            active_count=active_count,
            sandbox_ok=sandbox_ok,
            sandbox_count=sandbox_count,
            run_ok=run_ok,
            run_count=run_count,
        )

    async def _retry_budget_gate(
        self,
        session: AsyncSession,
        *,
        run: Run,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> Run | None:
        """Refuse an admission that would exceed the pipeline's retry budget.

        Defence in depth: THIS is the single point every graph
        execution is granted through (the capacity check has passed; the caller
        spends one execution after this gate). Returns the terminal row when the
        attempt is refused and ``None`` when the caller may admit. The policy is
        read here (soft-delete opt-out) rather than threaded
        from a caller, so no call site can omit the gate. ``None``/``{}``
        imposes no ceiling and is unaffected.
        """
        from farna.db.soft_delete import include_soft_deleted

        pipeline_row = (await session.execute(include_soft_deleted(select(Pipeline).where(Pipeline.id == pipeline_id)))).scalar_one_or_none()
        retry_policy = getattr(pipeline_row, "retry_policy", None)
        ceiling = retry_max_executions(retry_policy)
        consumed = retry_executions_consumed(execution_attempt_count=getattr(run, "execution_attempt_count", 0))
        if ceiling is None or not retry_budget_exhausted(retry_policy, execution_attempt_count=consumed):
            return None
        terminal_run = await update_run_status(
            session,
            run_id,
            "failed",
            error_code=_CODE_HARNESS_RETRY_BUDGET_EXHAUSTED,
            error_detail=(f"Run not admitted: its retry_policy budget is exhausted (execution_attempt_count={consumed} >= ceiling {ceiling}); the graph was not executed."),
            claim_token=self._claim_token,
            from_status="running",
        )
        if terminal_run is None:
            # The fenced write lost the row (cancellation raised, or a
            # superseded claim, in the same instant). Do NOT invent a terminal
            # row: re-read whatever the row now is.
            #
            # This MUST be a genuine re-read: the fenced write above is a raw
            # UPDATE that bypasses the ORM identity map, so a plain ``get_run``
            # in THIS session returns the STALE object loaded at the top of the
            # transaction — its ``claim_token``/``cancellation_requested``/
            # ``status`` are the pre-write values, making the reason split
            # below unreachable and masking a row that already left
            # ``running``. ``populate_existing`` forces the row read.
            terminal_run = await session.get(Run, run_id, populate_existing=True)
            if terminal_run is None:
                raise RunNotFoundError(run_id)
            _log.warning(
                "pipeline.retry_budget_gate_fence_lost run=%s org=%s status=%s — the terminal write was rejected; re-read the row instead of admitting the execution",
                run_id,
                org_id,
                terminal_run.status,
            )
            if terminal_run.status == "running":
                # A superseded token or a cancellation-pending row is STILL
                # ``running``, so ``execute()``'s ``status != "running"`` bail
                # would let it stream — exactly the execution past the ceiling
                # this gate exists to refuse. Keep the attempt non-executing:
                # raise, and let the wrapper map it to a non-terminal outcome.
                # Only a genuinely non-running row (a terminal or held state)
                # may be returned untouched.
                if terminal_run.claim_token != self._claim_token:
                    raise RetryBudgetGateRefusedError(run_id, reason="superseded claim token")
                if terminal_run.cancellation_requested:
                    raise RetryBudgetGateRefusedError(run_id, reason="cancellation pending")
                raise RetryBudgetGateRefusedError(run_id, reason="fence lost")
            return terminal_run
        _log.warning(
            "pipeline.retry_budget_exhausted_at_admission run=%s org=%s execution_attempts=%s ceiling=%s — refused to execute past the retry_policy budget",
            run_id,
            org_id,
            consumed,
            ceiling,
        )
        return terminal_run

    async def _demote_for_capacity(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        max_concurrent: int,
        capacity: _CapacitySnapshot,
        org_sandbox_cap: int | None,
        org_run_limit: int | None,
    ) -> Run:
        """Demote an over-capacity claim back to ``pending`` with a reason marker.

        The recovery sweeps (dispatcher_reconcile / stale-run) pick it up. FENCED
        to this executor's claim token and only from ``running`` (A1): a
        superseded original (token rotated by a successor) cannot demote the
        successor's running row back to pending.

        D8: this claim-time demotion is explicitly ADVISORY — a
        best-effort, lock-free, population-only read (its own-row lock exists
        only at this fenced demote write) that must NEVER take the per-org
        advisory lock (a count-then-row shape would invert the uniform
        row→advisory ordering). The runner-marker reconciliation sweep is its
        named backstop.
        """
        decline_code, decline_detail = self._capacity_decline(
            max_concurrent=max_concurrent,
            active_count=capacity.active_count,
            _pipeline_capacity_ok=capacity.pipeline_ok,
            org_sandbox_cap=org_sandbox_cap,
            org_count=capacity.sandbox_count,
            org_capacity_ok=capacity.sandbox_ok,
            org_run_limit=org_run_limit,
            org_run_count=capacity.run_count,
            org_run_capacity_ok=capacity.run_ok,
        )
        await update_run_status(
            session,
            run_id,
            "pending",
            error_code=decline_code,
            error_detail=decline_detail,
            claim_token=self._claim_token,
            from_status="running",
        )
        pending_run = await get_run(session, run_id)
        if pending_run is None:
            raise RunNotFoundError(run_id)
        return pending_run

    async def _check_spend_ceiling_gate(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        claim_token: str | None,
    ) -> Run | None:
        """Halt a run BEFORE any billable step when the org budget is exhausted.

        Reads the org's lifetime spend ceiling and its consumed total
        from the per-org cost-state row (``organisation_cost_state``; .
        If the org is already at/over its ceiling (no remaining budget for ANY
        new run), the run is terminalized as ``cost_ceiling_exceeded`` and
        returned so ``execute()`` never spawns an LLM / E2B call. Returns
        ``None`` when the run may proceed (no ceiling, budget remaining, or any
        read error).

        FAIL-OPEN by design (mirrors ``_check_capacity``): a settings/DB read
        failure must NEVER block the run — the terminal ledger block in
        ``finalize.py`` is the authoritative hard ceiling that refuses billing
        beyond the limit regardless of this pre-gate.

        Only the org ceiling is checked here (the per-run ceiling is enforced
        after the run has incurred cost, at the terminal ledger block, because a
        run's cost is only known once nodes have executed).
        """
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                # Bound the hot ``runs`` row-lock wait BEFORE the
                # transaction's first lock (the terminalizing
                # ``update_run_status`` below). Issued in the caller's
                # transaction; the shared ``update_run_status`` stays unbounded
                # -. A 55P03 lands in this method's existing fail-open
                # ``except Exception`` (WARNING + return None) — the ledger block
                # remains the authoritative hard ceiling.
                await set_mutation_row_lock_timeout(session)
                # Opt out of the global soft-delete filter — a
                # pending-deletion org (deleted_at stamped at initiate) is still
                # operationally live.  Skipping here means the pre-dispatch
                # ceiling gate returns None and the run proceeds unchecked.
                from farna.db.soft_delete import include_soft_deleted

                org = (await session.execute(include_soft_deleted(select(Organisation).where(Organisation.id == org_id)))).scalar_one_or_none()
                if org is None:
                    return None
                # The ceiling/cumulative live on the per-org cost-state
                # row; a missing row is the defaults (both ceilings unlimited,
                # cumulative 0), not an error.
                state = await get_org_cost_state(session, org_id)
                # Pass a minimal 1-cent charge as the "next run" so the gate
                # honours the documented at-ceiling / kill-switch semantics: with
                # zero remaining budget (cumulative >= ceiling, or ceiling == 0)
                # the run must be terminalized BEFORE any billable work, not left
                # to the finalize ledger block. A ceiling of 0 therefore blocks
                # every new run, and an org exactly at its ceiling (1 cent would
                # exceed it) is halted. A genuinely non-empty remaining budget
                # (>= 1 cent) still passes so the run can execute.
                decision = evaluate_org_spend_ceiling(
                    org_cumulative_spend_cents=(state.org_cumulative_spend_cents or 0) if state is not None else 0,
                    additional_cents=1,
                    spend_ceiling_cents=state.spend_ceiling_cents if state is not None else None,
                )
                if decision.allowed:
                    return None
                await update_run_status(
                    session,
                    run_id,
                    "cost_ceiling_exceeded",
                    error_code=ORG_CEILING_EXCEEDED,
                    error_detail=decision.message,
                    claim_token=claim_token,
                )
                halted_run = await get_run(session, run_id)
                if halted_run is None:
                    raise RunNotFoundError(run_id)
                _log.info(
                    "pipeline.spend_ceiling_gate_halt",
                    extra={"run_id": str(run_id), "org_id": str(org_id)},
                )
                return halted_run
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "pipeline.spend_ceiling_gate_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id)},
            )
            return None

    async def _check_policy_gate_pin(
        self,
        session: AsyncSession,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        snapshot: PipelineSnapshot,
        claim_token: str | None,
    ) -> Run | None:
        """Re-verify the snapshot's policy-gate pin fingerprint.

        Fail-CLOSED backstop for §3.4 on BOTH the execute() and resume()
        paths: the run-creation seam blocks a mismatched snapshot before
        dispatch, but the snapshot's pin columns could be corrupted AFTER
        creation (direct DB write, partial restore, migration accident).
        A mismatch terminalizes the run ``eval_failed`` / ``eval_blocked``
        BEFORE any evaluation happens, and the caller returns the row early
        — no compensation and no finalize run, because nothing has executed
        (spend-ceiling-gate early-return precedent).

        Returns ``None`` when there is nothing to verify: an unpinned
        snapshot (legacy NULL fingerprint) or a non-``str`` fingerprint
        (a test double / stand-in — gated so one can never trip a block).
        The terminalizing write happens in the CALLER's transaction
        (*session* is caller-owned so the execute() and resume() paths can
        pass a session that already holds the run's row lock).
        """
        fingerprint = snapshot.policy_gate_pins_fingerprint
        if not isinstance(fingerprint, str):
            # Unpinned (NULL) or a non-string stand-in — pre-pinning
            # behaviour: fall back to the pipeline's live gates.
            return None
        blocked, block_message = _verify_policy_gate_pin_fingerprint(
            org_id=org_id,
            run_id=run_id,
            snapshot_id=snapshot.id,
            snap_pins=snapshot.policy_gate_pins_json,
            saved_fingerprint=fingerprint,
            pipeline_id=snapshot.pipeline_id,
            snapshot_version=snapshot.snapshot_version,
        )
        if not blocked:
            return None
        await update_run_status(
            session,
            run_id,
            "eval_failed",
            error_code="eval_blocked",
            error_detail=block_message,
            claim_token=claim_token,
        )
        halted_run = await get_run(session, run_id)
        if halted_run is None:
            raise RunNotFoundError(run_id)
        _log.error(
            "policy_gates.pin_fingerprint_mismatch_terminalized",
            extra={
                "run_id": str(run_id),
                "org_id": str(org_id),
                "snapshot_id": str(snapshot.id),
                "pipeline_id": str(snapshot.pipeline_id),
            },
        )
        return halted_run

    async def _claim_run_and_audit(
        self,
        *,
        session: AsyncSession,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> Run:
        """Claim a capacity-admitted run (pending→running) and fire ``run_started``.

        Extracted from ``_check_capacity`` (pure refactor). PRD §8.12: a run
        "starts" exactly once — the pending→running claim transition in the
        execute() path. The resume() path sets ``running`` directly (no
        _check_capacity call) so the event fires once per run, not once per
        resume. Failure-isolated: a broken audit append must never block run
        admission (the savepoint rollback undoes only the audit write).
        """
        await update_run_status(
            session,
            run_id,
            "running",
            claimed_by=_WORKER_ID,
            clear_error_code=True,
        )
        running_run = await get_run(session, run_id)
        if running_run is None:
            raise RunNotFoundError(run_id)
        try:
            actor_user_id, actor_label = resolve_run_actor(
                trigger_type=running_run.trigger_type,
                trigger_id=running_run.trigger_id,
                account_id=running_run.account_id,
            )
            payload: dict[str, Any] = {
                "pipeline_id": str(pipeline_id),
                # The run-keyed audit payload carries the
                # run's execution origin ('dispatched' / NULL) so the audit log
                # can tell a dispatched run from one Modulo executed. Composed
                # from the shared serializer — core may not import
                # the API layer, which is exactly why the serializer lives here
                # in core and serves both layers.
                **run_provenance_fields(running_run),
                "summary": compose_run_started_summary(
                    await _safe_pipeline_name(session, pipeline_id, org_id, run_id),
                    pipeline_id,
                    running_run.trigger_type,
                ),
            }
            if actor_label is not None:
                payload["actor"] = actor_label
            await append_audit_event(
                session,
                org_id=org_id,
                event_type="run_started",
                actor_user_id=actor_user_id,
                resource_type="run",
                resource_id=run_id,
                payload_json=payload,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.run_started_audit_failed",
                extra={"run_id": str(run_id), "org_id": str(org_id)},
                exc_info=True,
            )
        return running_run

    async def _org_sandbox_active_count(
        self,
        session: AsyncSession,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        *,
        population: str = "graph",
        host_resource_only: bool = False,
    ) -> int:
        """Count the org's active sandbox runs (fail-open to 0 on read error).

        D8: ``population="marker"`` (the flag-on claim-time path)
        counts the narrowed running-only marker population; the default
        ``"graph"`` keeps the pre-D8 sandbox-graph count for flag-off.
        """
        try:
            if population == "marker":
                from farna.core.runner_capacity import count_active_runner_dispatches_for_decision

                return await count_active_runner_dispatches_for_decision(session, org_id, exclude_run_id=run_id, host_resource_only=host_resource_only)
            return await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_org_count_failed",
                extra={"org_id": str(org_id), "run_id": str(run_id)},
                exc_info=True,
            )
            return 0

    async def _org_run_active_count(self, session: AsyncSession, org_id: uuid.UUID, run_id: uuid.UUID) -> int:
        """Count the org's active runs (fail-open to 0 on read error)."""
        try:
            return await count_active_runs_for_org(session, org_id, include_pending=False, exclude_run_id=run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.org_run_count_failed",
                extra={"org_id": str(org_id), "run_id": str(run_id)},
                exc_info=True,
            )
            return 0

    async def _read_org_sandbox_cap_full(
        self,
        org_id: uuid.UUID,
        graph_json: dict[str, Any] | None,
        snapshot_id: uuid.UUID | None,
    ) -> tuple[int | None, bool, str]:
        """The claim-time cap read + its count-population contract (D8).

        Returns ``(cap, host_resource_only, population)``: flag-on resolves
        the tier-scoped cap through the shared runner-capacity reader (the
        absent-key Docker-tier default 4 counts host-resource providers only;
        an explicit value gates ALL runner dispatches) and pairs it with the
        matching ``"marker"`` population; flag-off keeps the pre-D8
        ``enforced_cap`` + ``"graph"`` population exactly.

        E2B carve-out omission (D8 qa F14, documented at both sites): this
        claim-time read does not model the dispatch gate's e2b skip — the
        demotion it drives is an ADVISORY backstop (the dispatch gate is the
        enforcement point), so a conservative host-only count here is
        acceptable over-demotion, never lost work.
        """
        try:
            has_sandbox = _sandbox_agent_for_snapshot(snapshot_id, graph_json) if snapshot_id is not None else _graph_contains_sandbox_agent(graph_json)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_graph_scan_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            return None, False, "graph"
        if not has_sandbox:
            return None, False, "graph"
        try:
            from farna.settings import get_settings

            flag_on = get_settings().runner_capacity_gate_enabled
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                if flag_on:
                    from farna.core.runner_capacity import read_runner_cap_contract

                    cap, host_resource_only = await read_runner_cap_contract(session, org_id)
                    return cap, host_resource_only, "marker"
                # The one reader returns the full contract; this
                # claim-time cap read enforces the flag-off window cap only
                # (absent key = no gate until D8's rollout flag).
                return (await get_sandbox_concurrency_limit(session, org_id)).enforced_cap, False, "graph"
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.sandbox_cap_read_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            return None, False, "graph"

    async def _read_org_run_concurrency_limit(self, org_id: uuid.UUID) -> int | None:
        """Read the org's run-concurrency cap (``None`` = no cap / fail-open).

        Mirrors :meth:`_read_org_sandbox_cap_full` but for the org-wide
        ``run_concurrency_limit`` (applies to EVERY run, not just sandbox
        graphs). Fail-open: any read error logs a warning and treats the org
        as uncapped.
        """
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                return await get_org_run_concurrency_limit(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.org_run_cap_read_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            return None

    @staticmethod
    def _capacity_decline(
        *,
        max_concurrent: int,
        active_count: int,
        _pipeline_capacity_ok: bool,
        org_sandbox_cap: int | None,
        org_count: int,
        org_capacity_ok: bool,
        org_run_limit: int | None = None,
        org_run_count: int = 0,
        org_run_capacity_ok: bool = True,
    ) -> tuple[str, str]:
        """Pick the capacity reason marker + sanitized human detail."""
        if not org_capacity_ok and org_sandbox_cap is not None:
            return (
                ERROR_CODE_ORG_CAPACITY_LIMITED,
                f"Org sandbox concurrency limit reached: {org_count} active, cap {org_sandbox_cap}",
            )
        if not org_run_capacity_ok and org_run_limit is not None:
            return (
                ERROR_CODE_ORG_CAPACITY_LIMITED,
                f"Org run concurrency limit reached: {org_run_count} active, cap {org_run_limit}",
            )
        return (
            ERROR_CODE_PIPELINE_CAPACITY,
            f"Pipeline max_concurrent_runs reached: {active_count} active, limit {max_concurrent}",
        )

    async def _load_claimed_conformance_guardrails(
        self,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
    ) -> tuple[list[Any], bool]:
        """Hoisted per-run claim discovery for the conformance re-check.

        Computed ONCE at run start (both ``execute`` and ``resume``) and seeded
        into the run-scoped conformance context, so the per-node check pays
        zero DB round-trips when the pipeline has no conformance claims and one
        query per run when it does. Returns ``(claimed, load_failed)`` — a
        load failure sets ``load_failed=True`` so the node gate fails CLOSED
        (unknown blocks) rather than silently skipping claims.
        """
        from farna.core.guardrails.conformance import load_claimed_guardrails

        return await load_claimed_guardrails(
            self._session_factory,
            org_id=org_id,
            pipeline_id=pipeline_id,
        )

    async def _load_eval_defs_for_pipeline(
        self,
        session: AsyncSession,
        pipeline_id: uuid.UUID,
    ) -> list[tuple[Eval, PolicyGate | None]]:
        """Load eval definitions for a pipeline that are scoped to a node.

        Reads from ``evals`` + ``policy_gates``.  Returns
        ``(Eval, PolicyGate | None)`` tuples — the PolicyGate is ``None``
        for guardrail-typed Evals and any eval whose binding was rejected
        during backfill.

        The gate row is returned EVEN WHEN ``enabled`` is
        ``false`` — the operator-control re-check happens in
        :meth:`_build_eval_defs_by_node`, where a deliberately-disabled gate
        remains distinguishable from a gate that never existed (the
        node-scoped-without-a-gate anomaly warning must not fire for an
        operator-disabled gate).  Filtering ``enabled`` out of this JOIN
        would make the two cases indistinguishable.
        """
        eval_stmt = (
            select(Eval, PolicyGate)
            .outerjoin(
                PolicyGate,
                (PolicyGate.eval_id == Eval.id) & (PolicyGate.organisation_id == Eval.organisation_id) & (PolicyGate.deleted_at.is_(None)),
            )
            .where(
                Eval.pipeline_id == pipeline_id,
                Eval.node_id.isnot(None),
                Eval.deleted_at.is_(None),
            )
        )
        return list((await session.execute(eval_stmt)).all())  # Row[tuple[Eval, PolicyGate | None]] (LEFT OUTER JOIN)

    @staticmethod
    def _resolve_governed_gate(
        eval_row: Eval,
        policy_gate: PolicyGate | None,
        pin: dict[str, Any] | None,
        *,
        pinned_universe: bool,
        pipeline_id: uuid.UUID,
    ) -> tuple[str, uuid.UUID | None, int | None, uuid.UUID | None, str | None]:
        """Resolve the gate that governs THIS run for one node-scoped eval.

        Chunk 10 (§3.1, §5.2, §6.1):

        * **Live fallback** (``pinned_universe=False`` — a snapshot with no
          ``policy_gate_pins_json``, §6.1 case (i)): the live, enabled
          ``PolicyGate.action`` governs.
        * **Pinned universe** (case ii/iii): pin MEMBERSHIP is the run's
          evaluation universe — an eval with no pin is never gate-governed
          regardless of live state (§3.1) — and the pin's ``action``
          governs (criterion 10): a live edit of ``action`` cannot re-score
          this run.
        * **Operator control overrides the pin** (§5.2): a live row with
          ``enabled`` false removes the gate from evaluation (criteria 8/9),
          whether or not it is pinned.  The re-check can only REMOVE gates
          from the evaluated set, never add them (§5.4 interleaving 2).

        Returns ``(failure_behaviour, policy_gate_id, policy_gate_version,
        policy_gate_node_id, evaluation_mode)``.  When no gate governs the
        eval the return is ``("warn", None, None, None, None)``: no
        ``PolicyGateDecision`` row can be written for it
        (``policy_gate_id is None`` downstream), the eval still runs, and it
        can never block.  A node-scoped non-guardrail eval with no gate row
        AND no pin keeps its pre-existing ``eval_defs.node_without_gate``
        anomaly warning.

        ``evaluation_mode`` (chunk 12, extended by 1749) is the
        gate's ``policy_gates.evaluation_mode``, resolved ONLY when the gate
        governs — ``"human"`` marks the eval for the shared-helper skip
        (chunk 12 §3.3). For a pinned gate the mode comes from the snapshot
        PIN, so the run START-time mode is frozen for the run's whole gate
        universe (every pinned gate, fired or not). A legacy pin that
        predates the field falls back to the live row's mode — today's
        behaviour — and a malformed pinned value logs and does the same.
        The LIVE fallback path (no pins at all) keeps the live row's mode.
        """

        eval_id = str(eval_row.id)
        pipeline_key = str(pipeline_id)

        # No live gate row at all.
        if policy_gate is None:
            return PipelineExecutor._governed_gate_absent_live_row(
                eval_row,
                pin,
                pinned_universe=pinned_universe,
                pipeline_id=pipeline_id,
            )

        # Pin membership first: a gate absent from the pin set is never
        # evaluated for this run, whatever the live state (§3.1).
        if pinned_universe and pin is None:
            _log.info(
                "eval_defs.gate_not_pinned",
                extra={"eval_id": eval_id, "pipeline_id": pipeline_key},
            )
            return _NO_GOVERNED_GATE

        # Operator control (§5.2): a disabled live row removes the gate from
        # evaluation — it overrides the pin. STRICT attribute read
        # F7): ``enabled`` is a NOT NULL column on every live row, so a
        # stand-in without the field is a fixture bug that must fail loudly
        # — the old getattr-default silently evaluated such a stand-in as
        # enabled, hiding incomplete test fixtures from this gate.
        if not policy_gate.enabled:
            _log.info(
                "eval_defs.gate_disabled_excluded",
                extra={
                    "eval_id": eval_id,
                    "pipeline_id": pipeline_key,
                    "policy_gate_id": str(policy_gate.id),
                },
            )
            return _NO_GOVERNED_GATE

        # Live fallback (§6.1 case (i)): the live action governs.
        if not pinned_universe:
            return (
                policy_gate.action,
                policy_gate.id,
                policy_gate.version,
                policy_gate.node_id,
                _governed_evaluation_mode(policy_gate),
            )

        # Pinned universe, live row present and enabled: the PIN governs
        # (criterion 10) — not the live ``action``. ``pin`` is non-None here
        # by construction: the no-pin case returned above (pinned universe)
        # and the live fallback returned just above.
        assert pin is not None
        return PipelineExecutor._governed_gate_from_pin(
            policy_gate,
            pin,
            eval_id=eval_id,
            pipeline_key=pipeline_key,
        )

    @staticmethod
    def _governed_gate_absent_live_row(
        eval_row: Eval,
        pin: dict[str, Any] | None,
        *,
        pinned_universe: bool,
        pipeline_id: uuid.UUID,
    ) -> tuple[str, uuid.UUID | None, int | None, uuid.UUID | None, str | None]:
        """Verdict when NO live PolicyGate row exists for this eval.

        Extracted from :meth:`_resolve_governed_gate` for cognitive-complexity
        budget (S3776) — behaviour is unchanged: every arm returns the
        ungoverned ``("warn", None, None, None, None)`` verdict, differing
        only in which structured event it emits.
        """
        eval_id = str(eval_row.id)
        eval_type = eval_row.eval_type
        pipeline_key = str(pipeline_id)
        if pinned_universe and pin is not None:
            # Pinned, but the live row is gone (soft-deleted since
            # snapshot creation) — skip, mirroring the guardrail-pin
            # soft-delete skip precedent.
            _log.info(
                "eval_defs.pinned_gate_unavailable",
                extra={"eval_id": eval_id, "pipeline_id": pipeline_key},
            )
            return _NO_GOVERNED_GATE
        if eval_type == "guardrail":
            # Guardrail-typed Evals keep warn semantics by design.
            return _NO_GOVERNED_GATE
        # Anomaly: node-scoped non-guardrail Eval without a gate. This
        # means its PolicyGate binding was rejected during backfill —
        # log a warning but do not crash.
        _log.warning(
            "eval_defs.node_without_gate",
            extra={
                "eval_id": eval_id,
                "eval_type": eval_type,
                "pipeline_id": pipeline_key,
            },
        )
        return _NO_GOVERNED_GATE

    @staticmethod
    def _governed_gate_from_pin(
        policy_gate: PolicyGate,
        pin: dict[str, Any],
        *,
        eval_id: str,
        pipeline_key: str,
    ) -> tuple[str, uuid.UUID | None, int | None, uuid.UUID | None, str | None]:
        """Resolve the governing verdict from a snapshot PIN.

        Extracted from :meth:`_resolve_governed_gate` for cognitive-complexity
        budget (S3776). The pin's ``policy_gate_id`` must still match the live
        row, its ``action`` must be in the closed decision vocabulary, and its
        optional ``node_id`` (when parseable) overrides the live row's node.
        Returns the ungoverned verdict on any pin-side rejection.

        ``evaluation_mode`` (1749): a pin carrying a closed-vocabulary
        ``evaluation_mode`` FREEZES the mode the run started with — a live
        flip before resume cannot change it. A legacy pin without the field
        falls back to the live row's mode (pre-1749 behaviour), and a
        malformed pinned value logs and does the same. A replaced gate (pin id
        mismatch) is ungoverned, so its pinned mode is never used.
        """
        if str(pin.get("policy_gate_id")) != str(policy_gate.id):
            # The pinned gate was replaced since snapshot creation (the live
            # row belongs to a different gate) — the pinned row is gone.
            _log.info(
                "eval_defs.pinned_gate_replaced",
                extra={"eval_id": eval_id, "pipeline_id": pipeline_key},
            )
            return _NO_GOVERNED_GATE

        pin_action = pin.get("action")
        if pin_action not in ("warn", "block"):
            # A malformed pin action can never drive a decision row. The
            # fingerprint (§3.3) makes this unreachable for verified pins;
            # case (ii) pins are trusted as-is, so fail open to warn here.
            _log.warning(
                "eval_defs.pin_action_invalid",
                extra={"eval_id": eval_id, "pipeline_id": pipeline_key, "action": str(pin_action)},
            )
            return _NO_GOVERNED_GATE

        gate_node_id = policy_gate.node_id
        pin_node_id = pin.get("node_id")
        if pin_node_id:
            try:
                gate_node_id = uuid.UUID(str(pin_node_id))
            except (ValueError, AttributeError, TypeError):
                _log.warning(
                    "eval_defs.pin_node_id_invalid",
                    extra={"eval_id": eval_id, "pipeline_id": pipeline_key},
                )

        # FAR-1749: the pin's mode is the run-start freeze — every pinned gate
        # (whether or not it has fired) resolves the mode the run started with.
        # Tuple membership (not a set): a malformed pin value can be an
        # unhashable JSON container, and ``in`` must degrade to the fallback
        # rather than raise.
        pinned_mode = pin.get("evaluation_mode")
        if pinned_mode in ("human", "automatic"):
            evaluation_mode = str(pinned_mode)
        else:
            # Legacy pin (pre-1749 snapshots carry no mode) or a malformed
            # value: fall back to the live row's mode — the pre-1749
            # behaviour. The fingerprint covers the stored pin, so a malformed
            # value here means an unverified (case ii) pin list; log it rather
            # than swallow it.
            if pinned_mode is not None:
                _log.warning(
                    "eval_defs.pin_evaluation_mode_invalid",
                    extra={
                        "eval_id": eval_id,
                        "pipeline_id": pipeline_key,
                        "evaluation_mode": str(pinned_mode),
                    },
                )
            evaluation_mode = _governed_evaluation_mode(policy_gate)

        return (
            str(pin_action),
            policy_gate.id,
            policy_gate.version,
            gate_node_id,
            evaluation_mode,
        )

    @staticmethod
    def _build_eval_defs_by_node(
        eval_rows: list[tuple[Eval, PolicyGate | None]],
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        policy_gate_pins: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[EvalDefDTO]]:
        """Convert (Eval, PolicyGate) rows to a dict keyed by node id.

        ``failure_behaviour`` and the PolicyGate metadata are resolved per
        eval by :meth:`_resolve_governed_gate` (chunk 10):

        * ``policy_gate_pins is None`` — legacy live fallback (§6.1 case
          (i)): the live, enabled ``PolicyGate.action`` populates
          ``failure_behaviour``; defaulting to ``"warn"`` when no gate is
          present (guardrail-typed Evals keep warn semantics, and any eval
          whose binding was rejected during backfill also defaults to warn).
        * ``policy_gate_pins`` is a list (including empty) — the PINNED set
          is the run's evaluation universe: the pin's ``action`` governs
          (a live edit cannot re-score this run), gates missing from the pin
          set are never evaluated, and a disabled live row removes a pinned
          gate from evaluation (operator control overrides the pin).

        Anomaly guard: if a node-scoped Eval has NO gate AND is NOT
        guardrail-typed, log a WARNING naming the eval id — this would be
        a silent block→warn downgrade from a rejected backfill binding.
        """
        policy_gate_pins = PipelineExecutor._normalise_policy_gate_pins(policy_gate_pins, pipeline_id)
        pinned_universe = policy_gate_pins is not None
        pinned_by_eval = PipelineExecutor._index_policy_gate_pins(policy_gate_pins)
        eval_defs_by_node: dict[str, list[EvalDefDTO]] = {}
        for eval_row, policy_gate in eval_rows:
            PipelineExecutor._append_eval_definition(
                eval_defs_by_node,
                eval_row,
                policy_gate,
                org_id=org_id,
                pipeline_id=pipeline_id,
                pin=pinned_by_eval.get(str(eval_row.id)) if pinned_universe else None,
                pinned_universe=pinned_universe,
            )
        return eval_defs_by_node

    @staticmethod
    def _normalise_policy_gate_pins(
        pins: list[dict[str, Any]] | None,
        pipeline_id: uuid.UUID,
    ) -> list[dict[str, Any]] | None:
        """Accept only the persisted pin-list shape; malformed pins use legacy resolution."""
        if pins is None or isinstance(pins, list):
            return pins
        _log.warning(
            "eval_defs.policy_gate_pins_malformed",
            extra={"pipeline_id": str(pipeline_id), "value_type": type(pins).__name__},
        )
        return None

    @staticmethod
    def _index_policy_gate_pins(pins: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for entry in pins or []:
            if not isinstance(entry, dict):
                continue
            eval_id = entry.get("eval_id")
            if eval_id and str(eval_id) not in indexed:
                indexed[str(eval_id)] = entry
        return indexed

    @staticmethod
    def _append_eval_definition(
        definitions: dict[str, list[EvalDefDTO]],
        eval_row: Eval,
        policy_gate: PolicyGate | None,
        *,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        pin: dict[str, Any] | None,
        pinned_universe: bool,
    ) -> None:
        node_key = str(eval_row.node_id) if eval_row.node_id else ""
        if not node_key:
            return
        failure_behaviour, gate_id, gate_version, gate_node_id, evaluation_mode = PipelineExecutor._resolve_governed_gate(
            eval_row,
            policy_gate,
            pin,
            pinned_universe=pinned_universe,
            pipeline_id=pipeline_id,
        )
        definitions.setdefault(node_key, []).append(
            EvalDefDTO(
                id=eval_row.id,
                org_id=org_id,
                pipeline_id=eval_row.pipeline_id,
                node_id=node_key,
                name=eval_row.name or "",
                eval_type=eval_row.eval_type,
                config=eval_row.config_json,
                failure_behaviour=failure_behaviour,
                pass_threshold=eval_row.pass_threshold,
                suite_id=eval_row.suite_id,
                version=eval_row.version,
                policy_gate_id=gate_id,
                policy_gate_version=gate_version,
                policy_gate_node_id=gate_node_id,
                policy_gate_evaluation_mode=evaluation_mode,
            )
        )

    @staticmethod
    def _frozen_gated_eval_modes(
        eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None,
        review_id: str,
    ) -> list[dict[str, Any]]:
        """The evaluation modes of the fired gate's source node, as of FIRE time.

        Chunk 12 (M4, §3.3 / §10): the mode is run-frozen — resolved
        when the run starts and unchanged for the run's duration. The
        interrupt handler stamps this list onto the claim row's
        ``gate_config_json`` when the gate fires; ``resume()`` reads it back
        and re-applies the modes to the freshly built DTO universe, so a
        mid-run flip of ``policy_gates.evaluation_mode`` between pause and
        resume cannot change the resumed run's behaviour (neither the
        human-governed eval skip nor the Evidence grounding can drift).

        Relationship to the 1749 pin freeze: for a snapshot whose pins
        carry ``evaluation_mode`` the freshly built DTO already holds the
        SAME run-start mode, so re-applying this stamp is a consistent
        NO-OP there. The stamp stays load-bearing for legacy pins (which
        carry no mode) and for the coalesced/claim path, and remains as
        defence-in-depth. Do not remove it.

        Returns ``[]`` for an unparseable gate id (manual nodes have no
        claim row) or when no eval defs were built for the run.
        """
        if not eval_definitions_by_node:
            return []
        parsed = parse_hitl_review_id(review_id)
        if parsed is None:
            return []
        source, _target = parsed
        frozen: list[dict[str, Any]] = []
        for dto in eval_definitions_by_node.get(source, []):
            if dto.policy_gate_id is None:
                # No governing gate at fire time — nothing to freeze.
                continue
            frozen.append(
                {
                    "eval_id": str(dto.id),
                    "policy_gate_id": str(dto.policy_gate_id),
                    # A governing gate always resolves a mode; the fallback
                    # keeps a legacy/stand-in DTO on today's behaviour.
                    "evaluation_mode": dto.policy_gate_evaluation_mode or "automatic",
                }
            )
        return frozen

    @staticmethod
    def _overlay_frozen_gated_eval_modes(
        eval_defs_by_node: dict[str, list[EvalDefDTO]],
        fired_gate_configs: Sequence[dict[str, Any] | None] | None,
    ) -> int:
        """Re-apply fire-time evaluation modes to a freshly built DTO universe.

        FAR-939 chunk 12 (M4): called by ``resume()`` AFTER
        ``_build_eval_defs_by_node`` and BEFORE the graph compile, so the
        compile cache key, the shared-helper skip and the Evidence grounding
        all see the modes the run STARTED with. Returns the number of
        entries applied (a pure no-op when no gate ever fired — every
        pre-M4 claim row carries no stamp).

        For a FAR-1749 snapshot the pin already yields the same run-start
        mode at DTO assembly, so this overlay is a consistent no-op there;
        it is kept (not removed) for legacy pins and the coalesced/claim
        path, and as defence-in-depth for the fired gate.

        A stamp whose ``policy_gate_id`` no longer matches the live DTO's
        governing gate (disabled / soft-deleted / replaced since fire) is
        SKIPPED — operator control and gate identity win over the freeze;
        only the mode of the SAME governing gate is restored.
        """
        if not fired_gate_configs:
            return 0
        by_eval_id = _index_dtos_by_eval_id(eval_defs_by_node)
        applied = 0
        for config in fired_gate_configs:
            for entry in _frozen_eval_mode_entries(config):
                if _apply_frozen_eval_mode_entry(entry, by_eval_id):
                    applied += 1
        return applied

    async def _run_post_node_evals(
        self,
        node_id: str,
        envelope: dict[str, Any],
        eval_definitions_by_node: dict[str, list[EvalDefDTO]],
        run_id: uuid.UUID,
        org_id: uuid.UUID | None,
        node_type_map: dict[str, str] | None = None,
    ) -> None:
        """FAR-305/FAR-971: run node-scoped evals for a completed node (standalone path).

        Per-eval compute→persist→decide (persist-before-decide, FAR-971 chunk 2).
        Each ``EvalResult`` is committed in its own transaction *before* the
        block/warn decision is taken, so a ``block`` eval's result is always
        durable when ``EvalBlockedError`` propagates.

        If a ``block`` eval fails, ``EvalBlockedError`` propagates to
        ``_stream_graph``'s existing handler, transitioning the run to
        ``eval_failed`` with ``error_code="eval_blocked"``.
        """
        eval_defs = eval_definitions_by_node.get(node_id)
        if not eval_defs:
            return
        # The captured ``output`` is the envelope ``{"artifacts": [...],
        # "output": {...}}``.  Validate the node's CONTRACT output (what the
        # agent produced — ``artifacts[0].output.output_json`` for a
        # sandbox_agent), NOT the telemetry-style outer ``output`` envelope
        # (FAR-311: the outer output carries status/summary/cost but no
        # pr_url / changed_files).
        eval_target = _resolve_post_node_eval_target(node_id, envelope, node_type_map)

        def _executor_resolve_eval_target(
            _eval_def: EvalDefDTO,
        ) -> Any:
            """Return the already-resolved eval target (Defect 4 fix)."""
            return eval_target

        def _on_post_node_eval_result(eval_def: EvalDefDTO, result: EngineEvalResult) -> None:
            """Per-eval structured log (Defect 3 fix — restores dropped log)."""
            _log.info(
                "post_node_eval.result",
                extra={
                    "node_id": node_id,
                    "eval_name": eval_def.name,
                    "eval_id": str(eval_def.id),
                    "passed": result.passed,
                    "score": result.score,
                    "detail": result.detail,
                },
            )

        await run_evals_persist_before_decide(
            eval_defs=eval_defs,
            resolve_eval_target=_executor_resolve_eval_target,
            run_id=run_id,
            org_id=org_id,
            session_factory=self._session_factory,
            node_id=node_id,
            # FAR-315: mirror the HITL-gate path (FAR-307) — resolve the LLM
            # judge callable from eval_def.config["model_backend_id"] via the
            # ModelBackendHub, so model_judge evals score against a real judge
            # instead of returning the fail-closed 0.0 "callable not provided".
            resolve_model_judge=resolve_model_judge_callable,
            on_eval_result=_on_post_node_eval_result,
        )

    async def _run_output_guardrails(
        self,
        *,
        node_id: str,
        envelope: dict[str, Any],
        guardrail_defs: list[EvalDefDTO],
        run_id: uuid.UUID,
        org_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """FAR-209 Track B: intercept a completed node's output envelope.

        Runs ONLY the output-point guardrails (filtered here — the input-point
        ones are excluded upstream, before this seam). Orders per the design:
        interception runs BEFORE `_run_post_node_evals` in
        `_handle_chain_end_event`, the redacted envelope is OVERWRITTEN into
        ``ctx.completed_node_outputs`` there, and downstream LangGraph state
        propagation is intentionally NOT in scope (documented known gap).

        A block outcome raises ``GuardrailBlockedError`` — the stream loop's
        existing ``except EvalBlockedError`` handler terminalizes the run
        (``eval_failed``) unchanged. A destroy-mode mechanism error (fail
        closed) becomes the same terminal block. Detection-only (replay /
        kill switch) never acts and returns the envelope untouched.
        """
        from farna.core.guardrails import GuardrailBlockedError, GuardrailRedactionMechanismError
        from farna.core.guardrails.output_intercept import run_output_interception_async

        if not org_id:
            _log.warning("guardrails.output_no_org", extra={"node_id": node_id})
            return envelope
        try:
            outcome = await run_output_interception_async(
                guardrail_defs,
                envelope,
                session_factory=self._session_factory,
                run_id=run_id,
                org_id=org_id,
            )
        except GuardrailRedactionMechanismError as exc:
            _log.exception("guardrails.output_destroy_mechanism_error")
            raise GuardrailBlockedError(node_id, f"guardrail output mechanism error (destroy redaction failed closed): {exc}") from exc
        except Exception as exc:
            # Defect path guard: any unexpected pass failure with a guarding
            # guardrail must not pass through unredacted — block. The shared
            # pass already fail-closes internally for detection mechanism
            # errors; this covers anything IT failed to convert.
            guarding = any((d.config.get("action") in ("block", "redact")) for d in guardrail_defs)
            if guarding:
                _log.exception("guardrails.output_pass_failed_closed")
                raise GuardrailBlockedError(node_id, "guardrail output mechanism error at node-output edge") from exc
            _log.warning("guardrails.output_pass_failed_open", extra={"node_id": node_id})
            return envelope
        # Typed round-trip: the seam returns the pass's redacted envelope
        # by reference (outcome is dynamically typed from the async helper).
        redacted: dict[str, Any] = outcome.payload
        if outcome.blocked:
            raise GuardrailBlockedError(outcome.blocking_eval_name or node_id, outcome.block_message)
        return redacted

    async def _intercept_node_output_and_run_evals(
        self,
        *,
        name: str,
        output: dict[str, Any],
        ctx: _StreamContext,
    ) -> dict[str, Any]:
        """FAR-209 Track B: split a completed node's eval defs at the output seam.

        Guardrail-typed defs bound to *name* are interception-owned, not
        eval-owned: the OUTPUT-point ones intercept THIS envelope (and the
        redacted copy is written back into ``ctx.completed_node_outputs``);
        the rest keep flowing through ``_run_post_node_evals``. A guardrail
        def left in the eval-shaped list would raise ``GuardrailMisroutedError``
        inside the eval engine and crash the run.

        Returns the (possibly redacted) envelope. A block outcome raises
        ``GuardrailBlockedError`` (an ``EvalBlockedError`` subclass) — the
        stream loop's handler terminalizes the run unchanged.
        """
        from farna.core.guardrails.output_intercept import is_output_interception

        node_defs = (ctx.eval_definitions_by_node or {}).get(name) or []
        # Only the OUTPUT-point guardrail defs intercept THIS envelope
        # (strictly the "output" literal -- an unknown value stays with the
        # eval-shape list below, where strict validation fails closed).
        # Input-point ones are owned by their upstream seam, so they keep
        # flowing in the eval-shaped path here.
        guardrail_defs = [d for d in node_defs if str(d.eval_type) == GUARDRAIL_EVAL_TYPE and is_output_interception(d)]
        if guardrail_defs:
            redacted = await self._run_output_guardrails(
                node_id=name,
                envelope=output,
                guardrail_defs=guardrail_defs,
                run_id=ctx.run_id,
                org_id=ctx.org_id,
            )
            output = redacted
            if ctx.completed_node_outputs is not None:
                ctx.completed_node_outputs[name] = redacted
        # Non-output defs (other eval types AND input-point guardrails) keep
        # flowing in the eval-shaped path — an output-point one is the only
        # def owned by THIS seam.
        eval_defs = [d for d in node_defs if not (str(d.eval_type) == GUARDRAIL_EVAL_TYPE and is_output_interception(d))]
        if eval_defs:
            await self._run_post_node_evals(
                name,
                output,
                {name: eval_defs},
                ctx.run_id,
                ctx.org_id,
                node_type_map=ctx.node_type_map,
            )
        return output

    async def _init_model_backend_hub(self, org_id: uuid.UUID) -> ModelBackendHub | None:
        """Load active model backends for the org and initialise ModelBackendHub.

        Sets the hub on the current ContextVar so node_runner can access it.
        Returns the hub (or None if no backends are configured) for cleanup
        in the caller's finally block.
        """
        hub: ModelBackendHub | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                backend_rows = (
                    (
                        await session.execute(
                            select(ModelBackend).where(
                                ModelBackend.organisation_id == org_id,
                                ModelBackend.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(backend_rows, list) and backend_rows:
                    from farna.settings import get_settings

                    _settings = get_settings()
                    from farna.core.secrets_backend import create_secrets_backend

                    secrets_backend = create_secrets_backend(
                        fernet_key=_settings.fernet_key,
                        session=session,
                    )
                    hub = ModelBackendHub()
                    await hub.__aenter__()
                    await hub.initialise(backend_rows, secrets_backend=secrets_backend)
                    set_model_backend_hub(hub)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.model_backend_hub_init_failed")
            if hub is not None:
                await _teardown_hub(hub)
            hub = None
        return hub

    async def _load_connector_scope_grants(
        self,
        session: Any,
        org_id: uuid.UUID,
        agent_ids: list[uuid.UUID],
    ) -> tuple[dict[str, set[str]], set[str]]:
        """Load connector grants and apply the FAR-764 execution gate.

        Agents from a ``registry_sourced`` collection install (registry-sourced
        installs still set it) that the operator has not yet granted are
        returned in the gated set and lose their connector grants
        (default-deny until ``grant_collection_agents`` flips the install).
        """
        from sqlalchemy import select

        from farna.core.capability_scope import agent_granted_connector_types
        from farna.db.models.agent import Agent
        from farna.db.models.collection_install import CollectionInstall

        grants: dict[str, set[str]] = {}
        gated: set[str] = set()
        if not agent_ids:
            return grants, gated
        agent_rows = (await session.execute(select(Agent).where(Agent.id.in_(agent_ids), Agent.organisation_id == org_id))).scalars().all()
        install_ids = {cid for agent in agent_rows if (cid := getattr(agent, "collection_install_id", None)) is not None}
        install_rows = []
        if install_ids:
            install_rows = (await session.execute(select(CollectionInstall).where(CollectionInstall.install_id.in_(install_ids)))).scalars().all()
        gated_installs = {str(ci.install_id) for ci in install_rows if ci.registry_sourced and not ci.agents_granted}
        for agent in agent_rows:
            agent_key = str(agent.id)
            grants[agent_key] = agent_granted_connector_types(agent.connector_type_refs)
            collection_install_id = getattr(agent, "collection_install_id", None)
            if collection_install_id is not None and str(collection_install_id) in gated_installs:
                gated.add(agent_key)
                grants.pop(agent_key, None)
        return grants, gated

    async def _resolve_run_connector_scope(
        self,
        session: Any,
        org_id: uuid.UUID,
        graph_json: dict[str, Any] | None,
    ) -> list[str] | None:
        """Resolve the run-level fetch scope (deny-by-default, FAR-435 on FAR-418).

        With *graph_json* (the run-start path) the scope is the union of every
        node's ``capability_scope.allowed_connectors`` and the referenced
        Agents' ``connector_type_refs`` grants; it is cached on the executor
        (``_run_connector_scope``) and reused by the compensation path (which
        has no graph).

        The connector scope is deny-by-default: only connector types granted by
        referenced agents are returned.

        Also enforces the execution gate (FAR-764 / ADR 032 D2): agents whose
        ``collection_install_id`` references a registry-sourced,
        not-yet-granted install get NO connector scope (default-deny) and are
        recorded in ``_registry_gated_agents`` for the node-level gate.
        """
        if graph_json is None:
            return self._run_connector_scope

        from farna.core.capability_scope import compute_run_fetch_scope

        agent_ids = _connector_scope_agent_ids(graph_json)
        grants, registry_gated = await self._load_connector_scope_grants(
            session,
            org_id,
            agent_ids,
        )
        self._registry_gated_agents = registry_gated
        allowed_connectors = compute_run_fetch_scope(graph_json, grants)
        self._run_connector_scope = allowed_connectors
        return allowed_connectors

    async def _persist_connector_degraded_markers(self, session: Any, hub: Any) -> None:
        """FAR-495 degraded-marker persistence (best-effort, own savepoint).

        Persists a degraded marker for the skipped instances and clears stale
        markers for instances that initialised successfully (a connector fixed
        via a config/plugin change stops being flagged degraded), inside its
        own savepoint so a failure here can NEVER fail or roll back the
        run-start transaction (the hub itself already logged the skips). Only
        instances actually attempted this run appear in ``hub.skipped`` /
        ``hub.healthy``, so out-of-scope instances are never touched.
        """
        from farna.db.crud.connector_instance import (
            clear_degraded_markers,
            mark_instances_degraded,
        )

        try:
            async with session.begin_nested():
                if hub.skipped:
                    await mark_instances_degraded(session, hub.skipped)
                if hub.healthy:
                    await clear_degraded_markers(session, hub.healthy)
        except Exception:
            # No run id in scope here (_init_connector_hub is
            # org-scoped); the instance ids are the correlatable
            # identifiers.
            _log.warning(
                "pipeline.connector_degraded_marker_failed skipped=%s healthy=%s",
                sorted(str(i) for i in hub.skipped),
                sorted(str(i) for i in hub.healthy),
                exc_info=True,
            )

    async def _build_connector_hub(
        self,
        session: Any,
        org_id: uuid.UUID,
        rows: list[Any],
        allowed_connectors: list[str] | None,
    ) -> Any:
        """Build, enter and initialise the ConnectorHub for the active rows.

        Connectors confirmed configured — any failure below re-raises
        (fail closed) via the configured path. The runtime-provider
        hub stays reachable for the teardown path via the ConnectorHub
        constructor seam (``runtime_provider=`` -> ``_runtime_provider``);
        _teardown_hub acloses it there, disposing provider-tracked workspaces
        e.g. billable E2B sandboxes.
        """
        from farna.core.connector_hub import ConnectorHub
        from farna.core.pipeline_engine.decorator import set_connector_hub
        from farna.core.runtime_provider import create_default_hub
        from farna.core.secrets_backend import create_secrets_backend
        from farna.settings import get_settings

        _settings = get_settings()
        secrets_backend = create_secrets_backend(
            fernet_key=_settings.fernet_key,
            session=session,
        )
        runtime_hub = create_default_hub()
        hub: ConnectorHub | None = None
        try:
            hub = ConnectorHub(
                secrets_backend=secrets_backend,
                runtime_provider=runtime_hub,
                org_id=str(org_id),
            )
            await hub.__aenter__()
            await hub.initialise(rows, allowed_connectors=allowed_connectors)
            if hub.skipped or hub.healthy:
                await self._persist_connector_degraded_markers(session, hub)
            set_connector_hub(hub)
        except BaseException:
            # A partially-initialised hub (constructed but __aenter__ or
            # initialise raised) must still be torn down before the
            # fail-closed re-raise: the caller sees hub=None for this
            # failure mode (this helper never returned), so without this
            # guard the teardown — and with it the runtime-provider
            # aclose disposing provider-tracked workspaces (e.g.
            # billable E2B sandboxes) — would be skipped exactly here.
            # Cleanup failures are logged inside _teardown_hub and
            # suppressed here so they never mask the original error.
            if hub is not None:
                with suppress(Exception):
                    await _teardown_hub(hub)
            raise
        return hub

    async def _hub_init_failure_hub(self, connectors_configured: bool, hub: Any) -> Any | None:
        """Fail-closed teardown for a generic hub-init failure; returns the final hub.

        *connectors_configured* means connectors ARE configured, OR we could
        NOT establish that they are NOT (a read/query error with the
        fail-closed default). A run that may require connector work must never
        fall through to ``hub=None`` — the node_runner would treat it as
        vacuous success, silently no-op'ing a configured remote integration
        and finalising the run GREEN. Re-raise so the run terminalises as
        FAILED.

        Defensive: ``connectors_configured`` is False ONLY on a confirmed
        empty result, which never raises — this branch is otherwise
        unreachable. Keep the vacuous-success return for safety.
        """
        if connectors_configured:
            _log.exception("pipeline.connector_hub_init_failed_configured")
            if hub is not None:
                await _teardown_hub(hub)
            raise
        _log.exception("pipeline.connector_hub_init_failed")
        if hub is not None:
            await _teardown_hub(hub)
        return None

    async def _init_connector_hub(
        self,
        org_id: uuid.UUID,
        *,
        graph_json: dict[str, Any] | None = None,
    ) -> Any | None:
        """Load active ConnectorInstance rows for the org and initialise ConnectorHub.

        Sets the hub on the current ContextVar so make_connector_fn can access it.
        Returns the hub (or None if no connectors are configured).

        When *graph_json* is provided (the run-start path), the hub applies a
        run-level fetch scope (deny-by-default, FAR-435 building on FAR-418): the
        union of every node's ``capability_scope.allowed_connectors`` and the
        referenced Agents' ``connector_type_refs`` grants. The hub decrypts ONLY
        those connectors, so out-of-scope credentials are never disclosed. The scope
        is cached on the executor and reused by the compensation path (which has no
        graph) so both apply the same deny-by-default fetch gate.

        Contract (fail-closed on the configured path):
          - If NO active connectors are configured for this run, returns None —
            the node_runner treats a None hub as vacuous success, which is correct
            only when no connector work is expected.
          - If connectors ARE configured, any failure to build the hub (settings
            read, secrets_backend construction, ConnectorHub construction,
            ``__aenter__``, or ``initialise``) RAISES, so the run terminalises as
            FAILED rather than silently completing green with no connector work.
        """
        hub: Any | None = None
        # Fail-closed default (FAR-439): ``connectors_configured`` starts True so
        # that ANY failure that prevents us from determining whether connectors
        # exist — a failed session / RLS / ``session.execute`` read, a settings
        # read, secrets_backend construction, ConnectorHub construction,
        # ``__aenter__``, or ``initialise`` — takes the re-raise path below. It is
        # set False ONLY when the reader query returns a confirmed-EMPTY result
        # (no error): the single case where ``hub=None`` (vacuous success) is
        # correct. A read/query error is NOT swallowed into the None path —
        # "couldn't determine whether connectors exist" is a fatal hub-build
        # failure (fail closed), not evidence that none are configured.
        connectors_configured = True
        try:
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                from sqlalchemy import select

                from farna.db.models.connector_instance import ConnectorInstance

                allowed_connectors = await self._resolve_run_connector_scope(session, org_id, graph_json)

                rows = (
                    (
                        await session.execute(
                            select(ConnectorInstance).where(
                                ConnectorInstance.organisation_id == org_id,
                                ConnectorInstance.status == "active",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if isinstance(rows, list) and rows:
                    # Connectors confirmed configured: build the hub (fail-closed).
                    hub = await self._build_connector_hub(session, org_id, rows, allowed_connectors)
                else:
                    # Confirmed-EMPTY result (no error): the ONE genuine "no
                    # connectors configured" case. ``connectors_configured`` False
                    # -> ``hub=None`` (vacuous success). Unreachable from a read
                    # error — the fail-closed default above re-raises instead.
                    connectors_configured = False
        except asyncio.CancelledError:
            raise
        except SharedBudgetUnavailableError:
            # Configured-but-unconstructable shared Redis budget / settings-read
            # failure on the executor path (FAR-439). Swallowing it and returning
            # None would make the connector node vacuously "succeed" with the
            # "no connector hub" fallback, silently no-op'ing the remote
            # integration and finalising the run GREEN. Fail closed, loudly.
            _log.exception("pipeline.connector_hub_init_failed_shared_budget")
            if hub is not None:
                await _teardown_hub(hub)
            raise
        except Exception:
            hub = await self._hub_init_failure_hub(connectors_configured, hub)
        return hub

    async def _compensate_blocked_run_best_effort(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        executed_nodes: dict[str, Any],
    ) -> None:
        """Run-termination compensation for a guardrail-blocked mid-run terminalization (FAR-291).

        A run whose earlier nodes performed connector side effects (pushed a PR,
        flipped a Linear status) before a later node's output was
        guardrail-blocked must have those side effects compensated. Invoked from
        ``_finalize_run_after_stream`` AFTER the terminal status write
        (``finalize_cost``), so both the ``execute()`` and ``resume()`` paths
        share this single wiring point.

        Uses its OWN fresh session (``set_rls_org`` inside ``session.begin``)
        and a FRESH connector hub for the compensation window — it never touches
        the claim_token-fenced ``finalize_cost`` transaction, and the hub
        teardown cannot interfere with the run's own hub lifecycle. Best-effort
        + failure-isolated (guard-the-guard): every raise is logged here and
        never propagates into terminalization.
        """
        hub: Any | None = None
        try:
            hub = await self._init_connector_hub(org_id)
            # The compensation + summary write commit INSIDE this session
            # transaction (so the blocked_partial summary is durable) BEFORE
            # the hub is torn down. If teardown failed inside the transaction
            # block it would roll the summary back with it — guard-the-guard:
            # teardown must never lose an already-committed compensation.
            async with self._session_factory() as session, session.begin():
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                run = await get_run(session, run_id)
                if run is None:
                    _log.warning("guardrails.compensation.run_not_found run=%s", run_id)
                    return
                from farna.core.guardrails.compensation import compensate_blocked_run

                await compensate_blocked_run(
                    session,
                    run,
                    guardrail_block=run.error_detail or "",
                    connector_hub=hub,
                    executed_nodes=executed_nodes,
                    blocking_eval_name="",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("guardrails.compensation.error run=%s", run_id)
        finally:
            if hub is not None:
                await _teardown_hub(hub)
            set_connector_hub(None)

    async def _enforce_resume_sandbox_capacity(
        self,
        session: AsyncSession,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        snapshot_id: uuid.UUID | None,
    ) -> None:
        """Atomic sandbox-capacity enforcement for a resume (FAR-1306 TOCTOU fix).

        The pre-check in the HITL route is a fast-fail optimisation; THIS is
        the real gate — serialised per-org via advisory lock so two concurrent
        approvals cannot both pass. Raises ``SandboxCapacityExceededError`` when
        the org's sandbox cap is reached.

        D8 (FAR-594) converges this path onto the runner-capacity scheme when
        the rollout flag is on: the per-org advisory lock uses the RESERVED
        ``runner-capacity`` namespace (never the shared
        ``_uuid_to_lock_keys`` keyspace) and the count is the narrowed
        running-only marker population with the tier-scoped Docker default.
        ``SET LOCAL lock_timeout`` bounds the advisory wait to the same
        Settings knob the dispatch gate uses: a crowded per-org lock degrades
        to the RETRYABLE ``SandboxCapacityExceededError`` (SQLSTATE 55P03 —
        ``sqlstate_of`` extraction from the DBAPI chain), never a hang on
        ``deadlock_timeout``. The caller has ALREADY written the run row
        (``update_run_status`` above) — the uniform row→advisory ordering that
        makes the scheme cycle-free with the dispatch gate. Flag-off keeps the
        pre-D8 behaviour exactly (legacy keyspace, graph-based count,
        ``enforced_cap``, no lock_timeout).

        E2B carve-out omission (D8 qa F14, deliberate): unlike the dispatch
        gate, this gate does NOT skip an e2b dispatch when the Docker-tier
        default bucket is saturated — a Docker-tier-saturated org also blocks
        e2b resumes (conservative: resumes are far rarer than dispatches, and
        an approval that waits for host-resource capacity is never lost work;
        the HITL pre-check mirrors the same omission — both sites documented).
        """
        from farna.core.runner_capacity import (
            runner_capacity_lock_keys,
        )

        graph_json = (await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == snapshot_id))).scalar_one_or_none()
        if not _graph_contains_sandbox_agent(graph_json):
            return
        from farna.settings import get_settings

        if get_settings().runner_capacity_gate_enabled:
            from farna.core.runner_capacity import count_active_runner_dispatches_for_decision
            from farna.db.sqlstates import sqlstate_of

            cap, host_resource_only = await self._read_runner_cap_converged(session, org_id)
            if cap is None:
                return
            try:
                # SET LOCAL lock_timeout BEFORE the advisory lock (qa F7): a
                # crowded per-org lock degrades to SQLSTATE 55P03 — mapped to
                # the RETRYABLE SandboxCapacityExceededError below — instead
                # of hanging on deadlock_timeout.
                lock_timeout_ms = get_settings().runner_capacity_lock_timeout_ms
                await session.execute(
                    text("SELECT set_config('lock_timeout', :val, true)"),
                    {"val": f"{lock_timeout_ms}ms"},
                )
                # pg_advisory_xact_lock(k1, k2) over the RESERVED namespace — all
                # workers/machines for the same org hash to the SAME key;
                # xact-scoped, released exactly at this transaction's commit /
                # rollback — never leaked onto a pooled connection.
                k1, k2 = runner_capacity_lock_keys(org_id)
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
                    {"k1": k1, "k2": k2},
                )
                active = await count_active_runner_dispatches_for_decision(session, org_id, exclude_run_id=run_id, host_resource_only=host_resource_only)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # qa F7: the designed degradation — lock_not_available (what
                # SET LOCAL lock_timeout actually raises) is a RETRYABLE
                # capacity denial, matching the dispatch gate's 55P03 →
                # RunnerCapacityDeniedError mapping (the resume caller maps
                # SandboxCapacityExceededError to the same ``capacity.org``
                # house failure). Any OTHER DB error is not a capacity signal
                # — re-raise.
                if sqlstate_of(exc) == "55P03":
                    _log.warning(
                        "pipeline.runner_resume_lock_degraded",
                        extra={"org_id": str(org_id), "run_id": str(run_id), "lock_timeout_ms": lock_timeout_ms},
                        exc_info=True,
                    )
                    raise SandboxCapacityExceededError(org_id) from exc
                raise
            if active >= cap:
                raise SandboxCapacityExceededError(org_id)
            return
        cap = (await get_sandbox_concurrency_limit(session, org_id)).enforced_cap
        if cap is None:
            return
        # pg_advisory_xact_lock(k1, k2) — the two int4 keys are derived
        # from a deterministic md5 of the org UUID (same pattern as the
        # break-glass migration), so all workers/machines for the same
        # org hash to the SAME key (unlike hash(str(org_id)), which
        # PYTHONHASHSEED salts per-process). xact-scoped, so it is
        # released automatically exactly at this transaction's commit /
        # rollback — never leaked onto a pooled connection.
        k1, k2 = _uuid_to_lock_keys(org_id)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
            {"k1": k1, "k2": k2},
        )
        active = await count_active_sandbox_runs_for_org(session, org_id, exclude_run_id=run_id)
        if active >= cap:
            raise SandboxCapacityExceededError(org_id)

    async def _read_runner_cap_converged(self, session: AsyncSession, org_id: uuid.UUID) -> tuple[int | None, bool]:
        """The D8-converged cap read for the resume path (flag-on only).

        Returns ``(cap, host_resource_only)`` from the shared runner-capacity
        reader; identical per-org derivation to the dispatch gate (asserted by
        the convergence tests). Fail-open to no-gate on any read error.
        """
        try:
            from farna.core.runner_capacity import read_runner_cap_contract

            return await read_runner_cap_contract(session, org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.runner_cap_read_failed",
                extra={"org_id": str(org_id)},
                exc_info=True,
            )
            return None, False

    async def _read_live_earned_for_pin(self, pipeline_id: uuid.UUID, org_id: uuid.UUID) -> str | None:
        """Read the pipeline's live ``earned_autonomy_level`` for the run-start pin.

        Failure-isolated (FAR-1175): any read error degrades to ``None`` so a
        transient DB failure can never seed a PIN of ``None`` — the caller then
        falls back to the pinned default. Cancellation still propagates.
        """
        try:
            from farna.db.crud.pipeline import get_pipeline

            async with self._session_factory() as session, session.begin():
                from farna.db.rls import set_rls_org

                await set_rls_org(session, org_id)
                pipeline = await get_pipeline(session, pipeline_id, organisation_id=org_id)
                if pipeline is not None:
                    earned = getattr(pipeline, "earned_autonomy_level", None)
                    if isinstance(earned, str) and earned:
                        return earned
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.executor.earned_autonomy_read_failed",
                extra={"run_pipeline_id": str(pipeline_id), "org_id": str(org_id)},
                exc_info=True,
            )
        return None

    async def _resolve_earned_at_start(
        self,
        pipeline_id: uuid.UUID,
        org_id: uuid.UUID,
        snapshot: Any,
    ) -> str | None:
        """Pin the earned autonomy level for a run at its first execution (FAR-1175).

        Returns the level the run will use as its in-flight *earned* base,
        clamped to the pipeline ceiling:

        * the live ``earned_autonomy_level`` from the pipeline row when set,
        * else the snapshot's ``default_autonomy_level``,
        * clamped to ``max_autonomy_level`` (falling back to the default as the
          ceiling, mirroring runtime resolution).

        Failure-isolated: any read error degrades to the pinned default so a
        transient DB failure can never seed a PIN of ``None`` (which would let a
        later live promotion loosen the run). Returns ``None`` only when no
        default is available either.
        """
        default_level = getattr(snapshot, "default_autonomy_level", None)
        ceiling = getattr(snapshot, "max_autonomy_level", None)
        earned_live = await self._read_live_earned_for_pin(pipeline_id, org_id)

        base = earned_live or (default_level if isinstance(default_level, str) and default_level else None)
        if base is None:
            return None
        # Clamp to the ceiling using the rank map (levels are not lexicographic).
        try:
            base_level = AutonomyLevel(base)
        except ValueError:
            return None
        return _clamp_to_ceiling(base_level, ceiling)


# ---------------------------------------------------------------------------
# End of the PipelineExecutor class body.
#
# ``_HitlMixin`` / ``_StreamingMixin`` / ``_RunExecuteMixin`` /
# ``_FinalizeMixin`` / ``_TelemetryMixin`` are private aliases for the mixin
# bases (steps 2-4 of GAPS-harden-w2-executor.md section 5). They are composition
# detail, not importable surface: dropping the bindings here keeps ``dir()`` on
# this module exactly what it was before the split. Everything the rest of the
# codebase imports or ``mock.patch``es still resolves, because the methods live
# on the class through the MRO and the moved bodies resolve their free names via
# ``executor_parts.support._ref``.
# ---------------------------------------------------------------------------
del _HitlMixin, _StreamingMixin, _RunExecuteMixin, _FinalizeMixin, _TelemetryMixin
