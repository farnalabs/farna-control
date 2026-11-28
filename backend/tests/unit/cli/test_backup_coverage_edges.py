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
        # The verified-liveness seam is POSIX-only; pin it so the assertion
        # is the same on every developer platform (the sibling suite does this too).
        monkeypatch.setattr(backup_mod.os, "name", "posix")
        assert backup_mod._stop_launcher_writers(tmp_path) is True

    def test_an_upgrade_error_during_stop_refuses_the_restore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.launcher.upgrade import UpgradeError

        def _stop(_data_dir: Path) -> None:
            raise UpgradeError("still running")

        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: True)
        monkeypatch.setattr("farna.launcher.upgrade.stop_running_stack", _stop)
        # The verified-liveness seam is POSIX-only; pin it so the assertion
        # is the same on every developer platform (the sibling suite does this too).
        monkeypatch.setattr(backup_mod.os, "name", "posix")
        with pytest.raises(click.ClickException, match="still running after a stop request"):
            backup_mod._stop_launcher_writers(tmp_path)

    def test_an_unexpected_stop_error_refuses_the_restore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def _stop(_data_dir: Path) -> None:
            raise RuntimeError("no stop seam")

        monkeypatch.setattr("farna.launcher.supervisor._read_lock_holder", lambda _path: SimpleNamespace(pid=4321))
        monkeypatch.setattr("farna.launcher.upgrade._pid_alive", lambda _pid: True)
        monkeypatch.setattr("farna.launcher.upgrade.stop_running_stack", _stop)
        # The verified-liveness seam is POSIX-only; pin it so the assertion
        # is the same on every developer platform (the sibling suite does this too).
        monkeypatch.setattr(backup_mod.os, "name", "posix")
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


class TestRedactSecrets:
    def test_an_unparseable_url_leaves_the_text_alone(self) -> None:
        assert backup_mod._redact_url_secrets("some error text", "postgresql://owner:secret@db:not-a-port/x") == "some error text"

    def test_the_url_userinfo_is_replaced(self) -> None:
        assert backup_mod._redact_url_secrets("failed for postgresql://owner:secret@db/x", "postgresql://owner:secret@db/x") == "failed for postgresql://owner:***@db/x"

    def test_a_password_with_a_space_is_replaced(self) -> None:
        assert backup_mod._redact_secrets("boom proof pa ss", "proof pa ss") == "boom ***"

    def test_a_bad_percent_escape_is_replaced_in_both_forms(self) -> None:
        assert backup_mod._redact_secrets("raw pa%zzword encoded pa%25zzword", "pa%zzword") == "raw *** encoded ***"


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


class TestStopBundledClusterOnWindows:
    def test_windows_skips_the_sigint_rung_and_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SIGINT cannot reach a child on Windows, so the fast rung is skipped."""
        from unittest.mock import MagicMock

        monkeypatch.setattr(sys, "platform", "win32")
        process = MagicMock()
        process.poll.return_value = None

        backup_mod._stop_bundled_cluster(process, Path(backup_mod._RESTORE_PG_LOG))

        process.send_signal.assert_not_called()
        process.wait.assert_called_once()


class TestStopIdentifiedPid:
    def test_a_non_posix_host_refuses_rather_than_pretend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")

        assert backup_mod._stop_identified_pid(4242) is False

    def test_a_pid_that_survives_every_signal_is_reported_not_stopped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exhausting SIGINT/SIGTERM/SIGKILL must report the truth, not assume success."""
        from farna.launcher import supervisor as supervisor_module

        ticks = [0.0]

        def _monotonic() -> float:
            ticks[0] += 1_000_000.0
            return ticks[0]

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(backup_mod.os, "kill", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(backup_mod.time, "monotonic", _monotonic)

        assert backup_mod._stop_identified_pid(4242) is False


def _write_window_record(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int = 4321,
    start_epoch: float = 1_000.0,
) -> Path:
    """Write the restore-window record through the REAL writer, pinning its start time."""
    from farna.launcher import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "_pid_starttime_epoch", lambda _pid: start_epoch)
    return supervisor_module.write_restore_window_pidfile(data_dir, pid)


