"""Contract round-trip: live response vs OpenAPI schema vs generated frontend type (FAR-2702).

A test that fakes the transport can only prove our own assumption. This module
closes one narrow, high-value loop: for a small, explicitly listed sample of
GET routes it calls the REAL endpoint through the ASGI app against Postgres and
checks the LIVE response against two independent statements of the contract:

1. the app's own OpenAPI schema (the same document ``/openapi.json`` serves and
   ``frontend/src/lib/api/schema.ts`` is generated from), and
2. the generated frontend type in ``frontend/src/lib/api/schema.ts`` — the file
   the SPA compiles against.

The sample is deliberately tiny and deterministic. It is not an attempt to
cover every route: a broad sweep belongs to the schema-freshness gate; this is
the executable proof that a wire key and its declared type agree end-to-end.

A negative case proves the checker actually catches the defect class it exists
for: a camelCase key where the contract declares snake_case (and vice versa) is
reported, never silently accepted.

The limitation is real and documented in ``docs/testing/transport-policy.md``:
this catches OUR drift. It cannot see a provider changing its payload — that is
FAR-2703.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from farna.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_SCHEMA_TS = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "api" / "schema.ts"

#: The sample. Every entry must keep a response model AND a generated
#: frontend type; ``test_sampled_routes_have_a_generated_frontend_type``
#: enforces that, so a route that loses either drops out loudly.
SAMPLED_ROUTES: tuple[dict[str, str], ...] = (
    {"path": "/api/v1/me", "method": "get", "operation_id": "me_api_v1_me_get"},
    {"path": "/api/v1/auth/me", "method": "get", "operation_id": "me_api_v1_auth_me_get"},
    {"path": "/api/v1/me/settings", "method": "get", "operation_id": "get_user_settings_api_v1_me_settings_get"},
)

_TS_PROP_RE = re.compile(r"([A-Za-z_$][\w$]*)(\?)?:")


# ---------------------------------------------------------------------------
# OpenAPI side
# ---------------------------------------------------------------------------
def _resolve_ref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` (one hop is enough for a response model)."""
    ref = node.get("$ref")
    if not ref:
        return node
    target: Any = schema
    for part in ref.lstrip("#/").split("/"):
        target = target[part]
    return target


