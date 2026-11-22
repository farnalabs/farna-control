"""Feature flag registry — behaviour flags with no commercial tier (FAR-2665).

A flag here is BEHAVIOUR ONLY: a per-org kill switch and a rollout gate. The
commercial question ("is this capability bought?") belongs to
:mod:`farna.core.entitlements`, and a flag therefore does not declare a tier.
``FeatureFlag.tier`` remains as a read-only PROJECTION of the entitlement
registry, kept because the admin catalogue, the plan payload and the MCP feature
list all render it; it is derived, never declared and never read from the DB.

Tier vocabulary in the DB tier_catalog (rank order):
    community (0) — Free tier, all features active without a license
    team      (1) — The one paid tier
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, ClassVar, Protocol

from farna.core.entitlements import entitlement_for_flag, entitlement_key_for_flag, granted_entitlements
from farna.core.license import LicenseData, is_parked

logger = logging.getLogger(__name__)


@dataclass
class FeatureFlag:
    name: str
    description: str
    currently_active: bool = False
    depends_on: list[str] | None = None
    # FAR-2302: whether an ORGANISATION ADMIN may set this flag for their own
    # org. Fail-closed default: a flag that does not explicitly opt in is
    # system-administrator-only, so a new sensitive flag can never be opened to
    # org admins by omission. Only flags no entitlement covers are eligible; the
    # route (``api/routes/admin_org_feature_flags.py``) refuses a paid-tier flag
    # regardless of this value.
    org_admin_settable: bool = False

    @property
    def tier(self) -> str:
        """The plan tier this flag is presented under — a DERIVED projection.

        FAR-2665: a flag no longer declares a tier. "team" means the flag is
        covered by an entitlement in :mod:`farna.core.entitlements`; every other
        flag is "community" behaviour. Kept as a property so the admin
        catalogue, the ``/api/v1/plan`` payload and the MCP feature list keep
        their existing wire shape without a per-consumer rewrite — and so no
        consumer can read a tier out of the DB and reintroduce the FAR-2604
        divergence.
        """
        return "team" if entitlement_key_for_flag(self.name) is not None else "community"


_KNOWN_FLAGS: list[FeatureFlag] = [
    # ── Community tier ─────────────────────────────────────────────────
    FeatureFlag(
        name="eval_system",
        description="Built-in eval runner for LLM output quality gates",
    ),
    FeatureFlag(
        name="webhook_trigger",
        description="Trigger pipelines via incoming webhooks",
    ),
    FeatureFlag(
        name="mcp_server",
        description="Expose pipelines as MCP tools",
    ),
    FeatureFlag(
        name="dashboard_charts",
        description="Dashboard trend charts (run activity sparklines)",
        org_admin_settable=True,
    ),
    FeatureFlag(
        name="web_vitals_analytics",
        description="Web Vitals analytics dashboard for monitoring frontend performance",
    ),
    FeatureFlag(
        name="assistant",
        description="Assistant in-app AI assistant",
    ),
    FeatureFlag(
        name="model_backend_management",
        description="Manage LLM backend connections and credentials",
    ),
    FeatureFlag(
        name="user_management",
        description="Basic user management — create, deactivate, and role-assign organisation users",
    ),
    FeatureFlag(
        name="observability",
        description="OpenTelemetry export and LangSmith integration settings",
    ),
    # FAR-2604: these two are Community, not Team. A self-hosted Community
    # install cannot operate without them — the runners page manages the
    # environment profiles every sandbox_agent dispatch resolves, and the
    # email settings are the organisation SMTP configuration that HITL email
    # alerts and readiness alerts send through. Gating either to Team left a
    # Community operator with a page they could reach but a feature they
    # could not use (a "Team Feature" lock plus 402s on every call).
    FeatureFlag(
        name="email_config",
        description="SMTP email configuration for notifications",
    ),
    FeatureFlag(
        name="environment_profiles",
        description="Sandbox environment profiles for code execution",
    ),
    # FAR-2663: schema versioning is provenance, not organisation-scale
    # capability — the audit boundary principle puts it in Community. It was
    # gated on the write path as well as the reads, so a Community author could
    # create a schema shell and could never publish a definition (the editor
    # reported "Schema created but version save failed" with no reason, and the
    # onboarding wizard stopped on a 402).
    FeatureFlag(
        name="schema_version_history",
        description="Version history and diff for schema definitions",
    ),
    # ── Entitled capabilities (Team) ───────────────────────────────────
    # FAR-2665: these flags no longer DECLARE a tier. Each is covered by an
    # entitlement in ``core.entitlements`` (identity / audit_evidence /
    # cost_governance) and is active exactly when the licence grants that
    # entitlement. ``error_tracking`` and ``error_forwarders`` used to live in
    # this block and are now plain Community behaviour flags: they are
    # operational/integration capability, not organisation-scale governance.
    FeatureFlag(
        name="sso",
        description="Single sign-on via OIDC / SAML 2.0 providers",
    ),
    FeatureFlag(
        name="team_rbac",
        description="Team-level role-based access control",
    ),
    FeatureFlag(
        name="audit_viewer",
        description="Tamper-evident audit log viewer",
    ),
    FeatureFlag(
        name="admin_spend_limits",
        description="Per-organisation daily spend limits and budgets",
    ),
    FeatureFlag(
        name="admin_cost_controls",
        description="Budget overview, team budgets, alert thresholds, and billing settings",
    ),
    FeatureFlag(
        name="scim",
        description="SCIM 2.0 user and group provisioning",
    ),
    FeatureFlag(
        name="admin_cost_breakdown",
        description="Monthly cost breakdown and anomaly detection across teams",
    ),
    FeatureFlag(
        name="admin_run_retention",
        description="Configure run retention policies and manual purge",
    ),
    # ── Community tier — operational / integration capability (FAR-2665) ──
    # FAR-2665 lowered these five from Team. Each is how a self-hoster operates
    # and integrates their own install — runtime settings, rate limits, plugin
    # management and error tracking/forwarding — not organisation-scale
    # governance, so withholding them stranded a Community operator on pages
    # they could reach but not use. They are Community by not being named by any
    # entitlement, and stay per-org kill-switchable behaviour flags.
    FeatureFlag(
        name="error_tracking",
        description="External error tracking and alerting integrations",
    ),
    FeatureFlag(
        name="error_forwarders",
        description="Dispatch errors to external services via webhooks",
    ),
    FeatureFlag(
        name="rate_limits",
        description="Configure API rate limits",
    ),
    FeatureFlag(
        name="runtime_config",
        description="Runtime configuration overrides",
    ),
    FeatureFlag(
        name="plugin_management",
        description="Manage plugins, connectors, and node categories",
    ),
    # ── Community tier — analytics page (live; frontend shipped in PR #747) ─
    FeatureFlag(
        name="analytics_page",
        description="Run analytics dashboard (rolling-window run/cost/quality series)",
        org_admin_settable=True,
    ),
    # ── Community tier — user-scoped MCP keys (default ON, FAR-2301) ────────
    # Default ON since FAR-2301: a human-linked user-scoped key is already
    # quota-limited, TTL-capped, live-role clamped and attribution-bearing, so
    # the flag is a per-org kill switch rather than an opt-in gate. An org
    # override of ``false`` still disables it (mint and auth both deny).
    FeatureFlag(
        name="user_scoped_mcp_keys",
        description="Per-user MCP API keys (keys operate as their creator's identity)",
        org_admin_settable=True,
    ),
    # ── Community tier — API-key grant-sets (default OFF, FAR-1477 / ADR 058) ──
    FeatureFlag(
        name="api_key_grants",
        description="Explicit capability grant-sets on API keys (role becomes a bundle over grants)",
        org_admin_settable=True,
    ),
    # ── Community tier - SSO unrestricted JIT provisioning (default OFF, FAR-855) ──
    FeatureFlag(
        name="sso_unrestricted_provisioning",
        description=("Allow SSO providers to auto-provision ANY authenticated identity into the organisation (auto_provision=true with no allowed_domains). DANGEROUS: self-service org join for every IdP account. Intentionally not plan-tier gated."),
    ),
    # Retained in the registry only so persisted/catalogued organisations can
    # upgrade without losing compatibility; retired flags are filtered from all
    # customer-facing catalogue and plan responses below.
    FeatureFlag(
        name="lifecycle_map_journeys",
        description="Retired internal Lifecycle Map journey display",
    ),
    # ── Community tier — System Maps authoring and published read model ────────
    # FAR-2595: authoring is a clearly-labelled Beta-preview surface, not
    # dev-mode-only. It stays default-OFF so each organisation opts in, but it
    # is org-admin settable and visible in the customer-facing plan payload.
    FeatureFlag(
        name="system_maps",
        description="Beta preview: System Maps authoring (discovery and map editing)",
        org_admin_settable=True,
    ),
    FeatureFlag(
        name="system_maps_read",
        description="Published System Maps read model",
    ),
    # ── Community tier — webhook notification delivery log (default ON, FAR-2235) ──
    FeatureFlag(
        name="webhook_notification_log",
        description="Webhook notification delivery log (admin delivery-log UI)",
        org_admin_settable=True,
    ),
    # ── Community tier — library collections (FAR-760, default OFF) ───────
    FeatureFlag(
        name="library_collection",
        description="Library collections — pin multiple primitives into a browsable bundle",
        org_admin_settable=True,
    ),
    # ── Community tier — evidence-driven autonomy gating (FAR-1175, default OFF) ──
    FeatureFlag(
        name="autonomy_gating",
        description=("Evidence-driven autonomy gating — a pipeline's HITL gates resolve against its runtime earned autonomy level (demotion bites in-flight runs at the next gate; promotion applies only to new runs)"),
        org_admin_settable=True,
    ),
    # ── Community tier — human-linked MCP authority for human_only HITL gates
    # (ADR 063 step 1, FAR-1957; FAR-2281; default OFF). NOT a plan-tier
    # feature: an admin may enable it per-org. The restrictive state is "OFF",
    # so the resolver fails CLOSED to OFF (deny) on any read error.
    FeatureFlag(
        name="mcp_human_linked_hitl_authority",
        description=(
            "Allow a verified human-linked credential to claim, approve or reject "
            "human_only HITL gates over MCP based on persona (ADR 063 step 1, "
            "FAR-2281). Eligible credentials are consent-derived OAuth tokens "
            "and user-scoped human-linked API keys. When disabled, every MCP "
            "caller is denied on human_only gates. deliver_manual remains "
            "browser-only."
        ),
    ),
]


# community=Free, team=one paid tier
TIER_RANK: dict[str, int] = {"community": 0, "team": 1}


def declared_feature_tier(feature_name: str) -> str | None:
    """The tier PRESENTED for ``feature_name``, or ``None`` if unknown.

    FAR-2665: derived from the entitlement registry, never declared by the flag
    and never read from the DB. A consumer that only has a resolved plan (e.g.
    the 402 detail builder) can still tell a commercial restriction from a
    default-off behaviour flag without a session or a registry instance. An
    unknown name returns ``None`` so callers fail closed instead of inventing an
    entitlement.
    """
    for flag in _KNOWN_FLAGS:
        if flag.name == feature_name:
            return flag.tier
    return None


# Flags shipped default-OFF on every tier regardless of tier rank. Each is
# listed in the catalog for per-org toggling AND hardcoded here so the
# community-tier tier-rank fallback in ``FeatureFlagRegistry._refresh`` can
# never activate it. Orgs enable them per-org via ``feature_overrides``; a
# system-level ``set_override`` also wins.
# Retained only in persisted data for backwards compatibility; it is not a
# customer-configurable flag. Internal Lifecycle Map access is controlled by the
# route's dev-mode visibility gate instead of an org feature override.
#
# FAR-2663 retired ``pipeline_delete`` outright (it is no longer in
# ``_KNOWN_FLAGS``): deleting a pipeline was never backend-gated — ``DELETE
# /api/v1/pipelines/{id}`` is guarded by the Community RBAC permission
# ``pipeline.delete`` and soft-deletes, which is the correct control. The flag
# only hid two buttons in the UI, and migration 0360 deletes the stale catalog
# row so the admin Feature Flags surface cannot advertise a flag that gates
# nothing.
RETIRED_INTERNAL_FLAGS: frozenset[str] = frozenset({"lifecycle_map_journeys"})

# Internal-only flags that are visible/enableable ONLY in dev-mode for
# internal operators (system admin + operator role + in-dev grant). These are
# filtered from customer-facing catalogue and plan responses (see
# effective_feature_flags, get_available_features MCP tool).
#
# FAR-2595: the set is currently empty. ``system_maps`` left it (its authoring
# surface is a Beta-preview feature behind the default-OFF ``system_maps``
# flag, not a dev-mode-only internal surface), and the retained
# ``lifecycle_map_journeys`` lives in ``RETIRED_INTERNAL_FLAGS``. Keeping the
# two sets disjoint avoids every reader having to union/duplicate them; a
# future internal-only flag that is NOT retired belongs here.
INTERNAL_FLAGS: frozenset[str] = frozenset()


DEFAULT_OFF_FLAGS: frozenset[str] = frozenset(
    {
        "library_collection",
        "sso_unrestricted_provisioning",
        "api_key_grants",
        "autonomy_gating",
        "mcp_human_linked_hitl_authority",
        "system_maps",
    }
)


# FAR-2302: the curated set of flags an ORGANISATION ADMIN may set for their
# own organisation. Derived from ``_KNOWN_FLAGS`` so the classification is
# declared once, on the flag itself, and cannot drift from a second hand-
# maintained list. Every other flag — including every team-tier flag and the
# sensitive community-tier flags (SSO unrestricted provisioning, web vitals,
# observability, SCIM, error forwarders, runtime config, rate limits, assistant
# UI driving) — stays system-administrator-only. A future sensitive org-scoped
# flag (telemetry/metrics export) must be left
# ``org_admin_settable=False`` (the default) until it is deliberately opened.
ORG_ADMIN_SETTABLE_FLAGS: frozenset[str] = frozenset(flag.name for flag in _KNOWN_FLAGS if flag.org_admin_settable)


class CommunityTier:
    """Default plan — community-tier features active without a license key.
    Backward-compatible class satisfying the PlanContext protocol.
    """

    def __init__(self) -> None:
        self._registry = FeatureFlagRegistry(current_tier="community", has_license_key=False)

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag (active and inactive) for this context's tier state."""
        return self._registry.list_flags()

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key

    def entitlements(self) -> frozenset[str]:
        """The commercial capabilities this plan grants (FAR-2665)."""
        return self._registry.entitlements


