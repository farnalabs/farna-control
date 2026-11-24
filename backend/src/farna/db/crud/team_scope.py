"""Shared team-boundary helpers for the RLS-parity authorization floor.

The ``owner_team_id`` boundary predicate — org-level (NULL owner) OR owned by
the caller's team — and the effective-owner resolver are centralised here so
the semantics cannot drift between the list filters (pipelines, runs, triggers,
HITL pending gates, analytics facts) and the MCP per-row guards.

Live in the DB layer (not ``farna.api``) so both ``farna.db`` and
``farna.core`` can import it without violating the layer contracts — which is
also why :func:`team_blind_org_scope` (the FAR-1515 widened read) lives HERE:
the binding gates in ``farna.core.team_visibility`` need it, and ``farna.db``
must not import ``farna.core``.

ADR 070 §3 — no escape may widen past ``organisation_id``:
:func:`team_blind_org_scope` widens ONLY the team clause
(``app.execution_context``). Its ``org_id`` argument is a claim the session
must already satisfy, never a value it writes: the helper refuses (with
:class:`TeamBlindScopeOrgMismatchError`) when the session's own
``app.organisation_id`` is unset or different. Enforce the org from the
authenticated principal (``db.rls.set_rls_org``) BEFORE widening.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Text, cast, false, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from farna.db.models.pipeline import Pipeline
from farna.db.models.run import Run
from farna.db.models.team_membership import TeamMembership

# FAR-1515 CRITICAL 1: candidate rows behind the connector/model-backend team
# gates must be read TEAM-BLIND but ORG-SCOPED. ``rls_team_isolation``
# (migration 0124) hides a ``visibility='team'`` row the caller does not own,
# so a plain read in the request session returns NOTHING for another team's
# row - the mismatch loop would skip it silently and the write would be
# accepted (at run time the hub loads the same row team-blind). These
# statements widen only the TEAM clause (``app.execution_context``) while
# keeping the org AND gate, then restore the caller's previous GUCs so the
# rest of the request transaction keeps its original visibility. Same
# ``set_config(..., is_local => true)`` form as ``db/rls.py``
# (transaction-scoped, reverts on rollback).
#
# ADR 070 §3: the org write in _RLS_WIDEN_SQL is a RE-STATEMENT, never a grant.
# ``team_blind_org_scope`` refuses to issue any of these statements unless the
# session's own ``app.organisation_id`` already equals its ``org_id``
# argument, so the value written here is provably the value already in force.
_RLS_READ_GUCS_SQL = "SELECT current_setting('app.organisation_id', true), current_setting('app.execution_context', true)"
_RLS_WIDEN_SQL = "SELECT set_config('app.organisation_id', :oid, true), set_config('app.execution_context', 'true', true)"
_RLS_RESTORE_SQL = "SELECT set_config('app.organisation_id', :oid, true), set_config('app.execution_context', :exec_ctx, true)"


def team_scope_clause(owner_team_id_col: Any, team_id: Any) -> ColumnElement[bool]:
    """Build the team-boundary WHERE predicate for *team_id*.

    An org-level row (NULL owner) is visible to every team; a row owned by
    *team_id* is visible to that team. Pass an effective-owner expression
    (e.g. ``func.coalesce(Run.owner_team_id, Pipeline.owner_team_id)``) when
    the row's stamped owner must take precedence over the pipeline's current
    owner.
    """
    return or_(owner_team_id_col.is_(None), owner_team_id_col == team_id)


def account_team_scope_clause(owner_team_id_col: Any, account_id: uuid.UUID | None) -> ColumnElement[bool]:
    """Build the team-visibility predicate for an account-scoped viewer read.

    Org-visible rows are available to every account. Team-private rows are
    available only when the account has a membership in the owning team. A
    missing account context is fail-closed rather than becoming an unscoped
    read.
    """
    if account_id is None:
        return false()
    membership_team_ids = select(TeamMembership.team_id).where(TeamMembership.account_id == account_id)
    return or_(owner_team_id_col.is_(None), owner_team_id_col.in_(membership_team_ids))


async def viewer_run_scope_visible(
    session: AsyncSession,
    run_id: uuid.UUID,
    account_id: uuid.UUID,
) -> bool:
    """True when a viewer's team memberships cover the run's effective owner.

    Tables that carry only the org-isolation RLS policy (``hitl_claims``,
    ``runs``) have no team boundary of their own, so a viewer read has to
    apply the account-team membership clamp explicitly. Org-level runs (no
    owning team) are visible to every account; a team-private run is visible
    only to members of the owning team. The run's stamped owner is
    authoritative, with the pipeline owner as the fallback for pre-stamp rows
    — the same precedence the list filters use.
    """
    visible = (
        await session.execute(
            select(Run.id)
            .join(Pipeline, Run.pipeline_id == Pipeline.id)
            .where(
                Run.id == run_id,
                account_team_scope_clause(func.coalesce(Run.owner_team_id, Pipeline.owner_team_id), account_id),
            )
        )
    ).first()
    return visible is not None


async def pipeline_owner_team_id(session: AsyncSession, pipeline_id: uuid.UUID) -> uuid.UUID | None:
    """Resolve a pipeline's ``owner_team_id`` (None for org-level pipelines)."""
    result = await session.execute(select(Pipeline.owner_team_id).where(Pipeline.id == pipeline_id))
    return result.scalar_one_or_none()


