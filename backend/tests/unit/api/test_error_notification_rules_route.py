"""Route-level tests for HTTP status-code mappings on error-notification rules.

Migration 0117 (#1376) added the DB-level unique backstop for the per-org
notification-rule cap. The create route must translate the resulting
IntegrityError into 422 (not 409 or a 500), so a duplicate default-seed or a
concurrent over-cap insert reads as a validation conflict to the caller. This
module pins that mapping at the route boundary.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from farna.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes import error_notification_rules as rules_module
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.core.entitlements import granted_entitlements
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble
from tests.unit.api.plan_stubs import PlanStub, all_features

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_VALID_32 = "a" * 32


class _FlagPlan:
    """Plan double whose TIER and behaviour flag are independent (FAR-2698).

    The bug was that a behaviour flag stood in for the licence, so the two
    could not be set separately. The matrix tests need ``tier`` (the licence)
    and ``error_tracking`` (the operator kill switch) to vary independently.
    """

    def __init__(self, *, tier: str, error_tracking: bool) -> None:
        self._tier = tier
        self._error_tracking = error_tracking

    def feature_enabled(self, name: str) -> bool:
        return self._error_tracking if name == "error_tracking" else False

    def list_enabled_features(self) -> list[str]:
        return []

    def list_all_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return self._tier

    def has_license_key(self) -> bool:
        return self._tier != "community"

    def entitlements(self) -> frozenset[str]:
        return granted_entitlements(self._tier)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _make_session(*, flush_raises: bool, existing_count: int = 0) -> AsyncMock:
    # Contract-correct double: the RLS preamble reads ``in_transaction()`` /
    # ``get_bind()`` / ``add()`` synchronously — a bare AsyncMock turns each
    # call into a never-awaited coroutine. The execute result is a plain
    # ``MagicMock`` below.
    session = configure_rls_preamble(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    count_result = MagicMock()
    count_result.scalar_one.return_value = existing_count
    session.execute = AsyncMock(return_value=count_result)

    if flush_raises:
        session.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("UNIQUE constraint failed")))
    else:
        session.flush = AsyncMock(return_value=None)
    return session


class _MockFactory:
    """Reusable async-context session factory double."""

    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> "_MockFactory":
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        pass


def _build_client(
    *,
    flush_raises: bool,
    plan: PlanStub,
    role: str = "admin",
    existing_count: int = 0,
) -> Generator[TestClient, None, None]:
    """Build a TestClient with the error-route dependencies overridden.

    ``flush_raises`` controls whether the session flush simulates a unique
    violation; ``plan`` selects the feature-gate plan context; ``role`` sets the
    caller's org role (FAR-2683 authz tests); ``existing_count`` is what the
    per-org count query reports (FAR-2698 cap tests).
    """
    session = _make_session(flush_raises=flush_raises, existing_count=existing_count)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=role,
    )
    app.dependency_overrides[get_plan_context] = lambda: plan

    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    yield from _build_client(flush_raises=True, plan=all_features())


def test_create_rule_maps_unique_violation_to_422(client: TestClient) -> None:
    """IntegrityError (migration 0117 unique backstop) must surface as 422."""
    resp = client.post(
        "/api/v1/errors/notification-rules",
        json={"name": "dup-rule"},
    )

    assert resp.status_code == 422


# ── FAR-2698: the cap is a flat licence limit; the flag is availability only ──
#
# Option 2 (stated in the PR): once FAR-2665 made ``error_tracking`` a Community
# behaviour flag, the tier-differentiated cap had no entitlement behind it, so
# every plan now gets the single flat limit of 10. The flag governs AVAILABILITY
# only — switching it off returns 402 and must never lower the cap.


_CREATE_IN_APP = {"name": "in-app-rule", "action_type": "in_app"}
_CREATE_WEBHOOK = {"name": "webhook-rule", "action_type": "webhook", "webhook_url": "https://example.com/hook"}


@pytest.mark.parametrize("tier", ["community", "team"])
def test_limit_is_ten_on_every_plan_with_the_flag_on(tier: str) -> None:
    """A 10th rule is admitted and an 11th refused, whatever the licence tier.

    The Community arm is the discriminator: a cap keyed on the behaviour flag or
    on a Community tier would refuse the 10th rule at 3.
    """
    for tc in _build_client(flush_raises=False, plan=_FlagPlan(tier=tier, error_tracking=True), existing_count=9):
        assert tc.post("/api/v1/errors/notification-rules", json=_CREATE_IN_APP).status_code == 201
    for tc in _build_client(flush_raises=False, plan=_FlagPlan(tier=tier, error_tracking=True), existing_count=10):
        resp = tc.post("/api/v1/errors/notification-rules", json=_CREATE_IN_APP)
        assert resp.status_code == 422
        assert "Maximum 10" in resp.text


@pytest.mark.parametrize("tier", ["community", "team"])
def test_webhook_rule_is_allowed_on_every_plan_when_available(tier: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Webhook rules are no longer tier-refused (the 402 was the old Team gate)."""
    monkeypatch.setattr(rules_module, "_validate_webhook_url", AsyncMock())
    for tc in _build_client(flush_raises=False, plan=_FlagPlan(tier=tier, error_tracking=True)):
        resp = tc.post("/api/v1/errors/notification-rules", json=_CREATE_WEBHOOK)
        assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("tier", ["community", "team"])
