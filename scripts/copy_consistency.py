#!/usr/bin/env python3
# Vendored from Repos/devtools/harness/tools/copy_consistency.py (canonical). Keep in sync; do not edit the rule logic here without changing the canonical copy.
# copy-consistency: allow-file
"""Check customer-facing copy for defects that have shipped more than once.

Usage:
    python harness/tools/copy_consistency.py --customer-copy <paths...>
    python harness/tools/copy_consistency.py <paths...> [--allow-modulo-run]
    python harness/tools/copy_consistency.py --self-test

Intended invocations:
    website customer copy:  --customer-copy src
    product customer copy:  --customer-copy README.md CONTRIBUTING.md SECURITY.md
    product internal docs:  (no flag) over docs/

A path may be a file or a directory. Directories are scanned recursively for
.md, .vue, .ts, .mjs, .json and .py files; a file named explicitly on the
command line is scanned whatever its suffix.

Skip directory names (node_modules, .git, .venv, build and the rest of
SKIP_DIR_NAMES) are matched against the directories a walk descends into,
never against the scan root's own ancestors, so a worktree under
.agents/worktrees is scanned while a nested node_modules is still pruned. A
directory argument that holds no scannable file is a usage error, never a
clean result.

Exit codes: 0 clean, 1 violations found, 2 usage or IO error (missing path,
empty scan, unreadable path).

Scope is explicit. The flag decides, never the path name.

  Universal rules run always: the dead app.farna.dev host, the retired
  farnalabs/modulo repo URL, the retired modulo.run host (unless
  --allow-modulo-run), and "cd Farna Control". They are wrong in any file,
  internal docs included.

  The retired-host rule reports a HOST, not an identifier that merely contains
  the same text: an email address (@modulo.run) and a label key
  (modulo.run.id, modulo.org.id) are facts, not branding, and are not
  reported.

  Customer-only rules run only with --customer-copy: "opt-in only", a
  milestone label, and the retired product name. Milestone labels and
  "opt-in only" are legitimate in internal docs, so a scan without the flag
  reports only universal defects.

CUSTOMER_COPY_HINT_DIRS and CUSTOMER_COPY_HINT_FILES are a convenience, not a
gate: a scan without the flag that walks a path which looks like customer copy
prints a stderr note naming --customer-copy, so a missing flag is visible
rather than silent. The note never changes findings or the exit code.

Exemptions. Exact identifiers are facts, not branding: a wire header, a
release artefact name and a file path are all allowed to contain the retired
product name. See EXEMPT_EXACT, EXEMPT_PREFIXES and _brand_exempt.

Opting out. A line containing "copy-consistency: allow" is skipped, and a file
whose first FILE_MARKER_WINDOW lines contain "copy-consistency: allow-file" is
skipped as a whole. The tool's own rule table, its fixture corpus and any
document that has to quote a forbidden phrase use these. A skipped file is
named on stderr, so a whole-file opt-out is never silent.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LINE_MARKER = "copy-consistency: allow"
FILE_MARKER = "copy-consistency: allow-file"
FILE_MARKER_WINDOW = 5

SCAN_SUFFIXES = {".md", ".vue", ".ts", ".mjs", ".json", ".py"}
SKIP_DIR_NAMES = {
    ".git",
    ".agents",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
}

# Hints only: they drive the stderr note that names --customer-copy and never
# decide which rules run.
CUSTOMER_COPY_HINT_DIRS = {"docs", "website", "blog", "marketing", "public"}
CUSTOMER_COPY_HINT_FILES = {
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
}

# Exact identifiers that are allowed to contain the retired product name.
EXEMPT_EXACT = frozenset({"X-Modulo-Webhook-Secret", "X-Modulo-Timestamp"})
# Release artefact names: "Modulo-<version>-macos-arm64.dmg" and its lowercase
# sibling. An artefact name is the prefix followed by a version or a file
# extension, so a hyphenated prose compound ("Modulo-based") is still a brand
# use and is still reported.
EXEMPT_PREFIXES = ("Modulo-", "modulo-")

# Angle brackets are part of a token so a documented placeholder artefact name
# (Modulo-<version>-macos-arm64.dmg) stays one identifier instead of breaking
# at the "<" and reading as a bare brand prefix.
_BRAND_CHARS = re.compile(r"[A-Za-z0-9_.\-/<>]")
_HOST_SUFFIX = re.compile(r"^\.(?:run|dev|com|io|ai|org|net)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    """One copy defect: a name, a pattern, and whether copy scope applies."""

    name: str
    pattern: re.Pattern[str]
    customer_copy_only: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        "app-farna-dev",
        re.compile(r"app\.farna\.dev", re.IGNORECASE),
    ),
    Rule(
        "old-repo-url",
        re.compile(
            r"(?:api\.)?github\.com(?:/repos)?/farnalabs/modulo(?![\w-])",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "retired-host",
        # Report the host, not an identifier that contains the same text. An
        # email address (@modulo.run) and a label key (modulo.run.id) are
        # exact identifiers, not a retired hostname. A real host is followed by
        # end of line, whitespace, "/", ":" or punctuation, is not part of a
        # dotted label key, and is never immediately preceded by "@".
        re.compile(r"(?<!@)modulo\.run(?![\w-])(?!\.\w)", re.IGNORECASE),
    ),
    Rule(
        "opt-in-only",
        re.compile(r"opt[-\s]?in\s+only", re.IGNORECASE),
        True,
    ),
    Rule(
        "milestone-label",
        re.compile(r"\b(?:planned for ga|beta scope)\b", re.IGNORECASE),
        True,
    ),
    Rule(
        "cd-product-name",
        re.compile(r"\bcd\s+\"?Farna Control"),
    ),
    Rule(
        "retired-product-name",
        re.compile(r"\bModulo\b"),
        True,
    ),
)

# Rules that --allow-modulo-run silences.
MODULO_RUN_RULES = frozenset({"retired-host"})


@dataclass(frozen=True)
class Violation:
    """One finding: where it is, which rule, and the text that matched."""

    path: str
    line: int
    rule: str
    text: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}: {self.text}"


class EmptyScanError(Exception):
    """A directory argument held no scannable file: a usage error, not a pass."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _looks_like_customer_copy(path: str) -> bool:
    """True when path suggests customer copy. Drives the stderr hint only."""
    parts = PurePosixPath(str(path).replace("\\", "/")).parts
    if not parts:
        return False
    if parts[-1] in CUSTOMER_COPY_HINT_FILES:
        return True
    return any(part in CUSTOMER_COPY_HINT_DIRS for part in parts[:-1])


