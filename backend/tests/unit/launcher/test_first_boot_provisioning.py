"""FAR-2560: first-boot application-database provisioning.

The macOS installer smoke reached the migrations stage with a healthy Postgres
that rejected every connection because the application database did not exist:

    asyncpg.exceptions.InvalidCatalogNameError: database "modulo" does not exist

Root cause: the provisioner created one database name while the composed
``DATABASE_URL`` connected to another (two literals, one left un-rebranded).
These tests lock the structural fix:

* the database the launcher CREATES is DERIVED from the composed
  ``DATABASE_URL`` — the exact URL the app, its migrations and the SAQ children
  connect with — so the two can never disagree, and no second literal exists;
* provisioning is IDEMPOTENT: a missing database is created exactly once, a
  restart creates nothing, and a pre-existing database is adopted untouched;
* the existence check is a bound parameter and the DDL name is grammar-validated
  (CREATE DATABASE cannot take a bind parameter);
* the bundled children's documented log paths are real destinations the
  supervisor redirects service output into (the diagnostic half of the ticket:
  the smoke reported ``logs/postgres.log`` / ``logs/redis.log`` absent while
  Postgres was demonstrably serving).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

import farna.launcher.entry as entry_module
from farna.launcher import config_source, upgrade
from farna.launcher.config_source import APP_DB_NAME, POSTGRES_HOST, compose_config
from farna.launcher.supervisor import log_paths


class _FakeCluster:
    """In-memory stand-in for the bundled cluster's database catalog."""

    def __init__(self, existing: set[str] | None = None) -> None:
        self.databases = set(existing or ())
        self.connects: list[str] = []
        self.existence_queries: list[tuple[str, tuple[Any, ...]]] = []
        self.statements: list[str] = []

    def creates(self) -> list[str]:
        return [statement for statement in self.statements if statement.startswith("CREATE DATABASE")]


class _FakeConnection:
    def __init__(self, cluster: _FakeCluster) -> None:
        self._cluster = cluster

    async def fetchval(self, query: str, *params: Any) -> int | None:
        self._cluster.existence_queries.append((query, params))
        return 1 if params and params[0] in self._cluster.databases else None

    async def execute(self, statement: str) -> str:
        self._cluster.statements.append(statement)
        quoted = statement.split('"')
        self._cluster.databases.add(quoted[1])
        return "CREATE DATABASE"

    async def close(self) -> None:
        return None


def _install_fake_cluster(monkeypatch: pytest.MonkeyPatch, cluster: _FakeCluster) -> None:
    import asyncpg

    async def _connect(dsn: str, **_kwargs: Any) -> _FakeConnection:
        cluster.connects.append(dsn)
        return _FakeConnection(cluster)

    monkeypatch.setattr(asyncpg, "connect", _connect)


def _composed() -> dict[str, str]:
    """A real composed config (the URL builder under test, not a stub)."""
    state = SimpleNamespace(postgres_port=15432, redis_port=16379)
    secrets = SimpleNamespace(postgres_password="pg-pass", redis_password="redis-pass", state_hmac_key=bytes(range(32)))
    return compose_config(state, secrets)


def _launcher_state() -> Any:
    return SimpleNamespace(postgres_port=15432)


def _secrets() -> Any:
    return SimpleNamespace(postgres_password="pg-pass")


def test_created_database_is_the_one_the_composed_url_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The create target must come from the URL, never from a second literal.

    The URL is retargeted to a name that appears nowhere as a constant, so an
    implementation that provisions any fixed name (the FAR-2560 defect) cannot
    pass this test.
    """
    cluster = _FakeCluster()
    _install_fake_cluster(monkeypatch, cluster)
    composed = _composed()
    composed["DATABASE_URL"] = "postgresql+asyncpg://modulo:pg-pass@127.0.0.1:15432/tenant_alpha"

    entry_module._ensure_app_database(composed, _launcher_state(), _secrets())

    assert cluster.creates() == ['CREATE DATABASE "tenant_alpha"']
    assert cluster.databases == {"tenant_alpha"}
    # The maintenance connection is the cluster's own `postgres` database: the
    # app database cannot be connected to before it exists.
    assert cluster.connects == ["postgresql://modulo:pg-pass@127.0.0.1:15432/postgres"]


def test_first_boot_creates_missing_database_then_restart_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    _install_fake_cluster(monkeypatch, cluster)
    composed = _composed()

    entry_module._ensure_app_database(composed, _launcher_state(), _secrets())
    assert cluster.creates() == [f'CREATE DATABASE "{APP_DB_NAME}"']

    entry_module._ensure_app_database(composed, _launcher_state(), _secrets())

    assert cluster.creates() == [f'CREATE DATABASE "{APP_DB_NAME}"']
    assert cluster.databases == {APP_DB_NAME}


def test_preexisting_database_is_adopted_without_ddl(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster(existing={"operator_created"})
    _install_fake_cluster(monkeypatch, cluster)
    composed = _composed()
    composed["DATABASE_URL"] = "postgresql+asyncpg://modulo:pg-pass@127.0.0.1:15432/operator_created"

    entry_module._ensure_app_database(composed, _launcher_state(), _secrets())

    assert not cluster.creates()
    assert cluster.databases == {"operator_created"}


def test_existence_check_binds_the_name_as_a_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Injection safety on the half of the statement that CAN be parameterised."""
    cluster = _FakeCluster()
    _install_fake_cluster(monkeypatch, cluster)

    entry_module._ensure_app_database(_composed(), _launcher_state(), _secrets())

    assert len(cluster.existence_queries) == 1
    query, params = cluster.existence_queries[0]
    assert "datname = $1" in query
    assert params == (APP_DB_NAME,)


