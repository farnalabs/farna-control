"""FAR-2705: Hypothesis stateful test of the REAL run lifecycle against Postgres.

The machine drives the production functions — ``create_run``, ``claim_run_async``,
``PipelineExecutor._check_capacity`` (the production admission gate),
``claim_resume_run_async``, ``HITLManager.create_gate``/``claim``/``approve``/
``reject``, ``park_expired_hitl_runs``, ``reconcile_pipeline_slots``,
``mark_complete`` and ``request_cancellation`` — through arbitrary interleavings of
those operations (including the illegal ones: a second claimant, deciding twice,
and an attempt to reach across the tenant boundary).

Isolation (FAR-2705 qa):

* This module runs against its OWN database (``isolated_lifecycle_db_url``), so
  the GLOBAL park/slot sweeps — which enumerate every organisation — cannot reap
  another test's rows. A companion test proves it: a matching row seeded in the
  SHARED database is untouched by this module's sweeps.
* Every engine runs as the non-superuser ``modulo_integration_app`` role, so RLS
  is live (not inert as it is under the testcontainer superuser).

After EVERY step it asserts, against the database and ``farna.lifecycle``:

* capacity is never exceeded (the pipeline execution projection is the bound, and
  production admission — not the test driver — is what enforces it);
* a gate is never claimed twice;
* no run is terminal while it has an open gate;
* every non-terminal run is queued/executing/parked (exactly one class), never
  executing and parked at the same time;
* a decided gate always un-parks a ``hitl_parked`` run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import uuid
from collections.abc import Coroutine, Generator
from datetime import timedelta
from typing import Any, TypeVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, multiple, rule, run_state_machine_as_test
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from farna import lifecycle
from farna.core import cron_helpers
from farna.core.hitl_manager import AlreadyClaimedError, GateAlreadyDecidedError, GateNotFoundError, HITLManager
from farna.core.pipeline_engine.executor import PipelineExecutor
from farna.core.pipeline_execution import claim_resume_run_async, claim_run_async, mark_complete
from farna.core.run_admission import park_expired_hitl_runs, reconcile_pipeline_slots
from farna.db.crud.run import create_run, request_cancellation, transition_run
from farna.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_T = TypeVar("_T")

_PIPELINE_CAPACITY = 2
_GRAPH: dict[str, Any] = {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# A private event loop, because Hypothesis rules are synchronous.
# ---------------------------------------------------------------------------
class _LoopRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name="run-lifecycle-loop", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


async def _create_engine(database_url: str, role: str | None = None) -> AsyncEngine:
    engine = create_async_engine(database_url, echo=False, poolclass=NullPool)
    if role is not None:

        @event.listens_for(engine.sync_engine, "checkout")
        def _set_role_on_checkout(dbapi_connection: object, _record: object, _proxy: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            try:
                cursor.execute(f'SET ROLE "{role}"')
            finally:
                cursor.close()

    return engine


@pytest.fixture
def lifecycle_runtime(isolated_lifecycle_db_url: str, non_superuser_role: str) -> Generator[tuple[_LoopRunner, AsyncEngine], None, None]:
    """A private loop + an RLS-enforcing engine on this module's own database."""
    runner = _LoopRunner()
    engine = runner.run(_create_engine(isolated_lifecycle_db_url, non_superuser_role))
    try:
        yield runner, engine
    finally:
        runner.run(engine.dispose())
        # The production call paths under test lazily create PROCESS-GLOBAL
        # pooled engines (the shared engine, cron/system engines). They are
        # created on THIS private loop, so they must be disposed on it before it
        # closes — otherwise asyncpg reports the transport as unclosed at an
        # unrelated teardown, on a loop it has never run on.
        _dispose_process_global_engines(runner)
        runner.close()


def _dispose_process_global_engines(runner: _LoopRunner) -> None:
    async def _dispose() -> None:
        import contextlib

        from farna.api import dependencies as api_dependencies
        from farna.core import cron_helpers as helpers
        from farna.db import session as db_session

        caches = (
            (db_session, "_shared_engine"),
            (api_dependencies, "_engine"),
            (api_dependencies, "_SYSTEM_ASYNC_ENGINE"),
            (helpers, "_ENGINE"),
            (helpers, "_SYSTEM_ENGINE"),
        )
        seen: set[int] = set()
        for module, attribute in caches:
            engine = getattr(module, attribute, None)
            if engine is None:
                continue
            if id(engine) not in seen:
                seen.add(id(engine))
                with contextlib.suppress(Exception):
                    await engine.dispose()
            setattr(module, attribute, None)
        api_dependencies._session_factory = None
        api_dependencies._SYSTEM_SESSION_FACTORY = None

    # Teardown must never mask the test result.
    with contextlib.suppress(Exception):
        runner.run(_dispose())


