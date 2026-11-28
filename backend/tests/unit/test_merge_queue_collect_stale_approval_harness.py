"""Behavioural harness for the merge-queue collect step's stale-approval gate (FAR-2871).

2026-11-28: a force-push made the approval on the oldest open PR stale. The
merge step's SHA-pin guard refused it, but the collect step did not apply that
guard, so with ``BATCH_LIMIT=1`` the PR took the only slot on every pass and
nothing merged for over three hours.

This runs the REAL ``run:`` block of the ``collect`` step (read out of the
workflow YAML, never copied) under ``bash --noprofile --norc -eo pipefail`` with
the real guard scripts and a stubbed ``gh``. Only the GitHub expression and the
step's ``/tmp/`` paths are substituted.

The harness skips when the box has no ``bash`` with ``jq``; CI's Linux runners
have both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "merge-queue.yml"
REPO = "farnalabs/farna-control"

STALE_PR = 3648
FRESH_PR = 3650
HEAD = "a" * 40

APPROVED_AT = "2026-11-28T07:51:11Z"
BEFORE_APPROVAL = "2026-11-28T07:00:00Z"
AFTER_APPROVAL = "2026-11-28T09:11:43Z"


def _bash_probe() -> str | None:
    """The bash that can run the step, or None."""
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv; the interpreter is what shutil.which found
            [bash, "--noprofile", "--norc", "-c", "command -v jq >/dev/null 2>&1"],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return None
    return bash if completed.returncode == 0 else None


_BASH = _bash_probe()

pytestmark = pytest.mark.skipif(_BASH is None, reason="FAR-2871 harness needs bash with jq")


_GH_STUB = """#!/usr/bin/env bash
# FAR-2871 harness stub for `gh`: serves per-PR fixtures from $FIXTURES.
case "$1 $2" in
  "pr list") cat "$FIXTURES/pr_list.json" ;;
  "pr view")
    case "$*" in
      *"--json reviews"*) cat "$FIXTURES/approvers_$3" ;;
      *"--json labels"*) cat "$FIXTURES/labels_$3" 2>/dev/null || true ;;
      *"--json headRefOid"*) echo "__HEAD__" ;;
    esac
    ;;
  "run list") echo "success" ;;
  "api "*)
    url="$2"
    if [ "$url" = "--paginate" ]; then url="$3"; fi
    num=$(printf '%s' "$url" | sed -n 's#.*/pulls/\\([0-9][0-9]*\\).*#\\1#p')
    case "$url" in
      */reviews*) cat "$FIXTURES/reviews_$num.json" || exit 1 ;;
      */commits/*/check-runs*) echo "success" ;;
      */commits*) cat "$FIXTURES/commits_$num.json" || exit 1 ;;
      */files*) echo "0" ;;
      *) cat "$FIXTURES/pull.json" ;;
    esac
    ;;
  *) echo "stub gh: unsupported invocation: $*" >&2; exit 1 ;;
