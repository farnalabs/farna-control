"""FAR-1715 — independent SAQ zero-progress terminalisation in the stale sweep.

``stale_run_recovery_sweep`` gains a zero-progress branch for
``dispatcher='saq'`` runs: the SAME predicate shape the dispatcher_reconcile
nodeless router selects with (``cron_helpers._ZERO_PROGRESS_SHAPE_SQL``), the
SAME Settings knob (``saq_zero_progress_max_age_minutes``), the SAME
``executor_stalled`` error_code, a ``status='running'`` guard (double-firing
with the reconcile path is a no-op) and a per-org row cap.

Pre-PR QA M2: the branch also emits the FAR-714 class signal itself — the
SHARED stable error message (``cron_helpers._NODELESS_ZOMBIE_STABLE_MESSAGE``)
via ``_ingest_saq_error`` — so when the sweep wins the race against
dispatcher_reconcile the Error Dashboard group is still fed.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import farna.core.cron_helpers as ch
import farna.core.pipeline_execution as pe
from farna.core.dispatch_phases import PHASE_FIRST_NODE_DISPATCHED
from farna.db.models.run_node_outputs import FINAL_ATTEMPT_KEY


def _zero_progress_row(
    run_id: uuid.UUID,
    *,
    pipeline_id: uuid.UUID | None = None,
    trigger_id: uuid.UUID | None = None,
    claim_count: int = 1,
    dispatch_phase: str | None = None,
    dispatch_phase_entered_at: datetime | None = None,
    started_at: datetime | None = None,
    dispatched_at: datetime | None = None,
) -> tuple[Any, ...]:
    """The UPDATE's RETURNING shape: id + the FAR-714 emission diagnostics."""
    return (
        run_id,
        pipeline_id,
        trigger_id,
        claim_count,
        dispatch_phase,
        dispatch_phase_entered_at,
        started_at,
        dispatched_at,
    )


class _CapturingConn:
    """AsyncConnection double recording statements/params; rows configurable.

    Faithful shape for the sweep helpers: ``dialect`` for the row-lock bound
    (a non-postgres double takes its documented no-op branch), async context
    manager + ``begin()`` returning self, and ``execute`` capturing the
    compiled statement string and bind params.
    """

    def __init__(
        self,
        *,
        org_rows: list[Any] | None = None,
        zero_progress_rows: list[Any] | None = None,
        dialect_name: str = "sqlite",
    ) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.statements: list[str] = []
        self.params: list[dict[str, object]] = []
        self._org_rows = org_rows or []
        self._zero_progress_rows = zero_progress_rows or []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _CapturingConn:
        return self

    async def execute(self, stmt: object, bind: dict[str, object] | None = None) -> MagicMock:
        sql = str(stmt)
        self.statements.append(sql)
        self.params.append(bind or {})
        result = MagicMock()
        if "SELECT id FROM organisations" in sql:
            result.all.return_value = list(self._org_rows)
        elif "dispatcher='saq'" in sql:
            # The FAR-1715 zero-progress UPDATE — the only sweep statement
            # carrying the unspaced saq dispatcher guard.
            result.all.return_value = list(self._zero_progress_rows)
            result.rowcount = len(self._zero_progress_rows)
        else:
            result.all.return_value = []
            result.rowcount = 0
        return result


class _CapturingEngine:
    def __init__(self, conn: _CapturingConn) -> None:
        self._conn = conn

    def connect(self) -> _CapturingConn:
        return self._conn


