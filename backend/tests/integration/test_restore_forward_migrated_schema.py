"""FAR-2763: a pre-upgrade snapshot must restore over a FORWARD-migrated schema.

The documented compose recovery
================================

``scripts/compose-restore.sh --confirm <snapshot>`` followed by
``scripts/compose-rollback.sh <previous-version>`` is the ONE documented
compose recovery (FAR-2667). The FAR-2674 acceptance matrix proved it did not
work against real published images, in two independent ways:

1. **Option parsing.** ``restore``'s ``--target-fs`` was ``click.Path`` with
   click's default ``readable=True``. The packaged compose sets
   ``DB_CAPACITY_PATH`` to the postgres-owned 0700 database volume mounted
   read-only for the capacity monitor, so restore exited 2 at option parsing
   ("Path ... is not readable") before any pre-flight.
2. **The replay.** ``_restore_cluster`` replayed the snapshot's plain
   ``--clean --if-exists`` dump over whatever schema the target had. The dump
   was taken at revision N, so it does not know objects added by later
   migrations, and its clean preamble cannot drop a constraint a newer object
   depends on. Observed: ``cannot drop constraint organisations_pkey ...
   constraint farna_journey_facts_organisation_id_fkey depends on index
   organisations_pkey`` (``0359_registry_sourced_drop_index`` ->
   ``0364_rewrite_legacy_persisted_values``).

What is REAL here
=================

* a real PostgreSQL 16 server (``postgres:16-alpine``);
* the real dump producer (``farna.db.plain_dump.dump_plain_sql``) and the real
  replay (``farna.cli.backup._run_psql``), executing real ``pg_dump``/``psql``
  against that server;
* for the acceptance test, the SHIPPED ``scripts/compose-restore.sh`` executed
  under bash, with the real ``farna-control restore`` CLI behind it and the
  real database underneath — its presence check, both dry runs, the stop, the
  pre-restore safety dump and the replay all run for real;
* the exact failing shape: a snapshot at N restored onto a database at N+k
  that added an FK onto a pre-existing primary key (plus the 0362 table rename
  that produced the observed failure). The test also replays the same dump the
  OLD way (no schema reset) into a control database and asserts it fails with
  the dependency error — so the test cannot pass vacuously if the forward
  migration stops creating the dependency.

What is PROXIED (and why)
=========================

Hosted CI does not ship a PostgreSQL client of the server's major version (the
nightly backup gate installs one explicitly), so ``psql``/``pg_dump`` on PATH
are thin shims that ``docker exec`` the REAL client inside the real server
container. The SQL, the client binary and the server are real; only their
location is proxied. Likewise the helper's ``docker`` shim translates
``compose run`` into the real CLI on this host: no image build is reachable in
a test, and the image that contains a fix does not exist until the fix ships.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv lists for the repository's own scripts
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from farna.cli import backup as backup_mod
from farna.db import plain_dump

pytestmark = [pytest.mark.integration, pytest.mark.posix_only]

_IMAGE = "postgres:16-alpine"
_ADMIN_USER = "farna_control_test"
_ADMIN_PASSWORD = "farna_control_test"
_DEFAULT_DB = "farna_control_test"
#: A real Fernet-shaped key (Settings only requires >= 32 bytes) shared by the
#: archive manifest and the restore process, so no credential re-encryption is
#: triggered.
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_SECRET_KEY = "b" * 32

#: The revisions the FAR-2674 matrix used: the snapshot's revision and the
#: forward-migrated target's revision (both exist in this binary's chain, so
#: --verify-target-schema sees the target as an ancestor-descendant rollback).
_N_REVISION = "0359_registry_sourced_drop_index"
_N_PLUS_K_REVISION = "0364_rewrite_legacy_persisted_values"

_SNAPSHOT_NAME = "pre-upgrade-dump-0.3.330-rc2-20261125"

#: Schema at revision N: the tables the incident's snapshot carried.
_N_SCHEMA_SQL = (
    "DROP SCHEMA IF EXISTS public CASCADE",
    "CREATE SCHEMA public AUTHORIZATION pg_database_owner",
    "GRANT USAGE ON SCHEMA public TO PUBLIC",
    "CREATE TABLE public.organisations (id uuid PRIMARY KEY, name text NOT NULL)",
    "CREATE TABLE public.journey_facts_legacy (id uuid PRIMARY KEY, organisation_id uuid NOT NULL REFERENCES public.organisations(id))",
    "INSERT INTO public.organisations VALUES ('00000000-0000-0000-0000-0000000000aa', 'acme')",
    "INSERT INTO public.journey_facts_legacy VALUES ('00000000-0000-0000-0000-0000000000bb', '00000000-0000-0000-0000-0000000000aa')",
    "CREATE TABLE public.alembic_version (version_num varchar(255) NOT NULL PRIMARY KEY)",
    f"INSERT INTO public.alembic_version VALUES ('{_N_REVISION}')",  # noqa: S608 - module constant, not user input
)

#: What the migrations between N and N+k did: 0362 renamed the journey table
#: and its constraints, and a later migration added a NEW table whose FK
#: depends on the pre-existing ``organisations`` primary key. That is the
#: general shape: any migration that adds a constraint/FK onto a pre-existing
#: PK or unique index blocks the old dump's ``DROP CONSTRAINT``.
_FORWARD_MIGRATION_SQL = (
    "ALTER TABLE public.journey_facts_legacy RENAME TO farna_journey_facts",
    "ALTER TABLE public.farna_journey_facts RENAME CONSTRAINT journey_facts_legacy_pkey TO farna_journey_facts_pkey",
    "ALTER TABLE public.farna_journey_facts RENAME CONSTRAINT journey_facts_legacy_organisation_id_fkey TO farna_journey_facts_organisation_id_fkey",
    "CREATE TABLE public.journey_events (id uuid PRIMARY KEY, organisation_id uuid NOT NULL REFERENCES public.organisations(id))",
    f"UPDATE public.alembic_version SET version_num = '{_N_PLUS_K_REVISION}'",  # noqa: S608 - module constant, not user input
)

#: The pre-fix replay: the dump alone, under psql's single transaction. Kept
#: ONLY as the control that proves the forward migration creates the blocking
#: dependency the fixed replay has to survive.
_OLD_REPLAY_ARGV = ("psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1")


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "compose-restore.sh").is_file():
            return parent
    raise RuntimeError("Could not find the repository root (scripts/compose-restore.sh)")


_REPO_ROOT = _repo_root()


def _venv_cli() -> Path:
    """The ``farna-control`` console script beside the running interpreter."""
    candidate = Path(sys.executable).with_name("farna-control")
    if candidate.is_file():
        return candidate
    found = shutil.which("farna-control")
    if found is None:
        raise RuntimeError("the farna-control console script is not installed in this environment")
    return Path(found)


@dataclass(frozen=True)
class _Cluster:
    """A real PostgreSQL 16 server plus the host-side client shims."""

    container_id: str
    host: str
    port: int
    shims_dir: Path
    docker: str

    def url(self, database: str) -> str:
        return f"postgresql://{_ADMIN_USER}:{_ADMIN_PASSWORD}@{self.host}:{self.port}/{database}"


def _write_client_shims(shims_dir: Path, docker: str) -> None:
    """Write ``psql``/``pg_dump`` shims that exec the REAL clients in the container.

    This is a transport shim only: the same binary and the same server run the
    SQL. It exists because a matching-major ``psql``/``pg_dump`` is not on the
    test runner's PATH, and a mocked replay is not evidence about a replay.
    """
    shims_dir.mkdir(parents=True, exist_ok=True)
    psql = """#!/usr/bin/env bash
