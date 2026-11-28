"""Admin, org-config and governance MCP tools.

Covers System Maps overview/discovery, the audit trail, notification deliveries,
lifecycle maps, plugins, SSO providers, org configuration, the plan feature
list and housekeeping.

Extracted VERBATIM from ``farna/api/mcp_server.py`` as a behaviour-preserving
split of that module (structural hardening). Nothing here is reimplemented:
the decorators, signatures, defaults and docstrings are byte-identical.

Registration: importing this module decorates the handlers with
``@mcp.tool`` on the SAME ``FastMCP`` app instance created in
``farna.api.mcp_server``, so the advertised tool schema is unchanged.
``mcp_server`` imports this module at the end of its own body, after ``mcp``
and the shared spine exist, and re-exports the names the rest of the tree
still reaches through it, so ``from farna.api.mcp_server import <name>``
keeps working.

Shared helpers resolve lazily through ``mcp_tools._patch_surface`` so
``mock.patch("farna.api.mcp_server.<name>")`` still intercepts the call made
from this module (the test suite's stated patch contract).
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from farna.api.constants import SYSTEM_MAPS_FLAG
from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_DB_OPERATION_FAILED,
    _MSG_DISCOVER_SYSTEM_MAP_FAILED,
    _MSG_TOKEN_REVOKED,
    _MSG_UNEXPECTED_ERROR,
    _MSG_UUID_PARSE_FAILED,
    _RETRY_DB,
    _SYSTEM_MAP_DISCOVERY_SCOPE,
    _check_node_allowed_tools,
    _ctx_key_grants,
    _ctx_key_scope,
    _ctx_team_id_val,
    _mcp_flag_visible,
    _mcp_internal_lifecycle_map_access,
    _mcp_internal_system_maps_access,
    _parse_iso8601_param,
    _parse_optional_uuid,
    _require_mcp_internal_lifecycle_map_access,
    _require_mcp_internal_system_maps_access,
    _sqlalchemy_exception_response,
    _team_scope_error,
    _tool_auth_error,
    _tool_error,
    _tool_exception_error,
    _tool_session_contract_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    _log,
    _parse_uuid_param,
    _require_plan_entitlement,
    _require_plan_feature,
    _session,
    _system_maps_discovery_session,
    _team_scoped_key_mismatch,
    get_settings,
    patchable_local,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import is_sensitive_key as _shared_is_sensitive_key
from farna.core.mcp.scope_validator import MCPAuthorizationError

#: HTTP status -> branchable MCP error code for the reused System Maps route
#: handler. The handler is the REST surface's own implementation, so its
#: refusals arrive as ``HTTPException``; the MCP caller still needs a specific
#: code (FAR-1502), never a generic ``server_error`` for a knowable failure.
_SYSTEM_MAPS_HTTP_ERROR_CODES: dict[int, str] = {
    400: "invalid_request",
    402: "feature_required",
    403: "insufficient_scope",
    404: "not_found",
    409: "name_conflict",
    422: "validation_failed",
    501: "migration_required",
    503: "database_unavailable",
}


_SYSTEM_MAPS_AI_ERROR_CODES = frozenset({"model_backend_unauthorised", "eval_definition_unauthorised", "eval_gate_failed"})


def _system_maps_http_error_payload(exc: StarletteHTTPException) -> dict[str, Any]:
    """Render a System Maps route refusal as the MCP tool-error envelope.

    AI discovery uses a small, explicitly allow-listed structured-detail
    vocabulary. Other System Maps errors retain the generic transport mapping;
    in particular, arbitrary route detail dictionaries are never exposed.
    """
    code = _SYSTEM_MAPS_HTTP_ERROR_CODES.get(exc.status_code, "server_error")
    route_detail = exc.detail
    if isinstance(route_detail, dict):
        ai_detail: dict[str, Any] = route_detail
        ai_code = ai_detail.get("code")
        verdict = ai_detail.get("verdict")
        if ai_code in _SYSTEM_MAPS_AI_ERROR_CODES:
            payload: dict[str, Any] = {
                "error": ai_code,
                "detail": ai_detail.get("message", _MSG_UNEXPECTED_ERROR),
            }
            if ai_code == "eval_gate_failed" and verdict in {"fail", "error"}:
                payload["verdict"] = verdict
            return payload
    detail = route_detail if isinstance(route_detail, str) else _MSG_UNEXPECTED_ERROR
    return _tool_error(detail, code=code)


def _unwrap_system_maps_route_handler(handler: Any) -> Any:
    """Return the route handler with ``@handle_db_errors``'s wrapper removed.

    The wrapper is a single ``functools.wraps`` layer, so one ``__wrapped__``
    hop is exact. The ``inspect.isfunction`` guard keeps TEST DOUBLES out of the
    unwrap: mocks fabricate any attribute (including ``__wrapped__``), so
    blindly following the attribute would replace the double with a mock child
    instead of returning the handler under test.
    """
    if not inspect.isfunction(handler):
        return handler
    unwrapped = getattr(handler, "__wrapped__", None)
    return unwrapped if unwrapped is not None else handler


@mcp.tool(
    name="get_system_map_overview",
    description=(
        "Read the observed health, loop metrics and bottlenecks for a published System Maps "
        "revision (mirrors GET /api/v1/system-maps/{map_id}/overview). Requires the system_maps_read "
        "feature and system_map.overview permission; callers with authoring access (operator/admin "
        "role plus the system_maps_access grant) may select a draft or historical revision, while "
        "ordinary callers see published revisions only."
    ),
)
@mcp_audited("system_map_overview_read", "system_map", fail_closed=False)
@_RETRY_DB
async def get_system_map_overview(
    map_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
    at: datetime | None = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """Read System Maps overview through the canonical REST handler."""
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_system_map_overview")

        org_id = _ctx_org_id_val()
        user_id = _ctx_user_id_val()
        role = _ctx_role_val() or ""
        internal_access = _mcp_internal_system_maps_access()
        # FAR-2595: mirror the REST published router, which always gates the
        # overview on ``system_maps_read`` and uses ``internal_access`` ONLY to
        # widen revision selection (draft/historical). Gating the flag itself on
        # authoring access would refuse an operator the published overview
        # whenever the org's default-OFF authoring flag is off.
        feature_error = await _require_plan_feature(org_id, get_settings(), "system_maps_read")
        if feature_error is not None:
            return feature_error

        from farna.api.routes.system_maps import (
            OVERVIEW_WINDOW_MAX_DAYS,
            OVERVIEW_WINDOW_MIN_DAYS,
            get_repo,
        )
        from farna.api.routes.system_maps import get_system_map_overview_endpoint as rest_overview

        # Parity with the REST route's ``Query(ge=..., le=...)``: the handler is
        # invoked directly, so FastAPI never validates this bound for us.
        if not (OVERVIEW_WINDOW_MIN_DAYS <= window_days <= OVERVIEW_WINDOW_MAX_DAYS):
            return {
                "error": "validation_failed",
                "field": "window_days",
                "detail": (f"window_days must be between {OVERVIEW_WINDOW_MIN_DAYS} and {OVERVIEW_WINDOW_MAX_DAYS}, got {window_days}"),
            }

        rest_overview = _unwrap_system_maps_route_handler(rest_overview)
        from farna.auth.jwt import TenantPrincipal

        principal = TenantPrincipal(
            username=str(user_id),
            organisation_id=org_id,
            account_id=user_id,
            org_role=role,
            key_grants=_ctx_key_grants.get(None),
            key_scope=_ctx_key_scope.get(None),
            team_id=_ctx_team_id_val(),
        )
        async with _system_maps_discovery_session() as session:
            repository = await get_repo()
            result = await rest_overview(
                map_id=map_id,
                revision_id=revision_id,
                at=at,
                window_days=window_days,
                session=session,
                principal=principal,
                repository=repository,
                internal_access=internal_access,
            )
        return result.model_dump(mode="json")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except StarletteHTTPException as exc:
        return _system_maps_http_error_payload(exc)
    except ProgrammingError:
        _log.exception("get_system_map_overview failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.get_system_map_overview")) is not None:
            return contract_error
        _log.exception("get_system_map_overview failed")
        return _sqlalchemy_exception_response(exc, _MSG_DB_OPERATION_FAILED, "mcp.get_system_map_overview")
    except Exception as exc:
        _log.exception("get_system_map_overview failed")
        return _tool_exception_error("Failed to get System Maps overview", exc, "mcp.get_system_map_overview")


@mcp.tool(
    name="discover_system_map",
    description=(
        "Inspect the organisation and persist an unpublished System Maps "
        "discovery draft, returning the discovery report (draft map/revision ids, inferred "
        "nodes/edges, warnings, skipped sources, published=false; mirrors "
        "POST /api/v1/system-maps/discover). Requires the org-toggleable system_maps "
        "feature, an operator/admin role and the system_maps_access grant, plus an "
        "org-scoped credential — team-scoped and user-scoped credentials are refused "
        "because the scan covers the whole organisation; ordinary callers are refused."
    ),
)
@mcp_audited("system_map_discovery_drafted", "system_map", fail_closed=False)
@_RETRY_DB
async def discover_system_map(
    name: str = "Discovered System Map",
    description: str | None = None,
    model_backend_id: uuid.UUID | None = None,
    eval_definition_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Trigger System Maps discovery over MCP (FAR-2288).

    This tool deliberately does NOT re-implement inference, persistence or
    validation: it drives the SAME ``POST /api/v1/system-maps/discover``
    handler the REST surface uses, so the inventory query, the deterministic
    ``discover_draft`` inference, the persisted content envelope and the
    repository writes are one implementation. The only MCP-specific work is
    the transport gate (the REST dependency chain, re-applied here because the
    handler is invoked directly rather than through FastAPI), the principal
    construction from the request ContextVars, and the error/payload rendering.

    The handler is resolved through ``inspect.unwrap`` so the REST route's
    ``@handle_db_errors`` wrapper does not pre-empt the classification: that
    wrapper turns a session-contract violation into a generic 500 and an
    ``IntegrityError`` into 409 (rendered as ``name_conflict``), which would
    tell an agent to rename a map instead of retrying a concurrent uuid5
    revision write. Driving the UNDECORATED handler lets this tool classify DB
    failures itself, while the route's own genuine refusals (404/402/409 name
    conflict/422) remain plain ``HTTPException``s and are still mapped below.
    """
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("discover_system_map")
        _require_mcp_internal_system_maps_access()

        org_id = _ctx_org_id_val()
        user_id = _ctx_user_id_val()
        role = _ctx_role_val() or ""

        # Same internal gate as the REST route's ``require_feature`` dependency.
        # Evaluated AFTER the internal gate so an ordinary caller learns nothing
        # about the org's feature state.
        feature_error = await _require_plan_feature(org_id, get_settings(), SYSTEM_MAPS_FLAG)
        if feature_error is not None:
            return feature_error

        from pydantic import ValidationError as _PydanticValidationError

        from farna.api.schemas.system_maps import SystemMapDiscoveryRequest

        try:
            discovery_request = SystemMapDiscoveryRequest(
                name=name,
                description=description,
                model_backend_id=model_backend_id,
                eval_definition_id=eval_definition_id,
            )
        except _PydanticValidationError as exc:
            return {
                "error": "validation_failed",
                "detail": f"Discovery request validation failed: {exc.errors(include_url=False)}",
            }

        from farna.api.routes.system_maps import discover_system_map as rest_discover_system_map
        from farna.auth.jwt import TenantPrincipal

        # Resolve the UNDECORATED handler: ``@handle_db_errors`` on the REST
        # route would otherwise convert a session-contract ``InvalidRequestError``
        # to a generic 500 and an ``IntegrityError`` to 409 (``name_conflict``)
        # before this tool can classify them (FAR-2288). The REST route keeps
        # its own wrapper — only the MCP invocation bypasses it.
        rest_discover_system_map = _unwrap_system_maps_route_handler(rest_discover_system_map)

        principal = TenantPrincipal(
            username=str(user_id),
            organisation_id=org_id,
            account_id=user_id,
            org_role=role,
            key_grants=_ctx_key_grants.get(None),
            key_scope=_ctx_key_scope.get(None),
            team_id=_ctx_team_id_val(),
        )

        async with _system_maps_discovery_session() as session:
            report = await rest_discover_system_map(
                request=discovery_request,
                session=session,
                principal=principal,
            )
        return report.model_dump(mode="json")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except StarletteHTTPException as exc:
        return _system_maps_http_error_payload(exc)
    except ProgrammingError:
        _log.exception(_MSG_DISCOVER_SYSTEM_MAP_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        # Classify the DB failure HERE (the REST wrapper was bypassed above):
        # session-contract -> session_contract_error, a bounded lock wait ->
        # lock_timeout, IntegrityError -> conflict (a concurrent uuid5 revision
        # write, not a name clash), ProgrammingError -> migration_required,
        # anything else -> database_unavailable. The contract guard stays the
        # arm's FIRST statement so the static FAR-1464/FAR-1482 architecture
        # guard sees it.
        if (contract_error := _tool_session_contract_error(exc, _SYSTEM_MAP_DISCOVERY_SCOPE)) is not None:
            return contract_error
        _log.exception(_MSG_DISCOVER_SYSTEM_MAP_FAILED)
        return _sqlalchemy_exception_response(exc, _MSG_DB_OPERATION_FAILED, _SYSTEM_MAP_DISCOVERY_SCOPE)
    except Exception as exc:
        _log.exception(_MSG_DISCOVER_SYSTEM_MAP_FAILED)
        return _tool_exception_error("Failed to discover system map", exc, _SYSTEM_MAP_DISCOVERY_SCOPE)


@mcp.tool(
    name="get_audit_log",
    description=("Paged read of the organisation's audit trail with cursor pagination and filters (mirrors GET /api/v1/admin/audit)."),
)
@_RETRY_DB
async def get_audit_log(
    cursor: str | None = None,
    limit: int = 50,
    event_type: str | None = None,
    actor_user_id: str | None = None,
    resource_type: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_audit_log")

        org_id = _ctx_org_id_val()
        # Plan gate parity with the audit REST surface: GET /api/v1/admin/audit
        # is guarded by ``require_entitlement("audit_evidence")``. MCP must
        # reach the SAME answer rather than gate on one member flag, otherwise
        # killing ``audit_viewer`` alone leaves REST open while MCP refuses
        # (FAR-2119, FAR-2665).
        feat_err = await _require_plan_entitlement(org_id, get_settings(), "audit_evidence")
        if feat_err:
            return feat_err

        actor_uid: uuid.UUID | None = None
        if actor_user_id is not None:
            actor_uid, actor_err = _parse_uuid_param(actor_user_id, "actor_user_id")
            if actor_err:
                return actor_err
        from_dt: datetime | None = None
        if from_date is not None:
            try:
                from_dt = datetime.fromisoformat(from_date)
            except ValueError:
                return {
                    "error": "validation_failed",
                    "field": "from_date",
                    "detail": f"Invalid from_date format: {from_date!r} (expected ISO 8601)",
                }
        to_dt: datetime | None = None
        if to_date is not None:
            try:
                to_dt = datetime.fromisoformat(to_date)
            except ValueError:
                return {
                    "error": "validation_failed",
                    "field": "to_date",
                    "detail": f"Invalid to_date format: {to_date!r} (expected ISO 8601)",
                }

        from farna.core.audit_logger import list_audit_events

        lim = max(1, min(limit, 200))

        async with _session(org_id) as s:
            return await list_audit_events(
                s,
                org_id,
                cursor=cursor,
                limit=lim,
                event_type=event_type,
                actor_user_id=actor_uid,
                resource_type=resource_type,
                from_date=from_dt,
                to_date=to_dt,
            )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_audit_log failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_audit_log failed")
        return _tool_exception_error("Failed to read audit log", exc, "mcp.get_audit_log")


def _notification_delivery_filters(
    *,
    status: str | None,
    event_type: str | None,
    endpoint_uid: uuid.UUID | None,
    from_dt: datetime | None,
    to_dt: datetime | None,
    date_to_is_bare: bool = False,
    cursor_dt: datetime | None = None,
) -> list[Any]:
    """Build the shared WHERE clauses for the delivery-list and count queries."""
    from farna.db.models.notification_delivery import NotificationDeliveryLog

    clauses: list[Any] = []
    if status:
        clauses.append(NotificationDeliveryLog.status == status)
    if event_type:
        clauses.append(NotificationDeliveryLog.event_type == event_type)
    if endpoint_uid:
        clauses.append(NotificationDeliveryLog.endpoint_id == endpoint_uid)
    if from_dt:
        clauses.append(NotificationDeliveryLog.created_at >= from_dt)
    if to_dt:
        if date_to_is_bare:
            clauses.append(NotificationDeliveryLog.created_at < to_dt + timedelta(days=1))
        else:
            clauses.append(NotificationDeliveryLog.created_at <= to_dt)
    if cursor_dt:
        clauses.append(NotificationDeliveryLog.created_at < cursor_dt)
    return clauses


def _notification_delivery_page(rows: list[Any], lim: int) -> tuple[list[Any], str | None]:
    """Trim the limit+1 probe to the page size and derive the next cursor."""
    has_more = len(rows) > lim
    if has_more:
        rows = rows[:lim]
    next_cursor: str | None = None
    if has_more and rows and rows[-1][0].created_at:
        next_cursor = rows[-1][0].created_at.isoformat()
    return rows, next_cursor


def _notification_delivery_item(row: Any) -> dict[str, Any]:
    """Serialize one ``(NotificationDeliveryLog, endpoint_url)`` row."""
    delivery, endpoint_url = row
    return {
        "id": str(delivery.id),
        "event_type": delivery.event_type,
        "status": delivery.status,
        "attempt_count": delivery.attempt_count,
        "response_code": delivery.response_code,
        "last_error": delivery.last_error,
        "response_body": delivery.response_body,
        "endpoint_url": endpoint_url or "",
        "endpoint_id": str(delivery.endpoint_id) if delivery.endpoint_id else None,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else "",
    }


@mcp.tool(
    name="list_notification_deliveries",
    description=("List notification delivery-log entries (webhook/in-app delivery attempts) with cursor pagination and filters (mirrors GET /api/v1/admin/notifications/deliveries)."),
)
@_RETRY_DB
async def list_notification_deliveries(
    cursor: str | None = None,
    limit: int = 25,
    status: str | None = None,
    event_type: str | None = None,
    endpoint_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_notification_deliveries")

        endpoint_uid, endpoint_err = _parse_optional_uuid(endpoint_id, "endpoint_id")
        if endpoint_err:
            return endpoint_err
        from_dt, from_err = _parse_iso8601_param(date_from, "date_from")
        if from_err:
            return from_err
        to_dt, to_err = _parse_iso8601_param(date_to, "date_to")
        if to_err:
            return to_err
        date_to_is_bare = date_to is not None and len(date_to) == 10 and date_to[4] == "-" and date_to[7] == "-"
        cursor_dt, cursor_err = _parse_iso8601_param(cursor, "cursor")
        if cursor_err:
            return cursor_err

        from sqlalchemy import func as sa_func

        from farna.db.models.notification_delivery import NotificationDeliveryLog
        from farna.db.models.notification_endpoint import NotificationEndpoint

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            # Same query shape as the REST route's _list_deliveries: org-
            # scoped, endpoint URL joined for display, created_at DESC with a
            # limit+1 probe for the next-cursor decision. Reimplemented here
            # (rather than importing the route helper) because the MCP
            # ``_session`` already owns the transaction and the route helper
            # opens its own ``session.begin()``.
            query = (
                select(
                    NotificationDeliveryLog,
                    NotificationEndpoint.url,
                )
                .outerjoin(
                    NotificationEndpoint,
                    NotificationDeliveryLog.endpoint_id == NotificationEndpoint.id,
                )
                .where(
                    NotificationDeliveryLog.organisation_id == org_id,
                    *_notification_delivery_filters(
                        status=status,
                        event_type=event_type,
                        endpoint_uid=endpoint_uid,
                        from_dt=from_dt,
                        to_dt=to_dt,
                        date_to_is_bare=date_to_is_bare,
                        cursor_dt=cursor_dt,
                    ),
                )
                .order_by(NotificationDeliveryLog.created_at.desc())
                .limit(lim + 1)
            )

            rows = list((await s.execute(query)).all())

            count_query = select(sa_func.count(NotificationDeliveryLog.id)).where(
                NotificationDeliveryLog.organisation_id == org_id,
                *_notification_delivery_filters(
                    status=status,
                    event_type=event_type,
                    endpoint_uid=endpoint_uid,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    date_to_is_bare=date_to_is_bare,
                ),
            )
            total = (await s.execute(count_query)).scalar() or 0

        rows, next_cursor = _notification_delivery_page(rows, lim)

        return {
            "data": [_notification_delivery_item(d) for d in rows],
            "total": total,
            "next_cursor": next_cursor,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_notification_deliveries failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_notification_deliveries failed")
        return _tool_exception_error("Failed to list notification deliveries", exc, "mcp.list_notification_deliveries")


@mcp.tool(
    name="list_lifecycle_maps",
    description=("List lifecycle maps in the organisation with page-based pagination (mirrors GET /api/v1/lifecycle-maps)."),
)
@_RETRY_DB
async def list_lifecycle_maps(
    page: int = 1,
    page_size: int = 20,
    owner_team_id: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_lifecycle_maps")
        _require_mcp_internal_lifecycle_map_access()

        owner_team: uuid.UUID | None = None
        if owner_team_id is not None:
            owner_team, owner_err = _parse_uuid_param(owner_team_id, "owner_team_id")
            if owner_err:
                return owner_err

        from farna.core.lifecycle_map.service import list_lifecycle_maps as db_list_lifecycle_maps

        org_id = _ctx_org_id_val()
        pg = max(1, page)
        ps = max(1, min(page_size, 100))

        async with _session(org_id) as s:
            result = await db_list_lifecycle_maps(
                s,
                page=pg,
                page_size=ps,
                owner_team_id=owner_team,
                include_archived=include_archived,
            )

        return {
            "data": [
                {
                    "id": str(m.id),
                    "name": m.name,
                    "description": m.description,
                    "owner_team_id": str(m.owner_team_id) if m.owner_team_id else None,
                    "visibility": m.visibility,
                    "version": m.version,
                    "content_json": m.content_json,
                    "archived_at": m.archived_at.isoformat() if m.archived_at else None,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                    "updated_at": m.updated_at.isoformat() if m.updated_at else None,
                }
                for m in result.items
            ],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_lifecycle_maps failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_lifecycle_maps failed")
        return _tool_exception_error("Failed to list lifecycle maps", exc, "mcp.list_lifecycle_maps")


@mcp.tool(
    name="get_lifecycle_map",
    description=("Get a single lifecycle map by ID including its content_json (mirrors GET /api/v1/lifecycle-maps/{lifecycle_map_id})."),
)
@_RETRY_DB
async def get_lifecycle_map(lifecycle_map_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_lifecycle_map")
        _require_mcp_internal_lifecycle_map_access()

        org_id = _ctx_org_id_val()
        lmid, lmid_err = _parse_uuid_param(lifecycle_map_id, "lifecycle_map_id")
        if lmid_err:
            return lmid_err
        if lmid is None:
            return {"error": "invalid_id", "field": "lifecycle_map_id", "detail": _MSG_UUID_PARSE_FAILED}

        from farna.core.lifecycle_map.service import get_lifecycle_map as db_get_lifecycle_map

        async with _session(org_id) as s:
            lm = await db_get_lifecycle_map(s, lmid)

        # Belt-and-braces org check (mirrors the REST get route) — RLS already
        # scopes the row, but a cross-org id must read as not_found even if
        # RLS were misconfigured.
        if lm is None or lm.organisation_id != org_id:
            return {"error": "not_found", "detail": f"Lifecycle map {lifecycle_map_id} not found"}
        # Team-scoped-key boundary (same pattern as get_pipeline_graph): a
        # team-A key must not read a map owned by team B.
        if _team_scoped_key_mismatch(lm.owner_team_id):
            return _team_scope_error("lifecycle_map", lifecycle_map_id)

        return {
            "id": str(lm.id),
            "name": lm.name,
            "description": lm.description,
            "owner_team_id": str(lm.owner_team_id) if lm.owner_team_id else None,
            "visibility": lm.visibility,
            "version": lm.version,
            "content_json": lm.content_json,
            "archived_at": lm.archived_at.isoformat() if lm.archived_at else None,
            "created_at": lm.created_at.isoformat() if lm.created_at else None,
            "updated_at": lm.updated_at.isoformat() if lm.updated_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_lifecycle_map failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_lifecycle_map failed")
        return _tool_exception_error("Failed to get lifecycle map", exc, "mcp.get_lifecycle_map")


@mcp.tool(
    name="list_plugins",
    description=("List installed plugins with health status (mirrors GET /api/v1/plugins). Read-only registry metadata."),
)
async def list_plugins() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_plugins")

        org_id = _ctx_org_id_val()  # fail closed when no authenticated org context
        # Plan gate parity with GET /api/v1/plugins: the plugin registry is the
        # ``plugin_management`` entitlement, enforced there by
        # ``require_feature``. MCP must enforce it too rather than stay an
        # unentitled way in (FAR-2129).
        feat_err = await _require_plan_feature(org_id, get_settings(), "plugin_management")
        if feat_err:
            return feat_err

        from farna.core.plugin_registry import get_plugin_registry

        registry = get_plugin_registry()
        health_results = registry.health_check()
        plugins = registry.list_plugins()
        data: list[dict[str, Any]] = []
        for pid, manifest in plugins.items():
            health = health_results.get(pid)
            data.append(
                {
                    "id": manifest.PLUGIN_ID,
                    "display_name": manifest.display_name,
                    "description": manifest.description,
                    "version": manifest.version,
                    "capabilities": sorted(manifest.capabilities),
                    "health_ok": health.ok if health is not None else False,
                    "health_detail": health.detail if health is not None else "Unknown",
                    "health_checked_at": (health.checked_at.isoformat() if health is not None and health.checked_at else None),
                }
            )
        return {"data": data, "total": len(data)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except Exception as exc:
        _log.exception("list_plugins failed")
        return _tool_exception_error("Failed to list plugins", exc, "mcp.list_plugins")


@mcp.tool(
    name="list_sso_providers",
    description=("List the organisation's SSO provider configurations, metadata only — client secrets are never returned (mirrors GET /api/v1/admin/sso/providers). Read-only; configuration changes stay human-only."),
)
@_RETRY_DB
async def list_sso_providers() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_sso_providers")

        from farna.db.crud.sso_provider import list_providers

        org_id = _ctx_org_id_val()
        # Plan gate parity with GET /api/v1/admin/sso/providers: SSO
        # configuration is guarded by ``require_entitlement("identity")``. MCP
        # must reach the SAME answer rather than gate on one member flag,
        # otherwise killing ``sso`` alone leaves REST open while MCP refuses
        # (FAR-2129, FAR-2665).
        feat_err = await _require_plan_entitlement(org_id, get_settings(), "identity")
        if feat_err:
            return feat_err

        async with _session(org_id) as s:
            providers = await list_providers(s, org_id=org_id)

        # Client secrets (and bulky raw SAML metadata XML) are deliberately
        # omitted — config exposure stays human-only (FAR-696).
        return {
            "data": [
                {
                    "id": str(p.id),
                    "provider_type": p.provider_type,
                    "name": p.name,
                    "provider_id": p.provider_id,
                    "client_id": p.client_id,
                    "enabled": p.enabled,
                    "auto_provision": p.auto_provision,
                    "default_role": p.default_role,
                    "allowed_domains": p.allowed_domains or [],
                    "preset": p.preset,
                    "tenant_domain": p.tenant_domain,
                    "discovery_url": p.discovery_url,
                    "metadata_url": p.metadata_url,
                    "entity_id": p.entity_id,
                    "scopes": p.scopes,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                    "updated_at": p.updated_at.isoformat() if p.updated_at else None,
                }
                for p in providers
            ],
            "total": len(providers),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_sso_providers failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_sso_providers failed")
        return _tool_exception_error("Failed to list SSO providers", exc, "mcp.list_sso_providers")


SENSITIVE_CONFIG_KEYS: set[str] = {
    "fernet_key",
    "secret_key",
    "database_url",
    "db_url",
    "postgres_url",
    "redis_url",
    "api_key",
    "api_keys",
    "license_key",
    "modulo_secret_key",
    "modulo_fernet_key",
}


def _is_sensitive_key(key: str) -> bool:
    # Primary net: the shared substring matcher used by the HTTP masking
    # surface (_SENSITIVE_KEY_PATTERNS covers secret/password/token/key/
    # credential/api_key/...), so key names like
    # "product_analytics_instance_secret", "smtp_password" or "api_token"
    # are excluded from get_org_config even without a known prefix match.
    # Over-masking is safe: the only consumer is row exclusion from the
    if _shared_is_sensitive_key(key):
        return True
    lower = key.lower()
    if any(lower.startswith(prefix) for prefix in SENSITIVE_CONFIG_KEYS):
        return True
    # Also match colon-separated segments (e.g. "assistant_config:{org}:api_key")
    return any(any(segment.startswith(prefix) for prefix in SENSITIVE_CONFIG_KEYS) for segment in lower.split(":"))


def _value_looks_sensitive(val: str) -> bool:
    """True when *val* matches a known secret-format pattern (API key, PAT, etc.)."""
    from farna.core.secret_patterns import SECRET_VALUE_PATTERNS, SECRET_VALUE_REDACT_CHAR_CAP

    # Bound the pattern scan: never run SECRET_VALUE_PATTERNS over an
    # unbounded string; the module caps redaction input at
    # SECRET_VALUE_REDACT_CHAR_CAP chars for exactly this ReDoS/cost reason.
    bounded = val[:SECRET_VALUE_REDACT_CHAR_CAP]
    return any(pattern.search(bounded) for pattern, _replacement in SECRET_VALUE_PATTERNS)


_VALID_CONFIG_SECTIONS = {"assistant", "plan", "rate_limits"}


def _config_key_prefixes(section: str | None) -> list[str] | None:
    """Key prefixes matching a config section, or None to match all sections."""
    org_ctx = f"{_ctx_org_id_val()}"
    if section == "assistant":
        return [f"assistant_config:{org_ctx}", "assistant_config"]
    if section in {"plan", "rate_limits"}:
        return ["feature_flags", "default_plan", "rate_limits"]
    return None


def _config_matches(cfg: Any, key_prefixes: list[str] | None) -> bool:
    """True when *cfg* falls within *key_prefixes* and is not a sensitive key."""
    if key_prefixes is not None and not any(cfg.key.startswith(p) for p in key_prefixes):
        return False
    return not _is_sensitive_key(cfg.key)


def _config_table(filtered: list[Any]) -> str:
    """Render config rows as a markdown table with long values truncated."""
    lines = ["| Key | Value |", "|-----|-------|"]
    for cfg in filtered:
        val = cfg.value
        val_str = json.dumps(val, default=str) if isinstance(val, dict) else str(val)
        if _value_looks_sensitive(val_str):
            val_str = "\u2022\u2022\u2022\u2022\u2022\u2022"
        elif len(val_str) > 200:
            val_str = val_str[:200] + "..."
        lines.append(f"| {cfg.key} | {val_str} |")
    return "\n".join(lines)


@mcp.tool(
    description=("Get org-level configuration. Optionally filter to a specific section (assistant, plan, rate_limits). Never exposes secrets."),
)
async def get_org_config(section: str | None = None) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1942: mirror the REST twin ``GET /api/v1/system-admin/config``,
        # gated by ``require_system_permission`` (a strict is_system_admin claim
        # with no org-role fall-through). The requirement is declared once in
        # ``SYSTEM_ADMIN_TOOLS``; this chokepoint call is what makes it LIVE
        # rather than merely declared (the FAR-1930 wired-chain requirement).
        _check_agent_tool_scope("get_org_config")
        if section is not None and section not in _VALID_CONFIG_SECTIONS:
            return {
                "error": "invalid_section",
                "detail": f"section must be one of: {', '.join(sorted(_VALID_CONFIG_SECTIONS))}",
            }
        from farna.db.crud.system_config import list_config

        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            configs = await list_config(s)

        key_prefixes = _config_key_prefixes(section)
        filtered = [cfg for cfg in configs if _config_matches(cfg, key_prefixes)]

        if not filtered:
            section_label = section or "org"
            return {"results": f"No configuration found for section '{section_label}'.", "count": 0}

        return {"results": _config_table(filtered), "count": len(filtered)}
    except MCPAuthorizationError as exc:
        # FAR-1942: same refusal envelope as every other system-admin-gated
        # MCP tool (e.g. ``list_housekeeping``), so the two surfaces cannot
        # diverge on the caller-facing shape.
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_org_config failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_org_config failed")
        return _tool_exception_error("Failed to get org configuration", exc, "mcp.get_org_config")


@mcp.tool(
    description="List product features enabled on the current plan tier.",
)
async def get_available_features() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("get_available_features")
        from farna.core.feature_flags import resolve_plan_context

        org_id = _ctx_org_id_val()
        settings = get_settings()

        from farna.db.crud.organisation import get_organisation

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)

        async with _session(org_id) as s:
            plan_ctx = await resolve_plan_context(settings, s, org)

        current_tier = plan_ctx.tier()
        internal_lifecycle_access = _mcp_internal_lifecycle_map_access()
        all_flags = [
            flag
            for flag in plan_ctx.list_enabled_features()
            if _mcp_flag_visible(
                flag.name,
                internal_lifecycle_access=internal_lifecycle_access,
            )
        ]

        lines = ["| Feature | Required Tier | Available |", "|---------|---------------|-----------|"]
        for flag in all_flags:
            available = "yes" if flag.currently_active else "no"
            lines.append(f"| {flag.name} | {flag.tier} | {available} |")

        return {"results": "\n".join(lines), "tier": current_tier, "feature_count": len(all_flags)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_available_features failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_available_features failed")
        return _tool_exception_error("Failed to get available features", exc, "mcp.get_available_features")


@mcp.tool(
    description="List housekeeping cleanup candidates for the organisation. "
    "Returns categories of potential cleanup items such as orphan secrets, "
    "unbound connectors, stale pipelines, and other candidates. "
    "(mirrors GET /api/v1/admin/housekeeping — requires an ADMIN org role AND "
    "a system-admin claim; a system admin whose org role is below admin is "
    "also refused. API-key and OAuth callers always fail this check, so only "
    "a JWT-authenticated session can invoke it.)",
)
@_RETRY_DB
async def list_housekeeping(limit: int = 100) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1879/FAR-1930: the REST list route is gated by
        # ``require_permission`` (admin org-role floor, pinned in the scope
        # map) AND ``require_system_permission`` (a strict is_system_admin gate
        # with no org-role fall-through). Both legs are enforced by
        # ``_check_agent_tool_scope``: the org-role floor via the scope map and
        # the claim via SYSTEM_ADMIN_TOOLS. The scan surfaces org-wide secret
        # NAMES and other entities, so a runner/operator credential must not
        # reach it.
        _check_agent_tool_scope("list_housekeeping")
        from farna.core.housekeeping import scan_all as hk_scan_all

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 500))
        async with _session(org_id) as s:
            results = await hk_scan_all(s, org_id)
        return {
            "categories": [
                {
                    "category": r.category,
                    "label": r.label,
                    "description": r.description,
                    "candidates": [c.to_dict() for c in r.candidates[:lim]],
                    "count": len(r.candidates),
                    # FAR-1977: mirrors the REST ``HousekeepingCategory`` shape —
                    # ``status`` is "ok" for a scanner that ran (empty = clean)
                    # and "failed" for one that raised, whose ``error`` carries
                    # the exception CLASS only (no message/traceback).
                    "status": r.status,
                    "error": r.error,
                }
                for r in results
            ],
            "total_count": sum(len(r.candidates) for r in results),
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_housekeeping failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_housekeeping failed")
        return _tool_exception_error("Failed to list housekeeping candidates", exc, "mcp.list_housekeeping")


def _group_housekeeping_items(items: list[dict[str, str]], errors: list[dict[str, str]]) -> dict[str, list[str]]:
    """Group items by entity_type, collecting malformed items into *errors*."""
    grouped: dict[str, list[str]] = {}
    for item in items:
        et = item.get("entity_type", "")
        eid = item.get("id", "")
        if not et or not eid:
            errors.append({"error": "item missing entity_type or id", "item": str(item)})
            continue
        grouped.setdefault(et, []).append(eid)
    return grouped


async def _delete_housekeeping_group(
    s: AsyncSession,
    entity_type: str,
    ids: list[str],
    model_cls: Any,
    org_id: uuid.UUID,
    errors: list[dict[str, str]],
) -> int:
    """Delete each id in *ids* under a savepoint. Returns the number deleted."""
    from farna.core.housekeeping import delete_one_in_savepoint

    deleted_count = 0
    for eid in ids:
        try:
            # FAR-1857: count only AFTER the savepoint released.
            # ``delete_one_in_savepoint`` returns True only once the DELETE
            # (flushed on savepoint exit) has committed, so an FK RESTRICT
            # refusal cannot be counted as a deletion. Shared with the REST
            # route so the two cannot drift.
            if await delete_one_in_savepoint(s, model_cls, eid, org_id):
                deleted_count += 1
        except IntegrityError as exc:
            _log.warning("IntegrityError cleaning up %s %s", entity_type, eid)
            from farna.db.crud.policy_gate_decision import is_policy_gate_decision_fk_error

            if is_policy_gate_decision_fk_error(exc):
                errors.append(
                    {
                        "id": eid,
                        "entity_type": entity_type,
                        "error": (f"Cannot delete {entity_type} {eid}: governance decision records present — archive or purge policy_gate_decisions first"),
                        "blocked_by": "policy_gate_decisions",
                    }
                )
            else:
                errors.append({"id": eid, "entity_type": entity_type, "error": "Foreign key constraint violation"})
    return deleted_count


async def _delete_housekeeping_groups(
    s: AsyncSession,
    grouped: dict[str, list[str]],
    hk_entity_map: Mapping[str, Any],
    org_id: uuid.UUID,
    errors: list[dict[str, str]],
) -> int:
    """Delete all grouped housekeeping items. Returns the total deleted count."""
    from farna.core.housekeeping import NON_DELETABLE_ENTITY_TYPES

    deleted_count = 0
    for entity_type, ids in grouped.items():
        if entity_type in NON_DELETABLE_ENTITY_TYPES:
            errors.append({"entity_type": entity_type, "error": "Surfaced for triage only — not auto-deleted."})
            continue
        model_cls = hk_entity_map.get(entity_type)
        if model_cls is None:
            errors.append({"entity_type": entity_type, "error": f"Unknown entity type: {entity_type}"})
            continue
        deleted_count += await _delete_housekeeping_group(s, entity_type, ids, model_cls, org_id, errors)
    return deleted_count


@mcp.tool(
    description="Delete housekeeping cleanup candidates. "
    "Accepts a list of items with id and entity_type. "
    "Valid entity types: secret, connector, model_backend, pipeline, "
    "pipeline_snapshot, trigger, webhook_dedup, environment_profile, "
    "org_api_key, sso_provider, team, parameter_schema, schema, lifecycle_map. "
    "Deletions are grouped by entity type with per-group savepoints. "
    "(mirrors POST /api/v1/admin/housekeeping/cleanup — requires an ADMIN org "
    "role AND a system-admin claim; a system admin whose org role is below "
    "admin is also refused. API-key and OAuth callers always fail this check, "
    "so only a JWT-authenticated session can invoke it.)",
)
@mcp_audited("housekeeping_performed", "housekeeping", fail_closed=True)
async def perform_housekeeping(items: list[dict[str, str]]) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1879/FAR-1930: the REST cleanup route is ``require_permission``
        # (admin org-role floor, pinned in the scope map) AND
        # ``require_system_permission`` — a STRICT is_system_admin gate with no
        # org-role fall-through. Both legs are enforced centrally by
        # ``_check_agent_tool_scope`` (the claim via SYSTEM_ADMIN_TOOLS), so
        # the destructive MCP path matches REST and API keys/OAuth tokens
        # always fail it.
        _check_agent_tool_scope("perform_housekeeping")
        from farna.core.housekeeping import ENTITY_MODEL_MAP as HK_ENTITY_MAP

        org_id = _ctx_org_id_val()
        errors: list[dict[str, str]] = []

        grouped = _group_housekeeping_items(items, errors)

        async with _session(org_id) as s:
            deleted_count = await _delete_housekeeping_groups(s, grouped, HK_ENTITY_MAP, org_id, errors)

        return {"deleted_count": deleted_count, "errors": errors}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("perform_housekeeping failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("perform_housekeeping failed")
        return _tool_exception_error("Failed to perform housekeeping", exc, "mcp.perform_housekeeping")


# Keep the suite's `patch.object(farna.api.mcp_server, "<helper>")` bindings
# live for the wrappers above: each call re-resolves the helper from
# `mcp_server`, which re-exports THIS module's original through PEP 562.
_delete_housekeeping_group = patchable_local("_delete_housekeeping_group", _delete_housekeeping_group)
