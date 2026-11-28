"""Bundled Runner shipped template constants (FAR-590, D4).

The single source of truth for the "Bundled Runner (Docker)" seeded
EnvironmentProfile template: provider type, the pinned runner image
reference, hardening + network preset, and the LOCKED ``ephemeral``
persistence policy.

This module lives in the DB layer so both the org-creation seeding hook
(``db.seed``) and the shipped-template drift helper can consume it without a
``db -> core`` import (import-linter contract ``db-does-not-import-core``).
The core-side ``bundled_runner.profile`` module re-exports these constants
and owns the LIVE drift helper.

Digest contract (ADR 029 / plan D4):
- ``BUNDLED_RUNNER_IMAGE_REF``'s committed default is the all-zero
  placeholder digest. ``release-unified.yml`` builds and pushes the release's
  ``ghcr.io/farnalabs/farna-control/runner-opencode`` image FIRST, resolves
  its manifest digest, and rewrites this constant IN THE CI CHECKOUT to
  ``ghcr.io/farnalabs/farna-control/runner-opencode@sha256:<digest>`` before
  the all-in-one and backend images are built, so the released image bakes
  the real digest. The rewritten value is never committed or pushed to main.
- ``scripts/check_released_runner_image_ref.py`` is the release gate. It runs
  after that injection and before the remaining images are built, and refuses
  a placeholder, malformed, or wrong-repository reference.
- Seeded rows carry their own copy of the template-owned fields at
  creation; drift is computed LIVE against these constants (no fingerprint
  lifecycle): a divergent row surfaces "shipped template updated - apply"
  on the Runners page. Operator-owned fields are surfaced, never silently
  re-applied.
- ``image_ref`` ownership is FIRST-PARTY-REPOSITORY-SCOPED: while a row
  carries no image, the release placeholder digest, or any reference under the
  first-party runner repository (``BUNDLED_RUNNER_IMAGE_REPOSITORY``, or the
  legacy pre-rename ``modulo-runner:opencode`` name), the template owns it — it
  participates in drift and an apply writes ``BUNDLED_RUNNER_IMAGE_REF``. That
  covers BOTH the first advance off the placeholder and every later release:
  a row seeded with release N's first-party digest still drifts when release
  N+1 advances the constant, so upgrades work release over release. Any other
  repository is operator-owned: it is not reported as drift and apply never
  overwrites it, so a BYO image can never be silently reverted.
- The template may only ADVANCE a reference, never downgrade it: ``image_ref``
  participates in drift and an apply writes the constant ONLY while the shipped
  ``BUNDLED_RUNNER_IMAGE_REF`` is itself a real release pin
  (:func:`shipped_image_ref_is_real_pin`). On a non-release build the constant
  is still the all-zero placeholder, so writing it would replace a real
  earlier-release first-party digest with the placeholder and break dispatch.
"""

from __future__ import annotations

from typing import Any

TEMPLATE_PROFILE_NAME = "Bundled Runner (Docker)"

# The pinned runner image reference. The committed default is the all-zero
# placeholder; the release workflow injects the real
# ``ghcr.io/farnalabs/farna-control/runner-opencode@sha256:<digest>`` into the
# CI checkout before the all-in-one/backend images are built (see the module
# docstring and ``scripts/check_released_runner_image_ref.py``).
#
# Kept as ONE single-string literal on purpose: the release gate extracts it
# from this source line, so it must stay a plain ``"..."`` assignment.
BUNDLED_RUNNER_IMAGE_REF = "ghcr.io/farnalabs/farna-control/runner-opencode@sha256:0000000000000000000000000000000000000000000000000000000000000000"

#: The first-party runner image repository — the single source of truth for
#: "is this our image". Derived from the shipped reference (the CI rewrite only
#: swaps the digest, never the repository) so the two can never diverge. Used
#: by :func:`is_first_party_runner_image_ref` (and therefore by the drift
#: comparison), and named by the release gate
#: ``scripts/check_released_runner_image_ref.py`` as the repository a released
#: reference must carry.
BUNDLED_RUNNER_IMAGE_REPOSITORY = BUNDLED_RUNNER_IMAGE_REF.split("@", 1)[0]

#: The pre-rename first-party image name (``modulo-runner:opencode``). Rows
#: seeded before the rename still carry it as ``<name>@sha256:<digest>``; they
#: are first-party-owned too, so a release advance rewrites them to the
#: canonical ``BUNDLED_RUNNER_IMAGE_REPOSITORY`` reference instead of treating
#: them as an operator's BYO image.
BUNDLED_RUNNER_LEGACY_IMAGE_NAME = "modulo-runner:opencode"

