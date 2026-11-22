"""Eval definitions and eval suites MCP tools.

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

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _EVAL_NAME_MAX_LENGTH,
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_EVAL_DEF_CREATE_FAILED,
    _MSG_EVAL_DEF_DELETE_FAILED,
    _MSG_EVAL_DEF_UPDATE_FAILED,
    _MSG_TOKEN_REVOKED,
    _MSG_UUID_PARSE_FAILED,
    _RETRY_DB,
    _ctx_key_grants,
    _log,
    _parse_optional_uuid,
    _team_scope_error,
    _tool_auth_error,
    _tool_db_shell,
    _tool_exception_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    _parse_uuid_param,
    _session,
    _team_scoped_key_mismatch,
    validate_current_auth,
)
from farna.api.routes.evals import _EVAL_TYPE_PATTERN
from farna.auth.permissions import grants_permit, resolve_required
from farna.auth.team_rbac import ORG_ROLE_HIERARCHY
from farna.core.mcp.scope_validator import MCPAuthorizationError


@mcp.tool(
    description="List eval definitions with cursor-based pagination. Optionally filter by pipeline_id.",
)
@_RETRY_DB
async def list_eval_definitions(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_eval_definitions")

        org_id = _ctx_org_id_val()
        # FAR-1540: malformed filter id -> ``invalid_id``, not ``internal_error``.
        pid, pid_err = _parse_optional_uuid(pipeline_id, "pipeline_id")
        if pid_err is not None:
            return pid_err
        lim = max(1, min(limit, 100))

        from farna.db.crud.pagination import CursorPaginator
        from farna.db.models.eval import Eval

        async with _session(org_id) as s:
            q = select(Eval).where(
                Eval.organisation_id == org_id,
                Eval.deleted_at.is_(None),
            )
            if pid is not None:
                q = q.where(Eval.pipeline_id == pid)

            paginator = CursorPaginator(sort_field="name", sort_dir="asc")
            page = await paginator.paginate(
                s,
                q,
                cursor=cursor,
                limit=lim,
                model=Eval,
                compute_total=True,
            )
            rows = page.items

        return {
            "data": [
                {
                    "id": str(d.id),
                    "name": d.name,
                    "type": d.eval_type,
                    "pipeline_id": str(d.pipeline_id),
                    "pass_threshold": float(d.pass_threshold) if d.pass_threshold is not None else None,
                    "suite_id": d.suite_id,
                }
                for d in rows
            ],
            "total": page.total,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_eval_definitions failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_eval_definitions failed")
        return _tool_exception_error("Failed to list eval definitions", exc, "mcp.list_eval_definitions")


def _assert_eval_type(eval_type: str) -> dict[str, Any] | None:
    """Validate an eval_type value, returning an error dict or None."""
    if not re.fullmatch(_EVAL_TYPE_PATTERN, eval_type):
        return {
            "error": "invalid_eval_type",
            "detail": "eval_type must be one of: model_judge|regex|json_schema|custom_function|guardrail|human_set",
        }
    return None


def _assert_pass_threshold(pass_threshold: float | None) -> dict[str, Any] | None:
    """Validate a pass_threshold value, returning an error dict or None."""
    if pass_threshold is not None and not (0.0 <= pass_threshold <= 1.0):
        return {
            "error": "invalid_pass_threshold",
            "detail": "pass_threshold must be between 0.0 and 1.0",
        }
    return None


def _assert_gate_write_scope(permission: str) -> None:
    """Apply a PolicyGate permission to the combined MCP eval-write path."""
    role = _ctx_role_val()
    if ORG_ROLE_HIERARCHY.get(role or "", -1) < ORG_ROLE_HIERARCHY[resolve_required(permission)]:
        raise MCPAuthorizationError(f"Permission '{permission}' is required for this policy-gate action")
    if not grants_permit(_ctx_key_grants.get(None), permission):
        raise MCPAuthorizationError(f"Permission '{permission}' is not granted to this credential")


def _assert_gate_transition_scope(current_action: str | None, new_action: str, reason: str | None) -> str | None:
    """Enforce the transition-specific gate permission and normalize its reason."""
    if new_action not in {"warn", "block"}:
        raise MCPAuthorizationError("PolicyGate action must be 'warn' or 'block'")
    if current_action == new_action or current_action is None:
        if current_action is None and new_action == "block":
            _assert_gate_write_scope("gate.enforcement.raise")
        return (reason or "").strip() or None
    if current_action == "warn" and new_action == "block":
        _assert_gate_write_scope("gate.enforcement.raise")
        return (reason or "").strip() or None
    if current_action == "block" and new_action == "warn":
        _assert_gate_write_scope("gate.enforcement.weaken")
        normalized = (reason or "").strip()
        if not normalized:
            raise MCPAuthorizationError("A non-blank reason is required when weakening a policy gate")
        return normalized
    raise MCPAuthorizationError("Unsupported PolicyGate action transition")


def _assert_eval_definition_write_scope(action: str, minimum_role: str) -> None:
    """Raise MCPAuthorizationError when the caller is below ``minimum_role``.

    Eval-definition create/update is operator-or-admin (FAR-735): the ADR 047
    registry already resolves ``eval.definition.create`` / ``eval.definition.update``
    to ``operator`` for the MCP tool-scope gate, so the tool-level gate here
    must match or an operator key would pass the scope gate and then be
    rejected in the tool banner. Delete stays admin-only (mirrors the REST
    ``DELETE /evals/{eval_id}`` route). A below-floor caller must receive
    ``insufficient_scope`` rather than a silent success.
    """
    if ORG_ROLE_HIERARCHY.get(_ctx_role_val() or "", -1) < ORG_ROLE_HIERARCHY[minimum_role]:
        role_label = "admins" if minimum_role == "admin" else "operators or admins"
        raise MCPAuthorizationError(f"Only {role_label} can {action} eval definitions")


def _parse_eval_ref_ids(
    primary_value: str,
    primary_field: str,
    node_id: str | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, dict[str, Any] | None]:
    """Parse a primary eval-reference UUID plus an optional node UUID.

    Returns ``(primary, node, None)`` on success or ``(primary_or_None, None,
    error_dict)`` when parsing fails.
    """
    primary, primary_err = _parse_uuid_param(primary_value, primary_field)
    if primary_err is not None:
        return None, None, primary_err
    node: uuid.UUID | None = None
    if node_id is not None:
        node, node_err = _parse_uuid_param(node_id, "node_id")
        if node_err is not None:
            return primary, None, node_err
    return primary, node, None


async def _load_eval_def(s: AsyncSession, org_id: uuid.UUID, eid: uuid.UUID) -> Any:
    """Load an org-scoped ``Eval`` row; ``None`` when the row does not exist.

    Return type is ``Eval | None`` (from ``modulo.db.models.eval``); written
    as ``Any`` because the import is deferred inside the function body.

    Consolidated single loader (chunk 3b cutover): reads from the ``evals``
    table instead of ``eval_definitions``.  Used by the update (W6) and
    delete (W9) impls.
    """
    from farna.db.models.eval import Eval
    from farna.db.soft_delete import include_soft_deleted

    return (
        await s.execute(
            include_soft_deleted(
                select(Eval).where(
                    Eval.id == eid,
                    Eval.organisation_id == org_id,
                )
            )
        )
    ).scalar_one_or_none()


def _assert_create_eval_definition_params(
    name: str,
    eval_type: str,
    pass_threshold: float | None,
) -> dict[str, Any] | None:
    """Validate the scalar create-inputs of an eval definition; error dict or None."""
    if not name or not name.strip():
        return {"error": "invalid_name", "detail": "name must be a non-empty string"}
    if len(name) > _EVAL_NAME_MAX_LENGTH:
        return {
            "error": "invalid_name",
            "detail": f"name must be at most {_EVAL_NAME_MAX_LENGTH} characters",
        }
    if (err := _assert_eval_type(eval_type)) is not None:
        return err
    return _assert_pass_threshold(pass_threshold)


async def _create_eval_definition_impl(
    pipeline_id: str,
    node_id: str | None,
    name: str,
    eval_type: str,
    config_json: dict[str, Any] | None,
    pass_threshold: float | None,
    suite_id: str | None,
    failure_behaviour: str = "warn",
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist a new eval definition (an ``Eval`` row); shared with the MCP tool wrapper.

    Redirected to write to Eval+PolicyGate via the shared helper (FAR-1101 chunk 3b).
    """
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("create_eval_definition")

    from farna.api.constants import MSG_PIPELINE_NOT_FOUND
    from farna.api.routes.evals import _eval_def_to_dict

    if (err := _assert_create_eval_definition_params(name, eval_type, pass_threshold)) is not None:
        return err

    _assert_eval_definition_write_scope("create", "operator")
    if node_id is not None:
        _assert_gate_write_scope("gate.create")
        _assert_gate_transition_scope(None, failure_behaviour, reason)

    org_id = _ctx_org_id_val()
    account_id = _ctx_user_id_val()

    pid, nid, pid_err = _parse_eval_ref_ids(pipeline_id, "pipeline_id", node_id)
    if pid_err is not None:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    cfg = config_json if config_json is not None else {}

    from starlette.exceptions import HTTPException as StarletteHTTPException

    from farna.core.eval_engine.eval_definition_write import create_or_update_eval, validate_guardrail_request
    from farna.core.eval_engine.policy_gate import PolicyGateBindingViolationError

    # Run guardrail validator; catch HTTPException and convert to MCP error dict
    try:
        validate_guardrail_request(
            eval_type=eval_type,
            config_json=cfg,
        )
    except StarletteHTTPException as exc:
        return {"error": "validation_failed", "detail": str(exc.detail)}

    from farna.db.models.pipeline import Pipeline

    async with _session(org_id) as s:
        pipeline = (
            await s.execute(
                select(Pipeline).where(
                    Pipeline.id == pid,
                    Pipeline.organisation_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if pipeline is None:
            return {"error": "pipeline_not_found", "detail": MSG_PIPELINE_NOT_FOUND}

        try:
            eval_row = await create_or_update_eval(
                s,
                org_id=org_id,
                account_id=account_id,
                pipeline_id=pid,
                node_id=nid,
                name=name,
                eval_type=eval_type,
                config_json=cfg,
                failure_behaviour=failure_behaviour,
                pass_threshold=pass_threshold,
                suite_id=suite_id,
            )
        except PolicyGateBindingViolationError as exc:
            return {"error": "validation_failed", "detail": f"PolicyGate binding violation: {exc}"}

        return _eval_def_to_dict(eval_row)


@mcp.tool(
    description="Create a new eval definition (operator or admin). Persists an Eval row scoped to the caller's org and returns its details. Requires an operator or admin caller; lower roles receive an insufficient_scope error.",
)
@mcp_audited("eval_definition_created", "eval_definition", fail_closed=False)
@_RETRY_DB
@_tool_db_shell(
    log_constant=_MSG_EVAL_DEF_CREATE_FAILED,
    integrity_detail="Eval definition references a resource that does not exist: {orig}",
    fallback="Failed to create eval definition",
    handle_http_exception=True,
)
async def create_eval_definition(
    pipeline_id: str,
    node_id: str | None = None,
    name: str = "",
    eval_type: str = "",
    config_json: dict[str, Any] | None = None,
    pass_threshold: float | None = None,
    suite_id: str | None = None,
    failure_behaviour: str = "warn",
    reason: str | None = None,
) -> dict[str, Any]:
    return await _create_eval_definition_impl(
        pipeline_id,
        node_id,
        name,
        eval_type,
        config_json,
        pass_threshold,
        suite_id,
        failure_behaviour,
        reason,
    )


def _assert_update_eval_definition_params(
    eval_type: str | None,
    pass_threshold: float | None,
    name: str | None,
) -> dict[str, Any] | None:
    """Validate the scalar update-inputs of an eval definition; error dict or None."""
    if eval_type is not None and (err := _assert_eval_type(eval_type)) is not None:
        return err
    if (err := _assert_pass_threshold(pass_threshold)) is not None:
        return err
    if name is not None and (not name or not name.strip()):
        return {"error": "invalid_name", "detail": "name must be a non-empty string when provided"}
    if name is not None and len(name) > _EVAL_NAME_MAX_LENGTH:
        return {
            "error": "invalid_name",
            "detail": f"name must be at most {_EVAL_NAME_MAX_LENGTH} characters",
        }
    return None


def _collect_eval_definition_updates(
    node_id: str | None,
    nid: uuid.UUID | None,
    name: str | None,
    eval_type: str | None,
    config_json: dict[str, Any] | None,
    pass_threshold: float | None,
    suite_id: str | None,
) -> dict[str, Any]:
    """Build the partial-update field map from the provided (non-None) inputs."""
    updates: dict[str, Any] = {}
    if node_id is not None:
        updates["node_id"] = nid
    if name is not None:
        updates["name"] = name
    if eval_type is not None:
        updates["eval_type"] = eval_type
    if config_json is not None:
        updates["config_json"] = config_json
    if pass_threshold is not None:
        updates["pass_threshold"] = pass_threshold
    if suite_id is not None:
        updates["suite_id"] = suite_id
    return updates


async def _update_eval_definition_impl(
    eval_id: str,
    node_id: str | None,
    name: str | None,
    eval_type: str | None,
    config_json: dict[str, Any] | None,
    pass_threshold: float | None,
    suite_id: str | None,
    failure_behaviour: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply a partial update to an eval definition (an ``Eval`` row); shared with the MCP tool wrapper.

    Redirected to write to Eval+PolicyGate via the shared helper (FAR-1101 chunk 3b).
    """
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("update_eval_definition")

    from farna.api.routes.evals import (
        _MSG_EVAL_DEFINITION_NOT_FOUND,
        _eval_def_to_dict,
    )

    val_err: dict[str, Any] | None = _assert_update_eval_definition_params(eval_type, pass_threshold, name)
    if val_err is not None:
        return val_err

    _assert_eval_definition_write_scope("update", "operator")

    org_id = _ctx_org_id_val()

    eid, nid, params_err = _parse_eval_ref_ids(eval_id, "eval_id", node_id)
    if params_err is not None:
        return params_err
    assert eid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    updates = _collect_eval_definition_updates(
        node_id=node_id,
        nid=nid,
        name=name,
        eval_type=eval_type,
        config_json=config_json,
        pass_threshold=pass_threshold,
        suite_id=suite_id,
    )

    from starlette.exceptions import HTTPException as StarletteHTTPException

    from farna.core.eval_engine.eval_definition_write import (
        EvalWriteOptions,
        create_or_update_eval,
        validate_guardrail_request,
    )
    from farna.core.eval_engine.policy_gate import PolicyGateBindingViolationError

    async with _session(org_id) as s:
        if failure_behaviour is not None:
            from farna.db.crud._gate_advisory_lock import acquire_gate_advisory_lock

            await acquire_gate_advisory_lock(eid, s)
        eval_row = await _load_eval_def(s, org_id, eid)
        if eval_row is None:
            return {"error": "eval_definition_not_found", "detail": _MSG_EVAL_DEFINITION_NOT_FOUND}

        # Resolve current failure_behaviour from the PolicyGate (if any),
        # falling back to "warn" for guardrail-typed / suite-scoped evals.
        from farna.db.models.policy_gate import PolicyGate as PolicyGateModel

        gate_result = await s.execute(
            select(PolicyGateModel).where(
                PolicyGateModel.eval_id == eval_row.id,
                PolicyGateModel.organisation_id == org_id,
                PolicyGateModel.deleted_at.is_(None),
            )
        )
        current_gate = gate_result.scalar_one_or_none()
        effective_node_id = updates.get("node_id", eval_row.node_id)
        if current_gate is not None:
            _assert_gate_write_scope("gate.edit")
        elif effective_node_id is not None and failure_behaviour is not None:
            _assert_gate_write_scope("gate.create")
        current_failure_behaviour = current_gate.action if current_gate is not None else "warn"
        requested_failure_behaviour = failure_behaviour if failure_behaviour is not None else current_failure_behaviour
        transition_reason = _assert_gate_transition_scope(current_failure_behaviour if current_gate is not None else None, requested_failure_behaviour, reason) if failure_behaviour is not None and effective_node_id is not None else None
        if current_gate is not None and transition_reason is not None:
            current_gate.pre_version_raw = {
                **(current_gate.pre_version_raw or {}),
                "previous_action": current_failure_behaviour,
                "new_action": requested_failure_behaviour,
                "reason": transition_reason,
            }
        # FAR-939 chunk 12 §3.1: preserve the live gate's evaluation mode
        # across an MCP eval edit — mirrors the failure_behaviour
        # preservation above (an eval edit must not silently reset a
        # human-mode gate to automatic). The REST update route does the
        # same; the shared helper preserves on None, and passes the live
        # value through here.
        current_evaluation_mode = current_gate.evaluation_mode if current_gate is not None else "automatic"

        # Run guardrail validator; catch HTTPException and convert to MCP error dict
        try:
            validate_guardrail_request(
                eval_type=updates.get("eval_type", eval_row.eval_type),
                config_json=updates.get("config_json", eval_row.config_json),
            )
        except StarletteHTTPException as exc:
            return {"error": "validation_failed", "detail": str(exc.detail)}

        # Redirect to Eval+PolicyGate via the shared helper — version
        # stamping and PolicyGate management are handled internally.
        try:
            eval_row = await create_or_update_eval(
                s,
                org_id=org_id,
                account_id=eval_row.account_id,
                pipeline_id=eval_row.pipeline_id,
                node_id=updates.get("node_id", eval_row.node_id),
                name=updates.get("name") or eval_row.name or "",
                eval_type=updates.get("eval_type", eval_row.eval_type),
                config_json=updates.get("config_json", eval_row.config_json),
                failure_behaviour=current_failure_behaviour if failure_behaviour is None else requested_failure_behaviour,
                pass_threshold=updates.get("pass_threshold", eval_row.pass_threshold),
                suite_id=updates.get("suite_id", eval_row.suite_id),
                write_options=EvalWriteOptions(
                    eval_suite_id=updates.get("eval_suite_id", getattr(eval_row, "eval_suite_id", None)),
                    existing_eval_id=eid,
                    evaluation_mode=current_evaluation_mode,
                ),
            )
        except PolicyGateBindingViolationError as exc:
            return {"error": "validation_failed", "detail": f"PolicyGate binding violation: {exc}"}

        return _eval_def_to_dict(eval_row)


@mcp.tool(
    description="Update an eval definition (operator or admin). Bumps the "
    "definition version and snapshots the pre-edit config, mirroring the REST "
    "semantics. Requires an operator or admin caller; lower roles receive an "
    "insufficient_scope error. "
    "NOTE: because None means 'not provided' in the tool signature, nullable "
    "fields (node_id, pass_threshold, suite_id) cannot be cleared to NULL via "
    "this tool - the REST PUT route must be used to unset them.",
)
@mcp_audited("eval_definition_updated", "eval_definition", fail_closed=False)
@_RETRY_DB
@_tool_db_shell(
    log_constant=_MSG_EVAL_DEF_UPDATE_FAILED,
    integrity_detail="Update would violate a constraint. Check referenced pipeline/suite: {orig}",
    fallback="Failed to update eval definition",
    handle_http_exception=True,
)
async def update_eval_definition(
    eval_id: str,
    node_id: str | None = None,
    name: str | None = None,
    eval_type: str | None = None,
    config_json: dict[str, Any] | None = None,
    pass_threshold: float | None = None,
    suite_id: str | None = None,
    failure_behaviour: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return await _update_eval_definition_impl(
        eval_id,
        node_id,
        name,
        eval_type,
        config_json,
        pass_threshold,
        suite_id,
        failure_behaviour,
        reason,
    )


async def _audit_eval_def_delete(
    s: AsyncSession,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    eid: uuid.UUID,
    hard: bool,
    soft: bool,
    eval_name: str,
) -> None:
    """Best-effort audit event for a guardrail eval-definition delete."""
    from farna.core.audit_logger import append_audit_event

    try:
        await append_audit_event(
            s,
            org_id=org_id,
            event_type="eval_definition.soft_deleted" if soft else "eval_definition.purged",
            actor_user_id=account_id,
            resource_type="eval_definition",
            resource_id=eid,
            payload_json={"eval_id": str(eid), "name": eval_name, "purge": hard},
        )
    except Exception:
        _log.exception(
            "delete_eval_definition_audit_failed",
            extra={"org_id": str(org_id), "eval_id": str(eid)},
        )


async def _delete_eval_definition_impl(eval_id: str, hard: bool) -> dict[str, Any]:
    """Soft-delete or purge an Eval; shared with the MCP tool wrapper.

    Reads from the ``evals`` table (chunk 3b cutover). Soft-delete stamps
    ``deleted_at``/``deleted_by`` on the ``Eval`` and its live ``PolicyGate``
    (if any) in the same transaction. Hard-delete removes the ``Eval`` row
    (``PolicyGate`` cascades via ``ON DELETE CASCADE``); ``PolicyGateDecision``
    rows block hard-delete via RESTRICT — both keys checked (``eval_id``
    direct and ``policy_gate_id`` cascade).
    """
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("delete_eval_definition")

    from farna.api.routes.evals import _MSG_EVAL_DEFINITION_NOT_FOUND

    _assert_eval_definition_write_scope("delete", "admin")

    org_id = _ctx_org_id_val()
    account_id = _ctx_user_id_val()

    eid, eid_err = _parse_uuid_param(eval_id, "eval_id")
    if eid_err is not None:
        return eid_err
    assert eid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    async with _session(org_id) as s:
        eval_row = await _load_eval_def(s, org_id, eid)
        if eval_row is None:
            return {"error": "eval_definition_not_found", "detail": _MSG_EVAL_DEFINITION_NOT_FOUND}

        is_guardrail = eval_row.eval_type == "guardrail"
        soft = is_guardrail and not hard
        eval_name = eval_row.name
        if soft:
            now = datetime.now(UTC)
            eval_row.deleted_at = now
            eval_row.deleted_by = account_id
            # Soft-delete the live PolicyGate (if any) in the same
            # transaction — no orphaned gate enforcing silently.
            from farna.db.models.policy_gate import PolicyGate

            gate_result = await s.execute(
                select(PolicyGate).where(
                    PolicyGate.eval_id == eval_row.id,
                    PolicyGate.organisation_id == org_id,
                    PolicyGate.deleted_at.is_(None),
                )
            )
            gate = gate_result.scalar_one_or_none()
            if gate is not None:
                gate.deleted_at = now
                gate.deleted_by = account_id
        else:
            # Hard-delete: PolicyGate cascades via ON DELETE CASCADE;
            # PolicyGateDecision rows block via RESTRICT (both keys:
            # eval_id direct + policy_gate_id cascade).
            try:
                await s.delete(eval_row)
                await s.flush()
            except IntegrityError:
                return {
                    "error": "delete_blocked_by_decisions",
                    "detail": ("Cannot hard-delete eval: existing decision rows prevent removal (RESTRICT on eval_id and/or policy_gate_id)"),
                }
        if is_guardrail:
            await _audit_eval_def_delete(s, org_id, account_id, eid, hard, soft, eval_name or "")
    return {"id": str(eid), "soft_deleted": soft, "hard_deleted": not soft}


@mcp.tool(
    description="Delete an eval definition (admin only). Guardrail eval definitions are SOFT-deleted (deleted_at stamped) by default; a second, admin-only hard purge (hard=True) removes the row outright. Non-admins receive an insufficient_scope error.",
)
@mcp_audited("eval_definition_deleted", "eval_definition", fail_closed=True)
@_RETRY_DB
@_tool_db_shell(
    log_constant=_MSG_EVAL_DEF_DELETE_FAILED,
    integrity_detail="Delete would violate a constraint: {orig}",
    fallback="Failed to delete eval definition",
)
async def delete_eval_definition(
    eval_id: str,
    hard: bool = False,
) -> dict[str, Any]:
    return await _delete_eval_definition_impl(eval_id, hard)


@mcp.tool(
    name="list_eval_suites",
    description=("List eval suites in the organisation with page-based pagination (mirrors GET /api/v1/evals/eval-suites)."),
)
@_RETRY_DB
async def list_eval_suites(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_eval_suites")

        from sqlalchemy import func as sa_func

        from farna.db.models.eval_suite import EvalSuite

        org_id = _ctx_org_id_val()
        pg = max(1, page)
        ps = max(1, min(page_size, 100))

        async with _session(org_id) as s:
            total = (await s.execute(select(sa_func.count(EvalSuite.id)).where(EvalSuite.organisation_id == org_id))).scalar() or 0
            q = select(EvalSuite).where(EvalSuite.organisation_id == org_id).order_by(EvalSuite.created_at.desc()).offset((pg - 1) * ps).limit(ps)
            rows = list((await s.execute(q)).scalars().all())

        return {
            "data": [_eval_suite_item(suite) for suite in rows],
            "total": total,
            "page": pg,
            "page_size": ps,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_eval_suites failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_eval_suites failed")
        return _tool_exception_error("Failed to list eval suites", exc, "mcp.list_eval_suites")


@mcp.tool(
    name="get_eval_suite",
    description=("Get a single eval suite by ID (mirrors GET /api/v1/evals/eval-suites/{suite_id})."),
)
@_RETRY_DB
async def get_eval_suite(suite_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_eval_suite")

        from farna.db.models.eval_suite import EvalSuite

        org_id = _ctx_org_id_val()
        sid, sid_err = _parse_uuid_param(suite_id, "suite_id")
        if sid_err:
            return sid_err
        if sid is None:
            return {"error": "invalid_id", "field": "suite_id", "detail": _MSG_UUID_PARSE_FAILED}

        async with _session(org_id) as s:
            result = await s.execute(
                select(EvalSuite).where(
                    EvalSuite.id == sid,
                    EvalSuite.organisation_id == org_id,
                )
            )
            suite = result.scalar_one_or_none()

        if suite is None:
            return {"error": "not_found", "detail": f"Eval suite {suite_id} not found"}
        # Team-scoped-key boundary (same pattern as get_pipeline_graph).
        if _team_scoped_key_mismatch(suite.owner_team_id):
            return _team_scope_error("eval_suite", suite_id)

        return _eval_suite_item(suite)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_eval_suite failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_eval_suite failed")
        return _tool_exception_error("Failed to get eval suite", exc, "mcp.get_eval_suite")


def _eval_suite_item(suite: Any) -> dict[str, Any]:
    """Serialize one ``EvalSuite`` row (mirrors the REST EvalSuiteResponse)."""
    return {
        "id": str(suite.id),
        "name": suite.name,
        "description": suite.description,
        "version": suite.version,
        "owner_team_id": str(suite.owner_team_id) if suite.owner_team_id else None,
        "visibility": suite.visibility,
        "eval_definition_ids": [str(eid) for eid in (suite.eval_definition_ids or [])],
        "created_at": suite.created_at.isoformat() if suite.created_at else None,
        "updated_at": suite.updated_at.isoformat() if suite.updated_at else None,
    }