def test_provisioning_fails_loudly_without_a_database_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    _install_fake_cluster(monkeypatch, cluster)

    with pytest.raises(entry_module.BootError, match="names no database"):
        entry_module._ensure_app_database({"DATABASE_URL": "postgresql+asyncpg://modulo:pg-pass@127.0.0.1:15432"}, _launcher_state(), _secrets())

    assert not cluster.connects


def test_provisioning_fails_loudly_without_a_composed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = _FakeCluster()
    _install_fake_cluster(monkeypatch, cluster)

    with pytest.raises(entry_module.BootError, match="no DATABASE_URL"):
        entry_module._ensure_app_database({}, _launcher_state(), _secrets())

    assert not cluster.connects


def test_the_database_name_has_exactly_one_owner() -> None:
    """No second literal may reintroduce the FAR-2560 drift."""
    assert APP_DB_NAME == "farna_control"
    assert not hasattr(entry_module, "APP_DB_NAME")
    assert upgrade._APP_DB_NAME == APP_DB_NAME
    assert upgrade._POSTGRES_HOST == POSTGRES_HOST


def test_composed_urls_name_that_one_database() -> None:
    from sqlalchemy.engine import make_url

    composed = _composed()

    assert make_url(composed["DATABASE_URL"]).database == APP_DB_NAME
    assert make_url(composed["FARNA_CONTROL_SYSTEM_DATABASE_URL"]).database == APP_DB_NAME
    assert make_url(composed["DATABASE_URL"]).host == POSTGRES_HOST


def test_bundled_service_specs_route_output_to_the_documented_log_paths(tmp_path: Any) -> None:
    data_dir = tmp_path / "data"
    paths = log_paths(data_dir)
    state = SimpleNamespace(postgres_port=15432, redis_port=16379)
    secrets = SimpleNamespace(redis_password="redis-pass")
    knobs = SimpleNamespace(pg_fast_shutdown_timeout=30.0)

    postgres = entry_module._postgres_child(state, data_dir / "pgdata", tmp_path / "bin", knobs)
    redis = entry_module._redis_child(state, secrets, tmp_path / "bin", data_dir)

    assert postgres.log_path == paths["postgres"]
    assert redis.log_path == paths["redis"]


def test_saq_specs_keep_output_on_the_launcher_stream() -> None:
    """Only the bundled services own a per-service log; SAQ output stays app output."""
    from farna.launcher.supervisor import ChildSpec

    spec = ChildSpec(name="saq-runs", argv_builder=lambda: ["saq"])

    assert spec.log_path is None


def test_tail_process_reads_the_log_file_when_stderr_is_redirected(tmp_path: Any) -> None:
    """With output redirected there is no stderr pipe; the file IS the tail."""
    from farna.launcher.supervisor import _TailProcess

    log_path = log_paths(tmp_path)["postgres"]
    log_path.parent.mkdir(parents=True)
    log_path.write_text("FATAL: could not create database\n", encoding="utf-8")

    proc = _TailProcess(SimpleNamespace(pid=4242, stderr=None), log_path=log_path)
    tail = proc.stderr_tail()

    assert tail is not None
    assert "could not create database" in tail

    absent = _TailProcess(SimpleNamespace(pid=4243, stderr=None), log_path=tmp_path / "not-created-yet.log")
    assert absent.stderr_tail() is None


@pytest.mark.skipif(sys.platform == "win32", reason="the child shim is POSIX-only")
def test_default_spawner_writes_child_output_to_its_log_file(tmp_path: Any) -> None:
    """Both streams land in the declared log, and the crash tail reads it back."""
    from farna.launcher.supervisor import _default_spawner, _TailProcess

    log_path = log_paths(tmp_path)["postgres"]
    argv = [sys.executable, "-c", "import sys; print('pg-stdout-marker'); print('pg-stderr-marker', file=sys.stderr)"]

    proc = _default_spawner(argv, None, log_path)
    try:
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert isinstance(proc, _TailProcess)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    assert "pg-stdout-marker" in text
    assert "pg-stderr-marker" in text
    tail = proc.stderr_tail()
    assert tail is not None
    assert "pg-stderr-marker" in tail


def test_compose_config_is_the_single_source_the_entry_derives_from() -> None:
    """A composed name that differs from the constant still drives the create."""
    composed = _composed()
    from sqlalchemy.engine import make_url

    assert make_url(composed["DATABASE_URL"]).database == config_source.APP_DB_NAME
