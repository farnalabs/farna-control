"""FAR-2667: the backup/restore guards' error and edge branches.

This complements ``test_backup.py`` and ``test_backup_restore_semantics.py``.
The changed-lines coverage gate measures the restore-hardening lines in
:mod:`farna.cli.backup`; the cases here reach the branches the CLI-level suite
never builds:

* stopping a live launcher (stale record, success, and both failure shapes);
* the pre-flight target resolver's server-data-directory arm;
* the migration-tree readers and the schema-downgrade / target-identity gates;
* the password redactor's unparseable-URL arm;
* the archive-member resolver's symlink and escape refusals.
"""

from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import pytest

from farna.cli import backup as backup_mod


def _context(value: Any) -> Any:
    class _Ctx:
        def __enter__(self) -> Any:
            return value

        def __exit__(self, *_exc: object) -> None:
            return None

    return _Ctx()


class TestStopLauncherWriters:
    def test_a_stale_lock_record_is_not_a_running_launcher(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: False)
        assert backup_mod._stop_launcher_writers(tmp_path) is False

    def test_a_live_launcher_is_stopped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: True)
        monkeypatch.setattr("farna.launcher.upgrade.stop_running_stack", lambda _data_dir: None)
        assert backup_mod._stop_launcher_writers(tmp_path) is True

    def test_an_upgrade_error_during_stop_refuses_the_restore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.launcher.upgrade import UpgradeError

        def _stop(_data_dir: Path) -> None:
            raise UpgradeError("still running")

        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: True)
        monkeypatch.setattr("farna.launcher.upgrade.stop_running_stack", _stop)
        with pytest.raises(click.ClickException, match="still running after a stop request"):
            backup_mod._stop_launcher_writers(tmp_path)

    def test_an_unexpected_stop_error_refuses_the_restore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stop(_data_dir: Path) -> None:
            raise RuntimeError("no stop seam")

        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: True)
        monkeypatch.setattr("farna.launcher.upgrade.stop_running_stack", _stop)
        with pytest.raises(click.ClickException, match="cannot be stopped from here"):
            backup_mod._stop_launcher_writers(tmp_path)


