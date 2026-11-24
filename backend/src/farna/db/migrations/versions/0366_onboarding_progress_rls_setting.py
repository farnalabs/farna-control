"""Make ``onboarding_progress``' RLS policy fail CLOSED on an unset org context.

The baseline (``0351_baseline_rls``) created this table's policy as::

    CREATE POLICY rls_org_isolation ON public.onboarding_progress
        USING (organisation_id = (current_setting('app.organisation_id'::text))::uuid);

Every sibling org-isolation policy uses the two-argument form::

    USING (organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)

The difference is not cosmetic. ``current_setting(name)`` RAISES
``unrecognized configuration parameter`` when the GUC was never set, while
``current_setting(name, missing_ok => true)`` returns NULL - and
``NULLIF(NULL, '')`` stays NULL, so the policy evaluates to NULL and the row is
filtered. The pool-checkout reset writes the empty string, which the
``NULLIF(..., '')`` arm also normalises to NULL, so BOTH no-context shapes (absent
GUC, empty GUC) return zero rows instead of one raising and the other matching
nothing.

Effect: a session that queries ``onboarding_progress`` without an org context now
gets zero rows (fail closed) instead of a PostgreSQL error that escapes as a 500
from whatever read path did not set the context.

Reversibility: ``downgrade`` restores the exact baseline policy text, so the
policy round-trips. Nothing else changes - no column, index, grant or FORCE-RLS
posture is touched.

Revision ID: 0366_onboarding_progress_rls_setting
Revises: 0365_runs_family_owner_team
Create Date: 2026-11-24
"""

from __future__ import annotations

from alembic import op

revision: str = "0366_onboarding_progress_rls_setting"
down_revision: str | None = "0365_runs_family_owner_team"
branch_labels = None
depends_on = None

TABLE = "onboarding_progress"
POLICY = "rls_org_isolation"

#: The fixed policy (two-argument ``current_setting`` + ``NULLIF`` empty-string
#: normalisation), byte-identical in shape to every sibling org-isolation policy.
POLICY_SQL = "CREATE POLICY rls_org_isolation ON public.onboarding_progress USING ((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid));"

#: The baseline (pre-0366) text - single-argument ``current_setting``, which raises
#: on an unset context. Restored verbatim by the downgrade.
LEGACY_POLICY_SQL = "CREATE POLICY rls_org_isolation ON public.onboarding_progress USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));"

DROP_POLICY_SQL = "DROP POLICY IF EXISTS rls_org_isolation ON public.onboarding_progress"


def upgrade() -> None:
    op.execute(DROP_POLICY_SQL)
    op.execute(POLICY_SQL)


def downgrade() -> None:
    op.execute(DROP_POLICY_SQL)
    op.execute(LEGACY_POLICY_SQL)
