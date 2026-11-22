"""Run listing, triggering, status, output, evals and cancellation MCP tools.

Also carries the ``modulo://pipelines/{pipeline_id}/runs`` and
``modulo://runs/{run_id}`` resources.

Extracted VERBATIM from ``farna/api/mcp_server.py`` as a behaviour-preserving
split of that module (structural hardening). Nothing here is reimplemented:
the decorators, signatures, defaults and docstrings are byte-identical.

Registration: importing this module decorates the handlers with
``@mcp.tool`` / ``@mcp.resource`` on the SAME ``FastMCP`` app instance created
in ``farna.api.mcp_server``, so the advertised tool/resource schema is
unchanged. ``mcp_server`` imports this module at the end of its own body,
after ``mcp`` and the shared spine exist, and re-exports the names the rest
of the tree still reaches through it, so ``from farna.api.mcp_server import
<name>`` keeps working.

Shared helpers resolve lazily through ``mcp_tools._patch_surface`` so
``mock.patch("farna.api.mcp_server.<name>")`` still intercepts the call made
from this module (the test suite's stated patch contract).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MCP_COST_ROLLUP_ZERO,
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_ERROR_TOKEN_REVOKED,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    _TEAM_SCOPE_ERROR,
    _check_node_allowed_tools,
    _ctx_team_id_val,
    _ctx_user_id,
    _format_breakdown_line,
    _parse_optional_uuid,
    _quantize_mcp_cost_rollup,
    _resource_node_scope_error,
    _sanitize_cost_breakdown,
    _serialize_run_evals,
    _team_scope_error,
    _team_scope_error_str,
    _tool_auth_error,
    _tool_exception_error,
    _trigger_pipeline_client_key,
    _trigger_pipeline_rate_allowed,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    _log,
    _parse_uuid_param,
    _pipeline_owner_team_id,
    _run_owner_team_id,
    _session,
    _team_scoped_key_mismatch,
    dispatch_run,
    finalize_cancelled_run,
    get_pipeline,
    get_run,
    map_legacy_code,
    patchable_local,
    present_error,
    read_run_blobs,
    validate_current_auth,
)
from farna.core.exceptions import OrgDeletedError, PipelineNotRunnableError, SnapshotLockNotAvailableError
from farna.core.mcp.scope_validator import MCPAuthorizationError
from farna.core.run_provenance import run_provenance_fields
from farna.db.capacity import StorageExhaustedError
from farna.db.crud.run import WorkItemRefsRequiredError
from farna.db.crud.run_node_outputs import RunBlobs
from farna.db.models.run import CANCEL_REASON_AGENT_REQUESTED, TERMINAL_STATUSES, Run
from farna.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of


def _mcp_run_item(r: Any, child_rollup: dict[Any, tuple[Any, int]]) -> dict[str, Any]:
    child_cost, child_count = child_rollup.get(r.id, (_MCP_COST_ROLLUP_ZERO, 0))
    child_cost = _quantize_mcp_cost_rollup(child_cost)
    own_cost = r.total_cost_usd if r.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
    _error_code, error_detail = present_error(r.error_code, r.error_detail, limit=200)
    return {
        "id": str(r.id),
        "pipeline_id": str(r.pipeline_id),
        "status": r.status,
        "trigger_type": r.trigger_type,
        # FAR-1141 / ADR-042: a dispatched run must never read
        # indistinguishably from one Modulo executed. Composed from the ONE
        # shared serializer (FAR-1565); a row loaded without the column reads
        # NULL, so provenance never breaks the read.
        **run_provenance_fields(r),
        "run_number": r.run_number,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "error_code": _error_code,
        "error_detail": error_detail,
        "total_cost_usd": float(r.total_cost_usd) if r.total_cost_usd is not None else None,
        "child_runs_cost_usd": float(child_cost),
        "child_runs_count": child_count,
        "aggregate_cost_usd": float(_quantize_mcp_cost_rollup(own_cost + child_cost)),
    }


@mcp.tool(
    description="List pipeline runs with filtering and cursor-based pagination.",
)
@_RETRY_DB
async def list_runs(
    pipeline_id: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        return await _list_runs_impl(pipeline_id, status, cursor, limit)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_runs failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_runs failed")
        return _tool_exception_error("Failed to list runs", exc, "mcp.list_runs")


async def _list_runs_impl(
    pipeline_id: str | None,
    status: str | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("list_runs")
    from farna.db.crud.run import get_child_run_rollup
    from farna.db.crud.run import list_runs as db_list_runs

    org_id = _ctx_org_id_val()
    # FAR-1540: a malformed filter id is a client error (``invalid_id``), not a
    # generic ``internal_error`` from the wrapper's ``except Exception`` arm.
    pid, pid_err = _parse_optional_uuid(pipeline_id, "pipeline_id")
    if pid_err is not None:
        return pid_err
    async with _session(org_id) as s:
        if pid is not None:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", str(pid))
        # ``run.list`` is viewer-reachable: a viewer (JWT/OAuth, no team-scoped
        # credential) has ``_ctx_team_id`` None, so without the membership
        # clause it would enumerate every team's runs. Fail closed on a missing
        # account context rather than widening to an org-wide read.
        viewer_account_id = _ctx_user_id_val() if _ctx_role_val() == "viewer" else None
        result = await db_list_runs(
            s,
            organisation_id=org_id,
            pipeline_id=pid,
            status=status,
            page=1,
            page_size=limit,
            cursor=cursor,
            team_id=_ctx_team_id_val(),
            account_id=viewer_account_id,
        )
        # Child-run cost+count rollup: ONE GROUP BY query for the whole
        # page, joined in Python — never a per-row aggregate (avoids N+1).
        run_ids = [r.id for r in result.items]
        child_rollup = await get_child_run_rollup(s, run_ids) if run_ids else {}
    items = [_mcp_run_item(r, child_rollup) for r in result.items]
    return {
        "items": items,
        "total": result.total,
        "next_cursor": result.next_cursor,
        "has_more": result.has_more,
    }


def _trigger_pipeline_validate_id(pipeline_id: str) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    return _parse_uuid_param(pipeline_id, "pipeline_id")


async def _create_manual_run(
    s: AsyncSession,
    org_id: uuid.UUID,
    pid: uuid.UUID,
    pipeline_id: str,
    payload: dict[str, Any],
    work_item_refs: list[dict[str, Any]] | None = None,
) -> tuple[uuid.UUID | None, str | None, dict[str, Any] | None]:
    from farna.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
    from farna.db.crud.run import create_run

    uid = _ctx_user_id_val()
    pipeline = await get_pipeline(s, pid, organisation_id=org_id)
    if pipeline is None:
        return None, None, {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
    if _team_scoped_key_mismatch(pipeline.owner_team_id):
        return None, None, _team_scope_error("pipeline", pipeline_id)
    snapshot = await create_snapshot_from_live_graph(s, pipeline_id=pid, account_id=uid)
    if snapshot is None:
        return None, None, {"error": "snapshot_failed", "pipeline_id": pipeline_id}
    if not snapshot.graph_json or not snapshot.graph_json.get("nodes"):
        return (
            None,
            None,
            {
                "error": "validation_failed",
                "detail": "Pipeline graph has no nodes — cannot trigger run",
            },
        )
    run = await create_run(
        s,
        org_id=org_id,
        pipeline_id=pid,
        snapshot_id=snapshot.id,
        trigger_type="manual",
        input_payload=payload,
        # FAR-794 slice 2a: MCP caller-supplied refs; provenance is
        # engine-assigned (caller) inside create_run — the wire source is
        # never trusted.
        work_item_refs=work_item_refs,
        # FAR-620 run attribution: manual MCP-triggered runs are stamped with
        # the CALLER's account (the account of the authenticating credential).
        # This enables the reject→correction guardrail dispatch
        # (feedback_manager) which requires a non-null run.account_id, and
        # keeps manual runs attributable no matter which surface triggered
        # them. Webhook/cron/agent_signal child runs stay legitimately NULL.
        account_id=uid,
    )
    return run.id, run.langgraph_thread_id, None


async def _trigger_pipeline_impl(
    pipeline_id: str,
    input_payload: dict[str, Any] | None,
    work_item_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("trigger_pipeline")
    if not await _trigger_pipeline_rate_allowed():
        _log.warning(
            "ratelimit.trigger_pipeline_exceeded",
            extra={"client_key": _trigger_pipeline_client_key()},
        )
        return {"error": "rate_limited", "detail": "Rate limit exceeded for trigger_pipeline (60/min)"}
    org_id = _ctx_org_id_val()
    pid, id_err = _trigger_pipeline_validate_id(pipeline_id)
    if id_err:
        return id_err
    if pid is None:
        raise RuntimeError("_trigger_pipeline_impl: validate returned no error and no pipeline id")
    payload = input_payload or {}

    async with _session(org_id) as s:
        run_id, thread_id, run_err = await _create_manual_run(s, org_id, pid, pipeline_id, payload, work_item_refs)
    if run_err:
        return run_err
    if run_id is None:
        raise RuntimeError("_trigger_pipeline_impl: manual run creation returned no error and no run id")

    await dispatch_run(str(run_id), str(org_id), queue="runs")

    return {
        "run_id": str(run_id),
        "status": "pending",
        "langgraph_thread_id": thread_id,
    }


@mcp.tool(
    description=(
        "Fire a pipeline run and return immediately with run_id. Poll get_run_status to track progress. "
        "Optional work_item_refs: a list of {kind, ref} entries recording the work items this run serves "
        '(e.g. [{"kind": "linear", "ref": "FAR-123"}]); provenance is engine-assigned (caller) and any '
        "wire source value is ignored. A pipeline that requires work-item refs returns the "
        "work_item_refs_required error when none are supplied."
    )
)
@mcp_audited("run_triggered", "run", fail_closed=False)
@_RETRY_DB
async def trigger_pipeline(
    pipeline_id: str,
    input_payload: dict[str, Any] | None = None,
    work_item_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        return await _trigger_pipeline_impl(pipeline_id, input_payload, work_item_refs)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except WorkItemRefsRequiredError as exc:
        # FAR-794 slice 2a: the pipeline declares work_item_refs_required and
        # the trigger supplied none — a caller-fixable validation failure.
        _log.info("trigger_pipeline work_item_refs_required pipeline=%s", exc.pipeline_id)
        return {
            "error": "work_item_refs_required",
            "detail": "This pipeline requires work_item_refs but none were supplied",
        }
    except SnapshotLockNotAvailableError:
        from farna.db.crud.pipeline_snapshot import SNAPSHOT_LOCK_ATTEMPTS

        _log.warning(
            "trigger_pipeline failed — snapshot lock not available for pipeline %s after %s attempts (FAR-527)",
            pipeline_id,
            SNAPSHOT_LOCK_ATTEMPTS,
        )
        return {
            "error": "snapshot_lock_busy",
            "detail": (f"Pipeline snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts — retry the trigger"),
        }
    except OrgDeletedError as exc:
        _log.exception("trigger_pipeline failed — organisation deleted or missing")
        if exc.deleted:
            return {"error": "org_deleted", "detail": f"Organisation {exc.org_id} is deleted"}
        return {"error": "org_not_found", "detail": f"Organisation {exc.org_id} not found"}
    except PipelineNotRunnableError as exc:
        # FAR-1528: a disabled pipeline must never return the generic tool
        # error — surface the refusing lifecycle state as a structured,
        # branchable envelope (caught at the tool level, exactly like
        # OrgDeletedError, so the transaction unwinds and the snapshot created
        # above rolls back with the refusal). FAR-1530's Paused state arrives
        # on the same envelope with state="paused".
        _log.warning(
            "trigger_pipeline pipeline_not_runnable pipeline=%s state=%s",
            exc.pipeline_id,
            exc.state,
        )
        return {
            "error": "pipeline_not_runnable",
            "state": exc.state,
            "pipeline_id": pipeline_id,
            "detail": f"Pipeline {pipeline_id} is {exc.state} and cannot start a run",
        }
    except StorageExhaustedError as exc:
        _log.warning("trigger_pipeline refused — storage exhausted (FAR-426)")
        return {"error": "storage_exhausted", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("trigger_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("trigger_pipeline failed")
        return _tool_exception_error("Failed to trigger pipeline", exc, "mcp.trigger_pipeline")


async def _load_run_for_status(s: AsyncSession, rid: uuid.UUID, org_id: uuid.UUID) -> Any | None:
    # Defence-in-depth org predicate (FAR-328): the MCP session already applies
    # RLS via ``_session(org_id)``; the explicit filter makes cross-tenant
    # access impossible even if RLS were misconfigured.
    run = await get_run(s, rid, organisation_id=org_id)
    if run is None:
        return None
    # The run carries its own owner_team_id (snapshot at creation) — that
    # is the source of truth, not the pipeline's current team assignment.
    # Legacy runs with a NULL stamp fall back to the pipeline owner.
    run_owner_team_id = await _run_owner_team_id(s, run)
    if _team_scoped_key_mismatch(run_owner_team_id):
        return _TEAM_SCOPE_ERROR
    return run


def _run_status_base(run: Run) -> dict[str, Any]:
    # FAR-1862: share the REST serialiser's defensive coercions rather than a
    # second copy (a MagicMock column value must degrade to None, never fail
    # the response).
    from farna.api.routes.runs import _int_or_none, _iso_or_none, _str_or_none

    result: dict[str, Any] = {
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "status": run.status,
        "trigger_type": run.trigger_type,
        # FAR-1141 / ADR-042: ``get_run_status`` is a claim-ready surface, so
        # it carries the run's execution origin ('dispatched' / NULL) exactly
        # like the REST detail — composed from the ONE shared serializer
        # (FAR-1565), never a hand-rolled copy.
        **run_provenance_fields(run),
        "created_at": run.created_at.isoformat(),
        # FAR-1862: the dispatch-state columns dispatcher_reconcile's recovery
        # decisions are computed from. Present on the BASE response (not only
        # detail) so a run whose status has not moved is diagnosable from a
        # plain ``get_run_status`` — the 2026-10-16 incident saw two runs sit
        # ``pending`` for 22h with no read surface exposing WHY. Additive and
        # unmasked: operational state, never a credential. Coercion is shared
        # with the REST serialiser (no second copy to drift).
        "dispatcher": _str_or_none(getattr(run, "dispatcher", None)),
        "dispatched_at": _iso_or_none(getattr(run, "dispatched_at", None)),
        "dispatch_phase": _str_or_none(getattr(run, "dispatch_phase", None)),
        "claim_count": _int_or_none(getattr(run, "claim_count", None)),
        "enqueue_failed_at": _iso_or_none(getattr(run, "enqueue_failed_at", None)),
    }
    if run.started_at:
        result["started_at"] = run.started_at.isoformat()
    if run.completed_at:
        result["completed_at"] = run.completed_at.isoformat()
    if run.error_code:
        result["error_code"] = map_legacy_code(run.error_code)
    if run.error_detail is not None:
        _, error_detail = present_error(run.error_code, run.error_detail, limit=5000)
        result["error_detail"] = error_detail
    return result


# FAR-1575: bound the MCP run-detail ``input_payload`` projection. REST carries
# the full masked payload; the MCP tool response must be size-bounded so a
# pathological input (a huge pasted prompt, a 10k-item list, a deeply nested
# blob) cannot produce an unbounded response. Three caps apply:
#   * each STRING is sliced to at most 8_000 chars (marker within the cap, via
#     the shared ``slice_with_marker`` helper — no second marker vocabulary);
#   * the WHOLE payload shares one budget of 500 dict entries / list items, so
#     nested collections cannot multiply the cap;
#   * nesting deeper than 20 levels is replaced by the truncation marker
#     (mirrors ``_mask_output_value``'s own depth guard).
# The bound runs AFTER masking: truncating first could strand a partial secret,
# and masking an already-truncated secret can miss it.
_MCP_INPUT_PAYLOAD_MAX_STRING_CHARS = 8_000
_MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS = 500
_MCP_INPUT_PAYLOAD_MAX_DEPTH = 20


@dataclass
class _McpPayloadBudget:
    """Mutable item budget shared across one input-payload projection."""

    items_remaining: int


def _bound_mcp_input_payload(
    value: Any,
    budget: _McpPayloadBudget,
    *,
    _depth: int = 0,
) -> Any:
    """Recursively size-bound an already-masked MCP ``input_payload`` value.

    Strings are sliced with the shared truncation marker, dict/list traversal
    draws from a single shared item budget, and nesting beyond
    :data:`_MCP_INPUT_PAYLOAD_MAX_DEPTH` collapses to the marker. A truncated
    collection carries the marker as its final element / a marker key.
    """
    from farna.core.pipeline_engine.hitl_context import TRUNCATION_MARKER, slice_with_marker

    if _depth > _MCP_INPUT_PAYLOAD_MAX_DEPTH:
        return TRUNCATION_MARKER
    if isinstance(value, str):
        return slice_with_marker(value, _MCP_INPUT_PAYLOAD_MAX_STRING_CHARS)
    if isinstance(value, dict):
        bounded: dict[Any, Any] = {}
        for key, item in value.items():
            if budget.items_remaining <= 0:
                omitted = len(value) - len(bounded)
                bounded[TRUNCATION_MARKER] = f"{omitted} item(s) omitted"
                break
            budget.items_remaining -= 1
            bounded[key] = _bound_mcp_input_payload(item, budget, _depth=_depth + 1)
        return bounded
    if isinstance(value, list):
        bounded_list: list[Any] = []
        for item in value:
            if budget.items_remaining <= 0:
                bounded_list.append(TRUNCATION_MARKER)
                break
            budget.items_remaining -= 1
            bounded_list.append(_bound_mcp_input_payload(item, budget, _depth=_depth + 1))
        return bounded_list
    return value


def _run_status_detail(run: Run, blobs: RunBlobs) -> dict[str, Any]:
    # FAR-1737: reuse the REST serialisers verbatim — the masked input payload
    # and the deadline-triage projection must be byte-identical to
    # ``GET /api/v1/runs/{run_id}``, never a hand-rolled copy of either.
    from farna.api.routes.runs import (
        _clamp_node_token_usage_union,
        _deadline_triage_from_markers,
        _mask_output_value,
        _run_attempt_count,
    )

    token_usage = _clamp_node_token_usage_union(run.node_token_usage or {})
    # FAR-583 read-switch: the blobs reassemble from run_node_outputs
    # (new-table-only reader) by the caller, inside the run-load transaction.
    outputs_json = blobs.outputs or {}
    telemetry_json = blobs.telemetry
    if not isinstance(telemetry_json, dict):
        telemetry_json = {}
    node_ids: set[str] = set()
    node_ids.update(token_usage.keys())
    node_ids.update(outputs_json.keys())
    node_ids.update(telemetry_json.keys())
    nodes = [_run_status_node(nid, token_usage, outputs_json, telemetry_json) for nid in sorted(node_ids)]
    result: dict[str, Any] = {"nodes": nodes}
    if run.cost_breakdown is not None:
        result["cost_breakdown"] = _sanitize_cost_breakdown(run.cost_breakdown)
    # FAR-1737: detail-parity with REST. Without these, a failed sandbox run
    # was unrecoverable over MCP — ``nodes: []`` carried neither the original
    # input (prompt) nor the per-attempt sandbox envelopes that explain why
    # the node produced no output. ``input_payload`` goes through the SAME
    # masker REST uses; ``deadline_triage`` is projected by the SAME helper
    # from the markers already reassembled for this detail read. Both are
    # detail-only: the base response never touches either (matches REST, where
    # the mask/projection sit behind the detail builder).
    # FAR-1575: mask FIRST, then size-bound — truncating before masking could
    # leave a partial secret, and masking a truncated secret can miss it. The
    # REST path keeps the full masked payload; only this MCP projection is
    # bounded.
    _raw_input_payload = getattr(run, "input_payload", None)
    if isinstance(_raw_input_payload, dict) and _raw_input_payload:
        result["input_payload"] = _bound_mcp_input_payload(
            _mask_output_value(_raw_input_payload),
            _McpPayloadBudget(_MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS),
        )
    else:
        result["input_payload"] = None
    # FAR-1886 B1: the run's attempt counter is DATA on every entry — taken
    # from the SAME run row the REST detail read uses, so the two surfaces'
    # values cannot diverge.
    result["deadline_triage"] = _deadline_triage_from_markers(
        getattr(blobs, "markers", None),
        run_attempt_count=_run_attempt_count(run),
    )
    return result


def _run_status_node(
    nid: str,
    token_usage: dict[str, Any],
    outputs_json: dict[str, Any],
    telemetry_json: dict[str, Any],
) -> dict[str, Any]:
    """Aggregate a single node's usage/status into its run-status dict entry."""
    usage = token_usage.get(nid, {})
    if not isinstance(usage, dict):
        usage = {}
    t_in = usage.get("input_tokens") or 0
    t_out = usage.get("output_tokens") or 0
    if nid in outputs_json:
        status = "completed"
    elif nid in telemetry_json:
        tel_entry = telemetry_json[nid]
        tel_status = tel_entry.get("status") if isinstance(tel_entry, dict) else None
        status = "failed" if tel_status == "failed" else "processed"
    else:
        status = "processed"
    node: dict[str, Any] = {
        "node_id": nid,
        "status": status,
        "input_tokens": t_in,
        "output_tokens": t_out,
        "total_tokens": usage.get("total_tokens") or (t_in + t_out),
        "cost_usd": usage.get("cost_usd", 0),
        "has_output": nid in outputs_json or nid in telemetry_json,
    }
    if usage.get("model_cost_display_usd") is not None:
        node["model_cost_display_usd"] = usage["model_cost_display_usd"]
    return node