async def _resolve_dialect(session: AsyncSession) -> str:
    """Return the session bind's dialect name (``"postgresql"``, ``"sqlite"``, ...).

    Mirrors ``db/rls._ensure_active_transaction``'s bind resolution: a sync
    session returns the engine directly, an async session may return a
    coroutine that must be awaited. Type-annotated loosely because test
    doubles satisfy it structurally.
    """
    bind: Any = session.get_bind()
    if asyncio.iscoroutine(bind):
        bind = await bind
    return str(bind.dialect.name)


class TeamBlindScopeOrgMismatchError(RuntimeError):
    """``team_blind_org_scope`` was asked to widen to a foreign or unset org.

    ADR 070 §3: no escape may widen past ``organisation_id``. The helper's
    ``org_id`` argument must therefore MATCH the org the session is already
    bound to — it is a requirement to verify, never a value to write. A
    mismatched (or unset) session org means the caller has not established the
    tenant from the authenticated principal, so widening would be an authority
    grant: exactly the defect this error exists to refuse.

    ``RuntimeError`` matches the contract of the sibling RLS helpers
    (``db.rls.set_rls_*`` raise ``RuntimeError`` for a misused session), so a
    caller that already guards those keeps its existing behaviour.
    """

    def __init__(self, *, requested_org_id: uuid.UUID, session_org_id: object) -> None:
        self.requested_org_id = requested_org_id
        self.session_org_id = session_org_id
        super().__init__(
            "team_blind_org_scope refused: the session's app.organisation_id is "
            f"{_render_org(session_org_id)} but the widen was requested for organisation "
            f"{requested_org_id}. This helper widens ONLY the team clause - set the "
            "organisation from the authenticated principal (db.rls.set_rls_org) before "
            "widening; it will never adopt or overwrite the org for you."
        )


def _render_org(session_org_id: object) -> str:
    """Render a session org GUC value for the refusal message."""
    if session_org_id is None or (isinstance(session_org_id, str) and not session_org_id.strip()):
        return "<unset>"
    return repr(session_org_id)


def _parse_session_org(session_org_id: object) -> uuid.UUID | None:
    """Return the session org as a UUID, or ``None`` when unset/unparseable."""
    if session_org_id is None:
        return None
    try:
        return uuid.UUID(str(session_org_id).strip())
    except (AttributeError, TypeError, ValueError):
        return None


def _assert_session_org_matches(session_org_id: object, org_id: uuid.UUID) -> None:
    """Refuse to widen unless the session is already bound to *org_id*.

    Fail closed on every departure from an exact match: an unset GUC (``NULL``
    from a never-defined setting, or ``''`` from the pool-checkout reset), an
    unparseable value, and a different organisation all raise. The org write in
    ``_RLS_WIDEN_SQL`` is therefore provably a re-statement of the same value.
    """
    if _parse_session_org(session_org_id) != org_id:
        raise TeamBlindScopeOrgMismatchError(requested_org_id=org_id, session_org_id=session_org_id)


