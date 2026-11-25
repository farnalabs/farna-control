"""Unit tests for scripts/provider_contract_probe.py (FAR-2703).

Uses respx (the house HTTP mock) and a temp directory for goldens. No network.
Proves the behaviours that make a nightly live probe safe:

* a mutated golden fails the check;
* a changed response shape is reported, never auto-accepted (the golden is
  not rewritten by check mode);
* check mode shapes the actual response at the depth each golden records, so
  editing the config ``max_depth`` cannot manufacture drift;
* a secret never reaches a golden, the report, or stdout/stderr;
* fail-closed skips: a missing credential, a missing golden and an unrecorded
  golden each fail by default and pass only via an explicit allow list;
* a run that ATTEMPTS zero probes fails, however many skips are excused;
* transient 429/5xx are retried with backoff and end as ``provider_unavailable``,
  never as contract drift;
* an array that silently empties (or a previously empty array that fills) is
  breaking drift, and array element shapes are unioned;
* addition of an object key is breaking by default;
* the loader enforces read-only host/method/env restrictions, and pins every
  provider id to its host, its credential env vars / header / param names, and
  every read-only POST to its exact URL and body - all in code, so a config edit
  cannot redirect a credential, read another provider's secret, or issue a
  write;
* array element-union widening (a concrete expected type met by a ``mixed``
  actual, or a ``mixed`` actual adding a type) is drift, while narrowing is not;
* one probe's unexpected exception cannot abort the run and always leaves a
  report;
* record mode rewrites only the selected provider, in the committed format;
* the real config and goldens are internally consistent;
* every connector / model-backend package is a probe or an explicit skip.
"""

from __future__ import annotations

import json
import os
import sys
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

_REPO_ROOT = None
for _parent in Path(__file__).resolve().parents:
    if (_parent / "scripts" / "provider_contract_probe.py").exists():
        _REPO_ROOT = _parent
        break
if _REPO_ROOT is None:
    raise RuntimeError("Could not find repo root (scripts/provider_contract_probe.py)")
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "provider_contract_probe.py"

_loader = SourceFileLoader("provider_contract_probe", str(_SCRIPT_PATH))
_module = module_from_spec(spec_from_loader("provider_contract_probe", _loader))
sys.modules["provider_contract_probe"] = _module
_loader.exec_module(_module)
probe = _module

_URL = "https://api.acme.test/ping"
_OK_URL = "https://api.acme.test/ok"
_RECORDED_AT = "2026-01-01T00:00:00+00:00"

#: Hosts pinned IN CODE for the synthetic ids these tests use. The real pins
#: live in ``PROVIDER_HOST_PINS``; a production config cannot add one.
_TEST_HOST_PINS = {"acme": "api.acme.test", "acme_ok": "api.acme.test", "boom": "api.boom.test"}

#: Credentials pinned IN CODE for the same synthetic ids - the analogue of
#: ``PROVIDER_CREDENTIAL_PINS`` for real providers.
_TEST_CREDENTIAL_PINS = {
    "acme": probe.CredentialPin(env_vars=frozenset({"ACME_TOKEN"})),
    "acme_ok": probe.CredentialPin(env_vars=frozenset({"OK_TOKEN"})),
    "boom": probe.CredentialPin(env_vars=frozenset({"SECOND_TOKEN"})),
}


@pytest.fixture(autouse=True)
def _pin_test_provider_hosts(monkeypatch: Any) -> None:
    """Give the synthetic test ids code-level host and credential pins.

    The loader rejects any provider id whose host or credential set is not
    pinned in the script, which is exactly what stops a merged config edit from
    redirecting a credential or reading another provider's secret. Tests that
    use a made-up provider therefore pin it the same way production does - in
    code, not in the config under test.
    """
    monkeypatch.setattr(probe, "PROVIDER_HOST_PINS", {**probe.PROVIDER_HOST_PINS, **_TEST_HOST_PINS})
    monkeypatch.setattr(probe, "PROVIDER_CREDENTIAL_PINS", {**probe.PROVIDER_CREDENTIAL_PINS, **_TEST_CREDENTIAL_PINS})