class TestSweepOrgSaqZeroProgressRuns:
    """Row-level behaviour of the new branch's helper (in-memory double)."""

    async def test_assembled_sql_carries_the_shared_shape_and_guards(self) -> None:
        """The UPDATE reuses ``cron_helpers._ZERO_PROGRESS_SHAPE_SQL`` verbatim
        (single definition — no drift from the reconcile router) and keeps the
        ``status='running'`` + ``dispatcher='saq'`` guards + the row cap."""
        conn = _CapturingConn()
        count = await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=uuid.uuid4(),
            max_age_minutes=135,
            max_rows=pe._SAQ_ZERO_PROGRESS_SWEEP_MAX_ROWS,
            terminalised_run_ids=[],
        )

        assert count == 0
        update_statements = [s for s in conn.statements if s.startswith("UPDATE runs")]
        assert len(update_statements) == 1
        sql = update_statements[0]
        # The shared shape is assembled in — not a re-spelling of it.
        assert ch._ZERO_PROGRESS_SHAPE_SQL in sql
        assert "__ZERO_PROGRESS_SHAPE__" not in sql
        assert "status='running'" in sql
        assert "dispatcher='saq'" in sql
        assert "LIMIT :max_rows" in sql
        assert ":max_age_minutes" in sql
        # FAR-714 emission diagnostics ride on RETURNING (M2).
        assert "RETURNING id, pipeline_id, trigger_id, claim_count, dispatch_phase, " in sql
        assert "dispatch_phase_entered_at, started_at, dispatched_at" in sql

    async def test_binds_the_shared_constants_and_knob(self) -> None:
        """final_key / first_node_phase bind the SHARED constants; the age
        bound and row cap travel as binds; the error_code is the age-bound
        path's ``executor_stalled``."""
        conn = _CapturingConn()
        org_id = uuid.uuid4()
        count = await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=200,
            max_rows=7,
            terminalised_run_ids=[],
        )

        assert count == 0
        update_params = [p for p in conn.params if "code" in p]
        assert len(update_params) == 1
        bound = update_params[0]
        assert bound["oid"] == str(org_id)
        assert bound["code"] == pe.EXECUTOR_STALLED_ERROR_CODE
        assert bound["code"] == "executor_stalled"
        assert "stale_run_recovery zero-progress sweep" in str(bound["detail"])
        assert "200m age bound" in str(bound["detail"])
        assert bound["final_key"] == FINAL_ATTEMPT_KEY
        assert bound["first_node_phase"] == PHASE_FIRST_NODE_DISPATCHED
        assert bound["max_age_minutes"] == 200
        assert bound["max_rows"] == 7

    async def test_scopes_the_org_and_bounds_the_row_lock(self) -> None:
        """The helper is self-contained in the caller's transaction: on
        postgres it issues the hot-``runs`` row-lock bound FIRST, then the
        org ``set_config``."""
        conn = _CapturingConn(dialect_name="postgresql")
        org_id = uuid.uuid4()
        await pe._sweep_org_saq_zero_progress_runs(
            conn,
            org_id=org_id,
            max_age_minutes=135,
            max_rows=25,
            terminalised_run_ids=[],
        )

        assert len(conn.statements) >= 2
        assert "lock_timeout" in conn.statements[0]
        # The org set_config binds the org id (the lock bound binds the
        # timeout value into its own set_config — both use a "val" param).
        assert any(p.get("val") == str(org_id) for p in conn.params)

    async def test_counts_and_collects_terminalised_rows(self) -> None:
        """Rows returned by the UPDATE are counted and collected into the
        caller's terminalised list (journeys advance + compensating daily
        facts ride on that list, like the other sweep branches)."""
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        conn = _CapturingConn(zero_progress_rows=[_zero_progress_row(run_id)])
        terminalised: list[tuple[uuid.UUID, uuid.UUID]] = []

        with patch.object(ch, "_ingest_saq_error", new=AsyncMock()):
            count = await pe._sweep_org_saq_zero_progress_runs(
                conn,
                org_id=org_id,
                max_age_minutes=135,
                max_rows=25,
                terminalised_run_ids=terminalised,
            )

        assert count == 1
        assert terminalised == [(run_id, org_id)]


