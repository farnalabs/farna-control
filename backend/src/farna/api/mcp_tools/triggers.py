"""Trigger CRUD, trigger events and org-wide pause MCP tools.

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
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_TOKEN_REVOKED,
    _MSG_TRIGGER_NOT_FOUND,
    _MSG_UUID_PARSE_FAILED,
    _RETRY_DB,
    McpAuthContextError,
    _ctx_team_id_val,
    _parse_optional_uuid,
    _team_scope_error,
    _tool_auth_error,
    _tool_db_shell,
    _tool_exception_error,
    _validate_trigger_numbers,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    _load_trigger_row,
    _log,
    _parse_uuid_param,
    _pipeline_owner_team_id,
    _pipeline_team_gate,
    _require_unique_trigger_name,
    _session,
    _streak_status_for,
    _team_scoped_key_mismatch,
    anchor_trigger_streak_epoch,
    clear_trigger_streak_after_reenable,
    compute_next_fire,
    get_settings,
    validate_cron_expression,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import mask_config_json, merge_masked_config_json
from farna.api.routes.triggers import _encrypt_trigger_config_secrets, _validate_connector_instance_team_scope, _validate_trigger_config_keys
from farna.core.mcp.scope_validator import MCPAuthorizationError


async def _apply_trigger_event_trigger_filter(
    s: AsyncSession,
    q: Any,
    key_team_id: uuid.UUID | None,
    trigger_id: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    from sqlalchemy import select

    from farna.db.models.trigger import Trigger
    from farna.db.models.trigger_event import TriggerEvent

    if trigger_id is None:
        return q, None
    try:
        tid = uuid.UUID(trigger_id)
    except ValueError:
        return q, {
            "error": "invalid_id",
            "field": "trigger_id",
            "detail": f"Invalid UUID format: {trigger_id}",
        }
    q = q.where(TriggerEvent.trigger_id == tid)
    if key_team_id is not None:
        # A team-scoped key must not read events for another
        # team's trigger even when no pipeline filter is given.
        # Fail closed: a soft-deleted or otherwise-unresolvable
        # trigger is treated as out of the key's team boundary too
        # (matching ``list_triggers``, which filters
        # ``Trigger.deleted_at.is_(None)``), so a deleted cross-team
        # trigger cannot fall through to an unfiltered listing.
        trigger = (
            await s.execute(
                select(Trigger).where(
                    Trigger.id == tid,
                    Trigger.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if trigger is None:
            return q, _team_scope_error("trigger", str(tid))
        if _team_scoped_key_mismatch(await _pipeline_owner_team_id(s, trigger.pipeline_id)):
            return q, _team_scope_error("pipeline", str(trigger.pipeline_id))
    return q, None


async def _build_trigger_event_query(
    s: AsyncSession,
    org_id: uuid.UUID,
    key_team_id: uuid.UUID | None,
    trigger_id: str | None,
    pipeline_id: str | None,
    account_id: uuid.UUID | None = None,
    viewer: bool = False,
) -> tuple[Any, dict[str, Any] | None]:
    from sqlalchemy import select

    from farna.db.models.pipeline import Pipeline as _Pipeline
    from farna.db.models.trigger import Trigger
    from farna.db.models.trigger_event import TriggerEvent

    q = select(TriggerEvent).where(TriggerEvent.organisation_id == org_id)
    joined = False

    def _ensure_pipeline_join() -> None:
        # TriggerEvent -> Trigger -> Pipeline is only needed by the
        # owner_team_id scopes below; join at most once across them.
        nonlocal q, joined
        if joined:
            return
        q = q.join(Trigger, TriggerEvent.trigger_id == Trigger.id)
        q = q.join(_Pipeline, Trigger.pipeline_id == _Pipeline.id)
        joined = True

    q, trigger_filter_err = await _apply_trigger_event_trigger_filter(s, q, key_team_id, trigger_id)
    if trigger_filter_err:
        return None, trigger_filter_err

    if pipeline_id is not None:
        try:
            pid = uuid.UUID(pipeline_id)
        except ValueError:
            return None, {
                "error": "invalid_id",
                "field": "pipeline_id",
                "detail": f"Invalid UUID format: {pipeline_id}",
            }
        if key_team_id is not None:
            owner_team_id = await _pipeline_owner_team_id(s, pid)
            if _team_scoped_key_mismatch(owner_team_id):
                return None, _team_scope_error("pipeline", str(pid))
        _ensure_pipeline_join()
        q = q.where(
            Trigger.pipeline_id == pid,
            _Pipeline.deleted_at.is_(None),
        )

    if viewer:
        # Fail closed: a viewer read MUST carry the account-membership
        # boundary. An unset account is an auth-context failure — it must never
        # degrade into an org-wide read.
        if account_id is None:
            raise McpAuthContextError("No authenticated user context for this MCP request")
        from farna.db.crud.team_scope import account_team_scope_clause

        _ensure_pipeline_join()
        q = q.where(account_team_scope_clause(_Pipeline.owner_team_id, account_id))

    if key_team_id is not None:
        # A team-scoped key only sees events whose trigger's pipeline
        # is org-level or owned by its own team — the same boundary
        # ``list_triggers`` applies.
        from farna.db.crud.team_scope import team_scope_clause

        _ensure_pipeline_join()
        q = q.where(team_scope_clause(_Pipeline.owner_team_id, key_team_id))

    return q, None


async def _paginate_trigger_events(
    s: AsyncSession,
    q: Any,
    cursor: str | None,
    lim: int,
) -> tuple[list[Any], str | None, bool]:
    from farna.db.crud.pagination import CursorPaginator
    from farna.db.models.trigger_event import TriggerEvent

    if cursor is not None:
        paginator = CursorPaginator(sort_field="created_at", sort_dir="desc")
        cp = await paginator.paginate(
            s,
            q,
            cursor=cursor,
            limit=lim,
            model=TriggerEvent,
            compute_total=False,
        )
        items = cp.items
        next_cursor = cp.next_cursor
        has_more = cp.has_more
    else:
        q = q.order_by(TriggerEvent.created_at.desc(), TriggerEvent.id.desc())
        rows: list[Any] = list((await s.execute(q.limit(lim + 1))).scalars().all())
        has_more = len(rows) > lim
        items = rows[:lim]
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = CursorPaginator.encode_cursor(last.created_at, last.id)
    return items, next_cursor, has_more


def _trigger_event_payloads(items: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(e.id),
            "trigger_id": str(e.trigger_id),
            "trigger_type": e.trigger_type,
            "validation_result": e.validation_result,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "run_id": str(e.run_id) if e.run_id else None,
        }
        for e in items
    ]


async def _list_trigger_events_impl(
    trigger_id: str | None,
    pipeline_id: str | None,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("list_trigger_events")
    from sqlalchemy import func, select

    org_id = _ctx_org_id_val()
    lim = max(1, min(limit, 100))

    async with _session(org_id) as s:
        key_team_id = _ctx_team_id_val()
        viewer = _ctx_role_val() == "viewer"
        # Fail closed: the viewer leg of the query needs a real account id.
        # ``_ctx_user_id_val`` raises rather than returning None, so an unset
        # context cannot silently skip the membership clause.
        viewer_account_id = _ctx_user_id_val() if viewer else None
        q, q_err = await _build_trigger_event_query(
            s,
            org_id,
            key_team_id,
            trigger_id,
            pipeline_id,
            account_id=viewer_account_id,
            viewer=viewer,
        )
        if q_err:
            return q_err
        total = int((await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one_or_none() or 0)
        items, next_cursor, has_more = await _paginate_trigger_events(s, q, cursor, lim)

    return {
        "data": _trigger_event_payloads(items),
        "total": total,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


@mcp.tool(
    name="list_trigger_events",
    description=("List recent trigger events with cursor-based pagination. Filter by trigger_id and/or pipeline_id. Returns events ordered by most recent first."),
)
@_RETRY_DB
async def list_trigger_events(
    trigger_id: str | None = None,
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        return await _list_trigger_events_impl(trigger_id, pipeline_id, cursor, limit)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_trigger_events failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_trigger_events failed")
        return _tool_exception_error("Failed to list trigger events", exc, "mcp.list_trigger_events")


@mcp.tool(
    description="List triggers configured for the organisation with cursor-based pagination. Optionally filter by pipeline_id. Returns trigger metadata including name, type, active status, and cron schedule.",
)
@_RETRY_DB
async def list_triggers(
    pipeline_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_triggers")
        from farna.db.crud.trigger import list_triggers as db_list_triggers

        org_id = _ctx_org_id_val()
        # FAR-1540: malformed filter id -> ``invalid_id``, not ``internal_error``.
        pid, pid_err = _parse_optional_uuid(pipeline_id, "pipeline_id")
        if pid_err is not None:
            return pid_err
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            if pid is not None:
                owner_team_id = await _pipeline_owner_team_id(s, pid)
                if _team_scoped_key_mismatch(owner_team_id):
                    return _team_scope_error("pipeline", str(pid))
            trigger_list_kwargs: dict[str, Any] = {
                "pipeline_id": pid,
                "cursor": cursor,
                "limit": lim,
                "team_id": _ctx_team_id_val(),
            }
            if _ctx_role_val() == "viewer":
                trigger_list_kwargs["account_id"] = _ctx_user_id_val()
            result = await db_list_triggers(s, org_id, **trigger_list_kwargs)
            # FAR-251 — surface the SAME streak_status shape as the REST
            # trigger serializers (via the shared routes helper), computed
            # INSIDE the RLS transaction so a deactivated trigger's reason /
            # streak reads land in-org (mirrors the FAR-191 list fix).
            data = [
                {
                    "id": str(t.id),
                    "pipeline_id": str(t.pipeline_id),
                    "name": t.name,
                    "trigger_type": t.trigger_type,
                    "active": t.active,
                    "max_concurrent_runs": t.max_concurrent_runs,
                    "cron_expression": t.cron_expression,
                    "last_fired_at": t.last_fired_at.isoformat() if t.last_fired_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                    "streak_status": await _streak_status_for(s, t),
                }
                for t in result.items
            ]

        return {
            "data": data,
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_triggers failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_triggers failed")
        return _tool_exception_error("Failed to list triggers", exc, "mcp.list_triggers")


def _validate_trigger_create_inputs(
    pipeline_id: str,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    try:
        pid = uuid.UUID(pipeline_id)
    except ValueError:
        return None, {
            "error": "invalid_id",
            "field": "pipeline_id",
            "detail": f"Invalid UUID format: {pipeline_id}",
        }
    num_err = _validate_trigger_numbers(max_concurrent_runs, daily_spend_limit)
    if num_err:
        return None, num_err
    return pid, None


async def _validate_ongoing_trigger_create(
    s: AsyncSession,
    pid: uuid.UUID,
    trigger_type: str,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
) -> tuple[datetime | None, dict[str, Any] | None]:
    if trigger_type != "ongoing":
        return None, None
    # FAR-158: identical guards to the REST create surface.
    from fastapi import HTTPException

    from farna.core.trigger_validation import validate_ongoing_config
    from farna.db.models.pipeline import Pipeline

    pipeline = await s.get(Pipeline, pid)
    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
    try:
        validate_ongoing_config(
            trigger_type,
            max_concurrent_runs=max_concurrent_runs,
            daily_spend_limit=Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else None,
            config_json=config_json,
            pipeline_max_concurrent_runs=pipeline_cap,
        )
    except HTTPException as exc:
        return None, {"error": "validation", "detail": str(exc.detail)}
    return datetime.now(UTC), None


def _normalise_trigger_name(name: Any) -> tuple[str, dict[str, Any] | None]:
    """FAR-2227: strip and validate a trigger name (1..255 chars); returns (name, error)."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned or len(cleaned) > 255:
        return cleaned, {"error": "validation", "field": "name", "detail": "name is required (1-255 characters)"}
    return cleaned, None


