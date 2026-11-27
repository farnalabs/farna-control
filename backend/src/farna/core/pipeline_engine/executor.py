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
from datetime import UTC, datetime, timedelta
from decimal import Decimal  # noqa: F401
from inspect import isawaitable
from typing import Any, Literal, NamedTuple, NoReturn  # noqa: F401

from langchain_core.messages import BaseMessage  # noqa: F401
from langgraph.errors import GraphInterrupt, NodeCancelledError
from opentelemetry import context as context_api
from opentelemetry.trace import set_span_in_context
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
    derive_node_type_map,
    finalize_cost,  # noqa: F401
)
from farna.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from farna.core.eval_engine import (
    EvalBlockedError,
)
from farna.core.eval_engine import (
    EvalResult as EngineEvalResult,
)
from farna.core.graph_validator import GraphValidator
from farna.core.graph_validator._types import ValidationResult
from farna.core.hitl_manager import HITLManager
from farna.core.hitl_manager.gate_coalescing import SUPERSEDE_REASON, evaluate_gate_coalescing  # noqa: F401
from farna.core.model_backend_hub import ModelBackendHub
from farna.core.node_output_split import (
    DEFAULT_NODE_TYPE,  # noqa: F401
    SPLITTABLE_NODE_TYPES,  # noqa: F401
    extend_node_type_map_from_edges,
    node_telemetry,  # noqa: F401
    resolve_node_contract_output,  # noqa: F401
)
from farna.core.notifier import (
    EVENT_BUDGET_EXCEEDED,
    EVENT_EVAL_BLOCKED,
    EVENT_HITL_AWAITING,
    EVENT_PROVIDER_FAILURE,  # noqa: F401
    EVENT_RUN_FAILED,
    EVENT_RUN_STALLED,
)
from farna.core.pipeline_engine import retry_compensation as rc
from farna.core.pipeline_engine import retry_policy as _retry_policy_module
from farna.core.pipeline_engine.decorator import (
    _RESERVED_RUN_CONTEXT_KEYS,  # noqa: F401
    RunCancelledError,
    set_audit_hook,
    set_cancellation_check,
    set_connector_hub,
    set_model_backend_hub,
)
from farna.core.pipeline_engine.error_codes import (
    _CODE_CONNECTOR_FAILED,
    _CODE_CONNECTOR_UNKNOWN,  # noqa: F401
    _CODE_HARNESS_RETRY_BUDGET_EXHAUSTED,
    _CODE_SANDBOX_AGENT_FAILED,  # noqa: F401
    _CODE_SANDBOX_NO_OUTPUT_JSON,  # noqa: F401
    map_legacy_code,
    sanitize_error_text,  # noqa: F401
    should_notify_terminal_failure,
)
from farna.core.pipeline_engine.errors import ResumeClaimSupersededError, RouterNoMatchError  # noqa: F401
from farna.core.pipeline_engine.eval_persist_order import (
    EvalDefDTO,
    run_evals_persist_before_decide,
)
from farna.core.pipeline_engine.event_broker import RunEventBroker, get_registry
from farna.core.pipeline_engine.evidence import (
    EvidenceProvider,
    build_default_evidence_provider,  # noqa: F401
    compute_work_intact,  # noqa: F401
    evidence_enabled,  # noqa: F401
    node_declared_success,  # noqa: F401
    run_evidence_probe,  # noqa: F401
)
from farna.core.pipeline_engine.graph_cache import build_graph_from_json, get_or_compile, struct_hash_with_eval_defs
from farna.core.pipeline_engine.hitl_context import HitlReviewContext, build_hitl_review_context
from farna.core.pipeline_engine.idempotency import read_before_write_suppression
from farna.core.pipeline_engine.llm_judge import resolve_model_judge_callable
from farna.core.pipeline_engine.model_backend_errors import (
    NON_RETRYABLE_PROVIDER_CODES,
    classify_provider_error,
    non_retryable_provider_code,
)
from farna.core.pipeline_engine.modulo_saver import ModuloPostgresSaver  # noqa: F401
from farna.core.pipeline_engine.node_runner import (
    LEGACY_SYNTHETIC_FAILURE_MARKERS,  # noqa: F401
    SYNTHETIC_FAILURE_MARKER,  # noqa: F401
    ConnectorNodeFailedError,
    OutputSchemaValidationError,
    SandboxNodeFailedError,
    SandboxTierRefusedError,
    SupersededNodeError,
    _idempotency_gate_skipped_envelope,
    _marker_delivery_done_for_node,  # noqa: F401
    merge_recovered_token_usage,
    set_conformance_ctx,
)
from farna.core.pipeline_engine.output_filter import OutputRejectedError
from farna.core.pipeline_engine.port_resolver import compute_port_topology_hash  # noqa: F401
from farna.core.pipeline_engine.retry_policy import (
    _ERROR_CODE_AGENT_FAILED,  # noqa: F401
    _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE,
    _ERROR_CODE_NODE_TIMEOUT,
    _ERROR_CODE_SANDBOX_TIER_REFUSED,
    _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN,
    _attempt_cap_reached,
    _attempt_capped_failure_detail,
    _attempt_started_at,
    _can_fenced_requeue,
    _failure_class_attempt_cap,
    _non_retryable_failure_detail,
    _transient_retry_allowance,
    maybe_retry_after_policy,
    sandbox_failure_error_code,
)
from farna.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from farna.core.pipeline_engine.runtime_retry import COMPENSATION_FAILED_CODE, CompensationFailedError
from farna.core.retry_budget import (
    # The ONE retry-budget module (zero-import leaf): the bound, the ceiling and
    # the consumed measure all live there so no path can define a second copy.
    retry_budget_exhausted,
    retry_executions_consumed,
    retry_max_executions,
)
from farna.core.run_context.autonomy import (
    PIPELINE_EARNED_AT_START_KEY,
    PIPELINE_MAX_AUTONOMY_KEY,  # noqa: F401
    AutonomyLevel,
    autonomy_level_rank,  # noqa: F401
)
from farna.core.run_provenance import run_provenance_fields
from farna.core.spend_ceiling import ORG_CEILING_EXCEEDED, evaluate_org_spend_ceiling
from farna.core.trigger_engine.agent_signal import fire_agent_signal
from farna.db.crud.hitl_review_config import (
    parse_hitl_review_id,
    resolve_hitl_review_config,
    resolve_review_window_for_gate,
)
from farna.db.crud.organisation_cost_state import get_org_cost_state
from farna.db.crud.pipeline import get_pipeline
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
from farna.db.models.hitl_claim import HitlClaim
from farna.db.models.model_backend import ModelBackend
from farna.db.models.organisation import Organisation
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.models.policy_gate import PolicyGate
from farna.db.models.run import Run
from farna.db.models.run_attempt import ATTEMPT_TERMINAL_REASON_RESET
from farna.db.rls import set_rls_execution_context, set_rls_org
from farna.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, is_lock_abort, sqlstate_of  # noqa: F401
from farna.lifecycle import ACTIVE_RUN_STATUSES, TERMINAL_STATUSES
from farna.otel_bridge import LangGraphOtelBridge, trace_id_for_thread  # noqa: F401

