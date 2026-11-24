"""Non-production tripwire: a TEAM-scoped statement with no identity or grant.

ADR 070 §3 makes the background grant a transaction-scoped property of the
session factory, and Phase 1B will give the runs family a team predicate.  The
silent failure that predicate makes possible is a read that returns ZERO rows:
an org-only session (no ``app.user_id``, no ``app.execution_context``) selecting
a team-scoped table sees only org-visible rows, so a background job can quietly
observe "nothing to do" and report success.

This module logs that shape LOUDLY while it is still only latent.  It is a
tripwire, not a gate:

* **off in production** — ``FARNA_CONTROL_ENV=production`` (or ``prod``) skips
  registration entirely, so a real deployment never pays for it and never emits
  it into a log an operator might treat as an outage;
* **off on non-Postgres** — the RLS GUCs do not exist on the SQLite test
  backend, so the signal would be meaningless noise there;
* **log only** — it never raises, never alters a statement, and never delays
  one.  A tripwire that could break a query would be worse than the defect.

Why a ``do_orm_execute`` listener: that is the only hook that sees every ORM
statement AND the session it ran on.  Raw ``text()`` statements bypass it
entirely (they also bypass the SQLite tenant filter), so a caller that builds
its own SQL is out of scope by construction — the tripwire is aimed at the ORM
read paths, which is where the Phase 1B under-reads live.

Evidence source: ``session.info``.  ``set_rls_user_context`` and
``set_rls_execution_context`` record their grants there on EVERY dialect
(in-process bookkeeping only — never an authorisation input), because a sync
listener cannot read the server-side GUCs.  That marker is exactly as good as
"this session object was told it has a grant", which is what a call site bug
looks like.
"""

import logging
from collections.abc import Sequence

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState
from sqlalchemy.orm import Session as SASession

from farna.settings import Settings, get_settings

__all__ = [
    "TEAM_SCOPED_TABLES",
    "register_rls_context_tripwire",
    "tripwire_enabled",
]

_log = logging.getLogger(__name__)

#: Tables whose policy is (or becomes, in ADR 070 Phase 1B) TEAM-scoped: an
#: org-only session reads them team-blind.  The eleven are the declared
#: ``rls_team_isolation`` set in ``0351_baseline_rls.py``; the runs family is the
#: Phase 1B flip (ADR 070 §4 — ``coalesce(Run.owner_team_id,
#: Pipeline.owner_team_id)``) and is included here so the tripwire warns about
#: the readers that flip will break BEFORE the flip lands.
TEAM_SCOPED_TABLES: frozenset[str] = frozenset(
    {
        # --- declared `rls_team_isolation` today (0351_baseline_rls.py) ---
        "connector_instances",
        "environment_profiles",
        "eval_datasets",
        "eval_suites",
        "external_agents",
        "library_primitives",
        "lifecycle_maps",
        "mcp_servers",
        "model_backends",
        "pipelines",
        "system_maps",
        # --- the runs family ADR 070 Phase 1B flips to the team predicate ---
        "runs",
        "run_attempts",
        "run_evidence",
        "run_node_outputs",
    }
)

#: Environments in which the tripwire stays registered.  A deny list, not an
#: allow list: a self-hosted deployment that leaves ``environment`` at its
#: default still gets the warning, which is where it is most useful.
_PRODUCTION_ENVIRONMENTS: frozenset[str] = frozenset({"production", "prod"})

#: Distinct table-sets already reported.  One line per shape per process: a
#: poll loop that trips on every tick must not turn into a log flood, which
#: would bury the first (informative) occurrence.
_REPORTED: set[tuple[str, ...]] = set()


def tripwire_enabled(settings: Settings) -> bool:
    """Whether the tripwire should register for this process.

    Postgres only (the RLS backend) and never in production (ADR 070 §3 keeps
    production free of developer diagnostics).  ``settings.db`` is the same
    signal ``db.session._build_engine`` uses to pick the engine knobs.
    """
    if settings.db.lower() != "postgres":
        return False
    return settings.environment.strip().lower() not in _PRODUCTION_ENVIRONMENTS


def register_rls_context_tripwire() -> None:
    """Register the tripwire listener once per process, if this process wants it.

    Called from ``db.session._register_global_hooks_once`` at import time (an
    import-time guard on settings), so every process that uses the DB layer —
    web, worker, crons — registers it, and production registers nothing.
    """
    settings = get_settings()
    if not tripwire_enabled(settings):
        _log.debug(
            "rls_context_tripwire.disabled environment=%s db=%s",
            settings.environment,
            settings.db,
        )
        return
    if event.contains(SASession, "do_orm_execute", _rls_context_tripwire):
        return
    event.listen(SASession, "do_orm_execute", _rls_context_tripwire)
    _log.info(
        "rls_context_tripwire.enabled environment=%s — ORM statements against team-scoped tables with no user identity or execution-context grant will be logged at ERROR",
        settings.environment,
    )


def _target_tables(execute_state: ORMExecuteState) -> set[str]:
    """The table names an ORM statement targets (SELECT/UPDATE/DELETE)."""
    names: set[str] = set()

    for mapper in getattr(execute_state, "all_mapper_classes", None) or ():
        table = getattr(mapper.class_, "__table__", None)
        if table is not None:
            names.add(str(table.name))

    statement = execute_state.statement
    for description in getattr(statement, "column_descriptions", None) or ():
        entity = description.get("entity")
        table = getattr(entity, "__table__", None)
        if table is not None:
            names.add(str(table.name))

    return names


def _has_identity_or_grant(session_info: object) -> bool:
    """Whether this session was told it carries a user identity or a grant.

    ``session.info`` may not be a dict on a lightweight session double; treat an
    unreadable container as "has a grant" (stay quiet) — a tripwire must never
    fire on a shape it cannot inspect.
    """
    if not isinstance(session_info, dict):
        return True
    if session_info.get("user_id") is not None:
        return True
    return bool(session_info.get("execution_context"))


def _bind_dialect(execute_state: ORMExecuteState) -> str | None:
    """The executing session's dialect name, or ``None`` when unreadable.

    Read from the session's BIND, not from settings: a test process configured
    for Postgres but running on the SQLite backend must not trip (the RLS GUCs
    it warns about do not exist there).  ``None`` means "cannot tell" and the
    caller stays quiet.
    """
    try:
        bind = execute_state.session.get_bind()
        return str(bind.dialect.name)
    except Exception:
        return None


def _describe_targets(tables: Sequence[str]) -> str:
    return ", ".join(tables)


def _rls_context_tripwire(execute_state: ORMExecuteState) -> None:
    """``do_orm_execute`` listener: log a team-scoped read with no RLS identity."""
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return

    hit = _target_tables(execute_state) & TEAM_SCOPED_TABLES
    if not hit:
        return
    if _bind_dialect(execute_state) != "postgresql":
        return
    if _has_identity_or_grant(getattr(execute_state.session, "info", None)):
        return

    key = tuple(sorted(hit))
    if key in _REPORTED:
        return
    _REPORTED.add(key)

    _log.error(
        "rls_context_tripwire.team_table_without_context tables=[%s] — this session has neither app.user_id nor app.execution_context; it reads team-scoped rows TEAM-BLIND (zero rows for other teams' data). Use farna.db.background.background_session for background code: %s",
        _describe_targets(key),
        execute_state.statement,
    )
