#!/usr/bin/env python3
"""Require a proving test for a PR that changes product code (FAR-2701).

A PR that touches ``backend/src/**`` or ``frontend/src/**`` must name at least
one test that proves the change, in its body::

    Proves: backend/tests/unit/api/test_health.py::test_liveness_ok
    Proves: frontend/src/__tests__/Badge.spec.ts::defaults to the neutral variant

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

A frontend title declared with a template literal (``test(`authenticated ${path}
has ...`)``) is captured by the extractor verbatim, placeholders and all, but a
CI run shows the substituted title. The gate accepts both forms: a ``${...}``
placeholder in the DECLARED title is a wildcard, so the verbatim template and the
filled-in title both resolve. Nothing else is a wildcard, and the submitted ref is
always matched literally.

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
import difflib
import re
import sys
import tempfile
from collections.abc import Iterator
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

#: A ``describe`` block that is disabled as a whole. Vitest/Playwright skip every
#: test inside it, so a test call inside one proves nothing even though the call
#: itself carries no ``.skip`` modifier.
_DESCRIBE_SKIP_RE = re.compile(r"\bdescribe\s*\.\s*(?:skip|skipIf)\s*\(")

#: A test-call modifier that tells Vitest/Playwright not to run the test. The
#: prove-the-fix gate and the product-map binding resolver share this so neither
#: can treat a skipped/fixme/todo test as proof.
_SPEC_SKIP_MODIFIER_RE = re.compile(r"\.(?:skip|fixme|todo)\b")

#: Fenced markdown code blocks and HTML comments: documenting the convention
#: must not satisfy it.
_FENCE_RE = re.compile(r"(?P<fence>```|~~~).*?(?P=fence)", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: Decorators and body openers that mean "this test does not run (or is expected
#: to fail)", so it cannot prove anything. Covers the pytest spelling AND the
#: ``unittest`` spelling (``@unittest.skip`` / ``skipIf`` / ``skipUnless`` /
#: ``expectedFailure``), which the class-based suites use and which the
#: pytest-only pattern silently missed. ``expectedFailure`` passes whether or not
#: the fix exists, so it is exactly as vacuous as ``xfail``.
_SKIP_MARKER_RE = re.compile(r"^(?:pytest\.mark\.(?:skip|skipif|xfail)|unittest\.skip(?:If|Unless)?|unittest\.expectedFailure)\b")
_PYTEST_SKIP = "pytest.skip"
_SKIP_BODY_CALLS = frozenset({_PYTEST_SKIP, "pytest.xfail"})

#: Calls made at module scope that skip collection of the whole module, so every
#: test in it is vacuous however clean its own decorators and body are.
_MODULE_SKIP_CALLS = frozenset({_PYTEST_SKIP, "pytest.importorskip"})


#: Allow-list for a caller-supplied input path.  The value is derived through
#: ``fullmatch(...).group(0)`` and then resolved, so the taint analyser sees a
#: string bounded to safe path characters before it reaches the filesystem
#: (pythonsecurity:S8707).
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9._/\\:-]+", re.ASCII)


def _allowed_read_roots(repo_root: Path) -> tuple[Path, ...]:
    """The only roots the gate may read its two inputs from.

    The workflow writes the PR body and the changed-file list into the system
    temp directory (``/tmp`` in CI) and runs the gate from the checkout, so those
    two roots are the whole legitimate surface.
    """
    return (Path(tempfile.gettempdir()).resolve(), repo_root.resolve())


def _sanitize_read_path(value: str, name: str, repo_root: Path) -> Path:
    """Bound a CLI-supplied input path and confine it to an allowed root.

    Rejects disallowed characters, a leading ``-``, and any ``.``/``..`` segment,
    then resolves the path and requires it to sit under the repo or the temp
    directory before it is opened (pythonsecurity:S8707).
    """
    matched = _PATH_CHARS_RE.fullmatch(value)
    if matched is None:
        raise ValueError(f"invalid {name}: {value!r} is not a plain file path")
    safe = matched.group(0)
    if safe.startswith("-") or any(part in {".", ".."} for part in re.split(r"[\\/]", safe)):
        raise ValueError(f"invalid {name}: {value!r} contains a traversal segment or a leading '-'")
    resolved = Path(safe).resolve()
    if not any(resolved.is_relative_to(root) for root in _allowed_read_roots(repo_root)):
        raise ValueError(f"invalid {name}: {value!r} resolves outside the repository and the temp directory")
    return resolved


def _read_text(path: Path, name: str, repo_root: Path) -> str:
    """Read an input file after confining its path (see :func:`_sanitize_read_path`)."""
    return _sanitize_read_path(str(path), name, repo_root).read_text(encoding="utf-8")


def _read_lines(path: Path, repo_root: Path) -> list[str]:
    try:
        text = _read_text(path, "changed-files", repo_root)
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
    names several references, so the failure message can point at the line
    instead of claiming the marker is absent. Fenced blocks and HTML comments are
    stripped first (see :func:`strip_documentation`).

    An EMPTY marker is treated as absent, not as malformed: the shipped
    ``.github/PULL_REQUEST_TEMPLATE.md`` carries a bare ``Proves:`` placeholder,
    and reading that as malformed failed every product PR that followed the
    template - even one that also filled in a valid marker. A body whose only
    marker is empty is caught by the "no proving test" path in :func:`evaluate`,
    which names the format to add.
    """
    refs: list[str] = []
    malformed: list[str] = []
    for line in strip_documentation(pr_body).splitlines():
        match = _MARKER_RE.match(line)
        if match is None:
            continue
        ref = match.group("rest").strip().strip("`").strip()
        if not ref:
            continue
        if "," in ref:
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