def test_flag_off_removes_availability_on_every_plan(tier: str) -> None:
    """Turning the kill switch off is a 402 on every plan.

    A Team plan with the flag off is refused on AVAILABILITY; it never falls
    back to a lower "community" limit, because the cap is not derived from the
    flag at all (see ``test_cap_is_not_derived_from_a_behaviour_flag``).
    """
    for tc in _build_client(flush_raises=False, plan=_FlagPlan(tier=tier, error_tracking=False), existing_count=0):
        assert tc.post("/api/v1/errors/notification-rules", json=_CREATE_IN_APP).status_code == 402
    for tc in _build_client(flush_raises=False, plan=_FlagPlan(tier=tier, error_tracking=False)):
        assert tc.get("/api/v1/errors/notification-rules").status_code == 402


async def test_flat_cap_admits_the_tenth_rule_without_a_plan_context() -> None:
    """The handler's cap takes no plan input at all (direct-call isolation).

    Bypassing the availability dependency proves the LIMIT does not consult the
    behaviour flag: with nine rules present the tenth is admitted, so a flag-off
    paid install can never be pushed down to the old Community limit of 3.
    """
    session = _make_session(flush_raises=False, existing_count=9)
    principal = TenantPrincipal(username="u", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin")
    with patch.object(rules_module, "set_rls_org", AsyncMock()):
        created = await rules_module.create_notification_rule(
            rules_module.ErrorNotificationRuleCreate(name="tenth", action_type="in_app"),
            session,
            principal,
        )
    assert created["name"] == "tenth"


def test_cap_is_not_derived_from_a_behaviour_flag() -> None:
    """FAR-2698 / ADR 069: a behaviour flag must never pick the licence limit.

    This fails if the route reintroduces the old ``feature_enabled`` licence
    branch — the exact regression that let a FAR-2665 re-tiering silently move a
    paid limit. Availability is gated by ``require_feature``; the limit is a
    constant.
    """
    import inspect

    source = inspect.getsource(rules_module)
    code_lines = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    assert "feature_enabled(" not in "\n".join(code_lines)
    assert rules_module._MAX_RULES_PER_ORG == 10
    assert not hasattr(rules_module, "_MAX_RULES_COMMUNITY")


def test_create_webhook_url_private_ip_rejected(client: TestClient) -> None:
    """SSRF-fail-closed: a loopback webhook_url must be rejected with 422."""
    resp = client.post(
        "/api/v1/errors/notification-rules",
        json={
            "name": "ssrf-rule",
            "action_type": "webhook",
            "webhook_url": "http://127.0.0.1:8080/hook",
        },
    )

    assert resp.status_code == 422
    assert "webhook_url rejected" in resp.json()["detail"]


def test_create_webhook_url_metadata_ip_rejected(client: TestClient) -> None:
    """The cloud-metadata address must be refused before any DB write."""
    resp = client.post(
        "/api/v1/errors/notification-rules",
        json={
            "name": "ssrf-rule",
            "action_type": "webhook",
            "webhook_url": "http://169.254.169.254/latest/meta-data",
        },
    )

    assert resp.status_code == 422
    assert "webhook_url rejected" in resp.json()["detail"]


def test_update_webhook_url_private_ip_rejected(client: TestClient) -> None:
    """SSRF-fail-closed: the update path validates a new webhook_url too."""
    resp = client.put(
        f"/api/v1/errors/notification-rules/{uuid.uuid4()}",
        json={"webhook_url": "http://[::1]:9000/hook"},
    )

    assert resp.status_code == 422
    assert "webhook_url rejected" in resp.json()["detail"]


# ── FAR-2683: the list read must carry the admin floor ─────────────────────
#
# ``GET /api/v1/errors/notification-rules`` returns each rule's ``webhook_url``
# — admin-class delivery configuration. Every write to the resource already
# requires ``error_notification.manage`` (admin); the read now reuses that same
# floor so any org member (viewer/operator/runner) can no longer enumerate
# where alerts are delivered.


def _assert_admin_only_list_refused(tc: TestClient) -> None:
    resp = tc.get("/api/v1/errors/notification-rules")
    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert "error_notification.manage" in detail
    assert "admin" in detail


def test_list_rules_viewer_forbidden() -> None:
    for tc in _build_client(flush_raises=False, plan=all_features(), role="viewer"):
        _assert_admin_only_list_refused(tc)


def test_list_rules_runner_forbidden() -> None:
    for tc in _build_client(flush_raises=False, plan=all_features(), role="runner"):
        _assert_admin_only_list_refused(tc)


def test_list_rules_operator_forbidden() -> None:
    for tc in _build_client(flush_raises=False, plan=all_features(), role="operator"):
        _assert_admin_only_list_refused(tc)


def test_list_rules_admin_succeeds() -> None:
    for tc in _build_client(flush_raises=False, plan=all_features(), role="admin"):
        resp = tc.get("/api/v1/errors/notification-rules")
        assert resp.status_code == 200, resp.text
        assert not resp.json()["items"]


def test_serialize_rule_returns_full_webhook_url_for_admin() -> None:
    """The admin-only read deliberately returns the full webhook_url.

    Admins already receive it in full from create/update (the same
    ``_serialize_rule`` feeds those responses), so redacting it for the only
    principals who can reach the list would add friction with no security
    gain — and would break the admin edit round-trip. This pins that decision.
    """
    from farna.api.routes.error_notification_rules import _serialize_rule

    rule = MagicMock()
    rule.id = uuid.uuid4()
    rule.name = "hooks"
    rule.enabled = True
    rule.condition_level = "error"
    rule.condition_min_count = 1
    rule.condition_window_seconds = 300
    rule.action_type = "webhook"
    rule.webhook_url = "https://hooks.example.com/services/T000/B000/secret"
    rule.cooldown_seconds = 300
    rule.created_at = None
    rule.updated_at = None

    assert _serialize_rule(rule)["webhook_url"] == "https://hooks.example.com/services/T000/B000/secret"
