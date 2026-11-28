"""ADR 070 §3 — the non-production RLS-context tripwire.

The tripwire exists because the Phase 1B failure mode is SILENT: an org-only
session selecting a team-scoped table returns zero rows, so a background job can
report success having seen nothing. These tests pin the three things that make
it trustworthy rather than noisy or dangerous:

* it registers ONLY outside production and only on Postgres (an import-time guard
  on settings);
* it fires on a TEAM table with neither a user identity nor an execution-context
  grant, and stays quiet when either is present;
* it logs once per table-set per process and never raises.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session as SASession

import farna.db.rls_tripwire as tripwire
from farna.db.models.agent import Agent
from farna.db.models.pipeline import Pipeline
from farna.db.rls_tripwire import TEAM_SCOPED_TABLES

_LOGGER = "farna.db.rls_tripwire"


@pytest.fixture(autouse=True)
def _reset_reported() -> None:
    """Every test starts with a clean "already reported" set."""
    tripwire._REPORTED.clear()


def _state(
    *,
    info: dict[str, Any],
    dialect: str = "postgresql",
    statement: Any = None,
    is_select: bool = True,
) -> SimpleNamespace:
    """A minimal ``ORMExecuteState`` stand-in (only the read attributes)."""
    bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
    return SimpleNamespace(
        is_select=is_select,
        is_update=False,
        is_delete=False,
        statement=select(Pipeline) if statement is None else statement,
        session=SimpleNamespace(info=info, get_bind=lambda: bind),
    )


def _settings(*, db: str, environment: str) -> SimpleNamespace:
    """The two fields ``tripwire_enabled`` reads."""
    return SimpleNamespace(db=db, environment=environment)


# ---------------------------------------------------------------------------
# Registration gate
# ---------------------------------------------------------------------------


def test_tripwire_enabled_matrix() -> None:
    assert tripwire.tripwire_enabled(_settings(db="postgres", environment="development")) is True
    assert tripwire.tripwire_enabled(_settings(db="postgres", environment=" staging ")) is True
    assert tripwire.tripwire_enabled(_settings(db="postgres", environment="production")) is False
    assert tripwire.tripwire_enabled(_settings(db="postgres", environment="PROD")) is False
    assert tripwire.tripwire_enabled(_settings(db="sqlite", environment="development")) is False


def test_register_is_skipped_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production never pays for the diagnostic and never emits it."""
    monkeypatch.setattr(tripwire, "get_settings", lambda: _settings(db="postgres", environment="production"))
    had_listener = event.contains(SASession, "do_orm_execute", tripwire._rls_context_tripwire)

    tripwire.register_rls_context_tripwire()

    assert event.contains(SASession, "do_orm_execute", tripwire._rls_context_tripwire) is had_listener


def test_register_installs_the_listener_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tripwire, "get_settings", lambda: _settings(db="postgres", environment="development"))
    already_installed = event.contains(SASession, "do_orm_execute", tripwire._rls_context_tripwire)

    try:
        tripwire.register_rls_context_tripwire()
        assert event.contains(SASession, "do_orm_execute", tripwire._rls_context_tripwire) is True
        # Idempotent: a second call must not double-register.
        tripwire.register_rls_context_tripwire()
        assert event.contains(SASession, "do_orm_execute", tripwire._rls_context_tripwire) is True
    finally:
        if not already_installed:
            event.remove(SASession, "do_orm_execute", tripwire._rls_context_tripwire)


# ---------------------------------------------------------------------------
# The trip itself
# ---------------------------------------------------------------------------


def test_team_table_with_no_identity_or_grant_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={}))

    messages = [record.getMessage() for record in caplog.records if record.name == _LOGGER]
    assert len(messages) == 1
    assert "team_table_without_context" in messages[0]
    assert "pipelines" in messages[0]


def test_a_user_identity_silences_the_tripwire(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={"user_id": "11111111-1111-1111-1111-111111111111"}))

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_an_execution_context_grant_silences_the_tripwire(caplog: pytest.LogCaptureFixture) -> None:
    """The background grant is exactly what the tripwire treats as authorised."""
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={"execution_context": True}))

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_a_non_team_table_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={}, statement=select(Agent)))

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_mapper_classes_contribute_their_target_tables(caplog: pytest.LogCaptureFixture) -> None:
    """A statement carrying mapper classes is inspected for team-scoped tables.

    ``all_mapper_classes`` is the primary source ``_target_tables`` reads, so a
    team-scoped mapper must trip even when ``column_descriptions`` is empty. A
    mapper whose class has no ``__table__`` is skipped, not crashed on.
    """

    class _NoTable:
        pass

    state = _state(info={}, statement=select(Agent))
    state.all_mapper_classes = [SimpleNamespace(class_=Pipeline), SimpleNamespace(class_=_NoTable)]

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(state)

    messages = [record.getMessage() for record in caplog.records if record.name == _LOGGER]
    assert len(messages) == 1
    assert "pipelines" in messages[0]


def test_an_unreadable_session_info_is_treated_as_granted(caplog: pytest.LogCaptureFixture) -> None:
    """A session double with no readable ``info`` must never trip the wire."""
    session = SimpleNamespace(
        info=None,
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    state = SimpleNamespace(is_select=True, is_update=False, is_delete=False, statement=select(Pipeline), session=session)

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(state)

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_a_non_postgres_bind_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """The SQLite test backend has no RLS GUCs — the signal would be noise."""
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={}, dialect="sqlite"))

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_an_unreadable_bind_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """A tripwire must never fire on a shape it cannot inspect."""
    session = SimpleNamespace(info={}, get_bind=MagicMock(side_effect=RuntimeError("unbound")))
    state = SimpleNamespace(is_select=True, is_update=False, is_delete=False, statement=select(Pipeline), session=session)

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(state)

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_non_select_statements_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    state = SimpleNamespace(is_select=False, is_update=False, is_delete=False, statement=select(Pipeline), session=SimpleNamespace(info={}))

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(state)

    assert not [record for record in caplog.records if record.name == _LOGGER]


def test_each_table_set_is_reported_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    """A poll loop must not turn the tripwire into a log flood."""
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        tripwire._rls_context_tripwire(_state(info={}))
        tripwire._rls_context_tripwire(_state(info={}))

    assert len([record for record in caplog.records if record.name == _LOGGER]) == 1


def test_the_runs_family_is_declared_team_scoped() -> None:
    """Phase 1B flips these; the tripwire warns BEFORE the flip lands."""
    assert "runs" in TEAM_SCOPED_TABLES
    assert "run_node_outputs" in TEAM_SCOPED_TABLES
    assert "pipelines" in TEAM_SCOPED_TABLES
