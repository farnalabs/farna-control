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
READ_CREDENTIAL_ALLOWLIST = "backend/tests/unit/api/read_credential_jwt_only_allowlist.txt"

# The read-credential allowlist carries a frozen header, so every fixture below
# keeps its counts consistent with its entries - the parser rejects drift.
_READ_CREDENTIAL_BASE_HEADER = "# Frozen (test):\n#   read routes total: 3\n#   jwt_only: 1   shared_secret: 1   any_credential: 1   public/exempt: 0\n"
_READ_CREDENTIAL_BASE = _READ_CREDENTIAL_BASE_HEADER + ("jwt_only\tGET\t/api/v1/a\treason a\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n")
_READ_CREDENTIAL_LOOSENED = "# Frozen (test):\n#   read routes total: 4\n#   jwt_only: 2   shared_secret: 1   any_credential: 1   public/exempt: 0\njwt_only\tGET\t/api/v1/a\treason a\njwt_only\tGET\t/api/v1/extra\treason extra\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n"
_READ_CREDENTIAL_TIGHTENED = "# Frozen (test):\n#   read routes total: 2\n#   jwt_only: 0   shared_secret: 1   any_credential: 1   public/exempt: 0\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n"
_READ_CREDENTIAL_KIND_CHANGED = "# Frozen (test):\n#   read routes total: 3\n#   jwt_only: 0   shared_secret: 2   any_credential: 1   public/exempt: 0\nshared_secret\tGET\t/api/v1/a\treason a\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n"
_READ_CREDENTIAL_REWORDED = _READ_CREDENTIAL_BASE_HEADER + ("jwt_only\tGET\t/api/v1/a\ta better reason\nshared_secret\tGET\t/scim/v2/b\tanother reason\n")
_READ_CREDENTIAL_STALE_HEADER = _READ_CREDENTIAL_BASE_HEADER + ("jwt_only\tGET\t/api/v1/a\treason a\njwt_only\tGET\t/api/v1/extra\treason extra\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n")
_READ_CREDENTIAL_BAD_ARITHMETIC = "# Frozen (test):\n#   read routes total: 9\n#   jwt_only: 1   shared_secret: 1   any_credential: 1   public/exempt: 0\njwt_only\tGET\t/api/v1/a\treason a\nshared_secret\tGET\t/scim/v2/b\tSCIM shared secret\n"


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


def test_path_exists_at_rev_fails_closed_on_a_bad_rev(tmp_path):
    repo = _make_repo(tmp_path)

    with pytest.raises(mod.RatchetError, match="does not resolve"):
        mod._path_exists_at_rev(repo, "does-not-exist-xyz", "base.txt")


def test_without_new_file_entries_drops_only_files_absent_at_base(tmp_path):
    repo = _make_repo(tmp_path)
    _write_file(repo, "pre_existing.py", "clean\n")
    _commit_all(repo, "add a file that has no brand allowance")

    base = {"a.py": 1}
    current = {"a.py": 1, "pre_existing.py": 2, "brand_new.py": 3}

    result = mod._without_new_file_entries(repo, "HEAD", base, current)

    assert result == {"a.py": 1, "pre_existing.py": 2}