class TestSafetyDumpNameCollision:
    def test_a_taken_name_is_retried_until_free(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        class _FixedDate:
            @classmethod
            def now(cls, _tz: Any = None) -> Any:
                from datetime import datetime as real_datetime

                return real_datetime(2026, 1, 1, tzinfo=None)

        monkeypatch.setattr(backup_mod, "datetime", _FixedDate)
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/pg_dump")
        taken = tmp_path / f"{backup_mod._SAFETY_DUMP_PREFIX}20260101-000000-1000"
        taken.mkdir()
        suffixes = iter([1000, 1001])
        monkeypatch.setattr(random, "randint", lambda _a, _b: next(suffixes))
        monkeypatch.setattr(backup_mod, "_run_pg_dump", lambda *_args, **_kwargs: None)
        safety = backup_mod._take_safety_dump("postgresql://owner@db/x", output_dir=tmp_path)
        assert safety is not None
        assert safety.name == f"{backup_mod._SAFETY_DUMP_PREFIX}20260101-000000-1001"


class TestResolvePreflightTarget:
    def test_the_server_data_directory_is_used_when_visible(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        server_dir = tmp_path / "pgdata"
        server_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_server_data_directory", lambda _url: str(server_dir))
        assert backup_mod._resolve_preflight_target(tmp_path, None, None, "postgresql://owner@db/x") == server_dir

    def test_no_source_falls_back_to_the_archive_filesystem(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert backup_mod._resolve_preflight_target(tmp_path, None, None, None) == tmp_path
        assert "measures the ARCHIVE filesystem" in capsys.readouterr().err


def _alembic_stubs(*, revisions: list[str] | None = None, ancestry: set[str] | None = None, raises: bool = False) -> dict[str, Any]:
    if raises:

        def _from_config(_config: object) -> Any:
            raise RuntimeError("bad migration tree")

    else:

        def _from_config(_config: object) -> Any:
            return SimpleNamespace(
                walk_revisions=lambda: [SimpleNamespace(revision=rev) for rev in (revisions or [])],
                iterate_revisions=lambda *_args: [SimpleNamespace(revision=rev) for rev in (ancestry or set())],
            )

    return {
        "alembic.config": SimpleNamespace(Config=lambda _path: object()),
        "alembic.script": SimpleNamespace(ScriptDirectory=SimpleNamespace(from_config=_from_config)),
    }


class TestKnownRevisions:
    def test_missing_alembic_ini_is_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        assert backup_mod._known_revisions() is None

    def test_every_revision_is_listed_and_sorted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "alembic.ini").write_text("", encoding="utf-8")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            for name, module in _alembic_stubs(revisions=["rev_b", "rev_a", "rev_a"]).items():
                mp.setitem(sys.modules, name, module)
            assert backup_mod._known_revisions() == ["rev_a", "rev_b"]

    def test_an_unreadable_tree_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "alembic.ini").write_text("", encoding="utf-8")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            for name, module in _alembic_stubs(raises=True).items():
                mp.setitem(sys.modules, name, module)
            assert backup_mod._known_revisions() is None


class TestCheckSchemaDowngrade:
    def test_a_legacy_manifest_is_only_warned_about(self, capsys: pytest.CaptureFixture[str]) -> None:
        backup_mod._check_schema_downgrade({})
        assert "legacy archive" in capsys.readouterr().err

    def test_an_unknown_revision_is_only_warned_about(self, capsys: pytest.CaptureFixture[str]) -> None:
        backup_mod._check_schema_downgrade({"manifest_version": 2, "schema_versions": ["unknown"]})
        assert "no verifiable schema version" in capsys.readouterr().err

    def test_an_unreadable_tree_is_only_warned_about(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(backup_mod, "_known_revisions", lambda: None)
        backup_mod._check_schema_downgrade({"manifest_version": 2, "schema_versions": ["rev_a"]})
        assert "cannot read this installation's migration tree" in capsys.readouterr().err

    def test_a_newer_archive_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_known_revisions", lambda: ["rev_a"])
        with pytest.raises(click.ClickException, match="does not ship"):
            backup_mod._check_schema_downgrade({"manifest_version": 2, "schema_versions": ["rev_future"]})

    def test_an_older_archive_rolls_forward(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(backup_mod, "_known_revisions", lambda: ["rev_a", "rev_b"])
        monkeypatch.setattr(backup_mod, "_get_schema_versions", lambda: ["rev_b"])
        backup_mod._check_schema_downgrade({"manifest_version": 2, "schema_versions": ["rev_a"]})
        assert "migrates it forward" in capsys.readouterr().err


class _TargetCursor:
    def __init__(self, *, present: Any, versions: list[Any]) -> None:
        self._present = present
        self._versions = versions

    def execute(self, *_args: object) -> None:
        return None

    def fetchone(self) -> Any:
        return self._present

    def fetchall(self) -> list[Any]:
        return self._versions


class TestReadTargetRevisions:
    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch, cursor: _TargetCursor) -> None:
        conn = SimpleNamespace(cursor=lambda: _context(cursor))
        monkeypatch.setattr(backup_mod, "_connect", lambda *_args, **_kwargs: _context(conn))

    def test_revisions_are_sorted_and_distinct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _TargetCursor(present=(True,), versions=[("r2",), (None,), ("r1",)]))
        assert backup_mod._read_target_revisions("postgresql://x") == ["r1", "r2"]

    def test_an_absent_table_reads_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _TargetCursor(present=(False,), versions=[]))
        assert not backup_mod._read_target_revisions("postgresql://x")

    def test_a_missing_probe_row_reads_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._install(monkeypatch, _TargetCursor(present=None, versions=[]))
        assert not backup_mod._read_target_revisions("postgresql://x")


