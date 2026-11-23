"""Entitlement registry — the commercial grant declared in code (FAR-2665).

An entitlement is a commercial capability the signed licence buys; a feature
flag is behaviour. These tests pin the separation and the three named Team
groups, and pin the licence-name mapping that lets an old key keep working.
"""

from __future__ import annotations

import pytest

from farna.core.entitlements import (
    ENTITLEMENTS,
    all_entitlements,
    entitlement_for_flag,
    entitlement_key_for_flag,
    entitlement_keys,
    entitlement_keys_for_licence,
    get_entitlement,
    granted_entitlements,
    plan_entitlements,
    validate_entitlement_key,
)
from farna.core.feature_flags import FeatureFlagRegistry

#: The three named Team capabilities (FAR-2665 stage 2).
EXPECTED_GROUPS = {
    "identity": {"sso", "scim", "team_rbac"},
    "audit_evidence": {"audit_viewer", "admin_run_retention"},
    "cost_governance": {"admin_spend_limits", "admin_cost_controls", "admin_cost_breakdown"},
}

#: Flags lowered to Community by FAR-2665 — operational/integration capability,
#: not organisation-scale governance. They must have NO entitlement.
LOWERED_FLAGS = {
    "runtime_config",
    "rate_limits",
    "plugin_management",
    "error_tracking",
    "error_forwarders",
}


class TestRegistryShape:
    def test_three_named_groups(self) -> None:
        assert entitlement_keys() == frozenset(EXPECTED_GROUPS)

    @pytest.mark.parametrize(("key", "flags"), EXPECTED_GROUPS.items())
    def test_group_covers_exactly_its_flags(self, key: str, flags: set[str]) -> None:
        assert set(get_entitlement(key).flags) == flags

    def test_every_entitlement_has_a_stable_display_name(self) -> None:
        for entitlement in all_entitlements():
            assert entitlement.name
            assert entitlement.description

    def test_a_flag_belongs_to_at_most_one_entitlement(self) -> None:
        seen: set[str] = set()
        for entitlement in all_entitlements():
            for flag in entitlement.flags:
                assert flag not in seen, f"{flag} appears in more than one entitlement"
                seen.add(flag)

    def test_unknown_key_fails_fast(self) -> None:
        with pytest.raises(KeyError):
            get_entitlement("not_a_capability")
        with pytest.raises(ValueError, match="Unknown entitlement 'not_a_capability'"):
            validate_entitlement_key("not_a_capability")


class TestLoweredFlags:
    """FAR-2665 stage 1: each lowered flag is Community behaviour."""

    @pytest.mark.parametrize("flag_name", sorted(LOWERED_FLAGS))
    def test_no_entitlement_covers_it(self, flag_name: str) -> None:
        assert entitlement_for_flag(flag_name) is None
        assert entitlement_key_for_flag(flag_name) is None

    @pytest.mark.parametrize("flag_name", sorted(LOWERED_FLAGS))
    def test_active_on_a_community_plan(self, flag_name: str) -> None:
        flag = FeatureFlagRegistry(current_tier="community").get_flag(flag_name)
        assert flag is not None, f"{flag_name} must stay registered"
        assert flag.currently_active is True

    @pytest.mark.parametrize("flag_name", sorted(ENTITLEMENTS))
    def test_every_entitled_flag_is_locked_on_community(self, flag_name: str) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        for covered in get_entitlement(flag_name).flags:
            flag = registry.get_flag(covered)
            assert flag is not None, f"{covered} must stay registered"
            assert flag.currently_active is False, f"{covered} is Team and must be off without a licence"


class TestGrantResolution:
    def test_community_grants_nothing(self) -> None:
        assert not granted_entitlements("community")

    def test_team_grants_every_group(self) -> None:
        assert granted_entitlements("team") == entitlement_keys()

    def test_undeclared_tier_grants_nothing(self) -> None:
        assert not granted_entitlements("mystery")

    def test_a_legacy_licence_name_maps_to_its_group(self) -> None:
        """A licence issued under the old flat names keeps working (no reissue)."""
        assert entitlement_keys_for_licence({"audit_viewer"}) == frozenset({"audit_evidence"})
        assert entitlement_keys_for_licence({"sso", "scim"}) == frozenset({"identity"})
        assert entitlement_keys_for_licence({"admin_cost_breakdown"}) == frozenset({"cost_governance"})

    def test_a_new_group_key_is_accepted_directly(self) -> None:
        assert entitlement_keys_for_licence({"identity"}) == frozenset({"identity"})

    def test_a_retired_or_unknown_licence_name_grants_nothing(self) -> None:
        assert not entitlement_keys_for_licence({"pipeline_delete", "nonsense"})

    def test_a_named_capability_is_granted_on_a_community_tier(self) -> None:
        assert granted_entitlements("community", {"audit_viewer"}) == frozenset({"audit_evidence"})


class TestPlanEntitlementsHelper:
    def test_prefers_the_context_resolver(self) -> None:
        class _Ctx:
            def entitlements(self) -> frozenset[str]:
                return frozenset({"identity"})

            def tier(self) -> str:
                return "community"

        assert plan_entitlements(_Ctx()) == frozenset({"identity"})

    def test_falls_back_to_the_tier_for_a_legacy_stub(self) -> None:
        class _Ctx:
            def tier(self) -> str:
                return "team"

        assert plan_entitlements(_Ctx()) == entitlement_keys()

    def test_a_community_legacy_stub_grants_nothing(self) -> None:
        class _Ctx:
            def tier(self) -> str:
                return "community"

        assert not plan_entitlements(_Ctx())

    def test_a_grant_set_containing_a_non_string_is_not_trusted(self) -> None:
        """A mock-shaped resolver can return non-strings; that is not a grant set.

        The shape check must fall through to the licence-derived answer rather
        than accept a set polluted with non-string entries — a bare Mock
        auto-answers ``entitlements()`` and would otherwise be read as a grant.
        """

        class _Ctx:
            def entitlements(self) -> list[object]:
                return ["identity", object()]

        assert not plan_entitlements(_Ctx())

    def test_a_context_with_no_resolvers_grants_nothing(self) -> None:
        """A minimal pre-protocol double (no tier/feature_enabled) grants nothing."""

        class _Ctx:
            pass

        assert not plan_entitlements(_Ctx())