def _json_type_matches(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return True


def wire_mismatches(schema: dict[str, Any], node: dict[str, Any], value: Any, pointer: str = "") -> list[str]:
    """Every way *value* disagrees with *node*, as ``pointer: message`` lines.

    Checks: declared type, required keys present, no undeclared keys, and the
    same rules recursively for object properties and array items. ``anyOf`` /
    ``oneOf`` pass when any branch accepts the value.
    """
    node = _resolve_ref(schema, node)
    pointer = pointer or "response"

    for combinator in ("anyOf", "oneOf"):
        branches = node.get(combinator)
        if branches:
            if any(not wire_mismatches(schema, branch, value, pointer) for branch in branches):
                return []
            return [f"{pointer}: matches none of the declared {combinator} branches"]

    problems: list[str] = []
    expected = node.get("type")
    if isinstance(expected, list):
        if not any(_json_type_matches(kind, value) for kind in expected):
            problems.append(f"{pointer}: {type(value).__name__} is none of {expected}")
            return problems
        expected = next(kind for kind in expected if _json_type_matches(kind, value))

    if expected is not None and not _json_type_matches(expected, value):
        problems.append(f"{pointer}: expected {expected}, got {type(value).__name__}")
        return problems

    if expected == "object" and isinstance(value, dict):
        properties: dict[str, Any] = node.get("properties", {})
        required = node.get("required", [])
        for key in required:
            if key not in value:
                problems.append(f"{pointer}.{key}: required by the contract, missing from the response")
        if node.get("additionalProperties") in (None, False):
            for key in sorted(set(value) - set(properties)):
                problems.append(f"{pointer}.{key}: present in the response, undeclared by the contract")
        for key in sorted(set(value) & set(properties)):
            problems.extend(wire_mismatches(schema, properties[key], value[key], f"{pointer}.{key}"))

    if expected == "array" and isinstance(value, list):
        items = node.get("items", {})
        for index, item in enumerate(value):
            problems.extend(wire_mismatches(schema, items, item, f"{pointer}[{index}]"))

    return problems


# ---------------------------------------------------------------------------
# Generated frontend type side
# ---------------------------------------------------------------------------
def _ts_operation_response_type(text: str, operation_id: str) -> str | None:
    """The named ``components["schemas"][...]`` type used by *operation_id*'s 200."""
    start = text.find(f"    {operation_id}: {{")
    if start < 0:
        return None
    window = text[start : start + 6000]
    match = re.search(r'"application/json": components\["schemas"\]\["([^"]+)"\]', window)
    return match.group(1) if match else None


def _ts_interface_props(text: str, type_name: str) -> dict[str, bool]:
    """Top-level property names of a generated ``components["schemas"]`` entry.

    ``{name: True}`` means optional (``name?:``). openapi-typescript writes the
    entry header at 8 spaces and its top-level properties at 12, so indentation
    is the delimiter — no brace matching against doc comments needed.
    """
    marker = f"        {type_name}: {{"
    start = text.find(marker)
    if start < 0:
        return {}
    end = text.find("\n        };", start)
    if end < 0:  # pragma: no cover - generated file is well formed
        return {}
    props: dict[str, bool] = {}
    for line in text[start:end].splitlines():
        if not line.startswith(" " * 12) or line.startswith(" " * 13):
            continue
        match = _TS_PROP_RE.match(line.strip())
        if match:
            props[match.group(1)] = match.group(2) == "?"
    return props


def _frontend_schema_text() -> str:
    return _SCHEMA_TS.read_text(encoding="utf-8")


def _openapi_response_schema(app_schema: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    operation = app_schema["paths"][path][method]
    return operation["responses"]["200"]["content"]["application/json"]["schema"]


# ---------------------------------------------------------------------------
# Non-network tests: the sample is real and the checker catches the defect class
# ---------------------------------------------------------------------------
def test_sampled_routes_have_a_generated_frontend_type() -> None:
    """Each sampled route must still resolve to a named generated frontend type."""
    from farna.api.main import app

    app_schema = app.openapi()
    text = _frontend_schema_text()

    problems: list[str] = []
    for sample in SAMPLED_ROUTES:
        operation = app_schema["paths"].get(sample["path"], {}).get(sample["method"])
        if operation is None:
            problems.append(f"{sample['method'].upper()} {sample['path']}: route missing from the OpenAPI schema")
            continue
        if operation.get("operationId") != sample["operation_id"]:
            problems.append(
                f"{sample['method'].upper()} {sample['path']}: operationId is {operation.get('operationId')!r}, the sample expects {sample['operation_id']!r}",
            )
            continue
        if _ts_operation_response_type(text, sample["operation_id"]) is None:
            problems.append(
                f"{sample['operation_id']}: no named frontend type in {_SCHEMA_TS.name} (regenerate it with the pinned generator)",
            )

    assert not problems, "\n".join(problems)


def test_checker_catches_camelcase_snake_case_mismatch() -> None:
    """The negative case: a renamed wire key must be reported, not accepted.

    ``farna__api__routes__auth__MeResponse`` declares ``display_name``. A
    response that carries ``displayName`` instead is exactly the drift a
    mocked test cannot see, and it must fail both the OpenAPI check (missing
    required key + undeclared key) and the generated-type check.
    """
    from farna.api.main import app

    app_schema = app.openapi()
    node = _openapi_response_schema(app_schema, "/api/v1/auth/me", "get")

    good = {
        "id": str(uuid.uuid4()),
        "email": "someone@example.com",
        "display_name": "Someone",
        "org_role": "admin",
        "active": True,
        "created_at": "2026-01-02T03:04:05Z",
        "is_system_admin": False,
        "must_change_password": False,
    }
    assert wire_mismatches(app_schema, node, good) == []

    camel_cased = dict(good)
    camel_cased["displayName"] = camel_cased.pop("display_name")
    problems = wire_mismatches(app_schema, node, camel_cased)
    assert any("required by the contract, missing from the response" in problem for problem in problems), problems
    assert any("displayName" in problem and "undeclared by the contract" in problem for problem in problems), problems

    type_name = _ts_operation_response_type(_frontend_schema_text(), "me_api_v1_auth_me_get")
    assert type_name is not None
    props = _ts_interface_props(_frontend_schema_text(), type_name)
    assert "display_name" in props
    assert "displayName" not in props


def test_sampled_routes_validate_a_representative_payload() -> None:
    """The checker accepts a well-formed payload for every sampled schema.

    This keeps the live test honest: if the checker were too strict it would
    fail here first, without a database.
    """
    from farna.api.main import app

    app_schema = app.openapi()
    payloads: dict[str, Any] = {
        "/api/v1/me": {
            "user": {"username": "someone"},
            "org": {"org_id": str(uuid.uuid4()), "org_name": "Org", "settings": {}},
            "team_memberships": [{"team_id": str(uuid.uuid4()), "team_role": "admin"}],
            "team_memberships_truncated": False,
            "org_role": "admin",
            "preferences": {},
            "is_system_admin": False,
            "must_change_password": False,
        },
        "/api/v1/auth/me": {
            "id": str(uuid.uuid4()),
            "email": "someone@example.com",
            "display_name": "Someone",
            "org_role": "admin",
            "active": True,
            "created_at": "2026-01-02T03:04:05Z",
            "is_system_admin": False,
            "must_change_password": False,
        },
        "/api/v1/me/settings": {},
    }
    problems = []
    for path, payload in payloads.items():
        node = _openapi_response_schema(app_schema, path, "get")
        problems.extend(f"{path}: {problem}" for problem in wire_mismatches(app_schema, node, payload))
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# Live round-trip
# ---------------------------------------------------------------------------
def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        is_system_admin=False,
        client_kind="browser",
    )


@pytest.mark.parametrize("sample", SAMPLED_ROUTES, ids=[route["operation_id"] for route in SAMPLED_ROUTES])
async def test_live_response_matches_openapi_and_generated_type(
    sample: dict[str, str],
    integration_client: AsyncClient,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    """Call the real endpoint and check the live body against both contracts."""
    from farna.api.main import app

    response = await integration_client.get(
        sample["path"],
        headers={"Authorization": f"Bearer {_token(test_org, test_user)}"},
    )
    assert response.status_code == 200, f"{sample['method'].upper()} {sample['path']} -> {response.status_code}: {response.text}"

    body = response.json()
    app_schema = app.openapi()

    problems = wire_mismatches(app_schema, _openapi_response_schema(app_schema, sample["path"], sample["method"]), body)
    assert not problems, f"{sample['method'].upper()} {sample['path']} disagrees with the OpenAPI schema:\n  " + "\n  ".join(problems) + "\n\nLive body: " + json.dumps(body, indent=2, sort_keys=True)

    text = _frontend_schema_text()
    type_name = _ts_operation_response_type(text, sample["operation_id"])
    assert type_name is not None, f"{sample['operation_id']} has no generated frontend type"
    props = _ts_interface_props(text, type_name)
    assert props, f"{type_name} has no top-level properties in the generated frontend type"

    undeclared = sorted(set(body) - set(props))
    assert not undeclared, f"live keys not declared in the generated frontend type {type_name}: {undeclared}"
    missing_required = sorted(prop for prop, optional in props.items() if not optional and prop not in body)
    assert not missing_required, f"required keys of {type_name} missing from the live response: {missing_required}"
