#!/usr/bin/env python3
"""Fail CI when a shrink-only ratchet is LOOSENED instead of obeyed (FAR-2699).

A shrink-only ratchet is only a control if loosening it fails. The brand gate
(``backend/tests/architecture/test_brand_consistency.py``) freezes each file's
occurrence count in ``brand_consistency_baseline.txt`` and its failure message
says to *replace* the drift, never to raise the baseline. But the test only
compares the working tree against the baseline it is handed, so an agent that
edits the baseline *and* the drift together passes: the ratchet is silently
loosened for good. That is exactly what happened when the autonomous Branch
Fixer raised two counts (36 -> 38 and 3 -> 4) to get green and the PRs merged.

This guard closes that hole by comparing each ratchet in the working tree
against the SAME file at the base revision. Every ratchet carries an explicit
direction, because "looser" is not always "bigger":

===========================================  ==================================
ratchet file                                 loosening means
===========================================  ==================================
brand_consistency_baseline.txt               added entry or raised count
audit_coverage_baseline.txt                  added entry
duplicate_test_body_baseline.txt             added entry
duplicate_validation_predicate_baseline.txt  a new tolerated duplicate pair
noop_bdd_then_baseline.txt                   added entry
self_asserting_bdd_baseline.txt              added entry
backend/pyproject.toml [coverage.report]     ``fail_under`` LOWERED
.github/scripts/coverage-thresholds.sh       any floor LOWERED
scripts/run_coverage_gate.py                 floor LOWERED, ``TINY_DIFF_THRESHOLD`` RAISED
scripts/run_vulture.py                       ``--min-confidence`` RAISED
backend/pyproject.toml ruff per-file-ignores an added ``(pattern, code)``
backend/pyproject.toml mypy overrides        an added suppression
test_test_suite_quality.py EXCLUDED_PACKAGES an added package
.gitleaks.toml allowlist ``paths``           an added path
.trivyignore                                 an added entry
.semgrepignore                               an added entry
sonar-project.properties coverage.exclusions an added pattern
===========================================  ==================================

Tightening any of them passes: a removed entry, a lowered count, a raised
coverage floor, a lowered vulture confidence, an added lint rule.

Excluded by nature: the frontend visual-regression baselines (PNG snapshots
under ``frontend/tests/``). A snapshotted image has no useful value/set diff -
regenerating it can encode any visual change, and the meaningful control is a
reviewer looking at the image diff. There is no "added entry or raised number"
to compare, so this guard deliberately does not pretend to ratchet it.

The duplicate-predicate baseline (``duplicate_validation_predicate_baseline.txt``)
is JSON lines whose group identity is its member set. The guard compares the
tolerated member PAIRS, not the raw line text: adding a group, adding a member
to a group, or merging two groups all create a new pair and fail; shrinking a
group (or untangling a member) only removes pairs and passes. The message /
constant value in an entry is deliberately not part of the comparison - the
ratchet measures the extent of tolerated duplication, not its wording.

Base revision
-------------
The base is resolved once, in this order, and the choice is printed so a reader
can see what was compared:

1. ``--base <rev>`` (explicit; used locally, e.g. ``--base origin/main``). An
   explicit rev that does not resolve is a hard failure - never a silent pass.
2. ``RATCHET_BASE_REV`` in the environment (the workflow passes a push event's
   ``github.event.before``, i.e. the previous main tip). An unresolvable hint
   falls through: on a brand-new branch it is the all-zero SHA, which names no
   commit.
3. ``HEAD^`` when ``HEAD^2`` exists - a pull_request checkout is the PR merge
   commit, whose first parent is the base branch tip. No ``origin/<base>``
   fetch is needed, which is why this comes before the ref lookup (see the
   integration-test note in ``ci.yml``: ``actions/checkout`` does not always
   create ``origin/<base>``).
4. ``origin/$GITHUB_BASE_REF`` then ``$GITHUB_BASE_REF`` when
   ``GITHUB_BASE_REF`` is set. If neither resolves the base is unreadable and
   the guard FAILS CLOSED.
5. ``HEAD^`` - the previous commit. This is the documented non-PR fallback: a
   push to main compares the new tip against the commit before it, so a raised
   baseline in the pushed commit is caught. With no parent commit at all the
   guard FAILS CLOSED.

Fail-closed rules (an unreadable base must never look like a pass):

* an explicit ``--base`` that does not resolve;
* ``GITHUB_BASE_REF`` set but neither candidate ref resolves;
* a git failure reading a ratchet at a base that does resolve;
* a ratchet file missing from the working tree, or unparseable - the failure
  names the ratchet;
* for the brand baseline, a malformed ``<path>\\t<count>`` line (no separator,
  a non-integer count, a duplicate path).

A ratchet file that does not exist at the base revision is the one legitimate
"nothing to compare" case: it means this change introduces that ratchet, so the
guard passes it with a loud notice. (Every ratchet exists on main today, so
this branch is only reachable when a new ratchet is added.)

Usage
-----
    # CI (base auto-resolved): checks every enabled ratchet.
    uv run --no-build --project backend --no-sync python scripts/run_check_ratchet_baseline.py

    # Locally, against the base branch:
    python scripts/run_check_ratchet_baseline.py --base origin/main

    # Legacy single-ratchet mode (counted baseline only), exactly as before:
    python scripts/run_check_ratchet_baseline.py --baseline backend/tests/architecture/brand_consistency_baseline.txt

Exit 0 clean, 1 loosened / unreadable / malformed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

#: The shrink-only counted baseline the legacy ``--baseline`` mode inspects.
DEFAULT_BASELINE = "backend/tests/architecture/brand_consistency_baseline.txt"

#: Environment hint the CI workflow passes for a push event.
BASE_ENV_VAR = "RATCHET_BASE_REV"

#: The single-commit parent revision, used as a documented fallback base.
_PARENT_REV = "HEAD^"

_ARCH = "backend/tests/architecture"

_MISSING = object()


class RatchetError(RuntimeError):
    """A failure that must stop the guard (never silently pass)."""


def _validated_git_arg(value: str, what: str) -> str:
    """Reject a git argument that git would read as an option.

    ``_git`` runs git directly (no shell), so there is no shell-injection surface,
    but a revision or path beginning with ``-`` would be interpreted by git as an
    OPTION (argument injection). A revision from ``--base``/``RATCHET_BASE_REV``
    and a repo-relative baseline path are never legitimately option-like, so
    reject them rather than let one reach argv.
    """
    if not value or value.startswith("-"):
        raise RatchetError(f"{what} {value!r} is option-like and cannot be passed to git safely")
    return value


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise RatchetError("git is not available on PATH")
    try:
        return subprocess.run(
            [git, *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RatchetError(f"could not run 'git {' '.join(args)}': {exc}") from exc


def _resolve_repo_root() -> Path:
    """The git worktree containing the current directory (works in worktrees)."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path(__file__).resolve().parent.parent


