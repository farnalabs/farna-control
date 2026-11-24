"""Terminalization / completion group of :class:`PipelineExecutor`.

Step 2 of the split described in ``GAPS-harden-w2-executor.md`` §5: "how does a
run become terminal, and what is written about it" moves out of ``executor.py``
as a mixin. The group owns the FAR-510 masked-failure downgrades, the FAR-1487
HITL terminate-rejection downgrade, the whole-transaction finalisation retry,
and the post-commit alert / notification / evidence / seal tail.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``Decimal`` / ``select`` and the
``HitlClaim`` / ``FinalizeMetricSink`` / ``RunNotFoundError`` objects are
imported directly: they are shared objects, so a patch on ``executor.<name>``
either mutates the same object or would have no meaning.

Host contract
-------------
This mixin also calls methods from the not-yet-split groups
(``_compensate_blocked_run_best_effort``, ``_dispatch_run_failed``,
``_dispatch_terminal_status_notification``). They are declared on
:class:`_ExecutorHost` so the type checker sees them; the declaration is
TYPE_CHECKING-only, so at runtime the mixin is ``FinalizeMixin(TelemetryMixin,
object)`` and the real implementations are found on ``PipelineExecutor``
through the MRO.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.core.cost_controller.finalize import FinalizeMetricSink
from farna.db.models.hitl_claim import HitlClaim
from farna.db.models.run import Run

from .support import RunNotFoundError, _log, _ref
from .telemetry import TelemetryMixin

if TYPE_CHECKING:

    class _ExecutorHost:
        """Collaborators this mixin calls that the host class provides.

        TYPE-CHECKING ONLY. At runtime the mixin's second base is ``object``;
        the real implementations are found on ``PipelineExecutor`` through the
        MRO. The signatures must match the host's exactly — a drift is a mypy
        error.
        """

        async def _compensate_blocked_run_best_effort(
            self,
            *,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
            executed_nodes: dict[str, Any],
        ) -> None: ...

        async def _dispatch_run_failed(
            self,
            *,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
            pipeline_id: uuid.UUID,
            error_code: str | None,
        ) -> None: ...

        async def _dispatch_terminal_status_notification(
            self,
            *,
            org_id: uuid.UUID,
            run_id: uuid.UUID,
            pipeline_id: uuid.UUID,
            final_run: Run,
        ) -> None: ...

else:

    class _ExecutorHost:
        """Runtime placeholder for the TYPE_CHECKING-only contract above."""

        __slots__ = ()


class FinalizeMixin(TelemetryMixin, _ExecutorHost):
    """Terminalization, downgrade and post-commit tail methods."""

    # Declared (assigned on the host) so the type checker can see them.
    _session_factory: async_sessionmaker[AsyncSession]
    _claim_token: str | None
    _terminating_rejected_gates: set[str]
    _notifier: Any | None

    async def _record_eval_blocked_audit(
        self,
        *,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        error_detail: str | None,
    ) -> None:
        """Record an ``eval.blocked`` audit event — failure-isolated.

        Runs in its own session/transaction so a blocked-eval audit failure
        never interrupts terminalization. The payload carries the sanitized
        detail plus the run's pipeline id so the event links back to the graph.
        """
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            # FAR-1141 / ADR-042: the run's execution origin travels with this
            # run-keyed payload so the audit log can tell a dispatched run from
            # one Modulo executed. Best-effort and read INSIDE this audit's own
            # transaction, with the serializer's ``None``-row form as the degrade
            # arm: any read failure yields the key with NULL (logged) and never
            # suppresses the audit event itself.
            provenance: dict[str, Any] = _ref("run_provenance_fields")(None)
            try:
                provenance = _ref("run_provenance_fields")(await _ref("get_run")(session, run_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning(
                    "audit.eval_blocked_origin_unavailable",
                    extra={"run_id": str(run_id)},
                    exc_info=True,
                )
            try:
                await _ref("append_audit_event")(
                    session,
                    org_id=org_id,
                    event_type="eval.blocked",
                    resource_type="run",
                    resource_id=run_id,
                    payload_json={
                        "pipeline_id": str(pipeline_id),
                        **provenance,
                        "error_detail": _ref("_sanitize_detail")(error_detail, limit=None),
                        "actor": _ref("SYSTEM_ACTOR"),
                        "summary": f"Guardrail eval blocked the run (pipeline {_ref('short_id')(pipeline_id)})",
                    },
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("audit.eval_blocked_failed", extra={"run_id": str(run_id)})

    @staticmethod
    def _sandbox_failure_scan_views(value: Any) -> list[dict[str, Any]]:
        """FAR-510 — the dict views of one per-node output value that may carry
        the sandbox envelope fields (``status`` / the synthetic-failure marker).

        NEW FORMAT (split envelope): value has ``output`` (pure return) and
        ``telemetry`` keys. The telemetry view carries the envelope fields.

        LEGACY FORMAT: A full runner envelope ``{artifacts, output}`` yields the
        value itself (flat / legacy mixed bodies), the outer ``output`` telemetry
        view, and the first artifact's inner ``output``.

        A post-P1b stored ``outputs_json`` value is the PURE agent return
        (``None`` or agent-authored business JSON) — scanned top-level only; the
        marker requirement in the predicate keeps agent-authored JSON
        collision-free.
        """
        views: list[dict[str, Any]] = []
        if not isinstance(value, dict):
            return views
        # New split format: telemetry channel
        telemetry = value.get("telemetry")
        if isinstance(telemetry, dict):
            views.append(telemetry)
        # Legacy format: artifacts and output
        views.append(value)
        outer = value.get("output")
        if isinstance(outer, dict):
            views.append(outer)
        artifacts = value.get("artifacts")
        if isinstance(artifacts, list) and artifacts and isinstance(artifacts[0], dict):
            inner = artifacts[0].get("output")
            if isinstance(inner, dict):
                views.append(inner)
        return views

    @staticmethod
    def _is_sandbox_synthetic_failure_view(view: dict[str, Any]) -> bool:
        """FAR-510 — the marker-based downgrade predicate.

        Requires ``status == "failed"`` AND the runner's machine marker
        (``SYNTHETIC_FAILURE_MARKER``) True — never summary text — so an
        agent-authored failure shape (honest exit-code failure, business JSON
        self-reporting failure) is never downgraded.

        Dual-read: a stored row written before the rebrand carries only a
        legacy marker key (``LEGACY_SYNTHETIC_FAILURE_MARKERS``), and a run can
        outlive a rolling deploy, so the predicate treats the new and every
        legacy key as the same terminal/synthetic signal until the drop
        release.
        """
        if view.get("status") != "failed":
            return False
        if view.get(_ref("SYNTHETIC_FAILURE_MARKER")) is True:
            return True
        return any(view.get(marker) is True for marker in _ref("LEGACY_SYNTHETIC_FAILURE_MARKERS"))

    def _downgrade_connector_failures(
        self,
        *,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str],
        stored_node_outputs: dict[str, Any] | None = None,
        stored_node_telemetry: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Downgrade an explicit connector failure envelope before finalization."""
        if final_status != "complete":
            return final_status, error_code, error_detail
        stored_outputs = stored_node_outputs or {}
        stored_telemetry = stored_node_telemetry or {}
        node_ids = set(completed_node_outputs) | set(stored_outputs) | set(stored_telemetry)
        failures = [
            _ref("_connector_failure_for_node")(
                node_id,
                node_type_map,
                completed_node_outputs,
                stored_outputs,
                stored_telemetry,
            )
            for node_id in node_ids
        ]
        observed = [failure for failure in failures if failure is not None]
        if not observed:
            return final_status, error_code, error_detail
        details = sorted({reason for reason, _ in observed})
        chosen_code = _ref("_CODE_CONNECTOR_UNKNOWN") if any(code == _ref("_CODE_CONNECTOR_UNKNOWN") for _, code in observed) else _ref("_CODE_CONNECTOR_FAILED")
        return "failed", chosen_code, "; ".join(details)

    def _downgrade_masked_sandbox_failures(
        self,
        *,
        run_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str],
        stored_node_outputs: dict[str, Any] | None = None,
        stored_node_telemetry: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str | None]:
        """FAR-510 — downgrade a masked sandbox-agent failure to an honest fail.

        The sandbox_agent runner's generic-exception path does NOT raise — it
        RETURNS a failure envelope stamped with the ``SYNTHETIC_FAILURE_MARKER``
        machine marker. (The schema-validation path no longer returns such an
        envelope: FAR-780 made it raise a retryable error instead.) Node
        completion is output-presence-based, so that node counts as completed
        and the run would finalize ``complete`` — a failed dispatch masked as
        success.

        Only a ``complete`` final_status is inspected (failed/eval_failed/etc.
        are already honest). A match is a completed output on a node the
        ``node_type_map`` says is ``sandbox_agent`` for which ANY scanned view
        shows ``status == "failed"`` AND the synthetic-failure marker (see
        :meth:`_is_sandbox_synthetic_failure_view`). Returns the inputs
        unchanged when nothing matches.

        The scan checks the per-node UNION of three views — the live
        ``completed_node_outputs`` envelope, the run's STORED cumulative
        ``outputs_json`` value, and the STORED ``node_telemetry_json`` view
        (via the legacy-safe ``node_output_split.node_telemetry`` accessor,
        which falls back to the legacy mixed-envelope inner output for pre-P1b
        rows). A HITL resume only re-emits the resumed segment's node events,
        so a masked failure from a PRIOR segment is only visible in the stored
        columns — and those hold the POST-split shapes (``outputs_json`` =
        pure return or ``None``; telemetry = the envelope fields), which are
        DIFFERENT shapes from the live envelope by design. No view clobbers
        another: a match in ANY view downgrades.
        """
        if final_status != "complete":
            return final_status, error_code, error_detail
        stored_outputs = stored_node_outputs or {}
        stored_telemetry = stored_node_telemetry or {}
        node_ids = set(completed_node_outputs) | set(stored_outputs) | set(stored_telemetry)
        found: list[str] = []
        for node_id in node_ids:
            if node_type_map.get(node_id) != "sandbox_agent":
                continue
            views = self._sandbox_failure_scan_views(completed_node_outputs.get(node_id))
            views.extend(self._sandbox_failure_scan_views(stored_outputs.get(node_id)))
            views.extend(self._sandbox_failure_scan_views(_ref("node_telemetry")(stored_telemetry, stored_outputs, node_id)))
            if any(self._is_sandbox_synthetic_failure_view(view) for view in views):
                found.append(node_id)
        if not found:
            return final_status, error_code, error_detail
        _log.warning(
            "pipeline.masked_sandbox_failure_downgraded",
            extra={"run_id": str(run_id), "node_ids": sorted(found)},
        )
        return "failed", _ref("_CODE_SANDBOX_AGENT_FAILED"), "Sandbox agent node(s) failed: " + ", ".join(sorted(found))

    async def _load_stored_node_columns(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> tuple[dict[str, Any], dict[str, Any]]:
        """FAR-510 — the run's STORED cumulative per-node columns.

        Returns ``(outputs_json, node_telemetry_json)``. A HITL resume only
        re-emits the resumed segment's node events, so the live
        ``completed_node_outputs`` can miss a masked sandbox failure from a
        prior segment; the stored row is the cumulative truth. Since the P1b
        write-flip the two columns hold DIFFERENT shapes (pure return vs
        telemetry envelope), so BOTH are loaded and the downgrade scans a
        per-node view of each. Best-effort (guard-the-guard): any failure —
        including a missing row or a non-dict column — yields empty dicts so
        the downgrade falls back to scanning the live dict only. It must never
        crash finalization.

        FAR-583: the blobs reassemble from ``run_node_outputs`` via the repo
        reader (with the empty/mismatch legacy fallback) inside the SAME
        transaction that loads the run row — one batched query, never a
        per-node lazy load.
        """
        if self._session_factory is None:
            return {}, {}
        outputs_json: dict[str, Any] = {}
        telemetry_json: dict[str, Any] = {}
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                run = await _ref("get_run")(session, run_id)
                if run is not None:
                    blobs = await _ref("read_run_blobs")(session, run_id=run_id, organisation_id=org_id)
                    outputs_json = dict(blobs.outputs) if isinstance(blobs.outputs, dict) else {}
                    telemetry_json = dict(blobs.telemetry) if isinstance(blobs.telemetry, dict) else {}
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.masked_sandbox_failure_stored_scan_unavailable",
                extra={"run_id": str(run_id)},
                exc_info=True,
            )
            return {}, {}
        return outputs_json, telemetry_json

    async def _downgrade_hitl_terminate_rejection(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
    ) -> tuple[str, str | None, str | None]:
        """Downgrade ``complete`` to ``rejected`` for a HITL terminate-rejection (FAR-1487).

        Keyed on BOTH signals - never the agent's/graph's echo alone:

        1. the gate artifact (``result == "rejected"`` + ``reject_disposition ==
           "terminate"``, for the gate ids latched by the stream), AND
        2. the COMMITTED ``hitl_claims`` row for (this run, that gate) with
           ``decision == "rejected"``.

        A coalesced-supersede system rejection (committed
        ``decision_payload.reason == SUPERSEDE_REASON`` with NO deciding
        account) lands as ``hitl.superseded`` - bookkeeping on an outdated work
        item, never a human "no" - so it is not counted as a human reject.
        Fail-safe: a lookup failure leaves the status untouched.
        """
        gate_ids = sorted(self._terminating_rejected_gates)
        if not gate_ids or self._session_factory is None:
            return final_status, error_code, error_detail
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                await _ref("set_rls_execution_context")(session)
                claims = (
                    (
                        await session.execute(
                            select(HitlClaim).where(
                                HitlClaim.run_id == run_id,
                                HitlClaim.organisation_id == org_id,
                                HitlClaim.review_id.in_(gate_ids),
                                HitlClaim.decision == "rejected",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.hitl_reject_downgrade_lookup_failed", extra={"run_id": str(run_id)})
            return final_status, error_code, error_detail
        if not claims:
            return final_status, error_code, error_detail
        # Prefer a human rejection when several gates rejected (a human said
        # "no" somewhere); only an all-system supersede is labelled superseded.
        superseded_only = all(_ref("_is_supersede_rejection")(c) for c in claims)
        _log.info(
            "pipeline.hitl_rejected",
            extra={"run_id": str(run_id), "gates": gate_ids, "superseded": superseded_only},
        )
        if superseded_only:
            return "rejected", "hitl.superseded", "Superseded by a newer version of the work item."
        return "rejected", "hitl.rejected", "Rejected by a reviewer at a HITL gate; the run ended."

    async def _finalize_then_publish_completed(self, *, defer_close: bool, **finalize_kwargs: Any) -> Run:
        """Run ``_finalize_run_after_stream`` then publish ``run_completed`` (FAR-1534).

        ``defer_close`` is True when the stream ended ``complete``: the broker
        was deliberately left open so the event is only emitted once finalize
        has committed the terminal status — a would-be ``complete`` that
        finalize downgrades (FAR-1487 ``rejected``, FAR-510 ``failed``) never
        shows live clients ``complete``. The payload stays ``{}`` for a genuine
        ``complete`` and carries ``{"status": <final>}`` for a downgraded run.
        The broker is always closed (even when finalize raises) so SSE
        subscribers are released.
        """
        run_id: uuid.UUID = finalize_kwargs["run_id"]
        try:
            final_run = await self._finalize_run_after_stream(**finalize_kwargs)
            if defer_close:
                self._publish_run_completed(run_id, final_run.status)
            return final_run
        finally:
            if defer_close:
                _ref("get_registry")().close(run_id)

    @staticmethod
    def _publish_run_completed(run_id: uuid.UUID, final_status: str) -> None:
        """Best-effort ``run_completed`` publish carrying the committed status."""
        broker = _ref("get_registry")().get_or_create(run_id)
        try:
            broker.publish("run_completed", {} if final_status == "complete" else {"status": final_status})
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("pipeline.run_completed_publish_failed", extra={"run_id": str(run_id)})

    async def _run_finalize_cost_transaction(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        node_token_usage: dict[str, Any] | None,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str],
        work_intact: bool | None,
    ) -> None:
        """Own + run the finalisation transaction, re-running it on a lock failure.

        The transaction is owned HERE (``session.begin()``), so re-owning it on a
        whole-transaction lock failure (40P01 deadlock / 55P03 lock_timeout) is
        feasible and is the only recovery — the transaction is aborted, so a
        retry must re-run it from the top, not attempt savepoint surgery.
        Idempotent-safe: a rolled-back transaction commits NOTHING (including
        the org accrual, which lives inside the ledger savepoint), so a re-run
        cannot double-count and the ``finalize_cost`` duplicate-terminal guard
        is re-derived from the DB each attempt.

        The bounded retry rides out transient contention; if it persists, the
        exception propagates — ``run_executor_with_watchdog`` terminal-fails the
        run as ``executor_failed`` (truthful, non-silent), never a raw 500.

        FAR-1642 retry-idempotent metrics: each attempt buffers its
        ``limit_refused`` / ``duplicate_terminal`` counters on a FRESH
        :class:`FinalizeMetricSink` and the sink is flushed only AFTER this
        attempt's transaction commits — so a rolled-back attempt emits nothing
        and a retried finalisation emits exactly one sample per event, instead
        of one per attempt. A discarded sink (attempt failed) is simply dropped.
        """
        for attempt in range(1, _ref("FINALIZE_LOCK_RETRY_ATTEMPTS") + 1):
            metric_sink = FinalizeMetricSink()
            try:
                async with self._session_factory() as session, session.begin():
                    await _ref("set_rls_org")(session, org_id)
                    await _ref("set_rls_execution_context")(session)
                    await _ref("finalize_cost")(
                        session,
                        run_id=run_id,
                        org_id=org_id,
                        status=final_status,
                        segment_node_token_usage=node_token_usage,
                        segment_completed_node_outputs=completed_node_outputs,
                        node_type_map=node_type_map,
                        error_code=error_code,
                        error_detail=error_detail,
                        is_terminal=final_status in _ref("_TERMINAL_STATUSES"),
                        session_factory=self._session_factory,
                        claim_token=self._claim_token,
                        metric_sink=metric_sink,
                    )
                    await _ref("_apply_work_intact_best_effort")(session, run_id, work_intact, claim_token=self._claim_token)
                # Committed — the buffered counters are now real (exactly once).
                metric_sink.flush()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt < _ref("FINALIZE_LOCK_RETRY_ATTEMPTS") and _ref("_is_finalize_lock_retryable")(exc):
                    _log.warning(
                        "pipeline.finalize_lock_retry",
                        extra={
                            "run_id": str(run_id),
                            "attempt": attempt,
                            "max_attempts": _ref("FINALIZE_LOCK_RETRY_ATTEMPTS"),
                            "sqlstate": _ref("sqlstate_of")(exc),
                        },
                    )
                    await asyncio.sleep(_ref("_FINALIZE_LOCK_RETRY_DELAY_SECONDS") * attempt)
                    continue
                raise

    async def _finalize_run_after_stream(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        node_type_map: dict[str, str],
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        node_token_usage: dict[str, Any] | None,
        completed_node_outputs: dict[str, Any],
        node_ids: set[str],
    ) -> Run:
        """Finalize a streamed run (execute + resume share this tail).

        Computes ``work_intact``, runs ``finalize_cost``, records the
        compensating daily fact when needed, fetches the final row, and fires
        the post-terminal evidence probes. ``final_status`` reflects the
        terminal/awaiting_human outcome of the stream, after the FAR-510
        masked-sandbox-failure downgrade.
        """
        # FAR-510 / FAR-1487: downgrade a stream that would otherwise finalize
        # ``complete`` on a masked sandbox or connector failure. Runs BEFORE the
        # eval_blocked audit, work_intact, and finalize_cost so every downstream
        # consumer sees the honest status.
        final_status, error_code, error_detail = await self._downgrade_final_status_after_stream(
            run_id=run_id,
            org_id=org_id,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            completed_node_outputs=completed_node_outputs,
            node_type_map=node_type_map,
        )

        # Record audit events for block failures on resume.
        eval_blocked = final_status == "eval_failed" and error_code == "eval_blocked"
        if eval_blocked:
            await self._record_eval_blocked_audit(
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                error_detail=error_detail,
            )

        # Resume recomputes from LIVE components over the cumulative merged set
        # (two-state rule, §4.4); finalize_cost reads the stored cumulative set,
        # merges the resumed segment (segment-wins), and recomputes.
        # FAR-152 §15.3 — work_intact computed at terminalization (same rule as
        # execute()).
        work_intact = self._compute_run_work_intact(final_status, error_code, completed_node_outputs, node_ids)
        await self._run_finalize_cost_transaction(
            run_id=run_id,
            org_id=org_id,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            node_token_usage=node_token_usage,
            completed_node_outputs=completed_node_outputs,
            node_type_map=node_type_map,
            work_intact=work_intact,
        )

        # Terminal-failure notification. Fired ONLY after the
        # terminal write above has COMMITTED and ONLY for a non-retryable
        # critical code (see ``should_notify_terminal_failure``) — routine
        # retryable failures stay silent, and a finalize lock retry (which
        # re-runs the transaction) can never double-notify because the dispatch
        # sits outside the retry loop. Failure-isolated inside the helper.
        if final_status == "failed":
            await self._dispatch_run_failed(
                org_id=org_id,
                run_id=run_id,
                pipeline_id=pipeline_id,
                error_code=error_code,
            )

        # FAR-291: run-termination compensation for a guardrail-blocked
        # MID-RUN terminalization. The terminal status write (finalize_cost
        # above) has already landed; a run whose earlier nodes pushed a PR /
        # flipped a Linear status before a later node's output was
        # guardrail-blocked now gets those side effects compensated. Best-effort
        # + failure-isolated (guard-the-guard): a compensation failure never
        # crashes terminalization. Uses its own fresh session + hub.
        if eval_blocked:
            await self._compensate_blocked_run_best_effort(
                org_id=org_id,
                run_id=run_id,
                executed_nodes=completed_node_outputs,
            )

        # Fetch the final run in a fresh session — finalize_cost's ledger block
        # may have aborted the transaction in the whole-tx-abort reduced-escape
        # path (the run row was re-terminalized there).
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            final_run: Run | None = await _ref("get_run")(session, run_id)

        if final_run is None:
            raise RunNotFoundError(run_id)

        # FAR-2006 / FAR-2138 / FAR-152 §15.3 / FAR-1690 A3: alerting,
        # terminal-status notifications, post-commit evidence probes and the
        # teardown seal — dispatched from this ONE shared finalization tail so
        # every terminal path is covered.
        await self._run_post_finalize_side_effects(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            final_status=final_status,
            error_code=error_code,
            final_run=final_run,
            completed_node_outputs=completed_node_outputs,
        )
        return final_run

    async def _downgrade_final_status_after_stream(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        error_detail: str | None,
        completed_node_outputs: dict[str, Any],
        node_type_map: dict[str, str],
    ) -> tuple[str, str | None, str | None]:
        """Downgrade a streamed ``complete`` that hides a masked node failure.

        FAR-510: a sandbox_agent node whose dispatch died to a generic
        exception RETURNS a failed envelope instead of raising, and node
        completion is output-presence-based — so a failed dispatch would
        otherwise finalize the run ``complete``. The scan also covers the
        run's STORED cumulative outputs (resume parity: a HITL resume only
        re-emits the resumed segment, so a masked failure from a prior
        segment is only visible in the stored row). The extra run-row read
        happens on the complete path ONLY — failed/awaiting_human skip it.
        """
        stored_node_outputs: dict[str, Any] = {}
        stored_node_telemetry: dict[str, Any] = {}
        if final_status == "complete":
            # FAR-1487: a stamped terminate-rejection ENDs the graph, which is
            # a NORMAL stream exit - without this downgrade the run would be
            # finalized ``complete`` (the same bug one layer up).
            final_status, error_code, error_detail = await self._downgrade_hitl_terminate_rejection(
                run_id=run_id,
                org_id=org_id,
                final_status=final_status,
                error_code=error_code,
                error_detail=error_detail,
            )
        if final_status == "complete":
            stored_node_outputs, stored_node_telemetry = await self._load_stored_node_columns(run_id=run_id, org_id=org_id)
        final_status, error_code, error_detail = self._downgrade_connector_failures(
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            completed_node_outputs=completed_node_outputs,
            node_type_map=node_type_map,
            stored_node_outputs=stored_node_outputs,
            stored_node_telemetry=stored_node_telemetry,
        )
        final_status, error_code, error_detail = self._downgrade_masked_sandbox_failures(
            run_id=run_id,
            final_status=final_status,
            error_code=error_code,
            error_detail=error_detail,
            completed_node_outputs=completed_node_outputs,
            node_type_map=node_type_map,
            stored_node_outputs=stored_node_outputs,
            stored_node_telemetry=stored_node_telemetry,
        )
        return final_status, error_code, error_detail

    async def _run_post_finalize_side_effects(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
        final_run: Run,
        completed_node_outputs: dict[str, Any],
    ) -> None:
        """Fire the alerting / notification / evidence / seal tail.

        Runs after the terminal write has COMMITTED, reading the COMMITTED row
        (``final_run``) rather than the pre-finalize locals.
        """
        # FAR-2006: a non-retryable provider auth/billing refusal is exactly
        # the class that must page. Dispatched from the ONE shared finalization
        # tail so every terminal path (transient short-circuit, retry-policy
        # exhaustion, generic catch) is covered without a second call site.
        await self._alert_provider_failure(
            run_id=run_id,
            org_id=org_id,
            pipeline_id=pipeline_id,
            final_status=final_status,
            error_code=error_code,
        )

        # FAR-2138 — terminal-outcome notifications for the statuses that carry
        # their own event (stalled / budget_exceeded / eval_blocked). Read from
        # the COMMITTED row, never the pre-finalize locals: the per-agent token
        # budget override lands INSIDE finalize_cost above, so only the row
        # carries the resulting ``budget_exceeded`` status. Dispatched from this
        # ONE shared tail so execute and resume are both covered.
        await self._dispatch_terminal_status_notification(
            org_id=org_id,
            run_id=run_id,
            pipeline_id=pipeline_id,
            final_run=final_run,
        )

        # FAR-152 §15.3 — post-commit async evidence probe (same rule as
        # execute()). Bounded ≤3s per node, gated by the EvidenceProvider seam.
        await self._run_post_terminal_evidence_probes(
            run_id=run_id,
            org_id=org_id,
            final_status=final_status,
            completed_node_outputs=completed_node_outputs,
        )
        # FAR-1690 A3 (run_teardown.RunTeardownSeal): the sealed terminal step —
        # revoke the run's per-run API key (FAR-296 Phase 3b), then destroy every
        # workspace labelled with the run id, then write one
        # run.teardown_sealed fact. Revoke-first, fail-closed. Failure-isolated
        # so a seal failure never crashes terminalization (the key's short-TTL
        # expiry in validate_api_key is the credential backstop; the
        # workspace-orphan reconciler reclaims a workspace the seal could not).
        try:
            await self._seal_run_teardown(run_id=run_id, org_id=org_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("run_teardown.seal_failed", extra={"run_id": str(run_id)})

    async def _alert_provider_failure(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        final_status: str,
        error_code: str | None,
    ) -> None:
        """Dispatch the provider-refusal alert (FAR-2006) — failure-isolated.

        A 401/402 provider refusal is terminal and never retried, so without a
        real alert the only trace is a log line plus an ``error_code`` row —
        exactly how a whole night ran at zero throughput with nothing
        surfaced. Uses the existing Notifier path (webhook + in-app) with a
        DEDICATED event so an operator can page on provider refusals without
        subscribing to every failed run.

        Called from the one shared finalization tail, so every terminal path
        is covered. Fail-open WITH a log: an alerting failure must never crash
        terminalization (the run is already terminal), but it also must never
        be silent.
        """
        if self._notifier is None or final_status != "failed":
            return
        canonical = _ref("map_legacy_code")(error_code)
        if canonical not in _ref("NON_RETRYABLE_PROVIDER_CODES"):
            return

        pipeline_name: str | None = None
        try:
            async with self._session_factory() as session, session.begin():
                await _ref("set_rls_org")(session, org_id)
                pipeline_name = await _ref("_safe_pipeline_name")(session, pipeline_id, org_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "pipeline.provider_failure_alert_name_lookup_failed",
                extra={"run_id": str(run_id), "pipeline_id": str(pipeline_id)},
                exc_info=True,
            )

        payload: dict[str, Any] = {
            "run_id": str(run_id),
            "pipeline_id": str(pipeline_id),
            "pipeline_name": pipeline_name or "unknown pipeline",
            "error_code": canonical,
        }
        try:
            await self._notifier.dispatch_event(
                org_id=org_id,
                event_type=_ref("EVENT_PROVIDER_FAILURE"),
                payload=payload,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "pipeline.provider_failure_alert_failed",
                extra={"run_id": str(run_id), "error_code": canonical},
            )

    async def _seal_run_teardown(self, *, run_id: uuid.UUID, org_id: uuid.UUID) -> None:
        """Run the sealed joint revoke+destroy step for a terminalized run (FAR-1690 A3).

        Failure-isolated at the call site — a seal failure never crashes
        terminalization. ``seal_run_teardown`` itself raises
        ``RunTeardownSealError`` when the credential revoke could not be
        confirmed (after writing the ``sealed=False`` fact); that surfaces here
        as a loud log line, and the compensating sweep retries.
        """
        if self._session_factory is None:
            return
        from farna.core.run_teardown import seal_run_teardown

        await seal_run_teardown(
            run_id=run_id,
            org_id=org_id,
            session_factory=self._session_factory,
        )