def test_without_new_file_entries_returns_current_when_nothing_was_added(tmp_path):
    repo = _make_repo(tmp_path)
    base = {"a.py": 1}
    current = {"a.py": 2}

    assert mod._without_new_file_entries(repo, "HEAD", base, current) == current


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
    "read-credential-allowlist": (
        _READ_CREDENTIAL_BASE,
        _READ_CREDENTIAL_LOOSENED,
        _READ_CREDENTIAL_TIGHTENED,
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
# Read-credential allowlist (FAR-2727 / FAR-2731)
# ---------------------------------------------------------------------------
def test_read_credential_allowlist_parses_entries_and_frozen_counts():
    parsed = mod._parse_read_credential_allowlist(_READ_CREDENTIAL_BASE, source="t")

    assert parsed.entries == {("GET", "/api/v1/a"): "jwt_only", ("GET", "/scim/v2/b"): "shared_secret"}
    assert parsed.counts["read routes total"] == 3


def test_read_credential_allowlist_rejects_an_entry_the_header_does_not_count():
    with pytest.raises(mod.RatchetError, match="frozen header says"):
        mod._parse_read_credential_allowlist(_READ_CREDENTIAL_STALE_HEADER, source="t")


def test_read_credential_allowlist_rejects_inconsistent_header_arithmetic():
    with pytest.raises(mod.RatchetError, match="frozen header is inconsistent"):
        mod._parse_read_credential_allowlist(_READ_CREDENTIAL_BAD_ARITHMETIC, source="t")


def test_read_credential_allowlist_rejects_an_unknown_kind():
    text = _READ_CREDENTIAL_BASE.replace("jwt_only\tGET\t/api/v1/a", "bogus\tGET\t/api/v1/a")

    with pytest.raises(mod.RatchetError, match="unknown credential kind"):
        mod._parse_read_credential_allowlist(text, source="t")


def test_read_credential_allowlist_rejects_a_duplicate_route():
    text = _READ_CREDENTIAL_BASE.replace(
        "shared_secret\tGET\t/scim/v2/b\tSCIM shared secret",
        "shared_secret\tGET\t/api/v1/a\tduplicate",
    )

    with pytest.raises(mod.RatchetError, match="duplicate allowlist entry"):
        mod._parse_read_credential_allowlist(text, source="t")


def test_read_credential_allowlist_rejects_a_line_without_four_fields():
    text = _READ_CREDENTIAL_BASE + "jwt_only\tGET\t/api/v1/c\n"

    with pytest.raises(mod.RatchetError, match="expected 4 tab-separated fields"):
        mod._parse_read_credential_allowlist(text, source="t")


def test_read_credential_allowlist_reason_rewording_is_not_a_loosening():
    ratchet = _ratchet("read-credential-allowlist")
    base = ratchet.parse(_READ_CREDENTIAL_BASE, source="base")
    reworded = ratchet.parse(_READ_CREDENTIAL_REWORDED, source="reworded")

    assert not ratchet.loosenings(base, reworded)


def test_read_credential_allowlist_kind_change_is_a_loosening():
    ratchet = _ratchet("read-credential-allowlist")
    base = ratchet.parse(_READ_CREDENTIAL_BASE, source="base")
    kind_changed = ratchet.parse(_READ_CREDENTIAL_KIND_CHANGED, source="kind-changed")

    findings = ratchet.loosenings(base, kind_changed)

    assert any("CHANGED" in finding for finding in findings)


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
    READ_CREDENTIAL_ALLOWLIST: _READ_CREDENTIAL_BASE,
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


def test_end_to_end_added_entry_for_an_existing_file_fails(tmp_path):
    repo = _make_repo(tmp_path)
    # c.py exists at the base tip but had no brand allowance; granting one is a
    # loosening because it gives an already-tracked file brand-copy immunity.
    _write_file(repo, "c.py", "clean\n")
    _commit_all(repo, "add c.py to the tree with no baseline entry")
    _write_baseline(repo, "a.py\t1\nb.py\t2\nc.py\t5\n")
    _commit_all(repo, "grant c.py a brand allowance")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "ADDED" in proc.stderr
    assert "c.py" in proc.stderr


def test_end_to_end_added_entry_for_a_new_file_passes(tmp_path):
    repo = _make_repo(tmp_path)
    # A genuinely new file (absent at the base tip) may be baselined at its real
    # count: the brand gate requires exactly that for non-brand identifiers.
    _write_file(repo, "c.py", "legacy\n")
    _write_baseline(repo, "a.py\t1\nb.py\t2\nc.py\t1\n")
    _commit_all(repo, "add a new file and baseline its occurrence")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "not loosened" in proc.stderr


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


def test_end_to_end_counted_mode_malformed_base_is_skipped(tmp_path):
    """The legacy counted mode tolerates an unreadable BASE the same way."""
    repo = _make_repo(tmp_path)
    _write_baseline(repo, "a.py\tmany\n")
    _commit_all(repo, "malformed baseline on the base")
    _write_baseline(repo, "a.py\t1\nb.py\t2\n")
    _commit_all(repo, "restore the baseline in the working tree")

    proc = _run_counted(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "SKIPPING" in proc.stderr
    assert "not an integer" in proc.stderr


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


def test_end_to_end_pr_context_prefers_origin_base_over_head_parent(tmp_path):
    """A PR base is the target branch, never the branch's own previous commit.

    The raise is in the branch's FIRST commit, then an unrelated commit sits on
    top, so ``HEAD^`` (the branch's previous commit) already carries the raise.
    A guard comparing against ``HEAD^`` passes; only the actual base branch
    (``origin/main``) catches the loosening. This is the FAR-2699 follow-up:
    ``github.event.before`` on a pull_request synchronize names the branch's
    previous head, which the old chain preferred over the base branch.

    Both env vars are set to the values a real ``pull_request`` run supplies:
    ``GITHUB_BASE_REF=main`` AND ``RATCHET_BASE_REV`` = the branch's previous head.
    Setting ``RATCHET_BASE_REV`` is what makes this fail against a guard that
    prefers the push hint; only making ``GITHUB_BASE_REF`` authoritative passes.
    """
    repo = _make_repo(tmp_path)
    origin_main = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "feature")
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    branch_previous_head = _commit_all(repo, "raise a.py on the feature branch")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "unrelated tip commit so HEAD^ is not the base")
    _git(repo, "update-ref", "refs/remotes/origin/main", origin_main)

    proc = _run_counted(
        repo,
        extra_env={"GITHUB_BASE_REF": "main", "RATCHET_BASE_REV": branch_previous_head},
    )

    assert proc.returncode == 1, proc.stderr
    assert "origin/main" in proc.stderr
    assert "9 > 1" in proc.stderr