BUNDLED_RUNNER_PROVIDER_TYPE = "runner_docker"

# The release placeholder digest: an all-zero sha256. A seeded profile whose
# ``image_ref`` still carries this cannot provision (the image does not exist
# in any registry) — a pipeline bound to it fails at dispatch with an explicit
# placeholder error until the release constant advances and an apply writes the
# real digest. This is a STABLE sentinel, intentionally independent of
# ``BUNDLED_RUNNER_IMAGE_REF``, which the release workflow rewrites to the real
# released digest — so the placeholder check below keeps firing only while the
# digest is un-landed, and never false-positives on a real digest.
BUNDLED_RUNNER_PLACEHOLDER_DIGEST = "sha256:" + "0" * 64


def is_placeholder_bundled_runner_image_ref(image_ref: str | None) -> bool:
    """True when ``image_ref`` still carries the un-landed placeholder digest."""
    return image_ref is not None and image_ref.endswith(BUNDLED_RUNNER_PLACEHOLDER_DIGEST)


def _image_name_part(image_ref: str) -> str:
    """``image_ref`` with any ``@sha256:<digest>`` suffix removed."""
    return image_ref.split("@", 1)[0]


def is_first_party_runner_image_ref(image_ref: str | None) -> bool:
    """True when ``image_ref`` points at the first-party runner image.

    Matches the canonical ``BUNDLED_RUNNER_IMAGE_REPOSITORY`` and the legacy
    pre-rename ``BUNDLED_RUNNER_LEGACY_IMAGE_NAME``, at ANY digest. The digest
    is deliberately ignored: an older first-party digest is still our image, so
    it stays template-owned across releases.
    """
    if not image_ref:
        return False
    name = _image_name_part(image_ref)
    return name in (BUNDLED_RUNNER_IMAGE_REPOSITORY, BUNDLED_RUNNER_LEGACY_IMAGE_NAME)


def is_template_owned_image_ref(image_ref: str | None) -> bool:
    """True while the template still owns ``image_ref`` for this row.

    Ownership is deterministic and needs no extra state: a row that carries no
    image at all, still carries the release placeholder digest, or points at
    the first-party runner image (any of its digests, canonical or legacy name)
    has not been claimed by the operator. In that state the reference
    participates in drift and an apply writes the release constant — which is
    what makes an upgrade from one release to the next advance the digest, not
    just the first advance off the placeholder — provided the shipped constant
    is itself a real release pin (see :func:`shipped_image_ref_is_real_pin`).
    Any other reference is operator-set (BYO) and is neither drift nor an apply
    target.
    """
    return not image_ref or is_placeholder_bundled_runner_image_ref(image_ref) or is_first_party_runner_image_ref(image_ref)


def shipped_image_ref_is_real_pin() -> bool:
    """True when the shipped ``BUNDLED_RUNNER_IMAGE_REF`` carries a real release pin.

    The committed default is the all-zero placeholder digest, and only the
    release job rewrites it (in its CI checkout). On a non-release build — a
    ``:latest`` image built by ``publish-images.yml``, local dev, or any
    checkout whose constant is still the placeholder — the shipped constant is
    NOT a pin. The template may only ever ADVANCE a row's reference, so in that
    state it must not write the constant: doing so would downgrade a real
    earlier-release first-party digest to the placeholder and break dispatch.

    Reads the module global at call time so the release rewrite (and a test's
    ``monkeypatch``) is observed live.
    """
    return not is_placeholder_bundled_runner_image_ref(BUNDLED_RUNNER_IMAGE_REF)


# Template-owned fields (drift-keyed): provider, digest, hardening, and
# network defaults. Operator-owned fields (name/description beyond the
# template defaults) are NEVER part of drift.
_TEMPLATE_HARDENING: dict[str, Any] = {
    "memory_mb": 1024,
    "cpu_limit": 1.0,
    "read_only_rootfs": True,
    "tmpfs_paths": {"/home/user": "size=512m,mode=1777", "/tmp": "size=128m,mode=1777"},  # noqa: S108 # nosec B108  # NOSONAR - container-side tmpfs path (mode=1777 sticky-bit)
    "capabilities_drop": ["ALL"],
    "no_new_privileges": True,
    "user": "1001:1001",
}
_TEMPLATE_NETWORK: dict[str, Any] = {
    "workspace_network": "modulo-runner-workspace",
}

