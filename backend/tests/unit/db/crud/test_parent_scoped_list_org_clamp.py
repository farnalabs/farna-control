"""FAR-2689: parent-scoped list helpers must clamp to the caller's org too.

FAR-2684 and FAR-2686 closed the same class on org-scoped list routes. Their
sweep found four more helpers that narrow only by a PARENT id — an agent, a
schema, a run, a team — and left tenant isolation entirely to PostgreSQL
row-level security. A ``BYPASSRLS`` connection (or a missing/misconfigured RLS
policy) would then list another org's rows for a parent id it guessed.

``list_runs`` had the sibling shape: ``organisation_id`` was OPTIONAL, so a
future caller could silently regress to an RLS-only read. It is now required.

Each case below runs against a plain SQLite engine that has neither Postgres
RLS nor an active tenant-filter listener (no ``set_rls_org`` call), so it is an
explicit RLS-bypass path: the ONLY thing that can narrow the result is the
predicate the helper itself builds. Every fixture gives BOTH orgs a row hanging
off the SAME parent id, so the parent id alone cannot separate them —
without the org predicate the behavioural assertion fails, and the
statement-level assertion fails with it.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.db.crud.agent_runner_binding import list_bindings_for_agent
from farna.db.crud.run import list_runs
from farna.db.crud.run_attempts import list_attempts
from farna.db.crud.schema import list_schema_versions
from farna.db.crud.team_membership import list_team_members
from farna.db.models.agent_runner_binding import AgentRunnerBinding
from farna.db.models.base import Base
from farna.db.models.pipeline import Pipeline
from farna.db.models.run import Run
from farna.db.models.run_attempt import RunAttempt
from farna.db.models.schema import SchemaVersion
from farna.db.models.team_membership import TeamMembership

_ORG_A = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
_ORG_B = uuid.UUID("00000000-0000-0000-0000-0000000000e2")
_ACCOUNT = uuid.UUID("00000000-0000-0000-0000-0000000000a9")

_CLAMPED_TABLES = [
    AgentRunnerBinding.__table__,
    Pipeline.__table__,
    Run.__table__,
    RunAttempt.__table__,
    SchemaVersion.__table__,
    TeamMembership.__table__,
]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=_CLAMPED_TABLES))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


@asynccontextmanager
async def _recording(session: AsyncSession) -> AsyncIterator[list[str]]:
    """Record every SELECT statement text the helper issues."""
    captured: list[str] = []
    real_execute = session.execute

    async def recording_execute(statement: object, *args: object, **kwargs: object) -> Any:
        if isinstance(statement, Select):
            captured.append(str(statement))
        return await real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    session.execute = recording_execute  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        session.execute = real_execute  # type: ignore[method-assign]


def _assert_scoped(statement_texts: list[str], expected: int) -> None:
    assert len(statement_texts) == expected
    for sql in statement_texts:
        # ``organisation_id`` alone is NOT a discriminator: an entity SELECT
        # projects the column, so it appears even with no predicate. Require the
        # WHERE-clause comparison itself, which only a bound predicate renders.
        assert "organisation_id = :" in sql


# ---------------------------------------------------------------------------
# agent runner bindings (parent: agent)
# ---------------------------------------------------------------------------


def _binding(org_id: uuid.UUID, agent_id: uuid.UUID, target: str) -> AgentRunnerBinding:
    return AgentRunnerBinding(
        id=uuid.uuid4(),
        organisation_id=org_id,
        agent_id=agent_id,
        model_backend_id=uuid.uuid4(),
        target_env_var=target,
        source_field="api_key",
        account_id=_ACCOUNT,
    )


async def test_list_bindings_returns_only_the_callers_org(session: AsyncSession) -> None:
    # Same agent id in both orgs: the parent id alone cannot separate them.
    agent_id = uuid.uuid4()
    own = _binding(_ORG_A, agent_id, "OPENCODE_API_KEY")
    session.add_all([_binding(_ORG_B, agent_id, "OPENCODE_API_KEY"), own])
    await session.flush()

    rows = await list_bindings_for_agent(session, agent_id, org_id=_ORG_A)

    assert [row.id for row in rows] == [own.id]


async def test_list_bindings_binds_the_org_predicate(session: AsyncSession) -> None:
    agent_id = uuid.uuid4()
    session.add_all([_binding(_ORG_A, agent_id, "A_KEY"), _binding(_ORG_B, agent_id, "B_KEY")])
    await session.flush()

    async with _recording(session) as captured:
        await list_bindings_for_agent(session, agent_id, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


# ---------------------------------------------------------------------------
# schema versions (parent: schema)
# ---------------------------------------------------------------------------


def _schema_version(org_id: uuid.UUID, schema_id: uuid.UUID, number: int) -> SchemaVersion:
    return SchemaVersion(
        id=uuid.uuid4(),
        organisation_id=org_id,
        schema_id=schema_id,
        version=f"1.0.{number}",
        version_number=number,
        definition_json={},
        account_id=_ACCOUNT,
    )


async def test_list_schema_versions_returns_only_the_callers_org(session: AsyncSession) -> None:
    schema_id = uuid.uuid4()
    own = _schema_version(_ORG_A, schema_id, 1)
    session.add_all([own, _schema_version(_ORG_B, schema_id, 1)])
    await session.flush()

    page = await list_schema_versions(session, schema_id, org_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_schema_versions_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    schema_id = uuid.uuid4()
    session.add_all([_schema_version(_ORG_A, schema_id, 1), _schema_version(_ORG_B, schema_id, 1)])
    await session.flush()

    async with _recording(session) as captured:
        await list_schema_versions(session, schema_id, org_id=_ORG_A)

    # COUNT and page — ``total`` and the page must describe the same universe.
    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# run attempts (parent: run)
# ---------------------------------------------------------------------------


def _run_attempt(org_id: uuid.UUID, run_id: uuid.UUID, index: int) -> RunAttempt:
    return RunAttempt(id=uuid.uuid4(), organisation_id=org_id, run_id=run_id, attempt_index=index)


async def test_list_attempts_returns_only_the_callers_org(session: AsyncSession) -> None:
    # Same run id in both orgs: the parent id alone cannot separate them.
    # ``uq_run_attempts_run_attempt_index`` is on ``(run_id, attempt_index)``
    # only — the org is NOT part of that identity — so the two attempted rows
    # must take different indices. That constraint is also why the org
    # predicate can never hide a legitimate row: one run id is one attempt
    # sequence, whichever org asks.
    run_id = uuid.uuid4()
    own = _run_attempt(_ORG_A, run_id, 0)
    session.add_all([own, _run_attempt(_ORG_B, run_id, 1)])
    await session.flush()

    rows = await list_attempts(session, run_id, org_id=_ORG_A)

    assert [row.id for row in rows] == [own.id]


async def test_list_attempts_binds_the_org_predicate(session: AsyncSession) -> None:
    run_id = uuid.uuid4()
    # Distinct indices: the table's uniqueness is (run_id, attempt_index), so
    # one run id carries one attempt sequence (see the behavioural test above).
    session.add_all([_run_attempt(_ORG_A, run_id, 0), _run_attempt(_ORG_B, run_id, 1)])
    await session.flush()

    async with _recording(session) as captured:
        await list_attempts(session, run_id, org_id=_ORG_A)

    _assert_scoped(captured, expected=1)


# ---------------------------------------------------------------------------
# team members (parent: team) — the ORG is the boundary, the team is the filter
# ---------------------------------------------------------------------------


def _team_membership(org_id: uuid.UUID, team_id: uuid.UUID, role: str = "viewer") -> TeamMembership:
    return TeamMembership(id=uuid.uuid4(), organisation_id=org_id, team_id=team_id, account_id=uuid.uuid4(), role=role)


async def test_list_team_members_returns_only_the_callers_org(session: AsyncSession) -> None:
    """Two members of ONE team are all still returned; the other org's are not.

    This is the per-table check that matters for ``team_memberships``: the org
    predicate must not narrow beyond the team. A team belongs to exactly one
    org, so the org clause is a superset of the team scope — every legitimate
    in-team member survives it.
    """
    team_id = uuid.uuid4()
    first = _team_membership(_ORG_A, team_id, "operator")
    second = _team_membership(_ORG_A, team_id, "viewer")
    session.add_all([first, second, _team_membership(_ORG_B, team_id, "viewer")])
    await session.flush()

    page = await list_team_members(session, team_id, org_id=_ORG_A)

    assert page.total == 2
    assert {item.id for item in page.items} == {first.id, second.id}


async def test_list_team_members_binds_the_org_into_both_statements(session: AsyncSession) -> None:
    team_id = uuid.uuid4()
    session.add_all([_team_membership(_ORG_A, team_id), _team_membership(_ORG_B, team_id)])
    await session.flush()

    async with _recording(session) as captured:
        await list_team_members(session, team_id, org_id=_ORG_A)

    _assert_scoped(captured, expected=2)


# ---------------------------------------------------------------------------
# runs — the org is REQUIRED, not optional
# ---------------------------------------------------------------------------


def _pipeline(org_id: uuid.UUID, name: str) -> Pipeline:
    return Pipeline(id=uuid.uuid4(), organisation_id=org_id, name=name, account_id=_ACCOUNT)


def _run(org_id: uuid.UUID, pipeline_id: uuid.UUID, number: int) -> Run:
    return Run(
        id=uuid.uuid4(),
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        snapshot_id=uuid.uuid4(),
        trigger_type="manual",
        run_number=number,
        input_hash="a" * 64,
        langgraph_thread_id=f"thread-{uuid.uuid4().hex}",
    )


def test_list_runs_requires_the_organisation() -> None:
    """FAR-2689: the RLS-only fallback is gone.

    ``organisation_id`` was optional, which let a future caller list runs with
    no org predicate at all. The parameter is now keyword-only and required, so
    omitting it is a ``TypeError`` at the call site rather than a silent
    cross-tenant read. This test FAILS on the pre-fix signature.
    """
    parameter = inspect.signature(list_runs).parameters["organisation_id"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert parameter.annotation is uuid.UUID


async def test_list_runs_returns_only_the_callers_org(session: AsyncSession) -> None:
    pipeline_a = _pipeline(_ORG_A, "Org A Pipeline")
    pipeline_b = _pipeline(_ORG_B, "Org B Pipeline")
    own = _run(_ORG_A, pipeline_a.id, 1)
    session.add_all([pipeline_a, pipeline_b, own, _run(_ORG_B, pipeline_b.id, 1)])
    await session.flush()

    page = await list_runs(session, organisation_id=_ORG_A)

    assert page.total == 1
    assert [item.id for item in page.items] == [own.id]


async def test_list_runs_binds_the_org_into_the_count_and_the_page(session: AsyncSession) -> None:
    pipeline_a = _pipeline(_ORG_A, "Org A Pipeline")
    pipeline_b = _pipeline(_ORG_B, "Org B Pipeline")
    session.add_all([pipeline_a, pipeline_b, _run(_ORG_A, pipeline_a.id, 1), _run(_ORG_B, pipeline_b.id, 1)])
    await session.flush()

    async with _recording(session) as captured:
        await list_runs(session, organisation_id=_ORG_A)

    # The pipeline ``selectinload`` issues its own SELECT; the two runs-list
    # statements are the ones that carry the org predicate.
    scoped = [sql for sql in captured if "runs.organisation_id = :" in sql]
    assert len(scoped) == 2
    assert any("count(" in sql for sql in scoped)
