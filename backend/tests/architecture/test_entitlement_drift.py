"""Entitlement drift guard (FAR-2665).

Two directions, both enforced structurally so a future paywall cannot land
without registering its capability and a future flag cannot silently
reintroduce a commercial tier:

1. **No runtime flag declares a tier.** The tier a flag is PRESENTED under is a
   projection of :mod:`farna.core.entitlements`; a ``tier=`` keyword on a
   ``FeatureFlag(...)`` declaration is the FAR-2604 defect returning (a code
   tier that the DB catalogue can override, or that never reaches an existing
   install).
2. **Every commercial gate names an entitlement.** Every ``require_entitlement``
   call and every manifest ``required_entitlement`` must name a declared entry,
   and every declared entry must be named by a gate somewhere (an unused
   capability is either a dead gate or a missing one).

A manifest ``required_tier`` is rejected outright: the router and the sidebar
read the entitlement grant from the plan payload, not a tier string.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from farna.core.entitlements import ENTITLEMENTS, entitlement_for_flag
from farna.core.feature_flags import _KNOWN_FLAGS
from tests.unit._source_scan import iter_source_text

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_SRC = _REPO_ROOT / "backend" / "src" / "farna"
_FRONTEND_SRC = _REPO_ROOT / "frontend" / "src"
_MANIFEST = _FRONTEND_SRC / "manifest.yaml"
_FEATURE_FLAGS_PY = _BACKEND_SRC / "core" / "feature_flags.py"

_ENTITLEMENT_GATE = re.compile(r'require_entitlement\("([a-z_]+)"\)')
_FLAG_GATE_PATTERNS = (
    re.compile(r'require_feature\("([a-z_]+)"\)'),
    re.compile(r'feature_enabled\("([a-z_]+)"\)'),
    re.compile(r'get_flag\("([a-z_]+)"\)'),
    re.compile(r'resolve_flag\("([a-z_]+)"\)'),
)
_FRONTEND_FLAG_PATTERNS = (
    re.compile(r'feature-name="([a-z_]+)"'),
    re.compile(r"featureEnabled\('([a-z_]+)'\)"),
    re.compile(r'featureEnabled\("([a-z_]+)"\)'),
)


def _known_flags_block() -> str:
    """The ``_KNOWN_FLAGS`` literal, so scanning cannot see unrelated code."""
    text = _FEATURE_FLAGS_PY.read_text(encoding="utf-8")
    start = text.index("_KNOWN_FLAGS: list[FeatureFlag] = [")
    end = text.index("\n\n\n# community=Free", start)
    return text[start:end]


def _backend_text() -> str:
    return "\n".join(source for _, source in iter_source_text(_BACKEND_SRC))


def _frontend_text() -> str:
    parts: list[str] = []
    for path in sorted(_FRONTEND_SRC.rglob("*")):
        if path.is_file() and path.suffix in {".vue", ".ts", ".tsx", ".js"}:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _manifest() -> dict[str, object]:
    return yaml.safe_load(_MANIFEST.read_text(encoding="utf-8"))


def _manifest_entitlement_values() -> set[str]:
    """Every route ``required_entitlement`` value in the parsed manifest.

    Parsed, not grepped: the manifest also *documents* the key in comments, and
    a regex over the raw text matches a prose line (it read ``the`` from one).
    """
    routes = _manifest().get("routes") or {}
    return {str(route["required_entitlement"]) for route in routes.values() if isinstance(route, dict) and route.get("required_entitlement")}


def _backend_entitlement_gates() -> set[str]:
    return set(_ENTITLEMENT_GATE.findall(_backend_text()))


class TestNoRuntimeFlagDeclaresATier:
    def test_no_feature_flag_declaration_carries_a_tier_keyword(self) -> None:
        block = _known_flags_block()
        offenders = [chunk.splitlines()[1].strip() if len(chunk.splitlines()) > 1 else chunk for chunk in block.split("FeatureFlag(") if re.search(r"\btier\s*=", chunk)]
        assert not offenders, "A FeatureFlag declaration carries a tier= keyword. Tier is a projection of core.entitlements; remove it: " + "; ".join(offenders)


class TestEntitlementRegistryIsConsistent:
    def test_every_entitlement_flag_is_a_declared_flag(self) -> None:
        declared = {flag.name for flag in _KNOWN_FLAGS}
        for entitlement in ENTITLEMENTS.values():
            stray = sorted(set(entitlement.flags) - declared)
            assert not stray, f"Entitlement '{entitlement.key}' names flags that are not registered: {stray}"

    def test_no_flag_belongs_to_two_entitlements(self) -> None:
        seen: dict[str, str] = {}
        for entitlement in ENTITLEMENTS.values():
            for flag in entitlement.flags:
                assert flag not in seen, f"'{flag}' is claimed by both '{seen[flag]}' and '{entitlement.key}'"
                seen[flag] = entitlement.key

    def test_the_projection_agrees_with_the_registry(self) -> None:
        for flag in _KNOWN_FLAGS:
            covering = entitlement_for_flag(flag.name)
            expected = "team" if covering is not None else "community"
            assert flag.tier == expected, f"{flag.name} projects tier '{flag.tier}' but the entitlement registry says '{expected}'"


class TestEveryCommercialGateNamesAnEntitlement:
    def test_backend_entitlement_gates_name_a_declared_entry(self) -> None:
        unknown = sorted(_backend_entitlement_gates() - set(ENTITLEMENTS))
        assert not unknown, f"require_entitlement names an undeclared entitlement: {unknown}"

    def test_manifest_entitlement_gates_name_a_declared_entry(self) -> None:
        unknown = sorted(_manifest_entitlement_values() - set(ENTITLEMENTS))
        assert not unknown, f"manifest.yaml required_entitlement names an undeclared entitlement: {unknown}"

    def test_manifest_carries_no_required_tier(self) -> None:
        text = _MANIFEST.read_text(encoding="utf-8")
        assert "required_tier" not in text, "manifest.yaml still declares required_tier; commercial gating must use required_entitlement"

    def test_frontend_router_and_nav_carry_no_tier_gate(self) -> None:
        for relative in ("router/index.ts", "config/navigation.ts", "components/FeatureGate.vue", "stores/planStore.ts"):
            path = _FRONTEND_SRC / relative
            text = path.read_text(encoding="utf-8")
            assert "required_tier" not in text, f"{relative} still reads required_tier; it must read the plan payload's entitlements"
            assert "required-tier" not in text, f"{relative} still carries a required-tier prop; entitlement now drives the gate"

    def test_every_declared_entitlement_is_named_by_a_gate(self) -> None:
        named = _backend_entitlement_gates() | _manifest_entitlement_values()
        unused = sorted(set(ENTITLEMENTS) - named)
        assert not unused, f"Entitlements declared but never gated (dead capability or missing gate): {unused}"

    def test_every_entitled_flag_is_enforced_somewhere(self) -> None:
        backend = _backend_text()
        frontend = _frontend_text()
        missing: list[str] = []
        for entitlement in ENTITLEMENTS.values():
            for flag in entitlement.flags:
                # Per-flag enforcement: an entitlement gate on a route answers
                # "is this capability bought?", it does not read the individual
                # flag. A flag no code path ever reads is a dead kill switch -
                # toggling it off changes nothing - so require a flag gate
                # somewhere for every entitled flag.
                if any(flag in pattern.findall(backend) for pattern in _FLAG_GATE_PATTERNS):
                    continue
                if any(flag in pattern.findall(frontend) for pattern in _FRONTEND_FLAG_PATTERNS):
                    continue
                missing.append(f"{flag} ({entitlement.key})")
        assert not missing, "Entitled flags are never read by a flag gate in backend or frontend, so switching them off has no effect (dead kill switch): " + ", ".join(missing)
