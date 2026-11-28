"""Pipeline CRUD, graph, ownership and snapshot-diff MCP tools and resources.

Covers list/create/update/delete pipeline, the circuit breaker, earned
autonomy, accountability owners, the live graph read, the graph write, the
connector-node binding, the HITL review listing, snapshot diff, and the four
``modulo://pipelines...`` resources.

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

import asyncio
import copy
import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, NamedTuple, cast

from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_ERROR_TOKEN_REVOKED,
    _MSG_TOKEN_REVOKED,
    _MSG_UUID_PARSE_FAILED,
    _RETRY_DB,
    McpAuthContextError,
    _check_node_allowed_tools,
    _ctx_key_grants,
    _ctx_may_manage_cost,
    _ctx_team_id,
    _ctx_team_id_val,
    _log_tool_failure,
    _parse_optional_uuid,
    _resource_node_scope_error,
    _serialize_edges,
    _team_scope_error,
    _team_scope_error_str,
    _tool_auth_error,
    _tool_exception_error,
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
    _session,
    _team_scoped_key_mismatch,
    get_pipeline,
    merge_masked_graph_nodes,
    patchable_local,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import mask_pipeline_graph_node
from farna.auth.permissions import grants_permit
from farna.auth.team_rbac import org_role_level
from farna.core.exceptions import SnapshotLockNotAvailableError
from farna.core.mcp.scope_validator import MCPAuthorizationError
from farna.db.crud.hitl_review_guard import GuardrailBindingStripDenied, HitlReviewWeakeningDenied
from farna.db.crud.pipeline import CircuitBreakerThresholdChangeDenied, ManualNodeOutputSchemaError
from farna.db.models.pipeline_edge import PipelineEdge
from farna.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of


@mcp.tool(
    name="list_pipelines",
    description=("List pipelines in the organisation with cursor-based pagination. Returns summaries. For raw text output, see the modulo://pipelines resource."),
)
@_RETRY_DB
async def list_pipelines_tool(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("list_pipelines")
        org_id = _ctx_org_id_val()
        from farna.db.crud.pipeline import list_pipelines

        lim = max(1, min(limit, 100))
        async with _session(org_id) as s:
            result = await list_pipelines(s, cursor=cursor, page_size=lim, team_id=_ctx_team_id_val(), organisation_id=org_id)
        return {
            "data": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "visibility": p.visibility,
                    "business_owner_id": _owner_id_str(p.business_owner_id),
                    "reliability_owner_id": _owner_id_str(p.reliability_owner_id),
                }
                for p in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_pipelines_tool failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_pipelines_tool failed")
        return _tool_exception_error("Failed to list pipelines", exc, "mcp.list_pipelines_tool")


class _CreatePipelineParsed(NamedTuple):
    """Parsed UUID arguments for ``create_pipeline``."""

    folder_id: uuid.UUID | None
    business_owner_id: uuid.UUID | None
    reliability_owner_id: uuid.UUID | None


def _validate_create_pipeline_args(
    *,
    folder_id: str | None,
    hitl_review_window_seconds: int | None,
    queue_timeout_seconds: int | None,
    circuit_breaker_threshold: float | None,
    business_owner_id: str | None,
    reliability_owner_id: str | None,
    default_autonomy_level: str,
    max_autonomy_level: str | None,
) -> _CreatePipelineParsed | dict[str, Any]:
    """Validate ``create_pipeline``'s arguments before auth (FAR-* checks).

    Returns the parsed UUIDs on success, or the standard error envelope for
    the first argument that fails validation:

    - ``folder_id`` must be a UUID (``invalid_folder_id``).
    - ``hitl_review_window_seconds`` must sit in the 60..604800 envelope
      (FAR-1257: the same envelope as the REST/Pydantic layer and
      ``ck_pipelines_hitl_review_window`` — validated up front so an
      out-of-range value is a clean tool error, not a DB IntegrityError).
    - ``queue_timeout_seconds`` must sit in the 1..604800 envelope (FAR-1883:
      the same envelope as the REST/Pydantic layer and
      ``ck_pipelines_queue_timeout``; null = never expire).
    - ``circuit_breaker_threshold`` runs the shared FAR-1182 check.
    - owner ids must parse as optional UUIDs (FAR-1161).
    - the autonomy ceiling must be a valid level at or above the default
      (FAR-1163, same rule as the REST create route).
    """
    parsed_folder_id: uuid.UUID | None = None
    if folder_id is not None:
        try:
            parsed_folder_id = uuid.UUID(folder_id)
        except ValueError:
            return {"error": "invalid_folder_id", "detail": f"Invalid folder_id UUID: {folder_id}"}
    if hitl_review_window_seconds is not None and not (60 <= hitl_review_window_seconds <= 604800):
        return {
            "error": "validation_failed",
            "detail": (f"hitl_review_window_seconds must be between 60 and 604800 seconds, got {hitl_review_window_seconds}"),
        }
    if queue_timeout_seconds is not None and not (1 <= queue_timeout_seconds <= 604800):
        return {
            "error": "validation_failed",
            "field": "queue_timeout_seconds",
            "detail": (f"queue_timeout_seconds must be between 1 and 604800 seconds, got {queue_timeout_seconds}"),
        }
    threshold_error = _circuit_breaker_threshold_error(circuit_breaker_threshold)
    if threshold_error is not None:
        return threshold_error
    parsed_business_owner, business_owner_err = _parse_optional_uuid(business_owner_id, "business_owner_id")
    if business_owner_err is not None:
        return business_owner_err
    parsed_reliability_owner, reliability_owner_err = _parse_optional_uuid(reliability_owner_id, "reliability_owner_id")
    if reliability_owner_err is not None:
        return reliability_owner_err
    if max_autonomy_level is not None:
        from farna.core.run_context.autonomy import validate_autonomy_ceiling

        try:
            validate_autonomy_ceiling(default_autonomy_level, max_autonomy_level)
        except ValueError as exc:
            return {"error": "invalid_max_autonomy_level", "detail": str(exc)}
    return _CreatePipelineParsed(parsed_folder_id, parsed_business_owner, parsed_reliability_owner)


@mcp.tool(
    description="Create a new pipeline in the organisation. Returns the created pipeline details. "
    "Optional circuit_breaker_threshold (USD, > 0) sets a monthly spend circuit breaker: when the "
    "pipeline's calendar-month spend would exceed it, runs are rejected and the pipeline's triggers "
    "pause until an org admin resets it. Omit or pass null for no breaker. "
    "Optional business_owner_id / reliability_owner_id (account UUIDs) assign the accountability "
    "owners (FAR-1161); each assignee must be an active member of this organisation and, when "
    "visibility is 'team', a member of the owner team — an ineligible owner is rejected with a "
    "validation error, never silently dropped. Omit or pass null for unassigned. "
    "Optional hitl_review_window_seconds (60-604800) sets the per-pipeline HITL review window "
    "override. Omit or pass null to inherit the org default, then the instance default. "
    "Optional queue_timeout_seconds (1-604800) sets the per-pipeline capacity-queue timeout: "
    "when set, a pending run carrying a capacity marker older than this window is ended as a "
    "supersession (status rejected, error_code queue.expired), never a failure. Omit it, or "
    "pass null (default), to never expire."
)
@mcp_audited("pipeline_created", "pipeline", fail_closed=False)
@_RETRY_DB
async def create_pipeline(
    name: str,
    description: str | None = None,
    visibility: str = "org",
    max_concurrent_runs: int = 5,
    lock_wait_timeout_seconds: int = 300,
    node_timeout_seconds: int = 300,
    hitl_review_window_seconds: int | None = None,
    queue_timeout_seconds: int | None = None,
    default_autonomy_level: str = "manual_approval",
    max_autonomy_level: str | None = None,
    folder_id: str | None = None,
    circuit_breaker_threshold: float | None = None,
    business_owner_id: str | None = None,
    reliability_owner_id: str | None = None,
) -> dict[str, Any]:
    # This flat signature IS the MCP tool's public JSON schema (FastMCP derives
    # the parameters from it), so it is deliberately not reshaped to satisfy
    # python:S107 — see the narrow file-scoped exclusion in
    # sonar-project.properties. A nested object here would change the wire
    # shape a client sees while sibling timeout arguments stay flat.
    args = _validate_create_pipeline_args(
        folder_id=folder_id,
        hitl_review_window_seconds=hitl_review_window_seconds,
        queue_timeout_seconds=queue_timeout_seconds,
        circuit_breaker_threshold=circuit_breaker_threshold,
        business_owner_id=business_owner_id,
        reliability_owner_id=reliability_owner_id,
        default_autonomy_level=default_autonomy_level,
        max_autonomy_level=max_autonomy_level,
    )
    if isinstance(args, dict):
        return args
    parsed_folder_id = args.folder_id
    parsed_business_owner = args.business_owner_id
    parsed_reliability_owner = args.reliability_owner_id

    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_pipeline")
        from farna.db.crud.pipeline import (
            circuit_breaker_threshold_change_allowed,
            create_pipeline,
            normalize_circuit_breaker_threshold,
        )

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        # FAR-1184: every surface that can set the threshold runs the ONE
        # shared check. Create always starts from previous=None ("set where
        # none exists"), so this consults cost.manage but can only refuse if
        # the rule ever tightens; the refusal still audits + returns a
        # permission-denied result like set_pipeline_circuit_breaker.
        new_threshold = normalize_circuit_breaker_threshold(circuit_breaker_threshold)
        if not circuit_breaker_threshold_change_allowed(
            None,
            new_threshold,
            may_manage_cost=_ctx_may_manage_cost(),
        ):
            raise CircuitBreakerThresholdChangeDenied(previous=None, new=new_threshold)

        async with _session(org_id) as s:
            pipeline = await create_pipeline(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                description=description,
                visibility=visibility,
                max_concurrent_runs=max_concurrent_runs,
                lock_wait_timeout_seconds=lock_wait_timeout_seconds,
                node_timeout_seconds=node_timeout_seconds,
                hitl_review_window_seconds=hitl_review_window_seconds,
                queue_timeout_seconds=queue_timeout_seconds,
                default_autonomy_level=default_autonomy_level,
                max_autonomy_level=max_autonomy_level,
                folder_id=parsed_folder_id,
                circuit_breaker_threshold=circuit_breaker_threshold,
                business_owner_id=parsed_business_owner,
                reliability_owner_id=parsed_reliability_owner,
            )

        return {
            "id": str(pipeline.id),
            "name": pipeline.name,
            "description": pipeline.description,
            "visibility": pipeline.visibility,
            "max_concurrent_runs": pipeline.max_concurrent_runs,
            "default_autonomy_level": pipeline.default_autonomy_level,
            "circuit_breaker_threshold": _threshold_float(pipeline.circuit_breaker_threshold),
            "max_autonomy_level": pipeline.max_autonomy_level,
            "business_owner_id": _owner_id_str(pipeline.business_owner_id),
            "reliability_owner_id": _owner_id_str(pipeline.reliability_owner_id),
            "created_at": pipeline.created_at.isoformat() if pipeline.created_at else None,
        }
    except CircuitBreakerThresholdChangeDenied as exc:
        await _append_mcp_threshold_denial_audit(org_id, None, exc)
        return {
            "error": "permission_denied",
            "field": "circuit_breaker_threshold",
            "detail": str(exc),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except FastAPIHTTPException as exc:
        # FAR-1161: the shared eligibility invariant rejects an ineligible
        # accountability owner with a 422 — surface the specific detail
        # instead of a generic internal error.
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except ProgrammingError:
        _log.exception("create_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("create_pipeline failed")
        return _tool_exception_error("Failed to create pipeline", exc, "mcp.create_pipeline")


def _circuit_breaker_threshold_error(value: float | None) -> dict[str, Any] | None:
    """Validate a circuit_breaker_threshold argument (FAR-1182); error dict or None."""
    from farna.db.crud.pipeline import normalize_circuit_breaker_threshold

    try:
        normalize_circuit_breaker_threshold(value)
    except ValueError as exc:
        return {"error": "validation_failed", "field": "circuit_breaker_threshold", "detail": str(exc)}
    return None


def _threshold_float(value: Any) -> float | None:
    """Serialise the Numeric threshold column as a JSON number (None = disabled)."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
        return None
    return float(value)


def _owner_id_str(value: Any) -> str | None:
    """Serialise an accountability-owner column as a JSON string (None = unassigned).

    FAR-1161: defensive like ``PipelineResponse._coerce_owner_id`` — partial
    ORM stand-ins and test doubles expose non-column attributes that must
    serialise as "unassigned" (None), never a repr string.
    """
    return str(value) if isinstance(value, uuid.UUID | str) else None


