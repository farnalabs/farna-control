"""FAR-2667: the pre-upgrade snapshot's db probes, integrity branches and edge paths.

This complements ``test_upgrade_snapshot.py``. The changed-lines coverage gate
measures the review-hardening lines in :mod:`farna.launcher.upgrade`; the cases
here reach the branches the happy-path suite stubs out or never builds:

* the live pg probes (database size, recorded revisions, pristine check) and
  their fail-closed arms;
* every ``snapshot_is_intact`` refusal branch, because retention DELETES on that
  answer;
* the retention helpers' unreadable-directory arms;
* the snapshot-directory collision, chmod-failure and dump-error arms;
* the bundle migration-tree resolvers and the CLI argument contract.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from farna.db import plain_dump
from farna.launcher import upgrade as upgrade_module
from farna.launcher.upgrade import (
    SNAPSHOT_MANIFEST_NAME,
    SNAPSHOT_PREFIX,
    UpgradeError,
    _bundle_alembic_heads,
    _bundle_alembic_revisions,
    _database_revisions,
    _database_size_bytes,
    _recorded_heads,
    _retained_snapshot_bytes,
    _snapshot_bytes,
    _snapshot_created_at,
    prune_snapshots,
    snapshot_in_progress,
    snapshot_is_intact,
    take_upgrade_snapshot,
)

_RESTORE_MANIFEST_NAME = "backup-info.json"
_DUMP_NAME = "database.sql"
_DB_URL = "postgresql://owner:secret@127.0.0.1:15432/farna_control"
_COMPLETE_PAYLOAD = b"--\n-- PostgreSQL database dump\n--\n\n-- PostgreSQL database dump complete\n\n"


def _context(value: Any) -> Any:
    class _Ctx:
        def __enter__(self) -> Any:
            return value

        def __exit__(self, *_exc: object) -> None:
            return None

    return _Ctx()


class _Cursor:
    def __init__(self, *, one: Any = None, all_rows: list[Any] | None = None) -> None:
        self._one = one
        self._all = all_rows or []

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchone(self) -> Any:
        return self._one

    def fetchall(self) -> list[Any]:
        return self._all


def _install_psycopg(monkeypatch: pytest.MonkeyPatch, cursor: _Cursor, *, raises: Exception | None = None) -> None:
    def _connect(*_args: object, **_kwargs: object) -> Any:
        if raises is not None:
            raise raises
        return _context(SimpleNamespace(cursor=lambda: _context(cursor)))

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=_connect))


def _intact_snapshot(root: Path, name: str = f"{SNAPSHOT_PREFIX}20260101-000000", *, dump_payload: bytes = _COMPLETE_PAYLOAD) -> Path:
    import hashlib

    directory = root / name
    directory.mkdir(parents=True)
    dump = directory / _DUMP_NAME
    dump.write_bytes(dump_payload)
    checksum = hashlib.sha256(dump_payload).hexdigest()
    (directory / SNAPSHOT_MANIFEST_NAME).write_text(
        json.dumps({"snapshot_for_upgrade": True, "dump_name": _DUMP_NAME, "dump_bytes": len(dump_payload), "dump_sha256": checksum, "schema_heads": ["head1"]}),
        encoding="utf-8",
    )
    (directory / _RESTORE_MANIFEST_NAME).write_text(json.dumps({"file_checksums": {_DUMP_NAME: checksum}, "schema_versions": ["head1"]}), encoding="utf-8")
    return directory


class TestLiveDbProbes:
    def test_database_size_returns_the_query_figure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(4096,)))
        assert _database_size_bytes(_DB_URL) == 4096

    def test_database_size_fails_closed_on_connect_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(1,)), raises=RuntimeError("no route to host"))
        with pytest.raises(UpgradeError, match="cannot measure the database size"):
            _database_size_bytes(_DB_URL)

    def test_database_size_fails_closed_on_an_unusable_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=("not-a-number",)))
        with pytest.raises(UpgradeError, match="no usable figure"):
            _database_size_bytes(_DB_URL)

    def test_database_revisions_returns_sorted_distinct_revisions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(True,), all_rows=[("rev_b",), (None,), ("rev_a",)]))
        assert _database_revisions(_DB_URL) == ["rev_a", "rev_b"]

    def test_database_revisions_reads_a_fresh_database_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(False,)))
        assert not _database_revisions(_DB_URL)

    def test_database_revisions_reads_an_unreadable_db_as_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(), raises=RuntimeError("boom"))
        assert _database_revisions(_DB_URL) == ["unknown"]

    def test_pristine_true_when_no_public_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(0,)))
        assert upgrade_module._database_is_pristine(_DB_URL) is True

    def test_pristine_false_when_tables_exist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=(3,)))
        assert upgrade_module._database_is_pristine(_DB_URL) is False

    def test_pristine_false_on_probe_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(), raises=RuntimeError("boom"))
        assert upgrade_module._database_is_pristine(_DB_URL) is False

    def test_pristine_false_on_an_unusable_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_psycopg(monkeypatch, _Cursor(one=("0",)))
        assert upgrade_module._database_is_pristine(_DB_URL) is False


class TestSnapshotIsIntactBranches:
    def test_snapshot_manifest_without_a_restore_manifest_is_not_intact(self, tmp_path: Path) -> None:
        directory = tmp_path / f"{SNAPSHOT_PREFIX}20260101-000000"
        directory.mkdir()
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps({"snapshot_for_upgrade": True}), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_unparseable_restore_manifest_is_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        (directory / _RESTORE_MANIFEST_NAME).write_text("not json", encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_non_object_manifests_are_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(["a-list"]), encoding="utf-8")
        (directory / _RESTORE_MANIFEST_NAME).write_text(json.dumps(["a-list"]), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_a_snapshot_not_marked_for_upgrade_is_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        manifest = json.loads((directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["snapshot_for_upgrade"] = False
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_a_non_string_dump_name_is_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        manifest = json.loads((directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["dump_name"] = 7
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_a_restore_manifest_without_the_dump_checksum_is_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        (directory / _RESTORE_MANIFEST_NAME).write_text(json.dumps({"file_checksums": {"other.sql": "abc"}}), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_manifests_that_disagree_on_the_checksum_are_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        manifest = json.loads((directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["dump_sha256"] = "f" * 64
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        assert snapshot_is_intact(directory) is False

    def test_a_missing_dump_file_is_not_intact(self, tmp_path: Path) -> None:
        directory = _intact_snapshot(tmp_path)
        (directory / _DUMP_NAME).unlink()
        assert snapshot_is_intact(directory) is False

    def test_a_corrupted_dump_with_a_matching_declared_checksum_is_not_intact(self, tmp_path: Path) -> None:
        """Both manifests agree, but the bytes no longer hash to that value."""
        directory = _intact_snapshot(tmp_path)
        (directory / _DUMP_NAME).write_bytes(_COMPLETE_PAYLOAD + b"-- appended junk\n")
        assert snapshot_is_intact(directory) is False

    def test_a_truncated_dump_is_not_intact(self, tmp_path: Path) -> None:
        payload = b"--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE t (id int);\n"
        directory = _intact_snapshot(tmp_path, dump_payload=payload)
        assert snapshot_is_intact(directory) is False

    def test_an_intact_snapshot_is_accepted(self, tmp_path: Path) -> None:
        assert snapshot_is_intact(_intact_snapshot(tmp_path)) is True


class TestRetentionHelpers:
    def test_created_at_is_empty_when_the_directory_is_gone(self, tmp_path: Path) -> None:
        assert not _snapshot_created_at(tmp_path / "absent")

    def test_created_at_falls_back_to_the_directory_mtime(self, tmp_path: Path) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        stamp = _snapshot_created_at(directory)
        assert stamp.startswith("20")

    def test_recorded_heads_skips_a_missing_restore_manifest(self, tmp_path: Path) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps({"schema_versions": ["head-a"]}), encoding="utf-8")
        assert _recorded_heads(directory) == ["head-a"]

    def test_recorded_heads_skips_an_unparseable_first_manifest(self, tmp_path: Path) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        (directory / _RESTORE_MANIFEST_NAME).write_text("not json", encoding="utf-8")
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps({"schema_versions": ["head-b"]}), encoding="utf-8")
        assert _recorded_heads(directory) == ["head-b"]

    def test_recorded_heads_is_empty_when_no_manifest_records_heads(self, tmp_path: Path) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        (directory / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps({"schema_versions": []}), encoding="utf-8")
        assert not _recorded_heads(directory)

    def test_unknown_heads_are_treated_as_different(self) -> None:
        assert upgrade_module._heads_differ_from_database(["unknown"], ["head1"]) is True

    def test_unrecorded_heads_are_treated_as_the_same(self) -> None:
        assert upgrade_module._heads_differ_from_database([], ["head1"]) is False

    def test_missing_current_heads_are_treated_as_the_same(self) -> None:
        assert upgrade_module._heads_differ_from_database(["head1"], None) is False

    def test_snapshot_bytes_skips_an_entry_that_cannot_be_statted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        (directory / "boom.txt").write_bytes(b"x")
        real_stat = Path.stat

        def _stat(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path.name == "boom.txt":
                raise OSError("permission denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _stat)
        assert _snapshot_bytes(directory) == 0

    def test_retained_bytes_is_zero_when_the_directory_cannot_be_listed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_path: Path) -> list[Path]:
            raise OSError("permission denied")

        monkeypatch.setattr(upgrade_module, "_snapshot_dirs", _boom)
        assert _retained_snapshot_bytes(tmp_path, ["head1"]) == 0

    def test_prune_on_a_missing_output_dir_is_a_noop(self, tmp_path: Path) -> None:
        assert not prune_snapshots(tmp_path / "absent")

    def test_in_progress_marker_unreadable_is_not_in_flight(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        directory = tmp_path / "snap"
        directory.mkdir()
        (directory / upgrade_module.SNAPSHOT_IN_PROGRESS_NAME).write_text("123", encoding="utf-8")
        real_read_text = Path.read_text

        def _read_text(path: Path, *args: Any, **kwargs: Any) -> str:
            if path.name == upgrade_module.SNAPSHOT_IN_PROGRESS_NAME:
                raise OSError("permission denied")
            return real_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _read_text)
        assert snapshot_in_progress(directory) is False


class _FixedDateTime:
    """A datetime whose ``now`` is pinned so a snapshot name collides."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        return datetime(2026, 1, 1, tzinfo=tz)

    @classmethod
    def fromtimestamp(cls, timestamp: float, tz: Any = None) -> Any:
        return datetime.fromtimestamp(timestamp, tz)


