"""Run-shape, admission, governance and accounting helpers for the executor.

Extracted verbatim from ``executor.py`` (step 1 of the package split in
``GAPS-harden-w2-executor.md`` §5). "Run shape" is everything that describes or
bounds one run before/during streaming: graph topology and timeouts, the
checkpointer scope, checkpoint-resume claim, sandbox-capacity admission,
work-intact persistence, per-node token/OTel accounting, the interrupt payload
readers, the autonomy ceiling clamp and the eval-mode normalisation helpers.

Every name stays importable from ``executor``; patch targets keep working
through :func:`._ref` and :data:`._log` (see ``support``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from langchain_core.messages import BaseMessage
from sqlalchemy import Boolean, Uuid, bindparam, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from farna.connectors.base import node_fires_dispatch_job
from farna.core.node_output_split import DEFAULT_NODE_TYPE, SPLITTABLE_NODE_TYPES
from farna.core.pipeline_engine.decorator import _RESERVED_RUN_CONTEXT_KEYS
from farna.core.pipeline_engine.errors import ResumeClaimSupersededError
from farna.core.pipeline_engine.eval_persist_order import EvalDefDTO
from farna.core.pipeline_engine.modulo_saver import ModuloPostgresSaver
from farna.core.pipeline_engine.port_resolver import compute_port_topology_hash
from farna.core.pipeline_engine.runaway_protection import RunawayGuard, RunawayRunError
from farna.core.run_context.autonomy import (
    PIPELINE_MAX_AUTONOMY_KEY,
    AutonomyLevel,
    autonomy_level_rank,
)
from farna.db.models.pipeline_snapshot import PipelineSnapshot
from farna.db.models.policy_gate import PolicyGate
from farna.db.models.run import Run
from farna.otel_bridge import trace_id_for_thread

from .support import RunNotFoundError, _log, _ref

_WORKER_ID: str = f"{socket.gethostname()}:{os.getpid()}"

# FAR-939 chunk 12 (M4, §3.3 run-frozen): key under which the fire-time
# evaluation modes of the gate's source node are stamped onto the claim's
# ``gate_config_json`` (the FAR-634 fire-time review-config column), then
# read back by ``resume()`` to re-freeze the DTO universe. Single name for
# the write site and the read site so the stamp cannot drift.
_GATED_EVAL_MODES_KEY = "gated_eval_modes"

# Engine-managed run_context keys that a caller-supplied
# ``snapshot.run_context_defaults`` spread must never seed. Derived from the
# decorator's reserved-key set (imported — no cycle: decorator does not
# import executor) UNION ``_run_overrides``, which is NOT in that set (it is
# a node_runner override boundary key, not a context-setter write guard) but
# is equally engine-managed: it is seeded ONLY from the run's frozen
# ``variant_config_snapshot`` in ``_seed_state`` below.
_ENGINE_MANAGED_SEED_KEYS: frozenset[str] = _RESERVED_RUN_CONTEXT_KEYS | {"_run_overrides"}

# Node types that can PAUSE a run for human input and therefore REQUIRE a
# LangGraph checkpointer to be able to resume. Any pipeline containing one is
# "interactive". Everything else (agent / connector / sandbox_agent) is a
# batch pipeline that never pauses, so its checkpoints are pure overhead.
_INTERACTIVE_NODE_TYPES: frozenset[str] = frozenset({"manual"})

_SANDBOX_AGENT_CACHE: OrderedDict[str, bool] = OrderedDict()
_SANDBOX_AGENT_CACHE_MAX = 512


def _governed_evaluation_mode(gate: PolicyGate) -> str:
    """Normalise ``policy_gates.evaluation_mode`` to the closed DTO vocabulary.

    Production rows always carry the NOT NULL column (migration
    0316_policy_gate_evaluation_mode); the attribute fallback keeps
    pre-0316 ORM stand-ins and legacy fixtures (no attribute at all) on
    today's behaviour — automatic. Hoisted out of
    ``PipelineExecutor._resolve_governed_gate`` for the cognitive-complexity
    budget (S3776); behaviour is unchanged.
    """
    raw = getattr(gate, "evaluation_mode", None)
    return "human" if raw == "human" else "automatic"


async def _claim_resume_for_executor(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    org_id: uuid.UUID,
    expected_claim_token: str,
    source_status: str,
) -> Run:
    """Atomically take the expected resume claim before loading/executing it.

    The MCP approval path captures the run lease before committing the decision.
    Matching both that token and the claimable source state in this UPDATE makes
    the approval a compare-and-swap: a reconcile claim or another resume that
    won first has already changed one of those predicates, so this path refuses
    without changing status or starting execution. The token is deliberately
    preserved; the caller's recovery path uses the same fence.
    """
    result = await session.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.organisation_id == org_id,
            Run.claim_token == expected_claim_token,
            Run.status == source_status,
        )
        .values(
            status="running",
            heartbeat_at=func.now(),
            claimed_by=_WORKER_ID,
        )
        .returning(Run.id)
    )
    if result.scalar_one_or_none() is None:
        raise ResumeClaimSupersededError(run_id)
    refreshed = await session.execute(select(Run).where(Run.id == run_id).execution_options(populate_existing=True))
    run = refreshed.scalar_one_or_none()
    if run is None:
        raise RunNotFoundError(run_id)
    return run


async def _teardown_hub(hub: Any) -> None:
    """Await a hub's ``__aexit__`` shielded against cancellation.

    A cancellation arriving during cleanup must not abort the hub teardown —
    ``asyncio.shield`` keeps the ``__aexit__`` coroutine running to completion
    even when the awaiting task is cancelled (a second CancelledError cannot
    abort the cleanup). The shielded future is re-awaited on cancellation so no
    "Task was destroyed but it is pending" warning is emitted at loop close.

    The runtime provider hub the executor built alongside the ConnectorHub is
    aclosed here too, best-effort: workspaces tracked at teardown are destroyed.
    A workspace whose provision was cancelled mid-create may outlive the run
    (known gap, tracked for D4 lifecycle work).
    """
    shielded = asyncio.shield(hub.__aexit__(None, None, None))
    try:
        await shielded
    except asyncio.CancelledError:
        with suppress(Exception):
            await shielded
        raise
    except Exception:
        _log.exception("pipeline.hub_cleanup_failed")
    finally:
        # Read the runtime hub through the ConnectorHub constructor seam.
        # Depending on construction ``_runtime_provider`` may hold
        # a bare provider (no ``aclose`` — disposal is then driven by the
        # hub's own ``__aexit__``) or a ``RuntimeProviderHub`` whose
        # ``aclose()`` disposes tracked workspaces.
        runtime_hub = getattr(hub, "_runtime_provider", None)
        if runtime_hub is not None and hasattr(runtime_hub, "aclose"):
            try:
                await runtime_hub.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("pipeline.runtime_hub_cleanup_failed")


def _seed_state(
    snapshot: PipelineSnapshot,
    input_payload: dict[str, Any],
    variant_config_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the initial LangGraph state for a run.

    If *input_payload* contains a ``_feedback_correction`` key, it is
    promoted to the top-level ``run_context`` as ``feedback_correction``
    and removed from the input dict so the pipeline agents never see it
    as part of their normal input.

    ``_run_overrides`` is a SYSTEM-RESERVED TOP-LEVEL run_context key. It is
    seeded ONLY from the run's frozen ``variant_config_snapshot`` (captured at
    fire time) — NEVER from caller input or caller-controlled
    ``run_context_defaults``. A normal run has no variant config, so a
    caller-supplied ``_run_overrides`` inside ``input_payload`` stays DATA in
    ``run_context["input"]`` and can never reach the node_runner's override
    boundary (injection surface).

    The same holds for the ``run_context_defaults`` spread: it is
    unfiltered arbitrary JSON (it travels through snapshot/clone/
    workflow-import), so every engine-managed key
    (``_ENGINE_MANAGED_SEED_KEYS`` — the reserved set UNION
    ``_run_overrides``) is filtered OUT of the spread before assembly.
    Engine-owned values (``cancelled``/``input``, the snapshot-pinned
    autonomy keys, and the variant-config ``_run_overrides``) are then set
    exactly as before, from their authoritative sources.
    """
    # Copy input_payload to avoid mutating the caller's dict.
    payload = dict(input_payload)
    run_context_defaults: dict[str, Any] = snapshot.run_context_defaults or {}
    # Filter the SPREAD (not the just-built dict — it already
    # legitimately sets cancelled/input). With a NULL ceiling/default the
    # autonomy keys stay absent and resolution falls back exactly as
    # designed; the snapshot values pinned below are authoritative.
    filtered_defaults = {k: v for k, v in run_context_defaults.items() if k not in _ENGINE_MANAGED_SEED_KEYS}
    run_context: dict[str, Any] = {
        **filtered_defaults,
        "cancelled": False,
        "input": payload,
    }
    # Promote feedback_correction from input_payload to run_context
    # so the entire graph can access rejection metadata.
    feedback_correction = payload.pop("_feedback_correction", None)
    if feedback_correction:
        run_context["feedback_correction"] = feedback_correction
    # Seed autonomy from snapshot-level default so gate nodes can resolve it.
    if snapshot.default_autonomy_level:
        run_context["_pipeline_default_autonomy"] = snapshot.default_autonomy_level
    # Pin the autonomy CEILING from the snapshot (nullable —
    # NULL means resolution falls back to the default as the effective
    # ceiling). isinstance-str guards non-string stand-ins (test mocks).
    max_autonomy = getattr(snapshot, "max_autonomy_level", None)
    if isinstance(max_autonomy, str) and max_autonomy:
        run_context[PIPELINE_MAX_AUTONOMY_KEY] = max_autonomy
    # Seed the system-reserved override namespace from the run's FROZEN variant
    # config only. This is the ONLY path that populates it — a caller-supplied
    # ``_run_overrides`` in the input payload (or in run_context_defaults,
    # filtered out of the spread above) is never promoted here.
    if isinstance(variant_config_snapshot, dict):
        overrides = variant_config_snapshot.get("_run_overrides")
        if isinstance(overrides, dict):
            run_context["_run_overrides"] = overrides
    return {
        "run_context": run_context,
        "artifacts": [],
        # Seeded so the loop-edge counter node starts from an explicit dict
        # (the counter node returns the incremented value as a real state
        # update — LangGraph discards router-side in-place mutations, so the
        # counter must live on a node, not a router).
        "_iteration_counts": {},
    }