def _probe(**overrides: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "id": "acme",
        "kind": "connector",
        "operation": "ping",
        "description": "test provider",
        "request": {"method": "GET", "url": _URL},
        "allowed_host": "api.acme.test",
        "auth": {"kind": "bearer", "env": "ACME_TOKEN"},
        "env_vars": ["ACME_TOKEN"],
    }
    definition.update(overrides)
    return definition


def _write_config(tmp_path: Path, providers: list[dict[str, Any]], skips: list[dict[str, Any]] | None = None, **extra: Any) -> Path:
    config: dict[str, Any] = {
        "version": 1,
        "defaults": {
            "success_status_class": "2xx",
            "timeout_seconds": 5,
            "max_depth": 1,
            "strict_shape": True,
            "deadline_seconds": 60,
            "retry": {"attempts": 3, "base_delay_seconds": 0.0, "max_delay_seconds": 30.0},
        },
        "providers": providers,
        "skips": skips or [],
    }
    config.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _ok_probe() -> dict[str, Any]:
    """A second, healthy probe for a config.

    Used where a test needs the run to attempt something, so an excused skip on
    another probe can be shown not to fail the run on its own. It reads its own
    credential (``OK_TOKEN``) so deleting ``ACME_TOKEN`` leaves it healthy.
    """
    return _probe(
        id="acme_ok",
        request={"method": "GET", "url": _OK_URL},
        auth={"kind": "bearer", "env": "OK_TOKEN"},
        env_vars=["OK_TOKEN"],
    )


def _golden_entry(shape: dict[str, Any], max_depth: int = 1, recorded_at: str | None = _RECORDED_AT) -> dict[str, Any]:
    return {"recorded_at": recorded_at, "success_status_class": "2xx", "max_depth": max_depth, "shape": shape}


def _write_goldens_entries(tmp_path: Path, entries: dict[str, Any]) -> Path:
    path = tmp_path / "goldens.json"
    path.write_text(json.dumps({"schema_version": 1, "goldens": entries}), encoding="utf-8")
    return path


def _write_goldens(tmp_path: Path, shape: dict[str, Any], max_depth: int = 1, recorded_at: str | None = _RECORDED_AT) -> Path:
    return _write_goldens_entries(tmp_path, {"acme/ping": _golden_entry(shape, max_depth, recorded_at)})


def _run(config: Path, goldens: Path, report: Path, *extra: str) -> int:
    return probe.main(["--config", str(config), "--goldens", str(goldens), "--report", str(report), *extra])


def _read_report(report: Path) -> dict[str, Any]:
    return json.loads(report.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Shape diffing
# ---------------------------------------------------------------------------


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


def test_check_shapes_actual_at_the_goldens_recorded_depth(tmp_path: Path, monkeypatch: Any) -> None:
    """The golden's recorded max_depth governs the actual shape in check mode.

    Building the actual at the CONFIG's depth made the golden's max_depth dead
    metadata: a golden recorded flat (depth 1) compared against an actual
    rebuilt at the config's depth 2 reported every nested key as phantom
    ``added`` drift. That turned editing the documented knob into false drift
    across the board and pressured a re-record of a genuinely unchanged payload.
    """
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    # The config pins depth 2; the golden was recorded at depth 1 (its nested
    # object is a bare type, without the keys depth 2 would pin).
    config = _write_config(tmp_path, [_probe(max_depth=2)])
    flat = {"type": "object", "keys": {"alpha": {"type": "string"}, "nested": {"type": "object"}}}
    goldens = _write_goldens(tmp_path, flat, max_depth=1)
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x", "nested": {"beta": 1}}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    result = _read_report(report)["results"][0]
    assert result["status"] == "pass"
    assert not result["additions"]


def test_check_falls_back_to_config_depth_when_golden_records_none(tmp_path: Path, monkeypatch: Any) -> None:
    """A golden that records no max_depth is shaped at the configured depth."""
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe(max_depth=2)])
    entry = _golden_entry({"type": "object", "keys": {"data": {"type": "object", "keys": {"id": {"type": "string"}}}}})
    del entry["max_depth"]
    goldens = _write_goldens_entries(tmp_path, {"acme/ping": entry})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"data": {"id": "x"}}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    assert _read_report(report)["results"][0]["status"] == "pass"


