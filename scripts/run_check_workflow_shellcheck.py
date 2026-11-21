#!/usr/bin/env python3
"""Run shellcheck over every ``run:`` block in ``.github/workflows/**`` (FAR-2606).

Why this helper exists
======================

``actionlint`` runs shellcheck itself, one process per ``run:`` block, and that
per-block process spawn is what made the ``actionlint`` CI job unaffordable: the
job crossed its 10-minute ``timeout-minutes`` on every run from 2026-11-20
onward, on ``main`` and on every PR branch, so every PR carried a red check.
``actionlint -shellcheck=`` finishes in about a second; the whole cost was the
shellcheck pass.

The gate must keep proving both things - actionlint's structural findings *and*
shellcheck's SC* findings on the run blocks - so the shellcheck pass moves here,
where it can be done properly: one shellcheck invocation per shell dialect over
every extracted block, instead of one process per block (523 blocks at the time
of writing).

Fidelity
========

This reproduces actionlint's own shellcheck integration rule for rule
(``actionlint`` docs, "shellcheck integration for ``run:``"):

* Only scripts whose resolved shell is ``bash`` or ``sh`` are checked. The shell
  is resolved step ``shell:`` -> job ``defaults.run.shell`` -> workflow
  ``defaults.run.shell`` -> the runner OS default (``bash`` on Linux and macOS,
  ``pwsh`` on Windows). A shell that cannot be resolved statically is assumed to
  be ``bash``, exactly as actionlint assumes.
* ``${{ ... }}`` expressions are replaced with underscores before shellcheck
  sees them, because ``${{`` otherwise reads as a broken shell variable. The
  replacement preserves character columns.
* The same shellcheck argv actionlint builds is used here, captured from the
  pinned actionlint 1.7.12 binary by pointing its ``-shellcheck=`` option at a
  logging wrapper:

      shellcheck --norc -f json -x --shell <dialect> -e <disabled rules> -

  The disabled-rule list is ``SC1091,SC2194,SC2050,SC2153,SC2154,SC2157,SC2043``.
  Note that ``SC2153`` is in the *binary's* list but is absent from the
  ``checks.md`` shipped alongside it, so the list below is taken from the binary,
  not the docs. Disabling exactly what actionlint disables is what keeps a
  previously green tree green: a rule actionlint suppresses must not start
  failing here. ``--format=gcc`` replaces ``-f json`` purely for readable
  output - the format does not change which findings shellcheck reports.

Line numbers
============

Each extracted block is written with leading blank lines so that script line N
reports as the workflow file's real line, then the temp path is rewritten back
to the workflow path. A finding therefore points at the workflow file and line,
which is what a reader of the CI log needs. Folded blocks (``run: >``) fold
physical lines, so their line numbers are approximate - there is one in the
tree.

Failure modes are loud, never silent
====================================

* shellcheck missing -> exit 2. A gate that cannot run must not pass.
* shellcheck exits with anything other than 0 (no findings) or 1 (findings) ->
  exit 2. A tool failure is not a clean tree.
* a workflow file that will not parse -> exit 2.
* zero run blocks extracted -> exit 2. An extractor that found nothing has not
  checked anything.

Usage
=====

    python3 scripts/run_check_workflow_shellcheck.py [--workflows-dir DIR] [--shellcheck PATH]

Exit codes: 0 = clean, 1 = shellcheck findings, 2 = could not run the check.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Rules actionlint disables when it drives shellcheck itself. This is the list
# the pinned actionlint 1.7.12 binary actually passes (`-e`), captured from its
# argv - not the shorter list printed in the docs bundled with it. Adding a rule
# here weakens the gate, so it is the one line to scrutinise in review; removing
# one can turn a currently green tree red.
SHELLCHECK_DISABLED_RULES = ("SC1091", "SC2194", "SC2050", "SC2153", "SC2154", "SC2157", "SC2043")

# Only these dialects are shellcheck-able. Every other shell (pwsh, python, cmd,
# perl) is skipped by actionlint too.
SHELLCHECK_DIALECTS = ("bash", "sh")

# `${{ expr }}` - non-greedy, single line. Replaced with an equal number of
# underscores so columns survive.
_EXPRESSION_RE = re.compile(r"\$\{\{.*?\}\}")

# shellcheck's gcc output is `<file>:<line>:<col>: <severity>: <message>`.
# Greedy `.*` up to `.sh:` keeps a Windows drive-letter colon inside the path.
_GCC_LINE_RE = re.compile(r"^(?P<file>.*\.sh):(?P<line>\d+):(?P<col>\d+): (?P<rest>.*)$")

# Files per shellcheck invocation. 500+ paths in one argv risks the platform
# limit; shellcheck startup is cheap, so a few batches cost nothing.
_BATCH_SIZE = 200

_WINDOWS_LABEL_PREFIXES = ("windows",)
_MACOS_LABEL_PREFIXES = ("macos",)
_LINUX_LABEL_PREFIXES = ("ubuntu", "linux")


@dataclass(frozen=True)
class RunBlock:
    """One ``run:`` block destined for shellcheck."""

    workflow: str  # repo-relative workflow path, e.g. ".github/workflows/ci.yml"
    job: str  # job id
    step: str  # step name, or "steps[i]" when the step is unnamed
    dialect: str  # "bash" or "sh"
    script: str  # script text, expressions already substituted
    first_line: int  # 1-based line in the workflow file of script line 1


def _scalar(node: Any) -> str | None:
    """Return a scalar node's string value, or None for any other node kind."""
    if isinstance(node, yaml.ScalarNode):
        value = node.value
        return value if isinstance(value, str) else None
    return None