def _pin_snapshot_environment(monkeypatch: pytest.MonkeyPatch, output_dir: Path) -> None:
    monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)
    monkeypatch.setattr(upgrade_module, "_database_revisions", lambda _url, **_kwargs: ["head1"])
    monkeypatch.setattr(upgrade_module, "_resolve_pg_dump", lambda _bin_dir: "pg_dump")
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=8 * 1024**3))


class TestTakeSnapshotEdgeArms:
    def test_a_colliding_directory_name_gets_a_counter_suffix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        output_dir = tmp_path / "backups"
        output_dir.mkdir()
        (output_dir / f"{SNAPSHOT_PREFIX}20260101-000000").mkdir()
        _pin_snapshot_environment(monkeypatch, output_dir)
        monkeypatch.setattr(upgrade_module, "datetime", _FixedDateTime)

        def _dump(_url: str, dump_path: Path, **_kwargs: object) -> int:
            dump_path.write_bytes(_COMPLETE_PAYLOAD)
            return len(_COMPLETE_PAYLOAD)

        monkeypatch.setattr(plain_dump, "dump_plain_sql", _dump)
        snapshot = take_upgrade_snapshot(_DB_URL, output_dir)
        assert snapshot.directory.name == f"{SNAPSHOT_PREFIX}20260101-000000-1"

    def test_a_chmod_failure_only_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        output_dir = tmp_path / "backups"
        _pin_snapshot_environment(monkeypatch, output_dir)

        def _dump(_url: str, dump_path: Path, **_kwargs: object) -> int:
            dump_path.write_bytes(_COMPLETE_PAYLOAD)
            return len(_COMPLETE_PAYLOAD)

        monkeypatch.setattr(plain_dump, "dump_plain_sql", _dump)

        def _chmod(_path: Path, _mode: int) -> None:
            raise OSError("chmod not permitted")

        monkeypatch.setattr(Path, "chmod", _chmod)
        snapshot = take_upgrade_snapshot(_DB_URL, output_dir)
        assert snapshot_is_intact(snapshot.directory)

    def test_a_dump_raising_upgrade_error_is_swept_and_reraised(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        output_dir = tmp_path / "backups"
        _pin_snapshot_environment(monkeypatch, output_dir)

        def _dump(_url: str, _dump_path: Path, **_kwargs: object) -> int:
            raise UpgradeError("pg_dump not found")

        monkeypatch.setattr(plain_dump, "dump_plain_sql", _dump)
        with pytest.raises(UpgradeError, match="pg_dump not found"):
            take_upgrade_snapshot(_DB_URL, output_dir)
        assert not list(output_dir.glob(f"{SNAPSHOT_PREFIX}*"))

    def test_a_dump_raising_os_error_becomes_an_output_dir_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        output_dir = tmp_path / "backups"
        _pin_snapshot_environment(monkeypatch, output_dir)

        def _dump(_url: str, _dump_path: Path, **_kwargs: object) -> int:
            raise OSError("no space left on device")

        monkeypatch.setattr(plain_dump, "dump_plain_sql", _dump)
        with pytest.raises(UpgradeError, match="no space left on device"):
            take_upgrade_snapshot(_DB_URL, output_dir)
        assert not list(output_dir.glob(f"{SNAPSHOT_PREFIX}*"))


class TestResolvePgDumpBundle:
    def test_a_bundled_binary_is_preferred_over_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        (bundled / "pg_dump").write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.delenv("FARNA_CONTROL_BUNDLED_BIN_DIR", raising=False)
        monkeypatch.setattr(upgrade_module, "_resolve_pg_bin_dir", lambda _bin_dir: bundled)
        assert upgrade_module._resolve_pg_dump(None) == str(bundled / "pg_dump")


class TestBundleMigrationTree:
    def test_revisions_is_empty_when_the_tree_is_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_bundle_script_directory", lambda _bundle: None)
        assert not _bundle_alembic_revisions(tmp_path)
        assert not _bundle_alembic_heads(tmp_path)

    def test_revisions_and_heads_come_from_the_script_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        script = SimpleNamespace(
            walk_revisions=lambda: [SimpleNamespace(revision="rev_a"), SimpleNamespace(revision="rev_b")],
            get_heads=lambda: ["head_x"],
        )
        monkeypatch.setattr(upgrade_module, "_bundle_script_directory", lambda _bundle: script)
        assert _bundle_alembic_revisions(tmp_path) == {"rev_a", "rev_b"}
        assert _bundle_alembic_heads(tmp_path) == {"head_x"}


class TestMainArgumentContract:
    def test_db_url_without_output_dir_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            upgrade_module.main(["--db-url", _DB_URL])

    def test_both_sources_at_once_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            upgrade_module.main(["--db-url", _DB_URL, "--output-dir", str(tmp_path), "--data-dir", str(tmp_path)])


class TestDiskUsageUnreadable:
    def test_an_unreadable_output_filesystem_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_database_size_bytes", lambda _url, **_kwargs: 0)

        def _boom(_path: str) -> Any:
            raise OSError("statvfs failed")

        monkeypatch.setattr(shutil, "disk_usage", _boom)
        with pytest.raises(UpgradeError, match="cannot measure free space"):
            upgrade_module.enforce_snapshot_disk_headroom(_DB_URL, tmp_path)
