#!/usr/bin/env python3
"""Run the strict respx transport policy and compare against the baseline (FAR-2702).

Runs a selected set of backend test modules with ``FARNA_STRICT_TRANSPORT=1``,
parses the violation report the instrumentation writes, and exits non-zero when
a violation appears that the baseline does not already record.

Default selection is the test modules changed versus ``--base-ref`` (CI feeds
the PR base). This is deliberately NOT the full suite: the policy is a ratchet,
and the whole point is to hold the line on the tests a change touches.

Usage (from anywhere; the script runs pytest from ``backend/``)::

    python scripts/run_transport_strict.py                     # changed modules
    python scripts/run_transport_strict.py backend/tests/bdd/steps/test_n8n.py
    python scripts/run_transport_strict.py --all               # every respx module
    python scripts/run_transport_strict.py --all --write-baseline
    python scripts/run_transport_strict.py --all --shrink-baseline

Deterministic and LLM-free: the only inputs are the test files, the report and
the baseline.

The plugin fails a test ONLY for a violation the baseline does not record;
baselined findings are still recorded in the report but never fail the test.
That is what makes pytest's exit code trustworthy: any exit code outside
``{0, 5}`` is a real problem (a new violation, or a genuine assertion failure),
never the ordinary consequence of running a module with known findings.

Exit codes:
    0   no violation outside the baseline, and pytest itself exited 0 or 5
    1   a new violation, or pytest exited with any other non-zero code
        (a real test failure)
    2   the invocation itself was unusable (unresolvable base ref, failed git
        diff, or modules selected but no report produced)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
BASELINE = BACKEND / "tests" / "architecture" / "transport_policy_baseline.json"
ENV_FLAG = "FARNA_STRICT_TRANSPORT"
ENV_REPORT = "FARNA_STRICT_TRANSPORT_REPORT"


def _is_collectable(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _relative(path: Path) -> str:
    """``backend/tests/...`` -> ``tests/...`` (the node-id prefix pytest uses)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(BACKEND).as_posix()
    except ValueError:
        return resolved.as_posix()


class BaseRefError(RuntimeError):
    """The base revision (or the diff against it) is unusable.

    Distinct from "no changed modules": returning ``[]`` for a failed diff made
    the whole policy a silent no-op whenever git or the base ref misbehaved,
    which is exactly the failure mode the policy cannot afford.
    """


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"version": 2, "modules": [], "violations": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_keys(baseline: dict) -> set[tuple[str, str, str]]:
    return {(node, entry["kind"], entry["fingerprint"]) for node, entries in baseline.get("violations", {}).items() for entry in entries}


def _git_diff_changed_modules(base_ref: str) -> list[Path]:
    if not base_ref or base_ref.startswith("-"):
        raise BaseRefError(f"base ref {base_ref!r} is empty or option-like")
    resolved = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if resolved.returncode != 0 or not resolved.stdout.strip():
        raise BaseRefError(f"base ref {base_ref!r} does not resolve to a commit in this repository")
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD", "--", "backend/tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BaseRefError(f"git diff against {base_ref} failed: {result.stderr.strip() or result.stdout.strip()}")
    modules = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or not _is_collectable(line.rsplit("/", 1)[-1]):
            continue
        candidate = REPO_ROOT / line
        if candidate.exists():
            modules.append(candidate)
    return sorted(set(modules))


def _baseline_modules(baseline: dict) -> list[Path]:
    modules = []
    for rel in sorted(baseline.get("modules", [])):
        candidate = BACKEND / rel
        if candidate.exists():
            modules.append(candidate)
    return modules


def _read_report(path: Path) -> set[tuple[str, str, str]]:
    """Merge the JSON-lines report and every per-process shard beside it."""
    found: set[tuple[str, str, str]] = set()
    for shard in [path, *sorted(path.parent.glob(f"{path.name}.*.jsonl"))]:
        if not shard.exists():
            continue
        for raw_line in shard.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            fingerprint = payload.get("fingerprint") or _fingerprint(payload.get("detail", ""))
            found.add((payload["test"], payload["kind"], fingerprint))
    return found


