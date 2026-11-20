"""Soft-delete legacy community-sourced library primitives.

The community library backend was removed in FAR-2587 / PR #3165, but
``library_primitives.source`` still permits ``'community'`` and legacy rows
survive. The frontend no longer labels that source, so a live
``source='community'`` row would render as an unlabelled native library item
with its provenance lost.

This migration soft-deletes every LIVE community-sourced row (it stamps
``deleted_at``). It deliberately does NOT hard-delete the rows (they are
preserved for audit and a later purge) and does NOT rewrite provenance to
``'local'``. The ``deleted_at IS NULL`` predicate keeps the upgrade idempotent:
re-running it soft-deletes nothing further.

The ``ck_library_primitives_source`` / ``ck_library_primitives_source_fields``
CHECK constraints are intentionally left untouched in this change: narrowing
them while the soft-deleted community rows still exist would make those rows
unrepresentable to the constraint. A follow-up hard-delete purge can narrow
them once the rows are gone.

Downgrade: an exact reversal is NOT safely expressible. ``deleted_at`` carries
no provenance identifying which code path stamped it, so a downgrade cannot
distinguish a row this migration soft-deleted from a community row removed by
any other path; clearing ``deleted_at`` for every community row would resurrect
rows removed for other reasons. Nothing is hard-deleted here, so a reverse
rollout leaves the data intact and an operator can restore specific rows
explicitly. The downgrade is therefore a documented no-op.

Revision ID: 0356_soft_delete_community_library_primitives
Revises: 0355_drop_library_sync_state
Create Date: 2026-11-20
"""

from alembic import op

revision = "0356_soft_delete_community_library_primitives"
down_revision = "0355_drop_library_sync_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
UPDATE public.library_primitives
SET deleted_at = now()
WHERE source = 'community'
  AND deleted_at IS NULL
"""
    )


def downgrade() -> None:
    """No-op: an exact reversal cannot be expressed safely (see module docstring)."""
