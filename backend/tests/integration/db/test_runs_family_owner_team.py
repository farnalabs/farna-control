"""ADR 070 Phase 1A - runs-family ownership on real Postgres (migrations 0365/0366).

Proves, against a real database and the REAL Alembic chain:

* **0365 upgrade** adds ``owner_team_id`` to the six runs-family child tables and
  ``owner_unresolved`` to ``runs``/``suite_runs``;
* the **three-pass backfill** resolves the owner AT ``created_at`` from the
  pipeline's ownership audit-event chain - never from the live pipeline - so a
  reassigned pipeline does not move its historical runs to the new team;
* a run whose history cannot be trusted is left UNOWNED and marked legacy,
  distinguishably from a genuinely org-level run;
* the children inherit the resolved parent run's team;
* the **Phase 1B data precondition** holds after the backfill: zero rows where
  ``owner_team_id IS NULL`` AND the pipeline is team-owned AND the row is not
  legacy;
* **0365 downgrade** removes every column it added (data loss by design);
* **0366** makes ``onboarding_progress``' policy fail CLOSED on an unset org
  context (zero rows, not a ``current_setting`` error), and its downgrade
  restores the raising shape.

The fixture builds its own container and applies the chain up to 0364, i.e. the
state a real install is in immediately before this change.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]

#: The revision immediately before this change - the state a real install is in.
_PRE_CHANGE = "0364_rewrite_legacy_persisted_values"
_HEAD = "0366_onboarding_progress_rls_setting"

_CHILD_TABLES = (
    "evidence",
    "node_observations",
    "nodes",
    "run_attempts",
    "run_evidence",
    "run_node_outputs",
)

#: The Phase 1B gate. Zero rows == the flip is safe.
UNOWNED_PRECONDITION_SQL = """
SELECT count(*)
FROM runs AS r
JOIN pipelines AS p ON p.id = r.pipeline_id
WHERE r.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
  AND r.owner_unresolved IS NOT TRUE
