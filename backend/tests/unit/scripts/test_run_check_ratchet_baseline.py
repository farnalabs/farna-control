"""Unit tests for scripts/run_check_ratchet_baseline.py (FAR-2699).

The guard exists because a shrink-only ratchet is not a control if loosening it
still gets a branch green. It compares every enabled ratchet in the working tree
against the same file at the base revision and fails on whichever change makes
that ratchet LOOSER - which is not always "bigger":

* a baseline gains an entry, or a count goes up;
* a coverage floor goes DOWN;
* vulture's ``--min-confidence`` goes UP;
* ``TINY_DIFF_THRESHOLD`` goes UP;
* a lint ignore / mypy suppression / allowlist entry is added.

The tests pin BOTH directions for every ratchet in the registry, the fail-closed
branches (missing file, unparseable file, unknown mypy option polarity), the
baseline-introduced-here case, and the legacy ``--baseline`` counted mode. The
end-to-end block runs the real script as a subprocess against a throwaway git
repository, so the git-reading paths are exercised for real rather than mocked.
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
# dataclasses resolves a class's module through sys.modules during decoration,
# so the loaded module must be registered before it is executed.
sys.modules[_loader.name] = mod
_loader.exec_module(mod)

BASELINE = "backend/tests/architecture/brand_consistency_baseline.txt"
ARCH = "backend/tests/architecture"


def _ratchet(key: str) -> mod.Ratchet:
    return next(ratchet for ratchet in mod.RATCHETS if ratchet.key == key)


# ---------------------------------------------------------------------------
# Counted-baseline parser (brand) - legacy mode, unchanged
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
# Option-like git arguments (argument-injection hardening)
# ---------------------------------------------------------------------------
def test_validated_git_arg_accepts_a_revision():
    assert mod._validated_git_arg("origin/main", "revision") == "origin/main"


@pytest.mark.parametrize("value", ["", "--output=/tmp/pwn", "-c core.pager=cat"])
def test_validated_git_arg_rejects_option_like_values(value):
    with pytest.raises(mod.RatchetError, match="option-like"):
        mod._validated_git_arg(value, "revision")


# ---------------------------------------------------------------------------
# Counted-baseline comparison (brand)
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
# Per-ratchet direction: loosening fails, tightening passes
# ---------------------------------------------------------------------------
_SHELL_BASE = 'uv run coverage report --include="src/pkg/auth/*" --fail-under=90\nuv run coverage report --fail-under=80\n'
_SHELL_LOOSENED = 'uv run coverage report --include="src/pkg/auth/*" --fail-under=89\nuv run coverage report --fail-under=80\n'
_SHELL_TIGHTENED = _SHELL_BASE.replace("fail-under=90", "fail-under=91")

_GATE_BASE = "COVERAGE_THRESHOLD = 90\nBRANCH_COVERAGE_THRESHOLD = 90\nTINY_DIFF_THRESHOLD = 5\n"
_GATE_LOOSENED = _GATE_BASE.replace("COVERAGE_THRESHOLD = 90", "COVERAGE_THRESHOLD = 89")
_GATE_TIGHTENED = _GATE_BASE.replace("COVERAGE_THRESHOLD = 90", "COVERAGE_THRESHOLD = 91")

_PYPROJECT_MYPY_BASE = '[tool.mypy]\nstrict = true\n\n[[tool.mypy.overrides]]\nmodule = "a"\nignore_missing_imports = true\n'
_PYPROJECT_MYPY_LOOSENED = '[tool.mypy]\nstrict = true\n\n[[tool.mypy.overrides]]\nmodule = ["a", "b"]\nignore_missing_imports = true\n'
_PYPROJECT_MYPY_TIGHTENED = '[tool.mypy]\nstrict = true\n\n[[tool.mypy.overrides]]\nmodule = "a"\n'

#: ``(key, base text, loosened text, tightened text)`` for every registry entry.
_CASES: dict[str, tuple[str, str, str]] = {
    "brand-consistency": ("a.md\t2\n", "a.md\t3\n", "a.md\t1\n"),
    "audit-coverage": ("a.py:f\nb.py:g\n", "a.py:f\nb.py:g\nc.py:h\n", "a.py:f\n"),
    "duplicate-test-body": (
        "p.py:<module>:test_a=test_b\np.py:<module>:test_c=test_d\n",
        "p.py:<module>:test_a=test_b\np.py:<module>:test_c=test_d\nq.py:<module>:test_e=test_f\n",
        "p.py:<module>:test_a=test_b\n",
    ),
    "duplicate-validation-predicate": (
        '{"block": ["a.py:f", "b.py:g"]}\n{"block": ["c.py:h", "d.py:i"]}\n',
        '{"block": ["a.py:f", "b.py:g"]}\n{"block": ["c.py:h", "d.py:i"]}\n{"block": ["e.py:j", "f.py:k"]}\n',
        '{"block": ["a.py:f", "b.py:g"]}\n',
    ),
    "noop-bdd-then": ("bdd/test_a.py:one\nbdd/test_a.py:two\n", "bdd/test_a.py:one\nbdd/test_a.py:two\nbdd/test_a.py:three\n", "bdd/test_a.py:one\n"),
    "self-asserting-bdd": ("# header only\n", "bdd/test_a.py:one\n", "# still empty\n"),
    "coverage-floor": ("[tool.coverage.report]\nfail_under = 80\n", "[tool.coverage.report]\nfail_under = 78\n", "[tool.coverage.report]\nfail_under = 85\n"),
    "coverage-thresholds": (_SHELL_BASE, _SHELL_LOOSENED, _SHELL_TIGHTENED),
    "coverage-gate": (_GATE_BASE, _GATE_LOOSENED, _GATE_TIGHTENED),
    "vulture-confidence": ('cmd = ["vulture", "--min-confidence", "60"]\n', 'cmd = ["vulture", "--min-confidence", "70"]\n', 'cmd = ["vulture", "--min-confidence", "50"]\n'),
    "ruff-per-file-ignores": (
        '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101"]\n',
        '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101", "S102"]\n',
        "[tool.ruff.lint.per-file-ignores]\n",
    ),
    "mypy-overrides": (_PYPROJECT_MYPY_BASE, _PYPROJECT_MYPY_LOOSENED, _PYPROJECT_MYPY_TIGHTENED),
    "excluded-packages": (
        'EXCLUDED_PACKAGES = {"load", "performance"}\n',
        'EXCLUDED_PACKAGES = {"load", "performance", "extra"}\n',
        'EXCLUDED_PACKAGES = {"load"}\n',
    ),
    "gitleaks-allowlist-paths": (
        '[allowlist]\npaths = ["tests/", "docs/"]\n',
        '[allowlist]\npaths = ["tests/", "docs/", "configs/"]\n',
        '[allowlist]\npaths = ["tests/"]\n',
    ),
    "trivyignore": ("# header\nCVE-1\nCVE-2\n", "# header\nCVE-1\nCVE-2\nCVE-3\n", "# header\nCVE-1\n"),
    "semgrepignore": ("node_modules/\nbuild/\n", "node_modules/\nbuild/\ndist/\n", "node_modules/\n"),
    "sonar-coverage-exclusions": (
        "sonar.coverage.exclusions=tests/**,docs/**\n",
        "sonar.coverage.exclusions=tests/**,docs/**,scripts/**\n",
        "sonar.coverage.exclusions=tests/**\n",
    ),
}


def test_every_registry_ratchet_has_a_direction_case():
    assert set(_CASES) == {ratchet.key for ratchet in mod.RATCHETS}


@pytest.mark.parametrize("key", [ratchet.key for ratchet in mod.RATCHETS])
def test_ratchet_loosening_fails_and_tightening_passes(key):
    base_text, loosened_text, tightened_text = _CASES[key]
    ratchet = _ratchet(key)

    base = ratchet.parse(base_text, source="base")
    loosened = ratchet.parse(loosened_text, source="loosened")
    tightened = ratchet.parse(tightened_text, source="tightened")

    assert ratchet.loosenings(base, loosened)
    assert not ratchet.loosenings(base, tightened)


def test_coverage_gate_tiny_diff_is_looser_when_raised():
    ratchet = _ratchet("coverage-gate")
    base = ratchet.parse(_GATE_BASE, source="base")

    raised = ratchet.parse(_GATE_BASE.replace("TINY_DIFF_THRESHOLD = 5", "TINY_DIFF_THRESHOLD = 10"), source="raised")
    lowered = ratchet.parse(_GATE_BASE.replace("TINY_DIFF_THRESHOLD = 5", "TINY_DIFF_THRESHOLD = 4"), source="lowered")

    assert ratchet.loosenings(base, raised)
    assert not ratchet.loosenings(base, lowered)


def test_coverage_floor_removed_is_a_loosening():
    ratchet = _ratchet("coverage-floor")

    with pytest.raises(mod.RatchetError, match="fail_under"):
        ratchet.parse("[tool.coverage.report]\n", source="current")


def test_coverage_threshold_key_removed_is_a_loosening():
    ratchet = _ratchet("coverage-thresholds")
    base = ratchet.parse(_SHELL_BASE, source="base")
    current = ratchet.parse('uv run coverage report --include="src/pkg/auth/*" --fail-under=90\n', source="current")

    findings = ratchet.loosenings(base, current)

    assert findings
    assert "whole-suite" in findings[0]


def test_vulture_lowered_confidence_is_a_tightening():
    ratchet = _ratchet("vulture-confidence")
    base = ratchet.parse('cmd = ["vulture", "--min-confidence", "60"]\n', source="base")
    current = ratchet.parse('cmd = ["vulture", "--min-confidence", "40"]\n', source="current")

    assert not ratchet.loosenings(base, current)


def test_mypy_disable_error_code_gain_is_a_loosening():
    ratchet = _ratchet("mypy-overrides")
    base = ratchet.parse(_PYPROJECT_MYPY_BASE, source="base")
    current = ratchet.parse(
        '[tool.mypy]\nstrict = true\n\n[[tool.mypy.overrides]]\nmodule = "a"\nignore_missing_imports = true\ndisable_error_code = ["return"]\n',
        source="current",
    )

    findings = ratchet.loosenings(base, current)

    assert findings
    assert "disable_error_code" in findings[0]


def test_mypy_loose_when_false_flip_is_a_loosening():
    ratchet = _ratchet("mypy-overrides")
    base = ratchet.parse(_PYPROJECT_MYPY_BASE, source="base")
    current = ratchet.parse(_PYPROJECT_MYPY_BASE + "disallow_untyped_defs = false\n", source="current")

    findings = ratchet.loosenings(base, current)

    assert findings
    assert "disallow_untyped_defs" in findings[0]


def test_mypy_loose_when_false_flipped_back_to_true_is_a_tightening():
    ratchet = _ratchet("mypy-overrides")
    base = ratchet.parse(_PYPROJECT_MYPY_BASE + "disallow_untyped_defs = false\n", source="base")
    current = ratchet.parse(_PYPROJECT_MYPY_BASE + "disallow_untyped_defs = true\n", source="current")

    assert not ratchet.loosenings(base, current)


def test_mypy_unclassified_option_change_fails_closed():
    ratchet = _ratchet("mypy-overrides")
    base = ratchet.parse(_PYPROJECT_MYPY_BASE, source="base")
    current = ratchet.parse(_PYPROJECT_MYPY_BASE + "some_new_knob = true\n", source="current")

    with pytest.raises(mod.RatchetError, match="no known polarity"):
        ratchet.loosenings(base, current)


# ---------------------------------------------------------------------------
# Duplicate-predicate pair semantics
# ---------------------------------------------------------------------------
def test_duplicate_predicate_shrinking_a_group_passes():
    ratchet = _ratchet("duplicate-validation-predicate")
    base = ratchet.parse('{"near_body": ["a.py:f", "b.py:g", "c.py:h"]}\n', source="base")
    current = ratchet.parse('{"near_body": ["a.py:f", "b.py:g"]}\n', source="current")

    assert not ratchet.loosenings(base, current)


def test_duplicate_predicate_adding_a_member_fails():
    ratchet = _ratchet("duplicate-validation-predicate")
    base = ratchet.parse('{"block": ["a.py:f", "b.py:g"]}\n', source="base")
    current = ratchet.parse('{"block": ["a.py:f", "b.py:g", "c.py:h"]}\n', source="current")

    assert ratchet.loosenings(base, current)


def test_duplicate_predicate_merging_two_groups_fails():
    ratchet = _ratchet("duplicate-validation-predicate")
    base = ratchet.parse('{"block": ["a.py:f", "b.py:g"]}\n{"block": ["c.py:h", "d.py:i"]}\n', source="base")
    current = ratchet.parse('{"block": ["a.py:f", "b.py:g", "c.py:h", "d.py:i"]}\n', source="current")

    assert ratchet.loosenings(base, current)


def test_duplicate_predicate_message_rename_keeps_the_same_extent():
    ratchet = _ratchet("duplicate-validation-predicate")
    base = ratchet.parse('{"detail": "old message", "modules": ["a.py", "b.py"]}\n', source="base")
    current = ratchet.parse('{"detail": "new message", "modules": ["a.py", "b.py"]}\n', source="current")

    assert not ratchet.loosenings(base, current)


@pytest.mark.parametrize(
    "text",
    [
        "not json\n",
        '{"unknown": ["a.py", "b.py"]}\n',
        '{"block": ["a.py:f"]}\n',
        '{"block": "a.py:f"}\n',
    ],
)
def test_duplicate_predicate_parse_errors_fail_closed(text):
    with pytest.raises(mod.RatchetError):
        mod._parse_json_group_pairs(text, source="t")


# ---------------------------------------------------------------------------
# End-to-end: real subprocess against a throwaway git repository
# ---------------------------------------------------------------------------
_TIMEOUT_SECONDS = 60

_PYPROJECT_FIXTURE = '[tool.ruff.lint.per-file-ignores]\n"tests/**" = ["S101"]\n\n[tool.mypy]\nstrict = true\n\n[[tool.mypy.overrides]]\nmodule = "some_untyped"\nignore_missing_imports = true\n\n[tool.coverage.report]\nfail_under = 80\n'

#: Minimal valid content for EVERY registry ratchet, keyed by repo-relative path.
_RATCHET_FIXTURES: dict[str, str] = {
    BASELINE: "a.md\t1\n",
    f"{ARCH}/audit_coverage_baseline.txt": "admin.py:release\n",
    f"{ARCH}/duplicate_test_body_baseline.txt": "p.py:<module>:test_a=test_b\n",
    f"{ARCH}/duplicate_validation_predicate_baseline.txt": '{"block": ["a.py:f", "b.py:g"]}\n',
    f"{ARCH}/noop_bdd_then_baseline.txt": "bdd/test_a.py:a step\n",
    f"{ARCH}/self_asserting_bdd_baseline.txt": "# header only\n",
    "backend/pyproject.toml": _PYPROJECT_FIXTURE,
    ".github/scripts/coverage-thresholds.sh": _SHELL_BASE,
    "scripts/run_coverage_gate.py": _GATE_BASE,
    "scripts/run_vulture.py": 'cmd = ["vulture", "--min-confidence", "60"]\n',
    f"{ARCH}/test_test_suite_quality.py": 'EXCLUDED_PACKAGES = {"load"}\n',
    ".gitleaks.toml": '[allowlist]\npaths = ["tests/"]\n',
    ".trivyignore": "# header\n",
    ".semgrepignore": "node_modules/\n",
    "sonar-project.properties": "sonar.coverage.exclusions=tests/**\n",
}


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


def _write_file(repo: Path, relpath: str, text: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_repo(tmp_path: Path, baseline_text: str | None = "a.py\t1\nb.py\t2\n") -> Path:
    """A throwaway repo carrying ONLY the counted brand baseline."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ratchet-test@example.com")
    _git(repo, "config", "user.name", "Ratchet Test")
    _git(repo, "config", "commit.gpgsign", "false")
    if baseline_text is not None:
        _write_file(repo, BASELINE, baseline_text)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _make_all_ratchets_repo(tmp_path: Path, *, omit: str | None = None) -> Path:
    """A throwaway repo carrying a minimal valid file for every ratchet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ratchet-test@example.com")
    _git(repo, "config", "user.name", "Ratchet Test")
    _git(repo, "config", "commit.gpgsign", "false")
    for relpath, text in _RATCHET_FIXTURES.items():
        if relpath == omit:
            continue
        _write_file(repo, relpath, text)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _write_baseline(repo: Path, text: str) -> None:
    _write_file(repo, BASELINE, text)


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run_script(repo: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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


def _run_counted(repo: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Legacy single-ratchet mode (``--baseline``)."""
    return _run_script(repo, "--baseline", BASELINE, *args, extra_env=extra_env)


