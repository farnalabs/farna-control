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
        required_heads: list[list[str] | None] = []

        def _live(data_dir: Path) -> list[str]:
            seen.append(data_dir)
            return ["db-revision"]

        monkeypatch.setattr(upgrade_module, "live_database_revisions", _live)
        # A script-heads probe would report the CODE's heads and miss a
        # database that is already ahead. Prove the bundle's heads are never
        # used as the database side.
        monkeypatch.setattr(upgrade_module, "_alembic_heads", lambda: ["code-head"])

        def _require(data_dir: Path, *, current_heads: list[str] | None = None) -> Path:
            required_heads.append(current_heads)
            return tmp_path / "pre-upgrade-dump-existing"

        monkeypatch.setattr(upgrade_module, "_require_existing_snapshot", _require)
        snapshot_dir = _preflight(tmp_path, skip_backup=True)
        assert seen == [tmp_path]
        # M5: the recovery-point check is given the LIVE database revisions, so
        # a snapshot of a different schema can never satisfy it.
        assert required_heads == [["db-revision"]]
        assert snapshot_dir == tmp_path / "pre-upgrade-dump-existing"

    def test_a_missing_data_dir_state_is_refused_not_guessed(self, tmp_path: Path) -> None:
        with pytest.raises(UpgradeError, match="not bootstrapped"):
            upgrade_module.live_database_revisions(tmp_path)