@asynccontextmanager
async def team_blind_org_scope(session: AsyncSession, org_id: uuid.UUID) -> AsyncIterator[None]:
    """Run the wrapped block with team visibility widened WITHIN the caller's org.

    FAR-1515 CRITICAL 1: the team-scope gates must judge rows the request
    caller's own RLS context cannot see - another team's ``visibility='team'``
    connector, or (on the connector re-scope path) another team's
    team-private pipeline binding the connector. On Postgres the block saves
    the caller's ``app.organisation_id`` / ``app.execution_context`` GUCs,
    widens to the internal-execution context, runs, and restores the caller's
    GUCs in a ``finally`` so the rest of the request transaction keeps its
    original team visibility.

    ADR 070 §3 - the widenable scope is the TEAM clause ONLY, never the org:

    * *org_id* is a REQUIREMENT, not a value this helper writes. The session
      must already carry that same ``app.organisation_id`` (set from the
      authenticated principal via ``db.rls.set_rls_org``); a session whose org
      is unset or different is refused with
      :class:`TeamBlindScopeOrgMismatchError` BEFORE anything is widened. A
      helper that can *set* the org is an authority grant, not an escape
      hatch - it would be structurally capable of crossing tenants for any
      caller that passed the wrong id.
    * the widen then re-states the (now proven identical) org and sets
      ``app.execution_context``; every read is still AND-gated by
      ``app.organisation_id``, so it can never cross organisations.

    Off Postgres there is no team RLS policy (the tenant filter scopes by org
    only), so reads are already team-blind and nothing is issued - there is no
    widen to bound, and no reachable cross-tenant grant.
    """
    if await _resolve_dialect(session) != "postgresql":
        yield
        return
    prev_org, prev_exec_ctx = (await session.execute(text(_RLS_READ_GUCS_SQL))).one()
    _assert_session_org_matches(prev_org, org_id)
    await session.execute(text(_RLS_WIDEN_SQL), {"oid": str(org_id)})
    try:
        yield
    finally:
        await session.execute(
            text(_RLS_RESTORE_SQL),
            {"oid": prev_org or "", "exec_ctx": prev_exec_ctx or ""},
        )


async def pipelines_binding_connector(
    session: AsyncSession,
    org_id: uuid.UUID,
    connector_id: uuid.UUID,
) -> list[Pipeline]:
    """Candidate pipelines in *org_id* whose stored graph mentions *connector_id*.

    FAR-1515 MAJOR 5: the connector visibility/owner PATCH needs the set of
    pipelines that bind the instance it is about to re-scope. The JSON column
    is searched with a text-contains prefilter (portable across ``json`` on
    Postgres and the SQLite dev backend); a UUID can only appear as the exact
    id or inside a larger string, so the CALLER must confirm the real binding
    (``extract_connector_bindings`` + an exact id match) before acting on a
    candidate — this query is deliberately a cheap prefilter, not the verdict.

    Read through :func:`team_blind_org_scope` for the same reason the binding
    gate itself is: under ``rls_team_isolation`` another team's team-private
    pipeline (exactly the row whose binding a re-scope could strand) is
    invisible to the request caller, and a prefilter that cannot see its own
    targets fails open. Soft-deleted pipelines are excluded (they cannot run,
    so they cannot be harmed by the re-scope).
    """
    pattern = f"%{connector_id}%"
    async with team_blind_org_scope(session, org_id):
        result = await session.execute(
            select(Pipeline).where(
                Pipeline.organisation_id == org_id,
                Pipeline.deleted_at.is_(None),
                cast(Pipeline.graph_nodes_json, Text).like(pattern),
            )
        )
        return list(result.scalars())


@dataclass(frozen=True)
class BlindPipelineScope:
    """A pipeline's team-gate inputs as seen even when RLS hides the row.

    FAR-1513: a caller-facing read under user RLS returns nothing for a
    team-private pipeline the caller cannot see, which made the pre-existing
    MCP gate fail OPEN (unknown visibility → allow). This read is org-scoped
    but team-BLIND: it flips the ``app.execution_context`` GUC so the
    ``rls_team_isolation`` policy's execution-context arm lets the org-scoped
    row through, then clears the GUC so the *rest* of the transaction keeps
    caller-facing visibility. Callers must act on ``row_present`` first —
    an absent row means the pipeline does not exist (or is soft-deleted),
    which is a denial by itself.
    """

    owner_team_id: uuid.UUID | None
    visibility: str | None


