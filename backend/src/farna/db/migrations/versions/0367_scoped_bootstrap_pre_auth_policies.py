"""Replace the four fail-open NULL arms with an explicit bootstrap grant (ADR 070 §2/§3).

Four policies in the 0351 baseline are fail-OPEN:

.. code-block:: sql

    USING ((organisation_id = current_setting('app.organisation_id')) OR
           (current_setting('app.organisation_id') IS NULL))

on ``oauth_clients``, ``oauth_consent_states``, ``org_memberships`` and
``token_families``. The pool-checkout reset writes ``app.organisation_id = ''``
and ``NULLIF('','')`` is NULL, so **any session with no org context reads every
row of these four tables**. They are pre-auth tables, so the need is real — the
mechanism is wrong: "no context" must not mean "everything".

The replacement grants exactly the same rows to exactly the same flows, but
only under an explicit, transaction-scoped GUC:

.. code-block:: sql

    USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR
           (current_setting('app.bootstrap_context'::text, true) = 'true'::text))

``app.bootstrap_context`` is set only by ``db.rls.set_rls_bootstrap_context`` /
``bootstrap_read`` (imported by every pre-auth read of these tables), is
transaction-local (``set_config(..., is_local=true)``), and is cleared at every
pool checkout (it is in ``_RLS_CONFIG_NAMES``). An unset or reset value is ''
or NULL, and neither equals 'true', so the arm fails closed.

Security invariant this migration must not weaken: a session with NO context
reads **zero** rows from all four tables. ``backend/tests/integration/test_rls_policy_gate.py``
enforces the shape structurally and ``test_rls_bootstrap_context.py`` proves the
behaviour on real Postgres.

Only the fail-open arm changes; the policy name, command (``FOR ALL``) and
roles are untouched. ``assistant_context_sources`` / ``assistant_skills`` keep
their ``organisation_id IS NULL`` arm — that is a genuinely global asset
catalogue (ADR 070 §6), named as such.

SQLite (test-only, ADR 068) has no RLS policies at all, so both directions are a
documented no-op there.

Revision ID: 0367_scoped_bootstrap_pre_auth_policies
Revises: 0366_onboarding_progress_rls_setting
Create Date: 2026-11-24
"""

import re

from alembic import op

revision: str = "0367_scoped_bootstrap_pre_auth_policies"
down_revision: str | None = "0366_onboarding_progress_rls_setting"
branch_labels = None
depends_on = None

#: The four pre-auth tables whose fail-open arm is replaced.
_PRE_AUTH_TABLES: tuple[str, ...] = (
    "oauth_clients",
    "oauth_consent_states",
    "org_memberships",
    "token_families",
)

#: The scoped replacement: org equality OR the explicit bootstrap grant. The
#: ``IS NULL`` arm is gone entirely — with no context, ``OR (NULL = 'true')``
#: is NULL, so the row is filtered.
_SCOPED_PREDICATE = "(organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (current_setting('app.bootstrap_context'::text, true) = 'true'::text)"

#: The 0351 text, restored verbatim by the downgrade.
_LEGACY_PREDICATE = "(organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL)"


def _is_postgres() -> bool:
    """RLS DDL is PostgreSQL-only; SQLite (test-only) has no policies to alter."""
    return op.get_bind().dialect.name == "postgresql"


_IDENT_RE = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _replace_policies(predicate: str) -> None:
    for table in _PRE_AUTH_TABLES:
        # Static DDL built by concatenation, never an f-string: the table names
        # and predicate are hardcoded module constants, and the regex guard is
        # defence in depth. Mirrors the guarded-DDL idiom in 0352.
        if not _IDENT_RE.fullmatch(table):
            raise ValueError(f"unsafe table identifier in migration: {table!r}")
        op.execute(
            "DROP POLICY IF EXISTS rls_org_isolation ON public." + table  # nosec B608
        )
        op.execute(
            "CREATE POLICY rls_org_isolation ON public."  # nosec B608
            + table
            + " FOR ALL USING ("
            + predicate
            + ")"
        )


def upgrade() -> None:
    if not _is_postgres():
        return
    _replace_policies(_SCOPED_PREDICATE)


def downgrade() -> None:
    if not _is_postgres():
        return
    _replace_policies(_LEGACY_PREDICATE)
