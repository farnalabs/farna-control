"""Remote MCP server — thin adapter over the ViewModel API.

Mounted at `/mcp` as a Starlette sub-application inside FastAPI.

Auth: API key bearer token (`Authorization: Bearer mk_<key>`).
      Validated by McpAuthMiddleware before the request reaches FastMCP.
      org_id and role are stored in a ContextVar for tool handlers.

Dual-layer enforcement:
  1. Middleware: validates key, rejects unauthenticated requests.
  2. Tool layer: checks role (operator vs runner) before sensitive ops.

Org context validated per-event for streaming (SSE) connections.
"""

import asyncio
import contextvars
import functools
import json
import logging
import threading
import time
import traceback as _traceback
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple, ParamSpec
from urllib.parse import quote

from jwt import InvalidTokenError as JWTError

# Ensure pydantic's lazily-loaded `root_model` submodule is resident before
# `mcp` builds `class JSONRPCMessage(RootModel[...])` at import time. Coverage's
# single-module source-spec resolution imports-then-rolls-back modules
# (coverage 7.16.2), which can delete `pydantic.root_model` while leaving a
# dangling `RootModel` attribute, crashing that specialization. See the
# "pydantic.root_model residency guard" note in backend/tests/conftest.py
# (the dedicated regression test was removed with the library sync package).
import pydantic.root_model  # noqa: F401  # isort: skip

from mcp.server.fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route
from tenacity import before_sleep_log, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from farna.api.db_error_handling import (
    _MSG_LOCK_TIMEOUT,
    raise_session_contract_error,
    session_contract_error_payload,
)
from farna.api.dependencies import (
    get_or_create_engine,
    get_or_create_session_factory,
)
from farna.api.hitl_answer_validation import (
    validate_hitl_answer,  # noqa: F401
)
from farna.api.middleware.rate_limiter import RateLimitMiddleware as RateLimiterMiddleware

# Re-exported for the documented patch surface: `api/mcp_tools/pipelines.py`
# resolves `merge_masked_graph_nodes` from THIS module at call time, so
# `mock.patch.object(mcp_server, "merge_masked_graph_nodes")` still intercepts.
from farna.api.middleware.sensitive_mask import merge_masked_graph_nodes  # noqa: F401

# FAR-2328: the MCP approve path resumes the pipeline inline, exactly as the
# REST approve route does. Reuse that route's executor builder (notifier-wired)
# and its capacity-deferred response body rather than duplicating either — the
# established sibling-route-module import pattern (`evals`, `triggers` above).
# ``modulo.api.routes.hitl`` imports nothing from ``mcp_server``, so there is
# no circular-import risk.
from farna.api.routes.hitl import (
    _build_resume_executor,  # noqa: F401
    _restore_failed_mcp_resume_for_reconcile,  # noqa: F401
    _run_is_f6a_reconcile_candidate,  # noqa: F401
)
from farna.api.routes.oauth_metadata import protected_resource_metadata_url
from farna.api.routes.triggers import (
    _require_unique_trigger_name,  # noqa: F401
    _streak_status_for,  # noqa: F401
)

# Re-exported for the documented patch surface: `api/mcp_tools/secrets_api_keys.py`
# resolves these names from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.auth_create_api_key")` and its siblings still
# intercept the relocated API-key tools.
from farna.auth.api_key import (
    ApiKeyGrantsUnavailableError,
    ApiKeyInvalidError,
    api_key_grants_enabled,  # noqa: F401
    resolve_key_grants,
    validate_api_key,
)
from farna.auth.api_key import (
    create_api_key as auth_create_api_key,  # noqa: F401
)
from farna.auth.api_key import (
    list_api_keys as auth_list_api_keys,  # noqa: F401
)
from farna.auth.api_key import (
    revoke_api_key as auth_revoke_api_key,  # noqa: F401
)
from farna.auth.dependencies import resolve_role_from_membership
from farna.auth.human_linkage import (
    LINKAGE_OAUTH_CONSENT,
    LINKAGE_USER_SCOPED_KEY,
    HumanLinkage,
    required_permission_for_action,
    resolve_human_linkage,
)
from farna.auth.oauth import (
    check_oauth_token_family_valid,
    clamp_oauth_role,
    decode_oauth_access_token,
    get_oauth_client_team_id,
    oauth_grant_set,
    scopes_required_role,
)
from farna.auth.permissions import (
    SYSTEM_MAPS_ACCESS_PERMISSION,
    _clamp_role,
    grants_permit,
    set_authz_enforce,
)
from farna.auth.team_rbac import ORG_ROLE_HIERARCHY

# Re-exported for the documented patch surface: `api/mcp_tools/analytics.py`
# resolves both names from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.run_analytics_query")` still intercepts.
from farna.core.analytics.service import (
    run_analytics_query,  # noqa: F401
    run_concurrency_query,  # noqa: F401
)
from farna.core.cost_controller.breakdown.constants import RAW_REPORTED_DISPLAY_CLAMP
from farna.core.cost_controller.finalize import finalize_cancelled_run  # noqa: F401
from farna.core.cron_helpers import (
    compute_next_fire,  # noqa: F401
    validate_cron_expression,  # noqa: F401
)
from farna.core.dispatch import dispatch_run  # noqa: F401
from farna.core.documentation_indexer import DocumentationIndex

# ContextVars populated by McpAuthMiddleware before each request.
# Propagation: this server runs FastMCP in stateless HTTP mode, where each request
# spawns a fresh per-request server task *from the already-authenticated request
# coroutine* (StreamableHTTPSessionManager._handle_stateless_request calls
# task_group.start(...) at request time). asyncio/anyio copy the caller's context
# at task-creation time, so values set here in the middleware propagate to tool
# handlers. If a handler ever runs without this context, tenant resolution FAILS
# CLOSED (auth error) — there must never be a process-global fallback, because
# under concurrent multi-tenant load a global would resolve to whichever org
# authenticated last, leaking cross-tenant data.
from farna.core.feature_flags import (
    INTERNAL_FLAGS,
    RETIRED_INTERNAL_FLAGS,
    get_registry,
    resolve_mcp_human_linked_hitl_authority,
    resolve_plan_context,
)
from farna.core.hitl_manager import (
    HITLManager,  # noqa: F401
)

# Re-exported for the documented patch surface: `api/mcp_tools/library.py`
# resolves these names from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.list_primitives")` and its siblings still
# intercept the relocated library tools/resources.
from farna.core.library_service import (
    copy_to_adapt as library_copy_to_adapt,  # noqa: F401
)
from farna.core.library_service import (
    get_primitive,  # noqa: F401
    get_primitive_by_slug,  # noqa: F401
    list_primitives,  # noqa: F401
)
from farna.core.library_service.primitive_types import PRIMITIVE_TYPES  # noqa: F401
from farna.core.logging_config import org_id_var
from farna.core.mcp.scope_validator import (
    MCPAuthorizationError,
    MCPSystemAdminRequiredError,
    check_node_allowed_tools,
    check_tool_scope,
    set_request_allowed_tools,
)

# Re-exported for the documented patch surface: `api/mcp_tools/runs.py`
# resolves both names from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.map_legacy_code")` still intercepts.
from farna.core.pipeline_engine.error_codes import (  # noqa: F401
    map_legacy_code,
    present_error,
)
from farna.core.rate_limiter import TokenBucketRegistry
from farna.core.runtime_config.key_bridge import public_url_is_configured

# Re-exported for the documented patch surface: `api/mcp_tools/connectors.py`
# resolves `create_secrets_backend` from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.create_secrets_backend")` still intercepts.
from farna.core.secrets_backend import (
    create_secrets_backend,  # noqa: F401
)
from farna.core.trigger_streak import (
    anchor_trigger_streak_epoch,  # noqa: F401
    clear_trigger_streak_after_reenable,  # noqa: F401
)

# Referenced as ``farna.api.mcp_server.HitlReviewWeakeningDenied`` by the suite
# (the relocated graph-write audit maps this exception); keep the name here.
from farna.db.crud.hitl_review_guard import HitlReviewWeakeningDenied  # noqa: F401

# Re-exported for the documented patch surface: `api/mcp_tools/connectors.py`
# resolves `db_create_model_backend` from THIS module at call time, so
# `mock.patch("farna.api.mcp_server.db_create_model_backend")` still intercepts.
from farna.db.crud.model_backend import create_model_backend as db_create_model_backend  # noqa: F401
from farna.db.crud.org_membership import resolve_live_role_and_kind
from farna.db.crud.pipeline import get_pipeline  # noqa: F401
from farna.db.crud.run import get_run  # noqa: F401
from farna.db.crud.run_node_outputs import read_run_blobs  # noqa: F401
from farna.db.crud.schema import create_schema as db_create_schema  # noqa: F401
from farna.db.crud.schema import get_schema  # noqa: F401
from farna.db.crud.schema import list_schemas as db_list_schemas  # noqa: F401
from farna.db.models.account import KIND_HUMAN
from farna.db.models.run import Run
from farna.db.rls import set_rls_org, set_rls_user_context
from farna.db.settings_resolver import resolve_authz_enforce
from farna.db.sqlstates import LOCK_NOT_AVAILABLE_SQLSTATE, sqlstate_of
from farna.settings import get_settings
from farna.util import callable_label

# NOTE: a `if TYPE_CHECKING:` block used to sit here as an empty placeholder;
# it held no imports and was removed as a deliberate no-op (S108). Re-add it
# together with the first type-only import it is meant to guard.

_log = logging.getLogger(__name__)

# S1192: hoisted duplicated literals (rate-limit bucket fallback + tool error
# detail shared by the parameter-schema restore/restore-set paths).
# NOTE: the OR_DELETED suffix is REQUIRED — this module already defines a
# pre-existing ``_MSG_PARAM_SCHEMA_NOT_FOUND`` ("Parameter schema not found",
# the lookup/list tools' message) further down, and a same-named second
# definition would bind LAST, silently shadowing one of the two values at
# every call site (FAR-1522 regression: the restore payloads briefly emitted
# the short string).
_CLIENT_USER_UNKNOWN = "user:unknown"
_MSG_PARAM_SCHEMA_NOT_FOUND_OR_DELETED = "Parameter schema not found or not deleted"

_CT_APPLICATION_JSON = "application/json"
_MSG_TOKEN_REVOKED = "Token revoked or expired - re-authenticate"  # nosec B105 -- user-facing error message string, NOT a secret credential
_MSG_ERROR_TOKEN_REVOKED = "error: Token revoked or expired - re-authenticate"  # nosec B105 -- user-facing error message string, NOT a secret credential
_MSG_DB_MIGRATION_REQUIRED = "Database migration required. Run `alembic upgrade head`."
_MSG_DB_MIGRATION_REQUIRED_HEADS = "Database migration required. Run alembic upgrade heads."
_MSG_TRIGGER_NOT_FOUND = "Trigger not found"
_MSG_USER_NOT_TEAM_MEMBER = "You are not a member of the team that owns this pipeline"
_MSG_UUID_PARSE_FAILED = "UUID parse failed"
_MSG_EVAL_DEF_CREATE_FAILED = "create_eval_definition failed"
_MSG_EVAL_DEF_UPDATE_FAILED = "update_eval_definition failed"
_MSG_EVAL_DEF_DELETE_FAILED = "delete_eval_definition failed"
_MSG_DB_OPERATION_FAILED = "Database operation failed. Please try again."
_MSG_DISCOVER_SYSTEM_MAP_FAILED = "discover_system_map failed"
_SYSTEM_MAP_DISCOVERY_SCOPE = "mcp.discover_system_map"
# FAR-1825: SQLSTATE 55P03 from a bounded row-lock wait is a busy-row CONFLICT,
# not an outage: the engine is healthy and re-issuing once the other change
# commits may succeed. The message is imported from ``db_error_handling`` (the
# single declaration) rather than re-typed here, so the two error surfaces
# cannot drift; the code is ``lock_timeout`` so a consuming agent branches on it
# instead of retrying a generic database outage.
_MSG_CREATE_API_KEY_FAILED = "create_api_key failed"
_MSG_LIST_API_KEYS_FAILED = "list_api_keys failed"
_MSG_REVOKE_API_KEY_FAILED = "revoke_api_key failed"
_MSG_CREATE_SCHEMA_FAILED = "create_schema failed"
_MSG_DB_TEMPORARILY_UNAVAILABLE = "Database temporarily unavailable"
_MSG_FEATURE_NOT_AVAILABLE_MIGRATE = "Feature is not available. Run database migrations to enable it."
_MSG_DB_ERROR_TRY_AGAIN = "Database error occurred. Please try again."
_MSG_UNEXPECTED_ERROR = "An unexpected error occurred"
_MSG_MCP_AUTH_DB_UNAVAILABLE = "mcp.auth.db_unavailable"
_CODE_PERMISSION_API_KEY_ROLE_CAP = "permission.api_key_role_cap"
# FAR-1825: the tool keys for the two bounded parameter CRUD paths, hoisted so
# the contract / lock-timeout / failure classifiers share one declaration.
_CODE_MCP_UPDATE_PARAMETER_SCHEMA = "mcp.update_parameter_schema"
_CODE_MCP_UPDATE_PARAMETER_SET = "mcp.update_parameter_set"
_JSON_AUTH_DB_UNAVAILABLE = '{"error":"temporarily_unavailable","detail":"Auth backend temporarily unavailable"}'
_JSON_FORBIDDEN_ORG_MEMBERSHIP = '{"error":"forbidden","detail":"Organisation membership required"}'
_BASIC_PREFIX = "Basic "

_MCP_SANITIZE_STRING_MAX = 256
_MCP_BREAKDOWN_KEYS = frozenset(
    {
        "component",
        "display_name",
        "source",
        "formula_applied",
        "rate_usd",
        "amount_usd",
        "basis",
        "missing_self_report",
        # FAR-1305/FAR-1308: which of the THREE missing-self-report states
        # applies — "agent_not_reported" (no cost key presented),
        # "zero_report_unproven" (an explicit $0.00 was presented and refused),
        # or "sub_floor_rejected" (a positive value below the countable floor
        # was presented and refused). Sanitised as a string by
        # _sanitize_cost_breakdown_entry like any other.
        "missing_self_report_reason",
        "error",
        "total_clamped",
    }
)


def _sanitize_mcp_string(value: str) -> str:
    """Truncate an agent-controlled string to 256 chars + strip control chars."""
    cleaned = "".join(ch for ch in value if ch == "\t" or ord(ch) >= 32)
    return cleaned[:_MCP_SANITIZE_STRING_MAX]


def _clamp_mcp_number(value: float) -> float:
    """Magnitude-clamp any numeric field that can carry a hostile raw value
    (e.g. ``basis.raw_reported`` of 1e300) at 1e6 for display — the MCP surface
    cannot render 1e300. Mirrors the breakdown serializer's display clamp.
    """
    try:
        d = Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return 0.0
    if not d.is_finite() or abs(d) > RAW_REPORTED_DISPLAY_CLAMP:
        return float(RAW_REPORTED_DISPLAY_CLAMP)
    return float(d)


def _sanitize_mcp_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {k: _sanitize_mcp_basis_value(v) for k, v in value.items()}


def _sanitize_mcp_sequence(value: list[Any]) -> list[Any]:
    return [_sanitize_mcp_basis_value(v) for v in value]