def test_addition_is_breaking_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x", "beta": 1}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    result = _read_report(report)["results"][0]
    assert result["status"] == "drift"
    assert not result["drift"]
    assert result["additions"][0]["path"] == "$.beta"


def test_addition_is_additive_when_strict_shape_disabled(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe(strict_shape=False)])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x", "beta": 1}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    result = _read_report(report)["results"][0]
    assert result["status"] == "pass"
    assert result["additions"][0]["path"] == "$.beta"


def test_array_emptied_is_breaking_drift() -> None:
    drift = probe.diff_shape({"type": "array", "items": {"type": "object"}}, {"type": "array", "items": None})
    assert drift == [{"path": "$[]", "kind": "emptied", "expected": "object", "actual": None}]


def test_array_populated_is_breaking_drift() -> None:
    drift = probe.diff_shape({"type": "array", "items": None}, {"type": "array", "items": {"type": "object"}})
    assert drift == [{"path": "$[]", "kind": "populated", "expected": None, "actual": "object"}]


def test_array_shape_unions_every_element() -> None:
    shape = probe.shape_of([{"id": "a"}, {"id": "b", "name": "c"}], 0, 2)
    assert shape["items"]["type"] == "object"
    assert set(shape["items"]["keys"]) == {"id", "name"}
    # A key absent from one element is optional, so it is not later "missing".
    assert shape["items"]["keys"]["name"]["optional"] is True


def test_optional_key_absence_is_not_drift() -> None:
    expected = {"type": "object", "keys": {"id": {"type": "string"}, "name": {"type": "string", "optional": True}}}
    actual = {"type": "object", "keys": {"id": {"type": "string"}}}
    assert not probe.diff_shape(expected, actual)


def test_array_element_type_widened_to_mixed_is_drift() -> None:
    """A mixed actual must not satisfy a concrete expected element type.

    Repro of the reported defect against the ``togetherai/models`` golden: its
    items are pinned to ``object``, and ``[{...}, "oops"]`` used to pass because
    ``object`` is one member of the actual ``mixed`` union.
    """
    golden = {"type": "array", "items": {"type": "object"}}
    actual = probe.shape_of([{"id": "a"}, "oops"], 0, 1)
    assert actual["items"] == {"type": "mixed", "types": ["object", "string"]}
    assert probe.diff_shape(golden, actual) == [{"path": "$[]", "kind": "type_changed", "expected": "object", "actual": "mixed"}]


def test_array_element_type_widened_with_null_is_drift() -> None:
    """The same widening via a null element: ``[{...}, null]`` is drift too."""
    golden = {"type": "array", "items": {"type": "object"}}
    actual = probe.shape_of([{"id": "a"}, None], 0, 1)
    assert actual["items"] == {"type": "mixed", "types": ["null", "object"]}
    assert probe.diff_shape(golden, actual) == [{"path": "$[]", "kind": "type_changed", "expected": "object", "actual": "mixed"}]


def test_mixed_actual_narrowing_to_an_expected_member_is_not_drift() -> None:
    """Narrowing is allowed: the actual may lose a member the golden permits."""
    expected = {"type": "array", "items": {"type": "mixed", "types": ["object", "string"]}}
    actual = probe.shape_of([{"id": "a"}], 0, 1)
    assert not probe.diff_shape(expected, actual)


def test_mixed_actual_adding_a_type_is_drift() -> None:
    """A mixed actual may never add a type the golden does not permit."""
    expected = {"type": "array", "items": {"type": "mixed", "types": ["object", "string"]}}
    actual = probe.shape_of([{"id": "a"}, 1], 0, 1)
    assert actual["items"] == {"type": "mixed", "types": ["integer", "object"]}
    assert probe.diff_shape(expected, actual) == [{"path": "$[]", "kind": "type_changed", "expected": "mixed", "actual": "mixed"}]


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fail-closed skips
# ---------------------------------------------------------------------------