def _streamed_interrupts(lg_event: dict[str, Any]) -> tuple[Any, ...]:
    """Extract native LangGraph interrupts from a top-level stream event."""
    if lg_event.get("event") != "on_chain_stream":
        return ()
    data = lg_event.get("data")
    chunk = data.get("chunk") if isinstance(data, dict) else None
    interrupts = chunk.get("__interrupt__") if isinstance(chunk, dict) else None
    if isinstance(interrupts, (list, tuple)):
        return tuple(interrupts)
    return (interrupts,) if interrupts is not None else ()


@asynccontextmanager
async def _checkpointer_scope(  # NOSONAR S7503 - async context-manager seam
    conn_string: str,
    organisation_id: uuid.UUID,
    fernet_key: str | None = None,
) -> AsyncIterator[ModuloPostgresSaver]:
    """Create a ModuloPostgresSaver for the duration of a single run execution."""
    async with ModuloPostgresSaver.from_conn_string(
        conn_string,
        organisation_id=organisation_id,
        fernet_key=fernet_key,
    ) as saver:
        yield saver


def _sandbox_agent_for_snapshot(snapshot_id: uuid.UUID, graph_json: dict[str, Any] | None) -> bool:
    """Bounded per-snapshot cache over the (immutable) snapshot graph JSON."""
    key = str(snapshot_id)
    cached = _SANDBOX_AGENT_CACHE.get(key)
    if cached is not None:
        return cached
    result: bool = _ref("_graph_contains_sandbox_agent")(graph_json)
    if len(_SANDBOX_AGENT_CACHE) >= _SANDBOX_AGENT_CACHE_MAX:
        _SANDBOX_AGENT_CACHE.clear()
    _SANDBOX_AGENT_CACHE[key] = result
    return result


