"""The 0362 migration renames the last legacy-prefixed database objects (FAR-2652).

The product is Farna Control, but the shipped baseline (0350/0351) still
materialises one legacy-prefixed schema and one legacy-prefixed table with its
primary key, unique constraint, foreign key and two indexes. Migration 0362
renames them so an upgraded install converges on the shape a fresh install
would materialise.

These tests EXECUTE the migration's own statements against a real Postgres 16,
because the renames are Postgres-only DDL (``ALTER SCHEMA``, ``DO`` blocks over
``pg_constraint``/``to_regclass``) that SQLite cannot run. A text-only
assertion could not prove the three properties that matter:

* every legacy object is renamed and **no legacy-prefixed object survives** —
  in particular the primary-key and unique-constraint backing indexes, which
  PostgreSQL renames together with their constraint;
* row-level security (enabled by the baseline) SURVIVES the rename, so the
  tenant boundary is never lost;
* a re-run is a no-op, and the downgrade restores the exact legacy names,
  so the migration is safe in both directions.

The container is capability-gated exactly like
``tests/unit/db/test_housekeeping_scan_savepoint_isolation.py``: an
env-provisioned URL is used when present, otherwise testcontainers, otherwise
the real-DB tests skip (never silently pass). Object names come from the
migration's own constants, so this file never restates them.
"""

from __future__ import annotations

import importlib.util
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0362_rename_legacy_db_objects.py"
_PG_URL_ENV = "FARNA_2652_TEST_PG_URL"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0362_rename_legacy_db_objects", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_url(raw: str) -> str:
    """Force the psycopg (v3) sync driver — psycopg2 is not in the tree."""
    return re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+psycopg://", raw, flags=re.IGNORECASE)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Throwaway Postgres URL: env-provisioned, else testcontainers, else skip."""
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        yield _sync_url(env_url)
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (no Docker): {exc}")
    try:
        yield _sync_url(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture
def engine(pg_url: str) -> Iterator[sa.Engine]:
    """A fresh database per test — the migration hard-codes the ``public`` schema."""
    base_url = make_url(pg_url)
    admin = sa.create_engine(base_url, isolation_level="AUTOCOMMIT")
    database = f"m0362_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    test_engine = sa.create_engine(base_url.set(database=database))
    try:
        yield test_engine
    finally:
        test_engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.dispose()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _regclass(table: str) -> str:
    """The ``public."<table>"`` text ``to_regclass`` resolves; bound as a parameter."""
    return "public." + _quote(table)


def _relation_exists(conn: sa.Connection, table: str) -> bool:
    return conn.exec_driver_sql("SELECT to_regclass(%s)", (_regclass(table),)).scalar() is not None


def _schema_exists(conn: sa.Connection, schema: str) -> bool:
    return conn.exec_driver_sql("SELECT count(*) FROM pg_namespace WHERE nspname = %s", (schema,)).scalar() == 1


def _snapshot(conn: sa.Connection, table: str) -> dict[str, object]:
    """The renameable/invariant state of one table, as PostgreSQL reports it."""
    target = _regclass(table)
    rel = conn.exec_driver_sql("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = to_regclass(%s)", (target,)).one()
    indexes = tuple(
        conn.exec_driver_sql(
            "SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid WHERE i.indrelid = to_regclass(%s) ORDER BY c.relname",
            (target,),
        )
        .scalars()
        .all()
    )
    constraints = tuple(conn.exec_driver_sql("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass(%s) ORDER BY conname", (target,)).scalars().all())
    policies = tuple(conn.exec_driver_sql("SELECT polname FROM pg_policy WHERE polrelid = to_regclass(%s) ORDER BY polname", (target,)).scalars().all())
    return {"rls": bool(rel[0]), "force_rls": bool(rel[1]), "indexes": indexes, "constraints": constraints, "policies": policies}


def _legacy_prefixed(conn: sa.Connection, prefix: str) -> list[str]:
    """Every relation, constraint or schema whose name carries ``prefix``."""
    relations = conn.exec_driver_sql("SELECT relname FROM pg_class WHERE relname LIKE %s ORDER BY relname", (prefix + "%",)).scalars().all()
    constraints = conn.exec_driver_sql("SELECT conname FROM pg_constraint WHERE conname LIKE %s ORDER BY conname", (prefix + "%",)).scalars().all()
    schemas = conn.exec_driver_sql("SELECT nspname FROM pg_namespace WHERE nspname LIKE %s ORDER BY nspname", (prefix + "%",)).scalars().all()
    return [*relations, *constraints, *schemas]


def _indexes_for(pairs: tuple[tuple[str, str], ...], *, legacy: bool) -> tuple[str, ...]:
    return tuple(old if legacy else new for old, new in pairs)


def _constraint_names(module: ModuleType, *, legacy: bool) -> tuple[str, str, str]:
    """Return (primary key, unique, foreign key) names as a triple."""
    names = [old if legacy else new for old, new in module._CONSTRAINTS]
    pkey = next(name for name in names if name.endswith("_pkey"))
    fkey = next(name for name in names if name.endswith("_fkey"))
    unique = next(name for name in names if name not in {pkey, fkey})
    return pkey, unique, fkey


def _create_state(engine: sa.Engine, module: ModuleType, *, legacy: bool) -> None:
    """Materialise the table with either its legacy or its final names."""
    schema = module._OLD_SCHEMA if legacy else module._NEW_SCHEMA
    table = module._OLD_TABLE if legacy else module._NEW_TABLE
    pkey, unique, fkey = _constraint_names(module, legacy=legacy)
    quoted_table = _quote(table)

    statements = [
        f"CREATE SCHEMA {_quote(schema)}",
        "CREATE TABLE public.organisations (id uuid PRIMARY KEY)",
        (
            f"CREATE TABLE public.{quoted_table} ("
            "  id uuid NOT NULL,"
            "  organisation_id uuid NOT NULL,"
            "  run_id uuid NOT NULL,"
            "  writer varchar(30) NOT NULL,"
            "  created_at timestamptz NOT NULL DEFAULT now(),"
            f"  CONSTRAINT {_quote(pkey)} PRIMARY KEY (id),"
            f"  CONSTRAINT {_quote(unique)} UNIQUE (run_id, writer),"
            f"  CONSTRAINT {_quote(fkey)} FOREIGN KEY (organisation_id) REFERENCES public.organisations (id) ON DELETE CASCADE"
            ")"
        ),
    ]
    for index in _indexes_for(module._INDEXES, legacy=legacy):
        # Column shape is irrelevant to a rename; the two prefixes are enough.
        columns = "organisation_id, created_at" if index.endswith("org_created") else "organisation_id"
        statements.append(f"CREATE INDEX {_quote(index)} ON public.{quoted_table} ({columns})")
    statements.extend(
        [
            f"ALTER TABLE public.{quoted_table} ENABLE ROW LEVEL SECURITY",
            (f"CREATE POLICY rls_org_isolation ON public.{quoted_table} USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid))"),
        ]
    )

    with engine.begin() as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)


