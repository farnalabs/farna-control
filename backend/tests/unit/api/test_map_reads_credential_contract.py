"""FAR-2730: the fifteen lifecycle-map / system-map reads are JWT-only BY DECISION.

FAR-2727's read-credential guard classified these reads ``jwt_only`` because a
JWT-only ``require_permission`` gate wins over an accompanying any-credential
"internal access" probe. FAR-2730 established that the classification is the
intended contract, not drift:

* the lifecycle-map surface is an internal, dev-mode dogfood surface (product
  map: "not Alpha-facing"); every read's own permission gate is JWT-only and
  the internal-access probe was the only any-credential dependency, and no CLI
  or external HTTP consumer reads lifecycle maps;
* the system-map read family is JWT-only *as delivered*: the published read
  model's plan feature gate resolves through ``get_current_user`` (JWT), the
  internal authoring read sits on the JWT-only authoring router, and the MCP
  read tools call the handlers in-process rather than over HTTP.

FAR-2730 also established a KNOWN GAP that the allowlist reasons record rather
than hide: the ``farna-control apply`` CLI reads ``GET /api/v1/system-maps``
with its documented ``mk_`` key (``cli/apply/executor.py``), but the whole
system-map family — reads AND writes — is JWT-only. Widening this one read
alone cannot deliver that workflow (the CLI's create/update writes are also
JWT-only), so this ticket does not widen on symmetry; the family-wide
key-capability fix is a separate change.

The defect this ticket fixes is that those reads carried a dependency that
*implied* an ``mk_`` capability they could not deliver. The probe now resolves
the JWT principal on the read paths (the mutation/self-report router keeps the
any-credential probe it genuinely uses).

These tests pin both halves:

1. no map read carries ANY any-credential principal dependency, and each is
   still classified JWT-only and declared in the shrink-only allowlist with the
   FAR-2730 decision recorded (structural — fails before the probe change);
2. an ``mk_`` Bearer is refused on a representative read from each family
   (behavioural refusal, real ``get_current_user`` decoding);
3. the writer route keeps its any-credential probe, so narrowing the reads did
   not silently narrow the self-report credential contract.
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
    CREDENTIAL_JWT_ONLY,
    get_applied_dependency_names,
    get_credential_kind,
    get_read_routes,
    load_read_credential_allowlist,
)

_VALID_32 = "a" * 32
_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

#: The exact fifteen paths FAR-2727 froze as JWT-only, and FAR-2730 classified
#: as a deliberate human-only contract.
_MAP_READ_PATHS = frozenset(
    {
        "/api/v1/lifecycle-maps",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/export",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys/{kind}/{ref}",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/versions",
        "/api/v1/lifecycle-maps/{lifecycle_map_id}/versions/{version}",
        "/api/v1/system-maps",
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
# Structural: no map read implies an mk_ capability.
# ---------------------------------------------------------------------------


def test_all_fifteen_map_reads_are_present() -> None:
    assert {route.path for route in _map_reads()} == _MAP_READ_PATHS


def test_no_map_read_carries_an_any_credential_principal_dependency() -> None:
    """The capability-implication defect this ticket exists to remove.

    Before FAR-2730 every one of these reads resolved
    ``get_current_tenant_user_or_api_key`` through an internal-access probe
    (or the published-read probe / org-feature gate), implying an ``mk_`` path
    the JWT-only permission gate could not deliver.
    """
    offenders = sorted(route.path for route in _map_reads() if get_applied_dependency_names(route) & API_KEY_PRINCIPAL_DEPENDENCIES)
    assert offenders == []


def test_map_reads_are_classified_and_declared_jwt_only() -> None:
    declared = load_read_credential_allowlist()
    for route in _map_reads():
        assert get_credential_kind(route) == CREDENTIAL_JWT_ONLY, route.path
        kind, reason = declared[("GET", route.path)]
        assert kind == "jwt_only", route.path
        assert "FAR-2730" in reason, f"{route.path}: the allowlist reason must record the FAR-2730 decision"


def test_system_map_list_reason_records_the_apply_cli_gap() -> None:
    """The decision must name the one real machine consumer, not deny it.

    ``cli/apply/executor.py`` fetches ``/system-maps`` with the CLI's documented
    ``mk_`` key, so "no external consumer" would be an untrue reason. The route
    is JWT-only as delivered and that gap is recorded on the entry; if the
    family is later made key-capable the entry (and this test) are removed.
    """
    declared = load_read_credential_allowlist()
    kind, reason = declared[("GET", "/api/v1/system-maps")]

    assert kind == "jwt_only"
    assert "apply CLI" in reason


# ---------------------------------------------------------------------------
# Behavioural: an mk_ Bearer is refused on a representative read per family.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/lifecycle-maps", "/api/v1/system-maps"])
def test_map_read_refuses_an_element_of_org_api_key(client: TestClient, path: str) -> None:
    response = client.get(path, headers=_MK_HEADERS)

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# The writer route keeps the any-credential probe the reads no longer need.
# ---------------------------------------------------------------------------


def test_self_report_writer_keeps_its_any_credential_probe() -> None:
    self_report = next(route for route in lifecycle_maps_router.routes if getattr(route, "path", None) == "/api/v1/lifecycle-maps/{lifecycle_map_id}/journeys/self-report")
    names = get_applied_dependency_names(self_report)
    assert "_require_internal_lifecycle_map_access" in names
    assert names & API_KEY_PRINCIPAL_DEPENDENCIES


def test_published_system_maps_router_still_serves_the_reads() -> None:
    """The published reads stayed on the published router."""
    published_paths = {route.path for route in system_maps_published_router.routes}
    assert "/api/v1/system-maps" in published_paths
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
