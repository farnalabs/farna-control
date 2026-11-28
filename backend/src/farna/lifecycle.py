"""The run lifecycle: statuses and transitions as data — ONE source of truth.

Every derived run-status set in the codebase is a *projection* of this module:

* :data:`ACTIVE_RUN_STATUSES` — every non-terminal status (a run that still holds
  a slot, queued or otherwise).
* :data:`EXECUTING_STATUSES` / :data:`PIPELINE_CAPACITY_STATUSES` — statuses that
  occupy a PIPELINE execution slot (in flight, plus ``unknown``, whose outcome is
  indeterminate but which still holds its slot).
* :data:`LIVE_STATUSES` — the analytics spelling of the active projection.
* :data:`TRIGGER_SLOT_HOLDING_STATUSES` — active statuses that hold a per-trigger
  slot (everything except a not-yet-claimed ``pending`` run).
* :data:`HITL_CLAIMABLE_RUN_STATUSES` — statuses from which a gate claim may
  acquire: exactly the ``from_status`` of a :data:`ACTION_CLAIM_GATE` transition.
* :data:`HITL_ACTIONABLE_RUN_STATUSES` — claimable statuses plus every status a
  claim can leave the run in: exactly ``{from_status, to_status}`` of every
  :data:`ACTION_CLAIM_GATE` transition.
* :data:`ONGOING_ACTIVE_STATUSES` — the ongoing-trigger top-up population:
  queued plus in-flight runs (a recovering run is deliberately excluded: a
  stuck ``unknown`` run is not a fresh unit of work).
* :data:`REDISPATCHABLE_STATUSES` — active minus the indeterminate ``unknown``
  outcome: the statuses the dispatcher/reconcile re-dispatch legs select.

The 26-hour capacity incident (2026-09-04) happened because two hand-maintained
projections of this same relation disagreed: the pipeline gate counted
``awaiting_human`` as executing while the park sweep treated it as parked.
Deriving every projection from one transition table makes that class of
disagreement structurally impossible.

Dependency-free by design
-------------------------
This module lives at the top of the package (``farna.lifecycle``) and imports
nothing from ``farna``. Both the DB layer (``farna.db.models.run``) and the
core/API layers import it, so it must stay a leaf: importing a db model or a
route here would create a cycle, and importing any ``farna.core`` business
module would make the DB layer depend on core. Being outside ``farna.core`` and
``farna.api`` is what lets the layering contracts
(``db-does-not-import-core`` / ``connectors-do-not-import-core``) hold without
an exemption.

Status classes
--------------
A non-terminal run is in exactly ONE class, and the classes partition the
non-terminal vocabulary:

* ``queued``      — admitted, not yet claimed (``pending``).
* ``in_flight``   — claimed and being executed (``running``, ``claimed``).
* ``recovering``  — outcome indeterminate, still holds its slot (``unknown``).
* ``parked``      — stopped on a human decision (``awaiting_human``,
  ``hitl_parked``).

No status is ever both executing and parked; ``assert_no_double_counting``-style
checks live in ``tests/unit/core/test_run_lifecycle.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Status literals — the closed vocabulary of ``ck_runs_status``.
# ---------------------------------------------------------------------------
STATUS_PENDING: Final[str] = "pending"
STATUS_RUNNING: Final[str] = "running"
STATUS_AWAITING_HUMAN: Final[str] = "awaiting_human"
STATUS_CLAIMED: Final[str] = "claimed"
STATUS_UNKNOWN: Final[str] = "unknown"
STATUS_HITL_PARKED: Final[str] = "hitl_parked"
STATUS_COMPLETE: Final[str] = "complete"
STATUS_FAILED: Final[str] = "failed"
STATUS_CANCELLED: Final[str] = "cancelled"
STATUS_EVAL_FAILED: Final[str] = "eval_failed"
STATUS_STALLED: Final[str] = "stalled"
STATUS_BUDGET_EXCEEDED: Final[str] = "budget_exceeded"
STATUS_ROUTER_NO_MATCH: Final[str] = "router_no_match"
STATUS_COST_CEILING_EXCEEDED: Final[str] = "cost_ceiling_exceeded"
STATUS_COMPENSATION_FAILED: Final[str] = "compensation_failed"
STATUS_REJECTED: Final[str] = "rejected"

# Public aliases kept for the existing importers of ``farna.db.models.run``.
AWAITING_HUMAN_STATUS: Final[str] = STATUS_AWAITING_HUMAN
HITL_PARKED_STATUS: Final[str] = STATUS_HITL_PARKED

# ---------------------------------------------------------------------------
# Status classes — the mutually exclusive partition of the non-terminal set.
# ---------------------------------------------------------------------------
QUEUED: Final[str] = "queued"
IN_FLIGHT: Final[str] = "in_flight"
RECOVERING: Final[str] = "recovering"
PARKED: Final[str] = "parked"
TERMINAL: Final[str] = "terminal"

NON_TERMINAL_CLASSES: frozenset[str] = frozenset({QUEUED, IN_FLIGHT, RECOVERING, PARKED})
ALL_CLASSES: frozenset[str] = NON_TERMINAL_CLASSES | {TERMINAL}


@dataclass(frozen=True, slots=True)
class Status:
    """One run status: its literal, its lifecycle class, a one-line description."""

    name: str
    cls: str
    description: str


STATUSES: tuple[Status, ...] = (
    Status(
        STATUS_PENDING,
        QUEUED,
        "admitted and waiting for a worker to claim it; holds a pipeline slot only once claimed",
    ),
    Status(
        STATUS_RUNNING,
        IN_FLIGHT,
        "claimed by a worker and executing (or being re-claimed after a stale heartbeat)",
    ),
    Status(
        STATUS_AWAITING_HUMAN,
        PARKED,
        "stopped at a HITL gate waiting for a human decision; the gate is open and claimable",
    ),
    Status(
        STATUS_CLAIMED,
        IN_FLIGHT,
        "a reviewer holds the HITL gate; the run resumes when the decision is committed",
    ),
    Status(
        STATUS_UNKNOWN,
        RECOVERING,
        "the outcome could not be determined (e.g. a lost sandbox); holds its slot until an operator re-runs it",
    ),
    Status(
        STATUS_HITL_PARKED,
        PARKED,
        "a gate expired unanswered past the grace window and the park sweep moved the run out of review state",
    ),
    Status(STATUS_COMPLETE, TERMINAL, "the run finished and delivered its work"),
    Status(STATUS_FAILED, TERMINAL, "the run failed"),
    Status(STATUS_CANCELLED, TERMINAL, "an operator or the agent cancelled the run"),
    Status(STATUS_EVAL_FAILED, TERMINAL, "a blocking eval (or guardrail) ended the run"),
    Status(STATUS_STALLED, TERMINAL, "the run stalled and was terminalised"),
    Status(STATUS_BUDGET_EXCEEDED, TERMINAL, "the run exhausted its configured budget"),
    Status(STATUS_ROUTER_NO_MATCH, TERMINAL, "no model route matched the run's requirements"),
    Status(STATUS_COST_CEILING_EXCEEDED, TERMINAL, "the run crossed its cost ceiling"),
    Status(STATUS_COMPENSATION_FAILED, TERMINAL, "terminating the run could not compensate its executed nodes"),
    Status(STATUS_REJECTED, TERMINAL, "a gate rejection ended the run (no reject route configured)"),
)


# ---------------------------------------------------------------------------
# Transitions — (from_status, action, to_status), each action documented once.
#
# ``from_status`` is ``None`` for creation. A terminal status has NO outgoing
# transition; every non-terminal status has at least one.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Transition:
    """One legal edge of the run lifecycle, with a one-line explanation.

    ``reopens_terminal`` marks the deliberate operator recovery edges that take a
    TERMINAL run back into execution (recover-node). Terminal is otherwise
    absorbing; keeping these edges in the table (instead of omitting them) is
    what makes every real writer expressible through the declared relation.
    """

    from_status: str | None
    action: str
    to_status: str
    description: str
    reopens_terminal: bool = False


#: Actions named by consumers that need to reason about the relation (the
#: gate-claim projection above, and the stateful test's legal-move chooser).
ACTION_CREATE: Final[str] = "create"
ACTION_CLAIM: Final[str] = "claim"
ACTION_CLAIM_GATE: Final[str] = "claim_gate"
ACTION_DECIDE: Final[str] = "decide"
ACTION_UNPARK: Final[str] = "unpark"
ACTION_PARK: Final[str] = "park"
ACTION_COMPLETE: Final[str] = "complete"
ACTION_FAIL: Final[str] = "fail"
ACTION_CANCEL: Final[str] = "cancel"
ACTION_RECONCILE_UNKNOWN: Final[str] = "reconcile_unknown"


TRANSITIONS: tuple[Transition, ...] = (
    # --- creation ---------------------------------------------------------
    Transition(None, ACTION_CREATE, STATUS_PENDING, "create_run admits a run as pending"),
    Transition(None, "create_guardrail_blocked", STATUS_EVAL_FAILED, "create_run stamps a guardrail-blocked run terminal at birth"),
    # --- pending ----------------------------------------------------------
    Transition(STATUS_PENDING, ACTION_CLAIM, STATUS_RUNNING, "a worker's SAQ claim moves the run to running (the atomic claim UPDATE)"),
    Transition(STATUS_PENDING, "defer_capacity", STATUS_AWAITING_HUMAN, "an admission-capacity denial defers a queued run to a human"),
    Transition(STATUS_PENDING, "expire_queue", STATUS_REJECTED, "a run that sat queued past its expiry is rejected (queue.expired)"),
    Transition(STATUS_PENDING, "never_dispatched", STATUS_FAILED, "an enqueue failure terminal-fails a pending run (never_dispatched)"),
    Transition(STATUS_PENDING, "retry_budget_exhausted", STATUS_FAILED, "the claim-attempt cap terminal-fails a pending run"),
    # --- running ----------------------------------------------------------
    Transition(STATUS_RUNNING, "heartbeat", STATUS_RUNNING, "a live executor refreshes the run's heartbeat (self-transition)"),
    Transition(STATUS_RUNNING, "request_hitl", STATUS_AWAITING_HUMAN, "the graph interrupts at a HITL gate and the run waits for a human"),
    Transition(STATUS_RUNNING, "defer_capacity", STATUS_AWAITING_HUMAN, "a pipeline-capacity denial after claim defers the run to a human"),
    Transition(STATUS_RUNNING, "reclaim_stale", STATUS_RUNNING, "a stale-heartbeat running run is re-claimed by a new worker (self-transition)"),
    Transition(STATUS_RUNNING, ACTION_COMPLETE, STATUS_COMPLETE, "the graph finishes and the run is completed"),
    Transition(STATUS_RUNNING, ACTION_FAIL, STATUS_FAILED, "an execution failure terminal-fails the run"),
    Transition(STATUS_RUNNING, "reject_terminal", STATUS_REJECTED, "a gate rejection with no reject route ends the resumed run rejected"),
    Transition(STATUS_RUNNING, "eval_fail", STATUS_EVAL_FAILED, "a blocking eval terminal-fails the run"),
    Transition(STATUS_RUNNING, "stall", STATUS_STALLED, "a stalled run is terminalised"),
    Transition(STATUS_RUNNING, "budget_exceeded", STATUS_BUDGET_EXCEEDED, "the run exhausts its budget"),
    Transition(STATUS_RUNNING, "router_no_match", STATUS_ROUTER_NO_MATCH, "no model route matches"),
    Transition(STATUS_RUNNING, "cost_ceiling_exceeded", STATUS_COST_CEILING_EXCEEDED, "the run crosses its cost ceiling"),
    Transition(STATUS_RUNNING, "compensation_failed", STATUS_COMPENSATION_FAILED, "node compensation fails while terminating"),
    Transition(STATUS_RUNNING, "mark_unknown", STATUS_UNKNOWN, "the executor cannot determine the outcome and marks the run unknown"),
    Transition(STATUS_RUNNING, "heartbeat_stale_reset", STATUS_PENDING, "reconciliation resets an in-budget stale run to pending for re-dispatch"),
    # --- awaiting_human ---------------------------------------------------
    Transition(STATUS_AWAITING_HUMAN, ACTION_CLAIM_GATE, STATUS_CLAIMED, "a reviewer claims the open gate and the run becomes claimed"),
    Transition(STATUS_AWAITING_HUMAN, ACTION_DECIDE, STATUS_RUNNING, "a committed decision lets the resume claim take the run back to running"),
    Transition(STATUS_AWAITING_HUMAN, ACTION_PARK, STATUS_HITL_PARKED, "the park sweep moves an expired-unanswered run out of review state"),
    Transition(STATUS_AWAITING_HUMAN, "expire_review", STATUS_CANCELLED, "an expired review window cancels the run (hitl_review_expired)"),
    # --- claimed ----------------------------------------------------------
    Transition(STATUS_CLAIMED, ACTION_DECIDE, STATUS_RUNNING, "the committed decision's resume claim takes the run back to running"),
    Transition(STATUS_CLAIMED, "reject_terminal", STATUS_REJECTED, "a rejection with no reject route ends the run rejected"),
    Transition(STATUS_CLAIMED, "claim_expired", STATUS_AWAITING_HUMAN, "an expired gate claim returns the run to awaiting_human"),
    # --- hitl_parked ------------------------------------------------------
    Transition(STATUS_HITL_PARKED, ACTION_CLAIM_GATE, STATUS_HITL_PARKED, "a parked run's gate stays claimable and the run stays parked"),
    Transition(STATUS_HITL_PARKED, "resume_claim", STATUS_RUNNING, "the resume claim takes a parked run back to running once its gate is decided"),
    Transition(STATUS_HITL_PARKED, ACTION_UNPARK, STATUS_AWAITING_HUMAN, "a committed decision un-parks the run into normal admission"),
    Transition(STATUS_HITL_PARKED, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a parked run"),
    # --- claimed (same-account re-claim, FAR-686 token recovery) ----------
    Transition(STATUS_CLAIMED, "reclaim_same_account", STATUS_CLAIMED, "the same reviewer re-claims their own held gate (self-transition), never another account's"),
    # --- unknown ----------------------------------------------------------
    Transition(STATUS_UNKNOWN, ACTION_RECONCILE_UNKNOWN, STATUS_COMPLETE, "an operator's same-key re-run reconciles the outcome to complete"),
    Transition(STATUS_UNKNOWN, "reconcile_unknown_failed", STATUS_FAILED, "an operator's same-key re-run reconciles the outcome to failed"),
    # --- guarded SAQ task-failure fence (saq_hooks._mark_run_failed) -------
    # The fence fails any non-terminal, non-``unknown`` run whose claimed SAQ job
    # died, so these are real edges of the relation, not error-handling escapes.
    Transition(STATUS_AWAITING_HUMAN, "saq_failure", STATUS_FAILED, "the SAQ job claimed by an awaiting_human run died and the fence failed it"),
    Transition(STATUS_CLAIMED, "saq_failure", STATUS_FAILED, "the SAQ job claimed by a claimed run died and the fence failed it"),
    Transition(STATUS_HITL_PARKED, "saq_failure", STATUS_FAILED, "the SAQ job claimed by a parked run died and the fence failed it"),
    # --- operator recovery (deliberately re-opens a terminal run) ----------
    Transition(STATUS_FAILED, "recover_node", STATUS_RUNNING, "an operator recovers a failed run at a node; the recovery lock re-enters execution", True),
    Transition(STATUS_EVAL_FAILED, "recover_node", STATUS_RUNNING, "an operator recovers a guardrail-blocked run at a started node (recovery lock)", True),
    Transition(STATUS_EVAL_FAILED, "recover_node_redispatch", STATUS_PENDING, "an operator recovers a zero-node guardrail block for a fresh dispatch", True),
    # --- cancellation (every non-terminal status can be cancelled) --------
    Transition(STATUS_PENDING, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a queued run"),
    Transition(STATUS_RUNNING, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a running run (cancel wins over every writer)"),
    Transition(STATUS_AWAITING_HUMAN, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a run waiting on a human"),
    Transition(STATUS_CLAIMED, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a run whose gate is claimed"),
    Transition(STATUS_UNKNOWN, ACTION_CANCEL, STATUS_CANCELLED, "an operator cancels a run whose outcome was indeterminate"),
)


# ---------------------------------------------------------------------------
# Derivations — every projection below is computed, never hand-listed.
#
# The module derives its OWN public sets through :func:`derive_projections`,
# the same function the unit test pins. An inconsistent table therefore fails
# at import time with a named violation instead of silently changing what a
# consumer sees — the failure mode the 2026-09-04 incident needed.
# ---------------------------------------------------------------------------
class LifecycleModelError(ValueError):
    """The lifecycle data is internally inconsistent (an unclassified status)."""


def transitions_from(status: str, transitions: tuple[Transition, ...] = TRANSITIONS) -> tuple[Transition, ...]:
    """Every transition whose source is *status* (empty for a terminal status)."""
    return tuple(transition for transition in transitions if transition.from_status == status)


def allowed_from_for_target(target_status: str, transitions: tuple[Transition, ...] = TRANSITIONS) -> frozenset[str]:
    """Every status the table declares a transition INTO *target_status* from.

    This is the guard a writer passes as ``allowed_from``: a write whose source
    status is not a declared edge is rejected by the conditional UPDATE, so an
    edge missing from the table is structurally impossible to take (the table,
    not a hand-written literal, decides). Empty when nothing may enter the
    status — the write can then never apply.
    """
    return frozenset(t.from_status for t in transitions if t.to_status == target_status and t.from_status is not None)


def transitions_for_action(action: str, transitions: tuple[Transition, ...] = TRANSITIONS) -> tuple[Transition, ...]:
    """Every transition named *action* (the edge list a writer's guard derives from)."""
    return tuple(transition for transition in transitions if transition.action == action)


def lifecycle_violations(statuses: tuple[Status, ...] = STATUSES, transitions: tuple[Transition, ...] = TRANSITIONS) -> list[str]:
    """Return every structural inconsistency in the lifecycle data (empty = sound)."""
    violations: list[str] = []
    names = [status.name for status in statuses]
    known = frozenset(names)

    if len(names) != len(known):
        duplicates = sorted(name for name in known if names.count(name) > 1)
        violations.append(f"duplicate status definitions: {duplicates}")

    for status in statuses:
        if status.cls not in ALL_CLASSES:
            violations.append(f"status {status.name!r} has unknown class {status.cls!r}")
        if not status.description.strip():
            violations.append(f"status {status.name!r} has no description")

    for transition in transitions:
        if transition.from_status is not None and transition.from_status not in known:
            violations.append(f"transition {transition.action!r} starts at undefined status {transition.from_status!r}")
        if transition.to_status not in known:
            violations.append(f"transition {transition.action!r} targets undefined status {transition.to_status!r}")
        if not transition.action.strip():
            violations.append(f"transition {transition.from_status!r}->{transition.to_status!r} has no action")
        if not transition.description.strip():
            violations.append(f"transition {transition.action!r} has no description")
        elif "\n" in transition.description.strip():
            violations.append(f"transition {transition.action!r} description is not one line")

    terminal = frozenset(status.name for status in statuses if status.cls == TERMINAL)
    non_terminal = known - terminal
    violations.extend(f"non-terminal status {name!r} has no outgoing transition" for name in sorted(non_terminal) if not transitions_from(name, transitions))
    violations.extend(f"terminal status {name!r} has an outgoing transition that does not declare ``reopens_terminal``" for name in sorted(terminal) if any(not transition.reopens_terminal for transition in transitions_from(name, transitions)))
    violations.extend(f"transition {transition.action!r} declares ``reopens_terminal`` from non-terminal status {transition.from_status!r}" for transition in transitions if transition.reopens_terminal and transition.from_status not in terminal)

    # The non-terminal classes must be a pairwise-disjoint partition.
    classes = {status.name: status.cls for status in statuses}
    for name in sorted(non_terminal):
        status_class = classes[name]
        if status_class not in NON_TERMINAL_CLASSES:
            violations.append(f"non-terminal status {name!r} is in class {status_class!r}, not a non-terminal class")

    # A run is never both executing and parked (the incident's double count).
    in_flight_or_recovering = frozenset(status.name for status in statuses if status.cls in (IN_FLIGHT, RECOVERING))
    parked = frozenset(status.name for status in statuses if status.cls == PARKED)
    overlap = in_flight_or_recovering & parked
    if overlap:
        violations.append(f"status(es) both executing and parked: {sorted(overlap)}")

    return violations


def derive_projections(statuses: tuple[Status, ...] = STATUSES, transitions: tuple[Transition, ...] = TRANSITIONS) -> dict[str, frozenset[str]]:
    """Derive every named projection from *statuses* and *transitions*.

    Raises :class:`LifecycleModelError` for a status whose class is unknown, a
    transition that names an undefined status, or a non-terminal status with no
    outgoing transition — the three ways a newly added status can silently
    break every consumer.
    """
    violations = lifecycle_violations(statuses, transitions)
    if violations:
        raise LifecycleModelError("; ".join(violations))

    def by_class(status_class: str) -> frozenset[str]:
        return frozenset(status.name for status in statuses if status.cls == status_class)

    all_statuses = frozenset(status.name for status in statuses)
    terminal = by_class(TERMINAL)
    active = all_statuses - terminal
    queued = by_class(QUEUED)
    in_flight = by_class(IN_FLIGHT)
    recovering = by_class(RECOVERING)
    parked = by_class(PARKED)
    claim_sources = frozenset(t.from_status for t in transitions if t.action == ACTION_CLAIM_GATE and t.from_status is not None)
    claim_targets = frozenset(t.to_status for t in transitions if t.action == ACTION_CLAIM_GATE)
    executing = in_flight | recovering
    return {
        "ALL_RUN_STATUSES": all_statuses,
        "TERMINAL_STATUSES": terminal,
        "ACTIVE_RUN_STATUSES": active,
        "QUEUED_STATUSES": queued,
        "IN_FLIGHT_STATUSES": in_flight,
        "RECOVERING_STATUSES": recovering,
        "PARKED_STATUSES": parked,
        "EXECUTING_STATUSES": executing,
        "PIPELINE_CAPACITY_STATUSES": executing,
        "LIVE_STATUSES": active,
        "TRIGGER_SLOT_HOLDING_STATUSES": active - queued,
        "ONGOING_ACTIVE_STATUSES": queued | in_flight,
        "REDISPATCHABLE_STATUSES": active - recovering,
        "HITL_CLAIMABLE_RUN_STATUSES": claim_sources,
        "HITL_ACTIONABLE_RUN_STATUSES": claim_sources | claim_targets,
        "SUCCESS_STATUSES": frozenset({STATUS_COMPLETE}),
    }


# The module's own projections. Importing this module therefore also validates
# the table (a programming error raises ``LifecycleModelError`` immediately).
_PROJECTIONS: dict[str, frozenset[str]] = derive_projections()

#: The whole closed vocabulary (the ``ck_runs_status`` values).
ALL_RUN_STATUSES: frozenset[str] = _PROJECTIONS["ALL_RUN_STATUSES"]

#: Terminal statuses: the run is finalised and can never transition again.
TERMINAL_STATUSES: frozenset[str] = _PROJECTIONS["TERMINAL_STATUSES"]

#: Non-terminal statuses — a run that still holds a slot (queued or otherwise).
ACTIVE_RUN_STATUSES: frozenset[str] = _PROJECTIONS["ACTIVE_RUN_STATUSES"]
NON_TERMINAL_STATUSES: frozenset[str] = ACTIVE_RUN_STATUSES

QUEUED_STATUSES: frozenset[str] = _PROJECTIONS["QUEUED_STATUSES"]
IN_FLIGHT_STATUSES: frozenset[str] = _PROJECTIONS["IN_FLIGHT_STATUSES"]
RECOVERING_STATUSES: frozenset[str] = _PROJECTIONS["RECOVERING_STATUSES"]
PARKED_STATUSES: frozenset[str] = _PROJECTIONS["PARKED_STATUSES"]

#: Statuses that occupy a PIPELINE execution slot. A parked run does not
#: execute (the 2026-09-04 incident); ``unknown`` does (it must be accounted for
#: until it is reconciled).
EXECUTING_STATUSES: frozenset[str] = _PROJECTIONS["EXECUTING_STATUSES"]
PIPELINE_CAPACITY_STATUSES: frozenset[str] = _PROJECTIONS["PIPELINE_CAPACITY_STATUSES"]

#: Analytics spelling of the active projection.
LIVE_STATUSES: frozenset[str] = _PROJECTIONS["LIVE_STATUSES"]

#: Active statuses that hold a per-trigger slot (everything but an unclaimed run).
TRIGGER_SLOT_HOLDING_STATUSES: frozenset[str] = _PROJECTIONS["TRIGGER_SLOT_HOLDING_STATUSES"]

#: The ongoing-trigger top-up population: queued work plus a run actually being
#: executed. ``unknown`` (a stuck recovery) and parked runs are excluded.
ONGOING_ACTIVE_STATUSES: frozenset[str] = _PROJECTIONS["ONGOING_ACTIVE_STATUSES"]

#: Active statuses a dispatcher/reconcile re-dispatch leg may select: the active
#: vocabulary minus ``unknown`` (an indeterminate outcome is reconciled by an
#: operator re-run with the same idempotency key, never re-dispatched as a fresh
#: unit of work — see ``ACTIVE_RUN_STATUSES`` and the transient-status docstring
#: in ``farna.db.models.run``).
REDISPATCHABLE_STATUSES: frozenset[str] = _PROJECTIONS["REDISPATCHABLE_STATUSES"]

#: Statuses from which a gate claim may ACQUIRE (FAR-645).
HITL_CLAIMABLE_RUN_STATUSES: frozenset[str] = _PROJECTIONS["HITL_CLAIMABLE_RUN_STATUSES"]

#: Statuses under which an undecided gate is actionable work (FAR-612): the
#: claimable statuses plus the statuses a claim leaves the run in (the gate is
#: then held, but still undecided).
HITL_ACTIONABLE_RUN_STATUSES: frozenset[str] = _PROJECTIONS["HITL_ACTIONABLE_RUN_STATUSES"]

#: Terminal statuses that represent a DELIVERED run.
SUCCESS_STATUSES: frozenset[str] = _PROJECTIONS["SUCCESS_STATUSES"]

#: ``transition_run`` / ``update_run_status`` refuse anything outside this set.
RUN_STATUS_WHITELIST: frozenset[str] = ALL_RUN_STATUSES


def sql_in_list(statuses: frozenset[str] | tuple[str, ...]) -> str:
    """Render *statuses* as a SQL ``IN`` body (``'a', 'b'``), sorted for stability.

    Raw-SQL writers and partial-index predicates cannot bind Python sets, so they
    embed the vocabulary as text. They MUST build it here from the lifecycle
    projections — a hand-written literal list is a second source of truth that
    silently drops a status added to the table (the FAR-2705 defect class).
    """
    return ", ".join(f"'{status}'" for status in sorted(statuses))


#: The terminal vocabulary as a SQL ``IN`` body for raw-SQL writers/indexes.
TERMINAL_STATUS_SQL: str = sql_in_list(TERMINAL_STATUSES)

#: The projections the unit test pins against the table (and the shipped values).
DOCUMENTED_PROJECTIONS: tuple[str, ...] = (
    "ACTIVE_RUN_STATUSES",
    "PIPELINE_CAPACITY_STATUSES",
    "EXECUTING_STATUSES",
    "LIVE_STATUSES",
    "TRIGGER_SLOT_HOLDING_STATUSES",
    "ONGOING_ACTIVE_STATUSES",
    "REDISPATCHABLE_STATUSES",
    "HITL_CLAIMABLE_RUN_STATUSES",
    "HITL_ACTIONABLE_RUN_STATUSES",
    "TERMINAL_STATUSES",
    "SUCCESS_STATUSES",
)
