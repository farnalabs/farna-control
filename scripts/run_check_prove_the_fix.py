#!/usr/bin/env python3
"""Require a proving test for a PR that changes product code (FAR-2701).

A PR that touches ``backend/src/**`` or ``frontend/src/**`` must name at least
one test that proves the change, in its body::

    Proves: backend/tests/unit/api/test_health.py::test_liveness_ok

The check is deterministic and uses no model: it reads the PR body and the
changed-file list, then verifies each named reference resolves to a test that
exists in the tree. That closes the "green PR proves nothing" gap - a reviewer
could previously only infer proof from the diff, and a test that already passed
before the change proves nothing about it.

Why a marker rather than a diff heuristic? Mapping "this line of src changed" to
"this test exercises it" is a judgement call no deterministic tool can make
honestly. Asking the author to name the test is one line of prose, and it is
checkable: the test must exist, and (by review convention) must fail without the
change. ``docs/product-map/README.md`` documents the convention.

Usage
-----
    # CI (the changed-file list and the PR body are handed in by the workflow)
    python scripts/run_check_prove_the_fix.py \\
        --pr-body /tmp/pr-body.md --changed-files /tmp/changed-files.txt

    # Locally, against a branch
    git diff --name-only origin/main...HEAD > /tmp/changed.txt
    echo "Proves: backend/tests/unit/api/test_health.py::test_liveness_ok" > /tmp/body.md
    python scripts/run_check_prove_the_fix.py --pr-body /tmp/body.md --changed-files /tmp/changed.txt

Exit 0 when the rule is satisfied (or when the diff needs no marker), 1 when it
is violated, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: A change to these trees changes product behaviour and needs a proving test.
PRODUCT_PREFIXES = (
    "backend/src/",
    "frontend/src/",
)

#: The marker line. Tolerates a markdown bullet and surrounding backticks; the
#: remainder is captured whole so a malformed marker (several refs, prose) is
#: reported as malformed rather than as absent.
_MARKER_RE = re.compile(r"^\s*(?:[-*+]\s+)?proves:\s*(?P<rest>.*?)\s*$", re.IGNORECASE)

#: ``<test module>::[<Class>::]<test name>`` - a pytest node id under backend/tests.
_NODE_RE = re.compile(r"^(?P<file>backend/tests/[A-Za-z0-9_./-]+\.py)::(?:(?P<owner>[A-Za-z0-9_]+)::)?(?P<name>test_[A-Za-z0-9_]+)$")

_FUNC_RE = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>test_[A-Za-z0-9_]+)", re.MULTILINE)
_CLASS_RE_TEMPLATE = r"^class\s+{owner}\b"


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"prove-the-fix: cannot read {path}: {exc}") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def product_changes(changed_files: list[str]) -> list[str]:
    """The changed files that live under a product-code prefix."""
    return sorted(path for path in changed_files if path.replace("\\", "/").startswith(PRODUCT_PREFIXES))


def marker_refs(pr_body: str) -> tuple[list[str], list[str]]:
    """Every ``Proves:`` reference in the PR body, backticks stripped.

    Returns ``(refs, malformed)``; a ``malformed`` entry is a marker line that
    names several references or something that is not a single node id, so the
    failure message can point at the line instead of claiming the marker is
    absent.
    """
    refs: list[str] = []
    malformed: list[str] = []
    for line in pr_body.splitlines():
        match = _MARKER_RE.match(line)
        if match is None:
            continue
        ref = match.group("rest").strip().strip("`").strip()
        if not ref:
            malformed.append(line.strip())
        elif "," in ref or " " in ref:
            malformed.append(f"{line.strip()} (name one test per Proves: line)")
        else:
            refs.append(ref)
    return refs, malformed


def verify_ref(ref: str, repo_root: Path) -> str | None:
    """Return a one-line problem description, or ``None`` when the ref is valid."""
    match = _NODE_RE.match(ref)
    if match is None:
        return f"{ref!r} is not a pytest node id of the form backend/tests/<module>.py::test_name"
    module = repo_root / match.group("file")
    if not module.is_file():
        return f"{ref!r} names a test module that does not exist: {match.group('file')}"
    try:
        source = module.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{ref!r} names a test module that cannot be read: {exc}"
    if match.group("name") not in {found.group("name") for found in _FUNC_RE.finditer(source)}:
        return f"{ref!r} names a test that is not defined in {match.group('file')}"
    owner = match.group("owner")
    if owner is not None and re.search(_CLASS_RE_TEMPLATE.format(owner=re.escape(owner)), source, re.MULTILINE) is None:
        return f"{ref!r} names class {owner!r}, which is not defined in {match.group('file')}"
    return None


def evaluate(pr_body: str, changed_files: list[str], repo_root: Path) -> int:
    """Apply the rule; returns the process exit code."""
    touched = product_changes(changed_files)
    if not touched:
        print("prove-the-fix: OK - the diff touches no product source, so no proving test is required.")
        return 0

    refs, malformed = marker_refs(pr_body)
    if malformed:
        print(
            "prove-the-fix: FAILED - this PR's body carries a Proves: line that does not name exactly one test:\n" + "\n".join(f"  {line}" for line in malformed),
            file=sys.stderr,
        )
        return 1
    if not refs:
        print(
            "prove-the-fix: FAILED - this PR changes product source but its body carries no proving test.\n"
            f"  product files changed ({len(touched)}), e.g. {', '.join(touched[:5])}\n"
            "  Add a line to the PR body:\n"
            "    Proves: backend/tests/<path>/test_<module>.py::test_<name>\n"
            "  Name the test that would FAIL without this change (docs/product-map/README.md documents the convention).",
            file=sys.stderr,
        )
        return 1

    problems = [problem for problem in (verify_ref(ref, repo_root) for ref in refs) if problem is not None]
    if problems:
        print(
            "prove-the-fix: FAILED - the PR body names a proving test that does not exist:\n" + "\n".join(f"  {problem}" for problem in problems),
            file=sys.stderr,
        )
        return 1

    print(f"prove-the-fix: OK - {len(refs)} proving test(s) named and present: {', '.join(refs)}")
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Require a Proves: marker naming an existing test when product source changes.")
    parser.add_argument("--pr-body", required=True, help="File holding the pull-request body (markdown).")
    parser.add_argument("--changed-files", required=True, help="File holding the changed paths, one per line.")
    parser.add_argument("--repo-root", default=None, help="Repository root (defaults to the parent of this script's directory).")
    args = parser.parse_args(argv)

    body_path = Path(args.pr_body)
    try:
        body = body_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"prove-the-fix: FAILED - cannot read the PR body file {body_path}: {exc}", file=sys.stderr)
        return 2
    changed = _read_lines(Path(args.changed_files))
    root = Path(args.repo_root).resolve() if args.repo_root else _repo_root()
    return evaluate(body, changed, root)


if __name__ == "__main__":
    sys.exit(main())
