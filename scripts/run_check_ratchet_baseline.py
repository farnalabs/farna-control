#!/usr/bin/env python3
"""Fail CI when a shrink-only baseline is LOOSENED instead of obeyed (FAR-2699).

A shrink-only ratchet is only a control if raising it fails. The brand gate
(``backend/tests/architecture/test_brand_consistency.py``) freezes each file's
occurrence count in ``brand_consistency_baseline.txt`` and its failure message
says to *replace* the drift, never to raise the baseline. But the test only
compares the working tree against the baseline it is handed, so an agent that
edits the baseline *and* the drift together passes: the ratchet is silently
loosened for good. That is exactly what happened when the autonomous Branch
Fixer raised two counts (36 -> 38 and 3 -> 4) to get green and the PRs merged.

This guard closes that hole by comparing the baseline file in the working tree
against the SAME file at the base revision. It fails when:

* an entry's count is HIGHER than the base revision's count for that path, or
* an entry exists in the working tree but not in the base revision (a file was
  ADDED to the baseline).

Removing an entry, or lowering a count, passes - that is the ratchet turning.

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
* a git failure reading the baseline at a base that does resolve;
* a malformed working-tree baseline (no ``<path>\\t<count>`` separator, a
  non-integer count, a duplicate path), or a missing working-tree baseline.

A baseline that does not exist at the base revision is the one legitimate
"nothing to compare" case: it means this change introduces the baseline, so
the guard passes with a loud notice. (The file already exists on main today, so
this branch is only reachable when a brand-new baseline is added.)

Usage
-----
    # CI (base auto-resolved):
    uv run --no-build --project backend --no-sync python scripts/run_check_ratchet_baseline.py

    # Locally, against the base branch:
    python scripts/run_check_ratchet_baseline.py --base origin/main

Exit 0 clean, 1 loosened / unreadable / malformed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

#: The shrink-only baseline this guard protects by default.
DEFAULT_BASELINE = "backend/tests/architecture/brand_consistency_baseline.txt"

#: Environment hint the CI workflow passes for a push event.
BASE_ENV_VAR = "RATCHET_BASE_REV"

#: The single-commit parent revision, used as a documented fallback base.
_PARENT_REV = "HEAD^"


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
    """Return ``(rev, why)`` for the revision the baseline is compared against."""
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


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail when a shrink-only baseline is loosened against the base revision.",
    )
    parser.add_argument(
        "--baseline",
        default=DEFAULT_BASELINE,
        help=f"Baseline file, relative to the repo root (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base revision to compare against (e.g. origin/main). Resolved automatically when omitted.",
    )
    args = parser.parse_args(argv)

    repo_root = _resolve_repo_root()
    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = repo_root / baseline_path
    try:
        baseline_rel = baseline_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        print(
            f"ratchet-baseline guard: --baseline {str(baseline_path)!r} is outside the repository; cannot read it from a git revision",
            file=sys.stderr,
        )
        return 1

    try:
        rev, why = _resolve_base(repo_root, args.base)

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


if __name__ == "__main__":
    sys.exit(_main())
