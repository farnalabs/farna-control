"""FAR-2703 - nightly live provider contract probe with golden diff.

Mocks and cassettes prove our code against a frozen assumption. Nothing in the
test suite detects that a *provider* changed. This script makes a cheap,
read-only call to each configured provider and compares the response SHAPE
(key names and value types) against a committed golden. It exits non-zero on
drift so the nightly workflow fails before a customer notices.

Modes
-----
check        (default) call every configured probe, diff shape vs golden,
             exit non-zero on drift.
record       explicitly (re)write ONE golden. This is the ONLY path that
             writes a golden: --mode record --provider <id> --operation <id>.
--dry-run    print what would be called. No network, no writes.
--report-only  never exit non-zero on drift (still writes the JSON report).

Design notes
------------
* Read-only calls only. The config names the cheapest read-only operation for
  each provider; this script never issues a write.
* Goldens store a SHAPE, never response values, so a secret (or any customer
  data) can never reach a golden.
* Volatile-value handling (ids, timestamps, etags) is declared in the probe
  config (``normalise:``), never scattered through this code.
* Request/response headers are never persisted. Tokenised URL userinfo and any
  credential value are scrubbed from every error string before it is written or
  printed.

Standard library plus httpx and PyYAML only (both already runtime deps).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "provider-contract-probes.yaml"
DEFAULT_GOLDENS = REPO_ROOT / "contract-goldens" / "provider-shapes.json"
DEFAULT_REPORT = REPO_ROOT / "contract-goldens" / "report.json"

GOLDEN_SCHEMA_VERSION = 1
VOLATILE = "<volatile>"
NULLABLE = "<null>"
_DROP = object()

_BREAKING_KINDS = frozenset({"missing", "type_changed"})


class ProbeConfigError(Exception):
    """The probe configuration is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------
# Shape helpers
# ---------------------------------------------------------------------------


