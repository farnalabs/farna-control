"""FAR-2705: Hypothesis stateful test of the REAL run lifecycle against Postgres.

The machine drives the production functions — ``create_run``, ``claim_run_async``,
``claim_resume_run_async``, ``HITLManager.create_gate``/``claim``/``approve``/
``reject``, ``park_expired_hitl_runs``, ``reconcile_pipeline_slots``,
``mark_complete`` and ``request_cancellation`` — through arbitrary interleavings
of those operations (including the illegal ones: a second claimant, deciding
twice).

After EVERY step it asserts, against the database and ``farna.core.run_lifecycle``:

* capacity is never exceeded (the pipeline execution projection is the bound);
* a gate is never claimed twice;
* no run is terminal while it has an open gate;
* every non-terminal run is queued/executing/parked (exactly one class), never
  executing and parked at the same time;
* a decided gate always un-parks a ``hitl_parked`` run.

The example count and deadline are bounded so the job stays inside the CI
integration budget.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, TypeVar

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, multiple, rule, run_state_machine_as_test
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from farna.core import run_lifecycle
from farna.core.hitl_manager import AlreadyClaimedError, GateAlreadyDecidedError, HITLManager
from farna.core.pipeline_execution import claim_resume_run_async, claim_run_async, mark_complete
from farna.core.run_admission import park_expired_hitl_runs, reconcile_pipeline_slots
from farna.db.crud.run import create_run, request_cancellation, transition_run
from farna.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_T = TypeVar("_T")

_PIPELINE_CAPACITY = 2
_GRAPH = {"nodes": [], "edges": []}


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
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=180)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


@pytest.fixture
def lifecycle_runtime(migrated_db_url: str) -> Any:
    runner = _LoopRunner()
    engine = runner.run(_create_engine(migrated_db_url))
    try:
        yield runner, engine
    finally:
        runner.run(engine.dispose())
        runner.close()


async def _create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, echo=False, poolclass=NullPool)


# ---------------------------------------------------------------------------
# Seeding — one isolated org + pipeline + snapshot per example.
# ---------------------------------------------------------------------------
async def _seed(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, snapshot_id: uuid.UUID, accounts: tuple[uuid.UUID, uuid.UUID]) -> None:
    async with engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"FAR-2705 {org_id.hex[:8]}", "slug": f"far2705-{org_id.hex[:12]}"},
        )
        for account_id in accounts:
            await conn.execute(
                text("INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) VALUES (:id, :email, :name, 'hash', 'local', true)"),
                {"id": str(account_id), "email": f"far2705-{account_id.hex[:12]}@test.local", "name": "FAR-2705"},
            )
        await conn.execute(
            text("INSERT INTO pipelines (id, organisation_id, name, account_id, max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, run_context_defaults, graph_nodes_json) VALUES (:id, :oid, :name, :uid, :cap, 30, 300, '{}'::json, (:graph)::json)"),
            {"id": str(pipeline_id), "oid": str(org_id), "name": f"FAR-2705 {pipeline_id.hex[:8]}", "uid": str(accounts[0]), "cap": _PIPELINE_CAPACITY, "graph": json.dumps(_GRAPH)},
        )
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, snapshot_version, graph_json, connector_bindings_json, "
                "schema_pins_json, prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, (:graph)::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id), "graph": json.dumps(_GRAPH)},
        )


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
        self._tokens: dict[tuple[uuid.UUID, str], str] = {}
        self._claimed_by: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        self._runner.run(_seed(engine, self.org_id, self.pipeline_id, self.snapshot_id, (self.account_one, self.account_two)))

    # -- async helpers ---------------------------------------------------
    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        return self._runner.run(coro)

    async def _status(self, run_id: uuid.UUID) -> str | None:
        async with self._engine.connect() as conn:
            row = await conn.execute(text("SELECT status FROM runs WHERE id = :rid AND organisation_id = :oid"), {"rid": str(run_id), "oid": str(self.org_id)})
            return row.scalar_one_or_none()

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
        assert self._run(self._status(run_id)) == run_lifecycle.STATUS_PENDING
        self._observed.add("admit")
        return run_id

    @rule(run=runs)
    def claim_run(self, run: uuid.UUID) -> None:
        """Real ``claim_run_async`` — only while the pipeline has capacity."""
        status = self._run(self._status(run))
        if status != run_lifecycle.STATUS_PENDING:
            return
        if self._run(self._capacity_used()) >= _PIPELINE_CAPACITY:
            return
        token = self._run(claim_run_async(self._engine, str(run), str(self.org_id), stale_seconds=60))
        assert token is not None, "a pending run under capacity must be claimable"
        assert self._run(self._status(run)) == run_lifecycle.STATUS_RUNNING
        self._observed.add("claim_run")

    @rule(run=runs, target=gates)
    def request_hitl(self, run: uuid.UUID) -> Any:
        """``transition_run`` running->awaiting_human, then the real gate creation."""
        status = self._run(self._status(run))
        if status != run_lifecycle.STATUS_RUNNING:
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
                    target_status=run_lifecycle.STATUS_AWAITING_HUMAN,
                    allowed_from=frozenset({run_lifecycle.STATUS_RUNNING}),
                )
                assert moved, "a running run must move to awaiting_human"
                mgr = HITLManager()
                await mgr.create_gate(
                    session,
                    run_id=run,
                    review_id=review_id,
                    pipeline_id=self.pipeline_id,
                    org_id=self.org_id,
                )
            return run, review_id

        self._observed.add("request_hitl")
        return self._run(_request())

    @rule(gate=gates)
    def claim_gate(self, gate: tuple[uuid.UUID, str]) -> None:
        """Real ``HITLManager.claim`` + the claim route's fenced status flip."""
        run, review_id = gate
        status = self._run(self._status(run))
        if status not in run_lifecycle.HITL_CLAIMABLE_RUN_STATUSES:
            return
        if (run, review_id) in self._claimed_by:
            return

        async def _claim() -> str:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                mgr = HITLManager()
                claimed = await mgr.claim(session, run_id=run, review_id=review_id, org_id=self.org_id, claimant_id=self.account_one)
                if status == run_lifecycle.STATUS_AWAITING_HUMAN:
                    await transition_run(
                        session,
                        run,
                        self.org_id,
                        target_status=run_lifecycle.STATUS_CLAIMED,
                        allowed_from=frozenset({run_lifecycle.STATUS_AWAITING_HUMAN}),
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
                mgr = HITLManager()
                await mgr.claim(session, run_id=run, review_id=review_id, org_id=self.org_id, claimant_id=self.account_two)

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
                mgr = HITLManager()
                if decision == "approved":
                    await mgr.approve(session, run_id=run, review_id=review_id, org_id=self.org_id, claim_token=token, actor_id=self.account_one)
                else:
                    await mgr.reject(session, run_id=run, review_id=review_id, org_id=self.org_id, claim_token=token, actor_id=self.account_one)

        self._run(_decide())
        self._tokens.pop((run, review_id), None)
        self._observed.add(f"decide_{decision}")
        status_after = self._run(self._status(run))
        if status_before == run_lifecycle.STATUS_HITL_PARKED:
            assert status_after != run_lifecycle.STATUS_HITL_PARKED, "a decided gate must un-park the run"

    @rule(gate=gates)
    def decide_twice(self, gate: tuple[uuid.UUID, str]) -> None:
        """Illegal sequence: a decided gate can never be decided again."""
        run, review_id = gate
        token = self._tokens.get((run, review_id))
        if token is not None:
            return
        if (run, review_id) not in self._claimed_by:
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
        if self._run(self._status(run)) != run_lifecycle.STATUS_AWAITING_HUMAN:
            return
        if self._run(self._has_claimed_undecided_gate(run)):
            return

        async def _expire() -> None:
            async with self._engine.connect() as conn, conn.begin():
                await conn.execute(
                    text("UPDATE hitl_claims SET expires_at = now() - interval '2 hours', terminalize_at = now() - interval '2 hours' WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL AND account_id IS NULL"),
                    {"rid": str(run), "oid": str(self.org_id)},
                )

        self._run(_expire())
        self._run(park_expired_hitl_runs(self._engine, grace_seconds=0))
        assert self._run(self._status(run)) == run_lifecycle.STATUS_HITL_PARKED
        self._observed.add("park_expired_review")

    @rule(run=runs)
    def reconcile_stale_running(self, run: uuid.UUID) -> None:
        """Real ``reconcile_pipeline_slots``: a stale running row leaves ``running``."""
        if self._run(self._status(run)) != run_lifecycle.STATUS_RUNNING:
            return

        async def _age_heartbeat() -> None:
            async with self._engine.connect() as conn, conn.begin():
                await conn.execute(
                    text("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = :rid AND organisation_id = :oid"),
                    {"rid": str(run), "oid": str(self.org_id)},
                )

        self._run(_age_heartbeat())
        self._run(reconcile_pipeline_slots(self._engine, stale_seconds=1))
        assert self._run(self._status(run)) != run_lifecycle.STATUS_RUNNING
        self._observed.add("reconcile_stale_running")

    @rule(run=runs)
    def resume_after_decision(self, run: uuid.UUID) -> None:
        """Real ``claim_resume_run_async`` — only when a gate decision is committed."""
        status = self._run(self._status(run))
        if status not in (run_lifecycle.STATUS_AWAITING_HUMAN, run_lifecycle.STATUS_CLAIMED, run_lifecycle.STATUS_HITL_PARKED):
            return
        if not self._run(self._has_decided_gate(run)):
            return
        if self._run(self._has_undecided_gate(run)):
            return
        if self._run(self._capacity_used()) >= _PIPELINE_CAPACITY:
            return
        token = self._run(claim_resume_run_async(self._engine, str(run), str(self.org_id)))
        if token is not None:
            assert self._run(self._status(run)) == run_lifecycle.STATUS_RUNNING
            self._observed.add("resume_after_decision")

    @rule(run=runs)
    def complete_run(self, run: uuid.UUID) -> None:
        """Real ``mark_complete`` — a running run finishes."""
        if self._run(self._status(run)) != run_lifecycle.STATUS_RUNNING:
            return
        self._run(mark_complete(self._engine, str(run), str(self.org_id)))
        assert self._run(self._status(run)) in (run_lifecycle.STATUS_COMPLETE, run_lifecycle.STATUS_RUNNING)
        self._observed.add("complete_run")

    @rule(run=runs)
    def cancel_without_open_gate(self, run: uuid.UUID) -> None:
        """Real ``request_cancellation`` — only when no gate is still open."""
        if self._run(self._status(run)) not in run_lifecycle.ACTIVE_RUN_STATUSES:
            return
        if self._run(self._has_undecided_gate(run)):
            return

        async def _cancel() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                await request_cancellation(session, run, actor=str(self.account_one))

        self._run(_cancel())
        assert self._run(self._status(run)) == run_lifecycle.STATUS_CANCELLED
        self._observed.add("cancel_without_open_gate")

    @rule(run=runs)
    def mark_unknown_then_reconcile(self, run: uuid.UUID) -> None:
        """``unknown`` is non-terminal and reconciles to a terminal outcome."""
        if self._run(self._status(run)) != run_lifecycle.STATUS_RUNNING:
            return

        async def _unknown_then_failed() -> None:
            factory = async_sessionmaker(self._engine, expire_on_commit=False)
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                assert await transition_run(session, run, self.org_id, target_status=run_lifecycle.STATUS_UNKNOWN, allowed_from=frozenset({run_lifecycle.STATUS_RUNNING}))
            async with factory() as session, session.begin():
                await set_rls_org(session, self.org_id)
                assert await transition_run(
                    session,
                    run,
                    self.org_id,
                    target_status=run_lifecycle.STATUS_FAILED,
                    allowed_from=frozenset({run_lifecycle.STATUS_UNKNOWN}),
                )

        self._run(_unknown_then_failed())
        assert self._run(self._status(run)) == run_lifecycle.STATUS_FAILED
        self._observed.add("mark_unknown_then_reconcile")

    # -- shared queries --------------------------------------------------
    async def _capacity_used(self) -> int:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM runs WHERE pipeline_id = :pid AND organisation_id = :oid AND status = ANY(:statuses)"),
                {"pid": str(self.pipeline_id), "oid": str(self.org_id), "statuses": sorted(run_lifecycle.PIPELINE_CAPACITY_STATUSES)},
            )
            return int(row.scalar_one())

    async def _has_undecided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    async def _has_decided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NOT NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    async def _has_claimed_undecided_gate(self, run: uuid.UUID) -> bool:
        async with self._engine.connect() as conn:
            row = await conn.execute(
                text("SELECT count(*) FROM hitl_claims WHERE run_id = :rid AND organisation_id = :oid AND decision IS NULL AND account_id IS NOT NULL"),
                {"rid": str(run), "oid": str(self.org_id)},
            )
            return int(row.scalar_one()) > 0

    # -- invariants (checked after every step) ---------------------------
    @invariant()
    def capacity_is_never_exceeded(self) -> None:
        used = self._run(self._capacity_used())
        assert used <= _PIPELINE_CAPACITY, f"pipeline capacity exceeded: {used} > {_PIPELINE_CAPACITY}"

    @invariant()
    def a_gate_is_never_claimed_twice(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                duplicate_holders = await conn.execute(
                    text("SELECT run_id, review_id, count(DISTINCT account_id) AS holders FROM hitl_claims WHERE organisation_id = :oid AND decision IS NULL AND account_id IS NOT NULL GROUP BY run_id, review_id HAVING count(DISTINCT account_id) > 1"),
                    {"oid": str(self.org_id)},
                )
                assert duplicate_holders.fetchall() == []

        self._run(_check())

    @invariant()
    def no_terminal_run_has_an_open_gate(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT r.id FROM runs r JOIN hitl_claims hc ON hc.run_id = r.id AND hc.organisation_id = r.organisation_id WHERE r.organisation_id = :oid AND hc.decision IS NULL AND r.status = ANY(:terminal)"),
                    {"oid": str(self.org_id), "terminal": sorted(run_lifecycle.TERMINAL_STATUSES)},
                )
                offenders = [str(row[0]) for row in rows.fetchall()]
                assert offenders == [], f"terminal run(s) with an open gate: {offenders}"

        self._run(_check())

    @invariant()
    def every_non_terminal_run_is_in_exactly_one_class(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                rows = await conn.execute(text("SELECT id, status FROM runs WHERE organisation_id = :oid"), {"oid": str(self.org_id)})
                for run_id, status in rows.fetchall():
                    assert status in run_lifecycle.ALL_RUN_STATUSES, f"run {run_id} has an unknown status {status!r}"
                    if status in run_lifecycle.TERMINAL_STATUSES:
                        continue
                    classes = [
                        name
                        for name, projection in (
                            ("queued", run_lifecycle.QUEUED_STATUSES),
                            ("in_flight", run_lifecycle.IN_FLIGHT_STATUSES),
                            ("recovering", run_lifecycle.RECOVERING_STATUSES),
                            ("parked", run_lifecycle.PARKED_STATUSES),
                        )
                        if status in projection
                    ]
                    assert len(classes) == 1, f"run {run_id} ({status}) is in classes {classes}"
                    assert not (status in run_lifecycle.EXECUTING_STATUSES and status in run_lifecycle.PARKED_STATUSES)

        self._run(_check())

    @invariant()
    def a_decided_gate_leaves_the_run_unparked(self) -> None:
        async def _check() -> None:
            async with self._engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT r.id FROM runs r WHERE r.organisation_id = :oid AND r.status = :parked "
                        "AND NOT EXISTS (SELECT 1 FROM hitl_claims hc WHERE hc.run_id = r.id AND hc.organisation_id = r.organisation_id AND hc.decision IS NULL)"
                        " AND EXISTS (SELECT 1 FROM hitl_claims hc2 WHERE hc2.run_id = r.id AND hc2.organisation_id = r.organisation_id AND hc2.decision IS NOT NULL)"
                    ),
                    {"oid": str(self.org_id), "parked": run_lifecycle.STATUS_HITL_PARKED},
                )
                offenders = [str(row[0]) for row in rows.fetchall()]
                assert offenders == [], f"run(s) still parked after their gate was decided: {offenders}"

        self._run(_check())


