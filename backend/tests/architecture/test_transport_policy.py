"""Architecture: the strict respx transport policy is enumerated and ratcheted (FAR-2702).

124 test modules fake 3,300+ tests through respx, and no stub was ever diffed
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

Violations are keyed ``(nodeid, kind, fingerprint)``. The fingerprint is a
stable hash of the finding's detail, so a test that already carries a baselined
``unused_route`` cannot quietly grow a second dead stub: the second stub has a
different fingerprint and is a new violation.

Shrinking is always allowed: a violation whose test no longer reproduces is
reported as "baseline may shrink" and never fails the run.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests import _transport_strict

TESTS_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "transport_policy_baseline.json"
CI_WORKFLOW_PATH = TESTS_ROOT.parent.parent / ".github" / "workflows" / "ci.yml"
STRICT_RUNNER_PATH = TESTS_ROOT.parent.parent / "scripts" / "run_transport_strict.py"

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


def _load_baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _baseline_keys(baseline: dict[str, Any]) -> set[tuple[str, str, str]]:
    return {(node, entry["kind"], entry["fingerprint"]) for node, entries in baseline["violations"].items() for entry in entries}


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
    assert baseline.get("version") == 2, f"{BASELINE_PATH.name} must be version 2 (fingerprinted violations); regenerate it with `python scripts/run_transport_strict.py --all --write-baseline`"
    declared_modules = set(baseline["modules"])
    violations: dict[str, list[dict[str, str]]] = baseline["violations"]

    problems: list[str] = []
    for node, entries in sorted(violations.items()):
        if node == _transport_strict.SESSION_NODEID:
            # Findings outside any test item name no module and no test.
            pass
        else:
            module, _, rest = node.partition("::")
            if module not in declared_modules:
                problems.append(f"{node}: module is not in the baseline's modules list")
            elif not rest:
                problems.append(f"{node}: no test name (node ids are '<module>::<test>')")
            else:
                module_path = TESTS_ROOT.parent / module
                if not module_path.exists():
                    problems.append(f"{node}: module file does not exist")
                elif "tests/bdd/" not in f"{module}/":
                    # pytest-BDD generates the test function from the Gherkin
                    # scenario at collection time, so it does not exist in the
                    # steps module's AST. Module existence is the check that
                    # applies there.
                    name = rest.split(_PARAM_SUFFIX, 1)[0]
                    leaf = name.split("::")[-1]
                    if leaf not in _defined_names(module_path):
                        problems.append(f"{node}: no function/class named {leaf!r} in {module}")
        if not entries:
            problems.append(f"{node}: empty violation list")
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append(f"{node}: entry {entry!r} is not an object (a legacy kind-only entry; regenerate the baseline)")
                continue
            kind = entry.get("kind")
            fingerprint = entry.get("fingerprint")
            if kind not in _transport_strict.VIOLATION_KINDS:
                problems.append(f"{node}: unknown violation kind {kind!r}")
            if not isinstance(fingerprint, str) or not fingerprint:
                problems.append(f"{node}: entry {kind!r} has no fingerprint")

    assert not problems, "Transport-policy baseline is malformed:\n  " + "\n  ".join(problems)


def test_baseline_ratchet_against_strict_report() -> None:
    """No violation in a supplied strict report may be absent from the baseline.

    The report path comes from ``FARNA_STRICT_TRANSPORT_REPORT`` and is produced
    by ``scripts/run_transport_strict.py``. When the env var is unset the test
    is a no-op: the runner already enforces the same rule on its own. When it IS
    set the report must exist - a missing report means the runner did not do its
    job, and skipping here used to hide exactly that.
    """
    raw = os.environ.get("FARNA_STRICT_TRANSPORT_REPORT", "").strip()
    if not raw:
        pytest.skip("no FARNA_STRICT_TRANSPORT_REPORT supplied - the runner enforces the ratchet")

    report_path = Path(raw)
    assert report_path.exists(), f"FARNA_STRICT_TRANSPORT_REPORT is set to {report_path} but the file does not exist: the strict runner did not produce a report, so the ratchet cannot be checked"

    baseline = _load_baseline()
    baseline_keys = _baseline_keys(baseline)
    declared_modules = set(baseline["modules"])
    report_keys = _transport_strict.collect_report_keys(report_path)

    new: list[str] = []
    for node, kind, fingerprint in sorted(report_keys - baseline_keys):
        if node.split("::", 1)[0] not in declared_modules and node != _transport_strict.SESSION_NODEID:
            continue
        new.append(f"{node} [{kind}] {fingerprint}")

    assert not new, "Strict transport violations not recorded in the baseline (the ratchet is shrink-only):\n  " + "\n  ".join(new) + "\nFix the test, or record a pre-existing violation deliberately with `python scripts/run_transport_strict.py --write-baseline` and justify it in the PR."


def test_ratchet_fails_when_report_env_points_at_a_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A set-but-missing report is a failure, never a skip.

    CI runs the architecture ratchet unconditionally against the runner's
    report. If the runner crashed before writing it, the ratchet must say so
    instead of quietly passing.
    """
    monkeypatch.setenv("FARNA_STRICT_TRANSPORT_REPORT", str(tmp_path / "missing-report.jsonl"))

    with pytest.raises(AssertionError, match="does not exist"):
        test_baseline_ratchet_against_strict_report()