def _graph_is_idempotent(graph_json: dict[str, Any] | None) -> bool:
    """True when every node in the graph is safe to re-run.

    A node is idempotent unless it EXPLICITLY declares ``idempotent=false``:
    the field defaults to ``true`` on the ``PipelineGraphNode`` schema, and a
    missing/None value (legacy graphs persisted before the field existed) is
    treated as idempotent so old graphs keep their retry behaviour unchanged.
    Returns False when ANY node is non-idempotent — a retry re-executes the
    whole run, so a single side-effecting node makes re-running the run unsafe.

    The graph itself is also non-idempotent when it contains a binding that
    FIRES an external job — ``operation="dispatch"`` + ``dispatch_action="trigger_run"``
    — regardless of the stored ``idempotent`` flag. The flag is a write-path side
    effect of the REST model, so an MCP-authored or hand-written graph could
    carry ``idempotent=true`` (or omit it) on a job-firing node and silently
    re-arm BOTH retry paths; the binding is the durable, writer-independent
    truth, keyed on the same ``connector_binding_operation`` the engine routes
    and the run classifier reads. Read-only dispatch actions
    (``get_run_status`` / ``get_run_logs`` / ``list_runs``) stay idempotent —
    re-running a read fires nothing.
    """
    if not isinstance(graph_json, dict):
        return True
    for node in graph_json.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        if node.get("idempotent") is False:
            return False
        if node_fires_dispatch_job(node):
            return False
    return True