def _is_named_function(node: ast.AST, name: str) -> bool:
    """True when *node* is a (sync or async) function called *name*."""
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name


def _find_test(tree: ast.Module, name: str, owner: str | None) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None] | None:
    """The test function *name* (optionally inside class *owner*), and its class.

    The enclosing class is returned too: a ``@pytest.mark.skip`` on the class
    skips every method in it, so the caller must see the class decorators.
    """
    if owner is None:
        # A bare node id (``module.py::test_name``) only names a module-level
        # function. A class member requires its owner (``::Class::test_name``),
        # exactly as pytest collects it - otherwise a node id pytest cannot
        # collect would resolve as proof.
        for node in tree.body:
            if _is_named_function(node, name):
                return node, None
        return None
    owner_class = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == owner), None)
    if owner_class is None:
        return None
    for child in owner_class.body:
        if _is_named_function(child, name):
            return child, owner_class
    return None


def _skip_marker(node: ast.AST) -> str | None:
    """The unparsed text of a pytest skip/skipif/xfail marker, else ``None``."""
    try:
        text = ast.unparse(node)
    except (ValueError, RecursionError):  # pragma: no cover - defensive
        return None
    return text if _SKIP_MARKER_RE.match(text) else None


def _pytestmark_markers(body: list[ast.stmt]) -> list[ast.AST]:
    """The marker expressions assigned to a ``pytestmark`` in *body*.

    ``pytestmark`` applies to every test in the scope it is assigned in: at
    module level it skips (or xfails) the whole module, and inside a class body it
    skips every method of that class. Both spellings matter - a class-level
    ``pytestmark`` was where a skipped test slipped through. It may be a single
    marker or a list of markers.
    """
    for node in body:
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or not any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
            continue
        if isinstance(value, (ast.List, ast.Tuple)):
            return list(value.elts)
        return [value]
    return []


def _has_true_keyword(call: ast.Call, name: str) -> bool:
    """True when *call* passes ``<name>=True`` as a keyword."""
    return any(keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in call.keywords)


def _call_expression_value(node: ast.stmt) -> ast.expr | None:
    """The expression a top-level ``Expr``/``Assign``/``AnnAssign`` evaluates."""
    if isinstance(node, ast.Expr):
        return node.value
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _module_level_skip(tree: ast.Module) -> str | None:
    """Why importing the module skips every test in it, or ``None``.

    A ``pytest.skip(..., allow_module_level=True)`` or a ``pytest.importorskip()``
    executed at module scope aborts collection of the whole file, so every test in
    it is reported skipped regardless of its own decorators and body. Plain
    ``pytest.skip()`` is only a collection skip when ``allow_module_level=True``
    is set (bare, it is an error, not a skip).
    """
    for node in tree.body:
        value = _call_expression_value(node)
        if not isinstance(value, ast.Call):
            continue
        try:
            callee = ast.unparse(value.func)
        except (ValueError, RecursionError):  # pragma: no cover - defensive
            continue
        if callee not in _MODULE_SKIP_CALLS:
            continue
        if callee == "pytest.skip" and not _has_true_keyword(value, "allow_module_level"):
            continue
        return f"its module calls {callee}() at import time, which skips every test in it"
    return None


def _first_skip_marker(decorators: list[ast.expr]) -> str | None:
    """The decorator name of the first skip-ish decorator, else ``None``."""
    for decorator in decorators:
        text = _skip_marker(decorator)
        if text is not None:
            return text.split("(")[0]
    return None


def _pytestmark_skip(body: list[ast.stmt]) -> str | None:
    """The marker name of the first skip-ish ``pytestmark`` in *body*, else ``None``."""
    for marker in _pytestmark_markers(body):
        text = _skip_marker(marker)
        if text is not None:
            return text.split("(")[0]
    return None


