from typing import ClassVar

import pytest

from farna.core.feature_flags import FeatureFlag, FeatureFlagRegistry


@pytest.fixture(autouse=True)
def _clean_global_overrides() -> None:
    """``_overrides`` is class-level and shared across instances; isolate each
    test so the lifecycle/override tests cannot pollute each other."""
    FeatureFlagRegistry._overrides.clear()
    yield
    FeatureFlagRegistry._overrides.clear()


class TestRetiredViewFlags:
    """FAR-2233: Saved Views is gone, so its two plan flags must be too."""

    @pytest.mark.parametrize("name", ["saved_views", "view_modes"])
    def test_flag_is_not_registered(self, name: str) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_flag(name) is None, f"{name} must no longer be registered in _KNOWN_FLAGS"

    def test_flag_is_not_default_off(self) -> None:
        from farna.core.feature_flags import DEFAULT_OFF_FLAGS

        assert "saved_views" not in DEFAULT_OFF_FLAGS


class TestFar2663FreedFlags:
    """FAR-2663: schema version history is provenance, so it is Community.

    Gating it withheld the WRITE path from every Community author — a schema
    shell could be created and a definition could never be published — so the
    flag must stay at the community tier; a future re-tiering has to change
    these tests deliberately.
    """

    def test_schema_version_history_is_registered_at_community(self) -> None:
        flag = FeatureFlagRegistry().get_flag("schema_version_history")
        assert flag is not None, "schema_version_history must stay registered (it is the plan-payload entry)"
        assert flag.tier == "community"

    def test_declared_tier_is_community(self) -> None:
        """The 402-detail builder reads this declarative tier, not the registry."""
        from farna.core.feature_flags import declared_feature_tier

        assert declared_feature_tier("schema_version_history") == "community"

    def test_schema_version_history_is_active_on_a_community_plan(self) -> None:
        flag = FeatureFlagRegistry(current_tier="community").get_flag("schema_version_history")
        assert flag is not None
        assert flag.currently_active is True


class TestRetiredPipelineDeleteFlag:
    """FAR-2663: ``pipeline_delete`` never gated the backend and is retired.

    ``DELETE /api/v1/pipelines/{id}`` is guarded by the Community RBAC
    permission ``pipeline.delete``; the flag only hid two UI buttons. It must
    not come back as a plan flag (migration 0360 deletes the stale catalog row).
    """

    def test_flag_is_not_registered(self) -> None:
        assert FeatureFlagRegistry().get_flag("pipeline_delete") is None

    def test_flag_is_not_default_off(self) -> None:
        from farna.core.feature_flags import DEFAULT_OFF_FLAGS

        assert "pipeline_delete" not in DEFAULT_OFF_FLAGS

    def test_flag_has_no_declared_tier(self) -> None:
        from farna.core.feature_flags import declared_feature_tier

        assert declared_feature_tier("pipeline_delete") is None


class TestWebhookNotificationLogFlag:
    """FAR-656: the webhook notification delivery log is a community feature."""

    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("webhook_notification_log")
        assert flag is not None, "webhook_notification_log flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("webhook_notification_log")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_active_on_community_by_default(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("webhook_notification_log")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("webhook_notification_log")
        assert flag is not None
        assert flag.description


class TestDashboardChartsFlag:
    """FAR-2235: dashboard charts (sparklines) is active by default on community tier."""

    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("dashboard_charts")
        assert flag is not None, "dashboard_charts flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("dashboard_charts")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_active_on_community_by_default(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("dashboard_charts")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("dashboard_charts")
        assert flag is not None
        assert flag.description


