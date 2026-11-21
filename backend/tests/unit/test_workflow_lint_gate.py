"""Contract gate for the ``actionlint`` workflow-lint job (FAR-2606).

The job proves two things: actionlint's structural findings, and shellcheck's
SC* findings inside ``run:`` blocks. It used to prove both in one ``actionlint``
invocation, which runs shellcheck once per run block; that per-block spawn made
the job cross its 10-minute ``timeout-minutes`` on every run from 2026-11-20
onward, so every PR carried a red check.

The fix splits the two proofs into two steps in the same job::

    actionlint -shellcheck=                                  # structural
    python3 scripts/run_check_workflow_shellcheck.py         # shellcheck

Splitting is safe only while BOTH steps exist and both stay fatal. A
``-shellcheck=`` with no shellcheck step is a green check that checked nothing -
exactly the silent-skip the job's install step exists to prevent - and a
``continue-on-error`` (or a ``|| true``) on either step would do the same while
looking wired up. These tests are the only gate on that: CI does not execute
this workflow's logic, it *is* the workflow.

The helper's own fidelity rules (shell resolution, expression substitution,
line mapping, the disabled-rule list, and the refusal to pass without
shellcheck) are pinned here too, because each of them is a way to weaken the
gate without touching the workflow file.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_LINT = REPO_ROOT / ".github" / "workflows" / "workflow-lint.yml"
HELPER_SCRIPT = REPO_ROOT / "scripts" / "run_check_workflow_shellcheck.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import run_check_workflow_shellcheck as helper  # noqa: E402

_GATE_JOB = "actionlint"
_HELPER_INVOCATION = "python3 scripts/run_check_workflow_shellcheck.py"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_LINT.read_text(encoding="utf-8"))


def _gate_job() -> dict[str, Any]:
    return _workflow()["jobs"][_GATE_JOB]


def _gate_steps() -> list[dict[str, Any]]:
    return list(_gate_job()["steps"])


def _step_running(needle: str) -> dict[str, Any]:
    matches = [step for step in _gate_steps() if needle in str(step.get("run", ""))]
    assert len(matches) == 1, f"expected exactly one gate step whose run: contains {needle!r}, found {len(matches)}"
    return matches[0]


# --------------------------------------------------------------------------- #
# The gate keeps its name, its structure and both proofs
# --------------------------------------------------------------------------- #


def test_gate_job_keeps_the_actionlint_check_name() -> None:
    """Nothing consumes the literal name today; keeping it is what guarantees that."""
    job = _gate_job()
    assert job["name"] == _GATE_JOB, "the check name is part of the job contract - do not rename or matrix it away"


def test_structural_step_runs_actionlint_with_its_own_shellcheck_disabled() -> None:
    step = _step_running("actionlint -shellcheck=")
    assert step["run"].strip().splitlines()[-1].strip() == "actionlint -shellcheck=", "the structural step must run actionlint with shellcheck disabled; leaving actionlint's own shellcheck on is what timed the job out"


def test_a_separate_step_runs_shellcheck_over_the_run_blocks() -> None:
    step = _step_running(_HELPER_INVOCATION)
    assert "python3" in step["run"], "the shellcheck half of the gate must actually run"


def test_neither_gate_step_may_be_made_non_fatal() -> None:
    """A non-fatal step is a green check that proves nothing."""
    for step in _gate_steps():
        name = step.get("name", "<unnamed>")
        assert step.get("continue-on-error") in (None, False), f"{name}: continue-on-error defeats this gate"
        script = str(step.get("run", ""))
        for swallow in ("|| true", "|| :", "|| exit 0"):
            assert swallow not in script, f"{name}: '{swallow}' would hide a failing gate"


def test_the_helper_the_shellcheck_step_runs_exists() -> None:
    assert HELPER_SCRIPT.is_file(), f"missing {HELPER_SCRIPT.relative_to(REPO_ROOT)} - the shellcheck step would fail"


def test_disabled_rule_list_matches_the_pinned_actionlint() -> None:
    """Pinned to what actionlint 1.7.12 passes, captured from its argv.

    Widening this list silences findings actionlint would report (a weaker
    gate); narrowing it turns a currently green tree red on rules actionlint
    deliberately suppresses. Either direction is a deliberate change.
    """
    assert helper.SHELLCHECK_DISABLED_RULES == (
        "SC1091",
        "SC2194",
        "SC2050",
        "SC2153",
        "SC2154",
        "SC2157",
        "SC2043",
    )


# --------------------------------------------------------------------------- #
# Helper fidelity: which blocks get checked, and how
# --------------------------------------------------------------------------- #

_FIXTURE_WORKFLOW = """\
name: fixture
on: push
jobs:
  linux:
    runs-on: ubuntu-24.04
    defaults:
      run:
        shell: sh
    steps:
      - name: inherits the job default
        run: |
          echo $INHERITED
      - name: overrides the job default
        run: |
          echo $OVERRIDDEN
        shell: bash
      - name: powershell is not shellcheckable
        run: |
          Write-Host $NOPE
        shell: pwsh
      - uses: actions/checkout@v4
  windows:
    runs-on: windows-latest
    steps:
      - name: windows default shell is pwsh
        run: |
          Write-Host $ALSO_NOPE
      - name: windows step pinned to bash is checked
        run: |
          echo $PINNED
        shell: bash
