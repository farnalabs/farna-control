"""FAR-2705: the run-lifecycle transition table is the single source of truth.

These tests pin four things:

1. **Frozen equality** — every documented projection equals an explicit literal
   captured below (a snapshot). Adding a non-terminal status therefore fails
   here until the author updates the literal: a computed projection alone could
   silently absorb a new status.
2. **Structure** — every non-terminal status has an outgoing transition, every
   terminal status has none, the non-terminal classes partition the active set,
   no status is both executing and parked, and the two real self/return edges
   (``claimed -> claimed``, ``hitl_parked -> running``) are declared.
3. **The constraint is derived** — ``ck_runs_status`` is parsed back out of the
   model and asserted equal to the lifecycle vocabulary (one source, not two).
4. **The model bites** — injecting a seventh non-terminal status (``paused_quota``)
   fails the frozen pins, a projection that drops a status is reported, and every
   writer's ``allowed_from`` is a subset of the table's declared edges.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from farna import lifecycle
from farna.lifecycle import (
    ACTION_CLAIM_GATE,
    DOCUMENTED_PROJECTIONS,
    IN_FLIGHT,
    STATUS_AWAITING_HUMAN,
    STATUS_CLAIMED,
    STATUS_HITL_PARKED,
    STATUS_RUNNING,
    STATUSES,
    TRANSITIONS,
    LifecycleModelError,
    Status,
    Transition,
    allowed_from_for_target,
    derive_projections,
    lifecycle_violations,
)

#: Frozen snapshot of every documented projection (the values the codebase
#: shipped, plus the projections derived since). Pinning explicit literals — not
#: re-deriving — is what makes "add a status, update the literals" enforceable.
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
    "REDISPATCHABLE_STATUSES": frozenset({"pending", "running", "awaiting_human", "claimed", "hitl_parked"}),
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

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "farna"


def _projection_violations(projections: dict[str, frozenset[str]], expected: dict[str, frozenset[str]]) -> list[str]:
    """Compare candidate projections with the pinned literals (not a re-derivation)."""
    violations: list[str] = []
    for name in DOCUMENTED_PROJECTIONS:
        candidate = frozenset(projections.get(name, frozenset()))
        reference = expected[name]
        missing = sorted(reference - candidate)
        extra = sorted(candidate - reference)
        if missing or extra:
            violations.append(f"projection {name!r} disagrees with its pinned literal: missing={missing} extra={extra}")
    return violations


def _ck_runs_status_values() -> set[str]:
    """Parse the ``ck_runs_status`` CHECK text back out of the ORM model."""
    from farna.db.models import run as model

    constraint = next(c for c in model.Run.__table__.constraints if getattr(c, "name", None) == "ck_runs_status")
    return set(re.findall(r"'([^']+)'", str(constraint.sqltext)))


class TestProjectionsAreUnchanged:
    """Values must be IDENTICAL — the refactor changed no behaviour."""

    @pytest.mark.parametrize(("name", "expected"), sorted(SHIPPED_PROJECTIONS.items()))
    def test_projection_equals_the_pinned_literal(self, name: str, expected: frozenset[str]) -> None:
        assert getattr(lifecycle, name) == expected, f"{name} drifted from its frozen literal"

    @pytest.mark.parametrize(("name", "expected"), sorted(SHIPPED_PROJECTIONS.items()))
    def test_the_db_model_reexport_is_identical(self, name: str, expected: frozenset[str]) -> None:
        """The historical import site (``farna.db.models.run``) exposes the same values."""
        from farna.db.models import run as model

        if not hasattr(model, name):
            pytest.skip(f"{name} is a core/analytics projection, not a DB-model re-export")
        assert getattr(model, name) == expected

    def test_the_db_model_reexports_every_status_name(self) -> None:
        from farna.db.models import run as model

        expected_names = (
            "ACTIVE_RUN_STATUSES",
            "TERMINAL_STATUSES",
            "SUCCESS_STATUSES",
            "PIPELINE_CAPACITY_STATUSES",
            "HITL_ACTIONABLE_RUN_STATUSES",
            "HITL_CLAIMABLE_RUN_STATUSES",
            "ONGOING_ACTIVE_STATUSES",
            "REDISPATCHABLE_STATUSES",
            "AWAITING_HUMAN_STATUS",
            "HITL_PARKED_STATUS",
            "RUN_STATUS_WHITELIST",
        )
        missing = [name for name in expected_names if not hasattr(model, name)]
        assert not missing, f"the DB model stopped re-exporting: {missing}"

    def test_status_whitelist_is_the_whole_vocabulary(self) -> None:
        from farna.db.models import run as model

        assert model.RUN_STATUS_WHITELIST == lifecycle.ALL_RUN_STATUSES
        assert SHIPPED_VOCABULARY == model.RUN_STATUS_WHITELIST

    def test_active_and_terminal_partition_the_vocabulary(self) -> None:
        assert lifecycle.ACTIVE_RUN_STATUSES.isdisjoint(lifecycle.TERMINAL_STATUSES)
        assert lifecycle.ACTIVE_RUN_STATUSES | lifecycle.TERMINAL_STATUSES == lifecycle.ALL_RUN_STATUSES


class TestTheConstraintIsDerived:
    """``ck_runs_status`` is built from the vocabulary, never hand-written twice."""

    def test_the_check_constraint_equals_the_lifecycle_vocabulary(self) -> None:
        assert _ck_runs_status_values() == set(lifecycle.ALL_RUN_STATUSES)

    def test_the_model_renders_the_constraint_from_the_vocabulary(self) -> None:
        from farna.db.models import run as model

        assert "status IN (" in model.CK_RUNS_STATUS_SQL
        assert set(re.findall(r"'([^']+)'", model.CK_RUNS_STATUS_SQL)) == set(lifecycle.ALL_RUN_STATUSES)

    def test_the_constraint_is_not_a_second_literal_list(self) -> None:
        """The model source must not spell the status values in the constraint call."""
        source = (_SRC_ROOT / "db" / "models" / "run.py").read_text(encoding="utf-8")
        assert "CK_RUNS_STATUS_SQL," in source
        assert "status IN ('pending'" not in source


class TestTableStructure:
    def test_the_model_is_internally_sound(self) -> None:
        assert not lifecycle_violations()

    def test_every_non_terminal_status_has_an_outgoing_transition(self) -> None:
        for status in sorted(lifecycle.ACTIVE_RUN_STATUSES):
            assert lifecycle.transitions_from(status), f"non-terminal status {status!r} can never be left"

    def test_terminal_statuses_only_leave_through_declared_recovery_edges(self) -> None:
        for status in sorted(lifecycle.TERMINAL_STATUSES):
            for transition in lifecycle.transitions_from(status):
                assert transition.reopens_terminal, f"terminal status {status!r} has an undeclared outgoing edge via {transition.action!r}"

    def test_every_non_terminal_status_can_reach_the_saq_failure_fence(self) -> None:
        """``saq_hooks._mark_run_failed`` fails any active status except ``unknown``.

        The fence is status-guarded (NOT IN terminal, <> unknown), so the table
        must declare an edge to ``failed`` from every status it can match.
        """
        expected = lifecycle.ACTIVE_RUN_STATUSES - {lifecycle.STATUS_UNKNOWN}
        assert expected <= allowed_from_for_target(lifecycle.STATUS_FAILED)

    def test_every_recovery_edge_reopens_a_terminal_status(self) -> None:
        recovery = [transition for transition in TRANSITIONS if transition.reopens_terminal]
        assert recovery, "the operator recovery edges must be declared, not omitted"
        assert all(transition.from_status in lifecycle.TERMINAL_STATUSES for transition in recovery)
        assert {(transition.from_status, transition.to_status) for transition in recovery} >= {
            (lifecycle.STATUS_FAILED, lifecycle.STATUS_RUNNING),
            (lifecycle.STATUS_EVAL_FAILED, lifecycle.STATUS_RUNNING),
            (lifecycle.STATUS_EVAL_FAILED, lifecycle.STATUS_PENDING),
        }

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

    def test_the_derived_sets_equal_their_pinned_literals(self) -> None:
        projections = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        assert not _projection_violations(projections, SHIPPED_PROJECTIONS)

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

    def test_redispatchable_is_active_minus_unknown(self) -> None:
        assert lifecycle.REDISPATCHABLE_STATUSES == lifecycle.ACTIVE_RUN_STATUSES - lifecycle.RECOVERING_STATUSES
        assert "unknown" not in lifecycle.REDISPATCHABLE_STATUSES

    def test_the_two_real_return_edges_are_declared(self) -> None:
        """``hitl_parked -> running`` (resume claim) and ``claimed -> claimed``."""
        assert (STATUS_HITL_PARKED, STATUS_RUNNING) in {(t.from_status, t.to_status) for t in TRANSITIONS}
        assert (STATUS_CLAIMED, STATUS_CLAIMED) in {(t.from_status, t.to_status) for t in TRANSITIONS}

    def test_the_sql_terminal_list_is_derived_from_the_table(self) -> None:
        """Raw-SQL writers/indexes embed the SAME terminal vocabulary."""
        assert ", ".join(f"'{status}'" for status in sorted(lifecycle.TERMINAL_STATUSES)) == lifecycle.TERMINAL_STATUS_SQL
        assert set(re.findall(r"'([^']+)'", lifecycle.TERMINAL_STATUS_SQL)) == set(lifecycle.TERMINAL_STATUSES)

    def test_analytics_status_matches_the_vocabulary(self) -> None:
        """The analytics filter enum is exactly the lifecycle vocabulary."""
        from farna.core.analytics.builder import AnalyticsStatus

        assert {member.value for member in AnalyticsStatus} == set(lifecycle.ALL_RUN_STATUSES)

    def test_the_module_is_dependency_free(self) -> None:
        """No import from ``farna`` — the neutral leaf both layers may load."""
        source = Path(lifecycle.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_modules.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
        offending = sorted(module for module in imported_modules if module == "farna" or module.startswith("farna."))
        assert not offending, f"farna.lifecycle must not import farna modules: {offending}"

    def test_the_leaf_lives_outside_core_and_api(self) -> None:
        """The neutral location is what lets the layering contracts hold unexempted."""
        assert Path(lifecycle.__file__).parent == _SRC_ROOT
        assert not (_SRC_ROOT / "core" / "run_lifecycle.py").exists()


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
        injected = (*TRANSITIONS, Transition(None, "create_ghost", "ghost_state", "targets a status the enum does not define"))
        with pytest.raises(LifecycleModelError, match="ghost_state"):
            derive_projections(transitions=injected)

    def test_a_seventh_non_terminal_status_fails_the_frozen_pins(self) -> None:
        """Inject ``paused_quota``: the computation succeeds, the pinned literals do not.

        This is the done-criterion, made to fire: while every projection is
        COMPUTED, a new status slides into it silently; the frozen snapshot is
        what forces the author to update the documented values deliberately.
        """
        injected_statuses = (*STATUSES, Status("paused_quota", IN_FLIGHT, "injected seventh non-terminal status"))
        injected_transitions = (
            *TRANSITIONS,
            Transition("paused_quota", "quota_replenished", lifecycle.STATUS_PENDING, "the quota window resets"),
        )
        derived = derive_projections(statuses=injected_statuses, transitions=injected_transitions)

        assert "paused_quota" in derived["ACTIVE_RUN_STATUSES"]
        assert derived["ACTIVE_RUN_STATUSES"] == SHIPPED_PROJECTIONS["ACTIVE_RUN_STATUSES"] | {"paused_quota"}
        assert derived["EXECUTING_STATUSES"] == SHIPPED_PROJECTIONS["EXECUTING_STATUSES"] | {"paused_quota"}
        assert derived["REDISPATCHABLE_STATUSES"] == SHIPPED_PROJECTIONS["REDISPATCHABLE_STATUSES"] | {"paused_quota"}
        violations = _projection_violations(derived, SHIPPED_PROJECTIONS)
        assert violations, "an added non-terminal status must fail the pinned projections"
        assert any("paused_quota" in violation for violation in violations), violations

    def test_a_projection_that_omits_a_non_terminal_status_is_rejected(self) -> None:
        """The exact 26-hour-capacity-incident shape: one projection forgets a status."""
        doctored = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        doctored["ACTIVE_RUN_STATUSES"] = doctored["ACTIVE_RUN_STATUSES"] - {"unknown"}

        violations = _projection_violations(doctored, SHIPPED_PROJECTIONS)

        assert violations, "a projection that silently dropped a status must be reported"
        assert any("ACTIVE_RUN_STATUSES" in violation and "unknown" in violation for violation in violations), violations

    def test_a_projection_that_adds_a_terminal_status_is_rejected(self) -> None:
        doctored = {name: getattr(lifecycle, name) for name in DOCUMENTED_PROJECTIONS}
        doctored["PIPELINE_CAPACITY_STATUSES"] = doctored["PIPELINE_CAPACITY_STATUSES"] | {"awaiting_human"}

        violations = _projection_violations(doctored, SHIPPED_PROJECTIONS)

        assert any("PIPELINE_CAPACITY_STATUSES" in violation and "awaiting_human" in violation for violation in violations), violations


class TestWritersUseDeclaredEdges:
    """Every writer's ``allowed_from`` is a subset of the table's declared edges."""

    def test_every_explicit_allowed_from_literal_is_a_declared_edge(self) -> None:
        offenders: list[str] = []
        for path in sorted(_SRC_ROOT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and _call_name(node) == "transition_run"):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                target_node = keywords.get("target_status")
                allowed_node = keywords.get("allowed_from")
                if not (isinstance(target_node, ast.Constant) and isinstance(target_node.value, str) and allowed_node is not None):
                    continue
                literals = {element.value for element in ast.walk(allowed_node) if isinstance(element, ast.Constant) and isinstance(element.value, str)}
                if not literals:
                    continue
                declared = allowed_from_for_target(target_node.value)
                undeclared = literals - set(declared)
                if undeclared:
                    offenders.append(f"{path.name}:{node.lineno} target={target_node.value!r} undeclared={sorted(undeclared)}")
        assert not offenders, f"writers take edges the table does not declare: {offenders}"

    def test_the_gate_claim_route_derives_its_allowed_from_from_the_table(self) -> None:
        from farna.api.routes import hitl

        claimed_edges = allowed_from_for_target(STATUS_CLAIMED)
        gate_claim_from = hitl._GATE_CLAIM_TO_CLAIMED_ALLOWED_FROM
        assert gate_claim_from <= claimed_edges
        assert gate_claim_from == frozenset({STATUS_AWAITING_HUMAN})

    def test_transition_run_defaults_to_the_declared_edges(self) -> None:
        """The default guard is the table, not the whole whitelist."""
        assert allowed_from_for_target(STATUS_RUNNING) == frozenset(
            {
                lifecycle.STATUS_PENDING,
                STATUS_RUNNING,
                STATUS_AWAITING_HUMAN,
                STATUS_CLAIMED,
                STATUS_HITL_PARKED,
                # Operator recovery deliberately re-opens a terminal run.
                lifecycle.STATUS_FAILED,
                lifecycle.STATUS_EVAL_FAILED,
            }
        )
        assert allowed_from_for_target(lifecycle.STATUS_COMPLETE) == frozenset({STATUS_RUNNING, lifecycle.STATUS_UNKNOWN})
        assert not allowed_from_for_target("no_such_status")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None
