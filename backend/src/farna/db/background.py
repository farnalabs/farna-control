"""The ONE way for background code to obtain a tenant-scoped session (ADR 070 §3).

Background machinery (the SAQ worker's job bodies, the executor's bookkeeping
writes, the shutdown handback, the notifier) reads and writes team-scoped tables
with no end-user identity: there is no single user context for a run.  Before
this module each of those paths obtained a session ad hoc — a fresh
``async_sessionmaker`` per call — and hand-set ``app.organisation_id`` (and, in
some paths, forgot ``app.execution_context`` entirely).  That made the grant a
per-call-site convention rather than a property of "background code", and left
silent under-reads waiting to happen: once the runs family carries the team
predicate (ADR 070 Phase 1B) an org-only background read returns ZERO rows, and
a job that sees nothing reports success.

``background_session`` is the single acquisition point.  It:

* opens ONE transaction on the caller's session factory (or engine, or the
  process engine), exactly like the hand-written
  ``async with factory() as session, session.begin():`` it replaces — commit on
  clean exit, rollback on exception, the same boundaries and the same errors;
* applies the background grant — org, then execution context — inside that
  transaction, so ``rls_team_isolation``'s ``app.execution_context`` arm lets
  the job read its team-scoped rows org-wide;
* and never touches connection state.

Why it is transaction-scoped and not connection-level (ADR 070 §3, rejected
alternatives): ``register_rls_reset_hook`` clears ``app.execution_context`` (it
is in ``db.rls._RLS_CONFIG_NAMES``) at every pool checkout.  A "reset that
preserves it" would either lose to that hook or defeat it — and a leaked grant
becomes a TEAM-BLIND WEB READ on whichever request next borrows the connection.
It would also need a second engine, reintroducing the pool-knob drift
``db/session.py`` was consolidated to remove.  ``set_config(..., is_local=true)``
dies at COMMIT or ROLLBACK by construction instead, which is the whole point.

The execution-context grant is restricted to an enumerated allowlist of module
classes (ADR 070 §3): this factory is the first entry.  Webhook/Slack ingress,
admin widening and the team-blind gate reads keep their explicit grant call
sites — they are not routed through here.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from farna.db.rls import set_rls_execution_context, set_rls_org

__all__ = ["apply_background_grant", "background_session"]


async def apply_background_grant(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Apply the background RLS grant to an ALREADY-OPEN transaction.

    Org first, then the execution-context arm — the order the hand-rolled
    ``_set_rls_org`` copies used.  Both are transaction-scoped; call this only
    inside an active transaction (``set_rls_*`` raises otherwise).

    Exposed for the few helpers that receive an open session rather than
    acquiring one (``cron_helpers._set_rls_org``, ``trigger_engine.polling``,
    ``reports.scheduler``): they delegate here so there is exactly ONE
    implementation of "what a background grant is".
    """
    await set_rls_org(session, org_id)
    await set_rls_execution_context(session)


@asynccontextmanager
async def background_session(
    org_id: uuid.UUID | str | None,
    *,
    execution_context: bool = True,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    engine: AsyncEngine | None = None,
) -> AsyncIterator[AsyncSession]:
    """Open a tenant-scoped transaction for background code and yield its session.

    ``org_id`` is required and must be a real organisation: this factory never
    widens past ``app.organisation_id`` (ADR 070 §3), so an org-less background
    transaction is refused rather than silently run cross-org.  Cross-org system
    crons use the separate ``farna_control_system`` (BYPASSRLS) engine and do
    not come through here.

    Session acquisition, in precedence order:

    * ``session_factory`` — the caller's own factory (e.g. ``Notifier``'s
      ``self._session_factory``).  Used as-is, so a caller that already holds a
      factory keeps its exact pool/config and its tests keep their doubles.
    * ``engine`` — the caller's engine (the SAQ worker's shared engine, a
      request-scoped engine, a test engine).  A factory is built over it exactly
      as the call sites used to, with ``expire_on_commit=False`` and
      ``autobegin=False``.  The ENGINE is reused; only the sessionmaker is new,
      so this never introduces a second connection pool.
    * neither — the process engine via ``farna.db.session.AsyncSessionLocal``
      (the same shared engine the web app, the worker and the crons use).

    Transaction semantics match the hand-written blocks this replaces:

    * ``session.begin()`` is entered explicitly, so a factory built with
      ``autobegin=False`` still opens its transaction (and a factory with the
      default ``autobegin`` is unaffected);
    * COMMIT on a clean exit from the ``async with`` body, ROLLBACK if the body
      raises (the exception still propagates to the caller's handler);
    * a value ``return``-ed from inside the body still commits — returning from
      an ``async with`` runs ``__aexit__``.

    ``execution_context=False`` applies the org scope only.  It exists for a
    caller that must stay team-filtered on purpose; the default is the grant,
    because that is what "background code" means.
    """
    if org_id is None:
        msg = "background_session requires an organisation_id — a background session must never run org-less (ADR 070 §3)"
        raise ValueError(msg)
    org_uuid = org_id if isinstance(org_id, uuid.UUID) else uuid.UUID(str(org_id))

    factory = session_factory if session_factory is not None else _factory_for(engine)

    async with factory() as session, session.begin():
        if execution_context:
            await apply_background_grant(session, org_uuid)
        else:
            await set_rls_org(session, org_uuid)
        yield session


def _factory_for(engine: AsyncEngine | None) -> async_sessionmaker[AsyncSession]:
    """Build (or borrow) the session factory for one ``background_session``.

    A per-call ``async_sessionmaker`` over the caller's engine is exactly what
    every converted call site did inline; it costs nothing and keeps this
    module free of any engine state of its own.
    """
    if engine is not None:
        return async_sessionmaker(engine, expire_on_commit=False, autobegin=False)

    # Imported lazily: ``farna.db.session`` builds the process engine at import
    # time and registers the global listeners (one of which is the RLS
    # tripwire); importing it from module scope here would make that side
    # effect happen whenever anything imports this module.
    from farna.db.session import AsyncSessionLocal

    return AsyncSessionLocal
