"""Unit tests for scripts/run_check_ratchet_baseline.py (FAR-2699).

The guard exists because a shrink-only baseline is not a control if raising it
still gets a branch green. It compares a counted baseline file in the working
tree against the same file at the base revision and fails on an ADDED entry or
a RAISED count. These tests pin both directions and the fail-closed branches:

* the comparison (added / raised fail; removed / lowered pass);
* the parser (comments, malformed lines, non-integer counts, duplicates);
* base resolution (explicit ``--base``, ``RATCHET_BASE_REV``, the PR merge
  commit's first parent, the ``HEAD^`` fallback) and the fail-closed cases
  (unresolvable explicit base, missing working-tree baseline);
* the legitimate "baseline introduced here" case (absent at base passes).

The end-to-end block runs the real script as a subprocess against a throwaway
git repository, so the git-reading paths are exercised for real rather than
mocked.
"""

from __future__ import annotations

import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_ratchet_baseline.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_ratchet_baseline.py)")

_loader = SourceFileLoader("run_check_ratchet_baseline", str(script_path))
mod = module_from_spec(spec_from_loader("run_check_ratchet_baseline", _loader))
_loader.exec_module(mod)

BASELINE = "backend/tests/architecture/brand_consistency_baseline.txt"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def test_parse_baseline_ignores_comments_and_blank_lines():
    text = "# comment\n\nfoo.py\t2\nbar.py\t0\n"

    assert mod.parse_baseline(text, source="t") == {"foo.py": 2, "bar.py": 0}


def test_parse_baseline_rejects_a_line_without_a_tab():
    with pytest.raises(mod.RatchetError, match="malformed"):
        mod.parse_baseline("no-tab-here\n", source="t")


def test_parse_baseline_rejects_a_non_integer_count():
    with pytest.raises(mod.RatchetError, match="not an integer"):
        mod.parse_baseline("foo.py\tmany\n", source="t")


def test_parse_baseline_rejects_a_negative_count():
    with pytest.raises(mod.RatchetError, match="negative"):
        mod.parse_baseline("foo.py\t-1\n", source="t")


def test_parse_baseline_rejects_a_duplicate_path():
    with pytest.raises(mod.RatchetError, match="duplicate"):
        mod.parse_baseline("foo.py\t1\nfoo.py\t2\n", source="t")


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def test_compare_flags_an_added_entry():
    added, raised = mod.compare({"a.py": 1}, {"a.py": 1, "b.py": 3})

    assert added == ["b.py"]
    assert not raised


def test_compare_flags_a_raised_count():
    added, raised = mod.compare({"a.py": 1}, {"a.py": 4})

    assert not added
    assert raised == [("a.py", 1, 4)]


def test_compare_allows_a_lowered_count_and_a_removed_entry():
    added, raised = mod.compare({"a.py": 4, "b.py": 2}, {"a.py": 1})

    assert not added
    assert not raised


def test_compare_allows_an_unchanged_baseline():
    added, raised = mod.compare({"a.py": 4}, {"a.py": 4})

    assert not added
    assert not raised


# ---------------------------------------------------------------------------
# End-to-end: real subprocess against a throwaway git repository
# ---------------------------------------------------------------------------
_TIMEOUT_SECONDS = 60


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test helper, fixed git argv
        ["git", *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
        timeout=30,
    )


def _clean_env() -> dict[str, str]:
    """Drop CI base hints so each test controls base resolution explicitly."""
    env = dict(os.environ)
    env.pop("GITHUB_BASE_REF", None)
    env.pop("RATCHET_BASE_REV", None)
    return env


def _make_repo(tmp_path: Path, baseline_text: str | None = "a.py\t1\nb.py\t2\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ratchet-test@example.com")
    _git(repo, "config", "user.name", "Ratchet Test")
    _git(repo, "config", "commit.gpgsign", "false")
    if baseline_text is not None:
        _write_baseline(repo, baseline_text)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _write_baseline(repo: Path, text: str) -> None:
    path = repo / BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run(repo: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = _clean_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603 - test helper, fixed argv (real script under test)
        [sys.executable, str(script_path), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=_TIMEOUT_SECONDS,
        env=env,
    )


def test_end_to_end_unchanged_baseline_passes(tmp_path):
    repo = _make_repo(tmp_path)

    proc = _run(repo, "--base", "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stderr


def test_end_to_end_raised_count_fails_naming_file_and_both_values(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t3\nb.py\t2\n")
    _commit_all(repo, "raise a.py from 1 to 3")

    proc = _run(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "RAISED" in proc.stderr
    assert "a.py" in proc.stderr
    assert "3 > 1" in proc.stderr


def test_end_to_end_added_entry_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t1\nb.py\t2\nc.py\t5\n")
    _commit_all(repo, "add c.py")

    proc = _run(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "ADDED" in proc.stderr
    assert "c.py" in proc.stderr


def test_end_to_end_lowered_count_passes(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t0\nb.py\t2\n")
    _commit_all(repo, "lower a.py from 1 to 0")

    proc = _run(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr


def test_end_to_end_removed_entry_passes(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t1\n")
    _commit_all(repo, "remove b.py from the baseline")

    proc = _run(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr


def test_end_to_end_unreadable_explicit_base_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)

    proc = _run(repo, "--base", "does-not-exist-xyz")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr


def test_end_to_end_missing_working_tree_baseline_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / BASELINE).unlink()

    proc = _run(repo, "--base", "HEAD")

    assert proc.returncode == 1, proc.stderr
    assert "missing from the working tree" in proc.stderr


def test_end_to_end_baseline_introduced_here_passes_with_notice(tmp_path):
    repo = _make_repo(tmp_path, baseline_text=None)
    _write_baseline(repo, "a.py\t2\n")
    _commit_all(repo, "introduce the baseline")

    proc = _run(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "introduces the baseline" in proc.stderr


def test_end_to_end_head_parent_fallback_catches_a_raise(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise a.py with no PR base")

    proc = _run(repo)

    assert proc.returncode == 1, proc.stderr
    assert "HEAD^" in proc.stderr


def test_end_to_end_ratchet_base_rev_hint_catches_a_raise_buried_in_the_push(tmp_path):
    repo = _make_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise a.py")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "unrelated tip commit")

    proc = _run(repo, extra_env={"RATCHET_BASE_REV": base_sha})

    assert proc.returncode == 1, proc.stderr
    assert "9 > 1" in proc.stderr


def test_end_to_end_pr_merge_commit_uses_first_parent(tmp_path):
    """A PR checkout is a merge commit; its first parent is the base tip."""
    repo = _make_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "feature")
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise on the feature branch")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")

    proc = _run(repo)

    assert proc.returncode == 1, proc.stderr
    assert "first parent of the PR merge commit" in proc.stderr
    assert base_sha == _git(repo, "rev-parse", "HEAD^").stdout.strip()
