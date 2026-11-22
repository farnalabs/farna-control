"""Commercial entitlements — the code-declared capability grants (FAR-2665).

An ENTITLEMENT is a commercial grant: a bundle of runtime feature flags that a
signed licence buys. It is deliberately NOT a feature flag, and the two must not
be conflated again:

* an **entitlement** answers "is this capability bought?" — declared here, in
  code, granted by the signed licence, resolvable WITHOUT a database session;
* a **feature flag** answers "is this behaviour switched on?" (a per-org kill
  switch and a rollout gate) and carries no tier of its own any more.

Before FAR-2665 one runtime flag encoded all three meanings, which is why
``pipeline_delete`` could sit beside ``sso`` and why FAR-2604 happened: the
DB-seeded catalogue replaced the code catalogue, so a code tier change never
reached an existing install. Tier is now a projection of this registry, never a
property a flag declares and never a column the resolver trusts.

RBAC is a THIRD, separate axis: :func:`farna.api.dependencies.require_permission`
answers who-may-do-a-thing INSIDE an entitled capability. Demoting an
entitlement to Community must never widen a role, so an entitlement gate is
always additive to a permission/role gate, never a replacement for one.

The groups (FAR-2665 stage 2). Three named Team capabilities replace a flat
"Team tier":

* Identity      — ``sso``, ``scim``, ``team_rbac``
* Audit and evidence — ``audit_viewer``, ``admin_run_retention``
* Cost governance — ``admin_spend_limits``, ``admin_cost_controls``,
  ``admin_cost_breakdown``

Every flag NOT named by an entitlement is a Community behaviour flag: available
unless a per-org override or a ``DEFAULT_OFF_FLAGS`` entry turns it off.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: The free tier id. Anything else is a paid tier.
COMMUNITY_TIER = "community"

#: The paid tier ids the tier catalogue ships. A paid tier grants EVERY
#: entitlement (the pre-FAR-2665 tier-rank rule). An undeclared tier id grants
#: nothing rather than guessing — a typo must fail closed, exactly as the old
#: rank-0 fallback did.
PAID_TIERS: frozenset[str] = frozenset({"team"})

#: The one paid tier id in the shipped catalogue.
TEAM_TIER = "team"


@dataclass(frozen=True)
class Entitlement:
    """One commercial capability, and the runtime flags it covers.

    ``key`` is the stable identifier the licence and the gates name.
    ``legacy_keys`` are the pre-FAR-2665 licence feature names that map onto
    this capability, so a licence issued under the old names keeps working
    without a reissue. The flags' own names are accepted as licence aliases too
    (a licence naming ``sso`` buys ``identity``).
    """

    key: str
    name: str
    description: str
    flags: tuple[str, ...]
    legacy_keys: tuple[str, ...] = ()


#: The single source of truth for commercial capability. Ordered for display.
ENTITLEMENTS: dict[str, Entitlement] = {
    "identity": Entitlement(
        key="identity",
        name="Identity",
        description="Single sign-on, SCIM provisioning and team-level role-based access control",
        flags=("sso", "scim", "team_rbac"),
        legacy_keys=("sso", "scim", "team_rbac"),
    ),
    "audit_evidence": Entitlement(
        key="audit_evidence",
        name="Audit and evidence",
        description="Tamper-evident audit log viewer, chain verification and run retention",
        flags=("audit_viewer", "admin_run_retention"),
        legacy_keys=("audit_viewer", "admin_run_retention"),
    ),
    "cost_governance": Entitlement(
        key="cost_governance",
        name="Cost governance",
        description="Spend limits, budget controls and cross-team cost breakdown",
        flags=("admin_spend_limits", "admin_cost_controls", "admin_cost_breakdown"),
        legacy_keys=("admin_spend_limits", "admin_cost_controls", "admin_cost_breakdown"),
    ),
}


def all_entitlements() -> tuple[Entitlement, ...]:
    """Every declared entitlement, in display order."""
    return tuple(ENTITLEMENTS.values())


def entitlement_keys() -> frozenset[str]:
    """Every declared entitlement key."""
    return frozenset(ENTITLEMENTS)


def get_entitlement(key: str) -> Entitlement:
    """The entitlement named ``key``.

    Raises ``KeyError`` for an unknown key. Gates call this at import time so a
    typo fails the process rather than silently gating on a capability that
    cannot be granted (the same fail-fast contract as an unknown RBAC
    permission key).
    """
    return ENTITLEMENTS[key]


def entitlement_for_flag(flag_name: str) -> Entitlement | None:
    """The entitlement covering ``flag_name``, or ``None`` for a Community flag."""
    for entitlement in ENTITLEMENTS.values():
        if flag_name in entitlement.flags:
            return entitlement
    return None


def entitlement_key_for_flag(flag_name: str) -> str | None:
    """The entitlement KEY covering ``flag_name``, or ``None``."""
    entitlement = entitlement_for_flag(flag_name)
    return entitlement.key if entitlement is not None else None


def _licence_aliases() -> dict[str, str]:
    """Every accepted licence feature name -> its entitlement key."""
    aliases: dict[str, str] = {}
    for entitlement in ENTITLEMENTS.values():
        for alias in (entitlement.key, *entitlement.flags, *entitlement.legacy_keys):
            aliases[alias] = entitlement.key
    return aliases


def entitlement_keys_for_licence(features: Iterable[str] | None) -> frozenset[str]:
    """Entitlement keys named by a licence's ``features`` list.

    Accepts the new entitlement keys, the covered flag names and the legacy
    licence names (mapped in :attr:`Entitlement.legacy_keys`), so a licence
    minted before the consolidation keeps granting what it bought. Unknown
    names are ignored — a licence naming a retired flag grants nothing.
    """
    if not features:
        return frozenset()
    aliases = _licence_aliases()
    return frozenset(aliases[name] for name in features if name in aliases)


def granted_entitlements(tier: str, licence_features: Iterable[str] | None = None) -> frozenset[str]:
    """The entitlements a plan grants — session-free.

    A paid tier grants EVERY entitlement, which is what the pre-FAR-2665
    tier-rank rule did for a Team licence (every Team flag was active at or
    above the Team rank). A Community (or absent) tier grants only what the
    licence names explicitly, which keeps a per-capability licence working.

    Deliberately session-free: the same function serves the authenticated plan
    context, the pre-auth SSO gates and the admin catalogue, so nobody has to
    read a tier out of the database to know what is bought.
    """
    named = entitlement_keys_for_licence(licence_features)
    if tier in PAID_TIERS:
        return named | entitlement_keys()
    return named


def validate_entitlement_key(key: str) -> None:
    """Raise ``ValueError`` unless ``key`` names a declared entitlement."""
    if key not in ENTITLEMENTS:
        raise ValueError(f"Unknown entitlement '{key}'; declared: {', '.join(sorted(ENTITLEMENTS))}")


def plan_entitlements(plan_ctx: Any) -> frozenset[str]:
    """The entitlements a resolved plan context grants.

    Prefers the context's own ``entitlements()`` (every production plan context
    implements it). A context that predates the protocol falls back to deriving
    the grant from its ``tier()`` — the same rule the registry applies — so a
    test double of ``tier="team"`` grants every capability and a
    ``tier="community"`` double grants none. A minimal double with neither
    method is probed through ``feature_enabled`` on the flags the group covers.
    Every fallback asks the plan, never the org overrides, so none can be
    widened by an org toggle.
    """
    resolver = getattr(plan_ctx, "entitlements", None)
    if callable(resolver):
        resolved = resolver()
        # Only a real grant set counts. A bare test double auto-answers every
        # attribute, so ``entitlements()`` can come back as a Mock; the shape
        # check keeps that from being read as "grants nothing" and routes it to
        # the licence-derived fallbacks below (both of which ask the PLAN).
        if isinstance(resolved, set | frozenset | tuple | list):
            non_strings = [entry for entry in resolved if not isinstance(entry, str)]
            if not non_strings:
                return frozenset(resolved)
    tier_fn = getattr(plan_ctx, "tier", None)
    if callable(tier_fn):
        tier = tier_fn()
        if isinstance(tier, str):
            return granted_entitlements(tier)
    # Last resort for a minimal pre-protocol double: ask the plan whether it
    # enables any flag the group covers. Correct in both directions (a
    # licensed double enables them, an all-disabled double does not) and it
    # still cannot be widened by an org override, because the double answers
    # for the plan, not the org.
    enabled_fn = getattr(plan_ctx, "feature_enabled", None)
    if not callable(enabled_fn):
        return frozenset()
    return frozenset(entitlement.key for entitlement in ENTITLEMENTS.values() if any(enabled_fn(flag) for flag in entitlement.flags))