async def _get_run_status_impl(run_id: str, detail: bool) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    # FAR-1976: this tool bypasses the full chokepoint, so it must still
    # enforce the node allowed_tools narrowing leg explicitly.
    _check_node_allowed_tools("get_run_status")
    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        run = await _load_run_for_status(s, rid, org_id)
        if run is _TEAM_SCOPE_ERROR:
            return _team_scope_error("run", run_id)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        # FAR-583 read-switch: reassemble the blobs INSIDE the run-load
        # transaction (one batched repo query, new-table-only reader);
        # the detail body is built after the session closes. Read only when
        # detail is requested (the base response never touches the blobs).
        blobs = await read_run_blobs(s, run_id=rid, organisation_id=org_id) if detail else None
    result = _run_status_base(run)
    if detail and blobs is not None:
        result.update(_run_status_detail(run, blobs))
    return result


@mcp.tool(
    description=(
        "Get current run status. Pass detail=true for a per-node breakdown, the "
        "masked run input_payload (the original run input, sensitive fields "
        "redacted), and deadline_triage[] — the retained triage envelopes "
        "(attempt_key, no_output_reason, sandbox_log_tail, raw_output_tail, "
        "raw_output_head, stdout_length, pr_url, run_attempt_count) that explain what the run's "
        "terminaliser observed. Sandbox nodes carry a per-attempt output/log "
        "tail and a non-gating pr_url — a PR URL observed in the retained tail "
        "(heuristic; not anchored to `gh pr create`); delivery_done stays "
        "sentinel-only; "
        "run-level entries (age gate, stranded pending, node-deadline "
        "reaper, worker handoff) carry no per-attempt tail. The "
        "input_payload is size-bounded: strings over 8000 characters and "
        "collections beyond 500 total entries/items are truncated with a "
        "marker."
    )
)
@_RETRY_DB
async def get_run_status(run_id: str, detail: bool = False) -> dict[str, Any]:
    """Get a run's status; ``detail=True`` adds nodes, the masked input and triage.

    The detail read's ``input_payload`` is masked with the same value-level
    masker the REST detail endpoint uses, then size-bounded so a pathological
    input cannot produce an unbounded response: each string is capped at
    ``_MCP_INPUT_PAYLOAD_MAX_STRING_CHARS`` (8,000) characters, and the whole
    payload shares a budget of ``_MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS`` (500)
    dict entries / list items. Capped strings and collections carry the shared
    truncation marker ``…(truncated)``. The base (``detail=False``) response is
    unchanged.
    """
    try:
        return await _get_run_status_impl(run_id, detail)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_status failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_run_status failed")
        return _tool_exception_error("Failed to get run status", exc, "mcp.get_run_status")


