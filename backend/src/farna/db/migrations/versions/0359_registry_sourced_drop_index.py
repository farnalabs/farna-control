"""Rename collection_install.community_sourced and drop a dead index

``collection_install.community_sourced`` gates a collection's agents until an
operator grants them access. With the community library gone the only source
that trips the gate is the signed registry, so the column is renamed to
``registry_sourced`` (values are preserved; existing gated installs stay gated).

``ix_library_primitives_visibility_contribution`` was built for the cross-org
community catalogue listing, which no longer exists; no query uses it.

Revision ID: 0359_registry_sourced_drop_index
Revises: 0358_community_flag_tiers
Create Date: 2026-11-20
"""

from alembic import op

revision = "0359_registry_sourced_drop_index"
down_revision = "0358_community_flag_tiers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("collection_install", "community_sourced", new_column_name="registry_sourced")
    op.execute("DROP INDEX IF EXISTS ix_library_primitives_visibility_contribution")


def downgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_library_primitives_visibility_contribution ON library_primitives (visibility, contribution_status) WHERE deleted_at IS NULL")
    op.alter_column("collection_install", "registry_sourced", new_column_name="community_sourced")
