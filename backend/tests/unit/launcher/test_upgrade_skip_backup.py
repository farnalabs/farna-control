"""FAR-2667 review round: the native pre-flight's recovery-point guards.

Three behaviours the review found missing:

* ``--skip-backup`` probed the OLD bundle's script heads instead of the LIVE
  database, so a database already migrated ahead was undetectable on the one
  path with no snapshot (M13);
* ``--skip-backup`` proceeded with no snapshot on disk at all, contradicting
  the documented "the upgrade refuses when no snapshot is present" (M15);
* ``restore_guidance(snapshot=None)`` claimed the emitted command restored the
  data when it only re-points the binary symlink (M16).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from farna.launcher import upgrade as upgrade_module
from farna.launcher.upgrade import UpgradeError, _require_existing_snapshot, restore_guidance


def _release_manifest() -> Any:
    return SimpleNamespace(components={"postgres": "16"}, release="bundle-v9.9.9")


def _preflight(tmp_path: Path, *, skip_backup: bool) -> Path | None:
    (tmp_path / "bundle" / "pg" / "bin").mkdir(parents=True, exist_ok=True)
    return upgrade_module._run_upgrade_preflight(
        skip_backup=skip_backup,
        data_dir=tmp_path,
        reserve_target=tmp_path / "bundle",
        bundle_dir=tmp_path / "bundle",
        release_manifest=_release_manifest(),
        tarball=tmp_path / "bundle.tar.gz",
        version="9.9.9",
    )


@pytest.fixture(autouse=True)
def _pin_preflight_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_module, "_check_pg_major", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade_module, "_quantified_disk_preflight", lambda *_a, **_k: None)
    monkeypatch.setattr(upgrade_module, "check_no_downgrade", lambda *_a, **_k: None)


class TestSkipBackupProbesTheDatabase:
    def test_the_live_database_is_read_not_the_old_script_heads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[Path] = []

        def _live(data_dir: Path) -> list[str]:
            seen.append(data_dir)
            return ["db-revision"]

        monkeypatch.setattr(upgrade_module, "live_database_revisions", _live)
        # A script-heads probe would report the CODE's heads and miss a
        # database that is already ahead. Prove the bundle's heads are never
        # used as the database side.
        monkeypatch.setattr(upgrade_module, "_alembic_heads", lambda: ["code-head"])

        def _require(data_dir: Path) -> Path:
            return tmp_path / "pre-upgrade-dump-existing"

        monkeypatch.setattr(upgrade_module, "_require_existing_snapshot", _require)
        snapshot_dir = _preflight(tmp_path, skip_backup=True)
        assert seen == [tmp_path]
        assert snapshot_dir == tmp_path / "pre-upgrade-dump-existing"

    def test_a_missing_data_dir_state_is_refused_not_guessed(self, tmp_path: Path) -> None:
        with pytest.raises(UpgradeError, match="not bootstrapped"):
            upgrade_module.live_database_revisions(tmp_path)


class TestSkipBackupRequiresASnapshot:
    def test_no_snapshot_at_all_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(UpgradeError, match="no verified pre-upgrade snapshot"):
            _require_existing_snapshot(tmp_path)

    def test_a_corrupt_snapshot_does_not_count(self, tmp_path: Path) -> None:
        broken = tmp_path / f"{upgrade_module.SNAPSHOT_PREFIX}broken"
        broken.mkdir()
        (broken / "database.sql").write_bytes(b"CREATE TABLE t (id int);\n")
        with pytest.raises(UpgradeError, match="no verified pre-upgrade snapshot"):
            _require_existing_snapshot(tmp_path)

    def test_a_verified_snapshot_is_returned(self, tmp_path: Path) -> None:
        snapshot = tmp_path / f"{upgrade_module.SNAPSHOT_PREFIX}good"
        snapshot.mkdir()
        payload = b"-- dump\n-- PostgreSQL database dump complete\n"
        (snapshot / "database.sql").write_bytes(payload)
        import hashlib

        checksum = hashlib.sha256(payload).hexdigest()
        (snapshot / "manifest.json").write_text(
            json.dumps({"snapshot_for_upgrade": True, "dump_name": "database.sql", "dump_bytes": len(payload), "dump_sha256": checksum}),
            encoding="utf-8",
        )
        (snapshot / "backup-info.json").write_text(json.dumps({"file_checksums": {"database.sql": checksum}}), encoding="utf-8")
        assert _require_existing_snapshot(tmp_path) == snapshot


class TestNoSnapshotWhenTheTargetHeadIsApplied:
    def test_an_at_head_database_takes_no_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "live_database_revisions", lambda _d: ["head-a"])
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _b: {"head-a"})

        def _must_not_dump(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("no migration is pending, so no snapshot may be taken")

        monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", _must_not_dump)
        assert _preflight(tmp_path, skip_backup=False) is None

    def test_a_behind_database_takes_a_snapshot(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "live_database_revisions", lambda _d: ["head-a"])
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _b: {"head-b"})
        snapshot_dir = tmp_path / "pre-upgrade-dump-new"
        snapshot_dir.mkdir()
        monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", lambda *_a, **_k: SimpleNamespace(directory=snapshot_dir))
        assert _preflight(tmp_path, skip_backup=False) == snapshot_dir


class TestRestoreGuidanceWording:
    def test_without_a_snapshot_it_does_not_claim_a_data_restore(self, tmp_path: Path) -> None:
        guidance = restore_guidance(snapshot=None, previous_version="1.2.3", install_root=tmp_path, data_dir=tmp_path / "data")
        assert "binary ONLY" in guidance
        assert "DATABASE IS NOT RESTORED" in guidance
        assert "ln -sfn versions/1.2.3" in guidance
        assert "farna-control restore" not in guidance

    def test_with_a_snapshot_it_names_both_the_binary_and_the_data(self, tmp_path: Path) -> None:
        guidance = restore_guidance(snapshot=tmp_path / "snap", previous_version="1.2.3", install_root=tmp_path, data_dir=tmp_path / "data")
        assert "BOTH the binary AND the data" in guidance
        assert "farna-control restore" in guidance
