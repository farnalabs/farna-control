"""The 0363 migration rewrites the last persisted legacy values (FAR-2652).

0362 renamed the legacy database *objects*. The values below live inside rows,
so 0363 widens each CHECK vocabulary, rewrites the rows, then narrows the
vocabulary again; it also renames the sentinel organisation's display name.

These tests EXECUTE the migration's own statements against a real PostgreSQL,
because the vocabulary change is CHECK-constraint DDL (``DROP CONSTRAINT`` /
``ADD CONSTRAINT``) that SQLite cannot run, and because the ``lifecycle_maps``
step is a JSONB array rewrite. A text-only assertion could not prove:

* every ``'modulo'`` row is rewritten and every unrelated row (and its count)
  is preserved;
* after the upgrade the CHECK genuinely REJECTS ``'modulo'`` — the whole point
  of the narrow step;
* the ``content_json`` stage types are rewritten without touching the rest of
  the document;
* a re-run is a no-op, and the downgrade restores the legacy values.

The container is capability-gated exactly like
``tests/unit/db/test_migration_0362_rename_legacy_db_objects.py``: an
env-provisioned URL is used when present, otherwise testcontainers, otherwise
the real-DB tests skip (never silently pass).
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

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0363_rewrite_legacy_persisted_values.py"
_PG_URL_ENV = "FARNA_2652_TEST_PG_URL"

#: A second organisation that also carries the retired display name. The rename
#: is keyed on the sentinel id, so this row must be left alone.
_OTHER_ORG_ID = "11111111-1111-1111-1111-111111111111"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0363_rewrite_legacy_persisted_values", _MIGRATION_PATH)
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
    """Throwaway PostgreSQL URL: env-provisioned, else testcontainers, else skip."""
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
    database = f"m0363_{uuid.uuid4().hex[:12]}"
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


#: The pre-migration schema, restricted to the columns the two CHECK
#: constraints and the rewrite touch. The legacy vocabulary/branches match
#: baseline 0350 exactly.
_LEGACY_DDL: tuple[str, ...] = (
    "CREATE TABLE public.organisations (id uuid PRIMARY KEY, name varchar(255) NOT NULL, slug varchar(255) NOT NULL)",
    """
    CREATE TABLE public.library_primitives (
        id uuid PRIMARY KEY,
        source varchar(20) NOT NULL,
        source_url varchar(2000),
        checksum varchar(128),
        ed25519_signature varchar(256),
        verified boolean,
        download_count integer,
        average_rating numeric,
        review_count integer,
        owner_team_id uuid,
        visibility varchar(20) NOT NULL,
        forked_from uuid,
        CONSTRAINT ck_library_primitives_source
            CHECK (source IN ('local', 'registry', 'modulo', 'community')),
        CONSTRAINT ck_library_primitives_source_fields CHECK (
            (source = 'local' AND source_url IS NULL AND checksum IS NULL
                AND ed25519_signature IS NULL AND verified IS NULL
                AND download_count IS NULL AND average_rating IS NULL
                AND review_count IS NULL)
            OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL
                AND ed25519_signature IS NULL AND verified IS NULL
                AND download_count IS NULL AND average_rating IS NULL
                AND review_count IS NULL)
            OR (source = 'community' AND source_url IS NULL AND checksum IS NULL
                AND ed25519_signature IS NULL
                AND download_count IS NULL AND average_rating IS NULL
                AND review_count IS NULL)
            OR (source = 'registry' AND owner_team_id IS NULL
                AND visibility = 'org' AND forked_from IS NULL
                AND source_url IS NOT NULL AND checksum IS NOT NULL
                AND verified IS NOT NULL AND download_count IS NOT NULL)
        )
    )
    """,
    """
    CREATE TABLE public.lifecycle_maps (
        id uuid PRIMARY KEY,
        content_json jsonb
    )
    """,
    """
    CREATE TABLE public.lifecycle_map_stages (
        id uuid PRIMARY KEY,
        stage_type varchar(20) NOT NULL,
        CONSTRAINT ck_lifecycle_map_stages_type
            CHECK (stage_type IN ('modulo', 'external', 'manual', 'placeholder'))
    )
    """,
)

#: One row per branch of the source vocabulary, plus a registry row to prove the
#: provenance-shape branch keeps working after the rewrite.
_SOURCE_ROWS: tuple[tuple[str, dict[str, object]], ...] = (
    ("local", {}),
    ("modulo", {}),
    ("modulo", {}),
    ("community", {}),
    ("registry", {"source_url": "https://example.test/p.json", "checksum": "abc", "verified": True, "download_count": 3, "visibility": "org"}),
)

_MODULO_STAGES = ("stage-a", "stage-b")


def _exec(conn: sa.Connection, sql: str, params: dict[str, object] | None = None) -> sa.CursorResult[object]:
    """Run ``sql`` with ``:name`` parameters translated by SQLAlchemy.

    ``exec_driver_sql`` would pass ``:name`` through untouched, and psycopg's
    paramstyle is ``pyformat``; ``Conn.execute(text(...))`` is the translating path.
    """
    return conn.execute(sa.text(sql), params or {})


def _create_legacy_state(engine: sa.Engine, module: ModuleType) -> None:
    with engine.begin() as conn:
        for statement in _LEGACY_DDL:
            conn.exec_driver_sql(statement)
        _exec(
            conn,
            "INSERT INTO public.organisations (id, name, slug) VALUES (CAST(:sentinel AS uuid), :name, :slug), (CAST(:other AS uuid), :name, :other_slug)",
            {"sentinel": module._SENTINEL_ORG_ID, "other": _OTHER_ORG_ID, "name": module._SENTINEL_NAME_BEFORE, "slug": "sentinel", "other_slug": "other"},
        )
        for index, (source, overrides) in enumerate(_SOURCE_ROWS):
            values: dict[str, object] = {
                "id": str(uuid.uuid4()),
                "source": source,
                "visibility": "community",
                "source_url": None,
                "checksum": None,
                "verified": None,
                "download_count": None,
            }
            values.update(overrides)
            _exec(
                conn,
                "INSERT INTO public.library_primitives (id, source, visibility, source_url, checksum, verified, download_count) VALUES (CAST(:id AS uuid), :source, :visibility, :source_url, :checksum, :verified, :download_count)",
                values,
            )
        for stage_id in (*_MODULO_STAGES, "stage-external"):
            stage_type = "external" if stage_id == "stage-external" else module._LEGACY
            _exec(
                conn,
                "INSERT INTO public.lifecycle_map_stages (id, stage_type) VALUES (CAST(:id AS uuid), :stage_type)",
                {"id": str(uuid.uuid4()), "stage_type": stage_type},
            )
        _exec(
            conn,
            "INSERT INTO public.lifecycle_maps (id, content_json) VALUES (CAST(:mixed AS uuid), CAST(:mixed_json AS jsonb)), (CAST(:plain AS uuid), CAST(:plain_json AS jsonb))",
            {
                "mixed": str(uuid.uuid4()),
                "plain": str(uuid.uuid4()),
                "mixed_json": '{"stages": [{"id": "s1", "name": "Build", "type": "modulo", "pipeline_id": "p1"}, {"id": "s2", "name": "Deploy", "type": "external"}], "edges": [{"id": "e1", "source": "s1", "target": "s2"}], "notes": "keep me"}',
                "plain_json": '{"stages": [{"id": "s3", "name": "Review", "type": "manual"}], "edges": [], "notes": ""}',
            },
        )


def _run(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, module: ModuleType, direction: str) -> None:
    """Execute ``module.upgrade``/``downgrade`` against the real database.

    ``op.execute`` runs a string through ``sa.text`` or executes a ``TextClause``
    as-is; both shapes are supported here so the test runs exactly what
    production runs.
    """
    with engine.begin() as conn:

        def _execute(sql: object) -> None:
            conn.execute(sa.text(sql) if isinstance(sql, str) else sql)  # type: ignore[arg-type]

        monkeypatch.setattr(op, "execute", _execute)
        getattr(module, direction)()


def _source_counts(conn: sa.Connection) -> dict[str, int]:
    rows = conn.exec_driver_sql("SELECT source, count(*) FROM public.library_primitives GROUP BY source").all()
    return {source: int(count) for source, count in rows}


def _table_counts(conn: sa.Connection) -> dict[str, int]:
    return {
        table: int(conn.exec_driver_sql(f"SELECT count(*) FROM public.{table}").scalar_one())  # noqa: S608 - fixed table names
        for table in ("organisations", "library_primitives", "lifecycle_maps", "lifecycle_map_stages")
    }


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0363_rewrite_legacy_persisted_values"
    assert module.down_revision == "0362_rename_legacy_db_objects"


def test_upgrade_rewrites_every_legacy_value_and_narrows_the_vocabulary(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    _create_legacy_state(engine, module)

    with engine.connect() as conn:
        before = _table_counts(conn)
        assert _source_counts(conn)["modulo"] == 2

    _run(engine, monkeypatch, module, "upgrade")

    with engine.connect() as conn:
        after = _table_counts(conn)
        sources = _source_counts(conn)
        stage_types = dict(conn.exec_driver_sql("SELECT stage_type, count(*) FROM public.lifecycle_map_stages GROUP BY stage_type").all())
        sentinel_name = _exec(conn, "SELECT name FROM public.organisations WHERE id = CAST(:id AS uuid)", {"id": module._SENTINEL_ORG_ID}).scalar_one()
        other_name = _exec(conn, "SELECT name FROM public.organisations WHERE id = CAST(:id AS uuid)", {"id": _OTHER_ORG_ID}).scalar_one()
        documents = {str(row[0]): row[1] for row in conn.exec_driver_sql("SELECT id, content_json::text FROM public.lifecycle_maps").all()}

    # Every row is preserved — this rewrites values, never deletes.
    assert after == before
    assert "modulo" not in sources
    assert sources["farna_control"] == 2
    assert sources["local"] == 1
    assert sources["community"] == 1
    assert sources["registry"] == 1
    assert stage_types == {"farna_control": 2, "external": 1}
    assert sentinel_name == "Farna Control"
    # Keyed on the fixed id, so a same-named organisation is untouched.
    assert other_name == "Modulo"
    assert "farna_control" in "".join(documents.values())
    assert '"type": "modulo"' not in "".join(documents.values())
    # The rewrite keeps every other part of the document intact.
    mixed = next(text for text in documents.values() if "keep me" in text)
    assert '"notes": "keep me"' in mixed
    assert '"edges": [{"id": "e1", "source": "s1", "target": "s2"}]' in mixed
    assert '"type": "external"' in mixed
    plain = next(text for text in documents.values() if "keep me" not in text)
    assert '"type": "manual"' in plain


def test_upgrade_rejects_the_retired_values_after_narrowing(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    _create_legacy_state(engine, module)
    _run(engine, monkeypatch, module, "upgrade")

    with engine.connect() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            _exec(
                conn,
                "INSERT INTO public.library_primitives (id, source, visibility) VALUES (CAST(:id AS uuid), :source, 'community')",
                {"id": str(uuid.uuid4()), "source": module._LEGACY},
            )
        conn.rollback()
        with pytest.raises(sa.exc.IntegrityError):
            _exec(
                conn,
                "INSERT INTO public.lifecycle_map_stages (id, stage_type) VALUES (CAST(:id AS uuid), :stage_type)",
                {"id": str(uuid.uuid4()), "stage_type": module._LEGACY},
            )
        conn.rollback()
        # The current value is accepted.
        _exec(
            conn,
            "INSERT INTO public.library_primitives (id, source, visibility) VALUES (CAST(:id AS uuid), :source, 'community')",
            {"id": str(uuid.uuid4()), "source": module._CURRENT},
        )
        _exec(
            conn,
            "INSERT INTO public.lifecycle_map_stages (id, stage_type) VALUES (CAST(:id AS uuid), :stage_type)",
            {"id": str(uuid.uuid4()), "stage_type": module._CURRENT},
        )
        conn.rollback()


def test_upgrade_is_idempotent(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    _create_legacy_state(engine, module)

    _run(engine, monkeypatch, module, "upgrade")
    with engine.connect() as conn:
        first = (dict(_source_counts(conn)), _table_counts(conn))

    _run(engine, monkeypatch, module, "upgrade")
    with engine.connect() as conn:
        second = (dict(_source_counts(conn)), _table_counts(conn))

    assert second == first


def test_downgrade_restores_the_legacy_values(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    _create_legacy_state(engine, module)

    _run(engine, monkeypatch, module, "upgrade")
    _run(engine, monkeypatch, module, "downgrade")

    with engine.connect() as conn:
        sources = _source_counts(conn)
        stage_types = dict(conn.exec_driver_sql("SELECT stage_type, count(*) FROM public.lifecycle_map_stages GROUP BY stage_type").all())
        sentinel_name = _exec(conn, "SELECT name FROM public.organisations WHERE id = CAST(:id AS uuid)", {"id": module._SENTINEL_ORG_ID}).scalar_one()
        mixed = _exec(conn, "SELECT content_json::text FROM public.lifecycle_maps WHERE content_json::text LIKE '%keep me%'").scalar_one()

    assert sources["modulo"] == 2
    assert "farna_control" not in sources
    assert stage_types == {"modulo": 2, "external": 1}
    assert sentinel_name == "Modulo"
    assert '"type": "modulo"' in mixed
    assert '"type": "farna_control"' not in mixed
