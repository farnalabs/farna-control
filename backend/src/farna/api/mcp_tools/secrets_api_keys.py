"""Secret-vault and organisation API-key MCP tools.

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

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, NamedTuple

from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _CODE_PERMISSION_API_KEY_ROLE_CAP,
    _MSG_CREATE_API_KEY_FAILED,
    _MSG_DB_MIGRATION_REQUIRED_HEADS,
    _MSG_DB_TEMPORARILY_UNAVAILABLE,
    _MSG_LIST_API_KEYS_FAILED,
    _MSG_REVOKE_API_KEY_FAILED,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    McpAuthContextError,
    _ctx_auth_type,
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
    _session,
    api_key_grants_enabled,
    auth_create_api_key,
    auth_list_api_keys,
    auth_revoke_api_key,
    get_settings,
    resolve_plan_context,
    resolve_role_from_membership,
    validate_current_auth,
)
from farna.api.routes.api_keys import enforce_grants_mint_cap_for
from farna.auth.api_key import (
    KEY_SCOPE_ORG,
    ApiKeyGrantsUnavailableError,
    ApiKeyScopeError,
    ApiKeyTtlError,
    api_key_role_violation,
)
from farna.auth.team_rbac import ORG_ROLE_HIERARCHY, org_role_level
from farna.core.mcp.scope_validator import MCPAuthorizationError


@mcp.tool(description="Create or update a secret in the organisation vault. Secrets are encrypted at rest and scoped to the organisation. Returns the created secret details.")
@mcp_audited("secret_created", "secret", fail_closed=True)
@_RETRY_DB
async def create_secret(
    key: str,
    value: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_secret")

        if not key or not key.strip():
            return {"error": "validation_failed", "field": "key", "detail": "Secret key is required"}
        if len(key) > 255:
            return {"error": "validation_failed", "field": "key", "detail": "Secret key exceeds 255 characters"}
        if not value:
            return {"error": "validation_failed", "field": "value", "detail": "Secret value is required"}

        org_id = _ctx_org_id_val()
        from farna.settings import get_settings

        settings = get_settings()
        from farna.core.secrets_backend import create_secrets_backend

        async with _session(org_id) as s:
            secrets_backend = create_secrets_backend(
                fernet_key=settings.fernet_key,
                session=s,
            )
            await secrets_backend.set_secret(key, value)

        return {"status": "created", "key": key}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_secret failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception as exc:
        _log.exception("create_secret failed")
        return _tool_exception_error("Failed to create secret", exc, "mcp.create_secret")


@mcp.tool(description="List all secret keys in the organisation vault. Returns secret keys and metadata — never exposes secret values.")
@_RETRY_DB
async def list_secrets(
    limit: int = 100,
    search: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_secrets")

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            from sqlalchemy import func, select

            from farna.db.models.secret import Secret

            query = select(Secret).where(Secret.organisation_id == org_id)
            if search:
                query = query.where(Secret.key.ilike(f"%{search}%"))
            query = query.order_by(Secret.key).limit(limit)

            result = await s.execute(query)
            secrets = result.scalars().all()

            count_query = select(func.count()).select_from(Secret).where(Secret.organisation_id == org_id)
            if search:
                count_query = count_query.where(Secret.key.ilike(f"%{search}%"))
            total = (await s.execute(count_query)).scalar() or 0

        return {
            "secrets": [
                {
                    "key": sec.key,
                    "created_at": sec.created_at.isoformat() if sec.created_at else None,
                    "updated_at": sec.updated_at.isoformat() if sec.updated_at else None,
                }
                for sec in secrets
            ],
            "total": total,
        }

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_secrets failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception as exc:
        _log.exception("list_secrets failed")
        return _tool_exception_error("Failed to list secrets", exc, "mcp.list_secrets")


@mcp.tool(description="Delete a secret from the organisation vault by key.")
@mcp_audited("secret_deleted", "secret", fail_closed=True)
@_RETRY_DB
async def delete_secret(
    key: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_secret")

        if not key or not key.strip():
            return {"error": "validation_failed", "field": "key", "detail": "Secret key is required"}

        org_id = _ctx_org_id_val()
        from farna.settings import get_settings

        settings = get_settings()
        from farna.core.secrets_backend import create_secrets_backend

        async with _session(org_id) as s:
            secrets_backend = create_secrets_backend(
                fernet_key=settings.fernet_key,
                session=s,
            )
            await secrets_backend.delete_secret(key)

        return {"status": "deleted", "key": key}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_secret failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception as exc:
        _log.exception("delete_secret failed")
        return _tool_exception_error("Failed to delete secret", exc, "mcp.delete_secret")


# ---------------------------------------------------------------------------
# Organisation API-key management (REST parity with /api/v1/api-keys)
# ---------------------------------------------------------------------------


def _parse_api_key_expires_at(value: str) -> datetime:
    """Parse an ISO datetime, normalising naive values to UTC (REST parity)."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _deny_break_glass_mint(session: AsyncSession, account_id: uuid.UUID) -> None:
    """REST-parity deny_break_glass_mint for MCP credential-minting tools.

    Break-glass accounts can never mint or revoke credentials (plan v17,
    API-key + long-lived deny). Mirrors the FastAPI dependency of the same
    name: load the account by primary key and deny outright when
    ``account.is_break_glass`` is True — for a break-glass account the shared
    ``is_break_glass_denied`` / ``is_break_glass_live`` decisions are ALWAYS
    true (live OR denied), so the predicate call is redundant. A missing
    account or a DB read failure raises (fail-closed) rather than silently
    allowing a mint.
    """
    from farna.db.models.account import Account

    account = await session.get(Account, account_id)
    # The guard below means `account.is_break_glass is True`: the union of the
    # shared `is_break_glass_denied` / `is_break_glass_live` decisions is then
    # ALWAYS true (plan v17 — break-glass can never mint, live OR denied), so
    # any break-glass account is denied outright.
    if account is not None and account.is_break_glass is True:
        _log.warning(
            "permission.break_glass_mint_denied",
            extra={"account_id": str(account_id)},
        )
        raise MCPAuthorizationError("Break-glass accounts cannot create or modify secrets/credentials")