def _run(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, module: ModuleType, direction: str) -> None:
    """Execute ``module.upgrade``/``downgrade`` against the real database.

    ``op.execute`` wraps a string in ``sa.text`` and executes it; the same path
    is used here so the ``format('%I')`` inside the guard blocks is treated as
    SQL, not as a driver placeholder.
    """
    with engine.begin() as conn:

        def _execute(sql: str) -> None:
            conn.execute(sa.text(sql))

        monkeypatch.setattr(op, "execute", _execute)
        getattr(module, direction)()


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0362_rename_legacy_db_objects"
    assert module.down_revision == "0360_free_schema_history_retire_pipeline_delete"


def test_upgrade_renames_every_legacy_object(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """No legacy-prefixed object survives, and RLS survives the table rename."""
    module = _load_migration()
    _create_state(engine, module, legacy=True)

    _run(engine, monkeypatch, module, "upgrade")

    with engine.connect() as conn:
        assert _relation_exists(conn, module._NEW_TABLE)
        assert not _relation_exists(conn, module._OLD_TABLE)
        assert _schema_exists(conn, module._NEW_SCHEMA)
        assert not _schema_exists(conn, module._OLD_SCHEMA)
        survivors = _legacy_prefixed(conn, module._OLD_TABLE)
        assert not survivors, f"legacy-prefixed objects survived the rename: {survivors}"

        snapshot = _snapshot(conn, module._NEW_TABLE)
        pkey, unique, fkey = _constraint_names(module, legacy=False)
        assert set(snapshot["constraints"]) == {pkey, unique, fkey}
        # The pkey/unique backing indexes are renamed WITH the constraint, so
        # the expected index set includes them alongside the two plain indexes.
        assert set(snapshot["indexes"]) == {*_indexes_for(module._INDEXES, legacy=False), pkey, unique}
        # Row-level security survives: ENABLE, the policy, and the tenant guard.
        assert snapshot["rls"] is True
        assert "rls_org_isolation" in snapshot["policies"]


def test_upgrade_is_idempotent(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second upgrade run must not raise and must change nothing."""
    module = _load_migration()
    _create_state(engine, module, legacy=True)

    _run(engine, monkeypatch, module, "upgrade")
    with engine.connect() as conn:
        first_pass = _snapshot(conn, module._NEW_TABLE)

    _run(engine, monkeypatch, module, "upgrade")
    with engine.connect() as conn:
        second_pass = _snapshot(conn, module._NEW_TABLE)

    assert second_pass == first_pass


def test_upgrade_on_already_new_shape_is_a_noop(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A database already at the final shape (no legacy objects) is untouched."""
    module = _load_migration()
    _create_state(engine, module, legacy=False)

    with engine.connect() as conn:
        before = _snapshot(conn, module._NEW_TABLE)

    _run(engine, monkeypatch, module, "upgrade")

    with engine.connect() as conn:
        after = _snapshot(conn, module._NEW_TABLE)

    assert after == before


def test_downgrade_restores_the_legacy_names(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The downgrade is the exact mirror: every legacy name comes back, and no
    final-name object survives."""
    module = _load_migration()
    _create_state(engine, module, legacy=True)

    _run(engine, monkeypatch, module, "upgrade")
    _run(engine, monkeypatch, module, "downgrade")

    with engine.connect() as conn:
        assert _relation_exists(conn, module._OLD_TABLE)
        assert not _relation_exists(conn, module._NEW_TABLE)
        assert _schema_exists(conn, module._OLD_SCHEMA)
        assert not _schema_exists(conn, module._NEW_SCHEMA)
        survivors = _legacy_prefixed(conn, module._NEW_TABLE)
        assert not survivors, f"final-name objects survived the downgrade: {survivors}"

        snapshot = _snapshot(conn, module._OLD_TABLE)
        pkey, unique, fkey = _constraint_names(module, legacy=True)
        assert set(snapshot["constraints"]) == {pkey, unique, fkey}
        assert set(snapshot["indexes"]) == {*_indexes_for(module._INDEXES, legacy=True), pkey, unique}
        assert snapshot["rls"] is True
        assert "rls_org_isolation" in snapshot["policies"]
