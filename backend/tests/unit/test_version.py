"""Unit tests for ``farna.version`` bundle-stamp resolution (FAR-2581)."""

from importlib.metadata import PackageNotFoundError
from pathlib import Path

import pytest

from farna.version import _bundle_version, _resolve_version


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


def test_path_without_enough_parents_falls_through() -> None:
    """A module path shallower than the bundle depth raises IndexError -> None."""
    assert _bundle_version("/version.py") is None


def test_resolve_version_prefers_bundle_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("farna.version._bundle_version", lambda: "0.3.329")

    assert _resolve_version() == "0.3.329"


def test_resolve_version_defaults_when_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("farna.version._bundle_version", lambda: None)
    monkeypatch.setattr("farna.version.version", _missing)

    assert _resolve_version() == "0.0.0-dev"
