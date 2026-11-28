"""ADR 070 Phase 1A - pipeline-family ownership on real Postgres (migration 0369).

Proves, against a real database and the REAL Alembic chain:

* **0369 upgrade** adds a nullable, indexed ``owner_team_id`` (FK -> ``teams``,
  ``ON DELETE RESTRICT``) to ``triggers`` and ``pipeline_edges`` - and only those
  two tables;
* the **backfill** copies the parent pipeline's CURRENT owner, leaves an
  org-level pipeline's children unowned, and never rewrites an already-stamped
  row;
* the **Phase 1B data precondition** holds: zero rows where
  ``owner_team_id IS NULL`` AND the pipeline is team-owned;
* that precondition is NOT vacuous - seeding one unowned row under a team-owned
  pipeline makes it fail, and removing the row makes it pass again;
* **0369 downgrade** removes every column it added (data loss by design).

The fixture builds its own container and applies the chain up to 0368, i.e. the
state a real install is in immediately before this change.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]

#: The revision immediately before this change - the state a real install is in.
_PRE_CHANGE = "0368_invitations_quarantine_rls"
_HEAD = "0369_pipeline_family_owner_team"

_CHILD_TABLES = ("pipeline_edges", "triggers")

#: Deliberately NOT stamped (see the migration docstring).
_UNTESTED_BY_DESIGN = ("pipeline_folders", "pipeline_snapshots", "snapshot_schema_pins")

#: The Phase 1B gate, per child table. Zero rows == the flip is safe.
UNOWNED_PRECONDITION_SQL_TEMPLATE = """
SELECT count(*)
FROM {table} AS c
JOIN pipelines AS p ON p.id = c.pipeline_id
WHERE c.owner_team_id IS NULL
  AND p.owner_team_id IS NOT NULL