def _brand_token(line: str, start: int, end: int) -> str:
    """Expand a match to the whole identifier that contains it."""
    left = start
    while left > 0 and _BRAND_CHARS.match(line[left - 1]):
        left -= 1
    right = end
    while right < len(line) and _BRAND_CHARS.match(line[right]):
        right += 1
    return line[left:right]


def _is_artefact(token: str) -> bool:
    """True for a release artefact name: a brand prefix plus a version or ext."""
    for prefix in EXEMPT_PREFIXES:
        if token.startswith(prefix):
            payload = token[len(prefix) :]
            return any(char.isdigit() or char == "." for char in payload)
    return False


def _brand_exempt(token: str, following: str) -> bool:
    """True when the matched token is an exact identifier, not a brand use."""
    if "/" in token:
        # A file path or a URL is a fact; it is not branding.
        return True
    if _HOST_SUFFIX.match(following):
        # The retired-host rule owns the hostname form.
        return True
    if token in EXEMPT_EXACT:
        return True
    return _is_artefact(token)


def _file_marker_present(lines: list[str]) -> bool:
    return any(FILE_MARKER in line for line in lines[:FILE_MARKER_WINDOW])


def scan_text(
    text: str,
    path: str,
    *,
    customer_copy: bool = False,
    allow_modulo_run: bool = False,
) -> list[Violation]:
    """Scan one document's text. path is only reported, never a scope gate."""
    lines = text.splitlines()
    if _file_marker_present(lines):
        return []
    findings: list[Violation] = []
    for number, line in enumerate(lines, 1):
        if LINE_MARKER in line:
            continue
        for rule in RULES:
            if rule.customer_copy_only and not customer_copy:
                continue
            if allow_modulo_run and rule.name in MODULO_RUN_RULES:
                continue
            for match in rule.pattern.finditer(line):
                if rule.name == "retired-product-name":
                    token = _brand_token(line, match.start(), match.end())
                    if _brand_exempt(token, line[match.end() :]):
                        continue
                findings.append(Violation(str(path), number, rule.name, match.group(0)))
    return findings


def _iter_directory(directory: Path) -> list[Path]:
    """Scannable files under directory, pruning skip names BELOW it only.

    Skip names are matched against the directories the walk descends into, so
    a nested node_modules or .git is pruned while a scan root whose own
    ancestors include .agents (every delivery worktree) is still scanned.
    """
    files: list[Path] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIR_NAMES)
        for name in sorted(names):
            candidate = Path(root) / name
            if candidate.suffix.lower() in SCAN_SUFFIXES:
                files.append(candidate)
    return files


