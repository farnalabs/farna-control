"""Route-level / integration tests for the product analytics endpoints (FAR-354).

These exercises the real FastAPI handlers (POST /consent, GET, PUT) end-to-end
through ``TestClient`` with a mocked DB session, so the permission gate, the
prompt-eligibility 409, and the single-statement atomic settings-patch write
path (FAR-1832; previously a ``FOR UPDATE`` read-modify-write) are all
round-tripped.

The DB session is mocked, so the org row is a MagicMock whose ``settings_json``
persists across requests within a single test — that lets us assert state
transitions (accept -> GET reflects level=all, re-accept -> 409).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from farna.api.constants import MSG_ORGANISATION_NOT_FOUND
from farna.api.dependencies import get_db_session
from farna.api.routes import product_analytics as pa_module
from farna.api.routes import product_analytics_transparency as pat_module
from farna.api.routes.product_analytics import router as product_analytics_router
from farna.api.routes.product_analytics_transparency import (
    _stale_dump_warning,
)
from farna.api.routes.product_analytics_transparency import (
    router as transparency_router,
)
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.core.product_analytics.constants import (
    DUMP_LAST_FAILURE_AT_KEY,
    DUMP_LAST_FAILURE_SUMMARY_KEY,
    DUMP_WATERMARK_KEY,
    INSTANCE_SWITCH_KEY,
    LEVEL_ALL,
    LEVEL_OFF,
)

# Mount only the product-analytics router on a minimal app so we exercise the
# real handlers through TestClient without pulling in the full API surface
# (and its optional extras such as the MCP server).
app = FastAPI()
app.include_router(product_analytics_router)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_CONSENT_URL = "/api/v1/org/product-analytics/consent"
_PRODUCT_ANALYTICS_URL = "/api/v1/org/product-analytics"


def _make_session() -> tuple[AsyncMock, MagicMock]:
    """Return (session, org) where org.settings_json persists across requests."""
    org = MagicMock()
    org.settings_json = {}
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    execute_result = MagicMock()
    execute_result.scalar_one_or_none = MagicMock(return_value=org)
    session.execute = AsyncMock(return_value=execute_result)
    # ``set_rls_org`` is stubbed in the ``env`` fixture (it would touch the
    # bind), so bind the org context directly: the FAR-1832 settings patch the
    # writers go through reads it back through its strict fail-closed tenant
    # guard, and an unbound session is refused (500) by design.
    session.info = {"org_id": str(_ORG_ID)}
    return session, org


def _principal(org_role: str) -> TenantPrincipal:
    return TenantPrincipal(
        username="tester",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )


@pytest.fixture
def env():
    """Wire a mocked session + no-op side-effect deps into the app."""
    session, org = _make_session()
    with (
        patch.object(pa_module, "set_rls_org", new=AsyncMock()),
        patch.object(pa_module, "append_audit_event", new=AsyncMock()),
        patch.object(pa_module, "is_instance_analytics_enabled", new=AsyncMock(return_value=False)),
    ):

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[get_current_tenant_user] = lambda: _principal("admin")
        yield session, org
        app.dependency_overrides.clear()


def _client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /consent — accept path + prompt-eligibility gate (409)
# ---------------------------------------------------------------------------


class TestConsentEndpoint:
    def test_accept_returns_all_and_prompted(self, env) -> None:
        client = _client()
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "all"
        assert body["prompted"] == "yes"
        assert body["instance_enabled"] is False
        assert body["egress_allowed"] is False

    def test_reaccept_after_accept_is_409(self, env) -> None:
        client = _client()
        first = client.post(_CONSENT_URL, json={"action": "accept"})
        assert first.status_code == 200
        second = client.post(_CONSENT_URL, json={"action": "accept"})
        assert second.status_code == 409, second.text

    def test_decline_then_consent_ineligible_409(self, env) -> None:
        client = _client()
        decline = client.post(_CONSENT_URL, json={"action": "decline"})
        assert decline.status_code == 200
        # declined -> prompted=no (sticky) -> not eligible -> 409 on a later accept
        retry = client.post(_CONSENT_URL, json={"action": "accept"})
        assert retry.status_code == 409, retry.text

    def test_dismiss_then_accept_still_409_within_cooldown(self, env) -> None:
        client = _client()
        dismiss = client.post(_CONSENT_URL, json={"action": "dismiss"})
        assert dismiss.status_code == 200
        retry = client.post(_CONSENT_URL, json={"action": "accept"})
        assert retry.status_code == 409, retry.text

    def test_consent_write_patches_only_its_block_without_prelatching(self, env) -> None:
        """FAR-1832 QA F1: the consent writer delegates to the atomic settings
        patch with a NESTED sub-field patch for ``product_analytics`` only —
        composed from the request alone — and issues no ``FOR UPDATE`` org
        select of its own.

        Fails against the old shape two ways: the old code never called the
        patch helper (``captured`` stays empty) and its existence read took
        ``FOR UPDATE``. The helper is faked so its generic-dialect fallback
        lock cannot mask the writer's own statements.
        """
        session, org = env
        captured: dict[str, object] = {}

        async def _fake_patch(_session, org_id, patch=None, delete_keys=None, **kwargs):
            captured["patch"] = patch
            captured["delete_keys"] = delete_keys
            captured["nested_patch"] = kwargs.get("nested_patch")
            merged = dict(org.settings_json or {})
            for nested_key, nested_sub in (kwargs.get("nested_patch") or {}).items():
                sub = dict(merged.get(nested_key) or {})
                sub.update(nested_sub)
                merged[nested_key] = sub
            merged.update(patch or {})
            org.settings_json = merged
            return merged

        with patch("farna.api.routes._org_settings.update_org_settings_patch", new=_fake_patch):
            resp = _client().post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 200, resp.text
        assert captured["patch"] is None
        assert set(captured["nested_patch"]) == {"product_analytics"}
        assert captured["delete_keys"] is None
        assert org.settings_json["product_analytics"]["level"] == "all"
        assert org.settings_json["product_analytics"]["prompted"] == "yes"
        for call in session.execute.await_args_list:
            assert "FOR UPDATE" not in str(call.args[0]), f"writer must not pre-lock the org row: {call.args[0]}"


# ---------------------------------------------------------------------------
# GET / — reflects committed consent state
# ---------------------------------------------------------------------------


class TestGetEndpoint:
    def test_get_after_accept_reflects_level_all(self, env) -> None:
        client = _client()
        accept = client.post(_CONSENT_URL, json={"action": "accept"})
        assert accept.status_code == 200
        resp = client.get(_PRODUCT_ANALYTICS_URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "all"
        assert body["prompted"] == "yes"

    def test_get_default_state(self, env) -> None:
        client = _client()
        resp = client.get(_PRODUCT_ANALYTICS_URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["level"] == "off"
        assert body["prompted"] is None


# ---------------------------------------------------------------------------
# PUT / — admin-only level toggle (permission gate)
# ---------------------------------------------------------------------------


class TestUpdateLevelEndpoint:
    def test_put_by_admin_succeeds(self, env) -> None:
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == "all"

    def test_put_by_non_admin_is_403(self, env) -> None:
        app.dependency_overrides[get_current_tenant_user] = lambda: _principal("member")
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 403, resp.text

    def test_put_invalid_level_422(self, env) -> None:
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "bogus"})
        # The pydantic Literal validation rejects an invalid level before the
        # handler runs, so FastAPI returns 422 Unprocessable Entity.
        assert resp.status_code == 422, resp.text

    def test_put_level_patches_only_its_block_without_prelatching(self, env) -> None:
        """FAR-1832 QA F1: the admin level toggle delegates to the atomic
        settings patch with a NESTED sub-field patch for ``product_analytics``
        only — composed from the request alone — no ``FOR UPDATE`` pre-lock,
        no whole-block write (same contract as the consent POST)."""
        session, org = env
        captured: dict[str, object] = {}

        async def _fake_patch(_session, org_id, patch=None, delete_keys=None, **kwargs):
            captured["patch"] = patch
            captured["nested_patch"] = kwargs.get("nested_patch")
            merged = dict(org.settings_json or {})
            for nested_key, nested_sub in (kwargs.get("nested_patch") or {}).items():
                sub = dict(merged.get(nested_key) or {})
                sub.update(nested_sub)
                merged[nested_key] = sub
            merged.update(patch or {})
            org.settings_json = merged
            return merged

        with patch("farna.api.routes._org_settings.update_org_settings_patch", new=_fake_patch):
            resp = _client().put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 200, resp.text
        assert captured["patch"] is None
        assert set(captured["nested_patch"]) == {"product_analytics"}
        # The subpatch carries ONLY this toggle's sub-fields — never the
        # consent writer's prompted* fields (a whole-block replace would).
        nested = captured["nested_patch"]
        assert isinstance(nested, dict)
        subpatch = nested["product_analytics"]
        assert set(subpatch) == {"level", "level_changed_at"}
        assert org.settings_json["product_analytics"]["level"] == "all"
        for call in session.execute.await_args_list:
            assert "FOR UPDATE" not in str(call.args[0]), f"writer must not pre-lock the org row: {call.args[0]}"


# ---------------------------------------------------------------------------
# Staging purge on consent withdrawal (FAR-1717)
# ---------------------------------------------------------------------------


class TestStagingPurgeOnOptOut:
    """Landing on level=off must purge the org's ``metrics_staging`` rows.

    Before FAR-1717 the PUT handler's docstring deferred this to FAR-355 and
    nothing deleted staged rows on withdrawal. These tests fail against that
    code: no DELETE statement on ``metrics_staging`` is ever executed.
    """

    @staticmethod
    def _staging_deletes(session: AsyncMock) -> list:
        from sqlalchemy.sql.dml import Delete

        return [call.args[0] for call in session.execute.await_args_list if call.args and isinstance(call.args[0], Delete) and call.args[0].table.name == "metrics_staging"]

    def test_put_level_off_purges_staged_events(self, env) -> None:
        session, _org = env
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "off"})
        assert resp.status_code == 200, resp.text
        assert self._staging_deletes(session), "PUT level=off must DELETE the org's staged events"

    def test_put_level_all_does_not_purge(self, env) -> None:
        session, _org = env
        client = _client()
        resp = client.put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 200, resp.text
        assert not self._staging_deletes(session), "PUT level=all must not purge staged events"

    def test_consent_decline_purges_staged_events(self, env) -> None:
        session, _org = env
        client = _client()
        resp = client.post(_CONSENT_URL, json={"action": "decline"})
        assert resp.status_code == 200, resp.text
        assert self._staging_deletes(session), "declining consent must DELETE the org's staged events"

    def test_consent_accept_does_not_purge(self, env) -> None:
        session, _org = env
        client = _client()
        resp = client.post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 200, resp.text
        assert not self._staging_deletes(session), "accepting consent must not purge staged events"


# ---------------------------------------------------------------------------
# FAR-1832 QA F11: BOTH 404 paths return the SAME shared detail body.
# The org-missing path is raised by ``_get_org_or_404`` and the patch-refusal
# path (soft-deleted org) by the shared route helper. Before F11 the former
# appended a trailing period and the latter did not, so the same endpoint
# answered two different bodies for the same "no usable org" condition.
# ---------------------------------------------------------------------------


class TestNotFoundBodies:
    def test_consent_missing_org_returns_shared_404_detail(self, env) -> None:
        session, _org = env
        session.execute.return_value.scalar_one_or_none.return_value = None
        resp = _client().post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == MSG_ORGANISATION_NOT_FOUND

    def test_consent_patch_refusal_returns_shared_404_detail(self, env) -> None:
        async def _refuse(*_args, **_kwargs):
            raise LookupError("organisation not found")

        with patch("farna.api.routes._org_settings.update_org_settings_patch", new=_refuse):
            resp = _client().post(_CONSENT_URL, json={"action": "accept"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == MSG_ORGANISATION_NOT_FOUND

    def test_put_level_missing_org_returns_shared_404_detail(self, env) -> None:
        session, _org = env
        session.execute.return_value.scalar_one_or_none.return_value = None
        resp = _client().put(_PRODUCT_ANALYTICS_URL, json={"level": "all"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == MSG_ORGANISATION_NOT_FOUND


# ---------------------------------------------------------------------------
# FAR-2740: transparency carries the dump failure and the never-dumped warning
# ---------------------------------------------------------------------------

_TAPP = FastAPI()
_TAPP.include_router(transparency_router)
_TURL = "/api/v1/product-analytics/transparency"

_T_SYS_ADMIN = AuthenticatedPrincipal(
    username="ops@test",
    organisation_id=uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
    account_id=uuid.UUID("00000000-0000-0000-0000-0000000000bb"),
    org_role="admin",
    is_system_admin=True,
)
_T_ORG_ALL = {"product_analytics": {"level": LEVEL_ALL}}


def _t_config(value: object) -> MagicMock:
    config = MagicMock()
    config.value = value
    return config


def _t_session(org_settings: object) -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    result = MagicMock()
    org = MagicMock()
    org.settings_json = org_settings
    result.scalar_one_or_none = MagicMock(return_value=org)
    session.execute = AsyncMock(return_value=result)
    return session


def _t_get_config(values: dict[str, object]):
    async def _get(_session: object, key: str) -> MagicMock | None:
        if key in values:
            return _t_config(values[key])
        return None

    return _get


def _t_request(
    monkeypatch: pytest.MonkeyPatch,
    *,
    route_values: dict[str, object] | None = None,
    switch_values: dict[str, object] | None = None,
    org_settings: object = _T_ORG_ALL,
) -> dict[str, object]:
    session = _t_session(org_settings)
    _TAPP.dependency_overrides[get_db_session] = lambda: session
    _TAPP.dependency_overrides[get_current_user] = lambda: _T_SYS_ADMIN
    monkeypatch.delenv("FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED", raising=False)
    try:
        with (
            patch.object(
                pat_module,
                "get_config",
                new=AsyncMock(side_effect=_t_get_config(route_values or {})),
            ),
            patch(
                "farna.core.product_analytics.consent.get_config",
                new=AsyncMock(side_effect=_t_get_config(switch_values or {})),
            ),
            patch("farna.core.product_analytics.consent.get_license", lambda: None),
        ):
            resp = TestClient(_TAPP).get(_TURL)
    finally:
        _TAPP.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


class TestTransparencyDumpFailureFields:
    def test_response_carries_the_failure_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _t_request(
            monkeypatch,
            route_values={
                DUMP_LAST_FAILURE_AT_KEY: "2026-11-24T10:00:00+00:00",
                DUMP_LAST_FAILURE_SUMMARY_KEY: "status=500: vendor boom",
            },
        )

        assert body["last_failed_dump_at"] == "2026-11-24T10:00:00+00:00"
        assert body["last_dump_failure"] == "status=500: vendor boom"

    def test_absent_failure_record_reads_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = _t_request(monkeypatch)

        assert body["last_failed_dump_at"] is None
        assert body["last_dump_failure"] is None

    def test_never_dumped_opted_in_instance_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The production state FAR-2740 fixes: switch on, consent given, zero
        successful dumps. It must warn (it previously returned no warning)."""
        body = _t_request(monkeypatch, switch_values={INSTANCE_SWITCH_KEY: True})

        assert body["instance_enabled"] is True
        assert body["last_successful_dump_at"] is None
        assert body["warning"] == "never_reached_farnalabs"

    def test_stale_previous_success_keeps_the_old_warning_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale = (datetime.now(UTC) - timedelta(days=4)).isoformat()
        body = _t_request(
            monkeypatch,
            route_values={DUMP_WATERMARK_KEY: stale},
            switch_values={INSTANCE_SWITCH_KEY: True},
        )

        assert body["warning"] == "not_reaching_farnalabs"