"""


def _backfill_sql() -> dict[str, str]:
    """The migration's OWN backfill statements, keyed by table.

    Loaded from the shipped file (not re-typed here) so the idempotence proof
    exercises the exact SQL the upgrade ran.
    """
    path = BACKEND_ROOT / "src" / "farna" / "db" / "migrations" / "versions" / "0369_pipeline_family_owner_team.py"
    spec = importlib.util.spec_from_file_location("migration_0369_pipeline_family_owner_team", path)
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {table: module.BACKFILL_CHILD_SQL_TEMPLATE.format(table=table) for table in module.CHILD_TABLES}


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
    """A real Postgres migrated to 0368 (before this change).

    Yields the superuser sync URL and the alembic config.
    """
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


def _column_names(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            row[0]
            for row in conn.execute(
                sa.text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
                {"t": table},
            )
        }


def _index_names(engine: sa.Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(sa.text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = :t"), {"t": table})}


def _fk_delete_action(engine: sa.Engine, constraint: str) -> str | None:
    with engine.connect() as conn:
        # pg_constraint.confdeltype: 'r' = RESTRICT, 'c' = CASCADE, 'a' = NO ACTION.
        return conn.execute(sa.text("SELECT confdeltype FROM pg_constraint WHERE conname = :c"), {"c": constraint}).scalar()


def _scalar(engine: sa.Engine, sql: str, **params: object) -> object:
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).scalar()


class Seed:
    """Ids of the seeded fixture rows."""

    def __init__(self) -> None:
        self.org = uuid.uuid4()
        self.account = uuid.uuid4()
        self.team_a = uuid.uuid4()
        self.team_b = uuid.uuid4()
        self.team_pipeline = uuid.uuid4()
        self.org_pipeline = uuid.uuid4()
        self.trigger_team = uuid.uuid4()
        self.trigger_org = uuid.uuid4()
        self.edge_team = uuid.uuid4()
        self.edge_org = uuid.uuid4()


def _insert_trigger(conn: sa.Connection, *, org: uuid.UUID, account: uuid.UUID, pipeline: uuid.UUID, name: str) -> uuid.UUID:
    trigger_id = uuid.uuid4()
    conn.execute(
        sa.text("INSERT INTO triggers (id, organisation_id, pipeline_id, name, trigger_type, config_json, account_id) VALUES (:id, :org, :pid, :name, 'manual', '{}'::jsonb, :acct)"),
        {"id": trigger_id, "org": org, "pid": pipeline, "name": name, "acct": account},
    )
    return trigger_id


def _insert_edge(conn: sa.Connection, *, org: uuid.UUID, pipeline: uuid.UUID) -> uuid.UUID:
    edge_id = uuid.uuid4()
    conn.execute(
        sa.text("INSERT INTO pipeline_edges (id, organisation_id, pipeline_id, source_node_id, target_node_id, edge_type, source_port, target_port) VALUES (:id, :org, :pid, :src, :dst, 'normal', 'out', 'in')"),
        {"id": edge_id, "org": org, "pid": pipeline, "src": uuid.uuid4(), "dst": uuid.uuid4()},
    )
    return edge_id


def _seed(engine: sa.Engine) -> Seed:
    """One team-owned pipeline and one org-level pipeline, each with children."""
    seed = Seed()
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
        conn.execute(
            sa.text("INSERT INTO pipelines (id, name, account_id, organisation_id, owner_team_id, visibility) VALUES (:id, 'team-owned', :acct, :org, :owner, 'team')"),
            {"id": seed.team_pipeline, "acct": seed.account, "org": seed.org, "owner": seed.team_a},
        )
        conn.execute(
            sa.text("INSERT INTO pipelines (id, name, account_id, organisation_id, owner_team_id, visibility) VALUES (:id, 'org-level', :acct, :org, NULL, 'org')"),
            {"id": seed.org_pipeline, "acct": seed.account, "org": seed.org},
        )

        seed.trigger_team = _insert_trigger(conn, org=seed.org, account=seed.account, pipeline=seed.team_pipeline, name="team-trigger")
        seed.trigger_org = _insert_trigger(conn, org=seed.org, account=seed.account, pipeline=seed.org_pipeline, name="org-trigger")
        seed.edge_team = _insert_edge(conn, org=seed.org, pipeline=seed.team_pipeline)
        seed.edge_org = _insert_edge(conn, org=seed.org, pipeline=seed.org_pipeline)
    return seed


def test_0369_upgrade_adds_columns_backfills_and_satisfies_the_precondition(pre_change_db) -> None:
    sync_url, config = pre_change_db
    engine = sa.create_engine(sync_url)
    try:
        seed = _seed(engine)
        # The columns do not exist before the change on the child set, and never
        # exist on the deferred tables.
        for table in _CHILD_TABLES:
            assert "owner_team_id" not in _column_names(engine, table), table
        for table in _UNTESTED_BY_DESIGN:
            assert "owner_team_id" not in _column_names(engine, table), table

        command.upgrade(config, _HEAD)
        engine.dispose()
        engine = sa.create_engine(sync_url)

        for table in _CHILD_TABLES:
            assert "owner_team_id" in _column_names(engine, table), table
            assert f"ix_{table}_owner_team_id" in _index_names(engine, table), table
            assert _fk_delete_action(engine, f"{table}_owner_team_id_fkey") == "r", table
        # The scope decision is enforced: the deferred tables did not gain a column.
        for table in _UNTESTED_BY_DESIGN:
            assert "owner_team_id" not in _column_names(engine, table), table

        # The backfill: the pipeline's CURRENT owner, org-level stays NULL.
        assert _scalar(engine, "SELECT owner_team_id FROM triggers WHERE id = :r", r=seed.trigger_team) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM triggers WHERE id = :r", r=seed.trigger_org) is None
        assert _scalar(engine, "SELECT owner_team_id FROM pipeline_edges WHERE id = :r", r=seed.edge_team) == seed.team_a
        assert _scalar(engine, "SELECT owner_team_id FROM pipeline_edges WHERE id = :r", r=seed.edge_org) is None

        # Idempotence / pass-1: a re-run of the backfill rewrites nothing, so an
        # already-stamped row (here forced to team B) keeps its value.
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE triggers SET owner_team_id = :team WHERE id = :id"), {"team": seed.team_b, "id": seed.trigger_team})
        with engine.begin() as conn:
            conn.execute(sa.text(_backfill_sql()["triggers"]))
        assert _scalar(engine, "SELECT owner_team_id FROM triggers WHERE id = :r", r=seed.trigger_team) == seed.team_b

        # The Phase 1B gate.
        for table in _CHILD_TABLES:
            assert _scalar(engine, UNOWNED_PRECONDITION_SQL_TEMPLATE.format(table=table)) == 0, table
    finally:
        engine.dispose()


def test_the_precondition_is_not_vacuous(pre_change_db) -> None:
    """Seed one unowned row under a team-owned pipeline: the gate must fail.

    The row is then removed, so this test leaves the container as it found it and
    the passing assertion is demonstrably about the data, not about an empty
    table set.
    """
    sync_url, config = pre_change_db
    engine = sa.create_engine(sync_url)
    try:
        seed = _seed(engine)
        command.upgrade(config, _HEAD)
        engine.dispose()
        engine = sa.create_engine(sync_url)

        for table in _CHILD_TABLES:
            assert _scalar(engine, UNOWNED_PRECONDITION_SQL_TEMPLATE.format(table=table)) == 0, table

        with engine.begin() as conn:
            offender = _insert_trigger(conn, org=seed.org, account=seed.account, pipeline=seed.team_pipeline, name="unowned-after-backfill")
        assert _scalar(engine, UNOWNED_PRECONDITION_SQL_TEMPLATE.format(table="triggers")) == 1

        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM triggers WHERE id = :id"), {"id": offender})
        assert _scalar(engine, UNOWNED_PRECONDITION_SQL_TEMPLATE.format(table="triggers")) == 0
    finally:
        engine.dispose()


def test_0369_downgrade_removes_every_added_column(pre_change_db) -> None:
    """The downgrade restores the pre-0369 shape (stamped owners are lost)."""
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
            assert f"ix_{table}_owner_team_id" not in _index_names(engine, table), table
            assert _fk_delete_action(engine, f"{table}_owner_team_id_fkey") is None, table
    finally:
        engine.dispose()
