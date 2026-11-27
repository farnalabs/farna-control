"""FAR-2837 - bind a HITL approval to the code revision it was given for.

Adds ``hitl_claims.revision`` (nullable ``String(64)``), the code revision a
gate was raised for and its approval is bound to. Before this column there was
nowhere to record WHAT a human approved, so a decision given for one revision
was honoured for the run's later state and an approved gate was never
invalidated by a new commit.

Revision identity
-----------------
The value is the run's ``input_hash`` (a SHA-256 hex of the run's work-item
payload), which is exactly the identity gate coalescing already treats as the
"entity SHA" (``gate_coalescing.evaluate_gate_coalescing``: unchanged hash ->
reuse, changed hash -> supersede). It is content-addressed, always present at
gate-raise, and the closest reliable revision the pipeline can produce --
pipelines are connector-agnostic, so an external git HEAD SHA is not reliably
available at gate raise. The column is stamped by ``HITLManager.create_gate``
and re-validated on resume; a decision whose revision no longer matches the
run's current ``input_hash`` is treated as stale and the run requires a fresh
human decision.

Cost / safety
-------------
Nullable with no server default, so ``ADD COLUMN`` is metadata-only on Postgres
-- no table rewrite. Existing rows (gates that fired before this migration) keep
NULL, which the resume check treats as "cannot validate" and skips, so an
in-flight gate is never retro-invalidated by the deploy.

Downgrade
---------
Drops the column, restoring the exact pre-0370 shape. Pre-release migration
chain (no customer data migration); this is DATA LOSS for any revision stamped
or approved after the migration.

Revision ID: 0370_hitl_claims_revision
Revises: 0369_pipeline_family_owner_team
Create Date: 2026-11-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0370_hitl_claims_revision"
down_revision: str | None = "0369_pipeline_family_owner_team"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, no server default -> metadata-only ADD COLUMN (no table rewrite).
    op.add_column("hitl_claims", sa.Column("revision", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("hitl_claims", "revision")