def test_missing_credential_fails_by_default(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    assert route.call_count == 0
    result = _read_report(report)["results"][0]
    assert result["status"] == "skipped"
    assert result["allowed"] is False
    assert "ACME_TOKEN" in result["reason"]
    captured = capsys.readouterr()
    assert "ACME_TOKEN" in captured.err


def test_missing_credential_allowed_when_listed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    monkeypatch.setenv("OK_TOKEN", "ok-token")
    config = _write_config(tmp_path, [_probe(), _ok_probe()], allow_missing_credential={"acme": "token not yet provisioned"})
    goldens = _write_goldens_entries(
        tmp_path,
        {
            "acme/ping": _golden_entry({"type": "object", "keys": {"alpha": {"type": "string"}}}),
            "acme_ok/ping": _golden_entry({"type": "object"}),
        },
    )
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_OK_URL).mock(return_value=httpx.Response(200, json={}))
        exit_code = _run(config, goldens, report)

    # The excused skip does not fail the run, which is green because the other
    # probe was actually attempted and passed.
    assert exit_code == 0
    results = {r["provider"]: r for r in _read_report(report)["results"]}
    assert results["acme"]["status"] == "skipped"
    assert results["acme"]["allowed"] is True
    assert "ACME_TOKEN" in results["acme"]["reason"]
    assert _read_report(report)["summary"]["attempted"] == 1


def test_missing_golden_fails_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = tmp_path / "absent.json"
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False):
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    assert not goldens.exists()
    result = _read_report(report)["results"][0]
    assert result["status"] == "skipped"
    assert result["allowed"] is False


def test_missing_golden_allowed_when_listed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    monkeypatch.setenv("OK_TOKEN", "ok-token")
    config = _write_config(tmp_path, [_probe(), _ok_probe()], allow_missing_golden={"acme": "new provider pending first record"})
    goldens = _write_goldens_entries(tmp_path, {"acme_ok/ping": _golden_entry({"type": "object"})})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_OK_URL).mock(return_value=httpx.Response(200, json={}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    results = {r["provider"]: r for r in _read_report(report)["results"]}
    assert results["acme"]["allowed"] is True
    assert _read_report(report)["summary"]["attempted"] == 1
    # Check mode never creates a golden, even for an excused missing one.
    assert "acme/ping" not in json.loads(goldens.read_text(encoding="utf-8"))["goldens"]


def test_unrecorded_golden_fails_by_default(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}}, recorded_at=None)
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    assert route.call_count == 0
    result = _read_report(report)["results"][0]
    assert result["status"] == "skipped"
    assert "not yet recorded" in result["reason"]