async def _enforce_api_key_mint_cap(
    session: AsyncSession,
    account_id: uuid.UUID,
    org_id: uuid.UUID,
    requested_role: str,
) -> None:
    """Enforce the API-key role-cap: never mint above the caller's LIVE role.

    Mirrors ``_enforce_mint_cap`` in ``api/routes/api_keys.py`` — the live
    membership role is the authoritative source, so a runner cannot mint an
    operator key, an operator can mint operator/runner, and a removed or
    deactivated member's live role is None, denying the mint outright.
    """
    live_role = await resolve_role_from_membership(
        session,
        str(account_id),
        str(org_id),
    )
    if live_role is None:
        _log.warning(
            _CODE_PERMISSION_API_KEY_ROLE_CAP,
            extra={"requested_role": requested_role, "live_role": None},
        )
        raise MCPAuthorizationError("Active organisation membership required to manage API keys")
    if org_role_level(requested_role) > org_role_level(live_role):
        _log.warning(
            _CODE_PERMISSION_API_KEY_ROLE_CAP,
            extra={"requested_role": requested_role, "live_role": live_role},
        )
        raise MCPAuthorizationError(f"Cannot use role '{requested_role}' for an API key while your live role is '{live_role}'")


async def _require_admin_for_team_key(org_id: uuid.UUID) -> None:
    """REST parity for team-scoped keys: feature + admin guard before setting team_id."""
    async with _session(org_id) as s:
        ctx = await resolve_plan_context(get_settings(), s)
        if not ctx.feature_enabled("team_rbac"):
            raise MCPAuthorizationError("Team-scoped API keys require an upgraded plan")
    if ORG_ROLE_HIERARCHY.get(_ctx_role_val() or "", -1) < ORG_ROLE_HIERARCHY["admin"]:
        raise MCPAuthorizationError("Only admin users can perform this action")


def _validate_api_key_role_and_name(name: str, role: str, scope: str = KEY_SCOPE_ORG) -> dict[str, Any] | None:
    """Return a validation error dict for invalid role/name, or None.

    FAR-2281: the role ceiling is scope-aware, using the SAME shared predicate
    as the REST mint path (``api_key_role_violation``). The MCP mint tool pins
    ``scope`` to ``org`` (an MCP caller mints an org-scoped machine key), so
    ``admin`` stays prohibited there; a user-scoped key's admin ceiling is
    reachable only through the REST surface that can mint it. The parameter
    exists so both surfaces enforce one rule rather than two drifting copies.
    """
    role_violation = api_key_role_violation(role, scope)
    if role_violation is not None:
        return {
            "error": "validation_failed",
            "field": "role",
            "detail": role_violation,
        }
    if not name.strip():
        return {
            "error": "validation_failed",
            "field": "name",
            "detail": "API key name must not be blank",
        }
    return None