def collect(paths: list[str]) -> list[Path]:
    """Resolve the scan set.

    Raises FileNotFoundError for a missing path and EmptyScanError for a
    directory argument that holds no scannable file. An explicitly named file
    is always scanned, whatever its suffix or its ancestors.
    """
    files: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_file():
            files.append(candidate)
        elif candidate.is_dir():
            found = _iter_directory(candidate)
            if not found:
                raise EmptyScanError(raw)
            files.extend(found)
        else:
            raise FileNotFoundError(raw)
    seen: set[Path] = set()
    unique: list[Path] = []
    for file in files:
        resolved = file.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(file)
    return unique


def scan_paths(
    paths: list[str],
    *,
    customer_copy: bool = False,
    allow_modulo_run: bool = False,
    note: bool = False,
) -> tuple[list[Violation], int]:
    """Scan paths. Returns (violations, files scanned)."""
    files = collect(paths)
    if note and not customer_copy:
        hinted = [file for file in files if _looks_like_customer_copy(str(file))]
        if hinted:
            print(
                "note: customer-only rules (opt-in-only, milestone-label, "
                f"retired-product-name) are off for {len(hinted)} scanned "
                f"path(s) that look like customer copy (e.g. {hinted[0]}). "
                "Pass --customer-copy if this scan is customer copy; internal "
                "docs are correct without it.",
                file=sys.stderr,
            )
    findings: list[Violation] = []
    for file in files:
        text = file.read_text(encoding="utf-8", errors="replace")
        if note and _file_marker_present(text.splitlines()):
            print(f"note: skipped by {FILE_MARKER}: {file}", file=sys.stderr)
        findings.extend(
            scan_text(
                text,
                str(file),
                customer_copy=customer_copy,
                allow_modulo_run=allow_modulo_run,
            )
        )
    return findings, len(files)


BAD_FIXTURE = """\
Open app.farna.dev in a browser.
Clone github.com/farnalabs/modulo or api.github.com/repos/farnalabs/modulo.
The old site is modulo.run.
The dashboard is opt-in only.
Dashboards are planned for GA. Retries leave Beta scope later.
Run cd Farna Control before the first task.
Modulo governs the pipeline run.
"""

CLEAN_FIXTURE = """\
Send X-Modulo-Webhook-Secret and X-Modulo-Timestamp with each request.
Download Modulo-1.2.3-macos-arm64.dmg or modulo-1.2.3-linux-x64.tar.gz.
The pinned artefact is Modulo-2.0.0.
The repo is farnalabs/farna-control and the site is farna.dev.
Notes live in Repos/modulo/backend/README.md.
Write to admin@modulo.run or security@modulo.run for access.
Container labels include modulo.run.id, modulo.org.id and modulo.node.id.
"""

MARKED_FIXTURE = """\
Modulo appears here only as a quoted example. <!-- copy-consistency: allow -->
"""


