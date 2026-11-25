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
* Read-only calls only. Each provider id is pinned to its host IN CODE
  (``PROVIDER_HOST_PINS``), so a config edit cannot introduce a host or redirect
  a credential elsewhere. Each provider id is also pinned to the exact credential
  env vars (and header / query-param names) it may read
  (``PROVIDER_CREDENTIAL_PINS``), so a config edit cannot make a github probe
  read Slack's token and send it to api.github.com. The loader rejects any
  non-GET/HEAD method, and pins each read-only POST to its exact URL and body
  (``READ_ONLY_POST_ENDPOINTS``), so a config edit cannot rename a probe onto a
  write endpoint.
* A run that attempts ZERO probes is a failure, however many of its skips are
  explicitly excused: counting attempts (not passes) is what stops an all-skipped
  run from looking clean.
* Goldens store a SHAPE, never response values, so a secret (or any customer
  data) can never reach a golden.
* Volatile-value handling (ids, timestamps, etags) is declared in the probe
  config (``normalise:``), never scattered through this code.
* Request/response headers are never persisted. Tokenised URL userinfo and any
  credential value are scrubbed from every error string before it is written or
  printed.
* Transient failures (network error, 429, 5xx) are retried with exponential
  backoff and jitter, honouring ``Retry-After``. After exhaustion they are
  classified ``provider_unavailable`` - never contract drift.
* Fail-closed: a missing credential, a missing golden, or a golden that has not
  yet been recorded fails the run unless the provider id is named in the
  matching explicit allow list (with a reason) in the config.

Standard library plus httpx and PyYAML only (both already runtime deps).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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

#: A shape-diff kind that fails the run.
_BREAKING_KINDS = frozenset({"missing", "type_changed", "emptied", "populated"})

#: Methods that can never mutate a provider.
_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

#: The ONE host each provider id may target, pinned IN CODE. The loader rejects
#: a probe whose id is absent here, whose URL host differs, or whose config
#: ``allowed_host`` disagrees with the pin. A merged config edit therefore
#: cannot point an existing credential at a different host: adding a host is a
#: code change that has to be reviewed here.
PROVIDER_HOST_PINS: dict[str, str] = {
    # connectors
    "asana": "app.asana.com",
    "bitbucket": "api.bitbucket.org",
    "buildkite": "api.buildkite.com",
    "circleci": "circleci.com",
    "codeclimate": "api.codeclimate.com",
    "datadog": "api.datadoghq.com",
    "discord": "discord.com",
    "dropbox_paper": "api.dropboxapi.com",
    "github": "api.github.com",
    "gitlab": "gitlab.com",
    "linear": "api.linear.app",
    "microsoft_teams": "graph.microsoft.com",
    "monday": "api.monday.com",
    "notion": "api.notion.com",
    "npm": "registry.npmjs.org",
    "opsgenie": "api.opsgenie.com",
    "pagerduty": "api.pagerduty.com",
    "pypi": "pypi.org",
    "sentry": "sentry.io",
    "shortcut": "api.app.shortcut.com",
    "slack": "slack.com",
    "snyk": "api.snyk.io",
    "sonarqube": "sonarcloud.io",
    "trello": "api.trello.com",
    # model backends
    "anthropic": "api.anthropic.com",
    "cohere": "api.cohere.com",
    "deepseek": "api.deepseek.com",
    "fireworks": "api.fireworks.ai",
    "gemini": "generativelanguage.googleapis.com",
    "grok": "api.x.ai",
    "groq": "api.groq.com",
    "mistral": "api.mistral.ai",
    "openai": "api.openai.com",
    "openrouter": "openrouter.ai",
    "qwen": "dashscope.aliyuncs.com",
    "togetherai": "api.together.xyz",
}


@dataclass(frozen=True)
class ReadOnlyPost:
    """A POST that is a read query, pinned to its exact URL and body."""

    url: str
    body_kind: str  # "json" | "form" | "none"
    body: Any = None


#: The few providers whose cheapest read-only operation is an HTTP POST (a read
#: query, not a write), pinned IN CODE to their exact URL and body. The loader
#: rejects any POST whose URL or body differs from the pin, so a merged config
#: edit cannot rename a probe onto a write endpoint (e.g. Slack ``chat.delete``).
READ_ONLY_POST_ENDPOINTS: dict[str, ReadOnlyPost] = {
    "dropbox_paper": ReadOnlyPost("https://api.dropboxapi.com/2/users/get_current_account", "json", None),
    "linear": ReadOnlyPost("https://api.linear.app/graphql", "json", {"query": "{ viewer { id name email } }"}),
    "monday": ReadOnlyPost("https://api.monday.com/v2", "json", {"query": "{ me { id name email } }"}),
    "slack": ReadOnlyPost("https://slack.com/api/auth.test", "none"),
}


@dataclass(frozen=True)
class CredentialPin:
    """The ONLY credentials a provider id may read, pinned IN CODE.

    ``env_vars`` are the env var names the probe may read (``auth.env``, the
    values of ``auth.params`` and ``extra_headers``, and ``env_vars`` itself).
    ``headers`` and ``params`` are the header / query-parameter NAMES it may
    populate. Unknown names are rejected, so a config edit cannot repurpose a
    probe to read (and send) another provider's secret.
    """

    env_vars: frozenset[str] = frozenset()
    headers: frozenset[str] = frozenset()
    params: frozenset[str] = frozenset()


