"""Architecture gate: the retired product name stays out of the tree (FAR-2641).

The product is Farna Control; ``Modulo`` is the retired name. The rename swept
the product surface, but a stray browser tab title or a user-facing string can
reintroduce the old name and no lint rule catches it. This gate freezes every
remaining occurrence in a shrink-only baseline so drift cannot grow: a *new*
occurrence in a file the baseline does not list fails immediately, and so does
an *increase* in a file the baseline does list. A count at or below its
baseline passes, so later slices can lower the numbers (or drop a file) without
touching the gate.

Scope and mechanics
-------------------
* Scope is exactly the tracked working tree: paths come from ``git ls-files``
  and matches from ``git grep`` over tracked files only. Untracked build output
  (``node_modules``, ``dist``, ``.venv``) can therefore never create a false
  failure.
* The needle is the bare word ``modulo`` matched case-insensitively, per the
  ticket's shape. ``-o`` makes every match its own record, so the baseline
  counts occurrences rather than matching lines.
* ``brand_consistency_baseline.txt`` holds one ``<relative-path>\\t<count>``
  line per file (blank lines and ``#`` comments are ignored).

Documented allowlist exceptions
-------------------------------
* **The arithmetic operator.** ``modulo`` also names the arithmetic operation,
  and the case-insensitive needle also matches internal identifiers that carry
  the old name (DB roles, the reserved ``modulo`` role/schema, CSS classes).
  Those are not brand copy; they are frozen in the baseline rather than
  exempted line-by-line because they are not syntactically distinguishable from
  the product name.
* **Git history.** History is not scanned at all. ``git grep`` searches the
  working tree of tracked files, never past commits, so an old commit message
  or a historic revision is out of scope by construction.
* **This gate's own two files** are excluded: the test must spell the needle
  and the baseline's path list can legitimately contain it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
BASELINE_PATH = Path(__file__).resolve().parent / "brand_consistency_baseline.txt"

#: The retired product name, matched case-insensitively (see the module docstring).
_NEEDLE = "modulo"

#: The gate's own files, relative to the repository root. Excluded so the gate
#: cannot trip over the needle it documents or over paths in its own baseline.
_GATE_PATHS = frozenset(
    {
        "backend/tests/architecture/test_brand_consistency.py",
        "backend/tests/architecture/brand_consistency_baseline.txt",
    }
)


def _run_git(args: list[str]) -> str:
    """Run one git command from the repository root and return its stdout.

    Only exit code 1 (git grep: no matches) is a normal non-zero result; any
    other failure is raised so a broken scan can never look like a clean tree.
    """
    git = shutil.which("git")
    if git is None:
        raise AssertionError("Unable to scan tracked files for brand drift: git is unavailable")
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell, no untrusted input
            [git, *args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AssertionError(f"Unable to run git {' '.join(args)}: {exc}") from exc
    if result.returncode not in (0, 1):
        raise AssertionError(f"git {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}")
    return result.stdout


def _tracked_paths() -> set[str]:
    """Every path git tracks, so the scan is provably limited to the working set."""
    return {path for path in _run_git(["ls-files", "-z"]).split("\0") if path}


def _occurrences_by_file() -> dict[str, int]:
    """Count case-insensitive occurrences of the retired name per tracked file.

    ``git grep -z`` emits ``path\\0lineno\\0match`` records (one line per
    occurrence, trailing NUL per record), so the path is parsed before the
    first NUL rather than by splitting on a colon a path could contain.
    """
    tracked = _tracked_paths()
    counts: Counter[str] = Counter()
    for record in _run_git(["grep", "-i", "-F", "-o", "-n", "-z", "--", _NEEDLE]).split("\n"):
        if not record:
            continue
        path, _, _ = record.split("\0", 2)
        if path in _GATE_PATHS:
            continue
        if path not in tracked:
            raise AssertionError(f"git grep returned an untracked path {path!r}; the scan must be tracked-only")
        counts[path] += 1
    return dict(counts)


def _load_baseline() -> dict[str, int]:
    """Read ``<relative-path>\\t<allowed_occurrences>`` entries from the baseline."""
    baseline: dict[str, int] = {}
    for raw in BASELINE_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        path, separator, count_text = line.rpartition("\t")
        if not separator:
            raise AssertionError(f"Malformed brand_consistency_baseline.txt line: {raw!r}")
        baseline[path] = int(count_text)
    return baseline


def test_no_new_or_growing_retired_brand_occurrences() -> None:
    """Fail on a new file or a grown count; a count at or below baseline passes."""
    baseline = _load_baseline()
    actual = _occurrences_by_file()

    new_files = sorted(path for path in actual if path not in baseline)
    assert not new_files, "A file that is not in brand_consistency_baseline.txt contains the retired product name 'Modulo'/'modulo'. Use 'Farna Control' for brand copy (or, for an internal identifier that is not brand copy, add the file and its count to the baseline):\n" + "\n".join(
        f"  {path} ({actual[path]})" for path in new_files
    )

    grown = sorted((path, actual[path], baseline[path]) for path in actual if path in baseline and actual[path] > baseline[path])
    assert not grown, "A file gained more occurrences of the retired product name than its brand_consistency_baseline.txt allowance. Replace the new occurrence with 'Farna Control' instead of raising the baseline:\n" + "\n".join(f"  {path}: {now} > {allowed}" for path, now, allowed in grown)
