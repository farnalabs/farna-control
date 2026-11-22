"""Tests for the admin org run-concurrency endpoint contract.

Mirrors ``test_admin_sandbox_concurrency.py`` for the ``run_concurrency_limit``
org setting. The two caps are independent org settings that both produce the
``org_capacity_limited`` marker on deferred runs, but they are configured via
distinct endpoints.

The mocked org session and the authenticated clients are shared with the
sandbox-concurrency contract in ``org_settings_concurrency_harness``; the
endpoint, payloads and expected bodies stay here.
"""

from unittest.mock import patch

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
async def test_get_defaults_to_null(client_admin):
    resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": None}


@pytest.mark.anyio
async def test_put_sets_limit(client_admin, mock_session):
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 5},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": 5}
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    assert org.settings_json["run_concurrency_limit"] == 5


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [0, -1, 101])
async def test_put_rejects_out_of_range(client_admin, bad):
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": bad},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_put_null_clears_limit(client_admin, org_settings):
    org_settings["run_concurrency_limit"] = 3
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"run_concurrency_limit": None}


@pytest.mark.anyio
async def test_put_merge_preserves_other_settings(client_admin, mock_session):
    org = mock_session.execute.return_value.scalar_one_or_none.return_value
    org.settings_json = {"license_key": "license-abc", "sandbox_concurrency_limit": 4}
    resp = await client_admin.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 7},
    )
    assert resp.status_code == 200
    assert org.settings_json["license_key"] == "license-abc"
    assert org.settings_json["sandbox_concurrency_limit"] == 4
    assert org.settings_json["run_concurrency_limit"] == 7


@pytest.mark.anyio
async def test_put_patches_only_its_own_settings_key(client_admin, mock_session):
    """FAR-1830: the writer emits ONLY ``run_concurrency_limit``.

    Asserting the patch call arguments fails if the route reverts to a
    whole-column ``settings_json`` write.
    """
    captured = {}

    async def _fake_patch(session, organisation_id, patch=None, delete_keys=None, **kwargs):
        captured["organisation_id"] = organisation_id
        captured["patch"] = patch
        return patch or {}

    with patch("farna.api.routes.admin.update_org_settings_patch", new=_fake_patch):
        resp = await client_admin.put(
            "/api/v1/admin/org/run-concurrency",
            json={"run_concurrency_limit": 9},
        )
    assert resp.status_code == 200
    assert captured["organisation_id"] == ORG_ID
    assert set(captured["patch"]) == {"run_concurrency_limit"}
    assert captured["patch"]["run_concurrency_limit"] == 9


@pytest.mark.anyio
async def test_viewer_forbidden_on_get(client_viewer):
    resp = await client_viewer.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_viewer_forbidden_on_put(client_viewer):
    resp = await client_viewer.put(
        "/api/v1/admin/org/run-concurrency",
        json={"run_concurrency_limit": 3},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_get_programming_error_returns_501(client_admin):
    """Migration-required errors on GET surface as 501."""
    with patch(
        "farna.api.routes.admin.get_org_run_concurrency_limit",
        new=raise_from(ProgrammingError("stmt", {}, Exception("missing table"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 501


@pytest.mark.anyio
async def test_get_sqlalchemy_error_returns_503(client_admin):
    """Generic SQLAlchemy errors on GET surface as 503."""
    with patch(
        "farna.api.routes.admin.get_org_run_concurrency_limit",
        new=raise_from(SQLAlchemyError("mock", {}, "")),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 503


@pytest.mark.anyio
async def test_get_integrity_error_returns_503_not_409(client_admin):
    """The read-only GET has no IntegrityError→409 'already exists' mapping."""
    with patch(
        "farna.api.routes.admin.get_org_run_concurrency_limit",
        new=raise_from(IntegrityError("stmt", {}, Exception("fk violation"))),
    ):
        resp = await client_admin.get("/api/v1/admin/org/run-concurrency")
    assert resp.status_code == 503
    assert "already exists" not in resp.text