#: The credential set of every provider id, pinned IN CODE. ``_validate_env_vars``
#: only proves that what is USED is DECLARED, which a merged config edit can
#: satisfy while declaring ANOTHER provider's secret (a ``github`` probe with
#: ``auth.env: FARNA_PROBE_SLACK_TOKEN`` would load it and send Slack's token to
#: api.github.com). This pin makes the credential set a property of the provider
#: id instead of the config: every env var, header name and query-param name the
#: probe touches must be listed here. Adding a credential is a reviewed code
#: change, never a config-only edit.
PROVIDER_CREDENTIAL_PINS: dict[str, CredentialPin] = {
    # connectors
    "asana": CredentialPin(env_vars=frozenset({"FARNA_PROBE_ASANA_TOKEN"})),
    "bitbucket": CredentialPin(env_vars=frozenset({"FARNA_PROBE_BITBUCKET_TOKEN"})),
    "buildkite": CredentialPin(env_vars=frozenset({"FARNA_PROBE_BUILDKITE_TOKEN"})),
    "circleci": CredentialPin(env_vars=frozenset({"FARNA_PROBE_CIRCLECI_TOKEN"}), headers=frozenset({"Circle-Token"})),
    "codeclimate": CredentialPin(env_vars=frozenset({"FARNA_PROBE_CODECLIMATE_TOKEN"})),
    "datadog": CredentialPin(
        env_vars=frozenset({"FARNA_PROBE_DATADOG_API_KEY", "FARNA_PROBE_DATADOG_APP_KEY"}),
        headers=frozenset({"DD-API-KEY", "DD-APPLICATION-KEY"}),
    ),
    "discord": CredentialPin(env_vars=frozenset({"FARNA_PROBE_DISCORD_TOKEN"}), headers=frozenset({"Authorization"})),
    "dropbox_paper": CredentialPin(env_vars=frozenset({"FARNA_PROBE_DROPBOX_TOKEN"})),
    "github": CredentialPin(env_vars=frozenset({"FARNA_PROBE_GITHUB_TOKEN"})),
    "gitlab": CredentialPin(env_vars=frozenset({"FARNA_PROBE_GITLAB_TOKEN"}), headers=frozenset({"PRIVATE-TOKEN"})),
    "linear": CredentialPin(env_vars=frozenset({"FARNA_PROBE_LINEAR_TOKEN"}), headers=frozenset({"Authorization"})),
    "microsoft_teams": CredentialPin(env_vars=frozenset({"FARNA_PROBE_MICROSOFT_TEAMS_TOKEN"})),
    "monday": CredentialPin(env_vars=frozenset({"FARNA_PROBE_MONDAY_TOKEN"})),
    "notion": CredentialPin(env_vars=frozenset({"FARNA_PROBE_NOTION_TOKEN"})),
    "npm": CredentialPin(env_vars=frozenset({"FARNA_PROBE_NPM_TOKEN"})),
    "opsgenie": CredentialPin(env_vars=frozenset({"FARNA_PROBE_OPSGENIE_TOKEN"}), headers=frozenset({"Authorization"})),
    "pagerduty": CredentialPin(env_vars=frozenset({"FARNA_PROBE_PAGERDUTY_TOKEN"}), headers=frozenset({"Authorization"})),
    "pypi": CredentialPin(),
    "sentry": CredentialPin(env_vars=frozenset({"FARNA_PROBE_SENTRY_TOKEN"})),
    "shortcut": CredentialPin(env_vars=frozenset({"FARNA_PROBE_SHORTCUT_TOKEN"}), headers=frozenset({"Shortcut-Token"})),
    "slack": CredentialPin(env_vars=frozenset({"FARNA_PROBE_SLACK_TOKEN"})),
    "snyk": CredentialPin(env_vars=frozenset({"FARNA_PROBE_SNYK_TOKEN"}), headers=frozenset({"Authorization"})),
    "sonarqube": CredentialPin(env_vars=frozenset({"FARNA_PROBE_SONARQUBE_TOKEN"})),
    "trello": CredentialPin(
        env_vars=frozenset({"FARNA_PROBE_TRELLO_KEY", "FARNA_PROBE_TRELLO_TOKEN"}),
        params=frozenset({"key", "token"}),
    ),
    # model backends
    "anthropic": CredentialPin(env_vars=frozenset({"FARNA_PROBE_ANTHROPIC_TOKEN"}), headers=frozenset({"x-api-key"})),
    "cohere": CredentialPin(env_vars=frozenset({"FARNA_PROBE_COHERE_TOKEN"})),
    "deepseek": CredentialPin(env_vars=frozenset({"FARNA_PROBE_DEEPSEEK_TOKEN"})),
    "fireworks": CredentialPin(env_vars=frozenset({"FARNA_PROBE_FIREWORKS_TOKEN"})),
    "gemini": CredentialPin(env_vars=frozenset({"FARNA_PROBE_GEMINI_TOKEN"}), params=frozenset({"key"})),
    "grok": CredentialPin(env_vars=frozenset({"FARNA_PROBE_GROK_TOKEN"})),
    "groq": CredentialPin(env_vars=frozenset({"FARNA_PROBE_GROQ_TOKEN"})),
    "mistral": CredentialPin(env_vars=frozenset({"FARNA_PROBE_MISTRAL_TOKEN"})),
    "openai": CredentialPin(env_vars=frozenset({"FARNA_PROBE_OPENAI_TOKEN"})),
    "openrouter": CredentialPin(env_vars=frozenset({"FARNA_PROBE_OPENROUTER_TOKEN"})),
    "qwen": CredentialPin(env_vars=frozenset({"FARNA_PROBE_QWEN_TOKEN"})),
    "togetherai": CredentialPin(env_vars=frozenset({"FARNA_PROBE_TOGETHERAI_TOKEN"})),
}

#: A 429 or any 5xx means "cannot verify right now", never "the contract
#: changed". Retried, then reported as ``provider_unavailable``.
_RETRYABLE_STATUS = frozenset({429}) | frozenset(range(500, 600))

_DEFAULT_RETRY = {"attempts": 3, "base_delay_seconds": 1.0, "max_delay_seconds": 30.0}