def _fingerprint(detail: str) -> str:
    """Local copy of the instrumentation's fingerprint (keeps this script import-free)."""
    import hashlib
    import re

    normalised = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "<uuid>", detail)
    normalised = re.sub(r"0x[0-9a-fA-F]+", "<addr>", normalised)
    normalised = re.sub(r"\b[0-9a-fA-F]{16,}\b", "<hex>", normalised)
    normalised = re.sub(r"\b\d+\b", "<n>", normalised)
    return hashlib.sha1(" ".join(normalised.split()).encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def _module_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _report_produced(path: Path) -> bool:
    return path.exists() or any(path.parent.glob(f"{path.name}.*.jsonl"))


def _run_pytest(modules: list[Path], report: Path, timeout: int, jobs: int, baseline_path: Path) -> int:
    relative = []
    for module in modules:
        try:
            relative.append(module.resolve().relative_to(BACKEND).as_posix())
        except ValueError:
            relative.append(str(module))
    env = dict(os.environ)
    env[ENV_FLAG] = "1"
    env[ENV_REPORT] = str(report)
    # The in-test hooks must compare against the SAME baseline the runner is
    # about to compare against, or a baselined finding would fail its test and
    # every run over a module with known findings would exit non-zero.
    env["FARNA_STRICT_TRANSPORT_BASELINE"] = str(baseline_path)
    # ``-p no:randomly`` is a no-op when the plugin is absent; it keeps ordering
    # stable when it is present. --jobs > 1 is for local baseline generation
    # only: each xdist worker writes its own report shard, and every finding is
    # attributed per test (the instrumentation resets per test), so the merged
    # set is identical to a serial run.
    command = ["uv", "run", "--no-sync", "pytest", *relative, "-q", "--tb=short", f"--timeout={timeout}", "-p", "no:randomly"]
    if jobs > 1:
        command.extend(["-n", str(jobs)])
    print("+ " + " ".join(command))
    completed = subprocess.run(command, cwd=BACKEND, env=env, check=False)
    return completed.returncode


def _respx_modules() -> list[str]:
    """Every collectable ``tests/test_*.py`` module importing respx (backend-relative)."""
    modules: list[str] = []
    for path in sorted((BACKEND / "tests").rglob("*.py")):
        if not _is_collectable(path.name):
            continue
        raw = path.read_bytes()
        if b"respx" not in raw:
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError):  # pragma: no cover - test tree is valid Python
            continue
        imports_respx = any(
            (isinstance(node, ast.Import) and any(alias.name == "respx" or alias.name.startswith("respx.") for alias in node.names)) or (isinstance(node, ast.ImportFrom) and node.module is not None and (node.module == "respx" or node.module.startswith("respx."))) for node in ast.walk(tree)
        )
        if imports_respx:
            modules.append(path.relative_to(BACKEND).as_posix())
    return modules


def _write_baseline(path: Path, baseline: dict, report: set[tuple[str, str, str]], scanned_modules: set[str], *, replace: bool) -> None:
    """Write the v2 (fingerprinted) baseline for the scanned modules.

    ``report`` is the set of ``(nodeid, kind, fingerprint)`` identities the run
    observed. Every identity is preserved - two dead stubs in one test are two
    entries, which is the whole point of fingerprinting.
    """
    # Refresh the module inventory too: a new respx-using module must be
    # enumerated for the policy scan to cover it, and a module that stopped
    # importing respx must leave (shrink-only).
    baseline["version"] = 2
    baseline["modules"] = _respx_modules()
    # A v1 entry is a bare kind with no fingerprint. It cannot be carried over,
    # but ``--all`` re-scans every module below, so it is re-derived with a
    # fingerprint instead of being lost.
    entries: dict[str, set[tuple[str, str]]] = {node: {(entry["kind"], entry["fingerprint"]) for entry in node_entries if isinstance(entry, dict)} for node, node_entries in baseline.get("violations", {}).items()}
    entries = {node: node_entries for node, node_entries in entries.items() if node_entries}
    if replace:
        for node in list(entries):
            if _module_of(node) in scanned_modules:
                del entries[node]
    for node, kind, fingerprint in report:
        if _module_of(node) in scanned_modules:
            entries.setdefault(node, set()).add((kind, fingerprint))
    known = set(baseline["modules"])
    baseline["violations"] = {node: [{"kind": kind, "fingerprint": fingerprint} for kind, fingerprint in sorted(entries[node])] for node in sorted(entries) if _module_of(node) in known or node.startswith("<")}
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    total = sum(len(entries[node]) for node in baseline["violations"])
    print(f"baseline written: {path} ({len(baseline['modules'])} module(s), {total} violation(s))")


