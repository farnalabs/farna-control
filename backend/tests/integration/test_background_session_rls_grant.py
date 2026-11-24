"""ADR 070 §3 — the notifier's status read survives a TEAM-scoped run, real Postgres.

The proof case from the ADR is the notifier: ``_run_is_terminal`` reads
``Run.status`` with org context only and deliberately FAILS OPEN on a missing
row. Once the runs family carries the team predicate (Phase 1B) an org-only
session reads zero rows for a team-owned run, so the guard would silently
conclude "not terminal" for exactly the runs it exists to suppress, and the
``hitl_awaiting`` post-terminal re-emission would go out again.

This module proves, against the migrated database with a NON-superuser (RLS
applied) engine, that:

* the team-owned run EXISTS (superuser read) — so a passing arm below cannot be
  explained by "there was nothing to find";
* an ORG-ONLY session cannot see a team-private row — the grant is load-bearing,
  not vacuous;
* ``background_session`` (org + ``app.execution_context``) DOES see it, and the
  notifier's own ``_run_is_terminal`` — which acquires its session there — still
  reads the team-owned run as terminal.

Scope note: today only the eleven declared ``rls_team_isolation`` tables carry
the team predicate; ``runs`` is org-only until Phase 1B. So the "cannot see it"
arm is asserted on the team-private PIPELINE the run belongs to (the table
family ADR 070 §4 derives run ownership from), and the run arm pins the
notifier's read path, which the conversion changed. When 1B flips ``runs``, this
module's run arm keeps passing and the pipeline arm keeps proving the grant.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from farna.core.notifier import Notifier
from farna.db.background import background_session
from farna.db.rls import set_rls_org

pytestmark = pytest.mark.integration


def _fernet_key() -> str:
    return Fernet.generate_key().decode()


async def _seed_team(db_engine: AsyncEngine, org_id: uuid.UUID, owner_id: uuid.UUID, label: str) -> uuid.UUID:
    """A committed team in ``org_id`` (RLS is bypassed on the migrator engine)."""
    from farna.db.crud.team import create_team

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(text("SELECT set_config('app.organisation_id', :oid, true)"), {"oid": str(org_id)})
        team = await create_team(
            session,
            org_id=org_id,
            name=f"bg-session-{label}-{uuid.uuid4().hex[:6]}",
            account_id=owner_id,
        )
        return team.id


async def _insert_pipeline(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    team_id: uuid.UUID,
) -> uuid.UUID:
    """A committed TEAM-PRIVATE pipeline owned by ``team_id`` (RLS-hidden elsewhere)."""
    pipeline_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, "
                "lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, "
                "graph_nodes_json, default_autonomy_level, visibility, owner_team_id) "
                "VALUES (:id, :oid, :name, :aid, 10, 30, 300, '{}'::json, '[]'::json, "
                "'manual_approval', 'team', :tid)"
            ),
            {
                "id": str(pipeline_id),
                "oid": str(org_id),
                "aid": str(account_id),
                "tid": str(team_id),
                "name": f"bg-session-{pipeline_id.hex[:8]}",
            },
        )
    return pipeline_id


async def _insert_snapshot(db_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with db_engine.begin() as conn:
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


async def _insert_team_owned_run(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    team_id: uuid.UUID,
    *,
    status: str,
) -> uuid.UUID:
    """A committed run whose OWNER is team A (``runs.owner_team_id``)."""
    run_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, input_hash, langgraph_thread_id, run_number, owner_team_id) VALUES (:id, :oid, :pid, :sid, 'manual', :status, :hash, :thread, 1, :tid)"),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "status": status,
                "hash": "bg-session-hash",
                "thread": f"bg-session-{run_id.hex}",
                "tid": str(team_id),
            },
        )
    return run_id


async def _pipeline_row_exists(db_engine: AsyncEngine, pipeline_id: uuid.UUID) -> bool:
    """Superuser read — the team-private pipeline demonstrably exists, RLS aside."""
    async with db_engine.connect() as conn:
        row = (await conn.execute(text("SELECT id FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})).scalar_one_or_none()
    return row is not None


async def _pipeline_visible_org_only(app_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> bool:
    """Read the pipeline the OLD way: one transaction, org context only."""
    from farna.db.models.pipeline import Pipeline

    factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, org_id)
        row = (await session.execute(select(Pipeline.id).where(Pipeline.id == pipeline_id))).scalar_one_or_none()
        return row is not None


async def _pipeline_visible_through_background_session(app_engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> bool:
    """Read the pipeline the background way: org + ``app.execution_context``."""
    from farna.db.models.pipeline import Pipeline

    async with background_session(org_id, engine=app_engine) as session:
        row = (await session.execute(select(Pipeline.id).where(Pipeline.id == pipeline_id))).scalar_one_or_none()
        return row is not None


async def _cleanup(
    db_engine: AsyncEngine,
    *,
    run_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    team_id: uuid.UUID,
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": str(run_id)})
        await conn.execute(text("DELETE FROM pipeline_snapshots WHERE id = :id"), {"id": str(snapshot_id)})
        await conn.execute(text("DELETE FROM pipelines WHERE id = :id"), {"id": str(pipeline_id)})
        await conn.execute(text("DELETE FROM team_memberships WHERE team_id = :id"), {"id": str(team_id)})
        await conn.execute(text("DELETE FROM teams WHERE id = :id"), {"id": str(team_id)})


async def test_notifier_sees_a_team_owned_run_through_the_background_grant(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The grant is load-bearing: org-only reads team-blind, the grant does not."""
    team_id = await _seed_team(db_engine, test_org, test_user, "owner")
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user, team_id)
    snapshot_id = await _insert_snapshot(db_engine, test_org, pipeline_id)
    run_id = await _insert_team_owned_run(db_engine, test_org, pipeline_id, snapshot_id, team_id, status="complete")

    try:
        # The premises: the team-private pipeline and the team-owned run exist.
        assert await _pipeline_row_exists(db_engine, pipeline_id) is True
        async with db_engine.connect() as conn:
            seeded = (await conn.execute(text("SELECT id FROM runs WHERE id = :id"), {"id": str(run_id)})).scalar_one_or_none()
        assert seeded is not None

        # ARM 1 — org context ALONE cannot see the team-private pipeline. This is
        # the masked read the ADR warns about; if it ever returns True the rest
        # of this test proves nothing, so it is asserted first.
        assert await _pipeline_visible_org_only(app_engine, test_org, pipeline_id) is False

        # ARM 2 — the same read through background_session (the grant) sees it.
        assert await _pipeline_visible_through_background_session(app_engine, test_org, pipeline_id) is True

        # ARM 3 — the notifier's own status read, which now acquires its session
        # from background_session, still reads the team-owned TERMINAL run.
        notifier = Notifier(app_engine, _fernet_key())
        assert await notifier._run_is_terminal(test_org, run_id) is True
    finally:
        await _cleanup(
            db_engine,
            run_id=run_id,
            snapshot_id=snapshot_id,
            pipeline_id=pipeline_id,
            team_id=team_id,
        )


async def test_notifier_read_is_org_scoped_not_a_wildcard(
    db_engine: AsyncEngine,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """The grant widens TEAM visibility only — never across organisations."""
    team_id = await _seed_team(db_engine, test_org, test_user, "org-scope")
    pipeline_id = await _insert_pipeline(db_engine, test_org, test_user, team_id)
    snapshot_id = await _insert_snapshot(db_engine, test_org, pipeline_id)
    run_id = await _insert_team_owned_run(db_engine, test_org, pipeline_id, snapshot_id, team_id, status="complete")

    try:
        notifier = Notifier(app_engine, _fernet_key())
        # A DIFFERENT organisation's id must never see this run, grant or not.
        assert await notifier._run_is_terminal(uuid.uuid4(), run_id) is False
    finally:
        await _cleanup(
            db_engine,
            run_id=run_id,
            snapshot_id=snapshot_id,
            pipeline_id=pipeline_id,
            team_id=team_id,
        )
