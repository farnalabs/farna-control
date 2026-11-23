"""The 0364 migration rewrites the last persisted legacy values (FAR-2652).

0362 renamed the legacy database *objects*. The values below live inside rows,
so 0364 widens each CHECK vocabulary, rewrites the rows, then narrows the
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

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from farna.core.lifecycle_map.import_export import materialize_map_from_primitive
from farna.core.lifecycle_map.validation import LifecycleMapContentError

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0364_rewrite_legacy_persisted_values.py"
_PG_URL_ENV = "FARNA_2652_TEST_PG_URL"

#: A second organisation that also carries the retired display name. The rename
#: is keyed on the sentinel id, so this row must be left alone.
_OTHER_ORG_ID = "11111111-1111-1111-1111-111111111111"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0364_rewrite_legacy_persisted_values", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sync_url(raw: str) -> str:
    """Force the psycopg (v3) sync driver — psycopg2 is not in the tree."""
    return re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+psycopg://", raw, flags=re.IGNORECASE)


def _docker_daemon_socket_present() -> bool:
    """Leak-free check for a local Docker daemon before touching testcontainers.

    ``docker.from_env()`` leaves its probe socket open when the local unix socket
    is absent, and this module runs under the gated-suite warning gate
    (``-W error::pytest.PytestUnraisableExceptionWarning``), which escalates that
    leak to a session error. Checking the well-known socket paths first keeps a
    no-Docker run a clean skip. An explicit ``DOCKER_HOST`` (remote, dind, ssh) is
    always honoured by deferring to testcontainers.
    """
    if os.environ.get("DOCKER_HOST"):
        return True
    candidates = [Path("/var/run/docker.sock"), Path("~/.docker/run/docker.sock").expanduser()]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidates.append(Path(xdg_runtime) / "docker.sock")
    return any(path.exists() for path in candidates)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Throwaway PostgreSQL URL: env-provisioned, else testcontainers, else skip."""
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        yield _sync_url(env_url)
        return

    if not _docker_daemon_socket_present():
        pytest.skip("testcontainers Postgres unavailable (no Docker daemon socket reachable)")

    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    # A configured-but-unreachable endpoint can still fail after open; close the
    # client on the skip path so the failure leaves no socket behind.
    container = None
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # capability-gated skip, never a silent pass
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop()
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
    database = f"m0364_{uuid.uuid4().hex[:12]}"
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
        primitive_type varchar(20) NOT NULL DEFAULT 'composite',
        name varchar(255) NOT NULL DEFAULT '',
        content_json jsonb NOT NULL DEFAULT '{}'::jsonb,
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

#: Imported lifecycle maps are persisted as ``library_primitives`` rows whose
#: ``content_json`` carries the stage graph in three possible shapes: a direct
#: ``stages[].type``; the export envelope's ``export.content_json.stages``; and
#: each exported version's ``export.versions[].stages``. Copy-to-adapt
#: (``materialize_map_from_primitive``) re-validates these through
#: ``normalize_content``, so a retired stage type here must be rewritten too.

