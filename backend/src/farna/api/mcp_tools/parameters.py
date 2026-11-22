"""Parameter schema and parameter set MCP tools.

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

import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _CODE_MCP_UPDATE_PARAMETER_SCHEMA,
    _CODE_MCP_UPDATE_PARAMETER_SET,
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_DB_OPERATION_FAILED,
    _MSG_PARAM_SCHEMA_NOT_FOUND,
    _MSG_PARAM_SCHEMA_NOT_FOUND_OR_DELETED,
    _MSG_PARAM_SET_NOT_FOUND,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    _log_tool_failure,
    _tool_auth_error,
    _tool_exception_error,
    _tool_session_contract_error,
    _tool_sqlalchemy_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_user_id_val,
    _log,
    _session,
    validate_current_auth,
)
from farna.core.mcp.scope_validator import MCPAuthorizationError


@mcp.tool(
    name="list_parameter_schemas",
    description=("List parameter schemas in the organisation with cursor-based pagination. Returns the schema metadata and parameter definitions."),
)
@_RETRY_DB
async def list_parameter_schemas(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_parameter_schemas")

        from farna.db.crud.parameter_schema import list_schemas as db_list_parameter_schemas

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_parameter_schemas(s, org_id=org_id, cursor=cursor, limit=lim)

        return {
            "data": [
                {
                    "id": str(ps.id),
                    "name": ps.name,
                    "description": ps.description,
                    "version": ps.version,
                    "parameters": ps.parameters or [],
                    "created_at": ps.created_at.isoformat() if ps.created_at else None,
                }
                for ps in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_parameter_schemas failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_parameter_schemas failed")
        return _tool_exception_error("Failed to list parameter schemas", exc, "mcp.list_parameter_schemas")


def _schema_to_dict(schema: Any, *, include_updated: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(schema.id),
        "name": schema.name,
        "description": schema.description,
        "version": schema.version,
        "parameters": schema.parameters or [],
        "created_at": schema.created_at.isoformat() if schema.created_at else None,
    }
    if include_updated:
        data["updated_at"] = schema.updated_at.isoformat() if schema.updated_at else None
    return data


def _set_to_dict(ps: Any, *, include_updated: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(ps.id),
        "parameter_schema_id": str(ps.parameter_schema_id),
        "name": ps.name,
        "description": ps.description,
        "version": ps.version,
        "schema_version": ps.schema_version,
        "values": ps.values or {},
        "created_at": ps.created_at.isoformat() if ps.created_at else None,
    }
    if include_updated:
        data["updated_at"] = ps.updated_at.isoformat() if ps.updated_at else None
    return data


@mcp.tool(
    description="Create a new parameter schema. Returns the created schema details.",
)
@mcp_audited("parameter_schema_created", "parameter_schema", fail_closed=False)
@_RETRY_DB
async def create_parameter_schema(
    name: str,
    description: str | None = None,
    parameters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_parameter_schema")

        from farna.db.crud.parameter_schema import create_schema as db_create_ps

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            schema = await db_create_ps(
                s,
                org_id=org_id,
                name=name,
                description=description,
                parameters=parameters or [],
                account_id=account_id,
            )

        return {"data": _schema_to_dict(schema, include_updated=False)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log_tool_failure("create_parameter_schema")
        return {"error": "conflict", "detail": "A parameter schema with this name already exists."}
    except ProgrammingError:
        _log_tool_failure("create_parameter_schema")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.create_parameter_schema")) is not None:
            return contract_error
        _log_tool_failure("create_parameter_schema")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("create_parameter_schema")
        return _tool_exception_error("Failed to create parameter schema", exc, "mcp.create_parameter_schema")


@mcp.tool(
    description="Get a single parameter schema by ID, including its parameter definitions.",
)
@_RETRY_DB
async def get_parameter_schema(
    schema_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_parameter_schema")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)

        if schema is None or schema.organisation_id != org_id:
            return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

        return {"data": _schema_to_dict(schema)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_parameter_schema failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_parameter_schema failed")
        return _tool_exception_error("Failed to get parameter schema", exc, "mcp.get_parameter_schema")


@mcp.tool(
    description="Update a parameter schema. Requires the current version for optimistic concurrency.",
)
@mcp_audited("parameter_schema_updated", "parameter_schema", fail_closed=False)
@_RETRY_DB
async def update_parameter_schema(
    schema_id: str,
    version: int,
    name: str | None = None,
    description: str | None = None,
    parameters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_parameter_schema")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_schema import update_schema as db_update_ps

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            existing = await db_get_ps(s, sid)
            if existing is None or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            schema = await db_update_ps(
                s,
                sid,
                name=name,
                description=description,
                parameters=parameters,
                version=version,
            )

        if schema is None:
            return {"error": "conflict", "detail": "Schema was modified by another user. Refresh and retry."}

        return {"data": _schema_to_dict(schema)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log_tool_failure("update_parameter_schema")
        return {"error": "conflict", "detail": "A parameter schema with this name already exists."}
    except ProgrammingError:
        _log_tool_failure("update_parameter_schema")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        # FAR-1482: a session-contract violation is a programming bug, never a
        # database outage — classify it before the FAR-1825 lock/outage ladder.
        if (contract_error := _tool_session_contract_error(exc, _CODE_MCP_UPDATE_PARAMETER_SCHEMA)) is not None:
            return contract_error
        _log_tool_failure("update_parameter_schema")
        # FAR-1825: a bounded row-lock wait (55P03) is a busy-row conflict.
        return _tool_sqlalchemy_error(exc, _CODE_MCP_UPDATE_PARAMETER_SCHEMA)
    except Exception as exc:
        _log_tool_failure("update_parameter_schema")
        return _tool_exception_error("Failed to update parameter schema", exc, _CODE_MCP_UPDATE_PARAMETER_SCHEMA)


@mcp.tool(
    description="Soft-delete a parameter schema by ID.",
)
@mcp_audited("parameter_schema_deleted", "parameter_schema", fail_closed=True)
@_RETRY_DB
async def delete_parameter_schema(
    schema_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_parameter_schema")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_schema import soft_delete_schema as db_delete_ps

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            existing = await db_get_ps(s, sid)
            if existing is None or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            schema = await db_delete_ps(s, sid)

        if schema is None:
            return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

        return {
            "data": {
                "id": str(schema.id),
                "name": schema.name,
                "deleted": True,
            }
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log_tool_failure("delete_parameter_schema")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.delete_parameter_schema")) is not None:
            return contract_error
        _log_tool_failure("delete_parameter_schema")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("delete_parameter_schema")
        return _tool_exception_error("Failed to delete parameter schema", exc, "mcp.delete_parameter_schema")


@mcp.tool(
    description="Restore a soft-deleted parameter schema by ID.",
)
@mcp_audited("parameter_schema_restored", "parameter_schema", fail_closed=False)
@_RETRY_DB
async def restore_parameter_schema(
    schema_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("restore_parameter_schema")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_schema import restore_schema as db_restore_ps

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            existing = await db_get_ps(s, sid)
            if existing is None or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND_OR_DELETED}

            schema = await db_restore_ps(s, sid)

        if schema is None:
            return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND_OR_DELETED}

        return {
            "data": {
                "id": str(schema.id),
                "name": schema.name,
                "version": schema.version,
                "parameters": schema.parameters or [],
                "created_at": schema.created_at.isoformat() if schema.created_at else None,
                "updated_at": schema.updated_at.isoformat() if schema.updated_at else None,
            }
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log_tool_failure("restore_parameter_schema")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.restore_parameter_schema")) is not None:
            return contract_error
        _log_tool_failure("restore_parameter_schema")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("restore_parameter_schema")
        return _tool_exception_error("Failed to restore parameter schema", exc, "mcp.restore_parameter_schema")


@mcp.tool(
    description="Get references (agents, sets) that use a parameter schema.",
)
@_RETRY_DB
async def get_parameter_schema_references(
    schema_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_parameter_schema_references")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_schema import get_schema_references as db_get_refs

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            refs = await db_get_refs(s, sid)

        return {
            "data": {
                "agents": [{"id": str(a)} for a in refs["agents"]],
                "sets": [{"id": str(s_id)} for s_id in refs["sets"]],
            }
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_parameter_schema_references failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_parameter_schema_references failed")
        return _tool_exception_error("Failed to get parameter schema references", exc, "mcp.get_parameter_schema_references")


@mcp.tool(
    description="Validate parameter values against a parameter schema. Returns validation errors or success.",
)
@_RETRY_DB
async def validate_parameter_schema(
    schema_id: str,
    values: dict[str, Any],
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("validate_parameter_schema")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.api.routes.parameter_schemas import validate_parameter_values
        from farna.db.crud.parameter_schema import get_schema as db_get_ps

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            errors_items = validate_parameter_values(schema.parameters, values)
            errors = [{"field": e.field, "message": e.message} for e in errors_items]

        return {"data": {"valid": len(errors_items) == 0, "errors": errors}}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("validate_parameter_schema failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("validate_parameter_schema failed")
        return _tool_exception_error("Failed to validate parameter schema", exc, "mcp.validate_parameter_schema")


# ---------------------------------------------------------------------------
# Parameter Set CRUD MCP tools (nested under schema)
# ---------------------------------------------------------------------------


@mcp.tool(
    description="List parameter sets for a given parameter schema.",
)
@_RETRY_DB
async def list_parameter_sets(
    schema_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_parameter_sets")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_set import list_sets as db_list_sets

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            sets = await db_list_sets(s, parameter_schema_id=sid, org_id=org_id)

        return {"data": [_set_to_dict(ps) for ps in sets]}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_parameter_sets failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_parameter_sets failed")
        return _tool_exception_error("Failed to list parameter sets", exc, "mcp.list_parameter_sets")


@mcp.tool(
    description="Create a new parameter set under a parameter schema. Returns the created set details.",
)
@mcp_audited("parameter_set_created", "parameter_set", fail_closed=False)
@_RETRY_DB
async def create_parameter_set(
    schema_id: str,
    name: str,
    description: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_parameter_set")

        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_set import create_set as db_create_set

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            ps = await db_create_set(
                s,
                parameter_schema_id=sid,
                org_id=org_id,
                name=name,
                description=description,
                values=values or {},
                account_id=account_id,
                schema_version=schema.version,
            )

        return {"data": _set_to_dict(ps, include_updated=False)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log_tool_failure("create_parameter_set")
        return {"error": "conflict", "detail": "A parameter set with this name already exists for this schema."}
    except ProgrammingError:
        _log_tool_failure("create_parameter_set")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.create_parameter_set")) is not None:
            return contract_error
        _log_tool_failure("create_parameter_set")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("create_parameter_set")
        return _tool_exception_error("Failed to create parameter set", exc, "mcp.create_parameter_set")


@mcp.tool(
    description="Get a single parameter set by ID under a parameter schema.",
)
@_RETRY_DB
async def get_parameter_set(
    schema_id: str,
    set_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_parameter_set")

        try:
            sid = uuid.UUID(schema_id)
            setid = uuid.UUID(set_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id} or {set_id}"}

        from farna.db.crud.parameter_set import get_set as db_get_set

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            ps = await db_get_set(s, setid)

        if ps is None or ps.parameter_schema_id != sid or ps.organisation_id != org_id:
            return {"error": "not_found", "detail": _MSG_PARAM_SET_NOT_FOUND}

        return {"data": _set_to_dict(ps)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_parameter_set failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_parameter_set failed")
        return _tool_exception_error("Failed to get parameter set", exc, "mcp.get_parameter_set")


@mcp.tool(
    description="Update a parameter set. Requires the current version for optimistic concurrency.",
)
@mcp_audited("parameter_set_updated", "parameter_set", fail_closed=False)
@_RETRY_DB
async def update_parameter_set(
    schema_id: str,
    set_id: str,
    version: int,
    name: str | None = None,
    description: str | None = None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("update_parameter_set")

        try:
            sid = uuid.UUID(schema_id)
            setid = uuid.UUID(set_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id} or {set_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_set import get_set as db_get_set
        from farna.db.crud.parameter_set import update_set as db_update_set

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            existing = await db_get_set(s, setid)
            if existing is None or existing.parameter_schema_id != sid or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SET_NOT_FOUND}

            ps = await db_update_set(
                s,
                setid,
                name=name,
                description=description,
                values=values,
                version=version,
            )

        if ps is None:
            return {"error": "conflict", "detail": "Parameter set was modified by another user. Refresh and retry."}

        return {"data": _set_to_dict(ps)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError:
        _log_tool_failure("update_parameter_set")
        return {"error": "conflict", "detail": "A parameter set with this name already exists."}
    except ProgrammingError:
        _log_tool_failure("update_parameter_set")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        # FAR-1482: a session-contract violation is a programming bug, never a
        # database outage — classify it before the FAR-1825 lock/outage ladder.
        if (contract_error := _tool_session_contract_error(exc, _CODE_MCP_UPDATE_PARAMETER_SET)) is not None:
            return contract_error
        _log_tool_failure("update_parameter_set")
        # FAR-1825: a bounded row-lock wait (55P03) is a busy-row conflict.
        return _tool_sqlalchemy_error(exc, _CODE_MCP_UPDATE_PARAMETER_SET)
    except Exception as exc:
        _log_tool_failure("update_parameter_set")
        return _tool_exception_error("Failed to update parameter set", exc, _CODE_MCP_UPDATE_PARAMETER_SET)


@mcp.tool(
    description="Soft-delete a parameter set by ID under a parameter schema.",
)
@mcp_audited("parameter_set_deleted", "parameter_set", fail_closed=True)
@_RETRY_DB
async def delete_parameter_set(
    schema_id: str,
    set_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("delete_parameter_set")

        try:
            sid = uuid.UUID(schema_id)
            setid = uuid.UUID(set_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id} or {set_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_set import get_set as db_get_set
        from farna.db.crud.parameter_set import soft_delete_set as db_delete_set

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND}

            existing = await db_get_set(s, setid)
            if existing is None or existing.parameter_schema_id != sid or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SET_NOT_FOUND}

            ps = await db_delete_set(s, setid)

        if ps is None:
            return {"error": "not_found", "detail": _MSG_PARAM_SET_NOT_FOUND}

        return {
            "data": {
                "id": str(ps.id),
                "name": ps.name,
                "deleted": True,
            }
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log_tool_failure("delete_parameter_set")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.delete_parameter_set")) is not None:
            return contract_error
        _log_tool_failure("delete_parameter_set")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("delete_parameter_set")
        return _tool_exception_error("Failed to delete parameter set", exc, "mcp.delete_parameter_set")


@mcp.tool(
    description="Restore a soft-deleted parameter set by ID under a parameter schema.",
)
@mcp_audited("parameter_set_restored", "parameter_set", fail_closed=False)
@_RETRY_DB
async def restore_parameter_set(
    schema_id: str,
    set_id: str,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("restore_parameter_set")

        try:
            sid = uuid.UUID(schema_id)
            setid = uuid.UUID(set_id)
        except ValueError:
            return {"error": "invalid_id", "detail": f"Invalid UUID format: {schema_id} or {set_id}"}

        from farna.db.crud.parameter_schema import get_schema as db_get_ps
        from farna.db.crud.parameter_set import get_set as db_get_set
        from farna.db.crud.parameter_set import restore_set as db_restore_set

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            schema = await db_get_ps(s, sid)
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": _MSG_PARAM_SCHEMA_NOT_FOUND_OR_DELETED}

            existing = await db_get_set(s, setid)
            if existing is None or existing.parameter_schema_id != sid or existing.organisation_id != org_id:
                return {"error": "not_found", "detail": "Parameter set not found or not deleted"}

            ps = await db_restore_set(s, setid)

        if ps is None:
            return {"error": "not_found", "detail": "Parameter set not found or not deleted"}

        return {"data": _set_to_dict(ps)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log_tool_failure("restore_parameter_set")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.restore_parameter_set")) is not None:
            return contract_error
        _log_tool_failure("restore_parameter_set")
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log_tool_failure("restore_parameter_set")
        return _tool_exception_error("Failed to restore parameter set", exc, "mcp.restore_parameter_set")
