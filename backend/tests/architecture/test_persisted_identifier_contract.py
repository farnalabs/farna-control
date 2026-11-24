"""Architecture gate: the persisted/wire identifiers keep their agreed literals.

Companion to the persisted-value migration plan (rebrand: persisted and wire
values) and to the behaviour pins in
``tests/unit/core/test_persisted_value_migration.py``. The brand-consistency
gate freezes the retired name's *count* per file, so an in-place swap (same
count, different value) slips through. This test pins the exact string of
every identifier the migration plan touched, plus the legacy alias each one must
still accept during the compatibility window, and the one value the plan keeps.

The legacy literals are spelled here on purpose: this is the single baselined
place that records them, so the alias cannot drift silently.
"""

from __future__ import annotations

from pathlib import Path

from farna.core.credential_issuer import (
    ISSUANCE_PROVENANCE_SELF_MINTED,
    ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY,
    ISSUANCE_PROVENANCES,
)
from farna.core.notifier import NOTIFIER_USER_AGENT
from farna.core.pipeline_engine.node_runner import (
    LEGACY_SYNTHETIC_FAILURE_MARKERS,
    SYNTHETIC_FAILURE_MARKER,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO_ROOT / "frontend" / "src" / "manifest.yaml"

#: Brand-free targets and their legacy aliases, agreed in the migration plan.
_SELF_MINTED = "self_minted"
_SELF_MINTED_LEGACY = "modulo_minted"
_SYNTHETIC_FAILURE = "synthetic_failure"
_SYNTHETIC_FAILURE_LEGACY = "modulo_synthetic_failure"
_NOTIFIER_USER_AGENT = "Farna-Control-Notifier/1.0"
#: Decision 1: the built-in trust-header provenance value is deliberately KEPT.
_KEPT_TRUST_SOURCE = "modulo"


def test_self_minted_provenance_value_and_legacy_alias() -> None:
    assert ISSUANCE_PROVENANCE_SELF_MINTED == _SELF_MINTED
    assert ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY == _SELF_MINTED_LEGACY
    assert _SELF_MINTED in ISSUANCE_PROVENANCES
    assert _SELF_MINTED_LEGACY in ISSUANCE_PROVENANCES


def test_synthetic_failure_marker_value_and_legacy_alias() -> None:
    assert SYNTHETIC_FAILURE_MARKER == _SYNTHETIC_FAILURE
    assert LEGACY_SYNTHETIC_FAILURE_MARKERS == (_SYNTHETIC_FAILURE_LEGACY,)


def test_notifier_user_agent_is_farna_control() -> None:
    assert NOTIFIER_USER_AGENT == _NOTIFIER_USER_AGENT


def test_product_map_records_the_notifier_user_agent() -> None:
    manifest = _MANIFEST.read_text(encoding="utf-8")
    assert _NOTIFIER_USER_AGENT in manifest
    assert "Modulo-Notifier/1.0" not in manifest


def test_builtin_trust_header_source_is_frozen() -> None:
    from farna.core.library_service._seed_data import _BUILTIN_BY_SLUG

    headers = [primitive.content_json.get("trust_header") for primitive in _BUILTIN_BY_SLUG.values()]
    headers = [header for header in headers if isinstance(header, dict)]
    assert len(headers) == 7
    for header in headers:
        assert header == {"source": _KEPT_TRUST_SOURCE, "verified": True}
