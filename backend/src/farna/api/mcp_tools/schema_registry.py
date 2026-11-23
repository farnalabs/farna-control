"""JSON schema registry MCP tools and resources.

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
from typing import Any

from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_CREATE_SCHEMA_FAILED,
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_DB_OPERATION_FAILED,
    _MSG_ERROR_TOKEN_REVOKED,
    _MSG_TOKEN_REVOKED,
    _MSG_UUID_PARSE_FAILED,
    _RETRY_DB,
    _check_node_allowed_tools,
    _resource_node_scope_error,
    _tool_auth_error,
    _tool_error,
    _tool_exception_error,
    _tool_session_contract_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _ctx_user_id_val,
    _log,
    _parse_uuid_param,
    _require_plan_feature,
    _session,
    db_create_schema,
    db_list_schemas,
    get_schema,
    get_settings,
    set_rls_org,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import mask_config_json
from farna.core.mcp.scope_validator import MCPAuthorizationError


@mcp.tool(
    name="get_schema_versions",
    description=("List the version history of a schema (mirrors GET /api/v1/schemas/{schema_id}/versions)."),
)
@_RETRY_DB
async def get_schema_versions(
    schema_id: str,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_schema_versions")

        from farna.db.crud.schema import get_schema, list_schema_versions

        org_id = _ctx_org_id_val()
        # Plan gate parity with GET /api/v1/schemas/{schema_id}/versions:
        # version history is the ``schema_version_history`` entitlement, enforced
        # there by ``require_feature``. MCP must enforce it too rather than stay
        # an unentitled way in (FAR-2129).
        feat_err = await _require_plan_feature(org_id, get_settings(), "schema_version_history")
        if feat_err:
            return feat_err

        schid, schid_err = _parse_uuid_param(schema_id, "schema_id")
        if schid_err:
            return schid_err
        if schid is None:
            return {"error": "invalid_id", "field": "schema_id", "detail": _MSG_UUID_PARSE_FAILED}
        pg = max(1, page)
        ps = max(1, min(page_size, 100))

        async with _session(org_id) as s:
            schema = await get_schema(s, schid)
            # Belt-and-braces org check (mirrors the REST route's 404).
            if schema is None or schema.organisation_id != org_id:
                return {"error": "not_found", "detail": f"Schema {schema_id} not found"}
            result = await list_schema_versions(s, schid, page=pg, page_size=ps)

        return {
            "data": [
                {
                    "id": str(sv.id),
                    "schema_id": str(sv.schema_id),
                    "version": sv.version,
                    "version_number": sv.version_number,
                    "definition_json": sv.definition_json,
                    "published": sv.published,
                    "created_by": str(sv.account_id) if sv.account_id else None,
                    "created_at": sv.created_at.isoformat() if sv.created_at else None,
                    "updated_at": sv.updated_at.isoformat() if sv.updated_at else None,
                }
                for sv in result.items
            ],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_schema_versions failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_schema_versions failed")
        return _tool_exception_error("Failed to list schema versions", exc, "mcp.get_schema_versions")


@mcp.tool(
    description="Create a new schema. Creates the schema record plus a 'latest' version placeholder so agents can reference the schema immediately. Returns the created schema details.",
)
@mcp_audited("schema_created", "schema", fail_closed=False)
@_RETRY_DB
async def create_schema(
    name: str,
    description: str | None = None,
    abstract_name: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_schema")

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        async with _session(org_id) as s:
            schema = await db_create_schema(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                description=description,
                abstract_name=abstract_name,
            )

            from farna.db.models.schema import SchemaVersion

            s.add(
                SchemaVersion(
                    organisation_id=org_id,
                    schema_id=schema.id,
                    version="latest",
                    version_number=0,
                    definition_json={"type": "object", "properties": {}, "additionalProperties": True},
                    account_id=account_id,
                )
            )

        return {
            "id": str(schema.id),
            "name": schema.name,
            "description": schema.description,
            "abstract_name": schema.abstract_name,
            "created_at": schema.created_at.isoformat() if schema.created_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except IntegrityError as exc:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "conflict", "detail": f"A schema with this name already exists: {exc.orig}"}
    except ProgrammingError:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except SQLAlchemyError as exc:
        if (contract_error := _tool_session_contract_error(exc, "mcp.create_schema")) is not None:
            return contract_error
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return {"error": "database_unavailable", "detail": _MSG_DB_OPERATION_FAILED}
    except Exception as exc:
        _log.exception(_MSG_CREATE_SCHEMA_FAILED)
        return _tool_exception_error("Failed to create schema", exc, "mcp.create_schema")


@mcp.tool(
    description="List registered schemas with cursor-based pagination. Returns schema metadata.",
)
@_RETRY_DB
async def list_schemas(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("list_schemas")
        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_schemas(s, cursor=cursor, limit=lim)

        return {
            "data": [
                {
                    "id": str(sc.id),
                    "name": sc.name,
                    "description": sc.description,
                    "version": sc.abstract_name,
                    "created_at": sc.created_at.isoformat() if sc.created_at else None,
                }
                for sc in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_schemas failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_schemas failed")
        return _tool_exception_error("Failed to list schemas", exc, "mcp.list_schemas")


@mcp.tool(
    name="list_environment_profiles",
    description=("List environment profiles (execution environments: local_docker, e2b, etc.) in the organisation with cursor-based pagination. Config values under sensitive keys are masked; secret_refs are vault key references, never secret values."),
)
@_RETRY_DB
async def list_environment_profiles(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_environment_profiles")

        from farna.db.crud.environment_profile import list_environment_profiles as db_list_environment_profiles

        org_id = _ctx_org_id_val()
        # Plan gate parity with GET /api/v1/environment-profiles: environment
        # profiles are the ``environment_profiles`` entitlement, enforced there
        # by ``require_feature``. MCP must enforce it too rather than stay an
        # unentitled way in (FAR-2129).
        feat_err = await _require_plan_feature(org_id, get_settings(), "environment_profiles")
        if feat_err:
            return feat_err

        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_environment_profiles(s, cursor=cursor, page_size=lim)

        return {
            "data": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "description": p.description,
                    "provider_type": p.provider_type,
                    "image_ref": p.image_ref,
                    "capabilities": p.capabilities_json or [],
                    "config_json": mask_config_json(p.config_json or {}),
                    "network_policy": p.network_policy,
                    "initialisation_strategy": p.initialisation_strategy,
                    "secret_refs": p.secret_refs_json or [],
                    "persistence_policy": p.persistence_policy,
                    "status": p.status,
                    "visibility": p.visibility,
                    "owner_team_id": str(p.owner_team_id) if p.owner_team_id else None,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
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
        _log.exception("list_environment_profiles failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_environment_profiles failed")
        return _tool_exception_error("Failed to list environment profiles", exc, "mcp.list_environment_profiles")


@mcp.tool(
    description="AI-assisted schema inference. Takes a sample JSON payload and returns an inferred "
    "JSON Schema definition. Precondition: this preview feature requires developer "
    "mode (toggle Developer Mode in Admin > Feature Flags, or FARNA_CONTROL_DEV_MODE=true) "
    "and at least one configured model backend; the call fails otherwise.",
)
@_RETRY_DB
async def infer_schema(
    input_sample: dict[str, Any],
    pipeline_id: str | None = None,
) -> dict[str, Any]:
    del pipeline_id  # retained for backward-compatible MCP input schema; unused by design
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("infer_schema")

        # Preview feature - requires dev mode
        from farna.settings import get_settings

        settings = get_settings()
        if not settings.dev_mode:
            return _tool_error(
                "Schema inference requires developer mode. Set FARNA_CONTROL_DEV_MODE=true or toggle Developer Mode in Admin > Feature Flags.",
                code="feature_required",
            )
        from farna.core.schema_registry import SchemaInferenceError, SchemaInferenceService

        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            from farna.db.crud.model_backend import list_model_backends

            mbs = await list_model_backends(s, org_id=org_id, page_size=1)
            if not mbs.items:
                return {"error": "no_backend", "detail": "No model backends configured; cannot perform inference"}

            from farna.core.model_backend_hub import ModelBackendHub
            from farna.core.secrets_backend import create_secrets_backend

            secrets_backend = create_secrets_backend(fernet_key=get_settings().fernet_key, session=s)
            async with ModelBackendHub() as mh:
                # Model-backend credential decrypt needs the org-scoped RLS
                # context in the SAME transaction as the secrets read —
                # set_config(..., true) is transaction-local, so re-assert
                # set_rls_org here (inside the ``_session`` transaction that
                # wraps this body) before the decrypt. Without the session the
                # tool 500s with "Failed to infer schema".
                await set_rls_org(s, org_id)
                await mh.initialise(mbs.items, secrets_backend=secrets_backend)
                backend = await mh.get(mbs.items[0].id)

                samples = [input_sample]
                service = SchemaInferenceService(backend)
                definition = await service.infer(samples)

        return {
            "definition": definition,
            "sample_count": 1,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except SchemaInferenceError as exc:
        return {"error": "inference_failed", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("infer_schema failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("infer_schema failed")
        return _tool_exception_error("Failed to infer schema", exc, "mcp.infer_schema")


@mcp.tool(
    description="Validate a payload against a registered schema by schema_id. Returns validation errors or success.",
)
@_RETRY_DB
async def validate_payload(
    schema_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("validate_payload")
        from jsonschema import Draft202012Validator, ValidationError
        from jsonschema.exceptions import SchemaError as JsSchemaError

        org_id = _ctx_org_id_val()
        try:
            sid = uuid.UUID(schema_id)
        except ValueError:
            return {"error": "invalid_id", "field": "schema_id", "detail": f"Invalid UUID format: {schema_id}"}

        async with _session(org_id) as s:
            schema = await get_schema(s, sid)
            if schema is None:
                return {"error": "not_found", "detail": f"Schema {schema_id} not found"}

            from sqlalchemy import select

            from farna.db.models.schema import SchemaVersion

            result = await s.execute(select(SchemaVersion).where(SchemaVersion.schema_id == sid).order_by(SchemaVersion.version_number.desc()).limit(1))
            sv = result.scalar_one_or_none()
            if sv is None:
                return {"error": "no_version", "detail": f"Schema {schema_id} has no versions"}

        definition = sv.definition_json
        try:
            Draft202012Validator.check_schema(definition)
            validator = Draft202012Validator(definition)
            errors = list(validator.iter_errors(payload))
            if not errors:
                return {"valid": True, "errors": []}
            return {
                "valid": False,
                "errors": [
                    {
                        "path": ".".join(str(p) for p in e.path),
                        "message": e.message,
                    }
                    for e in errors
                ],
            }
        except (ValidationError, JsSchemaError) as exc:
            return {
                "valid": False,
                "errors": [{"path": "(schema)", "message": f"Invalid schema definition: {exc.message}"}],
            }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("validate_payload failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("validate_payload failed")
        return _tool_exception_error("Failed to validate payload", exc, "mcp.validate_payload")


@mcp.resource("modulo://schemas")
async def resource_schemas() -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the list_schemas tool — same node-scope gate.
    denied = _resource_node_scope_error("list_schemas")
    if denied is not None:
        return denied
    from sqlalchemy import select

    from farna.db.models.schema import Schema

    org_id = _ctx_org_id_val()
    async with _session(org_id) as s:
        result = await s.execute(select(Schema).where(Schema.organisation_id == org_id).order_by(Schema.name))
        schemas = list(result.scalars())
    lines = [f"- {sc.name} (id={sc.id})" for sc in schemas]
    return f"Schemas ({len(schemas)}):\n" + "\n".join(lines)


@mcp.resource("modulo://schemas/{schema_id}@{version}")
async def resource_schema_detail(schema_id: str, version: str) -> str:
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: the schema-version read surface duplicates get_schema_versions
    # (list_schemas exposes no version rows) — gate under it.
    denied = _resource_node_scope_error("get_schema_versions")
    if denied is not None:
        return denied
    # FAR-2129: same plan-feature parity as the tool and the REST route it
    # mirrors (GET /api/v1/schemas/{schema_id}/versions/{version}, gated
    # require_feature("schema_version_history")). Without it the resource would
    # remain an unentitled way to read the same version payload.
    org_id = _ctx_org_id_val()
    feat_err = await _require_plan_feature(org_id, get_settings(), "schema_version_history")
    if feat_err:
        return f"error: {feat_err['error']}: {feat_err['detail']}"
    from sqlalchemy import select

    from farna.db.models.schema import Schema, SchemaVersion

    sid, sid_err = _parse_uuid_param(schema_id, "schema_id")
    if sid_err:
        return f"error: Invalid UUID format: {schema_id}"
    assert sid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above
    async with _session(org_id) as s:
        schema = await s.get(Schema, sid)
        if schema is None:
            return f"Schema {schema_id} not found."

        if version == "latest":
            result = await s.execute(select(SchemaVersion).where(SchemaVersion.schema_id == sid).order_by(SchemaVersion.version_number.desc()).limit(1))
            sv = result.scalar_one_or_none()
        else:
            result = await s.execute(
                select(SchemaVersion).where(
                    SchemaVersion.schema_id == sid,
                    SchemaVersion.version == version,
                )
            )
            sv = result.scalar_one_or_none()

        if sv is None:
            return f"Schema version '{version}' not found for schema {schema_id}."

    defn = sv.definition_json or {}
    schema_type = defn.get("type", "object")

    fields: list[dict[str, Any]] = []
    if "properties" in defn:
        required_set = set(defn.get("required", []))
        fields = [
            {
                "name": name,
                "type": prop.get("type", "unknown"),
                "required": name in required_set,
            }
            for name, prop in defn["properties"].items()
        ]
    elif "fields" in defn:
        fields = [
            {
                "name": f.get("name", "?"),
                "type": f.get("type", "unknown"),
                "required": f.get("required", False),
            }
            for f in defn["fields"]
        ]

    lines = [
        f"Schema: {schema.name}",
        f"ID: {schema.id}",
        f"Type: {schema_type}",
        f"Version: {sv.version}",
        f"Created: {sv.created_at.isoformat()}",
        f"Fields ({len(fields)}):",
    ]
    for f in fields:
        req = "required" if f["required"] else "optional"
        lines.append(f"  - {f['name']}: {f['type']} ({req})")

    return "\n".join(lines)
