"""Drop library_sync_state table

Remove the community library sync state table as part of removing the
community library backend feature.

Downgrade restores the table exactly as the ``0350_baseline_schema`` /
``0352_baseline_privileges`` baseline created it (columns, types, defaults,
NOT NULL constraints, primary key, and the role-guarded grants). Row data is
not restored: the table held a re-derivable sync cache, so a rolled-back
deployment starts with an empty table and repopulates on its next sync.

Revision ID: 0355_drop_library_sync_state
Revises: 0354_policy_gate_reason
Create Date: 2026-11-15

"""

from alembic import op

revision = "0355_drop_library_sync_state"
down_revision = "0354_policy_gate_reason"
branch_labels = None
depends_on = None

# Role-guarded grant, same shape as 0352_baseline_privileges. __ROLE__ is
# substituted with a fixed, code-owned role name (never external input).
_GRANT_IF_ROLE_EXISTS = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '__ROLE__') THEN
        EXECUTE 'GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.library_sync_state TO __ROLE__';
    END IF;
END $$;
"""


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.library_sync_state")


def downgrade() -> None:
    # Mirrors the original baseline DDL (0350_baseline_schema) verbatim.
    op.execute(
        """
CREATE TABLE public.library_sync_state (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    last_synced_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_success_at timestamp with time zone,
    last_error character varying(2000),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    manifest_json jsonb NOT NULL,
    catalog_json jsonb NOT NULL
)
"""
    )
    op.execute("ALTER TABLE ONLY public.library_sync_state ADD CONSTRAINT library_sync_state_pkey PRIMARY KEY (id)")
    # Mirrors the original role-guarded grants (0352_baseline_privileges).
    op.execute(_GRANT_IF_ROLE_EXISTS.replace("__ROLE__", "farna_control_app"))
    op.execute(_GRANT_IF_ROLE_EXISTS.replace("__ROLE__", "farna_control_system"))