def _rev_exists(repo_root: Path, rev: str) -> bool:
    result = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{_validated_git_arg(rev, 'revision')}^{{commit}}")
    return result.returncode == 0 and bool(result.stdout.strip())


def _resolve_base(repo_root: Path, explicit: str | None) -> tuple[str, str]:
    """Return ``(rev, why)`` for the revision the ratchets are compared against."""
    if explicit:
        if not _rev_exists(repo_root, explicit):
            raise RatchetError(f"--base {explicit!r} does not resolve to a commit in this repository; refusing to pass with an unreadable base")
        return explicit, "explicit --base"

    hint = os.environ.get(BASE_ENV_VAR, "").strip()
    if hint and _rev_exists(repo_root, hint):
        return hint, f"{BASE_ENV_VAR} (push event's previous tip)"

    if _rev_exists(repo_root, "HEAD^2"):
        return _PARENT_REV, "HEAD^ (first parent of the PR merge commit)"

    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    if base_ref:
        for candidate in (f"origin/{base_ref}", base_ref):
            if _rev_exists(repo_root, candidate):
                return candidate, f"{candidate} (GITHUB_BASE_REF)"
        raise RatchetError(f"GITHUB_BASE_REF={base_ref!r} is set but neither 'origin/{base_ref}' nor '{base_ref}' resolves; refusing to pass with an unreadable base")

    if _rev_exists(repo_root, _PARENT_REV):
        return _PARENT_REV, "HEAD^ (previous commit; no pull-request base available)"

    raise RatchetError("no base revision could be resolved (no --base, no usable RATCHET_BASE_REV, no PR merge commit, no GITHUB_BASE_REF, and HEAD has no parent); refusing to pass with an unreadable base")


def parse_baseline(text: str, *, source: str) -> dict[str, int]:
    """Parse ``<path>\\t<count>`` lines; blank lines and ``#`` comments ignored."""
    entries: dict[str, int] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, separator, count_text = line.rpartition("\t")
        if not separator:
            raise RatchetError(f"{source}:{lineno}: malformed entry (expected '<path>\\t<count>'): {raw!r}")
        try:
            count = int(count_text)
        except ValueError as exc:
            raise RatchetError(f"{source}:{lineno}: count {count_text!r} is not an integer: {raw!r}") from exc
        if count < 0:
            raise RatchetError(f"{source}:{lineno}: count {count} is negative: {raw!r}")
        if path in entries:
            raise RatchetError(f"{source}:{lineno}: duplicate entry for {path!r}")
        entries[path] = count
    return entries