#: Placeholder for the retired vocabulary value, substituted from the migration
#: module so this test never spells the retired name (brand-consistency gate).
_LEGACY_PLACEHOLDER = "@legacy@"
_PRIMITIVE_ROWS: tuple[tuple[str, str, dict[str, object]], ...] = (
    (
        "direct",
        "lifecycle_map",
        {
            "stages": [
                {"id": "p1", "name": "Build", "type": _LEGACY_PLACEHOLDER},
                {"id": "p2", "name": "Deploy", "type": "external"},
            ],
            "edges": [{"id": "pe1", "source": "p1", "target": "p2"}],
            "notes": "direct keep",
        },
    ),
    (
        "exported",
        "lifecycle_map",
        {
            "lifecycle_map_id": "00000000-0000-0000-0000-000000000009",
            "export": {
                "primitive_type": "lifecycle_map",
                "format_version": "2",
                "name": "Exported",
                "description": "",
                "content_json": {
                    "stages": [{"id": "ec1", "name": "Design", "type": _LEGACY_PLACEHOLDER}],
                    "edges": [],
                    "notes": "export keep",
                },
                "versions": [
                    {
                        "version": 1,
                        "stages": [
                            {"id": "v1", "name": "Design", "type": _LEGACY_PLACEHOLDER},
                            {"id": "v2", "name": "Run", "type": "manual"},
                        ],
                        "edges": [],
                        "notes": "version keep",
                    }
                ],
            },
        },
    ),
    # A non-map primitive whose stage-like content must NOT be rewritten.
    ("workflow", "workflow", {"stages": [{"id": "w1", "name": "Untouched", "type": _LEGACY_PLACEHOLDER}]}),
    # A map primitive already on the current vocabulary: untouched.
    ("already-current", "lifecycle_map", {"stages": [{"id": "ok", "name": "OK", "type": "manual"}]}),
)


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
        for name, primitive_type, content in _PRIMITIVE_ROWS:
            _exec(
                conn,
                "INSERT INTO public.library_primitives (id, source, primitive_type, name, content_json, visibility) VALUES (CAST(:id AS uuid), 'local', :primitive_type, :name, CAST(:content AS jsonb), 'community')",
                {
                    "id": str(uuid.uuid4()),
                    "primitive_type": primitive_type,
                    "name": name,
                    "content": json.dumps(content).replace(_LEGACY_PLACEHOLDER, module._LEGACY),
                },
            )
        _exec(
            conn,
            "INSERT INTO public.lifecycle_maps (id, content_json) VALUES (CAST(:mixed AS uuid), CAST(:mixed_json AS jsonb)), (CAST(:plain AS uuid), CAST(:plain_json AS jsonb))",
            {
                "mixed": str(uuid.uuid4()),
                "plain": str(uuid.uuid4()),
                "mixed_json": '{"stages": [{"id": "s1", "name": "Build", "type": "@legacy@", "pipeline_id": "p1"}, {"id": "s2", "name": "Deploy", "type": "external"}], "edges": [{"id": "e1", "source": "s1", "target": "s2"}], "notes": "keep me"}'.replace(_LEGACY_PLACEHOLDER, module._LEGACY),
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


def _content_documents(conn: sa.Connection) -> dict[str, str]:
    """Map every ``lifecycle_maps`` row id to its ``content_json`` as text.

    Keyed by id and rendered via ``::text`` so an equality between two reads
    compares the stored JSON value exactly, including key and array order — the
    property a re-aggregation of ``stages`` could silently change.
    """
    rows = conn.exec_driver_sql("SELECT id, content_json::text FROM public.lifecycle_maps").all()
    return {str(row[0]): row[1] for row in rows}


def _primitive_documents(conn: sa.Connection) -> dict[str, str]:
    """Map every ``library_primitives`` name to its ``content_json`` as text."""
    rows = conn.exec_driver_sql("SELECT name, content_json::text FROM public.library_primitives").all()
    return {str(row[0]): row[1] for row in rows}


def _materialized_content(content_json: dict[str, object]) -> dict[str, object]:
    """Run the real ``materialize_map_from_primitive`` over one primitive.

    Only the DB session and the ``LifecycleMap`` model are faked; the parsing,
    envelope handling and — critically — the ``normalize_content`` validation
    that rejects the retired vocabulary run for real, which is the 422 path this
    migration must clear.
    """
    primitive = MagicMock()
    primitive.id = uuid.uuid4()
    primitive.primitive_type = "lifecycle_map"
    primitive.name = "Exported"
    primitive.description = ""
    primitive.content_json = content_json
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock(return_value=[])
    with patch("farna.core.lifecycle_map.service.LifecycleMap", return_value=MagicMock()) as model_cls:
        asyncio.run(materialize_map_from_primitive(session, org_id=uuid.uuid4(), account_id=uuid.uuid4(), primitive=primitive))
    return model_cls.call_args.kwargs["content_json"]  # type: ignore[no-any-return]


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0364_rewrite_legacy_persisted_values"
    assert module.down_revision == "0363_lower_operational_flag_tiers"


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
        primitives = _primitive_documents(conn)

    # Every row is preserved — this rewrites values, never deletes.
    assert after == before
    assert "modulo" not in sources
    assert sources["farna_control"] == 2
    # The four lifecycle_map/primitive fixtures are imported-style local rows.
    assert sources["local"] == 5
    assert sources["community"] == 1
    assert sources["registry"] == 1
    assert stage_types == {"farna_control": 2, "external": 1}
    assert sentinel_name == "Farna Control"
    # Keyed on the fixed id, so a same-named organisation is untouched.
    assert other_name == "Modulo"
    assert "farna_control" in "".join(documents.values())
    assert f'"type": "{module._LEGACY}"' not in "".join(documents.values())
    # The rewrite keeps every other part of the document intact.
    mixed = next(text for text in documents.values() if "keep me" in text)
    assert '"notes": "keep me"' in mixed
    assert '"edges": [{"id": "e1", "source": "s1", "target": "s2"}]' in mixed
    assert '"type": "external"' in mixed
    # Stage order is positional downstream, so the array rebuild must preserve it.
    rewritten_stages = json.loads(mixed)["stages"]
    assert [stage["id"] for stage in rewritten_stages] == ["s1", "s2"]
    plain = next(text for text in documents.values() if "keep me" not in text)
    assert '"type": "manual"' in plain

    # library_primitives.content_json — all three lifecycle_map shapes rewritten,
    # positionally and without disturbing the rest of the document.
    direct = json.loads(primitives["direct"])
    assert [stage["id"] for stage in direct["stages"]] == ["p1", "p2"]
    assert [stage["type"] for stage in direct["stages"]] == ["farna_control", "external"]
    assert direct["edges"] == [{"id": "pe1", "source": "p1", "target": "p2"}]
    assert direct["notes"] == "direct keep"

    export = json.loads(primitives["exported"])["export"]
    assert export["content_json"]["stages"][0]["type"] == "farna_control"
    assert export["content_json"]["notes"] == "export keep"
    version = export["versions"][0]
    assert version["version"] == 1
    assert [stage["id"] for stage in version["stages"]] == ["v1", "v2"]
    assert [stage["type"] for stage in version["stages"]] == ["farna_control", "manual"]
    assert f'"type": "{module._LEGACY}"' not in primitives["direct"] + primitives["exported"]
    # Only lifecycle_map rows are touched: neither a non-map primitive nor a
    # map primitive already on the current vocabulary is rewritten.
    assert json.loads(primitives["workflow"])["stages"][0]["type"] == module._LEGACY
    assert json.loads(primitives["already-current"])["stages"][0]["type"] == "manual"


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
        first = (dict(_source_counts(conn)), _table_counts(conn), _content_documents(conn), _primitive_documents(conn))

    _run(engine, monkeypatch, module, "upgrade")
    with engine.connect() as conn:
        second = (dict(_source_counts(conn)), _table_counts(conn), _content_documents(conn), _primitive_documents(conn))

    # ``content_json`` is included so a rewrite that is not byte-stable on a
    # second run (e.g. it re-aggregates the stage array in a different order)
    # is caught — the count and source comparisons alone would miss it.
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
        primitives = _primitive_documents(conn)

    assert sources["modulo"] == 2
    assert "farna_control" not in sources
    assert stage_types == {"modulo": 2, "external": 1}
    assert sentinel_name == "Modulo"
    assert f'"type": "{module._LEGACY}"' in mixed
    assert '"type": "farna_control"' not in mixed
    # The primitive content shapes are restored to the legacy vocabulary too.
    direct = json.loads(primitives["direct"])
    assert [stage["type"] for stage in direct["stages"]] == [module._LEGACY, "external"]
    export = json.loads(primitives["exported"])["export"]
    assert export["content_json"]["stages"][0]["type"] == module._LEGACY
    assert [stage["type"] for stage in export["versions"][0]["stages"]] == [module._LEGACY, "manual"]


def test_upgrade_clears_the_materialize_validation_gate(engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-deploy primitive carrying the retired stage type materializes.

    An old primitive must materialize after the upgrade.

    Before the rewrite, copy-to-adapt re-validates the stored primitive graph
    through ``normalize_content`` and raises ``LifecycleMapContentError`` (422)
    with no repair path. This proves the exact failing gate is cleared after
    ``upgrade`` — with the real materialize path, not a text assertion.
    """
    module = _load_migration()
    _create_legacy_state(engine, module)

    with engine.connect() as conn:
        legacy = json.loads(conn.exec_driver_sql("SELECT content_json::text FROM public.library_primitives WHERE name = 'exported'").scalar_one())
    with pytest.raises(LifecycleMapContentError):
        _materialized_content(legacy)

    _run(engine, monkeypatch, module, "upgrade")

    with engine.connect() as conn:
        rewritten = json.loads(conn.exec_driver_sql("SELECT content_json::text FROM public.library_primitives WHERE name = 'exported'").scalar_one())
    materialized = _materialized_content(rewritten)
    assert [stage["type"] for stage in materialized["stages"]] == ["farna_control", "manual"]