# --- legacy counted mode ---------------------------------------------------
def test_end_to_end_unchanged_baseline_passes(tmp_path):
    repo = _make_repo(tmp_path)

    proc = _run_counted(repo, "--base", "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stderr


def test_end_to_end_raised_count_fails_naming_file_and_both_values(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t3\nb.py\t2\n")
    _commit_all(repo, "raise a.py from 1 to 3")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "RAISED" in proc.stderr
    assert "a.py" in proc.stderr
    assert "3 > 1" in proc.stderr


def test_end_to_end_added_entry_fails(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t1\nb.py\t2\nc.py\t5\n")
    _commit_all(repo, "add c.py")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "ADDED" in proc.stderr
    assert "c.py" in proc.stderr


def test_end_to_end_lowered_count_passes(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t0\nb.py\t2\n")
    _commit_all(repo, "lower a.py from 1 to 0")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr


def test_end_to_end_removed_entry_passes(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t1\n")
    _commit_all(repo, "remove b.py from the baseline")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr


def test_end_to_end_unreadable_explicit_base_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)

    proc = _run_counted(repo, "--base", "does-not-exist-xyz")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr


def test_end_to_end_option_like_explicit_base_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)

    proc = _run_counted(repo, "--base=--output=/tmp/pwn")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "option-like" in proc.stderr


def test_end_to_end_missing_working_tree_baseline_fails_closed(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / BASELINE).unlink()

    proc = _run_counted(repo, "--base", "HEAD")

    assert proc.returncode == 1, proc.stderr
    assert "missing from the working tree" in proc.stderr


def test_end_to_end_baseline_introduced_here_passes_with_notice(tmp_path):
    repo = _make_repo(tmp_path, baseline_text=None)
    _write_baseline(repo, "a.py\t2\n")
    _commit_all(repo, "introduce the baseline")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "introduces the baseline" in proc.stderr


def test_end_to_end_head_parent_fallback_catches_a_raise(tmp_path):
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise a.py with no PR base")

    proc = _run_counted(repo)

    assert proc.returncode == 1, proc.stderr
    assert "HEAD^" in proc.stderr


def test_end_to_end_ratchet_base_rev_hint_catches_a_raise_buried_in_the_push(tmp_path):
    repo = _make_repo(tmp_path)
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise a.py")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "unrelated tip commit")

    proc = _run_counted(repo, extra_env={"RATCHET_BASE_REV": base_sha})

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

    proc = _run_counted(repo)

    assert proc.returncode == 1, proc.stderr
    assert "first parent of the PR merge commit" in proc.stderr
    assert base_sha == _git(repo, "rev-parse", "HEAD^").stdout.strip()


# --- all-ratchets (default) mode ------------------------------------------
def test_end_to_end_all_ratchets_unchanged_passes(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)

    proc = _run_script(repo, "--base", "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert "none of the 17 ratchets is loosened" in proc.stderr


def test_end_to_end_all_ratchets_flags_a_loosened_ratchet_by_name(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, "scripts/run_vulture.py", 'cmd = ["vulture", "--min-confidence", "70"]\n')
    _commit_all(repo, "loosen vulture")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "[vulture-confidence]" in proc.stderr
    assert "RAISED" in proc.stderr


def test_end_to_end_all_ratchets_missing_file_fails_closed_naming_it(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    (repo / ".semgrepignore").unlink()

    proc = _run_script(repo, "--base", "HEAD")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "[semgrepignore]" in proc.stderr
    assert "missing from the working tree" in proc.stderr


def test_end_to_end_all_ratchets_unparseable_file_fails_closed_naming_it(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, ".gitleaks.toml", "this is not = valid [ toml\n")
    _commit_all(repo, "break gitleaks config")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "[gitleaks-allowlist-paths]" in proc.stderr
    assert "not valid TOML" in proc.stderr


def test_end_to_end_all_ratchets_introduced_ratchet_passes_with_notice(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path, omit=".trivyignore")
    _write_file(repo, ".trivyignore", "# header\n")
    _commit_all(repo, "introduce the trivyignore ratchet")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "introduces the ratchet" in proc.stderr
    assert "[trivyignore]" in proc.stderr
