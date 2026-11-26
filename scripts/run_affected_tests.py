#!/usr/bin/env python3
"""Fast best-guess affected-tests gate for pre-push.

Runs the changed test files plus candidate test files for changed source
modules — NOT the full backend suite.  The full backend unit suite runs in
CI's "Test (Backend)" job.

Modes
-----
Default (``--base origin/main``): every affected test file, every suite.
Used ad hoc.

``--prepush``: the bounded profile wired into the ``prepush-affected-tests``
hook.  It exists because a source change should exercise its tests *before* the
push, not only after CI finds the regression.  The profile adds four bounds:

* a file cap (``--max-files``, default 40) — a mass rename must not turn a
  pre-push hook into a suite run;
* a wall-clock budget (``--time-budget-seconds``, default 600) checked between
  suites, so the gate reports the remainder as deferred rather than silently
  running for half an hour;
* Docker/browser-dependent suites are skipped with an explicit
  "deferred to CI" line (never silently) — the E2B sandbox and a plain
  developer machine have no Docker, so an integration test there can only fail
  for the wrong reason;
* a hard refusal to run when no path scope can be resolved — the gate never
  falls back to the full suite.

Usage:
    python scripts/run_affected_tests.py [--list] [--base <ref>]
    python scripts/run_affected_tests.py --prepush
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

EXCLUDE_NAMES: frozenset[str] = frozenset({"conftest.py", "__init__.py"})
REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_TEST_FILES = 300
# Per-suite pytest --timeout, mirroring CI: ci.yml runs integration with
# --timeout=300 (testcontainer startup + migrations exceed the default 120).
SUITE_TIMEOUTS: dict[str, int] = {"integration": 300}

# Suites the bounded pre-push profile must not run, with the reason printed for
# each skipped one.  Deny-list (not allow-list) so a new plain unit suite is
# picked up automatically; a new Docker-dependent suite that is not listed here
# fails loudly in the gate, which is visible and fixable.
_PREPUSH_DEFERRED_SUITES: dict[str, str] = {
    "integration": "needs Docker/testcontainers",
    "docker": "needs Docker",
    "e2e": "needs Playwright browsers and a running app",
    "bdd": "browser/live-app scenarios; CI runs the self-contained subset",
    "load": "load suite, not a correctness gate",
}
_PREPUSH_MAX_FILES = 40
_PREPUSH_TIME_BUDGET_SECONDS = 600


def _git(*args: str) -> str | None:
    """Run a git command and return stripped stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def _resolve_merge_base(base_ref: str) -> str | None:
    """Return the merge-base between *base_ref* and HEAD, or None."""
    return _git("merge-base", base_ref, "HEAD")


def _collect_changed_files(base: str) -> list[str] | None:
    """Return the files changed between *base* and HEAD.

    Returns ``None`` — not ``[]`` — when the scope cannot be established at all
    (no merge base with *base* or ``HEAD~1``, or a failed ``git diff``).  The
    caller distinguishes "nothing changed" (``[]``, nothing to run) from
    "scope unknown" (``None``, refuse in the pre-push profile rather than
    guessing), so an unknown scope can never be mistaken for a clean tree.
    """
    merge_base = _resolve_merge_base(base)
    if merge_base is None:
        # Fallback: HEAD~1
        merge_base = _resolve_merge_base("HEAD~1")
        if merge_base is None:
            print(
                "notice: cannot determine merge-base with origin/main or HEAD~1 — scope unknown",
                file=sys.stderr,
            )
            return None
    raw = _git("diff", "--name-only", "--diff-filter=ACMR", f"{merge_base}...HEAD")
    if raw is None:
        print("notice: `git diff` failed — scope unknown", file=sys.stderr)
        return None
    if not raw:
        return []
    return raw.splitlines()


def _stem(path: str) -> str:
    """Return filename stem (no extension) from a POSIX-style path."""
    return Path(path).stem


def _matches_stem(name: str, stem: str) -> bool:
    """Word-boundary stem match for candidate test filenames.

    ``me`` must match ``test_me.py`` / ``test_me_password.py`` but NOT
    ``test_metrics_ingest.py`` — a bare ``test_{stem}*`` prefix match
    over-selects unrelated suites and can drag a different test SUITE (which
    needs a different process environment) into the gate.  The next character
    after ``test_{stem}`` must be the end of the string or ``_``.
    """
    prefix = f"test_{stem}"
    if name == f"{prefix}.py":
        return True
    return name.startswith(f"{prefix}_")


def _suite_of(repo_relative_test_path: str) -> str:
    """Return the test-suite directory name for a repo-relative test path.

    ``backend/tests/unit/api/test_x.py`` -> ``unit``; ``backend/tests/integration/bdd/test_x.py``
    -> ``integration``; a test file directly under ``backend/tests/`` falls
    back to the ``tests`` pseudo-suite.
    """
    parts = Path(repo_relative_test_path).parts
    if len(parts) >= 4:
        return parts[2]
    return "tests"


