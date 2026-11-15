"""Drop library_sync_state table

Remove the community library sync state table as part of removing the
community library backend feature.

Revision ID: 0355_drop_library_sync_state
Revises: 0354_policy_gate_reason
Create Date: 2026-11-15

"""

import sqlalchemy as sa
from alembic import op

revision = "0355_drop_library_sync_state"
down_revision = "0354_policy_gate_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the library_sync_state table created in migration 0122
    op.drop_table("library_sync_state", if_exists=True)


def downgrade() -> None:
    # Recreate the library_sync_state table (minimal schema for rollback)
    op.create_table(
        "library_sync_state",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("organisation_id", sa.UUID(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("entries_count", sa.Integer(), nullable=False, default=0),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
