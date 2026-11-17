"""Unit tests for scripts/main_failure_fix_pr_open.sh — the dispatcher-side
skip that stops a still-red main from re-dispatching the Branch Fixer when an
open fix PR already carries the failure's fingerprint.

The script runs for real in a subprocess with a stubbed ``gh`` first on PATH.
The stub serves a canned ``gh pr list`` JSON document (or fails), and logs every
``gh pr comment`` call so the evidence comment can be asserted.

Exit-code contract under test: 0 = open fix PR found (skip), 1 = none found
(dispatch), 2 = lookup failed (dispatch, fail-open).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "main_failure_fix_pr_open.sh"

#: Executes a shipped shell script with a stubbed binary on PATH: POSIX only
#: (see ``POSIX_ONLY_MARKER`` in backend/tests/conftest.py); Linux CI runs it.
pytestmark = pytest.mark.posix_only

FINGERPRINT = "CI: Fast Validation|main|sonarcloud-scan"
RUN_URL = "https://github.com/acme/repo/actions/runs/42"

GH_STUB = """\
#!/usr/bin/env bash
# Test stub for main_failure_fix_pr_open.sh. Consumes:
#   STUB_PR_LIST_FILE  JSON served for `gh pr list`
#   STUB_PR_LIST_FAIL  when set, `gh pr list` fails with that message
#   STUB_COMMENT_FAIL  when set, `gh pr comment` fails
#   STUB_LOG           file receiving one line per invocation
set -u
printf '%s\\n' "$*" >> "${STUB_LOG}"
case "$1 $2" in
  "pr list")
    if [ -n "${STUB_PR_LIST_FAIL:-}" ]; then
      echo "${STUB_PR_LIST_FAIL}" >&2
      exit 1
    fi
    cat "${STUB_PR_LIST_FILE}"
    ;;
  "pr comment")
    if [ -n "${STUB_COMMENT_FAIL:-}" ]; then
      echo "comment refused" >&2
      exit 1
    fi
    ;;
  *) exit 64 ;;
esac
"""


def _run(tmp_path: Path, prs: list[dict[str, object]], **extra_env: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)
    pr_list = tmp_path / "prs.json"
    pr_list.write_text(json.dumps(prs), encoding="utf-8")
    log = tmp_path / "gh.log"
    log.touch()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_PR_LIST_FILE": str(pr_list),
        "STUB_LOG": str(log),
        "FIX_FINGERPRINT": FINGERPRINT,
        "REPO": "acme/repo",
        "RUN_URL": RUN_URL,
        **extra_env,
    }
    result = subprocess.run(  # noqa: S603 — fixed argv, repo script under test
        ["bash", str(SCRIPT)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    return result, log.read_text(encoding="utf-8")


def _fix_pr_body(fingerprint: str) -> str:
    return f"Fixes main.\n\n<!-- branch-fixer-fingerprint: {fingerprint} -->\n"


def test_open_fix_pr_with_the_fingerprint_skips_the_dispatch(tmp_path: Path) -> None:
    prs = [{"number": 7, "body": "unrelated"}, {"number": 3214, "body": _fix_pr_body(FINGERPRINT)}]
    result, log = _run(tmp_path, prs)
    assert result.returncode == 0
    assert "already has open fix PR #3214" in result.stdout
    assert f"pr comment 3214 --repo acme/repo --body Main is still failing with the same fingerprint (`{FINGERPRINT}`) on a newer run: {RUN_URL}" in log


def test_lowest_numbered_matching_pr_is_reported(tmp_path: Path) -> None:
    prs = [{"number": 3216, "body": _fix_pr_body(FINGERPRINT)}, {"number": 3214, "body": _fix_pr_body(FINGERPRINT)}]
    result, _log = _run(tmp_path, prs)
    assert result.returncode == 0
    assert "open fix PR #3214" in result.stdout


def test_no_matching_pr_dispatches(tmp_path: Path) -> None:
    prs = [{"number": 7, "body": "unrelated"}, {"number": 8, "body": None}]
    result, log = _run(tmp_path, prs)
    assert result.returncode == 1
    assert "pr comment" not in log


def test_a_different_failure_identity_does_not_match(tmp_path: Path) -> None:
    prs = [{"number": 9, "body": _fix_pr_body("CI: Fast Validation|main|backend-test")}]
    result, _log = _run(tmp_path, prs)
    assert result.returncode == 1


def test_lookup_failure_fails_open_with_a_warning(tmp_path: Path) -> None:
    result, log = _run(tmp_path, [], STUB_PR_LIST_FAIL="HTTP 502: bad gateway")
    assert result.returncode == 2
    assert "::warning::Could not list open PRs" in result.stdout
    assert "dispatching the Branch Fixer anyway" in result.stdout
    assert "pr comment" not in log


def test_missing_fingerprint_fails_open(tmp_path: Path) -> None:
    result, _log = _run(tmp_path, [], FIX_FINGERPRINT="")
    assert result.returncode == 2
    assert "::warning::" in result.stdout


def test_comment_failure_still_skips_with_a_warning(tmp_path: Path) -> None:
    prs = [{"number": 3214, "body": _fix_pr_body(FINGERPRINT)}]
    result, _log = _run(tmp_path, prs, STUB_COMMENT_FAIL="1")
    assert result.returncode == 0
    assert "::warning::Could not comment the failing run on fix PR #3214" in result.stdout