def _sanitize_mcp_basis_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_mcp_string(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _clamp_mcp_number(value)
    if isinstance(value, dict):
        return _sanitize_mcp_mapping(value)
    if isinstance(value, list):
        return _sanitize_mcp_sequence(value)
    if value is None:
        return None
    return _sanitize_mcp_string(str(value))


def _sanitize_cost_breakdown(breakdown: Any) -> list[dict[str, Any]]:
    """MCP whole-resource sanitize of a run's ``cost_breakdown``.

    Every agent-controlled string — ``component``, ``display_name``,
    ``formula_applied``, and recursively every string in ``basis`` — is
    truncated to 256 chars + stripped of control chars; numeric/boolean fields
    are type-validated; out-of-shape keys are stripped. Numeric fields that can
    carry a hostile raw magnitude (``basis.raw_reported`` /
    ``basis.per_node_raw``) are magnitude-clamped at 1e6 for display.
    """
    if not isinstance(breakdown, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for entry in breakdown:
        if not isinstance(entry, dict):
            continue
        out = _sanitize_cost_breakdown_entry(entry)
        sanitized.append(out)
    return sanitized


def _sanitize_cost_breakdown_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single cost-breakdown entry (key-filtering + type-dispatch)."""
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key not in _MCP_BREAKDOWN_KEYS:
            continue
        if key == "basis":
            out[key] = _sanitize_mcp_basis_value(value)
        elif isinstance(value, str):
            out[key] = _sanitize_mcp_string(value)
        elif isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = _clamp_mcp_number(value)
        else:
            out[key] = _sanitize_mcp_string(str(value))
    return out


_MCP_COST_ROLLUP_ZERO = Decimal("0.000000")
_MCP_COST_ROLLUP_QUANTUM = Decimal("0.000001")


def _quantize_mcp_cost_rollup(value: Decimal) -> Decimal:
    """Normalise a cost rollup value to 6 decimal places (Numeric(14, 6) scale).

    Mirrors the REST runs API's ``_quantize_cost_rollup`` so the MCP run
    resources render the same ``child_runs_cost_usd`` / ``aggregate_cost_usd``
    values as the REST surface.
    """
    return value.quantize(_MCP_COST_ROLLUP_QUANTUM)


def _format_breakdown_line(entry: dict[str, Any]) -> str:
    """Compact single-line rendering of a sanitized breakdown entry."""
    name = entry.get("display_name") or entry.get("component") or "component"
    amount = entry.get("amount_usd")
    if amount is None:
        amount = entry.get("rate_usd")
    source = entry.get("source", "")
    parts = [f"- {name} ({entry.get('component', '')}): ${amount or '0.000000'}"]
    if source:
        parts.append(source)
    if entry.get("missing_self_report") is True:
        # FAR-1305: an explicit $0.00 that was REFUSED is not "not reported" —
        # the agent did report, the trust boundary rejected it as unproven.
        if entry.get("missing_self_report_reason") == "zero_report_unproven":
            parts.append("(reported $0.00, rejected as unproven)")
        elif entry.get("missing_self_report_reason") == "sub_floor_rejected":
            # FAR-1308: a positive value below the countable floor was
            # presented and refused - the agent DID report, so this must
            # never render as "not reported".
            parts.append("(reported a value below the countable minimum)")
        else:
            parts.append("(not reported)")
    if entry.get("error"):
        parts.append(f"({entry['error']})")
    return " ".join(parts)


_RETRY_DB = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception_type(OperationalError),
    reraise=True,
    before_sleep=before_sleep_log(_log, logging.WARNING),
)

_ctx_org_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_org_id")
_ctx_role: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_role")
_ctx_key_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_key_id")
_ctx_auth_token: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_token")
_ctx_user_id: contextvars.ContextVar[uuid.UUID] = contextvars.ContextVar("mcp_user_id")
_ctx_auth_type: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_auth_type")
# ADR 063 step 1 (FAR-1957): the OAuth access token's signed ``token_family``
# claim, set only on the OAuth dispatch path. ``None`` for API keys / regular
# JWTs (no signed family to observe). Read by the human-linkage resolver; never
# populated from a client-supplied field.
_ctx_token_family: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_token_family", default=None)
_ctx_team_id: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar("mcp_team_id")
# FAR-620: the credential's caller scope — 'org' (org-wide/team/run keys),
# 'user' (a user-scoped API key, a regular JWT session, or an OAuth token),
# None (unset — the caller-scope leg of the tool-scope check fails closed).
_ctx_key_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_key_scope", default=None)
# FAR-1477: the request's API-key grant-set. ``None`` = no grant-set (JWT/OAuth
# sessions and legacy role-bundle keys); an empty frozenset is explicit deny-all.
_ctx_key_grants: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar("mcp_key_grants", default=None)
# FAR-620: per-request resolution of the ``user_scoped_mcp_keys`` org flag.
# Read once per request by the auth middleware (no caching across requests);
# fail-closed default False so a read failure never broadens access.
_ctx_user_keys_enabled: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_user_keys_enabled", default=False)
# FAR-436: node-level allowed_tools for a run-scoped sandbox key. Set by
# ``_authenticate_api_key`` when the caller is a run-scoped key; None (default)
# means no node-level narrowing (legacy behaviour). Empty list = deny-all.
_ctx_node_allowed_tools: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("mcp_node_allowed_tools", default=None)
# FAR-696: the principal's ``is_system_admin`` flag, lifted by the auth paths
# so system-admin-only tools (``list_feature_flags`` — REST
# ``require_system_permission``) can enforce the SAME strict gate MCP-side.
# Fail-closed default False: API keys and OAuth tokens never carry the flag
# (mirroring REST, where API-key principals are constructed with
# ``is_system_admin=False`` and OAuth claims do not carry it); only a regular
# JWT whose decoded principal holds the flag sets it True.
_ctx_is_system_admin: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_is_system_admin", default=False)
# FAR-2281: the credential owner's ``Account.kind`` when a USER-scoped mk_ key
# is bound to a live human (``human``) — else None. Drives the audit
# attribution: the action is recorded against the human, never collapsed to a
# generic api_key principal. Fail-closed default None.
_ctx_principal_kind: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_principal_kind", default=None)


def _bind_org_context(
    request: Request | None,
    org_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> None:
    """Publish the MCP-resolved organisation where ERROR capture can see it (FAR-1417).

    ``McpAuthMiddleware`` resolves the tenant into ``_ctx_org_id`` for the tool
    handlers, but ``ErrorTrackingLogHandler`` reads a DIFFERENT contextvar
    (``org_id_var``) synchronously on every log record — left unwired, every
    ``_log.exception`` on this surface (auth DB failures, the tool-shell error
    mapping, the HITL decision-budget check) was dropped before it could reach
    ``error_events``.

    Same two carriers as ``auth.dependencies.bind_principal_context``:

    * ``org_id_var`` — visible to the middleware's own frame and, once
      ``call_next`` creates the downstream task, to every tool handler
      (asyncio copies the caller's context at task creation — the same
      propagation the ``_ctx_*`` vars above rely on);
    * ``request.state`` — ASGI-scope-backed, so an OUTER middleware
      (``CatchAllMiddleware``) can re-bind after a failure raised above this
      mounted sub-app, where the contextvar is not visible.

    Call it (a) exactly where ``_ctx_org_id`` is set on the success paths, and
    (b) BEFORE any database operation whose org was already resolved by an
    earlier step — that is what makes the ``mcp.auth.db_unavailable`` failure
    arms attributable. Never bind earlier than the resolution itself: when the
    failing operation IS the org lookup, the record stays an announced drop
    rather than being attributed to a guessed organisation.

    ``request`` is ``None`` only where the caller frame has no request object
    (``_verify_oauth_token_family``) — the contextvar carrier still applies.
    """
    org = str(org_id)
    org_id_var.set(org)
    if request is not None:
        request.state.organisation_id = org
        if account_id is not None:
            request.state.user_id = str(account_id)


class McpAuthContextError(LookupError):
    """Raised when a handler runs without an authenticated tenant context.

    Fail-closed guard: tenant scope must come from the request-scoped
    ContextVars set by McpAuthMiddleware. There is deliberately no
    process-global fallback and no placeholder org.
    """


def _ctx_org_id_val() -> uuid.UUID:
    """Get org_id from the request context. Fails closed if unset."""
    v = _ctx_org_id.get(None)
    if v is None:
        raise McpAuthContextError("No authenticated organisation context for this MCP request")
    return v


def _ctx_user_id_val() -> uuid.UUID:
    """Get user/account_id from the request context. Fails closed if unset."""
    v = _ctx_user_id.get(None)
    if v is None:
        raise McpAuthContextError("No authenticated user context for this MCP request")
    return v


def _ctx_role_val() -> str | None:
    """Get role from the request context (None if unset — scope checks then fail closed)."""
    return _ctx_role.get(None)


def _mcp_internal_lifecycle_map_access() -> bool:
    """Return whether the MCP caller has the retained internal lifecycle-map grant."""
    if not getattr(get_settings(), "modulo_dev_mode", True):
        return False
    role = _ctx_role_val()
    return ORG_ROLE_HIERARCHY.get(role or "", -1) >= ORG_ROLE_HIERARCHY["operator"] and grants_permit(_ctx_key_grants.get(None), "library.search.in_dev")


def _require_mcp_internal_lifecycle_map_access() -> None:
    if not _mcp_internal_lifecycle_map_access():
        raise MCPAuthorizationError("Lifecycle map tools are restricted to internal operators")


def _mcp_internal_system_maps_access() -> bool:
    """Return whether the MCP caller may author System Maps.

    FAR-2595: this mirrors the REST authoring gate exactly — the operator/admin
    role floor plus the dedicated ``system_maps_access`` grant. Developer mode
    is deliberately NOT required, so MCP dogfooding works on a self-hosted
    Alpha deployment running with dev mode off.

    The grant leg goes through ``grants_permit`` — the same live resolver the
    REST gate reaches via ``require_in_dev_operator`` -> ``_enforce_key_grant``
    -> ``assert_grant``. Membership is never tested by hand: ``grants_permit``
    re-reads ``is_delegable`` on every call, so marking the permission
    non-delegable revokes it from already-minted grant-bearing credentials
    without a second code change. ``grants is None`` (JWTs, legacy keys) does
    not narrow, matching REST.
    """
    role = _ctx_role_val()
    return ORG_ROLE_HIERARCHY.get(role or "", -1) >= ORG_ROLE_HIERARCHY["operator"] and grants_permit(_ctx_key_grants.get(None), SYSTEM_MAPS_ACCESS_PERMISSION)


def _require_mcp_internal_system_maps_access() -> None:
    """Refuse an MCP System Maps authoring call without the REST gate's context.

    The boolean leg is the same one ``/api/v1/system-maps`` reaches through
    ``_require_internal_system_maps_access``: operator/admin role plus the
    dedicated ``system_maps_access`` grant, with no developer-mode leg
    (FAR-2595). The role and grant legs are ALSO declared in the shared
    tool-scope registry, so this call-site check is defence in depth.
    """
    if not _mcp_internal_system_maps_access():
        raise MCPAuthorizationError("System Maps authoring is restricted to operators with the system_maps_access grant")


def _mcp_flag_visible(
    flag_name: str,
    *,
    internal_lifecycle_access: bool,
) -> bool:
    """Return whether the MCP caller may see ``flag_name`` in a flag listing.

    Retired internal flags need the retained lifecycle-map gate; any other
    internal-only flag is hidden from ordinary callers; everything else
    (including ``system_maps``, a normal default-OFF flag since FAR-2595) is
    listed for every caller.
    """
    if flag_name in RETIRED_INTERNAL_FLAGS:
        return internal_lifecycle_access
    return flag_name not in INTERNAL_FLAGS


def _ctx_team_id_val() -> uuid.UUID | None:
    """Get the team boundary of the current request (None when no team boundary).

    Set by ``McpAuthMiddleware`` only when the caller authenticated with a
    team-scoped credential: an API key with a non-null ``OrgApiKey.team_id``
    or a token minted by an OAuth client bound to a team. Org-wide API keys,
    org-wide OAuth clients and regular user (Assistant) JWTs carry no team
    boundary and resolve to ``None`` — they are org-role-only, matching the
    REST layer.
    """
    return _ctx_team_id.get(None)


def _ctx_node_allowed_tools_val() -> list[str] | None:
    """Get the node-level allowed_tools for the current request (None = no narrowing).

    Set by ``_authenticate_api_key`` when the caller is a run-scoped sandbox
    key whose node declares ``capability_scope.allowed_tools``. ``None`` (the
    default) preserves pre-scope behaviour; an empty list is deny-by-default.
    """
    return _ctx_node_allowed_tools.get(None)


def _ctx_is_system_admin_val() -> bool:
    """FAR-696: True when the request's principal holds ``is_system_admin``.

    Fail-closed: unset resolves to False (the ContextVar default), so only the
    auth paths that explicitly lifted the flag can ever pass a system-admin
    gate — the same strict ``require_system_permission`` posture REST uses.
    """
    return bool(_ctx_is_system_admin.get(False))


def _log_system_admin_denial(tool_name: str) -> None:
    """Emit the single canonical warning for a system-admin MCP refusal (FAR-1879/FAR-1930).

    FAR-1930: the requirement now lives in the shared scope resolver
    (``SYSTEM_ADMIN_TOOLS`` declares the tools; ``resolve_tool_access``
    enforces the claim). The resolver recognises the denial but does not log
    it, because the MCP surface owns the one call-site line: ``mcp_audited``
    records only a coarse ``outcome="error"`` with no call-site context, so
    without this a stream of refusals is invisible and indistinguishable from
    operational errors.

    The reason is always ``system_admin_required``: ``check_tool_scope``
    denies an unset auth context with a plain ``MCPAuthorizationError`` before
    the system-admin leg is reached, so a system-admin denial always has a
    live role. The caller-facing envelope is built from the resolver's own
    message in ``_check_agent_tool_scope`` — this helper only logs.
    """
    _log.warning("MCP tool '%s' refused: system_admin_required", tool_name)


def _check_agent_tool_scope(tool_name: str, action: str | None = None) -> None:
    """Reuse the ``check_tool_scope`` chokepoint with node-level allowed_tools (FAR-436).

    Every live MCP agent tool-call is gated at this single chokepoint: the role
    must still permit the tool, AND (when the node declares
    ``capability_scope.allowed_tools``) the tool must be on the node's
    allow-list. Absent scope = legacy behaviour (role check only).

    FAR-620: the request's credential caller-scope (``_ctx_key_scope``) and
    auth type are threaded into the chokepoint so the caller-scope leg of the
    tool-scope decision sees the live credential class (None fails closed).

    FAR-2288: the credential's team boundary (``_ctx_team_id``) is threaded too,
    so the ``org-credential`` caller-scope classification can refuse a
    team-scoped credential before an organisation-wide tool runs.

    FAR-1930: the request's live ``is_system_admin`` claim is threaded too, so
    the resolver's system-admin leg (for the tools declared in
    ``SYSTEM_ADMIN_TOOLS``) is evaluated here rather than in each tool body.
    """
    try:
        check_tool_scope(
            _ctx_role_val(),
            tool_name,
            action=action,
            allowed_tools=_ctx_node_allowed_tools_val(),
            key_scope=_ctx_key_scope.get(None),
            auth_type=_ctx_auth_type.get(None),
            grants=_ctx_key_grants.get(None),
            is_system_admin=_ctx_is_system_admin_val(),
            team_id=_ctx_team_id_val(),
        )
    except MCPSystemAdminRequiredError as exc:
        # FAR-1930: the shared resolver owns the gate. This adapter emits the
        # single canonical call-site warning and re-raises the resolver's own
        # refusal message, so the tool handler's ``except
        # MCPAuthorizationError`` envelope is byte-identical to the
        # pre-centralisation response.
        _log_system_admin_denial(tool_name)
        raise MCPAuthorizationError(str(exc)) from None


def _check_node_allowed_tools(tool_name: str) -> None:
    """Enforce ONLY the node-level allowed_tools narrowing (FAR-1976).

    The handful of read-only MCP tools that bypass ``_check_agent_tool_scope``
    (they declare no role/system-admin requirement, so they have no full-
    chokepoint leg to route through) must still honour a node's
    ``capability_scope.allowed_tools`` — the chokepoint's OTHER leg. Each such
    handler calls this helper at its top, before any DB/work, so "read-only" is
    not mistaken for "in scope".

    Reads the SAME request-scoped source as the chokepoint
    (``_ctx_node_allowed_tools_val()``, with the shared resolver's own
    request-header fallback); ``None`` = UNRESTRICTED/legacy. Raises
    ``MCPAuthorizationError`` with the byte-identical chokepoint leg-1 message,
    so the handler's ``except MCPAuthorizationError`` arm produces the same
    ``insufficient_scope`` envelope a full-chokepoint refusal does. The role
    and system-admin legs are deliberately NOT re-derived here — these tools
    declare neither, and re-deriving them would change their semantics.
    """
    check_node_allowed_tools(tool_name, allowed_tools=_ctx_node_allowed_tools_val())


def _resource_node_scope_error(tool_name: str) -> str | None:
    """Return the resource-body refusal when a node scope excludes *tool_name*.

    FAR-1976: ``@mcp.resource`` handlers returned the same tenant data the
    gated tools expose, so a run-scoped node whose ``allowed_tools`` excluded a
    tool could still read it through ``resources/read``. Each tool-backed
    resource gates under its duplicated tool's name via this adapter, which
    applies the SAME node-scope leg as the tool handlers but renders the
    refusal as resource text (MCP resources return plain strings, not the tool
    handlers' structured error envelope).

    Returns ``None`` when the read is allowed (including the ``None`` /
    unrestricted legacy case), otherwise the ``error: insufficient_scope: ...``
    body to return verbatim.
    """
    try:
        _check_node_allowed_tools(tool_name)
    except MCPAuthorizationError as exc:
        return f"error: insufficient_scope: {exc}"
    return None


# FAR-1184: raising or clearing a pipeline's spend circuit-breaker threshold
# requires the same org-admin permission as the circuit-breaker reset.
_CODE_COST_MANAGE = "cost.manage"  # nosec B105 — permission scope name, not a credential


def _ctx_may_manage_cost() -> bool:
    """FAR-1184: True when the request's role holds ``cost.manage`` (fail-closed).

    Uses the SAME authority as every other permission check — the ADR 047
    registry (``resolve_required``) plus the org-role hierarchy check
    (``assert_org_role``). A missing/unknown role raises ``PermissionDenied``
    and resolves to False, never True.
    """
    from farna.auth.permissions import PermissionDenied, assert_org_role, grants_and_role, resolve_required

    def _role_ok() -> bool:
        try:
            assert_org_role(_ctx_role_val(), resolve_required(_CODE_COST_MANAGE), _CODE_COST_MANAGE)
        except PermissionDenied:
            return False
        return True

    return grants_and_role(_ctx_key_grants.get(None), _CODE_COST_MANAGE, _role_ok)


def _team_scoped_key_mismatch(owner_team_id: uuid.UUID | None) -> bool:
    """True when a team-scoped credential must not access a resource owned by *owner_team_id*.

    The boundary applies to any credential that carries a team (non-null
    ``_ctx_team_id``): a team-scoped API key OR a token minted by a
    team-bound OAuth client. Org-wide keys and plain user (Assistant) tokens
    carry no team boundary. A resource with no owning team (org-level
    pipeline) is accessible to any team-scoped credential; a resource owned
    by a different team is blocked.
    """
    key_team_id = _ctx_team_id.get(None)
    if key_team_id is None:
        return False
    return owner_team_id is not None and owner_team_id != key_team_id


def _team_scope_error(resource_kind: str, resource_id: str) -> dict[str, Any]:
    """Error dict for a team-boundary violation by a team-scoped credential (API key or OAuth client)."""
    key_team_id = _ctx_team_id.get(None)
    return {
        "error": "team_boundary_violation",
        "detail": (f"This credential is scoped to team {key_team_id} and cannot access {resource_kind} {resource_id} owned by another team"),
    }


def _team_scope_error_str(resource_kind: str, resource_id: str) -> str:
    """String error (resource surface) for a team-boundary violation."""
    err = _team_scope_error(resource_kind, resource_id)
    return f"error: {err['error']} — {err['detail']}"


async def _pipeline_owner_team_id(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve the owning team of a pipeline (None for org-level pipelines).

    Thin wrapper over the shared ``team_scope`` resolver so MCP guards and the
    DB list filters share one effective-owner source.
    """
    from farna.db.crud.team_scope import pipeline_owner_team_id

    return await pipeline_owner_team_id(session, pipeline_id)


async def _run_owner_team_id(session: AsyncSession, run: Run) -> uuid.UUID | None:
    """Resolve the effective owning team of a run (None for org-level runs).

    ``Run.owner_team_id`` is the source of truth (snapshot at creation, see
    ``create_run``), but pre-existing runs predate the stamp and carry NULL.
    Falling back to the pipeline's current ``owner_team_id`` keeps the
    boundary enforced for those rows too — a NULL run owner must never mean
    "visible to every team-scoped key".
    """
    if run.owner_team_id is not None:
        return run.owner_team_id
    return await _pipeline_owner_team_id(session, run.pipeline_id)


async def _pipeline_team_gate(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Fail-closed team gate for MCP trigger mutations (FAR-1513 CRITICAL fix).

    Replaces the fail-open pair ``(caller-facing owner read +
    _user_team_private_denial on the RLS-hidden visibility)``: a caller-facing
    read returns NOTHING for a team-private pipeline the principal cannot see,
    so ``owner → None`` passed the key-boundary check (no owned team = no
    boundary) and the unknown visibility passed the membership matrix. The
    principal's real denial was invisible because the inputs to the decision
    were themselves team-filtered.

    This gate resolves the row through ``pipeline_team_scope_team_blind`` (a
    ONE-read team-blind flip of ``app.execution_context``) and re-evaluates on
    the RESOLVED row:

    1. row absent (or soft-deleted) in the org → ``pipeline_not_found``
       envelope — a hidden row is a denial, never an allow;
    2. team-scoped key boundary applied on the RESOLVED owner (MCP semantics:
       the boundary applies to any owned pipeline, org-visible included —
       unlike the REST dependency, which bounds keys only on team-private
       rows);
    3. key principals pass (their memberships are irrelevant);
    4. user principals run the shared ``evaluate_team_gate`` matrix
       (admin bypass / org-visible allow / membership-or-deny, fail closed on
       an unset user identity).

    Returns ``(owner_team_id, denial)``: ``owner_team_id`` is the resolved
    effective owner (None for an org-level pipeline); ``denial`` is the
    envelope the caller MUST return (and never mutate) when the principal may
    not proceed.
    """
    from farna.api.constants import MSG_PIPELINE_NOT_FOUND
    from farna.api.team_scope import evaluate_team_gate
    from farna.db.crud.team_scope import pipeline_team_scope_team_blind

    scope = await pipeline_team_scope_team_blind(session, pipeline_id)
    if scope is None:
        return None, {"error": "pipeline_not_found", "detail": MSG_PIPELINE_NOT_FOUND}
    if _team_scoped_key_mismatch(scope.owner_team_id):
        return scope.owner_team_id, _team_scope_error("pipeline", str(pipeline_id))
    if _ctx_team_id_val() is not None:
        # A team-scoped key that passed the boundary has no membership matrix.
        return scope.owner_team_id, None
    denial = await evaluate_team_gate(
        session,
        row_present=True,
        owner_team_id=scope.owner_team_id,
        visibility=scope.visibility,
        account_id=_ctx_user_id_val(),
        org_role=_ctx_role_val() or "",
        team_key_id=None,  # keys were bounded above with MCP's row-independent semantics
    )
    if denial is not None:
        return scope.owner_team_id, {
            "error": "team_boundary_violation",
            "detail": _MSG_USER_NOT_TEAM_MEMBER,
        }
    return scope.owner_team_id, None


# PRD §7.18: MCP trigger_pipeline is limited to 60 calls/min per client. All
# MCP tools share the /mcp HTTP path, so the middleware can't differentiate
# this tool (it is capped by the general 200/min rule); the 60/min limit is
# enforced here at the application level with a per-client in-memory bucket.
_TRIGGER_PIPELINE_RATE = 60 / 60.0  # 60 tokens per 60s window
_TRIGGER_PIPELINE_BURST = 60

_trigger_pipeline_limiter = TokenBucketRegistry(
    rate=_TRIGGER_PIPELINE_RATE,
    burst=_TRIGGER_PIPELINE_BURST,
)


def _trigger_pipeline_client_key() -> str:
    """Derive the per-client key for the trigger_pipeline rate limit.

    API-key calls are keyed by org + key id — EXCEPT user-scoped keys
    (FAR-620, key_scope='user'), which bucket as ``user:{account_id}`` like
    the identity-bound OAuth/JWT callers DO: one user-scoped key is one client,
    not a bucket the org's whole key population shares. This is a DELIBERATE
    divergence from the middleware ``_client_key`` identity (which keys every
    API key, org or user, by key id) — documented here, not mirrored.
    Org-wide + team-scoped + run-scoped keys hold ``ak:{key_id}`` buckets; the
    org-key multiplication hole (each org key its own bucket) is pre-existing
    and accepted. Distinct callers never share a bucket.
    """
    org = _ctx_org_id.get(None)
    org_s = str(org) if org is not None else "unknown"
    auth_type = _ctx_auth_type.get(None) or "unknown"
    if auth_type == "api_key":
        if _ctx_key_scope.get(None) == "user":
            # FAR-620: a user-scoped key acts as its creator — rate it per
            # account, mirroring the OAuth/JWT identity bucket.
            uid = _ctx_user_id.get(None)
            client = f"user:{uid}" if uid is not None else _CLIENT_USER_UNKNOWN
        else:
            key_id = _ctx_key_id.get(None)
            client = f"ak:{key_id}" if key_id is not None else "ak:unknown"
    else:
        uid = _ctx_user_id.get(None)
        client = f"user:{uid}" if uid is not None else _CLIENT_USER_UNKNOWN
    return f"trigger_pipeline:{org_s}:{auth_type}:{client}"


# FAR-748: per-key HITL decision budget on the MCP transport. HITL decision
# actions (approve / deliver_manual) ride the general 200/min ``/mcp`` rule;
# the 2026-09-05 sweep's actual vector is alarmed post-hoc (FAR-611) but was
# never throttled on the transport itself. This bucket caps DECISION actions
# at 20/min per key, mirroring the trigger_pipeline bucket's process-local
# floor precedent: per-process (not cross-worker), so a multi-replica
# deployment gets up to a 20x aggregated ceiling — availability, not
# enforcement, is the documented posture, and the FAR-611 sweep alarm remains
# the detective control. Rejections are deliberately outside the budget
# (they do NOT let a run continue; an approve or manual delivery does).
_HITL_DECISION_RATE = 20 / 60.0  # 20 decisions per 60s window
_HITL_DECISION_BURST = 20

_hitl_decision_limiter = TokenBucketRegistry(
    rate=_HITL_DECISION_RATE,
    burst=_HITL_DECISION_BURST,
)


def _hitl_decision_client_key() -> str:
    """Derive the per-key bucket for the HITL decision budget.

    Same derivation contract as :func:`_trigger_pipeline_client_key` (FAR-620:
    a user-scoped key acts as its creator — one user-scoped key is one client,
    org-wide/team/run-scoped keys hold ``ak:{key_id}`` buckets); decisions are
    attributed to the caller's account, so OAuth/JWT and user-scoped keys share
    the identity ``user:{account_id}`` bucket the attribution follows.
    """
    org = _ctx_org_id.get(None)
    org_s = str(org) if org is not None else "unknown"
    auth_type = _ctx_auth_type.get(None) or "unknown"
    if auth_type == "api_key":
        if _ctx_key_scope.get(None) == "user":
            uid = _ctx_user_id.get(None)
            client = f"user:{uid}" if uid is not None else _CLIENT_USER_UNKNOWN
        else:
            key_id = _ctx_key_id.get(None)
            client = f"ak:{key_id}" if key_id is not None else "ak:unknown"
    else:
        uid = _ctx_user_id.get(None)
        client = f"user:{uid}" if uid is not None else _CLIENT_USER_UNKNOWN
    return f"hitl_decision:{org_s}:{auth_type}:{client}"


async def _hitl_decision_budget_allowed() -> bool:
    """Consume one token from the caller's HITL decision bucket.

    Returns False once the caller exceeds 20 decisions/min (rate ~=0.33/s,
    burst=20). FAIL-OPEN by design (availability over enforcement, the same
    posture as the REST limiter's registry-failure behaviour): a limiter
    defect must never block the human's decision — the FAR-611 sweep alarm
    is the detective control that still fires regardless.
    """
    try:
        return await _hitl_decision_limiter.consume(_hitl_decision_client_key())
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("hitl_decision.budget_check_failed")
        return True


def _hitl_decision_rate_limited_response() -> dict[str, Any]:
    return {
        "error": "rate_limited",
        "detail": "Rate limit exceeded for HITL decisions (20/min)",
    }


async def _trigger_pipeline_rate_allowed() -> bool:
    """Consume one token from the caller's trigger_pipeline bucket.

    Returns False once the caller exceeds 60 calls/min (rate=1.0, burst=60).
    """
    return await _trigger_pipeline_limiter.consume(_trigger_pipeline_client_key())


# API-key role-cap degradation counter (ADR 047 DECISION 4): increments every
# time a live-role clamp demotes a key (or an owner removal kills one), so mass
# key-degradation is visible in logs and metrics. Module-level + lock, mirroring
# the CatchAllMiddleware counter pattern.
_api_key_role_cap_count: int = 0
_api_key_role_cap_lock = threading.Lock()


def _record_api_key_role_cap(
    *,
    minted_role: str,
    effective_role: str,
    org_id: uuid.UUID,
    degraded: bool,
    key_id: uuid.UUID | None = None,
) -> None:
    """Log + count an API-key role-cap clamp (degrade or deny-on-removal).

    ``degraded=True`` when the effective role is lower than the minted role
    (demoted operator); ``degraded=False`` with ``effective_role=""`` when the
    owner was removed/deactivated (key dies).
    """
    global _api_key_role_cap_count
    with _api_key_role_cap_lock:
        _api_key_role_cap_count += 1
    _log.warning(
        _CODE_PERMISSION_API_KEY_ROLE_CAP,
        extra={
            "minted_role": minted_role,
            "effective_role": effective_role,
            "org_id": str(org_id),
            "key_id": str(key_id) if key_id else None,
            "degraded": degraded,
            "total_caps": _api_key_role_cap_count,
        },
    )


def get_api_key_role_cap_count() -> int:
    """Return the total number of API-key role-cap clamps recorded."""
    with _api_key_role_cap_lock:
        return _api_key_role_cap_count


async def _set_authz_enforce(org_id: uuid.UUID) -> None:
    """Resolve the per-org authz-enforce kill-switch flag into the ContextVar.

    ``check_tool_scope`` reads it via ``assert_org_role``. Fail-closed: on any
    read error the flag defaults to enforcement ON (True) and the failure is
    logged under the structured kill-switch key. ADR 047 DECISION 3.
    """
    try:
        async with _session(org_id) as s:
            enforce = await resolve_authz_enforce(s, org_id)
    except Exception:
        _log.warning("permission.kill_switch_read_failed", exc_info=True)
        enforce = True
    set_authz_enforce(enforce)


# FAR-620: the org-level feature flag gating user-scoped MCP keys.
_FLAG_USER_SCOPED_MCP_KEYS = "user_scoped_mcp_keys"


async def _set_user_keys_flag(org_id: uuid.UUID) -> bool:
    """Read the per-org ``user_scoped_mcp_keys`` flag ONCE for this request.

    Stores the resolved value in the request-scoped ContextVar (no caching
    across requests — a mid-flight flag flip applies to the next request).
    Fail-closed: any read error is treated as OFF so a broken flag resolution
    never broadens access. Org-key requests never consult the flag at all
    (byte-identical org-key behaviour, no extra read); this helper runs only
    on the user-scoped-key mint/auth paths.
    """
    try:
        enabled = bool(await get_registry().resolve_flag(_FLAG_USER_SCOPED_MCP_KEYS, org_id=org_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning("feature_flag.user_scoped_mcp_keys_read_failed", exc_info=True)
        enabled = False
    _ctx_user_keys_enabled.set(enabled)
    return enabled


# ---------------------------------------------------------------------------
# FAR-1283 — the ``mcp_server`` plan feature is the operator kill switch for the
# whole MCP capability. It CANNOT be enforced with ``require_feature``: the /mcp
# sub-app is a plain Starlette app (no FastAPI dependencies), and the OAuth
# protocol endpoints are reachable BEFORE a session exists, where the
# authenticated ``get_plan_context`` chain would raise 401 "Not authenticated"
# and make the OAuth flow unreachable. The flag is therefore resolved here
# against an org the caller has already proved — the authenticated request's
# ContextVar org, or the OAuth client's own row — never through a dependency.
# ---------------------------------------------------------------------------
_FLAG_MCP_SERVER = "mcp_server"
# Same status as ProblemType.FEATURE_REQUIRED (what ``require_feature`` raises
# on the REST surface), in this surface's own ``{"error", "detail"}`` envelope.
_MCP_FEATURE_UNAVAILABLE_STATUS = 402


async def _mcp_server_flag_enabled(org_id: uuid.UUID, session: AsyncSession) -> bool:
    """True when the ``mcp_server`` plan feature is enabled for ``org_id``.

    Pre-auth safe: the org is derived from a credential the caller already
    presented (an authenticated request's ContextVar org, or the OAuth client's
    own row), so this never needs an authenticated dependency chain and can
    never turn a pre-auth caller into a 401. Resolution mirrors
    ``get_plan_context``'s org leg, so the pre-auth and authenticated surfaces
    judge the org by the same plan.

    Fails CLOSED: a resolution error is treated as OFF, because a broken flag
    read must never widen access to a capability an operator switched off.
    """
    from farna.db.crud.organisation import get_organisation

    try:
        org = await get_organisation(session, org_id)
        plan_ctx = await resolve_plan_context(get_settings(), session, org)
        return bool(plan_ctx.feature_enabled(_FLAG_MCP_SERVER))
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "feature_flag.mcp_server_read_failed",
            exc_info=True,
            extra={"org_id": str(org_id), "feature": _FLAG_MCP_SERVER},
        )
        return False


def _mcp_feature_unavailable_response() -> JSONResponse:
    """The MCP surface's structured feature-unavailable response (402)."""
    return JSONResponse(
        {"error": "feature_required", "detail": f"{_FLAG_MCP_SERVER} is not available on your plan"},
        status_code=_MCP_FEATURE_UNAVAILABLE_STATUS,
    )


async def _mcp_server_feature_gate(org_id: uuid.UUID) -> Response | None:
    """Return the feature-unavailable response when the flag is off, else None."""
    async with _session(org_id) as s:
        if await _mcp_server_flag_enabled(org_id, s):
            return None
    return _mcp_feature_unavailable_response()


async def _call_next_if_feature_enabled(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Continue an AUTHENTICATED MCP request unless ``mcp_server`` is off.

    Single chokepoint for every authenticated credential class (API key, regular
    JWT, OAuth token): the middleware resolves ``_ctx_org_id`` before continuing
    in all three, so this is one check rather than three per-credential copies.
    An unset org is not an enforcement hole — every tool handler resolves its
    tenant from the same ContextVars and fails closed without one, so such a
    request can never reach data.
    """
    org_id = _ctx_org_id.get(None)
    if org_id is None:
        return await call_next(request)
    denied = await _mcp_server_feature_gate(org_id)
    if denied is not None:
        return denied
    return await call_next(request)


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the process-global session factory, sharing the engine from dependencies.py."""
    settings = get_settings()
    return get_or_create_session_factory(get_or_create_engine(settings))


@asynccontextmanager
async def _session(org_id: uuid.UUID) -> AsyncGenerator[AsyncSession, None]:
    factory = _get_session_factory()
    async with factory() as s, s.begin():
        await set_rls_org(s, org_id)
        try:
            uid = _ctx_user_id_val()
            role = _ctx_role_val() or ""
            await set_rls_user_context(s, uid, role)
        except (LookupError, ValueError):
            _log.warning("mcp.session_user_context_failed", exc_info=True)
        yield s


@asynccontextmanager
async def _system_maps_discovery_session() -> AsyncGenerator[AsyncSession, None]:
    """Fresh session for the reused REST System Maps discovery handler.

    The handler owns its transaction (``async with session.begin():``) and
    binds the RLS org/user context itself, so this session must NOT be opened
    inside ``_session``'s transaction — a second ``begin()`` would raise.

    This stays in the spine even though the System Maps discovery TOOL lives in
    ``mcp_tools/admin.py``: the suite patches
    ``farna.api.mcp_server._system_maps_discovery_session`` and the tool resolves
    it from this module at call time (through ``mcp_tools._patch_surface``), so
    the interception stays live.
    """
    async with _get_session_factory()() as session:
        yield session


_doc_index: DocumentationIndex | None = None
_doc_index_ts: float = 0.0
_doc_index_ttl: float = 300.0  # 5 minutes


def _get_doc_index() -> DocumentationIndex:
    """Build (and cache) the product-documentation index.

    Stays in the spine even though its consumer (``search_documentation``)
    moved to ``mcp_tools/library.py``: the suite patches both this function and
    its ``DocumentationIndex`` class here, and drives the cache attributes
    directly (``mcp_server._doc_index``), so the whole unit resolves from this
    module's namespace.
    """
    global _doc_index, _doc_index_ts
    import time as _time

    now = _time.time()
    if _doc_index is None or (now - _doc_index_ts) > _doc_index_ttl:
        _doc_index = DocumentationIndex.build()
        _doc_index_ts = now
    return _doc_index


# ---------------------------------------------------------------------------
# Per-event auth validation
# ---------------------------------------------------------------------------

# TTL-bounded live-role cache for SSE per-event revalidation (ADR 047): at most
# one org_memberships read per connection per window, so a demoted admin loses
# scope mid-stream without a DB round-trip on every event.
_LIVE_ROLE_TTL_SECONDS = 15.0
_MAX_LIVE_ROLE_CACHE = 1024
_live_role_cache: dict[str, tuple[float, str | None]] = {}


def _evict_stale_live_role_cache(now: float) -> None:
    """Evict expired entries, then drop oldest few if still over capacity."""
    if len(_live_role_cache) < _MAX_LIVE_ROLE_CACHE:
        return
    for key in [k for k, v in _live_role_cache.items() if now - v[0] >= _LIVE_ROLE_TTL_SECONDS]:
        _live_role_cache.pop(key, None)
    overflow = len(_live_role_cache) - _MAX_LIVE_ROLE_CACHE + 1
    if overflow > 0:
        oldest = sorted(_live_role_cache.items(), key=lambda kv: kv[1][0])[:overflow]
        for key, _ in oldest:
            _live_role_cache.pop(key, None)


async def _revalidate_live_role(token: str, account_id: uuid.UUID, org_id: uuid.UUID) -> str | None:
    """TTL-bounded live-role re-read for a JWT principal (ADR 047).

    Returns the live org role, or None if the membership is missing/deactivated
    (removed user) or the read failed — the caller then denies (fail closed,
    matching the ``validate_current_auth`` posture). The cache is keyed by the
    connection's auth token, so it acts as a per-connection timestamp cache.
    """
    now = time.monotonic()
    cached = _live_role_cache.get(token)
    if cached is not None and now - cached[0] < _LIVE_ROLE_TTL_SECONDS:
        return cached[1]

    live_role: str | None = None
    try:
        async with _session(org_id) as s:
            live_role = await resolve_role_from_membership(
                s,
                str(account_id),
                str(org_id),
            )
    except SQLAlchemyError:
        _log.warning("permission.live_role_read_failed", exc_info=True)
        live_role = None

    _evict_stale_live_role_cache(now)
    _live_role_cache[token] = (now, live_role)
    return live_role


async def _validate_api_key_live(token: str, org_id: uuid.UUID) -> bool:
    """Re-validate an API-key credential and clamp its role against the live role."""
    async with _session(org_id) as s:
        key = await validate_api_key(s, token, org_id)
    # ADR 047 DECISION 4 — clamp on every per-event re-validation too.
    # The stored key.role is the minted role; the effective role is
    # min(minted, live), resolved TTL-bounded through the same cache
    # the JWT path uses (per-connection keyed by token).
    account_id = _ctx_user_id.get(None)
    if account_id is None:
        return False
    live_role = await _revalidate_live_role(token, account_id, org_id)
    if live_role is None:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role="",
            org_id=org_id,
            degraded=False,
            key_id=key.id,
        )
        return False
    clamped = _clamp_role(key.role, live_role)
    if not clamped:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role="",
            org_id=org_id,
            degraded=False,
            key_id=key.id,
        )
        return False
    if clamped != key.role:
        _record_api_key_role_cap(
            minted_role=key.role,
            effective_role=clamped,
            org_id=org_id,
            degraded=True,
            key_id=key.id,
        )
    # FAR-1477: re-run the grant resolver on every re-validation so flag-OFF
    # (or a malformed value) kills a grant-bearing key on a long-lived
    # connection, and the cached grant-set is refreshed. ApiKeyInvalidError /
    # ApiKeyGrantsUnavailableError propagate -> validate_current_auth denies.
    key_grants = await resolve_key_grants(key)
    _ctx_key_grants.set(key_grants)
    _ctx_role.set(clamped)
    _ctx_team_id.set(key.team_id)
    return True


async def _validate_principal_live(token: str, principal: Any) -> bool:
    """Re-validate a regular JWT principal's live org role."""
    if principal.organisation_id is None:
        return False
    live_role = await _revalidate_live_role(
        token,
        principal.account_id,
        principal.organisation_id,
    )
    if live_role is None:
        return False
    _ctx_role.set(live_role)
    _ctx_team_id.set(None)  # user tokens carry no team boundary
    return True


async def _validate_oauth_live(token: str) -> bool:
    """Re-validate an OAuth access token credential against its token family."""
    settings = get_settings()
    try:
        claims = decode_oauth_access_token(token, settings.secret_key)
    except JWTError:
        # Regular JWT (used by Assistant) — skip OAuth token family check
        try:
            from farna.auth.jwt import decode_principal

            principal = decode_principal(token, settings.secret_key)
        except JWTError:
            return False
        return await _validate_principal_live(token, principal)
    client_team_id: uuid.UUID | None = None
    async with _session(claims.organisation_id) as s:
        if not await check_oauth_token_family_valid(
            s,
            family_id=claims.token_family,
            client_id=claims.client_id,
            org_id=claims.organisation_id,
        ):
            return False
        # FAR-1476: resolve the client's team boundary in the SAME round-trip
        # the family check already makes (the client row is not otherwise loaded
        # on this leg). A team-bound client re-applies its boundary on every
        # SSE event; an org-wide client resolves None (no boundary).
        client_team_id = await get_oauth_client_team_id(s, claims.client_id)
    # ADR 047: re-resolve the account's LIVE role (TTL-bounded per
    # connection) and re-apply the scope→live clamp so a demoted
    # operator loses scope mid-stream too.
    live_role = await _revalidate_live_role(
        token,
        claims.account_id,
        claims.organisation_id,
    )
    if live_role is None:
        return False
    _ctx_role.set(clamp_oauth_role(scopes_required_role(claims.scopes), live_role))
    # FAR-1476: re-derive the consented grant-set too — a long-lived
    # connection must keep evaluating the token's permission set (and the
    # live non-delegable exclusion inside ``grants_permit``), not a stale
    # snapshot from auth time.
    _ctx_key_grants.set(oauth_grant_set(claims.scopes))
    _ctx_team_id.set(client_team_id)  # FAR-1476: client's team boundary (None = org-wide)
    return True


async def validate_current_auth() -> bool:
    """Re-validate the current auth credential for per-event SSE enforcement.

    Checks the stored credential against the DB/issuer to detect mid-session
    revocation, expiry, or OAuth token family blacklisting. For JWT principals
    the LIVE org role is re-resolved (TTL-bounded) and ``_ctx_role`` is re-set
    so a demoted admin loses scope mid-stream (ADR 047).
    Returns True if the credential is still valid, False otherwise.

    Fail closed: the credential and org come exclusively from the
    request-scoped ContextVars set by ``McpAuthMiddleware``. If any of them
    is missing, the request is treated as unauthenticated — there is no
    process-global fallback.
    """
    auth_type = _ctx_auth_type.get(None)
    token = _ctx_auth_token.get(None)
    org_id = _ctx_org_id.get(None)

    if auth_type is None:
        auth_type = "api_key" if token and token.startswith("mk_") else None

    if auth_type is None or token is None or org_id is None:
        return False

    try:
        if auth_type == "api_key":
            return await _validate_api_key_live(token, org_id)
        if auth_type == "oauth":
            return await _validate_oauth_live(token)
        if auth_type == "jwt":
            # FAR-620 CRITICAL CO-CHANGE: regular JWT (Assistant) sessions now carry
            # auth_type 'jwt' (previously conflated with 'oauth'). Route them
            # through the SAME live-principal revalidation that
            # ``_validate_oauth_live``'s JWT fallback performs — decode the
            # principal and re-resolve the live org role so a demoted or
            # removed member loses scope mid-stream. Without this branch every
            # MCP tool call from a JWT caller would fail token-revoked.
            try:
                from farna.auth.jwt import decode_principal

                principal = decode_principal(token, get_settings().secret_key)
            except JWTError:
                return False
            return await _validate_principal_live(token, principal)
        return False
    except (ApiKeyInvalidError, JWTError):
        return False
    except ApiKeyGrantsUnavailableError:
        # FAR-1477: grant flag unreadable -> retryable fail-closed denial; a
        # transient read failure is expected noise, not a traceback.
        _log.warning("mcp.validate_current_auth_grants_unavailable")
        return False
    except Exception:
        _log.exception("validate_current_auth failed")
        return False


def _resource_metadata_challenge(request: Request) -> dict[str, str]:
    """``WWW-Authenticate`` challenge pointing at the RFC 9728 metadata.

    A stock MCP harness (Claude Code 2.1.290 observed) learns where to start
    OAuth from this header on the MCP ``401``: without it the client has no
    way to discover ``/.well-known/oauth-protected-resource`` and falls back
    to a manual override, which is exactly the friction FAR-1476 removes.

    ONLY unauthenticated ``401`` responses carry it. ``403``/policy denials
    come from an authenticated principal that is being told "not allowed" —
    inviting it to re-authenticate would be wrong — so they never get it.
    """
    return {
        "WWW-Authenticate": f'Bearer resource_metadata="{protected_resource_metadata_url(request)}"',
    }


def _with_resource_metadata(response: Response, request: Request) -> Response:
    """Attach the RFC 9728 challenge to a ``401``; leave any other status alone.

    The token-family failures are built by ``_verify_oauth_token_family``,
    which is deliberately request-less (it documents that it is called
    without a request object), so it cannot build the absolute
    ``resource_metadata`` URL itself. The header is attached here instead, at
    the middleware boundary where ``request`` is in scope. ``403``/policy
    denials and ``5xx`` pass through untouched — only an *unauthenticated*
    ``401`` should invite the client to fetch OAuth metadata — and the
    ``WWW-Authenticate`` presence check keeps the operation idempotent should
    the helper ever set it itself.
    """
    if response.status_code == 401 and "WWW-Authenticate" not in response.headers:
        response.headers.update(_resource_metadata_challenge(request))
    return response


def _extract_bearer_token(request: Request) -> tuple[str | None, Response | None]:
    """Extract the Bearer token from the Authorization header.

    Returns ``(token, None)`` on success or ``(None, error_response)`` when the
    header is missing or not a Bearer token. The ``401`` carries the RFC 9728
    ``WWW-Authenticate`` challenge so an unauthenticated client can discover
    the OAuth metadata it needs to connect.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, Response(
            '{"error":"unauthorized","detail":"Bearer token required"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
            headers=_resource_metadata_challenge(request),
        )
    token = auth_header[len("Bearer ") :].strip()
    return token, None


def _extract_node_id_from_key_name(key_name: str | None) -> str | None:
    """Parse a per-node sandbox key ``name`` for its ``node_id`` (FAR-436).

    ``mint_run_api_key`` names run-scoped keys ``run:<run_id>:node:<node_id>``.
    Returns ``None`` when the name is not a run-scoped sandbox key (a normal
    user/org API key), so those callers are never narrowed.
    """
    if not key_name:
        return None
    marker = ":node:"
    idx = key_name.rfind(marker)
    if idx < 0:
        return None
    node_id = key_name[idx + len(marker) :].strip()
    return node_id or None


async def _node_allowed_tools_for_key(*, org_id: uuid.UUID, run_id: uuid.UUID | None, key_name: str) -> list[str] | None:
    """Resolve a run-scoped key's node-level ``capability_scope.allowed_tools`` (FAR-436).

    A run-scoped sandbox key (``run_id`` set) is minted per sandbox_agent NODE,
    so the node's snapshot ``capability_scope`` narrows the MCP tools that
    node's agent may call. Returns ``None`` for a non-run key or a node with no
    ``capability_scope`` (unrestricted — the pre-scope default); an explicit
    empty allow-list is returned untouched (deny-by-default). Any resolution
    failure returns ``None`` (unrestricted) with a log — a scope misread must
    never break the agent's MCP stream, and it cannot widen beyond the Agent's
    grants either (the role check still runs).
    """
    if run_id is None:
        return None
    node_id = _extract_node_id_from_key_name(key_name)
    if node_id is None:
        return None
    from sqlalchemy import select

    from farna.db.models.pipeline_snapshot import PipelineSnapshot

    try:
        async with _session(org_id) as s:
            snapshot_id = (
                await s.execute(
                    select(Run.snapshot_id).where(
                        Run.id == run_id,
                        Run.organisation_id == org_id,
                    )
                )
            ).scalar_one_or_none()
            if snapshot_id is None:
                return None
            snapshot = await s.get(PipelineSnapshot, snapshot_id)
            if snapshot is None:
                return None
            for node in snapshot.graph_json.get("nodes", []):
                if str(node.get("id")) == node_id:
                    scope = node.get("capability_scope") or {}
                    allowed = scope.get("allowed_tools")
                    if isinstance(allowed, list):
                        return [str(t) for t in allowed]
                    return None
            return None
    except (SQLAlchemyError, TimeoutError):
        _log.exception(
            "mcp.scope.node_allowed_tools_resolve_failed",
            extra={"run_id": str(run_id), "node_id": node_id},
        )
        return None


async def _resolve_api_key_org(prefix: str) -> uuid.UUID | None:
    """Resolve the organisation owning an ``mk_`` key prefix (RLS-safe).

    org_api_keys has RLS enabled (migration 0005, _STRICT_RLS) and the key's
    org is unknown until the record is read — a plain lookup in an empty org
    context would be filtered out by RLS and reject every valid key. On
    Postgres the runtime app role is RLS-subject (a non-owner DML-granted
    role), so the org is resolved through a SECURITY DEFINER function owned
    by the migration role rather than SET row_security TO OFF (which only
    bypasses RLS for owners and raises for a regular role). On generic
    backends there is no RLS, so a plain prefix scan works.
    """
    from sqlalchemy import select, text

    from farna.db.models.api_key import OrgApiKey
    from farna.db.rls import _ensure_active_transaction

    factory = _get_session_factory()
    async with factory() as s, s.begin():
        dialect = await _ensure_active_transaction(s)
        if dialect == "postgresql":
            return (
                await s.execute(
                    text("SELECT public.lookup_api_key_org(:prefix)"),
                    {"prefix": prefix},
                )
            ).scalar_one_or_none()
        key_record = (
            await s.execute(
                select(OrgApiKey).where(
                    OrgApiKey.lookup_prefix == prefix,
                    OrgApiKey.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return key_record.organisation_id if key_record is not None else None


async def _resolve_key_owner_kind(
    session: AsyncSession,
    key: Any,
    live_role: str | None,
) -> str | None:
    """Return a user-scoped key owner's live account kind, else ``None``.

    Only a user-scoped key needs the additional account-kind projection.
    The historical role read stays the clamp's sole query for org/team/run
    keys, preserving the existing auth path.
    """
    if getattr(key, "scope", None) != "user" or live_role is None:
        return None
    membership = await resolve_live_role_and_kind(
        session,
        str(key.account_id),
        str(key.organisation_id),
    )
    return membership[1] if membership is not None else None


async def _authenticate_api_key(
    request: Request,
    token: str,
) -> tuple[bool, Response | None]:
    """Authenticate an API-key bearer token (``mk_`` prefix).

    Sets the ``_ctx_*`` contextvars and ``request.scope["auth_principal"]`` on
    success. Returns ``(True, None)`` when the key authenticated (the caller
    should proceed to ``call_next``) or ``(False, error_response)`` on failure.
    """
    try:
        from farna.auth.api_key import _MK_PREFIX, _PREFIX_LEN

        prefix = token[len(_MK_PREFIX) :][:_PREFIX_LEN]
        # Resolve the org first, then re-validate inside the org's RLS context
        # before trusting the key.
        org_id = await _resolve_api_key_org(prefix)
        if org_id is None:
            raise ApiKeyInvalidError

        # FAR-1417 (auth-failure arm, mcp_server.py `_authenticate_api_key`):
        # the org was resolved by the lookup above, so bind BEFORE the
        # re-validation below — a SQLAlchemyError/TimeoutError from
        # validate_api_key, the live-role read, or the user-scoped-flag read
        # then reaches `_log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)` with the
        # org in scope. A failure INSIDE the lookup itself stays unbound on
        # purpose: that operation is what determines the org, so there is
        # nothing real to attribute it to (announced drop, never a guess).
        _bind_org_context(request, org_id)

        # Now re-validate within the correct RLS context.
        async with _session(org_id) as s:
            key = await validate_api_key(s, token, org_id=org_id)
            # ADR 047 DECISION 4 — live role-cap on EVERY MCP call. The
            # stored key.role is the minted role; the effective role is
            # min(minted, live). A demoted operator's key degrades to
            # the live role on the next call (never persisted — the ORM
            # flushes last_used_at, so mutating key.role here would
            # permanently store the demotion). An owner removed from
            # the org (no live membership) makes the key die (401).
            live_role = await resolve_role_from_membership(
                s,
                str(key.account_id),
                str(key.organisation_id),
            )
            owner_kind = await _resolve_key_owner_kind(s, key, live_role)
            clamped = _clamp_role(key.role, live_role)
            if not clamped:
                _record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role="",
                    org_id=key.organisation_id,
                    degraded=False,
                    key_id=key.id,
                )
                raise ApiKeyInvalidError
            if clamped != key.role:
                _record_api_key_role_cap(
                    minted_role=key.role,
                    effective_role=clamped,
                    org_id=key.organisation_id,
                    degraded=True,
                    key_id=key.id,
                )
        # FAR-1477: grant-set (None = legacy, no flag read; flag OFF on a
        # grant-bearing key raises ApiKeyInvalidError -> 401). Resolved AFTER
        # the validation session closes: the flag read opens its own pooled
        # session for grant-bearing keys, and nesting two sessions per request
        # risks pool-exhaustion deadlock.
        key_grants = await resolve_key_grants(key)
        org_id = key.organisation_id
        # FAR-620: the ``user_scoped_mcp_keys`` flag is the kill switch for
        # user-scoped keys — disabling it DENIES the key at auth (401),
        # revoking rather than broadening. The flag is read ONCE per request,
        # fail-closed, and ONLY for user-scoped keys: org-key authentication
        # is byte-identical to the pre-FAR-620 behaviour (no flag read). The
        # deny decision reads the request-scoped ContextVar (not the helper's
        # inline return) so the var - shared with the mint path - is the
        # single source of the flag's per-request state.
        if key.scope == "user":
            await _set_user_keys_flag(org_id)
            if not _ctx_user_keys_enabled.get():
                _log.info(
                    "api_key.user_scoped_key_denied_flag_off",
                    extra={"key_id": str(key.id), "org_id": str(org_id)},
                )
                raise ApiKeyInvalidError
        _ctx_org_id.set(org_id)
        _bind_org_context(request, org_id, key.account_id)
        _ctx_role.set(clamped)
        _ctx_key_id.set(key.id)
        _ctx_team_id.set(key.team_id)
        _ctx_user_id.set(key.account_id)
        _ctx_auth_token.set(token)
        _ctx_auth_type.set("api_key")
        # ADR 063 step 1: an API key carries no signed token family. The family
        # is the observed OAuth half of human-linkage, so it stays None (fail
        # closed); a user-scoped key can still be human-linked through its
        # server-derived key_scope + live human owner (FAR-2281).
        _ctx_token_family.set(None)
        # FAR-620: the credential's caller scope drives the tool-scope
        # caller-scope leg. The isinstance guard keeps test doubles (MagicMock
        # rows) on the fail-closed None; real rows always carry a string.
        _ctx_key_scope.set(key.scope if isinstance(key.scope, str) else None)
        # FAR-2281: a USER-scoped key owned by a live human acts as that human,
        # so the audit record attributes the action to the human rather than a
        # generic api_key principal. Org/team/run-scoped keys stay unattributed.
        _ctx_principal_kind.set(KIND_HUMAN if _ctx_key_scope.get() == "user" and owner_kind == KIND_HUMAN else None)
        _ctx_key_grants.set(key_grants)
        # FAR-696: an API-key principal is never a system admin — REST
        # constructs API-key tenants with ``is_system_admin=False`` and the
        # strict ``require_system_permission`` gates deny them; mirror that.
        _ctx_is_system_admin.set(False)
        # FAR-436: a run-scoped sandbox key narrows the agent's MCP tool-call
        # loop to the node's capability_scope.allowed_tools (deny-by-default
        # within the scope). Non-run keys / scoped-less nodes resolve to None
        # (unrestricted — legacy behaviour). Empty allow-list = deny-all.
        _ctx_node_allowed_tools.set(
            await _node_allowed_tools_for_key(
                org_id=org_id,
                run_id=key.run_id,
                key_name=key.name,
            )
        )
        request.scope["auth_principal"] = {
            "type": "api_key",
            "org_id": str(org_id),
            "prefix": token[3:11],
        }
    except ApiKeyInvalidError:
        return False, Response(
            '{"error":"unauthorized","detail":"Invalid or revoked API key"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
            headers=_resource_metadata_challenge(request),
        )
    except ApiKeyGrantsUnavailableError:
        # FAR-1477: grant flag unreadable -> fail closed as 503 (retryable), not 401.
        _log.warning("mcp.api_key_grants_unavailable")
        return False, Response(
            '{"error":"service_unavailable","detail":"API key grants temporarily unavailable"}',
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    except (SQLAlchemyError, TimeoutError) as exc:
        raise_session_contract_error(exc, "mcp_server._authenticate_api_key")
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return False, Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    return True, None


async def _authenticate_oauth_jwt(
    request: Request,
    token: str,
    settings: Any,
) -> tuple[bool, Response | None, Any]:
    """Authenticate an OAuth access token (JWT), falling back to a regular JWT.

    Returns ``(handled, error_response, claims)``:

    * ``(True, None, None)`` — a regular JWT (Assistant) fully authenticated; the
      caller should ``call_next`` and return.
    * ``(False, None, claims)`` — an OAuth access token decoded successfully;
      the caller continues to the token-family check with ``claims``.
    * ``(False, error_response, None)`` — authentication failed.

    Sets the ``_ctx_*`` contextvars and ``request.scope["auth_principal"]`` for
    the regular-JWT fallback path. The OAuth claims path defers context-setting
    until after the token-family and scope checks (in the caller).
    """
    try:
        claims = decode_oauth_access_token(token, settings.secret_key)
    except JWTError:
        # Fall back to regular JWT access token (used by Assistant MCP tool calls).
        try:
            from farna.auth.jwt import decode_principal

            principal = decode_principal(token, settings.secret_key)
        except JWTError:
            return (
                False,
                Response(
                    '{"error":"unauthorized","detail":"Invalid or expired access token"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                    headers=_resource_metadata_challenge(request),
                ),
                None,
            )
        if principal.organisation_id is None:
            return (
                False,
                Response(
                    _JSON_FORBIDDEN_ORG_MEMBERSHIP,
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        # ADR 047: no claim-less default-up. A None role claim fails closed,
        # and the LIVE role is re-read from org_memberships so a demoted or
        # removed member loses access on the very next request.
        if principal.org_role is None:
            return (
                False,
                Response(
                    '{"error":"forbidden","detail":"No org role claim on token"}',
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        # FAR-1417 (auth-failure arm, `_authenticate_oauth_jwt` fallback): the
        # org comes from the locally decoded token claim (None ruled out above),
        # so bind BEFORE the live-role read — its DB failure is then logged
        # with the org in scope instead of being dropped.
        _bind_org_context(request, principal.organisation_id, principal.account_id)
        try:
            async with _session(principal.organisation_id) as s:
                live_role = await resolve_role_from_membership(
                    s,
                    str(principal.account_id),
                    str(principal.organisation_id),
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise_session_contract_error(exc, "mcp_server._authenticate_oauth_jwt")
            _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
            return (
                False,
                Response(
                    _JSON_AUTH_DB_UNAVAILABLE,
                    status_code=503,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        if live_role is None:
            return (
                False,
                Response(
                    _JSON_FORBIDDEN_ORG_MEMBERSHIP,
                    status_code=403,
                    media_type=_CT_APPLICATION_JSON,
                ),
                None,
            )
        _ctx_org_id.set(principal.organisation_id)
        _bind_org_context(request, principal.organisation_id, principal.account_id)
        _ctx_role.set(live_role)
        _ctx_key_id.set(uuid.UUID(int=0))
        _ctx_user_id.set(principal.account_id)
        _ctx_auth_token.set(token)
        # FAR-620: a regular JWT (Assistant) session is the USER's own identity —
        # auth_type 'jwt' (distinct from OAuth tokens) and caller scope
        # 'user' (identity-bound, eligible for caller-scoped tools).
        _ctx_auth_type.set("jwt")
        # ADR 063 step 1: a non-OAuth JWT is structurally excluded from
        # human-linkage (including a browser-class password JWT — FAR-634).
        _ctx_token_family.set(None)
        _ctx_key_scope.set("user")
        _ctx_key_grants.set(None)
        _ctx_team_id.set(None)  # user tokens carry no team boundary
        # FAR-696: lift the principal's system-admin flag so system-admin-only
        # tools can enforce the same strict gate REST's
        # ``require_system_permission`` applies (fail-closed False elsewhere).
        _ctx_is_system_admin.set(bool(principal.is_system_admin))
        request.scope["auth_principal"] = {
            "type": "user",
            "org_id": str(principal.organisation_id) if principal.organisation_id else "",
            "user_id": str(principal.account_id) if principal.account_id else "",
        }
        return True, None, None
    return False, None, claims


async def _verify_oauth_token_family(
    _token: str,
    claims: Any,
) -> Response | None:
    """Return an error response if the OAuth token family is blacklisted.

    Returns ``None`` when the family is valid (the caller should continue), or
    the appropriate error ``Response`` otherwise.
    """
    # FAR-1417 (auth-failure arm, `_verify_oauth_token_family`): the org is on
    # the already-decoded OAuth claims — bind BEFORE the family check so a DB
    # failure (and the generic `except Exception` arm below it) is attributed.
    # This helper is called without a request object, so only the contextvar
    # carrier applies here.
    _bind_org_context(None, claims.organisation_id)
    try:
        async with _session(claims.organisation_id) as s:
            valid = await check_oauth_token_family_valid(
                s,
                family_id=claims.token_family,
                client_id=claims.client_id,
                org_id=claims.organisation_id,
            )
            if not valid:
                return Response(
                    '{"error":"unauthorized","detail":"Token family revoked"}',
                    status_code=401,
                    media_type=_CT_APPLICATION_JSON,
                )
    except (SQLAlchemyError, TimeoutError) as exc:
        raise_session_contract_error(exc, "mcp_server._verify_oauth_token_family")
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    except Exception:
        _log.exception("OAuth token family check failed")
        return Response(
            '{"error":"unauthorized","detail":"Token validation failed"}',
            status_code=401,
            media_type=_CT_APPLICATION_JSON,
        )
    return None


async def _dispatch_unauth_paths(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response | None:
    """Return the response when the path is served unauthenticated, else None.

    These endpoints manage their own auth (health checks, and the OAuth protocol
    endpoints which authenticate via client_id + client_secret).
    """
    clean = request.url.path.rstrip("/")
    if clean in ("/mcp/healthz", "/healthz"):
        resp: Response = await call_next(request)
        return resp
    if clean in ("/mcp/oauth/authorize", "/mcp/oauth/token", "/mcp/oauth/refresh"):
        resp2: Response = await call_next(request)
        return resp2
    return None


async def _finalize_oauth_principal(
    request: Request,
    token: str,
    claims: Any,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Resolve the OAuth principal role, set request context, and continue.

    Fail-closed: a DB read failure or missing/deactivated membership denies
    the request (ADR 047 — the scope grant is clamped to the account's live
    org role so a demoted operator loses scope on the next call).
    """
    # Resolve role from scopes (highest scope wins) — the scope grant is then
    # CLAMPED to the account's LIVE org role so a demoted operator loses scope
    # on the very next call. Fail-closed: a DB read failure or
    # missing/deactivated membership denies.
    scope_role = scopes_required_role(claims.scopes)
    # FAR-1417 (auth-failure arm, `_finalize_oauth_principal`): the org comes
    # from the decoded OAuth claims — bind BEFORE the live-role read so its DB
    # failure is attributed instead of dropped (the success-path bind below
    # re-binds the same org together with the account).
    _bind_org_context(request, claims.organisation_id, claims.account_id)
    client_team_id: uuid.UUID | None = None
    try:
        async with _session(claims.organisation_id) as s:
            live_role = await resolve_role_from_membership(
                s,
                str(claims.account_id),
                str(claims.organisation_id),
            )
            # FAR-1476: resolve the client's team boundary in the SAME round-trip
            # the live-role read already makes (the client row is not otherwise
            # loaded on this leg). A team-bound client scopes every token it was
            # issued to that team; an org-wide client resolves None (no boundary).
            client_team_id = await get_oauth_client_team_id(s, claims.client_id)
    except (SQLAlchemyError, TimeoutError) as exc:
        raise_session_contract_error(exc, "mcp_server._finalize_oauth_principal")
        _log.exception(_MSG_MCP_AUTH_DB_UNAVAILABLE)
        return Response(
            _JSON_AUTH_DB_UNAVAILABLE,
            status_code=503,
            media_type=_CT_APPLICATION_JSON,
        )
    if live_role is None:
        return Response(
            _JSON_FORBIDDEN_ORG_MEMBERSHIP,
            status_code=403,
            media_type=_CT_APPLICATION_JSON,
        )
    role = clamp_oauth_role(scope_role, live_role)

    _ctx_org_id.set(claims.organisation_id)
    _bind_org_context(request, claims.organisation_id, claims.account_id)
    _ctx_role.set(role)
    _ctx_key_id.set(uuid.UUID(int=0))  # sentinel for OAuth clients
    _ctx_user_id.set(claims.account_id)
    _ctx_auth_token.set(token)
    _ctx_auth_type.set("oauth")
    # ADR 063 step 1: the signed token family is the observed half of the
    # human-linkage check (condition 1). Cleared on every other auth path.
    _ctx_token_family.set(claims.token_family)
    # FAR-620: an OAuth token is the user's own identity — caller scope
    # 'user' (identity-bound, eligible for caller-scoped tools).
    _ctx_key_scope.set("user")
    # FAR-1476: the consented scope set IS the credential's grant-set, so the
    # MCP tool-access resolver's grant leg (``resolve_tool_access`` leg 5,
    # shared with API-key grant-sets) narrows every tool call to
    # ``consented set INTERSECT bundle(live role)``. Without this the widened
    # registry vocabulary would be unreachable-as-a-grant: the role leg alone
    # would let a single-scope token call every tool at or below its role.
    # ``grants_permit`` re-reads ``is_delegable`` live, so a token minted
    # before an exclusion tightened is denied at the very next call.
    _ctx_key_grants.set(oauth_grant_set(claims.scopes))
    _ctx_team_id.set(client_team_id)  # FAR-1476: client's team boundary (None = org-wide)
    # FAR-696: OAuth access-token claims do not carry ``is_system_admin`` —
    # fail closed (False), matching REST where such tokens are not system
    # admins either.
    _ctx_is_system_admin.set(False)
    request.scope["auth_principal"] = {
        "type": "user",
        "org_id": str(claims.organisation_id),
        "user_id": str(claims.account_id),
    }

    await _set_authz_enforce(claims.organisation_id)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class McpAuthMiddleware(BaseHTTPMiddleware):
    """Validate Bearer token on every MCP request.

    Supports two credential types (checked in order):
    1. API key bearer token (``mk_`` prefix)
    2. OAuth 2.0 access token (JWT with purpose=oauth_access)

    Once a credential resolves an org, ``_call_next_if_feature_enabled`` gates
    the request on the ``mcp_server`` plan feature (FAR-1283) — the health check
    and the three pre-auth OAuth protocol endpoints are exempt (they are served
    by ``_dispatch_unauth_paths`` and enforce the flag inside their handlers,
    against the resolved client's org).
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        unauth = await _dispatch_unauth_paths(request, call_next)
        if unauth is not None:
            return unauth

        # FAR-418: lift the node-level allowed_tools allow-list (if the calling
        # agent forwarded it) into the request-scoped ContextVar consumed by
        # check_tool_scope. Absent/empty header = UNRESTRICTED, preserving the
        # pre-scope behaviour for all non-node tool calls.
        raw_allowed = request.headers.get("X-Modulo-Allowed-Tools")
        if raw_allowed:
            set_request_allowed_tools([t.strip() for t in raw_allowed.split(",") if t.strip()])
        else:
            set_request_allowed_tools(None)

        # FAR-1283: every authenticated continuation below goes through the
        # mcp_server plan-feature gate. ``gated_next`` keeps the plain
        # ``Callable[[Request], Awaitable[Response]]`` shape every continuation
        # expects, so it can be handed to ``_finalize_oauth_principal`` (which
        # calls ``call_next(request)``) as well as called directly.
        async def gated_next(req: Request) -> Response:
            return await _call_next_if_feature_enabled(req, call_next)

        token, auth_err = _extract_bearer_token(request)
        if auth_err is not None:
            return auth_err
        if token is None:
            raise RuntimeError("McpAuthMiddleware.dispatch: token unresolved after successful extraction")

        # Try API key first (backwards compatible).
        if token.startswith("mk_"):
            handled, api_err = await _authenticate_api_key(request, token)
            if api_err is not None:
                return api_err
            if handled:
                await _set_authz_enforce(_ctx_org_id.get())
                return await gated_next(request)

        # Try OAuth access token (JWT).
        settings = get_settings()
        handled2, oauth_err, claims = await _authenticate_oauth_jwt(request, token, settings)
        if oauth_err is not None:
            return oauth_err
        if handled2:
            await _set_authz_enforce(_ctx_org_id.get())
            return await gated_next(request)

        # Verify token family is not blacklisted. That helper is deliberately
        # request-less, so the RFC 9728 challenge is attached here (every
        # other 401 below is built by a helper that has `request` in scope
        # and sets the header itself).
        family_err = await _verify_oauth_token_family(token, claims)
        if family_err is not None:
            return _with_resource_metadata(family_err, request)

        return await _finalize_oauth_principal(request, token, claims, gated_next)


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="Farna Control",
    instructions=("Farna Control is agent governance for your agentic SDLC. Use create_pipeline to define new pipelines, trigger_pipeline to fire runs, get_run_status to track them, get_run_output to inspect node outputs, and review_hitl to handle human-in-the-loop gates."),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _tool_error(msg: str, *, code: str) -> dict[str, Any]:
    """Return a safe error dict so internal traces don't leak to the MCP client.

    ``code`` is REQUIRED (FAR-1502): the consuming agent branches on it, so no
    call site may fall back to a generic code. Every site that can name its
    failure mode passes a specific one (``invalid_id``, ``database_unavailable``,
    ``feature_required``, ...); ``server_error`` is the reserved catch-all for
    genuinely unexpected failures where nothing more specific is knowable.
    """
    return {"error": code, "detail": msg}


def _tool_auth_error(msg: str) -> dict[str, Any]:
    """Return an auth-expired error dict for revoked/expired credentials."""
    return {"error": "auth_expired", "detail": msg}


def _tool_session_contract_error(exc: SQLAlchemyError, log_key: str) -> dict[str, Any] | None:
    """MCP payload rendering of the shared session-contract classifier (FAR-1482).

    The rendering itself lives next to ``raise_session_contract_error`` in
    ``db_error_handling`` (FAR-1502 review: a shared home), so the MCP tool
    surface and the run WebSocket control-frame surface share ONE
    implementation and neither has to import the other's module.
    """
    return session_contract_error_payload(exc, log_key)


def _tool_lock_timeout_error(exc: SQLAlchemyError, log_key: str) -> dict[str, Any] | None:
    """Return the branchable ``lock_timeout`` payload when *exc* is 55P03, else None.

    FAR-1825: the category-C CRUD sweep gave every reachable row lock a
    transaction-scoped ``lock_timeout`` (``set_mutation_row_lock_timeout``), so
    SQLSTATE 55P03 (``lock_not_available``) is now reachable from the MCP tools
    that front one of those helpers. An ``except SQLAlchemyError`` arm that
    merely returns ``database_unavailable`` reports a busy-row CONFLICT as a
    database OUTAGE — a retry-now signal against the very lock that is busy.

    Call sites are deliberately SCOPED to the paths where 55P03 is actually
    reachable, not sprayed onto every MCP tool:

    * ``_tool_exception_error`` (below) — the shared generic-``Exception``
      mapper every tool's backstop runs, so a bounded tool whose error escapes
      its narrower arms still classifies (e.g. ``set_hitl_email_alerts``, which
      reaches C8 ``_get_account_locked`` through the generic arm);
    * the shared ``_tool_sqlalchemy_error`` mapper behind the explicit
      ``except SQLAlchemyError`` arms of the two MCP tools whose CRUD helper a
      request path now bounds — ``update_parameter_schema`` (C14
      ``update_schema``) and ``update_parameter_set`` (C7 ``update_set``).

    Every other MCP tool's CRUD path takes no newly-bounded lock, so an arm
    there would be dead code asserting a reachability it does not have (the
    pre-PR gate rejected exactly that blanket application). ``cancel_run`` and
    ``update_pipeline_graph`` keep their bespoke inline ``lock_timeout``
    payloads (FAR-1610 / FAR-1845).

    Returns ``None`` for every other SQLAlchemy error so the caller's existing
    ``database_unavailable`` / re-raise handling is unchanged.
    """
    if sqlstate_of(exc) != LOCK_NOT_AVAILABLE_SQLSTATE:
        return None
    _log.warning("%s.lock_timeout", log_key, exc_info=exc)
    return _tool_error(_MSG_LOCK_TIMEOUT, code="lock_timeout")


def _tool_sqlalchemy_error(exc: SQLAlchemyError, log_key: str) -> dict[str, Any]:
    """Map a non-contract ``SQLAlchemyError`` from a bounded MCP tool to a payload.

    Shared by the two arms whose CRUD helper a request path now bounds
    (FAR-1825). Those arms run the FAR-1482 session-contract guard as their
    FIRST statement and log the failure themselves: both are arm-level
    obligations the architecture guards check (``test_api_db_error_guard`` /
    ``test_no_silent_db_swallow``), so they must live IN the arm rather than
    behind a helper the static scanner cannot see through. This helper
    therefore carries only the FAR-1825 branch — a bounded row-lock wait that
    timed out (SQLSTATE 55P03) is a retry-now ``lock_timeout`` conflict, not
    the ``database_unavailable`` outage fallback. Every other error keeps the
    pre-FAR-1825 ``database_unavailable`` payload.
    """
    if (lock_timeout_error := _tool_lock_timeout_error(exc, log_key)) is not None:
        return lock_timeout_error
    return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}


def _structured_http_error_detail(
    exc: StarletteHTTPException,
    error_code: str,
    fallback_detail: str,
) -> dict[str, Any] | None:
    """Return a STRUCTURED 4xx detail payload, or ``None`` for a plain one.

    The MCP surface is the only repair path for an agent, so the judge-write
    freeze's ``{error, detail, hint}`` must reach the caller intact rather than
    being replaced by a log constant (ADR 066 D8). ``detail["error"]`` is the
    repo's machine-readable code convention (see ``api/models/problem.py``);
    ``message`` is accepted for the older structured shape.
    """
    if not isinstance(exc.detail, dict):
        return None
    return {
        "error": error_code,
        "code": exc.detail.get("error") or exc.detail.get("code"),
        "detail": exc.detail.get("detail") or exc.detail.get("message") or fallback_detail,
        "hint": exc.detail.get("hint"),
    }


def _sqlalchemy_exception_response(
    exc: SQLAlchemyError,
    msg: str,
    log_key: str,
) -> dict[str, Any]:
    """Classify a non-contract ``SQLAlchemyError`` into a branchable payload."""
    if (contract_error := _tool_session_contract_error(exc, log_key)) is not None:
        return contract_error
    if (lock_timeout_error := _tool_lock_timeout_error(exc, log_key)) is not None:
        return lock_timeout_error
    if isinstance(exc, IntegrityError):
        return _tool_error(msg, code="conflict")
    if isinstance(exc, ProgrammingError):
        return _tool_error(msg, code="migration_required")
    return _tool_error(msg, code="database_unavailable")


def _tool_exception_error(msg: str, exc: BaseException, log_key: str) -> dict[str, Any]:
    """Map an exception that escaped a tool body to a SPECIFIC, branchable code.

    FAR-1502: the consuming agent branches on the ``error`` code, so an
    ``except Exception`` arm must not answer with a generic code when the
    exception itself names the failure mode. The ladder, most specific first:

    * ``MCPAuthorizationError`` → ``insufficient_scope`` (the same rendering
      every tool's explicit authz arm gives — some tools reach the generic
      arm without peeling it, e.g. ``list_api_keys``).
    * ``StarletteHTTPException`` → ``validation_failed`` (4xx — the same
      rendering the shell's ``handle_http_exception`` arm gives) /
      ``server_error`` (5xx).
    * a session-contract violation (``InvalidRequestError`` /
      ``MissingGreenlet``) → ``session_contract_error`` via the shared
      FAR-1482/FAR-1464 classifier, so the generic arm cannot re-introduce
      the misclassification FAR-1482 fixed on the explicit arms.
    * SQLSTATE 55P03 (a bounded row-lock wait expiring) → ``lock_timeout``:
      a busy-row conflict, not a database outage (FAR-1825). This must be
      checked BEFORE the ``IntegrityError`` / generic arms below, which
      otherwise answer ``database_unavailable``.
    * ``IntegrityError`` → ``conflict``; ``ProgrammingError`` →
      ``migration_required``; any other ``SQLAlchemyError`` (incl. a transient
      ``OperationalError`` / ``PendingRollbackError``) → ``database_unavailable``.
    * anything else → ``server_error`` — the reserved catch-all, the ONLY
      place a not-further-specifiable code is allowed (FAR-1502).

    ``msg`` stays the payload detail so the consuming agent keeps the
    operation context; only the session-contract detail is taken from the
    shared classifier (it must match the REST 500 text exactly).
    """
    if isinstance(exc, MCPAuthorizationError):
        return {"error": "insufficient_scope", "detail": str(exc)}
    if isinstance(exc, StarletteHTTPException):
        error_code = "validation_failed" if exc.status_code < 500 else "server_error"
        structured = _structured_http_error_detail(exc, error_code, msg)
        if structured is not None:
            return structured
        return _tool_error(msg, code=error_code)
    if isinstance(exc, SQLAlchemyError):
        return _sqlalchemy_exception_response(exc, msg, log_key)
    return _tool_error(msg, code="server_error")


def _parse_uuid_param(value: str, field: str) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse a UUID tool param, returning ``(value, None)`` or ``(None, error_dict)``."""
    try:
        return uuid.UUID(value), None
    except ValueError:
        return None, {"error": "invalid_id", "field": field, "detail": f"Invalid UUID format: {value}"}


def _parse_optional_uuid(
    value: str | None,
    field: str,
) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse an OPTIONAL UUID tool param (None passes through as "unassigned")."""
    if value is None:
        return None, None
    return _parse_uuid_param(value, field)


def _parse_iso8601_param(value: str | None, field: str) -> tuple[datetime | None, dict[str, Any] | None]:
    """Parse an OPTIONAL ISO 8601 timestamp param, returning ``(value, None)`` or ``(None, error_dict)``."""
    if value is None:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed, None
    except ValueError:
        return None, {
            "error": "validation_failed",
            "field": field,
            "detail": f"Invalid {field} format: {value!r} (expected ISO 8601)",
        }


_TOOL_SHELL_P = ParamSpec("_TOOL_SHELL_P")


class _DbShellConfig(NamedTuple):
    """Parameters for the shared DB/tool exception ladder."""

    log_constant: str
    integrity_detail: str | None
    fallback: str
    handle_http_exception: bool = False
    db_errors_to_fallback: bool = False


def _db_shell_integrity_response(exc: IntegrityError, cfg: _DbShellConfig) -> dict[str, Any]:
    """The ``IntegrityError`` arm of the shared DB/tool exception ladder.

    ``cfg.db_errors_to_fallback`` skips the detail clause and answers
    ``conflict`` with the ``fallback`` detail (a specific, branchable code —
    FAR-1502; shells such as ``get_trigger`` had no IntegrityError clause at
    all); a None ``integrity_detail`` yields the ``SQLAlchemyError``
    behaviour (log + ``database_unavailable``), which is what shells without
    an IntegrityError clause did; otherwise the conflict is formatted with
    ``orig``.
    """
    if cfg.db_errors_to_fallback:
        _log.exception(cfg.log_constant)
        return _tool_error(cfg.fallback, code="conflict")
    if cfg.integrity_detail is None:
        _log.exception(cfg.log_constant)
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    _log.exception(cfg.log_constant)
    return {"error": "conflict", "detail": cfg.integrity_detail.format(orig=exc.orig)}


def _db_shell_http_response(
    exc: StarletteHTTPException,
    cfg: _DbShellConfig,
    log_key: str,
) -> dict[str, Any]:
    """Render the shell's ``StarletteHTTPException`` arm."""
    if not cfg.handle_http_exception:
        _log.exception(cfg.log_constant)
        return _tool_exception_error(cfg.fallback, exc, log_key)
    structured = _structured_http_error_detail(exc, "validation_failed", cfg.fallback)
    if structured is not None:
        return structured
    return {"error": "validation_failed", "detail": str(exc.detail)}


def _db_shell_sqlalchemy_response(
    exc: SQLAlchemyError,
    cfg: _DbShellConfig,
    log_key: str,
) -> dict[str, Any]:
    """Render the shell's generic ``SQLAlchemyError`` arm.

    Rendering only — the ``except SQLAlchemyError`` arm logs once, before this
    helper runs, so every failure (including the session-contract branch) is
    logged exactly once and no path can return this payload unlogged.
    """
    # FAR-1482: the session-contract guard must run BEFORE the
    # db_errors_to_fallback split — a programming bug is not the generic
    # "failed to <tool>" fallback either.
    if (contract_error := _tool_session_contract_error(exc, log_key)) is not None:
        return contract_error
    if cfg.db_errors_to_fallback:
        return _tool_error(cfg.fallback, code="database_unavailable")
    return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}


# Semgrep's parser cannot parse the PEP 695 type-parameter syntax below
# (``_run_db_shell[**TOOL_SHELL_P]``), so it reports a one-line
# ``PartialParsing`` skip for this file. There is no inline directive that
# clears a parse failure (``nosemgrep`` only suppresses rule findings), so the
# skip is instead tolerated by the FAR-1933 carve-out in
# ``scripts/run_semgrep.py``.


async def _run_db_shell[**TOOL_SHELL_P](
    fn: Callable[TOOL_SHELL_P, Awaitable[dict[str, Any]]],
    cfg: _DbShellConfig,
    *args: TOOL_SHELL_P.args,
    **kwargs: TOOL_SHELL_P.kwargs,
) -> dict[str, Any]:
    """Run one MCP tool body under the shared DB/tool exception ladder.

    Reproduces the verbatim per-tool ``try/except`` shells the ladder
    replaces, keyed by ``cfg`` so the remaining tools can adopt it without
    hardcoding. Clauses carry specific, branchable codes (FAR-1502):

    - ``MCPAuthorizationError`` → ``insufficient_scope`` (all shells).
    - ``StarletteHTTPException`` → ``validation_failed`` when
      ``cfg.handle_http_exception`` is True; otherwise the shared exception
      classifier (``_tool_exception_error``), which renders 4xx as
      ``validation_failed`` and 5xx as ``server_error`` with the ``cfg.fallback``
      detail — what shells without the clause did, plus a branchable code.
    - ``IntegrityError`` → ``conflict`` with ``cfg.integrity_detail``
      formatted with ``orig``; when ``integrity_detail`` is None the
      ``SQLAlchemyError`` behaviour (log + ``database_unavailable``), which is
      what shells without an IntegrityError clause did. When
      ``cfg.db_errors_to_fallback`` is True the detail clause is skipped and
      the arm answers ``conflict`` with the ``cfg.fallback`` detail,
      reproducing shells (e.g. ``get_trigger``) that had no IntegrityError
      clause at all.
    - ``ProgrammingError`` → ``migration_required``.
    - ``SQLAlchemyError`` → the FAR-1482 session-contract classifier first
      (a programming bug surfaces as ``.session_contract_error``, never as
      the generic fallback), then ``database_unavailable``; when
      ``cfg.db_errors_to_fallback`` is True the detail clause is skipped and
      the arm answers ``database_unavailable`` with the ``cfg.fallback``
      detail, reproducing shells that had no SQLAlchemyError clause.
    - ``Exception`` → log + ``_tool_exception_error(cfg.fallback, exc, log_key)``
      — classified by exception type (FAR-1502), ``server_error`` only when
      nothing more specific is knowable.
    """
    # `callable_label(fn)` is the only per-tool identity available here —
    # ``cfg.log_constant`` is a "<tool> failed" log MESSAGE, not a key. The
    # classifier appends ``.session_contract_error`` itself (FAR-1482).
    # ``callable_label`` (not ``fn.__name__``): the tool body is only known to
    # be callable, and a callable object / partial has no ``__name__`` — reading
    # it here would raise AttributeError ABOVE the ``try``, escaping the whole
    # error ladder this wrapper exists to provide.
    log_key = f"mcp.{callable_label(fn)}"
    try:
        return await fn(*args, **kwargs)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except StarletteHTTPException as exc:
        return _db_shell_http_response(exc, cfg, log_key)
    except IntegrityError as exc:
        return _db_shell_integrity_response(exc, cfg)
    except ProgrammingError:
        _log.exception(cfg.log_constant)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        # Log in the ARM, not in the renderer: the renderer returns early for a
        # session-contract violation, so a log inside it would leave that path
        # silent (and a structural scanner cannot see through a helper anyway).
        _log.exception(cfg.log_constant)
        return _db_shell_sqlalchemy_response(exc, cfg, log_key)
    except Exception as exc:
        _log.exception(cfg.log_constant)
        return _tool_exception_error(cfg.fallback, exc, log_key)


def _tool_db_shell(
    *,
    log_constant: str,
    integrity_detail: str | None,
    fallback: str,
    handle_http_exception: bool = False,
    db_errors_to_fallback: bool = False,
) -> Callable[
    [Callable[_TOOL_SHELL_P, Awaitable[dict[str, Any]]]],
    Callable[_TOOL_SHELL_P, Awaitable[dict[str, Any]]],
]:
    """Wrap an async MCP tool body in the shared DB/tool exception ladder.

    The ladder itself lives in ``_run_db_shell`` (one module-level copy of the
    exception clauses, keyed by ``_DbShellConfig``); this factory only binds
    the per-tool parameters and returns the thin decorator that applies it.
    """
    cfg = _DbShellConfig(
        log_constant=log_constant,
        integrity_detail=integrity_detail,
        fallback=fallback,
        handle_http_exception=handle_http_exception,
        db_errors_to_fallback=db_errors_to_fallback,
    )

    def decorator(
        fn: Callable[_TOOL_SHELL_P, Awaitable[dict[str, Any]]],
    ) -> Callable[_TOOL_SHELL_P, Awaitable[dict[str, Any]]]:
        @functools.wraps(fn)
        async def wrapper(*args: _TOOL_SHELL_P.args, **kwargs: _TOOL_SHELL_P.kwargs) -> dict[str, Any]:
            return await _run_db_shell(fn, cfg, *args, **kwargs)

        return wrapper

    return decorator


def _serialize_edges(edges: list[Any]) -> list[dict[str, Any]]:
    """Serialize pipeline-graph edges to the MCP response shape."""
    return [
        {
            "id": str(e.id),
            "source_node_id": str(e.source_node_id),
            "target_node_id": str(e.target_node_id),
            "edge_type": e.edge_type,
        }
        for e in edges
    ]


def _serialize_run_evals(evals: list[Any]) -> list[dict[str, Any]]:
    """Serialize run eval rows to the MCP response shape."""
    return [
        {
            "id": str(e.id),
            "eval_id": str(e.eval_id),
            "node_id": str(e.node_id) if e.node_id else None,
            "passed": e.passed,
            "score": e.score,
            "detail": e.detail,
            "evaluated_at": e.evaluated_at.isoformat() if e.evaluated_at else None,
        }
        for e in evals
    ]


def _iso_or_none(value: Any) -> str | None:
    """ISO-format a datetime column, or None."""
    return value.isoformat() if value else None


def _parse_basic_auth_header(request: Request, params: dict[str, str]) -> tuple[dict[str, str], JSONResponse | None]:
    """Parse an HTTP Basic Authorization header into client credentials.

    Extracts ``client_id`` / ``client_secret`` from the ``Authorization``
    header when present, only filling fields the form body did not supply.
    Returns ``(creds_delta, None)`` on success or ``({}, error_response)`` on a
    malformed header. RFC 6749 §2.3.1.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith(_BASIC_PREFIX):
        return {}, None
    import base64 as _base64

    try:
        decoded = _base64.b64decode(auth_header[len(_BASIC_PREFIX) :]).decode("utf-8")
        basic_id, _, basic_secret = decoded.partition(":")
    except Exception:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "Malformed Basic Authorization header"},
            status_code=400,
        )
    creds: dict[str, str] = {}
    if not params.get("client_secret", ""):
        creds["client_secret"] = basic_secret
    if not params.get("client_id", ""):
        creds["client_id"] = basic_id
    return creds, None


async def _load_trigger_row(
    s: AsyncSession,
    org_id: uuid.UUID,
    tid: uuid.UUID,
    *,
    account_id: uuid.UUID | None = None,
) -> Any | None:
    """Load a non-deleted trigger row, org-scoped. None when not found.

    Pass *account_id* for a viewer-scoped read: the load is then additionally
    bounded by that account's team memberships (``account_team_scope_clause``),
    so ``trigger.list``'s viewer floor can never expose a trigger on a
    team-private pipeline the viewer does not belong to.
    """
    from sqlalchemy import select

    from farna.db.models.trigger import Trigger

    q = select(Trigger).where(
        Trigger.id == tid,
        Trigger.organisation_id == org_id,
        Trigger.deleted_at.is_(None),
    )
    if account_id is not None:
        from farna.db.crud.team_scope import account_team_scope_clause
        from farna.db.models.pipeline import Pipeline

        q = q.join(Pipeline, Trigger.pipeline_id == Pipeline.id).where(Pipeline.deleted_at.is_(None)).where(account_team_scope_clause(Pipeline.owner_team_id, account_id))
    return (await s.execute(q)).scalar_one_or_none()


def _validate_trigger_numbers(
    max_concurrent_runs: int | None,
    daily_spend_limit: float | None,
) -> dict[str, Any] | None:
    """Validate the numeric trigger fields; returns an error dict, or None."""
    if max_concurrent_runs is not None and max_concurrent_runs < 1:
        return {"error": "validation", "field": "max_concurrent_runs", "detail": "must be >= 1"}
    if daily_spend_limit is not None and daily_spend_limit < 0:
        return {"error": "validation", "field": "daily_spend_limit", "detail": "must be >= 0"}
    return None


async def _require_plan_feature(org_id: uuid.UUID, settings: Any, feature_name: str) -> dict[str, Any] | None:
    """Org-aware plan-feature gate for an MCP tool.

    Returns the standard ``feature_required`` error dict when the org does not
    enable ``feature_name``, else ``None``.

    FAR-2595: this applies the org's ``settings_json.feature_overrides`` on top
    of the tier/licence default, with the same precedence as ``GET
    /api/v1/plan``. Without that overlay a default-OFF, org-toggleable flag
    (``system_maps``) would stay refused forever even after an admin enabled
    it: ``resolve_plan_context`` alone never reads org overrides.
    """
    from farna.core.feature_flags import feature_overrides_from_settings, resolve_plan_context
    from farna.db.crud.organisation import get_organisation

    async with _session(org_id) as s:
        org = await get_organisation(s, org_id)
    async with _session(org_id) as s:
        plan_ctx = await resolve_plan_context(settings, s, org)
    override = feature_overrides_from_settings(getattr(org, "settings_json", None)).get(feature_name)
    enabled = plan_ctx.feature_enabled(feature_name) if override is None else override
    if not enabled:
        return {
            "error": "feature_required",
            "detail": f"{feature_name} is not enabled for this organisation",
        }
    return None


_EVAL_NAME_MAX_LENGTH = 255


_TEAM_SCOPE_ERROR = object()


def _hitl_human_only_denial_payload(
    *,
    run_id: uuid.UUID,
    review_id: str,
    action: str,
    verdict: str,
    linkage: HumanLinkage,
) -> dict[str, Any]:
    """The ``hitl.human_only_denied`` audit payload for MCP denials (FAR-634, ADR 063).

    Records HOW linkage was established, with per-field provenance, never a
    collapsed boolean:

    * ``auth_type`` — **observed** credential class (``api_key``/``oauth``/``jwt``);
    * ``account_id`` / ``token_family`` — **observed** (the credential's account
      and the signed token-family claim, both ``None`` when the credential
      carries no such value);
    * ``principal_kind`` — **derived** from ``Account.kind`` + live membership
      (``None`` when the credential is not a human-linkable class, i.e. neither
      consent-derived OAuth nor a user-scoped ``mk_`` key);
    * ``linkage`` — **derived** step-1 conditions
      (``none``/``oauth_consent``/``user_scoped_key``);
    * ``verdict`` — **derived** from ``human_only_denial``.

    The pre-ADR-063 payload named its auth-type field ``principal_kind``
    (an auth type, not a principal kind) and hard-coded ``client_kind: None``.
    ``client_kind`` is REMOVED: MCP has no server-derived value for it, and a
    field that is always ``None`` is indistinguishable from one never
    evaluated. No client-supplied field is written.
    """
    return {
        "run_id": str(run_id),
        "review_id": review_id,
        "action": action,
        "surface": "mcp",
        "auth_type": linkage.auth_type,
        "account_id": str(linkage.account_id) if linkage.account_id is not None else None,
        "token_family": linkage.token_family,
        "principal_kind": linkage.principal_kind,
        "linkage": linkage.linkage,
        "reason": verdict,
    }


async def _append_hitl_human_only_denied_audit(
    s: AsyncSession,
    org_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    review_id: str,
    action: str,
    verdict: str,
    linkage: HumanLinkage,
) -> None:
    """Append the ``hitl.human_only_denied`` audit event for an MCP denial (FAR-634).

    Appends on the CURRENT session (no rollback happens on this path — the
    denial returns an error dict and the ``_session`` context commits on clean
    exit), so the event commits together with the request. ``append_audit_event``
    scopes its own savepoint; the wrapper is failure-isolated: an audit failure
    is logged and NEVER changes the denial outcome (it is already a denial —
    the error dict below still returns). ``CancelledError`` always propagates.
    """
    try:
        from farna.core.audit_logger import append_audit_event
        from farna.db.crud.hitl_review_config import EVENT_HUMAN_ONLY_DENIED

        try:
            actor_user_id = _ctx_user_id_val()
        except McpAuthContextError:
            actor_user_id = None
        await append_audit_event(
            s,
            org_id=org_id,
            event_type=EVENT_HUMAN_ONLY_DENIED,
            actor_user_id=actor_user_id,
            resource_type="run",
            resource_id=run_id,
            payload_json=_hitl_human_only_denial_payload(
                run_id=run_id,
                review_id=review_id,
                action=action,
                verdict=verdict,
                linkage=linkage,
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.warning(
            "mcp.hitl_human_only_denial_audit_failed",
            extra={"run_id": str(run_id), "review_id": review_id, "action": action},
            exc_info=True,
        )


async def _check_human_only_gate(
    s: AsyncSession,
    org_id: uuid.UUID,
    run: Any,
    review_id: str,
    action: str,
) -> dict[str, Any] | None:
    """Return an error dict when the gate is human_only, else ``None``.

    Called for the ``claim``, ``approve``, ``reject`` and ``deliver_manual``
    actions (FAR-609: claim is human_only too). ``reject`` still requires a
    claim_token, and an unlinked MCP caller can no longer claim a default
    human_only gate, so an unlinked agent can neither claim, decide nor reject
    one — only a principal already holding a claim (e.g. claimed before the
    policy change) can reject. Agent-only runs on default gates require
    browser-human intervention (intended policy).

    FAR-610: the gate's config is resolved from the RUN's snapshot graph
    (falling back to the live pipeline edges, then the live HITL-node config)
    via the shared resolver (``db.crud.hitl_review_config``).

    Fail closed (FAR-610 review): when the config is UNRESOLVABLE but the
    gate actually fired (a claim row exists —
    ``hitl_review_exists_but_unresolved``), the human_only policy cannot be
    verified, so the decision is denied rather than silently allowed. The
    verdict itself lives in the shared pure function
    ``hitl_review_config.human_only_denial`` so REST and MCP enforce one policy
    with one wording (FAR-610 review).

    ADR 063 step 1 (FAR-1957): ``non_browser_credential`` is COMPUTED from the
    single shared authority resolver (``auth.human_linkage.resolve_human_linkage``)
    over the verified credential — no longer a hard-coded ``True``. The
    ``mcp_human_linked_hitl_authority`` flag is default-OFF and must be explicitly
    enabled by an organisation admin. When enabled, an eligible consent-derived
    OAuth credential belonging to a live human principal (with the action's
    consented grant), or a verified user-scoped human-linked API key (whose live
    role supplies its authority), is human-linked and may claim, approve or
    reject the gate. ``deliver_manual`` is excluded even then: it injects the
    run's output directly (ADR 035) and stays browser-only over MCP. Org-,
    team-, service- and run-scoped API keys, non-OAuth JWTs (including a
    browser-class password JWT, FAR-634), blacklisted families, inactive
    accounts or memberships, missing scopes, and every verification failure
    resolve to deny. Normal possession requirements still apply: claim, approve,
    reject and deliver-manual require the applicable claim token and gate rules.
    Every denial emits a warning log + the
    ``hitl.human_only_denied`` audit event (FAR-634) before the error dict is
    returned.
    """
    from farna.db.crud.hitl_review_config import (
        EVENT_HUMAN_ONLY_DENIED,
        hitl_review_exists_but_unresolved,
        human_only_denial,
        human_only_effective,
        resolve_hitl_review_config,
    )

    config = await resolve_hitl_review_config(s, run_id=run.id, review_id=review_id, org_id=org_id, run=run)
    if config is not None and not human_only_effective(config):
        # An explicitly non-human_only gate cannot deny ANY credential, so the
        # linkage + org-flag reads below would be wasted work (and a needless
        # second DB session for an OAuth caller). Equivalent to letting
        # ``human_only_denial`` return None.
        return None
    gate_fired = False
    if config is None:
        gate_fired = await hitl_review_exists_but_unresolved(s, run_id=run.id, review_id=review_id, org_id=org_id)

    # ADR 063 step 1: compute the denial input from the verified linkage.
    auth_type = _ctx_auth_type.get(None) or "unknown"
    linkage = await resolve_human_linkage(
        s,
        org_id=org_id,
        auth_type=auth_type,
        account_id=_ctx_user_id.get(None),
        token_family=_ctx_token_family.get(None),
        grants=_ctx_key_grants.get(None),
        required_permission=required_permission_for_action(action),
        key_scope=_ctx_key_scope.get(None),
    )
    # FAR-2281: the org-wide persona authority flag is consulted only after
    # server-side linkage verification. It is default-OFF and resolves
    # fail-closed, so neither an unlinked credential nor a flag read failure can
    # widen this boundary.
    human_linked_authority = linkage.linked and await resolve_mcp_human_linked_hitl_authority(
        org_id=org_id,
        flag_name="mcp_human_linked_hitl_authority",
    )
    # A verified human-linked credential may claim, approve or reject when the
    # org explicitly enables persona authority. ``deliver_manual`` is excluded:
    # it injects the run's output directly (ADR 035) and stays browser-only
    # over MCP even for a human-linked credential. Possession remains out of
    # scope; the flag controls only this persona-based authority.
    allow_via_linkage = human_linked_authority and linkage.linkage in {LINKAGE_OAUTH_CONSENT, LINKAGE_USER_SCOPED_KEY} and action != "deliver_manual"
    non_browser_credential = not allow_via_linkage
    verdict = human_only_denial(config, non_browser_credential=non_browser_credential, gate_fired=gate_fired)
    if verdict is not None:
        _log.warning(
            EVENT_HUMAN_ONLY_DENIED,
            extra={
                "run_id": str(run.id),
                "review_id": review_id,
                "action": action,
                "surface": "mcp",
                "auth_type": linkage.auth_type,
                "linkage": linkage.linkage,
            },
        )
        await _append_hitl_human_only_denied_audit(
            s,
            org_id,
            run_id=run.id,
            review_id=review_id,
            action=action,
            verdict=verdict,
            linkage=linkage,
        )
        return {"error": "human_only_gate", "detail": verdict}
    return None


# ---------------------------------------------------------------------------
# Parameter Schema CRUD MCP tools
# ---------------------------------------------------------------------------

_MSG_PARAM_SCHEMA_NOT_FOUND = "Parameter schema not found"
_MSG_PARAM_SET_NOT_FOUND = "Parameter set not found"


def _log_tool_failure(tool: str) -> None:
    _log.exception("%s failed", tool)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


#: FAR-688: the fire-context dump on the ``modulo://runs/{run_id}/hitl/{review_id}``
#: resource is sliced to this many characters (with the shared truncation
#: marker appended when sliced) so one briefing can never dominate the
#: resource payload.
_FIRE_CONTEXT_MAX_CHARS = 2048


# ---------------------------------------------------------------------------
# Health check (mounted inside the MCP sub-app, before auth middleware)
# ---------------------------------------------------------------------------


def _mcp_healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# OAuth 2.0 protocol endpoints (mounted inside the MCP sub-app, before auth)
# ---------------------------------------------------------------------------


def _frontend_url(settings: Any) -> str:
    """Derive the SPA base URL (delegates to the shared resolver)."""
    from farna.api.frontend_url import resolve_frontend_url

    return resolve_frontend_url(settings)


def _oauth_authorize_param_errors(params: Mapping[str, str]) -> JSONResponse | None:
    """First validation error for the authorize query, or None. Runs in wire order."""
    if params.get("response_type", "") != "code":
        return JSONResponse({"error": "unsupported_response_type"}, status_code=400)
    if not params.get("client_id", "") or not params.get("redirect_uri", ""):
        return JSONResponse(
            {"error": "invalid_request", "detail": "client_id and redirect_uri required"},
            status_code=400,
        )
    if not params.get("state", ""):
        return JSONResponse(
            {"error": "invalid_request", "detail": "state parameter required"},
            status_code=400,
        )
    # S256-only (RFC 7636) — the challenge is verified at token exchange, so
    # rejecting plain/empty here keeps every stored challenge verifiable.
    code_challenge_method = params.get("code_challenge_method", "")
    try:
        from farna.auth.oauth import InvalidGrantError, validate_pkce_method

        validate_pkce_method(code_challenge_method)
    except InvalidGrantError as exc:
        return JSONResponse(
            {"error": "invalid_request", "detail": str(exc)},
            status_code=400,
        )
    if not params.get("code_challenge", "") or not params.get("code_challenge", "").strip():
        return JSONResponse(
            {"error": "invalid_request", "detail": "code_challenge parameter required"},
            status_code=400,
        )
    return None


def _oauth_authorize_settings_error(settings: Any) -> JSONResponse | None:
    """Return an error response when the public URL is unconfigured."""
    if not public_url_is_configured(settings):
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
        )
    return None


async def _oauth_authorize(request: Request) -> JSONResponse | RedirectResponse:
    """GET /mcp/oauth/authorize — thin 302 to the SPA consent route.

    Anonymous (the browser is not yet authenticated against the SPA). Validates
    the request (client exists, exact-match redirect_uri, S256-only PKCE), then
    persists an ``oauth_consent_states`` row (account_id NULL until approve)
    and redirects the browser to ``/oauth/authorize?...`` on the SPA. The
    ``Referrer-Policy: no-referrer`` header keeps the client's query params
    from leaking to any third-party referer. The old POST handler that minted
    codes directly (anonymous, unbound) is DELETED — codes are only minted by
    the authenticated consent approve endpoint (ADR 047 DECISION 1).
    """
    params = request.query_params
    param_error = _oauth_authorize_param_errors(params)
    if param_error is not None:
        return param_error

    settings = get_settings()
    settings_error = _oauth_authorize_settings_error(settings)
    if settings_error is not None:
        return settings_error

    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    scope = params.get("scope", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")

    from farna.auth.oauth import (
        create_consent_state,
        get_oauth_client_by_client_id,
        is_valid_redirect_uri,
        normalize_scopes,
        redirect_uri_allowed,
        validate_client_scopes,
    )

    try:
        session_factory = _get_session_factory()
        async with session_factory() as s, s.begin():
            # Look up client by globally unique client_id.
            client = await get_oauth_client_by_client_id(s, client_id)
            if client is None:
                return JSONResponse(
                    {"error": "invalid_client", "detail": "Unknown client_id"},
                    status_code=400,
                )

            # FAR-1281: exact match AND the same validity predicate applied at
            # registration, so a row stored before that check existed fails
            # closed here instead of being redirected to.
            if not redirect_uri_allowed(client, redirect_uri):
                if not is_valid_redirect_uri(redirect_uri):
                    # Registered but forbidden by today's rules — the owning
                    # org's admin/operator has to re-register the client.
                    _log.warning(
                        "mcp_oauth.authorize.forbidden_registered_redirect_uri",
                        extra={"client_id": client_id},
                    )
                return JSONResponse(
                    {"error": "invalid_client", "detail": "redirect_uri not allowed"},
                    status_code=400,
                )

            try:
                requested_scopes = normalize_scopes(scope)
                valid_scopes = validate_client_scopes(client, requested_scopes)
            except Exception as exc:
                return JSONResponse(
                    {"error": "invalid_scope", "detail": str(exc)},
                    status_code=400,
                )

            # Set RLS context for the client's org before creating records.
            await set_rls_org(s, client.organisation_id)
            # FAR-1283: this leg is pre-auth (the browser is not yet signed in),
            # so it must NOT use require_feature — that would 401 before the
            # handler ran. The org comes from the client row the caller just
            # proved, so the kill switch is enforced without an authenticated
            # dependency chain and before any row is written.
            if not await _mcp_server_flag_enabled(client.organisation_id, s):
                return _mcp_feature_unavailable_response()
            await create_consent_state(
                s,
                state=state,
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=valid_scopes,
                code_challenge=code_challenge,
                org_id=client.organisation_id,
            )
    except ProgrammingError:
        _log.warning("mcp_oauth.authorize.programming_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_server._oauth_authorize")
        _log.warning("mcp_oauth.authorize.sqlalchemy_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.authorize.unexpected_error", extra={"client_id": client_id})
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )

    consent_url = f"{_frontend_url(settings)}/oauth/authorize?client_id={quote(client_id)}&redirect_uri={quote(redirect_uri)}&state={quote(state)}&code_challenge={quote(code_challenge)}"
    redirect = RedirectResponse(consent_url, status_code=302)
    redirect.headers["Referrer-Policy"] = "no-referrer"
    return redirect


async def _parse_oauth_form(request: Request) -> tuple[dict[str, str] | None, JSONResponse | None]:
    """Parse an RFC 6749 request body into a string dict.

    Accepts form-urlencoded (``request.form()``) and JSON bodies for backwards
    compatibility; anything else is ``invalid_request``. Returns
    ``(params, error)`` — exactly one is non-None.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        form = await request.form()
        params: dict[str, str] = {k: (str(v) if v is not None else "") for k, v in form.items()}
        return params, None
    if content_type == _CT_APPLICATION_JSON:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return None, JSONResponse(
                {"error": "invalid_request", "detail": "Request body must be JSON"},
                status_code=400,
            )
        params = {k: str(v) if v is not None else "" for k, v in body.items()}
        return params, None
    return None, JSONResponse(
        {"error": "invalid_request", "detail": "Content-Type must be application/x-www-form-urlencoded"},
        status_code=400,
    )


def _extract_oauth_client_credentials(request: Request, params: dict[str, str]) -> tuple[dict[str, str], JSONResponse | None]:
    """Extract and validate the client credentials for an authorization-code grant.

    Validates ``grant_type`` is ``authorization_code`` and reads
    ``code``/``redirect_uri``/``client_id``/``code_verifier``/``client_secret``
    from the body, falling back to an HTTP Basic Authorization header for
    ``client_secret``/``client_id`` (RFC 6749 §2.3.1). Returns
    ``(creds, error)`` where ``creds`` has the six keys — exactly one of the
    tuple is non-None only on error.
    """
    grant_type = params.get("grant_type", "")
    if grant_type != "authorization_code":
        return {}, JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    code = params.get("code", "")
    redirect_uri = params.get("redirect_uri", "")
    client_id = params.get("client_id", "")
    code_verifier = params.get("code_verifier", "")

    # client_secret may come from the body (RFC 6749) OR Basic auth.
    client_secret = params.get("client_secret", "")
    creds_delta, auth_err = _parse_basic_auth_header(request, params)
    if auth_err:
        return {}, auth_err
    client_id = creds_delta.get("client_id", client_id)
    client_secret = creds_delta.get("client_secret", client_secret)

    if not code or not redirect_uri or not client_id or not client_secret:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "Missing required parameters"},
            status_code=400,
        )

    return (
        {
            "grant_type": grant_type,
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "client_secret": client_secret,
        },
        None,
    )


async def _exchange_authorization_code(creds: dict[str, str], settings: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Exchange an authorization code for an access/refresh token pair.

    Runs the OAuth token exchange steps inside a DB transaction: validate the
    client secret, set RLS org context, consume the authorization code (PKCE
    verified inside), re-verify the consenting account's LIVE role against the
    granted scopes (ADR 047), create a token family, and mint the token pair.
    Returns ``(response_dict, error)`` — ``response_dict`` is the success body
    on success; OAuth/DB exceptions propagate to the caller's ``try/except``.
    """
    from farna.auth.oauth import (
        consume_authorization_code,
        create_oauth_access_token,
        create_oauth_refresh_token,
        create_oauth_token_family,
        validate_client_secret,
        verify_live_role_covers_scopes,
    )

    session_factory = _get_session_factory()
    async with session_factory() as s, s.begin():
        # Step 1: Validate client credentials to discover org_id.
        client = await validate_client_secret(s, creds["client_id"], creds["client_secret"])

        # Step 2: Set RLS context for the client's org.
        await set_rls_org(s, client.organisation_id)

        # FAR-1283: pre-auth grant — enforce the mcp_server kill switch against
        # the client's resolved org BEFORE the code is consumed, so a disabled
        # org cannot exchange a code for tokens. No require_feature here (the
        # caller has no session; it authenticated with client_id+client_secret).
        if not await _mcp_server_flag_enabled(client.organisation_id, s):
            return None, _mcp_feature_unavailable_response()

        # Step 3: Consume the authorization code (PKCE verified inside).
        auth_code = await consume_authorization_code(
            s,
            code=creds["code"],
            client_id=creds["client_id"],
            redirect_uri=creds["redirect_uri"],
            client_secret=creds["client_secret"],
            code_verifier=creds["code_verifier"],
        )

        # Step 4: The consenting account's LIVE role must still cover the
        # granted scopes — a demoted/removed account is denied (ADR 047).
        await verify_live_role_covers_scopes(
            s,
            account_id=auth_code.account_id,
            org_id=client.organisation_id,
            scopes=auth_code.scopes.split(),
        )

        # Step 5: Create a new token family.
        family_id, sequence = await create_oauth_token_family(
            s,
            client_id=creds["client_id"],
            org_id=client.organisation_id,
        )

        scopes_list = auth_code.scopes.split()
        access_token = create_oauth_access_token(
            creds["client_id"],
            settings.secret_key,
            organisation_id=str(client.organisation_id),
            account_id=str(auth_code.account_id),
            scopes=scopes_list,
            token_family=family_id,
            token_sequence=sequence,
        )
        refresh_token = create_oauth_refresh_token(
            creds["client_id"],
            settings.secret_key,
            organisation_id=str(client.organisation_id),
            account_id=str(auth_code.account_id),
            scopes=scopes_list,
            token_family=family_id,
            token_sequence=sequence,
        )

    return (
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
            "expires_in": 3600,
            "scope": " ".join(scopes_list),
        },
        None,
    )


async def _oauth_token_impl(request: Request) -> JSONResponse:
    params, parse_err = await _parse_oauth_form(request)
    if parse_err:
        return parse_err
    if params is None:
        raise RuntimeError("_oauth_token_impl: form parse returned no error and no params")

    # FAR-1544: RFC 6749 §6 — a standards-compliant client refreshes at the
    # advertised token_endpoint, so this route must serve the refresh_token
    # grant too. Dispatch on grant_type BEFORE the authorization-code
    # credential extraction hard-rejects anything that isn't
    # authorization_code; any other grant still falls through to
    # ``unsupported_grant_type`` unchanged.
    grant_type = params.get("grant_type", "")
    request.state.oauth_grant_type = grant_type
    if grant_type == "refresh_token":
        refresh_creds, refresh_err = _extract_oauth_refresh_credentials(request, params)
        if refresh_err:
            return refresh_err
        refresh_resp, refresh_resp_err = await _exchange_refresh_token(refresh_creds, get_settings())
        if refresh_resp_err:
            return refresh_resp_err
        if refresh_resp is None:
            raise RuntimeError("_oauth_token_impl: refresh exchange returned no error and no response")
        return JSONResponse(refresh_resp)

    creds, cred_err = _extract_oauth_client_credentials(request, params)
    if cred_err:
        return cred_err

    settings = get_settings()
    if not public_url_is_configured(settings):
        return JSONResponse(
            {"error": "server_error", "detail": "MODULO_PUBLIC_URL must be configured"},
            status_code=500,
        )

    token_resp, token_err = await _exchange_authorization_code(creds, settings)
    if token_err:
        return token_err
    if token_resp is None:
        raise RuntimeError("_oauth_token_impl: token exchange returned no error and no response")
    return JSONResponse(token_resp)


async def _oauth_token(request: Request) -> JSONResponse:
    """POST /mcp/oauth/token — exchange a code or refresh token for tokens.

    RFC 6749 wire format: form-urlencoded bodies (``request.form()``) with JSON
    bodies accepted for backwards compatibility; anything else is
    ``invalid_request``. Serves both grants a client discovers at the
    advertised ``token_endpoint``: ``authorization_code`` (the PKCE
    ``code_verifier`` is required and verified against the stored S256
    challenge, RFC 7636 §4.5/§4.6) and ``refresh_token`` (FAR-1544, RFC 6749
    §6 — identical behaviour to the ``/mcp/oauth/refresh`` alias). Any other
    grant is ``unsupported_grant_type``. ``client_secret`` may arrive in the
    form body OR an HTTP Basic Authorization header. The consenting account's
    LIVE org role is re-verified against the granted scopes — a demoted
    account is denied a token (ADR 047).
    """
    from farna.auth.oauth import (
        InvalidClientError,
        InvalidGrantError,
    )

    try:
        return await _oauth_token_impl(request)
    except (InvalidGrantError, InvalidClientError):
        # Mirror ``_oauth_refresh``: the detail names the grant that failed.
        detail = "Refresh token exchange failed" if getattr(request.state, "oauth_grant_type", "") == "refresh_token" else "Authorization code exchange failed"
        return JSONResponse(
            {"error": "invalid_grant", "detail": detail},
            status_code=400,
        )
    except (ValueError, JWTError) as exc:
        # A malformed/expired refresh token raises here — without this branch
        # it would fall through to ``Exception`` and surface as a 500.
        return JSONResponse(
            {"error": "invalid_grant", "detail": str(exc)},
            status_code=400,
        )
    except StarletteHTTPException as e:
        return JSONResponse(
            {"error": "server_error" if e.status_code >= 500 else "invalid_request", "detail": e.detail},
            status_code=e.status_code,
        )
    except ProgrammingError:
        _log.warning("mcp_oauth.token.programming_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_server._oauth_token")
        _log.warning("mcp_oauth.token.sqlalchemy_error")
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.token.unexpected_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )


def _extract_oauth_refresh_credentials(request: Request, params: dict[str, str]) -> tuple[dict[str, str], JSONResponse | None]:
    """Extract and validate the client credentials for a refresh-token grant.

    Validates ``grant_type`` is ``refresh_token`` and reads
    ``refresh_token``/``client_id``/``client_secret`` from the body, falling
    back to an HTTP Basic Authorization header for the client secret/ID
    (RFC 6749 §2.3.1). Returns ``(creds, error)`` — exactly one is non-None on
    error.
    """
    grant_type = params.get("grant_type", "")
    if grant_type != "refresh_token":
        return {}, JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )

    refresh_token_value = params.get("refresh_token", "")
    client_id = params.get("client_id", "")
    client_secret = params.get("client_secret", "")

    creds_delta, auth_err = _parse_basic_auth_header(request, params)
    if auth_err:
        return {}, auth_err
    client_id = creds_delta.get("client_id", client_id)
    client_secret = creds_delta.get("client_secret", client_secret)

    if not refresh_token_value or not client_id or not client_secret:
        return {}, JSONResponse(
            {"error": "invalid_request", "detail": "refresh_token, client_id and client_secret are required"},
            status_code=400,
        )

    return (
        {
            "grant_type": grant_type,
            "refresh_token": refresh_token_value,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        None,
    )


async def _exchange_refresh_token(creds: dict[str, str], settings: Any) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Rotate a refresh token into a new access/refresh token pair.

    Validates the client secret, sets RLS org context, decodes the refresh
    token, re-verifies the consenting account's LIVE role still covers the
    token's scopes (ADR 047), then issues a new pair with an incremented
    sequence — invalidating the old refresh token. Returns
    ``(response_dict, error)``; OAuth/DB exceptions propagate to the caller.
    """
    from farna.auth.oauth import (
        InvalidGrantError,
        create_oauth_access_token,
        create_oauth_refresh_token,
        decode_oauth_refresh_token,
        rotate_oauth_token_family,
        validate_client_secret,
        verify_live_role_covers_scopes,
    )

    session_factory = _get_session_factory()
    async with session_factory() as s, s.begin():
        client = await validate_client_secret(s, creds["client_id"], creds["client_secret"])
        await set_rls_org(s, client.organisation_id)

        # FAR-1283: pre-auth grant — the kill switch is enforced against the
        # client's resolved org before a new pair is minted (see
        # ``_exchange_authorization_code`` for the identical gate).
        if not await _mcp_server_flag_enabled(client.organisation_id, s):
            return None, _mcp_feature_unavailable_response()

        claims = decode_oauth_refresh_token(creds["refresh_token"], settings.secret_key)

        # Client binding (#1174): the refresh token must have been issued to
        # the authenticated client in the authenticated client's org.
        if claims.client_id != client.client_id or claims.organisation_id != client.organisation_id:
            raise InvalidGrantError("Refresh token was not issued to this client")

        # ADR 047: the consenting account's LIVE role must still cover the
        # scopes — a demoted/removed account is denied a fresh token.
        await verify_live_role_covers_scopes(
            s,
            account_id=claims.account_id,
            org_id=claims.organisation_id,
            scopes=claims.scopes,
        )

        # Rotation (#1173): DB-backed sequence tracking. A stale sequence
        # (replay) blacklists the family; blacklisted/unknown families are
        # rejected. The rejection is raised AFTER the transaction block so the
        # blacklist write is committed rather than rolled back with it.
        rotation_error: InvalidGrantError | None = None
        family_id = ""
        new_sequence = 0
        try:
            family_id, new_sequence = await rotate_oauth_token_family(
                s,
                family_id=claims.token_family,
                current_sequence=claims.token_sequence,
                client_id=client.client_id,
                org_id=client.organisation_id,
            )
        except InvalidGrantError as exc:
            rotation_error = exc

        if rotation_error is None:
            new_access_token = create_oauth_access_token(
                client.client_id,
                settings.secret_key,
                organisation_id=str(client.organisation_id),
                account_id=str(claims.account_id),
                scopes=claims.scopes,
                token_family=family_id,
                token_sequence=new_sequence,
            )
            new_refresh_token = create_oauth_refresh_token(
                client.client_id,
                settings.secret_key,
                organisation_id=str(client.organisation_id),
                account_id=str(claims.account_id),
                scopes=claims.scopes,
                token_family=family_id,
                token_sequence=new_sequence,
            )

    if rotation_error is not None:
        raise rotation_error

    return (
        {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",  # nosec B105 - RFC 6750 token_type label, not a credential
            "expires_in": 3600,
            "scope": " ".join(claims.scopes),
        },
        None,
    )


async def _oauth_refresh_impl(request: Request) -> JSONResponse:
    params, parse_err = await _parse_oauth_form(request)
    if parse_err:
        return parse_err
    if params is None:
        raise RuntimeError("_oauth_refresh_impl: form parse returned no error and no params")

    creds, cred_err = _extract_oauth_refresh_credentials(request, params)
    if cred_err:
        return cred_err

    settings = get_settings()
    token_resp, token_err = await _exchange_refresh_token(creds, settings)
    if token_err:
        return token_err
    if token_resp is None:
        raise RuntimeError("_oauth_refresh_impl: token exchange returned no error and no response")
    return JSONResponse(token_resp)


async def _oauth_refresh(request: Request) -> JSONResponse:
    """POST /mcp/oauth/refresh — exchange refresh token for new access token.

    Form-urlencoded per RFC 6749 with JSON compat, mirroring ``_oauth_token``.
    Re-verifies the client secret (body or Basic auth) and the consenting
    account's LIVE org role against the token's scopes — if the account was
    demoted (or removed) since the token was issued, the refresh is DENIED
    (ADR 047 demote-then-refresh). The refresh token is rotated: a new pair is
    issued with an incremented sequence, invalidating the old refresh token.
    """
    from farna.auth.oauth import (
        InvalidClientError,
        InvalidGrantError,
    )

    try:
        return await _oauth_refresh_impl(request)
    except (InvalidGrantError, InvalidClientError):
        return JSONResponse(
            {"error": "invalid_grant", "detail": "Refresh token exchange failed"},
            status_code=400,
        )
    except (ValueError, JWTError) as exc:
        return JSONResponse(
            {"error": "invalid_grant", "detail": str(exc)},
            status_code=400,
        )
    except StarletteHTTPException as e:
        return JSONResponse(
            {"error": "server_error" if e.status_code >= 500 else "invalid_request", "detail": e.detail},
            status_code=e.status_code,
        )
    except ProgrammingError:
        _log.warning("mcp_oauth.refresh.programming_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_FEATURE_NOT_AVAILABLE_MIGRATE},
            status_code=501,
        )
    except SQLAlchemyError as exc:
        raise_session_contract_error(exc, "mcp_server._oauth_refresh")
        _log.warning("mcp_oauth.refresh.sqlalchemy_error")
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": _MSG_DB_ERROR_TRY_AGAIN},
            status_code=503,
        )
    except Exception:
        _log.exception("mcp_oauth.refresh.unexpected_error")
        return JSONResponse(
            {"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Build the mounted ASGI app (called from main.py)
# ---------------------------------------------------------------------------


def _mcp_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log unhandled MCP exceptions and return a structured JSON error.

    Starlette's ``ServerErrorMiddleware`` (outermost in the middleware stack)
    catches unhandled exceptions and calls this handler instead of the default
    ``PlainTextResponse("Internal Server Error")`` — making errors observable
    in production logs for the first time.
    """
    _log.exception(
        "mcp.unhandled_exception",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "exc_type": type(exc).__name__,
            "exc_repr": str(exc),
            "traceback": _traceback.format_exc(),
        },
    )
    return JSONResponse(
        status_code=500,
        # FAR-1502: ``server_error`` is the surface's reserved catch-all
        # (the OAuth endpoints above already render it for this exact
        # frame); the generic ``internal_error`` is disallowed.
        content={"error": "server_error", "detail": _MSG_UNEXPECTED_ERROR},
    )


def build_mcp_asgi_app() -> Starlette:
    """Return the MCP Starlette app wrapped with auth middleware."""
    inner = mcp.streamable_http_app()

    # Mount an in-sub-app health check for orchestrators / load balancers.
    health_route = Route("/healthz", _mcp_healthz, methods=["GET"])

    # OAuth protocol endpoints — placed before auth middleware so they
    # don't require a Bearer token (authorize is an anonymous browser 302;
    # token/refresh authenticate via client_id + client_secret).
    oauth_authorize_route = Route("/oauth/authorize", _oauth_authorize, methods=["GET"])
    oauth_token_route = Route("/oauth/token", _oauth_token, methods=["POST"])
    oauth_refresh_route = Route("/oauth/refresh", _oauth_refresh, methods=["POST"])

    all_routes = [
        health_route,
        oauth_authorize_route,
        oauth_token_route,
        oauth_refresh_route,
        *list(inner.routes),
    ]
    return Starlette(
        routes=all_routes,
        middleware=[
            Middleware(McpAuthMiddleware),
            Middleware(RateLimiterMiddleware),  # type: ignore[arg-type]
        ],
        exception_handlers={Exception: _mcp_exception_handler},
        # Note: lifespan is managed by the parent FastAPI app's _lifespan
        # to ensure it is called — Starlette does not invoke sub-app lifespans.
    )


# ---------------------------------------------------------------------------
# Extracted tool domains (behaviour-preserving split of this module).
#
# Each imported module registers its handlers on the SAME `mcp` instance at
# import time, so the advertised tool/resource schema is unchanged. These
# imports must run AFTER `mcp` and the shared spine above exist. Shared helpers
# inside the extracted modules resolve lazily against THIS module's namespace,
# so `mock.patch("farna.api.mcp_server.<helper>")` keeps intercepting.
#
# The handler NAMES are re-exported through the module `__getattr__` below
# rather than an eager `from ... import ...`, because every domain module
# imports the shared spine from this module: an eager import here would make
# `farna.api.mcp_tools.<domain>` unimportable before `farna.api.mcp_server`
# (a partially-initialised circular import). PEP 562 keeps
# `from farna.api.mcp_server import <handler>` working in either order.
# ---------------------------------------------------------------------------
from farna.api.mcp_tools import admin as _mcp_tools_admin  # noqa: E402,F401
from farna.api.mcp_tools import agents as _mcp_tools_agents  # noqa: E402,F401
from farna.api.mcp_tools import analytics as _mcp_tools_analytics  # noqa: E402,F401
from farna.api.mcp_tools import connectors as _mcp_tools_connectors  # noqa: E402,F401
from farna.api.mcp_tools import evals as _mcp_tools_evals  # noqa: E402,F401
from farna.api.mcp_tools import hitl as _mcp_tools_hitl  # noqa: E402,F401
from farna.api.mcp_tools import library as _mcp_tools_library  # noqa: E402,F401
from farna.api.mcp_tools import parameters as _mcp_tools_parameters  # noqa: E402,F401
from farna.api.mcp_tools import pipelines as _mcp_tools_pipelines  # noqa: E402,F401
from farna.api.mcp_tools import runs as _mcp_tools_runs  # noqa: E402,F401
from farna.api.mcp_tools import schema_registry as _mcp_tools_schema_registry  # noqa: E402,F401
from farna.api.mcp_tools import secrets_api_keys as _mcp_tools_secrets_api_keys  # noqa: E402,F401
from farna.api.mcp_tools import triggers as _mcp_tools_triggers  # noqa: E402,F401

#: Handler/helper names the rest of the tree still reaches through this module
#: (the test suite imports handlers from `farna.api.mcp_server`), mapped to the
#: extracted module that now defines them.
_EXTRACTED_REEXPORTS: dict[str, str] = {
    "_agent_item": "agents",
    "_agent_to_dict": "agents",
    "_config_key_prefixes": "admin",
    "_config_matches": "admin",
    "_config_table": "admin",
    "_delete_housekeeping_group": "admin",
    "_delete_housekeeping_groups": "admin",
    "_group_housekeeping_items": "admin",
    "_is_sensitive_key": "admin",
    "_notification_delivery_filters": "admin",
    "_notification_delivery_item": "admin",
    "_notification_delivery_page": "admin",
    "_system_maps_http_error_payload": "admin",
    "_unwrap_system_maps_route_handler": "admin",
    "_value_looks_sensitive": "admin",
    "SENSITIVE_CONFIG_KEYS": "admin",
    "discover_system_map": "admin",
    "get_audit_log": "admin",
    "get_available_features": "admin",
    "get_lifecycle_map": "admin",
    "get_org_config": "admin",
    "get_system_map_overview": "admin",
    "list_housekeeping": "admin",
    "list_lifecycle_maps": "admin",
    "list_notification_deliveries": "admin",
    "list_plugins": "admin",
    "list_sso_providers": "admin",
    "perform_housekeeping": "admin",
    "create_agent": "agents",
    "get_agent": "agents",
    "list_agents": "agents",
    "list_feature_flags": "agents",
    "list_teams": "agents",
    "list_users": "agents",
    "_AnalyticsQueryInput": "analytics",
    "_analytics_deep_link": "analytics",
    "_build_analytics_params": "analytics",
    "_parse_analytics_enums": "analytics",
    "_parse_analytics_ids": "analytics",
    "_parse_mcp_datetime": "analytics",
    "_query_analytics_concurrency_impl": "analytics",
    "_query_analytics_impl": "analytics",
    "query_analytics": "analytics",
    "query_analytics_concurrency": "analytics",
    "_assert_create_eval_definition_params": "evals",
    "_assert_eval_definition_write_scope": "evals",
    "_assert_eval_type": "evals",
    "_assert_pass_threshold": "evals",
    "_assert_update_eval_definition_params": "evals",
    "_collect_eval_definition_updates": "evals",
    "_parse_eval_ref_ids": "evals",
    "create_eval_definition": "evals",
    "delete_eval_definition": "evals",
    "get_eval_suite": "evals",
    "list_eval_definitions": "evals",
    "list_eval_suites": "evals",
    "update_eval_definition": "evals",
    "_dispatch_hitl_action": "hitl",
    "_dispatch_hitl_approve": "hitl",
    "_dispatch_hitl_deliver_manual": "hitl",
    "_get_hitl_review_impl": "hitl",
    "_hitl_required_team_name": "hitl",
    "_list_hitl_reviews_impl": "hitl",
    "_load_pending_hitl_reviews": "hitl",
    "_load_hitl_run": "hitl",
    "_resume_run_after_mcp_approval": "hitl",
    "_review_hitl_impl": "hitl",
    "_validate_mcp_choice_answer": "hitl",
    "get_hitl_email_alerts": "hitl",
    "get_hitl_review": "hitl",
    "list_hitl_reviews": "hitl",
    "list_pending_hitl": "hitl",
    "resource_hitl_review": "hitl",
    "review_hitl": "hitl",
    "set_hitl_email_alerts": "hitl",
    "create_schema": "schema_registry",
    "get_schema_versions": "schema_registry",
    "infer_schema": "schema_registry",
    "list_environment_profiles": "schema_registry",
    "list_schemas": "schema_registry",
    "resource_schema_detail": "schema_registry",
    "resource_schemas": "schema_registry",
    "validate_payload": "schema_registry",
    "_schema_to_dict": "parameters",
    "_set_to_dict": "parameters",
    "list_pipelines_tool": "pipelines",
    "_CreatePipelineParsed": "pipelines",
    "_validate_create_pipeline_args": "pipelines",
    "create_pipeline": "pipelines",
    "_circuit_breaker_threshold_error": "pipelines",
    "_threshold_float": "pipelines",
    "_owner_id_str": "pipelines",
    "_append_mcp_threshold_denial_audit": "pipelines",
    "set_pipeline_circuit_breaker": "pipelines",
    "_ctx_autonomy_change_allowed": "pipelines",
    "_AUTONOMY_CHANGE_DENIED": "pipelines",
    "_mcp_autonomy_levels": "pipelines",
    "_mcp_autonomy_direction_error": "pipelines",
    "_append_mcp_autonomy_audit": "pipelines",
    "_apply_mcp_autonomy_change": "pipelines",
    "demote_pipeline_autonomy": "pipelines",
    "promote_pipeline_autonomy": "pipelines",
    "set_pipeline_owners": "pipelines",
    "_OWNER_TEAM_RESOURCE_TYPES": "pipelines",
    "_OwnerTeamScopeDeniedError": "pipelines",
    "_apply_owner_team_pipeline": "pipelines",
    "_apply_owner_team_simple": "pipelines",
    "set_resource_owner_team": "pipelines",
    "update_pipeline": "pipelines",
    "get_pipeline_graph_tool": "pipelines",
    "_append_mcp_hitl_denial_audit": "pipelines",
    "_validate_graph_update": "pipelines",
    "_reject_invalid_connector_bindings": "pipelines",
    "_validate_binding_routing": "pipelines",
    "_validate_effective_binding": "pipelines",
    "_routed_binding_instance_ids": "pipelines",
    "_invalid_connector_binding_error": "pipelines",
    "_dispatch_capability_error": "pipelines",
    "_operation_scope_error": "pipelines",
    "_replace_pipeline_graph_txn": "pipelines",
    "_update_pipeline_graph_impl": "pipelines",
    "_snapshot_lock_busy_error": "pipelines",
    "update_pipeline_graph": "pipelines",
    "_validate_sandbox_nodes": "pipelines",
    "_bind_connector_ids": "pipelines",
    "_BIND_OPERATIONS": "pipelines",
    "_BIND_DISPATCH_ACTIONS": "pipelines",
    "_validate_binding_arguments": "pipelines",
    "_find_graph_node": "pipelines",
    "_patched_connector_binding": "pipelines",
    "_apply_node_connector_binding": "pipelines",
    "bind_connector_to_node": "pipelines",
    "get_pipeline_reviews": "pipelines",
    "_get_pipeline_reviews_impl": "pipelines",
    "delete_pipeline": "pipelines",
    "_mask_diff_snapshot_graph": "pipelines",
    "_mask_pipeline_diff_payload": "pipelines",
    "get_pipeline_diff": "pipelines",
    "resource_pipelines": "pipelines",
    "resource_pipeline_detail": "pipelines",
    "resource_pipeline_snapshots": "pipelines",
    "_render_snapshot_node_line": "pipelines",
    "_render_snapshot_node_details": "pipelines",
    "resource_pipeline_snapshot_detail": "pipelines",
    "_mask_library_content_for_summary": "library",
    "_assert_create_model_backend_provider": "connectors",
    "_apply_model_backend_update": "connectors",
    "_collect_model_backend_updates": "connectors",
    "_connector_item": "connectors",
    "_connector_type_field_metadata": "connectors",
    "_model_backend_item": "connectors",
    "_parse_model_backend_update_args": "connectors",
    "create_connector": "connectors",
    "create_model_backend": "connectors",
    "delete_connector": "connectors",
    "get_connector": "connectors",
    "get_model_backend": "connectors",
    "list_connector_types": "connectors",
    "list_connectors": "connectors",
    "list_model_backends": "connectors",
    "resource_connectors": "connectors",
    "resource_model_backends": "connectors",
    "update_model_backend": "connectors",
    "copy_library_primitive": "library",
    "get_integration_status": "library",
    "resource_library": "library",
    "resource_library_detail": "library",
    "search_documentation": "library",
    "search_library": "library",
    "create_parameter_schema": "parameters",
    "create_parameter_set": "parameters",
    "delete_parameter_schema": "parameters",
    "delete_parameter_set": "parameters",
    "get_parameter_schema": "parameters",
    "get_parameter_schema_references": "parameters",
    "get_parameter_set": "parameters",
    "list_parameter_schemas": "parameters",
    "list_parameter_sets": "parameters",
    "restore_parameter_schema": "parameters",
    "restore_parameter_set": "parameters",
    "update_parameter_schema": "parameters",
    "update_parameter_set": "parameters",
    "validate_parameter_schema": "parameters",
    "_CreateApiKeyParsed": "secrets_api_keys",
    "_api_key_created_audit_payload": "secrets_api_keys",
    "_MCP_INPUT_PAYLOAD_MAX_DEPTH": "runs",
    "_MCP_INPUT_PAYLOAD_MAX_STRING_CHARS": "runs",
    "_MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS": "runs",
    "_McpPayloadBudget": "runs",
    "_bound_mcp_input_payload": "runs",
    "_detect_masked_fields": "runs",
    "_load_run_for_status": "runs",
    "_resolve_run_node_output": "runs",
    "_cancel_run_impl": "runs",
    "_create_manual_run": "runs",
    "_format_run_line": "runs",
    "_get_run_evals_impl": "runs",
    "_get_run_output_impl": "runs",
    "_get_run_status_impl": "runs",
    "_list_runs_impl": "runs",
    "_mcp_run_item": "runs",
    "_run_status_base": "runs",
    "_run_status_detail": "runs",
    "_run_status_node": "runs",
    "_trigger_pipeline_impl": "runs",
    "_trigger_pipeline_validate_id": "runs",
    "cancel_run": "runs",
    "get_run_evals": "runs",
    "get_run_output": "runs",
    "get_run_status": "runs",
    "list_runs": "runs",
    "resource_pipeline_runs": "runs",
    "resource_run": "runs",
    "trigger_pipeline": "runs",
    "_api_key_created_response": "secrets_api_keys",
    "_deny_break_glass_mint": "secrets_api_keys",
    "_emit_mcp_api_key_audit": "secrets_api_keys",
    "_enforce_api_key_mint_cap": "secrets_api_keys",
    "_mint_api_key_txn": "secrets_api_keys",
    "_parse_api_key_expires": "secrets_api_keys",
    "_parse_api_key_expires_at": "secrets_api_keys",
    "_parse_api_key_team_id": "secrets_api_keys",
    "_require_admin_for_team_key": "secrets_api_keys",
    "_validate_api_key_args": "secrets_api_keys",
    "_validate_api_key_role_and_name": "secrets_api_keys",
    "create_api_key": "secrets_api_keys",
    "create_secret": "secrets_api_keys",
    "delete_secret": "secrets_api_keys",
    "list_api_keys": "secrets_api_keys",
    "list_secrets": "secrets_api_keys",
    "revoke_api_key": "secrets_api_keys",
    "_build_trigger_event_query": "triggers",
    "_create_trigger_impl": "triggers",
    "_paginate_trigger_events": "triggers",
    "_validate_cron_update": "triggers",
    "_validate_ongoing_config_change": "triggers",
    "_validate_ongoing_trigger_update": "triggers",
    "create_trigger": "triggers",
    "delete_trigger": "triggers",
    "get_trigger": "triggers",
    "list_trigger_events": "triggers",
    "list_triggers": "triggers",
    "set_org_triggers_paused": "triggers",
    "update_trigger": "triggers",
}


def __getattr__(name: str) -> Any:
    """PEP 562: re-export a name that moved into ``api/mcp_tools/<domain>.py``."""
    module_name = _EXTRACTED_REEXPORTS.get(name)
    if module_name is not None:
        from importlib import import_module

        return getattr(import_module(f"farna.api.mcp_tools.{module_name}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