def _build_trigger_record(
    org_id: uuid.UUID,
    pid: uuid.UUID,
    name: str,
    trigger_type: str,
    active: bool,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    account_id: uuid.UUID,
    next_fire_at: datetime | None,
    cron_expression: str | None,
) -> tuple[Any, dict[str, Any] | None]:
    from farna.db.models.trigger import Trigger

    # FAR-1732: encrypt webhook/HMAC secrets BEFORE persisting — the same
    # shared helper REST's create route uses, so MCP-created triggers store
    # ``hmac_secret``/``signing_secret`` under Fernet tokens identical to the
    # REST path (read paths decrypt with plaintext fallback, so nothing that
    # consumed plaintext breaks).
    _settings = get_settings()
    encrypted_config = _encrypt_trigger_config_secrets(config_json, _settings.fernet_key, _settings.fernet_key_old)
    trigger = Trigger(
        organisation_id=org_id,
        pipeline_id=pid,
        name=name,
        trigger_type=trigger_type,
        active=active,
        max_concurrent_runs=max_concurrent_runs,
        daily_spend_limit=Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else None,
        config_json=encrypted_config or {},
        account_id=account_id,
        next_fire_at=next_fire_at,
        # FAR-190: creation anchors the no-delivery streak epoch (the
        # streak boundary) so pre-existing history can never count.
        streak_epoch=datetime.now(UTC),
    )
    if cron_expression:
        trigger.cron_expression = cron_expression
        error = validate_cron_expression(cron_expression)
        if error:
            return trigger, {"error": "invalid_cron", "detail": error}
        trigger.next_fire_at = compute_next_fire(cron_expression, timezone=trigger.cron_timezone or "UTC")
    return trigger, None