class TestStaleDumpWarningNeverDumped:
    def test_never_dumped_requires_switch_and_consent(self) -> None:
        assert _stale_dump_warning(None, LEVEL_ALL, instance_enabled=True) == "never_reached_farnalabs"
        assert _stale_dump_warning(None, LEVEL_ALL, instance_enabled=False) is None
        assert _stale_dump_warning(None, LEVEL_OFF, instance_enabled=True) is None

    def test_never_dumped_honours_the_consent_grace_window(self) -> None:
        """A recently opted-in instance is quiet until the threshold elapses."""
        recent = datetime.now(UTC) - timedelta(hours=6)
        assert _stale_dump_warning(None, LEVEL_ALL, instance_enabled=True, consent_since=recent) is None
        old = datetime.now(UTC) - timedelta(days=4)
        assert _stale_dump_warning(None, LEVEL_ALL, instance_enabled=True, consent_since=old) == "never_reached_farnalabs"

    def test_stale_previous_success_requires_the_switch(self) -> None:
        stale = (datetime.now(UTC) - timedelta(days=4)).isoformat()
        assert _stale_dump_warning(stale, LEVEL_ALL, instance_enabled=True) == "not_reaching_farnalabs"
        # Turning analytics off silences the warning rather than leaving it
        # forever (FAR-2740).
        assert _stale_dump_warning(stale, LEVEL_ALL, instance_enabled=False) is None

    def test_fresh_dump_never_warns(self) -> None:
        fresh = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        assert _stale_dump_warning(fresh, LEVEL_ALL, instance_enabled=True) is None
