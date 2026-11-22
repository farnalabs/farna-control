"""Analytics MCP tools (query_analytics / query_analytics_concurrency).

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
from datetime import date as _date
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.exc import ProgrammingError

from farna.api.mcp_server import _MSG_DB_MIGRATION_REQUIRED, _MSG_TOKEN_REVOKED, _RETRY_DB, _ctx_team_id_val, _log, _tool_auth_error, _tool_exception_error, mcp
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_role_val,
    _ctx_user_id_val,
    _get_session_factory,
    _require_plan_feature,
    get_settings,
    run_analytics_query,
    run_concurrency_query,
    validate_current_auth,
)
from farna.core.analytics.builder import AnalyticsDimension, AnalyticsGroupBy, AnalyticsStatus, AnalyticsTriggerType
from farna.core.analytics.service import AnalyticsDatabaseError, AnalyticsMigrationRequiredError, AnalyticsParams, AnalyticsQueryTimeoutError, AnalyticsRateLimitedError, AnalyticsValidationError
from farna.core.mcp.scope_validator import MCPAuthorizationError


def _parse_mcp_datetime(value: str, name: str) -> datetime:
    """Parse an MCP date/datetime param (bare date or ISO datetime) into a datetime.

    Matches the REST surface: "2026-08-06" is accepted as midnight, ISO
    datetimes accept a trailing 'Z' (Python 3.11+ fromisoformat handles it).
    """
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return datetime.combine(_date.fromisoformat(value), datetime.min.time())
    except ValueError:
        raise AnalyticsValidationError(f"{name}: invalid date value {value!r}") from None


def _analytics_deep_link(result: dict[str, Any], params: AnalyticsParams) -> str:
    """Relative /analytics deep link carrying the same filters as the query.

    Built from the RESOLVED result (``group_by``/``dimension``/``date_from``/
    ``date_to`` reflect the service's normalised effective range) plus the raw
    ``params`` filters (trigger/status/pipeline/folder/error_code). Emitted only
    on the MCP surface so Assistant can hand the user a clickable, pre-filtered link
    to the /analytics view. The REST route keeps its clean ``AnalyticsResponse``
    contract — this field is presentation-only.
    """
    parts: list[tuple[str, str]] = [("group_by", str(result.get("group_by") or params.group_by.value))]
    dimension = result.get("dimension")
    if dimension:
        parts.append(("dimension", str(dimension)))
    if params.trigger_type is not None:
        parts.append(("trigger_type", params.trigger_type.value))
    if params.status is not None:
        parts.append(("status", params.status.value))
    parts.extend(("pipeline_id", str(pid)) for pid in params.pipeline_ids)
    if params.error_code is not None:
        parts.append(("error_code", params.error_code))
    if params.folder_id is not None:
        parts.append(("folder_id", str(params.folder_id)))
    date_from = result.get("date_from")
    if date_from:
        parts.append(("date_from", str(date_from)))
    date_to = result.get("date_to")
    if date_to:
        parts.append(("date_to", str(date_to)))
    return "/analytics?" + urlencode(parts)


async def _require_analytics_feature(org_id: uuid.UUID, settings: Any) -> dict[str, Any] | None:
    return await _require_plan_feature(org_id, settings, "analytics_page")


def _parse_analytics_enums(
    group_by: str,
    trigger_type: str | None,
    status: str | None,
    dimension: str | None = None,
    error_detail: str = "",
) -> tuple[
    AnalyticsGroupBy | None,
    AnalyticsTriggerType | None,
    AnalyticsStatus | None,
    AnalyticsDimension | None,
    dict[str, Any] | None,
]:
    """Parse analytics enum params, returning ``(group_by, trigger, status, dimension, error)``."""
    try:
        grp = AnalyticsGroupBy(group_by)
        tt = AnalyticsTriggerType(trigger_type) if trigger_type is not None else None
        st = AnalyticsStatus(status) if status is not None else None
        dim = AnalyticsDimension(dimension) if dimension is not None else None
    except ValueError:
        return (
            None,
            None,
            None,
            None,
            {
                "error": "invalid_params",
                "detail": error_detail,
            },
        )
    return grp, tt, st, dim, None


def _parse_analytics_ids(
    pipeline_id: list[str] | None,
    folder_id: str | None,
) -> tuple[tuple[uuid.UUID, ...], uuid.UUID | None, dict[str, Any] | None]:
    """Parse the analytics pipeline/folder filters, returning ``(pids, fid, error)``."""
    pids: tuple[uuid.UUID, ...] = ()
    if pipeline_id:
        try:
            pids = tuple(uuid.UUID(p) for p in pipeline_id)
        except ValueError:
            return (), None, {"error": "invalid_params", "detail": "pipeline_id entries must be valid UUIDs"}

    fid: uuid.UUID | None = None
    if folder_id is not None:
        try:
            fid = uuid.UUID(folder_id)
        except ValueError:
            return (), None, {"error": "invalid_params", "detail": f"Invalid folder_id UUID: {folder_id}"}
    return pids, fid, None


def _build_analytics_params(
    group_by: AnalyticsGroupBy,
    auto_granularity: bool,
    trigger_type: AnalyticsTriggerType | None,
    status: AnalyticsStatus | None,
    dimension: AnalyticsDimension | None,
    pipeline_ids: tuple[uuid.UUID, ...],
    folder_id: uuid.UUID | None,
    error_code: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> AnalyticsParams:
    return AnalyticsParams(
        group_by=group_by,
        auto_granularity=auto_granularity,
        dimension=dimension,
        trigger_type=trigger_type,
        status=status,
        pipeline_ids=pipeline_ids,
        team_id=_ctx_team_id_val(),
        error_code=error_code,
        folder_id=folder_id,
        date_from=_parse_mcp_datetime(date_from, "date_from") if date_from is not None else None,
        date_to=_parse_mcp_datetime(date_to, "date_to") if date_to is not None else None,
        limit=max(1, min(limit, 1000)),
    )


def _parse_analytics_params(
    dimension: str | None,
    group_by: str,
    auto_granularity: bool,
    trigger_type: str | None,
    status: str | None,
    pipeline_id: list[str] | None,
    folder_id: str | None,
    error_code: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[AnalyticsParams | None, dict[str, Any] | None]:
    grp, tt, st, dim, enum_err = _parse_analytics_enums(
        group_by,
        trigger_type,
        status,
        dimension,
        error_detail=f"invalid enum value (dimension={dimension!r} group_by={group_by!r})",
    )
    if enum_err:
        return None, enum_err
    assert grp is not None  # nosec B101 -- _parse_analytics_enums returns (None, error) only on failure, already handled above
    pids, fid, id_err = _parse_analytics_ids(pipeline_id, folder_id)
    if id_err:
        return None, id_err
    params = _build_analytics_params(grp, auto_granularity, tt, st, dim, pids, fid, error_code, date_from, date_to, limit)
    return params, None


def _parse_analytics_concurrency_params(
    group_by: str,
    auto_granularity: bool,
    trigger_type: str | None,
    status: str | None,
    pipeline_id: list[str] | None,
    folder_id: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
) -> tuple[AnalyticsParams | None, dict[str, Any] | None]:
    grp, tt, st, _dim, enum_err = _parse_analytics_enums(
        group_by,
        trigger_type,
        status,
        dimension=None,
        error_detail=f"invalid enum value (group_by={group_by!r})",
    )
    if enum_err:
        return None, enum_err
    assert grp is not None  # nosec B101 -- _parse_analytics_enums returns (None, error) only on failure, already handled above
    pids, fid, id_err = _parse_analytics_ids(pipeline_id, folder_id)
    if id_err:
        return None, id_err
    params = _build_analytics_params(grp, auto_granularity, tt, st, None, pids, fid, None, date_from, date_to, limit)
    return params, None


@dataclass(frozen=True)
class _AnalyticsQueryInput:
    """Raw MCP tool params for an analytics query (or concurrency query)."""

    dimension: str | None = None
    group_by: str = "day"
    auto_granularity: bool = False
    trigger_type: str | None = None
    status: str | None = None
    pipeline_id: list[str] | None = None
    error_code: str | None = None
    folder_id: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 1000


async def _query_analytics_impl(input: _AnalyticsQueryInput) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("query_analytics")

    org_id = _ctx_org_id_val()
    settings = get_settings()

    feat_err = await _require_analytics_feature(org_id, settings)
    if feat_err:
        return feat_err

    params, p_err = _parse_analytics_params(
        input.dimension,
        input.group_by,
        input.auto_granularity,
        input.trigger_type,
        input.status,
        input.pipeline_id,
        input.folder_id,
        input.error_code,
        input.date_from,
        input.date_to,
        input.limit,
    )
    if p_err:
        return p_err
    if params is None:
        raise RuntimeError("_query_analytics_impl: parse returned no error and no params")

    result = await run_analytics_query(
        org_id=org_id,
        params=params,
        factory=_get_session_factory(),
        settings=settings,
        account_id=_ctx_user_id_val(),
        org_role=_ctx_role_val() or "",
    )
    result["deep_link"] = _analytics_deep_link(result, params)
    return result


@mcp.tool(
    name="query_analytics",
    description=(
        "Query run analytics over the daily facts table. Returns a bucketed series "
        "(hour/day/week) with per-bucket count, cost, tokens, duration, success rate, "
        "failure and stall counts, queue wait, claim→dispatch latency "
        "(`avg_dispatch_latency_ms` = dispatch_phase_entered_at - created_at, else "
        "started_at - created_at), final idle, output size, and node-deadline "
        "watchdog firings (`node_deadline_watchdog_fired_count` = firings summed "
        "across the bucket; > 0 means the watchdog fired — recorded whether the kill "
        "re-dispatched or terminal-failed, so a value > 0 with no matching "
        "`error_code=node_deadline_exceeded` means the kill re-dispatched). "
        "Accepts a repeated pipeline_id for A-vs-B comparisons in a single request, "
        "and error_code for filtering/grouping by failure code. `dimension` groups the "
        "series by a key — `trigger_type`, `trigger_id`, `status`, `pipeline`, `folder`, "
        "`team`, `error_code` or `execution_origin` — which is how per-trigger latency is "
        "read, and how a dispatched run is told apart from one Modulo executed "
        "(`execution_origin` groups on the FAR-1141 run provenance column: `dispatched` "
        "vs NULL). The result "
        "also carries a `deep_link` to the /analytics view pre-filtered with the same "
        "parameters — share that link instead of dumping the raw buckets. Requires "
        "the analytics.query permission and the analytics_page plan feature."
    ),
)
@_RETRY_DB
async def query_analytics(
    dimension: str | None = None,
    group_by: str = "day",
    auto_granularity: bool = False,
    trigger_type: str | None = None,
    status: str | None = None,
    pipeline_id: list[str] | None = None,
    error_code: str | None = None,
    folder_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    try:
        return await _query_analytics_impl(
            _AnalyticsQueryInput(
                dimension=dimension,
                group_by=group_by,
                auto_granularity=auto_granularity,
                trigger_type=trigger_type,
                status=status,
                pipeline_id=pipeline_id,
                error_code=error_code,
                folder_id=folder_id,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except AnalyticsRateLimitedError:
        return {"error": "rate_limited", "detail": "Rate limit exceeded"}
    except AnalyticsValidationError as exc:
        return {"error": "invalid_params", "detail": exc.detail}
    except AnalyticsQueryTimeoutError as exc:
        return {"error": "query_timeout", "detail": str(exc)}
    except AnalyticsMigrationRequiredError as exc:
        return {"error": "migration_required", "detail": str(exc)}
    except AnalyticsDatabaseError as exc:
        return {"error": "database_error", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("query_analytics failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("query_analytics failed")
        return _tool_exception_error("Failed to query analytics", exc, "mcp.query_analytics")


async def _query_analytics_concurrency_impl(input: _AnalyticsQueryInput) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    _check_agent_tool_scope("query_analytics_concurrency")

    org_id = _ctx_org_id_val()
    settings = get_settings()

    feat_err = await _require_analytics_feature(org_id, settings)
    if feat_err:
        return feat_err

    params, p_err = _parse_analytics_concurrency_params(
        input.group_by,
        input.auto_granularity,
        input.trigger_type,
        input.status,
        input.pipeline_id,
        input.folder_id,
        input.date_from,
        input.date_to,
        input.limit,
    )
    if p_err:
        return p_err
    if params is None:
        raise RuntimeError("_query_analytics_concurrency_impl: parse returned no error and no params")

    return await run_concurrency_query(
        org_id=org_id,
        params=params,
        factory=_get_session_factory(),
        settings=settings,
        account_id=_ctx_user_id_val(),
        org_role=_ctx_role_val() or "",
    )


@mcp.tool(
    name="query_analytics_concurrency",
    description=(
        "Query slot utilization / concurrency over the daily facts table. Returns a "
        "bucketed series (hour/day/week) with per-bucket max and average concurrent "
        "active runs (computed from [started_at, completed_at) overlap — a run "
        "spanning a bucket boundary counts in both) and max and average queued runs "
        "(created before started_at; never-started runs count as queued through the "
        "range). Also returns pool_reference: the org run_concurrency_limit, or a "
        "single filtered pipeline's max_concurrent_runs. Accepts a repeated "
        "pipeline_id filter. Requires the analytics.query permission and the "
        "analytics_page plan feature."
    ),
)
@_RETRY_DB
async def query_analytics_concurrency(
    group_by: str = "day",
    auto_granularity: bool = False,
    trigger_type: str | None = None,
    status: str | None = None,
    pipeline_id: list[str] | None = None,
    folder_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    try:
        return await _query_analytics_concurrency_impl(
            _AnalyticsQueryInput(
                group_by=group_by,
                auto_granularity=auto_granularity,
                trigger_type=trigger_type,
                status=status,
                pipeline_id=pipeline_id,
                folder_id=folder_id,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
            )
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except AnalyticsRateLimitedError:
        return {"error": "rate_limited", "detail": "Rate limit exceeded"}
    except AnalyticsValidationError as exc:
        return {"error": "invalid_params", "detail": exc.detail}
    except AnalyticsQueryTimeoutError as exc:
        return {"error": "query_timeout", "detail": str(exc)}
    except AnalyticsMigrationRequiredError as exc:
        return {"error": "migration_required", "detail": str(exc)}
    except AnalyticsDatabaseError as exc:
        return {"error": "database_error", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("query_analytics_concurrency failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("query_analytics_concurrency failed")
        return _tool_exception_error("Failed to query analytics concurrency", exc, "mcp.query_analytics_concurrency")
