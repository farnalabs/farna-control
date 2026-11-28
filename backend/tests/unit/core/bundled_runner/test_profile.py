"""Unit tests for the Bundled Runner shipped template + live drift (FAR-590 D4)."""

from types import SimpleNamespace

import pytest

from farna.core.bundled_runner import (
    BUNDLED_RUNNER_IMAGE_REF,
    BUNDLED_RUNNER_PROVIDER_TYPE,
    TEMPLATE_CONFIG_JSON,
    TEMPLATE_OWNED_FIELDS,
    build_bundled_runner_profile_values,
    template_drift_status,
)
from farna.db.bundled_runner_template import (
    BUNDLED_RUNNER_IMAGE_REPOSITORY,
    BUNDLED_RUNNER_LEGACY_IMAGE_NAME,
    BUNDLED_RUNNER_PLACEHOLDER_DIGEST,
    is_first_party_runner_image_ref,
    is_template_owned_image_ref,
    shipped_image_ref_is_real_pin,
)

#: The shipped placeholder reference (the committed default on a non-release build).
_PLACEHOLDER_REF = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@" + BUNDLED_RUNNER_PLACEHOLDER_DIGEST


def test_template_constants_shape() -> None:
    assert BUNDLED_RUNNER_PROVIDER_TYPE == "runner_docker"
    assert BUNDLED_RUNNER_IMAGE_REPOSITORY == "ghcr.io/farnalabs/farna-control/runner-opencode"
    assert BUNDLED_RUNNER_IMAGE_REF.startswith(BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:")
    assert len(BUNDLED_RUNNER_IMAGE_REF.split("@sha256:", 1)[1]) == 64
    assert TEMPLATE_OWNED_FIELDS["persistence_policy"] == "ephemeral"
    assert TEMPLATE_OWNED_FIELDS["network_policy"] == "outbound"
    # D4 hardening preset (ADR 029 config table)
    assert TEMPLATE_CONFIG_JSON["memory_mb"] == 1024
    assert TEMPLATE_CONFIG_JSON["cpu_limit"] == 1.0
    assert TEMPLATE_CONFIG_JSON["read_only_rootfs"] is True
    assert TEMPLATE_CONFIG_JSON["capabilities_drop"] == ["ALL"]
    assert TEMPLATE_CONFIG_JSON["no_new_privileges"] is True
    assert TEMPLATE_CONFIG_JSON["user"] == "1001:1001"
    assert TEMPLATE_CONFIG_JSON["workspace_network"] == "modulo-runner-workspace"
    assert TEMPLATE_CONFIG_JSON["timeout_seconds"] == 3600


def test_build_values_round_trip() -> None:
    values = build_bundled_runner_profile_values()
    assert values["name"] == "Bundled Runner (Docker)"
    assert values["provider_type"] == BUNDLED_RUNNER_PROVIDER_TYPE
    assert values["image_ref"] == BUNDLED_RUNNER_IMAGE_REF
    assert values["persistence_policy"] == "ephemeral"
    assert values["network_policy"] == "outbound"
    assert values["config_json"] == TEMPLATE_CONFIG_JSON


def test_committed_default_is_the_placeholder_digest() -> None:
    """The in-source default stays the all-zero placeholder.

    The release workflow injects the real ``runner-opencode`` manifest digest
    into the CI checkout; the committed value must NOT carry a per-release
    digest (that would be a stale pin on main between releases). The release
    gate ``scripts/check_released_runner_image_ref.py`` fails the release if
    the injected value is still this placeholder.
    """
    assert BUNDLED_RUNNER_IMAGE_REF == BUNDLED_RUNNER_IMAGE_REPOSITORY + "@" + BUNDLED_RUNNER_PLACEHOLDER_DIGEST


def _row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "provider_type": "runner_docker",
        "image_ref": BUNDLED_RUNNER_IMAGE_REF,
        "network_policy": "outbound",
        "persistence_policy": "ephemeral",
        "config_json": dict(TEMPLATE_CONFIG_JSON),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    ("image_ref", "owned"),
    [
        (None, True),
        ("", True),
        (BUNDLED_RUNNER_IMAGE_REF, True),
        (BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "3" * 64, True),
        (BUNDLED_RUNNER_LEGACY_IMAGE_NAME + "@sha256:" + "3" * 64, True),
        (BUNDLED_RUNNER_LEGACY_IMAGE_NAME, True),
        ("ghcr.io/acme/agent@sha256:" + "3" * 64, False),
        ("ghcr.io/farnalabs/farna-control/backend@sha256:" + "3" * 64, False),
        ("modulo-runner:opencode-custom@sha256:" + "3" * 64, False),
    ],
    ids=[
        "none",
        "empty",
        "placeholder-constant",
        "canonical-older-digest",
        "legacy-with-digest",
        "legacy-name-only",
        "third-party",
        "other-first-party-image",
        "legacy-lookalike-prefix",
    ],
)
def test_image_ref_ownership_is_scoped_to_the_first_party_repository(image_ref: str | None, owned: bool) -> None:
    """Ownership is first-party-repository-scoped, at any digest.

    Empty, the placeholder, the canonical repository and the legacy pre-rename
    name are all template-owned (so every release can advance the digest); any
    other repository is operator-owned (BYO).
    """
    assert is_template_owned_image_ref(image_ref) is owned
    assert is_first_party_runner_image_ref(image_ref) is (owned and bool(image_ref))


def test_drift_none_for_fresh_template_row() -> None:
    status = template_drift_status(_row())
    assert status == {"is_seeded": True, "drifted": False, "drifted_fields": []}