def test_end_to_end_origin_base_beats_the_merge_ref_first_parent(tmp_path):
    """A merge-ref PR checkout compares against the target branch, not HEAD^.

    Simulates ``actions/checkout``'s pull_request merge commit (two parents)
    where main advanced after the PR branched. ``fetch-depth: 0`` fetches
    ``origin/main``, which names the target branch; the merge ref's first parent
    is only a snapshot taken when GitHub minted that commit. The old chain
    preferred ``HEAD^``, so the failure named the wrong base. No local ``main``
    branch is left, so ``origin/main`` is the only route to the base.
    """
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    _write_baseline(repo, "a.py\t9\nb.py\t2\n")
    _commit_all(repo, "raise a.py on the feature branch")
    _git(repo, "checkout", "-q", "main")
    (repo / "main-only.txt").write_text("advanced\n", encoding="utf-8")
    advanced_main = _commit_all(repo, "advance main after the PR branched")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    _git(repo, "update-ref", "refs/remotes/origin/main", advanced_main)
    _git(repo, "checkout", "-q", "--detach")
    _git(repo, "branch", "-D", "main")

    proc = _run_counted(repo, extra_env={"GITHUB_BASE_REF": "main"})

    assert proc.returncode == 1, proc.stderr
    assert "origin/main" in proc.stderr
    assert "HEAD^ (first parent" not in proc.stderr
    assert "9 > 1" in proc.stderr


def test_resolve_base_ignores_the_push_hint_when_github_base_ref_is_set(tmp_path, monkeypatch):
    """``GITHUB_BASE_REF`` is authoritative; the push hint never overrides it.

    On a pull_request event both env vars can be present, and the hint names the
    branch's own previous head - not a base. The guard must ignore it whenever
    ``GITHUB_BASE_REF`` is set, so correctness does not depend on ``ci.yml``
    blanking the hint.
    """
    repo = _make_repo(tmp_path)
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "a second commit so HEAD^ exists")
    _git(repo, "branch", "release")
    monkeypatch.setenv("GITHUB_BASE_REF", "release")
    monkeypatch.setenv("RATCHET_BASE_REV", "HEAD")

    rev, why = mod._resolve_base(repo, None)

    assert rev == "release"
    assert "GITHUB_BASE_REF" in why


def test_resolve_base_uses_a_local_base_ref_when_origin_is_absent(tmp_path, monkeypatch):
    """A fetched ``origin/<base>`` is preferred, but a local ``<base>`` still works."""
    repo = _make_repo(tmp_path)
    _git(repo, "branch", "release")
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "a second commit so HEAD^ exists")
    monkeypatch.delenv("RATCHET_BASE_REV", raising=False)
    monkeypatch.setenv("GITHUB_BASE_REF", "release")

    rev, why = mod._resolve_base(repo, None)

    assert rev == "release"
    assert "GITHUB_BASE_REF" in why


def test_resolve_base_falls_through_an_unfetched_base_ref_to_head_parent(tmp_path, monkeypatch):
    """A base ref that was not fetched must fall through to HEAD^, not abort.

    ``actions/checkout`` at the default depth does not always create
    ``origin/<base>``; aborting here would turn a fetch gap into a red PR.
    """
    repo = _make_repo(tmp_path)
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _commit_all(repo, "a second commit so HEAD^ exists")
    monkeypatch.delenv("RATCHET_BASE_REV", raising=False)
    monkeypatch.setenv("GITHUB_BASE_REF", "release")

    rev, why = mod._resolve_base(repo, None)

    assert rev == "HEAD^"
    assert "previous commit" in why


