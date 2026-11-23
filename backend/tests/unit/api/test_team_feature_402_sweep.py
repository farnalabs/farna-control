"""402-sweep test for entitlement and feature gates (FAR-2665).

Two gate kinds are swept separately:

* an ENTITLEMENT gate (a commercial capability) returns 402 on a real
  Community plan and passes on a Team plan;
* a FLAG gate (behaviour) returns 402 when the flag is switched off and passes
  when it is on.

A route is never asserted 402 for a capability FAR-2665 made free: those are
asserted to PASS on the real Community plan, which is what pins the lowering.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal
from farna.core.feature_flags import CommunityTier
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session
from tests.unit.api.plan_stubs import all_features, community_features

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_SCHEMA_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_DUMMY_UUID = uuid.UUID("00000000-0000-0000-0000-000000000005")


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    configure_mock_session(session, allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    return session


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
    )


def _build_client(plan: object) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_plan_context] = lambda: plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def community_client() -> Generator[TestClient, None, None]:
    yield from _build_client(community_features())


@pytest.fixture
def community_tier_client() -> Generator[TestClient, None, None]:
    """Real Community plan — community-tier flags active without a licence."""
    yield from _build_client(CommunityTier())


@pytest.fixture
def team_client() -> Generator[TestClient, None, None]:
    yield from _build_client(all_features())


# (entitlement_key, method, path) — representative routes per ENTITLEMENT.
# FAR-2665: a Team-gated route now names the commercial capability it needs
# (Identity / Audit and evidence / Cost governance), not one flag inside it.
ENTITLED_ROUTES: list[tuple[str, str, str]] = [
    ("identity", "get", "/api/v1/admin/sso/providers"),
    ("identity", "get", "/api/v1/teams"),
    ("audit_evidence", "get", "/api/v1/admin/audit/export"),
    ("audit_evidence", "get", "/api/v1/admin/runs/retention"),
    ("cost_governance", "get", "/api/v1/admin/costs/limits"),
    ("cost_governance", "get", "/api/v1/admin/costs/controls"),
    ("cost_governance", "get", "/api/v1/admin/costs/export"),
]

# Flags LOWERED to Community by FAR-2665 (operational/integration capability).
# A real Community plan has these active without a licence, so they must NEVER
# gate: they are asserted to pass on the real CommunityTier below.
LOWERED_ROUTES: list[tuple[str, str, str]] = [
    ("runtime_config", "get", "/api/v1/admin/runtime-config"),
    ("rate_limits", "get", "/api/v1/admin/rate-limits"),
    ("plugin_management", "get", "/api/v1/plugins"),
    ("error_tracking", "get", "/api/v1/errors"),
    ("error_forwarders", "get", "/api/v1/errors/forwarders"),
]

# Previously-freed community capabilities (regression pins).
COMMUNITY_ROUTES: list[tuple[str, str, str]] = [
    ("environment_profiles", "get", "/api/v1/environment-profiles"),
    ("email_config", "get", f"/api/v1/admin/org/{_DUMMY_UUID}/email-settings"),
    ("schema_version_history", "get", f"/api/v1/schemas/{_SCHEMA_ID}/versions"),
    # FAR-2665: detecting a broken audit chain is a safety control, so the
    # integrity check is free while aggregation/export stay Team.
    ("audit_verify", "get", "/api/v1/admin/audit/verify"),
]

# Community behaviour flags whose GATE still exists (a per-org override or a
# system kill switch turns them off). Asserted against the all-disabled stub:
# 402 with the flag named, and not-402 when every flag is enabled.
FLAG_GATED_ROUTES: list[tuple[str, str, str]] = [
    ("observability", "get", "/api/v1/settings/observability"),
    ("analytics_page", "get", "/api/v1/analytics/query"),
]


def _call(client: TestClient, method: str, path: str):
    return getattr(client, method)(path)


@pytest.mark.parametrize(("feature", "method", "path"), COMMUNITY_ROUTES + LOWERED_ROUTES)
def test_community_tier_feature_is_not_gated(community_tier_client: TestClient, feature: str, method: str, path: str) -> None:
    resp = _call(community_tier_client, method, path)
    assert resp.status_code != 402, f"Community feature '{feature}' was gated on {method.upper()} {path}: {resp.text[:200]}"


@pytest.mark.parametrize(("entitlement", "method", "path"), ENTITLED_ROUTES)
def test_community_plan_returns_402_for_an_entitlement(community_tier_client: TestClient, entitlement: str, method: str, path: str) -> None:
    resp = _call(community_tier_client, method, path)
    assert resp.status_code == 402, f"Expected 402 for {method.upper()} {path} (entitlement '{entitlement}' not granted) but got {resp.status_code}: {resp.text[:200]}"
    assert entitlement in resp.text


@pytest.mark.parametrize(("entitlement", "method", "path"), ENTITLED_ROUTES)
def test_team_plan_does_not_402_on_an_entitlement(team_client: TestClient, entitlement: str, method: str, path: str) -> None:
    resp = _call(team_client, method, path)
    assert resp.status_code != 402, f"Entitlement '{entitlement}' should be granted on a team plan but {method.upper()} {path} returned 402"


@pytest.mark.parametrize(("feature", "method", "path"), FLAG_GATED_ROUTES)
def test_disabled_community_flag_returns_402(community_client: TestClient, feature: str, method: str, path: str) -> None:
    resp = _call(community_client, method, path)
    assert resp.status_code == 402, f"Expected 402 for {method.upper()} {path} (feature '{feature}' disabled) but got {resp.status_code}: {resp.text[:200]}"
    assert feature in resp.text


@pytest.mark.parametrize(("feature", "method", "path"), FLAG_GATED_ROUTES)
def test_enabled_community_flag_does_not_402(team_client: TestClient, feature: str, method: str, path: str) -> None:
    resp = _call(team_client, method, path)
    assert resp.status_code != 402, f"Feature '{feature}' is enabled on the stub plan but {method.upper()} {path} returned 402"