class LicenseKeyTier:
    """Licensed plan — activates features based on license tier and explicit feature list.
    Backward-compatible class satisfying the PlanContext protocol.
    """

    def __init__(self, license_data: LicenseData) -> None:
        self._tier = license_data.tier
        self._features = set(license_data.features)
        self._registry = FeatureFlagRegistry(
            current_tier=license_data.tier,
            has_license_key=True,
            entitlements=granted_entitlements(license_data.tier, license_data.features),
        )

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active or name in self._features

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active or f.name in self._features]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag; licence-named features read active regardless of tier state."""
        return [replace(flag, currently_active=True) if flag.name in self._features else flag for flag in self._registry.list_flags()]

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key

    def entitlements(self) -> frozenset[str]:
        """The commercial capabilities this plan grants (FAR-2665)."""
        return self._registry.entitlements


class PlanContext(Protocol):
    """Interface for plan-based feature gating."""

    def feature_enabled(self, name: str) -> bool: ...

    def list_enabled_features(self) -> list[FeatureFlag]: ...

    def list_all_features(self) -> list[FeatureFlag]: ...

    def tier(self) -> str: ...

    def has_license_key(self) -> bool: ...

    def entitlements(self) -> frozenset[str]: ...


class DbPlanContext:
    """Plan context resolved from the DB-backed tier catalog."""

    def __init__(self, registry: FeatureFlagRegistry) -> None:
        self._registry = registry

    @classmethod
    async def from_db(
        cls,
        session: Any,
        plan_id: str,
        has_license_key: bool = False,
        license_features: set[str] | None = None,
    ) -> DbPlanContext:
        """Resolve a plan context from the DB feature catalog.

        FAR-2665: the licence's named capabilities are folded in as ENTITLEMENT
        grants (``license_features`` accepts the new group keys, the covered
        flag names and the legacy licence names), so a per-capability licence
        unlocks exactly its group rather than a tier.
        """
        registry = await FeatureFlagRegistry.from_db(
            session,
            plan_id,
            has_license_key,
            license_features=license_features,
        )
        return cls(registry)

    def tier(self) -> str:
        return self._registry.current_tier

    def has_license_key(self) -> bool:
        return self._registry.has_license_key

    def entitlements(self) -> frozenset[str]:
        """The commercial capabilities this plan grants (FAR-2665)."""
        return self._registry.entitlements

    def feature_enabled(self, name: str) -> bool:
        flag = self._registry.get_flag(name)
        if flag is None:
            return False
        return flag.currently_active

    def list_enabled_features(self) -> list[FeatureFlag]:
        return [f for f in self._registry.list_flags() if f.currently_active]

    def list_all_features(self) -> list[FeatureFlag]:
        """Every known flag (active and inactive); licence features are folded in by ``from_db``."""
        return self._registry.list_flags()


def _verified_license_key(raw_key: str, error_log: str) -> LicenseData | None:
    """Verify one key rung of the licence ladder; ``None`` when absent or invalid.

    Session-free. An absent key resolves to ``None`` without touching the
    verifier; an unexpected parse failure is logged under ``error_log`` and
    treated as absent — the exact fall-through behaviour (and per-rung
    observability: each rung passes its own ``error_log``) of the key rungs of
    ``resolve_effective_license``, the shared ladder helper, which resolves
    both key rungs through ``_verified_license_key`` (this per-key verifier).
    Cancellation propagates (``CancelledError`` is a ``BaseException``, never
    swallowed by the ``except Exception``).

    A key carrying a FUTURE ``starts_at`` (FAR-2146) verifies but is not yet
    active: it resolves to ``None`` here, so the rung falls through and the
    instance keeps running on whatever licence is in force today. Without this gate a
    configured env-var or org-level successor would grant Team capability the
    day it was written into configuration, before the window the customer
    bought — the free-time defect the future-only rule exists to prevent. It
    becomes effective automatically once ``starts_at`` arrives, because the
    gate is evaluated on every resolution.
    """
    from farna.core.license import parse_and_verify

    if not raw_key:
        return None
    try:
        validation = parse_and_verify(raw_key)
    except Exception:
        logger.warning(error_log, exc_info=True)
        return None
    if validation.valid and validation.license_data is not None:
        if is_parked(validation.license_data):
            logger.info(
                "license.key_parked_until_start org_id=%s starts_at=%s",
                validation.license_data.org_id,
                validation.license_data.starts_at,
            )
            return None
        return validation.license_data
    return None


def _license_claims_org(data: LicenseData, org: Any) -> bool:
    """Whether an org-scoped licence may license ``org`` (FAR-2060).

    An empty ``org_id`` claim is instance-wide and applies anywhere. A non-empty
    claim names ONE organisation, and applying it to any other tenant would let
    org B's key license org A — so the claim is enforced here, at the point the
    key is APPLIED, not only at install time (a stored key can predate this
    check, and ``parse_and_verify`` never inspects the claim).
    """
    claimed = getattr(data, "org_id", "") or ""
    if not claimed:
        return True
    return claimed == str(getattr(org, "id", ""))


def resolve_effective_license(settings: Any, org: Any | None) -> LicenseData | None:
    """The canonical licence ladder, session-free — first valid rung wins.

    Rungs (absent/invalid falls through; a key is verified via
    ``parse_and_verify``, never trusted on mere presence — FAR-1733):

    1. Org-level key (``org.settings_json["license_key"]``) — applied ONLY when
       its ``org_id`` claim is empty or names this org (FAR-2060). A mismatch is
       treated as absent at this rung: the key is real, but it licenses a
       different tenant.
    2. System-level in-memory licence (``store_license()``) — the runtime install
       persisted in ``system_config`` (FAR-2153) and restored at startup. This
       rung OVERRIDES rung 3: an explicit operator install beats the deployment
       env key, and removing it falls back to the env key.
    3. System-level env-var key (``settings.license_key``) — the
       deployment FLOOR.

    ONE walk, shared by ``_resolve_paid_plan`` (the plan context built here),
    ``admin_license._resolve_effective_license`` (the admin licence view) and
    ``viewmodel._resolve_license_expiry`` / ``_resolve_tier`` /
    ``_resolve_license_info`` (the member-facing plan bootstrap), so a future
    rung change cannot silently drift between the copies. Returns ``None`` when
    no valid licence resolves.

    Grace: capability expiry is ``expires_at + LICENCE_GRACE_DAYS`` (a fixed,
    non-configurable delivery window in ``farna.core.license``), so a key that
    expired fewer than ``LICENCE_GRACE_DAYS`` ago still resolves here and keeps
    granting its tier. Past the grace every rung falls through to the
    unlicensed (Community) default — the lapse is never a lockout, because
    authentication does not depend on this ladder.

    A key whose signed ``starts_at`` is still in the future (FAR-2146) also
    falls through: it is valid but not yet active, so it must not grant
    capability early. It starts resolving here automatically once its start
    arrives — no operator action, because the gate is evaluated per call.

    Local imports keep ``farna.core.license.{get_license,parse_and_verify}``
    patchable at call time (tests patch the source module, not this one).
    """
    from farna.core.license import get_license

    # 1. Org-level license key
    if org is not None:
        org_settings = getattr(org, "settings_json", None)
        org_key = org_settings.get("license_key") if isinstance(org_settings, dict) else None
        if org_key:
            data = _verified_license_key(str(org_key), "Failed to parse org-level license key")
            if data is not None and _license_claims_org(data, org):
                return data
            if data is not None:
                logger.warning(
                    "license.org_id_mismatch",
                    extra={
                        "org_id": str(getattr(org, "id", "")),
                        "claimed_org_id": data.org_id,
                    },
                )

    # 2. System-level in-memory license (stored pre-verified; expiry re-checked by get_license)
    lic = get_license()
    if lic is not None:
        return lic

    # 3. System-level env-var license
    return _verified_license_key(
        getattr(settings, "license_key", "") or "",
        "Failed to parse env-var license key",
    )


async def _resolve_paid_plan(settings: Any, session: Any, org: Any | None) -> PlanContext | None:
    """Resolve a licensed (paid) plan context, or ``None`` when no valid license exists.

    License ladder (first valid source wins, absent/invalid falls through):
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level in-memory license (``store_license()``; the
       ``system_config`` install restored/refreshed from persistence)
    3. System-level env-var license (``settings.license_key``)

    Rung 2 OVERRIDES rung 3: the deployment's env key is the FLOOR and an
    explicit operator install is the override, so removing an install falls back
    to the env key (FAR-2153). The persisted system licence is re-read here (TTL
    throttled) before the walk, so a licence installed by another process
    becomes visible in this one without a restart.

    The rung WALK is the shared session-free ``resolve_effective_license`` —
    only the session-backed context build is local to this function — so this
    ladder cannot drift from the admin licence view or ``GET /api/v1/plan``.
    """
    if session is not None:
        # FAR-2153: cross-process visibility. TTL-throttled and fail-open, so
        # this is at most one indexed read per window and a DB blip cannot turn
        # a plan resolution into an error.
        from farna.core.license import refresh_persisted_system_license

        await refresh_persisted_system_license(session)
    data = resolve_effective_license(settings, org)
    if data is None:
        return None
    return await DbPlanContext.from_db(
        session,
        data.tier,
        has_license_key=True,
        license_features=set(data.features),
    )


async def _resolve_trial_state(session: Any, org: Any | None) -> PlanContext:
    """Resolve the unlicensed fallback state once no valid license matched.

    4. Org-level ``plan_id`` — used ONLY for the free "community" tier. Any
       higher tier (e.g. "team") requires a VALID SIGNED LICENSE; the license
       ladder in ``_resolve_paid_plan`` already returned when one exists, so
       reaching here with a paid plan_id means NO license is present. Downgrade
       to community instead of silently granting the paid tier from a bare
       plan_id.
    5. Community tier (default fallback)
    """
    if org is not None:
        org_plan_id: str | None = getattr(org, "plan_id", None)
        if org_plan_id:
            if org_plan_id == "community":
                return await DbPlanContext.from_db(session, "community")
            logger.info(
                "plan.team_without_license_falling_back_to_community",
                extra={"org_plan_id": org_plan_id},
            )

    # 5. Community fallback
    return await DbPlanContext.from_db(session, "community")


async def resolve_plan_context(settings: Any, session: Any, org: Any | None = None) -> PlanContext:
    """Resolve a PlanContext from an org-level license, system-level license, or CommunityTier.

    Resolution order:
    1. Org-level license key (from ``org.settings_json["license_key"]``)
    2. System-level in-memory license (``store_license()``)
    3. System-level env-var license (``settings.license_key``)
    4. Org-level ``plan_id`` — used ONLY for the free "community" tier. Any
       higher tier (e.g. "team") requires a VALID SIGNED LICENSE; a bare
       ``plan_id`` with no license (steps 1-3 all empty) resolves to community.
    5. Community tier (default fallback)

    Steps 1-3 delegate to ``_resolve_paid_plan`` (license ladder); steps 4-5
    delegate to ``_resolve_trial_state`` (unlicensed fallback).
    """
    licensed = await _resolve_paid_plan(settings, session, org)
    if licensed is not None:
        return licensed
    return await _resolve_trial_state(session, org)


def feature_overrides_from_settings(settings_json: Any) -> dict[str, bool]:
    """The org ``feature_overrides`` map (bool entries only) from a settings dict.

    Shared by the admin feature-flag list (``admin_feature_flags._read_org_overrides``)
    and the member-facing plan bootstrap (``viewmodel._org_feature_overrides``)
    so the bool-only rule cannot drift between them: only ``bool`` entries count
    as an override (``1``/``0`` and other truthy junk are ignored), and a
    missing or malformed settings / ``feature_overrides`` value resolves to
    ``{}`` — junk settings can never flip a flag's reported state.
    """
    if not isinstance(settings_json, dict):
        return {}
    overrides = settings_json.get("feature_overrides")
    if not isinstance(overrides, dict):
        return {}
    return {key: bool(value) for key, value in overrides.items() if isinstance(value, bool)}


def effective_feature_flags(
    plan_ctx: PlanContext,
    org_overrides: Mapping[str, bool] | None = None,
) -> list[FeatureFlag]:
    """The full known flag set with the org ``feature_overrides`` overlay applied.

    FAR-1806: the caller-scoped effective flag payload served to any
    authenticated org member (``GET /api/v1/plan`` and, there,
    ``/viewmodel/current``'s ``feature_flags``). Two gaps make
    ``PlanContext.list_enabled_features()`` unusable as a bootstrap payload:

    1. It is active-only — inactive flags are dropped entirely, so the client
       cannot tell "off" from "unknown" and default-OFF chrome never resolves.
    2. No resolution step overlays the org's ``feature_overrides``; only the
       admin feature-flag list applied that overlay (manually), which is why
       the admin endpoint and ``viewmodel/current`` disagreed on the same org.

    This helper closes both: it starts from ``list_all_features()`` (the full
    known set with tier state computed by the plan context) and overlays
    ``org_overrides`` exactly as the admin list endpoint does, so this payload
    and the admin endpoint report the same effective state for an org.
    Overlay entries naming unknown flags are ignored (matching the admin
    list); inactive flags stay present with ``currently_active=False``.

    Precedence note: the org overlay lands ON TOP of the plan context's state,
    mirroring the admin list — any system-level registry override
    (``FeatureFlagRegistry.set_override``) is already folded into each flag's
    ``currently_active`` by the registry before this overlay applies.

    Internal-only flags (``INTERNAL_FLAGS``) are filtered from customer-facing
    catalogue and plan responses — they are visible only in dev-mode to internal
    operators via the admin feature-flag endpoints and the MCP ``get_available_features``
    tool.

    Returns copies (``dataclasses.replace``) for overlaid flags so the plan
    context's own registry state is never mutated.
    """
    internal_and_retired = RETIRED_INTERNAL_FLAGS | INTERNAL_FLAGS
    flags = [flag for flag in plan_ctx.list_all_features() if flag.name not in internal_and_retired]
    if not org_overrides:
        return flags
    return [replace(flag, currently_active=org_overrides[flag.name]) if flag.name in org_overrides else flag for flag in flags]


class FeatureFlagRegistry:
    """Knows all feature flags and derives active status from granted ENTITLEMENTS.

    FAR-2665: the registry no longer ranks flags by a declared tier. A flag
    covered by an entitlement is active only when the plan grants that
    entitlement; a flag no entitlement covers is Community behaviour and is
    active unless it is default-OFF or explicitly overridden. The DB catalogue
    supplies ``is_active`` (the operator kill switch) — never a tier: tier is a
    projection of :mod:`farna.core.entitlements`, so a code tier change can no
    longer fail to reach an existing install (FAR-2604).
    """

    _overrides: ClassVar[dict[str, bool]] = {}

    def __init__(
        self,
        current_tier: str = "community",
        has_license_key: bool = False,
        entitlements: Iterable[str] | None = None,
    ) -> None:
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        # ``None`` means "derive from the tier" (a paid tier grants every
        # entitlement). A caller that resolved a licence passes the explicit set
        # so a per-capability key keeps granting only what it names.
        self._entitlements = frozenset(entitlements) if entitlements is not None else granted_entitlements(current_tier)
        self._flags = [FeatureFlag(**asdict(f)) for f in _KNOWN_FLAGS]
        self._refresh()

    @property
    def entitlements(self) -> frozenset[str]:
        """The entitlement keys this registry grants (FAR-2665)."""
        return self._entitlements

    async def _load_catalog(self, session: Any) -> None:
        """Read the tier/feature-flag catalog rows into this registry.

        Callers must guarantee an active transaction (``load_from_db`` wraps
        this in ``session.begin()`` unless one is already active).
        """
        from farna.db.crud.tier_catalog import list_feature_flags, list_tiers

        db_tiers = await list_tiers(session)
        if db_tiers:
            self._tier_rank = {t["tier_id"]: t["rank"] for t in db_tiers}

        db_flags = await list_feature_flags(session)
        if db_flags:
            # Keep ALL catalog rows, including ``is_active=false`` ones, so the
            # admin UI can list and toggle them. Rows seeded inactive (e.g. the
            # Inactive catalog flags are tracked in
            # ``_inactive_flags`` and stay ``currently_active=False`` in
            # ``_refresh`` unless an override exists.
            self._flags = [
                FeatureFlag(
                    name=f["name"],
                    description=f["description"],
                    # FAR-2665: the catalog's ``tier_id`` is deliberately NOT
                    # read. Tier is a projection of the entitlement registry,
                    # so a stale DB row cannot hold a capability back (the
                    # FAR-2604 defect). ``is_active`` is still the catalog's
                    # job — it is the per-install kill switch.
                    depends_on=f["depends_on"],
                    # FAR-2302: settability is a CODE-level classification (it
                    # is not a DB catalog column), so carry it from
                    # ``_KNOWN_FLAGS`` when the catalog row replaces the
                    # hardcoded entry. A catalog-only flag is not in
                    # ``ORG_ADMIN_SETTABLE_FLAGS`` and therefore defaults to
                    # system-admin-only (fail closed).
                    org_admin_settable=f["name"] in ORG_ADMIN_SETTABLE_FLAGS,
                )
                for f in db_flags
            ]
            self._inactive_flags = {f["name"] for f in db_flags if not f["is_active"]}

    async def load_from_db(self, session: Any) -> None:
        """Replace hardcoded flag data with DB-backed data from tier_catalog / feature_flag_catalog.

        The catalog reads are wrapped in an explicit transaction so callers
        using ``autobegin=False`` sessions (the DI default) never hit
        ``InvalidRequestError`` from a bare ``session.execute``. When the
        caller already manages an active transaction the reads run inside it
        (a nested ``session.begin()`` would raise).
        """
        in_transaction = session.in_transaction()
        if asyncio.iscoroutine(in_transaction):
            # Test doubles may expose an async ``in_transaction``; await it.
            in_transaction = await in_transaction
        if in_transaction:
            await self._load_catalog(session)
        else:
            async with session.begin():
                await self._load_catalog(session)
        self._refresh()

    @classmethod
    async def from_db(
        cls,
        session: Any,
        current_tier: str,
        has_license_key: bool = False,
        license_features: Iterable[str] | None = None,
    ) -> FeatureFlagRegistry:
        """Create a registry pre-loaded from the DB feature catalog.

        The DB supplies the operator kill switch (``is_active``) and row set;
        the tier/entitlement projection comes from code plus the licence's
        named capabilities (``license_features``), so no DB tier is consulted.
        """
        instance = cls(
            current_tier=current_tier,
            has_license_key=has_license_key,
            entitlements=granted_entitlements(current_tier, license_features),
        )
        await instance.load_from_db(session)
        return instance

    @property
    def current_tier(self) -> str:
        return self._current_tier

    @property
    def has_license_key(self) -> bool:
        return self._has_license_key

    def _effective_tier_ranks(self) -> dict[str, int]:
        """The tier -> rank map this registry ranks flags against.

        Prefers the DB-backed catalog ranks loaded by ``_load_catalog`` and
        falls back to the hardcoded ``TIER_RANK`` before/without a DB load, so
        every rank comparison uses the same map that produced
        ``currently_active``.
        """
        return getattr(self, "_tier_rank", TIER_RANK)

    def tier_rank(self, tier: str) -> int:
        """Rank of ``tier`` using this registry's effective rank map.

        Unknown tiers rank 0 (community-equivalent), matching ``_refresh``.
        """
        return self._effective_tier_ranks().get(tier, 0)

    def _refresh(self) -> None:
        # Flags seeded ``is_active=false`` in the DB catalog (experiments that
        # must ship default-OFF everywhere). ``__init__`` runs ``_refresh()``
        # before ``_load_catalog``, so guard with getattr. The hardcoded
        # ``DEFAULT_OFF_FLAGS`` set wins over the entitlement grant AND over a
        # DB catalog row marked active — only an explicit override (org
        # ``feature_overrides`` / system ``set_override``) can turn them on.
        inactive: set[str] = getattr(self, "_inactive_flags", set()) | DEFAULT_OFF_FLAGS

        for flag in self._flags:
            if flag.name in inactive:
                flag.currently_active = False
            else:
                covering = entitlement_for_flag(flag.name)
                # A flag no entitlement covers is Community behaviour: available
                # unless switched off. A covered flag follows its entitlement.
                flag.currently_active = covering is None or covering.key in self._entitlements

            override = self._overrides.get(flag.name)
            if override is not None:
                flag.currently_active = override

    def refresh(
        self,
        current_tier: str,
        has_license_key: bool,
        entitlements: Iterable[str] | None = None,
    ) -> None:
        """Re-resolve the plan and recompute active state.

        ``entitlements=None`` re-derives the grant from the tier (a paid tier
        grants everything), preserving the pre-FAR-2665 ``refresh(tier, ...)``
        call shape for callers that never had a licence feature list.
        """
        self._current_tier = current_tier
        self._has_license_key = has_license_key
        self._entitlements = frozenset(entitlements) if entitlements is not None else granted_entitlements(current_tier)
        self._refresh()

    def list_flags(self) -> list[FeatureFlag]:
        return list(self._flags)

    def get_flag(self, name: str) -> FeatureFlag | None:
        for flag in self._flags:
            if flag.name == name:
                return flag
        return None

    def set_override(self, name: str, enabled: bool) -> None:
        self._overrides[name] = enabled
        self._refresh()

    def clear_override(self, name: str) -> None:
        self._overrides.pop(name, None)
        self._refresh()

    def get_override(self, name: str) -> bool | None:
        return self._overrides.get(name)

    def tier_gap_flags(self) -> list[FeatureFlag]:
        """Entitled flags this plan does NOT grant — what a licence would unlock.

        Replaces the tier-rank gap report (FAR-2665): a flag is a gap when an
        entitlement covers it and that entitlement is not granted. Community
        behaviour flags can never be a gap (no purchase unlocks them).
        """
        return [flag for flag in self._flags if (covering := entitlement_for_flag(flag.name)) is not None and covering.key not in self._entitlements and not flag.currently_active]

    async def resolve_flag(
        self,
        flag_name: str,
        org_id: uuid.UUID | None = None,
        *,
        raise_on_error: bool = False,
    ) -> bool:
        """Resolve a flag from two layers: system override > org override > tier default.

        ``raise_on_error=True`` (strict, for enforcement callers that must not
        mistake a transient DB failure for "flag OFF") propagates the org
        override read error instead of falling through to the tier default.
        Default behaviour (swallow + log, fall through) is unchanged.

        Team-level and user-level overrides were removed (FAR-927). The
        resolution order is now:
            system override > org override > tier default.
        """
        sys_override = self._overrides.get(flag_name)
        if sys_override is not None:
            return sys_override

        if org_id is not None:
            org_val = await self._get_org_override(flag_name, org_id, raise_on_error=raise_on_error)
            if org_val is not None:
                return org_val

        for flag in self._flags:
            if flag.name == flag_name:
                return flag.currently_active
        return False

    async def _override_from_entity(
        self,
        flag_name: str,
        load_row_factory: Callable[[], Callable[[Any], Awaitable[Any]]],
        settings_attr: str,
        error_log: str,
        *,
        raise_on_error: bool = False,
    ) -> bool | None:
        """Resolve a ``feature_overrides`` entry from a single org/team/account row.

        Shared session/transaction/error boilerplate for the three entity-scoped
        flag overrides. ``load_row_factory`` returns a session-bound loader for
        the entity (so its lazy crud import happens inside this method's try
        block); ``settings_attr`` names the JSON column that carries
        ``feature_overrides`` (``settings_json`` / ``settings`` / ``preferences``)
        and ``error_log`` is logged when the lookup fails. Returns the override
        value when the entity defines one, else ``None``.
        """
        try:
            from sqlalchemy.ext.asyncio import AsyncSession

            from farna.api.dependencies import get_or_create_engine
            from farna.settings import get_settings

            load_row = load_row_factory()
            engine = get_or_create_engine(get_settings())
            async with AsyncSession(engine, autobegin=False) as session:
                # When the caller already owns an active transaction (the common
                # request path where DI provides the session), run the read inside
                # it — a nested ``session.begin()`` would raise. Otherwise open an
                # explicit transaction: the settings column MUST be read inside the
                # open transaction, because after ``begin()`` commits ORM attributes
                # are expired and reading them triggers a refresh requiring a new
                # transaction, which raises ``InvalidRequestError`` on an
                # ``autobegin=False`` session (FAR-820) — silently nullifying every
                # org/team/user override.
                in_transaction = session.in_transaction()
                if asyncio.iscoroutine(in_transaction):
                    # Test doubles may expose an async ``in_transaction``; await it.
                    in_transaction = await in_transaction
                if in_transaction:
                    entity = await load_row(session)
                    settings = getattr(entity, settings_attr, None) if entity is not None else None
                else:
                    async with session.begin():
                        entity = await load_row(session)
                        settings = getattr(entity, settings_attr, None) if entity is not None else None
                if entity and settings:
                    overrides = settings.get("feature_overrides", {})
                    if flag_name in overrides:
                        return bool(overrides[flag_name])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(error_log)
            if raise_on_error:
                raise
        return None

    async def _get_org_override(self, flag_name: str, org_id: uuid.UUID, *, raise_on_error: bool = False) -> bool | None:
        """Check org.settings_json.feature_overrides for this flag."""

        def _load_factory() -> Callable[[Any], Awaitable[Any]]:
            from farna.db.crud.organisation import get_organisation

            async def _load(session: Any) -> Any:
                return await get_organisation(session, org_id)

            return _load

        return await self._override_from_entity(
            flag_name,
            _load_factory,
            "settings_json",
            "Failed to check org flag override",
            raise_on_error=raise_on_error,
        )


_registry: FeatureFlagRegistry | None = None


def get_registry() -> FeatureFlagRegistry:
    """Return a process-global default FeatureFlagRegistry.

    The registry uses the hardcoded ``_KNOWN_FLAGS`` list with a ``"community"``
    tier.  Granular overrides (org) are resolved from the DB at query
    time via ``resolve_flag()``.
    """
    global _registry
    if _registry is None:
        _registry = FeatureFlagRegistry(current_tier="community")
    return _registry


SSO_UNRESTRICTED_PROVISIONING_FLAG = "sso_unrestricted_provisioning"


async def resolve_sso_unrestricted_provisioning(*, org_id: uuid.UUID | None) -> bool:
    """FAR-855: resolve the operator-level SSO unrestricted-provisioning flag.

    This flag is NOT a plan-tier feature: it is default-OFF on every tier
    (``DEFAULT_OFF_FLAGS``) and only an explicit operator-level override —
    system ``FeatureFlagRegistry.set_override`` or an org
    ``feature_overrides`` entry — can enable it. It gates the DANGEROUS
    "anyone who authenticates" SSO join mode (``auto_provision=true`` with an
    empty ``allowed_domains``).

    SECURITY: fails CLOSED. Any error resolving the flag denies the
    permissive mode, because flag resolution failing must never be the
    thing that widens an auth boundary.
    """
    try:
        registry = get_registry()
        return bool(await registry.resolve_flag(SSO_UNRESTRICTED_PROVISIONING_FLAG, org_id=org_id))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("sso_unrestricted_provisioning flag read failed; failing closed")
        return False


async def resolve_mcp_human_linked_hitl_authority(*, org_id: uuid.UUID | None, flag_name: str) -> bool:
    """Resolve the org-wide human-linked MCP authority flag (ADR 063 step 1).

    ``flag_name`` is supplied by the gate site (``api/mcp_server.py``), so the
    gate names the flag it enforces rather than the resolver hiding the name
    inside this registry module — the architecture reference-integrity guard
    treats a reference in a registry file as no reference at all (FAR-1957).

    This flag is NOT a plan-tier feature: it is **default OFF** on every tier
    (``DEFAULT_OFF_FLAGS``). An org admin may explicitly enable it through
    ``org.settings_json.feature_overrides[flag_name]``. When enabled, a verified
    human-linked credential may decide ``human_only`` HITL gates over MCP based
    on persona; possession remains out of scope.

    SECURITY: fails CLOSED to OFF (deny). The permissive state is ON, so a
    failed org-override read must NOT fall through to the permissive tier
    default — hence ``raise_on_error=True`` (which surfaces the read error
    instead of silently returning ``currently_active``) and the ``except``
    returning False. A flag-resolution failure can never widen the auth
    boundary.
    """
    try:
        registry = get_registry()
        return bool(
            await registry.resolve_flag(
                flag_name,
                org_id=org_id,
                raise_on_error=True,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mcp_human_linked_hitl_authority flag read failed; failing closed")
        return False
