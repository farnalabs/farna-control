"""Unit tests for the org-aware feature gate dependency (FAR-2595).

``require_org_feature`` overlays an organisation's ``feature_overrides`` on top
of the plan default with the bootstrap's precedence: an explicit override wins,
otherwise the plan default applies. These tests pin each precedence branch,
including that an unreadable override fails CLOSED rather than trusting a
default-ON plan.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError

from farna.api.dependencies import require_org_feature
from farna.api.models.problem import ProblemException

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Plan:
    def __init__(self, enabled: bool) -> None:
        self._enabled = enabled

    def feature_enabled(self, name: str) -> bool:
        return self._enabled

    def tier(self) -> str:
        return "community"


def _session() -> MagicMock:
    session = MagicMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _check():
    return require_org_feature("system_maps").dependency


def _principal(org_id: uuid.UUID | None = _ORG_ID) -> SimpleNamespace:
    return SimpleNamespace(organisation_id=org_id)


def _org(overrides: dict[str, bool]) -> SimpleNamespace:
    return SimpleNamespace(settings_json={"feature_overrides": overrides})


async def test_org_override_enables_a_default_off_flag() -> None:
    with patch("farna.db.crud.organisation.get_organisation", AsyncMock(return_value=_org({"system_maps": True}))):
        await _check()(_Plan(False), _principal(), _session())


async def test_org_override_disables_a_plan_enabled_flag() -> None:
    with patch("farna.db.crud.organisation.get_organisation", AsyncMock(return_value=_org({"system_maps": False}))), pytest.raises(ProblemException) as exc:
        await _check()(_Plan(True), _principal(), _session())

    assert exc.value.status_code == 402
    assert exc.value.problem.instance == "system_maps"


async def test_no_override_falls_back_to_the_plan_default() -> None:
    with patch("farna.db.crud.organisation.get_organisation", AsyncMock(return_value=None)), pytest.raises(ProblemException):
        await _check()(_Plan(False), _principal(), _session())


async def test_override_read_failure_fails_closed_even_when_the_plan_is_enabled() -> None:
    with patch("farna.db.crud.organisation.get_organisation", AsyncMock(side_effect=SQLAlchemyError("boom"))), pytest.raises(ProblemException) as exc:
        await _check()(_Plan(True), _principal(), _session())

    assert exc.value.status_code == 402
    assert exc.value.problem.type == "urn:problem:modulo:feature_required"
    assert exc.value.problem.instance == "system_maps"


async def test_principal_without_an_org_uses_the_plan_default() -> None:
    with patch("farna.db.crud.organisation.get_organisation", AsyncMock(side_effect=AssertionError("should not read"))):
        await _check()(_Plan(True), _principal(org_id=None), _session())
