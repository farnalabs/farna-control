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
_REPO_ROOT = _script_path.parents[1]

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

_EXPECTED_FAILURE_MODULE = "backend/tests/unit/test_expected_failure.py"
_EXPECTED_FAILURE_SOURCE = '''\
"""A module whose test is marked as an expected unittest failure."""

import unittest


class TestExpectedFailure(unittest.TestCase):
    @unittest.expectedFailure
    def test_known_broken(self):
        self.assertEqual(1, 2)
'''

_MODULE_SKIP_CALL_MODULE = "backend/tests/unit/test_module_skip_call.py"
_MODULE_SKIP_CALL_SOURCE = '''\
"""A module that skips collection outright."""

import pytest

pytest.skip("suite disabled", allow_module_level=True)


def test_never_collected():
    assert True
'''

_MODULE_IMPORTORSKIP_MODULE = "backend/tests/unit/test_module_importorskip.py"
_MODULE_IMPORTORSKIP_SOURCE = '''\
"""A module that skips collection when an optional dependency is absent."""

import pytest

optional = pytest.importorskip("a_module_that_is_never_installed")


def test_never_collected():
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
    (root / _EXPECTED_FAILURE_MODULE).write_text(_EXPECTED_FAILURE_SOURCE, encoding="utf-8")
    (root / _MODULE_SKIP_CALL_MODULE).write_text(_MODULE_SKIP_CALL_SOURCE, encoding="utf-8")
    (root / _MODULE_IMPORTORSKIP_MODULE).write_text(_MODULE_IMPORTORSKIP_SOURCE, encoding="utf-8")
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


def test_marker_refs_ignores_an_empty_marker():
    """A bare ``Proves:`` is ABSENT, not malformed.

    The shipped PR template carries exactly that placeholder line, so reading an
    empty marker as malformed failed every product PR that followed the template -
    including one that also filled in a valid marker further down.
    """
    assert _module.marker_refs("Proves:\n") == ([], []), "an empty marker must read as absent, not as malformed"

    refs, malformed = _module.marker_refs(f"Proves:   \nProves: `{_PROBE_MODULE}::test_runs`")
    assert refs == [f"{_PROBE_MODULE}::test_runs"], f"a filled-in marker beside the empty placeholder must still count, got {refs!r}"
    assert malformed == [], f"the empty placeholder must not be reported as malformed, got {malformed!r}"


def test_the_shipped_pr_template_does_not_trip_a_valid_marker(tmp_path: Path):
    """Following the shipped template must not fail the gate.

    Reads the REAL ``.github/PULL_REQUEST_TEMPLATE.md``, adds one valid marker the
    way a PR author would, and runs the gate end to end: the template's empty
    ``Proves:`` line used to make that body fail with "does not name exactly one
    test", which blocked every product PR that used the template.
    """
    root = _make_repo(tmp_path)
    template = (_REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    body = f"{template}\nProves: {_PROBE_MODULE}::test_runs\n"

    assert _marker_evaluate(root, body, _changed(_PROBE_MODULE)) == 0


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


def test_a_unittest_expected_failure_decorator_is_rejected(tmp_path: Path):
    """``@unittest.expectedFailure`` passes whether or not the fix exists."""
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_EXPECTED_FAILURE_MODULE}::TestExpectedFailure::test_known_broken", root, _changed(_EXPECTED_FAILURE_MODULE))
    assert problem is not None
    assert "it is decorated with @unittest.expectedFailure" in problem


def test_a_module_level_skip_call_or_importorskip_is_rejected(tmp_path: Path):
    """A module-scope skip/importorskip skips every test in the module."""
    root = _make_repo(tmp_path)
    skip_problem = _module.verify_ref(f"{_MODULE_SKIP_CALL_MODULE}::test_never_collected", root, _changed(_MODULE_SKIP_CALL_MODULE))
    importorskip_problem = _module.verify_ref(f"{_MODULE_IMPORTORSKIP_MODULE}::test_never_collected", root, _changed(_MODULE_IMPORTORSKIP_MODULE))
    assert skip_problem is not None
    assert "pytest.skip() at import time" in skip_problem
    assert importorskip_problem is not None
    assert "pytest.importorskip() at import time" in importorskip_problem


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


def test_a_class_member_requires_its_owner_in_the_node_id(tmp_path: Path):
    """A bare ``module.py::test_member`` must not resolve a class member.

    pytest collects ``TestGroup::test_plain_member``, not
    ``test_plain_member``. Searching class bodies when no owner is given made a
    node id pytest cannot collect resolve as proof.
    """
    root = _make_repo(tmp_path)
    problem = _module.verify_ref(f"{_PROBE_MODULE}::test_plain_member", root, _changed(_PROBE_MODULE))
    assert problem is not None
    assert "not defined" in problem


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


# --- CLI input path confinement (FAR-2701: pythonsecurity:S8707) -----------


def test_read_input_path_accepts_a_file_in_the_temp_directory(tmp_path: Path):
    """CI hands the inputs in from ``/tmp``, outside the checkout."""
    probe = tmp_path / "pr-body.md"
    probe.write_text("Proves: x\n", encoding="utf-8")
    assert _module._sanitize_read_path(str(probe), "pr-body", _REPO_ROOT) == probe.resolve()


def test_read_input_path_accepts_a_file_in_the_repo(tmp_path: Path):
    probe = tmp_path / "body.md"
    probe.write_text("x", encoding="utf-8")
    assert _module._sanitize_read_path(str(probe), "pr-body", tmp_path) == probe.resolve()


def test_read_input_path_rejects_traversal_and_paths_outside_the_allowed_roots(tmp_path: Path):
    """``..``/``.`` segments and a leading ``-`` are refused before the read, and
    an absolute path outside the repo and the temp directory is refused after it
    resolves."""
    import pytest

    for bad in ("../secrets", "-x", "a/../b", "/etc/passwd", "C:\\Windows\\win.ini"):
        with pytest.raises(ValueError, match="invalid"):
            _module._sanitize_read_path(bad, "pr-body", tmp_path)


def test_main_reads_the_confined_inputs_it_is_given(tmp_path: Path):
    root = _make_repo(tmp_path)
    body = tmp_path / "body.md"
    body.write_text(f"Proves: {_PROBE_MODULE}::test_runs\n", encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text(f"{_PRODUCT_FILE}\n{_PROBE_MODULE}\n", encoding="utf-8")

    code = _module.main(["--pr-body", str(body), "--changed-files", str(changed), "--repo-root", str(root)])

    assert code == 0


def test_main_refuses_a_traversing_input_path(tmp_path: Path):
    code = _module.main(["--pr-body", "../body.md", "--changed-files", str(tmp_path / "changed.txt"), "--repo-root", str(tmp_path)])
    assert code == 2