class TestReapLeftoverWindowPostmaster:
    """FAR-2753: signal a process ONLY when its recorded identity is verified.

    The consequence this pins: a reused pid that now belongs to any live
    postgres must never be fast-shutdown / SIGTERMed / SIGKILLed by a restore,
    and a record that cannot be identified is reported (file, pid, what to do)
    instead of killed.
    """

    @staticmethod
    def _identity(
        monkeypatch: pytest.MonkeyPatch,
        *,
        alive: bool = True,
        start_epoch: float = 1_000.0,
        is_postgres: bool = True,
    ) -> None:
        from farna.launcher import supervisor as supervisor_module

        monkeypatch.setattr(supervisor_module, "_pid_alive", lambda _pid: alive)
        monkeypatch.setattr(supervisor_module, "_pid_starttime_epoch", lambda _pid: start_epoch)
        monkeypatch.setattr(supervisor_module, "_is_postgres_process", lambda _pid: is_postgres)

    @staticmethod
    def _recorded_stops(monkeypatch: pytest.MonkeyPatch, *, stopped: bool = True) -> list[int]:
        stopped_pids: list[int] = []

        def _stop(pid: int, **_kwargs: object) -> bool:
            stopped_pids.append(pid)
            return stopped

        monkeypatch.setattr(backup_mod, "_stop_identified_pid", _stop)
        return stopped_pids

    def test_a_genuine_record_is_reaped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.launcher import supervisor as supervisor_module

        _write_window_record(tmp_path, monkeypatch)
        self._identity(monkeypatch)
        stopped = self._recorded_stops(monkeypatch)

        backup_mod._reap_leftover_window_postmaster(tmp_path)

        assert stopped == [4321]
        assert "PID 4321" in capsys.readouterr().out
        assert not (tmp_path / supervisor_module.RESTORE_WINDOW_PIDFILE).exists()

    def test_a_reused_pid_belonging_to_another_postgres_is_not_killed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The defect: the number is alive and postgres, but it is NOT ours."""
        from farna.launcher import supervisor as supervisor_module

        _write_window_record(tmp_path, monkeypatch)  # recorded when the pid started at 1000.0
        self._identity(monkeypatch, start_epoch=9_000.0)  # a different postgres now owns the pid
        stopped = self._recorded_stops(monkeypatch)

        backup_mod._reap_leftover_window_postmaster(tmp_path)

        assert stopped == [], "a reused pid must never be signalled"
        assert not (tmp_path / supervisor_module.RESTORE_WINDOW_PIDFILE).exists(), "a provably different process is stale debris"

    def test_a_legacy_bare_pid_record_is_reported_and_not_killed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.launcher import supervisor as supervisor_module

        record = tmp_path / supervisor_module.RESTORE_WINDOW_PIDFILE
        record.write_text("4321\n", encoding="ascii")
        self._identity(monkeypatch)
        stopped = self._recorded_stops(monkeypatch)

        with pytest.raises(click.ClickException) as caught:
            backup_mod._reap_leftover_window_postmaster(tmp_path)

        message = str(caught.value)
        assert stopped == []
        assert str(record) in message
        assert "4321" in message
        assert "cannot be verified" in message
        assert record.exists(), "the only handle on that process is kept"

    def test_a_record_written_for_another_data_dir_is_not_acted_on(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.launcher import supervisor as supervisor_module

        other = tmp_path / "another-install"
        other.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        copied = _write_window_record(other, monkeypatch).read_text(encoding="ascii")
        (data_dir / supervisor_module.RESTORE_WINDOW_PIDFILE).write_text(copied, encoding="ascii")
        self._identity(monkeypatch)
        stopped = self._recorded_stops(monkeypatch)

        with pytest.raises(click.ClickException) as caught:
            backup_mod._reap_leftover_window_postmaster(data_dir)

        message = str(caught.value)
        assert stopped == []
        assert "another-install" in message
        assert (data_dir / supervisor_module.RESTORE_WINDOW_PIDFILE).exists()

    def test_a_truncated_record_is_reported(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.launcher import supervisor as supervisor_module

        record = tmp_path / supervisor_module.RESTORE_WINDOW_PIDFILE
        record.write_text('{"version": 1, "pid": 4321, "start_ep', encoding="ascii")
        self._identity(monkeypatch)
        stopped = self._recorded_stops(monkeypatch)

        with pytest.raises(click.ClickException) as caught:
            backup_mod._reap_leftover_window_postmaster(tmp_path)

        message = str(caught.value)
        assert stopped == []
        assert str(record) in message
        assert "no readable PID" in message
        assert record.exists()

    def test_a_leftover_that_survives_the_stop_is_refused_loudly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_window_record(tmp_path, monkeypatch)
        self._identity(monkeypatch)
        self._recorded_stops(monkeypatch, stopped=False)

        with pytest.raises(click.ClickException, match="cannot stop it"):
            backup_mod._reap_leftover_window_postmaster(tmp_path)