async def pipeline_team_scope_team_blind(
    session: AsyncSession,
    pipeline_id: uuid.UUID,
) -> BlindPipelineScope | None:
    """Read a pipeline's team-gate fields UNBLINDED to team visibility.

    Org-scoped (``app.organisation_id`` stays set) but team-blind: inside the
    same transaction the ``app.execution_context`` GUC is turned ON for the
    read and turned OFF again afterwards, so only this read sees through
    team isolation and to every later read in the session stays caller-facing.
    Returns ``None`` when no non-deleted pipeline with that id exists in the
    org — calls to never use this to *surface* the row, only to decide a
    boundary denial.

    Raises ``RuntimeError`` when called outside an active transaction (same
    contract as ``set_rls_execution_context``); on any other failure the GUC
    reverts with the transaction rollback, so no execution-context leak is
    possible from a failed read.
    """
    from farna.db.rls import _ensure_active_transaction, set_rls_execution_context

    dialect = await _ensure_active_transaction(session)
    if dialect != "postgresql":
        # Only Postgres carries the team-isolation policy; on the SQLite
        # (test-only) backend team filtering does not exist, so the plain
        # org-scoped read IS the team-blind view there. session.info carries no
        # effective GUC.
        stmt = select(Pipeline.owner_team_id, Pipeline.visibility).where(
            Pipeline.id == pipeline_id,
            Pipeline.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is None:
            return None
        return BlindPipelineScope(owner_team_id=row[0], visibility=row[1])

    await set_rls_execution_context(session)
    try:
        stmt = select(Pipeline.owner_team_id, Pipeline.visibility).where(
            Pipeline.id == pipeline_id,
            Pipeline.deleted_at.is_(None),
        )
        result = await session.execute(stmt)
        row = result.first()
    finally:
        # Clear the GUC ONLY after a successful read: if the read raised, the
        # transaction unwinds and is_local reverts with the rollback, so there
        # is no branch that leaves execution context set on a live session.
        await session.execute(text("SELECT set_config('app.execution_context', '', true)"))
    if row is None:
        return None
    return BlindPipelineScope(owner_team_id=row[0], visibility=row[1])


@dataclass(frozen=True)
class RunEffectiveOwner:
    """A run's effective owning team, resolved without drifting from team RLS.

    ``owner_team_id`` is the stamped ``Run.owner_team_id`` when present, else
    the owning pipeline's current ``owner_team_id`` (None for an org-level
    pipeline). ``pipeline_present`` is False ONLY for a NULL-stamp run whose
    pipeline cannot be resolved even team-blind (absent or soft-deleted): the
    effective owner is then unknown, so a boundary check MUST deny rather than
    treat the run as org-level.
    """

    owner_team_id: uuid.UUID | None
    pipeline_present: bool


async def run_effective_owner_team_id(session: AsyncSession, run: Run) -> RunEffectiveOwner:
    """Resolve a run's effective owning team, TEAM-BLIND for pre-stamp rows.

    FAR-681 / FAR-1513: ``Run.owner_team_id`` is the source of truth, but
    pre-stamp runs carry NULL and fall back to the pipeline's current owner.
    That fallback MUST be resolved team-blind
    (:func:`pipeline_team_scope_team_blind`): ``pipelines`` carries
    ``rls_team_isolation``, so a caller-facing read returns NOTHING for
    another team's ``visibility='team'`` pipeline, which resolved the owner to
    None and failed OPEN — the exact FAR-1513 boundary bypass. A pipeline that
    is absent (or soft-deleted) even to the team-blind read is reported as
    ``pipeline_present=False`` so callers deny instead of guessing org-level.

    Centralising the owner precedence here gives REST and MCP one authoritative
    effective-owner source; the REST guard consumes it today.
    """
    if run.owner_team_id is not None:
        return RunEffectiveOwner(owner_team_id=run.owner_team_id, pipeline_present=True)
    scope = await pipeline_team_scope_team_blind(session, run.pipeline_id)
    if scope is None:
        return RunEffectiveOwner(owner_team_id=None, pipeline_present=False)
    return RunEffectiveOwner(owner_team_id=scope.owner_team_id, pipeline_present=True)
