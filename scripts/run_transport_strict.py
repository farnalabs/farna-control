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

Exit codes:
    0   no violation outside the baseline
    1   a new violation, or a test module failed for a non-transport reason
    2   the invocation itself was unusable (no modules, bad ref)
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


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "modules": [], "violations": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _changed_modules(base_ref: str) -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...HEAD", "--", "backend/tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"::error::git diff against {base_ref} failed: {result.stderr.strip()}", file=sys.stderr)
        return []
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


def _read_report(path: Path) -> dict[str, set[str]]:
    """Merge the JSON-lines report and every per-process shard beside it."""
    found: dict[str, set[str]] = {}
    for shard in [path, *sorted(path.parent.glob(f"{path.name}.*.jsonl"))]:
        if not shard.exists():
            continue
        for raw_line in shard.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            found.setdefault(payload["test"], set()).add(payload["kind"])
    return found


def _module_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def _run_pytest(modules: list[Path], report: Path, timeout: int, jobs: int) -> int:
    relative = []
    for module in modules:
        try:
            relative.append(module.resolve().relative_to(BACKEND).as_posix())
        except ValueError:
            relative.append(str(module))
    env = dict(os.environ)
    env[ENV_FLAG] = "1"
    env[ENV_REPORT] = str(report)
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


def _write_baseline(path: Path, baseline: dict, report: dict[str, set[str]], scanned_modules: set[str], *, replace: bool) -> None:
    # Refresh the module inventory too: a new respx-using module must be
    # enumerated for the policy scan to cover it, and a module that stopped
    # importing respx must leave (shrink-only).
    baseline["modules"] = _respx_modules()
    violations: dict[str, list[str]] = {node: sorted(kinds) for node, kinds in baseline.get("violations", {}).items()}
    if replace:
        for node in list(violations):
            if _module_of(node) in scanned_modules:
                del violations[node]
    for node, kinds in report.items():
        if _module_of(node) in scanned_modules:
            violations[node] = sorted(kinds)
    known = set(baseline["modules"])
    baseline["violations"] = {node: sorted(violations[node]) for node in sorted(violations) if _module_of(node) in known}
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"baseline written: {path} ({len(baseline['modules'])} module(s), {len(baseline['violations'])} violation(s))")


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
        modules = _changed_modules(args.base_ref)

    modules = [m for m in modules if m.exists() and _is_collectable(m.name)]
    if not modules:
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
        pytest_rc = _run_pytest(modules, report_path, args.timeout, args.jobs)
        report = _read_report(report_path)
        print(f"report: {report_path}")
    else:
        with tempfile.TemporaryDirectory(prefix="farna-strict-transport-") as tmp:
            report_path = Path(tmp) / "report.jsonl"
            pytest_rc = _run_pytest(modules, report_path, args.timeout, args.jobs)
            report = _read_report(report_path)

    if args.write_baseline:
        _write_baseline(args.baseline, baseline, report, scanned_modules, replace=True)
        return 0

    baseline_violations: dict[str, set[str]] = {node: set(kinds) for node, kinds in baseline.get("violations", {}).items()}

    new_violations: list[tuple[str, str]] = [(node, kind) for node in sorted(report) for kind in sorted(report[node]) if kind not in baseline_violations.get(node, set())]

    stale: list[tuple[str, str]] = [(node, kind) for node, kinds in sorted(baseline_violations.items()) if _module_of(node) in scanned_modules for kind in sorted(kinds) if kind not in report.get(node, set())]

    for node, kind in stale:
        print(f"  baseline may shrink: {node} [{kind}] no longer reproduces")

    if args.shrink_baseline:
        shrunk = {node: sorted(kind for kind in kinds if (node, kind) not in set(stale)) for node, kinds in baseline_violations.items()}
        baseline["violations"] = {node: sorted(shrunk[node]) for node in sorted(shrunk) if shrunk[node]}
        args.baseline.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline shrunk: {args.baseline} ({len(baseline['violations'])} violation(s))")

    if new_violations:
        print("\nNew strict-transport violations (not in the baseline):")
        for node, kind in new_violations:
            print(f"  {node} [{kind}]")
        print(
            "\nFix the test (use every registered route, stub every outbound request) or, only for a pre-existing defect outside this change's scope, record it deliberately with --write-baseline and say why in the PR.",
        )
        return 1

    if report:
        print(f"\n{sum(len(kinds) for kinds in report.values())} violation(s) reproduced, all already in the baseline.")
    else:
        print("\nNo strict-transport violations.")

    if pytest_rc != 0 and not report:
        # A failure with no transport finding is a genuine test failure; the
        # caller must see it rather than a green "policy clean" line.
        print("::error::pytest failed and reported no transport violation - this is a real test failure.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
