"""Architecture checks for the authenticated REST and MCP entry-point inventory.

Authorization intent remains declared by the central permission registry and the
existing route exemption inventory. This test deliberately does not introduce a
route-to-role map: it checks that every live entry point is either connected to
the shared gate, or carries a documented exemption marker, and that every MCP
scope still resolves through the same registry.
"""

from __future__ import annotations

import os

from fastapi.routing import APIRoute

# The app import constructs the database engine during module import. Keep this
# architecture test runnable as a standalone file, like the other app-importing
# architecture tests, without requiring a developer .env file.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("SECRET_KEY", "a" * 32)
os.environ.setdefault("FERNET_KEY", "a" * 32)
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("FARNA_CONTROL_MODULO_ADMIN_PASSWORD", "test")
os.environ.setdefault("FARNA_CONTROL_MODULO_CSRF_ENABLED", "false")

from farna.api.main import app
from farna.auth.permissions import resolve_required
from farna.core.mcp.scope_validator import READ_ONLY_TOOLS, TOOL_SCOPE_REQUIREMENTS
from tests.unit.api.route_introspection import (
    get_all_apiroutes,
    get_applied_dependency_names,
    get_mutating_routes,
    get_permission_tag,
    get_read_routes,
)
from tests.unit.api.test_route_introspection import EXEMPT, PRINCIPAL_DEPENDENCIES, READ_EXEMPT


def _route_key(route: APIRoute) -> tuple[str, str]:
    methods = sorted((getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"})
    return (methods[0] if methods else "?", route.path)


def test_authenticated_rest_entry_points_have_a_gate_or_explicit_exemption() -> None:
    """Every authenticated REST entry point is mechanically accounted for.

    A permission dependency is the normal path. Identity-only routes are
    intentional exceptions, and their route key must appear in the explicit
    exemption inventory with a non-empty reason. Shared-secret and pre-auth
    channels use the same inventory, so a newly added untagged route cannot be
    silently omitted from review.
    """
    violations: list[str] = []
    for route in get_all_apiroutes(app):
        key = _route_key(route)
        tag = get_permission_tag(route)
        applied = get_applied_dependency_names(route)
        methods = getattr(route, "methods", None) or set()
        if tag is not None:
            continue
        if methods & {"POST", "PUT", "PATCH", "DELETE"}:
            reason = EXEMPT.get(key)
            if not reason:
                violations.append(f"{key[0]} {key[1]}: missing mutating-route exemption marker")
            continue
        if "GET" in methods:
            if applied & PRINCIPAL_DEPENDENCIES:
                continue
            reason = READ_EXEMPT.get(key)
            if not reason:
                violations.append(f"GET {key[1]}: missing public/pre-auth exemption marker")

    assert not violations, "Unauthorised REST entry points:\n" + "\n".join(violations)


def test_rest_exemption_markers_are_live_and_documented() -> None:
    """Every explicit exemption marker names one live route and explains it."""
    mutating_keys = {_route_key(route) for route in get_mutating_routes(app)}
    read_keys = {_route_key(route) for route in get_read_routes(app)}
    stale_mutating = sorted(set(EXEMPT) - mutating_keys)
    stale_reads = sorted(set(READ_EXEMPT) - read_keys)
    undocumented = [
        *(f"mutating {key}" for key, reason in EXEMPT.items() if not reason.strip()),
        *(f"read {key}" for key, reason in READ_EXEMPT.items() if not reason.strip()),
    ]
    assert not stale_mutating, f"stale mutating-route exemption markers: {stale_mutating}"
    assert not stale_reads, f"stale read-route exemption markers: {stale_reads}"
    assert not undocumented, f"exemption markers must carry a reason: {undocumented}"


def test_mcp_entry_points_are_registered_and_resolve_through_central_registry() -> None:
    """Every registered MCP tool is scoped or explicitly read-only.

    The MCP scope validator already derives its public views from one internal
    requirement declaration. This check audits the live FastMCP registry against
    those derived views and resolves every permission key through PERMISSIONS;
    it does not maintain a second tool-to-role registry.
    """
    from farna.api.mcp_server import mcp

    registered = set(mcp._tool_manager._tools)
    accounted = set(TOOL_SCOPE_REQUIREMENTS) | set(READ_ONLY_TOOLS)
    missing = sorted(registered - accounted)
    stale = sorted(key for key in accounted if key.split(":", 1)[0] not in registered)
    assert not missing, f"registered MCP tools without a scope or explicit read-only marker: {missing}"
    assert not stale, f"MCP scope markers for unregistered tools: {stale}"

    for tool, permission in TOOL_SCOPE_REQUIREMENTS.items():
        assert resolve_required(permission), f"MCP tool {tool} has an unresolved permission key {permission!r}"
    assert registered, "MCP inventory is empty; authorization coverage would be vacuous"