class TestKnownRevisionAncestry:
    def test_missing_alembic_ini_is_unreadable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        assert backup_mod._known_revision_ancestry("rev_a") is None

    def test_the_full_lineage_is_returned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "alembic.ini").write_text("", encoding="utf-8")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            for name, module in _alembic_stubs(ancestry={"rev_a", "rev_b"}).items():
                mp.setitem(sys.modules, name, module)
            assert backup_mod._known_revision_ancestry("rev_b") == {"rev_a", "rev_b"}

    def test_an_unreadable_tree_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "alembic.ini").write_text("", encoding="utf-8")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            for name, module in _alembic_stubs(raises=True).items():
                mp.setitem(sys.modules, name, module)
            assert backup_mod._known_revision_ancestry("rev_b") is None


class TestTargetDescendsFromArchive:
    def test_an_unverifiable_lineage_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_known_revision_ancestry", lambda _rev: None)
        assert backup_mod._target_descends_from_archive(["target"], ["head1"]) is None

    def test_an_unrelated_revision_is_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_known_revision_ancestry", lambda _rev: {"other"})
        assert backup_mod._target_descends_from_archive(["target"], ["head1"]) is False

    def test_a_descended_revision_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backup_mod, "_known_revision_ancestry", lambda _rev: {"head1", "target"})
        assert backup_mod._target_descends_from_archive(["target"], ["head1"]) is True


class TestCheckTargetSchemaMatches:
    def test_an_archive_without_a_schema_record_is_refused(self) -> None:
        with pytest.raises(click.ClickException, match="no verifiable schema version"):
            backup_mod._check_target_schema_matches({"schema_versions": []}, "postgresql://x", False)

    def test_an_unreadable_target_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _read(_url: str) -> list[str]:
            raise RuntimeError("connect failed")

        monkeypatch.setattr(backup_mod, "_read_target_revisions", _read)
        with pytest.raises(click.ClickException, match="Nothing has been changed"):
            backup_mod._check_target_schema_matches({"schema_versions": ["rev_a"]}, "postgresql://x", False)

    def test_matching_revisions_are_verified(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(backup_mod, "_read_target_revisions", lambda _url: ["rev_a"])
        backup_mod._check_target_schema_matches({"schema_versions": ["rev_a"]}, "postgresql://x", False)
        assert "Target schema verified" in capsys.readouterr().out

    def test_a_target_ahead_of_the_snapshot_is_the_rollback(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(backup_mod, "_read_target_revisions", lambda _url: ["rev_next"])
        monkeypatch.setattr(backup_mod, "_target_descends_from_archive", lambda _targets, _heads: True)
        backup_mod._check_target_schema_matches({"schema_versions": ["rev_a"]}, "postgresql://x", False)
        assert "ROLLS THOSE MIGRATIONS BACK" in capsys.readouterr().err


class TestRedactPassword:
    def test_an_unparseable_url_returns_the_text_unchanged(self) -> None:
        assert backup_mod._redact_password("some error text", "postgresql://owner:secret@db:not-a-port/x") == "some error text"

    def test_the_password_is_replaced(self) -> None:
        assert backup_mod._redact_password("failed for owner:secret@db", "postgresql://owner:secret@db/x") == "failed for owner:***@db"


class TestArchiveMember:
    def test_a_symlink_entry_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "outside.txt"
        target.write_text("secret", encoding="utf-8")
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "link.txt").symlink_to(target)
        with pytest.raises(click.ClickException, match="is a symlink"):
            backup_mod._archive_member(archive, "link.txt")

    def test_a_resolved_path_outside_the_archive_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "file.txt").write_text("data", encoding="utf-8")
        outside = tmp_path / "outside"
        real_resolve = Path.resolve

        def _resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
            if path.name == "file.txt":
                return outside
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _resolve)
        with pytest.raises(click.ClickException, match="path outside the archive"):
            backup_mod._archive_member(archive, "file.txt")
