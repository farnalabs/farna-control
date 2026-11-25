"""Sanitiser tests for ``scripts/run_behaviour_bindings_baseline.py`` (FAR-2701).

The binding gate writes derived statuses back into product-map entries. The
write target is now regex-bounded to a repo-relative path and required to
resolve under ``docs/product-map``, so no scanned value can steer a write
outside the map (pythonsecurity:S2083).
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
        "docs/product-map/auth/sso-provider-ui.md",
    ],
)
def test_sanitize_relpath_accepts_repo_relative_paths(good: str):
    assert _bindings._sanitize_relpath(good, "path") == good


@pytest.mark.parametrize(
    "bad",
    ["../etc/passwd", "/etc/passwd", "-x", "a/../../b", "a/./b", ".", "..", "a//b"],
    ids=[
        "relative-traversal",
        "absolute-path",
        "leading-dash",
        "nested-traversal",
        "dot-segment",
        "single-dot",
        "parent-dot",
        "double-slash",
    ],
)
def test_sanitize_relpath_rejects_traversal_absolute_paths_and_flags(bad: str):
    with pytest.raises(ValueError, match="not a safe relative path"):
        _bindings._sanitize_relpath(bad, "path")


# --- _contained_entry_path --------------------------------------------------


def test_contained_entry_path_accepts_an_entry_under_the_product_map(tmp_path: Path):
    entry = tmp_path / "docs" / "product-map" / "probe.md"
    entry.parent.mkdir(parents=True)
    entry.write_text("x", encoding="utf-8")

    assert _bindings._contained_entry_path(tmp_path, "docs/product-map/probe.md") == entry.resolve()


def test_contained_entry_path_rejects_a_repo_path_outside_the_product_map(tmp_path: Path):
    """A regex-valid relative path still cannot escape the map."""
    other = tmp_path / "backend" / "tests" / "escape.md"
    other.parent.mkdir(parents=True)
    other.write_text("x", encoding="utf-8")

    with pytest.raises(ValueError, match="outside docs/product-map"):
        _bindings._contained_entry_path(tmp_path, "backend/tests/escape.md")


@pytest.mark.parametrize("bad", ["docs/product-map/../../../evil.md", "/etc/passwd", "docs/product-map/../evil.md", "-x"])
def test_contained_entry_path_rejects_traversal_and_flags(bad: str, tmp_path: Path):
    with pytest.raises(ValueError, match="entry path"):
        _bindings._contained_entry_path(tmp_path, bad)
