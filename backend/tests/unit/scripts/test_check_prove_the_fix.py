"""Unit tests for ``scripts/run_check_prove_the_fix.py`` (FAR-2701).

The prove-the-fix gate is what turns "the suite is green" into "the change is
proven": a PR that touches ``backend/src/**`` or ``frontend/src/**`` must name
one test in its body, and that test must exist, be changed by the same PR, and
not be skipped, xfailed, or vacuous.

Two defects shipped in untested branches of this script, which is why this file
pins every branch rather than only the happy path:

* ``_skip_reason`` only looked at the function's OWN decorators, so a module
  that skips everything with a module-level ``pytestmark`` (the real instance was
  ``tests/unit/test_merge_queue_fixer_dedupe_harness.py``) and a class-level
  ``@pytest.mark.skip`` both passed the gate as "proving" tests;
* nothing exercised the marker parser's fenced/HTML-comment stripping, the path
  confinement, or the frontend spec resolution.

The tests drive the real module in a throwaway repository built under
``tmp_path``, so the AST parsing, the path resolution and the diff-shape rules
run for real - no mocks of the thing under test.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for _parent in Path(__file__).resolve().parents:
    _script_path = _parent / "scripts" / "run_check_prove_the_fix.py"
    if _script_path.exists():
        break
else:  # pragma: no cover - the repository always ships the script
    raise RuntimeError("Could not locate scripts/run_check_prove_the_fix.py")

_loader = SourceFileLoader("run_check_prove_the_fix", str(_script_path))
_module = module_from_spec(spec_from_loader("run_check_prove_the_fix", _loader))
assert _module is not None, "could not build the prove-the-fix module spec"
sys.modules[_loader.name] = _module
_loader.exec_module(_module)

_PROBE_MODULE = "backend/tests/unit/test_probe.py"
_SPEC_MODULE = "frontend/src/__tests__/ActionRow.spec.ts"
_PRODUCT_FILE = "backend/src/farna/thing.py"

_PROBE_SOURCE = '''\
"""Fake pytest module used by the prove-the-fix unit tests."""

import unittest

import pytest


def test_runs():
    assert 1 + 1 == 2


@pytest.mark.skip(reason="not ready")
def test_function_skipped():
    assert True


@pytest.mark.xfail
def test_function_xfail():
    assert True


def test_body_skipped():
    pytest.skip("environment")


class TestGroup:
    def test_plain_member(self):
        assert 1 + 1 == 2

    @pytest.mark.skipif(True, reason="platform")
    def test_member_skipped(self):
        assert True


@pytest.mark.skip
class TestSkippedGroup:
    def test_member_of_skipped_class(self):
        assert True


class TestClassBodyMarked:
    pytestmark = pytest.mark.skip(reason="class harness unavailable")

    def test_member_of_class_body_pytestmark(self):
        assert True


class TestUnittestSkipped:
    @unittest.skip("legacy suite")
    def test_member_unittest_skip(self):
        assert True
'''

_MARKED_MODULE = "backend/tests/unit/test_module_marked.py"
_MARKED_SOURCE = '''\
"""A module whose pytestmark skips every test in it."""

import pytest

pytestmark = pytest.mark.skipif(True, reason="harness unavailable")


def test_module_level_skip():
    assert True
'''

_LIST_MARKED_MODULE = "backend/tests/unit/test_list_marked.py"
_LIST_MARKED_SOURCE = '''\
"""A module whose pytestmark list xfails every test in it."""

import pytest

pytestmark = [pytest.mark.xfail]


def test_module_level_list_xfail():
    assert True
'''

_SPEC_SOURCE = """\
import { describe, expect, it } from 'vitest'

describe('ActionRow', () => {
  it('stacks below sm', () => {
    expect(true).toBe(true)
  })

  it.skip('is paused for now', () => {
    expect(true).toBe(true)
  })
})
"""

_DISABLED_SPEC_MODULE = "frontend/src/__tests__/DisabledBlock.spec.ts"
_DISABLED_SPEC_SOURCE = """\
import { describe, expect, it } from 'vitest'