def _parse_api_key_expires(expires_at: str | None) -> tuple[datetime | None, dict[str, Any] | None]:
    """Parse and validate the expiry. Returns (datetime, error_or_None)."""
    if not expires_at:
        return (None, None)
    try:
        parsed = _parse_api_key_expires_at(expires_at)
    except ValueError:
        return (
            None,
            {
                "error": "validation_failed",
                "field": "expires_at",
                "detail": "expires_at must be a valid ISO-8601 datetime",
            },
        )
    if parsed <= datetime.now(UTC):
        return (
            None,
            {
                "error": "validation_failed",
                "field": "expires_at",
                "detail": "expires_at must be in the future",
            },
        )
    return (parsed, None)


async def _parse_api_key_team_id(team_id: str | None, org_id: uuid.UUID) -> tuple[uuid.UUID | None, dict[str, Any] | None]:
    """Parse and validate the team_id. Returns (team_uuid, error_or_None)."""
    if team_id is None:
        return (None, None)
    try:
        team_uuid = uuid.UUID(team_id)
    except ValueError:
        return (
            None,
            {
                "error": "invalid_id",
                "field": "team_id",
                "detail": f"Invalid UUID format: {team_id}",
            },
        )
    await _require_admin_for_team_key(org_id)
    return (team_uuid, None)


