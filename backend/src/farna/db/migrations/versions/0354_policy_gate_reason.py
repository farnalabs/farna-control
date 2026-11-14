"""Add operator reason to policy gates."""

import sqlalchemy as sa
from alembic import op

revision: str = "0354_policy_gate_reason"
down_revision: str | None = "0353_baseline_seed_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("policy_gates", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("policy_gates", "reason")