class TestSweepEmitsFar714ClassSignal:
    """Pre-PR QA M2: the sweep feeds the SAME Error Dashboard group the
    reconcile chokepoint does, when it wins the race."""

    async def test_ingests_the_shared_stable_message_per_terminalised_row(self) -> None:
        """Each terminalised row emits a ``source='saq'`` ingest carrying the
        SHARED stable class message (one group with the chokepoint) and
        ``function='stale_run_recovery'`` naming the writer."""
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        started = datetime.now(UTC) - timedelta(minutes=200)
        conn = _CapturingConn(
            zero_progress_rows=[
                _zero_progress_row(
                    run_id,
                    claim_count=3,
                    started_at=started,
                    dispatched_at=started - timedelta(minutes=2),
                ),
            ],
        )
        ingest = AsyncMock()
        with patch.object(ch, "_ingest_saq_error", new=ingest):
            count = await pe._sweep_org_saq_zero_progress_runs(
                conn,
                org_id=org_id,
                max_age_minutes=135,
                max_rows=25,
                terminalised_run_ids=[],
            )

        assert count == 1
        assert ingest.await_count == 1
        call = ingest.await_args
        assert call.args[1] == org_id
        assert call.kwargs["function"] == "stale_run_recovery"
        # The SHARED constant — the fingerprint that merges this writer into
        # the chokepoint's group. Never a re-spelling.
        assert call.kwargs["message"] == ch._NODELESS_ZOMBIE_STABLE_MESSAGE
        assert call.kwargs["message"] == ("dispatcher_reconcile: claimed-but-never-dispatched zombie terminal-failed (SAQ claimed the run but no node was ever dispatched)")
        context = call.kwargs["context"]
        assert context["run_id"] == str(run_id)
        assert context["claim_count"] == 3
        assert context["zombie_age_minutes"] is not None

    async def test_no_ingest_when_no_rows_match(self) -> None:
        """Zero rows -> zero ingests (the signal counts the class, it never
        fires for a no-op sweep)."""
        conn = _CapturingConn()
        ingest = AsyncMock()
        with patch.object(ch, "_ingest_saq_error", new=ingest):
            count = await pe._sweep_org_saq_zero_progress_runs(
                conn,
                org_id=uuid.uuid4(),
                max_age_minutes=135,
                max_rows=25,
                terminalised_run_ids=[],
            )

        assert count == 0
        ingest.assert_not_awaited()

    async def test_context_normalises_naive_timestamps_and_reports_phase(self) -> None:
        """asyncpg can hand back NAIVE datetimes: the emission normalises each
        to UTC before the elapsed arithmetic, reports the dispatch-phase
        elapsed when the phase was entered, and stringifies the ids."""
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        trigger_id = uuid.uuid4()
        completed = datetime.now(UTC)
        naive_started = (completed - timedelta(minutes=200)).replace(tzinfo=None)
        naive_dispatched = (completed - timedelta(minutes=205)).replace(tzinfo=None)
        naive_phase = (completed - timedelta(minutes=198)).replace(tzinfo=None)
        conn = _CapturingConn(
            zero_progress_rows=[
                _zero_progress_row(
                    run_id,
                    pipeline_id=pipeline_id,
                    trigger_id=trigger_id,
                    claim_count=2,
                    dispatch_phase=PHASE_FIRST_NODE_DISPATCHED,
                    dispatch_phase_entered_at=naive_phase,
                    started_at=naive_started,
                    dispatched_at=naive_dispatched,
                ),
            ],
        )
        ingest = AsyncMock()
        with patch.object(ch, "_ingest_saq_error", new=ingest):
            count = await pe._sweep_org_saq_zero_progress_runs(
                conn,
                org_id=org_id,
                max_age_minutes=135,
                max_rows=25,
                terminalised_run_ids=[],
            )

        assert count == 1
        context = ingest.await_args.kwargs["context"]
        assert context["pipeline_id"] == str(pipeline_id)
        assert context["trigger_id"] == str(trigger_id)
        assert context["dispatch_phase"] == PHASE_FIRST_NODE_DISPATCHED
        assert context["dispatch_phase_elapsed"] is not None
        assert context["zombie_age_minutes"] is not None
        assert context["saq_queue_wait_seconds"] is not None
        assert context["dispatched_at"] is not None
        assert context["started_at"] is not None

    async def test_context_tolerates_missing_timestamps_and_ids(self) -> None:
        """A sparse row (no timestamps, no pipeline/trigger ids) yields None
        diagnostics — the emission never raises on the NULL shape and the log
        renders 'unknown' for each absent value."""
        run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        conn = _CapturingConn(zero_progress_rows=[_zero_progress_row(run_id)])
        ingest = AsyncMock()
        with patch.object(ch, "_ingest_saq_error", new=ingest):
            count = await pe._sweep_org_saq_zero_progress_runs(
                conn,
                org_id=org_id,
                max_age_minutes=135,
                max_rows=25,
                terminalised_run_ids=[],
            )

        assert count == 1
        context = ingest.await_args.kwargs["context"]
        assert context["run_id"] == str(run_id)
        assert context["pipeline_id"] is None
        assert context["trigger_id"] is None
        assert context["dispatch_phase"] is None
        assert context["dispatch_phase_elapsed"] is None
        assert context["zombie_age_minutes"] is None
        assert context["saq_queue_wait_seconds"] is None
        assert context["dispatched_at"] is None
        assert context["started_at"] is None

    def test_the_reconcile_chokepoint_shares_the_same_constant(self) -> None:
        """The chokepoint's zero-node arm reads the SAME constant, so the two
        writers cannot drift into separate groups.

        The chokepoint delegates its error ingestion to
        ``_ingest_zombie_error``; the guard follows that call so the constant
        is still asserted to be on the chokepoint's path rather than in the
        (now extracted) function body.
        """
        import inspect

        chokepoint = inspect.getsource(ch._fail_nodeless_run)
        assert "_ingest_zombie_error(" in chokepoint

        ingest = inspect.getsource(ch._ingest_zombie_error)
        assert "_NODELESS_ZOMBIE_STABLE_MESSAGE" in ingest


