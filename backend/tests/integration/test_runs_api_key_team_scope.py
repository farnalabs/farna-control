"""Real-Postgres proof of the team boundary on the REST run surfaces.

``runs`` carries only org-isolation RLS (no ``visibility`` column), so a
team-scoped ``mk_`` API key's hard team boundary (``TenantPrincipal.team_id``)
must be applied explicitly on every run read. ``Run.owner_team_id`` is the
source of truth, but pre-stamp runs carry NULL and fall back to the pipeline
owner — and ``pipelines`` carries ``rls_team_isolation``, so a CALLER-FACING
fallback read returns NOTHING for another team's ``visibility='team'``
pipeline. Resolving that fallback caller-facing made the guard fail OPEN (the
FAR-1513 boundary bypass): a team-A key could read a team-B run whose stamp was
NULL.

This module exercises the REAL guard against REAL RLS (a non-superuser app
engine plus ``set_rls_org`` / ``set_rls_user_context``) with a real team-scoped
API key resolved through the real auth dependency:

* the pipeline is team-private and owned by team B,
* the key is scoped to team A and its account is NOT a member of team B, so the
  caller-facing pipeline read is empty,
* the run's ``owner_team_id`` is NULL (pre-stamp), so the guard must resolve the
  owner TEAM-BLIND and deny with 404.

The same fixtures prove the positive control: a team-B-scoped key reads the run.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from farna.api.routes import runs as runs_module
from farna.auth.api_key import generate_api_key
from farna.auth.dependencies import get_current_tenant_user_or_api_key
from farna.auth.jwt import TenantPrincipal
from farna.db.crud.team import create_team
from farna.db.crud.team_membership import add_team_member
from farna.settings import Settings

os.environ.setdefault("FARNA_CONTROL_AUTH_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("REDIS_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.xdist_group(name="runs_api_key_team_scope"),
]

_VALID_32 = "a" * 32

_MINIMAL_GRAPH_NODES = [
    {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
    }
]


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_account(db_engine: AsyncEngine, org_id: uuid.UUID, email: str, role: str = "runner") -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) VALUES (:id, :email, :name, 'local', true, 'hash')"),
            {"id": str(account_id), "email": email, "name": f"User {email}"},
        )
        await conn.execute(
            text("INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, :role)"),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id), "role": role},
        )
    return account_id


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, owner_id: uuid.UUID, name: str) -> uuid.UUID:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(session, org_id=org_id, name=name, account_id=owner_id)
        return team.id


async def _seed_team_membership(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    team_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        await add_team_member(session, org_id=org_id, team_id=team_id, account_id=account_id, role="runner")


async def _seed_team_private_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    name: str,
    owner_team_id: uuid.UUID,
) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, "
                "visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, "
                "'{}'::json, (:graph)::json, 'manual_approval', 'team', :tid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "name": name,
                "uid": str(owner_id),
                "graph": json.dumps(_MINIMAL_GRAPH_NODES),
                "tid": str(owner_team_id),
            },
        )
    return pipeline_id


async def _seed_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, "
                "'[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_pre_stamp_run(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    """Insert a run with ``owner_team_id`` left NULL (the pre-stamp state)."""
    run_id = uuid.uuid4()
    snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, status, trigger_type, langgraph_thread_id, input_hash, run_number) VALUES (:id, :oid, :pid, :sid, 'complete', 'manual', :thread, :hash, 1)"),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "thread": f"thread-{run_id.hex}",
                "hash": "0" * 64,
            },
        )
    return run_id


async def _mint_team_scoped_key(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
    name: str,
) -> str:
    """Insert a real team-scoped API key and return its one-time full key."""
    full_key, prefix, hashed = generate_api_key()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO org_api_keys (id, organisation_id, name, lookup_prefix, hashed_secret, role, account_id, team_id, expires_at) VALUES (:id, :oid, :name, :prefix, :hashed, 'runner', :uid, :tid, (CURRENT_TIMESTAMP + INTERVAL '30 days'))"),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "name": name,
                "prefix": prefix,
                "hashed": hashed,
                "uid": str(account_id),
                "tid": str(team_id),
            },
        )
    return full_key


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_engine_globals() -> Generator[None, None, None]:
    """Point the auth dependency's process-global engine at this test's DB."""
    import asyncio

    import farna.api.dependencies as deps

    deps._engine = None
    deps._session_factory = None
    yield
    engine = deps._engine
    deps._engine = None
    deps._session_factory = None
    if engine is not None:
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and not loop.is_closed():
            if loop.is_running():
                loop.create_task(engine.dispose())
            else:
                loop.run_until_complete(engine.dispose())


@pytest.fixture
def auth_settings(db_url: str) -> Settings:
    return Settings(
        database_url=db_url,
        db="postgres",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        auth_rate_limit_enabled=False,
        redis_url="",
    )


@pytest_asyncio.fixture(scope="module")
async def scenario(db_engine: AsyncEngine) -> dict[str, Any]:
    """One org: team B owns a team-private pipeline with a NULL-stamp run.

    Account ``team_a_user`` is a member of team A only, so team B's pipeline is
    invisible to it under ``rls_team_isolation``. Account ``team_b_user`` is a
    member of team B (the positive control).
    """
    org_id = await _seed_org(db_engine, "RunsApiKeyScope")
    owner_id = await _seed_account(db_engine, org_id, f"owner-{org_id.hex[:8]}@runkeyscope.test", role="admin")
    team_a = await _seed_team(db_engine, org_id, owner_id, "team-a")
    team_b = await _seed_team(db_engine, org_id, owner_id, "team-b")
    team_a_user = await _seed_account(db_engine, org_id, f"team-a-{org_id.hex[:8]}@runkeyscope.test")
    team_b_user = await _seed_account(db_engine, org_id, f"team-b-{org_id.hex[:8]}@runkeyscope.test")
    await _seed_team_membership(db_engine, org_id, team_a, team_a_user)
    await _seed_team_membership(db_engine, org_id, team_b, team_b_user)
    pipeline_id = await _seed_team_private_pipeline(db_engine, org_id, owner_id, "runkeyscope-team-b", team_b)
    run_id = await _seed_pre_stamp_run(db_engine, org_id, pipeline_id)
    return {
        "org_id": org_id,
        "team_a": team_a,
        "team_b": team_b,
        "team_a_user": team_a_user,
        "team_b_user": team_b_user,
        "pipeline_id": pipeline_id,
        "run_id": run_id,
    }


async def _resolve_key_principal(full_key: str, settings: Settings) -> TenantPrincipal:
    return await get_current_tenant_user_or_api_key(
        credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=full_key),
        settings=settings,
    )


def _app_factory(app_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_a_key_cannot_read_a_team_b_pre_stamp_run(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    scenario: dict[str, Any],
    auth_settings: Settings,
) -> None:
    """A team-A key gets 404 for a NULL-stamp run on team B's private pipeline."""
    full_key = await _mint_team_scoped_key(
        db_engine,
        scenario["org_id"],
        scenario["team_a_user"],
        scenario["team_a"],
        "team-a-runner",
    )
    principal = await _resolve_key_principal(full_key, auth_settings)
    assert principal.team_id == scenario["team_a"]
    assert principal.via_api_key is True

    with pytest.raises(HTTPException) as exc_info:
        await runs_module.get_run_status(
            scenario["run_id"],
            factory=_app_factory(app_engine),
            principal=principal,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_team_b_key_can_read_its_own_pre_stamp_run(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    scenario: dict[str, Any],
    auth_settings: Settings,
) -> None:
    """The owning team's key still reads the run (positive control)."""
    full_key = await _mint_team_scoped_key(
        db_engine,
        scenario["org_id"],
        scenario["team_b_user"],
        scenario["team_b"],
        "team-b-runner",
    )
    principal = await _resolve_key_principal(full_key, auth_settings)
    assert principal.team_id == scenario["team_b"]

    run, gate_fired = await runs_module._do_get_run_with_gate(
        _app_factory(app_engine),
        principal,
        scenario["run_id"],
    )

    assert run.id == scenario["run_id"]
    assert gate_fired is False
