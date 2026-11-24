"""FAR-2667: the native restore's stop/bring-back/replay ordering, against a REAL server.

The defect this pins
====================

``_run_restore`` stops a running launcher before it replays, and the launcher's
ordered teardown stops the bundled PostgreSQL it supervises. Every step after
the stop — the safety dump, ``_restore_cluster`` (psql plus psycopg), the
credential re-encryption, the collation check and ``_ensure_restore_posture`` —
connects to that server, so a native recovery used to replay into nothing.
Earlier tests missed it because they mocked ``_run_psql``, ``_restore_cluster``
and ``_stop_launcher_writers``: the process order looked right while every
connection was fake.

What is REAL here
=================

* a real PostgreSQL 16 server (``postgres:16-alpine``) on a real TCP port, whose
  data and endpoint survive a stop/start of the SAME container;
* ``_run_restore`` itself end to end: manifest validation, the instance-identity
  and disk checks, the dump step, ``_restore_cluster``'s psycopg replay of the
  checkpoint export, ``_ensure_restore_posture`` (the promoted
  ``farna.db.bootstrap_role.bootstrap_roles`` ceremony), launcher-artefact
  adoption and the printed start guidance;
* the server is genuinely DOWN after the stop (asserted) and genuinely UP inside
  the window (asserted) — without the window the replay's own connection fails,
  which is exactly the defect;
* the replayed row is read back from the server afterwards, and the server is
  genuinely stopped again when the restore returns (nothing auto-restarts).

What is SIMULATED (and why)
===========================

* the launcher's teardown (``stop_running_stack`` needs a live native launcher
  and POSIX file locking) is replaced by a real ``docker stop`` of the real
  server;
* the bundled-cluster restart (``_bundled_cluster_for_restore`` spawns the
  bundle's own ``pg/bin/postgres``, which no dev checkout and no Windows host
  ships) is replaced by a real ``docker start`` of the same container on the
  same fixed host port;
* the archive carries no ``database.sql``, so the psql replay step is not
  exercised on this host (no ``psql`` client). The JSON-export replay is real;
  the psql half is covered against a real database by
  ``scripts/verify_backup_restore.py`` in the nightly backup/restore job.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from farna.cli import backup as backup_mod

pytestmark = pytest.mark.integration

_OWNER_ROLE = "farna_control"
_OWNER_PASSWORD = "ownerpw"
_APP_ROLE = "farna_control_app"
_APP_PASSWORD = "apppw"
_DB_NAME = "farna_control"
_FERNET_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_THREAD_ID = "far-2667-thread"
_READY_TIMEOUT = 120.0


def _free_host_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


class _RealCluster:
    """A real Postgres whose stop/start keeps its DATA and its endpoint.

    testcontainers' own ``stop()``/``start()`` re-creates the container (its
    data is gone), which would simulate a different cluster coming back. The
    wrapped docker container is stopped and started instead, so the same server
    returns with the same rows on the same port — the property the restore's
    window depends on.
    """

    def __init__(self, container: PostgresContainer) -> None:
        self._container = container
        self._raw = container.get_wrapped_container()
        self.host = container.get_container_host_ip()
        self.port = int(container.get_exposed_port(5432))

    def url(self, role: str, password: str, *, scheme: str = "postgresql") -> str:
        return f"{scheme}://{role}:{password}@{self.host}:{self.port}/{_DB_NAME}"

    def accepting(self, *, timeout: int = 3) -> bool:
        try:
            with psycopg.connect(self.url(_OWNER_ROLE, _OWNER_PASSWORD), connect_timeout=timeout):
                return True
        except psycopg.Error:
            return False

    def stop(self) -> None:
        self._raw.stop()

    def start(self) -> None:
        self._raw.start()
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.accepting(timeout=2):
                return
            time.sleep(0.3)
        raise RuntimeError(f"the real cluster did not accept connections within {_READY_TIMEOUT:.0f}s after start")

    def teardown(self) -> None:
        self._container.stop()


@pytest.fixture(scope="module")
def real_cluster() -> Iterator[_RealCluster]:
    port = _free_host_port()
    container = PostgresContainer(
        "postgres:16-alpine",
        username=_OWNER_ROLE,
        password=_OWNER_PASSWORD,
        dbname=_DB_NAME,
    ).with_bind_ports(5432, port)
    container.start()
    cluster = _RealCluster(container)
    assert cluster.port == port, "the pinned host port is what keeps the endpoint stable across a stop/start"
    try:
        yield cluster
    finally:
        cluster.teardown()


def _seed_checkpoint_table(cluster: _RealCluster) -> None:
    """Create the checkpoint table the JSON replay writes into, plus a stale row."""
    with psycopg.connect(cluster.url(_OWNER_ROLE, _OWNER_PASSWORD)) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS checkpoints (organisation_id uuid, thread_id text, checkpoint_ns text, checkpoint_id text, parent_checkpoint_id text, type text, checkpoint jsonb, metadata jsonb)")
        cur.execute("DELETE FROM checkpoints")
        cur.execute("INSERT INTO checkpoints (thread_id, checkpoint_id) VALUES ('stale-row', 'stale-row')")
        # ``with conn`` commits on success; committing again would run on the
        # already-closed connection.


def _write_launcher_config(tmp_path: Path, cluster: _RealCluster) -> Path:
    """A bootstrapped data dir: the launcher's own pinned config.env."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.env").write_text(
        "\n".join(
            [
                f"DATABASE_URL={cluster.url(_APP_ROLE, _APP_PASSWORD, scheme='postgresql+asyncpg')}",
                f"DATABASE_ADMIN_URL={cluster.url(_OWNER_ROLE, _OWNER_PASSWORD, scheme='postgresql+asyncpg')}",
                f"FERNET_KEY={_FERNET_KEY}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return data_dir


def _write_archive(tmp_path: Path) -> Path:
    """A minimal archive: manifest + one checkpoint row, no database.sql."""
    archive = tmp_path / "archive"
    archive.mkdir()
    checkpoints = [
        {
            "organisation_id": None,
            "thread_id": _THREAD_ID,
            "checkpoint_ns": "",
            "checkpoint_id": "cp-restored",
            "parent_checkpoint_id": None,
            "type": "json",
            "checkpoint": {"v": 1},
            "metadata": {"source": "far-2667"},
        }
    ]
    payload = json.dumps(checkpoints).encode("utf-8")
    (archive / "checkpoints.json").write_bytes(payload)
    manifest = {
        "timestamp": "2024-01-01T00:00:00+00:00",
        "manifest_version": backup_mod.MANIFEST_VERSION,
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": backup_mod._get_schema_versions(),
        "fernet_key_hash": backup_mod._fernet_key_hash(_FERNET_KEY),
        "file_checksums": {"checkpoints.json": hashlib.sha256(payload).hexdigest()},
    }
    (archive / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    return archive


def test_the_replay_lands_in_the_cluster_the_stop_took_down(
    tmp_path: Path,
    real_cluster: _RealCluster,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """stop -> bring ONLY the cluster back -> dump+replay -> stop it again."""
    cluster = real_cluster
    _seed_checkpoint_table(cluster)
    data_dir = _write_launcher_config(tmp_path, cluster)
    archive = _write_archive(tmp_path)
    # The native operator's shell exports neither: the data dir's config.env is
    # the only source, exactly as on a real install (and this keeps the test
    # hermetic against an ambient value from a neighbouring suite).
    monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    events: list[str] = []

    def _stop_launcher(_data_dir: Path) -> bool:
        """The launcher stop: a REAL stop of the REAL server (what killed the replay)."""
        events.append("stop")
        cluster.stop()
        assert not cluster.accepting(), "the simulated launcher stop must really take the cluster away"
        return True

    @contextlib.contextmanager
    def _cluster_window(_data_dir: Path, _raw_url: str) -> Iterator[None]:
        """The restore window: a REAL start of the REAL server on the same endpoint."""
        events.append("window_up")
        cluster.start()
        assert cluster.accepting(), "the window must leave a server the replay can connect to"
        try:
            yield
        finally:
            events.append("window_down")
            cluster.stop()

    monkeypatch.setattr(backup_mod, "_stop_launcher_writers", _stop_launcher)
    monkeypatch.setattr(backup_mod, "_bundled_cluster_for_restore", _cluster_window)
    monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())

    options = backup_mod._RestoreOptions(
        backup_dir=archive,
        db_url=None,
        app_db_url=None,
        yes=True,
        previous_fernet_key=None,
        dry_run=False,
        data_dir=data_dir,
        # The seeded checkpoint table makes the target a POPULATED database
        # behind a fresh data dir, which needs the deliberate override.
        replace_cluster=True,
        no_safety_dump=False,
    )

    backup_mod._run_restore(options)

    # The stop happened, the window opened BEFORE the replay, and closed after it.
    assert events == ["stop", "window_up", "window_down"], events

    # Nothing auto-restarts: the cluster is down when the restore returns.
    assert not cluster.accepting(), "a restore must leave the writers (and the cluster) stopped"

    # The operator gets a copy-pasteable start command, with no URL they do not have.
    captured = capsys.readouterr()
    assert f"farna-control start --data-dir {data_dir}" in captured.out
    assert "--db-url" not in captured.out
    assert "No automatic restart was performed" in captured.out

    # The replay really landed: bring the server back and read the row back.
    cluster.start()
    with psycopg.connect(cluster.url(_OWNER_ROLE, _OWNER_PASSWORD)) as conn, conn.cursor() as cur:
        cur.execute("SELECT thread_id, checkpoint_id FROM checkpoints ORDER BY checkpoint_id")
        rows = cur.fetchall()

    assert rows == [(_THREAD_ID, "cp-restored")], f"the replay must replace the stale row; got {rows!r}"
