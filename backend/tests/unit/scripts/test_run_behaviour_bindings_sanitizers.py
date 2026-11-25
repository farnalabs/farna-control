"""Sanitiser tests for ``scripts/run_behaviour_bindings_baseline.py`` (FAR-2701).

The binding gate hands a base revision and a repo-relative path to ``git`` and
writes derived statuses back into product-map entries. Both now route through
regex allow-lists and filesystem confinement, so no scanned or caller value can
steer a subprocess argument or a write outside the tree
(pythonsecurity:S8705 / S2083).
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for _parent in Path(__file__).resolve().parents:
    _script_path = _parent / "scripts" / "run_behaviour_bindings_baseline.py"
    if _script_path.exists():
        break
else:  # pragma: no cover - the repository always ships the script
    raise RuntimeError("Could not locate scripts/run_behaviour_bindings_baseline.py")

_loader = SourceFileLoader("far2701_behaviour_bindings_sanitizers", str(_script_path))
_bindings = module_from_spec(spec_from_loader("far2701_behaviour_bindings_sanitizers", _loader))
assert _bindings is not None, "could not create the behaviour-bindings module spec"
# Registered before execution: the module's dataclasses resolve their string
# annotations through ``sys.modules[cls.__module__]``.
sys.modules[_loader.name] = _bindings
_loader.exec_module(_bindings)


# --- _sanitize_relpath ------------------------------------------------------


@pytest.mark.parametrize(
    "good",
    [
        "backend/tests/architecture/behaviour_bindings_baseline.json",
        "docs/product-map/probe-entry.md",
        "HEAD",
        "origin/main",
        "v1.2.3",
        "a1b2c3d4e5f6",
    ],
)
def test_sanitize_relpath_accepts_repo_relative_paths_and_refs(good: str):
    assert _bindings._sanitize_relpath(good, "path") == good


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "-x", "a/../../b", "a/./b", ".", ".."])
def test_sanitize_relpath_rejects_traversal_absolute_paths_and_flags(bad: str):
    with pytest.raises(ValueError, match="not a safe relative path"):
        _bindings._sanitize_relpath(bad, "path")


# --- _sanitize_git_object / _sanitize_committish ---------------------------


def test_sanitize_git_object_builds_a_bounded_spec():
    spec = _bindings._sanitize_git_object("HEAD", "backend/tests/architecture/behaviour_bindings_baseline.json")
    assert spec == "HEAD:backend/tests/architecture/behaviour_bindings_baseline.json"


@pytest.mark.parametrize(
    ("rev", "relpath"),
    [("-x", "a/b.md"), ("HEAD", "../secret"), ("HEAD", "/abs/path"), ("HEAD; rm -rf /", "ok.md")],
)
def test_sanitize_git_object_rejects_flag_and_traversal_payloads(rev: str, relpath: str):
    with pytest.raises(ValueError, match="object spec"):
        _bindings._sanitize_git_object(rev, relpath)


def test_sanitize_committish_builds_the_rev_parse_argument():
    assert _bindings._sanitize_committish("HEAD") == "HEAD^{commit}"
    assert _bindings._sanitize_committish("a" * 40) == f"{'a' * 40}^{{commit}}"


@pytest.mark.parametrize("bad", ["-x", "HEAD; rm -rf /", "HEAD^{tree}", ""])
def test_sanitize_committish_rejects_non_revisions(bad: str):
    with pytest.raises(ValueError, match="as a revision"):
        _bindings._sanitize_committish(bad)


# --- _contained_entry_path --------------------------------------------------


def test_contained_entry_path_accepts_an_entry_under_the_product_map(tmp_path: Path):
    entry = tmp_path / "docs" / "product-map" / "probe.md"
    entry.parent.mkdir(parents=True)
    entry.write_text("x", encoding="utf-8")

    assert _bindings._contained_entry_path(tmp_path, "docs/product-map/probe.md") == entry.resolve()


@pytest.mark.parametrize("bad", ["docs/product-map/../../../evil.md", "/etc/passwd", "docs/product-map/../evil.md"])
def test_contained_entry_path_rejects_escapes(bad: str, tmp_path: Path):
    with pytest.raises(ValueError, match="entry path"):
        _bindings._contained_entry_path(tmp_path, bad)