def _type_name(value: Any) -> str:
    """Return a JSON-ish type name. ``bool`` must precede ``int``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "unknown"


def _apply_rule(value: Any, rule: str) -> Any:
    if rule == "drop":
        return _DROP
    if rule == "volatile":
        return VOLATILE
    if rule == "nullable":
        return NULLABLE if value is None else value
    raise ProbeConfigError(f"unknown normalisation rule: {rule!r}")


def _transform(node: Any, tokens: list[str], rule: str) -> Any:
    """Apply *rule* at *tokens* inside *node*, returning a new structure."""
    if not tokens:
        return _apply_rule(node, rule)

    token = tokens[0]
    rest = tokens[1:]

    if isinstance(node, dict):
        if token == "*":
            result: dict[str, Any] = {}
            for key, value in node.items():
                transformed = _transform(value, rest, rule)
                if transformed is not _DROP:
                    result[key] = transformed
            return result
        if token not in node:
            return node
        transformed = _transform(node[token], rest, rule)
        result = dict(node)
        if transformed is _DROP:
            result.pop(token, None)
        else:
            result[token] = transformed
        return result

    if isinstance(node, list):
        if token != "[]":
            return node
        items: list[Any] = []
        for item in node:
            transformed = _transform(item, rest, rule)
            if transformed is not _DROP:
                items.append(transformed)
        return items

    return node


def normalise(payload: Any, rules: list[dict[str, str]]) -> Any:
    """Apply the config-declared normalisation rules to a response payload."""
    result = payload
    for rule in rules:
        tokens = rule["path"].split(".")
        result = _transform(result, tokens, rule["rule"])
        if result is _DROP:
            return None
    return result


def shape_of(value: Any, depth: int, max_depth: int) -> dict[str, Any]:
    """Describe *value* as a shape tree down to *max_depth* levels."""
    node: dict[str, Any] = {"type": _type_name(value)}
    if depth >= max_depth:
        return node
    if isinstance(value, dict):
        node["keys"] = {key: shape_of(value[key], depth + 1, max_depth) for key in sorted(value)}
    elif isinstance(value, list):
        node["items"] = shape_of(value[0], depth + 1, max_depth) if value else None
    return node


def diff_shape(expected: dict[str, Any], actual: dict[str, Any], path: str = "$") -> list[dict[str, Any]]:
    """Diff two shape trees. Returns a list of drift records."""
    expected_type = expected.get("type")
    actual_type = actual.get("type")
    if expected_type != actual_type:
        return [{"path": path, "kind": "type_changed", "expected": expected_type, "actual": actual_type}]

    diffs: list[dict[str, Any]] = []
    if expected_type == "object":
        expected_keys = expected.get("keys", {})
        actual_keys = actual.get("keys", {})
        for key in sorted(expected_keys):
            child_path = f"{path}.{key}"
            if key not in actual_keys:
                diffs.append({"path": child_path, "kind": "missing", "expected": expected_keys[key].get("type"), "actual": None})
            else:
                diffs.extend(diff_shape(expected_keys[key], actual_keys[key], child_path))
        diffs.extend({"path": f"{path}.{key}", "kind": "added", "expected": None, "actual": actual_keys[key].get("type")} for key in sorted(set(actual_keys) - set(expected_keys)))
    elif expected_type == "array":
        expected_items = expected.get("items")
        actual_items = actual.get("items")
        if expected_items and actual_items:
            diffs.extend(diff_shape(expected_items, actual_items, f"{path}[]"))
    return diffs


# ---------------------------------------------------------------------------
# Config loading / validation
# ---------------------------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ProbeConfigError(f"config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProbeConfigError("config must be a mapping")
    if data.get("version") != 1:
        raise ProbeConfigError(f"unsupported config version: {data.get('version')!r}")

    providers = data.get("providers") or []
    skips = data.get("skips") or []
    if not isinstance(providers, list) or not isinstance(skips, list):
        raise ProbeConfigError("'providers' and 'skips' must be lists")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ProbeConfigError("'defaults' must be a mapping")

    seen: set[str] = set()
    for skip in skips:
        _require_skip_fields(skip)
        if skip["id"] in seen:
            raise ProbeConfigError(f"duplicate provider id: {skip['id']}")
        seen.add(skip["id"])

    for probe in providers:
        _validate_probe(probe, defaults)
        if probe["id"] in seen:
            raise ProbeConfigError(f"duplicate provider id: {probe['id']}")
        seen.add(probe["id"])

    return data


def _require_skip_fields(skip: Any) -> None:
    if not isinstance(skip, dict):
        raise ProbeConfigError("each skip must be a mapping")
    for field in ("id", "kind", "reason"):
        if not isinstance(skip.get(field), str) or not skip[field].strip():
            raise ProbeConfigError(f"skip is missing a non-empty {field!r}: {skip!r}")


def _validate_probe(probe: Any, defaults: dict[str, Any]) -> None:
    if not isinstance(probe, dict):
        raise ProbeConfigError("each provider must be a mapping")
    for field in ("id", "kind", "operation", "description"):
        if not isinstance(probe.get(field), str) or not probe[field].strip():
            raise ProbeConfigError(f"probe is missing a non-empty {field!r}: {probe!r}")
    if probe["kind"] not in ("connector", "model_backend"):
        raise ProbeConfigError(f"unknown kind {probe['kind']!r} for {probe['id']}")

    request = probe.get("request")
    if not isinstance(request, dict):
        raise ProbeConfigError(f"{probe['id']}: 'request' must be a mapping")
    if request.get("method") not in ("GET", "POST"):
        raise ProbeConfigError(f"{probe['id']}: request.method must be GET or POST")
    if not isinstance(request.get("url"), str) or not request["url"].startswith("https://"):
        raise ProbeConfigError(f"{probe['id']}: request.url must be an https URL")

    _validate_auth(probe)

    status_class = probe.get("success_status_class", defaults.get("success_status_class", "2xx"))
    if not isinstance(status_class, str) or len(status_class) != 3 or not status_class.endswith("xx") or not status_class[0].isdigit():
        raise ProbeConfigError(f"{probe['id']}: invalid success_status_class {status_class!r}")

    max_depth = probe.get("max_depth", defaults.get("max_depth", 1))
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ProbeConfigError(f"{probe['id']}: max_depth must be a non-negative integer")

    rules = probe.get("normalise") or []
    if not isinstance(rules, list):
        raise ProbeConfigError(f"{probe['id']}: 'normalise' must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("path"), str) or rule.get("rule") not in ("drop", "volatile", "nullable"):
            raise ProbeConfigError(f"{probe['id']}: invalid normalise rule {rule!r}")


def _validate_auth(probe: dict[str, Any]) -> None:
    auth = probe.get("auth")
    if not isinstance(auth, dict):
        raise ProbeConfigError(f"{probe['id']}: 'auth' must be a mapping")
    kind = auth.get("kind")
    if kind not in ("none", "bearer", "header", "query"):
        raise ProbeConfigError(f"{probe['id']}: unknown auth kind {kind!r}")
    if kind in ("bearer", "header") and not auth.get("env"):
        raise ProbeConfigError(f"{probe['id']}: auth.env is required for {kind} auth")
    if kind == "query":
        params = auth.get("params")
        if not isinstance(params, dict) or not params:
            raise ProbeConfigError(f"{probe['id']}: auth.params must be a non-empty mapping for query auth")
        for name, env in params.items():
            if not isinstance(name, str) or not isinstance(env, str) or not env:
                raise ProbeConfigError(f"{probe['id']}: auth.params entries must map a name to an env var")

    extra = probe.get("extra_headers") or {}
    if not isinstance(extra, dict):
        raise ProbeConfigError(f"{probe['id']}: 'extra_headers' must be a mapping")
    static = probe.get("static_headers") or {}
    if not isinstance(static, dict):
        raise ProbeConfigError(f"{probe['id']}: 'static_headers' must be a mapping")


def _probe_settings(probe: dict[str, Any], defaults: dict[str, Any]) -> tuple[str, int]:
    status_class = probe.get("success_status_class", defaults.get("success_status_class", "2xx"))
    max_depth = probe.get("max_depth", defaults.get("max_depth", 1))
    return status_class, max_depth


# ---------------------------------------------------------------------------
# Credentials and request building
# ---------------------------------------------------------------------------


def _resolve_credentials(probe: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """Return (headers, query params, secret values, missing env var names)."""
    headers: dict[str, str] = dict(probe.get("static_headers") or {})
    params: dict[str, str] = {}
    secrets: list[str] = []
    missing: list[str] = []

    auth = probe["auth"]
    kind = auth["kind"]
    if kind == "bearer":
        token = os.environ.get(auth["env"], "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            secrets.append(token)
        else:
            missing.append(auth["env"])
    elif kind == "header":
        token = os.environ.get(auth["env"], "")
        if token:
            header_name = auth.get("header", "Authorization")
            template = auth.get("template", "{token}")
            headers[header_name] = template.format(token=token)
            secrets.append(token)
        else:
            missing.append(auth["env"])
    elif kind == "query":
        for name, env in auth["params"].items():
            value = os.environ.get(env, "")
            if value:
                params[name] = value
                secrets.append(value)
            else:
                missing.append(env)

    for header_name, env in (probe.get("extra_headers") or {}).items():
        value = os.environ.get(env, "")
        if value:
            headers[header_name] = value
            secrets.append(value)
        else:
            missing.append(env)

    return headers, params, secrets, missing


def _query_secret_param_names(probe: dict[str, Any]) -> list[str]:
    auth = probe["auth"]
    if auth["kind"] != "query":
        return []
    return list(auth["params"].keys())


def _safe_url(url: str, secret_params: list[str]) -> str:
    """Return *url* with userinfo stripped and secret query values masked."""
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    query = [(key, "***" if key in secret_params else value) for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit((parts.scheme, netloc, parts.path, urlencode(query), parts.fragment))


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if not secret:
            continue
        text = text.replace(secret, "***")
        encoded = quote(secret, safe="")
        if encoded != secret:
            text = text.replace(encoded, "***")
    return text


# ---------------------------------------------------------------------------
# Golden manifest
# ---------------------------------------------------------------------------


def _golden_key(provider: str, operation: str) -> str:
    return f"{provider}/{operation}"


def _load_goldens(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": GOLDEN_SCHEMA_VERSION, "goldens": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("goldens"), dict):
        raise ProbeConfigError(f"golden manifest is malformed: {path}")
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _record_golden(manifest: dict[str, Any], key: str, status_class: str, max_depth: int, shape: dict[str, Any]) -> None:
    manifest.setdefault("schema_version", GOLDEN_SCHEMA_VERSION)
    manifest["goldens"][key] = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "success_status_class": status_class,
        "max_depth": max_depth,
        "shape": shape,
    }


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


def _call(client: httpx.Client, probe: dict[str, Any], headers: dict[str, str], params: dict[str, str]) -> httpx.Response:
    request = probe["request"]
    call_headers = dict(headers)
    kwargs: dict[str, Any] = {}
    if "json" in request:
        payload = request["json"]
        if payload is None:
            # An explicit JSON ``null`` body (e.g. Dropbox no-argument RPCs).
            # httpx treats ``json=None`` as "no body", so send it literally.
            kwargs["content"] = b"null"
            call_headers.setdefault("Content-Type", "application/json")
        else:
            kwargs["json"] = payload
    return client.request(request["method"], request["url"], headers=call_headers, params=params, **kwargs)


def _shape_from_response(probe: dict[str, Any], defaults: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
    _, max_depth = _probe_settings(probe, defaults)
    payload = response.json()
    normalised = normalise(payload, probe.get("normalise") or [])
    return shape_of(normalised, 0, max_depth)


def _check_one(
    client: httpx.Client,
    probe: dict[str, Any],
    defaults: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    key = _golden_key(probe["id"], probe["operation"])
    headers, params, secrets, missing = _resolve_credentials(probe)
    if missing:
        reason = "missing credential: " + ", ".join(missing)
        print(f"WARNING: skipping {key} - {reason}", file=sys.stderr)
        return {"provider": probe["id"], "operation": probe["operation"], "status": "skipped", "reason": reason}

    golden = manifest["goldens"].get(key)
    if golden is None:
        reason = f"no golden recorded for {key}; run --mode record behind review"
        print(f"WARNING: skipping {key} - {reason}", file=sys.stderr)
        return {"provider": probe["id"], "operation": probe["operation"], "status": "skipped", "reason": reason}
    if not isinstance(golden.get("shape"), dict):
        return {"provider": probe["id"], "operation": probe["operation"], "status": "error", "error": "malformed golden entry (no shape)"}

    expected_class = golden.get("success_status_class") or _probe_settings(probe, defaults)[0]
    try:
        response = _call(client, probe, headers, params)
    except httpx.HTTPError as exc:
        return {"provider": probe["id"], "operation": probe["operation"], "status": "error", "error": _scrub(str(exc), secrets)}

    actual_class = f"{response.status_code // 100}xx"
    if actual_class != expected_class:
        return {
            "provider": probe["id"],
            "operation": probe["operation"],
            "status": "error",
            "status_code": response.status_code,
            "error": f"status class {actual_class} != expected {expected_class}",
        }

    try:
        actual_shape = _shape_from_response(probe, defaults, response)
    except ValueError as exc:
        return {"provider": probe["id"], "operation": probe["operation"], "status": "error", "error": _scrub(f"non-JSON response: {exc}", secrets)}

    diffs = diff_shape(golden["shape"], actual_shape)
    breaking = [diff for diff in diffs if diff["kind"] in _BREAKING_KINDS]
    additions = [diff for diff in diffs if diff["kind"] == "added"]
    strict = bool(probe.get("strict_shape", False))
    is_drift = bool(breaking) or (strict and bool(additions))
    return {
        "provider": probe["id"],
        "operation": probe["operation"],
        "status": "drift" if is_drift else "pass",
        "status_code": response.status_code,
        "drift": breaking,
        "additions": additions,
    }


def _record_one(client: httpx.Client, probe: dict[str, Any], defaults: dict[str, Any], manifest: dict[str, Any], goldens_path: Path) -> int:
    key = _golden_key(probe["id"], probe["operation"])
    headers, params, secrets, missing = _resolve_credentials(probe)
    if missing:
        print(f"ERROR: cannot record {key} - missing credential: {', '.join(missing)}", file=sys.stderr)
        return 2

    expected_class, max_depth = _probe_settings(probe, defaults)
    try:
        response = _call(client, probe, headers, params)
    except httpx.HTTPError as exc:
        print(f"ERROR: cannot record {key} - {_scrub(str(exc), secrets)}", file=sys.stderr)
        return 1

    actual_class = f"{response.status_code // 100}xx"
    if actual_class != expected_class:
        print(f"ERROR: cannot record {key} - status class {actual_class} != expected {expected_class}", file=sys.stderr)
        return 1

    try:
        shape = _shape_from_response(probe, defaults, response)
    except ValueError as exc:
        print(f"ERROR: cannot record {key} - non-JSON response: {_scrub(str(exc), secrets)}", file=sys.stderr)
        return 1

    _record_golden(manifest, key, expected_class, max_depth, shape)
    _write_json(goldens_path, manifest)
    print(f"RECORDED {key} -> {goldens_path}")
    return 0


def _dry_run(probes: list[dict[str, Any]], defaults: dict[str, Any], manifest: dict[str, Any]) -> int:
    for probe in probes:
        key = _golden_key(probe["id"], probe["operation"])
        request = probe["request"]
        safe_url = _safe_url(request["url"], _query_secret_param_names(probe))
        _, _, _, missing = _resolve_credentials(probe)
        credential_state = "MISSING" if missing else "present"
        golden_state = "present" if key in manifest["goldens"] else "MISSING"
        auth_kind = probe["auth"]["kind"]
        print(f"DRY-RUN {key}: {request['method']} {safe_url} auth={auth_kind} credential={credential_state} golden={golden_state}")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _build_report(mode: str, config_path: Path, goldens_path: Path, results: list[dict[str, Any]], skips: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "drifted": sum(1 for r in results if r["status"] == "drift"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "additions": sum(len(r.get("additions", [])) for r in results),
        "skipped_by_design": len(skips),
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "config": str(config_path),
        "goldens": str(goldens_path),
        "summary": summary,
        "results": results,
        "skips": skips,
    }


def _print_results(results: list[dict[str, Any]]) -> None:
    for result in results:
        key = _golden_key(result["provider"], result["operation"])
        status = result["status"]
        if status == "pass":
            print(f"PASS  {key}")
        elif status == "drift":
            details = "; ".join(f"{d['kind']} {d['path']}" for d in result.get("drift", []))
            print(f"DRIFT {key}: {details}")
        elif status == "skipped":
            print(f"SKIP  {key}: {result.get('reason', '')}")
        else:
            print(f"ERROR {key}: {result.get('error', '')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live provider contract probe with golden shape diff.")
    parser.add_argument("--mode", choices=("check", "record"), default="check")
    parser.add_argument("--provider", help="provider id (required for --mode record)")
    parser.add_argument("--operation", help="operation id (required for --mode record)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--goldens", type=Path, default=DEFAULT_GOLDENS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--dry-run", action="store_true", help="print planned calls; no network, no writes")
    parser.add_argument("--report-only", action="store_true", help="never exit non-zero on drift")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        config = load_config(args.config)
    except ProbeConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    defaults = config.get("defaults") or {}
    probes: list[dict[str, Any]] = config["providers"]
    skips: list[dict[str, Any]] = config["skips"]

    if args.dry_run:
        try:
            manifest = _load_goldens(args.goldens)
        except (ProbeConfigError, json.JSONDecodeError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        return _dry_run(probes, defaults, manifest)

    if args.mode == "record":
        if not args.provider or not args.operation:
            print("error: --mode record requires --provider and --operation", file=sys.stderr)
            return 2
        selected = [p for p in probes if p["id"] == args.provider and p["operation"] == args.operation]
        if not selected:
            print(f"error: no probe configured for {args.provider}/{args.operation}", file=sys.stderr)
            return 2
        try:
            manifest = _load_goldens(args.goldens)
        except (ProbeConfigError, json.JSONDecodeError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2
        with httpx.Client(timeout=30.0, follow_redirects=False) as client:
            return _record_one(client, selected[0], defaults, manifest, args.goldens)

    try:
        manifest = _load_goldens(args.goldens)
    except (ProbeConfigError, json.JSONDecodeError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    selected = [p for p in probes if not args.provider or p["id"] == args.provider]
    with httpx.Client(timeout=30.0, follow_redirects=False) as client:
        results = [_check_one(client, probe, defaults, manifest) for probe in selected]

    _print_results(results)
    report = _build_report(args.mode, args.config, args.goldens, results, skips)
    _write_json(args.report, report)

    summary = report["summary"]
    print(f"summary: {summary['passed']} passed, {summary['drifted']} drifted, {summary['skipped']} skipped, {summary['errors']} errors, {summary['additions']} additive changes; report -> {args.report}")

    if args.report_only:
        return 0
    if summary["drifted"] or summary["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
