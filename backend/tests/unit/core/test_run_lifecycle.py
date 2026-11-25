"""FAR-2705: the run-lifecycle transition table is the single source of truth.

These tests pin three things:

1. **Equality** — every derived projection equals the literal value the codebase
   shipped before the table existed (captured below), so replacing the five
   hand-maintained frozensets changed no behaviour.
2. **Structure** — every non-terminal status has an outgoing transition, every
   terminal status has none, the non-terminal classes partition the active set,
   and no status is both executing and parked.
3. **The model bites** — injecting a status the table does not know, or
   projecting away a status the table does know, fails the invariant checks.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from farna.core import run_lifecycle as lifecycle
from farna.core.run_lifecycle import (
    ACTION_CLAIM_GATE,
    DOCUMENTED_PROJECTIONS,
    STATUSES,
    TRANSITIONS,
    LifecycleModelError,
    Status,
    derive_projections,
    lifecycle_violations,
)

#: The exact literals that lived in ``farna/db/models/run.py`` before FAR-2705.
#: Pinning them makes the refactor behaviour-preserving by construction: any
#: drift in the derivation fails here.
SHIPPED_PROJECTIONS: dict[str, frozenset[str]] = {
    "TERMINAL_STATUSES": frozenset(
        {
            "complete",
            "failed",
            "cancelled",
            "eval_failed",
            "stalled",
            "budget_exceeded",
            "router_no_match",
            "cost_ceiling_exceeded",
            "compensation_failed",
            "rejected",
        }
    ),
    "SUCCESS_STATUSES": frozenset({"complete"}),
    "ACTIVE_RUN_STATUSES": frozenset({"pending", "running", "awaiting_human", "claimed", "unknown", "hitl_parked"}),
    "PIPELINE_CAPACITY_STATUSES": frozenset({"running", "claimed", "unknown"}),
    "EXECUTING_STATUSES": frozenset({"running", "claimed", "unknown"}),
    "LIVE_STATUSES": frozenset({"pending", "running", "awaiting_human", "claimed", "unknown", "hitl_parked"}),
    "TRIGGER_SLOT_HOLDING_STATUSES": frozenset({"running", "awaiting_human", "claimed", "unknown", "hitl_parked"}),
    "HITL_ACTIONABLE_RUN_STATUSES": frozenset({"awaiting_human", "claimed", "hitl_parked"}),
    "HITL_CLAIMABLE_RUN_STATUSES": frozenset({"awaiting_human", "hitl_parked"}),
    "ONGOING_ACTIVE_STATUSES": frozenset({"pending", "running", "claimed"}),
}


#: The whole status vocabulary as shipped (the ``ck_runs_status`` values).
SHIPPED_VOCABULARY: frozenset[str] = frozenset(
    {
        "pending",
        "running",
        "awaiting_human",
        "claimed",
        "unknown",
        "hitl_parked",
        "complete",
        "failed",
        "cancelled",
        "eval_failed",
        "stalled",
        "budget_exceeded",
        "router_no_match",
        "cost_ceiling_exceeded",
        "compensation_failed",
        "rejected",
    }
)


def _projection_violations(projections: dict[str, frozenset[str]]) -> list[str]:
    """Compare candidate projections with the truth the table derives.

    Kept in the test (not the production module) so production carries no
    test-only seam; the invariant still bites on a dropped/extra status.
    """
    truth = derive_projections()
    violations: list[str] = []
    for name in DOCUMENTED_PROJECTIONS:
        candidate = frozenset(projections.get(name, frozenset()))
        missing = sorted(truth[name] - candidate)
        extra = sorted(candidate - truth[name])
        if missing or extra:
            violations.append(f"projection {name!r} disagrees with the lifecycle table: missing={missing} extra={extra}")
    return violations


class TestProjectionsAreUnchanged:
    """Values must be IDENTICAL before and after the refactor."""

    @pytest.mark.parametrize(("name", "expected"), sorted(SHIPPED_PROJECTIONS.items()))
    def test_projection_equals_the_shipped_literal(self, name: str, expected: frozenset[str]) -> None:
        assert getattr(lifecycle, name) == expected, f"{name} drifted from the value shipped before FAR-2705"

    @pytest.mark.parametrize(("name", "expected"), sorted(SHIPPED_PROJECTIONS.items()))
    def test_the_db_model_reexport_is_identical(self, name: str, expected: frozenset[str]) -> None:
        """The historical import site (``farna.db.models.run``) exposes the same values."""
        from farna.db.models import run as model

        if not hasattr(model, name):
            pytest.skip(f"{name} is a core/analytics projection, not a DB-model re-export")
        assert getattr(model, name) == expected

    def test_the_db_model_reexports_every_status_name(self) -> None:
        """The names the rest of the codebase imports from the model stay present."""
        from farna.db.models import run as model

        expected_names = (
            "ACTIVE_RUN_STATUSES",
            "TERMINAL_STATUSES",
            "SUCCESS_STATUSES",
            "PIPELINE_CAPACITY_STATUSES",
            "HITL_ACTIONABLE_RUN_STATUSES",
            "HITL_CLAIMABLE_RUN_STATUSES",
            "ONGOING_ACTIVE_STATUSES",
            "AWAITING_HUMAN_STATUS",
            "HITL_PARKED_STATUS",
            "RUN_STATUS_WHITELIST",
        )
        missing = [name for name in expected_names if not hasattr(model, name)]
        assert missing == [], f"the DB model stopped re-exporting: {missing}"

    def test_status_whitelist_is_the_whole_vocabulary(self) -> None:
        from farna.db.models import run as model

        assert model.RUN_STATUS_WHITELIST == lifecycle.ALL_RUN_STATUSES
        assert SHIPPED_VOCABULARY == model.RUN_STATUS_WHITELIST

    def test_active_and_terminal_partition_the_vocabulary(self) -> None:
        assert lifecycle.ACTIVE_RUN_STATUSES.isdisjoint(lifecycle.TERMINAL_STATUSES)
        assert lifecycle.ACTIVE_RUN_STATUSES | lifecycle.TERMINAL_STATUSES == lifecycle.ALL_RUN_STATUSES


class TestTableStructure:
    def test_the_model_is_internally_sound(self) -> None:
        assert lifecycle_violations() == []

    def test_every_non_terminal_status_has_an_outgoing_transition(self) -> None:
        for status in sorted(lifecycle.ACTIVE_RUN_STATUSES):
            assert lifecycle.transitions_from(status), f"non-terminal status {status!r} can never be left"

    def test_terminal_statuses_have_no_outgoing_transition(self) -> None:
        for status in sorted(lifecycle.TERMINAL_STATUSES):
            assert lifecycle.transitions_from(status) == (), f"terminal status {status!r} has an outgoing edge"

    def test_every_action_is_documented_in_one_line(self) -> None:
        for transition in TRANSITIONS:
            assert transition.action.strip(), "a transition has no action"
            assert transition.description.strip(), f"action {transition.action!r} has no description"
            assert "\n" not in transition.description, f"action {transition.action!r} is documented on more than one line"

    def test_no_status_is_both_executing_and_parked(self) -> None:
        assert lifecycle.EXECUTING_STATUSES.isdisjoint(lifecycle.PARKED_STATUSES)

    def test_the_non_terminal_classes_partition_the_active_set(self) -> None:
        classes = (
            lifecycle.QUEUED_STATUSES,
            lifecycle.IN_FLIGHT_STATUSES,
            lifecycle.RECOVERING_STATUSES,
            lifecycle.PARKED_STATUSES,
        )
        union: frozenset[str] = frozenset().union(*classes)
        assert union == lifecycle.ACTIVE_RUN_STATUSES
        for index, left in enumerate(classes):
            for right in classes[index + 1 :]:
                assert left.isdisjoint(right)

    def test_the_derived_sets_equal_their_documented_projection(self) -> None:
        projections = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        assert _projection_violations(projections) == []

    def test_the_hitl_sets_are_exactly_the_gate_claim_endpoints(self) -> None:
        sources = frozenset(t.from_status for t in TRANSITIONS if t.action == ACTION_CLAIM_GATE and t.from_status is not None)
        targets = frozenset(t.to_status for t in TRANSITIONS if t.action == ACTION_CLAIM_GATE)
        assert sources == lifecycle.HITL_CLAIMABLE_RUN_STATUSES
        assert sources | targets == lifecycle.HITL_ACTIONABLE_RUN_STATUSES

    def test_capacity_is_the_executing_projection_and_excludes_parked(self) -> None:
        assert lifecycle.PIPELINE_CAPACITY_STATUSES == lifecycle.EXECUTING_STATUSES
        assert lifecycle.PIPELINE_CAPACITY_STATUSES.isdisjoint(lifecycle.PARKED_STATUSES)
        assert "pending" not in lifecycle.PIPELINE_CAPACITY_STATUSES

    def test_ongoing_is_queued_plus_in_flight_only(self) -> None:
        assert lifecycle.ONGOING_ACTIVE_STATUSES == lifecycle.QUEUED_STATUSES | lifecycle.IN_FLIGHT_STATUSES
        assert lifecycle.ONGOING_ACTIVE_STATUSES.isdisjoint(lifecycle.RECOVERING_STATUSES)
        assert lifecycle.ONGOING_ACTIVE_STATUSES.isdisjoint(lifecycle.PARKED_STATUSES)

    def test_the_module_is_dependency_free(self) -> None:
        """No import from ``farna`` — the leaf both the DB and core layers may load."""
        source = Path(lifecycle.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
        offending = sorted(module for module in imported_modules if module == "farna" or module.startswith("farna."))
        assert offending == [], f"run_lifecycle must not import farna modules: {offending}"


class TestTheModelBites:
    """An unclassified status, or a projection that drops one, must fail loudly."""

    def test_an_injected_unknown_status_breaks_derivation(self) -> None:
        injected = (*STATUSES, Status("brand_new_state", lifecycle.IN_FLIGHT, "injected by the test"))
        with pytest.raises(LifecycleModelError, match="brand_new_state"):
            derive_projections(statuses=injected)

    def test_an_injected_status_with_an_unknown_class_breaks_derivation(self) -> None:
        injected = (*STATUSES, Status("weird_state", "not-a-class", "injected by the test"))
        with pytest.raises(LifecycleModelError, match="not-a-class"):
            derive_projections(statuses=injected)

    def test_a_transition_to_an_undefined_status_breaks_derivation(self) -> None:
        injected = (*TRANSITIONS, lifecycle.Transition(None, "create_ghost", "ghost_state", "targets a status the enum does not define"))
        with pytest.raises(LifecycleModelError, match="ghost_state"):
            derive_projections(transitions=injected)

    def test_a_projection_that_omits_a_non_terminal_status_is_rejected(self) -> None:
        """The exact 26-hour-capacity-incident shape: one projection forgets a status."""
        doctored = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        doctored["ACTIVE_RUN_STATUSES"] = doctored["ACTIVE_RUN_STATUSES"] - {"unknown"}

        violations = _projection_violations(doctored)

        assert violations, "a projection that silently dropped a status must be reported"
        assert any("ACTIVE_RUN_STATUSES" in violation and "unknown" in violation for violation in violations), violations

    def test_a_projection_that_adds_a_terminal_status_is_rejected(self) -> None:
        doctored = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        doctored["PIPELINE_CAPACITY_STATUSES"] = doctored["PIPELINE_CAPACITY_STATUSES"] | {"awaiting_human"}

        violations = _projection_violations(doctored)

        assert any("PIPELINE_CAPACITY_STATUSES" in violation and "awaiting_human" in violation for violation in violations), violations
