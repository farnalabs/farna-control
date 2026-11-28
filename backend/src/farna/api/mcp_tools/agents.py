"""Agents, teams, users and feature-flag MCP tools.

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

from typing import Any

from sqlalchemy.exc import ProgrammingError

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    _mcp_flag_visible,
    _mcp_internal_lifecycle_map_access,
    _parse_optional_uuid,
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
    _require_plan_entitlement,
    _session,
    get_settings,
    resolve_plan_context,
    validate_current_auth,
)
from farna.core.mcp.scope_validator import MCPAuthorizationError
from farna.db.models.agent import Agent


@mcp.tool(description="Create a new agent. Returns the created agent details.")
@mcp_audited("agent_created", "agent", fail_closed=False)
@_RETRY_DB
async def create_agent(
    name: str,
    prompt_template: str,
    description: str | None = None,
    model_backend_id: str | None = None,
    input_schema_id: str | None = None,
    output_schema_id: str | None = None,
    connector_type_refs: list[dict[str, Any]] | None = None,
    required_environment_capabilities: list[str] | None = None,
    is_executable: bool = True,
    template_id: str | None = None,
    agent_commands: list[str] | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("create_agent")

        # FAR-220: the REST Agent save paths run the git-content pin gate; this
        # tool must not bypass it (an unpinned ``git+`` ref saved here would
        # only fail closed at render time). Same shared helper, tool-error on
        # failure — no DB session is opened for invalid content.
        from farna.core.pipeline_engine.git_content import GitContentRefError
        from farna.core.pipeline_engine.git_content import (
            validate_agent_git_content_values as _validate_agent_git_content_values,
        )

        try:
            _validate_agent_git_content_values(prompt_template=prompt_template, agent_commands=agent_commands)
        except GitContentRefError as exc:
            return {"error": "validation_failed", "detail": str(exc)}

        org_id = _ctx_org_id_val()
        account_id = _ctx_user_id_val()

        # FAR-1540: every id-shaped param here is parsed BEFORE any DB session
        # is opened. A malformed id used to raise ``ValueError`` out of
        # ``uuid.UUID(...)``, land in the generic ``except Exception`` arm and
        # come back as ``internal_error`` — a code the consuming agent cannot
        # branch on (and, for ``template_id``, the failure was worse: the CRUD
        # layer's lenient ``coerce_uuid`` silently DROPPED it, so the agent was
        # created with no template and no error at all). Same helper, same
        # ``invalid_id`` payload, as every other tool on this surface.
        parsed_model_backend_id, model_backend_err = _parse_optional_uuid(model_backend_id, "model_backend_id")
        if model_backend_err is not None:
            return model_backend_err
        parsed_input_schema_id, input_schema_err = _parse_optional_uuid(input_schema_id, "input_schema_id")
        if input_schema_err is not None:
            return input_schema_err
        parsed_output_schema_id, output_schema_err = _parse_optional_uuid(output_schema_id, "output_schema_id")
        if output_schema_err is not None:
            return output_schema_err
        parsed_template_id, template_err = _parse_optional_uuid(template_id, "template_id")
        if template_err is not None:
            return template_err

        from farna.db.crud.agent import create_agent as db_create_agent

        async with _session(org_id) as s:
            agent = await db_create_agent(
                s,
                org_id=org_id,
                name=name,
                account_id=account_id,
                is_executable=is_executable,
                input_schema_id=parsed_input_schema_id,
                input_schema_version="latest",
                output_schema_id=parsed_output_schema_id,
                output_schema_version="latest",
                prompt_template=prompt_template,
                model_backend_id=parsed_model_backend_id,
                description=description,
                connector_type_refs=connector_type_refs or [],
                template_id=str(parsed_template_id) if parsed_template_id is not None else None,
                agent_commands=agent_commands,
                required_environment_capabilities=required_environment_capabilities,
            )

        return {
            "id": str(agent.id),
            "name": agent.name,
            "description": agent.description,
            "is_executable": agent.is_executable,
            "created_at": agent.created_at.isoformat() if agent.created_at else None,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("create_agent failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("create_agent failed")
        return _tool_exception_error("Failed to create agent", exc, "mcp.create_agent")


def _agent_item(a: Any) -> dict[str, Any]:
    """Build an agent summary for list_agents (no prompt template bodies)."""
    return {
        "id": str(a.id),
        "name": a.name,
        "description": a.description,
        "is_executable": a.is_executable,
        "model_backend_id": str(a.model_backend_id) if a.model_backend_id else None,
        "input_schema_id": str(a.input_schema_id) if a.input_schema_id else None,
        "output_schema_id": str(a.output_schema_id) if a.output_schema_id else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


@mcp.tool(
    name="list_agents",
    description=("List agents in the organisation with cursor-based pagination. Returns agent summaries (no prompt templates — use get_agent for the full definition)."),
)
@_RETRY_DB
async def list_agents(
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_agents")

        from farna.db.crud.agent import list_agents as db_list_agents

        org_id = _ctx_org_id_val()
        lim = max(1, min(limit, 100))

        async with _session(org_id) as s:
            result = await db_list_agents(s, org_id=org_id, cursor=cursor, page_size=lim)

        return {
            "data": [_agent_item(a) for a in result.items],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_agents failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_agents failed")
        return _tool_exception_error("Failed to list agents", exc, "mcp.list_agents")


def _agent_to_dict(agent: Agent) -> dict[str, Any]:
    """Serialize one ``Agent`` row to the ``get_agent`` MCP response shape."""
    return {
        "id": str(agent.id),
        "name": agent.name,
        "description": agent.description,
        "is_executable": agent.is_executable,
        "prompt_template": agent.prompt_template,
        "prompt_version_history": agent.prompt_version_history or [],
        "model_backend_id": str(agent.model_backend_id) if agent.model_backend_id else None,
        "input_schema_id": str(agent.input_schema_id) if agent.input_schema_id else None,
        "input_schema_version": agent.input_schema_version,
        "output_schema_id": str(agent.output_schema_id) if agent.output_schema_id else None,
        "output_schema_version": agent.output_schema_version,
        "parameter_schema_id": str(agent.parameter_schema_id) if agent.parameter_schema_id else None,
        "connector_type_refs": agent.connector_type_refs or [],
        "required_environment_capabilities": agent.required_environment_capabilities or [],
        "retry_policy": agent.retry_policy or {},
        "token_budget": agent.token_budget,
        "max_input_length": agent.max_input_length,
        "agent_commands": agent.agent_commands,
        "created_at": agent.created_at.isoformat() if agent.created_at else None,
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }


@mcp.tool(description="Get a single agent by ID, including its prompt template and configuration.")
@_RETRY_DB
async def get_agent(agent_id: str) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("get_agent")

        org_id = _ctx_org_id_val()
        aid, aid_err = _parse_uuid_param(agent_id, "agent_id")
        if aid_err:
            return aid_err
        if aid is None:
            return {"error": "invalid_param", "detail": f"Invalid agent_id: {agent_id}"}

        from farna.db.crud.agent import get_agent as db_get_agent

        async with _session(org_id) as s:
            agent = await db_get_agent(s, aid)

        # Belt-and-braces org check (mirrors the REST get route) — RLS already
        # scopes the row, but a cross-org id must read as not_found even if
        # RLS were misconfigured.
        if agent is None or agent.organisation_id != org_id:
            return {"error": "not_found", "detail": f"Agent {agent_id} not found"}

        return _agent_to_dict(agent)
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_agent failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_agent failed")
        return _tool_exception_error("Failed to get agent", exc, "mcp.get_agent")


# ---------------------------------------------------------------------------
# FAR-696: governance-entity read/list tools.
#
# Read-only MCP surface for entities that previously had REST routes only.
# Each tool maps to the SAME permission key as its corresponding REST route
# (registered in ``core/mcp/scope_validator.py``) and mirrors that route's
# org scoping; no write tools in this block.
# ---------------------------------------------------------------------------


@mcp.tool(
    name="list_teams",
    description=("List teams in the organisation with page-based pagination (mirrors GET /api/v1/teams)."),
)
@_RETRY_DB
async def list_teams(page: int = 1, page_size: int = 20) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_teams")

        from farna.db.crud.team import list_teams as db_list_teams

        org_id = _ctx_org_id_val()
        # REST parity: GET /api/v1/teams is router-gated by
        # require_entitlement("identity") — the Identity bundle (sso / scim /
        # team_rbac). Gate the MCP tool on the SAME entitlement, not on a
        # single member flag: killing team_rbac alone must not refuse MCP while
        # the REST route stays open on sso/scim.
        feat_err = await _require_plan_entitlement(org_id, get_settings(), "identity")
        if feat_err:
            return feat_err
        pg = max(1, page)
        ps = max(1, min(page_size, 100))

        async with _session(org_id) as s:
            result = await db_list_teams(s, org_id=org_id, page=pg, page_size=ps)

        return {
            "data": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "description": t.description,
                    "account_id": str(t.account_id),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in result.items
            ],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_teams failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_teams failed")
        return _tool_exception_error("Failed to list teams", exc, "mcp.list_teams")


@mcp.tool(
    name="list_users",
    description=("List organisation members (accounts + org roles) with page-based pagination and optional search/role filters (mirrors GET /api/v1/admin/users — admin-only)."),
)
@_RETRY_DB
async def list_users(
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        _check_agent_tool_scope("list_users")

        if role is not None and role not in ("admin", "operator", "runner", "viewer"):
            return {
                "error": "validation_failed",
                "detail": f"Invalid role filter: {role!r} (expected admin|operator|runner|viewer)",
            }
        if search is not None and not search.strip():
            return {"error": "validation_failed", "detail": "search must be a non-empty string"}

        from farna.api.routes.admin import _list_org_accounts, _to_user_list_item

        org_id = _ctx_org_id_val()
        pg = max(1, page)
        ps = max(1, min(page_size, 1000))

        async with _session(org_id) as s:
            accounts_memberships, total = await _list_org_accounts(
                s,
                org_id=org_id,
                page=pg,
                page_size=ps,
                search=search,
                role_filter=role,
            )

        return {
            "data": [_to_user_list_item(a, m.role, org_membership=m).model_dump() for a, m in accounts_memberships],
            "total": total,
            "page": pg,
            "page_size": ps,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_users failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_users failed")
        return _tool_exception_error("Failed to list users", exc, "mcp.list_users")


@mcp.tool(
    name="list_feature_flags",
    description=("List the instance feature-flag registry with the calling org's effective tier and per-flag overrides (mirrors GET /api/v1/admin/feature-flags — SYSTEM-ADMIN only)."),
)
@_RETRY_DB
async def list_feature_flags() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-696/FAR-1930: the REST route is ``require_system_permission`` — a
        # STRICT is_system_admin gate with no org-role fall-through. The
        # requirement is declared in the shared scope map (SYSTEM_ADMIN_TOOLS)
        # and enforced by ``_check_agent_tool_scope``; API keys and OAuth
        # tokens always fail it.
        _check_agent_tool_scope("list_feature_flags")

        from farna.core.feature_flags import FeatureFlagRegistry, resolve_effective_license
        from farna.core.license import get_license, verify_license_key
        from farna.db.crud.organisation import get_organisation

        settings = get_settings()
        org_id = _ctx_org_id_val()

        async with _session(org_id) as s:
            org = await get_organisation(s, org_id)
            plan_ctx = await resolve_plan_context(settings, s, org)
            org_settings = getattr(org, "settings_json", None)
            org_key = org_settings.get("license_key") if isinstance(org_settings, dict) else None
            # is_valid is the OUTCOME of the one canonical session-free ladder
            # (core.feature_flags.resolve_effective_license: verified org key,
            # then the in-memory licence, then the verified env key) — the same
            # walk this tool's tier resolves through, so a future rung change
            # cannot make the two disagree. FAR-1849 replaces the inline
            # three-rung OR that duplicated it; a disjunction over the same
            # verified rungs gives an identical answer in every case.
            license_valid = resolve_effective_license(settings, org) is not None
            # has_license_key is NOT the ladder and must not be unified with
            # it: it mirrors the REST route's registry input — each configured
            # key source counts only after canonical verification. It answers
            # "is a valid key configured", where is_valid answers "did the
            # effective ladder resolve one".
            has_key = verify_license_key(getattr(settings, "license_key", "") or "") is not None or get_license() is not None or verify_license_key(str(org_key)) is not None
            registry = await FeatureFlagRegistry.from_db(s, plan_ctx.tier(), has_license_key=has_key)

            # Best-effort overlay of the org's per-flag overrides (same
            # semantics as the REST endpoint); a failed read falls back to
            # the registry defaults rather than failing the request.
            org_overrides: dict[str, bool] = {}
            if isinstance(org_settings, dict):
                raw_overrides = org_settings.get("feature_overrides")
                if isinstance(raw_overrides, dict):
                    org_overrides = {k: bool(v) for k, v in raw_overrides.items()}

        internal_lifecycle_access = _mcp_internal_lifecycle_map_access()
        return {
            "license": {
                "tier": registry.current_tier,
                "has_license_key": registry.has_license_key,
                "is_valid": license_valid,
            },
            "dev_mode": settings.dev_mode,
            "flags": [
                {
                    "name": f.name,
                    "description": f.description,
                    "tier": f.tier,
                    "currently_active": org_overrides.get(f.name, f.currently_active),
                    "depends_on": f.depends_on,
                }
                for f in registry.list_flags()
                if _mcp_flag_visible(
                    f.name,
                    internal_lifecycle_access=internal_lifecycle_access,
                )
            ],
            "would_activate": [
                {
                    "name": f.name,
                    "description": f.description,
                    "tier": f.tier,
                    "depends_on": f.depends_on,
                }
                for f in registry.tier_gap_flags()
                if f.name not in org_overrides
                if _mcp_flag_visible(
                    f.name,
                    internal_lifecycle_access=internal_lifecycle_access,
                )
            ],
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("list_feature_flags failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("list_feature_flags failed")
        return _tool_exception_error("Failed to list feature flags", exc, "mcp.list_feature_flags")