# ---------------------------------------------------------------------------
# CI wiring: the gate must not be able to pass without a report
# ---------------------------------------------------------------------------
def _transport_job_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]["transport-policy"]["steps"]


def _step_with_text(steps: list[dict[str, Any]], *needles: str) -> dict[str, Any]:
    for step in steps:
        run = step.get("run") or ""
        if all(needle in run for needle in needles):
            return step
    raise AssertionError(f"no CI step runs a command containing {needles}")


def test_ci_transport_runner_fails_when_no_report_is_written() -> None:
    """The CI transport step asserts the report exists after the runner."""
    step = _step_with_text(_transport_job_steps(), "run_transport_strict.py")
    run = step["run"]

    assert "transport-strict-report.jsonl" in run
    assert 'test -f "$RUNNER_TEMP/transport-strict-report.jsonl"' in run
    # The runner invokes pytest itself; its argv carries both warning flags
    # (pinned below by the script-level assertion).
    assert "python scripts/run_transport_strict.py" in run


def test_strict_runner_pytest_argv_carries_the_repo_warning_flags() -> None:
    """The runner's own pytest invocation keeps the repo warning gate.

    ``run_transport_strict.py`` builds the argv itself, so the generic
    command-scanner sees only the escalation it already requires. The repo-wide
    ``error::ResourceWarning`` filter must travel with it too.
    """
    text = STRICT_RUNNER_PATH.read_text(encoding="utf-8")

    assert '"error::pytest.PytestUnraisableExceptionWarning",' in text
    assert '"error::ResourceWarning",' in text
    assert '"--disable-warnings",' in text


def test_ci_ratchet_step_runs_unconditionally_over_the_same_report() -> None:
    """The architecture ratchet runs even when the runner step failed."""
    step = _step_with_text(_transport_job_steps(), "test_transport_policy.py")
    run = step["run"]

    assert step.get("if") == "always()"
    assert "FARNA_STRICT_TRANSPORT_REPORT" in step.get("env", {})
    assert "error::ResourceWarning" in run
    assert "error::pytest.PytestUnraisableExceptionWarning" in run


def test_ci_contract_roundtrip_diff_fails_closed() -> None:
    """A failed git diff must fail the contract job, not skip the ratchet."""
    workflow = yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["contract-roundtrip"]["steps"]
    detect = next(step for step in steps if step.get("name") == "Detect changed API route / schema files")
    code = "\n".join(line for line in detect["run"].splitlines() if not line.lstrip().startswith("#"))

    assert "set -euo pipefail" in code
    assert "|| true" not in code
    assert "api_files" in code

    roundtrip = next(step for step in steps if step.get("name") == "Run contract round-trip")
    assert "error::ResourceWarning" in roundtrip["run"]
    assert "error::pytest.PytestUnraisableExceptionWarning" in roundtrip["run"]