def _mapping_value(node: Any, key: str) -> Any:
    """Return the value node for ``key`` in a mapping node, else None.

    PyYAML resolves the YAML 1.1 boolean spellings, so a key written ``on``
    arrives as the boolean ``true``; this only looks up keys this helper asks
    for (``jobs``, ``steps``, ``run``, ``shell``, ``defaults``, ``runs-on``,
    ``name``), all of which are plain strings.
    """
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if _scalar(key_node) == key:
            return value_node
    return None


def _runner_os(runs_on: Any) -> str:
    """Classify a ``runs-on:`` value as windows / macos / linux / unknown."""
    raw: list[str] = []
    if isinstance(runs_on, yaml.ScalarNode):
        text = _scalar(runs_on)
        if text is not None:
            raw.append(text)
    elif isinstance(runs_on, yaml.SequenceNode):
        for item in runs_on.value:
            text = _scalar(item)
            if text is not None:
                raw.append(text)
    # An expression (e.g. `${{ matrix.os }}`) cannot be resolved statically.
    labels = [label.strip().lower() for label in raw if "${{" not in label]
    for prefixes, family in (
        (_WINDOWS_LABEL_PREFIXES, "windows"),
        (_MACOS_LABEL_PREFIXES, "macos"),
        (_LINUX_LABEL_PREFIXES, "linux"),
    ):
        if any(label.startswith(prefixes) for label in labels):
            return family
    return "unknown"


def _default_shell(runner_os: str) -> str:
    """actionlint's default shell for a runner OS (unknown -> bash)."""
    return "pwsh" if runner_os == "windows" else "bash"