class TestUserManagementFlag:
    """FAR-462: basic user management is a community-tier feature — the Users
    admin view must not be tier-locked on any plan."""

    def test_flag_is_registered(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None, "user_management flag must be registered in _KNOWN_FLAGS"

    def test_flag_tier_is_community(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.tier == "community"

    def test_flag_active_on_community_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_active_on_team_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team", has_license_key=True)
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.currently_active is True

    def test_flag_has_description(self) -> None:
        registry = FeatureFlagRegistry()
        flag = registry.get_flag("user_management")
        assert flag is not None
        assert flag.description


class TestFeatureFlagModel:
    """FAR-2665: a flag is BEHAVIOUR ONLY and declares no tier.

    ``tier`` is a read-only projection of the entitlement registry, so these
    tests pin that a construction cannot pass one and that the projection is
    derived from the flag's name.
    """

    def test_creates_with_minimal_fields(self) -> None:
        flag = FeatureFlag(name="test_flag", description="test")
        assert flag.name == "test_flag"
        assert flag.description == "test"
        assert flag.depends_on is None
        assert flag.currently_active is False

    def test_tier_is_not_a_constructor_field(self) -> None:
        with pytest.raises(TypeError):
            FeatureFlag(name="tier_flag", description="test", tier="community")  # type: ignore[call-arg]

    def test_tier_projection_is_community_for_a_behaviour_flag(self) -> None:
        assert FeatureFlag(name="eval_system", description="test").tier == "community"

    def test_tier_projection_is_team_for_an_entitled_flag(self) -> None:
        """Derived from ``core.entitlements`` — never declared on the flag."""
        assert FeatureFlag(name="sso", description="test").tier == "team"

    def test_currently_active_can_be_set_false(self) -> None:
        flag = FeatureFlag(name="inactive_flag", description="test", currently_active=False)
        assert flag.currently_active is False

    def test_non_blocked_flag_with_currently_active(self) -> None:
        flag = FeatureFlag(name="active_flag", description="test", currently_active=True)
        assert flag.currently_active is True

    def test_depends_on_relationship(self) -> None:
        child = FeatureFlag(name="child_flag", description="test", depends_on="parent_flag")
        assert child.depends_on == "parent_flag"

    def test_description_defaults_to_empty(self) -> None:
        flag = FeatureFlag(name="no_desc", description="")
        assert not flag.description


class TestRegistryLifecycle:
    """Sync registry API surfaced to the admin UI and plan-context code:
    ``refresh``, ``set_override``/``get_override``/``clear_override``, and the
    ``tier_gap_flags`` community-gap report."""

    def test_refresh_recomputes_active_state_from_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        sso = registry.get_flag("sso")
        assert sso is not None
        assert sso.tier == "team"
        assert sso.currently_active is False

        registry.refresh("team", has_license_key=True)
        assert registry.current_tier == "team"
        assert registry.has_license_key is True
        assert sso.currently_active is True

        registry.refresh("community", has_license_key=False)
        assert registry.has_license_key is False
        assert sso.currently_active is False

    def test_set_override_forces_flag_active_at_lower_tier(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)

        assert registry.get_override("sso") is True
        sso = registry.get_flag("sso")
        assert sso is not None
        assert sso.currently_active is True

    def test_clear_override_restores_tier_computed_state(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)
        assert registry.get_flag("sso").currently_active is True

        registry.clear_override("sso")
        assert registry.get_override("sso") is None
        assert registry.get_flag("sso").currently_active is False

    def test_get_override_returns_none_when_unset(self) -> None:
        registry = FeatureFlagRegistry()
        assert registry.get_override("sso") is None

    def test_tier_gap_flags_empty_when_not_community(self) -> None:
        registry = FeatureFlagRegistry(current_tier="team")
        assert not registry.tier_gap_flags()

    def test_tier_gap_flags_reports_inactive_team_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        gaps = registry.tier_gap_flags()

        assert gaps, "expected at least one inactive above-community flag"
        assert all(flag.tier != "community" for flag in gaps)
        assert all(not flag.currently_active for flag in gaps)
        assert any(flag.name == "sso" for flag in gaps)

    def test_tier_gap_flags_exclude_override_active_flags(self) -> None:
        registry = FeatureFlagRegistry(current_tier="community")
        registry.set_override("sso", True)

        gaps = {flag.name for flag in registry.tier_gap_flags()}
        assert "sso" not in gaps


class TestAllFlagsRegistered:
    def test_all_known_flags_have_tier(self) -> None:
        registry = FeatureFlagRegistry()
        for flag in registry.list_flags():
            assert flag.tier in {"community", "team", "v1", "v2"}, f"Flag {flag.name} has unknown tier {flag.tier}"

    def test_all_known_flags_have_unique_names(self) -> None:
        registry = FeatureFlagRegistry()
        names = [flag.name for flag in registry.list_flags()]
        assert len(names) == len(set(names)), "Duplicate flag names detected"

    def test_flags_with_depends_on_refer_to_existing_flags(self) -> None:
        registry = FeatureFlagRegistry()
        names = {flag.name for flag in registry.list_flags()}
        for flag in registry.list_flags():
            if flag.depends_on:
                assert flag.depends_on in names, f"Flag {flag.name} depends on unknown flag {flag.depends_on}"


class TestOrgAdminSettableClassification:
    """FAR-2302: only the curated low-sensitivity community flags are org-settable.

    The classification is fail-closed — a flag that does not opt in stays
    system-administrator-only — so this pins both the curated set and the
    sensitive flags that must never be opened to org admins by default.
    """

    _EXPECTED: ClassVar[set[str]] = {
        "user_scoped_mcp_keys",
        "api_key_grants",
        "webhook_notification_log",
        "dashboard_charts",
        "library_collection",
        "autonomy_gating",
        "analytics_page",
        "system_maps",
    }

    #: Sensitive or instance-scoped flags that must stay system-admin-only.
    _MUST_NOT_BE_SETTABLE: ClassVar[set[str]] = {
        "sso_unrestricted_provisioning",
        "web_vitals_analytics",
        "observability",
        "scim",
        "error_forwarders",
        "runtime_config",
        "rate_limits",
        "assistant",
        "mcp_human_linked_hitl_authority",
    }

    def test_curated_set_is_exactly_the_org_settable_flags(self) -> None:
        registry = FeatureFlagRegistry()
        settable = {flag.name for flag in registry.list_flags() if flag.org_admin_settable}
        assert settable == self._EXPECTED

    def test_org_settable_flags_are_all_community_tier(self) -> None:
        registry = FeatureFlagRegistry()
        for flag in registry.list_flags():
            if flag.org_admin_settable:
                assert flag.tier == "community", f"{flag.name} is org-settable but not community-tier"

    def test_no_team_tier_flag_is_org_settable(self) -> None:
        registry = FeatureFlagRegistry()
        team_flags = [flag for flag in registry.list_flags() if flag.tier == "team"]
        assert team_flags, "expected the registry to expose team-tier flags"
        assert all(not flag.org_admin_settable for flag in team_flags)

    def test_sensitive_flags_remain_system_admin_only(self) -> None:
        registry = FeatureFlagRegistry()
        for name in self._MUST_NOT_BE_SETTABLE:
            flag = registry.get_flag(name)
            assert flag is not None, f"{name} must stay registered"
            assert flag.org_admin_settable is False, f"{name} must not be org-admin-settable"

    def test_unknown_flag_defaults_to_system_admin_only(self) -> None:
        assert FeatureFlag(name="new_flag", description="unclassified").org_admin_settable is False
