"""Unit tests for scripts/provider_contract_probe_report.py (FAR-2703).

The nightly probe's issue body used to be built by an inline ``python3 -`` heredoc
inside ``.github/workflows/provider-contract-probe.yml``. Nothing executed it, so
nothing could catch a defect in it - and one was there: the bootstrap predicate
checked only ``attempted == 0`` and the skip reasons, so a real ``error`` row that
never issued a request (a malformed golden entry) classified the run as the
expected bootstrap red and the issue was silently suppressed.

These tests pin the contract that makes the issue trustworthy:

* the deliberate bootstrap red (nothing recorded live yet) raises nothing;
* an ``error`` during bootstrap is NOT silent - it produces a body;
* a drift run produces the summary line and the per-probe table;
* ``pass`` rows never reach the table (the issue shows only what needs a human);
* a run that died before writing a report is still raised;
* ``main`` drives the body file the workflow step tests with ``[ ! -s ]``.

One wiring test reads the workflow itself: it must call this script and must not
go back to an untestable heredoc.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

_REPO_ROOT = None
for _parent in Path(__file__).resolve().parents:
    if (_parent / "scripts" / "provider_contract_probe_report.py").exists():
        _REPO_ROOT = _parent
        break
if _REPO_ROOT is None:
    raise RuntimeError("Could not find repo root (scripts/provider_contract_probe_report.py)")

_SCRIPT_PATH = _REPO_ROOT / "scripts" / "provider_contract_probe_report.py"
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "provider-contract-probe.yml"

_loader = SourceFileLoader("provider_contract_probe_report", str(_SCRIPT_PATH))
_module = module_from_spec(spec_from_loader("provider_contract_probe_report", _loader))
sys.modules["provider_contract_probe_report"] = _module
_loader.exec_module(_module)
reporting = _module

_RUN_URL = "https://github.com/farnalabs/farna-control/actions/runs/123456"


def _summary(**overrides: int) -> dict[str, int]:
    """A report summary with every counter at zero, plus *overrides*."""
    summary: dict[str, int] = {
        "total": 0,
        "attempted": 0,
        "passed": 0,
        "drifted": 0,
        "skipped": 0,
        "skipped_unexcused": 0,
        "unavailable": 0,
        "errors": 0,
        "additions": 0,
        "expected": 0,
        "skipped_by_design": 0,
    }
    summary.update(overrides)
    return summary


def _row(provider: str, operation: str, status: str, **fields: Any) -> dict[str, Any]:
    return {"provider": provider, "operation": operation, "status": status, **fields}


def _bootstrap_skip(provider: str) -> dict[str, Any]:
    """The row a probe emits while its golden has not been recorded live yet."""
    return _row(
        provider,
        "pull",
        "skipped",
        attempted=False,
        allowed=True,
        reason=f"golden for {provider}/pull is not yet recorded; record it behind review",
    )


def _write_report(tmp_path: Path, report: dict[str, Any]) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_the_bootstrap_red_raises_nothing(tmp_path: Path) -> None:
    """Zero attempts and every skip "not yet recorded" is the expected red."""
    results = [_bootstrap_skip("github"), _bootstrap_skip("slack")]
    report = {"summary": _summary(total=2, skipped=2), "results": results}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert not body


def test_an_error_during_bootstrap_is_not_silent(tmp_path: Path) -> None:
    """A probe that errored without issuing a request must still raise the issue.

    This is the regression: ``attempted == 0`` and the skip reasons alone were
    read as "bootstrap", so this report produced no issue at all.
    """
    error = _row("github", "pull", "error", attempted=False, error="malformed golden entry (no shape)")
    results = [_bootstrap_skip("slack"), error]
    report = {"summary": _summary(total=2, skipped=1, errors=1, expected=1), "results": results}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert body
    assert _RUN_URL in body
    assert "errors=1" in body
    assert "| github/pull | error | malformed golden entry (no shape) |" in body


def test_an_unavailable_probe_during_bootstrap_is_not_silent(tmp_path: Path) -> None:
    """An unavailable probe is drift-shaped failure too, not the bootstrap red."""
    unavailable = _row("github", "pull", "unavailable", attempted=False, error="provider_unavailable")
    results = [_bootstrap_skip("slack"), unavailable]
    report = {"summary": _summary(total=2, skipped=1, unavailable=1, expected=1), "results": results}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert "| github/pull | unavailable | provider_unavailable |" in body


def test_a_report_missing_both_tables_is_raised_as_malformed(tmp_path: Path) -> None:
    """A report with no summary/results is unreadable, never the expected red."""
    body = reporting.build_body(_write_report(tmp_path, {"mode": "check"}), _RUN_URL)

    assert "report is malformed" in body


def test_a_run_that_never_wrote_a_report_is_still_raised(tmp_path: Path) -> None:
    """An install/setup failure means there is no report; the issue must say so."""
    body = reporting.build_body(tmp_path / "absent.json", _RUN_URL)

    assert _RUN_URL in body
    assert "failed before the probe could write a report" in body


# ---------------------------------------------------------------------------
# Drift and the table
# ---------------------------------------------------------------------------


def test_a_drift_report_carries_the_summary_and_the_probe_table(tmp_path: Path) -> None:
    drift = _row(
        "github",
        "pull",
        "drift",
        attempted=True,
        status_code=200,
        attempts=1,
        drift=[{"kind": "missing", "path": "$.state", "expected": "string", "actual": None}],
        additions=[],
    )
    report = {"summary": _summary(total=1, attempted=1, drifted=1, expected=1), "results": [drift]}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert body.startswith("Nightly provider contract probe failed. Latest failing run: ")
    assert _RUN_URL in body
    assert "Summary: attempted=1, passed=0, drifted=1, unavailable=0, errors=0, skipped=0, skipped_unexcused=0" in body
    assert "| Probe | Status | Detail |" in body
    assert "| github/pull | drift | missing $.state |" in body


def test_pass_rows_never_reach_the_table(tmp_path: Path) -> None:
    """The issue shows only what needs a human, so a clean probe adds no row."""
    passed = _row("slack", "auth_test", "pass", attempted=True, status_code=200, attempts=1, drift=[], additions=[])
    drift = _row("github", "pull", "drift", attempted=True, drift=[{"kind": "missing", "path": "$.state"}], additions=[])
    report = {"summary": _summary(total=2, attempted=2, passed=1, drifted=1, expected=2), "results": [passed, drift]}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert "| github/pull | drift | missing $.state |" in body
    assert "slack/auth_test" not in body
    assert "| pass |" not in body
    assert "passed=1" in body


def test_an_unexcused_skip_reaches_the_table_with_its_reason(tmp_path: Path) -> None:
    """An allow-list omission is a failure with a reason, so it is listed."""
    unexcused = _row("github", "pull", "skipped", attempted=False, allowed=False, reason="missing credential: GITHUB_TOKEN")
    report = {"summary": _summary(total=1, skipped=1, skipped_unexcused=1, expected=1), "results": [unexcused]}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert "| github/pull | skipped | missing credential: GITHUB_TOKEN |" in body


def test_a_pipe_in_a_detail_is_escaped_for_the_table(tmp_path: Path) -> None:
    """A provider error containing ``|`` must not break the markdown table."""
    drift = _row("github", "pull", "drift", attempted=True, drift=[{"kind": "type", "path": "$.state|name"}], additions=[])
    report = {"summary": _summary(total=1, attempted=1, drifted=1, expected=1), "results": [drift]}

    body = reporting.build_body(_write_report(tmp_path, report), _RUN_URL)

    assert "| github/pull | drift | type $.state\\|name |" in body


# ---------------------------------------------------------------------------
# The workflow-side entry point
# ---------------------------------------------------------------------------


def test_main_leaves_the_body_file_empty_for_the_bootstrap_red(tmp_path: Path) -> None:
    """The shell step raises nothing when the body file stays empty."""
    report = _write_report(tmp_path, {"summary": _summary(total=1, skipped=1), "results": [_bootstrap_skip("github")]})
    body_file = tmp_path / "body.md"
    body_file.write_text("", encoding="utf-8")

    exit_code = reporting.main([str(report), str(body_file), _RUN_URL])

    assert exit_code == 0
    assert not body_file.read_text(encoding="utf-8")


def test_main_writes_the_body_for_a_drift(tmp_path: Path) -> None:
    drift = _row("github", "pull", "drift", attempted=True, drift=[{"kind": "missing", "path": "$.state"}], additions=[])
    report = _write_report(tmp_path, {"summary": _summary(total=1, attempted=1, drifted=1, expected=1), "results": [drift]})
    body_file = tmp_path / "body.md"

    exit_code = reporting.main([str(report), str(body_file), _RUN_URL])

    assert exit_code == 0
    assert "| github/pull | drift | missing $.state |" in body_file.read_text(encoding="utf-8")


def test_main_rejects_a_missing_argument() -> None:
    """The usage is positional; a wrong arity must not silently write nothing."""
    assert reporting.main(["report.json"]) == 2


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_the_workflow_raises_issues_through_this_script() -> None:
    """Pinned so the issue body cannot go back to an unexecuted heredoc.

    A heredoc has no test; this module does. The workflow must call it, and the
    inline ``python3 - <<'PY'`` block must not come back.
    """
    workflow = _WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "python3 scripts/provider_contract_probe_report.py" in workflow
    assert "<<'PY'" not in workflow
