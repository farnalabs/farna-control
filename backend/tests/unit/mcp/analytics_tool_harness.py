"""Shared arrangement harness for the analytics MCP tool contract tests.

``test_query_analytics.py`` and ``test_query_analytics_concurrency.py`` are two
contracts over two tools that funnel through the same analytics service: both
resolve an MCP auth context, gate on the same ``analytics_page`` feature and
``analytics.query`` permission, parse the same typed params, and map the same
service exceptions onto the same error dicts. The arrangement for all of that
lives here.

What stays in each contract module is what actually differs: which tool is
called, which service entry point it drives (``run_analytics_query`` vs
``run_concurrency_query``), the tool-specific params/dimensions, and every
expected response. Nothing here asserts.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from tests.unit.api.mock_session import make_session_scope
from tests.unit.mcp.helpers import make_session_context

PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
#: Deliberately distinct from ``helpers.USER_ID``: these two contracts were
#: written against their own placeholder principal and their assertions (e.g.
#: ``kwargs["org_id"] == PLACEHOLDER_ORG_ID``) are pinned to it.
PLACEHOLDER_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
API_KEY = "mk_testprefix_testsecretkey1234567890abc"
#: The org role both analytics tools are exercised under.
AUTH_ROLE = "runner"

_UNSET = object()


class AuthContext:
    """Set/teardown the MCP ContextVars so a tool handler reaches its gates."""

    def setup_method(self) -> None:
        from farna.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(PLACEHOLDER_ORG_ID)
        _ctx_role.set(AUTH_ROLE)
        _ctx_user_id.set(PLACEHOLDER_USER_ID)
        _ctx_auth_token.set(API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from farna.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_user_id.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)


class EnabledPlan:
    def feature_enabled(self, name: str) -> bool:
        return True

    def tier(self) -> str:
        return "community"

    def list_enabled_features(self) -> list:
        return []


class DisabledPlan:
    def feature_enabled(self, name: str) -> bool:
        return False

    def tier(self) -> str:
        return "community"

    def list_enabled_features(self) -> list:
        return []


def plan_patches(*, enabled: bool = True) -> tuple[Any, Any, Any, Any]:
    """The org lookup + plan resolution patches for an enabled/disabled plan."""
    plan = EnabledPlan() if enabled else DisabledPlan()
    return (
        patch("farna.api.mcp_server._session", return_value=make_session_context(make_session_scope())),
        patch(
            "farna.db.crud.organisation.get_organisation",
            new=AsyncMock(return_value=MagicMock(settings_json={}, plan_id=None)),
        ),
        patch("farna.core.feature_flags.resolve_plan_context", new=AsyncMock(return_value=plan)),
        patch("farna.api.mcp_server.get_settings", return_value=MagicMock()),
    )


def enabled_plan_patches() -> list[Any]:
    """Plan-enabled patches up to (but not including) the service call."""
    session_cm, org_patch, plan_patch, settings_patch = plan_patches(enabled=True)
    return [
        session_cm,
        org_patch,
        plan_patch,
        settings_patch,
        patch("farna.api.mcp_server._get_session_factory", return_value=AsyncMock()),
    ]


@contextlib.contextmanager
def plan_context(*, enabled: bool = True, service_ready: bool = False) -> Iterator[None]:
    """Enter the gate patches: org lookup, plan resolution, settings.

    ``service_ready`` additionally stubs ``_get_session_factory``, which the
    tool resolves only once it proceeds past parameter parsing towards the
    shared-service call. A test whose params are rejected never needs it.
    """
    patches = enabled_plan_patches() if service_ready else list(plan_patches(enabled=enabled))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


@contextlib.contextmanager
def service_stub(
    service_name: str,
    *,
    return_value: Any = _UNSET,
    side_effect: Any = _UNSET,
) -> Iterator[AsyncMock]:
    """Enter the gate patches plus a stub of ``farna.api.mcp_server.<service_name>``.

    ``service_name`` is the tool's shared-service entry point
    (``run_analytics_query`` / ``run_concurrency_query``). Yields the service
    mock so a test can assert on the typed params it received; pass exactly one
    of ``return_value`` (success paths) or ``side_effect`` (error mapping).
    """
    if (return_value is _UNSET) == (side_effect is _UNSET):
        raise ValueError("service_stub requires exactly one of return_value / side_effect")
    service = AsyncMock(
        return_value=None if return_value is _UNSET else return_value,
        side_effect=None if side_effect is _UNSET else side_effect,
    )
    with plan_context(service_ready=True), patch(f"farna.api.mcp_server.{service_name}", new=service):
        yield service
