"""Move the email and environment-profile flag tiers to community.

FAR-2604 moved ``email_config`` and ``environment_profiles`` from the ``team``
tier to the ``community`` tier in ``core.feature_flags._KNOWN_FLAGS``. That code
change alone is inert on any existing install: the boot seed
(``_seed_tier_catalog``) inserts catalog rows with ``ON CONFLICT (name) DO
NOTHING``, and ``FeatureFlagRegistry.load_from_db`` replaces the code registry
with the DB rows, so ``currently_active`` keeps reading the stale
``tier_id='team'`` already stored. Only a fresh install would pick up the new
tier.

This migration rewrites those two existing catalog rows once, explicitly, so an
upgraded install gets what the ticket promises. It touches ``feature_flag_catalog``
only, and only the two named flags.

Idempotency and safety:

- The ``tier_id = 'team'`` predicate narrows the statement, so re-running it
  matches nothing and is a no-op. A fresh database whose catalog rows do not
  exist yet (the seed runs at application boot, after migrations) is likewise a
  no-op rather than an error.
- The statement is dialect-portable: the table is unqualified (no ``public.``
  prefix) and it uses no Postgres-only function, so the SQLite target of
  ``env.py`` can run it too.

Downgrade: the exact reverse UPDATE, guarded on ``tier_id = 'community'`` so it
is idempotent in the same way.

Revision ID: 0358_community_flag_tiers
Revises: 0357_rebrand_library_seed_copy
Create Date: 2026-11-20
"""

from alembic import op

revision: str = "0358_community_flag_tiers"
down_revision: str | None = "0357_rebrand_library_seed_copy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'community' WHERE name IN ('email_config', 'environment_profiles') AND tier_id = 'team'")


def downgrade() -> None:
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'team' WHERE name IN ('email_config', 'environment_profiles') AND tier_id = 'community'")
