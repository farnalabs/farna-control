"""Shared primitives for the pipeline executor (extracted from ``executor``).

Step 1 of the package split described in ``GAPS-harden-w2-executor.md`` §5: the
module-level pure helpers and value objects leave ``executor.py`` but stay
importable from it (``executor.py`` re-exports every name). Nothing here holds
per-instance state.

Patch safety
------------
Tests target ``mock.patch("farna.core.pipeline_engine.executor.<name>")``. A
helper that moved out of that module must NOT resolve such a name from its own
globals or the patch silently stops intercepting. Two seams enforce that:

* :func:`_ref` resolves a name from the ORIGINAL module's namespace at CALL
  time, so a patch applied to ``executor.<name>`` is always observed.
* :data:`_log` is a forwarding logger handle rather than a bound logger, so
  ``patch("...executor._log")`` (whole-attribute) is observed too. The real
  logger stays ``logging.getLogger("farna.core.pipeline_engine.executor")``, so
  the emitted records are unchanged.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession

from farna.core.pipeline_engine.error_codes import sanitize_error_text
from farna.core.pipeline_engine.eval_persist_order import EvalDefDTO
from farna.core.pipeline_engine.event_broker import RunEventBroker
from farna.core.pipeline_engine.runaway_protection import RunawayGuard
from farna.db.crud.run_node_outputs import DualWriteError
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.sqlstates import is_lock_abort

_ORIGINAL_MODULE: Any = None


def _ref(name: str) -> Any:
    """Resolve ``name`` from ``farna.core.pipeline_engine.executor`` at call time.

    ``executor`` re-exports every helper defined here, and it is also where the
    tests' ``mock.patch`` targets land. Resolving through it (instead of
    through this module's own globals) is what keeps those patches
    intercepting; see the module docstring.
    """
    global _ORIGINAL_MODULE
    if _ORIGINAL_MODULE is None:
        from farna.core.pipeline_engine import executor as original_module

        _ORIGINAL_MODULE = original_module
    return getattr(_ORIGINAL_MODULE, name)


class _ForwardedLog:
    """Logger handle that resolves ``executor._log`` on every attribute use.

    ``patch("...executor._log")`` replaces the module attribute wholesale; a
    helper holding its own ``Logger`` reference would not see that. Forwarding
    keeps both the whole-object patch and the per-method patches
    (``...executor._log.warning``) intercepting, while every record still goes
    to the same ``farna.core.pipeline_engine.executor`` logger.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(_ref("_log"), name)


_log = _ForwardedLog()


def _is_finalize_lock_retryable(exc: BaseException) -> bool:
    """True for a whole-transaction finalisation lock failure worth re-running.

    Covers a deadlock (40P01) and the bounded lock_timeout expiry (55P03) —
    both abort the transaction, so the only recovery is re-running it from its
    ownership layer. ``is_lock_abort`` recognises both SQLSTATEs across the raw
    asyncpg ``LockNotAvailableError`` and SQLAlchemy ``OperationalError`` shapes.

    EXCLUDES :class:`~farna.db.crud.run_node_outputs.DualWriteError`: it is a
    deliberate FAIL-CLOSED abort that ``finalize_cost`` raises only AFTER the
    run has been terminalised ``dual_write_failed``. Its ``sqlstate`` reflects
    the underlying failure (often 40P01), so a bare SQLSTATE test would retry
    and overwrite that truthful status — the predicate must never do so.
    """
    if isinstance(exc, DualWriteError):
        return False
    return is_lock_abort(exc)


def _sanitize_detail(detail: Any, limit: int | None = 5000) -> str:
    """Sanitize an error detail at a write surface.

    Redaction runs before any truncation so a secret straddling the cut point is
    still removed. Pass ``limit=None`` to skip the cap — ``runs.error_detail`` was
    widened to ``Text`` by migration, so error-detail writes no longer need
    to be bounded. Never raises — :func:`sanitize_error_text` coerces any input
    via ``str()`` and is a NO-OP for clean strings.
    """
    return sanitize_error_text(detail, limit)


async def _safe_pipeline_name(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> str | None:
    """Best-effort pipeline name lookup for audit labelling (never raises).

    A failed or missing lookup degrades the ``run_started`` summary to the
    nameless form instead of suppressing the audit event — the event's write
    must not depend on the label lookup succeeding.
    """
    try:
        pipeline: Pipeline | None = await _ref("get_pipeline")(session, pipeline_id, organisation_id=org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "pipeline.run_started_audit_name_lookup_failed",
            extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id)},
            exc_info=True,
        )
        return None
    return pipeline.name if pipeline is not None else None