# ---------------------------------------------------------------------------
# Seeding — one isolated org + pipeline + snapshot per example, plus a second
# org that must remain untouchable across the tenant boundary.
# ---------------------------------------------------------------------------
async def _insert_org(conn: Any, org_id: uuid.UUID) -> None:
    await conn.execute(
        text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
        {"id": str(org_id), "name": f"FAR-2705 {org_id.hex[:8]}", "slug": f"far2705-{org_id.hex[:12]}"},
    )


async def _insert_account(conn: Any, account_id: uuid.UUID) -> None:
    await conn.execute(
        text("INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"),
        {"id": str(account_id), "email": f"far2705-{account_id.hex[:12]}@test.local", "name": "FAR-2705"},
    )


async def _insert_pipeline(conn: Any, org_id: uuid.UUID, pipeline_id: uuid.UUID, account_id: uuid.UUID) -> None:
    await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
    await conn.execute(
        text("INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json) VALUES (:id, :oid, :name, :uid, :cap, 30, 300, '{}'::json, (:graph)::json)"),
        {"id": str(pipeline_id), "oid": str(org_id), "name": f"FAR-2705 {pipeline_id.hex[:8]}", "uid": str(account_id), "cap": _PIPELINE_CAPACITY, "graph": json.dumps(_GRAPH)},
    )


async def _insert_snapshot(conn: Any, org_id: uuid.UUID, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID, account_id: uuid.UUID) -> None:
    await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
    await conn.execute(
        text(
            "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, account_id, snapshot_version, graph_json, connector_bindings_json, "
            "schema_pins_json, prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
            "VALUES (:id, :pid, :oid, :aid, 1, (:graph)::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
        ),
        {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "aid": str(account_id), "graph": json.dumps(_GRAPH)},
    )


async def _seed(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID, accounts: tuple[uuid.UUID, uuid.UUID]) -> None:
    async with engine.connect() as conn:
        await conn.begin()
        await _insert_org(conn, org_id)
        for account_id in accounts:
            await _insert_account(conn, account_id)
        await _insert_pipeline(conn, org_id, pipeline_id, accounts[0])
        await _insert_snapshot(conn, org_id, pipeline_id, snapshot_id, accounts[0])
        await conn.commit()


