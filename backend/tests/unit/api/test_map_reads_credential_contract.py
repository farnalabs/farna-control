"""The lifecycle-map reads are JWT-only; the system-map family is split by need.

History:

* FAR-2727's read-credential guard classified the fifteen lifecycle-map /
  system-map reads ``jwt_only`` because a JWT-only ``require_permission`` gate
  wins over an accompanying any-credential "internal access" probe.
* FAR-2730 established that classification for the lifecycle-map surface as the
  intended contract (an internal, dev-mode dogfood surface; product map: "not
  Alpha-facing"), and recorded the system-map read family as a KNOWN GAP: the
  ``farna-control apply`` CLI reads ``GET /api/v1/system-maps`` with its
  documented ``mk_`` key, but the whole family (reads AND writes) was JWT-only.
* FAR-2748 closed that gap. ``farna-control apply`` manages system maps as a
  declared entity kind (``cli/apply/system_map_apply.py``): it reads the map
  collection and writes create / update / revision-create. Those four routes
  now accept a JWT or an ``mk_`` key. The other system-map reads keep the
  JWT-only ``require_permission`` on the endpoint — no CLI consumer reads them —
  so widening the collection read did not widen them.

This module pins the full split:

1. every one of the fifteen paths is still present;
2. exactly one map read (the apply-CLI collection read) is ``any_credential``;
   all fourteen others remain ``jwt_only``;
3. the allowlist declares the fourteen JWT-only reads and NOT the widened one;
4. an ``mk_`` Bearer is refused on a representative read that did not widen
   (lifecycle maps), while the widened system-map read is covered positively by
   ``test_system_map_apply_credential_contract.py``;
5. the lifecycle writer keeps its any-credential probe.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from farna.api.dependencies import get_db_session, get_plan_context
from farna.api.main import app
from farna.api.routes.lifecycle_maps import router as lifecycle_maps_router
from farna.api.routes.system_maps import published_router as system_maps_published_router
from farna.settings import Settings, get_settings
from tests.unit.api.route_introspection import (
    API_KEY_PRINCIPAL_DEPENDENCIES,
    CREDENTIAL_ANY,
    CREDENTIAL_JWT_ONLY,
    get_applied_dependency_names,
    get_credential_kind,
    get_read_routes,
    load_read_credential_allowlist,
)

_VALID_32 = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

#: The apply-CLI read FAR-2748 widened: declarative apply plans system maps
#: from the collection with its documented ``mk_`` org key.
_WIDENED_READ = "/api/v1/system-maps"

#: The exact fifteen paths FAR-2727 froze, all still read routes.
_MAP_READ_PATHS = frozenset(
    {
        "/api/v1/lifecycle-maps",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/export",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys/{kind}/{ref}",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/versions",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/versions/{version}",
        _WIDENED_READ,
        "/api/v1/system-maps/{map_id}",
        "/api/v1/system-maps/{map_id}/attribution-bindings",
        "/api/v1/system-maps/{map_id}/overview",
        "/api/v1/system-maps/{map_id}/revisions/by-time",
        "/api/v1/system-maps/{map_id}/revisions/current",
        "/api/v1/system-maps/{map_id}/revisions/history",
        "/api/v1/system-maps/{map_id}/revisions/{rev_id}",
    }
)

_MK_HEADERS = {"Authorization": "Bearer mk_12345678_" + "x" * 32}


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_FERNET_KEY,
        admin_password="testpass",
        dev_mode=True,
    )


class _PlanContext:
    """Plan stub: every feature enabled, so only authentication can refuse."""

    def feature_enabled(self, feature_name: str) -> bool:
        return True

    def tier(self) -> str:
        return "community"


def _map_reads() -> list[Any]:
    return [route for route in get_read_routes(app) if route.path in _MAP_READ_PATHS]


@pytest.fixture
def client() -> AsyncGenerator[TestClient, None]:
    async def override_session() -> AsyncGenerator[MagicMock, None]:
        yield MagicMock()

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = lambda: _PlanContext()
    # No ``with`` block: entering TestClient runs the app lifespan, which needs a
    # real Redis. The route dependency overrides above cover what the read needs.
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Structural: exactly the widened read accepts an mk_ key.
# ---------------------------------------------------------------------------


def test_all_fifteen_map_reads_are_present() -> None:
    assert {route.path for route in _map_reads()} == _MAP_READ_PATHS


def test_only_the_apply_collection_read_accepts_an_mk_key() -> None:
    """FAR-2748 widened exactly ONE map read — the apply CLI's collection read.

    The remaining fourteen keep the JWT-only ``require_permission`` on the
    endpoint, so widening the collection read did not widen the per-map,
    revision or lifecycle views. This is a capability assertion, not a
    dependency-name one: an unused any-credential router probe may sit on a
    route whose own gate still refuses an ``mk_`` key.
    """
    widened = sorted(route.path for route in _map_reads() if get_credential_kind(route) == CREDENTIAL_ANY)
    assert widened == [_WIDENED_READ]


def test_every_other_map_read_is_jwt_only() -> None:
    offenders = sorted(route.path for route in _map_reads() if route.path != _WIDENED_READ and get_credential_kind(route) != CREDENTIAL_JWT_ONLY)
    assert offenders == []


def test_map_reads_are_classified_and_declared() -> None:
    declared = load_read_credential_allowlist()
    for route in _map_reads():
        kind = get_credential_kind(route)
        if route.path == _WIDENED_READ:
            assert kind == CREDENTIAL_ANY, route.path
            assert ("GET", route.path) not in declared, f"{route.path}: a widened read must NOT stay on the JWT-only allowlist"
            continue
        assert kind == CREDENTIAL_JWT_ONLY, route.path
        declared_kind, reason = declared[("GET", route.path)]
        assert declared_kind == "jwt_only", route.path
        # Lifecycle reads carry the FAR-2730 decision; system-map siblings carry
        # the FAR-2748 per-route rationale.
        marker = "FAR-2730" if route.path.startswith("/api/v1/lifecycle-maps") else "FAR-2748"
        assert marker in reason, f"{route.path}: the allowlist reason must record the {marker} decision"


def test_system_map_sibling_reasons_no_longer_record_a_known_gap() -> None:
    """FAR-2748 removed the KNOWN GAP: the apply workflow is delivered now.

    The six per-map/revision system-map reads stay JWT-only, but their reason
    must describe that as a deliberate per-route floor, not as an unresolved
    defect. The old text claimed the whole family was JWT-only and the fix was
    "tracked separately"; that is no longer true.
    """
    declared = load_read_credential_allowlist()
    for path in sorted(p for p in _MAP_READ_PATHS if p.startswith("/api/v1/system-maps") and p != _WIDENED_READ):
        _kind, reason = declared[("GET", path)]
        assert "KNOWN GAP" not in reason, f"{path}: stale KNOWN GAP text after FAR-2748"
        assert "FAR-2748" in reason, f"{path}: reason must record the FAR-2748 decision"


# ---------------------------------------------------------------------------
# Behavioural: an mk_ Bearer is refused on a read that deliberately did not widen.
# ---------------------------------------------------------------------------


def test_unwidened_map_read_still_refuses_an_element_of_org_api_key(client: TestClient) -> None:
    response = client.get("/api/v1/lifecycle-maps", headers=_MK_HEADERS)

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# The lifecycle writer keeps the any-credential probe its reads no longer need.
# ---------------------------------------------------------------------------


def test_self_report_writer_keeps_its_any_credential_probe() -> None:
    self_report = next(route for route in lifecycle_maps_router.routes if getattr(route, "path", None) == "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys/self-report")
    names = get_applied_dependency_names(self_report)
    assert "_require_internal_lifecycle_map_access" in names
    assert names & API_KEY_PRINCIPAL_DEPENDENCIES


def test_published_system_maps_router_still_serves_the_reads() -> None:
    """The published reads stayed on the published router."""
    published_paths = {route.path for route in system_maps_published_router.routes}
    assert _WIDENED_READ in published_paths
    assert "/api/v1/system-maps/{map_id}/overview" in published_paths


def test_every_lifecycle_route_keeps_an_internal_access_probe() -> None:
    """The probe is now declared per route; none may lose the dev-mode gate.

    The router-level dependency was replaced by a per-route JWT-only probe
    (reads) / any-credential probe (writes). A forgotten route would silently
    lose the internal dev-mode gate, so pin every route.
    """
    from tests.unit.api.route_introspection import get_all_apiroutes

    lifecycle = [route for route in get_all_apiroutes(app) if route.path.startswith("/api/v1/lifecycle-maps")]
    probes = {
        "_require_internal_lifecycle_map_access",
        "_require_internal_lifecycle_map_read_access",
    }
    missing = sorted(f"{sorted(route.methods or set())} {route.path}" for route in lifecycle if not (get_applied_dependency_names(route) & probes))
    assert missing == []


def test_lifecycle_map_routes_are_not_duplicated() -> None:
    from tests.unit.api.route_introspection import get_all_apiroutes

    lifecycle = [route for route in get_all_apiroutes(app) if route.path.startswith("/api/v1/lifecycle-maps")]
    keys = [(tuple(sorted(route.methods or set())), route.path) for route in lifecycle]
    assert len(keys) == len(set(keys))