class TestStaleRunRecoverySweepWiring:
    """The sweep resolves the knob once, runs the branch per org, reports it."""

    async def test_sweep_runs_the_branch_with_the_knob_and_row_cap(self) -> None:
        """The per-org loop calls the zero-progress branch after the legacy
        UPDATEs, binding the resolved knob and the row cap resolved from the
        reconcile terminalize knob, and reports ``saq_zero_progress_swept`` in
        the result."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = 200
        # REAL int so _int_setting uses it (a MagicMock attribute coerces to
        # the coded fallback, which the default-path test below covers).
        settings.dispatcher_reconcile_terminalize_max_per_tick = 25
        with patch.object(pe, "get_settings", return_value=settings):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 0
        zp_statements = [s for s in conn.statements if "dispatcher='saq'" in s]
        assert len(zp_statements) == 1
        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_age_minutes"] == 200
        assert zp_params[0]["max_rows"] == 25

    async def test_sweep_resolves_the_row_cap_from_the_reconcile_knob(self) -> None:
        """The per-org row cap is the EXISTING reconcile knob — an operator
        pin flows through to the sweep's LIMIT (one knob, never two)."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = None
        settings.dispatcher_reconcile_terminalize_max_per_tick = 7
        with patch.object(pe, "get_settings", return_value=settings):
            await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_rows"] == 7

    async def test_sweep_row_cap_falls_back_to_the_coded_default(self) -> None:
        """A stand-in settings object without a REAL int knob keeps the
        protective coded cap (25) — never a MagicMock-coerced 1."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = None
        # Deliberately leave dispatcher_reconcile_terminalize_max_per_tick as
        # a MagicMock attribute.
        with patch.object(pe, "get_settings", return_value=settings):
            await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_rows"] == pe._SAQ_ZERO_PROGRESS_SWEEP_MAX_ROWS

    async def test_sweep_falls_back_to_the_historical_bound_when_knob_is_unset(self) -> None:
        """An unset (None) knob — the shipped default — keeps the pre-FAR-1715
        ~135m derivation: behaviour-neutral at the default."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = None
        with patch.object(pe, "get_settings", return_value=settings):
            await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        zp_params = [p for p in conn.params if "max_age_minutes" in p and "final_key" in p]
        assert len(zp_params) == 1
        assert zp_params[0]["max_age_minutes"] == 135

    async def test_sweep_reports_terminalised_zero_progress_rows(self) -> None:
        """A row selected by the branch lands in the result count."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        run_id = uuid.uuid4()
        conn = _CapturingConn(org_rows=[(org_id,)], zero_progress_rows=[_zero_progress_row(run_id)])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        with (
            patch.object(pe, "get_settings", return_value=settings),
            # The post-commit advance machinery is exercised elsewhere; keep
            # this test on the sweep's own counting/selection behaviour. The
            # M2 class ingest is covered by TestSweepEmitsFar714ClassSignal.
            patch.object(pe, "_advance_terminalised_run", new=AsyncMockNone()),
            patch.object(ch, "_ingest_saq_error", new=AsyncMock()),
        ):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 1

    async def test_no_orgs_early_return_carries_the_new_key(self) -> None:
        """The no-orgs early return stays key-complete."""
        conn = _CapturingConn(org_rows=[])
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        with patch.object(pe, "get_settings", return_value=settings):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert result["saq_zero_progress_swept"] == 0
        assert result["never_dispatched_swept"] == 0


class AsyncMockNone:
    """Async no-op stand-in for the post-commit terminal advance."""

    async def __call__(self, *args: object, **kwargs: object) -> None:
        return None


class TestPreCheckpointSchemaTick:
    """FAR-2782 — a tick that runs before the backend's checkpointer DDL.

    The SAQ zero-progress arm is the only statement in this sweep that
    references the LangGraph ``checkpoints`` table, which is created by the
    backend's boot-time saver DDL (``api.main._init_checkpointer``) and never by
    Alembic. ``saq-system`` starts as soon as ``db`` is healthy and does not run
    the entrypoint, so on a fresh install its first ticks hit a fully-migrated
    database with no ``checkpoints`` table. PostgreSQL resolves every relation
    in a statement at parse/plan time, so the arm's ``UPDATE`` raised
    ``relation "checkpoints" does not exist`` even though it would have matched
    no rows — and the wrapper persisted that as the sweep's reported failure.
    These tests pin the DEFERRAL: that one arm is skipped, everything else still
    runs, and the tick reports a deferral instead of an error.
    """

    @staticmethod
    def _settings() -> MagicMock:
        settings = MagicMock()
        settings.saq_never_dispatched_window = 300
        settings.saq_worker_lost_window = 600
        settings.saq_zero_progress_max_age_minutes = 200
        settings.dispatcher_reconcile_terminalize_max_per_tick = 25
        return settings

    async def test_absent_checkpoint_schema_skips_only_the_zero_progress_arm(self) -> None:
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        with patch.object(pe, "get_settings", return_value=self._settings()):
            result = await pe.stale_run_recovery_sweep(
                _CapturingEngine(conn),  # type: ignore[arg-type]
                checkpoint_schema_present=False,
            )

        # The arm's UPDATE — the one statement that cannot be planned without
        # the checkpoints relation — never reaches the database at all.
        assert [s for s in conn.statements if "dispatcher='saq'" in s] == []
        # ... while the arms that depend only on the Alembic schema still ran.
        assert [s for s in conn.statements if "error_code = 'never_dispatched'" in s]
        # A deferral, never an error: the tick did not fail.
        assert "error" not in result
        assert result["zero_progress_deferred"] == "checkpointer_schema_missing"
        assert result["saq_zero_progress_swept"] == 0

    async def test_present_checkpoint_schema_runs_the_arm_and_reports_no_deferral(self) -> None:
        """The default keeps today's behaviour exactly: the arm runs and no
        deferral marker is emitted (so the deferral can never become a silent
        way to stop the arm on a healthy install)."""
        org_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
        conn = _CapturingConn(org_rows=[(org_id,)])
        with patch.object(pe, "get_settings", return_value=self._settings()):
            result = await pe.stale_run_recovery_sweep(_CapturingEngine(conn))  # type: ignore[arg-type]

        assert len([s for s in conn.statements if "dispatcher='saq'" in s]) == 1
        assert result["zero_progress_deferred"] is None

    def test_probe_asks_the_catalog_instead_of_referencing_the_relation(self) -> None:
        """The probe must not name ``checkpoints`` as a FROM/UPDATE target —
        that is exactly what raises. ``to_regclass`` is a catalog lookup that
        returns NULL for a missing relation and resolves through the session
        search_path like the arm's own unqualified reference."""
        assert str(pe._CHECKPOINT_SCHEMA_PROBE_SQL) == "SELECT to_regclass('checkpoints') IS NOT NULL"

    async def test_probe_reports_an_absent_relation(self) -> None:
        class _Conn:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(self, *args: object) -> bool:
                return False

            async def scalar(self, _stmt: object) -> bool:
                return False

        class _Engine:
            def connect(self) -> _Conn:
                return _Conn()

        assert await pe.langgraph_checkpoint_schema_present(_Engine()) is False  # type: ignore[arg-type]

    async def test_probe_fails_open_to_running_the_arm(self) -> None:
        """An unevaluable probe must leave the sweep's behaviour exactly as it
        was — run the arm and report whatever the real statements do — so a
        probe bug can never suppress a genuine failure."""

        class _ExplodingEngine:
            def connect(self) -> object:
                raise RuntimeError("connection pool exhausted")

        assert await pe.langgraph_checkpoint_schema_present(_ExplodingEngine()) is True  # type: ignore[arg-type]

    async def test_probe_lets_cancellation_propagate(self) -> None:
        """Cancellation is never swallowed by the fail-open path: a cancelled
        tick must stay cancelled so the worker can stop, not be reported as a
        healthy sweep."""

        class _CancellingEngine:
            def connect(self) -> object:
                raise asyncio.CancelledError

        try:
            await pe.langgraph_checkpoint_schema_present(_CancellingEngine())  # type: ignore[arg-type]
        except asyncio.CancelledError:
            return
        raise AssertionError("CancelledError must propagate, not be caught by the fail-open path")
