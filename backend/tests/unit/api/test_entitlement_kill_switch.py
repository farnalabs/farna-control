"""The operator kill switch composes with the entitlement gate (FAR-2665 follow-up).

Before this fix every commercial route replaced ``require_feature(...)`` with
``require_entitlement(...)``, which read only the licence grant. The per-install
operator kill switch (``feature_flag_catalog.is_active``) still drove the plan
payload and the UI, so an operator could switch a paid capability off — its UI
locked — while every entitled API route stayed open. These tests pin the
composition that closes that gap:

* an entitled route is REFUSED when the operator switches off every flag that
  implements the capability (the licence is granted, the capability is not);
* the plan payload keeps advertising those flags as ``currently_active=false``,
  so what it tells the client can never contradict the API's refusal;
* a normal Team plan still reaches the entitled route (the composition does not
  over-gate a capability whose flags are on);
* switching off only ONE sub-flag of a multi-flag capability narrows that
  surface, it does not withdraw the whole bought capability.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_user
from farna.auth.jwt import AuthenticatedPrincipal
from farna.core.entitlements import ENTITLEMENTS, entitlement_available
from farna.core.feature_flags import _KNOWN_FLAGS, DbPlanContext, FeatureFlagRegistry
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_IDENTITY_FLAGS = ENTITLEMENTS["identity"].flags


def _settings_with_license() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
        license_key="valid-license-key",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = 0
    session.execute.return_value = scalar_result
    return session


async def _registry_with_killed_flags(killed: set[str]) -> FeatureFlagRegistry:
    """A real Team registry loaded from a DB catalog with ``killed`` ``is_active=false``.

    Built through ``from_db`` (not by poking the registry) so the test exercises
    the SAME path a real install takes: the catalogue's ``is_active`` column is
    what the operator flips, and the boot seed deliberately leaves it alone.
    """
    catalog_rows = [
        {
            "name": flag.name,
            "description": flag.description,
            "tier_id": flag.tier,
            "depends_on": flag.depends_on,
            "is_active": flag.name not in killed,
        }
        for flag in _KNOWN_FLAGS
    ]
    tiers = [
        {"tier_id": "community", "rank": 0},
        {"tier_id": "team", "rank": 1},
    ]
    with (
        patch("farna.db.crud.tier_catalog.list_tiers", return_value=tiers),
        patch("farna.db.crud.tier_catalog.list_feature_flags", return_value=catalog_rows),
    ):
        return await FeatureFlagRegistry.from_db(
            MagicMock(),
            current_tier="team",
            has_license_key=True,
        )


def _client(plan_ctx: DbPlanContext) -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings_with_license
    app.dependency_overrides[get_plan_context] = lambda: plan_ctx
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def _plan_payload_client(plan_ctx: DbPlanContext) -> Generator[TestClient, None, None]:
    """TestClient whose ``GET /api/v1/plan`` resolves the supplied plan context.

    The plan endpoint recomputes the plan via ``resolve_plan_context`` (it does
    not use the request-scoped dependency), so both seams are patched.
    """
    mock_session = _make_mock_session()
    org = MagicMock()
    org.id = _ORG_ID
    org.name = "Test Org"
    org.settings_json = {}
    org.daily_spend_limit = None

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _settings_with_license
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    try:
        with patch.multiple(
            "farna.api.routes.viewmodel",
            get_organisation=AsyncMock(return_value=org),
            resolve_plan_context=AsyncMock(return_value=plan_ctx),
            set_rls_org=AsyncMock(),
            set_rls_user_context=AsyncMock(),
        ):
            yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class _FlagStatePlan:
    """Minimal plan double: a fixed enabled-flag set, every entitlement granted."""

    def __init__(self, enabled: set[str]) -> None:
        self._enabled = enabled

    def feature_enabled(self, name: str) -> bool:
        return name in self._enabled

    def entitlements(self) -> frozenset[str]:
        return frozenset(ENTITLEMENTS)

    def tier(self) -> str:
        return "team"


async def test_entitlement_available_is_false_when_every_flag_is_switched_off() -> None:
    """The composition helper refuses a granted capability whose flags are all off."""
    killed_ctx = DbPlanContext(await _registry_with_killed_flags(set(_IDENTITY_FLAGS)))
    # The licence still grants Identity — this is not a grant failure.
    assert killed_ctx.entitlements() == frozenset(ENTITLEMENTS)
    assert not entitlement_available(killed_ctx, "identity")

    live_ctx = DbPlanContext(await _registry_with_killed_flags(set()))
    assert entitlement_available(live_ctx, "identity")


async def test_entitled_call_is_refused_when_the_capability_is_switched_off() -> None:
    """An entitled Identity route 402s when the operator kills every Identity flag.

    This is the regression the finding named: the licence grants Identity, but
    the operator has switched the capability off, so the API must refuse to
    match the locked UI rather than stay open.
    """
    killed = DbPlanContext(await _registry_with_killed_flags(set(_IDENTITY_FLAGS)))
    for client in _client(killed):
        resp = client.get("/api/v1/teams")
        assert resp.status_code == 402, f"expected the switched-off capability to be refused, got {resp.status_code}: {resp.text[:200]}"
        assert "Identity" in resp.text
        assert "not enabled for this organisation" in resp.text


async def test_entitled_call_still_works_when_the_capability_is_switched_on() -> None:
    """The composition does not over-gate: an all-on Team plan reaches the route."""
    live = DbPlanContext(await _registry_with_killed_flags(set()))
    for client in _client(live):
        resp = client.get("/api/v1/teams")
        assert resp.status_code == 200, f"an all-on capability must stay reachable, got {resp.status_code}: {resp.text[:200]}"


async def test_plan_payload_advertises_the_killed_flags_as_inactive() -> None:
    """The payload can never contradict the API: it reports the killed flags OFF.

    The entitlement's ``granted`` stays the licence answer (grant and behaviour
    are separate axes — the capability is bought, but switched off); the flags
    the payload serves all report ``currently_active=false``, so a client that
    reads them sees exactly the state that makes the API refuse.
    """
    killed = DbPlanContext(await _registry_with_killed_flags(set(_IDENTITY_FLAGS)))
    with _plan_payload_client(killed) as client:
        resp = client.get("/api/v1/plan")

    assert resp.status_code == 200
    body = resp.json()
    flags = {flag["name"]: flag for flag in body["flags"]}
    for name in _IDENTITY_FLAGS:
        assert flags[name]["currently_active"] is False, f"{name} is switched off but the payload advertises it active — that would contradict the API"
    # And the API really does refuse the same plan.
    for api_client in _client(killed):
        assert api_client.get("/api/v1/teams").status_code == 402


def test_identity_flags_cover_the_whole_identity_capability() -> None:
    """Guard: the test's kill set is the registry's, not a hand-copied list."""
    declared = {flag.name for flag in _KNOWN_FLAGS}
    assert set(_IDENTITY_FLAGS) == {"sso", "scim", "team_rbac"}
    assert set(_IDENTITY_FLAGS) <= declared


def test_switching_off_one_flag_does_not_withdraw_the_whole_capability() -> None:
    """One sub-flag off narrows a surface; it is not a capability withdrawal.

    FAR-2665 consolidated each set of surfaces into one entitlement and the
    routes bless "any flag in the group grants the group". The kill switch is
    therefore at CAPABILITY granularity: killing one sub-flag leaves the other
    surfaces reachable; killing all of them withdraws the capability.
    """
    assert entitlement_available(_FlagStatePlan({"sso"}), "identity")
    assert not entitlement_available(_FlagStatePlan(set()), "identity")


def test_entitlement_available_trusts_the_licence_when_the_plan_cannot_read_flags() -> None:
    """A plan context without ``feature_enabled`` falls back to the licence grant.

    A minimal legacy double cannot answer the kill switch, so the helper must
    not invent a refusal it cannot substantiate: the granted capability stays
    available rather than being silently withdrawn.
    """

    class _LicenceOnlyPlan:
        def tier(self) -> str:
            return "team"

    assert entitlement_available(_LicenceOnlyPlan(), "identity")