set -euo pipefail
args=(); files=()
while [ $# -gt 0 ]; do
  case "$1" in
    -f) files+=("$2"); shift 2 ;;
    postgres://*|postgresql://*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
if [ ${#files[@]} -eq 0 ]; then echo "psql shim: no -f" >&2; exit 2; fi
fargs=(); i=0
for f in "${files[@]}"; do
  i=$((i+1))
  in_container="/tmp/farna-test-$$-$i-$(basename "$f")"
  "$FARNA_TEST_DOCKER" cp "$f" "$FARNA_TEST_PG_CONTAINER:$in_container" >/dev/null
  fargs+=(-f "$in_container")
done
exec "$FARNA_TEST_DOCKER" exec -e PGPASSWORD "$FARNA_TEST_PG_CONTAINER" psql "${args[@]}" \
  -h 127.0.0.1 -p 5432 -U "$FARNA_TEST_PG_USER" -d "$FARNA_TEST_PG_DB" "${fargs[@]}"
"""
    pg_dump = """#!/usr/bin/env bash
set -euo pipefail
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    postgres://*|postgresql://*) shift ;;
    *) args+=("$1"); shift ;;
  esac
done
exec "$FARNA_TEST_DOCKER" exec -e PGPASSWORD "$FARNA_TEST_PG_CONTAINER" pg_dump "${args[@]}" \
  -h 127.0.0.1 -p 5432 -U "$FARNA_TEST_PG_USER" -d "$FARNA_TEST_PG_DB"
"""
    for name, body in (("psql", psql), ("pg_dump", pg_dump)):
        path = shims_dir / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(0o755)


def _write_compose_shim(shims_dir: Path) -> None:
    """A ``docker`` shim that runs the real CLI for the helper's one-off containers.

    The helper's contract is a sequence of ``docker compose run`` one-offs. No
    image build is reachable from a test, so the shim maps the container
    filesystem (``/backups``) onto the host and executes the REAL CLI; the
    helper's own argv, ordering and flags run unchanged.
    """
    body = """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" != "compose" ]; then echo "unexpected docker args: $*" >&2; exit 3; fi
shift
while [ "${1:-}" = "-f" ] || [ "${1:-}" = "--file" ]; do shift 2; done
verb="${1:-}"; shift || true
case "$verb" in
  stop) exit 0 ;;
  run)
    entrypoint=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --rm|--no-deps) shift ;;
        --entrypoint) entrypoint="$2"; shift 2 ;;
        -*) shift ;;
        *) break ;;
      esac
    done
    if [ "$#" -eq 0 ]; then echo "no service" >&2; exit 3; fi
    shift
    mapped=()
    for a in "$@"; do
      case "$a" in
        /backups|/backups/*) mapped+=("${FARNA_TEST_SNAPSHOT_ROOT}${a#/backups}") ;;
        *) mapped+=("$a") ;;
      esac
    done
    if [ "$entrypoint" = "sh" ]; then
      exec sh "${mapped[@]}"
    fi
    if [ "$entrypoint" = "farna-control" ]; then
      cd "$FARNA_TEST_CLI_CWD"
      exec "$FARNA_TEST_CLI" "${mapped[@]}"
    fi
    echo "unsupported entrypoint: $entrypoint" >&2
    exit 3
    ;;
  *) echo "unsupported compose verb: $verb" >&2; exit 3 ;;
esac
"""
    path = shims_dir / "docker"
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> _Cluster:
    docker = shutil.which("docker")
    if docker is None:
        pytest.fail("the Docker CLI is required: the replay shims exec the real psql/pg_dump inside the server container")
    container = PostgresContainer(
        _IMAGE,
        username=_ADMIN_USER,
        password=_ADMIN_PASSWORD,
        dbname=_DEFAULT_DB,
    )
    container.start()
    try:
        shims_dir = tmp_path_factory.mktemp("far-2763-shims")
        _write_client_shims(shims_dir, docker)
        _write_compose_shim(shims_dir)
        yield _Cluster(
            container_id=container.get_wrapped_container().id,
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            shims_dir=shims_dir,
            docker=docker,
        )
    finally:
        container.stop()


@pytest.fixture
def replay_env(cluster: _Cluster, monkeypatch: pytest.MonkeyPatch) -> _Cluster:
    """Put the client shims first on PATH and point them at the real container."""
    monkeypatch.setenv("PATH", f"{cluster.shims_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FARNA_TEST_DOCKER", cluster.docker)
    monkeypatch.setenv("FARNA_TEST_PG_CONTAINER", cluster.container_id)
    monkeypatch.setenv("FARNA_TEST_PG_USER", _ADMIN_USER)
    monkeypatch.setenv("FARNA_TEST_PG_DB", _DEFAULT_DB)
    return cluster


def _new_database(cluster: _Cluster) -> str:
    """Create a uniquely named database on the real server; return its name."""
    name = f"far2763_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(cluster.url(_DEFAULT_DB), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    return name


def _apply_sql(cluster: _Cluster, database: str, statements: tuple[str, ...]) -> None:
    with psycopg.connect(cluster.url(database), autocommit=True) as conn, conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def _write_archive(archive: Path, dump_path: Path) -> None:
    """A real restore archive: the shipped dump plus its manifest."""
    checksum = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    manifest = {
        "timestamp": "2026-11-25T00:00:00+00:00",
        "manifest_version": backup_mod.MANIFEST_VERSION,
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": [_N_REVISION],
        "fernet_key_hash": backup_mod._fernet_key_hash(_FERNET_KEY),
        "file_checksums": {"database.sql": checksum},
        "dump_complete": True,
    }
    (archive / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")


@contextmanager
def _target_database(database: str) -> Iterator[None]:
    """Point the host-side client shims at *database* for the duration.

    The shims read their target from the environment (the production code owns
    the child env), so the test is what selects the per-test database.
    """
    with patch.dict(os.environ, {"FARNA_TEST_PG_DB": database}):
        yield


def _take_snapshot(cluster: _Cluster, database: str, archive: Path) -> Path:
    """Dump ``database`` with the SHIPPED producer and write a matching manifest."""
    archive.mkdir(parents=True, exist_ok=True)
    dump_path = archive / "database.sql"
    with _target_database(database):
        plain_dump.dump_plain_sql(cluster.url(database), dump_path)
    _write_archive(archive, dump_path)
    return dump_path


def _forward_migrate(cluster: _Cluster, database: str) -> None:
    _apply_sql(cluster, database, _FORWARD_MIGRATION_SQL)


def _old_replay(cluster: _Cluster, database: str, dump_path: Path) -> subprocess.CompletedProcess[str]:
    """Replay the dump the PRE-FIX way (no schema reset) — the control case."""
    env = dict(os.environ)
    env["PGPASSWORD"] = _ADMIN_PASSWORD
    env["FARNA_TEST_DOCKER"] = cluster.docker
    env["FARNA_TEST_PG_CONTAINER"] = cluster.container_id
    env["FARNA_TEST_PG_USER"] = _ADMIN_USER
    env["FARNA_TEST_PG_DB"] = database
    return subprocess.run(  # noqa: S603 - fixed argv, repository script under test
        [*_OLD_REPLAY_ARGV, "-f", str(dump_path), cluster.url(database)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=env,
    )


def _table_names(cluster: _Cluster, database: str) -> set[str]:
    with psycopg.connect(cluster.url(database)) as conn, conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def _schema_version(cluster: _Cluster, database: str) -> str:
    with psycopg.connect(cluster.url(database)) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM public.alembic_version")
        row = cur.fetchone()
    return str(row[0]) if row else ""


def _organisation_count(cluster: _Cluster, database: str) -> int:
    with psycopg.connect(cluster.url(database)) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.organisations")
        row = cur.fetchone()
    return int(row[0]) if row else -1


def test_pre_fix_replay_really_cannot_drop_the_forward_migrated_pk(replay_env: _Cluster, tmp_path: Path) -> None:
    """The control: the exact FAR-2674 failure, reproduced against a real server.

    Without this the regression test could pass because the forward migration
    stopped creating the blocking dependency.
    """
    cluster = replay_env
    database = _new_database(cluster)
    _apply_sql(cluster, database, _N_SCHEMA_SQL)
    control = _new_database(cluster)
    _apply_sql(cluster, control, _N_SCHEMA_SQL)
    _forward_migrate(cluster, control)
    dump_path = _take_snapshot(cluster, database, tmp_path / "archive")

    result = _old_replay(cluster, control, dump_path)

    assert result.returncode != 0, result.stdout
    assert "cannot drop constraint organisations_pkey" in result.stderr
    assert "depends on index" in result.stderr
    # The failed replay rolled back: the control database is still at N+k.
    assert _schema_version(cluster, control) == _N_PLUS_K_REVISION


def test_a_snapshot_from_n_restores_over_a_database_at_n_plus_k(replay_env: _Cluster, tmp_path: Path) -> None:
    """FAR-2763 defect 2: the shipped replay resets the schema first."""
    cluster = replay_env
    database = _new_database(cluster)
    _apply_sql(cluster, database, _N_SCHEMA_SQL)
    dump_path = _take_snapshot(cluster, database, tmp_path / "archive")
    _forward_migrate(cluster, database)

    with _target_database(database):
        backup_mod._run_psql(
            cluster.url(database),
            dump_path,
            preamble_sql=backup_mod._PUBLIC_SCHEMA_RESET_SQL,
        )

    tables = _table_names(cluster, database)
    assert "journey_facts_legacy" in tables
    assert "organisations" in tables
    assert "farna_journey_facts" not in tables
    assert "journey_events" not in tables
    assert _schema_version(cluster, database) == _N_REVISION
    assert _organisation_count(cluster, database) == 1


def test_a_failed_replay_rolls_the_schema_reset_back_too(replay_env: _Cluster, tmp_path: Path) -> None:
    """The reset and the dump share ONE transaction: a broken dump changes nothing."""
    cluster = replay_env
    database = _new_database(cluster)
    _apply_sql(cluster, database, _N_SCHEMA_SQL)
    dump_path = _take_snapshot(cluster, database, tmp_path / "archive")
    _forward_migrate(cluster, database)
    # A statement the dump does not contain, appended after its trailer. It
    # must fail AFTER the reset has already dropped the old schema; if the
    # reset were a separate transaction the forward-migrated state would be
    # gone and the cluster would be left half-restored.
    broken_dump = tmp_path / "broken-database.sql"
    broken_dump.write_bytes(dump_path.read_bytes() + b"\nSELECT 1/0;\n")

    with pytest.raises(RuntimeError, match="psql restore failed"), _target_database(database):
        backup_mod._run_psql(
            cluster.url(database),
            broken_dump,
            preamble_sql=backup_mod._PUBLIC_SCHEMA_RESET_SQL,
        )

    tables = _table_names(cluster, database)
    assert "farna_journey_facts" in tables
    assert "journey_events" in tables
    assert _schema_version(cluster, database) == _N_PLUS_K_REVISION
    assert _organisation_count(cluster, database) == 1


def _helper_env(cluster: _Cluster, database: str, snapshot_root: Path, capacity_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{cluster.shims_dir}{os.pathsep}{_venv_cli().parent}{os.pathsep}{env.get('PATH', '')}"
    env["FARNA_TEST_DOCKER"] = cluster.docker
    env["FARNA_TEST_PG_CONTAINER"] = cluster.container_id
    env["FARNA_TEST_PG_USER"] = _ADMIN_USER
    env["FARNA_TEST_PG_DB"] = database
    env["FARNA_TEST_SNAPSHOT_ROOT"] = str(snapshot_root)
    env["FARNA_TEST_CLI"] = str(_venv_cli())
    env["FARNA_TEST_CLI_CWD"] = str(_REPO_ROOT / "backend")
    env["DATABASE_ADMIN_URL"] = f"postgresql+asyncpg://{_ADMIN_USER}:{_ADMIN_PASSWORD}@{cluster.host}:{cluster.port}/{database}"
    env["DATABASE_URL"] = f"postgresql+asyncpg://farna_control_app:apppw@{cluster.host}:{cluster.port}/{database}"
    env["FERNET_KEY"] = _FERNET_KEY
    env["SECRET_KEY"] = _SECRET_KEY
    env["REDIS_URL"] = ""
    env["DB_CAPACITY_PATH"] = str(capacity_path)
    return env


def test_the_shipped_compose_restore_helper_restores_the_pre_upgrade_snapshot(replay_env: _Cluster, tmp_path: Path) -> None:
    """FAR-2763 acceptance: the DOCUMENTED command, executed end to end.

    Snapshot at N -> target at N+k (with an FK onto the pre-existing PK) ->
    ``scripts/compose-restore.sh --confirm <snapshot>`` succeeds with the
    compose default ``DB_CAPACITY_PATH`` shape (an unreadable database-volume
    directory), and the database is back at N with the pre-upgrade data intact.
    """
    cluster = replay_env
    database = _new_database(cluster)
    _apply_sql(cluster, database, _N_SCHEMA_SQL)

    snapshot_root = tmp_path / "backups"
    snapshot_root.mkdir()
    archive = snapshot_root / _SNAPSHOT_NAME
    _take_snapshot(cluster, database, archive)
    _forward_migrate(cluster, database)

    # The compose volume: postgres-owned 0700, mounted read-only for the
    # capacity monitor. The app user cannot read it — restore must not need to.
    capacity_path = tmp_path / "db-volume"
    capacity_path.mkdir()
    capacity_path.chmod(0o000)

    bash = shutil.which("bash")
    assert bash is not None, "the helper's interpreter (bash) must be available"
    env = _helper_env(cluster, database, snapshot_root, capacity_path)
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, repository script under test
            [
                bash,
                str(_REPO_ROOT / "scripts" / "compose-restore.sh"),
                "--confirm",
                _SNAPSHOT_NAME,
                str(_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"),
            ],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        # pytest's tmp cleanup cannot descend into a 000 directory.
        capacity_path.chmod(0o700)

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "Restore complete. Writers stay stopped." in result.stdout
    assert "compose-rollback.sh" in result.stdout

    tables = _table_names(cluster, database)
    assert "journey_facts_legacy" in tables
    assert "farna_journey_facts" not in tables
    assert "journey_events" not in tables
    assert _schema_version(cluster, database) == _N_REVISION
    assert _organisation_count(cluster, database) == 1

    # The pre-restore safety dump was taken BEFORE the replay, on the volume.
    safety_dumps = sorted(path for path in snapshot_root.iterdir() if path.name.startswith("pre-restore-dump-"))
    assert safety_dumps, "the helper's restore must take a pre-restore safety dump first"
    assert (safety_dumps[-1] / "database.sql").is_file()

    # The posture step re-applied the application role's schema grants after
    # the schema was recreated.
    with psycopg.connect(cluster.url(database)) as conn, conn.cursor() as cur:
        cur.execute("SELECT has_schema_privilege('farna_control_app', 'public', 'USAGE')")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True
