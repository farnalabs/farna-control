"""Tests for the admin org sandbox-concurrency endpoint contract.

The mocked org session and the authenticated clients are shared with the
run-concurrency contract in ``org_settings_concurrency_harness``; the endpoint,
payloads and expected bodies stay here.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

# ``mock_session`` / ``client_admin`` / ``client_viewer`` / ``org_settings`` are
# pytest fixtures, collected from this module's NAMESPACE - importing them is
# what makes the pair share one definition instead of two copies. The tuple
# below keeps the imports referenced for ruff (they otherwise appear only in
# test signatures) and doubles as the list of what is pulled in.
from tests.unit.api.org_settings_concurrency_harness import (
    ORG_ID,
    client_admin,
    client_viewer,
    mock_session,
    org_settings,
    raise_from,
)

_HARNESS_FIXTURES = (client_admin, client_viewer, mock_session, org_settings)


@pytest.mark.anyio
async def test_get_absent_key_resolves_docker_tier_default(client_admin):
    """FAR-589 D3b: an ABSENT key returns the Docker-tier default 4 with
    ``is_default=True`` so the UI can render "4 (default)"."""
    resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": 4, "is_default": True}


@pytest.mark.anyio
async def test_get_explicit_value_is_not_default(client_admin, org_settings):
    org_settings["sandbox_concurrency_limit"] = 7
    resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": 7, "is_default": False}


@pytest.mark.anyio
async def test_get_explicit_null_is_no_gate_not_default(client_admin, org_settings):
    """An explicit ``null`` (no gate) must stay distinguishable from the absent
    Docker-tier default."""
    org_settings["sandbox_concurrency_limit"] = None
    resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": None, "is_default": False}


@pytest.mark.anyio
async def test_get_stored_zero_reads_zero(client_admin, org_settings):
    org_settings["sandbox_concurrency_limit"] = 0
    resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": 0, "is_default": False}


@pytest.mark.anyio
async def test_put_sets_limit(client_admin, mock_session):
    resp = await client_admin.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": 5, "is_default": False}
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json["sandbox_concurrency_limit"] == 5


@pytest.mark.anyio
async def test_put_patches_only_its_own_key_without_prelatching_the_org_row(client_admin, mock_session):
    """FAR-1832: the writer delegates to the atomic settings patch with ONLY
    its own key and issues no ``FOR UPDATE`` org select of its own.

    This fails against the old shape two ways: the old code never called the
    patch helper (``captured`` stays empty), and it executed a
    ``SELECT ... FOR UPDATE`` on the org row before writing. The audit append
    (which legitimately locks the per-org audit-chain head) is stubbed out so
    the statement scan sees only the writer's own statements.
    """
    captured: dict[str, object] = {}

    async def _fake_patch(session, organisation_id, patch=None, delete_keys=None, **kwargs):
        captured["organisation_id"] = organisation_id
        captured["patch"] = patch
        captured["delete_keys"] = delete_keys
        return dict(patch or {})

    with (
        patch("farna.api.routes._org_settings.update_org_settings_patch", new=_fake_patch),
        patch("farna.api.routes.admin._record_org_audit", new=AsyncMock()),
    ):
        resp = await client_admin.put(
            "/api/v1/admin/org/sandbox-concurrency",
            json={"sandbox_concurrency_limit": 5},
        )
    assert resp.status_code == 200
    assert captured["organisation_id"] == ORG_ID
    assert captured["patch"] == {"sandbox_concurrency_limit": 5}
    assert captured["delete_keys"] is None
    for call in mock_session.execute.await_args_list:
        assert "FOR UPDATE" not in str(call.args[0]), f"writer must not pre-lock the org row: {call.args[0]}"


@pytest.mark.anyio
@pytest.mark.parametrize("value", [0, 100])
async def test_put_accepts_zero_and_hundred(client_admin, mock_session, value):
    """FAR-589 D3b: ``0`` is a meaningful value (deny-all); 100 stays the cap."""
    resp = await client_admin.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": value},
    )
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": value, "is_default": False}
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json["sandbox_concurrency_limit"] == value


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [-1, 101])
async def test_put_rejects_out_of_range(client_admin, bad):
    resp = await client_admin.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": bad},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_put_null_clears_limit(client_admin, org_settings):
    org_settings["sandbox_concurrency_limit"] = 3
    resp = await client_admin.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"sandbox_concurrency_limit": None, "is_default": False}


@pytest.mark.anyio
async def test_put_merge_preserves_other_settings(client_admin, mock_session):
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    org.settings_json = {"license_key": "license-abc", "retention_days": 30}
    resp = await client_admin.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": 7},
    )
    assert resp.status_code == 200
    assert org.settings_json["license_key"] == "license-abc"
    assert org.settings_json["retention_days"] == 30
    assert org.settings_json["sandbox_concurrency_limit"] == 7


@pytest.mark.anyio
async def test_viewer_forbidden_on_get(client_viewer):
    resp = await client_viewer.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_viewer_forbidden_on_put(client_viewer):
    resp = await client_viewer.put(
        "/api/v1/admin/org/sandbox-concurrency",
        json={"sandbox_concurrency_limit": 3},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_get_programming_error_returns_501(client_admin):
    """Migration-required errors on GET surface as 501."""
    with patch(
        "farna.api.routes.admin.get_sandbox_concurrency_limit",
        new=raise_from(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 501


@pytest.mark.anyio
async def test_get_sqlalchemy_error_returns_503(client_admin):
    """Generic SQLAlchemy errors on GET surface as 503."""
    with patch(
        "farna.api.routes.admin.get_sandbox_concurrency_limit",
        new=raise_from(SQLAlchemyError("mock", {}, "")),
    ):
        resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_get_integrity_error_returns_503_not_409(client_admin):
    """The read-only GET has no IntegrityError→409 'already exists' mapping.

    IntegrityError is a subclass of SQLAlchemyError, so it must fall through
    to the 503 branch — a read-only GET can never produce a duplicate-value
    conflict, and must not surface the write-handler 409 boilerplate.
    """
    with patch(
        "farna.api.routes.admin.get_sandbox_concurrency_limit",
        new=raise_from(IntegrityError("stmt", {}, Exception("fk violation"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/sandbox-concurrency")
    assert resp.status_code == 503
    assert "already exists" not in resp.text