"""


def _write_fixture(tmp_path: Path, name: str = "fixture.yml", body: str = _FIXTURE_WORKFLOW) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    path = workflows / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def test_shell_resolution_matches_actionlint(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    blocks, skipped = helper._collect_blocks(path, ".github/workflows/fixture.yml")
    by_step = {block.step: block.dialect for block in blocks}
    assert by_step == {
        "inherits the job default": "sh",
        "overrides the job default": "bash",
        "windows step pinned to bash is checked": "bash",
    }, "the resolved shell decides the dialect, and only bash/sh blocks are checked"
    assert skipped == {"pwsh": 2}, "Windows-defaulted and explicit pwsh blocks are skipped, exactly as actionlint skips them"


def test_expressions_are_replaced_with_equal_width_underscores(tmp_path: Path) -> None:
    body = "name: fixture\non: push\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: |\n          echo ${{ github.sha }} done\n"
    path = _write_fixture(tmp_path, body=body)
    blocks, _ = helper._collect_blocks(path, ".github/workflows/fixture.yml")
    expected = f"echo {'_' * len('${{ github.sha }}')} done\n"
    assert blocks[0].script == expected, "expressions must not reach shellcheck as broken expansions"


def test_block_line_numbers_match_the_workflow_file(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    blocks, _ = helper._collect_blocks(path, ".github/workflows/fixture.yml")
    lines = path.read_text(encoding="utf-8").splitlines()
    for block in blocks:
        first_script_line = block.script.splitlines()[0]
        assert lines[block.first_line - 1].strip() == first_script_line.strip(), f"{block.step}: first_line {block.first_line} does not point at the block's first script line"


# --------------------------------------------------------------------------- #
# Helper failure modes are loud, never a silent pass
# --------------------------------------------------------------------------- #


def _no_shellcheck_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(helper.shutil, "which", lambda _name: None)


def test_helper_refuses_to_run_without_shellcheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_shellcheck_on_path(monkeypatch)
    _write_fixture(tmp_path)
    assert helper.main(["--workflows-dir", str(tmp_path / ".github" / "workflows")]) == 2


def test_helper_refuses_when_a_shellcheck_invocation_errors(tmp_path: Path) -> None:
    """A tool failure must never read as a clean tree."""
    _write_fixture(tmp_path)
    rc = helper.main(
        [
            "--workflows-dir",
            str(tmp_path / ".github" / "workflows"),
            # Python rejects the shellcheck flags the helper passes, so this
            # stands in for a broken shellcheck: exit code 2, no findings.
            "--shellcheck",
            sys.executable,
        ]
    )
    assert rc == 2


def test_helper_refuses_a_workflow_tree_with_nothing_to_check(tmp_path: Path) -> None:
    body = "name: fixture\non: push\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo hi\n        shell: pwsh\n"
    _write_fixture(tmp_path, body=body)
    assert helper.main(["--workflows-dir", str(tmp_path / ".github" / "workflows")]) == 2


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed on this host")
def test_a_real_finding_fails_the_helper_and_names_the_workflow(tmp_path: Path) -> None:
    body = "name: fixture\non: push\njobs:\n  a:\n    runs-on: ubuntu-24.04\n    steps:\n      - name: unquoted expansion\n        run: |\n          echo $UNQUOTED\n"
    _write_fixture(tmp_path, body=body)
    assert helper.main(["--workflows-dir", str(tmp_path / ".github" / "workflows")]) == 1