"""


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "farna" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _sync_url(asyncpg_url: str) -> str:
    from farna.db.migrations import env as migration_env

    return migration_env._to_sync_url(asyncpg_url)


def _with_credentials(database_url: str, user: str, password: str) -> str:
    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


@pytest.fixture
def pre_change_db(monkeypatch: pytest.MonkeyPatch):
    """A real Postgres migrated to 0364 (before this change). Yields the superuser
    sync URL and the alembic config."""
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")

    engine = sa.create_engine(_sync_url(raw))
    with engine.connect() as conn:
        conn.execute(sa.text('DROP ROLE IF EXISTS "farna_control_migrate"'))
        conn.execute(sa.text('DROP ROLE IF EXISTS "farna_control_breakglass"'))
        conn.execute(sa.text('DROP ROLE IF EXISTS "farna_control_app"'))
        conn.execute(sa.text("CREATE ROLE farna_control_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
        conn.execute(sa.text("CREATE ROLE farna_control_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
        conn.execute(sa.text("CREATE ROLE farna_control_app NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD 'apppass'"))
        conn.commit()
    engine.dispose()

    app_url = _with_credentials(raw, "farna_control_app", "apppass")
    bg_url = _with_credentials(raw, "farna_control_breakglass", "bgpass")
    config = _alembic_config(raw)
    with monkeypatch.context() as m:
        m.setenv("DATABASE_URL", raw)
        m.setenv("DATABASE_ADMIN_URL", raw)
        m.setenv("FARNA_CONTROL_BREAK_GLASS_DATABASE_URL", bg_url)
        from farna.db.bootstrap_role import bootstrap_roles

        asyncio.run(bootstrap_roles(raw, app_url))
        command.upgrade(config, _PRE_CHANGE)
        yield _sync_url(raw), config
    pg.stop()


@pytest.fixture
def head_db(pre_change_db):
    """The same database upgraded to the migration head (this change applied)."""
    sync_url, config = pre_change_db
    command.upgrade(config, _HEAD)
    return sync_url, config


def _column_names(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
                {"t": table},
            )
        }


def _scalar(engine: sa.Engine, sql: str, **params: object) -> object:
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


class Seed:
    """Ids of the seeded fixture rows (one attribute per named entity)."""

    def __init__(self) -> None:
        self.org = uuid.uuid4()
        self.account = uuid.uuid4()
        self.team_a = uuid.uuid4()
        self.team_b = uuid.uuid4()
        self.snapshot = uuid.uuid4()
        self.suite = uuid.uuid4()
        self.dataset = uuid.uuid4()
        self.backend = uuid.uuid4()
        self.suite_run = uuid.uuid4()
        self.pipelines = {
            name: uuid.uuid4()
            for name in (
                "never_moved",
                "moved",
                "moved_from_org",
                "broken",
                "org_level",
                "org_then_team",
                "moved_to_org",
                "broken_org_level",
            )
        }
        self.runs: dict[str, uuid.UUID] = {}


def _seed(engine: sa.Engine) -> Seed:
    """Seed the ownership shapes the three passes must distinguish.

    Pipelines / event chains:
      never_moved     owner A, no events
      moved           owner B, one event A->B at T-1h
      moved_from_org  owner B, events NULL->A (T-2h), A->B (T-30m)
      broken          owner B, one event A->NULL at T-1h (a later move wrote no
                      event, so the chain cannot corroborate the current owner)
      org_level       owner NULL, no events
      org_then_team   owner A, one event NULL->A at T-1h
    """
    now = datetime.now(UTC)
    seed = Seed()
    p = seed.pipelines
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'Owner Org', 'owner-org', '{}'::jsonb)"), {"id": seed.org})
        conn.execute(
            sa.text("INSERT INTO accounts (id, email, display_name) VALUES (:id, 'owner@example.com', 'Owner')"),
            {"id": seed.account},
        )
        for team_id, team_name in ((seed.team_a, "Team A"), (seed.team_b, "Team B")):
            conn.execute(
                sa.text("INSERT INTO teams (id, name, account_id, organisation_id, notification_endpoints, settings) VALUES (:id, :name, :acct, :org, '[]'::jsonb, '{}'::jsonb)"),
                {"id": team_id, "name": team_name, "acct": seed.account, "org": seed.org},
            )

        for key, owner in (
            ("never_moved", seed.team_a),
            ("moved", seed.team_b),
            ("moved_from_org", seed.team_b),
            ("broken", seed.team_b),
            ("org_level", None),
            ("org_then_team", seed.team_a),
            ("moved_to_org", None),
            ("broken_org_level", None),
        ):
            conn.execute(
                sa.text("INSERT INTO pipelines (id, name, account_id, organisation_id, owner_team_id, visibility) VALUES (:id, :name, :acct, :org, :owner, 'org')"),
                {"id": p[key], "name": key, "acct": seed.account, "org": seed.org, "owner": owner},
            )

        conn.execute(
            sa.text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, snapshot_version, organisation_id, graph_json, "
                "connector_bindings_json, schema_pins_json, prompt_pins_json, model_backend_pins_json, "
                "run_context_defaults) VALUES "
                "(:id, :pid, 1, :org, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb)"
            ),
            {"id": seed.snapshot, "pid": p["never_moved"], "org": seed.org},
        )

        def add_run(name: str, pipeline: str, created_at: datetime, owner: uuid.UUID | None = None) -> None:
            run_id = uuid.uuid4()
            seed.runs[name] = run_id
            conn.execute(
                sa.text("INSERT INTO runs (id, pipeline_id, snapshot_id, trigger_type, status, run_number, input_hash, langgraph_thread_id, organisation_id, owner_team_id, created_at) VALUES (:id, :pid, :sid, 'manual', 'complete', :number, :hash, :thread, :org, :owner, :created_at)"),
                {
                    "id": run_id,
                    "pid": p[pipeline],
                    "sid": seed.snapshot,
                    "number": len(seed.runs),
                    "hash": "0" * 64,
                    "thread": f"thread-{name}",
                    "org": seed.org,
                    "owner": owner,
                    "created_at": created_at,
                },
            )

        def add_event(pipeline: str, created_at: datetime, old: uuid.UUID | None, new: uuid.UUID | None) -> None:
            conn.execute(
                sa.text(
                    "INSERT INTO audit_events (id, organisation_id, event_type, account_id, resource_type, "
                    "resource_id, payload_json, created_at) VALUES "
                    "(:id, :org, 'resource_team_ownership_changed', :acct, 'pipeline', :rid, "
                    "jsonb_build_object('resource_type', 'pipeline', 'resource_id', CAST(:rid_text AS text), "
                    "'old_team_id', CAST(:old AS text), 'new_team_id', CAST(:new AS text)), :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "org": seed.org,
                    "acct": seed.account,
                    "rid": p[pipeline],
                    "rid_text": str(p[pipeline]),
                    "old": None if old is None else str(old),
                    "new": None if new is None else str(new),
                    "created_at": created_at,
                },
            )

        add_run("never_moved", "never_moved", now - timedelta(hours=4))
        add_run("before_move", "moved", now - timedelta(hours=3))
        add_run("after_move", "moved", now - timedelta(minutes=10))
        add_run("between_moves", "moved_from_org", now - timedelta(hours=1))
        add_run("broken", "broken", now - timedelta(hours=2))
        add_run("org_then_team", "org_then_team", now - timedelta(hours=3))
        add_run("already_stamped", "never_moved", now - timedelta(hours=5), owner=seed.team_b)
        add_run("org_level", "org_level", now - timedelta(hours=1))
        add_run("before_move_to_org", "moved_to_org", now - timedelta(hours=3))
        add_run("broken_org_level", "broken_org_level", now - timedelta(hours=2))

        add_event("moved", now - timedelta(hours=1), seed.team_a, seed.team_b)
        add_event("moved_from_org", now - timedelta(hours=2), None, seed.team_a)
        add_event("moved_from_org", now - timedelta(minutes=30), seed.team_a, seed.team_b)
        add_event("broken", now - timedelta(hours=1), seed.team_a, None)
        add_event("org_then_team", now - timedelta(hours=1), None, seed.team_a)
        # moved_to_org: team A -> org level one hour ago (a run before it stays A).
        add_event("moved_to_org", now - timedelta(hours=1), seed.team_a, None)
        # broken_org_level: the chain ends on team B but the pipeline is org level
        # now (a move that wrote no event), so nothing can be trusted.
        add_event("broken_org_level", now - timedelta(hours=1), seed.team_a, seed.team_b)

        # Children of a never-moved pipeline's run: all must inherit team A.
        run_id = seed.runs["never_moved"]
        conn.execute(
            sa.text("INSERT INTO run_attempts (id, organisation_id, run_id, attempt_index) VALUES (:id, :org, :run, 0)"),
            {"id": uuid.uuid4(), "org": seed.org, "run": run_id},
        )
        conn.execute(
            sa.text("INSERT INTO run_node_outputs (run_id, node_id, attempt_key, organisation_id) VALUES (:run, 'node-1', '__final__', :org)"),
            {"run": run_id, "org": seed.org},
        )
        conn.execute(
            sa.text("INSERT INTO run_evidence (run_id, node_id, organisation_id, evidence_state) VALUES (:run, :node, :org, 'has_work')"),
            {"run": run_id, "node": uuid.uuid4(), "org": seed.org},
        )
        conn.execute(
            sa.text("INSERT INTO node_observations (id, organisation_id, run_id, node_id, human_observed_at) VALUES (:id, :org, :run, :node, CURRENT_TIMESTAMP)"),
            {"id": uuid.uuid4(), "org": seed.org, "run": run_id, "node": uuid.uuid4()},
        )
        conn.execute(
            sa.text("INSERT INTO evidence (id, organisation_id, key, subject_type, subject_id, value, producer_type) VALUES (:id, :org, 'eval_guardrail_demo', 'run', :subject, 'true'::jsonb, 'eval')"),
            {"id": uuid.uuid4(), "org": seed.org, "subject": str(run_id)},
        )

        # A suite run for the moved pipeline, created BEFORE the move: resolves to A.
        conn.execute(
            sa.text("INSERT INTO eval_datasets (id, organisation_id, name) VALUES (:id, :org, 'Owner dataset')"),
            {"id": seed.dataset, "org": seed.org},
        )
        conn.execute(
            sa.text("INSERT INTO eval_suites (id, organisation_id, name) VALUES (:id, :org, 'Owner suite')"),
            {"id": seed.suite, "org": seed.org},
        )
        conn.execute(
            sa.text("INSERT INTO model_backends (id, name, display_name, provider, model_id, credentials_ciphertext, account_id, organisation_id, default_params) VALUES (:id, 'owner-backend', 'Owner backend', 'openai', 'gpt-4o-mini', :cipher, :acct, :org, '{}'::jsonb)"),
            {"id": seed.backend, "cipher": b"\x00", "acct": seed.account, "org": seed.org},
        )
        conn.execute(
            sa.text("INSERT INTO suite_runs (id, organisation_id, suite_id, dataset_id, model_backend_id, definition_checksum, state, extra, created_at) VALUES (:id, :org, :suite, :dataset, :backend, 'checksum', 'completed', jsonb_build_object('pipeline_id', CAST(:pid AS text)), :created_at)"),
            {
                "id": seed.suite_run,
                "org": seed.org,
                "suite": seed.suite,
                "dataset": seed.dataset,
                "backend": seed.backend,
                "pid": str(p["moved"]),
                "created_at": now - timedelta(hours=3),
            },
        )
    return seed


def test_0365_backfill_resolves_owner_at_created_at_and_marks_legacy(pre_change_db) -> None:
    """Three passes, the legacy marker, child inheritance and the 1B precondition."""
    sync_url, _config = pre_change_db
    engine = sa.create_engine(sync_url)
    try:
        seed = _seed(engine)
        # The columns do not exist before the change.
        assert "owner_team_id" not in _column_names(engine, "run_attempts")
        assert "owner_unresolved" not in _column_names(engine, "runs")

        command.upgrade(_alembic_config(sync_url), _HEAD)
        engine.dispose()
        engine = sa.create_engine(sync_url)

        for table in _CHILD_TABLES:
            assert "owner_team_id" in _column_names(engine, table), table

        # pass 1: an already-stamped run is never rewritten
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["already_stamped"]) == seed.team_b
        # pass 2: the pipeline never moved -> the pipeline's owner
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["never_moved"]) == seed.team_a
        # pass 3: resolved at created_at, NOT from the live pipeline
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["before_move"]) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["after_move"]) == seed.team_b
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["between_moves"]) == seed.team_a
        # genuinely org-level pipeline: unowned and NOT legacy
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["org_level"]) is None
        assert _scalar(engine, "SELECT owner_unresolved FROM runs WHERE id = :r", r=seed.runs["org_level"]) is None
        # a pipeline moved TO org-level still resolves its historical run to the
        # team that owned it then (the live pipeline is NULL - proof the answer is
        # not read from it)
        assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs["before_move_to_org"]) == seed.team_a
        assert _scalar(engine, "SELECT owner_unresolved FROM runs WHERE id = :r", r=seed.runs["before_move_to_org"]) is None
        # untrustworthy history and "no team at created_at" are both legacy-unowned,
        # including a BROKEN chain on a pipeline that is org-level now (fail closed:
        # an unreadable history must not publish a team's runs org-wide)
        for name in ("broken", "org_then_team", "broken_org_level"):
            assert _scalar(engine, "SELECT owner_team_id FROM runs WHERE id = :r", r=seed.runs[name]) is None, name
            assert _scalar(engine, "SELECT owner_unresolved FROM runs WHERE id = :r", r=seed.runs[name]) is True, name

        # suite_runs: the same three-pass rule, pipeline id read from ``extra``
        assert _scalar(engine, "SELECT owner_team_id FROM suite_runs WHERE id = :r", r=seed.suite_run) == seed.team_a

        # children inherit the resolved parent run (never the live pipeline)
        run_id = seed.runs["never_moved"]
        assert _scalar(engine, "SELECT owner_team_id FROM run_attempts WHERE run_id = :r", r=run_id) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM run_node_outputs WHERE run_id = :r", r=run_id) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM run_evidence WHERE run_id = :r", r=run_id) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM node_observations WHERE run_id = :r", r=run_id) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM evidence WHERE subject_id = :r", r=str(run_id)) == seed.team_a

        # the Phase 1B gate
        assert _scalar(engine, UNOWNED_PRECONDITION_SQL) == 0
    finally:
        engine.dispose()


def test_0365_downgrade_removes_every_added_column(pre_change_db) -> None:
    """The downgrade restores the pre-0365 shape (owner data is lost by design)."""
    sync_url, config = pre_change_db
    engine = sa.create_engine(sync_url)
    try:
        _seed(engine)
        command.upgrade(config, _HEAD)
        command.downgrade(config, _PRE_CHANGE)
        engine.dispose()
        engine = sa.create_engine(sync_url)
        for table in _CHILD_TABLES:
            assert "owner_team_id" not in _column_names(engine, table), table
        for table in ("runs", "suite_runs"):
            assert "owner_unresolved" not in _column_names(engine, table), table
    finally:
        engine.dispose()


def test_0366_onboarding_progress_policy_fails_closed(head_db) -> None:
    """Unset org context returns ZERO rows instead of raising; the downgrade
    restores the raising shape and an org-scoped read works in both."""
    sync_url, config = head_db
    engine = sa.create_engine(sync_url)
    app_engine = sa.create_engine(_with_credentials(sync_url, "farna_control_app", "apppass"))
    try:
        org_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, 'RLS Org', 'rls-org', '{}'::jsonb)"), {"id": org_id})
            conn.execute(
                sa.text("INSERT INTO onboarding_progress (id, organisation_id) VALUES (:id, :org)"),
                {"id": uuid.uuid4(), "org": org_id},
            )

        # No app.organisation_id bound: the fixed policy yields zero rows.
        with app_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM onboarding_progress")).scalar_one() == 0
            conn.execute(sa.text("SELECT set_config('app.organisation_id', :org, true)"), {"org": str(org_id)})
            assert conn.execute(sa.text("SELECT count(*) FROM onboarding_progress")).scalar_one() == 1

        # The pre-0366 policy RAISES on the same no-context query - the fix is real.
        command.downgrade(config, "0365_runs_family_owner_team")
        app_engine.dispose()
        app_engine = sa.create_engine(_with_credentials(sync_url, "farna_control_app", "apppass"))
        with app_engine.connect() as conn, pytest.raises(sa.exc.DBAPIError):
            conn.execute(sa.text("SELECT count(*) FROM onboarding_progress")).scalar_one()
    finally:
        app_engine.dispose()
        engine.dispose()