# Source-location shapes that must never reach a stored, API-exposed
# ``runs.error_detail``. The detail is rendered on the Runs list and served by
# the API, so a raw traceback (module paths, source file names) is a
# data-hygiene leak. The FULL traceback is retained in the server log by each
# caller's ``exc_info`` record; this only scrubs what is STORED on the row.
#
# The separator and the inner character class are MUTUALLY EXCLUSIVE: the inner
# class excludes ``/`` and ``\`` so ``(?:[/\\][^/\\]+)+`` has exactly one way to
# split a path. Leaving the separator inside the class yields
# ``(?:[/\\][^...]+)+`` — the classic ``(a+)+`` shape that backtracks
# catastrophically on a long run (a 660-char ``'/aaaaaaaaaa'`` repeat measured
# >20s), which would hang the executor's ``except`` handler on every failed run.
_SOURCE_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[/\\][^\s\"':;,()\[\]<>/\\]+)+\.py[cod]?\b")
# One dotted segment (e.g. ``farna.db`` / ``modulo.core``) is already an
# internal module reference and must be scrubbed too.
_INTERNAL_MODULE_RE = re.compile(r"\b(?:farna|modulo)(?:\.\w+){1,}\b")
_TRACEBACK_HEADER = "Traceback (most recent call last)"

# Upper bound on the text handed to the scrub patterns above. They run on every
# failed run inside the executor's ``except`` handler, so the untrusted message
# is capped BEFORE the pattern engine sees it — the same ReDoS defense
# ``sanitize_error_text`` uses. Without a cap a long URL / base64 / path run in
# an exception message hands the regex an unbounded haystack.
_MAX_PUBLIC_MESSAGE_CHARS = 5000


def _public_exception_message(exc: BaseException) -> str:
    """Return *exc*'s message with internal source locations removed.

    Keeps the exception's own wording (the readable part a user needs) and
    scrubs anything that would leak the codebase layout: an embedded formatted
    traceback, ``File "...", line N`` frame lines, source file paths and
    internal ``farna.*`` / ``modulo.*`` module paths. Never raises.
    """
    try:
        raw = str(exc).strip()
    except Exception:  # pragma: no cover - str() must not break a failure path
        return ""
    if not raw:
        return ""
    # Some exceptions embed a fully formatted traceback in their message. Keep
    # only the final exception line so no frame line can survive.
    if _TRACEBACK_HEADER in raw:
        raw = raw.rsplit("\n", 1)[-1].strip()
    # Drop any residual ``File "<path>", line N, in <func>`` frame lines.
    lines = [line for line in raw.splitlines() if not line.strip().startswith('File "')]
    raw = " ".join(line.strip() for line in lines if line.strip())
    # Bound BEFORE the scrub regexes (ReDoS defense): the message is untrusted
    # and may be arbitrarily long.
    raw = raw[:_MAX_PUBLIC_MESSAGE_CHARS]
    raw = _SOURCE_PATH_RE.sub("<internal path>", raw)
    raw = _INTERNAL_MODULE_RE.sub("<internal module>", raw)
    return raw.strip()


def _failure_reason(exc: BaseException, limit: int = 500) -> str:
    """Short, user-facing failure reason for a caught executor exception.

    ``runs.error_detail`` is API-exposed, so it must never carry raw exception
    text with a traceback or internal paths. This returns
    ``"<ExceptionType>: <scrubbed message>"`` (or just the type name when the
    message is empty), sanitized and bounded.

    The full traceback is NOT returned — every call site records it to the
    server log via ``exc_info`` before this reason is stored on the run row.
    """
    reason = type(exc).__name__
    message = _public_exception_message(exc)
    if message and not message.startswith(f"{reason}:"):
        reason = f"{reason}: {message}"
    elif message:
        reason = message
    return _sanitize_detail(reason, limit=limit)


class RunNotFoundError(KeyError):
    def __init__(self, run_id: uuid.UUID) -> None:
        super().__init__(str(run_id))
        self.run_id = run_id


class SandboxCapacityExceededError(Exception):
    """Raised when the org sandbox concurrency cap blocks a resume."""

    def __init__(self, org_id: uuid.UUID) -> None:
        self.org_id = org_id
        super().__init__(f"Sandbox concurrency limit reached for org {org_id}; gate left undecided. Retry when capacity frees up.")