_HEADER_NAME_CHARS = frozenset("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


class ProbeConfigError(Exception):
    """The probe configuration is malformed or internally inconsistent."""


class ProviderUnavailableError(Exception):
    """A provider could not be reached or answered transiently forever."""


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


def _mark_optional(shape: dict[str, Any]) -> dict[str, Any]:
    return {**shape, "optional": True}


def _merge_shapes(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any] | None:
    """Union two element shapes so one array covers every element it contains.

    A key missing from some elements is marked ``optional`` so its absence in a
    later response is not reported as drift.
    """
    if left is None:
        return right
    if right is None:
        return left

    left_type = left.get("type")
    right_type = right.get("type")
    if left_type != right_type:
        left_types = set(left.get("types") or [left_type])
        right_types = set(right.get("types") or [right_type])
        merged = left_types | right_types
        if len(merged) == 1:
            return {"type": merged.pop()}
        return {"type": "mixed", "types": sorted(merged)}

    if left_type == "object":
        left_keys = left.get("keys") or {}
        right_keys = right.get("keys") or {}
        keys: dict[str, Any] = {}
        for key in sorted(set(left_keys) | set(right_keys)):
            if key in left_keys and key in right_keys:
                keys[key] = _merge_shapes(left_keys[key], right_keys[key])
            elif key in left_keys:
                keys[key] = _mark_optional(left_keys[key])
            else:
                keys[key] = _mark_optional(right_keys[key])
        return {"type": "object", "keys": keys}

    if left_type == "array":
        return {"type": "array", "items": _merge_shapes(left.get("items"), right.get("items"))}

    return left


def shape_of(value: Any, depth: int, max_depth: int) -> dict[str, Any]:
    """Describe *value* as a shape tree down to *max_depth* levels.

    Arrays union the shape of *every* element, so a polymorphic listing is
    described in full rather than by an arbitrary first item.
    """
    node: dict[str, Any] = {"type": _type_name(value)}
    if depth >= max_depth:
        return node
    if isinstance(value, dict):
        node["keys"] = {key: shape_of(value[key], depth + 1, max_depth) for key in sorted(value)}
    elif isinstance(value, list):
        merged: dict[str, Any] | None = None
        for item in value:
            merged = _merge_shapes(merged, shape_of(item, depth + 1, max_depth))
        node["items"] = merged
    return node


def _shapes_equal(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """True when *actual* satisfies *expected* (used for ``mixed`` unions).

    Widening is drift, narrowing is not:

    * a concrete expected type is NEVER satisfied by a ``mixed`` actual - an
      array whose elements were all objects and now also holds a string or a
      null has changed type, even though ``object`` is one member of the union;
    * a ``mixed`` actual only satisfies a ``mixed`` expected whose type set is a
      SUPERSET of the actual one - the actual may lose a member (narrowing) but
      may never add one.
    """
    expected_type = expected.get("type")
    actual_type = actual.get("type")
    if expected_type == "mixed":
        expected_types = set(expected.get("types") or [])
        if actual_type == "mixed":
            return set(actual.get("types") or []) <= expected_types
        return actual_type in expected_types
    return expected_type == actual_type


def diff_shape(expected: dict[str, Any], actual: dict[str, Any], path: str = "$") -> list[dict[str, Any]]:
    """Diff two shape trees. Returns a list of drift records."""
    if not _shapes_equal(expected, actual):
        return [{"path": path, "kind": "type_changed", "expected": expected.get("type"), "actual": actual.get("type")}]

    expected_type = expected.get("type")
    if expected_type == "mixed":
        return []

    diffs: list[dict[str, Any]] = []
    if expected_type == "object":
        expected_keys = expected.get("keys", {})
        actual_keys = actual.get("keys", {})
        for key in sorted(expected_keys):
            child_path = f"{path}.{key}"
            if key not in actual_keys:
                if expected_keys[key].get("optional"):
                    continue
                diffs.append({"path": child_path, "kind": "missing", "expected": expected_keys[key].get("type"), "actual": None})
            else:
                diffs.extend(diff_shape(expected_keys[key], actual_keys[key], child_path))
        diffs.extend({"path": f"{path}.{key}", "kind": "added", "expected": None, "actual": actual_keys[key].get("type")} for key in sorted(set(actual_keys) - set(expected_keys)))
    elif expected_type == "array":
        expected_items = expected.get("items")
        actual_items = actual.get("items")
        if expected_items and not actual_items:
            # The array is now empty where items were pinned: a listing silently
            # lost its payload, which is a breaking change, not "no items today".
            diffs.append({"path": f"{path}[]", "kind": "emptied", "expected": expected_items.get("type"), "actual": None})
        elif actual_items and not expected_items:
            diffs.append({"path": f"{path}[]", "kind": "populated", "expected": None, "actual": actual_items.get("type")})
        elif expected_items and actual_items:
            diffs.extend(diff_shape(expected_items, actual_items, f"{path}[]"))
    return diffs


def path_in_shape(shape: Any, tokens: list[str]) -> bool:
    """True when a ``normalise.path`` resolves against a recorded shape."""
    if not tokens:
        return isinstance(shape, dict)
    if not isinstance(shape, dict):
        return False
    token = tokens[0]
    rest = tokens[1:]
    if token == "*":
        keys = shape.get("keys") or {}
        return bool(keys) and all(path_in_shape(child, rest) for child in keys.values())
    if token == "[]":
        items = shape.get("items")
        return isinstance(items, dict) and path_in_shape(items, rest)
    keys = shape.get("keys")
    if not isinstance(keys, dict) or token not in keys:
        return False
    return path_in_shape(keys[token], rest)


# ---------------------------------------------------------------------------
# Config loading / validation
# ---------------------------------------------------------------------------


def _valid_status_class(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 3 and value.endswith("xx") and value[0].isdigit()


def _validate_allow_list(config: dict[str, Any], name: str) -> dict[str, str]:
    raw = config.get(name) or {}
    if not isinstance(raw, dict):
        raise ProbeConfigError(f"'{name}' must be a mapping of provider id -> reason")
    result: dict[str, str] = {}
    for provider_id, reason in raw.items():
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ProbeConfigError(f"{name}: provider id must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ProbeConfigError(f"{name}: {provider_id} needs a non-empty reason")
        result[provider_id] = reason
    return result


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
    if not providers and not skips:
        raise ProbeConfigError("config declares no providers and no skips")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ProbeConfigError("'defaults' must be a mapping")
    _validate_defaults(defaults)

    for name in ("allow_missing_credential", "allow_missing_golden", "allow_unrecorded_golden"):
        data[name] = _validate_allow_list(data, name)

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

    for name in ("allow_missing_credential", "allow_missing_golden", "allow_unrecorded_golden"):
        for provider_id in data[name]:
            if provider_id not in seen:
                raise ProbeConfigError(f"{name} names an unknown provider id: {provider_id}")

    return data


def _validate_defaults(defaults: dict[str, Any]) -> None:
    status_class = defaults.get("success_status_class", "2xx")
    if not _valid_status_class(status_class):
        raise ProbeConfigError(f"defaults: invalid success_status_class {status_class!r}")
    timeout = defaults.get("timeout_seconds", 20)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProbeConfigError("defaults: timeout_seconds must be a positive number")
    max_depth = defaults.get("max_depth", 1)
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ProbeConfigError("defaults: max_depth must be a non-negative integer")
    if not isinstance(defaults.get("strict_shape", True), bool):
        raise ProbeConfigError("defaults: strict_shape must be a boolean")
    deadline = defaults.get("deadline_seconds", 1500)
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or deadline <= 0:
        raise ProbeConfigError("defaults: deadline_seconds must be a positive number")
    retry = defaults.get("retry", _DEFAULT_RETRY)
    if not isinstance(retry, dict):
        raise ProbeConfigError("defaults: retry must be a mapping")
    attempts = retry.get("attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise ProbeConfigError("defaults: retry.attempts must be a positive integer")
    for field in ("base_delay_seconds", "max_delay_seconds"):
        value = retry.get(field, _DEFAULT_RETRY[field])
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ProbeConfigError(f"defaults: retry.{field} must be a non-negative number")


def _require_skip_fields(skip: Any) -> None:
    if not isinstance(skip, dict):
        raise ProbeConfigError("each skip must be a mapping")
    for field in ("id", "kind", "reason"):
        if not isinstance(skip.get(field), str) or not skip[field].strip():
            raise ProbeConfigError(f"skip is missing a non-empty {field!r}: {skip!r}")


def _validate_header_name(name: Any, where: str) -> None:
    if not isinstance(name, str) or not name or any(char not in _HEADER_NAME_CHARS for char in name):
        raise ProbeConfigError(f"{where}: invalid header name {name!r}")


def _validate_template(template: Any, where: str) -> None:
    if not isinstance(template, str):
        raise ProbeConfigError(f"{where}: template must be a string")
    placeholders: list[str] = []
    index = 0
    while True:
        start = template.find("{", index)
        if start == -1:
            break
        end = template.find("}", start)
        if end == -1:
            raise ProbeConfigError(f"{where}: unterminated template placeholder")
        placeholders.append(template[start + 1 : end])
        index = end + 1
    if any(placeholder != "token" for placeholder in placeholders):
        raise ProbeConfigError(f"{where}: template may only reference '{{token}}'")
    if not placeholders:
        raise ProbeConfigError(f"{where}: template must contain '{{token}}'")


def _validate_probe(probe: Any, defaults: dict[str, Any]) -> None:
    if not isinstance(probe, dict):
        raise ProbeConfigError("each provider must be a mapping")
    for field in ("id", "kind", "operation", "description"):
        if not isinstance(probe.get(field), str) or not probe[field].strip():
            raise ProbeConfigError(f"probe is missing a non-empty {field!r}: {probe!r}")
    if probe["kind"] not in ("connector", "model_backend"):
        raise ProbeConfigError(f"unknown kind {probe['kind']!r} for {probe['id']}")

    _validate_request(probe)
    _validate_auth(probe)
    _validate_env_vars(probe)
    _validate_credential_pins(probe)

    status_class = probe.get("success_status_class", defaults.get("success_status_class", "2xx"))
    if not _valid_status_class(status_class):
        raise ProbeConfigError(f"{probe['id']}: invalid success_status_class {status_class!r}")

    max_depth = probe.get("max_depth", defaults.get("max_depth", 1))
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ProbeConfigError(f"{probe['id']}: max_depth must be a non-negative integer")

    timeout = probe.get("timeout_seconds", defaults.get("timeout_seconds", 20))
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ProbeConfigError(f"{probe['id']}: timeout_seconds must be a positive number")

    if not isinstance(probe.get("strict_shape", defaults.get("strict_shape", True)), bool):
        raise ProbeConfigError(f"{probe['id']}: strict_shape must be a boolean")

    rules = probe.get("normalise") or []
    if not isinstance(rules, list):
        raise ProbeConfigError(f"{probe['id']}: 'normalise' must be a list")
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("path"), str) or not rule["path"].strip() or rule.get("rule") not in ("drop", "volatile", "nullable"):
            raise ProbeConfigError(f"{probe['id']}: invalid normalise rule {rule!r}")


def _request_body(request: dict[str, Any]) -> tuple[str, Any]:
    """Return the request body as (kind, payload) for exact comparison."""
    if "json" in request:
        return "json", request["json"]
    if "form" in request:
        return "form", request["form"]
    return "none", None


def _validate_read_only_post(probe: dict[str, Any], request: dict[str, Any], url: str) -> None:
    """Reject a POST whose id, URL or body is not the pinned read-only call."""
    pinned = READ_ONLY_POST_ENDPOINTS.get(probe["id"])
    if pinned is None:
        raise ProbeConfigError(f"{probe['id']}: POST is only allowed for the endpoints pinned in code (READ_ONLY_POST_ENDPOINTS); use GET/HEAD or add the endpoint there deliberately")
    if url != pinned.url:
        raise ProbeConfigError(f"{probe['id']}: POST url {url!r} does not match the endpoint pinned in code ({pinned.url!r})")
    kind, body = _request_body(request)
    if kind != pinned.body_kind or body != pinned.body:
        raise ProbeConfigError(f"{probe['id']}: POST body ({kind}) does not match the body pinned in code")


def _validate_request(probe: dict[str, Any]) -> None:
    request = probe.get("request")
    if not isinstance(request, dict):
        raise ProbeConfigError(f"{probe['id']}: 'request' must be a mapping")

    method = request.get("method")
    if not isinstance(method, str) or method not in _READ_ONLY_METHODS | {"POST"}:
        raise ProbeConfigError(f"{probe['id']}: request.method must be GET, HEAD or POST")

    if "json" in request and "form" in request:
        raise ProbeConfigError(f"{probe['id']}: request cannot set both json and form")
    if "form" in request and not isinstance(request["form"], dict):
        raise ProbeConfigError(f"{probe['id']}: request.form must be a mapping")

    url = request.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ProbeConfigError(f"{probe['id']}: request.url must be an https URL")
    parts = urlsplit(url)
    if parts.username or parts.password or "@" in parts.netloc:
        raise ProbeConfigError(f"{probe['id']}: request.url must not embed credentials")
    if parts.hostname is None:
        raise ProbeConfigError(f"{probe['id']}: request.url has no host")
    if parts.query:
        raise ProbeConfigError(f"{probe['id']}: request.url must not carry a query string; use auth.params")

    pinned_host = PROVIDER_HOST_PINS.get(probe["id"])
    if pinned_host is None:
        raise ProbeConfigError(f"{probe['id']}: no host is pinned for this provider id in scripts/provider_contract_probe.py (PROVIDER_HOST_PINS); add the pin there deliberately")
    if parts.hostname.lower() != pinned_host.lower():
        raise ProbeConfigError(f"{probe['id']}: request.url host {parts.hostname!r} does not match the host pinned in code ({pinned_host!r})")

    # ``allowed_host`` is a visible declaration in the config, but the code pin
    # is the authority: the two must agree, so changing both together is still
    # rejected unless the pin in this module changes (a reviewed code change).
    allowed_host = probe.get("allowed_host")
    if not isinstance(allowed_host, str) or not allowed_host.strip():
        raise ProbeConfigError(f"{probe['id']}: allowed_host is required")
    if allowed_host.lower() != pinned_host.lower():
        raise ProbeConfigError(f"{probe['id']}: allowed_host {allowed_host!r} does not match the host pinned in code ({pinned_host!r})")

    if method == "POST":
        _validate_read_only_post(probe, request, url)


def _validate_auth(probe: dict[str, Any]) -> None:
    auth = probe.get("auth")
    if not isinstance(auth, dict):
        raise ProbeConfigError(f"{probe['id']}: 'auth' must be a mapping")
    kind = auth.get("kind")
    if kind not in ("none", "bearer", "header", "query"):
        raise ProbeConfigError(f"{probe['id']}: unknown auth kind {kind!r}")
    if kind in ("bearer", "header") and (not isinstance(auth.get("env"), str) or not auth["env"]):
        raise ProbeConfigError(f"{probe['id']}: auth.env is required for {kind} auth")
    if kind == "header":
        _validate_header_name(auth.get("header", "Authorization"), f"{probe['id']}: auth.header")
        if "template" in auth:
            _validate_template(auth["template"], f"{probe['id']}: auth.template")
    if kind == "query":
        params = auth.get("params")
        if not isinstance(params, dict) or not params:
            raise ProbeConfigError(f"{probe['id']}: auth.params must be a non-empty mapping for query auth")
        for name, env in params.items():
            if not isinstance(name, str) or not name or not isinstance(env, str) or not env:
                raise ProbeConfigError(f"{probe['id']}: auth.params entries must map a name to an env var")

    extra = probe.get("extra_headers") or {}
    if not isinstance(extra, dict):
        raise ProbeConfigError(f"{probe['id']}: 'extra_headers' must be a mapping")
    for header_name, env in extra.items():
        _validate_header_name(header_name, f"{probe['id']}: extra_headers")
        if not isinstance(env, str) or not env:
            raise ProbeConfigError(f"{probe['id']}: extra_headers values must be env var names")

    static = probe.get("static_headers") or {}
    if not isinstance(static, dict):
        raise ProbeConfigError(f"{probe['id']}: 'static_headers' must be a mapping")
    for header_name, value in static.items():
        _validate_header_name(header_name, f"{probe['id']}: static_headers")
        if not isinstance(value, str):
            raise ProbeConfigError(f"{probe['id']}: static_headers values must be strings")


def _used_env_vars(probe: dict[str, Any]) -> set[str]:
    auth = probe["auth"]
    used: set[str] = set()
    if auth["kind"] in ("bearer", "header"):
        used.add(auth["env"])
    elif auth["kind"] == "query":
        used.update(auth["params"].values())
    used.update((probe.get("extra_headers") or {}).values())
    return used


def _validate_env_vars(probe: dict[str, Any]) -> None:
    env_vars = probe.get("env_vars")
    if not isinstance(env_vars, list) or any(not isinstance(name, str) or not name for name in env_vars):
        raise ProbeConfigError(f"{probe['id']}: 'env_vars' must be a list of env var names")
    if len(env_vars) != len(set(env_vars)):
        raise ProbeConfigError(f"{probe['id']}: 'env_vars' contains duplicates")
    declared = set(env_vars)
    undeclared = sorted(_used_env_vars(probe) - declared)
    if undeclared:
        raise ProbeConfigError(f"{probe['id']}: credential env vars not declared in env_vars: {', '.join(undeclared)}")
    for name in declared:
        if not name.replace("_", "").isalnum() or name.upper() != name:
            raise ProbeConfigError(f"{probe['id']}: invalid env var name {name!r}")


def _format_pinned(names: frozenset[str]) -> str:
    return ", ".join(sorted(names)) or "none"


def _validate_credential_pins(probe: dict[str, Any]) -> None:
    """Reject any credential name outside the provider's code pin.

    Used-vs-declared is not enough: a merged config edit can declare ANOTHER
    provider's secret and have ``_resolve_credentials`` read it, sending (e.g.)
    Slack's token to api.github.com from a ``github`` probe. ``env_vars``, every
    used env var, every header name and every query-param name must fall inside
    the provider's ``CredentialPin``, so the credential set belongs to the
    provider id, not to the config.
    """
    pin = PROVIDER_CREDENTIAL_PINS.get(probe["id"])
    if pin is None:
        raise ProbeConfigError(f"{probe['id']}: no credential pin exists for this provider id in scripts/provider_contract_probe.py (PROVIDER_CREDENTIAL_PINS); add the pin there deliberately")

    used_outside = sorted(_used_env_vars(probe) - pin.env_vars)
    if used_outside:
        raise ProbeConfigError(f"{probe['id']}: credential env vars {', '.join(used_outside)} are not permitted for this provider (pinned in code: {_format_pinned(pin.env_vars)}); a config edit cannot read another provider's credential")

    declared_outside = sorted(set(probe["env_vars"]) - pin.env_vars)
    if declared_outside:
        raise ProbeConfigError(f"{probe['id']}: env_vars {', '.join(declared_outside)} are not permitted for this provider (pinned in code: {_format_pinned(pin.env_vars)})")

    auth = probe["auth"]
    if auth["kind"] == "header":
        header = auth.get("header", "Authorization")
        if header not in pin.headers:
            raise ProbeConfigError(f"{probe['id']}: auth.header {header!r} is not permitted for this provider (pinned in code: {_format_pinned(pin.headers)})")
    if auth["kind"] == "query":
        for name in auth["params"]:
            if name not in pin.params:
                raise ProbeConfigError(f"{probe['id']}: auth.params name {name!r} is not permitted for this provider (pinned in code: {_format_pinned(pin.params)})")

    for header_name in probe.get("extra_headers") or {}:
        if header_name not in pin.headers:
            raise ProbeConfigError(f"{probe['id']}: extra_headers name {header_name!r} is not permitted for this provider (pinned in code: {_format_pinned(pin.headers)})")


def _probe_settings(probe: dict[str, Any], defaults: dict[str, Any]) -> tuple[str, int]:
    status_class = probe.get("success_status_class", defaults.get("success_status_class", "2xx"))
    max_depth = probe.get("max_depth", defaults.get("max_depth", 1))
    return status_class, max_depth


def _probe_timeout(probe: dict[str, Any], defaults: dict[str, Any]) -> float:
    return float(probe.get("timeout_seconds", defaults.get("timeout_seconds", 20)))


def _retry_settings(defaults: dict[str, Any]) -> tuple[int, float, float]:
    retry = defaults.get("retry") or {}
    attempts = int(retry.get("attempts", _DEFAULT_RETRY["attempts"]))
    base = float(retry.get("base_delay_seconds", _DEFAULT_RETRY["base_delay_seconds"]))
    max_delay = float(retry.get("max_delay_seconds", _DEFAULT_RETRY["max_delay_seconds"]))
    return attempts, base, max_delay


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


def _is_compact(node: Any) -> bool:
    if isinstance(node, dict):
        return all(not isinstance(value, (dict, list)) for value in node.values())
    if isinstance(node, list):
        return all(not isinstance(value, (dict, list)) or (isinstance(value, dict) and _is_compact(value)) for value in node)
    return True


def _format_json(node: Any, level: int) -> str:
    """Format like the committed goldens: expanded structures, inline leaves."""
    if _is_compact(node):
        return json.dumps(node, ensure_ascii=False, separators=(", ", ": "))
    pad = "  " * level
    inner = "  " * (level + 1)
    if isinstance(node, dict):
        if not node:
            return "{}"
        parts = [f"{inner}{json.dumps(key, ensure_ascii=False)}: {_format_json(value, level + 1)}" for key, value in node.items()]
        return "{\n" + ",\n".join(parts) + f"\n{pad}}}"
    if isinstance(node, list):
        if not node:
            return "[]"
        parts = [f"{inner}{_format_json(value, level + 1)}" for value in node]
        return "[\n" + ",\n".join(parts) + f"\n{pad}]"
    return json.dumps(node, ensure_ascii=False)


def _write_goldens(path: Path, manifest: dict[str, Any]) -> None:
    """Write the manifest in the compact inline-leaf format the repo commits.

    Rewriting in one canonical format keeps a single-provider re-record to a
    single-provider diff: the rest of the file serialises identically.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_format_json(manifest, 0) + "\n", encoding="utf-8")
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


def _call(client: httpx.Client, probe: dict[str, Any], headers: dict[str, str], params: dict[str, str], timeout: float) -> httpx.Response:
    request = probe["request"]
    call_headers = dict(headers)
    kwargs: dict[str, Any] = {"timeout": timeout}
    if "json" in request:
        payload = request["json"]
        if payload is None:
            # An explicit JSON ``null`` body (e.g. Dropbox no-argument RPCs).
            # httpx treats ``json=None`` as "no body", so send it literally.
            kwargs["content"] = b"null"
            call_headers.setdefault("Content-Type", "application/json")
        else:
            kwargs["json"] = payload
    elif "form" in request:
        kwargs["data"] = dict(request["form"])
    return client.request(request["method"], request["url"], headers=call_headers, params=params, **kwargs)


def _jitter(scale: float) -> float:
    if scale <= 0:
        return 0.0
    # os.urandom keeps this free of the (non-cryptographic) random module; jitter
    # only has to de-correlate retries across concurrent runs.
    return (int.from_bytes(os.urandom(4), "big") / 2**32) * scale


def _backoff_delay(attempt: int, retry_after: float | None, base: float, max_delay: float) -> float:
    if retry_after is not None:
        return min(max(retry_after, 0.0), max_delay)
    exponential = base * (2 ** (attempt - 1))
    return min(exponential + _jitter(exponential), max_delay)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


#: Indirection so tests can run the retry path without real sleeping.
_sleep = time.sleep


def _call_with_retries(
    client: httpx.Client,
    probe: dict[str, Any],
    defaults: dict[str, Any],
    headers: dict[str, str],
    params: dict[str, str],
    deadline: float,
) -> tuple[httpx.Response, int]:
    attempts, base, max_delay = _retry_settings(defaults)
    timeout = _probe_timeout(probe, defaults)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if time.monotonic() >= deadline:
            raise ProviderUnavailableError("overall deadline exceeded before request")
        try:
            response = _call(client, probe, headers, params, timeout)
        except httpx.TransportError as exc:
            last_error = exc
            if attempt >= attempts:
                raise ProviderUnavailableError(f"transport error after {attempts} attempt(s): {exc}") from exc
            _sleep(_backoff_delay(attempt, None, base, max_delay))
            continue
        if response.status_code in _RETRYABLE_STATUS and attempt < attempts:
            _sleep(_backoff_delay(attempt, _retry_after_seconds(response), base, max_delay))
            continue
        return response, attempt
    raise ProviderUnavailableError(f"request failed after {attempts} attempt(s): {last_error}")


def _shape_from_response(
    probe: dict[str, Any],
    defaults: dict[str, Any],
    response: httpx.Response,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Shape *response*, at *max_depth* when given (check mode) else the config depth."""
    if max_depth is None:
        _, max_depth = _probe_settings(probe, defaults)
    payload = response.json()
    normalised = normalise(payload, probe.get("normalise") or [])
    return shape_of(normalised, 0, max_depth)


def _golden_depth(golden: dict[str, Any], probe: dict[str, Any], defaults: dict[str, Any]) -> int:
    """Return the depth to shape the ACTUAL response at in check mode.

    The golden's recorded ``max_depth`` governs the comparison, mirroring the
    recorded ``success_status_class``: it is the depth the golden's shape was
    actually built at, and shaping the actual at a different depth makes that
    metadata dead. Rebuilding at the config depth reports every key below it as
    phantom ``added`` drift (a shallower golden) or as ``missing`` drift (a
    deeper one), so editing the documented ``max_depth`` knob would turn every
    probe into false drift and pressure a re-record. Fall back to the config
    depth only for a golden that records no usable depth.
    """
    recorded = golden.get("max_depth")
    if isinstance(recorded, int) and not isinstance(recorded, bool) and recorded >= 0:
        return recorded
    _, configured = _probe_settings(probe, defaults)
    return configured


def _row(probe: dict[str, Any], status: str, *, attempted: bool = False, **fields: Any) -> dict[str, Any]:
    return {"provider": probe["id"], "operation": probe["operation"], "status": status, "attempted": attempted, **fields}


def _skip(probe: dict[str, Any], reason: str, *, allowed: bool) -> dict[str, Any]:
    print(f"{'NOTE' if allowed else 'WARNING'}: skipping {_golden_key(probe['id'], probe['operation'])} - {reason}", file=sys.stderr)
    return _row(probe, "skipped", attempted=False, reason=reason, allowed=allowed)


def _check_one(
    client: httpx.Client,
    probe: dict[str, Any],
    defaults: dict[str, Any],
    manifest: dict[str, Any],
    allow: dict[str, dict[str, str]],
    deadline: float,
) -> dict[str, Any]:
    secrets: list[str] = []
    provider_id = probe["id"]
    key = _golden_key(provider_id, probe["operation"])
    #: True once a request is actually issued. A run that issues none cannot be
    #: green, however many of its skips are explicitly excused.
    attempted = False
    try:
        golden = manifest["goldens"].get(key)
        if golden is None:
            return _skip(probe, f"no golden recorded for {key}; run --mode record behind review", allowed=provider_id in allow["missing_golden"])
        if golden.get("recorded_at") in (None, ""):
            return _skip(probe, f"golden for {key} is not yet recorded; record it behind review", allowed=provider_id in allow["unrecorded_golden"])
        if not isinstance(golden.get("shape"), dict):
            return _row(probe, "error", attempted=False, error="malformed golden entry (no shape)")

        headers, params, secrets, missing = _resolve_credentials(probe)
        if missing:
            return _skip(probe, "missing credential: " + ", ".join(missing), allowed=provider_id in allow["missing_credential"])

        expected_class = golden.get("success_status_class") or _probe_settings(probe, defaults)[0]
        attempted = True
        response, attempts = _call_with_retries(client, probe, defaults, headers, params, deadline)

        actual_class = f"{response.status_code // 100}xx"
        if actual_class != expected_class:
            if response.status_code in _RETRYABLE_STATUS:
                return _row(probe, "unavailable", attempted=True, status_code=response.status_code, attempts=attempts, error="provider_unavailable")
            return _row(probe, "error", attempted=True, status_code=response.status_code, attempts=attempts, error=f"status class {actual_class} != expected {expected_class}")

        try:
            actual_shape = _shape_from_response(probe, defaults, response, max_depth=_golden_depth(golden, probe, defaults))
        except ValueError as exc:
            return _row(probe, "error", attempted=True, status_code=response.status_code, error=_scrub(f"non-JSON response: {exc}", secrets))

        diffs = diff_shape(golden["shape"], actual_shape)
        breaking = [diff for diff in diffs if diff["kind"] in _BREAKING_KINDS]
        additions = [diff for diff in diffs if diff["kind"] == "added"]
        strict = bool(probe.get("strict_shape", defaults.get("strict_shape", True)))
        is_drift = bool(breaking) or (strict and bool(additions))
        return _row(
            probe,
            "drift" if is_drift else "pass",
            attempted=True,
            status_code=response.status_code,
            attempts=attempts,
            drift=breaking,
            additions=additions,
        )
    except ProviderUnavailableError as exc:
        return _row(probe, "unavailable", attempted=attempted, error=_scrub(str(exc), secrets))
    except Exception as exc:  # a single probe must never abort the whole run
        return _row(probe, "error", attempted=attempted, error=_scrub(f"{type(exc).__name__}: {exc}", secrets))


def _record_one(
    client: httpx.Client,
    probe: dict[str, Any],
    defaults: dict[str, Any],
    manifest: dict[str, Any],
    goldens_path: Path,
    deadline: float,
) -> int:
    key = _golden_key(probe["id"], probe["operation"])
    secrets: list[str] = []
    try:
        headers, params, secrets, missing = _resolve_credentials(probe)
        if missing:
            print(f"ERROR: cannot record {key} - missing credential: {', '.join(missing)}", file=sys.stderr)
            return 2

        expected_class, max_depth = _probe_settings(probe, defaults)
        response, _ = _call_with_retries(client, probe, defaults, headers, params, deadline)

        actual_class = f"{response.status_code // 100}xx"
        if actual_class != expected_class:
            print(f"ERROR: cannot record {key} - status class {actual_class} != expected {expected_class}", file=sys.stderr)
            return 1

        try:
            shape = _shape_from_response(probe, defaults, response)
        except ValueError as exc:
            print(f"ERROR: cannot record {key} - non-JSON response: {_scrub(str(exc), secrets)}", file=sys.stderr)
            return 1
    except ProviderUnavailableError as exc:
        print(f"ERROR: cannot record {key} - {_scrub(str(exc), secrets)}", file=sys.stderr)
        return 1
    except Exception as exc:  # report any failure, never crash the recorder
        print(f"ERROR: cannot record {key} - {_scrub(f'{type(exc).__name__}: {exc}', secrets)}", file=sys.stderr)
        return 1

    _record_golden(manifest, key, expected_class, max_depth, shape)
    _write_goldens(goldens_path, manifest)
    print(f"RECORDED {key} -> {goldens_path}")
    return 0


def _dry_run(probes: list[dict[str, Any]], defaults: dict[str, Any], manifest: dict[str, Any]) -> int:
    for probe in probes:
        key = _golden_key(probe["id"], probe["operation"])
        request = probe["request"]
        safe_url = _safe_url(request["url"], _query_secret_param_names(probe))
        _, _, _, missing = _resolve_credentials(probe)
        credential_state = "MISSING" if missing else "present"
        golden = manifest["goldens"].get(key)
        if golden is None:
            golden_state = "MISSING"
        elif golden.get("recorded_at") in (None, ""):
            golden_state = "UNRECORDED"
        else:
            golden_state = "recorded"
        print(f"DRY-RUN {key}: {request['method']} {safe_url} auth={probe['auth']['kind']} credential={credential_state} golden={golden_state}")
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _build_report(mode: str, config_path: Path, goldens_path: Path, results: list[dict[str, Any]], skips: list[dict[str, Any]]) -> dict[str, Any]:
    skipped = [r for r in results if r["status"] == "skipped"]
    skipped_unexcused = [r for r in skipped if not r.get("allowed")]
    summary = {
        "total": len(results),
        #: Probes that actually issued a request. Zero is always a failed run in
        #: check mode: excused skips do not make an empty run green.
        "attempted": sum(1 for r in results if r.get("attempted")),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "drifted": sum(1 for r in results if r["status"] == "drift"),
        "skipped": len(skipped),
        "skipped_unexcused": len(skipped_unexcused),
        "unavailable": sum(1 for r in results if r["status"] == "unavailable"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "additions": sum(len(r.get("additions", [])) for r in results),
        "expected": len(results) - len(skipped),
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
            label = "SKIP" if result.get("allowed") else "FAIL-SKIP"
            print(f"{label} {key}: {result.get('reason', '')}")
        elif status == "unavailable":
            print(f"UNAVAILABLE {key}: {result.get('error', '')}")
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


def _load_manifest_or_exit(path: Path) -> dict[str, Any] | int:
    try:
        return _load_goldens(path)
    except (ProbeConfigError, json.JSONDecodeError, OSError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
        config = load_config(args.config)
    except ProbeConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (OSError, yaml.YAMLError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    defaults = config.get("defaults") or {}
    probes: list[dict[str, Any]] = config["providers"]
    skips: list[dict[str, Any]] = config["skips"]
    allow = {
        "missing_credential": config["allow_missing_credential"],
        "missing_golden": config["allow_missing_golden"],
        "unrecorded_golden": config["allow_unrecorded_golden"],
    }

    manifest = _load_manifest_or_exit(args.goldens)
    if isinstance(manifest, int):
        return manifest

    if args.dry_run:
        return _dry_run(probes, defaults, manifest)

    deadline = time.monotonic() + float(defaults.get("deadline_seconds", 1500))

    if args.mode == "record":
        if not args.provider or not args.operation:
            print("error: --mode record requires --provider and --operation", file=sys.stderr)
            return 2
        selected = [p for p in probes if p["id"] == args.provider and p["operation"] == args.operation]
        if not selected:
            print(f"error: no probe configured for {args.provider}/{args.operation}", file=sys.stderr)
            return 2
        with httpx.Client(follow_redirects=False) as client:
            return _record_one(client, selected[0], defaults, manifest, args.goldens, deadline)

    selected = [p for p in probes if not args.provider or p["id"] == args.provider]
    results: list[dict[str, Any]] = []
    with httpx.Client(follow_redirects=False) as client:
        for probe in selected:
            results.append(_check_one(client, probe, defaults, manifest, allow, deadline))
            # Write after every probe so a killed run still leaves a usable report.
            _write_json(args.report, _build_report(args.mode, args.config, args.goldens, results, skips))

    _print_results(results)
    report = _build_report(args.mode, args.config, args.goldens, results, skips)
    _write_json(args.report, report)

    summary = report["summary"]
    print(
        f"summary: {summary['attempted']} attempted, {summary['passed']} passed, {summary['drifted']} drifted, {summary['unavailable']} unavailable, {summary['skipped']} skipped ({summary['skipped_unexcused']} unexcused), {summary['errors']} errors, {summary['additions']} additive changes; report -> {args.report}",
        file=sys.stderr,
    )

    if args.report_only:
        return 0
    failures = summary["drifted"] + summary["errors"] + summary["unavailable"] + summary["skipped_unexcused"]
    if failures:
        return 1
    if summary["attempted"] == 0:
        # Counting PASSES let an all-excused run look clean (passed:0,
        # expected:0). Counting ATTEMPTS cannot: a check that probed nothing
        # proves nothing, so it is a failure with an explanation.
        print(
            "error: zero probes were attempted - a check run that probes nothing cannot be green; record goldens behind review and provide their credentials (every excused skip is still an unproven contract)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