async def _append_mcp_threshold_denial_audit(
    org_id: uuid.UUID,
    resource_id: uuid.UUID | None,
    exc: CircuitBreakerThresholdChangeDenied,
) -> None:
    """FAR-1184: append the threshold-change-denied audit event for an MCP denial.

    Runs in a fresh ``_session`` after the guarded write's transaction rolled
    back (or before it ever ran), so the refusal is never lost. Best-effort:
    an audit failure is logged but never masks the permission-denied result.
    ``resource_id`` is None on ``create_pipeline`` (no row exists yet).
    """
    try:
        from farna.core.audit_logger import append_audit_event
        from farna.db.crud.pipeline import CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT

        async with _session(org_id) as s:
            try:
                actor_user_id = _ctx_user_id_val()
            except McpAuthContextError:
                actor_user_id = None
            await append_audit_event(
                s,
                org_id=org_id,
                event_type=CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT,
                actor_user_id=actor_user_id,
                resource_type="pipeline",
                resource_id=resource_id,
                payload_json={
                    "denied": True,
                    "previous_threshold_usd": float(exc.previous) if exc.previous is not None else None,
                    "new_threshold_usd": float(exc.new) if exc.new is not None else None,
                    "changed_by": str(actor_user_id) if actor_user_id is not None else None,
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(
            "mcp.circuit_breaker_threshold_change_denial_audit_failed",
            extra={"org_id": str(org_id)},
        )


@mcp.tool(
    description="Set or clear a pipeline's monthly spend circuit breaker (USD). "
    "circuit_breaker_threshold > 0 enables it: when the pipeline's calendar-month spend plus a run's "
    "cost would exceed the threshold, the run is rejected, all of the pipeline's triggers pause and "
    "admins are notified; an org admin resets it via POST "
    "/api/v1/admin/costs/circuit-breaker/{pipeline_id}/reset. Pass null to disable. "
    "Lowering the threshold or setting the first one requires pipeline.update; RAISING it or "
    "CLEARING an existing one requires the cost.manage (org admin) permission and is refused "
    "otherwise (FAR-1184). Every change is audited (pipeline.circuit_breaker_threshold_changed); "
    "refusals are audited as pipeline.circuit_breaker_threshold_change_denied. Available on every plan."
)
@mcp_audited("pipeline_circuit_breaker_updated", "pipeline", fail_closed=False)
@_RETRY_DB
async def set_pipeline_circuit_breaker(
    pipeline_id: str,
    circuit_breaker_threshold: float | None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("set_pipeline_circuit_breaker")

        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "detail": _MSG_UUID_PARSE_FAILED}
        threshold_error = _circuit_breaker_threshold_error(circuit_breaker_threshold)
        if threshold_error is not None:
            return threshold_error

        from farna.db.crud.pipeline import (
            circuit_breaker_threshold_change_allowed,
            get_pipeline,
            normalize_circuit_breaker_threshold,
            update_pipeline,
        )

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        # FAR-1184: validated above; normalise to the column's Decimal scale
        # BEFORE the permission comparison so an unexpected type never
        # reaches `new <= previous`.
        new_threshold = normalize_circuit_breaker_threshold(circuit_breaker_threshold)
        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            current = await get_pipeline(s, pid, organisation_id=org_id)
            # FAR-1184: raising/clearing the spend limit requires cost.manage;
            # lowering or setting where none exists keeps pipeline.update.
            # Raised BEFORE the mutation; the denial audits fresh + returns a
            # permission-denied result via the except clause below.
            if current is not None and not circuit_breaker_threshold_change_allowed(
                current.circuit_breaker_threshold,
                new_threshold,
                may_manage_cost=_ctx_may_manage_cost(),
            ):
                raise CircuitBreakerThresholdChangeDenied(
                    previous=current.circuit_breaker_threshold,
                    new=new_threshold,
                )
            pipeline = await update_pipeline(
                s,
                pid,
                {"circuit_breaker_threshold": circuit_breaker_threshold},
                org_id=org_id,
                account_id=account_id,
            )
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            # Built inside the session (commits on exit) so no expired attribute is read.
            return {
                "pipeline_id": pipeline_id,
                "circuit_breaker_threshold": _threshold_float(pipeline.circuit_breaker_threshold),
                "circuit_breaker_tripped": bool(pipeline.circuit_breaker_tripped),
            }
    except CircuitBreakerThresholdChangeDenied as exc:
        await _append_mcp_threshold_denial_audit(org_id, pid, exc)
        return {
            "error": "permission_denied",
            "field": "circuit_breaker_threshold",
            "detail": str(exc),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("set_pipeline_circuit_breaker failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("set_pipeline_circuit_breaker failed")
        return _tool_exception_error("Failed to set pipeline circuit breaker", exc, "mcp.set_pipeline_circuit_breaker")


def _ctx_autonomy_change_allowed(owner_team_id: uuid.UUID | None) -> bool:
    """FAR-1175: admin or the pipeline's owner team may change autonomy (fail-closed)."""
    role = _ctx_role_val()
    if role is not None and org_role_level(role) >= org_role_level("admin"):
        return True
    key_team_id = _ctx_team_id.get(None)
    return owner_team_id is not None and key_team_id == owner_team_id


_AUTONOMY_CHANGE_DENIED = "Only an organisation admin or the pipeline's owner team may change its autonomy level"


def _mcp_autonomy_levels(pipeline: Any) -> tuple[Any, Any]:
    """Resolve a pipeline's ``(current earned-or-default, ceiling)`` (FAR-1175).

    ``current`` is the earned level when set and valid, else the pipeline
    default; ``ceiling`` is ``max_autonomy_level`` when set and valid, else the
    current level (an absent/invalid ceiling never constrains the change).
    """
    from farna.core.run_context.autonomy import AutonomyLevel

    default = pipeline.default_autonomy_level or AutonomyLevel.default().value
    earned = getattr(pipeline, "earned_autonomy_level", None)
    try:
        current_level = AutonomyLevel(earned) if isinstance(earned, str) and earned else AutonomyLevel(default)
    except ValueError:
        current_level = AutonomyLevel(default)
    ceiling_raw = getattr(pipeline, "max_autonomy_level", None)
    try:
        ceiling = AutonomyLevel(ceiling_raw) if isinstance(ceiling_raw, str) and ceiling_raw else current_level
    except ValueError:
        ceiling = current_level
    return current_level, ceiling


def _mcp_autonomy_direction_error(
    promote: bool,
    target_level: Any,
    current_level: Any,
    ceiling: Any,
) -> dict[str, Any] | None:
    """Direction-validation error for a changed target, or ``None`` (FAR-1175).

    Promote must strictly raise and never exceed the ceiling; demote must
    strictly lower. Mirrors the REST ``_apply_manual_autonomy_change`` rules.
    """
    from farna.core.run_context.autonomy import autonomy_level_rank

    target = target_level.value
    if promote:
        if autonomy_level_rank(target_level) <= autonomy_level_rank(current_level):
            return {
                "error": "validation_failed",
                "field": "level",
                "detail": f"Promotion must raise autonomy above {current_level.value}: {target}",
            }
        if autonomy_level_rank(target_level) > autonomy_level_rank(ceiling):
            return {
                "error": "validation_failed",
                "field": "level",
                "detail": f"Promotion to {target} exceeds the max_autonomy_level ceiling ({ceiling.value})",
            }
    elif autonomy_level_rank(target_level) >= autonomy_level_rank(current_level):
        return {
            "error": "validation_failed",
            "field": "level",
            "detail": f"Demotion must lower autonomy below {current_level.value}: {target}",
        }
    return None


async def _append_mcp_autonomy_audit(
    session: Any,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None,
    pipeline_id: uuid.UUID,
    current_level: Any,
    target: str,
    reason: str | None,
    ceiling: Any,
    promote: bool,
    event_type: str,
) -> None:
    """Append the promote/demote audit event in the write transaction (FAR-1175)."""
    from farna.core.audit_logger import append_audit_event

    await append_audit_event(
        session,
        org_id=org_id,
        event_type=event_type,
        actor_user_id=account_id,
        resource_type="pipeline",
        resource_id=pipeline_id,
        payload_json={
            "pipeline_id": str(pipeline_id),
            "previous_level": current_level.value,
            "new_level": target,
            "reason": reason,
            "actor": str(account_id),
            "ceiling": ceiling.value,
            "direction": "promote" if promote else "demote",
        },
    )


async def _apply_mcp_autonomy_change(
    pipeline_id: str,
    level: str,
    reason: str | None,
    *,
    promote: bool,
    tool_name: str,
) -> dict[str, Any]:
    """Shared body of the MCP autonomy demote/promote tools (FAR-1175).

    Mirrors the REST endpoints: admin/owner gated, direction-validated, ceiling
    capped on promote, and the ``pipeline.autonomy_demoted`` /
    ``pipeline.autonomy_promotion_decided`` audit event appended in the same
    transaction as the write.
    """
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope(tool_name)

        from farna.core.run_context.autonomy import AutonomyLevel
        from farna.db.crud.pipeline import get_pipeline, set_earned_autonomy_level

        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        # _parse_uuid_param returns (None, error) only on failure, already handled
        # above; cast narrows for mypy without an assert inside the except-Exception
        # block (SonarCloud S5779).
        pid = cast(uuid.UUID, pid)
        try:
            target_level = AutonomyLevel(level)
        except ValueError:
            return {"error": "validation_failed", "field": "level", "detail": f"Invalid autonomy level: {level!r}"}
        target = target_level.value

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            pipeline = await get_pipeline(s, pid, organisation_id=org_id)
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            if not _ctx_autonomy_change_allowed(owner_team_id):
                return {"error": "permission_denied", "detail": _AUTONOMY_CHANGE_DENIED}

            current_level, ceiling = _mcp_autonomy_levels(pipeline)
            direction_error = _mcp_autonomy_direction_error(promote, target_level, current_level, ceiling)
            if direction_error is not None:
                return direction_error

            updated = await set_earned_autonomy_level(s, org_id=org_id, pipeline_id=pid, level=target)
            if updated is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            event_type = "pipeline.autonomy_promotion_decided" if promote else "pipeline.autonomy_demoted"
            await _append_mcp_autonomy_audit(
                s,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pid,
                current_level=current_level,
                target=target,
                reason=reason,
                ceiling=ceiling,
                promote=promote,
                event_type=event_type,
            )
            return {
                "pipeline_id": pipeline_id,
                "previous_level": current_level.value,
                "earned_autonomy_level": target,
                "max_autonomy_level": getattr(updated, "max_autonomy_level", None),
                "event_type": event_type,
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log_tool_failure(tool_name)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log_tool_failure(tool_name)
        return _tool_exception_error("Failed to change pipeline autonomy", exc, f"mcp.{tool_name}")


@mcp.tool(
    description="Lower a pipeline's earned autonomy level (FAR-1175, ADR 043 S1). "
    "Demotion adds human review so it is always safe: an admin or the pipeline's owner team may "
    "lower it at any time. Takes effect at the run's next HITL gate (in-flight runs included) when "
    "the autonomy_gating flag is on. Records a pipeline.autonomy_demoted audit event carrying the "
    "previous/new level and the reason."
)
@mcp_audited("pipeline_autonomy_demoted", "pipeline", fail_closed=False)
@_RETRY_DB
async def demote_pipeline_autonomy(
    pipeline_id: str,
    level: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return await _apply_mcp_autonomy_change(pipeline_id, level, reason, promote=False, tool_name="demote_pipeline_autonomy")


@mcp.tool(
    description="Raise a pipeline's earned autonomy level (FAR-1175, ADR 043 S1). "
    "Promotion is never automatic: an admin or the pipeline's owner team approves it, capped at the "
    "pipeline's max_autonomy_level ceiling. It applies only to runs created after the approval — "
    "in-flight runs keep the lower level (ADR 043 §3). Records a pipeline.autonomy_promotion_decided "
    "audit event."
)
@mcp_audited("pipeline_autonomy_promoted", "pipeline", fail_closed=False)
@_RETRY_DB
async def promote_pipeline_autonomy(
    pipeline_id: str,
    level: str,
    reason: str | None = None,
) -> dict[str, Any]:
    return await _apply_mcp_autonomy_change(pipeline_id, level, reason, promote=True, tool_name="promote_pipeline_autonomy")


@mcp.tool(
    description=(
        "Set BOTH of a pipeline's accountability owners (FAR-1161) in one atomic write. "
        "Both parameters are REQUIRED and nullable: pass an account UUID to assign, or null to "
        "clear that owner (there is no leave-unchanged state — the tool always writes both "
        "fields; pass the owner's current id to keep it). The assignee must be an active member "
        "of this organisation and, when the pipeline's visibility is 'team', a member of the "
        "pipeline's owner team — the shared eligibility invariant rejects an ineligible owner "
        "with a validation error (never silently dropped). Returns the stored owners. "
        "Every change is audited (pipeline.business_owner_changed / pipeline.reliability_owner_changed)."
    ),
)
@mcp_audited("pipeline_owners_updated", "pipeline", fail_closed=False)
@_RETRY_DB
async def set_pipeline_owners(
    pipeline_id: str,
    business_owner_id: str | None,
    reliability_owner_id: str | None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("set_pipeline_owners")

        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "detail": _MSG_UUID_PARSE_FAILED}
        parsed_business, business_err = _parse_optional_uuid(business_owner_id, "business_owner_id")
        if business_err is not None:
            return business_err
        parsed_reliability, reliability_err = _parse_optional_uuid(reliability_owner_id, "reliability_owner_id")
        if reliability_err is not None:
            return reliability_err

        from farna.db.crud.pipeline import update_pipeline

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            pipeline = await update_pipeline(
                s,
                pid,
                {
                    "business_owner_id": parsed_business,
                    "reliability_owner_id": parsed_reliability,
                },
                org_id=org_id,
                account_id=account_id,
            )
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            # Built inside the session (commits on exit) so no expired attribute is read.
            return {
                "pipeline_id": pipeline_id,
                "business_owner_id": _owner_id_str(pipeline.business_owner_id),
                "reliability_owner_id": _owner_id_str(pipeline.reliability_owner_id),
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except FastAPIHTTPException as exc:
        # FAR-1161: the shared eligibility invariant rejects an ineligible
        # accountability owner with a 422 — surface the specific detail
        # instead of a generic internal error.
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except ProgrammingError:
        _log.exception("set_pipeline_owners failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("set_pipeline_owners failed")
        return _tool_exception_error("Failed to set pipeline owners", exc, "mcp.set_pipeline_owners")


_OWNER_TEAM_RESOURCE_TYPES = ("pipeline", "model_backend", "connector")


class _OwnerTeamScopeDeniedError(Exception):
    """A team-scoped credential tried to move another team's resource (raised before any write)."""


async def _apply_owner_team_pipeline(s: AsyncSession, principal: Any, resource_id: uuid.UUID, new_team: uuid.UUID | None, org_id: uuid.UUID) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
    """Move a pipeline to *new_team* through the REST PATCH gates; None when not found."""
    from farna.api.routes.pipelines import (
        _assert_team_transition_allowed,
        _enforce_stored_bindings_on_team_change,
        _reapply_team_gate_inside_mutation_txn,
    )
    from farna.db.crud.pipeline import update_pipeline as update_pipeline_row

    try:
        locked = await _reapply_team_gate_inside_mutation_txn(s, principal, resource_id)
    except FastAPIHTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    previous = locked.owner_team_id
    updates: dict[str, Any] = {"owner_team_id": new_team}
    if new_team is not None and new_team != previous:
        from farna.api.team_scope import validate_owner_team_for_create

        await validate_owner_team_for_create(s, principal, new_team)
    await _assert_team_transition_allowed(s, principal, locked, updates)
    await _enforce_stored_bindings_on_team_change(s, principal, locked, updates, ownership_changed=new_team != previous, graph_replaced=False)
    pipeline = await update_pipeline_row(s, resource_id, updates, org_id=org_id, account_id=principal.account_id)
    if pipeline is None:
        return None
    return previous, pipeline.owner_team_id


async def _apply_owner_team_simple(
    s: AsyncSession,
    principal: Any,
    resource_type: str,
    resource_id: uuid.UUID,
    new_team: uuid.UUID | None,
    org_id: uuid.UUID,
) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
    """Move a model backend or connector to *new_team*; None when not found in this org."""
    from farna.api.team_scope import validate_owner_team_for_create, validate_team_transition_for_update
    from farna.core.audit_logger import append_audit_event

    get_row: Any
    update_row: Any
    if resource_type == "model_backend":
        from farna.db.crud import model_backend as _mb_crud

        get_row, update_row = _mb_crud.get_model_backend, _mb_crud.update_model_backend
    else:
        from farna.db.crud import connector_instance as _ci_crud

        get_row, update_row = _ci_crud.get_connector_instance, _ci_crud.update_connector_instance

    row = await get_row(s, resource_id)
    if row is None or row.organisation_id != org_id:
        return None
    previous = row.owner_team_id
    if _team_scoped_key_mismatch(previous):
        raise _OwnerTeamScopeDeniedError
    if new_team is not None and new_team != previous:
        await validate_owner_team_for_create(s, principal, new_team)
    await validate_team_transition_for_update(
        s,
        principal,
        current_owner_team_id=previous,
        current_visibility=row.visibility,
        new_owner_team_id=new_team,
        new_visibility=row.visibility,
    )
    updates: dict[str, Any] = {"owner_team_id": new_team}
    if resource_type == "connector":
        from farna.api.routes.connectors import _reject_re_scope_that_breaks_a_bound_pipeline

        await _reject_re_scope_that_breaks_a_bound_pipeline(s, org_id=org_id, connector=row, updates=updates)
    updated = await update_row(s, resource_id, updates)
    if updated is None:
        return None
    if new_team != previous:
        await append_audit_event(
            s,
            org_id=org_id,
            event_type="resource_team_ownership_changed",
            actor_user_id=principal.account_id,
            resource_type=resource_type,
            resource_id=resource_id,
            payload_json={
                "resource_type": resource_type,
                "resource_id": str(resource_id),
                "old_team_id": str(previous) if previous is not None else None,
                "new_team_id": str(new_team) if new_team is not None else None,
                "changed_by": str(principal.account_id),
            },
        )
    return previous, updated.owner_team_id


@mcp.tool(
    description=(
        "Set (or clear) the owner team of a team-owned resource (FAR-2298). resource_type is "
        "one of 'pipeline', 'model_backend' or 'connector'; resource_id is that resource's UUID. "
        "owner_team_id is REQUIRED and nullable: pass a team UUID to assign, or null to clear the "
        "owner team (there is no leave-unchanged state). The same gates as the REST PATCH apply: "
        "the team must exist in this organisation, a non-admin caller must be a member of the "
        "current and the new team, a team-visibility resource cannot lose its owner team, a "
        "pipeline with active runs cannot change team, and a re-scope that would strand a bound "
        "connector/pipeline is refused. Any rejection returns validation_failed with the detail "
        "(never silently dropped). Returns the previous and stored owner team. Requires the "
        "update permission of the resource type (operator role). Audited "
        "(resource_team_ownership_changed)."
    ),
)
@mcp_audited("resource_owner_team_updated", "resource", fail_closed=False)
@_RETRY_DB
async def set_resource_owner_team(
    resource_type: str,
    resource_id: str,
    owner_team_id: str | None,
) -> dict[str, Any]:
    from types import SimpleNamespace

    from farna.db.crud.pipeline import PipelineHasActiveRunsError

    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        if resource_type not in _OWNER_TEAM_RESOURCE_TYPES:
            return {
                "error": "validation_failed",
                "detail": f"resource_type must be one of {', '.join(_OWNER_TEAM_RESOURCE_TYPES)}",
            }
        _check_agent_tool_scope("set_resource_owner_team", action=resource_type)

        rid, rid_err = _parse_uuid_param(resource_id, "resource_id")
        if rid_err:
            return rid_err
        # _parse_uuid_param returns (None, error) only on failure, already handled
        # above; cast narrows for mypy without a dead branch or an assert inside
        # the except-Exception block (SonarCloud S5779).
        rid = cast(uuid.UUID, rid)
        new_team, team_err = _parse_optional_uuid(owner_team_id, "owner_team_id")
        if team_err is not None:
            return team_err

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        principal = SimpleNamespace(organisation_id=org_id, account_id=account_id, org_role=_ctx_role_val())
        async with _session(org_id) as s:
            if resource_type == "pipeline":
                current_owner = await _pipeline_owner_team_id(s, rid)
                if _team_scoped_key_mismatch(current_owner):
                    return _team_scope_error("pipeline", resource_id)
                outcome = await _apply_owner_team_pipeline(s, principal, rid, new_team, org_id)
            else:
                outcome = await _apply_owner_team_simple(s, principal, resource_type, rid, new_team, org_id)
            if outcome is None:
                return {"error": f"{resource_type}_not_found", "resource_id": resource_id}
            previous, stored = outcome
            return {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "previous_owner_team_id": _owner_id_str(previous),
                "owner_team_id": _owner_id_str(stored),
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except _OwnerTeamScopeDeniedError:
        return _team_scope_error(resource_type, resource_id)
    except PipelineHasActiveRunsError as exc:
        return {
            "error": "pipeline_has_active_runs",
            "detail": (f"{exc.active_run_count} run(s) still in progress; cannot change ownership while any run is active"),
        }
    except FastAPIHTTPException as exc:
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except ProgrammingError:
        _log.exception("set_resource_owner_team failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("set_resource_owner_team failed")
        return _tool_exception_error("Failed to set owner team", exc, "mcp.set_resource_owner_team")


@mcp.tool(
    description=(
        "Update a pipeline's environment_profile_id binding (FAR-1599) — the environment "
        "profile the pipeline's sandbox_agent nodes dispatch on (FAR-1558). "
        "environment_profile_id is REQUIRED and nullable: pass an environment-profile UUID to "
        "bind, or pass null to clear the binding and restore the default route (MCP arguments "
        "cannot express 'omitted', so there is no leave-unchanged state — pass the profile's "
        "current id to keep it). The profile must exist in this organisation and be visible to "
        "the pipeline's owner team (an org-visible profile, or a team profile owned by that "
        "team); the shared FAR-1558 validation runs server-side exactly as on REST PATCH "
        "/api/v1/pipelines/{pipeline_id}, and a violation returns validation_failed carrying "
        "the typed environment_profile_* code. Returns the stored binding. Requires the "
        "pipeline.update permission."
    ),
)
@mcp_audited("pipeline_environment_profile_updated", "pipeline", fail_closed=False)
@_RETRY_DB
async def update_pipeline(
    pipeline_id: str,
    environment_profile_id: str | None,
) -> dict[str, Any]:
    """FAR-1599: MCP set path for the per-pipeline environment-profile binding.

    Validation is DELEGATED to ``_assert_environment_profile_bindable`` — the
    shared FAR-1558 predicate the REST update route calls — never re-implemented
    here, so the MCP and REST writers cannot drift (same-org existence +
    team-visibility, raising HTTP 422 with the typed wire code).
    """
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_pipeline")

        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "detail": _MSG_UUID_PARSE_FAILED}
        # Required + nullable: None CLEARS the binding (mirrors the REST
        # explicit-null clear, which likewise runs no profile lookup).
        parsed_profile, profile_err = _parse_optional_uuid(environment_profile_id, "environment_profile_id")
        if profile_err is not None:
            return profile_err

        from farna.db.crud.pipeline import update_pipeline as update_pipeline_row

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            if parsed_profile is not None:
                from farna.api.routes.pipelines import _assert_environment_profile_bindable

                # Raises HTTPException 422 (mapped below): missing, deleted,
                # cross-org, or team-private-and-not-owned-by-this-team.
                await _assert_environment_profile_bindable(
                    s,
                    org_id=org_id,
                    profile_id=parsed_profile,
                    pipeline_owner_team_id=owner_team_id,
                )
            pipeline = await update_pipeline_row(
                s,
                pid,
                {"environment_profile_id": parsed_profile},
                org_id=org_id,
                account_id=account_id,
            )
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
            # Built inside the session (commits on exit) so no expired attribute is read.
            stored = getattr(pipeline, "environment_profile_id", None)
            return {
                "pipeline_id": pipeline_id,
                "environment_profile_id": str(stored) if isinstance(stored, uuid.UUID) else None,
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except FastAPIHTTPException as exc:
        # FAR-1558: the shared predicate's 422 detail (including the typed
        # environment_profile_team_mismatch code clients branch on) must
        # reach the caller — not a generic internal error.
        return {"error": "validation_failed", "detail": str(exc.detail)}
    except ProgrammingError:
        _log.exception("update_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("update_pipeline failed")
        return _tool_exception_error("Failed to update pipeline", exc, "mcp.update_pipeline")


@mcp.tool(
    name="get_pipeline_graph",
    description="Get the full graph (nodes + edges) of a pipeline by ID. "
    "Returns nodes with their configuration (agent_prompt, agent_commands, template_id, timeout_seconds, etc.) "
    "and edges with their source/target/type. "
    "Secret-bearing node fields (env_vars, context_files, composite_parameter_values, "
    "parameter_overrides) are masked as \u2022\u2022\u2022\u2022\u2022\u2022 here; those masked values are "
    "safe to send back unchanged to update_pipeline_graph, which resolves them to the "
    "stored secrets rather than overwriting them — so a graph containing secrets can be "
    "read, edited and written back. "
    "For pipelines that use sandbox_agent nodes, this is how you read the current agent_commands before modifying it.",
)
@_RETRY_DB
async def get_pipeline_graph_tool(
    pipeline_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("get_pipeline_graph")
        from farna.db.crud.pipeline import get_pipeline_graph

        org_id = _ctx_org_id_val()
        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": _MSG_UUID_PARSE_FAILED}

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            result = await get_pipeline_graph(s, pid, organisation_id=org_id)

        if result is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

        nodes, edges = result
        edge_dicts = _serialize_edges(edges)

        # FAR-1181 (echo of the REST parity in routes/pipelines.py): the same
        # credential-bearing node fields (env_vars / context_files /
        # composite_parameter_values / parameter_overrides) must be masked on
        # every graph read, not only the REST surface.
        masked_nodes = [mask_pipeline_graph_node(n) if isinstance(n, dict) else n for n in nodes]

        return {
            "pipeline_id": pipeline_id,
            "nodes": masked_nodes,
            "edges": edge_dicts,
            "node_count": len(masked_nodes),
            "edge_count": len(edge_dicts),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_pipeline_graph_tool failed")
        return {
            "error": "migration_required",
            "detail": "Database migration may be required. Run alembic upgrade heads.",
        }
    except Exception as exc:
        _log.exception("get_pipeline_graph_tool failed")
        return _tool_exception_error("Failed to get pipeline graph", exc, "mcp.get_pipeline_graph_tool")


async def _append_mcp_hitl_denial_audit(org_id: uuid.UUID, pipeline_id: uuid.UUID, exc: HitlReviewWeakeningDenied) -> None:
    """Append the hitl_review_removal_denied audit event for an MCP denial.

    Runs in a fresh ``_session`` after the guarded write's transaction rolled
    back, so the denial is never lost (hitl-gate-removal-guard-plan.md v19 §5).
    Best-effort: an audit failure is logged but never masks the denial.
    """
    try:
        from farna.core.audit_logger import append_audit_event

        payload = exc.payload_json or {
            "caller_type": "mcp",
            "reason_code": exc.reason_code,
            "denied": True,
            "affected_edges": [{"source_node_id": k[0], "target_node_id": k[1], "edge_type": k[2]} for k in exc.correlation_keys],
            "weakening_types": exc.weakening_types,
        }
        async with _session(org_id) as s:
            try:
                actor_user_id = _ctx_user_id_val()
            except McpAuthContextError:
                actor_user_id = None
            await append_audit_event(
                s,
                org_id=org_id,
                event_type="hitl_review_removal_denied",
                actor_user_id=actor_user_id,
                resource_type="pipeline",
                resource_id=pipeline_id,
                payload_json=payload,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("mcp.hitl_denial_audit_failed", extra={"org_id": str(org_id)})


def _validate_graph_update(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Validate an MCP graph-update payload before it touches the database.

    Covers, in order: the REST-parity Pydantic structural contract, the
    FAR-296 mode-aware ``sandbox_agent`` gate, and the FAR-613 HITL
    gate-description requirement. Returns the standard ``validation_failed``
    envelope on the first failure, else None.

    FAR-613: the MCP path never runs the full graph validator, and a node's
    ``hitl_config`` is an unvalidated ``dict[str, Any]`` that bypasses the
    edge-level ``HitlReviewConfig`` Pydantic contract entirely — so the HITL
    gate-description requirement is enforced here explicitly. An
    agent-authored gate is exactly where an unexplained gate most needs its
    decision briefing: a node-level or edge-level gate without a
    human-provided description is rejected with the same
    ``validation_failed`` shape as structural validation failures.
    The check is single-sourced with the save-time validator via the public
    helper (deliberately NOT the full ``validate_definition`` — that would
    surface every pre-existing issue and break MCP flows).

    DECIDED (FAR-688, Conductor decision): KEEP this narrow node-level
    description check; do NOT wire the full ``validate_definition`` here.
    Rationale for keeping the narrow check: it enforces the one rule an
    agent-authored graph would most plausibly violate (an unexplained
    gate) at the only surface that bypasses the Pydantic contract, with
    zero false positives. Rationale for deferring full validation: the
    MCP tool writes onto pipelines that may predate any validator rule —
    running the full validator would reject a write because of PRE-EXISTING
    unrelated issues (legacy topology, schema drift), blocking legitimate
    MCP flows for defects this call did not introduce; save-time
    enforcement for REST writes stays the forcing function, and the
    editor surfaces legacy violations to the user (PipelineEditorView
    banner, FAR-688) instead of blocking the read.
    """
    # Validate graph structure using Pydantic models (same as REST endpoint)
    from pydantic import ValidationError as _PydanticValidationError

    from farna.api.routes.pipelines import PipelineGraphUpdate

    try:
        PipelineGraphUpdate.model_validate({"nodes": nodes, "edges": edges})
    except _PydanticValidationError as exc:
        return {
            "error": "validation_failed",
            "detail": f"Graph validation failed: {exc.errors(include_url=False)}",
        }

    # FAR-296 mode-aware sandbox_agent gate — the SAME shared helper the
    # Pydantic model, node runner, and GraphValidator use, applied to the
    # raw node dicts so this gate agrees with save-time and run-time
    # validation even if the Pydantic surface is bypassed.
    sandbox_err = _validate_sandbox_nodes(nodes)
    if sandbox_err:
        return sandbox_err

    from farna.core.graph_validator import check_hitl_review_descriptions as _check_hitl_descriptions

    hitl_issues = _check_hitl_descriptions({"nodes": nodes, "edges": edges})
    hitl_description_errors = [i.message for i in hitl_issues if i.code == "HITL_REVIEW_DESCRIPTION_REQUIRED"]
    if hitl_description_errors:
        return {
            "error": "validation_failed",
            "detail": f"Graph validation failed: {'; '.join(hitl_description_errors)}",
        }
    return None


async def _reject_invalid_connector_bindings(
    s: AsyncSession,
    org_id: uuid.UUID,
    nodes: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Instance-level connector checks for MCP full-graph writes.

    Two invariants REST's save-time ``GraphValidator`` enforces are checked here
    against the bound INSTANCE row, because the MCP full-graph write bypasses
    that validator and ``_validate_graph_update`` only inspects the
    caller-DECLARED ``connector_binding.type`` string:

    * **CI-runner capability** (FAR-1141). A binding that routes the
      ``dispatch`` verb must target a connector type implementing the four
      CI-runner operations; otherwise the job would fail at run time with an
      ``AttributeError``-shaped error *after* it should have fired.
    * **Operation scope** (FAR-1395). The binding's routed verb must be allowed
      by the instance's ``allowed_operations``. Without this the MCP surface
      could persist a ``query``/``write``/``dispatch`` binding that the run-time
      ``ConnectorACL`` then denies — and, for a query with no authorable
      resource, one the connector could never serve.

    Both answers come from the SHARED helpers (``connector_binding_operation`` /
    ``connector_type_supports_dispatch`` / ``connector_binding_denied_acl_operations``),
    so this surface cannot disagree with REST or with run-time enforcement.

    Same posture as the validator: an instance that cannot be resolved, or one
    with an unset ``connector_type_id``, is SKIPPED (the team gate / ordinary
    binding checks cover those shapes); a capability we cannot prove fails
    CLOSED. Returns a standard ``validation_failed`` envelope, or ``None`` to let
    the write proceed. No query is issued when no node routes a binding.
    """
    from sqlalchemy import select

    from farna.connectors.base import node_routes_binding_to_connector
    from farna.db.models.connector_instance import ConnectorInstance

    target_ids = _routed_binding_instance_ids(nodes, node_routes_binding_to_connector)
    if not target_ids:
        return None

    rows = (
        (
            await s.execute(
                select(ConnectorInstance).where(
                    ConnectorInstance.organisation_id == org_id,
                    ConnectorInstance.id.in_(target_ids),
                ),
            )
        )
        .scalars()
        .all()
    )
    found: dict[uuid.UUID, Any] = {row.id: row for row in rows}
    return _invalid_connector_binding_error(nodes, found)


def _validate_binding_routing(
    target: dict[str, Any],
    probe: dict[str, Any],
    node_id: str,
    routes_to_connector: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    if probe["connector_binding"].get("operation") != "dispatch" or routes_to_connector(probe):
        return None
    node_kind = str(target.get("node_type") or "agent")
    return {
        "error": "validation_failed",
        "field": "operation",
        "detail": (f"Node {node_id} ({node_kind}) does not route its connector_binding to a connector, so operation='dispatch' would never fire — use a dispatch or connector node for the dispatch binding"),
    }


def _validate_effective_binding(
    target: dict[str, Any],
    effective_operation: str,
    connector_type: str,
    connector_supports_dispatch: bool | None,
) -> dict[str, Any] | None:
    if str(target.get("node_type") or "") == "dispatch" and effective_operation != "dispatch":
        return {
            "error": "validation_failed",
            "field": "operation",
            "detail": f"Dispatch nodes require connector_binding.operation='dispatch' (got {effective_operation!r}) — a dispatch node that queries fires no job",
        }
    if effective_operation == "dispatch" and connector_supports_dispatch is False:
        return {
            "error": "validation_failed",
            "field": "connector_type",
            "detail": f"connector type {connector_type!r} does not implement the CI-runner operations (trigger_run / get_run_status / get_run_logs / list_runs) a dispatch binding requires",
        }
    return None


def _routed_binding_instance_ids(
    nodes: list[dict[str, Any]],
    routes_resolver: Callable[[dict[str, Any]], bool],
) -> set[uuid.UUID]:
    """The instance ids of every node whose connector_binding the engine routes.

    ``query`` / ``write`` / ``dispatch`` bindings are all included — the
    operation-scope gate applies to every routed verb, not only ``dispatch`` —
    while an unrouted binding (an ``agent`` node with an ``agent_id``, a
    ``sandbox_agent``) contributes nothing: the engine never reads it, so there
    is no verb to gate.
    """
    target_ids: set[uuid.UUID] = set()
    for node in nodes:
        if not isinstance(node, dict) or not routes_resolver(node):
            continue
        binding = node.get("connector_binding")
        if not isinstance(binding, dict):
            continue
        try:
            target_ids.add(uuid.UUID(str(binding.get("instance_id"))))
        except (TypeError, ValueError):
            continue
    return target_ids


def _invalid_connector_binding_error(
    nodes: list[dict[str, Any]],
    found: dict[uuid.UUID, Any],
) -> dict[str, Any] | None:
    """First invalid ROUTED connector binding, as a ``validation_failed`` envelope."""
    for node in nodes:
        if not isinstance(node, dict):
            continue
        binding = node.get("connector_binding")
        if not isinstance(binding, dict):
            continue
        try:
            cid = uuid.UUID(str(binding.get("instance_id")))
        except (TypeError, ValueError):
            continue
        instance = found.get(cid)
        if instance is None:
            continue
        error = _dispatch_capability_error(node, instance, cid)
        if error is not None:
            return error
        error = _operation_scope_error(node, instance, cid)
        if error is not None:
            return error
    return None


def _dispatch_capability_error(
    node: dict[str, Any],
    instance: Any,
    cid: uuid.UUID,
) -> dict[str, Any] | None:
    """Reject a ``dispatch`` binding whose connector type is not a CI runner."""
    from farna.connectors.base import connector_binding_operation, connector_type_supports_dispatch

    if connector_binding_operation(node) != "dispatch":
        return None
    type_id = str(instance.connector_type_id or "").strip()
    if not type_id or connector_type_supports_dispatch(type_id):
        return None
    return {
        "error": "validation_failed",
        "field": "connector_type",
        "detail": (f"Node '{node.get('id')}': connector instance {cid} ({instance.name!r}) has type {type_id!r}, which does not implement the CI-runner operations (trigger_run / get_run_status / get_run_logs / list_runs) a dispatch binding requires"),
    }


def _operation_scope_error(
    node: dict[str, Any],
    instance: Any,
    cid: uuid.UUID,
) -> dict[str, Any] | None:
    """Reject a routed binding the instance's ``allowed_operations`` denies."""
    from farna.connectors.base import (
        connector_binding_denied_acl_operations,
        connector_binding_operation,
        node_routes_binding_to_connector,
    )

    if not node_routes_binding_to_connector(node):
        return None
    denied = connector_binding_denied_acl_operations(
        node,
        visibility=instance.visibility,
        allowed_operations=instance.allowed_operations,
        connector_type_id=instance.connector_type_id,
    )
    if not denied:
        return None
    operation = connector_binding_operation(node)
    return {
        "error": "validation_failed",
        "field": "connector_binding",
        "detail": (f"Node '{node.get('id')}': connector instance {cid} ({instance.name!r}) does not allow {', '.join(repr(op) for op in denied)} required by the node's {operation!r} operation"),
    }


async def _replace_pipeline_graph_txn(
    org_id: uuid.UUID,
    pid: uuid.UUID,
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    *,
    is_privileged: bool,
    graph_account_id: uuid.UUID | None,
    is_guardrail_admin: bool,
) -> tuple[list[Any], list[Any]] | dict[str, Any]:
    """Run the MCP graph replacement inside its own bounded-lock session.

    Returns ``(updated_nodes, updated_edges)`` on success or a standard error
    envelope (``pipeline_not_found`` / team-scope / connector-team-mismatch)
    when the write must not proceed.
    """
    from farna.core.team_visibility import (
        CONNECTOR_TEAM_MISMATCH,
        ConnectorBindingMissingError,
        connector_team_mismatch_detail,
        extract_connector_bindings,
        find_connector_team_mismatches,
    )
    from farna.db.crud.pipeline import (
        GraphWriteActor,
        GraphWriteRecording,
        get_pipeline,
        replace_pipeline_graph,
    )
    from farna.db.crud.row_lock import set_mutation_row_lock_timeout
    from farna.db.models.pipeline import Pipeline

    async with _session(org_id) as s:
        # FAR-1361: bound every row-lock wait in this transaction BEFORE its
        # first lock. The MCP graph-update transaction opens its own
        # ``_session`` and never runs the REST layer's in-txn team gate, so
        # without this the graph write's ``SELECT ... FOR UPDATE``
        # (``replace_pipeline_graph``) is an UNBOUNDED wait on a contended
        # pipeline row - a held lock parks a pooled connection indefinitely.
        # The helper is transaction-scoped (``set_config(..., is_local =>
        # true)`` == ``SET LOCAL``) and dialect-gated, so it reverts on
        # COMMIT/ROLLBACK and is a no-op off Postgres; the bound itself is
        # ``Settings.mutation_row_lock_timeout_ms`` (the same one the REST
        # mutation endpoints use).
        await set_mutation_row_lock_timeout(s)

        pipeline = await get_pipeline(s, pid, organisation_id=org_id)
        if pipeline is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
        if _team_scoped_key_mismatch(pipeline.owner_team_id):
            return _team_scope_error("pipeline", pipeline_id)
        # FAR-1845: standardised Pipeline -> Agent lock order. This transaction
        # takes the pipeline row lock FIRST, before *any* write path can touch
        # Agent rows. The FAR-488a sync below UPDATEs Agent rows, and under the
        # session's default autoflush those locks are flushed before the next
        # statement — so syncing first and only then letting
        # ``replace_pipeline_graph`` take the pipeline ``FOR UPDATE`` would
        # acquire Agent -> Pipeline. That inverts the order every REST graph
        # write (and the snapshot-rollback hook) uses, and a concurrent write
        # sharing an Agent row could deadlock (40P01) instead of hitting the
        # bounded-lock 409/503. The later ``replace_pipeline_graph`` lock is
        # re-entrant inside this same transaction.
        locked_pipeline = (
            await s.execute(
                select(Pipeline)
                .where(
                    Pipeline.id == pid,
                    Pipeline.organisation_id == org_id,
                    Pipeline.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_pipeline is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
        # FAR-1181: the graph READ masks env_vars/context_files/parameter
        # values; a full-replace write round-tripping that masked read must
        # not persist the mask literals over the stored secrets. Echoes are
        # resolved against the stored graph before the write commits — the
        # same parity routes/pipelines.py applies on REST graph writes.
        nodes = merge_masked_graph_nodes(nodes, list(pipeline.graph_nodes_json or []))
        try:
            mismatches = await find_connector_team_mismatches(
                s,
                org_id=org_id,
                pipeline_owner_team_id=pipeline.owner_team_id,
                connector_bindings=extract_connector_bindings(nodes),
            )
        except ConnectorBindingMissingError as exc:
            # FAR-1515 CRITICAL 1: a binding the team-blind org-scoped read
            # cannot resolve is the same named refusal the REST surface
            # returns (409 connector_team_mismatch), never a silent skip.
            return {
                "error": CONNECTOR_TEAM_MISMATCH,
                "detail": str(exc),
            }
        if mismatches:
            return {
                "error": CONNECTOR_TEAM_MISMATCH,
                "detail": connector_team_mismatch_detail(mismatches),
            }
        # FAR-1141 FIX 2 / FAR-1395: the MCP full-graph write runs the SAME
        # instance-level checks REST's GraphValidator runs on save — the
        # CI-runner capability for a dispatch binding AND the instance's
        # ``allowed_operations`` scope for every routed verb — so an
        # unauthorised binding is rejected HERE rather than deferred to a
        # run-time failure.
        binding_err = await _reject_invalid_connector_bindings(s, org_id, nodes)
        if binding_err is not None:
            return binding_err
        # FAR-1810 review (MAJOR): the MCP surface is an explicit-save surface,
        # so it must run the SAME FAR-488a agent-command sync the REST graph
        # endpoints run BEFORE the write mints the edit snapshot. The snapshot
        # copy's ``_materialize_agent_fields`` OVERWRITES each bound node's
        # ``agent_commands`` with the Agent-row value at snapshot time, so
        # syncing first makes the live graph the write persists and the new
        # edit snapshot agree — and any rollback replaying that snapshot pins
        # the command the caller saved, not the pre-sync Agent-row value. The
        # sync runs inside this transaction (``_session`` commits it with the
        # graph write), so a failure rolls the whole save back.
        # The helper lives in ``core`` so every writer (REST, MCP, import,
        # rollback) shares ONE implementation; import it from its home rather
        # than through the API module's private alias.
        from farna.core.agent_command_sync import sync_agent_row_commands

        await sync_agent_row_commands(s, org_id=org_id, nodes=nodes, pipeline_id=pid)
        # FAR-309 PR A review: the guardrail-binding strip guard runs in the
        # service layer (replace_pipeline_graph, under the row lock) — the
        # MCP surface inherits it via caller_type="mcp".
        result = await replace_pipeline_graph(
            s,
            pipeline_id=pid,
            org_id=org_id,
            nodes=nodes,
            edges=edges,
            actor=GraphWriteActor(
                is_privileged=is_privileged,
                caller_type="mcp",
                account_id=graph_account_id,
                is_guardrail_admin=is_guardrail_admin,
            ),
            # FAR-1810: an MCP graph update is an explicit save — it records an
            # immutable edit snapshot like the REST graph-write endpoints.
            recording=GraphWriteRecording(record_snapshot=True),
        )
        if result is None:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
        updated_nodes, updated_edges = result
    return updated_nodes, updated_edges


async def _update_pipeline_graph_impl(
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("update_pipeline_graph")

    org_id = _ctx_org_id_val()
    pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
    if pid_err:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    # ADR 047 service-layer backstop + hitl-gate-removal-guard-plan.md v19:
    # the MCP surface is structurally excluded from gate weakening. The
    # guarded function hardcodes is_privileged=False when
    # caller_type=="mcp" (no DB query); the literal below is enforced by a
    # .semgrep/ rule (mcp call site must pass the literal, not a variable).
    from farna.api.routes.pipelines import _is_privileged

    is_privileged = _is_privileged(_ctx_role_val(), _ctx_key_grants.get(None))

    # FAR-309 PR A review: the guardrail-binding strip guard lives in the
    # SERVICE LAYER (replace_pipeline_graph, under the row lock) so the MCP
    # surface inherits it — no separate call-site check. The admin flag is
    # resolved from the caller's org role; for MCP the service layer uses it
    # as-is (the MCP role is resolved at the tool boundary).
    _mcp_is_guardrail_admin = _ctx_role_val() == "admin" and grants_permit(_ctx_key_grants.get(None), "guardrail.manage")

    # FAR-1471: attribute the MCP graph write to the caller's ACCOUNT id, so
    # ``pipeline.graph_updated`` records a real ``changed_by`` instead of
    # null. Every production auth path (API key, OAuth JWT, browser
    # principal) sets ``_ctx_user_id`` to the credential's owning account; a
    # session without one resolves to None and the event honestly records an
    # unattributed write rather than a bogus id — the same posture
    # ``_review_hitl_impl`` uses for its audit actor. Passing the id does NOT
    # widen privilege: ``resolve_effective_privilege`` returns False for
    # ``caller_type == "mcp"`` before it ever looks at ``account_id``, and
    # the guardrail strip guard only re-reads the live role for ``"rest"``.
    try:
        graph_account_id: uuid.UUID | None = _ctx_user_id_val()
    except McpAuthContextError:
        graph_account_id = None

    validation_err = _validate_graph_update(nodes, edges)
    if validation_err is not None:
        return validation_err

    from pydantic import ValidationError as _PydanticValidationError

    from farna.api.routes.pipelines import PipelineGraphUpdate, strip_unset_connector_binding_inputs

    try:
        graph_update = PipelineGraphUpdate.model_validate({"nodes": nodes, "edges": edges})
    except _PydanticValidationError as exc:
        return {
            "error": "validation_failed",
            "detail": f"Graph validation failed: {exc.errors(include_url=False)}",
        }

    # FAR-1141 (CRITICAL 1): PERSIST THE VALIDATED node objects. The old call
    # validated and then DISCARDED the result, keeping the raw dicts, so every
    # model-side effect was thrown away — the forced ``idempotent=false`` on a
    # job-firing dispatch binding never reached an MCP-authored graph, and MCP
    # was the one write surface where "never retried into a second external job"
    # did not hold. ``model_dump(mode="json")`` is exactly what REST's
    # ``_prepare_graph_write`` serialises, so REST and MCP now land the SAME
    # graph: declared fields take the validated value (defaults filled, side
    # effects applied), undeclared keys are dropped on both paths rather than
    # surviving on one (a field only one writer persists is plan drift, not
    # configuration).
    nodes = [strip_unset_connector_binding_inputs(node.model_dump(mode="json")) for node in graph_update.nodes]

    try:
        # The validated ``nodes`` above (FAR-1141) are what the transaction
        # persists; the helper owns the session, the bounded row-lock wait, the
        # masked-graph round-trip and the connector/team gate (FAR-1361/FAR-1181).
        outcome = await _replace_pipeline_graph_txn(
            org_id,
            pid,
            pipeline_id,
            nodes,
            edges,
            is_privileged=is_privileged,
            graph_account_id=graph_account_id,
            is_guardrail_admin=_mcp_is_guardrail_admin,
        )
    except HitlReviewWeakeningDenied as exc:
        await _append_mcp_hitl_denial_audit(org_id, pid, exc)
        return {
            "error": "hitl_review_removal_denied",
            "detail": str(exc),
            "reason_code": exc.reason_code,
            "affected_edges": [{"source_node_id": k[0], "target_node_id": k[1], "edge_type": k[2]} for k in exc.correlation_keys],
        }
    except GuardrailBindingStripDenied as exc:
        return {
            "error": "guardrail_strip_forbidden",
            "detail": exc.detail,
        }
    except ManualNodeOutputSchemaError as exc:
        return {
            "error": "validation_failed",
            "detail": f"Graph validation failed: {exc}",
        }
    except SQLAlchemyError as exc:
        # FAR-1361: the bounded lock above turns a contended graph write into
        # SQLSTATE 55P03 (``lock_not_available``) instead of an unbounded park.
        # Map it here, INSIDE the impl, for two reasons: (1) the tool wrapper's
        # ``except Exception`` would otherwise collapse it into the generic
        # "Failed to update pipeline graph", losing the "another change is in
        # progress, re-issue later" answer the REST layer gives as 409; (2)
        # ``_RETRY_DB`` retries ``OperationalError`` three times with backoff,
        # so letting it escape would multiply the bounded wait before failing.
        # Any OTHER SQLAlchemy error re-raises untouched - ProgrammingError
        # still reaches the wrapper's migration_required arm, everything else
        # still reaches its generic arm.
        if sqlstate_of(exc) == LOCK_NOT_AVAILABLE_SQLSTATE:
            _log.warning(
                "mcp.update_pipeline_graph.lock_timeout",
                extra={"pipeline_id": pipeline_id, "org_id": str(org_id)},
            )
            return {
                "error": "lock_timeout",
                "detail": ("Timed out waiting for a lock on this pipeline; another change is in progress. Re-issue the update once the other change completes."),
                "pipeline_id": pipeline_id,
            }
        raise

    if isinstance(outcome, dict):
        return outcome
    updated_nodes, updated_edges = outcome
    return {
        "pipeline_id": pipeline_id,
        # FAR-1181: mask the response so the MCP write surface cannot echo the
        # stored credentials back (parity with the REST _graph_response).
        "nodes": [mask_pipeline_graph_node(n) if isinstance(n, dict) else n for n in updated_nodes],
        "edges": _serialize_edges(updated_edges),
        "node_count": len(updated_nodes),
        "edge_count": len(updated_edges),
    }


def _snapshot_lock_busy_error(pipeline_id: str) -> dict[str, Any]:
    """The retryable ``snapshot_lock_busy`` envelope for a graph SAVE (FAR-1810).

    A graph save that loses the bounded snapshot-lock race (the graph-copy
    advisory lock held by a concurrent run start) is CONTENTION, not an
    unexpected fault. Before this translation the exception escaped the tool
    wrapper's ``except Exception`` arm and answered a generic ``server_error``,
    reporting a transient busy lock as a server bug. The payload mirrors the
    run-start contract ``trigger_pipeline`` already returns so a caller can
    branch on one code across both surfaces.
    """
    from farna.db.crud.pipeline_snapshot import SNAPSHOT_LOCK_ATTEMPTS

    return {
        "error": "snapshot_lock_busy",
        "pipeline_id": pipeline_id,
        "detail": (f"Pipeline snapshot lock unavailable after {SNAPSHOT_LOCK_ATTEMPTS} attempts — retry the save once the concurrent snapshot completes"),
    }


@mcp.tool(
    description="Set or replace the graph (nodes + edges) of an existing pipeline. "
    "Pass nodes as a list of dicts with id, node_type, agent_id, position (x, y), "
    "and edges as a list of dicts with id, source_node_id, target_node_id, edge_type. "
    "HITL gates (node hitl_config or edge hitl_review_config) must carry a human-provided "
    "description (min 20 chars) explaining why the gate exists; a graph write whose "
    "gates lack one is rejected with validation_failed (FAR-613: the MCP path enforces "
    "the HITL description rule specifically). "
    "Redacted secret values round-trip safely: the credential-bearing node fields "
    "(env_vars, context_files, composite_parameter_values, parameter_overrides) are rendered "
    "as \u2022\u2022\u2022\u2022\u2022\u2022 on read, and a masked value you re-send is resolved back to the "
    "stored secret before the write persists — you CAN update a graph that contains secrets "
    "without re-entering them. Resolution is per node, matched by the node's unchanged id: a "
    "masked echo is only resolved against the stored node with the same id, so a re-id'd "
    "node's masked values are dropped rather than restored. Only a new, non-masked value "
    "overwrites a stored secret; omitting the field drops it, so re-send masked values "
    "unchanged rather than dropping them. "
    "EXPERIMENTAL (FAR-1743 / ADR 037): a "
    "sandbox_agent node may set `model_class` (a registry capability class such as "
    "'builder', resolved to a concrete model id at snapshot mint and consumed via the "
    "top-level `{{ model }}` template variable, e.g. `--model {{ model }}`); the field, "
    "the template variable, and the baked `_resolved_model` / `_model_origin` metadata "
    "are all experimental and may change before de-experimentalisation. Returns the "
    "updated graph."
)
@mcp_audited("pipeline_graph_updated", "pipeline", fail_closed=False)
@_RETRY_DB
async def update_pipeline_graph(
    pipeline_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        return await _update_pipeline_graph_impl(pipeline_id, nodes, edges)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except SnapshotLockNotAvailableError:
        # FAR-1810: an edit snapshot's bounded lock wait losing its race is a
        # retryable contention, not a server bug — without this arm the
        # exception fell through to the generic ``Exception`` arm below.
        _log.warning("update_pipeline_graph snapshot_lock_busy pipeline=%s", pipeline_id)
        return _snapshot_lock_busy_error(pipeline_id)
    except ProgrammingError:
        _log.exception("update_pipeline_graph failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("update_pipeline_graph failed")
        return _tool_exception_error("Failed to update pipeline graph", exc, "mcp.update_pipeline_graph")


def _validate_sandbox_nodes(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Mode-aware sandbox_agent gate over raw node dicts.

    Applies the same shared validation helper the Pydantic model, node runner,
    and GraphValidator use. Imported from the lightweight sandbox_mode module
    (no LangGraph) to keep the API layer import-linter-clean. Returns None if
    all sandbox_agent nodes validate, otherwise the error dict.
    """
    from farna.core.pipeline_engine.sandbox_mode import (
        _validate_sandbox_managed_inputs_config,
        _validate_sandbox_mode_config,
        validate_sandbox_agent_command_jinja,
    )

    for node in nodes:
        if node.get("node_type") == "sandbox_agent":
            try:
                _validate_sandbox_mode_config(node)
            except ValueError as exc:
                return {"error": "validation_failed", "field": "nodes", "detail": str(exc)}
            jinja_err = validate_sandbox_agent_command_jinja(node)
            if jinja_err:
                return {"error": "validation_failed", "field": "nodes", "detail": jinja_err}
            try:
                _validate_sandbox_managed_inputs_config(node)
            except ValueError as exc:
                return {"error": "validation_failed", "field": "nodes", "detail": str(exc)}
    return None


def _bind_connector_ids(
    pipeline_id: str,
    node_id: str,
    connector_instance_id: str,
) -> tuple[uuid.UUID, tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None, dict[str, Any] | None]:
    org_id = _ctx_org_id_val()
    parsed: list[uuid.UUID | None] = []
    for value, field in (
        (pipeline_id, "pipeline_id"),
        (node_id, "node_id"),
        (connector_instance_id, "connector_instance_id"),
    ):
        identifier, error = _parse_uuid_param(value, field)
        if error is not None:
            return org_id, None, error
        parsed.append(identifier)
    if any(identifier is None for identifier in parsed):
        return org_id, None, {"error": "invalid_id", "detail": _MSG_UUID_PARSE_FAILED}
    return org_id, (parsed[0], parsed[1], parsed[2]), None  # type: ignore[return-value]


# FAR-1141: connector-binding operation verbs and CI dispatch selectors the API
# model enforces via Literal — MCP writes raw graph dicts, so it validates the
# same vocabulary itself instead of persisting a value the read path rejects.
_BIND_OPERATIONS = ("query", "write", "dispatch")
_BIND_DISPATCH_ACTIONS = ("trigger_run", "get_run_status", "get_run_logs", "list_runs")


def _validate_binding_arguments(operation: str | None, dispatch_action: str | None) -> dict[str, Any] | None:
    for value, field, choices in (
        (operation, "operation", _BIND_OPERATIONS),
        (dispatch_action, "dispatch_action", _BIND_DISPATCH_ACTIONS),
    ):
        if value is not None and value not in choices:
            return {
                "error": "validation_failed",
                "field": field,
                "detail": f"{field} must be one of {list(choices)}, got {value!r}",
            }
    return None


def _find_graph_node(nodes: list[dict[str, Any]], nid: uuid.UUID) -> dict[str, Any] | None:
    """Return the graph node whose ``id`` equals *nid*, or ``None`` when absent."""
    for node in nodes:
        if uuid.UUID(node["id"]) == nid:
            return node
    return None


def _patched_connector_binding(
    existing: object,
    connector_type: str,
    connector_instance_id: str,
    operation: str | None,
    dispatch_action: str | None,
    resource: str | None,
    filters: dict[str, Any] | None,
    data: dict[str, Any] | None,
) -> dict[str, Any]:
    """Apply the caller's patch to the stored binding (FAR-1141 MAJOR 9 / FAR-1395).

    Keys the caller does not mention keep their stored value; a string
    (including ``""``) or mapping replaces it, and a blank ``resource`` CLEARS
    the key so a binding can be reduced back to its dispatch shape.
    """
    binding: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    binding["type"] = connector_type
    binding["instance_id"] = connector_instance_id
    if operation is not None:
        binding["operation"] = operation
    if dispatch_action is not None:
        binding["dispatch_action"] = dispatch_action
    if resource is not None:
        if resource.strip():
            binding["resource"] = resource
        else:
            binding.pop("resource", None)
    if filters is not None:
        binding["filters"] = dict(filters)
    if data is not None:
        binding["data"] = dict(data)
    return binding


def _apply_node_connector_binding(
    pipeline: Any,
    nid: uuid.UUID,
    node_id: str,
    connector_type: str,
    connector_instance_id: str,
    operation: str | None = None,
    dispatch_action: str | None = None,
    resource: str | None = None,
    filters: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    *,
    connector_supports_dispatch: bool | None = None,
    connector_visibility: str | None = None,
    connector_allowed_operations: object = None,
    connector_type_id: str | None = None,
) -> dict[str, Any] | None:
    """Bind the connector onto the matching node. Returns an error dict, or None.

    FAR-1141 (MAJOR 9): the binding is PATCHED, never rebuilt. The old code
    constructed ``{type, instance_id, operation?, dispatch_action?}`` from
    scratch, so a bind that only carried ``operation`` silently DROPPED an
    existing ``dispatch_action`` / ``input`` — a read-only node could become a
    ``trigger_run`` job-firer (or lose its configured action) without anyone
    asking. Keys the caller does not mention keep their stored value; the
    caller's values win only for the keys it names.

    FAR-1395: ``resource`` / ``filters`` / ``data`` are part of that same
    patch contract — the query/write payload a connector binding must carry for
    the engine to issue a real connector call (``node_runner._connector_inputs``
    reads them; without them a query falls back to ``resource="command"``, which
    most connectors cannot serve). ``None`` leaves the stored value untouched; a
    string (including ``""``) or mapping replaces it, and an empty/blank
    ``resource`` CLEARS the key so a binding can be reduced back to its
    dispatch shape.

    Dispatch invariants are applied with the SAME single helpers the REST path
    and the engine use (``graph_validator.connector_binding_operation`` /
    ``node_fires_dispatch_job``): a ``dispatch`` node must route the dispatch
    verb, a dispatch binding must target a connector whose type implements the
    CI-runner contract (the caller resolves that from the instance row —
    ``None`` means the caller did not resolve it and the check is skipped), and
    a job-firing binding is persisted ``idempotent=false`` exactly as the REST
    model forces it.

    *connector_visibility* / *connector_allowed_operations* / *connector_type_id*
    are the resolved instance row's operation-scope inputs (FAR-1395). The
    production bind path always supplies them, which makes the write enforce the
    same ``allowed_operations`` gate the run-time ``ConnectorACL`` will; passing
    ``connector_visibility=None`` (the default, used by unit tests that are not
    exercising the scope gate) skips it rather than assuming an allowlist.

    A ``connector`` node with a ``query`` / ``write`` verb must end up with a
    non-empty ``resource`` — the same rule the REST ``PipelineGraphNode`` model
    enforces on save — so the bind path cannot author the binding shape the
    REST surface would reject.
    """
    validation_error = _validate_binding_arguments(operation, dispatch_action)
    if validation_error is not None:
        return validation_error
    # WORK ON A DEEP COPY — never mutate the ORM-loaded value in place.
    # ``pipelines.graph_nodes_json`` is a plain JSON column (no Mutable
    # wrapper), so an in-place edit of a loaded node dict is invisible to the
    # unit of work: the reassignment below then handed SQLAlchemy a list whose
    # ELEMENTS were the very dicts the old value still points at, ``old ==
    # new``, history came back UNCHANGED and the flush issued NO UPDATE at all.
    # The tool answered ``status: "bound"`` while the stored graph never
    # changed — and the edit snapshot minted from the in-memory object then
    # disagreed with the row. Observed against the real ORM before this copy:
    # ``attributes.get_history(pipeline, "graph_nodes_json").has_changes()``
    # was False after a successful bind, and the committed row had no
    # ``connector_binding``. The copy also makes the caller's no-op comparison
    # (FAR-1810) genuinely pre-state vs post-state.
    nodes = copy.deepcopy(list(pipeline.graph_nodes_json)) if pipeline.graph_nodes_json else []
    target = _find_graph_node(nodes, nid)
    if target is None:
        return {"error": "node_not_found", "detail": f"Node {node_id} not found in pipeline graph"}

    from farna.connectors.base import (
        connector_binding_denied_acl_operations,
        connector_binding_operation,
        node_fires_dispatch_job,
        node_routes_binding_to_connector,
    )

    binding = _patched_connector_binding(
        target.get("connector_binding"),
        connector_type,
        connector_instance_id,
        operation,
        dispatch_action,
        resource,
        filters,
        data,
    )
    probe = {**target, "connector_binding": binding}

    # FAR-1141 FIX 2: a DECLARED ``dispatch`` verb on a node the engine does
    # NOT route to a connector (an ``agent`` node with an ``agent_id``, a
    # ``sandbox_agent``) must be REJECTED, never silently coerced to the
    # resolver's ``query`` fallback — the resolver answers "query" for those
    # shapes because the engine will never read the binding, so persisting its
    # verdict would record a dispatch the caller asked for as a query nobody
    # asked for, with nothing ever firing. The Pydantic model rejects the same
    # shape on the full-graph write (and REST); this bind path writes raw node
    # dicts, so it enforces the rule itself.
    routing_error = _validate_binding_routing(target, probe, node_id, node_routes_binding_to_connector)
    if routing_error is not None:
        return routing_error

    # Resolve the verb exactly as the engine will route it, delegating to the
    # SHARED resolver (FAR-1141 FIX 6: ``connectors.base.connector_binding_operation``
    # is the single source of truth for the explicit-value-wins /
    # node-type-fallback default — never a second copy of the rule here), then
    # persist the resolution so the stored graph never relies on a
    # reader-specific default.
    effective_operation = connector_binding_operation(probe)
    binding["operation"] = effective_operation
    # ...and the dispatch node's own requirement, also expressed through that
    # resolver's verdict rather than a re-derived default.
    effective_error = _validate_effective_binding(
        target,
        effective_operation,
        connector_type,
        connector_supports_dispatch,
    )
    if effective_error is not None:
        return effective_error
    if str(target.get("node_type") or "") == "connector" and effective_operation != "dispatch" and not str(binding.get("resource") or "").strip():
        return {
            "error": "validation_failed",
            "field": "resource",
            "detail": (f"Connector node {node_id} requires a non-empty 'resource' for a {effective_operation!r} operation — without one the connector is called with an empty resource at run time"),
        }
    if connector_visibility is not None:
        # FAR-1395: the write enforces the instance's own ``allowed_operations``
        # with the SAME helper the run-time hub uses, so a binding this surface
        # persists can never be one the run then denies.
        denied = connector_binding_denied_acl_operations(
            probe,
            visibility=connector_visibility,
            allowed_operations=connector_allowed_operations,
            connector_type_id=connector_type_id,
        )
        if denied:
            return {
                "error": "validation_failed",
                "field": "connector_binding",
                "detail": (f"Connector instance {connector_instance_id} does not allow {', '.join(repr(op) for op in denied)} required by the node's {effective_operation!r} operation"),
            }
    if node_fires_dispatch_job(probe):
        # Parity with the REST model's forced ``idempotent=false``: the binding
        # fires a job on the customer's substrate, so the node is never safe to
        # re-run (and the executor now derives this from the binding too).
        target["idempotent"] = False
    target["connector_binding"] = binding
    pipeline.graph_nodes_json = nodes
    return None


@mcp.tool(
    description="Bind a connector instance to a pipeline node. "
    "Updates the node's connector_binding in the pipeline graph. "
    "The connector must already exist in the organisation. "
    "Optional operation ('query' | 'write' | 'dispatch') selects the binding "
    "verb; optional dispatch_action ('trigger_run' | 'get_run_status' | "
    "'get_run_logs' | 'list_runs') selects the CI-runner method for a dispatch "
    "binding (FAR-1141). Optional resource / filters / data supply the "
    "query/write payload the engine passes to the connector (FAR-1395): "
    "resource names the connector resource to fetch, filters narrow it, and "
    "data carries the write body. Omitted values leave the stored binding "
    "untouched; the connector's allowed_operations are enforced on write."
)
@mcp_audited("pipeline_node_connector_bound", "pipeline", fail_closed=False)
@_RETRY_DB
async def bind_connector_to_node(  # NOSONAR S3776 - orchestration endpoint delegates validation/persistence helpers
    pipeline_id: str,
    node_id: str,
    connector_type: str,
    connector_instance_id: str,
    operation: str | None = None,
    dispatch_action: str | None = None,
    resource: str | None = None,
    filters: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("bind_connector_to_node")

        from farna.db.crud.connector_instance import get_connector_instance

        org_id, ids, id_error = _bind_connector_ids(pipeline_id, node_id, connector_instance_id)
        if id_error is not None:
            return id_error
        # _bind_connector_ids returns (None, error) only on failure, already
        # handled above; cast narrows for mypy without an assert inside the
        # except-Exception block (SonarCloud S5779).
        pid, nid, cid = cast(tuple[uuid.UUID, uuid.UUID, uuid.UUID], ids)

        async with _session(org_id) as s:
            # Verify connector exists in org
            connector = await get_connector_instance(s, cid)
            if connector is None or connector.organisation_id != org_id:
                return {"error": "connector_not_found", "detail": "Connector not found in this organisation"}

            # Get pipeline and update node
            from sqlalchemy import select

            from farna.db.models.pipeline import Pipeline

            pipeline = (await s.execute(select(Pipeline).where(Pipeline.id == pid).with_for_update())).scalar_one_or_none()
            if pipeline is None:
                return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

            if _team_scoped_key_mismatch(pipeline.owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)

            from farna.core.team_visibility import (
                CONNECTOR_TEAM_MISMATCH,
                ConnectorTeamMismatch,
                connector_team_mismatch,
                connector_team_mismatch_detail,
            )

            if connector_team_mismatch(connector.visibility, connector.owner_team_id, pipeline.owner_team_id):
                # FAR-1515 / FAR-1618: route through the shared detail builder
                # so this surface names the same fix as the REST save path —
                # a team-private connector reaching outside its owner team.
                # (An org-visibility connector is shared across the
                # organisation and never reaches this branch.)
                return {
                    "error": CONNECTOR_TEAM_MISMATCH,
                    "detail": connector_team_mismatch_detail(
                        [
                            ConnectorTeamMismatch(
                                connector_id=cid,
                                connector_name=connector.name,
                                connector_owner_team_id=connector.owner_team_id,
                                pipeline_owner_team_id=pipeline.owner_team_id,
                                node_id=node_id,
                            )
                        ]
                    ),
                }

            # FAR-1141 (MAJOR 8/9): resolve the CI-runner capability from the
            # INSTANCE row (the authoritative connector type) so a dispatch
            # binding to a Linear/Slack connector is rejected here, at bind
            # time, instead of failing at run time with an AttributeError-shaped
            # error. Passed through so the same invariant the REST graph-save
            # validator enforces also holds on this write path.
            from farna.connectors.base import connector_type_supports_dispatch

            # FAR-1810: capture the pre-bind node payload for no-op suppression
            # (a re-bind that changes nothing must not push a snapshot).
            #
            # The copy MUST be DEEP (the exact pattern ``replace_pipeline_graph``
            # uses for the same comparison): ``_apply_node_connector_binding``
            # builds its working list the same shallow way and then mutates each
            # target node dict IN PLACE (``target["connector_binding"] = ...``).
            # A shallow ``list(...)`` copies only the container, so every element
            # is the SAME dict object the post-bind list holds — the comparison
            # below would then compare the mutated graph against ITSELF, read
            # "unchanged" for EVERY bind, and silently suppress every snapshot
            # (making the fail-closed ``snapshot is None`` guard underneath it
            # unreachable on this surface).
            previous_nodes = copy.deepcopy(list(pipeline.graph_nodes_json or []))

            # FAR-1471: attribute the write to the caller's ACCOUNT id through
            # the same ladder ``_update_pipeline_graph_impl`` uses. Every
            # production auth path sets ``_ctx_user_id`` to the credential's
            # owning account; a session without one RAISES here and is caught,
            # so the snapshot is written honestly unattributed (account_id NULL)
            # with a warning, never silently attributed to nobody.
            try:
                bind_account_id: uuid.UUID | None = _ctx_user_id_val()
            except McpAuthContextError:
                _log.warning(
                    "mcp.bind_connector_to_node.unattributed_write pipeline_id=%s: no authenticated user context for this MCP request",
                    pipeline_id,
                )
                bind_account_id = None

            bind_error = _apply_node_connector_binding(
                pipeline,
                nid,
                node_id,
                connector_type,
                connector_instance_id,
                operation=operation,
                dispatch_action=dispatch_action,
                resource=resource,
                filters=filters,
                data=data,
                connector_supports_dispatch=connector_type_supports_dispatch(connector.connector_type_id),
                # FAR-1395: the instance's own operation scope is supplied from
                # the resolved row, so this bind enforces the same
                # ``allowed_operations`` allowlist the run-time hub does.
                connector_visibility=connector.visibility,
                connector_allowed_operations=connector.allowed_operations,
                connector_type_id=connector.connector_type_id,
            )
            if bind_error is not None:
                return bind_error
            await s.flush()

            # FAR-1810: a connector bind mutates node binding state in the graph,
            # so it records an edit snapshot in the SAME transaction — the bind is
            # a real graph edit and must be restorable. The pipeline row is held
            # FOR UPDATE, so the snapshot's allocation lock is self-reentrant (see
            # ``_acquire_snapshot_allocation_lock``). Fail closed: a snapshot error
            # propagates to the handler's error envelope and rolls the bind back.
            from farna.db.crud.pipeline import _graph_content_unchanged
            from farna.db.crud.pipeline_snapshot import create_snapshot_edit

            if not _graph_content_unchanged(previous_nodes, [], list(pipeline.graph_nodes_json or []), []):
                snapshot = await create_snapshot_edit(s, pipeline_id=pid, account_id=bind_account_id)
                if snapshot is None:
                    raise RuntimeError(f"connector bind on pipeline {pid} could not record its edit snapshot; refusing to commit an unversioned graph write")
                _log.info(
                    "pipeline.connector_bind_snapshot_created pipeline_id=%s snapshot_version=%s",
                    pid,
                    snapshot.snapshot_version,
                )

        response: dict[str, Any] = {
            "pipeline_id": pipeline_id,
            "node_id": node_id,
            "connector_type": connector_type,
            "connector_instance_id": connector_instance_id,
            "status": "bound",
        }
        if operation is not None:
            response["operation"] = operation
        if dispatch_action is not None:
            response["dispatch_action"] = dispatch_action
        # FAR-1395: echo the query/write payload the caller supplied so a
        # round-trip read (get_pipeline_graph) and the tool response agree.
        if resource is not None:
            response["resource"] = resource
        if filters is not None:
            response["filters"] = filters
        if data is not None:
            response["data"] = data
        return response
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except SnapshotLockNotAvailableError:
        # FAR-1810: the bind's edit snapshot can lose the bounded lock race to
        # a concurrent run start — a retryable contention, never a generic
        # server_error (the transaction rolls the bind back with it).
        _log.warning("bind_connector_to_node snapshot_lock_busy pipeline_id=%s", pipeline_id)
        return _snapshot_lock_busy_error(pipeline_id)
    except ProgrammingError:
        _log.exception("bind_connector_to_node failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("bind_connector_to_node failed")
        return _tool_exception_error("Failed to bind connector to node", exc, "mcp.bind_connector_to_node")


@mcp.tool(
    description=("List the hitl_review_config blocks on a pipeline's committed graph edges (source/target node ids + full config). Read-only: the graph is modified via update_pipeline_graph, never by this tool."),
)
@_RETRY_DB
async def get_pipeline_reviews(pipeline_id: str) -> dict[str, Any]:
    try:
        return await _get_pipeline_reviews_impl(pipeline_id)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_pipeline_reviews failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_pipeline_reviews failed")
        return _tool_exception_error("Failed to get pipeline reviews", exc, "mcp.get_pipeline_reviews")


async def _get_pipeline_reviews_impl(pipeline_id: str) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_pipeline_reviews")
    from farna.db.crud.pipeline import get_pipeline_graph

    org_id = _ctx_org_id_val()
    pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
    if pid_err:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        owner_team_id = await _pipeline_owner_team_id(s, pid)
        if _team_scoped_key_mismatch(owner_team_id):
            return _team_scope_error("pipeline", pipeline_id)
        result = await get_pipeline_graph(s, pid, organisation_id=org_id)
    if result is None:
        return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}
    _nodes, edges = result
    gates = [
        {
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
            "hitl_review_config": dict(e.hitl_review_config),
        }
        for e in edges
        if isinstance(e.hitl_review_config, dict)
    ]
    return {"pipeline_id": pipeline_id, "reviews": gates, "review_count": len(gates)}


@mcp.tool(description="Delete a pipeline by ID.")
@mcp_audited("pipeline_deleted", "pipeline", fail_closed=True)
@_RETRY_DB
async def delete_pipeline(
    pipeline_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_pipeline")

        org_id = _ctx_org_id_val()
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": f"Invalid UUID format: {pipeline_id}"}

        from farna.db.crud.pipeline import soft_delete_pipeline

        async with _session(org_id) as s:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            deleted = await soft_delete_pipeline(s, pid, organisation_id=org_id)

        if not deleted:
            return {"error": "pipeline_not_found", "pipeline_id": pipeline_id}

        return {"status": "deleted", "pipeline_id": pipeline_id}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_pipeline failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("delete_pipeline failed")
        return _tool_exception_error("Failed to delete pipeline", exc, "mcp.delete_pipeline")


def _mask_diff_snapshot_graph(graph: Any) -> Any:
    """Mask the ``nodes`` of one snapshot graph, leaving a non-dict graph untouched."""
    if not isinstance(graph, dict):
        return graph
    graph_copy = dict(graph)
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        graph_copy["nodes"] = [mask_pipeline_graph_node(n) if isinstance(n, dict) else n for n in nodes]
    return graph_copy


def _mask_pipeline_diff_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Mask node credentials across a snapshot-diff payload (REST parity, FAR-1181)."""
    masked: dict[str, Any] = dict(result)
    for key in ("snapshot_a", "snapshot_b"):
        snapshot_payload = masked.get(key)
        if isinstance(snapshot_payload, dict):
            masked[key] = {
                **snapshot_payload,
                "graph": _mask_diff_snapshot_graph(snapshot_payload.get("graph")),
            }
    for key in ("nodes_added", "nodes_removed"):
        entries = masked.get(key)
        if isinstance(entries, list):
            masked[key] = [mask_pipeline_graph_node(n) if isinstance(n, dict) else n for n in entries]
    return masked


@mcp.tool(
    name="get_pipeline_diff",
    description=("Diff two snapshots of a pipeline (mirrors POST /api/v1/pipelines/{pipeline_id}/snapshots/diff). Node credentials in the snapshot graphs are masked, exactly like the REST surface."),
)
@_RETRY_DB
async def get_pipeline_diff(
    pipeline_id: str,
    snapshot_a_id: str,
    snapshot_b_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_pipeline_diff")

        org_id = _ctx_org_id_val()
        pid, pid_err = _parse_uuid_param(pipeline_id, "pipeline_id")
        if pid_err:
            return pid_err
        if pid is None:
            return {"error": "invalid_id", "field": "pipeline_id", "detail": _MSG_UUID_PARSE_FAILED}
        snap_a, snap_a_err = _parse_uuid_param(snapshot_a_id, "snapshot_a_id")
        if snap_a_err:
            return snap_a_err
        if snap_a is None:
            return {"error": "invalid_id", "field": "snapshot_a_id", "detail": _MSG_UUID_PARSE_FAILED}
        snap_b, snap_b_err = _parse_uuid_param(snapshot_b_id, "snapshot_b_id")
        if snap_b_err:
            return snap_b_err
        if snap_b is None:
            return {"error": "invalid_id", "field": "snapshot_b_id", "detail": _MSG_UUID_PARSE_FAILED}

        from farna.db.crud.pipeline_snapshot_versioning import diff_snapshots

        async with _session(org_id) as s:
            # Team-scoped-key boundary (same pattern as get_pipeline_graph).
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", pipeline_id)
            result = await diff_snapshots(s, snap_a, snap_b, organisation_id=org_id, pipeline_id=pid)

        if result is None:
            return {"error": "not_found", "detail": "One or both snapshots not found"}

        # FAR-1181 (echo of the REST parity in routes/pipelines.py): the
        # diff payload carries raw node dicts copied from the stored
        # snapshots — mask them exactly like the snapshot-detail graph read.
        return _mask_pipeline_diff_payload(result)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_pipeline_diff failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_pipeline_diff failed")
        return _tool_exception_error("Failed to diff pipeline snapshots", exc, "mcp.get_pipeline_diff")


@mcp.resource("modulo://pipelines")
async def resource_pipelines() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the list_pipelines tool — same node-scope gate.
    denied = _resource_node_scope_error("list_pipelines")
    if denied is not None:
        return denied
    from farna.db.crud.pipeline import list_pipelines

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await list_pipelines(s, page=1, page_size=50, team_id=_ctx_team_id_val(), organisation_id=org_id)
    lines = [f"- {p.name} (id={p.id}, visibility={p.visibility})" for p in result.items]
    return f"Pipelines ({result.total} total):\n" + "\n".join(lines)


@mcp.resource("modulo://pipelines/{pipeline_id}")
async def resource_pipeline_detail(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: exposes the pipeline metadata list_pipelines returns
    # (name/id/visibility/owners) plus structural counts — gate under it.
    denied = _resource_node_scope_error("list_pipelines")
    if denied is not None:
        return denied
    from sqlalchemy import func, select

    from farna.db.models.pipeline_snapshot import PipelineSnapshot
    from farna.db.models.run import Run

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

        edge_result = await s.execute(select(func.count()).select_from(PipelineEdge).where(PipelineEdge.pipeline_id == pid))
        edge_count = edge_result.scalar_one()

        snap_result = await s.execute(select(func.count()).select_from(PipelineSnapshot).where(PipelineSnapshot.pipeline_id == pid))
        snapshot_count = snap_result.scalar_one()

        run_result = await s.execute(select(Run.created_at).where(Run.pipeline_id == pid).order_by(Run.created_at.desc()).limit(1))
        last_run_at = run_result.scalar_one_or_none()

    parts = [
        f"Pipeline: {pipeline.name}",
        f"ID: {pipeline.id}",
        f"Description: {pipeline.description or '(none)'}",
        f"Status: {'active' if pipeline.graph_nodes_json else 'inactive'}",
        f"Visibility: {pipeline.visibility}",
        f"Business owner: {_owner_id_str(getattr(pipeline, 'business_owner_id', None)) or '(none)'}",
        f"Reliability owner: {_owner_id_str(getattr(pipeline, 'reliability_owner_id', None)) or '(none)'}",
        f"Created: {pipeline.created_at.isoformat()}",
        f"Node count: {len(pipeline.graph_nodes_json)}",
        f"Edge count: {edge_count}",
        f"Snapshot count: {snapshot_count}",
    ]
    if last_run_at:
        parts.append(f"Last run: {last_run_at.isoformat()}")
    return "\n".join(parts)


@mcp.resource("modulo://pipelines/{pipeline_id}/snapshots")
async def resource_pipeline_snapshots(pipeline_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: deliberately NOT node-scope gated — no gated tool lists a
    # pipeline's snapshots (``get_pipeline_diff`` only diffs two identified
    # snapshots; ``get_pipeline_graph`` returns the live graph). This resource
    # exposes snapshot ids/versions/tags only, never graph content.
    from farna.db.crud.pipeline_snapshot_versioning import list_snapshots

    org_id = _ctx_org_id_val()
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return f"error: Invalid pipeline_id UUID: {pipeline_id}"

    async with _session(org_id) as s:
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, pid)):
            return _team_scope_error_str("pipeline", pipeline_id)
        snapshots, _ = await list_snapshots(s, pid, page=1, page_size=50, organisation_id=org_id)

    lines = [f"- snapshot {s.snapshot_version} (id={s.id}, tag={s.tag or ''}, created={s.created_at.isoformat()})" for s in snapshots]
    return f"Snapshots for pipeline {pipeline_id} ({len(snapshots)}):\n" + "\n".join(lines)


def _render_snapshot_node_line(n: dict[str, Any]) -> str:
    """Render a single node summary line for MCP resource output."""
    nid = n.get("id", "?")
    ntype = n.get("node_type", "?")
    agent_id = n.get("agent_id", "")
    agent_cmd = n.get("agent_commands", ["(required)"])
    prompt_preview = (n.get("prompt_template", "") or "")[:80].replace("\n", " ")
    line = f"  - {nid} (type={ntype}, agent={agent_id}, command={agent_cmd})\n"
    if prompt_preview:
        line += f"    prompt: {prompt_preview}...\n"
    return line


def _render_snapshot_node_details(nodes: list[dict[str, Any]]) -> str:
    """Render the full node JSON plus prompt/command previews."""
    result = ""
    for n in nodes:
        safe = {k: v for k, v in n.items() if k not in ("agent_prompt", "agent_commands")}
        result += json.dumps(safe, indent=2, default=str)[:2000] + "\n"
        ap = n.get("agent_prompt")
        if ap is None:
            ap = ""
        if ap:
            result += f"    agent_prompt: {ap[:200].replace(chr(10), ' ')}...\n"
        ac = n.get("agent_commands", []) or []
        if ac:
            result += f"    agent_commands: {' && '.join(str(c) for c in ac)[:200].replace(chr(10), ' ')}...\n"
        cf = n.get("context_files", {}) or {}
        for cfp, cfc in cf.items():
            result += f"    context_file {cfp}: {len(str(cfc))} bytes\n"
        tid = n.get("template_id", "")
        if tid:
            result += f"    template_id: {tid}\n"
    return result


@mcp.resource("modulo://pipelines/{pipeline_id}/snapshots/{snapshot_id}")
async def resource_pipeline_snapshot_detail(pipeline_id: str, snapshot_id: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: the payload is a HISTORICAL SNAPSHOT's graph (nodes/edges plus
    # connector bindings), so this resource needs BOTH capabilities: the
    # live-graph capability (get_pipeline_graph) and the snapshot/diff
    # capability (get_pipeline_diff). Requiring both is fail-closed — a node
    # allowed one but not the other must not read snapshot content through
    # resources/read.
    denied = _resource_node_scope_error("get_pipeline_graph")
    if denied is not None:
        return denied
    denied = _resource_node_scope_error("get_pipeline_diff")
    if denied is not None:
        return denied
    from farna.db.crud.pipeline_snapshot_versioning import get_snapshot_detail

    org_id = _ctx_org_id_val()
    try:
        uuid.UUID(pipeline_id)
        sid = uuid.UUID(snapshot_id)
    except ValueError:
        return "error: Invalid UUID format"

    async with _session(org_id) as s:
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, uuid.UUID(pipeline_id))):
            return _team_scope_error_str("pipeline", pipeline_id)
        snap = await get_snapshot_detail(s, sid, organisation_id=org_id, pipeline_id=uuid.UUID(pipeline_id))

    if snap is None:
        return f"error: Snapshot {snapshot_id} not found"

    # Mask credential-bearing node fields (env_vars, context_files,
    # composite_parameter_values, parameter_overrides) exactly as the REST
    # snapshot-detail and MCP get_pipeline_graph read paths do: snapshots
    # store the graph with real stored values (mask echoes are resolved back
    # to secrets on write), so raw serialization would leak them.
    nodes = [mask_pipeline_graph_node(dict(n)) if isinstance(n, dict) else n for n in snap.graph_json.get("nodes", [])]
    edges = snap.graph_json.get("edges", [])
    result = f"Snapshot {snapshot_id} (v{snap.snapshot_version}) for pipeline {pipeline_id}\n"
    result += f"Nodes ({len(nodes)}):\n"
    result += "".join(_render_snapshot_node_line(n) for n in nodes)
    result += f"Edges ({len(edges)}):\n"
    for e in edges:
        result += f"  - {e.get('id', '?')}: {e.get('source', '?')} -> {e.get('target', '?')} ({e.get('type', '?')})\n"
    result += "  Full node JSON:\n"
    result += _render_snapshot_node_details(nodes)
    result += f"Connector bindings: {json.dumps(snap.connector_bindings_json, indent=2)}\n"
    return result


# Keep the suite's `patch.object(farna.api.mcp_server, "<helper>")` bindings
# live for the wrappers above: each call re-resolves the helper from
# `mcp_server`, which re-exports THIS module's original through PEP 562.
_apply_owner_team_pipeline = patchable_local("_apply_owner_team_pipeline", _apply_owner_team_pipeline)
_reject_invalid_connector_bindings = patchable_local("_reject_invalid_connector_bindings", _reject_invalid_connector_bindings)
_replace_pipeline_graph_txn = patchable_local("_replace_pipeline_graph_txn", _replace_pipeline_graph_txn)
_update_pipeline_graph_impl = patchable_local("_update_pipeline_graph_impl", _update_pipeline_graph_impl)
_validate_graph_update = patchable_local("_validate_graph_update", _validate_graph_update)