def _group_by_suite(test_paths: list[str]) -> dict[str, list[str]]:
    """Group test paths by their suite directory, preserving sorted order."""
    groups: dict[str, list[str]] = {}
    for p in sorted(test_paths):
        groups.setdefault(_suite_of(p), []).append(p)
    return dict(sorted(groups.items()))


def _select_test_files(changed: list[str]) -> list[str]:
    """Derive the set of test file paths (repo-relative) to run."""
    selected: set[str] = set()
    backend_root = Path("backend")

    for f in changed:
        if not f.startswith("backend/"):
            continue
        name = Path(f).name
        if name in EXCLUDE_NAMES:
            continue

        if f.startswith("backend/tests/") and f.endswith(".py"):
            selected.add(f)
            continue

        if f.startswith("backend/src/") and f.endswith(".py"):
            stem = _stem(f)
            pattern = str(backend_root / "tests" / "**" / f"test_{stem}*.py")
            for match in sorted(backend_root.parent.glob(pattern)):
                rel = match.as_posix()
                if rel not in selected and _matches_stem(Path(rel).name, stem):
                    selected.add(rel)

    return sorted(selected)[:MAX_TEST_FILES]


def _run_pytest(test_paths: list[str], timeout: int = 120, wall_timeout: float | None = None) -> int:
    """Run pytest on *test_paths* from the backend/ directory. Returns exit code.

    *timeout* bounds each individual test (pytest ``--timeout``).  *wall_timeout*
    bounds the whole suite process; it is used by the pre-push profile so a
    pathological suite cannot turn a push into an unbounded wait.
    """
    cmd = [
        "uv",
        "run",
        "--no-sync",
        # FAR-2308: never let a test hook rewrite backend/uv.lock mid-gate.
        "--frozen",
        "pytest",
        "--tb=short",
        "-q",
        f"--timeout={timeout}",
        # Defence in depth for the gated-suite warning gate installed by
        # tests/conftest.py::pytest_runtest_setup (the suites selected here are
        # all gated trees). Byte-identical to every CI command; pinned by
        # tests/architecture/test_ci_pytest_commands_carry_the_warning_gate.py.
        "-W",
        "error::pytest.PytestUnraisableExceptionWarning",
        "--disable-warnings",
        *test_paths,
    ]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    print("from:    backend/", file=sys.stderr)
    try:
        result = subprocess.run(cmd, cwd="backend", check=False, timeout=wall_timeout)  # NOSONAR - fixed argv, no shell
    except subprocess.TimeoutExpired:
        print(
            f"FAILED: suite exceeded its {wall_timeout:.0f}s wall-clock bound and was killed. Rerun the suite command above to see where it hangs.",
            file=sys.stderr,
        )
        return 1
    return result.returncode


def _count_all_test_files(root: Path | None = None) -> int:
    """Return how many ``backend/tests/**/test_*.py`` files exist under *root*.

    *root* defaults to the repository containing this script (not the current
    directory), so the count is correct whichever directory the gate is
    invoked from.
    """
    base = (root or REPO_ROOT) / "backend" / "tests"
    return sum(1 for _ in base.rglob("test_*.py"))


def _is_whole_suite(selected: list[str], root: Path | None = None) -> bool:
    """True when *selected* is the entire backend test suite.

    The pre-push gate must never run the full backend suite.  Comparing against
    the on-disk count (rather than a magic number) keeps the guard correct as
    the suite grows.
    """
    total = _count_all_test_files(root)
    return total > 0 and len(selected) >= total


def _apply_prepush_bounds(
    test_paths: list[str],
    max_files: int,
) -> list[str]:
    """Cap the selection at *max_files*, reporting any truncation loudly."""
    if len(test_paths) <= max_files:
        return test_paths
    print(
        f"Notice: {len(test_paths)} affected test files (mass change/sweep). Running the first {max_files} to stay bounded (CI runs the full suite).",
        file=sys.stderr,
    )
    return test_paths[:max_files]


def _split_deferred(
    grouped: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[tuple[str, str, list[str]]]]:
    """Split *grouped* into (runnable, deferred) for the pre-push profile."""
    runnable: dict[str, list[str]] = {}
    deferred: list[tuple[str, str, list[str]]] = []
    for suite, paths in grouped.items():
        reason = _PREPUSH_DEFERRED_SUITES.get(suite)
        if reason is None:
            runnable[suite] = paths
        else:
            deferred.append((suite, reason, paths))
    return runnable, deferred


def _report_deferred(deferred: list[tuple[str, str, list[str]]]) -> None:
    """Print one loud line per suite the pre-push profile did not run."""
    for suite, reason, paths in deferred:
        print(
            f"deferred to CI: suite [{suite}] ({len(paths)} file(s)) — {reason}; not run at pre-push (never silently skipped)",
            file=sys.stderr,
        )