esac
"""


def _collect_run_block() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["queue"]["steps"]:
        if isinstance(step, dict) and step.get("id") == "collect":
            return str(step["run"])
    raise AssertionError("merge-queue.yml has no 'collect' step")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")


class Harness:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.fixtures = work / "fixtures"
        _write(work / "bin" / "gh", _GH_STUB.replace("__HEAD__", HEAD))
        # `python3` is not on PATH everywhere (Windows): point it at this interpreter.
        _write(work / "bin" / "python3", f'#!/usr/bin/env bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n')
        for stub in (work / "bin").iterdir():
            stub.chmod(0o755)
        same_repo = {"head": {"repo": {"full_name": REPO}}, "author_association": "MEMBER", "user": {"login": "farnalabs"}}
        _write(self.fixtures / "pull.json", json.dumps(same_repo))
        self.prs: list[dict[str, Any]] = []

    def add_pr(self, number: int, *, commit_date: str, approved: bool = True) -> None:
        """An open PR, oldest first. ``commit_id`` always equals the head (#3648)."""
        self.prs.append(
            {"number": number, "title": f"PR {number}", "headRefName": f"b{number}", "baseRefName": "main", "createdAt": f"2026-11-28T0{len(self.prs)}:00:00Z"},
        )
        reviews = [{"state": "APPROVED", "submitted_at": APPROVED_AT, "commit_id": HEAD}] if approved else []
        commits = [{"sha": HEAD, "parents": [{"sha": "p"}], "commit": {"committer": {"date": commit_date}}}]
        _write(self.fixtures / f"approvers_{number}", "farna-reviewbot\n" if approved else "")
        _write(self.fixtures / f"reviews_{number}.json", json.dumps(reviews))
        _write(self.fixtures / f"commits_{number}.json", json.dumps(commits))

    def run(self) -> tuple[list[int], str, str]:
        """Return ``(ready PR numbers, step log, step summary)``."""
        _write(self.fixtures / "pr_list.json", json.dumps(self.prs))
        fragment = _collect_run_block().replace("${{ github.repository }}", REPO)
        fragment = fragment.replace("/tmp/", "tmp/")
        (self.work / "tmp").mkdir(exist_ok=True)
        # The step calls its helpers by repo-relative path.
        shutil.copytree(REPO_ROOT / ".github" / "scripts", self.work / ".github" / "scripts")
        _write(self.work / "step.sh", 'export PATH="$PWD/bin:$PATH"\n' + fragment)
        env = {
            **os.environ,
            "FIXTURES": "fixtures",
            "GITHUB_OUTPUT": "output.txt",
            "GITHUB_STEP_SUMMARY": "summary.txt",
        }
        completed = subprocess.run(  # noqa: S603 - fixed argv; the interpreter is what shutil.which found
            [_BASH or "bash", "--noprofile", "--norc", "-eo", "pipefail", "step.sh"],
            cwd=self.work,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=100,
            check=False,
        )
        log = completed.stdout + completed.stderr
        assert completed.returncode == 0, log
        ready = json.loads((self.work / "tmp" / "prs.json").read_text(encoding="utf-8"))
        summary_path = self.work / "summary.txt"
        summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        return [pr["number"] for pr in ready], log, summary


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_a_stale_approval_does_not_take_the_batch_slot(harness: Harness) -> None:
    harness.add_pr(STALE_PR, commit_date=AFTER_APPROVAL)
    harness.add_pr(FRESH_PR, commit_date=BEFORE_APPROVAL)

    ready, log, summary = harness.run()

    assert ready == [FRESH_PR]
    assert f"::warning::PR #{STALE_PR} is NOT ready (no batch slot)" in log
    assert "approval stale" in log
    assert str(STALE_PR) in summary


def test_an_unreadable_pr_is_not_ready_and_does_not_stop_the_others(harness: Harness) -> None:
    harness.add_pr(STALE_PR, commit_date=BEFORE_APPROVAL)
    harness.add_pr(FRESH_PR, commit_date=BEFORE_APPROVAL)
    (harness.fixtures / f"commits_{STALE_PR}.json").unlink()

    ready, log, _summary = harness.run()

    assert ready == [FRESH_PR]
    assert "could not fetch reviews/commits - fail closed" in log


def test_the_fast_lane_path_without_an_approval_is_unchanged(harness: Harness) -> None:
    harness.add_pr(FRESH_PR, commit_date=AFTER_APPROVAL, approved=False)
    _write(harness.fixtures / f"labels_{FRESH_PR}", "fast-lane:test-infra\n")

    ready, log, _summary = harness.run()

    assert ready == [FRESH_PR]
    assert "fast-lane eligible" in log


def test_a_leftover_held_back_file_from_an_earlier_pass_is_cleared(harness: Harness) -> None:
    harness.add_pr(FRESH_PR, commit_date=BEFORE_APPROVAL)
    _write(harness.work / "tmp" / "stale_approval_prs.txt", f"{STALE_PR}\n")

    ready, log, summary = harness.run()

    assert ready == [FRESH_PR]
    assert "Held back" not in log
    assert "Held back" not in summary
