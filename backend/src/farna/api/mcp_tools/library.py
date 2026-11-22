"""Library, product-documentation search and integration-status MCP tools.

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

import json
from typing import Any

from sqlalchemy.exc import ProgrammingError

from farna.api.mcp_audit import mcp_audited
from farna.api.mcp_server import (
    _MSG_DB_MIGRATION_REQUIRED,
    _MSG_ERROR_TOKEN_REVOKED,
    _MSG_TOKEN_REVOKED,
    _RETRY_DB,
    _check_node_allowed_tools,
    _mcp_internal_lifecycle_map_access,
    _require_mcp_internal_lifecycle_map_access,
    _resource_node_scope_error,
    _tool_auth_error,
    _tool_exception_error,
    mcp,
)
from farna.api.mcp_tools._patch_surface import (
    _check_agent_tool_scope,
    _ctx_org_id_val,
    _get_doc_index,
    _log,
    _parse_uuid_param,
    _session,
    get_primitive,
    get_primitive_by_slug,
    library_copy_to_adapt,
    list_primitives,
    validate_current_auth,
)
from farna.api.middleware.sensitive_mask import mask_pipeline_graph_node
from farna.core.library_service.primitive_types import PRIMITIVE_TYPES
from farna.core.mcp.scope_validator import MCPAuthorizationError


@mcp.tool(
    description=("Copy a library primitive to the org workspace. This creates an editable copy in your workspace; your copy diverges from upstream on first edit."),
)
@mcp_audited("library_primitive_copied", "library_primitive", fail_closed=False)
@_RETRY_DB
async def copy_library_primitive(
    primitive_id: str,
) -> dict[str, Any]:
    if not await validate_current_auth():
        return _tool_auth_error(_MSG_TOKEN_REVOKED)
    try:
        _check_agent_tool_scope("copy_library_primitive")
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}

    org_id = _ctx_org_id_val()
    pid, pid_err = _parse_uuid_param(primitive_id, "primitive_id")
    if pid_err:
        return pid_err
    assert pid is not None  # nosec B101 -- _parse_uuid_param returns (None, error) only on failure, already handled above

    async with _session(org_id) as s:
        try:
            primitive = await get_primitive(s, org_id, pid)
            if primitive is not None and primitive.primitive_type == "lifecycle_map":
                _require_mcp_internal_lifecycle_map_access()
            result = await library_copy_to_adapt(s, org_id, pid, via_mcp=False)
        except LookupError:
            return {"error": "not_found", "primitive_id": primitive_id}
        except ProgrammingError:
            _log.exception("copy_library_primitive failed")
            return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
        except Exception as exc:
            _log.exception("copy_library_primitive failed")
            return _tool_exception_error("Failed to copy library primitive", exc, "mcp.copy_library_primitive")

    return {
        "status": "copied",
        "primitive_id": str(result.id),
        "name": result.name,
        "slug": result.slug,
    }


@mcp.tool(
    name="search_library",
    description=("Search the library of primitives (schemas, agents, workflows, pipeline templates, test fixtures). Supports filtering by type, text search, and cursor-based pagination. For text output, see the modulo://library resource."),
)
@_RETRY_DB
async def search_library(
    primitive_type: str | None = None,
    search: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        org_id = _ctx_org_id_val()
        try:
            _check_agent_tool_scope("search_library")
            lifecycle_maps_allowed = _mcp_internal_lifecycle_map_access()
        except MCPAuthorizationError as exc:
            return {"error": "insufficient_scope", "detail": str(exc)}
        if not lifecycle_maps_allowed and primitive_type == "lifecycle_map":
            return {"items": [], "total": 0, "next_cursor": None, "has_more": False}
        visible_primitive_types = None
        if not lifecycle_maps_allowed:
            visible_primitive_types = [primitive_name for primitive_name in PRIMITIVE_TYPES if primitive_name != "lifecycle_map"]
        async with _session(org_id) as s:
            result = await list_primitives(
                s,
                org_id,
                primitive_type=primitive_type,
                primitive_types=visible_primitive_types,
                search=search,
                page=1,
                page_size=limit,
                cursor=cursor,
            )
        return {
            "items": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "description": p.description,
                    "type": p.primitive_type,
                    "version": p.version,
                    "average_rating": p.average_rating,
                    "tags": list(p.tags) if p.tags else [],
                }
                for p in result.items
            ],
            "total": result.total,
            "next_cursor": result.next_cursor,
            "has_more": result.has_more,
        }
    except ProgrammingError:
        _log.exception("search_library failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("search_library failed")
        return _tool_exception_error("Failed to search library", exc, "mcp.search_library")


# ---------------------------------------------------------------------------
# Context retrieval tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="search_documentation",
    description=("Search the product surface and navigation for relevant sections. Supports free-text keyword search against routes and pages in the product manifest. Returns Markdown-formatted results."),
)
async def search_documentation(query: str, section: str | None = None) -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("search_documentation")
        index = _get_doc_index()
        results = index.search(query, section=section)
        if not results:
            return {"results": "No documentation found for query.", "count": 0}
        formatted = index.format_results(results)
        return {"results": formatted, "count": len(results)}
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except Exception as exc:
        _log.exception("search_documentation failed")
        return _tool_exception_error("Failed to search documentation", exc, "mcp.search_documentation")


@mcp.tool(
    description=("Get current health status of all connectors, model backends, and triggers. Returns a Markdown table plus structured JSON fields. For individual connector/model-backend details, see modulo://connectors and modulo://model-backends resources."),
)
async def get_integration_status() -> dict[str, Any]:
    try:
        if not await validate_current_auth():
            return _tool_auth_error(_MSG_TOKEN_REVOKED)
        # FAR-1976: this tool bypasses the full chokepoint, so it must still
        # enforce the node allowed_tools narrowing leg explicitly.
        _check_node_allowed_tools("get_integration_status")
        from sqlalchemy import func, select

        from farna.db.models.connector_instance import ConnectorInstance
        from farna.db.models.model_backend import ModelBackend
        from farna.db.models.trigger import Trigger

        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            connector_rows = (await s.execute(select(ConnectorInstance).where(ConnectorInstance.organisation_id == org_id))).scalars().all()
            backend_rows = (await s.execute(select(ModelBackend).where(ModelBackend.organisation_id == org_id))).scalars().all()
            trigger_count_result = await s.execute(select(func.count()).select_from(Trigger).where(Trigger.organisation_id == org_id))
            trigger_count = trigger_count_result.scalar_one()

        connector_list: list[dict[str, Any]] = []
        connector_lines = [
            "| Name | Type | Status | Validation Level | Last Check | Error |",
            "|------|------|--------|------------------|------------|-------|",
        ]
        for c in connector_rows:
            last_check = c.last_health_check_at.isoformat() if c.last_health_check_at else "never"
            error = c.last_health_check_error or ""
            vl = getattr(c, "validation_level", None) or "unit-only"
            connector_lines.append(f"| {c.name} | {c.connector_type_id} | {c.status} | {vl} | {last_check} | {error} |")
            connector_list.append(
                {
                    "name": c.name,
                    "type": c.connector_type_id,
                    "status": c.status,
                    "validation_level": vl,
                    "last_check": last_check,
                    "error": error,
                }
            )

        backend_list: list[dict[str, Any]] = []
        backend_lines = [
            "| Name | Provider | Model | Has Credentials | Status | Validation Level |",
            "|------|----------|-------|-----------------|--------|------------------|",
        ]
        for b in backend_rows:
            has_creds = "yes" if b.credentials_ciphertext else "no"
            vl = getattr(b, "validation_level", None) or "unit-only"
            backend_lines.append(f"| {b.name} | {b.provider} | {b.model_id} | {has_creds} | {b.status} | {vl} |")
            backend_list.append(
                {
                    "name": b.name,
                    "provider": b.provider,
                    "model": b.model_id,
                    "has_credentials": bool(b.credentials_ciphertext),
                    "status": b.status,
                    "validation_level": vl,
                }
            )

        parts = [
            f"## Connectors ({len(connector_rows)})",
            "\n".join(connector_lines) if connector_rows else "No connectors configured.",
            "",
            f"## Model Backends ({len(backend_rows)})",
            "\n".join(backend_lines) if backend_rows else "No model backends configured.",
            "",
            f"## Triggers\n\nTotal triggers: {trigger_count}",
        ]
        return {
            "results": "\n".join(parts),
            "connectors": connector_list,
            "model_backends": backend_list,
            "trigger_count": trigger_count,
        }
    except MCPAuthorizationError as exc:
        return {"error": "insufficient_scope", "detail": str(exc)}
    except ProgrammingError:
        _log.exception("get_integration_status failed")
        return {"error": "migration_required", "detail": _MSG_DB_MIGRATION_REQUIRED}
    except Exception as exc:
        _log.exception("get_integration_status failed")
        return _tool_exception_error("Failed to get integration status", exc, "mcp.get_integration_status")


@mcp.resource("modulo://library")
async def resource_library() -> str:
    """List library primitives — schemas, agents, workflows, pipeline templates, test fixtures.

    For filtered browsing, use the ``search_library`` tool instead.
    """
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: duplicates the search_library tool — same node-scope gate.
    denied = _resource_node_scope_error("search_library")
    if denied is not None:
        return denied
    try:
        org_id = _ctx_org_id_val()
        lifecycle_maps_allowed = _mcp_internal_lifecycle_map_access()
        visible_primitive_types = None
        if not lifecycle_maps_allowed:
            visible_primitive_types = [primitive_name for primitive_name in PRIMITIVE_TYPES if primitive_name != "lifecycle_map"]
        async with _session(org_id) as s:
            result = await list_primitives(
                s,
                org_id,
                primitive_types=visible_primitive_types,
                page=1,
                page_size=50,
            )
        if not result.items:
            return "Library is empty."
        lines: list[str] = []
        for p in result.items:
            tags_str = ", ".join(p.tags) if p.tags else ""
            rating_str = f"{p.average_rating:.1f}" if p.average_rating is not None else "N/A"
            desc = f" — {p.description}" if p.description else ""
            lines.append(f"- {p.name} (id={p.id}, type={p.primitive_type}, v{p.version}, tags=[{tags_str}], rating={rating_str}){desc}")
        header = f"Library ({result.total} primitives):"
        return header + "\n" + "\n".join(lines)
    except Exception:
        _log.exception("resource_library failed")
        return "error: Failed to browse library"


def _mask_library_content_for_summary(content: Any, primitive_type: str) -> Any:
    """Mask credential-bearing node fields in a composite primitive's content.

    FAR-1380: a composite primitive's ``content_json`` carries the SAME
    credential-bearing node fields a pipeline graph stores (env_vars /
    context_files / composite_parameter_values / parameter_overrides) — mask
    them with the shipped per-node masker exactly like the pipeline graph
    reads; non-composite content passes through untouched.
    """
    if primitive_type != "composite" or not isinstance(content, dict):
        return content
    nodes = content.get("nodes")
    if not isinstance(nodes, list):
        return content
    masked = dict(content)
    masked["nodes"] = [mask_pipeline_graph_node(node) if isinstance(node, dict) else node for node in nodes]
    return masked


@mcp.resource("modulo://library/{primitive_type}/{slug}")
async def resource_library_detail(primitive_type: str, slug: str) -> str:
    """Get details of a single library primitive by type and slug."""
    if not await validate_current_auth():
        return _MSG_ERROR_TOKEN_REVOKED
    # FAR-1976: the library browse/read surface duplicates search_library —
    # gate under it (there is no separate library-primitive-read tool).
    denied = _resource_node_scope_error("search_library")
    if denied is not None:
        return denied
    try:
        if primitive_type == "lifecycle_map" and not _mcp_internal_lifecycle_map_access():
            return f"Library primitive '{slug}' of type '{primitive_type}' not found."
        org_id = _ctx_org_id_val()
        async with _session(org_id) as s:
            p = await get_primitive_by_slug(s, org_id, primitive_type, slug)

        if p is None:
            return f"Library primitive '{slug}' of type '{primitive_type}' not found."

        tags_str = ", ".join(p.tags) if p.tags else ""
        rating_str = f"{p.average_rating:.2f}" if p.average_rating is not None else "N/A"
        downloads_str = str(p.download_count) if p.download_count is not None else "0"
        desc = p.description or "(no description)"
        content_summary_str = json.dumps(
            _mask_library_content_for_summary(p.content_json, p.primitive_type),
            indent=2,
        )

        parts = [
            f"Name: {p.name}",
            f"ID: {p.id}",
            f"Type: {p.primitive_type}",
            f"Version: {p.version}",
            f"Author: {p.author}",
            f"Tags: [{tags_str}]",
            f"Average Rating: {rating_str}",
            f"Download Count: {downloads_str}",
            f"Description: {desc}",
            f"\nContent Summary:\n{content_summary_str}",
        ]
        return "\n".join(parts)
    except Exception:
        _log.exception("resource_library_detail failed")
        return "error: Failed to get library primitive detail"
