"""FAR-2667 — the pre-upgrade snapshot: completeness, disk guard, retention.

These are the hardening tests for the ONE ``take_upgrade_snapshot`` callable
the native launcher and the container entrypoint both use. They are
deliberately NOT ``posix_only``: everything asserted here is file I/O, argv
construction and arithmetic, so the guards are proven on every developer
platform and not only in Linux CI.

The three non-negotiables under test:
* a dump is PROVEN complete at dump time (pg_dump's completion trailer);
* the upgrade refuses rather than proceeding without a snapshot (disk guard);
* retention never deletes a recovery point before its replacement verifies.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from farna.launcher import upgrade as upgrade_module
from farna.launcher.upgrade import (
    RETENTION_COUNT,
    SNAPSHOT_MANIFEST_NAME,
    SNAPSHOT_PREFIX,
    UpgradeError,
    prune_snapshots,
    required_snapshot_free_bytes,
    snapshot_is_intact,
    take_upgrade_snapshot,
    verify_dump_complete,
)

_RESTORE_MANIFEST_NAME = "backup-info.json"

_COMPLETE_PAYLOAD = b"--\n-- PostgreSQL database dump\n--\n\n-- PostgreSQL database dump complete\n\n"
_TRUNCATED_PAYLOAD = b"--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE t (id int);\n"

_DB_URL = "postgresql://owner:secret@127.0.0.1:15432/farna_control"


def _fake_completed_run(payload: bytes) -> Callable[..., MagicMock]:
    def _run(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
        if stdout is not None:
            stdout.write(payload)
        return MagicMock(returncode=0, stderr=b"")

    return _run


def _fake_failed_run(stderr: bytes = b"pg_dump: connection refused") -> Callable[..., MagicMock]:
    def _run(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
        return MagicMock(returncode=1, stderr=stderr)

    return _run


@pytest.fixture
def small_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disk guard's size probe, pinned to a tiny database."""
    monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 1024)