def run_self_test() -> int:
    """Prove every rule fires in its mode and every exemption stays quiet."""
    problems: list[str] = []
    universal = {rule.name for rule in RULES if not rule.customer_copy_only}
    customer_only = {rule.name for rule in RULES if rule.customer_copy_only}
    with tempfile.TemporaryDirectory(prefix="copy-consistency-") as tmp:
        root = Path(tmp)
        docs = root / "docs"
        docs.mkdir()
        bad_path = docs / "bad-copy.md"
        bad_path.write_text(BAD_FIXTURE, encoding="utf-8")
        clean_path = docs / "clean-copy.md"
        clean_path.write_text(CLEAN_FIXTURE, encoding="utf-8")
        marked_path = docs / "marked-copy.md"
        marked_path.write_text(MARKED_FIXTURE, encoding="utf-8")

        # Without --customer-copy only the universal rules run, whatever the
        # path is called. With the flag every rule runs.
        plain_findings, bad_files = scan_paths([str(bad_path)])
        plain_rules = {finding.rule for finding in plain_findings}
        if plain_rules != universal:
            problems.append(
                f"default scan rules: {sorted(plain_rules)} "
                f"(expected {sorted(universal)})"
            )
        if bad_files != 1:
            problems.append(f"bad fixture scan count: {bad_files} (expected 1)")

        copy_findings, _ = scan_paths([str(bad_path)], customer_copy=True)
        copy_rules = {finding.rule for finding in copy_findings}
        if copy_rules != universal | customer_only:
            problems.append(
                f"--customer-copy rules: {sorted(copy_rules)} "
                f"(expected {sorted(universal | customer_only)})"
            )

        # The clean fixture must be clean in the stricter mode too.
        clean_findings, _ = scan_paths([str(clean_path)], customer_copy=True)
        for finding in clean_findings:
            problems.append(f"exempt string reported: {finding.render()}")

        marked_findings, _ = scan_paths([str(marked_path)], customer_copy=True)
        for finding in marked_findings:
            problems.append(f"line marker ignored: {finding.render()}")

        # --allow-modulo-run removes exactly one rule and nothing else.
        allowed_findings, _ = scan_paths(
            [str(bad_path)], customer_copy=True, allow_modulo_run=True
        )
        allowed_rules = {finding.rule for finding in allowed_findings}
        for name in sorted(MODULO_RUN_RULES & allowed_rules):
            problems.append(f"--allow-modulo-run did not suppress: {name}")
        for name in sorted(
            (universal | customer_only) - MODULO_RUN_RULES - allowed_rules
        ):
            problems.append(f"--allow-modulo-run suppressed too much: {name}")

        # The flag, not the path name, decides: the same file is scanned in
        # both modes and only the mode changes the rule set.
        directory_findings, directory_files = scan_paths(
            [str(docs)], customer_copy=True
        )
        if directory_files != 3:
            problems.append(
                f"directory scan found {directory_files} files (expected 3)"
            )
        if {
            finding.rule for finding in directory_findings
        } != universal | customer_only:
            problems.append("directory scan did not report every rule")

        # A nested skip directory must be pruned, not counted.
        nested_skip = docs / "node_modules" / "pkg"
        nested_skip.mkdir(parents=True)
        (nested_skip / "copy.md").write_text(BAD_FIXTURE, encoding="utf-8")
        pruned_findings, pruned_files = scan_paths([str(docs)], customer_copy=True)
        if pruned_files != 3:
            problems.append(
                f"nested skip directory was scanned: {pruned_files} files (expected 3)"
            )
        for finding in pruned_findings:
            if "node_modules" in finding.path:
                problems.append(f"nested skip directory reported: {finding.render()}")

        # A scan root under .agents/worktrees must still be scanned: every
        # delivery worktree lives there and a no-op scan must never read clean.
        worktree_src = root / ".agents" / "worktrees" / "demo" / "src"
        worktree_src.mkdir(parents=True)
        (worktree_src / "copy.md").write_text(BAD_FIXTURE, encoding="utf-8")
        worktree_findings, worktree_files = scan_paths([str(worktree_src)])
        if worktree_files != 1:
            problems.append(
                f"worktree path scan found {worktree_files} files (expected 1)"
            )
        if {finding.rule for finding in worktree_findings} != universal:
            problems.append(
                "worktree path default rules: "
                f"{sorted({f.rule for f in worktree_findings})} "
                f"(expected {sorted(universal)})"
            )
        worktree_copy_findings, _ = scan_paths([str(worktree_src)], customer_copy=True)
        if {finding.rule for finding in worktree_copy_findings} != (
            universal | customer_only
        ):
            problems.append("worktree path ignored --customer-copy")

        # A directory with nothing scannable is a usage error, never a pass.
        empty_dir = root / "empty"
        empty_dir.mkdir()
        try:
            scan_paths([str(empty_dir)])
        except EmptyScanError:
            pass
        else:
            problems.append("empty directory scanned without EmptyScanError")

        suffixes_only = root / "binaries"
        suffixes_only.mkdir()
        (suffixes_only / "logo.png").write_bytes(b"\x89PNG\r\n")
        try:
            scan_paths([str(suffixes_only)])
        except EmptyScanError:
            pass
        else:
            problems.append("unsupported-suffix directory scanned without error")

    if problems:
        for problem in problems:
            print(f"self-test FAIL: {problem}")
        return 1
    print(
        f"self-test OK: {len(universal)} universal + {len(customer_only)} "
        f"customer-only rules fired in their modes ({len(RULES)} total), "
        "clean fixture clean, exemptions quiet, markers honoured, "
        "worktree paths scanned, empty directories refused"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report copy defects in customer-facing docs and copy. "
            "Exit 0 clean, 1 on a violation, 2 on a usage or IO error."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan (.md, .vue, .ts, .mjs, .json, .py)",
    )
    parser.add_argument(
        "--customer-copy",
        action="store_true",
        help=(
            "Treat the scan as customer copy: also report 'opt-in only', "
            "milestone labels and the retired product name"
        ),
    )
    parser.add_argument(
        "--allow-modulo-run",
        action="store_true",
        help="Do not report the retired modulo.run host (redirect notes)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in fixture self-test and exit",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_test()
    if not args.paths:
        parser.error("at least one path is required unless --self-test is given")

    try:
        findings, scanned = scan_paths(
            args.paths,
            customer_copy=args.customer_copy,
            allow_modulo_run=args.allow_modulo_run,
            note=True,
        )
    except FileNotFoundError as exc:
        print(f"copy_consistency: no such file or directory: {exc}", file=sys.stderr)
        return 2
    except EmptyScanError as exc:
        print(f"copy_consistency: no scannable files under {exc.path}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"copy_consistency: cannot read path: {exc}", file=sys.stderr)
        return 2

    for finding in findings:
        print(finding.render())
    print(f"{len(findings)} violations in {scanned} files")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
