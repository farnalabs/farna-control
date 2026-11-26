"""Enable RLS on the last two application-owned tenant tables (ADR 070 §2/§3).

Five public tables carry an ``organisation_id`` column without
``relrowsecurity``: the three runtime-managed LangGraph checkpoint tables (left
alone — ``ModuloPostgresSaver`` owns their DDL and scopes its own queries) and
the two this migration closes:

``run_node_outputs_quarantine``
    Ops-only quarantine of oversized / unparseable node outputs (FAR-583,
    migration 0192). Written by that migration, deleted by the retention purge
    (``db.crud.run_retention._delete_quarantine_rows``), read by ops SQL. The
    purge already runs under an org context (``admin_run_retention._run_scoped``)
    and its ``run_id`` batch comes from an org-scoped ``runs`` read, so a plain
    org predicate is exactly the authorisation the table always relied on — it
    was simply not enforced by the database.

``invitations``
    One-time org-enrolment tokens (FAR-461). It holds a **Bearer-equivalent
    secret** (the SHA-256 of a 256-bit token) and every pre-auth accept reads it
    with no authenticated principal, which is precisely why it was skipped by
    the 0351 baseline. That is no longer an acceptable reason to run unprotected:
    the pre-auth read now goes through the explicit, transaction-scoped
    ``app.bootstrap_context`` grant landed by migration 0367, exactly like the
    four pre-auth tables it re-scoped.

Policy shape
------------
Both policies are named ``rls_org_isolation`` (the house name, so the census in
``tests/integration/test_rls_policy_gate.py`` and ``test_rls_isolation.py`` sees
them as ordinary org-isolated tables).

``run_node_outputs_quarantine`` gets the **plain** single-clause org equality::

    organisation_id = (NULLIF(current_setting('app.organisation_id', true), ''))::uuid

``invitations`` gets the org equality **OR** the 0367 scoped bootstrap grant::

    (organisation_id = (NULLIF(current_setting('app.organisation_id', true), ''))::uuid)
    OR (current_setting('app.bootstrap_context', true) = 'true')

Why ``NULLIF`` and not a bare cast: the pool-checkout reset writes
``app.organisation_id = ''``, and ``''::uuid`` RAISES
``invalid input syntax for type uuid``. On ``invitations`` that would break the
bootstrap arm itself (PostgreSQL does not promise to short-circuit the ``OR``:
the faulting cast is evaluated on the very sessions the grant exists for), and
on ``run_node_outputs_quarantine`` it would turn "no context" into a 500 instead
of zero rows. The two-argument + ``NULLIF`` form is the same shape every sibling
org-isolation policy uses (see 0366), so both tables fail CLOSED and silently.

Security invariant this migration must not weaken
-------------------------------------------------
* a session with **no** org context and **no** bootstrap grant reads **zero**
  rows from either table;
* a session with org A's context reads org A's rows and **zero** of org B's;
* the pre-auth accept lookup still resolves its invitation, but only because the
  call site pins the unique ``token_hash`` inside ``db.rls.bootstrap_read``
  (``api.routes.auth.accept_invite`` → ``crud.invitations.get_valid_by_token_hash``)
  — the grant widens the predicate, the exact-key pinning is what keeps the read
  narrow.

Proven against real Postgres as a NON-superuser role by
``tests/integration/test_rls_policy_gate.py`` (shape), and behaviourally —
cross-org reads, the bootstrap accept path, list/revoke, and this migration's
upgrade/downgrade round trip — by
``tests/integration/test_rls_invitations_quarantine.py``.

Downgrade
---------
Restores the previous state exactly: drops both policies and disables RLS on
both tables. Nothing else (grant, owner, index, constraint, FORCE posture) is
touched by either direction.

SQLite (test-only, ADR 068) has no RLS policies at all, so both directions are a
documented no-op there.

Revision ID: 0368_invitations_quarantine_rls
Revises: 0367_scoped_bootstrap_pre_auth_policies
Create Date: 2026-11-25
"""

from __future__ import annotations

import re

from alembic import op

revision: str = "0368_invitations_quarantine_rls"
down_revision: str | None = "0367_scoped_bootstrap_pre_auth_policies"
branch_labels = None
depends_on = None

#: The canonical house org predicate (two-argument ``current_setting`` +
#: ``NULLIF`` empty-string normalisation), byte-identical in shape to every
#: sibling ``rls_org_isolation`` policy.
_ORG_EQUALITY = "(organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"

#: The 0367 scoped pre-auth arm. ``app.bootstrap_context`` is set only by
#: ``db.rls.set_rls_bootstrap_context`` / ``bootstrap_read``, is transaction-local
#: and is cleared at every pool checkout, so an unset or reset value fails closed.
_BOOTSTRAP_GRANT = "(current_setting('app.bootstrap_context'::text, true) = 'true'::text)"

#: ``invitations`` is read pre-auth (accept-by-token) and post-auth (admin list /
#: revoke), so it needs both arms.
_INVITATIONS_PREDICATE = _ORG_EQUALITY + " OR " + _BOOTSTRAP_GRANT

#: ``run_node_outputs_quarantine`` is only ever touched under an org context or by
#: a superuser/DBA doing ops SQL, so it gets no pre-auth widening at all.
_QUARANTINE_PREDICATE = _ORG_EQUALITY

#: table -> predicate, upgrade direction. A tuple (not a dict literal at call
#: time) so the DDL loop below is provably over fixed, reviewed constants.
_POLICIES: tuple[tuple[str, str], ...] = (
    ("invitations", _INVITATIONS_PREDICATE),
    ("run_node_outputs_quarantine", _QUARANTINE_PREDICATE),
)

_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _is_postgres() -> bool:
    """RLS DDL is PostgreSQL-only; SQLite (test-only) has no policies to create."""
    return op.get_bind().dialect.name == "postgresql"


def _enable(table: str, predicate: str) -> None:
    if not _IDENT_RE.fullmatch(table):
        raise ValueError(f"unsafe table identifier in migration: {table!r}")
    op.execute("ALTER TABLE public." + table + " ENABLE ROW LEVEL SECURITY")  # nosec B608
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public." + table)  # nosec B608
    op.execute(
        "CREATE POLICY rls_org_isolation ON public."  # nosec B608
        + table
        + " FOR ALL USING ("
        + predicate
        + ")"
    )


def _disable(table: str) -> None:
    if not _IDENT_RE.fullmatch(table):
        raise ValueError(f"unsafe table identifier in migration: {table!r}")
    op.execute("DROP POLICY IF EXISTS rls_org_isolation ON public." + table)  # nosec B608
    op.execute("ALTER TABLE public." + table + " DISABLE ROW LEVEL SECURITY")  # nosec B608


def upgrade() -> None:
    if not _is_postgres():
        return
    for table, predicate in _POLICIES:
        _enable(table, predicate)


def downgrade() -> None:
    if not _is_postgres():
        return
    for table, _predicate in _POLICIES:
        _disable(table)