@pytest.fixture(autouse=True)
def _pinned_pg_dump(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the pg_dump resolution so these tests never need a real client.

    The resolution itself (bundled dir -> env dir -> PATH -> refusal) is the
    subject of TestResolvePgDump, which therefore runs against the real
    function.
    """
    if request.node.cls is TestResolvePgDump:
        return
    monkeypatch.setattr(upgrade_module, "_resolve_pg_dump", lambda _bin_dir: "pg_dump")
    from farna.db import plain_dump

    # The compatibility rewrite asks the server for its GUC names; these
    # tests never touch a database.
    monkeypatch.setattr(plain_dump, "server_guc_names", lambda _url, **_kwargs: {"statement_timeout"})


def _take(output_dir: Path, *, payload: bytes = _COMPLETE_PAYLOAD, retain: int = RETENTION_COUNT) -> Any:
    with patch("farna.launcher.upgrade.subprocess.run", new=_fake_completed_run(payload)):
        return take_upgrade_snapshot(_DB_URL, output_dir, retain=retain)


# ---------------------------------------------------------------------------
# Completeness at dump time
# ---------------------------------------------------------------------------


class TestVerifyDumpComplete:
    def test_complete_plain_dump_passes(self, tmp_path: Path) -> None:
        dump = tmp_path / "database.sql"
        dump.write_bytes(_COMPLETE_PAYLOAD)
        assert verify_dump_complete(dump) == len(_COMPLETE_PAYLOAD)

    def test_truncated_dump_is_refused(self, tmp_path: Path) -> None:
        """A dump cut short is a syntactically valid script — only the missing
        trailer proves it never finished."""
        dump = tmp_path / "database.sql"
        dump.write_bytes(_TRUNCATED_PAYLOAD)
        with pytest.raises(UpgradeError, match="INCOMPLETE"):
            verify_dump_complete(dump)

    def test_empty_dump_is_refused(self, tmp_path: Path) -> None:
        dump = tmp_path / "database.sql"
        dump.write_bytes(b"")
        with pytest.raises(UpgradeError, match="EMPTY"):
            verify_dump_complete(dump)

    def test_custom_format_archive_is_refused_by_name(self, tmp_path: Path) -> None:
        dump = tmp_path / "database.sql"
        dump.write_bytes(b"PGDMP" + b"\x00" * 64)
        with pytest.raises(UpgradeError, match="CUSTOM-format"):
            verify_dump_complete(dump)

    def test_missing_dump_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(UpgradeError, match="cannot be read back"):
            verify_dump_complete(tmp_path / "nope.sql")


# ---------------------------------------------------------------------------
# The disk guard
# ---------------------------------------------------------------------------


class TestDiskGuard:
    def test_requirement_is_the_larger_of_floor_and_headroom(self) -> None:
        assert required_snapshot_free_bytes(0) == 2 * 1024**3
        assert required_snapshot_free_bytes(1024**3) == 2 * 1024**3
        # 4 GiB database: 1.25 x 4 GiB = 5 GiB beats the 2 GiB floor.
        assert required_snapshot_free_bytes(4 * 1024**3) == 5 * 1024**3

    def test_refuses_with_required_available_and_nothing_changed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)
        monkeypatch.setattr(upgrade_module.shutil, "disk_usage", lambda _path: type("DU", (), {"free": 1 * 1024**3})())
        with pytest.raises(UpgradeError) as excinfo:
            upgrade_module.enforce_snapshot_disk_headroom(_DB_URL, tmp_path)
        message = str(excinfo.value)
        assert "Refusing to upgrade" in message
        assert "2.0 GB free" in message
        assert "1.0 GB is available" in message
        assert "Free 1.0 GB and re-run" in message
        assert "Nothing has been changed" in message

    def test_passes_when_the_requirement_is_met(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)
        monkeypatch.setattr(upgrade_module.shutil, "disk_usage", lambda _path: type("DU", (), {"free": 2 * 1024**3})())
        assert upgrade_module.enforce_snapshot_disk_headroom(_DB_URL, tmp_path) == 2 * 1024**3

    def test_accepts_an_output_dir_that_does_not_exist_yet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard measures the nearest existing ancestor, so a first run into
        a new /backups directory does not fail on statfs."""
        monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)
        target = tmp_path / "not-yet" / "backups"
        seen: list[str] = []

        def _usage(path: str) -> Any:
            seen.append(path)
            return type("DU", (), {"free": 8 * 1024**3})()

        monkeypatch.setattr(upgrade_module.shutil, "disk_usage", _usage)
        upgrade_module.enforce_snapshot_disk_headroom(_DB_URL, target)
        assert seen == [str(tmp_path)]

    def test_unmeasurable_database_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_url: str, **_kwargs: Any) -> int:
            raise UpgradeError("Refusing to upgrade: cannot measure the database size")

        monkeypatch.setattr(upgrade_module, "_database_size_bytes", _boom)
        with pytest.raises(UpgradeError, match="cannot measure"):
            upgrade_module.enforce_snapshot_disk_headroom(_DB_URL, tmp_path)

    def test_take_refuses_before_writing_anything(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guard runs BEFORE the snapshot directory exists."""
        monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)
        monkeypatch.setattr(upgrade_module.shutil, "disk_usage", lambda _path: type("DU", (), {"free": 0})())
        output_dir = tmp_path / "backups"
        with pytest.raises(UpgradeError, match="Refusing to upgrade"):
            take_upgrade_snapshot(_DB_URL, output_dir)
        assert not list(output_dir.glob(f"{SNAPSHOT_PREFIX}*"))


# ---------------------------------------------------------------------------
# take_upgrade_snapshot
# ---------------------------------------------------------------------------


class TestTakeUpgradeSnapshot:
    def test_writes_both_manifests_checksum_and_heads(self, tmp_path: Path, small_database: None) -> None:
        snapshot = _take(tmp_path)
        assert snapshot.directory.parent == tmp_path
        assert snapshot.directory.name.startswith(SNAPSHOT_PREFIX)
        assert snapshot.dump_path == snapshot.directory / "database.sql"
        assert snapshot.bytes == len(_COMPLETE_PAYLOAD)
        assert snapshot.checksum == hashlib.sha256(_COMPLETE_PAYLOAD).hexdigest()
        assert isinstance(snapshot.schema_heads, list)

        manifest = json.loads((snapshot.directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        assert manifest["snapshot_for_upgrade"] is True
        assert manifest["dump_complete"] is True
        assert manifest["dump_sha256"] == snapshot.checksum
        assert manifest["dump_bytes"] == snapshot.bytes

        restore_manifest = json.loads((snapshot.directory / _RESTORE_MANIFEST_NAME).read_text(encoding="utf-8"))
        # The restore side's contract: checksum coverage for the dump it replays.
        assert restore_manifest["file_checksums"]["database.sql"] == snapshot.checksum
        assert restore_manifest["dump_complete"] is True

    def test_release_token_lands_in_the_directory_name(self, tmp_path: Path, small_database: None) -> None:
        with patch("farna.launcher.upgrade.subprocess.run", new=_fake_completed_run(_COMPLETE_PAYLOAD)):
            snapshot = take_upgrade_snapshot(_DB_URL, tmp_path, release="0.5.0")
        assert snapshot.directory.name.startswith(f"{SNAPSHOT_PREFIX}0.5.0-")

    def test_incomplete_dump_is_swept_and_refused(self, tmp_path: Path, small_database: None) -> None:
        with pytest.raises(UpgradeError, match="INCOMPLETE"):
            _take(tmp_path, payload=_TRUNCATED_PAYLOAD)
        assert not list(tmp_path.glob(f"{SNAPSHOT_PREFIX}*"))

    def test_failed_dump_is_swept_and_refused(self, tmp_path: Path, small_database: None) -> None:
        with patch("farna.launcher.upgrade.subprocess.run", new=_fake_failed_run()), pytest.raises(UpgradeError, match="dump FAILED"):
            take_upgrade_snapshot(_DB_URL, tmp_path)
        assert not list(tmp_path.glob(f"{SNAPSHOT_PREFIX}*"))

    def test_missing_pg_dump_leaves_no_snapshot_directory(self, tmp_path: Path, small_database: None) -> None:
        """Resolving pg_dump happens inside the guarded block, so a missing
        client sweeps the just-created directory instead of leaving an empty one
        that looks like a snapshot."""
        with (
            patch.object(upgrade_module, "_resolve_pg_dump", side_effect=UpgradeError("no pg_dump found")),
            pytest.raises(UpgradeError, match="no pg_dump found"),
        ):
            take_upgrade_snapshot(_DB_URL, tmp_path)
        assert not list(tmp_path.glob(f"{SNAPSHOT_PREFIX}*"))

    def test_incomplete_dump_never_prunes_an_existing_snapshot(self, tmp_path: Path, small_database: None) -> None:
        """ORDERING: a failed dump must not delete anything — the existing
        recovery points are still the only ones the operator has."""
        first = _take(tmp_path)
        assert snapshot_is_intact(first.directory)

        with pytest.raises(UpgradeError):
            _take(tmp_path, payload=_TRUNCATED_PAYLOAD)
        assert first.directory.exists()
        assert snapshot_is_intact(first.directory)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestPruneSnapshots:
    def test_keeps_the_newest_retained_and_deletes_the_rest(self, tmp_path: Path, small_database: None) -> None:
        snapshots = [_take(tmp_path, retain=RETENTION_COUNT) for _ in range(RETENTION_COUNT + 2)]
        # Compare by creation stamp, not by path: within one wall-clock second a
        # pruned directory name can be REUSED by the next snapshot, so a Path
        # from an earlier ref may now point at a newer snapshot.
        created = {snapshot.directory.name: snapshot.created_at for snapshot in snapshots}
        newest_kept = set(sorted(created, key=lambda name: created[name])[-RETENTION_COUNT:])
        survivors = {entry.name for entry in tmp_path.iterdir() if entry.is_dir()}
        assert survivors == newest_kept

    def test_never_deletes_the_snapshot_just_written(self, tmp_path: Path, small_database: None) -> None:
        newest = _take(tmp_path, retain=1)
        pruned = prune_snapshots(tmp_path, retain=1, protect=newest.directory)
        assert pruned == []
        assert newest.directory.exists()

    def test_never_deletes_the_last_surviving_snapshot(self, tmp_path: Path, small_database: None) -> None:
        only = _take(tmp_path)
        assert not prune_snapshots(tmp_path, retain=1)
        assert only.directory.exists()

    def test_sweeps_a_broken_snapshot(self, tmp_path: Path, small_database: None) -> None:
        broken = tmp_path / f"{SNAPSHOT_PREFIX}broken"
        broken.mkdir()
        (broken / "database.sql").write_bytes(_TRUNCATED_PAYLOAD)
        newest = _take(tmp_path, retain=RETENTION_COUNT)
        assert not broken.exists()
        assert newest.directory.exists()

    def test_ignores_non_snapshot_directories(self, tmp_path: Path, small_database: None) -> None:
        unrelated = tmp_path / "pgdata"
        unrelated.mkdir()
        (unrelated / "PG_VERSION").write_text("16", encoding="utf-8")
        _take(tmp_path)
        assert unrelated.exists()

    def test_is_intact_rejects_a_size_mismatch(self, tmp_path: Path, small_database: None) -> None:
        snapshot = _take(tmp_path)
        manifest = json.loads((snapshot.directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["dump_bytes"] = 1
        (snapshot.directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        assert snapshot_is_intact(snapshot.directory) is False

    def test_is_intact_rejects_a_missing_checksum(self, tmp_path: Path, small_database: None) -> None:
        snapshot = _take(tmp_path)
        (snapshot.directory / _RESTORE_MANIFEST_NAME).write_text(json.dumps({"file_checksums": {}}), encoding="utf-8")
        assert snapshot_is_intact(snapshot.directory) is False


class TestDbUrlNormalisation:
    """The compose entrypoint passes SQLAlchemy asyncpg URLs; psycopg and
    pg_dump only accept the libpq form, so both the pristine probe and pg_dump
    must receive ``postgresql://`` (FAR-2667)."""

    _ASYNC_URL = "postgresql+asyncpg://owner:secret@127.0.0.1:15432/farna_control"

    def test_take_normalises_asyncpg_url_for_pg_dump(self, tmp_path: Path, small_database: None) -> None:
        seen: list[list[str]] = []

        def _run(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
            seen.append(argv)
            if stdout is not None:
                stdout.write(_COMPLETE_PAYLOAD)
            return MagicMock(returncode=0, stderr=b"")

        with patch("farna.launcher.upgrade.subprocess.run", new=_run):
            take_upgrade_snapshot(self._ASYNC_URL, tmp_path)
        url_arg = seen[0][-1]
        assert url_arg.startswith("postgresql://")
        assert "asyncpg" not in url_arg

    def test_pristine_probe_receives_the_normalised_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def _pristine(url: str, **_kwargs: Any) -> bool:
            seen.append(url)
            return True

        monkeypatch.setattr(upgrade_module, "_database_is_pristine", _pristine)
        code = upgrade_module.main(["--db-url", self._ASYNC_URL, "--output-dir", str(tmp_path), "--skip-if-pristine"])
        assert code == 0
        assert seen
        assert seen[0].startswith("postgresql://")
        assert "asyncpg" not in seen[0]

    def test_entrypoint_mode_normalises_asyncpg_url_for_pg_dump(self, tmp_path: Path, small_database: None) -> None:
        seen: list[list[str]] = []

        def _run(argv: list[str], stdout: Any = None, **kwargs: Any) -> MagicMock:
            seen.append(argv)
            if stdout is not None:
                stdout.write(_COMPLETE_PAYLOAD)
            return MagicMock(returncode=0, stderr=b"")

        with patch("farna.launcher.upgrade.subprocess.run", new=_run):
            code = upgrade_module.main(["--db-url", self._ASYNC_URL, "--output-dir", str(tmp_path)])
        assert code == 0
        url_arg = seen[0][-1]
        assert url_arg.startswith("postgresql://")
        assert "asyncpg" not in url_arg


class TestResolvePgDump:
    """The container has pg_dump on PATH; the native bundle has its own tree."""

    def test_explicit_bin_dir_wins(self, tmp_path: Path) -> None:
        resolved = upgrade_module._resolve_pg_dump(tmp_path)
        assert resolved.startswith(str(tmp_path))
        assert "pg_dump" in resolved

    def test_env_bin_dir_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FARNA_CONTROL_BUNDLED_BIN_DIR", str(tmp_path))
        resolved = upgrade_module._resolve_pg_dump(None)
        assert resolved.startswith(str(tmp_path))

    def test_falls_back_to_path_when_no_bundle_ships_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FARNA_CONTROL_BUNDLED_BIN_DIR", raising=False)
        monkeypatch.setattr(upgrade_module, "_resolve_pg_bin_dir", lambda _bin_dir: tmp_path / "no-bundle")
        monkeypatch.setattr(upgrade_module.shutil, "which", lambda _name: "/usr/bin/pg_dump")
        assert upgrade_module._resolve_pg_dump(None) == "/usr/bin/pg_dump"

    def test_refuses_when_no_pg_dump_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("FARNA_CONTROL_BUNDLED_BIN_DIR", raising=False)
        monkeypatch.setattr(upgrade_module, "_resolve_pg_bin_dir", lambda _bin_dir: tmp_path / "no-bundle")
        monkeypatch.setattr(upgrade_module.shutil, "which", lambda _name: None)
        with pytest.raises(UpgradeError, match="no pg_dump found"):
            upgrade_module._resolve_pg_dump(None)


class TestEntrypointMode:
    """The container path's CLI contract: one line on stdout, nothing else."""

    def test_db_url_mode_prints_only_the_snapshot_path(self, tmp_path: Path, small_database: None, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("farna.launcher.upgrade.subprocess.run", new=_fake_completed_run(_COMPLETE_PAYLOAD)):
            code = upgrade_module.main(["--db-url", _DB_URL, "--output-dir", str(tmp_path)])
        captured = capsys.readouterr()
        assert code == 0
        lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(lines) == 1
        assert Path(lines[0]).is_dir()

    def test_pristine_database_skips_the_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(upgrade_module, "_database_is_pristine", lambda _url, **_kwargs: True)
        code = upgrade_module.main(["--db-url", _DB_URL, "--output-dir", str(tmp_path), "--skip-if-pristine"])
        captured = capsys.readouterr()
        assert code == 0
        assert not captured.out.strip()
        assert "no pre-upgrade snapshot needed" in captured.err
        assert not list(tmp_path.glob(f"{SNAPSHOT_PREFIX}*"))

    def test_failed_snapshot_exits_non_zero(self, tmp_path: Path, small_database: None) -> None:
        with patch("farna.launcher.upgrade.subprocess.run", new=_fake_failed_run()):
            code = upgrade_module.main(["--db-url", _DB_URL, "--output-dir", str(tmp_path)])
        assert code == 1

    def test_requires_exactly_one_mode(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            upgrade_module.main(["--output-dir", str(tmp_path)])
        with pytest.raises(SystemExit):
            upgrade_module.main(["--data-dir", str(tmp_path), "--db-url", _DB_URL, "--output-dir", str(tmp_path)])
