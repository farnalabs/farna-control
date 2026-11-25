"""Architecture: the strict respx transport policy is enumerated and ratcheted (FAR-2702).

127 test modules fake 3,396 tests through respx, and no stub was ever diffed
against a real provider response, so a green test can prove our own assumption.
``tests/_transport_strict.py`` makes that visible when
``FARNA_STRICT_TRANSPORT=1``. This module is the ratchet that lets the policy
land incrementally and never grow:

* every test module that imports respx must be enumerated in the baseline's
  ``modules`` list, so adding a respx-based module cannot bypass the policy
  scan silently;
* every recorded violation must name an existing module and test function with
  a known violation kind, so the baseline cannot rot into noise;
* when a strict report is supplied (CI runs the strict runner first), no
  violation in that report may be missing from the baseline. The runner
  (``scripts/run_transport_strict.py``) performs the same comparison for a
  human; this test makes it a gate even if the runner is bypassed.

Shrinking is always allowed: a violation whose test no longer reproduces is
reported as "baseline may shrink" and never fails the run.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from tests import _transport_strict

TESTS_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "transport_policy_baseline.json"

_PARAM_SUFFIX = "["


def _imports_respx(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "respx" or alias.name.startswith("respx.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module is not None and (node.module == "respx" or node.module.startswith("respx.")):
            return True
    return False


def respx_test_modules() -> list[str]:
    """Every collectable ``tests/test_*.py`` module that imports respx.

    Only test modules are in scope: the instrumentation itself
    (``tests/_transport_strict.py``) and conftest helpers import respx without
    being a test, so they are not gated by the policy.

    A raw-byte prefilter keeps the walk cheap: only files that mention respx at
    all are parsed (a few dozen of ~1,500 test modules).
    """
    modules: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        raw = path.read_bytes()
        if b"respx" not in raw:
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):  # pragma: no cover - test tree is valid Python
            continue
        if _imports_respx(tree):
            modules.append(path.relative_to(TESTS_ROOT.parent).as_posix())
    return modules


def _load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _defined_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def test_every_respx_module_is_enumerated() -> None:
    baseline = _load_baseline()
    declared = set(baseline["modules"])
    actual = set(respx_test_modules())

    missing = sorted(actual - declared)
    assert not missing, f"These test modules import respx but are absent from {BASELINE_PATH.name}['modules'] - add them so the strict transport policy scan covers them (the baseline is regenerated with `python scripts/run_transport_strict.py --all --write-baseline`):\n  " + "\n  ".join(missing)

    stale = sorted(declared - actual)
    assert not stale, "These modules are listed in the transport-policy baseline but no longer import respx - remove them (shrink-only baseline):\n  " + "\n  ".join(stale)


def test_baseline_entries_are_well_formed() -> None:
    baseline = _load_baseline()
    declared_modules = set(baseline["modules"])
    violations: dict[str, list[str]] = baseline["violations"]

    problems: list[str] = []
    for node, kinds in sorted(violations.items()):
        module, _, rest = node.partition("::")
        if module not in declared_modules:
            problems.append(f"{node}: module is not in the baseline's modules list")
            continue
        if not kinds:
            problems.append(f"{node}: empty violation list")
        unknown = sorted(set(kinds) - _transport_strict.VIOLATION_KINDS)
        if unknown:
            problems.append(f"{node}: unknown violation kind(s) {unknown}")
        if not rest:
            problems.append(f"{node}: no test name (node ids are '<module>::<test>')")
            continue
        module_path = TESTS_ROOT.parent / module
        if not module_path.exists():
            problems.append(f"{node}: module file does not exist")
            continue
        if "tests/bdd/" in f"{module}/":
            # pytest-BDD generates the test function from the Gherkin scenario
            # at collection time, so it does not exist in the steps module's
            # AST. Module existence is the check that applies here.
            continue
        name = rest
        if _PARAM_SUFFIX in name:
            name = name.split(_PARAM_SUFFIX, 1)[0]
        leaf = name.split("::")[-1]
        if leaf not in _defined_names(module_path):
            problems.append(f"{node}: no function/class named {leaf!r} in {module}")

    assert not problems, "Transport-policy baseline is malformed:\n  " + "\n  ".join(problems)


def test_baseline_ratchet_against_strict_report() -> None:
    """No violation in a supplied strict report may be absent from the baseline.

    The report path comes from ``FARNA_STRICT_TRANSPORT_REPORT`` and is produced
    by ``scripts/run_transport_strict.py``. When the env var is unset the test
    is a no-op: the runner already enforces the same rule on its own.
    """
    raw = os.environ.get("FARNA_STRICT_TRANSPORT_REPORT", "").strip()
    if not raw:
        pytest.skip("no FARNA_STRICT_TRANSPORT_REPORT supplied - the runner enforces the ratchet")

    report_path = Path(raw)
    if not report_path.exists():
        pytest.skip(f"strict report {report_path} does not exist")

    baseline = _load_baseline()
    baseline_kinds = {node: set(kinds) for node, kinds in baseline["violations"].items()}
    declared_modules = set(baseline["modules"])
    report = _transport_strict.collect_report_violations(report_path)

    new: list[str] = []
    for node, kinds in sorted(report.items()):
        if node.split("::", 1)[0] not in declared_modules:
            continue
        for kind in sorted(kinds - baseline_kinds.get(node, set())):
            new.append(f"{node} [{kind}]")

    assert not new, "Strict transport violations not recorded in the baseline (the ratchet is shrink-only):\n  " + "\n  ".join(new) + "\nFix the test, or record a pre-existing violation deliberately with `python scripts/run_transport_strict.py --write-baseline` and justify it in the PR."