async def _create_trigger_impl(
    pipeline_id: str,
    name: str,
    trigger_type: str,
    active: bool,
    cron_expression: str | None,
    config_json: dict[str, Any] | None,
    max_concurrent_runs: int,
    daily_spend_limit: float | None,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("create_trigger")

    org_id = _ctx_org_id_val()
    account_id = _ctx_user_id_val()
    pid, input_err = _validate_trigger_create_inputs(pipeline_id, max_concurrent_runs, daily_spend_limit)
    if input_err:
        return input_err
    clean_name, name_err = _normalise_trigger_name(name)
    if name_err:
        return name_err
    try:
        _validate_trigger_config_keys(config_json)
    except FastAPIHTTPException as exc:
        return {"error": "validation", "detail": exc.detail}
    if pid is None:
        raise RuntimeError("_create_trigger_impl: validate returned no error and no pipeline id")

    async with _session(org_id) as s:
        # FAR-1513 CRITICAL fix: gate on the team-blind read (see
        # _pipeline_team_gate) so an RLS-hidden team-private pipeline is
        # DENIED, never allowed (the caller-facing owner read returned None,
        # which passed every old boundary/membership check).
        owner_team_id, gate_denial = await _pipeline_team_gate(s, pid)
        if gate_denial:
            return gate_denial
        try:
            await _require_unique_trigger_name(s, pid, clean_name)
        except FastAPIHTTPException as exc:
            return {"error": "conflict", "field": "name", "detail": exc.detail}
        # FAR-1595: a connector reference named at create time is
        # team-validated against the owning pipeline before the trigger can
        # ever poll it (the fire job reads the row team-blind). This is the
        # SAME shared gate REST's create route calls — without it the MCP
        # create tool was a bypass around it.
        try:
            await _validate_connector_instance_team_scope(
                s,
                org_id=org_id,
                pipeline_owner_team_id=owner_team_id,
                config=config_json,
            )
        except FastAPIHTTPException as exc:
            from farna.core.team_visibility import CONNECTOR_TEAM_MISMATCH

            return {"error": CONNECTOR_TEAM_MISMATCH, "detail": str(exc.detail)}
        next_fire_at, ongoing_err = await _validate_ongoing_trigger_create(s, pid, trigger_type, max_concurrent_runs, daily_spend_limit, config_json)
        if ongoing_err:
            return ongoing_err
        trigger, build_err = _build_trigger_record(
            org_id,
            pid,
            clean_name,
            trigger_type,
            active,
            max_concurrent_runs,
            daily_spend_limit,
            config_json,
            account_id,
            next_fire_at,
            cron_expression,
        )
        if build_err:
            return build_err
        s.add(trigger)
        await s.flush()
        # FAR-251 — surface the created trigger's streak_status exactly as
        # the REST create serializer does (computed inside the RLS
        # transaction; for a fresh ongoing trigger this reads the anchored
        # streak=0 / state=ok baseline).
        created_streak_status = await _streak_status_for(s, trigger)

    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "name": trigger.name,
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": float(trigger.daily_spend_limit) if trigger.daily_spend_limit is not None else None,
        "cron_expression": trigger.cron_expression,
        "streak_status": created_streak_status,
    }