def _resolve_arg(raw: str) -> Path:
    """Resolve a CLI path relative to the repo root or to ``backend/``.

    CI passes ``backend/tests/...`` (repo-relative); a local run from
    ``backend/`` naturally passes ``tests/...``. Accept both rather than
    silently filtering the module out.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    for base in (REPO_ROOT, BACKEND):
        resolved = base / candidate
        if resolved.exists():
            return resolved
    return REPO_ROOT / candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Test modules to run (default: changed vs --base-ref)")
    parser.add_argument("--base-ref", default="origin/main", help="Ref to diff changed test modules against")
    parser.add_argument("--all", action="store_true", help="Run every respx-using module listed in the baseline")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--shrink-baseline", action="store_true", help="Drop baseline entries that no longer reproduce")
    parser.add_argument("--write-baseline", action="store_true", help="Record the report as the baseline for the scanned modules")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--jobs", type=int, default=1, help="xdist workers (local baseline generation only; default serial)")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Keep the violation report here (plus per-process shards) instead of a temp dir; CI points FARNA_STRICT_TRANSPORT_REPORT at it so the architecture ratchet reads the same file",
    )
    args = parser.parse_args(argv)

    baseline = _load_baseline(args.baseline)

    if args.paths:
        modules = [_resolve_arg(p) for p in args.paths]
    elif args.all:
        modules = _baseline_modules(baseline)
    else:
        try:
            modules = _git_diff_changed_modules(args.base_ref)
        except BaseRefError as exc:
            print(f"::error::strict transport policy cannot select changed modules: {exc}. Refusing to pass with an unusable base ref.", file=sys.stderr)
            return 2

    modules = [m for m in modules if m.exists() and _is_collectable(m.name)]
    if not modules:
        # Still create the report when one was requested: CI runs the ratchet
        # architecture test unconditionally against that path, and a missing
        # file must mean "the runner never ran", never "nothing changed".
        if args.report is not None:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text("", encoding="utf-8")
            print(f"report: {args.report} (empty)")
        print("No collectable test modules to check - strict transport policy is a no-op for this change.")
        return 0

    scanned_modules = {_relative(m) for m in modules}
    print(f"Strict transport policy over {len(modules)} module(s):")
    for module in modules:
        print(f"  {_relative(module)}")

    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        for stale_shard in args.report.parent.glob(f"{args.report.name}.*.jsonl"):
            stale_shard.unlink(missing_ok=True)
        report_path = args.report
        pytest_rc = _run_pytest(modules, report_path, args.timeout, args.jobs, args.baseline)
        print(f"report: {report_path}")
        produced = _report_produced(report_path)
        report = _read_report(report_path)
    else:
        with tempfile.TemporaryDirectory(prefix="farna-strict-transport-") as tmp:
            report_path = Path(tmp) / "report.jsonl"
            pytest_rc = _run_pytest(modules, report_path, args.timeout, args.jobs, args.baseline)
            produced = _report_produced(report_path)
            report = _read_report(report_path)

    if not produced:
        # Modules were selected and pytest ran, but the instrumentation wrote
        # nothing - not even the empty file start_session creates. The policy
        # did not actually run; that must never look like a clean pass.
        print(
            f"::error::strict transport policy selected {len(modules)} module(s) but no report was produced (pytest rc={pytest_rc}). The instrumentation did not run.",
            file=sys.stderr,
        )
        return 2

    if args.write_baseline:
        _write_baseline(args.baseline, baseline, report, scanned_modules, replace=True)
        return 0

    baseline_keys = _baseline_keys(baseline)

    new_violations = sorted(report - baseline_keys)

    stale = sorted((node, kind, fingerprint) for node, kind, fingerprint in baseline_keys if _module_of(node) in scanned_modules and (node, kind, fingerprint) not in report)

    for node, kind, fingerprint in stale:
        print(f"  baseline may shrink: {node} [{kind}] {fingerprint} no longer reproduces")

    if args.shrink_baseline:
        remaining = baseline_keys - set(stale)
        by_node: dict[str, set[tuple[str, str]]] = {}
        for node, kind, fingerprint in remaining:
            by_node.setdefault(node, set()).add((kind, fingerprint))
        baseline["version"] = 2
        baseline["violations"] = {node: [{"kind": kind, "fingerprint": fingerprint} for kind, fingerprint in sorted(by_node[node])] for node in sorted(by_node)}
        args.baseline.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline shrunk: {args.baseline} ({sum(len(v) for v in by_node.values())} violation(s))")

    if new_violations:
        print("\nNew strict-transport violations (not in the baseline):")
        for node, kind, fingerprint in new_violations:
            print(f"  {node} [{kind}] {fingerprint}")
        print(
            "\nFix the test (use every registered route, stub every outbound request, send the write headers) or, only for a pre-existing defect outside this change's scope, record it deliberately with --write-baseline and say why in the PR.",
        )
        return 1

    if report:
        print(f"\n{len(report)} violation(s) reproduced, all already in the baseline.")
    else:
        print("\nNo strict-transport violations.")

    if pytest_rc not in (0, 5):
        # Baselined findings no longer fail their tests, so any non-zero code
        # other than "no tests collected" (5) is a genuine test failure. Never
        # infer this from whether the report looks clean: a module with one real
        # assertion failure and no transport finding used to slip through here.
        print(
            f"::error::pytest exited {pytest_rc}; that is outside {{0, 5}} and means a real test failure (a new violation or a failed assertion), not a baselined finding.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
