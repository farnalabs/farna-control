"""Team-scope clamp shared by every per-run REST read (ADR 070 §4, Phase 0).

``runs`` carries only the org-isolation RLS policy: there is no ``visibility``
column, and the stamped ``owner_team_id`` is metadata the database does not
enforce. A run's team boundary is therefore application-layer, and the list
surface clamps a viewer to their team memberships in SQL
(``db.crud.run._runs_list_scope_filters``). Every per-run read applies the SAME
two clauses here, in one place, so a new sibling cannot silently reintroduce a
cross-team read:

1. **team-scoped credential boundary** — a credential bound to a team
   (``AuthenticatedPrincipal.team_id``: a team-scoped ``mk_`` API key or a
   token minted by a team-bound OAuth client) may not touch a run owned by
   another team. Org-wide credentials and browser JWTs carry ``team_id=None``
   and are unaffected. An org-level run (no owning team) stays visible, matching
   ``team_scope_clause``.
2. **viewer membership** — a viewer is bounded by their own team memberships
   against the run's effective owner, matching ``account_team_scope_clause``.
   Org-level runs stay visible.

The run's effective owner is ``coalesce(Run.owner_team_id,
Pipeline.owner_team_id)``: the stamped owner is authoritative, and a
pre-stamp row (NULL) falls back to the pipeline's current owner so a NULL
stamp can never widen the boundary (ADR 070 §4 — NULL owner never means
"org-visible" for the runs family).

An organisation admin is exempt: they see every run in their organisation.
Callers render a failed clause as **404**, never 403, so a run's existence is
not confirmed to a principal outside its boundary.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from farna.auth.jwt import AuthenticatedPrincipal
from farna.db.crud import team_scope
from farna.db.crud.run import get_run
from farna.db.crud.team_scope import team_blind_org_scope, viewer_run_scope_visible

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from farna.db.models.run import Run

#: ``(session, run_id, *, organisation_id)`` → the org-scoped run or ``None``.
RunFetcher = Callable[..., Awaitable["Run | None"]]

#: The run's effective owner could not be resolved (pipeline absent/hidden).
#: Distinct from ``None``, which means a genuinely org-level run: an unresolved
#: owner must fail CLOSED, never be treated as org-level (FAR-1513).
OWNER_UNRESOLVED: Any = object()


def team_boundary_blocked(
    credential_team_id: uuid.UUID | None,
    owner_team_id: uuid.UUID | None,
) -> bool:
    """True when a team-bound credential must not access a resource.

    The single predicate behind both surfaces' team boundary: the REST clamp
    below and the MCP guard (``mcp_server._team_scoped_key_mismatch``, whose
    credential team is a request contextvar rather than a principal field).
    An unbound credential (``None``) is never blocked, and a resource with no
    owning team is visible to every team-bound credential.
    """
    if credential_team_id is None:
        return False
    return owner_team_id is not None and owner_team_id != credential_team_id


async def effective_run_owner_team_id(session: AsyncSession, run: Run) -> uuid.UUID | None:
    """Resolve the run's effective owning team (``None`` for an org-level run).

    The stamped ``Run.owner_team_id`` is the source of truth; a pre-stamp row
    (NULL) falls back to the pipeline's current owner — the same precedence the
    list filters and the MCP run gates use.

    The fallback read is TEAM-BLIND (``pipeline_team_scope_team_blind``). A
    caller-facing read of ``pipelines`` is filtered by ``rls_team_isolation``,
    which without a user context hides a team-private pipeline — so a plain
    read would answer ``None`` and this decision would treat a team-private run
    as org-level. The inputs to a boundary decision must not be filtered by the
    boundary (the FAR-1513 fail-open shape).
    """
    if run.owner_team_id is not None:
        return run.owner_team_id
    # Resolve through the defining module at call time, not a bound name: the
    # boundary resolver is patched on farna.db.crud.team_scope in tests, and a
    # module-level import would make that patch silently non-intercepting.
    scope = await team_scope.pipeline_team_scope_team_blind(session, run.pipeline_id)
    return OWNER_UNRESOLVED if scope is None else scope.owner_team_id


async def run_visible_to_principal(
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    run: Run,
) -> bool:
    """True when *principal* may read *run* under the two ADR 070 §4 clauses.

    Read-only: it resolves the run's effective owner and, for a viewer only, the
    account's team memberships. The membership read runs TEAM-BLIND inside the
    org (the membership predicate is applied explicitly by
    ``viewer_run_scope_visible``), so a viewer's own RLS context can never hide
    the pipeline row whose ownership decides their access. The only session
    writes are that helper's transaction-local GUC save/restore. It never raises
    for a boundary failure — callers own the 404 rendering.
    """
    if principal.org_role == "admin":
        return True
    owner_team_id = await effective_run_owner_team_id(session, run)
    # An owner that cannot be resolved (pipeline absent or hidden) is unknown,
    # not org-level: fail closed so a pre-stamp run can never leak by omission.
    if owner_team_id is OWNER_UNRESOLVED:
        return False
    # A principal shape without ``team_id`` is an unbound credential: the field
    # exists on every production principal, and reading it defensively keeps the
    # guard usable from the lightweight principal stand-ins in tests.
    if team_boundary_blocked(getattr(principal, "team_id", None), owner_team_id):
        return False
    if principal.org_role == "viewer":
        if owner_team_id is None:
            return True
        if principal.organisation_id is None:
            return await viewer_run_scope_visible(session, run.id, principal.account_id)
        async with team_blind_org_scope(session, principal.organisation_id):
            return await viewer_run_scope_visible(session, run.id, principal.account_id)
    return True


async def load_run_in_scope(
    session: AsyncSession,
    principal: AuthenticatedPrincipal,
    run_id: uuid.UUID,
    *,
    fetch: RunFetcher = get_run,
) -> Run | None:
    """Load an org-scoped run, returning ``None`` when the principal may not see it.

    The one loader the per-run read siblings share: a missing run and a run
    outside the principal's team boundary are indistinguishable (both
    ``None``), so every caller's existing "run is None → 404" path is the
    refusal. Callers must already have set the RLS org context on *session*.

    *fetch* is the run read (``get_run`` by default). It exists so a router
    that exposes ``get_run`` as its own patchable module seam can pass that
    binding through instead of reimplementing the two-clause composition.
    """
    run = await fetch(session, run_id, organisation_id=principal.organisation_id)
    if run is None:
        return None
    if not await run_visible_to_principal(session, principal, run):
        return None
    return run
