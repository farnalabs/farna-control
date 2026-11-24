"""Unit tests for scripts/provider_contract_probe.py (FAR-2703).

Uses respx (the house HTTP mock) and a temp directory for goldens. No network.
Proves the behaviours that make a nightly live probe safe:

* a mutated golden fails the check;
* a changed response shape is reported, never auto-accepted (the golden is
  not rewritten by check mode);
* a secret never reaches a golden, the report, or stdout/stderr;
* a provider with a missing credential is skipped loudly, not silently passed;
* record mode is the only writer of a golden and check mode is read-only;
* a config-declared normalisation rule absorbs a volatile field.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import httpx
import respx
import yaml

for _parent in Path(__file__).resolve().parents:
    _script_path = _parent / "scripts" / "provider_contract_probe.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/provider_contract_probe.py)")

_loader = SourceFileLoader("provider_contract_probe", str(_script_path))
_module = module_from_spec(spec_from_loader("provider_contract_probe", _loader))
sys.modules["provider_contract_probe"] = _module
_loader.exec_module(_module)
probe = _module

_URL = "https://api.acme.test/ping"


def _probe(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "id": "acme",
        "kind": "connector",
        "operation": "ping",
        "description": "test provider",
        "request": {"method": "GET", "url": _URL},
        "auth": {"kind": "bearer", "env": "ACME_TOKEN"},
    }
    definition.update(overrides)
    return definition


def _write_config(tmp_path: Path, providers: list[dict[str, Any]], skips: list[dict[str, Any]] | None = None) -> Path:
    config = {
        "version": 1,
        "defaults": {"success_status_class": "2xx", "timeout_seconds": 5, "max_depth": 1},
        "providers": providers,
        "skips": skips or [],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _write_goldens(tmp_path: Path, shape: dict[str, Any], max_depth: int = 1) -> Path:
    goldens = {
        "schema_version": 1,
        "goldens": {
            "acme/ping": {
                "recorded_at": None,
                "success_status_class": "2xx",
                "max_depth": max_depth,
                "shape": shape,
            }
        },
    }
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps(goldens), encoding="utf-8")
    return path


def _run(config: Path, goldens: Path, report: Path, *extra: str) -> int:
    return probe.main(["--config", str(config), "--goldens", str(goldens), "--report", str(report), *extra])


def _read_report(report: Path) -> dict[str, Any]:
    return json.loads(report.read_text(encoding="utf-8"))


def test_mutated_golden_fails_check(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"beta": 1}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    result = _read_report(report)["results"][0]
    assert result["status"] == "drift"
    assert {"path": "$.alpha", "kind": "missing", "expected": "string", "actual": None} in result["drift"]


def test_changed_shape_is_reported_not_auto_accepted(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"
    before = goldens.read_text(encoding="utf-8")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": 123}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    result = _read_report(report)["results"][0]
    assert result["status"] == "drift"
    assert result["drift"][0]["kind"] == "type_changed"
    assert result["drift"][0]["path"] == "$.alpha"
    # Check mode must never rewrite the golden to "accept" the change.
    assert goldens.read_text(encoding="utf-8") == before


def test_secret_never_appears_in_output(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    secret = "SUPERSECRET-TOKEN-123"
    monkeypatch.setenv("ACME_TOKEN", secret)
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert secret not in report.read_text(encoding="utf-8")
    assert secret not in goldens.read_text(encoding="utf-8")
    # The credential was still sent to the provider.
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {secret}"


def test_missing_credential_is_skipped_loudly(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    assert route.call_count == 0
    result = _read_report(report)["results"][0]
    assert result["status"] == "skipped"
    assert "ACME_TOKEN" in result["reason"]
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "ACME_TOKEN" in captured.err


def test_record_writes_golden_and_check_is_read_only(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = tmp_path / "goldens.json"
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        record_exit = probe.main(
            [
                "--mode",
                "record",
                "--provider",
                "acme",
                "--operation",
                "ping",
                "--config",
                str(config),
                "--goldens",
                str(goldens),
                "--report",
                str(report),
            ]
        )

    assert record_exit == 0
    assert goldens.exists()
    manifest = json.loads(goldens.read_text(encoding="utf-8"))
    assert "acme/ping" in manifest["goldens"]
    recorded = goldens.read_text(encoding="utf-8")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        check_exit = _run(config, goldens, report)

    assert check_exit == 0
    assert _read_report(report)["results"][0]["status"] == "pass"
    assert goldens.read_text(encoding="utf-8") == recorded

    # A check against a missing golden skips; it must not create the file.
    absent = tmp_path / "absent.json"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        absent_exit = _run(config, absent, report)

    assert absent_exit == 0
    assert not absent.exists()


def test_normalisation_absorbs_volatile_field(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    volatile_probe = _probe(normalise=[{"path": "id", "rule": "volatile"}])
    config = _write_config(tmp_path, [volatile_probe])
    goldens = tmp_path / "goldens.json"
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"id": 123}))
        record_exit = probe.main(
            [
                "--mode",
                "record",
                "--provider",
                "acme",
                "--operation",
                "ping",
                "--config",
                str(config),
                "--goldens",
                str(goldens),
                "--report",
                str(report),
            ]
        )

    assert record_exit == 0
    manifest = json.loads(goldens.read_text(encoding="utf-8"))
    # The volatile rule normalises the integer id to a string marker.
    assert manifest["goldens"]["acme/ping"]["shape"]["keys"]["id"] == {"type": "string"}

    # A different value AND a different scalar type is absorbed by the rule.
    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"id": "a-different-id"}))
        check_exit = _run(config, goldens, report)

    assert check_exit == 0
    assert _read_report(report)["results"][0]["status"] == "pass"


def test_report_only_never_fails_on_drift(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"beta": 1}))
        exit_code = _run(config, goldens, report, "--report-only")

    assert exit_code == 0
    assert _read_report(report)["results"][0]["status"] == "drift"
