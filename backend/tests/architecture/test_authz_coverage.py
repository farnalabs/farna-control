"""Architecture checks for the authenticated REST and MCP entry-point inventory.

Authorization intent remains declared by the central permission registry and the
existing route exemption inventory. This test deliberately does not introduce a
route-to-role map: it checks that every live entry point is either connected to
the shared gate, or carries a documented exemption marker, and that every MCP
scope still resolves through the same registry.
"""

from __future__ import annotations

import ast
import inspect
import os
from collections.abc import Callable

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

_SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _route_keys(route: APIRoute) -> set[tuple[str, str]]:
    methods = (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
    unsupported = methods - _SUPPORTED_HTTP_METHODS
    if unsupported:
        raise AssertionError(f"Unsupported HTTP methods on {route.path}: {sorted(unsupported)}")
    return {(method, route.path) for method in methods}


def _route_key(route: APIRoute) -> tuple[str, str]:
    keys = _route_keys(route)
    if len(keys) != 1:
        raise AssertionError(f"Multi-method route requires per-method coverage keys: {sorted(keys)}")
    return next(iter(keys))


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
        keys = _route_keys(route)
        tag = get_permission_tag(route)
        applied = get_applied_dependency_names(route)
        if tag is not None:
            continue
        for method, path in keys:
            key = (method, path)
            if method in {"POST", "PUT", "PATCH", "DELETE"}:
                reason = EXEMPT.get(key)
                if not reason:
                    violations.append(f"{method} {path}: missing mutating-route exemption marker")
                continue
            if method == "GET":
                if applied & PRINCIPAL_DEPENDENCIES:
                    continue
                reason = READ_EXEMPT.get(key)
                if not reason:
                    violations.append(f"GET {path}: missing public/pre-auth exemption marker")

    assert not violations, "Unauthorised REST entry points:\n" + "\n".join(violations)


def _route_for_key(key: tuple[str, str]) -> APIRoute | None:
    method, path = key
    return next(
        (route for route in get_all_apiroutes(app) if (method, route.path) in _route_keys(route) and route.path == path),
        None,
    )


def _endpoint_ast(route: APIRoute) -> ast.AST:
    """Parse executable endpoint code, excluding comments and docstrings."""
    return ast.parse(inspect.getsource(route.endpoint))


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            names.add(callee.id)
        elif isinstance(callee, ast.Attribute):
            names.add(callee.attr)
    return names


def _has_org_role_comparison(tree: ast.AST, roles: set[str]) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        has_role = any(isinstance(value, ast.Attribute) and value.attr == "org_role" for value in operands)
        constants = {value.value for value in ast.walk(node) if isinstance(value, ast.Constant) and isinstance(value.value, str)}
        if has_role and roles <= constants:
            return True
    return False


def _exemption_has_live_evidence(route: APIRoute, reason: str) -> bool:
    """Check executable guard structure, never comments or docstrings."""
    tree = _endpoint_ast(route)
    calls = _called_names(tree)
    dependencies = get_applied_dependency_names(route)
    endpoint_name = route.endpoint.__name__

    if "inline _require_admin" in reason or "inline _require_org_admin" in reason or "inline admin check" in reason:
        return "_require_admin" in calls or "_admin_journey_write" in calls or "_run_admin_rls_txn" in calls or _has_org_role_comparison(tree, {"admin"})
    if "org_role==admin" in reason:
        return "_run_admin_rls_txn" in calls or "_require_admin" in calls or _has_org_role_comparison(tree, {"admin"})
    if "admin/operator" in reason:
        return _has_org_role_comparison(tree, {"admin", "operator"})
    if "SCIM" in reason:
        return "get_scim_principal" in dependencies
    if "HMAC" in reason or "Slack" in reason or "Stripe" in reason:
        return any(name.startswith("verify_") and "signature" in name for name in calls) or "_verify_slack_delivery" in calls or "_verify_webhook_signature" in calls or "_replay_webhook_intake" in calls or "_receive_webhook_intake" in calls
    if "shared-secret Bearer" in reason:
        return "_require_drain_token" in calls
    if "auth (no principal" in reason or "refresh-token auth" in reason:
        return endpoint_name in {"login", "demo_login", "accept_invite", "refresh", "logout"}
    if "SAML ACS" in reason:
        return "get_anonymous_plan_context" in dependencies and "require_feature_anonymous" in calls
    if "public error ingestion" in reason:
        return "_check_public_rate_limit" in calls and "bind_audit_org" in calls
    if "demo auto-login" in reason:
        return endpoint_name == "demo_login"
    if "creation-only" in reason:
        return bool(dependencies & PRINCIPAL_DEPENDENCIES) or "_detect_parameter_ports" in calls
    if "community publish" in reason:
        return "require_system_admin" in dependencies or "require_system_admin" in calls
    if "health probe" in reason or "readiness probe" in reason:
        return endpoint_name in {"liveness", "readiness"}
    if "OAuth protected-resource metadata" in reason:
        return endpoint_name in {"protected_resource_metadata", "protected_resource_metadata_mcp"}
    if "OAuth discovery metadata" in reason:
        return endpoint_name in {"protected_resource_metadata", "protected_resource_metadata_mcp", "authorization_server_metadata"}
    if "pre-auth" in reason or "SSO discovery" in reason:
        return endpoint_name in {"login_context", "org_login", "oidc_login", "oidc_callback", "saml_login", "saml_metadata", "saml_login_provider", "saml_metadata_provider", "consent_context", "sso_providers"}
    if "self-profile" in reason or "user content" in reason or "onboarding" in reason or "Assistant" in reason or "OAuth" in reason or "setup flow" in reason or ("consent" in reason and "anonymous" not in reason):
        return bool(dependencies & PRINCIPAL_DEPENDENCIES)
    if "public" in reason or "static" in reason or "catalogue" in reason or "liveness" in reason or "deployment-level" in reason or "deploy-pipeline" in reason:
        return endpoint_name in {
            "deployment_info",
            "list_connector_types",
            "manifest_endpoint",
            "list_registry_primitives_endpoint",
            "get_registry_primitive_endpoint",
            "list_publishers_endpoint",
            "get_registry_primitive",
            "pull_registry_primitive_v2",
            "ping",
            "license_info",
            "consent_context",
        }
    return False


def test_rest_exemption_markers_are_live_documented_and_guarded() -> None:
    """Every exemption names a live route and has evidence of its real guard."""
    mutating_keys = {key for route in get_mutating_routes(app) for key in _route_keys(route)}
    read_keys = {key for route in get_read_routes(app) for key in _route_keys(route)}
    stale_mutating = sorted(set(EXEMPT) - mutating_keys)
    stale_reads = sorted(set(READ_EXEMPT) - read_keys)
    undocumented = [
        *(f"mutating {key}" for key, reason in EXEMPT.items() if not reason.strip()),
        *(f"read {key}" for key, reason in READ_EXEMPT.items() if not reason.strip()),
    ]
    unguarded = [
        *(f"mutating {key}: {reason}" for key, reason in EXEMPT.items() if (route := _route_for_key(key)) is not None and not _exemption_has_live_evidence(route, reason)),
        *(f"read {key}: {reason}" for key, reason in READ_EXEMPT.items() if (route := _route_for_key(key)) is not None and not _exemption_has_live_evidence(route, reason)),
    ]
    assert not stale_mutating, f"stale mutating-route exemption markers: {stale_mutating}"
    assert not stale_reads, f"stale read-route exemption markers: {stale_reads}"
    assert not undocumented, f"exemption markers must carry a reason: {undocumented}"
    assert not unguarded, "REST exemptions lack evidence of their underlying guard:\n" + "\n".join(unguarded)


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


def _reachable_function_names(function: Callable[..., object]) -> set[str]:
    """Return functions reachable from an MCP handler through direct calls."""
    source = inspect.getsource(function)
    module = inspect.getmodule(function)
    assert module is not None
    tree = ast.parse(inspect.getsource(module))
    functions = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    calls = {name: {node.func.id for node in ast.walk(function_node) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions} for name, function_node in functions.items()}
    root = function.__name__
    if root not in functions:
        raise AssertionError(f"MCP handler source is not available for {root}: {source[:80]!r}")
    reached: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        pending.extend(calls.get(name, set()) - reached)
    return reached


def test_mcp_handlers_reach_the_shared_runtime_scope_gate() -> None:
    """Every registered MCP handler must call the live scope chokepoint.

    Checking only ``TOOL_SCOPE_REQUIREMENTS`` proves bookkeeping, not runtime
    enforcement. Follow direct helper calls through each registered handler so a
    tool cannot be listed in the policy while bypassing ``check_tool_scope``.
    """
    from farna.api.mcp_server import mcp

    bypasses: list[str] = []
    for name, registered_tool in mcp._tool_manager._tools.items():
        reached = _reachable_function_names(registered_tool.fn)
        if "_check_agent_tool_scope" not in reached and "_check_node_allowed_tools" not in reached:
            bypasses.append(name)
    assert not bypasses, "MCP handlers bypass the shared runtime scope gate: " + ", ".join(sorted(bypasses))
