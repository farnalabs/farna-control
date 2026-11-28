"""Unit tests for apply_bundled_runner_template (FAR-591, D5 "apply" action).

The apply refreshes ONLY the drifted template-owned fields; operator-owned
fields (name, description, capabilities, secret refs, untouched config
keys) must survive. Ownership of the image reference is
first-party-repository-scoped: while the row carries no image, the release
placeholder digest, or a first-party runner reference at ANY digest, the
template owns it and an apply writes ``BUNDLED_RUNNER_IMAGE_REF`` — so the
digest advances on every release, not just the first one off the placeholder.
Once the operator points the profile at another repository it is neither a
drift key nor an apply target, so it survives the apply unchanged. Non-template
rows are rejected (None). The template may only ADVANCE a reference: an apply
writes ``image_ref`` only while the shipped ``BUNDLED_RUNNER_IMAGE_REF`` is a
real release pin, so a non-release build whose constant is still the placeholder
never downgrades a row's real digest to the placeholder.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.db.bundled_runner_template import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_IMAGE_REPOSITORY,
    BUNDLED_RUNNER_PLACEHOLDER_DIGEST,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
)
from farna.db.crud.environment_profile import apply_bundled_runner_template

_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")

#: A genuinely third-party (operator BYO) image, in another repository.
_OPERATOR_IMAGE_REF = "ghcr.io/acme/agent@sha256:" + "2" * 64

#: The shipped placeholder reference (the committed default on a non-release build).
_PLACEHOLDER_REF = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@" + BUNDLED_RUNNER_PLACEHOLDER_DIGEST


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_profile(**overrides: Any) -> MagicMock:
    profile = MagicMock()
    profile.id = _PROFILE_ID
    profile.provider_type = overrides.get("provider_type", "runner_docker")
    profile.image_ref = overrides.get("image_ref", _OPERATOR_IMAGE_REF)
    profile.network_policy = overrides.get("network_policy", "none")
    profile.persistence_policy = overrides.get("persistence_policy", "ephemeral")
    profile.name = overrides.get("name", "Bundled Runner (Docker)")
    profile.description = "operator description"
    profile.capabilities_json = ["docker"]
    config = {
        "timeout_seconds": 7200,
        "memory_mb": 4096,
        "cpu_limit": 2.0,
    }
    config.update(overrides.get("config_extra", {}))
    profile.config_json = overrides.get("config_json", config)
    profile.secret_refs_json = ["vault://x"]
    return profile


async def _apply(profile: Any) -> Any:
    session = _make_session()
    with patch("farna.db.crud.environment_profile.get_environment_profile", new=AsyncMock(return_value=profile)):
        return await apply_bundled_runner_template(session, _PROFILE_ID)


class TestApplyBundledRunnerTemplate:
    @pytest.mark.asyncio
    async def test_refreshes_drifted_template_fields_preserving_operator_image(self) -> None:
        # First-party-repository-scoped ownership: a third-party image_ref is
        # operator-owned, so apply refreshes the shipped template WITHOUT
        # resetting the operator's chosen image.
        profile = _make_profile()
        operator_image_ref = profile.image_ref
        assert operator_image_ref != BUNDLED_RUNNER_IMAGE_REF
        applied = await _apply(profile)
        assert applied.image_ref == operator_image_ref
        assert applied.network_policy == TEMPLATE_OWNED_FIELDS["network_policy"]
        assert applied.persistence_policy == "ephemeral"

    @pytest.mark.asyncio
    async def test_apply_does_not_advance_operator_image_when_constant_advances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A third-party operator image is never overwritten by an advancing release."""
        advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
        monkeypatch.setattr("farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF", advanced)
        profile = _make_profile(image_ref=_OPERATOR_IMAGE_REF)
        await _apply(profile)
        assert profile.image_ref == _OPERATOR_IMAGE_REF

    @pytest.mark.asyncio
    async def test_apply_advances_placeholder_image_to_template_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A placeholder-seeded row is refreshed to the advanced release constant.

        The template owns a row that still carries the placeholder digest, so
        an advanced ``BUNDLED_RUNNER_IMAGE_REF`` must reach it (the documented
        remediation path).
        """
        advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            advanced,
        )
        profile = _make_profile(image_ref=BUNDLED_RUNNER_IMAGE_REF)
        await _apply(profile)
        assert profile.image_ref == advanced

    @pytest.mark.asyncio
    async def test_apply_advances_an_older_first_party_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A row seeded with an EARLIER release's first-party digest still advances.

        This is the upgrade-from-one-release-to-the-next case: the row carries a
        real (non-placeholder) first-party digest, which the template still owns
        because it is in the first-party repository.
        """
        advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            advanced,
        )
        older_release = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "3" * 64
        profile = _make_profile(image_ref=older_release)
        await _apply(profile)
        assert profile.image_ref == advanced

    @pytest.mark.asyncio
    async def test_apply_advances_a_legacy_first_party_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A pre-rename ``modulo-runner:opencode`` row is first-party-owned and advances."""
        advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            advanced,
        )
        profile = _make_profile(image_ref="modulo-runner:opencode@sha256:" + "3" * 64)
        await _apply(profile)
        assert profile.image_ref == advanced

    @pytest.mark.asyncio
    async def test_apply_writes_template_image_when_row_has_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A row with no image is template-owned, so a real-pin apply supplies the constant."""
        advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            advanced,
        )
        profile = _make_profile(image_ref=None)
        await _apply(profile)
        assert profile.image_ref == advanced

    @pytest.mark.asyncio
    async def test_apply_never_downgrades_a_real_digest_to_the_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On a non-release build apply MUST NOT rewrite a real digest to the placeholder.

        The shipped constant is still the all-zero placeholder (``:latest``,
        local dev, a placeholder checkout). A row seeded by an earlier release
        carries a real first-party digest; writing the placeholder would break
        dispatch, so the template may only ever advance a reference.
        """
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            _PLACEHOLDER_REF,
        )
        real_digest_ref = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "3" * 64
        profile = _make_profile(image_ref=real_digest_ref)
        await _apply(profile)
        assert profile.image_ref == real_digest_ref
        assert profile.image_ref != _PLACEHOLDER_REF

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "image_ref",
        [
            _PLACEHOLDER_REF,
            BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "0" * 64,
            _OPERATOR_IMAGE_REF,
        ],
        ids=["placeholder", "placeholder-by-digest", "third-party"],
    )
    async def test_apply_writes_nothing_to_image_ref_when_constant_is_the_placeholder(self, image_ref: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a placeholder shipped constant, apply leaves image_ref untouched."""
        monkeypatch.setattr(
            "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
            _PLACEHOLDER_REF,
        )
        profile = _make_profile(image_ref=image_ref)
        await _apply(profile)
        assert profile.image_ref == image_ref

    @pytest.mark.asyncio
    async def test_config_drifted_keys_reset_operator_keys_survive(self) -> None:
        """Only drifted config keys reset; operator-owned keys are untouched."""
        profile = _make_profile(config_extra={"operator_note": "keep me"})
        assert profile.config_json["memory_mb"] == 4096
        assert profile.config_json["operator_note"] == "keep me"
        await _apply(profile)
        cfg = profile.config_json
        assert cfg["memory_mb"] == TEMPLATE_CONFIG_JSON["memory_mb"]
        assert cfg["cpu_limit"] == TEMPLATE_CONFIG_JSON["cpu_limit"]
        assert cfg["operator_note"] == "keep me"

    @pytest.mark.asyncio
    async def test_operator_owned_fields_survive(self) -> None:
        profile = _make_profile()
        await _apply(profile)
        assert profile.name == "Bundled Runner (Docker)"
        assert profile.description == "operator description"
        assert profile.capabilities_json == ["docker"]
        assert profile.secret_refs_json == ["vault://x"]

    @pytest.mark.asyncio
    async def test_clean_row_is_unchanged_in_shape(self) -> None:
        """Applying to a non-drifted row is a no-op on every template field."""
        profile = _make_profile(
            image_ref=BUNDLED_RUNNER_IMAGE_REF,
            network_policy="outbound",
            config_json=dict(TEMPLATE_CONFIG_JSON),
        )
        await _apply(profile)
        assert profile.image_ref == BUNDLED_RUNNER_IMAGE_REF
        assert profile.network_policy == "outbound"
        assert profile.config_json == dict(TEMPLATE_CONFIG_JSON)

    @pytest.mark.asyncio
    async def test_non_template_row_returns_none(self) -> None:
        profile = _make_profile(provider_type="e2b")
        applied = await _apply(profile)
        assert applied is None

    @pytest.mark.asyncio
    async def test_missing_row_returns_none(self) -> None:
        session = _make_session()
        with patch("farna.db.crud.environment_profile.get_environment_profile", new=AsyncMock(return_value=None)):
            applied = await apply_bundled_runner_template(session, _PROFILE_ID)
        assert applied is None
