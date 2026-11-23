"""Scheduling / admission group of :class:`PipelineExecutor`.

Step 4b of the split described in ``GAPS-harden-w2-executor.md`` §5: the
capacity, claim, execution-spend, retry-ceiling and spend/policy pre-gates move
out of ``executor.py`` as a mixin.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``select`` and the ORM classes are imported
directly: they are shared objects, so a patch on ``executor.<name>`` either
mutates the same object or would have no meaning.

Host contract
-------------
``_defer_after_admission_lock_timeout`` calls ``_fenced_pending_reset``, which
belongs to the not-yet-split execute group. It is declared on
:class:`_ExecutorHost` so the type checker sees it; the declaration is
TYPE_CHECKING-only, so at runtime the mixin's base is
``_ExecutorHost(object)`` and the real implementation is found on
``PipelineExecutor`` through the MRO.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.db.models.organisation import Organisation
from farna.db.models.pipeline import Pipeline
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.models.run import Run

from .support import (
    AdmissionLockTimeoutError,
    AdmissionResetState,
    RetryBudgetGateRefusedError,
    RunNotFoundError,
    _CapacitySnapshot,
    _log,
    _ref,
)

if TYPE_CHECKING:

    class _ExecutorHost:
        """Collaborators this mixin calls that the host class provides.

        TYPE-CHECKING ONLY. At runtime the mixin's base is a placeholder class,
        and the real implementations are found on ``PipelineExecutor`` through
        the MRO. The signature must match the host's exactly — a drift is a
        mypy error.
        """

        async def _fenced_pending_reset(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> int | None: ...

else:

    class _ExecutorHost:
        """Runtime placeholder for the TYPE_CHECKING-only contract above."""

        __slots__ = ()


class AdmissionMixin(_ExecutorHost):
    """Capacity / claim / retry-ceiling / spend-and-policy pre-gate methods."""

    # Assigned on the host in ``PipelineExecutor.__init__``.
    _session_factory: async_sessionmaker[AsyncSession]
    _claim_token: str | None

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
            if _ref("sqlstate_of")(exc) != _ref("LOCK_NOT_AVAILABLE_SQLSTATE"):
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
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            await _ref("set_mutation_row_lock_timeout")(session)
            run: Run | None = await _ref("get_run")(session, run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            if run.cancellation_requested:
                await _ref("update_run_status")(session, run_id, "cancelled")
                cancelled_run: Run | None = await _ref("get_run")(session, run_id)
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
            if not _ref("_all_capacities_ok")(capacity.pipeline_ok, capacity.sandbox_ok, capacity.run_ok):
                return await self._demote_for_capacity(
                    session,
                    run_id=run_id,
                    max_concurrent=max_concurrent,
                    capacity=capacity,
                    org_sandbox_cap=org_sandbox_cap,
                    org_run_limit=org_run_limit,
                )

            if run.status not in _ref("_ADMISSIBLE_STATUSES"):
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
            active_count = await _ref("count_active_runs_for_pipeline")(session, pipeline_id, include_pending=False, exclude_run_id=run_id)
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
        ceiling = _ref("retry_max_executions")(retry_policy)
        consumed = _ref("retry_executions_consumed")(execution_attempt_count=getattr(run, "execution_attempt_count", 0))
        if ceiling is None or not _ref("retry_budget_exhausted")(retry_policy, execution_attempt_count=consumed):
            return None
        terminal_run: Run | None = await _ref("update_run_status")(
            session,
            run_id,
            "failed",
            error_code=_ref("_CODE_HARNESS_RETRY_BUDGET_EXHAUSTED"),
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
        await _ref("update_run_status")(
            session,
            run_id,
            "pending",
            error_code=decline_code,
            error_detail=decline_detail,
            claim_token=self._claim_token,
            from_status="running",
        )
        pending_run: Run | None = await _ref("get_run")(session, run_id)
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
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                # Bound the hot ``runs`` row-lock wait BEFORE the
                # transaction's first lock (the terminalizing
                # ``update_run_status`` below). Issued in the caller's
                # transaction; the shared ``update_run_status`` stays unbounded
                # -. A 55P03 lands in this method's existing fail-open
                # ``except Exception`` (WARNING + return None) — the ledger block
                # remains the authoritative hard ceiling.
                await _ref("set_mutation_row_lock_timeout")(session)
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
                state = await _ref("get_org_cost_state")(session, org_id)
                # Pass a minimal 1-cent charge as the "next run" so the gate
                # honours the documented at-ceiling / kill-switch semantics: with
                # zero remaining budget (cumulative >= ceiling, or ceiling == 0)
                # the run must be terminalized BEFORE any billable work, not left
                # to the finalize ledger block. A ceiling of 0 therefore blocks
                # every new run, and an org exactly at its ceiling (1 cent would
                # exceed it) is halted. A genuinely non-empty remaining budget
                # (>= 1 cent) still passes so the run can execute.
                decision = _ref("evaluate_org_spend_ceiling")(
                    org_cumulative_spend_cents=(state.org_cumulative_spend_cents or 0) if state is not None else 0,
                    additional_cents=1,
                    spend_ceiling_cents=state.spend_ceiling_cents if state is not None else None,
                )
                if decision.allowed:
                    return None
                await _ref("update_run_status")(
                    session,
                    run_id,
                    "cost_ceiling_exceeded",
                    error_code=_ref("ORG_CEILING_EXCEEDED"),
                    error_detail=decision.message,
                    claim_token=claim_token,
                )
                halted_run: Run | None = await _ref("get_run")(session, run_id)
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
        blocked, block_message = _ref("_verify_policy_gate_pin_fingerprint")(
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
        await _ref("update_run_status")(
            session,
            run_id,
            "eval_failed",
            error_code="eval_blocked",
            error_detail=block_message,
            claim_token=claim_token,
        )
        halted_run: Run | None = await _ref("get_run")(session, run_id)
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
        await _ref("update_run_status")(
            session,
            run_id,
            "running",
            claimed_by=_ref("_WORKER_ID"),
            clear_error_code=True,
        )
        running_run: Run | None = await _ref("get_run")(session, run_id)
        if running_run is None:
            raise RunNotFoundError(run_id)
        try:
            actor_user_id, actor_label = _ref("resolve_run_actor")(
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
                **_ref("run_provenance_fields")(running_run),
                "summary": _ref("compose_run_started_summary")(
                    await _ref("_safe_pipeline_name")(session, pipeline_id, org_id, run_id),
                    pipeline_id,
                    running_run.trigger_type,
                ),
            }
            if actor_label is not None:
                payload["actor"] = actor_label
            await _ref("append_audit_event")(
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
            active: int = await _ref("count_active_sandbox_runs_for_org")(session, org_id, exclude_run_id=run_id)
            return active
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
            active: int = await _ref("count_active_runs_for_org")(session, org_id, include_pending=False, exclude_run_id=run_id)
            return active
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
            has_sandbox = _ref("_sandbox_agent_for_snapshot")(snapshot_id, graph_json) if snapshot_id is not None else _ref("_graph_contains_sandbox_agent")(graph_json)
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
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                if flag_on:
                    from farna.core.runner_capacity import read_runner_cap_contract

                    cap, host_resource_only = await read_runner_cap_contract(session, org_id)
                    return cap, host_resource_only, "marker"
                # The one reader returns the full contract; this
                # claim-time cap read enforces the flag-off window cap only
                # (absent key = no gate until D8's rollout flag).
                return (await _ref("get_sandbox_concurrency_limit")(session, org_id)).enforced_cap, False, "graph"
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
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                limit: int | None = await _ref("get_org_run_concurrency_limit")(session, org_id)
                return limit
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
                _ref("ERROR_CODE_ORG_CAPACITY_LIMITED"),
                f"Org sandbox concurrency limit reached: {org_count} active, cap {org_sandbox_cap}",
            )
        if not org_run_capacity_ok and org_run_limit is not None:
            return (
                _ref("ERROR_CODE_ORG_CAPACITY_LIMITED"),
                f"Org run concurrency limit reached: {org_run_count} active, cap {org_run_limit}",
            )
        return (
            _ref("ERROR_CODE_PIPELINE_CAPACITY"),
            f"Pipeline max_concurrent_runs reached: {active_count} active, limit {max_concurrent}",
        )
