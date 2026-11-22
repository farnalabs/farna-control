"""Direct branch coverage for helpers extracted by the run-crud decomposition.

The decomposition lifted pre-existing run-crud logic into new module-level
helpers. Because those lines now APPEAR as newly added in the diff, the
changed-lines coverage gate measures them and requires both arms of every
guard to be exercised by the unit suite. These tests target the extracted
helpers directly:

* ``_probe_agent_journey_pairs`` — the fail-open exception arm (and the
  CancelledError re-raise arm);
* ``_consume_agent_mint_budget_or_drop`` — the zero-mint short circuit, the
  within-budget path, and the over-budget suppression arm;
* ``_runs_list_attribute_filters`` — the full filter tuple (every predicate
  arm) plus the all-None arm;
* ``_success_rate`` — the empty-input and zero-denominator guards.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from farna.core.pipeline_engine.classify import (
    RunClassificationValue,
    classify_run,
    normalize_benign_signatures,
)
from farna.db.crud.run import (
    _consume_agent_mint_budget_or_drop,
    _drop_dismissed_agent_entries,
    _probe_agent_journey_pairs,
    _runs_list_attribute_filters,
    _success_rate,
)
from farna.db.models.run import ACTIVE_RUN_STATUSES, SUCCESS_STATUSES, TERMINAL_STATUSES

_ORG_ID = uuid.uuid4()
_AGENT_ENTRIES = [{"kind": "issue", "ref": "FAR-1"}]


# ---------------------------------------------------------------------------
# _success_rate
# ---------------------------------------------------------------------------


def test_success_rate_empty_input_returns_zero() -> None:
    assert _success_rate([]) == 0.0


def test_success_rate_all_benign_returns_zero() -> None:
    runs = [SimpleNamespace(status="complete", run_classification={"value": "benign"})]
    assert _success_rate(runs) == 0.0


def test_success_rate_counts_non_benign_share() -> None:
    runs = [
        SimpleNamespace(status="complete", run_classification=None),
        SimpleNamespace(status="failed", run_classification=None),
    ]
    assert _success_rate(runs) == 0.5


def test_benign_run_statuses_are_disjoint_from_success_statuses() -> None:
    """Pin the invariant that keeps ``_success_rate`` at or below 1.0 (FAR-707).

    The rate divides the count of ``SUCCESS_STATUSES`` runs by the count of
    NON-benign runs, so it can only stay <= 1.0 while a run the classifier
    marks ``benign`` can never also be a success. ``benign`` is produced
    solely by the COUNTABLE-failure arm of ``classify_run``; sweep every run
    status the DB CHECK constraint allows (``TERMINAL_STATUSES`` +
    ``ACTIVE_RUN_STATUSES``) through it with a matching signature and assert
    the benign verdicts never touch the success set. A future change that
    routes a success status into that arm fails here instead of silently
    pushing the reported success rate above 100%.
    """
    signatures = normalize_benign_signatures([{"error_code": "agent.failed"}])
    benign_statuses = {status for status in TERMINAL_STATUSES | ACTIVE_RUN_STATUSES if classify_run(status, "agent.failed", benign_signatures=signatures).value is RunClassificationValue.benign}
    # Non-vacuous: the sweep must actually reach a benign verdict, otherwise the
    # disjointness assertion below could pass while pinning nothing.
    assert benign_statuses, "no status classifies as benign - the sweep no longer exercises the invariant"
    overlapping = benign_statuses & SUCCESS_STATUSES
    assert not overlapping, f"a benign run can be a success status: {sorted(overlapping)}"


# ---------------------------------------------------------------------------
# _runs_list_attribute_filters
# ---------------------------------------------------------------------------


def test_runs_list_attribute_filters_all_none_is_empty() -> None:
    clauses = _runs_list_attribute_filters(
        pipeline_id=None,
        status=None,
        trigger_type=None,
        search=None,
        variant_group_id=None,
        batch_id=None,
    )
    assert clauses == []


def test_runs_list_attribute_filters_full_tuple_appends_each_predicate() -> None:
    clauses = _runs_list_attribute_filters(
        pipeline_id=uuid.uuid4(),
        status="complete",
        trigger_type="manual",
        search="50%_off\\x",
        variant_group_id=uuid.uuid4(),
        batch_id=uuid.uuid4(),
    )
    assert len(clauses) == 6


# ---------------------------------------------------------------------------
# _drop_dismissed_agent_entries
# ---------------------------------------------------------------------------


def test_drop_dismissed_agent_entries_removes_tombstoned_and_notifies() -> None:
    newly = [{"kind": "issue", "ref": "FAR-1"}, {"kind": "issue", "ref": "FAR-2"}]
    dismissed = {("issue", "FAR-2")}
    with patch("farna.db.crud.run.notify_refs_event") as mock_notify:
        remaining, minted = _drop_dismissed_agent_entries(newly, dismissed, 2)
    assert remaining == [{"kind": "issue", "ref": "FAR-1"}]
    assert minted == 1
    mock_notify.assert_called_once()


def test_drop_dismissed_agent_entries_no_dismissals_is_noop() -> None:
    newly = [{"kind": "issue", "ref": "FAR-1"}]
    with patch("farna.db.crud.run.notify_refs_event") as mock_notify:
        remaining, minted = _drop_dismissed_agent_entries(newly, set(), 1)
    assert remaining == newly
    assert minted == 1
    mock_notify.assert_not_called()


# ---------------------------------------------------------------------------
# _consume_agent_mint_budget_or_drop
# ---------------------------------------------------------------------------


async def test_consume_budget_or_drop_zero_mints_is_noop() -> None:
    session = MagicMock()
    result, minted = await _consume_agent_mint_budget_or_drop(session, _ORG_ID, _AGENT_ENTRIES, 0)
    assert result == _AGENT_ENTRIES
    assert minted == 0


async def test_consume_budget_or_drop_within_budget_keeps_entries() -> None:
    session = MagicMock()
    with patch(
        "farna.core.runtime_config.mint_budget.consume_agent_mint_budget",
        new=AsyncMock(return_value=True),
    ):
        result, minted = await _consume_agent_mint_budget_or_drop(session, _ORG_ID, _AGENT_ENTRIES, 1)
    assert result == _AGENT_ENTRIES
    assert minted == 1


async def test_consume_budget_or_drop_over_budget_suppresses_and_notifies() -> None:
    session = MagicMock()
    with (
        patch(
            "farna.core.runtime_config.mint_budget.consume_agent_mint_budget",
            new=AsyncMock(return_value=False),
        ),
        patch("farna.db.crud.run.notify_refs_event") as mock_notify,
    ):
        result, minted = await _consume_agent_mint_budget_or_drop(session, _ORG_ID, _AGENT_ENTRIES, 2)
    assert result == []
    assert minted == 0
    mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# _probe_agent_journey_pairs
# ---------------------------------------------------------------------------


async def test_probe_agent_journey_pairs_failed_probe_is_fail_open() -> None:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("probe boom"))
    existing, dismissed = await _probe_agent_journey_pairs(session, _ORG_ID, _AGENT_ENTRIES)
    assert existing == set()
    assert dismissed == set()


async def test_probe_agent_journey_pairs_cancelled_reraises() -> None:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=asyncio.CancelledError())
    try:
        await _probe_agent_journey_pairs(session, _ORG_ID, _AGENT_ENTRIES)
    except asyncio.CancelledError:
        pass
    else:  # pragma: no cover - the re-raise is the behaviour under test
        raise AssertionError("CancelledError should have propagated")
