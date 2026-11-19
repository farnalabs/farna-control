"""Unit tests for ``farna.version`` bundle-stamp resolution (FAR-2581)."""

from pathlib import Path

from farna.version import _bundle_version


def _fake_bundle(root: Path, stamp: str | None, *, launcher: bool = True) -> str:
    module_file = root / "backend" / "src" / "farna" / "version.py"
    module_file.parent.mkdir(parents=True)
    module_file.write_text("", encoding="utf-8")
    if launcher:
        (root / "launcher").write_text("#!/bin/sh\n", encoding="utf-8")
    if stamp is not None:
        (root / "VERSION").write_text(stamp, encoding="utf-8")
    return str(module_file)


def test_bundle_stamp_wins(tmp_path: Path) -> None:
    module_file = _fake_bundle(tmp_path, "bundle-v0.3.329\n")

    assert _bundle_version(module_file) == "0.3.329"


def test_no_launcher_means_not_a_bundle(tmp_path: Path) -> None:
    module_file = _fake_bundle(tmp_path, "bundle-v0.3.329\n", launcher=False)

    assert _bundle_version(module_file) is None


def test_missing_version_file_falls_through(tmp_path: Path) -> None:
    module_file = _fake_bundle(tmp_path, None)

    assert _bundle_version(module_file) is None


def test_unprefixed_stamp_is_ignored(tmp_path: Path) -> None:
    module_file = _fake_bundle(tmp_path, "0.3.329\n")

    assert _bundle_version(module_file) is None


def test_empty_prefixed_stamp_is_ignored(tmp_path: Path) -> None:
    module_file = _fake_bundle(tmp_path, "bundle-v\n")

    assert _bundle_version(module_file) is None