@mcp.tool(description="Create a new trigger for a pipeline. `name` is required (1-255 chars) and must be unique per pipeline.")
@mcp_audited("trigger_created", "trigger", fail_closed=False)
@_RETRY_DB
async def create_trigger(
    pipeline_id: str,
    name: str,
    trigger_type: str = "manual",
    active: bool = True,
    cron_expression: str | None = None,
    config_json: dict[str, Any] | None = None,
    max_concurrent_runs: int = 1,
    daily_spend_limit: float | None = None,
) -> dict[str, Any]:
    try:
        return await _create_trigger_impl(
            pipeline_id,
            name,
            trigger_type,
            active,
            cron_expression,
            config_json,
            max_concurrent_runs,
            daily_spend_limit,
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("create_trigger failed")
        return _tool_exception_error("Failed to create trigger", exc, "mcp.create_trigger")


def _trigger_detail_dict(trigger: Any, in_flight: int, streak_status: Any) -> dict[str, Any]:
    """Serialize a trigger row to the MCP response shape.

    FAR-1732: ``config_json`` is MASKED through the same shipped
    :func:`mask_config_json` the REST serializer uses — an operator-scoped
    MCP token must never receive a stored webhook secret (plaintext on
    legacy rows, a recoverable Fernet ciphertext on new ones). The
    ``input_template`` echo derives from the MASKED config so it cannot
    bypass the mask either.
    """
    config = mask_config_json(trigger.config_json or {})
    return {
        "id": str(trigger.id),
        "pipeline_id": str(trigger.pipeline_id),
        "name": trigger.name,
        "trigger_type": trigger.trigger_type,
        "active": trigger.active,
        "max_concurrent_runs": trigger.max_concurrent_runs,
        "daily_spend_limit": float(trigger.daily_spend_limit) if trigger.daily_spend_limit is not None else None,
        "config_json": config,
        "cron_expression": trigger.cron_expression,
        "cron_timezone": trigger.cron_timezone,
        "last_fired_at": trigger.last_fired_at.isoformat() if trigger.last_fired_at else None,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
        "input_template": config.get("input_template"),
        "in_flight": in_flight,
        "streak_status": streak_status,
    }


async def _get_trigger_impl(trigger_id: str) -> dict[str, Any]:
    """Load a single trigger with team-scope + streak detail; shared with the MCP tool wrapper."""
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("get_trigger")

    org_id = _ctx_org_id_val()
    tid, tid_err = _parse_uuid_param(trigger_id, "trigger_id")
    if tid_err:
        return tid_err
    if tid is None:
        return {"error": "invalid_id", "field": "trigger_id", "detail": _MSG_UUID_PARSE_FAILED}

    from farna.core.cron_helpers import _count_ongoing_runs

    # ``trigger.list`` is viewer-reachable, so a viewer read must be bounded by
    # the account's team memberships — never org-wide. Fail closed when the
    # account context is missing rather than degrading to an unscoped read.
    viewer_account_id = _ctx_user_id_val() if _ctx_role_val() == "viewer" else None

    async with _session(org_id) as s:
        trigger = await _load_trigger_row(s, org_id, tid, account_id=viewer_account_id)
        if trigger is not None:
            owner_team_id = await _pipeline_owner_team_id(s, trigger.pipeline_id)
            if _team_scoped_key_mismatch(owner_team_id):
                return _team_scope_error("pipeline", str(trigger.pipeline_id))
        in_flight = await _count_ongoing_runs(s, tid) if trigger is not None and trigger.trigger_type == "ongoing" else 0
        # FAR-251 — surface the SAME streak_status shape as the REST
        # trigger detail serializer (shared ``_streak_status_for``), computed
        # INSIDE the RLS transaction (mirrors the FAR-191 fix — never read
        # streak status post-commit).
        streak_status = await _streak_status_for(s, trigger) if trigger is not None else None

    if trigger is None:
        return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

    return _trigger_detail_dict(trigger, in_flight, streak_status)


@mcp.tool(description="Get a single trigger by ID.")
@_RETRY_DB
@_tool_db_shell(
    log_constant="get_trigger failed",
    integrity_detail=None,
    fallback="Failed to get trigger",
    db_errors_to_fallback=True,
)
async def get_trigger(trigger_id: str) -> dict[str, Any]:
    return await _get_trigger_impl(trigger_id)


def _validate_trigger_update_inputs(
    trigger_id: str,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse the trigger UUID and validate the numeric update inputs."""
    tid, tid_err = _parse_uuid_param(trigger_id, "trigger_id")
    if tid_err:
        return None, tid_err
    assert tid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    num_err = _validate_trigger_numbers(max_concurrent_runs, daily_spend_limit)
    if num_err:
        return None, num_err
    return tid, None


async def _load_trigger_for_update(s: AsyncSession, org_id: uuid.UUID, tid: uuid.UUID) -> tuple[Any | None, uuid.UUID | None, dict[str, Any] | None]:
    """Load the trigger row for update plus its pipeline's team-gate owner and denial.

    Returns ``(trigger, owner_team_id, denial)``: ``(None, None, None)`` when
    the trigger row itself is absent (caller renders ``_MSG_TRIGGER_NOT_FOUND``),
    ``(trigger, owner, None)`` when the gate allows, and ``(None, owner,
    denial)``/``(trigger, owner, denial)`` when the gate denies — FAR-1513
    fix: the gate evaluates the pipeline through the team-blind read, so an
    RLS-hidden team-private pipeline denies instead of falling through the key
    mismatch that consumed the old ``_TEAM_SCOPE_ERROR`` sentinel.

    ``owner_team_id`` is the gate's RESOLVED effective owner (None for an
    org-level pipeline), returned so callers can run team-scoped validation
    (FAR-1595's connector gate) against the same value the gate judged — a
    second caller-facing read would return None under a team-scoped key and
    deny an otherwise legitimate write.
    """
    trigger = await _load_trigger_row(s, org_id, tid)
    if trigger is None:
        return None, None, None
    owner_team_id, denial = await _pipeline_team_gate(s, trigger.pipeline_id)
    if denial is not None:
        return None, owner_team_id, denial
    return trigger, owner_team_id, None


async def _validate_ongoing_config_change(
    s: AsyncSession,
    trigger: Any,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    active: bool | None,
) -> dict[str, Any] | None:
    """Validate an ongoing trigger's config change; returns None if valid or an error dict."""
    from fastapi import HTTPException

    from farna.core.trigger_validation import validate_ongoing_config
    from farna.db.models.pipeline import Pipeline

    ongoing_fields_changing = any(x is not None for x in [max_concurrent_runs, config_json, active]) or (daily_spend_limit is not None)
    if not ongoing_fields_changing:
        return None
    pipeline = await s.get(Pipeline, trigger.pipeline_id)
    pipeline_cap = pipeline.max_concurrent_runs if pipeline is not None else 0
    resolved_daily_spend_limit = Decimal(str(daily_spend_limit)) if daily_spend_limit is not None else trigger.daily_spend_limit
    try:
        validate_ongoing_config(
            trigger.trigger_type,
            max_concurrent_runs=(max_concurrent_runs if max_concurrent_runs is not None else trigger.max_concurrent_runs),
            daily_spend_limit=resolved_daily_spend_limit,
            config_json=(config_json if config_json is not None else trigger.config_json),
            pipeline_max_concurrent_runs=pipeline_cap,
        )
    except HTTPException as exc:
        return {"error": "validation", "detail": str(exc.detail)}
    return None


async def _validate_ongoing_trigger_update(
    s: AsyncSession,
    trigger: Any,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    config_json: dict[str, Any] | None,
    active: bool | None,
    clear_daily_spend_limit: bool,
) -> tuple[bool, dict[str, Any] | None]:
    """FAR-158 ongoing guards (identical to REST PUT); returns (scan_interval_changed, error)."""
    ongoing_scan_interval_changed = False
    if trigger.trigger_type == "ongoing":
        if clear_daily_spend_limit:
            return False, {
                "error": "validation",
                "detail": "ongoing triggers require daily_spend_limit; clearing it is not allowed",
            }
        cfg_err = await _validate_ongoing_config_change(s, trigger, max_concurrent_runs, daily_spend_limit, config_json, active)
        if cfg_err:
            return False, cfg_err
        if config_json is not None:
            old_scan = int((trigger.config_json or {}).get("scan_interval_seconds") or 60)
            new_scan = int(config_json.get("scan_interval_seconds") or 60)
            ongoing_scan_interval_changed = new_scan != old_scan
    return ongoing_scan_interval_changed, None


def _validate_cron_update(
    trigger: Any,
    cron_expression: str | None,
    cron_timezone: str | None,
) -> tuple[datetime | None, dict[str, Any] | None]:
    """Validate cron config; returns (next_fire_at, error)."""
    next_fire_at = None
    if cron_expression is not None or cron_timezone is not None:
        expr = cron_expression if cron_expression is not None else trigger.cron_expression
        if expr is None:
            return None, {"error": "invalid_cron", "detail": "Cron expression is required"}
        tz = cron_timezone if cron_timezone is not None else trigger.cron_timezone or "UTC"
        error = validate_cron_expression(expr, tz)
        if error:
            return None, {"error": "invalid_cron", "detail": error}
        next_fire_at = compute_next_fire(expr, timezone=tz)
    return next_fire_at, None


def _merge_trigger_config_json(trigger: Any, config_json: dict[str, Any] | None) -> None:
    if config_json is None:
        return
    # MERGE into the existing blob — never wholesale replace.
    # A masked placeholder must never clobber the stored secret
    # (read-modify-write round-trip guard). Keep the existing value.
    # FAR-1735: use the RECURSIVE masked merge — the same
    # ``merge_masked_config_json`` REST's PATCH uses — so a nested masked
    # secret (e.g. ``input_template.api_key``) round-tripped through MCP is
    # preserved at every depth, not clobbered by the shallow merge.
    merged = merge_masked_config_json(trigger.config_json or {}, config_json)
    # FAR-1732: encrypt webhook/HMAC secrets after the masked merge — the same
    # shared helper REST's PUT route uses. Idempotent: values already stored
    # under Fernet tokens (or mask sentinels) pass through untouched.
    settings = get_settings()
    trigger.config_json = _encrypt_trigger_config_secrets(merged, settings.fernet_key, settings.fernet_key_old)


async def _apply_trigger_field_updates(
    s: AsyncSession,
    trigger: Any,
    active: bool | None,
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
    clear_daily_spend_limit: bool,
    config_json: dict[str, Any] | None,
    cron_expression: str | None,
    cron_timezone: str | None,
    next_fire_at: datetime | None,
    prev_active: bool | None,
) -> None:
    """Apply the field updates to the trigger row in place."""
    if active is not None:
        trigger.active = active
        # FAR-190: re-anchor the no-delivery streak epoch on any
        # active=True transition (no un-epoch'd re-enable path).
        if trigger.active and not prev_active:
            await anchor_trigger_streak_epoch(s, trigger_id=trigger.id)
    if max_concurrent_runs is not None:
        trigger.max_concurrent_runs = max_concurrent_runs
    if clear_daily_spend_limit:
        trigger.daily_spend_limit = None
    elif daily_spend_limit is not None:
        trigger.daily_spend_limit = Decimal(str(daily_spend_limit))
    _merge_trigger_config_json(trigger, config_json)
    if cron_expression is not None:
        trigger.cron_expression = cron_expression
    if cron_timezone is not None:
        trigger.cron_timezone = cron_timezone
    if next_fire_at is not None:
        trigger.next_fire_at = next_fire_at


def _recompute_ongoing_next_fire(
    trigger: Any,
    max_concurrent_runs: int | None,
    active: bool | None,
    prev_max: int | None,
    prev_active: bool | None,
    ongoing_scan_interval_changed: bool,
) -> None:
    # Ongoing triggers recompute next_fire_at when the pool / cadence /
    # active actually changes so the new config takes effect promptly.
    if trigger.trigger_type == "ongoing":
        from datetime import UTC

        target_changed = max_concurrent_runs is not None and max_concurrent_runs != prev_max
        activated = active is not None and trigger.active and not prev_active
        if target_changed or ongoing_scan_interval_changed or activated:
            trigger.next_fire_at = datetime.now(UTC)


async def _rename_trigger_in_session(s: AsyncSession, trigger: Any, name: str | None) -> dict[str, Any] | None:
    """Apply a requested rename inside the update session; returns a conflict envelope or None."""
    if name is not None and name != trigger.name:
        # Validated BEFORE mutation: a clean early return commits.
        try:
            await _require_unique_trigger_name(s, trigger.pipeline_id, name, exclude_id=trigger.id)
        except FastAPIHTTPException as exc:
            return {"error": "conflict", "field": "name", "detail": exc.detail}
        trigger.name = name
    return None


async def _validate_trigger_update_config(
    s: AsyncSession,
    org_id: uuid.UUID,
    trigger: Any,
    owner_team_id: uuid.UUID | None,
    config_json: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate a write-time config merge before mutation; returns an error envelope or None."""
    if config_json is None:
        return None
    # FAR-1735: validate the SAME recursive masked merge the write path uses
    # (REST parity) — a shallow merge would judge a different shape than
    # `_merge_trigger_config_json` persists.
    merged_config = merge_masked_config_json(trigger.config_json or {}, config_json)
    try:
        _validate_trigger_config_keys(merged_config, context="merged config_json")
    except FastAPIHTTPException as exc:
        return {"error": "validation", "detail": exc.detail}
    # FAR-1595: a NEW connector reference written through the general update
    # path is team-validated too — same shared gate REST's PUT calls, so MCP is
    # not a bypass around it. Judged on the MERGED config and against the
    # PREVIOUS value (an unchanged reference is not re-judged), all BEFORE the
    # ORM object is mutated: a clean early return from `_session` COMMITS.
    try:
        await _validate_connector_instance_team_scope(
            s,
            org_id=org_id,
            pipeline_owner_team_id=owner_team_id,
            config=merged_config,
            previous_connector_instance_id=(trigger.config_json or {}).get("connector_instance_id"),
        )
    except FastAPIHTTPException as exc:
        from farna.core.team_visibility import CONNECTOR_TEAM_MISMATCH

        return {"error": CONNECTOR_TEAM_MISMATCH, "detail": str(exc.detail)}
    return None


async def _update_trigger_txn(
    org_id: uuid.UUID,
    tid: uuid.UUID,
    *,
    active: bool | None,
    max_concurrent_runs: int | None,
    cron_expression: str | None,
    cron_timezone: str | None,
    daily_spend_limit: float | None,
    clear_daily_spend_limit: bool,
    config_json: dict[str, Any] | None,
    name: str | None = None,
) -> tuple[Any, int, dict[str, Any], bool] | dict[str, Any]:
    """Run the trigger update inside its own session (validate-then-mutate).

    Returns ``(trigger, in_flight, updated_streak_status, prev_active)`` on
    success, or a standard error envelope (team scope / not found / validation)
    when the write must not proceed. ``prev_active`` is captured before the
    field updates so the caller can clear the re-enable streak (FAR-190) after
    the commit.
    """
    async with _session(org_id) as s:
        # FAR-1513 CRITICAL fix: the team gate now runs inside
        # _load_trigger_for_update against the team-blind-resolved pipeline;
        # a denial envelope is returned verbatim (never re-mapped through the
        # team-scope sentinel, which would misname a membership/not-found
        # denial as a key-boundary message).
        trigger, owner_team_id, gate_denial = await _load_trigger_for_update(s, org_id, tid)
        if gate_denial is not None:
            return gate_denial
        if trigger is None:
            return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

        cron_config_requested = cron_expression is not None or cron_timezone is not None
        if cron_config_requested and trigger.trigger_type != "cron":
            return {"error": "validation", "detail": "Only cron triggers can have cron configuration"}

        ongoing_scan_interval_changed, ongoing_err = await _validate_ongoing_trigger_update(s, trigger, max_concurrent_runs, daily_spend_limit, config_json, active, clear_daily_spend_limit)
        if ongoing_err:
            return ongoing_err

        next_fire_at, cron_err = _validate_cron_update(trigger, cron_expression, cron_timezone)
        if cron_err:
            return cron_err

        prev_max = trigger.max_concurrent_runs
        prev_active = trigger.active
        # Validate the write-time config gate ONLY when config_json was
        # part of the request (mirrors the REST _apply_trigger_update
        # semantics): a legacy trigger with an unread key must stay
        # updatable by any other field via MCP, same as via REST.
        #
        # Validate the MERGED config BEFORE mutating the ORM object.
        # `_session()` wraps the body in `s.begin()`, so a clean early
        # return COMMITS — validating after mutation would persist an
        # invalid merged config while reporting a validation error.
        config_err = await _validate_trigger_update_config(s, org_id, trigger, owner_team_id, config_json)
        if config_err is not None:
            return config_err
        rename_err = await _rename_trigger_in_session(s, trigger, name)
        if rename_err is not None:
            return rename_err
        await _apply_trigger_field_updates(
            s,
            trigger,
            active,
            max_concurrent_runs,
            daily_spend_limit,
            clear_daily_spend_limit,
            config_json,
            cron_expression,
            cron_timezone,
            next_fire_at,
            prev_active,
        )

        _recompute_ongoing_next_fire(trigger, max_concurrent_runs, active, prev_max, prev_active, ongoing_scan_interval_changed)
        await s.flush()
        from farna.core.cron_helpers import _count_ongoing_runs

        in_flight = await _count_ongoing_runs(s, trigger.id) if trigger.trigger_type == "ongoing" else 0
        # FAR-251 — surface the updated trigger's streak_status exactly as
        # the REST update serializer does (computed inside the RLS
        # transaction so a re-enabled trigger reflects its reset streak).
        updated_streak_status = await _streak_status_for(s, trigger)

    return trigger, in_flight, updated_streak_status, prev_active


@mcp.tool(
    description="Update an existing trigger's configuration. Mirrors PUT /api/v1/triggers/{id}. Pass name to rename (unique per pipeline). Setting cron_expression or cron_timezone is only valid for cron triggers.",
)
@mcp_audited("trigger_updated", "trigger", fail_closed=False)
@_RETRY_DB
async def update_trigger(
    trigger_id: str,
    active: bool | None = None,
    max_concurrent_runs: int | None = None,
    cron_expression: str | None = None,
    cron_timezone: str | None = None,
    daily_spend_limit: float | None = None,
    clear_daily_spend_limit: bool = False,
    config_json: dict[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_trigger")
        clean_name: str | None = None
        if name is not None:
            clean_name, name_err = _normalise_trigger_name(name)
            if name_err:
                return name_err

        org_id = _ctx_org_id_val()
        tid, input_err = _validate_trigger_update_inputs(trigger_id, max_concurrent_runs, daily_spend_limit)
        if input_err:
            return input_err
        if tid is None:
            raise RuntimeError("_validate_trigger_update_inputs returned an error dict but no parsed trigger id")

        outcome = await _update_trigger_txn(
            org_id,
            tid,
            active=active,
            max_concurrent_runs=max_concurrent_runs,
            cron_expression=cron_expression,
            cron_timezone=cron_timezone,
            daily_spend_limit=daily_spend_limit,
            clear_daily_spend_limit=clear_daily_spend_limit,
            config_json=config_json,
            name=clean_name,
        )
        if isinstance(outcome, dict):
            return outcome
        trigger, in_flight, updated_streak_status, prev_active = outcome

        # FAR-190: clear the config-failure Redis counter only AFTER the commit
        # (the _session context commits on exit); best-effort.
        if active is True and not prev_active:
            await clear_trigger_streak_after_reenable(trigger.id)

        return _trigger_detail_dict(trigger, in_flight, updated_streak_status)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("update_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("update_trigger failed")
        return _tool_exception_error("Failed to update trigger", exc, "mcp.update_trigger")


@mcp.tool(description="Soft-delete a trigger by ID.")
@mcp_audited("trigger_deleted", "trigger", fail_closed=True)
@_RETRY_DB
async def delete_trigger(trigger_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_trigger")

        org_id = _ctx_org_id_val()
        try:
            tid = uuid.UUID(trigger_id)
        except ValueError:
            return {"error": "invalid_id", "field": "trigger_id", "detail": f"Invalid UUID format: {trigger_id}"}

        from sqlalchemy import select

        from farna.db.crud.trigger import soft_delete_trigger
        from farna.db.models.trigger import Trigger

        async with _session(org_id) as s:
            trigger = (
                await s.execute(
                    select(Trigger).where(
                        Trigger.id == tid,
                        Trigger.organisation_id == org_id,
                        Trigger.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if trigger is None:
                return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}
            # FAR-1513 CRITICAL fix: the team-blind gate re-evaluates the
            # RLS-hidden pipeline row, so a team-private pipeline the caller
            # cannot see is denied instead of silently allowed (the old
            # caller-facing owner read returned None and passed every check).
            _, gate_denial = await _pipeline_team_gate(s, trigger.pipeline_id)
            if gate_denial:
                return gate_denial
            deleted = await soft_delete_trigger(s, tid)

        if deleted is None:
            return {"error": "not_found", "detail": _MSG_TRIGGER_NOT_FOUND}

        return {"id": str(tid), "deleted": True}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_trigger failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("delete_trigger failed")
        return _tool_exception_error("Failed to delete trigger", exc, "mcp.delete_trigger")


@mcp.tool(
    description="Pause or resume all pipeline triggers for the current organisation. "
    "When paused, new trigger-initiated runs (webhook, cron, polling, agent_signal, "
    "replay) are blocked at the run-creation gate; manual runs, MCP trigger_pipeline, "
    "and scheduled reports are not paused. Idempotent: setting the state it is "
    "already in is a no-op that returns the current state without writing an audit event."
)
@_RETRY_DB
async def set_org_triggers_paused(paused: bool) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("set_org_triggers_paused")

        org_id = _ctx_org_id_val()

        from datetime import UTC

        from farna.core.audit_logger import append_audit_event
        from farna.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)
            if org is None:
                return {"error": "not_found", "detail": "Organisation not found"}

            # Idempotency: toggling to the current state is a no-op (no audit write).
            if org.triggers_paused == paused:
                paused_at = org.triggers_paused_at.isoformat() if org.triggers_paused_at else None
                return {"paused": org.triggers_paused, "paused_at": paused_at}

            org.triggers_paused = paused
            org.triggers_paused_at = datetime.now(UTC) if paused else None
            await s.flush()

            # Audit is fail-open-with-alert: the toggle ALWAYS commits; a failed
            # audit write is loudly logged and never rolls back the toggle.
            try:
                await append_audit_event(
                    s,
                    org_id=org_id,
                    event_type="triggers_paused",
                    actor_user_id=_ctx_user_id_val(),
                    payload_json={"paused": paused},
                )
            except SQLAlchemyError:
                _log.exception("set_org_triggers_paused audit write failed")
            except Exception:
                _log.exception("set_org_triggers_paused audit write failed (non-DB)")

            return {
                "paused": org.triggers_paused,
                "paused_at": org.triggers_paused_at.isoformat() if org.triggers_paused_at else None,
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("set_org_triggers_paused failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except StarletteHTTPException:
        return {"error": "not_found", "detail": "Organisation not found"}
    except Exception as exc:
        _log.exception("set_org_triggers_paused failed")
        return _tool_exception_error("Failed to update org trigger pause state", exc, "mcp.set_org_triggers_paused")
