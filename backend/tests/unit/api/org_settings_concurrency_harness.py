"""Shared arrangement harness for the admin org-settings concurrency endpoints.

``test_admin_run_concurrency.py`` (FAR run cap) and
``test_admin_sandbox_concurrency.py`` (FAR-589 sandbox cap) are two contracts
over the same machinery: each endpoint reads or patches ONE key of
``organisations.settings_json`` through a mocked AsyncSession, authenticated by
a tenant principal whose ``org_role`` decides 403-vs-200. The two caps stay
independent org settings, so the CONTRACT modules keep their own endpoints,
payloads, expected bodies and per-route assertions; only the arrangement is
shared here.

The fixture NAMES are deliberately the house ones (``org_settings`` /
``mock_session`` / ``client_admin`` / ``client_viewer``); pytest collects
fixtures from a test module's namespace, so a plain import from this module
makes the pair a single definition instead of two identical copies.

Nothing here asserts. Every expectation stays in the contract module that owns
it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from farna.api.dependencies import get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_tenant_user
from farna.auth.jwt import TenantPrincipal
from tests.unit.api.mock_session import configure_rls_preamble

ORG_ID = uuid4()
USER_ID = uuid4()


def admin_principal(role: str = "admin") -> TenantPrincipal:
    """The tenant principal the overridden auth dependency resolves to."""
    return TenantPrincipal(
        username="admin@test",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )


def make_org_settings_session(org_settings: dict[str, Any]) -> AsyncMock:
    """Mock session whose org row exposes a mutable ``settings_json``.

    ``org.settings_json`` IS the dict handed in, so a test that seeds a value
    through its ``org_settings`` fixture - and a test that reads back what the
    route wrote - both see the same object without reaching into the mock.
    """
    org = MagicMock()
    org.id = ORG_ID
    org.settings_json = org_settings

    # Plain MagicMock result so `.scalar_one_or_none()` stays synchronous.
    result = MagicMock()
    result.scalar_one_or_none.return_value = org

    session = configure_rls_preamble(AsyncMock())
    # Real SQLAlchemy sessions expose ``info`` as a dict; the route binds RLS
    # via ``set_rls_org`` which stores the org id here on generic backends, and
    # the FAR-1830/FAR-1832 patch helper reads it back through its strict
    # fail-closed tenant guard.
    session.info = {}
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    nested_cm = MagicMock()
    nested_cm.__aenter__ = AsyncMock(return_value=None)
    nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=nested_cm)
    session.execute.return_value = result
    session.flush = AsyncMock()
    return session


def make_client(mock_session: AsyncMock, role: str) -> AsyncClient:
    """An ASGI client with the auth, plan and session dependencies overridden."""
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    async def _override_tenant() -> TenantPrincipal:
        return admin_principal(role)

    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = lambda: mock_session
    app.dependency_overrides[get_current_tenant_user] = _override_tenant
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def raise_from(exc: Exception) -> Callable[..., Any]:
    """A callable that raises ``exc`` when invoked - a ``patch(..., new=...)`` target."""

    async def _raises(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    return _raises


@pytest.fixture
def org_settings() -> dict[str, Any]:
    """The org row's live ``settings_json`` for one test."""
    return {}


@pytest.fixture
def mock_session(org_settings: dict[str, Any]) -> AsyncMock:
    return make_org_settings_session(org_settings)


@pytest.fixture
def client_admin(mock_session: AsyncMock) -> Iterator[AsyncClient]:
    yield make_client(mock_session, role="admin")
    app.dependency_overrides.clear()


@pytest.fixture
def client_viewer(mock_session: AsyncMock) -> Iterator[AsyncClient]:
    yield make_client(mock_session, role="viewer")
    app.dependency_overrides.clear()


# Re-exported for test modules that type-annotate a fixture parameter.
__all__ = [
    "ORG_ID",
    "USER_ID",
    "admin_principal",
    "client_admin",
    "client_viewer",
    "make_client",
    "make_org_settings_session",
    "mock_session",
    "org_settings",
    "raise_from",
]