# ---------------------------------------------------------------------------
# Module-level helpers, value objects and private exceptions extracted into the
# ``executor_parts`` package (GAPS-harden-w2-executor.md §5 step 1). They are
# re-exported here so every existing import path and every
# ``mock.patch("farna.core.pipeline_engine.executor.<name>")`` target keeps
# resolving. Do not redefine these names in this module.
#
# ``_FinalizeMixin`` / ``_TelemetryMixin`` are the step 2-3 class splits: the
# finalize and telemetry method groups moved into mixin modules, imported under
# private aliases so they do NOT join this module's importable surface.
# ---------------------------------------------------------------------------
from .executor_parts.finalize import FinalizeMixin as _FinalizeMixin
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
    _enforce_resume_pipeline_capacity,
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
    "_enforce_resume_pipeline_capacity",
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


class PipelineExecutor(_FinalizeMixin, _TelemetryMixin):
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
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        await set_mutation_row_lock_timeout(session)
        run = await get_run(session, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        # FAR-2705 (defence in depth): the ``running`` write below takes a
        # PIPELINE slot, so it is capacity-admitted through the SAME production
        # decision the HITL claim/approval routes use. A run that already holds
        # its slot is admitted; only a parked/awaiting run at cap is refused,
        # with the resume deferred (the decision stays committed).
        #
        # The admission is skipped when the caller's fence is already stale: a
        # supersession is not a capacity question, so the fenced claim below
        # must be the thing that reports it (``ResumeClaimSupersededError``)
        # rather than being pre-empted by a capacity refusal.
        fence_holds = expected_claim_status is None or (claim_token is not None and run.status == expected_claim_status and run.claim_token == claim_token)
        if fence_holds:
            await _enforce_resume_pipeline_capacity(session, run_id=run_id, org_id=org_id)
        if expected_claim_status is not None:
            if claim_token is None:
                raise ValueError("expected_claim_status requires claim_token")
            run = await _claim_resume_for_executor(
                session,
                run_id=run_id,
                org_id=org_id,
                expected_claim_token=claim_token,
                source_status=expected_claim_status,
            )
        else:
            await update_run_status(session, run_id, "running", claimed_by=_WORKER_ID)
        if check_sandbox_capacity:
            await self._enforce_resume_sandbox_capacity(
                session,
                org_id=org_id,
                run_id=run_id,
                snapshot_id=run.snapshot_id,
            )
        snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
        snapshot = snapshot_result.scalar_one()
        graph_json: dict[str, Any] = snapshot.graph_json
        node_type_map = derive_node_type_map(graph_json)
        validation = await GraphValidator().validate_for_run(snapshot, {}, session)
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
                    select(HitlClaim.gate_config_json).where(
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
        await set_rls_org(session, org_id)
        await set_rls_execution_context(session)
        from farna.db.soft_delete import include_soft_deleted

        pipeline_result = await session.execute(include_soft_deleted(select(Pipeline).where(Pipeline.id == pipeline_id)))
        pipeline = pipeline_result.scalar_one_or_none()
        if pipeline is None:
            raise RunNotFoundError(run_id)
        run = await get_run(session, run_id)
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
        run_ref = rc.build_run_ref(str(pipeline_id), scalars["run_number"])

        def _node_idempotency_key(node_id: str, _state: dict[str, Any]) -> str | None:
            return rc.node_idempotency_key(run_ref=run_ref, node_ref=node_id)

        compiled = get_or_compile(
            pipeline_id,
            snapshot_id,
            lambda: build_graph_from_json(
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
            graph_struct_hash=struct_hash_with_eval_defs(
                compute_retry_aware_topology_hash(graph_json, pipeline_retry_policy),
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
        set_conformance_ctx(
            self._session_factory,
            org_id,
            snapshot.environment_profile_id,
            pipeline_id,
            claimed,
            claims_load_failed,
        )
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        set_audit_hook(self._dispatch_context_write_audit(org_id, run_id))
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
            async with _checkpointer_scope(
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
            _raw_tb = _traceback_detail(exc, limit=2000)
            error_detail = _enrich_failure_detail(_raw_tb, exc)
            _log.exception(
                "pipeline.resume_error",
                extra={
                    "run_id": str(run_id),
                    **(
                        {
                            "failure_build_sha": _failure_build_sha(),
                            "failing_column": _failure_column(exc),
                        }
                        if _ATTRIBUTION_AVAILABLE
                        else {}
                    ),
                },
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
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            await set_mutation_row_lock_timeout(session)
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
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
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
        _tb = _traceback_detail(exc, limit=2000)
        error_detail = _enrich_failure_detail(_tb, exc)
        _log.exception(
            "pipeline.execution_error",
            extra={
                "run_id": str(run_id),
                **(
                    {
                        "failure_build_sha": _failure_build_sha(),
                        "failing_column": _failure_column(exc),
                    }
                    if _ATTRIBUTION_AVAILABLE
                    else {}
                ),
            },
        )
        final_status = "failed"
        error_code = type(exc).__name__
        _provider_refusal = non_retryable_provider_code(str(exc)) or non_retryable_provider_code(error_detail)
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
            retry_decision = await maybe_retry_after_policy(
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
                graph_has_script_mode_fn=_graph_has_script_mode,
                node_timeouts=self._node_timeouts.values(),
            )
        if retry_decision == "side_effect_unknown":
            final_status = "failed"
            error_code = _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN
            error_detail = _sanitize_detail(
                "Script-mode sandbox has an unresolved execution claim (side effect unknown); not retried — needs human review.",
                limit=5000,
            )
        return retry_decision, final_status, error_code, error_detail

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
                pipeline = await get_pipeline(session, pipeline_id, organisation_id=org_id)
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
                return await build_hitl_review_context(
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
                return await resolve_hitl_review_config(
                    session,
                    run_id=run_id,
                    review_id=review_id,
                    org_id=org_id,
                )
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
                window_seconds = await resolve_review_window_for_gate(
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
        set_cancellation_check(None)
        set_audit_hook(None)
        set_model_backend_hub(None)
        if model_backend_hub is not None:
            await _teardown_hub(model_backend_hub)
        if final_status not in ("awaiting_human", "complete"):
            get_registry().close(run_id)

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
        graph_idempotent = _graph_is_idempotent(graph_json)

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
        run_ref = rc.build_run_ref(str(pipeline_id), run_number)

        def _node_idempotency_key(node_id: str, _state: dict[str, Any]) -> str | None:
            return rc.node_idempotency_key(run_ref=run_ref, node_ref=node_id)

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
            error_detail = _sanitize_detail(str(exc), limit=None)
        except (NodeCancelledError, SandboxNodeFailedError) as exc:
            node_token_usage = merge_recovered_token_usage(node_token_usage, exc)
            tier_refused = isinstance(exc, SandboxTierRefusedError)
            if tier_refused:
                _log.warning(
                    "pipeline.tier_refused_terminal",
                    extra={"run_id": str(scope.run_id), "exc_type": type(exc).__name__},
                )
                final_status = "failed"
                error_code = _ERROR_CODE_SANDBOX_TIER_REFUSED
                error_detail = _sanitize_detail(str(exc), limit=None)
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
        signal_node_type_map = extend_node_type_map_from_edges(node_type_map, graph_json)
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
        execution_attempt_count = retry_executions_consumed(
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
        failure_code = classify_provider_error(str(exc))
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
        failure_class_code = _sandbox_failure_class_code(exc)
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
        return _transient_retry_allowance(retries, pipeline_retry_policy)

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
            "error_code": _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE,
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
        provider_code = non_retryable_provider_code(str(exc))
        if provider_code is None:
            return None
        error_detail = _sanitize_detail(
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
            non_retryable_code=failure_code if _is_non_retryable_error_code(failure_code) else None,
            failure_class_code=failure_class_code,
            attempt_cap=_failure_class_attempt_cap(failure_class_code),
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
            completed_node_outputs[transient["gated_node_id"]] = _idempotency_gate_skipped_envelope(transient["gated_node_id"])
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
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            run = await get_run(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            # Opt out of the global soft-delete filter -- an in-flight
            # run's pipeline may have been soft-deleted; the pipeline must still
            # be loadable for graph construction.
            from farna.db.soft_delete import include_soft_deleted

            pipeline_result = await session.execute(include_soft_deleted(select(Pipeline).where(Pipeline.id == run.pipeline_id)))
            pipeline = pipeline_result.scalar_one()
            snapshot_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
            snapshot = snapshot_result.scalar_one()
            graph_json: dict[str, Any] = snapshot.graph_json

            # FROZEN node-type map — captured ONCE per run at run start (§1.6)
            # and passed into finalize_cost at every pause and resume.
            node_type_map = derive_node_type_map(graph_json)

            # Pre-run validation — blocks execution on errors.
            validation = await GraphValidator().validate_for_run(snapshot, input_payload, session)
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
                GraphValidator.check_retry_policy(_retry_policy_value, retry_policy_check)
                if not retry_policy_check.is_valid:
                    raise GraphValidationError(retry_policy_check.issues, run_id)
                # FAR-525: the OPTIONAL backoff_schedule is validated SEPARATELY
                # and is NON-BLOCKING at run start — the runtime resolver
                # fail-opens to the hardcoded default schedule, so a faulting
                # schedule must warn loudly but never brick the run (direct DB
                # writes and future bounds tightening must not strand runs).
                schedule_check = ValidationResult()
                GraphValidator.check_retry_policy_schedule(_retry_policy_value, schedule_check)
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
        guard = RunawayGuard(
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
        broker = get_registry().get_or_create(run_id)
        set_cancellation_check(self._check_db_cancellation(org_id, run_id))
        set_audit_hook(self._dispatch_context_write_audit(org_id, run_id))
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
                await _teardown_hub(model_backend_hub)
            set_model_backend_hub(None)
            set_connector_hub(None)
            set_cancellation_check(None)
            set_audit_hook(None)
            get_registry().close(run_id)
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
        if self._checkpointer_conn_string and _graph_is_interactive(config.graph_json):
            from farna.settings import get_settings

            _settings = get_settings()
            async with _checkpointer_scope(
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
        return get_or_compile(
            scope.pipeline_id,
            scope.snapshot_id,
            lambda: build_graph_from_json(
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
            graph_struct_hash=struct_hash_with_eval_defs(
                compute_retry_aware_topology_hash(config.graph_json, config.pipeline_retry_policy),
                config.eval_defs_by_node,
            ),
        )

    async def _build_initial_stream_state(self, *, scope: _RunScope, config: _StreamConfig) -> dict[str, Any]:
        """Seed the LangGraph state, pinning the earned autonomy level at start."""
        initial_state = _seed_state(config.snapshot, config.input_payload, config.variant_config_snapshot)
        earned_pin = await self._resolve_earned_at_start(scope.pipeline_id, scope.org_id, config.snapshot)
        if earned_pin is not None:
            initial_state["run_context"][PIPELINE_EARNED_AT_START_KEY] = earned_pin
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
        self._node_timeouts.update(_resolved_node_timeouts(config.graph_json, config.pipeline_node_timeout_seconds))
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
        set_conformance_ctx(
            self._session_factory,
            scope.org_id,
            config.snapshot.environment_profile_id,
            scope.pipeline_id,
            claimed,
            claims_load_failed,
        )

        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, scope.org_id)
            await set_rls_execution_context(session)
            await session.execute(
                text("UPDATE runs SET node_attempt_count = node_attempt_count + 1 WHERE id = :rid"),
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
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            current_run = await get_run(session, run_id)
            if current_run is not None:
                execution_attempt_count = int(current_run.execution_attempt_count or 0)
                current_token = current_run.claim_token
                # FAR-583 read-switch: the markers reassemble from
                # run_node_outputs (with the empty/mismatch legacy fallback)
                # inside this SAME transaction — one batched repo read, no
                # per-node lazy loads. The run row here is a FRESH load in
                # this transaction, so no in-session unsaved state is relied
                # on.
                run_markers = await read_run_markers(session, run_id=run_id, organisation_id=org_id)
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
            delivered = _should_skip_retry(node_id, run_markers, str(run_id))
            # read_before_write_suppression is typed ``str`` for both refs and
            # fail-opens (returns False) on a None/empty run_ref or node_ref —
            # guard here so a None idempotency_key (no persisted key) never
            # even attempts the derivation, and to satisfy mypy's arg-types.
            replay_suppressed = False
            if idempotency_key and node_id:
                replay_suppressed = read_before_write_suppression(
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
        if _graph_has_script_mode(graph_json):
            try:
                script_lease_ok = await _script_lease_probe_ok(self._session_factory, str(run_id), org_id, self._claim_token)
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
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                # Transaction-scoped bound BEFORE this transaction's first lock
                # (FAR-1610 recipe; the UPDATE below is that first lock).
                await set_mutation_row_lock_timeout(session)
                result = await session.execute(
                    text("UPDATE runs SET status='pending', error_code=NULL, error_detail=NULL WHERE id=:rid AND claim_token=:tok AND status='running' AND cancellation_requested = false"),
                    {"rid": str(run_id), "tok": self._claim_token},
                )
                return int(getattr(result, "rowcount", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if sqlstate_of(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
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
            terminal_reason=ATTEMPT_TERMINAL_REASON_RESET,
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
        set_cancellation_check(None)
        set_audit_hook(None)
        set_model_backend_hub(None)
        set_connector_hub(None)
        if model_backend_hub is not None:
            await _teardown_hub(model_backend_hub)
        if connector_hub is not None:
            await _teardown_hub(connector_hub)
        get_registry().close(run_id)

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
            return _ERROR_CODE_SCRIPT_SIDE_EFFECT_UNKNOWN, _sanitize_detail(
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
            return non_retryable_code, _non_retryable_failure_detail(non_retryable_code, exc, execution_attempt_count, retries)
        if failure_class_code is not None and attempt_cap is not None and execution_attempt_count < retries and _attempt_cap_reached(attempt_cap, execution_attempt_count):
            # FAR-2562: the class has spent its registry execution cap while the
            # transport budget still had room. Report the class's own code (the
            # one the run would carry anyway) and an accurate reason — the
            # generic branch below would claim "retries exhausted", which is
            # false here and would hide why the run stopped early. When the
            # budget is ALSO spent the generic wording is already accurate, so
            # that case keeps it unchanged.
            return failure_class_code, _attempt_capped_failure_detail(failure_class_code, exc, execution_attempt_count, attempt_cap)
        error_code = "node_cancelled"
        if not graph_idempotent and execution_attempt_count < retries:
            # FAR-295: the run was NOT retried because a node in the
            # graph is non-idempotent (idempotent=false) — a retry
            # would re-execute a side-effecting step. Distinguish
            # this from the retries-exhausted message so the run
            # detail readout explains WHY it terminal-failed on the
            # first attempt.
            error_detail = _sanitize_detail(
                "Sandbox node failed (transient); retry suppressed because a node in the graph is non-idempotent (idempotent=false) and re-running could double-execute a side effect: " + str(exc),
                limit=5000,
            )
        elif isinstance(exc, NodeCancelledError):
            error_detail = _sanitize_detail("Sandbox node cancelled (transient) after retries exhausted: " + str(exc), limit=5000)
        else:
            # SandboxNodeFailedError: the FAR-197 no-output diagnostic
            # is a fully bounded message designed to survive this
            # surface in full — keep the limit at the sanitizer/column
            # cap (5000), not the 500 used for the short
            # NodeCancelledError string, or the kill-reason log tail
            # would be the first thing truncated (FAR-197 review).
            error_detail = _sanitize_detail("Sandbox node failed (transient) after retries exhausted: " + str(exc), limit=5000)
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
                error_code = sandbox_failure_error_code(exc)
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
            _provider_code = classify_provider_error(str(exc))
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
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            current_run = await get_run(session, run_id)
            if current_run is not None:
                execution_attempt_count = int(current_run.execution_attempt_count or 0)
                current_claim_token = current_run.claim_token
                attempt_started_at = _attempt_started_at(current_run)
        return execution_attempt_count, current_claim_token, attempt_started_at

    async def _probe_script_lease(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """FAR-296 Phase 2 stale-claim lease probe before ANY requeue of script mode.

        Proves no script process could still be alive. A probe evaluation
        error fails CLOSED (returns False, logged) — an unknown side-effect
        state must never be re-dispatched.
        """
        try:
            ok = await _script_lease_probe_ok(self._session_factory, str(run_id), org_id, self._claim_token)
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
            if final_status == "complete" and error_code != _ERROR_CODE_HARNESS_IDEMPOTENCY_GATE:
                # Fire agent_signal triggers for each completed node.
                async with self._session_factory() as session, session.begin():
                    await set_rls_org(session, org_id)
                    await set_rls_execution_context(session)
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
            set_cancellation_check(None)
            set_audit_hook(None)
            set_model_backend_hub(None)
            set_connector_hub(None)
            if model_backend_hub is not None:
                await _teardown_hub(model_backend_hub)
            if connector_hub is not None:
                await _teardown_hub(connector_hub)
            # FAR-1534: a clean ``complete`` keeps the broker open — the
            # finalize step may still downgrade it and then publishes
            # ``run_completed`` (final status) and closes the broker.
            if final_status != "awaiting_human" and not (final_status == "complete" and tail_ok):
                get_registry().close(run_id)
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
            if _node_output_has_idempotency_gate(node_output):
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
            signal_results = await fire_agent_signal(
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
        mgr = HITLManager()
        pipeline_name: str | None = None
        coalesce_reused = False
        async with self._session_factory() as session, session.begin():
            await set_rls_org(session, org_id)
            await set_rls_execution_context(session)
            outcome = await evaluate_gate_coalescing(
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
            gate_config = {**gate_config, _GATED_EVAL_MODES_KEY: frozen_gated_eval_modes}

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
        gate_payload = _interrupt_gate_payload(interrupts)
        review_id = gate_payload.get("review_id", "")
        required_team_id = _interrupt_required_team_id(gate_payload)
        condition_result = _interrupt_condition_result(gate_payload)
        subject = _interrupt_subject(gate_payload)
        subject_parent = _interrupt_subject_parent(gate_payload)
        subject_leaf_key = _interrupt_subject_leaf_key(gate_payload)
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
                return _terminal_failure(broker, "failed", "executor_superseded", detail, node_token_usage)
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
                event_type=EVENT_HITL_AWAITING,
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
        if not should_notify_terminal_failure(error_code):
            return
        canonical_code = map_legacy_code(error_code)
        if canonical_code in NON_RETRYABLE_PROVIDER_CODES:
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
                event_type=EVENT_RUN_FAILED,
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
        event_type = _TERMINAL_STATUS_EVENTS.get(status)
        if event_type is None and status == "eval_failed" and error_code == "eval_blocked":
            event_type = EVENT_EVAL_BLOCKED
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
                await set_rls_org(session, org_id)
                await set_rls_execution_context(session)
                return await _safe_pipeline_name(session, pipeline_id, org_id, run_id)
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
            if _is_terminating_rejection_output(name, (lg_event.get("data") or {}).get("output")):
                state.terminating_rejected_gates.add(name)
            return
        state.segments_completed += 1
        if ctx.guard is not None:
            ctx.guard.record_step()
        output = _record_chain_end_output(
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
        stall_reason, agent_failure, session_lost = _record_node_markers(
            output,
            ctx.broker,
            name,
        )
        # FAR-1800: capture the session-lost envelope's RETAINED text from the
        # RAW output (same object the markers were read from, BEFORE any FAR-209
        # interception rewrites it) — the wrapper's ``no_output_reason`` is the
        # only place the killing provider error (e.g. HTTP 402) survives.
        session_lost_text = _node_output_sandbox_session_lost_text(output) if session_lost else None
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
        _record_connector_failure(state, ctx, name, output)
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
        interrupts = _streamed_interrupts(lg_event)
        if interrupts:
            return await self._handle_graph_interrupt(interrupts, state, ctx)
        mapped = _map_lg_event(lg_event, ctx.node_ids)
        if mapped is not None:
            completion = self._publish_mapped_stream_event(mapped, state, ctx)
            if completion is not None:
                await completion
        event_kind = lg_event.get("event", "")
        # Capture node output for agent_signal trigger firing.
        if event_kind == "on_chain_end":
            await self._handle_chain_end_event(state=state, ctx=ctx, lg_event=lg_event)
        if event_kind == "on_chat_model_end":
            _accumulate_chat_model_tokens(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
        elif event_kind == "on_llm_end":
            _accumulate_llm_tokens(lg_event, state.node_token_usage, ctx.guard, ctx.node_token_budgets)
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
            return _terminal_failure(
                broker,
                "eval_failed",
                "eval_blocked",
                _sanitize_detail(exc, limit=None),
                node_token_usage,
            )
        if isinstance(exc, OutputRejectedError):
            # C4: ``output_rejected`` violates the ``ck_runs_status`` CHECK
            # constraint as a STATUS — it is an error CODE on a ``failed`` run.
            return _terminal_failure(
                broker,
                "failed",
                "output_rejected",
                _sanitize_detail(exc, limit=None),
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
            return _terminal_failure(
                broker,
                "failed",
                envelope.get("error_code", _CODE_CONNECTOR_FAILED),
                _sanitize_detail(str(exc.cause), limit=None),
                node_token_usage,
            )
        if isinstance(exc, CompensationFailedError):
            # FAR-402 P5 (§E): a watched node's compensation target itself failed.
            # The node wrapper wrapped the watched-node terminal failure into a
            # forward compensation execution, which then failed — terminalize the
            # run as COMPENSATION_FAILED (never retried; the run already left the
            # batch pipeline's normal failure path via the compensation edge).
            scrubbed = _sanitize_detail(str(exc), limit=None)
            _log.warning(
                "pipeline.compensation_failed",
                extra={
                    "run_id": str(run_id),
                    "node_id": getattr(exc, "node_id", None),
                    "compensation_target": getattr(exc, "compensation_target", None),
                },
            )
            return _terminal_failure(
                broker,
                "compensation_failed",
                COMPENSATION_FAILED_CODE,
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
            error_detail = _sanitize_detail(exc, limit=None)
            _log.warning(
                "runaway.terminated",
                extra={
                    "run_id": str(run_id),
                    "guard": exc.guard,
                    "current": exc.current,
                    "limit": exc.limit,
                },
            )
            return _terminal_failure(broker, "failed", "runaway", error_detail, node_token_usage)
        if isinstance(exc, TimeoutError):
            error_detail = _sanitize_detail(exc, limit=None)
            _log.warning(
                _ERROR_CODE_NODE_TIMEOUT,
                extra={"run_id": str(run_id), "detail": error_detail},
            )
            return _terminal_failure(broker, "failed", "node_timeout", error_detail, node_token_usage)
        if isinstance(exc, SupersededNodeError):
            # A6: the sandbox dispatch marker was denied — a superseded claim
            # or a run no longer running. Terminal ``superseded`` failure; the
            # token-guarded finalize write is a no-op if a successor already
            # owns the run. NEVER a completed run with zero work.
            scrubbed = _sanitize_detail(exc, limit=None)
            _log.warning(
                "pipeline.node_superseded",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_failure(
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
            scrubbed = _sanitize_detail(exc, limit=None)
            _log.warning(
                "pipeline.output_schema_validation_failed",
                extra={"run_id": str(run_id), "detail": scrubbed[:500]},
            )
            return _terminal_failure(
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
            error_detail = _sanitize_detail(exc, limit=None)
            _log.info("pipeline.router_no_match", extra={"run_id": str(run_id), "detail": error_detail})
            return _terminal_failure(
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
        _tb = _traceback_detail(exc, limit=5000)
        return _terminal_failure(
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
        run_trace_id, run_root_span = _compute_otel_run_context(
            config,
            self._otel_bridge,
        )
        ctx.run_trace_id = run_trace_id
        run_root_token = context_api.attach(set_span_in_context(run_root_span)) if run_root_span is not None else None
        try:
            async for lg_event in compiled.astream_events(initial_state, lg_config, version="v2"):
                if ctx.guard is not None:
                    ctx.guard.check_duration()
                outcome = await self._handle_stream_event(state, ctx, lg_event)
                if outcome is not None:
                    return outcome

            self._terminating_rejected_gates = set(state.terminating_rejected_gates)
            terminal = _stream_terminal_reason(state, broker, run_id)
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
                context_api.detach(run_root_token)
            self._otel_bridge.end_run_root()


# ---------------------------------------------------------------------------
# End of the PipelineExecutor class body.
#
# ``_FinalizeMixin`` / ``_TelemetryMixin`` are private aliases for the two mixin
# bases (steps 2-3 of GAPS-harden-w2-executor.md section 5). They are composition
# detail, not importable surface: dropping the bindings here keeps ``dir()`` on
# this module exactly what it was before the split. Everything the rest of the
# codebase imports or ``mock.patch``es still resolves, because the methods live
# on the class through the MRO and the moved bodies resolve their free names via
# ``executor_parts.support._ref``.
# ---------------------------------------------------------------------------
del _FinalizeMixin, _TelemetryMixin