def test_unrecorded_golden_allowed_when_listed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    monkeypatch.setenv("OK_TOKEN", "ok-token")
    config = _write_config(tmp_path, [_probe(), _ok_probe()], allow_unrecorded_golden={"acme": "hand-authored baseline"})
    goldens = _write_goldens_entries(
        tmp_path,
        {
            "acme/ping": _golden_entry({"type": "object", "keys": {"alpha": {"type": "string"}}}, recorded_at=None),
            "acme_ok/ping": _golden_entry({"type": "object"}),
        },
    )
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_OK_URL).mock(return_value=httpx.Response(200, json={}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    results = {r["provider"]: r for r in _read_report(report)["results"]}
    assert results["acme"]["allowed"] is True
    assert _read_report(report)["summary"]["attempted"] == 1


def test_zero_attempts_fails_even_when_every_skip_is_excused(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    """The old bootstrap state (every probe excused) must be RED, not green.

    Counting passes let ``passed:0 expected:0`` exit 0 while probing nothing.
    Counting attempts cannot: a check that issued no request proves nothing,
    however explicit the excuse.
    """
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    config = _write_config(tmp_path, [_probe()], allow_missing_credential={"acme": "excused"})
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    assert route.call_count == 0
    summary = _read_report(report)["summary"]
    assert summary["attempted"] == 0
    assert summary["passed"] == 0
    assert summary["skipped_unexcused"] == 0
    assert "zero probes were attempted" in capsys.readouterr().err


def test_one_attempt_is_enough_to_be_green(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    assert _read_report(report)["summary"]["attempted"] == 1


# ---------------------------------------------------------------------------
# Retry / backoff / unavailability
# ---------------------------------------------------------------------------


def test_retry_on_429_honours_retry_after_then_succeeds(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    delays: list[float] = []
    monkeypatch.setattr(probe, "_sleep", delays.append)
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json={"alpha": "x"}),
            ]
        )
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    assert route.call_count == 2
    assert delays == [2.0]
    result = _read_report(report)["results"][0]
    assert result["status"] == "pass"
    assert result["attempts"] == 2


def test_5xx_after_exhaustion_is_provider_unavailable_not_drift(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    monkeypatch.setattr(probe, "_sleep", lambda _delay: None)
    config = _write_config(tmp_path, [_probe()])
    goldens = _write_goldens(tmp_path, {"type": "object", "keys": {"alpha": {"type": "string"}}})
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(_URL).mock(return_value=httpx.Response(503))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    assert route.call_count == 3
    result = _read_report(report)["results"][0]
    assert result["status"] == "unavailable"
    assert result["error"] == "provider_unavailable"
    assert result["status"] != "drift"
    summary = _read_report(report)["summary"]
    assert summary["drifted"] == 0
    assert summary["unavailable"] == 1


def test_deadline_is_enforced_before_a_request() -> None:
    with pytest.raises(probe.ProviderUnavailableError, match="deadline"):
        probe._call_with_retries(None, _probe(), {}, {}, {}, deadline=time.monotonic() - 1)


# ---------------------------------------------------------------------------
# Read-only enforcement / config validation
# ---------------------------------------------------------------------------


def test_post_not_pinned_in_code_is_rejected(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(request={"method": "POST", "url": _URL})])
    with pytest.raises(probe.ProbeConfigError, match="pinned in code"):
        probe.load_config(config)


def test_posting_provider_cannot_be_renamed_onto_a_write_endpoint(tmp_path: Path) -> None:
    # Repro of the reported defect: the id is on the read-only POST list, but
    # the URL is supplied by the config, so a merge could point it at a write.
    slack = _probe(
        id="slack",
        request={"method": "POST", "url": "https://slack.com/api/chat.delete", "json": {"channel": "C1", "ts": "1"}},
        allowed_host="slack.com",
        auth={"kind": "bearer", "env": "FARNA_PROBE_SLACK_TOKEN"},
        env_vars=["FARNA_PROBE_SLACK_TOKEN"],
    )
    config = _write_config(tmp_path, [slack])
    with pytest.raises(probe.ProbeConfigError, match="does not match the endpoint pinned in code"):
        probe.load_config(config)


def test_pinned_post_body_must_match_exactly(tmp_path: Path) -> None:
    slack = _probe(
        id="slack",
        request={"method": "POST", "url": "https://slack.com/api/auth.test", "json": {"channel": "C1"}},
        allowed_host="slack.com",
        auth={"kind": "bearer", "env": "FARNA_PROBE_SLACK_TOKEN"},
        env_vars=["FARNA_PROBE_SLACK_TOKEN"],
    )
    config = _write_config(tmp_path, [slack])
    with pytest.raises(probe.ProbeConfigError, match="body"):
        probe.load_config(config)


def test_url_host_must_match_the_host_pinned_in_code(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(allowed_host="other.test")])
    with pytest.raises(probe.ProbeConfigError, match="does not match the host pinned in code"):
        probe.load_config(config)


def test_config_cannot_redirect_a_credential_to_a_new_host(tmp_path: Path) -> None:
    # Repro of the reported defect: changing the URL and ``allowed_host``
    # together used to be accepted, sending the token to the new host. The code
    # pin makes that a load-time failure.
    github = _probe(
        id="github",
        request={"method": "GET", "url": "https://evil.test/rate_limit"},
        allowed_host="evil.test",
        auth={"kind": "bearer", "env": "FARNA_PROBE_GITHUB_TOKEN"},
        env_vars=["FARNA_PROBE_GITHUB_TOKEN"],
    )
    config = _write_config(tmp_path, [github])
    with pytest.raises(probe.ProbeConfigError, match="pinned in code"):
        probe.load_config(config)


def test_unknown_provider_id_cannot_introduce_a_new_host(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(id="unpinned", request={"method": "GET", "url": "https://evil.test/ping"}, allowed_host="evil.test")])
    with pytest.raises(probe.ProbeConfigError, match="no host is pinned"):
        probe.load_config(config)