async def _emit_mcp_api_key_audit(
    *,
    org_id: uuid.UUID,
    resource_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any],
    log_context: str,
) -> None:
    """Emit an ``api_key_created`` / ``api_key_revoked`` audit event for MCP.

    FAR-620: parity with the REST surface, which has audited both events since
    PRD §8.12. The REST payloads always carry ``auth_type='jwt'``; MCP stamps
    the request's live credential class instead. Written in a fresh
    ``_session`` AFTER the mint/revoke transaction has committed (the same
    post-append pattern as ``_append_mcp_hitl_denial_audit``, which the REST
    routes implement via ``append_audit_event_isolated``). Best-effort: a
    failed audit append is logged and NEVER fails the completed operation.
    Exact event_type strings match the REST surface (``api_key_created`` /
    ``api_key_revoked``); NOTE the audit-viewer's filter options still say
    ``api_key.created`` / ``api_key.deleted`` (a pre-existing filter drift,
    tracked separately — do not "fix" the strings here).
    """
    try:
        from farna.core.audit_logger import append_audit_event

        async with _session(org_id) as s:
            try:
                actor_user_id = _ctx_user_id_val()
            except McpAuthContextError:
                actor_user_id = None
            await append_audit_event(
                s,
                org_id=org_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                resource_type="api_key",
                resource_id=resource_id,
                payload_json=payload,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(log_context, extra={"org_id": str(org_id), "resource_id": str(resource_id)})


class _CreateApiKeyParsed(NamedTuple):
    """Parsed arguments for ``create_api_key``."""

    name: str
    expires_at: datetime | None
    team_id: uuid.UUID | None


async def _validate_api_key_args(
    *,
    name: str,
    role: str,
    expires_at: str | None,
    team_id: str | None,
    grants: list[str] | None,
    org_id: uuid.UUID,
) -> _CreateApiKeyParsed | dict[str, Any]:
    """Validate ``create_api_key``'s arguments before it touches the database.

    Runs, in order: the role/name check, the optional expiry parse, the
    optional team parse (admin-gated for team keys), and the FAR-1477 grants
    gate — grants are accepted ONLY with the org flag ON (OFF => 422, never a
    silent downgrade to a legacy full-role key). The flag read is STRICT so a
    transient flag-read failure is not mistaken for "flag OFF" — it must
    surface as a retryable 503, matching the REST mint path (and this
    server's own grant-enforcement auth path).

    Returns the parsed arguments on success, or the first error envelope.
    """
    validation_error = _validate_api_key_role_and_name(name, role)
    if validation_error is not None:
        return validation_error

    name = name.strip()

    parsed_expires_at, expires_error = _parse_api_key_expires(expires_at)
    if expires_error is not None:
        return expires_error

    team_uuid, team_error = await _parse_api_key_team_id(team_id, org_id)
    if team_error is not None:
        return team_error

    if grants is not None:
        try:
            grants_on = await api_key_grants_enabled(org_id, strict=True)
        except ApiKeyGrantsUnavailableError:
            _log.warning("mcp.create_api_key_grants_unavailable")
            return {
                "error": "service_unavailable",
                "status": 503,
                "detail": "API key grant settings are temporarily unavailable; retry shortly",
            }
        if not grants_on:
            return {
                "error": "validation_error",
                "status": 422,
                "detail": "API key grant-sets are not enabled for this organisation",
            }

    return _CreateApiKeyParsed(name, parsed_expires_at, team_uuid)


async def _mint_api_key_txn(
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    name: str,
    role: str,
    team_uuid: uuid.UUID | None,
    parsed_expires_at: datetime | None,
    grants: list[str] | None,
) -> tuple[Any, str] | dict[str, Any]:
    """Mint the API key inside its own session (caps + break-glass denial).

    Returns ``(key, full_key)`` on success, or an error envelope when a mint
    cap refuses the request.
    """
    async with _session(org_id) as s:
        await _deny_break_glass_mint(s, account_id)
        await _enforce_api_key_mint_cap(s, account_id, org_id, role)
        if grants is not None:
            try:
                await enforce_grants_mint_cap_for(s, account_id, org_id, grants)
            except FastAPIHTTPException as exc:
                if exc.status_code == 422:
                    return {"error": "validation_error", "status": 422, "detail": str(exc.detail)}
                raise MCPAuthorizationError(str(exc.detail)) from None
        try:
            key, full_key = await auth_create_api_key(
                s,
                org_id=org_id,
                name=name,
                role=role,
                account_id=account_id,
                team_id=team_uuid,
                expires_at=parsed_expires_at,
                grants=grants,
            )
        except (ApiKeyScopeError, ApiKeyTtlError) as exc:
            # FAR-1478 / FAR-2281: a >365d expiry on a service-owned key, or a
            # role not permitted for the key's scope (admin on a team-scoped
            # key) — client errors with the same 422 shape as the REST surface,
            # never a silent clamp.
            return {"error": "validation_error", "status": 422, "detail": str(exc)}
    return key, full_key


def _api_key_created_audit_payload(
    *,
    key: Any,
    name: str,
    role: str,
    team_uuid: uuid.UUID | None,
    grants: list[str] | None,
) -> dict[str, Any]:
    """Build the FAR-620 ``api_key_created`` audit payload stamps."""
    return {
        "name": name,
        "role": role,
        "team_id": str(team_uuid) if team_uuid else None,
        "auth_type": _ctx_auth_type.get(None) or "unknown",
        # MCP minting is org-only (caller-scope org-only pin) — the
        # tool can never produce a user-scoped key.
        "key_scope": "org",
        "lookup_prefix": f"mk_{key.lookup_prefix}****",
        **({"grants": sorted(grants)} if grants is not None else {}),
    }


def _api_key_created_response(key: Any, full_key: str, grants: list[str] | None) -> dict[str, Any]:
    """Serialize a freshly minted API key to the ``create_api_key`` shape."""
    return {
        "id": str(key.id),
        "name": key.name,
        "role": key.role,
        "key_value": full_key,
        "lookup_prefix": f"mk_{key.lookup_prefix}****",
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "team_id": str(key.team_id) if key.team_id else None,
        "grants": sorted(grants) if grants is not None else None,
    }


@mcp.tool(
    description=(
        "Create a new organisation API key. Returns the full mk_... key value "
        "ONLY at creation — store it immediately, it is never returned again. "
        "Mirrors POST /api/v1/api-keys. Roles: 'operator' or 'runner' (this "
        "tool mints an ORG-scoped key, so 'admin' is not available here; a "
        "user-scoped key may carry 'admin' and is minted via the REST surface). A key "
        "cannot be minted above the caller's live org role. Optional 'grants' "
        "(list of permission names, e.g. ['pipeline.list', 'pipeline.graph.read']) "
        "restricts the key to exactly those permissions instead of its full role "
        "bundle; omit it for the legacy role bundle, pass [] to deny everything. "
        "Requires the org's api_key_grants flag; grants must be delegable and "
        "within your own live capability. NOTE: over MCP, every read-only tool is "
        "gated by the single coarse key 'resource.read_only' (include it to allow "
        "MCP reads; fine-grained read keys like 'pipeline.list' do NOT apply to "
        "MCP read tools), whereas REST enforces the fine-grained keys exactly."
    ),
)
@_RETRY_DB
async def create_api_key(
    name: str,
    role: str = "operator",
    expires_at: str | None = None,
    team_id: str | None = None,
    grants: list[str] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_api_key")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        args = await _validate_api_key_args(
            name=name,
            role=role,
            expires_at=expires_at,
            team_id=team_id,
            grants=grants,
            org_id=org_id,
        )
        if isinstance(args, dict):
            return args

        outcome = await _mint_api_key_txn(
            org_id=org_id,
            account_id=account_id,
            name=args.name,
            role=role,
            team_uuid=args.team_id,
            parsed_expires_at=args.expires_at,
            grants=grants,
        )
        if isinstance(outcome, dict):
            return outcome
        key, full_key = outcome

        # FAR-620: parity with the REST mint audit (PRD §8.12) + payload stamps.
        await _emit_mcp_api_key_audit(
            org_id=org_id,
            resource_id=key.id,
            event_type="api_key_created",
            payload=_api_key_created_audit_payload(key=key, name=args.name, role=role, team_uuid=args.team_id, grants=grants),
            log_context="mcp.create_api_key_audit_failed",
        )

        return _api_key_created_response(key, full_key, grants)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return {"error": "conflict", "detail": "A resource with this value already exists"}
    except ProgrammingError:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.create_api_key")) is not None:
            return contract_error
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE, code="database_unavailable")
    except Exception as exc:
        _log.exception(_MSG_CREATE_API_KEY_FAILED)
        return _tool_exception_error("Failed to create API key", exc, "mcp.create_api_key")


