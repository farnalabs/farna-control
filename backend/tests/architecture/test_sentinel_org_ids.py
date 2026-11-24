"""One identity for the built-in/registry sentinel organisation.

``db.models.organisation.REGISTRY_ORG_ID`` names the sentinel org that library
collection installs materialise registry primitives under, and
``core.library_service._seed_data.BUILTIN_ORG_ID`` names the same sentinel for
the in-memory built-ins. Two modules carry the value deliberately: importing
``core`` from ``db.models`` would invert the layering. This gate pins them
equal to each other and to the sentinel UUID, so a drift can never split the
built-in library across two "owners".
"""

from __future__ import annotations

import uuid

from farna.core.library_service._seed_data import BUILTIN_ORG_ID
from farna.db.models.organisation import REGISTRY_ORG_ID, SYSTEM_ORG_ID

#: The one sentinel UUID the built-in library lives under. Written literally on
#: purpose: the value is the contract, and both constants must equal it.
_SENTINEL_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_registry_org_id_is_the_library_sentinel() -> None:
    assert REGISTRY_ORG_ID == _SENTINEL_ORG_ID


def test_builtin_org_id_is_the_same_identity_as_registry_org_id() -> None:
    assert BUILTIN_ORG_ID == REGISTRY_ORG_ID


def test_the_library_sentinel_is_not_the_system_org() -> None:
    """Two sentinels, two jobs: mixing them would hide built-ins or leak them."""
    assert REGISTRY_ORG_ID != SYSTEM_ORG_ID