def test_end_to_end_no_resolvable_base_fails_closed(tmp_path):
    """Nothing resolvable (no base, no PR context, no parent) still fails closed."""
    repo = _make_repo(tmp_path)

    proc = _run_script(repo)

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "no base revision could be resolved" in proc.stderr


# --- all-ratchets (default) mode ------------------------------------------
def test_end_to_end_all_ratchets_unchanged_passes(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)

    proc = _run_script(repo, "--base", "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert "none of the checked ratchets is loosened" in proc.stderr


def test_end_to_end_all_ratchets_new_file_brand_entry_passes(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, "new_brand.py", "legacy\n")
    _write_file(repo, BASELINE, "a.md\t1\nnew_brand.py\t1\n")
    _commit_all(repo, "add a new file and baseline its occurrence")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "none of the checked ratchets is loosened" in proc.stderr


def test_end_to_end_all_ratchets_existing_file_brand_entry_fails(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, "existing.py", "clean\n")
    _commit_all(repo, "add existing.py to the tree with no baseline entry")
    _write_file(repo, BASELINE, "a.md\t1\nexisting.py\t2\n")
    _commit_all(repo, "grant existing.py a brand allowance")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "[brand-consistency]" in proc.stderr
    assert "ADDED" in proc.stderr
    assert "existing.py" in proc.stderr


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


def test_end_to_end_read_credential_added_route_fails_naming_it(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_LOOSENED)
    _commit_all(repo, "add a non-mk_ read to the allowlist")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "[read-credential-allowlist]" in proc.stderr
    assert "ADDED" in proc.stderr
    assert "/api/v1/extra" in proc.stderr


def test_end_to_end_read_credential_removed_route_passes(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_TIGHTENED)
    _commit_all(repo, "remove a read from the allowlist")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 0, proc.stderr
    assert "none of the checked ratchets is loosened" in proc.stderr


def test_end_to_end_read_credential_kind_change_fails(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_KIND_CHANGED)
    _commit_all(repo, "change an allowlisted route's credential kind")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "[read-credential-allowlist]" in proc.stderr
    assert "CHANGED" in proc.stderr


def test_end_to_end_read_credential_header_drift_fails_closed(tmp_path):
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_STALE_HEADER)
    _commit_all(repo, "add an entry without updating the frozen header")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "[read-credential-allowlist]" in proc.stderr


def test_end_to_end_malformed_base_file_is_skipped_with_a_loud_warning(tmp_path):
    """An unreadable BASE is not this change's defect: warn, skip, pass.

    The base header claims 1 jwt_only entry but the file declares 2 - exactly
    the shape that blocked PR #3461. The working tree at the tip is valid, so
    the only unreadable file is the base's, and the run must succeed with a
    warning that names the ratchet and the base revision.
    """
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_STALE_HEADER)
    bad_base = _commit_all(repo, "commit a malformed allowlist header (the base)")
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_BASE)
    _commit_all(repo, "restore the allowlist in the working tree")

    proc = _run_script(repo, "--base", bad_base)

    assert proc.returncode == 0, proc.stderr
    assert "FAILED CLOSED" not in proc.stderr
    assert "WARNING" in proc.stderr
    assert "[read-credential-allowlist]" in proc.stderr
    assert bad_base in proc.stderr
    assert "SKIPPED" in proc.stderr
    assert "none of the checked ratchets is loosened" in proc.stderr
    assert "1 skipped: unreadable base" in proc.stderr


def test_end_to_end_malformed_working_tree_fails_closed_even_when_the_base_is_also_malformed(tmp_path):
    """The base tolerance must never swallow a working-tree defect."""
    repo = _make_all_ratchets_repo(tmp_path)
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_STALE_HEADER)
    _commit_all(repo, "commit a malformed allowlist header (the base)")
    _write_file(repo, READ_CREDENTIAL_ALLOWLIST, _READ_CREDENTIAL_BAD_ARITHMETIC)
    _commit_all(repo, "introduce a differently malformed working-tree allowlist")

    proc = _run_script(repo, "--base", "HEAD^")

    assert proc.returncode == 1, proc.stderr
    assert "FAILED CLOSED" in proc.stderr
    assert "[read-credential-allowlist]" in proc.stderr