def _shell_program(shell: str | None) -> str | None:
    """Extract the program name from a ``shell:`` value.

    Handles ``bash``, ``bash -e {0}``, ``/bin/bash``, ``bash.exe`` and a
    path-qualified Windows bash. Returns None when the value is absent or an
    expression, meaning "assume bash", which is what actionlint does.
    """
    if shell is None:
        return None
    text = shell.strip()
    if not text or "${{" in text:
        return None
    return text.split()[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _resolve_shell(step_shell: str | None, job_shell: str | None, workflow_shell: str | None, runner_os: str) -> str:
    """Resolve the shell program for a step, actionlint-style (unknown -> bash)."""
    for candidate in (step_shell, job_shell, workflow_shell):
        program = _shell_program(candidate)
        if program is not None:
            return program
    return _default_shell(runner_os)


def _defaults_shell(node: Any) -> str | None:
    """Read ``defaults.run.shell`` from a workflow or job mapping node."""
    defaults = _mapping_value(node, "defaults")
    run = _mapping_value(defaults, "run")
    return _scalar(_mapping_value(run, "shell"))


def _collect_blocks(workflow_path: Path, rel_path: str) -> tuple[list[RunBlock], Counter[str]]:
    """Extract every shellcheck-able ``run:`` block from one workflow file."""
    text = workflow_path.read_text(encoding="utf-8")
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        raise ValueError(f"{rel_path}: workflow root is not a mapping")

    workflow_shell = _defaults_shell(root)
    jobs = _mapping_value(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        raise ValueError(f"{rel_path}: no `jobs:` mapping")

    blocks: list[RunBlock] = []
    skipped: Counter[str] = Counter()
    for job_key, job_node in jobs.value:
        job_id = _scalar(job_key) or "<unknown job>"
        steps = _mapping_value(job_node, "steps")
        if not isinstance(steps, yaml.SequenceNode):
            continue
        job_shell = _defaults_shell(job_node)
        runner_os = _runner_os(_mapping_value(job_node, "runs-on"))
        for index, step_node in enumerate(steps.value):
            run_node = _mapping_value(step_node, "run")
            script = _scalar(run_node)
            if script is None:
                continue
            step_name = _scalar(_mapping_value(step_node, "name")) or f"steps[{index}]"
            shell = _resolve_shell(_scalar(_mapping_value(step_node, "shell")), job_shell, workflow_shell, runner_os)
            if shell not in SHELLCHECK_DIALECTS:
                skipped[shell] += 1
                continue
            # A block scalar's mark points at the `run:` key, so script line 1 is
            # the next line; a plain scalar's content is on the same line.
            is_block_scalar = isinstance(run_node, yaml.ScalarNode) and run_node.style in ("|", ">")
            first_line = run_node.start_mark.line + (2 if is_block_scalar else 1)
            blocks.append(
                RunBlock(
                    workflow=rel_path,
                    job=job_id,
                    step=step_name,
                    dialect=shell,
                    script=_EXPRESSION_RE.sub(lambda match: "_" * len(match.group(0)), script),
                    first_line=first_line,
                )
            )
    return blocks, skipped


def _write_block(directory: Path, index: int, block: RunBlock) -> Path:
    """Materialise one block on disk with its workflow line numbers preserved."""
    path = directory / f"{index:04d}-{block.dialect}.sh"
    body = block.script if block.script.endswith("\n") else f"{block.script}\n"
    # Pad so script line N lands on file line N + first_line - 1 and shellcheck's
    # reported line equals the workflow file's line.
    # newline="\n" is load-bearing: the platform default would write CRLF on
    # Windows, and shellcheck reports every line of a CRLF file as SC1017.
    path.write_text("\n" * (block.first_line - 1) + body, encoding="utf-8", newline="\n")
    return path


def run_check(workflows_dir: Path, shellcheck: str) -> int:
    """Extract every run block, shellcheck it, and report. Returns the exit code."""
    workflow_files = sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")])
    if not workflow_files:
        print(f"::error::no workflow files found under {workflows_dir}", file=sys.stderr)
        return 2

    blocks: list[RunBlock] = []
    skipped: Counter[str] = Counter()
    for workflow_path in workflow_files:
        rel_path = workflow_path.as_posix()
        try:
            file_blocks, file_skipped = _collect_blocks(workflow_path, rel_path)
        except (ValueError, yaml.YAMLError) as exc:
            print(f"::error::could not extract run blocks from {rel_path}: {exc}", file=sys.stderr)
            return 2
        blocks.extend(file_blocks)
        skipped.update(file_skipped)

    if not blocks:
        print(
            "::error::extracted no shellcheck-able run blocks - refusing to report a clean gate without checking anything",
            file=sys.stderr,
        )
        return 2

    dialect_counts = Counter(block.dialect for block in blocks)
    with tempfile.TemporaryDirectory(prefix="workflow-shellcheck-") as tmp:
        tmpdir = Path(tmp)
        written: dict[str, RunBlock] = {}
        by_dialect: dict[str, list[str]] = {dialect: [] for dialect in SHELLCHECK_DIALECTS}
        for index, block in enumerate(blocks):
            path = _write_block(tmpdir, index, block)
            written[path.name] = block
            by_dialect[block.dialect].append(str(path))

        findings: list[str] = []
        for dialect, paths in by_dialect.items():
            for start in range(0, len(paths), _BATCH_SIZE):
                batch = paths[start : start + _BATCH_SIZE]
                argv = [
                    shellcheck,
                    # actionlint's own argv, in its own order (see the module
                    # docstring): --norc keeps a stray .shellcheckrc or
                    # SHELLCHECK_OPTS from changing the result, -x follows
                    # source directives.
                    "--norc",
                    "--format=gcc",
                    "-x",
                    f"--shell={dialect}",
                    "-e",
                    ",".join(SHELLCHECK_DISABLED_RULES),
                    *batch,
                ]
                # argv[0] is a PATH-resolved shellcheck executable and every
                # other element is a fixed literal or a temp path this helper
                # created itself; subprocess runs without a shell, so an
                # operator-supplied --shellcheck cannot inject a command. CI
                # invokes the gate with no arguments at all (S8701 false positive).
                completed = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)  # NOSONAR pythonsecurity:S8701
                if completed.returncode not in (0, 1):
                    print(
                        f"::error::shellcheck failed with exit code {completed.returncode} (not a findings exit) - refusing to report a result:\n{completed.stderr}",
                        file=sys.stderr,
                    )
                    return 2
                findings.extend(line for line in completed.stdout.splitlines() if line.strip())

    if skipped:
        summary = ", ".join(f"{shell}={count}" for shell, count in sorted(skipped.items()))
        print(f"skipped run blocks whose shell is not shellcheck-able (actionlint skips these too): {summary}")
    print(f"checked {len(blocks)} run blocks (" + ", ".join(f"{dialect}={count}" for dialect, count in sorted(dialect_counts.items())) + ") with shellcheck")

    if not findings:
        return 0

    for raw_line in findings:
        match = _GCC_LINE_RE.match(raw_line)
        if match is None:
            print(raw_line)
            continue
        block = written.get(Path(match.group("file")).name)
        location = f"{block.workflow}:{match.group('line')}:{match.group('col')}" if block else match.group("file")
        context = f" ({block.job} / {block.step})" if block else ""
        print(f"{location}: {match.group('rest')}{context}")

    print(f"::error::{len(findings)} shellcheck finding(s) in workflow run blocks", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run shellcheck over every run block in .github/workflows/**")
    parser.add_argument("--workflows-dir", default=".github/workflows", help="directory holding workflow YAML files")
    parser.add_argument("--shellcheck", default=None, help="path to the shellcheck executable (default: found on PATH)")
    args = parser.parse_args(argv)

    shellcheck = args.shellcheck or shutil.which("shellcheck")
    if not shellcheck:
        print(
            "::error::shellcheck is not on PATH - without it this gate would silently check nothing, so it refuses to pass. Install shellcheck on the runner image rather than shipping a blind gate.",
            file=sys.stderr,
        )
        return 2

    # shellcheck is a PATH-resolved executable and subprocess runs without a
    # shell, so no command injection is reachable (S8701 false positive).
    version = subprocess.run([shellcheck, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)  # NOSONAR pythonsecurity:S8701
    if version.returncode == 0:
        first_line = (version.stdout or version.stderr).splitlines()[0] if (version.stdout or version.stderr) else "unknown"
        print(f"using {first_line}")

    return run_check(Path(args.workflows_dir), shellcheck)


if __name__ == "__main__":
    sys.exit(main())
