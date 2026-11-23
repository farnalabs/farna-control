"""Team-boundary enforcement for API-key callers on the REST run surfaces.

A team-scoped ``mk_`` org API key carries a hard team boundary in
``TenantPrincipal.team_id`` (FAR-681). ``runs`` has only org-isolation RLS and
no ``visibility`` column, so that boundary must be applied explicitly on every
run read/mutation that accepts an API key. The MCP run tools already enforce it
(``_team_scoped_key_mismatch``); before this guard the REST equivalents did not,
so a key scoped to team A could read another team's run detail, attempts,
live events, work-item enrichment, and trigger a rerun of it.

These tests pin the REST guard: a foreign-team run is reported as NOT FOUND
(never a distinguishing boundary error), while the key's own team's runs and
org-level runs stay visible.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.api.routes import runs as runs_module
from farna.api.routes.runs import RunNotFoundError
from farna.auth.jwt import TenantPrincipal
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_RUN_ID = uuid.uuid4()
_PIPELINE_ID = uuid.uuid4()


def _make_run(*, owner_team_id: uuid.UUID | None) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.pipeline_id = _PIPELINE_ID
    run.owner_team_id = owner_team_id
    run.status = "complete"
    run.organisation_id = _ORG_ID
    return run


def _team_scoped_key_principal() -> TenantPrincipal:
    """A team-A-scoped API key: the presented role is deliberately low."""
    return TenantPrincipal(
        username="team-a-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="runner",
        team_id=_TEAM_A,
        via_api_key=True,
    )


def _org_wide_key_principal() -> TenantPrincipal:
    """An org-wide API key (``team_id=None``) keeps the org-level role."""
    return TenantPrincipal(
        username="org-key",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="runner",
        via_api_key=True,
    )


def _mock_session(run: Any) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=run)
    session.execute = AsyncMock(return_value=result)
    return session


class _Factory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _Factory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> bool:
        return False


async def _load(run: Any, principal: TenantPrincipal) -> Any:
    session = _mock_session(run)
    factory: Any = _Factory(session)
    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        patch.object(runs_module, "_run_gate_fired", AsyncMock(return_value=False)),
    ):
        return await runs_module._do_get_run_with_gate(factory, principal, _RUN_ID)


@pytest.mark.asyncio
async def test_team_scoped_key_cannot_read_another_teams_run_detail() -> None:
    """A team-A key gets NOT FOUND for a run owned by team B (not the row)."""
    run = _make_run(owner_team_id=_TEAM_B)

    with pytest.raises(RunNotFoundError):
        await _load(run, _team_scoped_key_principal())


@pytest.mark.asyncio
async def test_team_scoped_key_reads_its_own_teams_run() -> None:
    run = _make_run(owner_team_id=_TEAM_A)

    loaded, gate_fired = await _load(run, _team_scoped_key_principal())

    assert loaded is run
    assert gate_fired is False


@pytest.mark.asyncio
async def test_team_scoped_key_reads_an_org_level_run() -> None:
    """Org-level runs (no owner team) stay visible to every team-scoped key."""
    run = _make_run(owner_team_id=None)

    with patch("farna.db.crud.team_scope.pipeline_owner_team_id", AsyncMock(return_value=None)):
        loaded, _ = await _load(run, _team_scoped_key_principal())

    assert loaded is run


@pytest.mark.asyncio
async def test_pre_stamp_run_falls_back_to_the_pipeline_owner_team() -> None:
    """A NULL run owner is resolved from the pipeline, never treated as org-level."""
    run = _make_run(owner_team_id=None)

    with (
        patch("farna.db.crud.team_scope.pipeline_owner_team_id", AsyncMock(return_value=_TEAM_B)) as owner,
        pytest.raises(RunNotFoundError),
    ):
        await _load(run, _team_scoped_key_principal())

    owner.assert_awaited_once()


@pytest.mark.asyncio
async def test_org_wide_key_is_unaffected_by_the_team_guard() -> None:
    run = _make_run(owner_team_id=_TEAM_B)

    loaded, _ = await _load(run, _org_wide_key_principal())

    assert loaded is run


@pytest.mark.asyncio
async def test_events_loader_applies_the_same_boundary() -> None:
    """``_do_get_run`` (the live-events loader) carries the identical guard."""
    run = _make_run(owner_team_id=_TEAM_B)
    session = _mock_session(run)
    factory: Any = _Factory(session)

    with (
        patch.object(runs_module, "set_rls_org", AsyncMock()),
        patch.object(runs_module, "set_rls_user_context", AsyncMock()),
        pytest.raises(RunNotFoundError),
    ):
        await runs_module._do_get_run(factory, _team_scoped_key_principal(), _RUN_ID)