async def _seed_foreign_tenant(
    engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    run_id: uuid.UUID,
    review_id: str,
) -> None:
    """A second org with a running run and an open gate — the tenant boundary."""
    async with engine.connect() as conn:
        await conn.begin()
        await _insert_org(conn, org_id)
        await _insert_account(conn, account_id)
        await _insert_pipeline(conn, org_id, pipeline_id, account_id)
        await _insert_snapshot(conn, org_id, pipeline_id, snapshot_id, account_id)
        await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, trigger_type, status, input_hash, "
                "langgraph_thread_id, run_number, claim_count, claim_token, idempotency_key) "
                "VALUES (:id, :oid, :pid, :sid, :uid, 'manual', 'running', 'hash', :thread, :rn, 1, 'foreign-token', :key)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "uid": str(account_id),
                "thread": f"{org_id}:{run_id}",
                "rn": 1,
                "key": f"{pipeline_id}:1",
            },
        )
        await conn.execute(
            text("INSERT INTO hitl_claims (run_id, organisation_id, pipeline_id, review_id, expires_at, terminalize_at) VALUES (:rid, :oid, :pid, :gate, now() + interval '1 day', now() + interval '1 day')"),
            {"rid": str(run_id), "oid": str(org_id), "pid": str(pipeline_id), "gate": review_id},
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# The state machine.
# ---------------------------------------------------------------------------
class RunLifecycleMachine(RuleBasedStateMachine):
    runs = Bundle("runs")
    gates = Bundle("gates")  # (run_id, review_id)

    def __init__(self, runner: _LoopRunner, engine: AsyncEngine, observed: set[str]) -> None:
        super().__init__()
        self._runner = runner
        self._engine = engine
        self._observed = observed
        self.org_id = uuid.uuid4()
        self.pipeline_id = uuid.uuid4()
        self.snapshot_id = uuid.uuid4()
        self.account_one = uuid.uuid4()
        self.account_two = uuid.uuid4()
        self.foreign_org_id = uuid.uuid4()
        self.foreign_pipeline_id = uuid.uuid4()
        self.foreign_snapshot_id = uuid.uuid4()
        self.foreign_account_id = uuid.uuid4()
        self.foreign_run_id = uuid.uuid4()
        self.foreign_review_id = f"foreign-gate-{uuid.uuid4().hex[:8]}"
        self._tokens: dict[tuple[uuid.UUID, str], str] = {}
        self._claimed_by: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self._runner.run(_seed(engine, self.org_id, self.pipeline_id, self.snapshot_id, (self.account_one, self.account_two)))
        self._runner.run(
            _seed_foreign_tenant(
                engine,
                org_id=self.foreign_org_id,
                account_id=self.foreign_account_id,
                pipeline_id=self.foreign_pipeline_id,
                snapshot_id=self.foreign_snapshot_id,
                run_id=self.foreign_run_id,
                review_id=self.foreign_review_id,
            )
        )

    # -- async helpers ---------------------------------------------------
    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        return self._runner.run(coro)

    async def _status(self, run_id: uuid.UUID) -> str | None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
            row = await conn.execute(text("SELECT status FROM runs WHERE id = :rid AND organisation_id = :oid"), {"rid": str(run_id), "oid": str(self.org_id)})
            return row.scalar_one_or_none()

    async def _foreign_status(self) -> str | None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.foreign_org_id)})
            row = await conn.execute(
                text("SELECT status FROM runs WHERE id = :rid AND organisation_id = :oid"),
                {"rid": str(self.foreign_run_id), "oid": str(self.foreign_org_id)},
            )
            return row.scalar_one_or_none()

    async def _admit_under_capacity(self, run_id: uuid.UUID, claim_token: str) -> Any:
        """Drive the PRODUCTION capacity admission (never a driver-side pre-check).

        The SAQ claim above already moved the run ``running``. This is the exact
        production decision that follows it: admitting the attempt or demoting
        the run back to ``pending`` with a capacity marker. Capacity is enforced
        here, by the product, not by the test choosing not to claim.
        """
        executor = PipelineExecutor(self._engine)
        executor._claim_token = claim_token
        return await executor._check_capacity(
            run_id=run_id,
            org_id=self.org_id,
            pipeline_id=self.pipeline_id,
            max_concurrent=_PIPELINE_CAPACITY,
            graph_json=_GRAPH,
            snapshot_id=self.snapshot_id,
        )

    # -- rules -----------------------------------------------------------
    @rule(target=runs)
    def admit(self) -> uuid.UUID:
        """Real ``create_run`` — a run enters the lifecycle as ``pending``."""

        async def _admit() -> uuid.UUID:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                run = await create_run(
                    session,
                    org_id=self.org_id,
                    pipeline_id=self.pipeline_id,
                    snapshot_id=self.snapshot_id,
                    trigger_type="manual",
                    input_payload={"ticket": f"FAR-2705-{uuid.uuid4().hex}"},
                    account_id=self.account_one,
                )
                return run.id

        run_id = self._run(_admit())
        assert self._run(self._status(run_id)) == lifecycle.STATUS_PENDING
        self._observed.add("admit")
        return run_id

    @rule(run=runs)
    def claim_run(self, run: uuid.UUID) -> None:
        """Real ``claim_run_async`` then the real capacity admission, unconditionally."""
        if self._run(self._status(run)) != lifecycle.STATUS_PENDING:
            return
        token = self._run(claim_run_async(self._engine, str(run), str(self.org_id), stale_seconds=60))
        if token is None:
            return
        admitted = self._run(self._admit_under_capacity(run, token))
        if admitted.status == lifecycle.STATUS_RUNNING:
            self._observed.add("claim_run")
            return
        # At cap: production demoted the claim back to pending with a marker.
        assert admitted.status == lifecycle.STATUS_PENDING, f"capacity refusal left the run {admitted.status!r}"
        assert admitted.error_code in {"pipeline_capacity", "org_capacity_limited"}, admitted.error_code
        self._observed.add("claim_refused_at_capacity")

    @rule(run=runs, target=gates)
    def request_hitl(self, run: uuid.UUID) -> Any:
        """``transition_run`` running->awaiting_human, then the real gate creation."""
        if self._run(self._status(run)) != lifecycle.STATUS_RUNNING:
            return multiple()
        review_id = f"gate-{uuid.uuid4().hex[:8]}"

        async def _request() -> tuple[uuid.UUID, str]:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                moved = await transition_run(
                    session,
                    run,
                    self.org_id,
                    target_status=lifecycle.STATUS_AWAITING_HUMAN,
                    allowed_from=frozenset({lifecycle.STATUS_RUNNING}),
                )
                assert moved, "a running run must move to awaiting_human"
                await HITLManager().create_gate(session, run_id=run, review_id=review_id, pipeline_id=self.pipeline_id, org_id=self.org_id)
            return run, review_id

        self._observed.add("request_hitl")
        return self._run(_request())

    @rule(gate=gates)
    def claim_gate(self, gate: tuple[uuid.UUID, str]) -> None:
        """Real ``HITLManager.claim`` + the claim route's fenced status flip."""
        run, review_id = gate
        status = self._run(self._status(run))
        if status not in lifecycle.HITL_CLAIMABLE_RUN_STATUSES or (run, review_id) in self._claimed_by:
            return

        async def _claim() -> str:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                claimed = await HITLManager().claim(session, run_id=run, review_id=review_id, org_id=self.org_id, claimant_id=self.account_one)
                if status == lifecycle.STATUS_AWAITING_HUMAN:
                    await transition_run(
                        session,
                        run,
                        self.org_id,
                        target_status=lifecycle.STATUS_CLAIMED,
                        allowed_from=frozenset({lifecycle.STATUS_AWAITING_HUMAN}),
                    )
                return claimed.claim_token

        self._tokens[(run, review_id)] = self._run(_claim())
        self._claimed_by[(run, review_id)] = self.account_one
        self._observed.add("claim_gate")

    @rule(gate=gates)
    def claim_gate_again_as_another_account(self, gate: tuple[uuid.UUID, str]) -> None:
        """Illegal sequence: a second account must never acquire the same gate."""
        run, review_id = gate
        if (run, review_id) not in self._claimed_by or (run, review_id) in self._tokens:
            return

        async def _reclaim() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                await HITLManager().claim(session, run_id=run, review_id=review_id, org_id=self.org_id, claimant_id=self.account_two)

        with pytest.raises(AlreadyClaimedError):
            self._run(_reclaim())
        self._observed.add("claim_gate_again")

    @rule(gate=gates, decision=st.sampled_from(["approved", "rejected"]))
    def decide_gate(self, gate: tuple[uuid.UUID, str], decision: str) -> None:
        """Real ``HITLManager.approve``/``reject`` — a decision must un-park."""
        run, review_id = gate
        token = self._tokens.get((run, review_id))
        if token is None:
            return
        status_before = self._run(self._status(run))

        async def _decide() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                manager = HITLManager()
                if decision == "approved":
                    await manager.approve(session, run_id=run, review_id=review_id, org_id=self.org_id, claim_token=token, actor_id=self.account_one)
                else:
                    await manager.reject(session, run_id=run, review_id=review_id, org_id=self.org_id, claim_token=token, actor_id=self.account_one)

        self._run(_decide())
        self._tokens.pop((run, review_id), None)
        self._observed.add(f"decide_{decision}")
        status_after = self._run(self._status(run))
        if status_before == lifecycle.STATUS_HITL_PARKED:
            assert status_after != lifecycle.STATUS_HITL_PARKED, "a decided gate must un-park the run"

    @rule(gate=gates)
    def decide_twice(self, gate: tuple[uuid.UUID, str]) -> None:
        """Illegal sequence: a decided gate can never be decided again."""
        run, review_id = gate
        if (run, review_id) in self._tokens or (run, review_id) not in self._claimed_by:
            return

        async def _decide_again() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                await HITLManager().approve(session, run_id=run, review_id=review_id, org_id=self.org_id, claim_token="stale-token", actor_id=self.account_one)

        with pytest.raises(GateAlreadyDecidedError):
            self._run(_decide_again())
        self._observed.add("decide_twice")

    @rule(run=runs)
    def park_expired_review(self, run: uuid.UUID) -> None:
        """Real ``park_expired_hitl_runs``: an expired, unclaimed gate parks the run."""
        if self._run(self._status(run)) != lifecycle.STATUS_AWAITING_HUMAN or self._run(self._has_claimed_undecided_gate(run)):
            return

        async def _expire() -> None:
            async with self._engine.connect() as conn, conn.begin():
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                await conn.execute(
                    text("UPDATE hitl_claims SET expires_at = now() - interval '2 hours', terminalize_at = now() - interval '2 hours' WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL AND account_id IS NULL"),
                    {"rid": str(run), "oid": str(self.org_id)},
                )

        self._run(_expire())
        self._run(park_expired_hitl_runs(self._engine, grace_seconds=0))
        assert self._run(self._status(run)) == lifecycle.STATUS_HITL_PARKED
        self._observed.add("park_expired_review")

    @rule(run=runs)
    def reconcile_stale_running(self, run: uuid.UUID) -> None:
        """Real ``reconcile_pipeline_slots``: a stale running row leaves ``running``."""
        if self._run(self._status(run)) != lifecycle.STATUS_RUNNING:
            return

        async def _age_heartbeat() -> None:
            async with self._engine.connect() as conn, conn.begin():
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                await conn.execute(
                    text("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = :rid AND organisation_id = :oid"),
                    {"rid": str(run), "oid": str(self.org_id)},
                )

        self._run(_age_heartbeat())
        self._run(reconcile_pipeline_slots(self._engine, stale_seconds=1))
        assert self._run(self._status(run)) != lifecycle.STATUS_RUNNING
        self._observed.add("reconcile_stale_running")

    @rule(run=runs)
    def resume_after_decision(self, run: uuid.UUID) -> None:
        """Real ``claim_resume_run_async`` — only when a gate decision is committed."""
        if self._run(self._status(run)) not in (lifecycle.STATUS_AWAITING_HUMAN, lifecycle.STATUS_CLAIMED, lifecycle.STATUS_HITL_PARKED):
            return
        if not self._run(self._has_decided_gate(run)) or self._run(self._has_undecided_gate(run)):
            return
        token = self._run(claim_resume_run_async(self._engine, str(run), str(self.org_id)))
        if token is not None:
            assert self._run(self._status(run)) == lifecycle.STATUS_RUNNING
            self._observed.add("resume_after_decision")

    @rule(run=runs)
    def complete_run(self, run: uuid.UUID) -> None:
        """Real ``mark_complete`` — a running run finishes."""
        if self._run(self._status(run)) != lifecycle.STATUS_RUNNING:
            return
        self._run(mark_complete(self._engine, str(run), str(self.org_id)))
        assert self._run(self._status(run)) in (lifecycle.STATUS_COMPLETE, lifecycle.STATUS_RUNNING)
        self._observed.add("complete_run")

    @rule(run=runs)
    def cancel_without_open_gate(self, run: uuid.UUID) -> None:
        """Real ``request_cancellation`` — only when no gate is still open."""
        if self._run(self._status(run)) not in lifecycle.ACTIVE_RUN_STATUSES or self._run(self._has_undecided_gate(run)):
            return

        async def _cancel() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                await request_cancellation(session, run, actor=str(self.account_one))

        self._run(_cancel())
        assert self._run(self._status(run)) == lifecycle.STATUS_CANCELLED
        self._observed.add("cancel_without_open_gate")

    # -- cross-tenant rules: every boundary write/read must be refused -----
    @rule()
    def cross_tenant_claim_is_refused(self) -> None:
        """A claim against the other org's run must no-op and leave it unchanged."""
        before = self._run(self._foreign_status())
        token = self._run(claim_run_async(self._engine, str(self.foreign_run_id), str(self.org_id), stale_seconds=60))
        assert token is None, "a cross-tenant claim must be refused"
        assert self._run(self._foreign_status()) == before
        self._observed.add("cross_tenant_claim")

    @rule()
    def cross_tenant_transition_is_refused(self) -> None:
        """A transition fenced to THIS org must never move the other org's run."""

        async def _transition() -> bool:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                return await transition_run(session, self.foreign_run_id, self.org_id, target_status=lifecycle.STATUS_CANCELLED)

        before = self._run(self._foreign_status())
        assert self._run(_transition()) is False, "a cross-tenant transition must be refused"
        assert self._run(self._foreign_status()) == before
        self._observed.add("cross_tenant_transition")

    @rule()
    def cross_tenant_read_is_refused(self) -> None:
        """A read scoped to THIS org cannot see the other org's run."""

        async def _read() -> Any:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                from farna.db.crud.run import get_run

                return await get_run(session, self.foreign_run_id)

        assert self._run(_read()) is None, "RLS must hide another org's run"
        self._observed.add("cross_tenant_read")

    @rule()
    def cross_tenant_decide_is_refused(self) -> None:
        """A gate claim scoped to THIS org cannot reach the other org's gate."""

        async def _decide() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                await HITLManager().claim(
                    session,
                    run_id=self.foreign_run_id,
                    review_id=self.foreign_review_id,
                    org_id=self.org_id,
                    claimant_id=self.account_one,
                )

        with pytest.raises(GateNotFoundError):
            self._run(_decide())
        self._observed.add("cross_tenant_decide")

    # -- shared queries --------------------------------------------------
    async def _has_undecided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    async def _has_decided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NOT NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    async def _has_claimed_undecided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL AND account_id IS NOT NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    async def capacity_used(self) -> int:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
            row = await conn.execute(
                text("SELECT count(*) FROM runs WHERE pipeline_id = :pid AND organisation_id = :oid AND status = ANY(:statuses)"),
                {"pid": str(self.pipeline_id), "oid": str(self.org_id), "statuses": sorted(lifecycle.PIPELINE_CAPACITY_STATUSES)},
            )
            return int(row.scalar_one())

    # -- invariants (checked after every step) ---------------------------
    @invariant()
    def capacity_is_never_exceeded(self) -> None:
        used = self._run(self.capacity_used())
        assert used <= _PIPELINE_CAPACITY, f"pipeline capacity exceeded: {used} > {_PIPELINE_CAPACITY}"

    @invariant()
    def a_gate_is_never_claimed_twice(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                duplicate_holders = await conn.execute(
                    text("SELECT run_id, review_id, count(DISTINCT account_id) AS holders FROM hitl_claims WHERE organisation_id = :oid AND decision IS NULL AND account_id IS NOT NULL GROUP BY run_id, review_id HAVING count(DISTINCT account_id) > 1"),
                    {"oid": str(self.org_id)},
                )
                assert not duplicate_holders.fetchall()

        self._run(_check())

    @invariant()
    def no_terminal_run_has_an_open_gate(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                rows = await conn.execute(
                    text("SELECT r.id FROM runs r JOIN hitl_claims hc ON hc.run_id = r.id AND hc.organisation_id = r.organisation_id WHERE r.organisation_id = :oid AND hc.decision IS NULL AND r.status = ANY(:terminal)"),
                    {"oid": str(self.org_id), "terminal": sorted(lifecycle.TERMINAL_STATUSES)},
                )
                offenders = [str(row[0]) for row in rows.fetchall()]
                assert not offenders, f"terminal run(s) with an open gate: {offenders}"

        self._run(_check())

    @invariant()
    def every_non_terminal_run_is_in_exactly_one_class(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                rows = await conn.execute(text("SELECT id, status FROM runs WHERE organisation_id = :oid"), {"oid": str(self.org_id)})
                for run_id, status in rows.fetchall():
                    assert status in lifecycle.ALL_RUN_STATUSES, f"run {run_id} has an unknown status {status!r}"
                    if status in lifecycle.TERMINAL_STATUSES:
                        continue
                    classes = [
                        name
                        for name, projection in (
                            ("queued", lifecycle.QUEUED_STATUSES),
                            ("in_flight", lifecycle.IN_FLIGHT_STATUSES),
                            ("recovering", lifecycle.RECOVERING_STATUSES),
                            ("parked", lifecycle.PARKED_STATUSES),
                        )
                        if status in projection
                    ]
                    assert len(classes) == 1, f"run {run_id} ({status}) is in classes {classes}"
                    assert not (status in lifecycle.EXECUTING_STATUSES and status in lifecycle.PARKED_STATUSES)

        self._run(_check())

    @invariant()
    def a_decided_gate_leaves_the_run_unparked(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(self.org_id)})
                rows = await conn.execute(
                    text(
                        "SELECT r.id FROM runs r WHERE r.organisation_id = :oid AND r.status = :parked "
                        "AND NOT EXISTS (SELECT 1 FROM hitl_claims hc WHERE hc.run_id = r.id AND hc.organisation_id = r.organisation_id AND hc.decision IS NULL)"
                        " AND EXISTS (SELECT 1 FROM hitl_claims hc2 WHERE hc2.run_id = r.id AND hc2.organisation_id = r.organisation_id AND hc2.decision IS NOT NULL)"
                    ),
                    {"oid": str(self.org_id), "parked": lifecycle.STATUS_HITL_PARKED},
                )
                offenders = [str(row[0]) for row in rows.fetchall()]
                assert not offenders, f"run(s) still parked after their gate was decided: {offenders}"

        self._run(_check())

    @invariant()
    def the_foreign_tenant_is_untouched(self) -> None:
        assert self._run(self._foreign_status()) == lifecycle.STATUS_RUNNING


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_run_lifecycle_state_machine(lifecycle_runtime: tuple[_LoopRunner, AsyncEngine]) -> None:
    """Random interleavings within the PR budget; deterministic by construction.

    The budget is env-driven so the nightly workflow can run a much larger one
    (``FARNA_CONTROL_LIFECYCLE_MAX_EXAMPLES`` / ``..._STEP_COUNT``) without a
    second test body. ``derandomize`` keeps the PR run reproducible; rule-level
    coverage is proven separately on the machine's own rules.
    """
    runner, engine = lifecycle_runtime
    observed: set[str] = set()
    max_examples = int(os.environ.get("FARNA_CONTROL_LIFECYCLE_MAX_EXAMPLES", "4"))
    step_count = int(os.environ.get("FARNA_CONTROL_LIFECYCLE_STEP_COUNT", "6"))
    run_state_machine_as_test(
        lambda: RunLifecycleMachine(runner, engine, observed),
        settings=hypothesis_settings(
            max_examples=max_examples,
            stateful_step_count=step_count,
            derandomize=True,
            deadline=timedelta(seconds=120),
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
    # The machine really ran (coverage of every leg is proven separately, on the
    # machine's own rules, in test_every_rule_fires_on_the_machine).
    assert "admit" in observed, "the state machine never admitted a run"


def test_every_rule_fires_on_the_machine(lifecycle_runtime: tuple[_LoopRunner, AsyncEngine]) -> None:
    """Coverage from the MACHINE's own rule firings (there is no prelude).

    Each call below is a real rule invocation on a real machine, each with its own
    isolated org, so the assertion cannot be satisfied by a fixture that merely
    seeds rows before Hypothesis starts.
    """
    runner, engine = lifecycle_runtime
    observed: set[str] = set()
    machine = RunLifecycleMachine(runner, engine, observed)

    first = machine.admit()
    machine.claim_run(first)
    first_gate = machine.request_hitl(first)
    machine.claim_gate(first_gate)
    machine.claim_gate_again_as_another_account(first_gate)
    machine.decide_gate(first_gate, "approved")
    machine.decide_twice(first_gate)

    second = machine.admit()
    machine.claim_run(second)
    second_gate = machine.request_hitl(second)
    machine.park_expired_review(second)
    machine.claim_gate(second_gate)
    machine.decide_gate(second_gate, "rejected")
    machine.resume_after_decision(second)
    machine.complete_run(second)

    third = machine.admit()
    machine.claim_run(third)
    machine.cancel_without_open_gate(third)

    fourth = machine.admit()
    machine.claim_run(fourth)
    machine.reconcile_stale_running(fourth)

    machine.cross_tenant_claim_is_refused()
    machine.cross_tenant_transition_is_refused()
    machine.cross_tenant_read_is_refused()
    machine.cross_tenant_decide_is_refused()

    required = {
        "admit",
        "claim_run",
        "request_hitl",
        "claim_gate",
        "decide_approved",
        "decide_rejected",
        "park_expired_review",
        "reconcile_stale_running",
        "resume_after_decision",
        "complete_run",
        "cancel_without_open_gate",
        "cross_tenant_claim",
        "cross_tenant_transition",
        "cross_tenant_read",
        "cross_tenant_decide",
    }
    assert required <= observed, f"the machine never exercised: {sorted(required - observed)}"


def test_a_capacity_leak_trips_the_invariant(
    lifecycle_runtime: tuple[_LoopRunner, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject an under-counting capacity predicate: the invariant MUST fail.

    Proves ``capacity_is_never_exceeded`` is a real guard over the production
    admission path, not a tautology: when the executor's pipeline count is
    doctored to zero, production admits past the cap and the invariant catches it.
    """
    runner, engine = lifecycle_runtime
    observed: set[str] = set()
    machine = RunLifecycleMachine(runner, engine, observed)

    admitted: list[uuid.UUID] = []
    for _ in range(_PIPELINE_CAPACITY):
        run_id = machine.admit()
        machine.claim_run(run_id)
        assert runner.run(machine._status(run_id)) == lifecycle.STATUS_RUNNING
        admitted.append(run_id)

    # The cap is honoured by production: a third claim is demoted.
    overflow = machine.admit()
    machine.claim_run(overflow)
    assert runner.run(machine._status(overflow)) == lifecycle.STATUS_PENDING
    assert "claim_refused_at_capacity" in observed

    # Now leak capacity by undercounting: production admits the third run.
    import farna.core.pipeline_engine.executor as executor_module

    monkeypatch.setattr(executor_module, "count_active_runs_for_pipeline", _always_zero)
    machine.claim_run(overflow)
    assert runner.run(machine._status(overflow)) == lifecycle.STATUS_RUNNING

    with pytest.raises(AssertionError, match="capacity exceeded"):
        machine.capacity_is_never_exceeded()


async def _always_zero(*_args: Any, **_kwargs: Any) -> int:
    return 0


# ---------------------------------------------------------------------------
# The reconcile leg: the REAL dispatcher_reconcile tick
# ---------------------------------------------------------------------------
def test_reconcile_redispatches_a_stale_run_with_its_own_idempotency_key(
    lifecycle_runtime: tuple[_LoopRunner, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real ``dispatcher_reconcile`` (not a faked ``transition_run``).

    A stale ``running`` run is reset to ``pending`` by the real slot sweep and then
    re-dispatched by the real 60-second reconcile tick. The tick must re-enqueue
    the SAME run — the run's persisted ``idempotency_key`` is what a re-run reuses
    — never mint a fresh unit of work.
    """
    runner, engine = lifecycle_runtime
    observed: set[str] = set()
    machine = RunLifecycleMachine(runner, engine, observed)

    run_id = machine.admit()
    machine.claim_run(run_id)
    assert runner.run(machine._status(run_id)) == lifecycle.STATUS_RUNNING
    key_before = runner.run(_run_idempotency_key(engine, machine.org_id, run_id))
    assert key_before, "create_run must persist the run-level idempotency key"

    runner.run(_age_run_heartbeat(engine, machine.org_id, run_id))
    runner.run(reconcile_pipeline_slots(engine, stale_seconds=1))
    assert runner.run(machine._status(run_id)) == lifecycle.STATUS_PENDING

    redis = MagicMock()
    for attribute in ("set", "delete", "zrem", "lrem", "aclose", "get", "keys", "hset", "expire", "incr"):
        setattr(redis, attribute, AsyncMock(return_value=None))
    queue = MagicMock()
    queue.job = AsyncMock(return_value=None)  # the SAQ job hash is gone

    re_enqueue = AsyncMock(return_value=("enqueued", "job-reconcile"))
    monkeypatch.setattr(cron_helpers, "_get_engine", lambda: engine)
    monkeypatch.setattr(cron_helpers, "_get_system_engine", lambda: engine)
    monkeypatch.setattr(cron_helpers, "_collect_org_ids", AsyncMock(return_value=[machine.org_id]))
    monkeypatch.setattr(cron_helpers, "_re_enqueue_run", re_enqueue)
    monkeypatch.setattr(cron_helpers, "_run_reconcile_sweeps", AsyncMock())
    monkeypatch.setattr(cron_helpers, "_ingest_saq_error", AsyncMock())
    monkeypatch.setattr(cron_helpers, "AsyncRedis", MagicMock(from_url=MagicMock(return_value=redis)))
    monkeypatch.setattr(cron_helpers, "RedisQueue", MagicMock(return_value=queue))

    summary = runner.run(cron_helpers.dispatcher_reconcile())

    assert summary["repaired"] >= 1, summary
    dispatched = [call.args for call in re_enqueue.await_args_list if call.args[1] == str(run_id)]
    assert dispatched, f"the tick did not re-dispatch run {run_id}: {re_enqueue.await_args_list}"
    assert dispatched[0][3] == "execute_run", dispatched
    # Same run, same persisted idempotency key — no fresh unit of work was minted.
    assert runner.run(_run_idempotency_key(engine, machine.org_id, run_id)) == key_before
    assert runner.run(_count_runs(engine, machine.org_id, machine.pipeline_id)) == 1


async def _run_idempotency_key(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
        return (await conn.execute(text("SELECT idempotency_key FROM runs WHERE id = :rid"), {"rid": str(run_id)})).scalar_one_or_none()


async def _count_runs(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> int:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
        row = await conn.execute(text("SELECT count(*) FROM runs WHERE organisation_id = :oid AND pipeline_id = :pid"), {"oid": str(org_id), "pid": str(pipeline_id)})
        return int(row.scalar_one())


async def _age_run_heartbeat(engine: AsyncEngine, org_id: uuid.UUID, run_id: uuid.UUID) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
        await conn.execute(
            text("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = :rid AND organisation_id = :oid"),
            {"rid": str(run_id), "oid": str(org_id)},
        )


# ---------------------------------------------------------------------------
# Shared-database isolation proof
# ---------------------------------------------------------------------------
async def test_the_global_sweeps_cannot_reach_the_shared_database(
    lifecycle_runtime: tuple[_LoopRunner, AsyncEngine],
    db_engine: AsyncEngine,
) -> None:
    """A matching row in the SHARED database is untouched by this module's sweeps.

    The park/slot sweeps enumerate every organisation, so isolation has to be a
    different DATABASE, not a filter. Seed a row that would match both sweeps in
    the shared DB, run both sweeps here, and assert it did not move.
    """
    runner, _isolated_engine = lifecycle_runtime
    org_id, pipeline_id, snapshot_id, account_id, run_id = (uuid.uuid4() for _ in range(5))

    async with db_engine.connect() as conn:
        await conn.begin()
        await _insert_org(conn, org_id)
        await _insert_account(conn, account_id)
        await _insert_pipeline(conn, org_id, pipeline_id, account_id)
        await _insert_snapshot(conn, org_id, pipeline_id, snapshot_id, account_id)
        await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
        # awaiting_human with an expired, unclaimed gate -> park-sweep candidate.
        await conn.execute(
            text("INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, account_id, trigger_type, status, input_hash, langgraph_thread_id, run_number, claim_count, claim_token) VALUES (:id, :oid, :pid, :sid, :uid, 'manual', 'awaiting_human', 'hash', :thread, 1, 1, 'shared-token')"),
            {"id": str(run_id), "oid": str(org_id), "pid": str(pipeline_id), "sid": str(snapshot_id), "uid": str(account_id), "thread": f"{org_id}:{run_id}"},
        )
        await conn.execute(
            text("INSERT INTO hitl_claims (run_id, organisation_id, pipeline_id, review_id, expires_at, terminalize_at) VALUES (:rid, :oid, :pid, :gate, now() - interval '2 hours', now() - interval '2 hours')"),
            {"rid": str(run_id), "oid": str(org_id), "pid": str(pipeline_id), "gate": f"shared-gate-{run_id.hex[:8]}"},
        )
        await conn.commit()

    try:
        runner.run(park_expired_hitl_runs(_isolated_engine, grace_seconds=0))
        runner.run(reconcile_pipeline_slots(_isolated_engine, stale_seconds=1))

        async with db_engine.connect() as conn:
            await conn.execute(text("SELECT set_config('app.organisation_id', :val, true)"), {"val": str(org_id)})
            status = (await conn.execute(text("SELECT status FROM runs WHERE id = :rid"), {"rid": str(run_id)})).scalar_one()
        assert status == "awaiting_human", f"the shared database was reaped: {status}"
    finally:
        async with db_engine.connect() as conn:
            await conn.begin()
            await conn.execute(text("DELETE FROM hitl_claims WHERE organisation_id = :oid"), {"oid": str(org_id)})
            await conn.execute(text("DELETE FROM runs WHERE organisation_id = :oid"), {"oid": str(org_id)})
            await conn.execute(text("DELETE FROM pipeline_snapshots WHERE organisation_id = :oid"), {"oid": str(org_id)})
            await conn.execute(text("DELETE FROM pipelines WHERE organisation_id = :oid"), {"oid": str(org_id)})
            await conn.execute(text("DELETE FROM accounts WHERE id = :aid"), {"aid": str(account_id)})
            await conn.execute(text("DELETE FROM organisations WHERE id = :oid"), {"oid": str(org_id)})
            await conn.commit()