# ---------------------------------------------------------------------------
# Meta-test: prove the instrumentation is live, not merely imported
# ---------------------------------------------------------------------------
def test_fingerprint_ratchet_distinguishes_a_second_stub_in_one_test(tmp_path: Path) -> None:
    """A test with ONE baselined dead stub must fail when it grows a SECOND one.

    Keying on ``(nodeid, kind)`` was the original hole: any number of unused
    routes in an already-baselined test passed. The fingerprint makes the second
    stub a distinct identity, so the baseline comparison reports it.
    """
    node = "tests/unit/example/test_example.py::test_with_one_baselined_stub"
    first = _transport_strict.Violation(node, _transport_strict.UNUSED_ROUTE, "GET https://a.example/one")
    second = _transport_strict.Violation(node, _transport_strict.UNUSED_ROUTE, "GET https://a.example/two")

    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps({"version": 2, "modules": ["tests/unit/example/test_example.py"], "violations": {node: [{"kind": first.kind, "fingerprint": first.fingerprint}]}}),
        encoding="utf-8",
    )

    _transport_strict.configure_baseline(baseline_file)
    try:
        assert not _transport_strict.unbaselined([first])
        assert _transport_strict.unbaselined([first, second]) == [second]
        assert _transport_strict.unbaselined([second]) == [second]
    finally:
        _transport_strict.configure_baseline(_transport_strict.DEFAULT_BASELINE)


def test_sentinel_router_makes_an_unmocked_request_fail() -> None:
    """Requirement (a): a raw httpx call with no router must fail in strict mode.

    The autouse fixture installs this router for every strict test; this drives
    it directly so the proof does not depend on the env flag.
    """
    import httpx
    from respx.models import AllMockedAssertionError

    _transport_strict.install(force=True)
    _transport_strict.start_sentinel()
    try:
        with pytest.raises(AllMockedAssertionError):
            httpx.get("https://sentinel.example/unmocked", timeout=5)
    finally:
        _transport_strict.stop_sentinel()


def test_findings_outside_a_test_go_to_the_session_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requirement: session-scoped activity is not dropped.

    ``_record`` with no current test used to return silently. A module- or
    session-scoped router stopped outside any item is a real finding, so it is
    bucketed and failed at session finish.
    """
    _transport_strict.install(force=True)
    monkeypatch.setattr(_transport_strict, "_report_path", None)
    monkeypatch.setattr(_transport_strict._state, "test", None)
    _transport_strict.finish_session()  # start clean

    _transport_strict._record(_transport_strict.UNUSED_ROUTE, "session-scoped detail")
    violations = _transport_strict.finish_session()

    assert [(violation.test, violation.kind) for violation in violations] == [(_transport_strict.SESSION_NODEID, _transport_strict.UNUSED_ROUTE)]


def test_instrumentation_records_every_violation_kind() -> None:
    """Drive the mechanism directly: one dead stub, one unmatched, one bare write.

    Without this, every other test here could pass while the hooks were never
    installed (or never recorded anything), because a report with zero rows also
    satisfies the ratchet. ``install(force=True)`` makes the test independent of
    the opt-in env flag.
    """
    import httpx
    import respx

    _transport_strict.install(force=True)
    assert _transport_strict.enabled() or _transport_strict._installed

    router = respx.MockRouter(assert_all_mocked=False, assert_all_called=False)
    router.start()
    try:
        router.get("https://meta.example/unused")  # registered, never called
        router.post("https://meta.example/api/v1/thing", json={"ok": True})
        httpx.get("https://meta.example/unmatched", timeout=5)
        httpx.post("https://meta.example/api/v1/thing", json={"ok": True}, timeout=5)
    finally:
        router.stop()

    # Drain inside the test so the report hook does not also fail it; the
    # findings are persisted either way (drain persists), which is what puts
    # this test's fingerprint in the baseline.
    violations = _transport_strict.drain_violations()
    kinds = {violation.kind for violation in violations}

    assert _transport_strict.UNMATCHED_REQUEST in kinds, violations
    assert _transport_strict.UNUSED_ROUTE in kinds, violations
    assert _transport_strict.MISSING_WRITE_HEADERS in kinds, violations