def _body_opening_skip(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """Why *fn*'s body opens with a skip/xfail call, or ``None``."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body or not isinstance(body[0], ast.Expr) or not isinstance(body[0].value, ast.Call):
        return None
    try:
        callee = ast.unparse(body[0].value.func)
    except (ValueError, RecursionError):  # pragma: no cover - defensive
        return None
    if callee in _SKIP_BODY_CALLS:
        return f"its body starts with {callee}()"
    return None


def _skip_reason(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    owner_class: ast.ClassDef | None,
    tree: ast.Module,
) -> str | None:
    """Why *fn* cannot prove anything (skipped/xfail), or ``None``.

    Every scope pytest lets a skip attach to is inspected: the function's own
    decorators (pytest OR unittest spelling), the enclosing class's decorators,
    a ``pytestmark`` in the class body, the module-level ``pytestmark``
    assignment, a module-level ``pytest.skip(allow_module_level=True)`` or
    ``pytest.importorskip()`` that aborts collection, and a
    ``pytest.skip()``/``xfail()`` opening the body.
    """
    decorated = _first_skip_marker(fn.decorator_list)
    if decorated is not None:
        return f"it is decorated with @{decorated}"
    if owner_class is not None:
        class_decorated = _first_skip_marker(owner_class.decorator_list)
        if class_decorated is not None:
            return f"its class is decorated with @{class_decorated}"
        class_mark = _pytestmark_skip(owner_class.body)
        if class_mark is not None:
            return f"its class sets pytestmark = {class_mark}"
    module_mark = _pytestmark_skip(tree.body)
    if module_mark is not None:
        return f"its module sets pytestmark = {module_mark}"
    module_skip = _module_level_skip(tree)
    if module_skip is not None:
        return module_skip
    return _body_opening_skip(fn)


def _verify_pytest_ref(ref: str, match: re.Match[str], module: Path) -> str | None:
    try:
        source = module.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module))
    except (OSError, SyntaxError) as exc:
        return f"{ref!r} names a test module that cannot be parsed: {exc}"
    fn = _find_test(tree, match.group("name"), match.group("owner"))
    if fn is None:
        owner = match.group("owner")
        where = f"class {owner!r} in " if owner else ""
        return f"{ref!r} names a test that is not defined in {where}{match.group('file')}"
    test_function, owner_class = fn
    reason = _skip_reason(test_function, owner_class, tree)
    if reason is not None:
        return f"{ref!r} names a test that cannot prove anything: {reason}"
    return None


def _skip_comment(source: str, index: int) -> int | None:
    """Index just past a ``//`` or ``/* */`` comment at *index*, else ``None``."""
    if source.startswith("//", index):
        newline = source.find("\n", index)
        return len(source) if newline == -1 else newline
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        return len(source) if end == -1 else end + 1
    return None


def _consume_quoted(source: str, index: int, quote: str) -> tuple[int, str | None]:
    """Advance one step through a quoted string, honouring backslash escapes."""
    char = source[index]
    if char == "\\":
        return index + 2, quote
    if char == quote:
        return index + 1, None
    return index + 1, quote


def _paren_step(source: str, index: int, quote: str | None) -> tuple[int, str | None, int]:
    """Consume one character; return ``(next_index, quote, depth_delta)``."""
    char = source[index]
    if quote is not None:
        next_index, next_quote = _consume_quoted(source, index, quote)
        return next_index, next_quote, 0
    if char in "'\"`":
        return index + 1, char, 0
    comment_end = _skip_comment(source, index)
    if comment_end is not None:
        return comment_end, None, 0
    if char == "(":
        return index + 1, None, 1
    if char == ")":
        return index + 1, None, -1
    return index + 1, None, 0


def _matching_paren(source: str, open_index: int) -> int | None:
    """Index of the ``)`` closing the ``(`` at *open_index*.

    Quote- and comment-aware: a ``)`` inside a string, a ``//`` comment or a
    ``/* */`` block comment must not close the call early (an apostrophe in a
    comment is the common case in these specs).
    """
    depth = 0
    index = open_index
    quote: str | None = None
    while index < len(source):
        index, quote, delta = _paren_step(source, index, quote)
        depth += delta
        if delta == -1 and depth == 0:
            return index - 1
    return None


def _skipped_describe_ranges(source: str) -> list[tuple[int, int]]:
    """Character ranges spanned by a ``describe.skip(...)`` call's arguments.

    A test nested anywhere inside a disabled ``describe`` block never runs, even
    when its own call is a plain ``it(...)``. The block is delimited by matching
    the wrapper call's parentheses so only its real body counts.
    """
    ranges: list[tuple[int, int]] = []
    for match in _DESCRIBE_SKIP_RE.finditer(source):
        open_index = source.find("(", match.start())
        if open_index == -1:
            continue
        close_index = _matching_paren(source, open_index)
        if close_index is not None:
            ranges.append((open_index, close_index))
    return ranges


def iter_spec_test_calls(source: str) -> Iterator[tuple[str, str | None]]:
    """Yield ``(title, skip_reason)`` for every spec test call in *source*.

    ``describe(...)`` calls are suite titles, not tests, so they are skipped
    entirely. ``skip_reason`` is ``None`` when the test actually runs, otherwise a
    short description of why it cannot prove anything. The prove-the-fix gate and
    the product-map binding resolver both consume this so a skipped/fixme/todo
    test, or a test inside a ``describe.skip`` block, can never resolve as proof.
    """
    skipped_ranges = _skipped_describe_ranges(source)
    for call in _TS_TEST_CALL_RE.finditer(source):
        text = call.group(0)
        prefix = text[: text.index(call.group("quote"))]
        if "describe" in prefix:
            continue
        reason: str | None = None
        if _SPEC_SKIP_MODIFIER_RE.search(prefix):
            reason = "it is marked skipped/fixme/todo"
        elif any(start <= call.start() <= end for start, end in skipped_ranges):
            reason = "it sits inside a describe.skip block"
        yield call.group("name"), reason


def runnable_spec_titles(source: str) -> set[str]:
    """Titles of spec tests that can actually run (never skipped/describe titles)."""
    return {title for title, reason in iter_spec_test_calls(source) if reason is None}


#: A template-literal placeholder in a DECLARED spec test title, e.g.
#: ``authenticated ${path} has no unexpected WCAG AA violations``. Only
#: placeholders on the declared side are wildcards; the submitted ref is matched
#: literally everywhere else.
_SPEC_TITLE_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")


def _declared_title_pattern(title: str) -> re.Pattern[str]:
    """Compile a declared spec title to a regex, its ``${...}`` placeholders as ``.*``.

    Every other character is escaped, so a declared title holding parentheses, a
    slash or a dot still matches only itself. The wildcard lives on the declared
    side only: the submitted ref never turns into a pattern.
    """
    parts: list[str] = []
    cursor = 0
    for placeholder in _SPEC_TITLE_PLACEHOLDER_RE.finditer(title):
        parts.append(re.escape(title[cursor : placeholder.start()]))
        parts.append(".*")
        cursor = placeholder.end()
    parts.append(re.escape(title[cursor:]))
    return re.compile("".join(parts))


def _title_matches(declared: str, ref_title: str) -> bool:
    """True when *ref_title* names the declared *title* under either accepted form.

    A declared title with no ``${...}`` placeholder matches only verbatim. A
    parameterised one also matches with its placeholders filled in - the title an
    author copies from a CI run.
    """
    if declared == ref_title:
        return True
    if "${" not in declared:
        return False
    return _declared_title_pattern(declared).fullmatch(ref_title) is not None


def _nearest_declared_titles(ref_title: str, declared_titles: list[str], limit: int = 3) -> list[str]:
    """Up to *limit* declared titles close to *ref_title* (stdlib ``difflib``)."""
    return difflib.get_close_matches(ref_title, declared_titles, n=limit)


def _undefined_spec_title_message(ref: str, ref_title: str, calls: list[tuple[str, str | None]], rel: str) -> str:
    """The "not defined" error, plus up to three near matches to copy from."""
    message = f"{ref!r} names a test title that is not defined in {rel}"
    declared_titles = list(dict.fromkeys(title for title, _reason in calls))
    nearest = _nearest_declared_titles(ref_title, declared_titles)
    if not nearest:
        return message
    return f"{message}. Did you mean: {'; '.join(f'{title!r}' for title in nearest)}"


def _verify_spec_ref(ref: str, match: re.Match[str], module: Path) -> str | None:
    try:
        source = module.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{ref!r} names a spec that cannot be read: {exc}"
    name = match.group("name").strip()
    calls = list(iter_spec_test_calls(source))
    matched = [(title, reason) for title, reason in calls if _title_matches(title, name)]
    if not matched:
        return _undefined_spec_title_message(ref, name, calls, match.group("file"))
    if any(reason is None for _title, reason in matched):
        return None
    return f"{ref!r} names a spec test that cannot prove anything: {matched[0][1]}"


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

    root = Path(args.repo_root).resolve() if args.repo_root else _repo_root()
    body_path = Path(args.pr_body)
    try:
        body = _read_text(body_path, "pr-body", root)
    except OSError as exc:
        print(f"prove-the-fix: FAILED - cannot read the PR body file {body_path}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"prove-the-fix: FAILED - {exc}", file=sys.stderr)
        return 2
    changed = _read_lines(Path(args.changed_files), root)
    return evaluate(body, changed, root)


if __name__ == "__main__":
    sys.exit(main())