// A whole block parked behind a disabled describe: nothing inside it runs.
describe.skip('DisabledBlock', () => {
  it('never runs in CI', () => {
    expect(true).toBe(true)
  })
})
"""


def _make_repo(root: Path) -> Path:
    """A minimal repo holding both test trees the checker resolves against."""
    for rel in (Path(_PROBE_MODULE).parent, Path("frontend/src/__tests__")):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / _PROBE_MODULE).write_text(_PROBE_SOURCE, encoding="utf-8")
    (root / _MARKED_MODULE).write_text(_MARKED_SOURCE, encoding="utf-8")
    (root / _LIST_MARKED_MODULE).write_text(_LIST_MARKED_SOURCE, encoding="utf-8")
    (root / _SPEC_MODULE).write_text(_SPEC_SOURCE, encoding="utf-8")
    (root / _DISABLED_SPEC_MODULE).write_text(_DISABLED_SPEC_SOURCE, encoding="utf-8")
    return root


def _changed(*extra: str) -> list[str]:
    """The changed-file list a typical product PR carries."""
    return [_PRODUCT_FILE, *extra]


def _marker_evaluate(root: Path, body: str, changed: list[str]) -> int:
    """Run the gate and return its exit code."""
    return _module.evaluate(body, changed, root)


# --- marker parsing ---------------------------------------------------------


def test_marker_refs_reads_a_valid_marker():
    refs, malformed = _module.marker_refs(f"Proves: {_PROBE_MODULE}::test_runs")
    assert refs == [f"{_PROBE_MODULE}::test_runs"]
    assert not malformed


def test_marker_refs_tolerates_a_bullet_and_backticks():
    refs, malformed = _module.marker_refs(f"- Proves: `{_PROBE_MODULE}::test_runs`")
    assert refs == [f"{_PROBE_MODULE}::test_runs"]
    assert malformed == []


def test_marker_refs_reports_a_marker_naming_several_tests():
    refs, malformed = _module.marker_refs(f"Proves: {_PROBE_MODULE}::test_runs, {_PROBE_MODULE}::test_body_skipped")
    assert refs == []
    assert malformed == [f"Proves: {_PROBE_MODULE}::test_runs, {_PROBE_MODULE}::test_body_skipped (name one test per Proves: line)"]


def test_marker_inside_a_fenced_block_does_not_count():
    body = f"```\nProves: {_PROBE_MODULE}::test_runs\n```\n"
    assert _module.marker_refs(body) == ([], [])


def test_marker_inside_an_html_comment_does_not_count():
    body = f"<!--\nProves: {_PROBE_MODULE}::test_runs\n-->\n"
    assert _module.marker_refs(body) == ([], [])


# --- the happy path and the missing-marker path ----------------------------


def test_a_valid_changed_proving_test_passes(tmp_path: Path):
    root = _make_repo(tmp_path)
    code = _marker_evaluate(root, f"Proves: {_PROBE_MODULE}::test_runs", _changed(_PROBE_MODULE))
    assert code == 0


def test_a_product_change_with_no_marker_fails(tmp_path: Path):
    root = _make_repo(tmp_path)
    code = _marker_evaluate(root, "No proving marker here.", [_PRODUCT_FILE])
    assert code == 1


def test_a_product_change_with_only_a_fenced_marker_fails(tmp_path: Path):
    root = _make_repo(tmp_path)
    body = f"```\nProves: {_PROBE_MODULE}::test_runs\n```\n"
    assert _marker_evaluate(root, body, _changed(_PROBE_MODULE)) == 1


def test_a_change_with_no_product_source_passes_without_a_marker(tmp_path: Path):
    root = _make_repo(tmp_path)
    code = _marker_evaluate(root, "docs only", ["docs/product-map/README.md"])
    assert code == 0


# --- the changed-module requirement ----------------------------------------


def test_a_proving_test_this_pr_does_not_change_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_runs", root, [_PRODUCT_FILE])
    assert problem is not None
    assert "does not change" in problem


# --- path confinement -------------------------------------------------------


def test_parent_traversal_in_a_test_path_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    ref = "backend/tests/../../../etc/passwd.py::test_runs"
    problem = _module.verify_ref(ref, root, [ref.partition("::")[0]])
    assert problem is not None
    assert "traversal" in problem


def test_an_absolute_test_path_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    module, problem = _module._resolve_test_module(f"/{_PROBE_MODULE}", root)
    assert module is None
    assert problem is not None
    assert "repo-relative" in problem


def test_a_test_outside_the_test_roots_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    module, problem = _module._resolve_test_module("backend/src/farna/test_sneaky.py", root)
    assert module is None
    assert problem is not None
    assert "not under" in problem


# --- skip / xfail detection at every pytest scope --------------------------


def test_a_function_level_skip_decorator_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_function_skipped", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "it is decorated with @pytest.mark.skip" in problem


def test_a_function_level_xfail_decorator_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_function_xfail", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "@pytest.mark.xfail" in problem


def test_a_skip_call_opening_the_body_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_body_skipped", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "pytest.skip()" in problem


def test_a_class_level_skip_decorator_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::TestSkippedGroup::test_member_of_skipped_class", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "its class is decorated with @pytest.mark.skip" in problem


def test_a_member_skip_decorator_inside_a_clean_class_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::TestGroup::test_member_skipped", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "@pytest.mark.skipif" in problem


def test_a_unittest_skip_decorator_is_rejected(tmp_path: Path):
    """The class-based suites spell it ``@unittest.skip``, not ``@pytest.mark``."""
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::TestUnittestSkipped::test_member_unittest_skip", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "it is decorated with @unittest.skip" in problem


def test_a_class_body_pytestmark_is_rejected(tmp_path: Path):
    """A ``pytestmark`` assigned in the CLASS body skips every method of it."""
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::TestClassBodyMarked::test_member_of_class_body_pytestmark", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "its class sets pytestmark = pytest.mark.skip" in problem


def test_a_module_level_pytestmark_is_rejected(tmp_path: Path):
    """The real regression: ``pytestmark = pytest.mark.skipif(...)`` at module level."""
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_MARKED_MODULE}::test_module_level_skip", root, _changed(_MARKED_MODULE))
    assert problem is not None
    assert "its module sets pytestmark = pytest.mark.skipif" in problem


def test_a_module_level_pytestmark_list_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_LIST_MARKED_MODULE}::test_module_level_list_xfail", root, _changed(_LIST_MARKED_MODULE))
    assert problem is not None
    assert "pytestmark = pytest.mark.xfail" in problem


def test_a_clean_test_in_a_class_passes(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::TestGroup::test_plain_member", root, _changed(_PROBE_MODULE))
    assert problem is None


# --- frontend spec refs -----------------------------------------------------


def test_a_frontend_spec_title_resolves(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_SPEC_MODULE}::stacks below sm", root, _changed(_SPEC_MODULE))
    assert problem is None


def test_an_unknown_frontend_spec_title_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_SPEC_MODULE}::never written", root, _changed(_SPEC_MODULE))
    assert problem is not None
    assert "not defined" in problem


def test_a_skipped_frontend_spec_title_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_SPEC_MODULE}::is paused for now", root, _changed(_SPEC_MODULE))
    assert problem is not None
    assert "skipped/fixme/todo" in problem


def test_a_test_inside_a_skipped_describe_block_is_rejected(tmp_path: Path):
    """A plain ``it(...)`` inside ``describe.skip(...)`` never runs either."""
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_DISABLED_SPEC_MODULE}::never runs in CI", root, _changed(_DISABLED_SPEC_MODULE))
    assert problem is not None
    assert "describe.skip" in problem


# --- a named test that does not exist --------------------------------------


def test_an_unknown_pytest_node_is_rejected(tmp_path: Path):
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_never_written", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "not defined" in problem