def compute_retry_aware_topology_hash(
    graph_json: dict[str, Any] | None,
    pipeline_retry_policy: dict[str, Any] | None,
) -> str:
    """Structural compile-cache hash that folds the pipeline retry policy in.

    The per-node retry/compensation wrapper reads the pipeline ``retry_policy``
    as the node default. The graph_cache key is
    ``(pipeline_id, snapshot_id, node_timeout, struct_hash)``; folding the
    policy into ``struct_hash`` means a PATCH to ``retry_policy`` recompiles
    the wrapped nodes instead of serving a stale graph (the same reason
    ``pipeline_node_timeout_seconds`` is part of the key). The per-node
    ``retry`` blocks live in ``graph_json`` (already part of the topology hash),
    so only the pipeline-level default needs folding here.
    """
    base = compute_port_topology_hash(graph_json if isinstance(graph_json, dict) else {})
    if not isinstance(pipeline_retry_policy, dict) or not pipeline_retry_policy:
        return base
    policy_payload = json.dumps(pipeline_retry_policy, sort_keys=True, default=str).encode("utf-8")
    return base + ":" + hashlib.sha256(policy_payload).hexdigest()[:16]


def _graph_is_interactive(graph_json: dict[str, Any] | None) -> bool:
    """True when the graph needs checkpoint persistence (HITL/interactive).

    A checkpoint is only useful for a run that can PAUSE and RESUME. Two graph
    shapes make a pipeline interactive, matching exactly the LangGraph paths
    that call ``interrupt()`` (node_runner):
      - an edge carrying ``hitl_review_config`` (a HITL gate is inserted between
        source and target and interrupts for human approval / review), and
      - a node with ``node_type == "manual"`` (a manual-input node that
        interrupts and waits for human output).

    Batch pipelines (pure ``agent`` / ``connector`` / ``sandbox_agent`` nodes,
    no HITL gates) never pause, so persisting a checkpoint after every
    superstep is pure overhead.

    When the graph cannot be inspected (None / not a dict) we are conservative
    and return True so a genuinely interactive pipeline is never broken by
    lacking a checkpointer — the only caller (``_execute_stream``) always has a
    real ``graph_json``.
    """
    if not isinstance(graph_json, dict):
        return True
    any_interactive_node = any(isinstance(node, dict) and str(node.get("node_type", "")).strip() in _INTERACTIVE_NODE_TYPES for node in graph_json.get("nodes", []) or [])
    any_hitl_review_edge = any(isinstance(edge, dict) and edge.get("hitl_review_config") for edge in graph_json.get("edges", []) or [])
    return any_interactive_node or any_hitl_review_edge


def _resolved_node_timeouts(graph_json: dict[str, Any] | None, default_seconds: int) -> dict[str, int]:
    """Resolve each graph node's effective ``timeout_seconds``.

    The absolute node-deadline watchdog holds every node to its own hard
    deadline, falling back to the pipeline-level default when a node does not
    configure one.

    Absent and explicitly-``null`` are the SAME state here: the API
    ``PipelineGraphNode`` model defaults ``timeout_seconds`` to ``None`` and
    serialises the key, so graphs authored through the API carry an explicit
    ``"timeout_seconds": null``. Resolving that with ``dict.get(key, default)``
    would return the persisted ``None`` (the key IS present) rather than the
    default, and ``int(None)`` raises a TypeError that fails the run before any
    node starts. Guard ``None`` explicitly so the default is always applied.
    """
    resolved: dict[str, int] = {}
    for node in (graph_json or {}).get("nodes", []) or []:
        if not isinstance(node, dict) or "id" not in node:
            continue
        configured = node.get("timeout_seconds")
        resolved[str(node["id"])] = int(configured if configured is not None else default_seconds)
    return resolved


async def _script_lease_probe_ok(
    session_factory: Callable[..., Any] | None,
    run_id: str,
    org_id: Any,
    claim_token: str | None,
) -> bool:
    """Stale-claim probe: is it SAFE to requeue this run?

    Reads ``runs.sandbox_dispatch_state`` for a live ``script_executing`` lease
    (execution claimed, completion marker pending) that belongs to THIS claim.
    A stale lease means a script PROCESS could still have been alive — requeue
    is forbidden (exactly-once). Returns True (safe to requeue) when:
      - no session factory / claim / org (fail-open to the normal path), or
      - the dispatch state carries NO ``script_executing`` lease (no process
        could have been alive for this claim).
    Returns False (DO NOT requeue) only when a live script lease exists.
    NEVER auto-retries a stale claim — the caller escalates to needs-human.
    """
    if session_factory is None or not claim_token:
        return True
    org_uuid: uuid.UUID | None = None
    try:
        org_uuid = uuid.UUID(str(org_id)) if org_id else None
    except (TypeError, ValueError):
        org_uuid = None
    if org_uuid is None:
        return True
    try:
        from sqlalchemy import text as _sql_text

        from farna.db.rls import set_rls_execution_context, set_rls_org

        async with session_factory() as session, session.begin():
            await set_rls_org(session, org_uuid)
            await set_rls_execution_context(session)
            row = (
                await session.execute(
                    _sql_text("SELECT sandbox_dispatch_state FROM runs WHERE id=:rid AND organisation_id=:oid AND claim_token=:tok"),
                    {"rid": run_id, "oid": str(org_uuid), "tok": claim_token},
                )
            ).fetchone()
            if row is None:
                return True
            raw = row[0]
            if not raw:
                return True
            try:
                state = json.loads(raw)
            except (TypeError, ValueError):
                return True
            return not (isinstance(state, dict) and state.get("state") == "script_executing")
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "script.lease_probe_failed",
            extra={"run_id": str(run_id), "exc_type": "probe"},
            exc_info=True,
        )
        # Fail-closed on a probe error: a script-mode run whose lease we cannot
        # read is NEVER requeued (a hidden live process would double-execute).
        return False