# The outcome of the fenced pending-reset that follows a two-timeout admission
# deferral. Carried as the OUTCOME, never a bool: ``fence_lost`` (rowcount 0)
# and ``timeout`` (``None``) were previously collapsed to ``False``, so the
# deferral log claimed the reset had timed out on a fence loss too — a claim it
# could not know.
#
# ``unknown`` covers the case the return value cannot describe: the reset
# RAISED, so whether it landed is not knowable here. The wording names BOTH
# possible resting states (``pending``, recovered by ``dispatcher_reconcile``;
# ``running``, left for the nodeless/age backstop) and claims neither. There is
# deliberately no re-read: opening a session just to look at the row is more
# surface than the ambiguity warrants, and every attempt to add one introduced
# its own defect.
AdmissionResetState = Literal[
    "confirmed",
    "fence_lost",
    "timeout",
    "unknown",
]


class AdmissionLockTimeoutError(Exception):
    """The claim-time admission transaction kept hitting a row lock.

    Raised after the bounded retry in :meth:`PipelineExecutor._check_capacity`
    is exhausted (two consecutive SQLSTATE 55P03 waits). The admission+bump
    transaction rolled back BOTH times, so the run consumed **zero** executions
    and nothing was uncounted. A transient lock wait must never terminal-fail a
    run that never executed.

    The run's resting state depends on the fenced pending-reset that follows
    (``_fenced_pending_reset`` returns ``int | None``), carried as
    ``reset_state`` — the OUTCOME, not a boolean, because a fence loss and a
    timeout are different facts:

    * ``"confirmed"`` — the reset landed; the run is ``pending`` and recovered
      by ``dispatcher_reconcile``'s re-enqueue leg;
    * ``"fence_lost"`` — the reset matched no row: a successor owns the run or
      it has already left ``running``. Its resting state is NOT knowable here,
      so the deferral must not describe it;
    * ``"timeout"`` — the bounded reset itself timed out; the row was NOT reset
      and is most likely still ``running`` under this executor's claim token,
      left for the nodeless/age backstop;
    * ``"unknown"`` — the reset RAISED, so whether it landed could not be
      determined. The run may be ``pending`` (recovered by
      ``dispatcher_reconcile``'s re-enqueue leg) or still ``running`` (left for
      the nodeless/age backstop); the deferral names BOTH and claims neither.

    Every arm maps to the SAME non-terminal outcome
    (``pipeline_execution._handle_executor_failure`` returns
    ``{"status": "deferred"}`` and never terminal-fails the run) — only the
    log wording differs, and no arm asserts a resting state it cannot know.
    Any OTHER executor exception still terminal-fails as before.
    """

    def __init__(self, run_id: uuid.UUID, attempts: int, *, reset_state: AdmissionResetState) -> None:
        self.run_id = run_id
        self.attempts = attempts
        self.reset_state = reset_state
        resting_state = {
            "confirmed": "the fenced pending-reset was CONFIRMED, so the run was left pending for dispatcher_reconcile",
            "fence_lost": ("the fenced pending-reset matched no row (fence lost), so the run is owned by a successor or has already left running and was left untouched"),
            "timeout": ("the fenced pending-reset ALSO timed out, so the run was NOT reset and is left for the nodeless/age backstop"),
            "unknown": ("the fenced pending-reset raised, so whether it landed could not be determined: if it did, the row is pending and dispatcher_reconcile will re-dispatch it; if it did not, the row is still running and falls to the nodeless/age backstop"),
        }[reset_state]
        super().__init__(f"Admission for run {run_id} still hit a row-lock timeout after {attempts} attempts; {resting_state}")


class RetryBudgetGateRefusedError(Exception):
    """The retry-ceiling gate refused admission and its terminal write lost the fence.

    Raised by ``_claim_under_capacity`` when the run is at its policy ceiling,
    the fenced ``failed`` write was rejected, and the re-read row is still
    ``running`` — either under a SUPERSEDED claim token or with a pending
    cancellation. Handing that row back to ``execute()`` would let it stream:
    the caller only refuses to execute on ``status != "running"``, and such a
    row IS running, so the graph would execute past the ceiling the gate
    exists to enforce. Raising keeps the attempt strictly non-executing (no
    graph/``execute_fn`` call follows) and
    ``pipeline_execution._handle_executor_failure`` maps it to the non-terminal
    ``deferred`` outcome: the row is owned by its successor or by the
    cancellation machinery, never by this attempt.
    """

    def __init__(self, run_id: uuid.UUID, *, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"Admission refused for run {run_id}: the retry-budget gate's terminal write lost the fence and the row is still running ({reason}); the graph was not executed")


class GraphValidationError(ValueError):
    """Raised when pre-run graph validation fails with blocking errors."""

    def __init__(
        self,
        issues: list[Any],
        run_id: uuid.UUID,
    ) -> None:
        messages = [f"[{i.code}] {i.message}" for i in issues]
        super().__init__(f"Graph validation failed for run {run_id}: {'; '.join(messages)}")
        self.run_id = run_id
        self.issues = issues