def _resolve_run_node_output(outputs: dict[str, Any], telemetry: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    """Resolve the pure node return, or a status summary for telemetry-only nodes."""
    from farna.core.node_output_split import node_return

    node_output = node_return(outputs, telemetry, node_id)
    if node_output is None and node_id in telemetry:
        node_meta = telemetry[node_id]
        if isinstance(node_meta, dict):
            node_output = {key: node_meta[key] for key in ("status", "summary") if key in node_meta}
    return cast("dict[str, Any] | None", node_output)


def _detect_masked_fields(masked: Any) -> list[str]:
    """Keys whose masked value contains the bullet mask character."""
    if not isinstance(masked, dict):
        return []
    return [k for k, v in masked.items() if isinstance(v, str) and "\u2022" in v]


@mcp.tool(
    description=("Get a specific node's output from a completed pipeline run. Sensitive fields (tokens, secrets, API keys, passwords, credentials) are masked in the response."),
)
@_RETRY_DB
async def get_run_output(run_id: str, node_id: str) -> dict[str, Any]:
    try:
        return await _get_run_output_impl(run_id, node_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_output failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_run_output failed")
        return _tool_exception_error("Failed to get node output", exc, "mcp.get_run_output")


async def _get_run_output_impl(run_id: str, node_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_run_output")
    from farna.api.routes.runs import _mask_output_value
    from farna.core.node_output_split import node_stderr_artifact, node_stdout_artifact

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        run = await get_run(s, rid, organisation_id=org_id)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        run_owner_team_id = await _run_owner_team_id(s, run)
        if _team_scoped_key_mismatch(run_owner_team_id):
            return _team_scope_error("run", run_id)
        # FAR-583 read-switch: reassemble the blobs INSIDE the run-load
        # transaction (one batched repo query, new-table-only reader).
        blobs = await read_run_blobs(s, run_id=rid, organisation_id=org_id)
    outputs = blobs.outputs or {}
    telemetry = blobs.telemetry
    if not isinstance(telemetry, dict):
        telemetry = {}
    node_output = _resolve_run_node_output(outputs, telemetry, node_id)
    if node_output is None:
        return {"error": "node_output_not_found", "run_id": run_id, "node_id": node_id}
    masked = _mask_output_value(node_output)
    masked_fields = _detect_masked_fields(masked)

    return {
        "node_id": node_id,
        "output": masked,
        "masked_fields": masked_fields,
        # FAR-811 pointer (dict) or None; the transcript body is never inlined.
        "stdout_artifact": node_stdout_artifact(telemetry, outputs, node_id),
        # FAR-879 pointer (dict) or None; stderr parity with stdout_artifact.
        "stderr_artifact": node_stderr_artifact(telemetry, outputs, node_id),
    }


@mcp.tool(
    description="Get eval results for a given run. Returns structured eval outcomes including pass/fail status, scores, and detailed feedback.",
)
@_RETRY_DB
async def get_run_evals(run_id: str) -> dict[str, Any]:
    try:
        return await _get_run_evals_impl(run_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_run_evals failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_run_evals failed")
        return _tool_exception_error("Failed to get run evals", exc, "mcp.get_run_evals")


async def _get_run_evals_impl(run_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_run_evals")
    from farna.db.crud.eval_run import get_run_evals as db_get_run_evals

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    async with _session(org_id) as s:
        run = await get_run(s, rid, organisation_id=org_id)  # defence-in-depth org predicate (FAR-328)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error("run", run_id)
        evals = await db_get_run_evals(s, rid)

    return {
        "run_id": run_id,
        "status": run.status,
        "evals": _serialize_run_evals(evals),
        "eval_count": len(evals),
    }


# Mirrors the REST ``max_length=255`` on the eval-definition name field so an
# oversized MCP-supplied name surfaces as ``invalid_name`` rather than a generic
# constraint conflict.


@mcp.tool(description="Cancel a running pipeline run.")
@mcp_audited("run_cancelled", "run", fail_closed=False)
@_RETRY_DB
async def cancel_run(run_id: str) -> dict[str, Any]:
    try:
        return await _cancel_run_impl(run_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("cancel_run failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("cancel_run failed")
        return _tool_exception_error("Failed to cancel run", exc, "mcp.cancel_run")


async def _cancel_run_impl(run_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("cancel_run")
    from farna.db.crud.row_lock import set_mutation_row_lock_timeout
    from farna.db.crud.run import get_run, request_cancellation

    org_id = _ctx_org_id_val()
    rid, rid_err = _parse_uuid_param(run_id, "run_id")
    if rid_err:
        return rid_err
    assert rid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        # FAR-1610: bound the hot ``runs`` row-lock wait BEFORE the first lock.
        # ``get_run`` is a plain read; ``request_cancellation`` takes the
        # ``SELECT ... FOR UPDATE``. Issued in the caller's transaction (the
        # shared CRUD function stays unbounded — FAR-1601), so a contended row
        # yields a bounded 55P03 mapped to a branchable ``lock_timeout`` result
        # below (mirroring ``_update_pipeline_graph_impl``) instead of the
        # ``_RETRY_DB`` retry multiplying the wait and then collapsing into a
        # generic ``database_unavailable``.
        await set_mutation_row_lock_timeout(s)
        run = await get_run(s, rid, organisation_id=org_id)  # defence-in-depth org predicate (FAR-328)
        if run is None:
            return {"error": "run_not_found", "run_id": run_id}
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error("run", run_id)
        if run.status in TERMINAL_STATUSES:
            detail = f"Run is already in terminal status: {run.status}"
            return {"error": "cannot_cancel", "run_id": str(run_id), "detail": detail}
        # PAUSED-then-cancelled class (awaiting_human/claimed/hitl_parked —
        # qa F10: parked runs are the awaiting_human class after expiry)
        # runs NO finalize (§4.2). A STREAMED running run cancelled
        # cross-process is routed through finalize_cost, re-reading the
        # STORED cumulative sets; a NEVER-PAUSED in-flight run has none on
        # THIS request path (cost_components.cancel_salvage_deferred log) —
        # its docket is NOT forfeited: after the executor settles,
        # pipeline_execution._salvage_cancelled_run_cost attributes
        # marker-provable spend from its persisted attempt marker
        # (FAR-1914 C2).
        was_paused = run.status in ("awaiting_human", "claimed", "hitl_parked")
        # FAR-1233: this is the AGENT-initiated cancel leg (the MCP tool), so
        # the reason distinguishes it from an operator clicking Cancel in the
        # UI. The acting account is read fail-open (absent -> the caller
        # degrades to the ``system`` sentinel instead of failing the tool).
        _actor = _ctx_user_id.get(None)
        try:
            run = await request_cancellation(
                s,
                rid,
                reason=CANCEL_REASON_AGENT_REQUESTED,
                actor=str(_actor) if _actor is not None else None,
            )
            if not was_paused:
                await finalize_cancelled_run(s, run_id=rid, org_id=org_id)
        except SQLAlchemyError as exc:
            # FAR-1610: 55P03 from the bounded row-lock wait is a busy-row
            # conflict, not a DB outage — surface the same branchable
            # ``lock_timeout`` code the graph-update tool returns (FAR-1361)
            # and stop ``_RETRY_DB`` from retrying the bounded wait 3x. Any
            # other SQLAlchemy error re-raises to the tool wrapper's ladder.
            if sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
                _log.warning("mcp.cancel_run.lock_timeout", extra={"run_id": str(run_id), "org_id": str(org_id)})
                return {
                    "error": "lock_timeout",
                    "run_id": run_id,
                    "detail": ("Timed out waiting for a lock on this run; another change is in progress. Re-issue the cancel once the other change completes."),
                }
            raise
    if run is None:
        return {"error": "run_not_found", "run_id": run_id}
    return {"run_id": run_id, "cancellation_requested": True}


def _format_run_line(r: Any, child_rollups: dict[Any, tuple[Any, int]], cost_breakdown: Any) -> str:
    """Render a single run row as a text line for MCP resources.

    ``cost_breakdown`` is passed in pre-loaded by the caller because it is a
    deferred column on the runs-list query (crud/run.py
    ``_RUNS_LIST_DEFERRED_COLUMNS``). Reading ``r.cost_breakdown`` here is never
    valid: the implicit lazy load attempts IO outside a greenlet and so raises
    ``MissingGreenlet`` while the session is open, and ``DetachedInstanceError``
    once it has closed. The caller loads it via an awaited query instead.
    """
    child_cost, child_count = child_rollups.get(r.id, (_MCP_COST_ROLLUP_ZERO, 0))
    own_cost = Decimal(str(r.total_cost_usd)) if r.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
    aggregate_cost = _quantize_mcp_cost_rollup(own_cost + child_cost)
    line = f"- Run {r.id} | status={r.status} | trigger={r.trigger_type} | created={r.created_at.isoformat()} | tokens={r.total_tokens or 0} | cost=${r.total_cost_usd or 0} | child_count={child_count} | child_cost=${child_cost} | aggregate_cost=${aggregate_cost}"
    if cost_breakdown is not None:
        breakdown = _sanitize_cost_breakdown(cost_breakdown)
        if breakdown:
            line += " | breakdown={" + ", ".join(_format_breakdown_line(e) for e in breakdown) + "}"
    return line


@mcp.resource("modulo://pipelines/{pipeline_id}/runs")
async def resource_pipeline_runs(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the list_runs tool — same node-scope gate.
    denied = _resource_node_scope_error("list_runs")
    if denied is not None:
        return denied
    from farna.db.crud.run import list_runs

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return f"error: Invalid UUID format: {pipeline_id}"
    async with _session(org_id) as s:
        pipeline = await get_pipeline(s, pid, organisation_id=org_id)
        if pipeline is None:
            return f"Pipeline {pipeline_id} not found."
        if _team_scoped_key_mismatch(pipeline.owner_team_id):
            return _team_scope_error_str("pipeline", pipeline_id)
        result = await list_runs(s, organisation_id=org_id, pipeline_id=pid, page=1, page_size=50, team_id=_ctx_team_id_val())
        # Child-run cost+count rollup: ONE GROUP BY query for the whole page,
        # joined in Python — never a per-row aggregate (avoids N+1).
        run_ids = [r.id for r in result.items]
        from farna.db.crud.run import get_child_run_rollup, get_run_cost_breakdowns

        child_rollups = await get_child_run_rollup(s, run_ids) if run_ids else {}
        # ``cost_breakdown`` is deferred by the list query. It MUST be loaded by
        # an awaited query — a plain ``r.cost_breakdown`` read would raise
        # ``MissingGreenlet`` under asyncio even here with the session open.
        # ONE query for the whole page (no N+1).
        cost_breakdowns = await get_run_cost_breakdowns(s, run_ids) if run_ids else {}

    if not result.items:
        return f"Pipeline '{pipeline.name}' has no runs."

    lines = [_format_run_line(r, child_rollups, cost_breakdowns.get(r.id)) for r in result.items]
    return f"Runs for pipeline {pipeline.name} ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://runs/{run_id}")
async def resource_run(run_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the get_run_status tool — same node-scope gate.
    denied = _resource_node_scope_error("get_run_status")
    if denied is not None:
        return denied
    org_id = _ctx_org_id_val()
    try:
        rid = uuid.UUID(run_id)
    except ValueError:
        return f"error: Invalid UUID format: {run_id}"
    async with _session(org_id) as s:
        run = await get_run(s, rid, organisation_id=org_id)  # defence-in-depth org predicate (FAR-328)
        if run is None:
            return f"Run {run_id} not found."
        if _team_scoped_key_mismatch(await _run_owner_team_id(s, run)):
            return _team_scope_error_str("run", run_id)
        from farna.db.crud.run import get_child_run_rollup

        child_rollups = await get_child_run_rollup(s, [rid])
        child_cost, child_count = child_rollups.get(rid, (_MCP_COST_ROLLUP_ZERO, 0))
        own_cost = Decimal(str(run.total_cost_usd)) if run.total_cost_usd is not None else _MCP_COST_ROLLUP_ZERO
        aggregate_cost = _quantize_mcp_cost_rollup(own_cost + child_cost)
    parts = [
        f"Run: {run.id}",
        f"Pipeline: {run.pipeline_id}",
        f"Status: {run.status}",
        f"Trigger: {run.trigger_type}",
        f"Created: {run.created_at.isoformat()}",
    ]
    if run.error_code:
        parts.append(f"Error: {map_legacy_code(run.error_code)}")
    if run.total_cost_usd is not None:
        parts.append(f"Total cost: ${run.total_cost_usd}")
    parts.extend(
        [
            f"Child runs cost: ${child_cost}",
            f"Child runs count: {child_count}",
            f"Aggregate cost: ${aggregate_cost}",
        ]
    )
    if run.cost_breakdown is not None:
        breakdown = _sanitize_cost_breakdown(run.cost_breakdown)
        if breakdown:
            parts.append("Cost breakdown:")
            parts.extend(_format_breakdown_line(entry) for entry in breakdown)
    return "\n".join(parts)


# Keep the suite's `patch.object(farna.api.mcp_server, "<helper>")` bindings
# live for the wrappers above: each call re-resolves the helper from
# `mcp_server`, which re-exports THIS module's original through PEP 562.
_cancel_run_impl = patchable_local("_cancel_run_impl", _cancel_run_impl)
_create_manual_run = patchable_local("_create_manual_run", _create_manual_run)
_get_run_evals_impl = patchable_local("_get_run_evals_impl", _get_run_evals_impl)
_get_run_output_impl = patchable_local("_get_run_output_impl", _get_run_output_impl)
_list_runs_impl = patchable_local("_list_runs_impl", _list_runs_impl)
_load_run_for_status = patchable_local("_load_run_for_status", _load_run_for_status)
_trigger_pipeline_impl = patchable_local("_trigger_pipeline_impl", _trigger_pipeline_impl)
_trigger_pipeline_validate_id = patchable_local("_trigger_pipeline_validate_id", _trigger_pipeline_validate_id)