def _resolve_post_node_eval_target(
    node_id: str,
    envelope: dict[str, Any],
    node_type_map: dict[str, str] | None,
) -> Any:
    """Resolve the eval target for a completed node envelope.

    Returns the node's CONTRACT output — the same pure return users see as the
    node return — so evals validate what the agent actually produced, not the
    telemetry-style outer ``output`` envelope. For a sandbox_agent that means
    ``artifacts[0].output.output_json`` (which carries ``pr_url`` /
    ``changed_files``). Unknown node types keep the legacy ``envelope["output"]``
    read, falling back to the whole envelope if it is not a dict — preserving
    historical behaviour for graphs without a resolved node type.
    """
    node_type = (node_type_map or {}).get(node_id) or DEFAULT_NODE_TYPE
    found, contract_output = _ref("resolve_node_contract_output")(envelope, node_type)
    if found:
        return contract_output
    if node_type in SPLITTABLE_NODE_TYPES:
        # Splittable type but no dict contract output (e.g. a sandbox_agent
        # that produced no output_json) — fail closed on the whole envelope
        # rather than silently validating telemetry.
        _log.debug(
            "post_node_eval.missing_contract_output",
            extra={"node_id": node_id, "node_type": node_type},
        )
        return envelope
    inner_output = envelope.get("output")
    if isinstance(inner_output, dict):
        return inner_output
    return envelope


async def _apply_work_intact(
    session: AsyncSession,
    run_id: uuid.UUID,
    work_intact: bool,
    *,
    claim_token: str | None,
) -> None:
    """Fenced UPDATE of ``runs.work_intact`` — mirrors ``update_run_status``
    fencing (F3a): a superseded executor's token no longer matches and the
    write is a no-op, so it cannot stamp work_intact on a successor's run.
    Runs INSIDE the caller's terminalization transaction.

    ``run_id`` is bound with the ``Uuid`` type so the raw SQL matches the
    stored id on every backend (SQLite stores the 32-hex form, not the
    dashed ``str(uuid)``).
    """
    if claim_token is None:
        await session.execute(
            text("UPDATE runs SET work_intact = :wi WHERE id = :rid").bindparams(
                bindparam("rid", type_=Uuid()),
                bindparam("wi", type_=Boolean()),
            ),
            {"wi": work_intact, "rid": run_id},
        )
        return
    await session.execute(
        text("UPDATE runs SET work_intact = :wi WHERE id = :rid AND claim_token = :tok").bindparams(
            bindparam("rid", type_=Uuid()),
            bindparam("wi", type_=Boolean()),
        ),
        {"wi": work_intact, "rid": run_id, "tok": claim_token},
    )


