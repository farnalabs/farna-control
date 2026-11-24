"""Strict route-introspection test for the ADR 047 permission sweep.

Walks the FastAPI router and asserts the route authorisation guarantees. The
guard is split across the two route surfaces the router exposes, because the
ADR 047 model gates them differently:

* **Mutating routes** (POST/PUT/PATCH/DELETE) — every mutating user-principal
  route carries a permission dependency (tagged via ``_tagged_dep``) OR is on
  the explicit documented ``EXEMPT`` allowlist. This is the completeness
  guarantee for task-authz-b-sweep: a mutating route added without a permission
  tag fails loudly.
* **Read routes** (GET) — every read route is permission-tagged, OR is
  authenticated by a principal-bearing dependency (``PRINCIPAL_DEPENDENCIES`` —
  the ADR 047 model for reads, which rely on the tenant/user principal rather
  than an org-role tag), OR is on the explicit documented ``READ_EXEMPT``
  public/pre-auth allowlist. Before FAR-1969 every read route was outside the
  guard entirely, so an added read route with no authorisation at all could not
  be caught — precisely the FAR-1955 surface class.
* **Read credential kinds** (FAR-2727) — for every authenticated read route the
  guard derives *which credential can pass* it (JWT-only, any ``mk_`` key, or
  the SCIM shared secret) from the applied dependency set, and requires every
  non-``mk_`` read to be declared in the shrink-only
  ``read_credential_jwt_only_allowlist.txt`` with a reason. The pre-FAR-2727
  guard only asserted that *a* gate existed, so an accidentally JWT-only read
  stayed green forever — FAR-2681/2682/2726 were each found by a human instead.
* **Dependency-site coverage** — a permission tag is honoured wherever it is
  declared: endpoint signature, route decorator, router-level
  ``dependencies=[...]``, or nested inside another dependency
  (``test_route_level_permission_gates_are_visible``).

Explicitly **not** asserted here: the *effective role* of a resolved permission
key (a valid-but-wrong key such as ``runner.status`` resolving to the viewer
``run.status``). A ``{route: role}`` map is the wrong instrument for that — it
can itself encode the typo and would read as complete; behavioural boundary
tests are the mechanism (FAR-1964 design verdict). This guard asserts presence
and resolvability of a gate, not its role.

The mutating-route guard stays separate from the read credential-kind guard
(FAR-2727 decision). It asserts that a gate *exists* (a permission tag, or a
documented exempt channel) because a mutating route being JWT-only is the safe
default and widening one is a privilege decision already pinned by
``test_run_trigger_status_use_any_credential``. A read route is where an ``mk_``
key is normally expected, so reads additionally assert *which* credential can
pass. Both use the same principal/credential vocabulary from
``route_introspection`` — the split is in the invariant, not in the machinery.

The ADR's intent (v9, "Phase-1 sweep" + "Strict introspection test"):
- bidirectional map: every guarded route is tagged or exempt; nothing is both
- variant-kind assertions: strict system routes use require_system_permission,
  org-deletion uses require_system_or_org_admin, admin_email/orgs use the
  scoped-hybrid, run trigger/status use tenant_or_api_key
- min-role assertions: the resolved role matches the registry
"""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi import APIRouter, Depends, FastAPI

from farna.api.main import app
from tests.unit.api.route_introspection import (
    CREDENTIAL_ANY,
    CREDENTIAL_JWT_ONLY,
    PRINCIPAL_DEPENDENCIES,
    find_read_credential_violations,
    get_all_apiroutes,
    get_applied_dependency_names,
    get_credential_kind,
    get_mutating_routes,
    get_permission_tag,
    get_read_routes,
    get_resolved_min_role,
    load_read_credential_allowlist,
    read_credential_guidance,
)