def _write_snapshot(root: Path, name: str, *, schema_versions: list[str] | None) -> Path:
    """A structurally valid snapshot of *schema_versions* (None -> no record)."""
    snapshot = root / f"{upgrade_module.SNAPSHOT_PREFIX}{name}"
    snapshot.mkdir()
    payload = b"-- dump\n-- PostgreSQL database dump complete\n"
    (snapshot / "database.sql").write_bytes(payload)
    import hashlib

    checksum = hashlib.sha256(payload).hexdigest()
    manifest: dict[str, Any] = {"snapshot_for_upgrade": True, "dump_name": "database.sql", "dump_bytes": len(payload), "dump_sha256": checksum}
    (snapshot / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    restore_manifest: dict[str, Any] = {"file_checksums": {"database.sql": checksum}}
    if schema_versions is not None:
        restore_manifest["schema_versions"] = schema_versions
    (snapshot / "backup-info.json").write_text(json.dumps(restore_manifest), encoding="utf-8")
    return snapshot


class TestSkipBackupRequiresASnapshot:
    def test_no_snapshot_at_all_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(UpgradeError, match="no verified pre-upgrade snapshot"):
            _require_existing_snapshot(tmp_path, current_heads=["head-a"])

    def test_a_corrupt_snapshot_does_not_count(self, tmp_path: Path) -> None:
        broken = tmp_path / f"{upgrade_module.SNAPSHOT_PREFIX}broken"
        broken.mkdir()
        (broken / "database.sql").write_bytes(b"CREATE TABLE t (id int);\n")
        with pytest.raises(UpgradeError, match="no verified pre-upgrade snapshot"):
            _require_existing_snapshot(tmp_path, current_heads=["head-a"])

    def test_a_verified_snapshot_of_the_current_schema_is_returned(self, tmp_path: Path) -> None:
        snapshot = _write_snapshot(tmp_path, "good", schema_versions=["head-a"])
        assert _require_existing_snapshot(tmp_path, current_heads=["head-a"]) == snapshot

    def test_a_snapshot_of_a_different_schema_is_refused(self, tmp_path: Path) -> None:
        """M5: an intact snapshot several migrations stale is not a recovery point."""
        _write_snapshot(tmp_path, "stale", schema_versions=["head-old"])
        with pytest.raises(UpgradeError, match="CURRENT schema"):
            _require_existing_snapshot(tmp_path, current_heads=["head-a"])

    def test_the_newest_matching_snapshot_wins_over_a_newer_stale_one(self, tmp_path: Path) -> None:
        matching = _write_snapshot(tmp_path, "matching", schema_versions=["head-a"])
        stale = _write_snapshot(tmp_path, "stale", schema_versions=["head-b"])
        stale.touch()  # a stale snapshot can still be the newest on disk
        assert _require_existing_snapshot(tmp_path, current_heads=["head-a"]) == matching

    def test_an_unrecorded_or_unknown_schema_is_refused(self, tmp_path: Path) -> None:
        _write_snapshot(tmp_path, "no-record", schema_versions=None)
        with pytest.raises(UpgradeError, match="CURRENT schema"):
            _require_existing_snapshot(tmp_path, current_heads=["head-a"])

    def test_an_unknown_live_head_is_refused_rather_than_guessed(self, tmp_path: Path) -> None:
        _write_snapshot(tmp_path, "good", schema_versions=["head-a"])
        with pytest.raises(UpgradeError, match="CURRENT schema"):
            _require_existing_snapshot(tmp_path, current_heads=["unknown"])
        with pytest.raises(UpgradeError, match="CURRENT schema"):
            _require_existing_snapshot(tmp_path, current_heads=None)


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


class TestThePendingCheckCannotBeBypassed:
    """M7: the pending decision lives in the SHARED helper.

    The installer runs the OLD installation's helper against the NEW bundle, so
    a check against the helper's own code would report "nothing pending" for
    every real upgrade and silently drop the recovery point. Every case below
    therefore drives :func:`pre_upgrade_dump` DIRECTLY — a caller that forgets
    the check cannot re-enable the unconditional dump.
    """

    @pytest.fixture(autouse=True)
    def _pin_the_native_seams(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dumps: list[Path] = []
        self.dumps = dumps
        monkeypatch.setattr(
            upgrade_module,
            "_owner_database_url",
            lambda _data_dir: ("postgresql://owner:pw@127.0.0.1:15432/farna_control", SimpleNamespace(postgres_port=15432)),
        )
        monkeypatch.setattr(upgrade_module, "_database_revisions", lambda _url, **_kwargs: ["head-live"])

        def _take(url: str, output_dir: Path, **_kwargs: Any) -> Any:
            dumps.append(output_dir)
            return SimpleNamespace(directory=output_dir / "snap")

        monkeypatch.setattr(upgrade_module, "take_upgrade_snapshot", _take)

    def test_an_at_head_target_bundle_takes_no_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _bundle: {"head-live"})
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_bundle=tmp_path) is None
        assert not self.dumps

    def test_explicit_target_heads_decide(self, tmp_path: Path) -> None:
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_heads=["head-live"]) is None
        assert not self.dumps

    def test_a_pending_target_still_takes_the_dump(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _bundle: {"head-new"})
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_bundle=tmp_path) is not None
        assert self.dumps == [tmp_path]

    def test_the_helper_never_compares_against_its_own_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The installer's exact shape: OLD code heads == the live database."""
        monkeypatch.setattr(upgrade_module, "_alembic_heads", lambda: ["head-live"])
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _bundle: {"head-new"})
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_bundle=tmp_path) is not None
        assert self.dumps == [tmp_path]

    def test_an_unreadable_target_bundle_fails_closed_and_dumps(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(upgrade_module, "_bundle_alembic_heads", lambda _bundle: set())
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_bundle=tmp_path) is not None
        assert self.dumps == [tmp_path]

    def test_an_unreadable_bundle_head_set_is_undecidable_not_at_head(self, tmp_path: Path) -> None:
        """M7 fail-closed: ``--target-heads`` empty is NOT "nothing to do"."""
        assert upgrade_module.pre_upgrade_dump(tmp_path, target_heads=[]) is not None
        assert self.dumps == [tmp_path]

    def test_main_reports_the_skip_on_stderr_and_writes_no_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(upgrade_module, "pre_upgrade_dump", lambda *_args, **_kwargs: None)
        code = upgrade_module.main(["--data-dir", str(tmp_path), "--target-bundle", str(tmp_path)])
        captured = capsys.readouterr()
        assert code == 0
        assert not captured.out.strip(), "stdout is the snapshot path the installer captures"
        assert "no migration is pending" in captured.err.lower()
