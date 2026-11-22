"""Composed integration: the org feature override really unlocks System Maps authoring.

FAR-2595 regression: the authoring router used ``require_feature("system_maps")``,
which reads only the tier/licence default from ``get_plan_context``. Because
``system_maps`` is DEFAULT-OFF, the API answered 402 forever while
``GET /api/v1/plan`` (which overlays ``settings_json.feature_overrides``)
reported the flag enabled — an admin toggle showed the UI controls but the API
refused. The MCP discovery tool had the same hole.

This test deliberately keeps the REAL ``get_plan_context`` dependency (no
stubbed ``feature_enabled``): it writes the org override to the database and
drives the real discover route over HTTP, so it fails without the org-aware
gate.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from farna.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


async def _seed_org(db_engine: AsyncEngine, *, overrides: dict[str, bool]) -> uuid.UUID:
    import json

    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, CAST(:settings AS json))"),
            {
                "id": str(org_id),
                "name": f"SystemMapsAuthoringOrg {org_id.hex[:8]}",
                "slug": f"system-maps-authoring-{org_id.hex[:8]}",
                "settings": json.dumps({"feature_overrides": overrides}),
            },
        )
    return org_id


async def _seed_operator(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) VALUES (:id, :email, :name, 'local', true, 'hash')"),
            {"id": str(account_id), "email": f"operator-{org_id.hex[:8]}@test.local", "name": "operator"},
        )
        await conn.execute(
            text("INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'operator')"),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


def _operator_token(org_id: uuid.UUID, account_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"operator-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role="operator",
        is_system_admin=False,
        client_kind="browser",
    )


@pytest.fixture
async def real_plan_client(db_url: str, app_engine: AsyncEngine) -> AsyncGenerator[AsyncClient, None]:
    """ASGI client with the REAL ``get_plan_context`` (no plan stub)."""
    from farna.api.dependencies import _get_engine, get_db_session
    from farna.api.main import app
    from farna.settings import Settings, get_settings

    settings = Settings(
        database_url=db_url,
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        csrf_enabled=False,
        auth_rate_limit_enabled=False,
        redis_url="",
        admin_password="",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as client:
        yield client

    app.dependency_overrides.clear()


async def test_org_override_unlocks_discovery_through_the_real_plan_context(
    db_engine: AsyncEngine,
    real_plan_client: AsyncClient,
) -> None:
    """An org admin enabling ``system_maps`` really unlocks discovery (201)."""
    org_id = await _seed_org(db_engine, overrides={"system_maps": True})
    account_id = await _seed_operator(db_engine, org_id)
    headers = {"Authorization": f"Bearer {_operator_token(org_id, account_id)}"}

    response = await real_plan_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"Override Unlocked {uuid.uuid4().hex[:8]}"},
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["published"] is False
    assert body["draft_map_id"]


async def test_discovery_is_refused_without_the_org_override(
    db_engine: AsyncEngine,
    real_plan_client: AsyncClient,
) -> None:
    """The same operator with no override is refused with feature_required (402)."""
    org_id = await _seed_org(db_engine, overrides={})
    account_id = await _seed_operator(db_engine, org_id)
    headers = {"Authorization": f"Bearer {_operator_token(org_id, account_id)}"}

    response = await real_plan_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"No Override {uuid.uuid4().hex[:8]}"},
        headers=headers,
    )

    assert response.status_code == 402, response.text
    problem = response.json()
    assert problem["type"] == "urn:problem:modulo:feature_required"
    assert problem["instance"] == "system_maps"
