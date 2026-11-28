"""Connector-instance and model-backend MCP tools and resources.

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
from typing import Any, cast

from fastapi import HTTPException as FastAPIHTTPException
from sqlalchemy.exc import IntegrityError, ProgrammingError
from starlette.exceptions import HTTPException as StarletteHTTPException

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_DB_MIGRATION_REQUIRED_HEADS,
    _MSG_ERROR_TOKEN_REVOKED,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    _ctx_team_id_val,
    _resource_node_scope_error,
    _team_scope_error,
    _tool_auth_error,
    _tool_exception_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_user_id_val,
    _log,
    _parse_uuid_param,
    _session,
    _team_scoped_key_mismatch,
    create_secrets_backend,
    db_create_model_backend,
    get_settings,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import mask_config_json
from farna.core.mcp.scope_validator import MCPAuthorizationError
from farna.core.secrets_backend import (
    CredentialReferenceError,
    is_secret_ref,
    resolve_credential,
)


def _assert_create_model_backend_provider(provider: str) -> dict[str, Any] | None:
    """Validate the ``provider`` of create_model_backend; error dict or None.

    An unknown provider previously reached the DB check constraint and surfaced
    as internal_error; validate up-front (mirroring the REST 422) and call out
    the test-only 'stub' backend explicitly.
    """
    if provider == "stub":
        return {
            "error": "validation_error",
            "field": "provider",
            "detail": ("The 'stub' provider is a test double used by automated tests and is not supported via MCP. Choose a real provider, e.g. openai, anthropic, gemini, deepseek, groq, opencode."),
        }
    try:
        from farna.api.routes.model_backends import _validate_provider

        _validate_provider(provider)
    except StarletteHTTPException:
        return {
            "error": "validation_error",
            "field": "provider",
            "detail": f"Unknown model backend provider: {provider!r}",
        }
    except Exception:
        # The plugin registry path failed for an infra reason — the REST route
        # treats that the same way (422); log and mirror its verdict.
        _log.exception("create_model_backend provider validation failed")
        return {
            "error": "validation_error",
            "field": "provider",
            "detail": f"Unknown model backend provider: {provider!r}",
        }
    return None


@mcp.tool(
    description="Create a new model backend (provider configuration). "
    "The API key is NOT sent through this tool — instead, a one-time setup URL is returned. "
    "Open the URL in your browser to provide the API key directly. "
    "This keeps the secret out of the LLM context and MCP transport logs. "
    "Common providers include: openai, anthropic, gemini, deepseek, groq, opencode. "
    "The built-in 'stub' provider is a test double and cannot be created via MCP."
)
@mcp_audited("model_backend_created", "model_backend", fail_closed=False)
@_RETRY_DB
async def create_model_backend(
    name: str,
    display_name: str,
    provider: str,
    model_id: str,
    default_params: dict[str, Any] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_model_backend")

        if (err := _assert_create_model_backend_provider(provider)) is not None:
            return err

        from farna.core.mcp_setup_handoff import create_handoff

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            mb = await db_create_model_backend(
                s,
                org_id=org_id,
                name=name,
                display_name=display_name,
                provider=provider,
                model_id=model_id,
                credentials_ciphertext=b"",
                account_id=account_id,
                default_params=default_params or {},
                visibility=visibility,
                fallback_backend_ids=None,
                # FAR-2280: the setup route refuses unless the stored status is
                # "pending_setup"; the column default ("active") made every
                # setup URL return already_configured.
                status="pending_setup",
            )
            handoff = await create_handoff(
                s,
                org_id=org_id,
                resource_type="model-backend",
                resource_id=mb.id,
                created_by=account_id,
            )

        return {
            "id": str(mb.id),
            "name": mb.name,
            "display_name": mb.display_name,
            "provider": mb.provider,
            "model_id": mb.model_id,
            "status": mb.status,
            "visibility": mb.visibility,
            **handoff,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_model_backend failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("create_model_backend failed")
        return _tool_exception_error("Failed to create model backend", exc, "mcp.create_model_backend")


def _model_backend_item(mb: Any) -> dict[str, Any]:
    """Serialise a model backend row for the MCP read tools.

    Mirrors the REST ``model_backend.list`` surface: credential material is
    NEVER included — only a boolean presence flag.
    """
    return {
        "id": str(mb.id),
        "name": mb.name,
        "display_name": mb.display_name,
        "provider": mb.provider,
        "model_id": mb.model_id,
        "has_credentials": bool(mb.credentials_ciphertext),
        "default_params": mb.default_params or {},
        "visibility": mb.visibility,
        "owner_team_id": str(mb.owner_team_id) if mb.owner_team_id else None,
        "tier": mb.tier,
        "status": mb.status,
        "created_at": mb.created_at.isoformat() if mb.created_at else None,
        "updated_at": mb.updated_at.isoformat() if mb.updated_at else None,
    }


@mcp.tool(
    name="list_model_backends",
    description=("List model backends (LLM provider configurations) in the organisation with cursor-based pagination. Returns metadata only — never credential values."),
)
@_RETRY_DB
async def list_model_backends(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_model_backends")

        from farna.db.crud.model_backend import list_model_backends as db_list_model_backends

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_model_backends(s, org_id=org_id, cursor=cursor, page_size=lim, team_id=_ctx_team_id_val())

        return {
            "data": [_model_backend_item(mb) for mb in result.items],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_model_backends failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_model_backends failed")
        return _tool_exception_error("Failed to list model backends", exc, "mcp.list_model_backends")


@mcp.tool(
    description=("Get a single model backend by ID. Returns metadata only — never credential values."),
)
@_RETRY_DB
async def get_model_backend(model_backend_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_model_backend")

        org_id = _ctx_org_id_val()
        mid, mid_err = _parse_uuid_param(model_backend_id, "model_backend_id")
        if mid_err:
            return mid_err
        if mid is None:
            return {"error": "invalid_param", "detail": f"Invalid model_backend_id: {model_backend_id}"}

        from farna.db.crud.model_backend import get_model_backend as db_get_model_backend

        async with _session(org_id) as s:
            mb = await db_get_model_backend(s, mid)

        # Belt-and-braces org check (mirrors the REST get route) — RLS already
        # scopes the row, but a cross-org id must read as not_found even if
        # RLS were misconfigured.
        if mb is None or mb.organisation_id != org_id:
            return {"error": "not_found", "detail": f"Model backend {model_backend_id} not found"}
        # ADR 070 §5: a team-scoped key must not read another team's backend.
        if _team_scoped_key_mismatch(mb.owner_team_id):
            return _team_scope_error("model_backend", model_backend_id)

        item = _model_backend_item(mb)
        raw_fallback_ids = getattr(mb, "fallback_backend_ids", None)
        item["fallback_backend_ids"] = [str(fid) for fid in raw_fallback_ids] if raw_fallback_ids is not None else None
        item["cost_tracking"] = mb.cost_tracking
        item["currency"] = mb.currency
        return item
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_model_backend failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_model_backend failed")
        return _tool_exception_error("Failed to get model backend", exc, "mcp.get_model_backend")


def _parse_model_backend_update_args(
    *,
    fallback_backend_ids: list[str] | None,
    clear_fallback_chain: bool,
    default_params: dict[str, Any] | None,
) -> tuple[list[uuid.UUID] | None, dict[str, Any] | None]:
    """Validate ``update_model_backend``'s non-UUID arguments.

    Returns ``(fallback_ids, None)`` on success or ``(None, error_dict)``.
    """
    if fallback_backend_ids is not None and clear_fallback_chain:
        return None, {
            "error": "validation_error",
            "field": "fallback_backend_ids",
            "detail": "Supply fallback_backend_ids or clear_fallback_chain, not both",
        }
    if default_params is not None and "base_url" in default_params:
        return None, {
            "error": "validation_error",
            "field": "default_params.base_url",
            "detail": ("Changing a model backend endpoint requires the explicit credential-rebinding setup flow; base_url cannot be changed through MCP"),
        }
    if any(value is not None and not isinstance(value, str) for value in fallback_backend_ids or []):
        return None, {
            "error": "validation_error",
            "field": "fallback_backend_ids",
            "detail": "Fallback IDs must be strings",
        }
    try:
        parsed = [uuid.UUID(value) for value in fallback_backend_ids] if fallback_backend_ids is not None else None
    except ValueError as exc:
        return None, {"error": "invalid_id", "field": "fallback_backend_ids", "detail": str(exc)}
    return parsed, None


def _collect_model_backend_updates(
    *,
    name: str | None,
    display_name: str | None,
    model_id: str | None,
    default_params: dict[str, Any] | None,
    visibility: str | None,
    tier: str | None,
    fallback_ids: list[uuid.UUID] | None,
    clear_fallback_chain: bool,
) -> dict[str, Any]:
    """Collect the non-None fields the caller asked to change."""
    updates: dict[str, Any] = {
        field: value
        for field, value in (
            ("name", name),
            ("display_name", display_name),
            ("model_id", model_id),
            ("default_params", default_params),
            ("visibility", visibility),
            ("tier", tier),
        )
        if value is not None
    }
    if fallback_ids is not None or clear_fallback_chain:
        updates["fallback_backend_ids"] = [str(value) for value in (fallback_ids or [])]
    return updates


async def _apply_model_backend_update(
    org_id: uuid.UUID,
    backend_id: uuid.UUID,
    model_backend_id: str,
    updates: dict[str, Any],
    fallback_ids: list[uuid.UUID] | None,
) -> dict[str, Any]:
    """Validate and persist the update; returns the rendered item or an error dict."""
    from pydantic import ValidationError as PydanticValidationError

    from farna.api.routes.model_backends import ModelBackendUpdate, _validate_fallback_ids
    from farna.db.crud.model_backend import get_model_backend as db_get_model_backend
    from farna.db.crud.model_backend import update_model_backend as db_update_model_backend

    async with _session(org_id) as s:
        existing = await db_get_model_backend(s, backend_id)
        if existing is None or existing.organisation_id != org_id:
            return {"error": "not_found", "detail": f"Model backend {model_backend_id} not found"}
        # ADR 070 §5: a team-scoped key must not mutate another team's backend.
        if _team_scoped_key_mismatch(existing.owner_team_id):
            return _team_scope_error("model_backend", model_backend_id)

        # Reuse the HTTP PATCH request contract so MCP cannot bypass the
        # tier enum, the name/model_id bounds, or the team-visibility
        # invariant that the REST route enforces.
        validation_payload: dict[str, Any] = {key: value for key, value in updates.items() if key != "fallback_backend_ids"}
        # owner_team_id is not exposed by this tool: carry the existing
        # owner forward so an already-team backend stays valid, and let the
        # shared validator reject a new team visibility with no owner.
        validation_payload["owner_team_id"] = existing.owner_team_id if validation_payload.get("visibility") == "team" else None
        try:
            ModelBackendUpdate.model_validate(validation_payload)
        except PydanticValidationError as exc:
            return {"error": "validation_error", "detail": exc.errors(include_url=False)}

        if fallback_ids is not None:
            await _validate_fallback_ids(s, org_id, fallback_ids, backend_id=backend_id)
        updated = await db_update_model_backend(s, backend_id, updates)
        if updated is None:
            return {"error": "not_found", "detail": f"Model backend {model_backend_id} not found"}
        await s.refresh(updated)
    item = _model_backend_item(updated)
    item["fallback_backend_ids"] = [str(fid) for fid in (updated.fallback_backend_ids or [])]
    return item


@mcp.tool(
    name="update_model_backend",
    description=(
        "Update an existing model backend's non-secret configuration and replace or clear its fallback chain. "
        "Credentials are never accepted by this tool; use the one-time setup URL flow for credential changes. "
        "The fallback chain is replaced when fallback_backend_ids is supplied, or cleared with "
        "clear_fallback_chain=true."
    ),
)
@mcp_audited("model_backend_updated", "model_backend", fail_closed=False)
@_RETRY_DB
async def update_model_backend(
    model_backend_id: str,
    name: str | None = None,
    display_name: str | None = None,
    model_id: str | None = None,
    default_params: dict[str, Any] | None = None,
    visibility: str | None = None,
    tier: str | None = None,
    fallback_backend_ids: list[str] | None = None,
    clear_fallback_chain: bool = False,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_model_backend")
        backend_id, input_err = _parse_uuid_param(model_backend_id, "model_backend_id")
        if input_err:
            return input_err
        fallback_ids, arg_err = _parse_model_backend_update_args(
            fallback_backend_ids=fallback_backend_ids,
            clear_fallback_chain=clear_fallback_chain,
            default_params=default_params,
        )
        if arg_err is not None:
            return arg_err
        updates = _collect_model_backend_updates(
            name=name,
            display_name=display_name,
            model_id=model_id,
            default_params=default_params,
            visibility=visibility,
            tier=tier,
            fallback_ids=fallback_ids,
            clear_fallback_chain=clear_fallback_chain,
        )
        return await _apply_model_backend_update(
            _ctx_org_id_val(),
            cast(uuid.UUID, backend_id),
            model_backend_id,
            updates,
            fallback_ids,
        )
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except FastAPIHTTPException as exc:
        return {"error": "validation_error", "detail": exc.detail}
    except IntegrityError:
        # The DB uniqueness constraint on (organisation_id, name) is the
        # race-proof backstop; mirror the HTTP 409 already-exists contract
        # instead of leaking a generic internal error.
        _log.exception("update_model_backend name conflict")
        return {
            "error": "conflict",
            "field": "name",
            "detail": "A model backend with this name already exists in this organisation",
        }
    except ProgrammingError:
        _log.exception("update_model_backend failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("update_model_backend failed")
        return _tool_exception_error("Failed to update model backend", exc, "mcp.update_model_backend")


@mcp.tool(description="Create a new connector instance (provider configuration). Credentials are encrypted at rest. Returns the created connector details. Pass a literal credential, or a 'secretref://<key>' token to resolve the value from the organisation vault server-side at write time.")
@mcp_audited("connector_created", "connector", fail_closed=True)
@_RETRY_DB
async def create_connector(
    name: str,
    connector_type_id: str,
    credentials: str,
    config_json: dict[str, Any] | None = None,
    allowed_operations: list[str] | None = None,
    visibility: str = "org",
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_connector")

        from cryptography.fernet import Fernet

        from farna.db.crud.connector_instance import create_connector_instance

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()
        settings = get_settings()

        async with _session(org_id) as s:
            # FAR-1640: a ``secretref://<key>`` credential is resolved
            # SERVER-SIDE under the caller's org RLS context (``_session`` has
            # already set app.organisation_id) before it is encrypted — the
            # REST create path resolves identically. A literal passes through
            # untouched and never constructs a secrets backend (so the
            # historical path does not depend on the vault being configured). A
            # missing/foreign/empty key — or an external backend in a multi-org
            # deployment — fails closed as a typed validation error naming only
            # the offending key; the reference is never persisted as a literal
            # credential.
            if is_secret_ref(credentials):
                try:
                    resolved_credentials = await resolve_credential(
                        create_secrets_backend(fernet_key=settings.fernet_key, session=s),
                        credentials,
                    )
                except CredentialReferenceError as exc:
                    return {
                        "error": "validation_error",
                        "field": "credentials",
                        "detail": f"vault key {exc.key!r}: {exc.reason}",
                    }
            else:
                resolved_credentials = credentials

            credentials_ciphertext = Fernet(settings.fernet_key.encode()).encrypt(resolved_credentials.encode())
            ci = await create_connector_instance(
                s,
                org_id=org_id,
                name=name,
                connector_type_id=connector_type_id,
                account_id=account_id,
                credentials_ciphertext=credentials_ciphertext,
                config_json=config_json or {},
                allowed_operations=allowed_operations or [],
                visibility=visibility,
            )

        return {
            "id": str(ci.id),
            "name": ci.name,
            "connector_type_id": ci.connector_type_id,
            "visibility": ci.visibility,
            "status": "created",
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_connector failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("create_connector failed")
        return _tool_exception_error("Failed to create connector", exc, "mcp.create_connector")


@mcp.tool(description="Delete a connector instance by ID.")
@mcp_audited("connector_deleted", "connector", fail_closed=True)
@_RETRY_DB
async def delete_connector(
    connector_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_connector")

        org_id = _ctx_org_id_val()
        try:
            cid = uuid.UUID(connector_id)
        except ValueError:
            return {
                "error": "invalid_id",
                "field": "connector_id",
                "detail": f"Invalid UUID format: {connector_id}",
            }

        from farna.db.crud.connector_instance import delete_connector_instance as db_delete_connector
        from farna.db.crud.connector_instance import get_connector_instance as db_get_connector_instance

        async with _session(org_id) as s:
            # ADR 070 §5: a team-scoped key must not DELETE another team's
            # connector. Read first (same transaction) so the boundary is
            # applied to the row about to be removed, never to a stale lookup.
            existing = await db_get_connector_instance(s, cid)
            if existing is not None and _team_scoped_key_mismatch(existing.owner_team_id):
                return _team_scope_error("connector", connector_id)
            deleted = await db_delete_connector(s, cid)

        if not deleted:
            return {"error": "connector_not_found", "connector_id": connector_id}
        return {"status": "deleted", "connector_id": connector_id}

    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("delete_connector failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED_HEADS}
    except Exception as exc:
        _log.exception("delete_connector failed")
        return _tool_exception_error("Failed to delete connector", exc, "mcp.delete_connector")


def _connector_item(ci: Any) -> dict[str, Any]:
    """Serialise a connector instance row for the MCP read tools.

    Mirrors the REST ``connector.list`` surface: the credential ciphertext is
    NEVER included (only a ``has_credentials`` presence flag) and ``config_json``
    is passed through the shared sensitive-key mask.
    """
    return {
        "id": str(ci.id),
        "name": ci.name,
        "connector_type_id": ci.connector_type_id,
        "has_credentials": bool(ci.credentials_ciphertext),
        "config_json": mask_config_json(ci.config_json or {}),
        "allowed_operations": ci.allowed_operations or [],
        "status": ci.status,
        "visibility": ci.visibility,
        "owner_team_id": str(ci.owner_team_id) if ci.owner_team_id else None,
        "tier": ci.tier,
        "created_at": ci.created_at.isoformat() if ci.created_at else None,
        "updated_at": ci.updated_at.isoformat() if ci.updated_at else None,
    }


@mcp.tool(
    name="list_connectors",
    description=("List connector instances in the organisation with cursor-based pagination. Returns connector metadata (type, status, masked config) — never credential values."),
)
@_RETRY_DB
async def list_connectors(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_connectors")

        from farna.db.crud.connector_instance import list_connector_instances as db_list_connector_instances

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_connector_instances(
                s,
                organisation_id=org_id,
                page_size=lim,
                cursor=cursor,
                team_id=_ctx_team_id_val(),
            )

        return {
            "data": [_connector_item(ci) for ci in result.items],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_connectors failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_connectors failed")
        return _tool_exception_error("Failed to list connectors", exc, "mcp.list_connectors")


@mcp.tool(description="Get a single connector instance by ID. Returns metadata — never credential values.")
@_RETRY_DB
async def get_connector(connector_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_connector")

        org_id = _ctx_org_id_val()
        cid, cid_err = _parse_uuid_param(connector_id, "connector_id")
        if cid_err:
            return cid_err
        if cid is None:
            return {"error": "invalid_param", "detail": f"Invalid connector_id: {connector_id}"}

        from farna.db.crud.connector_instance import get_connector_instance as db_get_connector_instance

        async with _session(org_id) as s:
            ci = await db_get_connector_instance(s, cid)

        # Belt-and-braces org check (mirrors the REST get route) — RLS already
        # scopes the row, but a cross-org id must read as not_found even if
        # RLS were misconfigured.
        if ci is None or ci.organisation_id != org_id:
            return {"error": "not_found", "detail": f"Connector {connector_id} not found"}
        # ADR 070 §5: a team-scoped key must not read another team's connector.
        if _team_scoped_key_mismatch(ci.owner_team_id):
            return _team_scope_error("connector", connector_id)

        item = _connector_item(ci)
        item["degraded_at"] = ci.degraded_at.isoformat() if ci.degraded_at else None
        item["last_skip_error"] = ci.last_skip_error
        return item
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_connector failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_connector failed")
        return _tool_exception_error("Failed to get connector", exc, "mcp.get_connector")


def _connector_type_field_metadata(type_id: str) -> dict[str, Any] | None:
    """Credential/config FIELD metadata for a connector type (values never included).

    Sourced from the canonical library integration definitions (the single
    source of truth for the credential keys a connector type consumes). Best
    effort: returns None when the type has no library definition.
    """
    try:
        from farna.core.library.integrations import __all__ as _integration_exports
        from farna.core.library.integrations import definitions as _integration_defs

        for name in _integration_exports:
            definition = getattr(_integration_defs, name)
            if definition.get("connector_type") != type_id:
                continue
            credential_fields = definition.get("credential_fields") or {}
            return {
                "credential_fields": {key: {"required": bool(spec.get("required", False))} for key, spec in credential_fields.items()},
                "config_fields": sorted((definition.get("default_config") or {}).keys()),
            }
    except Exception:
        _log.debug("connector type field metadata unavailable for %s", type_id, exc_info=True)
    return None


@mcp.tool(
    name="list_connector_types",
    description=("List the registered connector type catalogue: id, display name, capabilities, and — for types with a canonical definition — the credential/config FIELD metadata each type consumes (field names and required flags only, never values)."),
)
async def list_connector_types() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_connector_types")

        # FAR-1772: advertise the hub-BUILDABLE catalogue — the same set
        # ``GET /api/v1/connectors/types`` offers (FAR-1764, the shared
        # ``buildable_connector_type_ids()`` helper). The raw ``ConnectorType``
        # enum also contains the abstract labels ``ci-runner`` / ``custom``
        # that ``_build_connector`` cannot instantiate, and omits the hub-native
        # CI-runner ids ``github_actions_ci`` / ``gitlab_ci``.
        from farna.connectors.base import (
            CI_RUNNER_CAPABILITIES,
            ConnectorType,
            connector_type_supports_dispatch,
        )
        from farna.core.connector_hub import buildable_connector_type_ids

        buildable = buildable_connector_type_ids()
        # Ordering mirrors the REST route: enum declaration order first, then
        # hub-native/plugin ids alphabetically.
        ordered: list[str] = [t.value for t in ConnectorType if t.value in buildable]
        ordered += sorted(buildable - set(ordered))

        items: list[dict[str, Any]] = []
        for type_id in ordered:
            try:
                capabilities = sorted(c.value for c in ConnectorType(type_id).capabilities)
            except ValueError:
                # Hub-native CI-runner ids (github_actions_ci / gitlab_ci)
                # implement the CI-runner capability contract; plugin-
                # registered types are not introspectable here (empty list).
                capabilities = sorted(c.value for c in CI_RUNNER_CAPABILITIES) if connector_type_supports_dispatch(type_id) else []
            item: dict[str, Any] = {
                "id": type_id,
                "name": type_id.replace("_", " ").title(),
                "capabilities": capabilities,
            }
            metadata = _connector_type_field_metadata(type_id)
            if metadata is not None:
                item["credential_fields"] = metadata["credential_fields"]
                item["config_fields"] = metadata["config_fields"]
            items.append(item)

        return {"data": items, "total": len(items)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except Exception as exc:
        _log.exception("list_connector_types failed")
        return _tool_exception_error("Failed to list connector types", exc, "mcp.list_connector_types")


@mcp.resource("modulo://connectors")
async def resource_connectors() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the list_connectors tool — same node-scope gate.
    denied = _resource_node_scope_error("list_connectors")
    if denied is not None:
        return denied
    from sqlalchemy import select

    from farna.db.models.connector_instance import ConnectorInstance

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id).order_by(ConnectorInstance.name))
        connectors = list(result.scalars())
    lines = [f"- {c.name} (id={c.id}, type={c.connector_type_id})" for c in connectors]
    return f"Connectors ({len(connectors)}):\n" + "\n".join(lines)


@mcp.resource("modulo://model-backends")
async def resource_model_backends() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the list_model_backends tool — same node-scope gate.
    denied = _resource_node_scope_error("list_model_backends")
    if denied is not None:
        return denied
    from sqlalchemy import select

    from farna.db.models.model_backend import ModelBackend

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id).order_by(ModelBackend.name))
        backends = list(result.scalars())
    lines = [f"- {b.name} (id={b.id}, {b.provider}/{b.model_id})" for b in backends]
    return f"Model Backends ({len(backends)}):\n" + "\n".join(lines)