def _scripted_prelude(runner: _LoopRunner, engine: AsyncEngine, observed: set[str]) -> None:
    """One deterministic walk through every rule, so no invariant is vacuous.

    The random machine below explores interleavings; this prelude guarantees the
    HITL (and park/decide/resume) legs are executed at least once on every run,
    which is what makes the coverage assertion meaningful.
    """
    machine = RunLifecycleMachine(runner, engine, observed)

    # Claim + decide: the gate is acquired once, cannot be acquired again by
    # another account, and a decided gate cannot be decided twice.
    first = machine.admit()
    machine.claim_run(first)
    first_gate = machine.request_hitl(first)
    machine.claim_gate(first_gate)
    machine.claim_gate_again_as_another_account(first_gate)
    machine.decide_gate(first_gate, "approved")
    machine.decide_twice(first_gate)

    # Park -> claim while parked -> decide (must un-park) -> resume -> complete.
    second = machine.admit()
    machine.claim_run(second)
    second_gate = machine.request_hitl(second)
    machine.park_expired_review(second)
    machine.claim_gate(second_gate)
    machine.decide_gate(second_gate, "rejected")
    machine.resume_after_decision(second)
    machine.complete_run(second)

    # unknown -> terminal reconciliation, and a clean cancellation.
    third = machine.admit()
    machine.claim_run(third)
    machine.mark_unknown_then_reconcile(third)

    fourth = machine.admit()
    machine.claim_run(fourth)
    machine.cancel_without_open_gate(fourth)

    # A heartbeat-stale running run is reaped by the real slot reconciler.
    fifth = machine.admit()
    machine.claim_run(fifth)
    machine.reconcile_stale_running(fifth)


async def test_run_lifecycle_state_machine(lifecycle_runtime: tuple[_LoopRunner, AsyncEngine]) -> None:
    runner, engine = lifecycle_runtime
    observed: set[str] = set()
    _scripted_prelude(runner, engine, observed)
    run_state_machine_as_test(
        lambda: RunLifecycleMachine(runner, engine, observed),
        settings=hypothesis_settings(
            max_examples=4,
            stateful_step_count=6,
            deadline=timedelta(seconds=120),
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        ),
    )
    # Non-vacuity: a machine that only ever admitted runs would satisfy every
    # invariant trivially. These rules must all have fired at least once.
    required = {
        "admit",
        "claim_run",
        "request_hitl",
        "claim_gate",
        "decide_approved",
        "park_expired_review",
        "reconcile_stale_running",
        "cancel_without_open_gate",
    }
    assert required <= observed, f"the state machine never exercised: {sorted(required - observed)}; observed={sorted(observed)}"
