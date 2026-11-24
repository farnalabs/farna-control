#!/usr/bin/env python3
"""Require a proving test for a PR that changes product code (FAR-2701).

A PR that touches ``backend/src/**`` or ``frontend/src/**`` must name at least
one test that proves the change, in its body::

    Proves: backend/tests/unit/api/test_health.py::test_liveness_ok
    Proves: frontend/src/__tests__/ActionRow.spec.ts::stacks below sm and becomes a row at sm

The check is deterministic and uses no model: it reads the PR body and the
changed-file list, then verifies each named reference resolves to a test that
EXISTS, that this PR actually changes the module holding it, and that the test is
not skipped or expected to fail. That closes the "green PR proves nothing" gap -
a reviewer could previously only infer proof from the diff, and an unchanged or
skipped test proves nothing about the change.

Why a marker rather than a diff heuristic? Mapping "this line of src changed" to
"this test exercises it" is a judgement call no deterministic tool can make
honestly. Asking the author to name the test is one line of prose, and it is
checkable: the test must exist, must be touched by the same PR, and (by review
convention) must fail without the change. ``docs/product-map/README.md``
documents the convention.

Fenced code blocks and HTML comments are stripped before the ``Proves:`` lines
are read, so *documenting* the convention inside a PR body (the README example is
itself a fenced block) can never satisfy it.

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
import ast
import re
import sys
from pathlib import Path

#: A change to these trees changes product behaviour and needs a proving test.
PRODUCT_PREFIXES = (
    "backend/src/",
    "frontend/src/",
)

#: The only roots a proving test may live in. Anything else (a src module, a doc,
#: a path outside the repo) is rejected even if it happens to exist.
BACKEND_TEST_ROOT = "backend/tests/"
FRONTEND_TEST_ROOTS = ("frontend/src/", "frontend/tests/")

#: The marker line. Tolerates a markdown bullet and surrounding backticks; the
#: remainder is captured whole so a malformed marker (several refs, prose) is
#: reported as malformed rather than as absent.
_MARKER_RE = re.compile(r"^\s*(?:[-*+]\s+)?proves:\s*(?P<rest>.*?)\s*$", re.IGNORECASE)

#: ``<test module>::[<Class>::]<test name>`` - a pytest node id under backend/tests.
_NODE_RE = re.compile(r"^(?P<file>backend/tests/[A-Za-z0-9_./-]+\.py)::(?:(?P<owner>[A-Za-z0-9_]+)::)?(?P<name>test_[A-Za-z0-9_]+)$")

#: ``frontend/(src|tests)/.../*.spec.ts::<test title>`` - a Vitest or Playwright node.
_SPEC_RE = re.compile(r"^(?P<file>frontend/(?:src|tests)/[A-Za-z0-9_./-]+\.spec\.ts)::(?P<name>[^,]+)$")

#: A JS/TS test call whose first string argument is the test title. Consumes the
#: modifier chain (``it.skip``, ``it.each([...])(``, ``test.describe``) first.
_TS_TEST_CALL_RE = re.compile(r"\b(?:it|test)(?:\.[A-Za-z_$][\w$]*(?:\s*\((?:[^()]|\([^()]*\))*\))?)*\s*\(\s*(?P<quote>['\"`])(?P<name>(?:\\.|(?!\1).)*)\1")

#: Fenced markdown code blocks and HTML comments: documenting the convention
#: must not satisfy it.
_FENCE_RE = re.compile(r"(?P<fence>```|~~~).*?(?P=fence)", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: Decorators and body openers that mean "this test does not run (or is expected
#: to fail)", so it cannot prove anything.
_SKIP_MARKER_RE = re.compile(r"^pytest\.mark\.(?:skip|skipif|xfail)\b")
_SKIP_BODY_CALLS = frozenset({"pytest.skip", "pytest.xfail"})


def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"prove-the-fix: cannot read {path}: {exc}") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def strip_documentation(pr_body: str) -> str:
    """Blank out fenced code blocks and HTML comments before reading markers."""
    without_fences = _FENCE_RE.sub("\n", pr_body)
    return _HTML_COMMENT_RE.sub("\n", without_fences)


def product_changes(changed_files: list[str]) -> list[str]:
    """The changed files that live under a product-code prefix."""
    return sorted(path for path in _normalised(changed_files) if path.startswith(PRODUCT_PREFIXES))


def _normalised(changed_files: list[str]) -> list[str]:
    return [path.replace("\\", "/") for path in changed_files]


def marker_refs(pr_body: str) -> tuple[list[str], list[str]]:
    """Every ``Proves:`` reference in the PR body, backticks stripped.

    Returns ``(refs, malformed)``; a ``malformed`` entry is a marker line that
    names several references or nothing, so the failure message can point at the
    line instead of claiming the marker is absent. Fenced blocks and HTML
    comments are stripped first (see :func:`strip_documentation`).
    """
    refs: list[str] = []
    malformed: list[str] = []
    for line in strip_documentation(pr_body).splitlines():
        match = _MARKER_RE.match(line)
        if match is None:
            continue
        ref = match.group("rest").strip().strip("`").strip()
        if not ref:
            malformed.append(line.strip())
        elif "," in ref:
            malformed.append(f"{line.strip()} (name one test per Proves: line)")
        else:
            refs.append(ref)
    return refs, malformed


def _resolve_test_module(rel: str, repo_root: Path) -> tuple[Path | None, str | None]:
    """Resolve a repo-relative test path, confined to the test roots and the repo."""
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        return None, f"{rel!r} must be a repo-relative path"
    if ".." in Path(rel).parts:
        return None, f"{rel!r} contains a '..' traversal segment"
    if not (rel.startswith(BACKEND_TEST_ROOT) or rel.startswith(FRONTEND_TEST_ROOTS)):
        return None, f"{rel!r} is not under {BACKEND_TEST_ROOT} or a frontend test root ({', '.join(FRONTEND_TEST_ROOTS)})"
    root = repo_root.resolve()
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, f"{rel!r} resolves outside the repository"
    allowed = tuple((root / prefix).resolve() for prefix in (BACKEND_TEST_ROOT, *FRONTEND_TEST_ROOTS))
    if not any(_is_within(candidate, base) for base in allowed):
        return None, f"{rel!r} resolves outside the test roots"
    return candidate, None


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True


def _find_function(tree: ast.Module, name: str, owner: str | None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The test function *name* (optionally inside class *owner*) in *tree*."""
    for node in tree.body:
        if owner is None and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
        if isinstance(node, ast.ClassDef) and (owner is None or node.name == owner):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                    return child
    return None