def test_url_must_not_embed_credentials(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(request={"method": "GET", "url": "https://user:pw@api.acme.test/ping"})])
    with pytest.raises(probe.ProbeConfigError, match="must not embed credentials"):
        probe.load_config(config)


def test_undeclared_extra_header_env_is_rejected(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(extra_headers={"X-Extra": "UNDECLARED_TOKEN"})])
    with pytest.raises(probe.ProbeConfigError, match="not declared in env_vars"):
        probe.load_config(config)


def test_credential_pin_rejects_another_providers_env_var(tmp_path: Path) -> None:
    """Repro of the reported defect: used-vs-declared alone let this load.

    A ``github`` probe that declares Slack's credential is internally
    consistent, so it used to pass the loader and send Slack's token to
    api.github.com. The per-provider credential pin rejects it at load.
    """
    github = _probe(
        id="github",
        request={"method": "GET", "url": "https://api.github.com/rate_limit"},
        allowed_host="api.github.com",
        auth={"kind": "bearer", "env": "FARNA_PROBE_SLACK_TOKEN"},
        env_vars=["FARNA_PROBE_SLACK_TOKEN"],
    )
    config = _write_config(tmp_path, [github])
    with pytest.raises(probe.ProbeConfigError, match="not permitted for this provider"):
        probe.load_config(config)


def test_credential_pin_rejects_an_undeclared_but_unpinned_env_var(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(auth={"kind": "bearer", "env": "ACME_OTHER_SECRET"}, env_vars=["ACME_OTHER_SECRET"])])
    with pytest.raises(probe.ProbeConfigError, match="not permitted for this provider"):
        probe.load_config(config)


def test_credential_pin_rejects_an_unpinned_header_name(tmp_path: Path) -> None:
    github = _probe(
        id="github",
        request={"method": "GET", "url": "https://api.github.com/rate_limit"},
        allowed_host="api.github.com",
        auth={"kind": "header", "env": "FARNA_PROBE_GITHUB_TOKEN", "header": "X-Slack-Only"},
        env_vars=["FARNA_PROBE_GITHUB_TOKEN"],
    )
    config = _write_config(tmp_path, [github])
    with pytest.raises(probe.ProbeConfigError, match="not permitted for this provider"):
        probe.load_config(config)


def test_credential_pin_rejects_an_unpinned_query_param_name(tmp_path: Path) -> None:
    trello = _probe(
        id="trello",
        request={"method": "GET", "url": "https://api.trello.com/1/members/me"},
        allowed_host="api.trello.com",
        auth={"kind": "query", "params": {"org": "FARNA_PROBE_TRELLO_KEY"}},
        env_vars=["FARNA_PROBE_TRELLO_KEY"],
    )
    config = _write_config(tmp_path, [trello])
    with pytest.raises(probe.ProbeConfigError, match="not permitted for this provider"):
        probe.load_config(config)


def test_bad_header_template_is_rejected_at_load(tmp_path: Path) -> None:
    config = _write_config(tmp_path, [_probe(auth={"kind": "header", "env": "ACME_TOKEN", "header": "X-Key", "template": "{bogus}"})])
    with pytest.raises(probe.ProbeConfigError, match="may only reference"):
        probe.load_config(config)