@mcp.tool(
    description=(
        "List API keys in the organisation. Requires an operator or admin caller. "
        "Returns id/name/role/scope/lookup_prefix/last_used_at/created_at/"
        "expires_at/revoked_at/is_active/grants plus account_kind, "
        "account_display_name, and account_email (null for service accounts). "
        "Never returns key_value or hashed_secret. Mirrors GET /api/v1/api-keys."
    ),
)
@_RETRY_DB
async def list_api_keys() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_api_keys")

        org_id = _ctx_org_id_val()
        if ORG_ROLE_HIERARCHY.get(_ctx_role_val() or "", -1) < ORG_ROLE_HIERARCHY["operator"]:
            raise MCPAuthorizationError("Only admin or operator users can list API keys")

        async with _session(org_id) as s:
            keys = await auth_list_api_keys(s, org_id)

        return {"api_keys": keys, "total": len(keys)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.list_api_keys")) is not None:
            return contract_error
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE, code="database_unavailable")
    except Exception as exc:
        _log.exception(_MSG_LIST_API_KEYS_FAILED)
        return _tool_exception_error("Failed to list API keys", exc, "mcp.list_api_keys")


@mcp.tool(
    description=("Revoke an API key by ID. The key is immediately invalidated and can no longer authenticate. Mirrors DELETE /api/v1/api-keys/{key_id}."),
)
@_RETRY_DB
async def revoke_api_key(key_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("revoke_api_key")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        try:
            kid = uuid.UUID(key_id)
        except ValueError:
            return {
                "error": "invalid_id",
                "field": "key_id",
                "detail": f"Invalid UUID format: {key_id}",
            }

        async with _session(org_id) as s:
            await _deny_break_glass_mint(s, account_id)
            revoked_key = await auth_revoke_api_key(s, kid, org_id)

        if not revoked_key:
            return {"error": "not_found", "detail": "API key not found"}

        # FAR-620: parity with the REST revoke audit (PRD §8.12) + payload
        # stamps (the revoked row supplies the key's scope + masked prefix).
        await _emit_mcp_api_key_audit(
            org_id=org_id,
            resource_id=kid,
            event_type="api_key_revoked",
            payload={
                "revoked_by": str(account_id),
                "auth_type": _ctx_auth_type.get(None) or "unknown",
                "key_scope": revoked_key.scope,
                "lookup_prefix": f"mk_{revoked_key.lookup_prefix}****",
            },
            log_context="mcp.revoke_api_key_audit_failed",
        )
        return {"id": str(kid), "revoked": True}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return {"error": "conflict", "detail": "A resource with this value already exists"}
    except ProgrammingError:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.revoke_api_key")) is not None:
            return contract_error
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return _tool_error(_MSG_DB_TEMPORARILY_UNAVAILABLE, code="database_unavailable")
    except Exception as exc:
        _log.exception(_MSG_REVOKE_API_KEY_FAILED)
        return _tool_exception_error("Failed to revoke API key", exc, "mcp.revoke_api_key")