async def _reclassify_after_work_intact(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Refresh + re-persist the classification record
    after the ``work_intact`` write so the record carries the real value.

    ``finalize_cost`` triggers the terminal status write → inline classify
    BEFORE ``_apply_work_intact`` runs, so the record persists
    ``work_intact=None`` for every executor-terminalized run — and the
    reconciliation sweep SKIPS already-classified rows, so it never corrects
    them. This is the only path that fixes the executor-written records.

    Runs INSIDE the caller's terminalization transaction, AFTER
    ``_apply_work_intact`` succeeded. The re-read forces a real row load
    (``populate_existing=True``) because the work_intact write bypassed the ORM
    identity map (a raw UPDATE); ``classify_and_persist_run`` then recomputes
    the verdict from the fresh row and upserts it. Best-effort and NEVER
    raises — mirrors the ``work_intact.write_failed`` wrapper so a classify or
    persist failure can never block or fail terminalization.
    """
    try:
        from farna.core.pipeline_engine.classify import classify_and_persist_run

        run = await session.get(Run, run_id, populate_existing=True)
        if run is not None:
            await classify_and_persist_run(session, run)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("work_intact.classify_refresh_failed", extra={"run_id": str(run_id)})


async def _apply_work_intact_best_effort(
    session: AsyncSession,
    run_id: uuid.UUID,
    work_intact: bool | None,
    *,
    claim_token: str | None,
) -> None:
    """Best-effort persist of ``work_intact`` inside the finalisation transaction.

    ``None`` means "not computed" — nothing to persist. A non-cancelled write
    failure is logged and swallowed so the finalisation still completes (the
    reclassify step, which depends on a successful write, is then skipped);
    ``CancelledError`` propagates so the ownership layer's retry owns it.
    """
    if work_intact is None:
        return
    try:
        await _ref("_apply_work_intact")(session, run_id, work_intact, claim_token=claim_token)
        # finalize_cost's inline classify ran BEFORE this
        # write (work_intact still NULL at classify time). Re-persist the
        # classification with the real value — the sweep skips already-classified
        # rows, so this is the only correction for executor-terminalized runs.
        await _ref("_reclassify_after_work_intact")(session, run_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("work_intact.write_failed", extra={"run_id": str(run_id)})


async def org_sandbox_capacity_free(
    session: AsyncSession,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
) -> bool:
    """True when the org's sandbox cap (if configured) can admit *run_id*.

    Used by HITL routes as a PRE-approval check: the gate decision must not be
    committed when the org is at sandbox capacity. Fail-open — any error reads
    as ``True`` (admit) with a warning, never raises.

    D8: lock-free by contract — the resume path's advisory-lock gate
    (``_enforce_resume_sandbox_capacity``) is the enforcement point; taking
    the advisory lock here would invert the uniform row→advisory ordering
    (this pre-check holds no run row). Flag-on converges onto the shared
    ``resolve_runner_capacity_decision`` reader (the narrowed running-only
    marker population, tier-scoped Docker default, cap-None short-circuit);
    flag-off keeps the pre-D8 graph-based count and ``enforced_cap``.

    E2B carve-out omission (D8 qa F14, deliberate — mirrored by
    ``_enforce_resume_sandbox_capacity``): this pre-check does not model the
    dispatch gate's e2b skip. With the Docker-tier default active it counts
    host-resource markers only, so an org whose ONLY saturation is e2b
    markers reads as capacity-free here anyway; an org saturated on
    host-resource markers conservatively blocks the approval even though the
    dispatch gate would admit an e2b re-dispatch. An approval that waits for
    host-resource capacity is never lost work.
    """
    try:
        from farna.settings import get_settings

        flag_on = get_settings().runner_capacity_gate_enabled
        run = await _ref("get_run")(session, run_id)
        if run is None or run.snapshot_id is None:
            return True
        snap_result = await session.execute(select(PipelineSnapshot.graph_json).where(PipelineSnapshot.id == run.snapshot_id))
        graph_json = snap_result.scalar_one_or_none()
        if not _ref("_graph_contains_sandbox_agent")(graph_json):
            return True
        if flag_on:
            from farna.core.runner_capacity import resolve_runner_capacity_decision

            decision = await resolve_runner_capacity_decision(session, org_id, exclude_run_id=run_id)
            return decision.cap is None or decision.active < decision.cap
        cap: int | None = (await _ref("get_sandbox_concurrency_limit")(session, org_id)).enforced_cap
        if cap is None:
            return True
        active: int = await _ref("count_active_sandbox_runs_for_org")(session, org_id, exclude_run_id=run_id)
        return active < cap
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "hitl.sandbox_capacity_check_failed",
            extra={"org_id": str(org_id), "run_id": str(run_id)},
            exc_info=True,
        )
        return True


def _compute_otel_run_context(
    config: dict[str, Any],
    bridge: Any,
) -> tuple[str | None, Any | None]:
    """Compute the run's deterministic OTel trace id and root span.

    The root span is created via the bridge (spans created by the bridge
    inherit it). The ``context_api.attach`` is deferred to the caller so the
    ``context_api.detach`` in the caller's ``finally`` stays balanced with the
    attach.
    """
    thread_id = (config.get("configurable") or {}).get("thread_id")
    run_trace_id = trace_id_for_thread(thread_id) if thread_id else None
    run_root_span = None
    if thread_id:
        run_root_span = bridge.start_run_root(thread_id)
    return run_trace_id, run_root_span


def _accumulate_node_token_usage(
    node_name: str,
    token_usage: dict[str, Any],
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage for a single node and enforce the per-node budget."""
    node_data = node_token_usage.setdefault(node_name, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    pt = token_usage.get("input_tokens", token_usage.get("prompt_tokens", 0)) or 0
    ct = token_usage.get("output_tokens", token_usage.get("completion_tokens", 0)) or 0
    tt = token_usage.get("total_tokens", 0) or 0
    node_data["input_tokens"] += pt
    node_data["output_tokens"] += ct
    node_data["total_tokens"] += tt
    if guard is not None:
        guard.record_tokens(tt)
    if node_token_budgets is not None:
        node_budget = node_token_budgets.get(node_name)
        if node_budget is not None and node_data["total_tokens"] > node_budget:
            raise RunawayRunError("token_budget", node_data["total_tokens"], node_budget)


def _extract_chat_model_token_usage(output: Any) -> dict[str, Any]:
    """Extract token usage from a chat model end event output."""
    usage_metadata = getattr(output, "usage_metadata", None) if isinstance(output, BaseMessage) else None
    if usage_metadata is not None:
        return {
            "input_tokens": usage_metadata.get("input_tokens", 0) or 0,
            "output_tokens": usage_metadata.get("output_tokens", 0) or 0,
            "total_tokens": usage_metadata.get("total_tokens", 0) or 0,
        }
    if isinstance(output, dict):
        # Legacy fallback: llm_output.token_usage
        llm_output = output.get("llm_output", {})
        return llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}
    return {}


def _extract_llm_token_usage(output: Any) -> dict[str, Any]:
    """Extract token usage from an llm end event output (legacy interface)."""
    llm_output = output.get("llm_output", {}) if isinstance(output, dict) else {}
    return llm_output.get("token_usage", {}) if isinstance(llm_output, dict) else {}


def _accumulate_chat_model_tokens(
    lg_event: Any,
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage from an ``on_chat_model_end`` event.

    Reads ``usage_metadata`` (with the legacy ``llm_output.token_usage``
    fallback), records cumulative tokens on the guard, and enforces the
    per-node token budget (raising ``RunawayRunError`` on breach).
    """
    metadata = lg_event.get("metadata") or {}
    node_name = metadata.get("langgraph_node")
    if node_name:
        data = lg_event.get("data", {})
        output = data.get("output", {}) if isinstance(data, dict) else {}
        token_usage = _extract_chat_model_token_usage(output)
        if isinstance(token_usage, dict):
            _accumulate_node_token_usage(node_name, token_usage, node_token_usage, guard, node_token_budgets)


def _accumulate_llm_tokens(
    lg_event: Any,
    node_token_usage: dict[str, dict[str, int]],
    guard: RunawayGuard | None,
    node_token_budgets: dict[str, int] | None,
) -> None:
    """Accumulate token usage from an ``on_llm_end`` event (legacy interface).

    Reads ``llm_output.token_usage``, records cumulative tokens on the guard,
    and enforces the per-node token budget (raising ``RunawayRunError`` on
    breach).
    """
    metadata = lg_event.get("metadata") or {}
    node_name = metadata.get("langgraph_node")
    if node_name:
        data = lg_event.get("data", {})
        output = data.get("output", {}) if isinstance(data, dict) else {}
        token_usage = _extract_llm_token_usage(output)
        if isinstance(token_usage, dict):
            _accumulate_node_token_usage(node_name, token_usage, node_token_usage, guard, node_token_budgets)


def _connector_scope_agent_ids(graph_json: dict[str, Any]) -> list[uuid.UUID]:
    """Distinct Agent ids referenced by the graph's nodes (for the fetch scope).

    Non-dict nodes and unparseable agent ids are skipped — a malformed node
    must never abort hub construction.
    """
    agent_ids: list[uuid.UUID] = []
    for node in graph_json.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        agent_id = node.get("agent_id")
        if agent_id:
            try:
                agent_ids.append(uuid.UUID(str(agent_id)))
            except (TypeError, ValueError):
                continue
    return agent_ids


def _all_capacities_ok(pipeline_ok: bool, sandbox_ok: bool, run_ok: bool) -> bool:
    """All three capacity gates (pipeline / sandbox / org-run) admit the run."""
    return pipeline_ok and sandbox_ok and run_ok


def _interrupt_gate_payload(interrupts: Any) -> dict[str, Any]:
    """The gate payload carried by the first LangGraph interrupt (or ``{}``)."""
    first_interrupt = interrupts[0] if interrupts else None
    value = getattr(first_interrupt, "value", None)
    return value if isinstance(value, dict) else {}


def _interrupt_required_team_id(gate_payload: dict[str, Any]) -> uuid.UUID | None:
    """Parse the interrupt payload's ``required_team_id`` (absent -> ``None``)."""
    required_team_id_str = gate_payload.get("required_team_id")
    return uuid.UUID(required_team_id_str) if required_team_id_str else None


def _interrupt_condition_result(gate_payload: dict[str, Any]) -> dict[str, Any] | None:
    """The interrupt payload's ``condition_result`` member, or None.

    The gate node evaluates the JMESPath condition at fire time and stamps the
    MATCHED value ({"expression", "value"}, redacted + bounded) into the
    payload; the briefing capture stores it as PRIMARY evidence. None for
    gates without a condition and for legacy payloads — the briefing then
    falls back to the supplementary regex-extracted artifacts unchanged.
    """
    result = gate_payload.get("condition_result")
    return result if isinstance(result, dict) else None


def _interrupt_subject(gate_payload: dict[str, Any]) -> str | None:
    """The interrupt payload's ``subject`` member, or None.

    The gate node resolves ``subject_path`` against the run state at fire
    time and stamps the serialised value into the payload; the briefing
    capture stores it as the reviewer's primary focus. None for gates
    without a ``subject_path`` or when resolution failed.
    """
    result = gate_payload.get("subject")
    return result if isinstance(result, str) else None


def _interrupt_subject_parent(gate_payload: dict[str, Any]) -> dict[str, Any] | None:
    """The interrupt payload's ``subject_parent`` member, or None.

    The gate node resolves the JMESPath prefix of ``subject_path`` to find
    the container dict holding the subject value; the briefing capture
    stores it so the frontend can reconstruct a shape-preserving
    ``modified_output``. None for gates without a ``subject_path`` or when
    the parent cannot be resolved.
    """
    result = gate_payload.get("subject_parent")
    return result if isinstance(result, dict) else None


def _interrupt_subject_leaf_key(gate_payload: dict[str, Any]) -> str | None:
    """The interrupt payload's ``subject_leaf_key`` member, or None.

    The leaf key within ``subject_parent`` whose value is the subject.
    """
    result = gate_payload.get("subject_leaf_key")
    return result if isinstance(result, str) else None


def _clamp_to_ceiling(base_level: AutonomyLevel, ceiling: str | None) -> str:
    """Clamp ``base_level`` to a raw ceiling string, by rank.

    Levels are not lexicographic, so the clamp compares ``autonomy_level_rank``.
    An absent or unparseable ceiling imposes no constraint — the base wins.
    """
    if isinstance(ceiling, str) and ceiling:
        try:
            ceiling_level = AutonomyLevel(ceiling)
        except ValueError:
            ceiling_level = None
        if ceiling_level is not None and autonomy_level_rank(ceiling_level) < autonomy_level_rank(base_level):
            return ceiling_level.value
    return base_level.value


def _index_dtos_by_eval_id(eval_defs_by_node: dict[str, list[EvalDefDTO]]) -> dict[str, EvalDefDTO]:
    """Flatten a per-node DTO universe into an id-indexed lookup.

    Extracted from ``PipelineExecutor._overlay_frozen_gated_eval_modes`` for
    the cognitive-complexity budget (S3776); behaviour unchanged. Last write
    wins on duplicate ids, exactly as the original inline double loop.
    """
    by_eval_id: dict[str, EvalDefDTO] = {}
    for dtos in eval_defs_by_node.values():
        for dto in dtos:
            by_eval_id[str(dto.id)] = dto
    return by_eval_id


def _frozen_eval_mode_entries(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The ``gated_eval_modes`` stamp entries of one fired-gate config.

    Extracted from ``PipelineExecutor._overlay_frozen_gated_eval_modes`` for
    the cognitive-complexity budget (S3776); behaviour unchanged — a config
    that is not a dict, or whose stamp key is not a list, contributes no
    entries, and non-dict entries are dropped.
    """
    if not isinstance(config, dict):
        return []
    entries = config.get(_GATED_EVAL_MODES_KEY)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _apply_frozen_eval_mode_entry(entry: dict[str, Any], by_eval_id: dict[str, EvalDefDTO]) -> bool:
    """Apply ONE fire-time evaluation-mode stamp to its target DTO.

    Extracted from ``PipelineExecutor._overlay_frozen_gated_eval_modes`` for
    the cognitive-complexity budget (S3776); behaviour unchanged. Returns
    ``True`` only when a stamp was applied: the entry must carry a known
    ``eval_id`` and a closed-vocabulary ``evaluation_mode``, the DTO must
    exist and be gate-owned, and the stamp's ``policy_gate_id`` must match
    the DTO's live governing gate (operator control / gate identity win over
    the freeze). Mutates ``target_dto`` in place on success.
    """
    eval_id = entry.get("eval_id")
    mode = entry.get("evaluation_mode")
    if not eval_id or mode not in ("automatic", "human"):
        return False
    target_dto = by_eval_id.get(str(eval_id))
    if target_dto is None or target_dto.policy_gate_id is None:
        return False
    if str(target_dto.policy_gate_id) != str(entry.get("policy_gate_id")):
        return False
    target_dto.policy_gate_evaluation_mode = str(mode)
    return True