@dataclass
class _StreamState:
    """Mutable per-run stream accumulators for ``_stream_graph``.

    Groups the state the ``astream_events`` loop reads and mutates so the
    per-event helper takes a single bundle instead of many loose parameters
    (CodeScene: excess function arguments / complex method). A pure refactor —
    no behaviour, ordering, or state-transition change.
    """

    node_token_usage: dict[str, dict[str, int]] = field(default_factory=dict)
    segments_completed: int = 0
    first_node_signalled: bool = False
    # Set when a captured sandbox-agent node output carries stall_reason — a
    # stalled node RETURNS a failed output dict instead of raising, so the run
    # must be recorded as 'stalled', not 'complete'.
    stall_reason: str | None = None
    # Set when a captured sandbox-agent node output self-reported failure
    # (agent_status=failed OR outcome=failed) — A1 elevation (agent-failure UX,
    # phase 1): such a run must NEVER land 'complete'.
    agent_failure_reason: str | None = None
    connector_failure_reason: str | None = None
    connector_failure_code: str | None = None
    # set when a captured sandbox-agent node output carries the
    # sandbox-session-lost marker (the E2B wrapper's fallback echo — a dead
    # opencode session). Routes to retryable ``sandbox.no_output_json``.
    session_lost_reason: str | None = None
    # The envelope's retained text surfaces (output.json's
    # ``no_output_reason`` + the captured stdout/stderr), which is where the
    # terminal PROVIDER error that killed the session lives. The marker reason
    # above is the wrapper's fixed placeholder, so classification must run
    # against THIS text — a session killed by an HTTP 402 billing refusal must
    # terminalise ``model.insufficient_funds``, not be masked as the retryable
    # ``sandbox.no_output_json``.
    session_lost_retained_text: str | None = None
    # IDs of HITL gates whose REJECTED artifact was stamped
    # ``reject_disposition == "terminate"`` in this stream. The gate's router
    # ENDs the graph on such a rejection, which is a NORMAL stream exit, so
    # ``_finalize_run_after_stream`` needs this (plus the committed claim row)
    # to downgrade the would-be ``complete`` to ``rejected``.
    terminating_rejected_gates: set[str] = field(default_factory=set)


@dataclass
class _StreamContext:
    """Immutable per-run stream context shared by the ``astream_events`` handlers.

    Bundles the fixed per-run values every per-event handler needs so the
    loop body and helpers take a single bundle instead of many loose
    parameters (CodeScene: excess function arguments). A pure refactor — no
    behaviour, ordering, or state-transition change.
    """

    node_ids: set[str]
    guard: RunawayGuard | None
    completed_node_outputs: dict[str, Any] | None
    run_trace_id: str | None
    broker: RunEventBroker
    run_id: uuid.UUID
    pipeline_id: uuid.UUID | None
    org_id: uuid.UUID | None
    node_token_budgets: dict[str, int] | None
    eval_definitions_by_node: dict[str, list[EvalDefDTO]] | None
    node_type_map: dict[str, str] | None


class _CapacitySnapshot(NamedTuple):
    """Counts and admit/reject verdicts from ONE claim-time capacity read."""

    pipeline_ok: bool
    active_count: int
    sandbox_ok: bool
    sandbox_count: int
    run_ok: bool
    run_count: int


@dataclass(frozen=True)
class _RunScope:
    """Immutable per-run identity scalars for ``_prepare_and_stream``.

    Bundles the fixed run-scoped values the stream preparation path needs so
    the method takes a single bundle instead of many loose parameters
    (SonarQube S107: too many parameters). An annotation-only public attribute
    set; a pure refactor — no behaviour, ordering, or state-transition change.
    """

    run_id: uuid.UUID
    org_id: uuid.UUID
    pipeline_id: uuid.UUID
    snapshot_id: uuid.UUID
    thread_id: str


@dataclass(frozen=True)
class _StreamConfig:
    """Immutable inputs shared by the graph stream preparation path."""

    snapshot: PipelineSnapshot
    input_payload: dict[str, Any]
    variant_config_snapshot: dict[str, Any] | None
    graph_json: dict[str, Any]
    eval_defs_by_node: dict[str, list[EvalDefDTO]]
    node_ids: set[str]
    completed_node_outputs: dict[str, Any]
    guard: RunawayGuard
    node_type_map: dict[str, str]
    pipeline_node_timeout_seconds: int
    broker: RunEventBroker
    pipeline_retry_policy: dict[str, Any] | None = None
    idempotency_key: Callable[[str, dict[str, Any]], str | None] | None = None