def read_baseline_at_rev(repo_root: Path, rev: str, relpath: str) -> str | None:
    """Read *relpath* at *rev*; ``None`` means the path is absent at that rev.

    The revision itself is validated first, so a non-zero ``git cat-file`` can
    only mean "path absent", never "bad base" (which must fail closed).
    """
    if not _rev_exists(repo_root, rev):
        raise RatchetError(f"base revision {rev!r} does not resolve; refusing to pass with an unreadable base")
    relpath = _validated_git_arg(relpath, "baseline path")
    probe = _git(repo_root, "cat-file", "-e", f"{rev}:{relpath}")
    if probe.returncode != 0:
        return None
    shown = _git(repo_root, "show", f"{rev}:{relpath}")
    if shown.returncode != 0:
        raise RatchetError(f"could not read {relpath!r} at base {rev!r}: {shown.stderr.strip()}")
    return shown.stdout


def compare(base: dict[str, int], current: dict[str, int]) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Return ``(added_paths, raised_entries)``; removed/lowered entries pass."""
    added = sorted(path for path in current if path not in base)
    raised = sorted((path, base[path], current[path]) for path in current if path in base and current[path] > base[path])
    return added, raised


# ---------------------------------------------------------------------------
# Per-ratchet adapters
#
# Every adapter is one small ``parse(text, source)`` plus a ``loosenings``
# comparison. Keeping them separate is what makes the DIRECTION testable: a
# set ratchet flags added members, a coverage floor flags a lowered value, a
# vulture confidence flags a raised value.
# ---------------------------------------------------------------------------
def _read_toml(text: str, source: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RatchetError(f"{source}: not valid TOML: {exc}") from exc
    return data


def _parse_ast(text: str, source: str) -> ast.Module:
    try:
        return ast.parse(text, filename=source)
    except SyntaxError as exc:
        raise RatchetError(f"{source}: not valid Python: {exc}") from exc


def _parse_line_set(text: str, source: str) -> frozenset[str]:
    """Non-blank, non-comment lines as a set (duplicates are a malformed file)."""
    entries: set[str] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in entries:
            raise RatchetError(f"{source}:{lineno}: duplicate entry {line!r}")
        entries.add(line)
    return frozenset(entries)


_JSON_FAMILIES = frozenset({"detail", "constant", "block", "near_body"})


def _parse_json_group_pairs(text: str, source: str) -> frozenset[tuple[str, str, str]]:
    """The tolerated duplicate member PAIRS implied by the JSON-lines baseline.

    Each line is one group; a group of N members tolerates N*(N-1)/2 duplicate
    pairs. Comparing pairs rather than raw lines means a group that SHRINKS
    (a legitimate tightening) is a subset of the base and passes, while a new
    group, a new member, or a merged group adds pairs and fails.
    """
    pairs: set[tuple[str, str, str]] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RatchetError(f"{source}:{lineno}: not valid JSON: {raw!r} ({exc})") from exc
        if not isinstance(entry, dict):
            raise RatchetError(f"{source}:{lineno}: expected a JSON object: {raw!r}")
        families = sorted(_JSON_FAMILIES & set(entry))
        if len(families) != 1:
            raise RatchetError(f"{source}:{lineno}: expected exactly one of {sorted(_JSON_FAMILIES)}, got {families}: {raw!r}")
        family = families[0]
        # detail/constant entries carry their members under "modules"; block and
        # near_body entries carry them in the family key itself.
        members = entry["modules"] if family in {"detail", "constant"} else entry[family]
        if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
            raise RatchetError(f"{source}:{lineno}: {family!r} members must be a list of strings: {raw!r}")
        unique = sorted(set(members))
        if len(unique) < 2:
            raise RatchetError(f"{source}:{lineno}: {family!r} group needs at least two distinct members: {raw!r}")
        for left, right in combinations(unique, 2):
            pairs.add((family, left, right))
    return frozenset(pairs)


def _parse_pyproject_coverage_floor(text: str, source: str) -> dict[str, float]:
    data = _read_toml(text, source)
    try:
        value = data["tool"]["coverage"]["report"]["fail_under"]
    except (KeyError, TypeError) as exc:
        raise RatchetError(f"{source}: no [tool.coverage.report] fail_under") from exc
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RatchetError(f"{source}: fail_under must be a number, got {value!r}")
    return {"fail_under": float(value)}


_INCLUDE_RE = re.compile(r'--include="([^"]+)"')
_FAIL_UNDER_RE = re.compile(r"--fail-under=(\d+)")
_COV_FAIL_UNDER_RE = re.compile(r"--cov-fail-under=(\d+)")


def _parse_thresholds_shell(text: str, source: str) -> dict[str, float]:
    """Named coverage floors from the shell gate: per-``--include`` and whole-suite.

    Comment lines are skipped and an inline ``#`` is stripped first, so a
    commented-out (or narrative) floor is never read as an active one. A
    ``--fail-under=0`` is coverage's explicit "do not enforce here" opt-out
    (the shard/xml steps), not a floor, so zeros are ignored.
    """
    floors: dict[str, float] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        code = raw.split("#", 1)[0].strip()
        if not code:
            continue
        includes = _INCLUDE_RE.findall(code)
        plain = [int(match) for match in _FAIL_UNDER_RE.findall(code)]
        cov = [int(match) for match in _COV_FAIL_UNDER_RE.findall(code)]
        values = [value for value in (*plain, *cov) if value > 0]
        if not values:
            continue
        if len(values) > 1:
            raise RatchetError(f"{source}:{lineno}: more than one coverage floor on one line: {raw!r}")
        value = float(values[0])
        keys = [f"include:{include}" for include in includes] or ["whole-suite"]
        for key in keys:
            if key in floors and floors[key] != value:
                raise RatchetError(f"{source}:{lineno}: conflicting floors for {key!r}: {floors[key]} vs {value}")
            floors[key] = value
    if not floors:
        raise RatchetError(f"{source}: no coverage floors found (expected --fail-under / --cov-fail-under)")
    return floors


_GATE_FLOOR_NAMES = frozenset({"COVERAGE_THRESHOLD", "BRANCH_COVERAGE_THRESHOLD", "TINY_DIFF_THRESHOLD"})


def _parse_module_numeric_constants(text: str, source: str) -> dict[str, float]:
    """Module-level numeric assignments for the changed-lines gate constants."""
    tree = _parse_ast(text, source)
    values: dict[str, float] = {}
    for node in tree.body:
        value_node: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value_node = node.value
        for target in targets:
            if not isinstance(target, ast.Name) or target.id not in _GATE_FLOOR_NAMES:
                continue
            try:
                literal = ast.literal_eval(value_node) if value_node is not None else None
            except (ValueError, TypeError, SyntaxError) as exc:
                raise RatchetError(f"{source}: {target.id} is not a literal number: {exc}") from exc
            if not isinstance(literal, (int, float)) or isinstance(literal, bool):
                raise RatchetError(f"{source}: {target.id} must be a number, got {literal!r}")
            values[target.id] = float(literal)
    missing = sorted(_GATE_FLOOR_NAMES - set(values))
    if missing:
        raise RatchetError(f"{source}: expected constant(s) {missing} not found at module level")
    return values


def _parse_vulture_min_confidence(text: str, source: str) -> int:
    tree = _parse_ast(text, source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        for index, element in enumerate(node.elts[:-1]):
            if not isinstance(element, ast.Constant) or element.value != "--min-confidence":
                continue
            following = node.elts[index + 1]
            if isinstance(following, ast.Constant) and isinstance(following.value, str) and following.value.isdigit():
                return int(following.value)
    raise RatchetError(f"{source}: no '--min-confidence <int>' pair found in the vulture command")


def _parse_ruff_per_file_ignores(text: str, source: str) -> frozenset[tuple[str, str]]:
    data = _read_toml(text, source)
    tool = data.get("tool", {})
    ruff = tool.get("ruff", {}) if isinstance(tool, dict) else {}
    lint = ruff.get("lint", {}) if isinstance(ruff, dict) else {}
    section = lint.get("per-file-ignores") if isinstance(lint, dict) else None
    if section is None:
        return frozenset()
    if not isinstance(section, dict):
        raise RatchetError(f"{source}: [tool.ruff.lint.per-file-ignores] must be a table")
    tokens: set[tuple[str, str]] = set()
    for pattern, codes in section.items():
        if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
            raise RatchetError(f"{source}: per-file-ignores[{pattern!r}] must be a list of strings")
        for code in codes:
            tokens.add((pattern, code))
    return frozenset(tokens)


#: mypy options where an explicit ``true`` suppresses a check (loosening).
_MYPY_LOOSE_WHEN_TRUE = frozenset(
    {
        "ignore_missing_imports",
        "ignore_errors",
        "allow_untyped_defs",
        "allow_untyped_calls",
        "allow_untyped_decorators",
        "allow_untyped_globals",
        "allow_redefinition",
        "allow_any_generics",
        "implicit_reexport",
    }
)

#: mypy options where an explicit ``false`` disables a check (loosening).
_MYPY_LOOSE_WHEN_FALSE = frozenset(
    {
        "disallow_any_unimported",
        "disallow_any_expr",
        "disallow_any_decorated",
        "disallow_any_generics",
        "disallow_any_explicit",
        "disallow_subclassing_any",
        "disallow_untyped_calls",
        "disallow_untyped_defs",
        "disallow_incomplete_defs",
        "disallow_untyped_decorators",
        "disallow_untyped_globals",
        "strict_optional",
        "warn_return_any",
        "warn_unused_ignores",
        "warn_unused_configs",
        "no_implicit_optional",
        "check_untyped_defs",
    }
)


def _parse_mypy_overrides(text: str, source: str) -> dict[str, dict[str, Any]]:
    """Suppression options per module: ``{module: {option: value}}``.

    The global ``ignore_missing_imports`` (if present) is filed under
    ``"<global>"``. Modules from separate ``[[tool.mypy.overrides]]`` blocks
    merge; a conflicting duplicate is malformed configuration, not a ratchet.
    """
    data = _read_toml(text, source)
    tool = data.get("tool", {})
    mypy = tool.get("mypy") if isinstance(tool, dict) else None
    if not isinstance(mypy, dict):
        raise RatchetError(f"{source}: no [tool.mypy] table")
    result: dict[str, dict[str, Any]] = {}
    if "ignore_missing_imports" in mypy:
        result["<global>"] = {"ignore_missing_imports": mypy["ignore_missing_imports"]}
    overrides = mypy.get("overrides", [])
    if not isinstance(overrides, list):
        raise RatchetError(f"{source}: tool.mypy.overrides must be an array of tables")
    for index, override in enumerate(overrides, start=1):
        if not isinstance(override, dict):
            raise RatchetError(f"{source}: overrides[{index}] is not a table")
        modules = override.get("module", _MISSING)
        if modules is _MISSING:
            raise RatchetError(f"{source}: overrides[{index}] has no 'module'")
        if isinstance(modules, str):
            modules = [modules]
        if not isinstance(modules, list) or not all(isinstance(module, str) for module in modules):
            raise RatchetError(f"{source}: overrides[{index}].module must be a string or list of strings")
        options = {key: value for key, value in override.items() if key != "module"}
        for module in modules:
            existing = result.setdefault(module, {})
            for key, value in options.items():
                if key in existing and existing[key] != value:
                    raise RatchetError(f"{source}: conflicting mypy override for {module!r} option {key!r}")
                existing[key] = value
    return result


def _assignment_targets(node: ast.stmt) -> tuple[list[ast.expr], ast.expr | None]:
    if isinstance(node, ast.Assign):
        return list(node.targets), node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _parse_excluded_packages(text: str, source: str) -> frozenset[str]:
    tree = _parse_ast(text, source)
    found = False
    value_node: ast.expr | None = None
    for node in tree.body:
        targets, value = _assignment_targets(node)
        if not any(isinstance(target, ast.Name) and target.id == "EXCLUDED_PACKAGES" for target in targets):
            continue
        found = True
        value_node = value
    if not found:
        raise RatchetError(f"{source}: module-level EXCLUDED_PACKAGES assignment not found")
    if value_node is None:
        raise RatchetError(f"{source}: EXCLUDED_PACKAGES has no value")
    if isinstance(value_node, ast.Call) and isinstance(value_node.func, ast.Name) and value_node.func.id in {"set", "frozenset", "list", "tuple"} and len(value_node.args) == 1 and not value_node.keywords:
        value_node = value_node.args[0]
    try:
        literal = ast.literal_eval(value_node)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise RatchetError(f"{source}: EXCLUDED_PACKAGES is not a literal collection: {exc}") from exc
    if isinstance(literal, str):
        raise RatchetError(f"{source}: EXCLUDED_PACKAGES must be a collection, not a bare string")
    try:
        entries = frozenset(literal)
    except TypeError as exc:
        raise RatchetError(f"{source}: EXCLUDED_PACKAGES is not iterable: {exc}") from exc
    if not all(isinstance(entry, str) for entry in entries):
        raise RatchetError(f"{source}: EXCLUDED_PACKAGES must contain only strings")
    return entries


def _parse_gitleaks_allowlist_paths(text: str, source: str) -> frozenset[str]:
    data = _read_toml(text, source)
    allowlist = data.get("allowlist")
    if not isinstance(allowlist, dict):
        raise RatchetError(f"{source}: no [allowlist] table")
    paths = allowlist.get("paths", _MISSING)
    if paths is _MISSING:
        raise RatchetError(f"{source}: [allowlist] has no 'paths'")
    if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
        raise RatchetError(f"{source}: allowlist.paths must be a list of strings")
    return frozenset(paths)


def _parse_sonar_coverage_exclusions(text: str, source: str) -> frozenset[str]:
    found: frozenset[str] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "sonar.coverage.exclusions":
            continue
        if found is not None:
            raise RatchetError(f"{source}:{lineno}: duplicate sonar.coverage.exclusions property")
        found = frozenset(part.strip() for part in value.split(",") if part.strip())
    if found is None:
        raise RatchetError(f"{source}: no sonar.coverage.exclusions property")
    return found


# ---------------------------------------------------------------------------
# Loosening comparisons (direction is explicit in the function name)
# ---------------------------------------------------------------------------
def _counted_loosenings(base: Any, current: Any) -> list[str]:
    added, raised = compare(base, current)
    findings = [f"RAISED {path}: {new} > {old} (base)" for path, old, new in raised]
    findings.extend(f"ADDED {path}: not in the base baseline" for path in added)
    return findings


def _added_set_loosenings(base: Any, current: Any) -> list[str]:
    return [f"ADDED {entry}" for entry in sorted(current - base)]


def _lowered_floor_loosenings(base: dict[str, float], current: dict[str, float]) -> list[str]:
    findings: list[str] = []
    for key in sorted(base):
        if key not in current:
            findings.append(f"floor {key!r} was REMOVED (base {base[key]})")
        elif current[key] < base[key]:
            findings.append(f"floor {key!r} was LOWERED: {current[key]} < {base[key]} (lower = looser)")
    return findings


def _coverage_gate_loosenings(base: dict[str, float], current: dict[str, float]) -> list[str]:
    """Changed-lines floors lower = looser; TINY_DIFF_THRESHOLD higher = looser."""
    findings: list[str] = []
    for key in sorted(base):
        if key not in current:
            findings.append(f"{key} was REMOVED (base {base[key]})")
        elif key == "TINY_DIFF_THRESHOLD":
            if current[key] > base[key]:
                findings.append(f"TINY_DIFF_THRESHOLD was RAISED: {current[key]} > {base[key]} (bigger diff excused = looser)")
        elif current[key] < base[key]:
            findings.append(f"{key} was LOWERED: {current[key]} < {base[key]} (lower = looser)")
    return findings


def _vulture_loosenings(base: int, current: int) -> list[str]:
    if current > base:
        return [f"--min-confidence was RAISED: {current} > {base} (higher confidence = fewer findings = looser)"]
    return []


def _as_code_set(value: Any, *, option: str) -> set[str]:
    if value is _MISSING:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        return set(value)
    raise RatchetError(f"mypy option {option!r} must be a string or list of strings, got {value!r}")


def _mypy_loosenings(base: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for module in sorted(set(base) | set(current)):
        base_options = base.get(module, {})
        current_options = current.get(module, {})
        for option in sorted(set(base_options) | set(current_options)):
            base_value = base_options.get(option, _MISSING)
            current_value = current_options.get(option, _MISSING)
            if base_value is _MISSING and current_value is _MISSING:
                continue
            if base_value is not _MISSING and current_value is not _MISSING and base_value == current_value:
                continue
            if option == "disable_error_code":
                added = _as_code_set(current_value, option=option) - _as_code_set(base_value, option=option)
                findings.extend(f"{module}: disable_error_code gained {code!r}" for code in sorted(added))
            elif option == "enable_error_code":
                removed = _as_code_set(base_value, option=option) - _as_code_set(current_value, option=option)
                findings.extend(f"{module}: enable_error_code lost {code!r} (looser)" for code in sorted(removed))
            elif option in _MYPY_LOOSE_WHEN_TRUE:
                if current_value is not _MISSING and not isinstance(current_value, bool):
                    raise RatchetError(f"mypy option {option!r} on {module!r} must be a boolean")
                if current_value is True and base_value is not True:
                    findings.append(f"{module}: {option} set to true (looser)")
            elif option in _MYPY_LOOSE_WHEN_FALSE:
                if current_value is not _MISSING and not isinstance(current_value, bool):
                    raise RatchetError(f"mypy option {option!r} on {module!r} must be a boolean")
                if current_value is False and base_value is not False:
                    findings.append(f"{module}: {option} set to false (looser)")
            else:
                raise RatchetError(f"mypy option {option!r} on {module!r} changed but has no known polarity; classify it in the guard (loose-when-true or loose-when-false) before changing it")
    return findings


@dataclass(frozen=True)
class Ratchet:
    """One ratchet file, its parser, and its explicit loosening direction."""

    key: str
    path: str
    description: str
    parse: Callable[[str, str], Any]
    loosenings: Callable[[Any, Any], list[str]]


RATCHETS: tuple[Ratchet, ...] = (
    Ratchet(
        "brand-consistency",
        DEFAULT_BASELINE,
        "per-file brand-copy occurrence counts (raise/add = looser)",
        parse_baseline,
        _counted_loosenings,
    ),
    Ratchet(
        "audit-coverage",
        f"{_ARCH}/audit_coverage_baseline.txt",
        "audited-route exceptions (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "duplicate-test-body",
        f"{_ARCH}/duplicate_test_body_baseline.txt",
        "duplicate test-body pairs (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "duplicate-validation-predicate",
        f"{_ARCH}/duplicate_validation_predicate_baseline.txt",
        "tolerated duplicate member pairs (add = looser)",
        _parse_json_group_pairs,
        _added_set_loosenings,
    ),
    Ratchet(
        "noop-bdd-then",
        f"{_ARCH}/noop_bdd_then_baseline.txt",
        "no-op BDD @then steps (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "self-asserting-bdd",
        f"{_ARCH}/self_asserting_bdd_baseline.txt",
        "self-asserting BDD steps (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "coverage-floor",
        "backend/pyproject.toml",
        "[tool.coverage.report] fail_under (lower = looser)",
        _parse_pyproject_coverage_floor,
        _lowered_floor_loosenings,
    ),
    Ratchet(
        "coverage-thresholds",
        ".github/scripts/coverage-thresholds.sh",
        "per-module and whole-suite coverage floors (lower = looser)",
        _parse_thresholds_shell,
        _lowered_floor_loosenings,
    ),
    Ratchet(
        "coverage-gate",
        "scripts/run_coverage_gate.py",
        "changed-line floors (lower = looser) and TINY_DIFF_THRESHOLD (higher = looser)",
        _parse_module_numeric_constants,
        _coverage_gate_loosenings,
    ),
    Ratchet(
        "vulture-confidence",
        "scripts/run_vulture.py",
        "vulture --min-confidence (higher = looser)",
        _parse_vulture_min_confidence,
        _vulture_loosenings,
    ),
    Ratchet(
        "ruff-per-file-ignores",
        "backend/pyproject.toml",
        "ruff per-file-ignore codes (add = looser)",
        _parse_ruff_per_file_ignores,
        _added_set_loosenings,
    ),
    Ratchet(
        "mypy-overrides",
        "backend/pyproject.toml",
        "mypy suppression options per module (add suppression = looser)",
        _parse_mypy_overrides,
        _mypy_loosenings,
    ),
    Ratchet(
        "excluded-packages",
        "backend/tests/architecture/test_test_suite_quality.py",
        "test-suite scanner EXCLUDED_PACKAGES (add = looser)",
        _parse_excluded_packages,
        _added_set_loosenings,
    ),
    Ratchet(
        "gitleaks-allowlist-paths",
        ".gitleaks.toml",
        "gitleaks allowlisted paths (add = looser)",
        _parse_gitleaks_allowlist_paths,
        _added_set_loosenings,
    ),
    Ratchet(
        "trivyignore",
        ".trivyignore",
        "ignored vulnerabilities (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "semgrepignore",
        ".semgrepignore",
        "semgrep-ignored paths (add = looser)",
        _parse_line_set,
        _added_set_loosenings,
    ),
    Ratchet(
        "sonar-coverage-exclusions",
        "sonar-project.properties",
        "sonar coverage exclusions (add = looser)",
        _parse_sonar_coverage_exclusions,
        _added_set_loosenings,
    ),
)


def _read_working_tree(repo_root: Path, relpath: str) -> str:
    path = repo_root / relpath
    if not path.is_file():
        raise RatchetError(f"ratchet file {relpath!r} is missing from the working tree. If the ratchet is being retired entirely, retire it from the guard in the same change (a human decision); otherwise restore the file.")
    return path.read_text(encoding="utf-8")


def _relative_to_repo(repo_root: Path, candidate: Path) -> str:
    try:
        return candidate.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise RatchetError(f"{candidate!s} is outside the repository; cannot read it from a git revision") from exc


def _print_failure(baseline_rel: str, rev: str, why: str, added: list[str], raised: list[tuple[str, int, int]]) -> None:
    print(
        f"ratchet-baseline guard: {baseline_rel} was LOOSENED against {rev} ({why}).",
        file=sys.stderr,
    )
    for path, base_count, new_count in raised:
        print(f"  RAISED  {path}: {new_count} > {base_count} (base {rev})", file=sys.stderr)
    for path in added:
        print(f"  ADDED   {path}: not in the base baseline ({rev})", file=sys.stderr)
    print(
        "\nA shrink-only baseline may only shrink. Fix the drift (replace the new occurrence with 'Farna Control') or, for an internal identifier that is not brand copy, get a deliberate human decision - do not raise the baseline to get green.",
        file=sys.stderr,
    )


def _run_counted(repo_root: Path, baseline_rel: str, explicit_base: str | None) -> int:
    """Legacy single-ratchet mode: the counted brand baseline, unchanged."""
    try:
        rev, why = _resolve_base(repo_root, explicit_base)

        baseline_path = repo_root / baseline_rel
        if not baseline_path.is_file():
            raise RatchetError(f"baseline {baseline_rel!r} is missing from the working tree. If the ratchet is being retired entirely, retire this guard in the same change (a human decision); otherwise restore the file.")
        current = parse_baseline(baseline_path.read_text(encoding="utf-8"), source=baseline_rel)

        base_text = read_baseline_at_rev(repo_root, rev, baseline_rel)
        if base_text is None:
            print(
                f"ratchet-baseline guard: OK - {baseline_rel} does not exist at {rev} ({why}); this change introduces the baseline, so there is nothing to ratchet against yet.",
                file=sys.stderr,
            )
            return 0
        base = parse_baseline(base_text, source=f"{baseline_rel}@{rev}")
    except RatchetError as exc:
        print(f"ratchet-baseline guard: FAILED CLOSED - {exc}", file=sys.stderr)
        return 1

    added, raised = compare(base, current)
    if added or raised:
        _print_failure(baseline_rel, rev, why, added, raised)
        return 1

    print(
        f"ratchet-baseline guard: OK - {baseline_rel} is not loosened against {rev} ({why}); {len(current)} entr{'y' if len(current) == 1 else 'ies'} checked, none added or raised.",
        file=sys.stderr,
    )
    return 0


def _run_all(repo_root: Path, explicit_base: str | None) -> int:
    """Check every enabled ratchet against one base revision."""
    try:
        rev, why = _resolve_base(repo_root, explicit_base)
    except RatchetError as exc:
        print(f"ratchet-baseline guard: FAILED CLOSED - {exc}", file=sys.stderr)
        return 1

    print(f"ratchet-baseline guard: base revision {rev} ({why}); checking {len(RATCHETS)} ratchets.", file=sys.stderr)
    failures: list[tuple[Ratchet, list[str]]] = []
    introduced: list[Ratchet] = []

    for ratchet in RATCHETS:
        try:
            current = ratchet.parse(_read_working_tree(repo_root, ratchet.path), source=ratchet.path)
            base_text = read_baseline_at_rev(repo_root, rev, ratchet.path)
            if base_text is None:
                introduced.append(ratchet)
                continue
            base = ratchet.parse(base_text, source=f"{ratchet.path}@{rev}")
            findings = ratchet.loosenings(base, current)
        except RatchetError as exc:
            print(f"ratchet-baseline guard: FAILED CLOSED - [{ratchet.key}] {ratchet.path}: {exc}", file=sys.stderr)
            return 1
        if findings:
            failures.append((ratchet, findings))
        else:
            print(f"ratchet-baseline guard: OK - [{ratchet.key}] {ratchet.path} is not loosened against {rev}.", file=sys.stderr)

    for ratchet in introduced:
        print(
            f"ratchet-baseline guard: OK - [{ratchet.key}] {ratchet.path} does not exist at {rev}; this change introduces the ratchet, so there is nothing to compare yet.",
            file=sys.stderr,
        )

    if failures:
        print(f"\nratchet-baseline guard: {len(failures)} ratchet(s) LOOSENED against {rev} ({why}):", file=sys.stderr)
        for ratchet, findings in failures:
            print(f"  [{ratchet.key}] {ratchet.path} - {ratchet.description}", file=sys.stderr)
            for finding in findings:
                print(f"      {finding}", file=sys.stderr)
        print(
            "\nA shrink-only ratchet may only be tightened. Fix the drift instead of weakening the gate; if the change is deliberate, get a human decision and record it with the change.",
            file=sys.stderr,
        )
        return 1

    print(
        f"ratchet-baseline guard: OK - none of the {len(RATCHETS)} ratchets is loosened against {rev} ({why}).",
        file=sys.stderr,
    )
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a shrink-only ratchet is loosened against the base revision.",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=(f"Legacy single-ratchet mode: compare this counted baseline only (default when omitted: every enabled ratchet). Example: {DEFAULT_BASELINE}."),
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base revision to compare against (e.g. origin/main). Resolved automatically when omitted.",
    )
    args = parser.parse_args(argv)

    repo_root = _resolve_repo_root()

    if args.baseline is not None:
        baseline_path = Path(args.baseline)
        if not baseline_path.is_absolute():
            baseline_path = repo_root / baseline_path
        try:
            baseline_rel = _relative_to_repo(repo_root, baseline_path)
        except RatchetError as exc:
            print(f"ratchet-baseline guard: FAILED CLOSED - {exc}", file=sys.stderr)
            return 1
        return _run_counted(repo_root, baseline_rel, args.base)

    return _run_all(repo_root, args.base)


if __name__ == "__main__":
    sys.exit(_main())