# ---------------------------------------------------------------------------
# Exempt allowlist: mutating routes that intentionally do NOT carry a
# permission tag, each with a documented reason (ADR 047 exempt channels).
# Key = (method, path) ; value = reason.
# ---------------------------------------------------------------------------
EXEMPT: dict[tuple[str, str], str] = {
    # SCIM: shared-secret FARNA_CONTROL_SCIM_TOKEN is the authorization (phase 1).
    ("POST", "/scim/v2/Users"): "SCIM shared-secret channel",
    ("PUT", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("PATCH", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("DELETE", "/scim/v2/Users/{user_id}"): "SCIM shared-secret channel",
    ("POST", "/scim/v2/Groups"): "SCIM shared-secret channel",
    ("PUT", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    ("PATCH", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    ("DELETE", "/scim/v2/Groups/{group_id}"): "SCIM shared-secret channel",
    # Error telemetry: HMAC-signed ingestion + public ingestion + session key.
    ("POST", "/api/v1/errors/ingest/public"): "public error ingestion (telemetry)",
    # Onboarding: creation-only + user onboarding actions (ADR creation-only rule).
    ("POST", "/api/v1/onboarding/actions/{action_id}/complete"): "onboarding user action",
    ("POST", "/api/v1/onboarding/actions/{action_id}/skip"): "onboarding user action",
    ("POST", "/api/v1/onboarding/dismiss"): "onboarding user action",
    # Assistant: dev-mode-only assistant with its own session auth model.
    ("POST", "/api/v1/assistant/sessions"): "Assistant own session auth",
    ("PATCH", "/api/v1/assistant/sessions/{session_id}"): "Assistant own session auth",
    ("DELETE", "/api/v1/assistant/sessions/{session_id}"): "Assistant own session auth",
    ("POST", "/api/v1/assistant/sessions/{session_id}/messages"): "Assistant own session auth",
    ("POST", "/api/v1/assistant/sessions/{session_id}/stream"): "Assistant own session auth",
    # me/*: the user's own profile (get_current_user is the correct gate).
    ("PUT", "/api/v1/me/settings"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/password"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/hitl-email-preferences"): "self-profile (get_current_tenant_user)",
    ("POST", "/api/v1/me/assistant/skills"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/assistant/skills/{skill_id}"): "self-profile (get_current_user)",
    ("DELETE", "/api/v1/me/assistant/skills/{skill_id}"): "self-profile (get_current_user)",
    ("PUT", "/api/v1/me/assistant/context-sources/{source_key}"): "self-profile (get_current_user)",
    ("DELETE", "/api/v1/me/assistant/context-sources"): "self-profile (get_current_user)",
    # Libraries: creation-only + user content (ADR creation-only rule).
    ("POST", "/api/v1/libraries/{primitive_id}/ratings"): "user content",
    ("POST", "/api/v1/libraries/{primitive_id}/ratings/abuse"): "user content",
    # OAuth protocol: handled by A1b (client create/delete gated via oauth.client.*).
    ("POST", "/api/v1/mcp/oauth/clients"): "OAuth A1b flow",
    ("DELETE", "/api/v1/mcp/oauth/clients/{client_id}"): "OAuth A1b flow",
    ("POST", "/api/v1/mcp/oauth/consent/approve"): "OAuth A1b consent (get_current_tenant_user)",
    # Product analytics: self-service consent (accept/decline/dismiss) for the
    # current user's own org — get_current_tenant_user is the correct gate.
    (
        "POST",
        "/api/v1/org/product-analytics/consent",
    ): "product analytics self-service consent (get_current_tenant_user)",
    # Model backend setup flow.
    ("POST", "/api/v1/model-backends/{backend_id}/complete-setup"): "setup flow",
    # auth: no principal yet at these endpoints.
    ("POST", "/api/v1/auth/login"): "auth (no principal)",
    ("POST", "/api/v1/auth/refresh"): "auth (no principal)",
    ("POST", "/api/v1/auth/logout"): "auth (refresh-token auth)",
    ("POST", "/api/v1/auth/saml/acs"): "SAML ACS (IdP session)",
    ("POST", "/api/v1/auth/saml/acs/{provider_id}"): "SAML ACS (IdP session)",
    # FAR-461: pre-enrollment token consumption happens before any principal
    # exists (the token IS the credential), so it cannot carry a permission tag.
    ("POST", "/api/v1/auth/accept-invite"): "auth (no principal - pre-enrollment token consumption)",
    # FAR-535: demo auto-login MINTS the principal — the kill switch + demo-org
    # viewer scoping are the gates; a permission tag would require the session
    # the endpoint exists to create.
    ("POST", "/api/v1/auth/demo"): "demo auto-login (mints the principal)",
    # Composite templates: creation/update/delete/publish routes are now
    # permission-tagged (pipeline.*); only stateless detect-params remains exempt.
    ("POST", "/api/v1/composite-templates/detect-params"): "composite creation-only (ADR 3.6)",
    # Runs: node observe/recover carry inline admin/operator checks in the handler.
    ("POST", "/api/v1/runs/{run_id}/nodes/{node_id}/observe"): "inline admin/operator check in handler",
    ("POST", "/api/v1/runs/{run_id}/nodes/{node_id}/recover"): "inline admin/operator check in handler",
    # Runs: guardrail-override carries an inline admin/operator check in the handler.
    ("POST", "/api/v1/runs/{run_id}/guardrail-override"): "inline admin/operator check in handler",
    # FAR-461: admin invite/revoke routes carry an inline _require_admin check
    # in the handler (admin-only), consistent with the runs inline-check pattern.
    ("POST", "/api/v1/admin/users/invite"): "inline admin check in handler",
    ("DELETE", "/api/v1/admin/users/invitations/{invitation_id}"): "inline admin check in handler",
    # Webhooks: HMAC/shared-secret is the authorization (exempt channel).
    ("POST", "/api/v1/triggers/{trigger_id}/webhook"): "HMAC/shared-secret channel",
    # Stripe: Stripe-Signature (HMAC-SHA256 of the raw body with the webhook
    # secret) is the authorization (exempt channel, mirrors the trigger webhook).
    ("POST", "/api/v1/webhooks/stripe"): "Stripe-Signature HMAC channel",
    (
        "POST",
        "/api/v1/triggers/{trigger_id}/webhook/replay/{event_id}",
    ): "HMAC/shared-secret channel (runner-or-HMAC in handler)",
    # Slack: X-Slack-Signature (HMAC-SHA256 with the trigger signing secret)
    # is the authorization (exempt channel, mirrors the webhook route).
    ("POST", "/api/v1/triggers/{trigger_id}/slack"): "X-Slack-Signature HMAC channel",
    # Deployment drain: deploy-pipeline control authenticated by a shared-secret
    # Bearer token (FARNA_CONTROL_DEPLOY_DRAIN_TOKEN). No user principal exists (the
    # deploy workflow calls it), so a permission tag cannot gate it — the same
    # shared-secret channel as SCIM/webhooks.
    ("POST", "/api/v1/deployment/drain"): "deploy drain shared-secret Bearer channel",
    # admin.py user/team/publisher/purge/retention routes: inline org_role==admin
    # checks in the handler (gated but not tagged). Tracked for conversion to
    # require_permission dependencies in a follow-up sweep.
    ("POST", "/api/v1/admin/users"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/deactivate"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/reactivate"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/users/{user_id}/reset-password"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/users/{user_id}"): "admin.py inline _require_admin",
    # FAR-1478: service-account lifecycle carries an inline _require_admin
    # check in the handler (admin-only), same pattern as the admin user
    # routes above. The key-mint route is NOT exempt — it is tagged via
    # require_permission_any_credential("api_key.create").
    ("POST", "/api/v1/service-accounts"): "service_accounts.py inline _require_admin",
    ("PATCH", "/api/v1/service-accounts/{service_account_id}"): "service_accounts.py inline _require_admin",
    ("POST", "/api/v1/service-accounts/{service_account_id}/decommission"): "service_accounts.py inline _require_admin",
    ("POST", "/api/v1/admin/teams"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/teams/{team_id}"): "admin.py inline _require_admin",
    ("DELETE", "/api/v1/admin/teams/{team_id}"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/publishers"): "admin.py inline _require_admin",
    ("PUT", "/api/v1/admin/publishers/{publisher_id}"): "admin.py inline _require_admin",
    ("DELETE", "/api/v1/admin/publishers/{publisher_id}"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/purge/runs"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/purge"): "admin.py inline _require_admin",
    ("POST", "/api/v1/admin/runs/purge"): "admin.py inline org_role==admin",
    ("PUT", "/api/v1/admin/runs/retention"): "admin.py inline org_role==admin",
    ("PUT", "/api/v1/admin/org"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/sandbox-concurrency"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/hitl-review-window"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/run-concurrency"): "admin.py inline _require_org_admin",
    ("PUT", "/api/v1/admin/org/work-item-agent-minting"): "admin.py inline _require_org_admin",
    ("POST", "/api/v1/admin/org/journeys/dismiss"): "admin.py inline _require_org_admin",
    ("POST", "/api/v1/admin/org/journeys/restore"): "admin.py inline _require_org_admin",
    ("POST", "/api/v1/admin/org/regenerate-api-key"): "admin.py inline _require_org_admin",
}


# ---------------------------------------------------------------------------
# Dependencies that establish a caller principal (authentication). A read route
# carrying any of these is authenticated even without a permission tag — the
# ADR 047 model for reads, which rely on the tenant/user principal rather than
# an org-role tag. Optional (`*_optional`) variants are deliberately ABSENT: an
# optional principal does not gate the route. Add a name here only when the
# dependency genuinely requires a valid principal.
#
# Imported from ``route_introspection`` (not defined here): the credential-kind
# guard (FAR-2727) splits the same set by credential, so a second literal would
# be able to drift.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Read-route public/pre-auth allowlist: GET routes that intentionally carry NO
# permission tag and NO principal dependency, each with a documented reason.
# Key = (method, path) ; value = reason. Everything here is reachable without a
# session BY DESIGN; a read route that is neither tagged, nor authenticated,
# nor listed here fails the guard.
# ---------------------------------------------------------------------------
READ_EXEMPT: dict[tuple[str, str], str] = {
    # Liveness/readiness probes: hit by the platform's health checks with no
    # credentials; return no tenant data.
    ("GET", "/healthz"): "health probe (no principal)",
    ("GET", "/healthz/ready"): "readiness probe (no principal)",
    # OAuth discovery metadata (RFC 8414 / RFC 9728): public by specification.
    ("GET", "/.well-known/oauth-authorization-server"): "OAuth discovery metadata (public by spec)",
    ("GET", "/.well-known/oauth-protected-resource"): "OAuth protected-resource metadata (public by spec)",
    ("GET", "/.well-known/oauth-protected-resource/mcp"): "OAuth protected-resource metadata (public by spec)",
    # Pre-auth login/SSO entry points: no principal exists yet (the route
    # mints or resolves one). Adding an authenticated dependency here would
    # 401 the logged-out browser — see the pre-auth route lesson.
    ("GET", "/api/v1/auth/login-context"): "pre-auth login page context (no principal yet)",
    ("GET", "/api/v1/auth/org-login/{slug}"): "pre-auth org login (no principal yet)",
    ("GET", "/api/v1/auth/oidc/{provider}/login"): "pre-auth OIDC login (no principal yet)",
    ("GET", "/api/v1/auth/oidc/{provider}/callback"): "pre-auth OIDC callback (no principal yet)",
    ("GET", "/api/v1/auth/saml/login"): "pre-auth SAML login (no principal yet)",
    ("GET", "/api/v1/auth/saml/metadata"): "pre-auth SAML metadata (no principal yet)",
    ("GET", "/api/v1/auth/saml/{provider_id}/login"): "pre-auth SAML login (no principal yet)",
    ("GET", "/api/v1/auth/saml/{provider_id}/metadata"): "pre-auth SAML metadata (no principal yet)",
    ("GET", "/api/v1/auth/sso/providers"): "pre-auth SSO discovery (login page)",
    # MCP OAuth consent: anonymous display context for a pending consent grant
    # (the caller has not authenticated yet); enforced against the pending
    # consent row's resolved org, never a session.
    ("GET", "/api/v1/mcp/oauth/consent/context"): "anonymous OAuth consent context (pre-auth)",
    # Deployment build metadata: unauthenticated BY DESIGN so the CI/CD deploy
    # pipelines can verify which build is live; only already-public git
    # metadata is exposed (docstring in routes/deployment.py).
    ("GET", "/api/v1/deployment"): "deploy-pipeline build verification (public by design)",
    # Public, tenant-free catalogues and static content: the connector-type
    # catalogue, the product-map manifest, and the community registry. None
    # reads tenant data.
    ("GET", "/api/v1/connectors/types"): "static connector-type catalogue (no tenant data)",
    ("GET", "/api/v1/manifest"): "public product-map manifest (no tenant data)",
    ("GET", "/api/v1/registry/primitives"): "public community registry (no tenant data)",
    ("GET", "/api/v1/registry/primitives/{slug:path}"): "public community registry (no tenant data)",
    ("GET", "/api/v1/registry/publishers"): "public community registry (no tenant data)",
    ("GET", "/api/v1/registry/pull/{slug:path}"): "public community registry (no tenant data)",
    # Public liveness ping and instance licence view: both unauthenticated and
    # org-less BY DESIGN (FAR-1720 for the licence view — it reports the
    # deployment-level licence ladder with no org context).
    ("GET", "/api/v1/libraries/ping"): "public liveness ping (no tenant data)",
    ("GET", "/api/v1/license"): "deployment-level licence view (unauthenticated + org-less by design, FAR-1720)",
}


_SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


def _route_keys(route) -> set[tuple[str, str]]:
    methods = (route.methods or set()) - {"HEAD", "OPTIONS"}
    unsupported = methods - _SUPPORTED_HTTP_METHODS
    if unsupported:
        raise AssertionError(f"Unsupported HTTP methods on {route.path}: {sorted(unsupported)}")
    return {(method, route.path) for method in methods}


def _route_key(route) -> tuple[str, str]:
    keys = _route_keys(route)
    if len(keys) != 1:
        raise AssertionError(f"Multi-method route requires per-method coverage keys: {sorted(keys)}")
    return next(iter(keys))


def test_every_mutating_route_is_tagged_or_exempt() -> None:
    """Bidirectional map: each mutating route is tagged or exempt; nothing both."""
    mutating = get_mutating_routes(app)
    untagged = []
    for route in mutating:
        keys = _route_keys(route)
        tag = get_permission_tag(route)
        for key in keys:
            in_exempt = key in EXEMPT
            if tag is None and not in_exempt:
                untagged.append(f"{key[0]} {key[1]}")
            elif tag is not None and in_exempt:
                # Tagged AND exempt = contradiction — must be one or the other.
                pytest.fail(f"Route {key} is both tagged and exempt in the allowlist: {tag}")

    assert not untagged, "Mutating routes without a permission tag or exemption:\n" + "\n".join(untagged)


def test_exempt_allowlist_entries_exist() -> None:
    """Every exempt entry must actually be a mutating route (no stale entries)."""
    mutating_keys = {key for route in get_mutating_routes(app) for key in _route_keys(route)}
    missing = [k for k in EXEMPT if k not in mutating_keys]
    assert not missing, "Exempt entries that are not mutating routes:\n" + "\n".join(str(m) for m in missing)


def test_get_all_apiroutes_recurses_through_nested_included_routers() -> None:
    """Nested router inclusion must not hide routes from authorization coverage."""
    nested = APIRouter(prefix="/nested")

    @nested.get("/leaf")
    async def nested_leaf() -> dict[str, str]:
        return {"status": "ok"}

    middle = APIRouter(prefix="/middle")
    middle.include_router(nested)
    fixture = FastAPI()
    fixture.include_router(middle)

    routes = get_all_apiroutes(fixture)

    assert {(route.path, frozenset(route.methods)) for route in routes} == {
        ("/nested/leaf", frozenset({"GET"})),
    }


def test_every_read_route_is_authorised_or_exempt() -> None:
    """Every read route is tagged, authenticated, or on the public allowlist.

    Getting this to pass was FAR-1969's coverage decision: reads are NOT all
    permission-tagged in the ADR 047 model, so the honest invariant is
    "tagged OR authenticated by a principal dependency OR publicly exempt".
    A read route carrying NONE of the three fails — which is how the added
    read-regression class (the FAR-1955 surface) is caught.
    """
    reads = get_read_routes(app)
    unauthorised = []
    for route in reads:
        keys = _route_keys(route)
        tag = get_permission_tag(route)
        for key in keys:
            if tag is not None:
                if key in READ_EXEMPT:
                    # Tagged AND exempt = contradiction — must be one or the other.
                    pytest.fail(f"Read route {key} is both tagged and in the read exempt allowlist: {tag}")
                continue
            if get_applied_dependency_names(route) & PRINCIPAL_DEPENDENCIES:
                continue
            if key in READ_EXEMPT:
                continue
            unauthorised.append(f"{key[0]} {key[1]}")

    assert not unauthorised, "Read routes with no permission tag, no principal dependency, and no read-exempt entry:\n" + "\n".join(unauthorised)


def test_read_exempt_allowlist_entries_exist() -> None:
    """Every read-exempt entry must be a GET route (no stale entries)."""
    read_keys = {key for route in get_read_routes(app) for key in _route_keys(route)}
    missing = [k for k in READ_EXEMPT if k not in read_keys]
    assert not missing, "Read-exempt entries that are not GET routes:\n" + "\n".join(str(m) for m in missing)


# ---------------------------------------------------------------------------
# FAR-2727: read-route credential kinds.
#
# The tests above assert that a read route HAS an authorisation gate. They do
# not say WHICH credential can pass it, so a read that only accepts a user JWT
# (and rejects an `mk_` org API key) is green by default — the hole a human
# noticed three times (FAR-2681, FAR-2682, FAR-2726). These tests derive the
# credential kind of every read route from the dependency set FastAPI actually
# resolves and require every non-`mk_` read to be declared, with a reason, in
# `read_credential_jwt_only_allowlist.txt`.
#
# That file is a SHRINK-ONLY RATCHET. Adding an entry for a new read is the
# escape hatch this test offers, but it is not the default: widen the route.
# The test alone cannot distinguish a new entry for a genuinely-new route from
# a loosening of the baseline; the FAR-2699 shrink-only-baseline guard
# (scripts/run_check_ratchet_baseline.py) can be extended to cover the file so
# an ADDED entry fails CI against the base revision.
# ---------------------------------------------------------------------------
def test_read_route_credential_kinds_are_declared() -> None:
    """Every authenticated read route declares whether an `mk_` key can pass it."""
    reads = get_read_routes(app)
    declared = load_read_credential_allowlist()
    kinds = Counter(get_credential_kind(route) for route in reads)

    # Non-vacuity: if the derivation silently stopped seeing dependencies, every
    # read would classify as `none` and this guard would pass without checking
    # anything. The read-authorisation test would fail too, but fail loudly here.
    assert kinds[CREDENTIAL_ANY] > 0, f"no any-credential read routes derived — the credential-kind derivation is broken: {dict(kinds)}"
    assert kinds[CREDENTIAL_JWT_ONLY] > 0, f"no JWT-only read routes derived — the credential-kind derivation is broken: {dict(kinds)}"

    violations = find_read_credential_violations(reads, declared)
    assert not violations, "Read routes whose credential kind is not declared correctly:\n\n" + "\n".join(violations) + "\n\n" + read_credential_guidance()


def test_read_credential_allowlist_entries_are_live_and_jwt_only() -> None:
    """No stale read-credential entry, and no entry that now accepts an `mk_` key.

    The main guard reports these too; this test pins the ratchet's other
    direction on its own so it is obvious which invariant broke: an entry must
    name a live read route that is still non-`mk_`-capable. A deliberately
    JWT-only read that is widened therefore fails until its entry is removed —
    which is the review signal FAR-2681/2682/2726 lacked.
    """
    reads = get_read_routes(app)
    declared = load_read_credential_allowlist()
    live = {(method, path): get_credential_kind(route) for route in reads for method, path in _route_keys(route)}

    stale = [f"{method} {path} (declared {kind})" for (method, path), (kind, _reason) in declared.items() if live.get((method, path)) is None]
    widened = [f"{method} {path} (declared {declared_kind}, now {live[(method, path)]})" for (method, path), (declared_kind, _reason) in declared.items() if live.get((method, path)) is not None and live[(method, path)] != declared_kind]

    assert not stale, "Read-credential entries that no longer name a read route — remove them:\n" + "\n".join(stale)
    assert not widened, "Read routes declared non-`mk_`-capable that now derive a different credential kind — the credential surface changed (widened to `mk_`, re-gated, or made public). Remove the entry and justify the change in the PR:\n" + "\n".join(widened)


def test_read_credential_allowlist_is_disjoint_from_read_exempt() -> None:
    """A route cannot be both public/pre-auth and a declared authenticated read."""
    overlap = sorted(set(load_read_credential_allowlist()) & set(READ_EXEMPT))
    assert not overlap, "Read routes that are both on the public READ_EXEMPT list and the read-credential allowlist:\n" + "\n".join(f"{m} {p}" for m, p in overlap)


def _scratch_app(*, accepts_api_key: bool) -> FastAPI:
    """A minimal app with one scratch read route of the chosen credential kind."""
    from farna.api.dependencies import require_permission, require_permission_any_credential
    from farna.auth.dependencies import TenantPrincipal

    factory = require_permission_any_credential if accepts_api_key else require_permission
    fixture = FastAPI()

    @fixture.get("/scratch/read")
    async def scratch_read(principal: TenantPrincipal = factory("pipeline.list")) -> dict[str, str]:
        return {"principal": repr(principal)}

    return fixture


def test_read_credential_guard_flags_a_new_jwt_only_read() -> None:
    """Prove-the-fix: a new JWT-only read fails BY NAME; widening it passes.

    Uses a scratch app rather than a real route so the mechanism is exercised
    without ever editing a route module. The three cases are exactly the
    author's three options: undeclared (fail), widen (pass), declare (pass).
    """
    jwt_only = get_read_routes(_scratch_app(accepts_api_key=False))
    undeclared = find_read_credential_violations(jwt_only, {})
    assert len(undeclared) == 1, f"expected exactly one violation for the scratch JWT-only read, got {undeclared}"
    assert "GET /scratch/read" in undeclared[0], f"the violation must name the route, got: {undeclared[0]}"
    assert "does NOT accept an `mk_` org API key" in undeclared[0], f"the violation must state the credential gap, got: {undeclared[0]}"

    declared = {("GET", "/scratch/read"): (CREDENTIAL_JWT_ONLY, "scratch: deliberately JWT-only")}
    assert not find_read_credential_violations(jwt_only, declared), "a justified declaration must pass"

    widened = get_read_routes(_scratch_app(accepts_api_key=True))
    assert not find_read_credential_violations(widened, {}), "widening the route to accept an `mk_` key must pass"
    contradiction = find_read_credential_violations(widened, declared)
    assert len(contradiction) == 1, f"a widened route must contradict its JWT-only declaration, got {contradiction}"
    assert "now accepts an `mk_` org API key" in contradiction[0], f"the contradiction must say the route was widened, got: {contradiction[0]}"


def test_read_credential_kind_reads_a_jwt_gate_hidden_outside_the_signature() -> None:
    """A JWT-requiring dependency anywhere on the route makes it JWT-only.

    The any-credential variant can be present *and* a JWT be mandatory — e.g.
    ``GET /api/v1/pipelines/{pipeline_id}`` combines ``require_permission`` with
    a team gate. FastAPI resolves every dependency, so an `mk_` key is rejected;
    classifying by the any-credential dependency alone would call it
    mk_-capable. This pins the precedence in the derivation.
    """
    from fastapi import APIRouter

    from farna.api.dependencies import require_permission_any_credential
    from farna.auth.dependencies import TenantPrincipal, get_current_user

    fixture = FastAPI()
    router = APIRouter(dependencies=[Depends(get_current_user)])

    @router.get("/mixed")
    async def mixed_read(principal: TenantPrincipal = require_permission_any_credential("pipeline.list")) -> dict[str, str]:
        return {"principal": repr(principal)}

    fixture.include_router(router)
    (route,) = get_read_routes(fixture)
    assert get_credential_kind(route) == CREDENTIAL_JWT_ONLY, "a JWT-requiring dependency must win over the any-credential gate"

    plain = FastAPI()

    @plain.get("/plain")
    async def plain_read(principal: TenantPrincipal = require_permission_any_credential("pipeline.list")) -> dict[str, str]:
        return {"principal": repr(principal)}

    (plain_route,) = get_read_routes(plain)
    assert get_credential_kind(plain_route) == CREDENTIAL_ANY, "an any-credential gate with no JWT dependency must classify as any-credential"


def test_route_level_permission_gates_are_visible() -> None:
    """A permission gate declared outside the endpoint signature is still seen.

    The pre-FAR-1969 ``get_permission_tag`` read only the endpoint signature, so
    a gate on the route decorator / router ``dependencies=[...]`` was invisible.
    This asserts the guard now sees every route-level permission tag — and that
    at least one such route exists, so the check cannot pass vacuously.
    """
    checked = 0
    invisible = []
    for route in get_all_apiroutes(app):
        declared = sorted(d.permission for d in (getattr(route, "dependencies", None) or []) if getattr(d, "permission", None) is not None)
        if not declared:
            continue
        checked += 1
        tag = get_permission_tag(route)
        seen = {t["permission"] for t in (tag or {}).get("tags", [])}
        missing = [p for p in declared if p not in seen]
        if missing:
            invisible.append(f"{route.path}: {missing}")

    assert checked > 0, "no route-level permission gates found — the guard is vacuous"
    assert not invisible, "Route-level permission gates invisible to get_permission_tag:\n" + "\n".join(invisible)


def test_verification_key_route_level_gate_is_visible() -> None:
    """FAR-1969 regression pin for the route-level-gate blind spot.

    ``GET /api/v1/admin/audit/verification-key`` is gated ONLY by a route-level
    ``require_permission("audit.manage")`` — it has no permission dependency in
    its endpoint signature. Before the fix, ``get_permission_tag`` returned
    ``None`` for it (the guard read a gated route as untagged). If the
    route-dependency read regresses, this fails.
    """
    route = next(
        (r for r in get_all_apiroutes(app) if r.path == "/api/v1/admin/audit/verification-key" and "GET" in (r.methods or set())),
        None,
    )
    assert route is not None, "verification-key route not found — the fixture moved"
    tag = get_permission_tag(route)
    assert tag is not None, "verification-key route-level permission gate is invisible to get_permission_tag"
    tags = tag.get("tags", [tag])
    assert any(t["permission"] == "audit.manage" and t["permission_kind"] == "tenant" for t in tags), f"verification-key should carry the route-level audit.manage (tenant) tag; got {tags}"


def test_permission_keys_resolve_in_registry() -> None:
    """Every tagged permission key resolves in PERMISSIONS (no typos)."""
    tagged = 0
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag:
            tagged += 1
            resolved = get_resolved_min_role(tag["permission"])  # raises if key missing
            assert resolved, f"empty min-role for permission {tag['permission']} on {route.path}"
    assert tagged > 0, "no tagged mutating routes found — introspection sweep is vacuous"


def test_variant_kinds() -> None:
    """Variant-kind distribution sanity: org-scoped CRUD uses tenant, run
    trigger/status use tenant_or_api_key, admin strict uses system."""
    kinds = Counter()
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag:
            kinds[tag["permission_kind"]] += 1
    # The three expected kinds must be present.
    assert "tenant" in kinds, f"no tenant-kind routes; got {dict(kinds)}"
    assert "tenant_or_api_key" in kinds, f"no tenant_or_api_key routes; got {dict(kinds)}"
    assert "system" in kinds, f"no system-kind routes; got {dict(kinds)}"


def test_scoped_hybrid_min_roles() -> None:
    """admin_email/admin_orgs scoped-hybrid routes carry the right min_role."""
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag and tag["permission_kind"] == "scoped_hybrid":
            perm = tag["permission"]
            assert perm in (
                "org.email.view",
                "org.email.manage",
                "org.license.view",
                "org.license.manage",
                "org.authz_enforce.manage",
                "org.triggers.pause.manage",
                "org.guardrails.kill_switch.manage",
            ), f"unexpected scoped-hybrid permission {perm} on {route.path}"
            if perm in ("org.email.view", "org.license.view"):
                assert tag["min_role"] == "operator", f"{perm} should be operator, got {tag['min_role']}"
            else:
                assert tag["min_role"] == "admin", f"{perm} should be admin, got {tag['min_role']}"


def test_run_trigger_status_use_any_credential() -> None:
    """API-key REST triggering (PRD 5.2) uses the any-credential variant.

    Only the explicit trigger/status endpoints accept `mk_` keys over REST;
    `ws-token` (which reuses `run.status`) is a user-JWT mint endpoint and
    stays on the tenant variant.
    """
    api_key_paths = {
        "/api/v1/runs",
        "/api/v1/runs/{run_id}",
    }
    for route in get_mutating_routes(app):
        tag = get_permission_tag(route)
        if tag and tag["permission"] in ("run.trigger", "run.status") and route.path in api_key_paths:
            assert tag["permission_kind"] == "tenant_or_api_key", f"{route.path} should use tenant_or_api_key, got {tag['permission_kind']}"


def test_team_membership_gate_present_on_team_pipeline_routes() -> None:
    """PUT/graph, the node patch + DELETE carry the team-scope gate.

    The node-conversion pair (convert-to-agent / revert-to-manual) used to be
    the odd pair out: they mutated a pipeline's graph with NEITHER this
    request-time dependency nor an in-txn re-check, while their sibling graph
    writes had both. The enumeration is deliberate - a route listed here that
    ever loses its ``team_scope`` tag fails this test.

    ``/graph/nodes/{node_id}`` (FAR-1829) is enumerated for the same reason:
    it is a graph write like any other, so losing the gate there would be a
    silent privilege widening rather than a missing assertion.
    """
    for route in get_mutating_routes(app):
        if route.path in (
            "/api/v1/pipelines/{pipeline_id}",
            "/api/v1/pipelines/{pipeline_id}/graph",
            "/api/v1/pipelines/{pipeline_id}/graph/nodes/{node_id}",
            "/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/convert-to-agent",
            "/api/v1/pipelines/{pipeline_id}/nodes/{node_id}/revert-to-manual",
        ):
            tag = get_permission_tag(route)
            assert tag is not None, f"{route.path} missing permission tag"
            kinds = {t["permission_kind"] for t in tag.get("tags", [tag])}
            assert "team_scope" in kinds, f"{route.path} missing team_scope gate; tags={tag.get('tags')}"


def test_no_duplicate_route_registrations() -> None:
    """Every (method, path) is registered exactly once.

    A route stacked with two identical ``@router.<method>(...)`` decorators
    (e.g. around ``@handle_db_errors``) registers twice. Starlette matches the
    FIRST-registered handler, so the raw, unwrapped function wins and the
    ``handle_db_errors`` wrapper becomes dead code — plus the route is
    duplicated in the OpenAPI schema. Previously fixed in
    admin_monitor_config.py / admin_triggers.py; the same defect was removed
    from events.py, hitl.py, and run_ws.py.
    """
    keys: dict[tuple[str, str], str] = {}
    duplicates: list[tuple[str, str]] = []

    for route in get_all_apiroutes(app):
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            key = (method, route.path)
            if key in keys:
                duplicates.append(key)
            else:
                keys[key] = route.path

    assert not duplicates, "Routes registered more than once (first registration wins, so any error-handling wrapper on the second is dead code):\n" + "\n".join(f"{m} {p}" for m, p in duplicates)


def test_handle_db_errors_wrapper_active_on_regexposed_routes() -> None:
    """The re-exposed HITL/SSE/WS handlers keep the handle_db_errors wrapper.

    Locking the fix from the double-registration cleanup: each previously
    duplicated route must now resolve to the *wrapped* endpoint (the wrapper
    sets ``__wrapped__``), not the raw handler that the old first-registration
    used to shadow.
    """
    wrapped: dict[str, str] = {}

    def _collect(routes: object, prefix: str = "") -> None:
        for r in getattr(routes, "routes", []):
            tn = type(r).__name__
            if tn in ("APIRoute", "APIWebSocketRoute") or "WebSocketRoute" in tn:
                path = (prefix + r.path) if prefix else r.path
                endpoint = getattr(r, "endpoint", None)
                if endpoint is not None and hasattr(endpoint, "__wrapped__"):
                    wrapped[path] = endpoint.__name__
            elif tn == "_IncludedRouter" and hasattr(r, "original_router"):
                _collect(r.original_router, prefix)

    _collect(app)

    expected = {
        "/api/v1/events",
        "/api/v1/runs/{run_id}/hitl/{review_id}/claim",
        "/api/v1/runs/{run_id}/hitl/{review_id}/approve",
        "/api/v1/runs/{run_id}/hitl/{review_id}/approve-with-modification",
        "/api/v1/runs/{run_id}/hitl/{review_id}/reject",
        "/api/v1/runs/{run_id}/hitl/{review_id}/deliver-manual",
        "/api/v1/runs/{run_id}/manual/{review_id}/submit",
        "/api/v1/runs/{run_id}/hitl/pending",
        "/api/v1/hitl/pending",
    }
    missing = sorted(p for p in expected if p not in wrapped)
    assert not missing, f"handle_db_errors wrapper not active on: {missing}"
    ws = [p for p, _ in wrapped.items() if p.endswith("/ws")]
    assert ws, "websocket route should carry the handle_db_errors wrapper"