def _parse_args(args: list[str]) -> dict[str, object] | None:
    """Parse the CLI into an options dict, or return ``None`` on a usage error."""
    options: dict[str, object] = {
        "list_only": False,
        "base": "origin/main",
        "prepush": False,
        "max_files": None,
        "time_budget_seconds": None,
    }
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--list":
            options["list_only"] = True
        elif arg == "--prepush":
            options["prepush"] = True
        elif arg in ("--base", "--max-files", "--time-budget-seconds"):
            i += 1
            if i >= len(args):
                print(f"error: {arg} requires a value", file=sys.stderr)
                return None
            value = args[i]
            if arg == "--base":
                options["base"] = value
            elif arg == "--max-files":
                options["max_files"] = _positive_int(value, arg)
            else:
                options["time_budget_seconds"] = _positive_int(value, arg)
            if (arg == "--max-files" and options["max_files"] is None) or (arg == "--time-budget-seconds" and options["time_budget_seconds"] is None):
                return None
        else:
            print(f"error: unexpected argument {arg!r}", file=sys.stderr)
            return None
        i += 1
    return options


def _positive_int(value: str, flag: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        print(f"error: {flag} expects an integer, got {value!r}", file=sys.stderr)
        return None
    if parsed <= 0:
        print(f"error: {flag} expects a positive integer, got {parsed}", file=sys.stderr)
        return None
    return parsed


def _rerun_command(base: str, prepush: bool) -> str:
    suffix = " --prepush" if prepush else ""
    return f"uv run --project backend --no-sync --frozen python scripts/run_affected_tests.py --base {base}{suffix}"


def main(argv: list[str] | None = None) -> int:
    options = _parse_args(argv if argv is not None else sys.argv[1:])
    if options is None:
        return 2

    list_only = bool(options["list_only"])
    base = str(options["base"])
    prepush = bool(options["prepush"])
    max_files = int(options["max_files"] or (_PREPUSH_MAX_FILES if prepush else MAX_TEST_FILES))
    time_budget = options["time_budget_seconds"]
    if time_budget is None and prepush:
        time_budget = _PREPUSH_TIME_BUDGET_SECONDS

    started = time.monotonic()
    changed = _collect_changed_files(base)
    if changed is None:
        # Scope unknown.  Non-pre-push keeps its historical skip; the pre-push
        # profile REFUSES, because silently skipping would make a disabled gate
        # indistinguishable from a passing one, and falling back to the full
        # suite is never allowed.
        if prepush:
            print(
                "FAILED: cannot resolve the changed-path scope against the base ref — refusing to run (this gate never falls back to the full backend suite; fetch origin/main and retry)",
                file=sys.stderr,
            )
            return 2
        print("no affected tests selected - skipping (CI runs the full suite)", file=sys.stderr)
        return 0

    test_paths = _apply_prepush_bounds(_select_test_files(changed), max_files)
    if not test_paths:
        print("no affected tests selected - skipping (CI runs the full suite)", file=sys.stderr)
        return 0

    if _is_whole_suite(test_paths):
        print(
            f"FAILED: refusing to run — the selection is the entire backend test suite ({len(test_paths)} files). This gate may only run affected tests; the full suite is CI's job.",
            file=sys.stderr,
        )
        return 2

    if list_only:
        for p in test_paths:
            print(p)
        return 0

    print(f"selected {len(test_paths)} test file(s):", file=sys.stderr)
    for p in test_paths:
        print(f"  {p}", file=sys.stderr)

    # Run each SUITE in its own pytest process. Conftest files set
    # process-wide environment (e.g. tests/bdd/conftest.py points DATABASE_URL
    # at sqlite at import time); mixing suites (BDD + integration) in one
    # process lets one conftest's environment leak into the other's tests and
    # fail them spuriously (mirrors CI, which runs each suite as a separate
    # job). Selection is unchanged — every affected test still runs.
    grouped = _group_by_suite(test_paths)
    if prepush:
        grouped, deferred = _split_deferred(grouped)
        _report_deferred(deferred)

    worst = 0
    deferred_by_budget: list[str] = []
    for suite, paths in grouped.items():
        if time_budget is not None:
            elapsed = time.monotonic() - started
            if elapsed >= float(str(time_budget)):
                deferred_by_budget.append(suite)
                continue
            remaining = float(str(time_budget)) - elapsed
        else:
            remaining = None
        out_paths = [p.removeprefix("backend/").removeprefix("./") for p in paths]
        print(f"\nsuite [{suite}] ({len(paths)} file(s)):", file=sys.stderr)
        for p in paths:
            print(f"  {p}", file=sys.stderr)
        run_kwargs: dict[str, object] = {"timeout": SUITE_TIMEOUTS.get(suite, 120)}
        if prepush:
            # The wall-clock bound is a pre-push-only guarantee; the default
            # (ad hoc) invocation keeps its historical call shape.
            run_kwargs["wall_timeout"] = remaining
        code = _run_pytest(out_paths, **run_kwargs)
        if code != 0:
            worst = code

    for suite in deferred_by_budget:
        print(
            f"deferred to CI: suite [{suite}] — the {time_budget}s pre-push time budget was exhausted (not run; never silently skipped)",
            file=sys.stderr,
        )

    if worst != 0:
        print(f"\nFAILED: affected tests did not pass. Rerun locally with:\n    {_rerun_command(base, prepush)}", file=sys.stderr)
    return worst


if __name__ == "__main__":
    sys.exit(main())