TEMPLATE_CONFIG_JSON: dict[str, Any] = {
    "template": "bundled-runner-docker",
    **_TEMPLATE_HARDENING,
    **_TEMPLATE_NETWORK,
    "timeout_seconds": 3600,
}

#: The complete shipped template of template-owned fields (drift source).
#:
#: ``image_ref`` is conditional rather than listed here: while a row carries
#: no image, the release placeholder digest, or any reference under the
#: first-party runner repository (``BUNDLED_RUNNER_IMAGE_REPOSITORY`` /
#: ``BUNDLED_RUNNER_LEGACY_IMAGE_NAME`` — see ``is_template_owned_image_ref``),
#: the template owns it, so an advanced ``BUNDLED_RUNNER_IMAGE_REF`` reaches it
#: through drift + apply on EVERY release, not just the first. A row the
#: operator has pointed at another repository is operator-owned and is never a
#: drift key or an apply target.
TEMPLATE_OWNED_FIELDS: dict[str, Any] = {
    "provider_type": BUNDLED_RUNNER_PROVIDER_TYPE,
    "network_policy": "outbound",
    "persistence_policy": "ephemeral",
    "config_json": TEMPLATE_CONFIG_JSON,
}


def build_bundled_runner_profile_values() -> dict[str, Any]:
    """Shipped template values for seeding (org-creation + startup re-seed)."""
    return {
        "name": TEMPLATE_PROFILE_NAME,
        "description": ("The Bundled Runner: first-party ghcr.io/farnalabs/farna-control/runner-opencode workspace executed on this deployment's Docker engine via the filtered socket proxy. Persistence is locked to ephemeral."),
        "provider_type": BUNDLED_RUNNER_PROVIDER_TYPE,
        "image_ref": BUNDLED_RUNNER_IMAGE_REF,
        "capabilities_json": [],
        "config_json": TEMPLATE_CONFIG_JSON,
        "network_policy": "outbound",
        "initialisation_strategy": "git_clone",
        "secret_refs_json": [],
        "persistence_policy": "ephemeral",
    }


def template_drift_fields(
    *,
    provider_type: str | None,
    image_ref: str | None,
    network_policy: str | None,
    persistence_policy: str | None,
    config_json: Any,
) -> dict[str, Any]:
    """Compute template drift from RAW FIELD VALUES (DB-layer pure function).

    Returns ``{"is_seeded": bool, "drifted": bool, "drifted_fields": [..]}``.
    Lives in the DB layer so the crud apply helper (FAR-591 D5) and the
    core-side live drift helper share ONE implementation — the core
    ``template_drift_status`` delegates here.

    ``image_ref`` participates only while the template still owns it (empty, the
    release placeholder digest, or a first-party runner reference at any
    digest) AND the shipped constant is itself a real release pin. A shipped
    placeholder (a non-release build) never drifts a row's image: the template
    may only advance a reference, never downgrade it to the placeholder. An
    operator-set BYO image in another repository is not drift and is never
    reported as a "shipped template updated" state.
    """
    if provider_type != BUNDLED_RUNNER_PROVIDER_TYPE:
        # Only template-shaped runner_docker rows participate in drift.
        return {"is_seeded": False, "drifted": False, "drifted_fields": []}
    drifted_fields: list[str] = []
    # The template may only ADVANCE a reference. When the shipped constant is
    # still the placeholder (non-release build) writing it to a row would
    # downgrade a real earlier-release digest and break dispatch, so image_ref
    # is not a drift key in that state.
    if shipped_image_ref_is_real_pin() and is_template_owned_image_ref(image_ref) and image_ref != BUNDLED_RUNNER_IMAGE_REF:
        drifted_fields.append("image_ref")
    if network_policy != TEMPLATE_OWNED_FIELDS["network_policy"]:
        drifted_fields.append("network_policy")
    if persistence_policy != "ephemeral":
        drifted_fields.append("persistence_policy")
    cfg = config_json or {}
    shipped = TEMPLATE_OWNED_FIELDS["config_json"]
    for key, value in shipped.items():
        if cfg.get(key) != value:
            drifted_fields.append(f"config_json.{key}")
    return {
        "is_seeded": True,
        "drifted": bool(drifted_fields),
        "drifted_fields": drifted_fields,
    }