def _skip_reason(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Why *fn* cannot prove anything (skipped/xfail), or ``None``."""
    for decorator in fn.decorator_list:
        try:
            text = ast.unparse(decorator)
        except (ValueError, RecursionError):  # pragma: no cover - defensive
            continue
        if _SKIP_MARKER_RE.match(text):
            return f"it is decorated with @{text.split('(')[0]}"
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Call):
        try:
            callee = ast.unparse(body[0].value.func)
        except (ValueError, RecursionError):  # pragma: no cover - defensive
            callee = ""
        if callee in _SKIP_BODY_CALLS:
            return f"its body starts with {callee}()"
    return None


def _verify_pytest_ref(ref: str, match: re.Match[str], module: Path) -> str | None:
    try:
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
    except (OSError, SyntaxError) as exc:
        return f"{ref!r} names a test module that cannot be parsed: {exc}"
    fn = _find_function(tree, match.group("name"), match.group("owner"))
    if fn is None:
        owner = match.group("owner")
        where = f"class {owner!r} in " if owner else ""
        return f"{ref!r} names a test that is not defined in {where}{match.group('file')}"
    reason = _skip_reason(fn)
    if reason is not None:
        return f"{ref!r} names a test that cannot prove anything: {reason}"
    return None


def _verify_spec_ref(ref: str, match: re.Match[str], module: Path) -> str | None:
    try:
        source = module.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{ref!r} names a spec that cannot be read: {exc}"
    name = match.group("name").strip()
    found = False
    for call in _TS_TEST_CALL_RE.finditer(source):
        text = call.group(0)
        prefix = text[: text.index(call.group("quote"))]
        if "describe" in prefix:
            continue
        if call.group("name") != name:
            continue
        found = True
        if re.search(r"\.(?:skip|fixme|todo)\b", prefix):
            return f"{ref!r} names a spec test that cannot prove anything: it is marked skipped/fixme/todo"
    if not found:
        return f"{ref!r} names a test title that is not defined in {match.group('file')}"
    return None


def verify_ref(ref: str, repo_root: Path, changed_files: list[str]) -> str | None:
    """Return a one-line problem description, or ``None`` when the ref is valid."""
    spec_match = _SPEC_RE.match(ref)
    node_match = _NODE_RE.match(ref)
    match = node_match or spec_match
    if match is None:
        return f"{ref!r} is not a pytest node id (backend/tests/<module>.py::test_name) or a frontend spec node (frontend/(src|tests)/<spec>.spec.ts::<test title>)"
    module, problem = _resolve_test_module(match.group("file"), repo_root)
    if problem is not None:
        return f"{ref!r} {problem}"
    assert module is not None
    if not module.is_file():
        return f"{ref!r} names a test module that does not exist: {match.group('file')}"
    if match.group("file") not in set(_normalised(changed_files)):
        return f"{ref!r} names a test module this PR does not change ({match.group('file')}); a proving test must be added or updated in the same change"
    if spec_match is not None:
        return _verify_spec_ref(ref, spec_match, module)
    return _verify_pytest_ref(ref, node_match, module)


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
            "  or, for a frontend change:\n"
            "    Proves: frontend/(src|tests)/<path>/<name>.spec.ts::<test title>\n"
            "  Name the test that would FAIL without this change (docs/product-map/README.md documents the convention).",
            file=sys.stderr,
        )
        return 1

    problems = [problem for problem in (verify_ref(ref, repo_root, changed_files) for ref in refs) if problem is not None]
    if problems:
        print(
            "prove-the-fix: FAILED - the PR body names a proving test that does not hold up:\n" + "\n".join(f"  {problem}" for problem in problems),
            file=sys.stderr,
        )
        return 1

    print(f"prove-the-fix: OK - {len(refs)} proving test(s) named, present and changed: {', '.join(refs)}")
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Require a Proves: marker naming an existing, changed test when product source changes.")
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