def test_shipped_pin_gate_tracks_the_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is False while the shipped constant is the placeholder."""
    monkeypatch.setattr("farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF", _PLACEHOLDER_REF)
    assert shipped_image_ref_is_real_pin() is False
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64,
    )
    assert shipped_image_ref_is_real_pin() is True


@pytest.mark.parametrize(
    "image_ref",
    [
        BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "3" * 64,  # real first-party digest (earlier release)
        _PLACEHOLDER_REF,  # the placeholder itself
        BUNDLED_RUNNER_LEGACY_IMAGE_NAME + "@" + BUNDLED_RUNNER_PLACEHOLDER_DIGEST,  # legacy name, placeholder digest
        "ghcr.io/acme/agent@sha256:" + "3" * 64,  # third-party (BYO)
        None,  # no image at all
    ],
    ids=[
        "real-first-party-digest",
        "placeholder",
        "legacy-name-placeholder-digest",
        "third-party",
        "none",
    ],
)
def test_drift_never_reports_image_ref_when_constant_is_the_placeholder(image_ref: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder shipped constant never downgrades or drifts a row's image.

    On a non-release build (``:latest``, local dev, a placeholder checkout) the
    constant is the all-zero placeholder, so the template may not ADVANCE a row
    to it: a real earlier-release first-party digest must NOT report image_ref
    drift (apply would otherwise rewrite it to the placeholder and break
    dispatch).
    """
    monkeypatch.setattr("farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF", _PLACEHOLDER_REF)
    status = template_drift_status(_row(image_ref=image_ref))
    assert status["is_seeded"] is True
    assert status["drifted"] is False
    assert "image_ref" not in status["drifted_fields"]


def test_drift_none_when_row_matches_the_first_party_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first-party ref equal to the constant is not drift, at any digest value."""
    real_digest_ref = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "4" * 64
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        real_digest_ref,
    )
    status = template_drift_status(_row(image_ref=real_digest_ref))
    assert status == {"is_seeded": True, "drifted": False, "drifted_fields": []}


def test_drift_ignores_operator_owned_image_ref() -> None:
    """An operator-set image_ref in another repository is not drift.

    Ownership is first-party-repository-scoped: once the operator owns the
    reference (a third-party image), it must not be reported as a "shipped
    template updated" state.
    """
    row = _row(image_ref="ghcr.io/acme/agent@sha256:" + "1" * 64)
    status = template_drift_status(row)
    assert status == {"is_seeded": True, "drifted": False, "drifted_fields": []}


def test_drift_detects_placeholder_image_ref_when_constant_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A placeholder-seeded row reports image_ref drift once the constant advances.

    While the row still carries the release placeholder digest the template owns
    the reference, so an advanced ``BUNDLED_RUNNER_IMAGE_REF`` surfaces as drift
    (and apply writes it).
    """
    advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    row = _row(image_ref=BUNDLED_RUNNER_IMAGE_REF)
    status = template_drift_status(row)
    assert status["is_seeded"] is True
    assert status["drifted"] is True
    assert "image_ref" in status["drifted_fields"]


def test_drift_detects_older_first_party_digest_when_constant_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row on a PREVIOUS release's first-party digest drifts when the constant advances.

    This is the release-over-release upgrade path: the row is a real
    (non-placeholder) first-party digest, still template-owned because it lives
    in the first-party repository.
    """
    advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    row = _row(image_ref=BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "3" * 64)
    status = template_drift_status(row)
    assert status["drifted"] is True
    assert "image_ref" in status["drifted_fields"]


def test_drift_detects_legacy_first_party_name_when_constant_advances(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-rename ``modulo-runner:opencode`` row is first-party-owned and drifts."""
    advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    row = _row(image_ref=BUNDLED_RUNNER_LEGACY_IMAGE_NAME + "@sha256:" + "3" * 64)
    status = template_drift_status(row)
    assert status["drifted"] is True
    assert "image_ref" in status["drifted_fields"]


def test_drift_treats_missing_image_ref_as_template_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with no image at all is template-owned, so it reports drift."""
    advanced = BUNDLED_RUNNER_IMAGE_REPOSITORY + "@sha256:" + "9" * 64
    monkeypatch.setattr(
        "farna.db.bundled_runner_template.BUNDLED_RUNNER_IMAGE_REF",
        advanced,
    )
    status = template_drift_status(_row(image_ref=None))
    assert "image_ref" in status["drifted_fields"]


def test_seeded_profile_still_receives_template_image_ref() -> None:
    """The template supplies image_ref at SEED time (and it stays template-owned).

    Ownership is first-party-repository-scoped, so the seeded reference keeps
    participating in drift until an operator points the row elsewhere.
    """
    values = build_bundled_runner_profile_values()
    assert values["image_ref"] == BUNDLED_RUNNER_IMAGE_REF


def test_drift_detects_config_change() -> None:
    cfg = dict(TEMPLATE_CONFIG_JSON)
    cfg["timeout_seconds"] = 600
    status = template_drift_status(_row(config_json=cfg))
    assert status["drifted"] is True
    assert "config_json.timeout_seconds" in status["drifted_fields"]


def test_drift_detects_persistence_unlock_attempt() -> None:
    status = template_drift_status(_row(persistence_policy="retained"))
    assert "persistence_policy" in status["drifted_fields"]


def test_drift_ignores_non_template_rows() -> None:
    row = _row(provider_type="e2b")
    status = template_drift_status(row)
    assert status == {"is_seeded": False, "drifted": False, "drifted_fields": []}
