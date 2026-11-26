"""Focused unit tests for the non-admin org settings read surface (FAR-2682).

``GET /api/v1/org/settings`` and
``GET /api/v1/org/settings/guardrails/kill-switch`` are deliberately
tenant-auth-only: every member of an organisation may read the display currency
and the guardrails kill-switch state, with no plan gate and no admin role. These
tests pin that contract directly (the broad route-coverage smoke test asserts
the shapes but not the member-vs-admin boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from farna.api.dependencies import get_db_session
from farna.api.main import app
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="JvUi8wck_O-wMwfX6IkzwHj5d_1XbN8MlChjM7pupog=",
        admin_password="testpass",
    )


def _tenant(*, role: str) -> TenantPrincipal:
    return TenantPrincipal(username=f"user-{role}", organisation_id=ORG_ID, account_id=USER_ID, org_role=role)


def _auth(*, role: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(username=f"user-{role}", organisation_id=ORG_ID, account_id=USER_ID, org_role=role)


def _session() -> AsyncMock:
    session = configure_rls_preamble(AsyncMock())
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=None)
    context.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=context)
    session.execute = AsyncMock(return_value=MagicMock())
    return session


@pytest.fixture
def make_client() -> Generator[Callable[..., TestClient], None, None]:
    def _factory(*, role: str = "admin") -> TestClient:
        session = _session()

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_settings] = _settings
        app.dependency_overrides[get_current_user] = lambda: _auth(role=role)
        app.dependency_overrides[get_current_tenant_user] = lambda: _tenant(role=role)
        return TestClient(app, raise_server_exceptions=False)

    try:
        yield _factory
    finally:
        app.dependency_overrides.clear()


def _org(**over: object) -> SimpleNamespace:
    return SimpleNamespace(
        settings_json=over.get("settings_json"),
        guardrails_kill_switch=over.get("guardrails_kill_switch", False),
        guardrails_kill_switch_at=over.get("guardrails_kill_switch_at"),
    )


def test_org_settings_returns_org_display_currency(make_client: Callable[..., TestClient]) -> None:
    client = make_client(role="admin")
    org = _org(settings_json={"cost_controls": {"currency": "GBP"}})
    with patch("farna.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=org):
        resp = client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json() == {"currency": "GBP"}


def test_org_settings_defaults_to_usd_for_an_unsupported_currency(make_client: Callable[..., TestClient]) -> None:
    client = make_client(role="admin")
    org = _org(settings_json={"cost_controls": {"currency": "XXX"}})
    with patch("farna.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=org):
        resp = client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json() == {"currency": "USD"}


def test_org_settings_is_readable_by_a_non_admin_member(make_client: Callable[..., TestClient]) -> None:
    client = make_client(role="viewer")
    org = _org(settings_json={"cost_controls": {"currency": "EUR"}})
    with patch("farna.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=org):
        resp = client.get("/api/v1/org/settings")
    assert resp.status_code == 200
    assert resp.json() == {"currency": "EUR"}


def test_kill_switch_read_returns_enabled_state_for_any_member(make_client: Callable[..., TestClient]) -> None:
    client = make_client(role="viewer")
    enabled_at = datetime(2025, 1, 1, tzinfo=UTC)
    org = _org(guardrails_kill_switch=True, guardrails_kill_switch_at=enabled_at)
    with patch("farna.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=org):
        resp = client.get("/api/v1/org/settings/guardrails/kill-switch")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "enabled_at": enabled_at.isoformat()}


def test_kill_switch_read_returns_404_when_the_org_is_absent(make_client: Callable[..., TestClient]) -> None:
    client = make_client(role="admin")
    with patch("farna.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=None):
        resp = client.get("/api/v1/org/settings/guardrails/kill-switch")
    assert resp.status_code == 404