def test_form_body_is_sent(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("FARNA_PROBE_MONDAY_TOKEN", "test-token")
    # The real monday pin is a JSON body; pin a form body here so the transport
    # path for ``form`` is exercised without weakening the production pin. The
    # credential names are monday's real ones, because the credential pin is
    # keyed on the provider id and is not patched for a real provider.
    monkeypatch.setattr(
        probe,
        "READ_ONLY_POST_ENDPOINTS",
        {**probe.READ_ONLY_POST_ENDPOINTS, "monday": probe.ReadOnlyPost("https://api.monday.com/v2", "form", {"query": "{ me { id } }"})},
    )
    form_probe = _probe(
        id="monday",
        request={"method": "POST", "url": "https://api.monday.com/v2", "form": {"query": "{ me { id } }"}},
        allowed_host="api.monday.com",
        auth={"kind": "bearer", "env": "FARNA_PROBE_MONDAY_TOKEN"},
        env_vars=["FARNA_PROBE_MONDAY_TOKEN"],
    )
    config = _write_config(tmp_path, [form_probe])
    goldens = tmp_path / "goldens.json"
    goldens.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goldens": {
                    "monday/ping": {
                        "recorded_at": _RECORDED_AT,
                        "success_status_class": "2xx",
                        "max_depth": 1,
                        "shape": {"type": "object", "keys": {"result": {"type": "string"}}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    with respx.mock(assert_all_called=False) as mock:
        route = mock.post("https://api.monday.com/v2").mock(return_value=httpx.Response(200, json={"result": "ok"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 0
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert "query=" in sent.content.decode()


def test_timeout_comes_from_config_and_per_probe_override() -> None:
    defaults = {"timeout_seconds": 7}
    assert probe._probe_timeout(_probe(), defaults) == 7.0
    assert probe._probe_timeout(_probe(timeout_seconds=3), defaults) == 3.0


# ---------------------------------------------------------------------------
# Per-probe isolation
# ---------------------------------------------------------------------------


def test_one_probe_exception_does_not_abort_the_run(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    secret = "ISOLATION-SECRET-456"
    monkeypatch.setenv("ACME_TOKEN", secret)
    monkeypatch.setenv("SECOND_TOKEN", secret)
    ok = _probe()
    boom = _probe(id="boom", operation="ping", request={"method": "GET", "url": "https://api.boom.test/ping"}, allowed_host="api.boom.test", auth={"kind": "bearer", "env": "SECOND_TOKEN"}, env_vars=["SECOND_TOKEN"])
    config = _write_config(tmp_path, [boom, ok])
    goldens = tmp_path / "goldens.json"
    goldens.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goldens": {
                    "boom/ping": {"recorded_at": _RECORDED_AT, "success_status_class": "2xx", "max_depth": 1, "shape": {"type": "object"}},
                    "acme/ping": {"recorded_at": _RECORDED_AT, "success_status_class": "2xx", "max_depth": 1, "shape": {"type": "object", "keys": {"alpha": {"type": "string"}}}},
                },
            }
        ),
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    real_call = probe._call

    def _explode(client: Any, p: dict[str, Any], headers: dict[str, str], params: dict[str, str], timeout: float) -> httpx.Response:
        if p["id"] == "boom":
            raise RuntimeError(f"kaboom {secret}")
        return real_call(client, p, headers, params, timeout)

    monkeypatch.setattr(probe, "_call", _explode)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        exit_code = _run(config, goldens, report)

    assert exit_code == 1
    results = {r["provider"]: r for r in _read_report(report)["results"]}
    assert results["boom"]["status"] == "error"
    assert results["acme"]["status"] == "pass"
    body = report.read_text(encoding="utf-8")
    assert secret not in body
    assert secret not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Record mode
# ---------------------------------------------------------------------------


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
    assert manifest["goldens"]["acme/ping"]["recorded_at"]
    recorded = goldens.read_text(encoding="utf-8")

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        check_exit = _run(config, goldens, report)

    assert check_exit == 0
    assert _read_report(report)["results"][0]["status"] == "pass"
    assert goldens.read_text(encoding="utf-8") == recorded


def test_record_rewrites_only_the_selected_provider_in_committed_format(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACME_TOKEN", "test-token")
    config = _write_config(tmp_path, [_probe()])
    goldens = tmp_path / "goldens.json"
    other = {"recorded_at": _RECORDED_AT, "success_status_class": "2xx", "max_depth": 1, "shape": {"type": "object", "keys": {"alpha": {"type": "string"}}}}
    manifest = {"schema_version": 1, "goldens": {"other/op": other}}
    probe._write_goldens(goldens, manifest)
    before_other = json.dumps(manifest["goldens"]["other/op"], sort_keys=True)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(_URL).mock(return_value=httpx.Response(200, json={"alpha": "x"}))
        report = tmp_path / "report.json"
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
    reloaded = json.loads(goldens.read_text(encoding="utf-8"))
    assert json.dumps(reloaded["goldens"]["other/op"], sort_keys=True) == before_other
    # The whole file is in the canonical committed format, so the diff is one block.
    assert goldens.read_text(encoding="utf-8") == probe._format_json(reloaded, 0) + "\n"


# ---------------------------------------------------------------------------
# Real config / goldens / registry coverage
# ---------------------------------------------------------------------------


def _real_config() -> dict[str, Any]:
    return probe.load_config(_REPO_ROOT / "configs" / "provider-contract-probes.yaml")


def _real_goldens() -> dict[str, Any]:
    return json.loads((_REPO_ROOT / "contract-goldens" / "provider-shapes.json").read_text(encoding="utf-8"))


def test_real_config_and_goldens_are_consistent() -> None:
    config = _real_config()
    manifest = _real_goldens()
    golden_keys = set(manifest["goldens"])

    ids = [p["id"] for p in config["providers"]]
    assert len(ids) == len(set(ids))

    expected_keys = set()
    for definition in config["providers"]:
        key = f"{definition['id']}/{definition['operation']}"
        expected_keys.add(key)
        # Every configured provider has a golden.
        assert key in golden_keys, key

        status_class = definition.get("success_status_class", config["defaults"]["success_status_class"])
        assert status_class in {"1xx", "2xx", "3xx", "4xx", "5xx"}, key

        shape = manifest["goldens"][key]["shape"]
        assert isinstance(shape, dict), key
        # Check mode shapes the actual response at the depth the golden records,
        # so every committed golden must carry a usable depth.
        depth = manifest["goldens"][key].get("max_depth")
        assert isinstance(depth, int), key
        assert not isinstance(depth, bool), key
        assert depth >= 0, key
        for rule in definition.get("normalise") or []:
            resolves = probe.path_in_shape(shape, rule["path"].split("."))
            if rule["rule"] == "drop":
                # The golden stores the *normalised* shape, so a dropped path
                # must be absent; any other rule must still resolve.
                assert not resolves, (key, rule["path"], "dropped path is still in the golden")
            else:
                assert resolves, (key, rule["path"])

    # Every golden belongs to a configured provider (no orphan blocks).
    assert golden_keys == expected_keys


def test_real_config_check_is_red_until_goldens_are_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    """The committed state is honest: no golden is recorded, and that is RED.

    The golden gate runs before any credential is resolved, so this touches no
    network. It must exit non-zero with zero attempts: the nightly is meant to
    fail until each shape is recorded live behind review. An "all 36 excused"
    allow list - which made this run green while probing nothing - must never
    come back.
    """
    for name in [key for key in os.environ if key.startswith("FARNA_PROBE_")]:
        monkeypatch.delenv(name)
    report = tmp_path / "report.json"

    exit_code = probe.main(
        [
            "--mode",
            "check",
            "--config",
            str(_REPO_ROOT / "configs" / "provider-contract-probes.yaml"),
            "--goldens",
            str(_REPO_ROOT / "contract-goldens" / "provider-shapes.json"),
            "--report",
            str(report),
        ]
    )

    assert exit_code == 1
    summary = _read_report(report)["summary"]
    assert summary["attempted"] == 0
    assert summary["passed"] == 0
    excused = set(_real_config()["allow_unrecorded_golden"])
    assert excused != {p["id"] for p in _real_config()["providers"]}


def _packages(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_dir() and not path.name.startswith(("_", ".")) and (path / "__init__.py").exists()}


def test_every_connector_and_model_backend_is_a_probe_or_an_explicit_skip() -> None:
    config = _real_config()
    configured = {p["id"] for p in config["providers"]} | {s["id"] for s in config["skips"]}
    connectors = _packages(_REPO_ROOT / "backend" / "src" / "farna" / "connectors")
    backends = _packages(_REPO_ROOT / "backend" / "src" / "farna" / "model_backends")

    missing = sorted((connectors | backends) - configured)
    assert not missing, f"registry packages with neither a probe nor a skip: {missing}"
    # And no configured id points at a package that does not exist (typo guard).
    assert not (configured - (connectors | backends))
