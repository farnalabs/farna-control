"""Unit tests for scripts/run_transport_strict.py (FAR-2702).

The strict-transport runner is the CI gate that turns a respx finding into a
non-zero exit. These tests drive its exit-code contract WITHOUT running pytest:
``_run_pytest`` is replaced with a fake that writes (or deliberately does not
write) the report the real instrumentation would produce.

Covered contract:

* a report whose only findings are already baselined exits 0;
* a genuine pytest failure (rc outside ``{0, 5}``) exits 1;
* an unbaselined finding exits 1;
* an unusable base ref exits 2 (fail closed, never a silent no-op);
* selected modules that produce no report exit 2;
* an explicitly requested module that does not exist exits 2.
"""

from __future__ import annotations

import json
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

import pytest

for _parent in Path(__file__).resolve().parents:
    _script = _parent / "scripts" / "run_transport_strict.py"
    if _script.exists():
        break
else:  # pragma: no cover - the script always exists in the repo
    raise RuntimeError("could not find the repo root (scripts/run_transport_strict.py)")

_REPO = _script.parent.parent
_SCRIPT = _script

_spec = spec_from_file_location("run_transport_strict", _SCRIPT)
if _spec is None or _spec.loader is None:  # pragma: no cover - the script always exists
    raise RuntimeError("could not load scripts/run_transport_strict.py")
mod: ModuleType = module_from_spec(_spec)
sys.modules[_spec.name or "run_transport_strict"] = mod
_spec.loader.exec_module(mod)

#: A real, collectable module inside the repo. The fake runner never executes it.
_MODULE = _REPO / "backend" / "tests" / "architecture" / "test_transport_policy.py"


def _write_baseline(path: Path, violations: dict[str, list[dict[str, str]]]) -> None:
    path.write_text(
        json.dumps({"version": 2, "modules": ["tests/architecture/test_transport_policy.py"], "violations": violations}),
        encoding="utf-8",
    )


def _write_report(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry, sort_keys=True) + "\n" for entry in entries), encoding="utf-8")


def _finding(node: str, detail: str) -> dict[str, str]:
    return {"test": node, "kind": "unused_route", "detail": detail, "fingerprint": mod._fingerprint(detail)}


def test_baselined_only_report_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = "tests/architecture/test_transport_policy.py::test_sample"
    detail = "GET https://example.test/api/v1/known"
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.jsonl"
    _write_baseline(baseline, {node: [{"kind": "unused_route", "fingerprint": mod._fingerprint(detail)}]})

    def fake_run(modules, report_path, timeout, jobs, baseline_path):
        _write_report(report_path, [_finding(node, detail)])
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    rc = mod.main(["--baseline", str(baseline), "--report", str(report), str(_MODULE)])

    assert rc == 0


def test_unbaselined_report_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = "tests/architecture/test_transport_policy.py::test_sample"
    detail = "GET https://example.test/api/v1/new"
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.jsonl"
    _write_baseline(baseline, {})

    def fake_run(modules, report_path, timeout, jobs, baseline_path):
        _write_report(report_path, [_finding(node, detail)])
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    rc = mod.main(["--baseline", str(baseline), "--report", str(report), str(_MODULE)])

    assert rc == 1


def test_real_assertion_failure_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.jsonl"
    _write_baseline(baseline, {})

    def fake_run(modules, report_path, timeout, jobs, baseline_path):
        _write_report(report_path, [])
        # 1 is pytest's "a test failed". Baselined findings never fail a test,
        # so this can only be a genuine failure and must not be swallowed.
        return 1

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    rc = mod.main(["--baseline", str(baseline), "--report", str(report), str(_MODULE)])

    assert rc == 1


def test_unresolvable_base_ref_exits_two(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, {})

    rc = mod.main(["--baseline", str(baseline), "--base-ref", "definitely-not-a-real-revision"])

    assert rc == 2


def test_missing_report_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.jsonl"
    _write_baseline(baseline, {})

    def fake_run(modules, report_path, timeout, jobs, baseline_path):
        # The instrumentation never ran: no main report and no shard.
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    rc = mod.main(["--baseline", str(baseline), "--report", str(report), str(_MODULE)])

    assert rc == 2


def test_explicit_missing_module_exits_two(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    _write_baseline(baseline, {})

    rc = mod.main(["--baseline", str(baseline), "backend/tests/does_not_exist.py"])

    assert rc == 2


def test_report_shard_is_read_when_the_main_file_is_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """xdist workers write shards; an empty main file must not hide them."""
    node = "tests/architecture/test_transport_policy.py::test_sample"
    detail = "GET https://example.test/api/v1/known"
    baseline = tmp_path / "baseline.json"
    report = tmp_path / "report.jsonl"
    _write_baseline(baseline, {node: [{"kind": "unused_route", "fingerprint": mod._fingerprint(detail)}]})

    def fake_run(modules, report_path, timeout, jobs, baseline_path):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("", encoding="utf-8")
        report_path.with_name(f"{report_path.name}.{os.getpid()}.jsonl").write_text(json.dumps(_finding(node, detail)) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    assert mod.main(["--baseline", str(baseline), "--report", str(report), str(_MODULE)]) == 0
