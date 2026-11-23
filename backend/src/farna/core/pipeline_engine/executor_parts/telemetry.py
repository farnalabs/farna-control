"""Telemetry / cost / evidence group of :class:`PipelineExecutor`.

Step 3 of the split described in ``GAPS-harden-w2-executor.md`` §5: the cost,
token-accounting, evidence-probe and cancellation/audit-hook concerns move out
of ``executor.py`` as a mixin. Nothing here holds its own state — every method
reads the host's ``_session_factory`` / ``_evidence_provider``.

Patch safety
------------
The bodies below were moved verbatim. Their free variables used to resolve from
``farna.core.pipeline_engine.executor``'s module globals, which is exactly where
``mock.patch`` targets land, so each one is resolved through
:func:`~.support._ref` at CALL time instead of being bound as a module-local in
this file. ``_log`` is :data:`~.support._log`, which forwards to
``executor._log``. ``asyncio`` / ``math`` / ``Decimal`` are imported directly:
they are shared objects, so ``patch("...executor.asyncio.sleep")`` mutates the
same module object this file uses.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from farna.core.pipeline_engine.evidence import EvidenceProvider

from .support import _log, _ref


class TelemetryMixin:
    """Cost, token-accounting, evidence and cancellation/audit-hook methods."""

    # Declared (assigned on the host) so the type checker can see them.
    _session_factory: async_sessionmaker[AsyncSession]
    _evidence_provider: EvidenceProvider | None

    def _check_db_cancellation(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> Callable[[], Awaitable[bool]]:
        """Build a DB-backed cancellation check closure for a run."""

        async def _check() -> bool:
            try:
                return await asyncio.wait_for(
                    self._do_db_cancellation_check(org_id, run_id),
                    timeout=5.0,
                )
            except TimeoutError:
                _log.warning(
                    "run_context.cancellation_db_timeout",
                    extra={"run_id": str(run_id)},
                )
                return False

        return _check

    async def _do_db_cancellation_check(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> bool:
        """Execute the DB cancellation check query."""
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            run = await _ref("get_run")(session, run_id)
            return run is not None and run.cancellation_requested

    def _dispatch_context_write_audit(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> Callable[[dict[str, Any]], Awaitable[None]]:
        """Build the audit hook that records non-context-setter run_context writes.

        Mirrors ``_check_db_cancellation``: opens a fresh session, applies RLS, and
        appends a ``context_write_by_non_setter`` event to the org's audit chain
        (§8.18). Failures and timeouts are logged and swallowed so the hook can
        never mask the ContextSetterViolationError raised by the decorator.
        """

        async def _audit(payload: dict[str, Any]) -> None:
            try:
                await asyncio.wait_for(
                    self._do_context_write_audit(org_id, run_id, payload),
                    timeout=5.0,
                )
            except TimeoutError:
                _log.warning(
                    "run_context.audit_hook_timeout",
                    extra={"run_id": str(run_id)},
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception(
                    "run_context.audit_hook_failed",
                    extra={"run_id": str(run_id)},
                )

        return _audit

    async def _do_context_write_audit(
        self,
        org_id: uuid.UUID,
        run_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        """Append the ``context_write_by_non_setter`` audit event for a run."""
        node_id = payload.get("node_id")
        role = payload.get("role")
        summary = f'Non-setter context write on node "{node_id}"' if node_id else "Non-setter context write"
        if role:
            summary += f" (role {role})"
        async with self._session_factory() as session, session.begin():
            await _ref("set_rls_org")(session, org_id)
            await _ref("set_rls_execution_context")(session)
            await _ref("append_audit_event")(
                session,
                org_id=org_id,
                event_type="context_write_by_non_setter",
                resource_type="run",
                resource_id=run_id,
                payload_json={
                    "node_id": node_id,
                    "role": role,
                    "attempted_keys": list(payload.get("attempted_keys") or []),
                    "actor": _ref("SYSTEM_ACTOR"),
                    "summary": summary,
                },
            )

    @staticmethod
    def _log_accumulation_state(
        run_id: uuid.UUID,
        segments_completed: int,
        node_token_usage: dict[str, dict[str, int]] | None,
    ) -> None:
        """Distinguish a genuinely-empty accumulator from an upstream wiring
        regression (§4.2). A ``{}``/``None`` accumulator is legitimate when ZERO
        segments streamed; an EMPTY dict after ≥1 segment streamed means the
        on_chain_end / on_chat_model_end handlers stopped populating it.
        """
        if segments_completed > 0 and not node_token_usage:
            _log.warning(
                "cost_components_accumulation_broken",
                extra={"run_id": str(run_id), "segments_completed": segments_completed},
            )
        elif segments_completed == 0:
            _log.info(
                "cost_components_zero_nodes",
                extra={"run_id": str(run_id)},
            )

    @staticmethod
    def _compute_token_costs(
        node_token_usage: dict[str, dict[str, int]] | None,
        input_rate: Decimal,
        output_rate: Decimal,
    ) -> tuple[int | None, Decimal | None, dict[str, Any] | None]:
        """Compute total tokens, total cost, and per-node cost from node token usage."""
        if node_token_usage is None:
            return None, None, None

        total_tokens = sum(n.get("total_tokens") or 0 for n in node_token_usage.values())
        total_cost = Decimal(0)
        result_usage: dict[str, dict[str, Any]] = {}
        for node_id, n_data in node_token_usage.items():
            input_tokens = n_data.get("input_tokens")
            output_tokens = n_data.get("output_tokens")
            n_cost = Decimal(str(input_tokens if input_tokens is not None else 0)) * input_rate
            n_cost += Decimal(str(output_tokens if output_tokens is not None else 0)) * output_rate
            result_usage[node_id] = {**n_data, "cost_usd": float(n_cost)}
            total_cost += n_cost

        return total_tokens, total_cost, result_usage

    @staticmethod
    def _aggregate_sandbox_cost(completed_node_outputs: dict[str, Any] | None) -> Decimal:
        """Sum per-node sandbox-agent cost estimates into a single Decimal.

        NEW FORMAT (split envelope): each node_output has ``telemetry`` key with
        ``cost_estimate_usd`` directly.

        LEGACY FORMAT: each completed node's captured output is ``{"artifacts": [...],
        "output": {...}}``; the inner ``output`` dict carries the numeric
        ``cost_estimate_usd``.

        Non-dict outputs, missing estimates, non-positive, and non-finite
        values (NaN/inf, which would otherwise corrupt the run total) contribute
        zero. Kept as a small pure helper so cost parity is testable and shared
        between :meth:`execute` and :meth:`resume`.
        """
        if not completed_node_outputs:
            return Decimal(0)
        sandbox_cost = Decimal(0)
        for node_output in completed_node_outputs.values():
            if not isinstance(node_output, dict):
                continue
            # New split format: telemetry channel carries cost_estimate_usd
            telemetry = node_output.get("telemetry")
            est = None
            if isinstance(telemetry, dict):
                est = telemetry.get("cost_estimate_usd")
            else:
                # Legacy format fallback
                out = node_output.get("output")
                if isinstance(out, dict):
                    est = out.get("cost_estimate_usd")
            if isinstance(est, (int, float)) and est > 0 and math.isfinite(est):
                sandbox_cost += Decimal(str(est))
        return sandbox_cost

    def _get_evidence_provider(self, org_id: uuid.UUID) -> EvidenceProvider:
        """The injected evidence provider (tests) or the production
        E2B+DB-backed provider wired for this run's org.
        """
        if self._evidence_provider is not None:
            return self._evidence_provider
        provider: EvidenceProvider = _ref("build_default_evidence_provider")(self._session_factory, org_id)
        return provider

    def _compute_run_work_intact(
        self,
        final_status: str,
        error_code: str | None,
        completed_node_outputs: dict[str, Any],
        node_ids: set[str],
    ) -> bool | None:
        """FAR-152 §15.3 — work_intact computed at terminalization from
        completed-node artifacts + the full DAG ran. NOT from the async
        evidence probe (restores the false-failure banner for #1/#3).

        Returns None for non-terminal statuses (nothing to write). An
        A1-elevated run (``failed`` + ``agent.failed``) is NOT complete — its
        honest work verdict is False (§15.4), so the zero-work elevation banner
        is what renders. Same for a ``sandbox.no_output_json`` session-lost run
        (FAR-227): the agent produced no output at all, so it can never be
        "work intact". Same for a FAR-510 downgraded run
        (``failed`` + ``sandbox.agent_failed``): the synthetic failure envelope
        is not a work artifact — the agent died.
        """
        if final_status not in _ref("_TERMINAL_STATUSES"):
            return None
        if final_status == "failed" and error_code in (
            "agent.failed",
            _ref("_CODE_SANDBOX_NO_OUTPUT_JSON"),
            _ref("_CODE_SANDBOX_AGENT_FAILED"),
        ):
            return False
        work_intact: bool = _ref("compute_work_intact")(completed_node_outputs, node_ids)
        return work_intact

    async def _run_post_terminal_evidence_probes(
        self,
        *,
        run_id: uuid.UUID,
        org_id: uuid.UUID,
        final_status: str,
        completed_node_outputs: dict[str, Any],
    ) -> None:
        """FAR-152 §15.3 — post-commit async evidence probe for no-op-eligible
        nodes (declared ``outcome:success``) on a complete run. Runs off the
        critical path (after terminalization commits), bounded ≤3s per probe,
        gated by the EvidenceProvider seam. Fail-open — a probe failure never
        affects the run.
        """
        if final_status != "complete" or not _ref("evidence_enabled")():
            return
        evidence_nodes = [node_id for node_id, out in completed_node_outputs.items() if _ref("node_declared_success")(out)]
        if not evidence_nodes:
            return
        provider = self._get_evidence_provider(org_id)
        results = await asyncio.gather(
            *[
                _ref("run_evidence_probe")(
                    provider=provider,
                    session_factory=self._session_factory,
                    run_id=run_id,
                    node_id=node_id,
                    organisation_id=org_id,
                )
                for node_id in evidence_nodes
            ],
            return_exceptions=True,
        )
        for node_id, res in zip(evidence_nodes, results, strict=False):
            if isinstance(res, Exception):
                _log.warning(
                    "heuristic.probe_task_failed",
                    extra={"run_id": str(run_id), "node_id": node_id},
                    exc_info=res,
                )
