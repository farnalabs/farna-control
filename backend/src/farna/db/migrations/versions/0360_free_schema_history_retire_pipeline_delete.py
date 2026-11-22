"""Free ``schema_version_history`` and retire ``pipeline_delete`` (FAR-2663).

Two changes in ``core.feature_flags._KNOWN_FLAGS`` need to reach existing
installs, because the boot seed (``_seed_tier_catalog``) inserts catalog rows
with ``ON CONFLICT (name) DO NOTHING`` and ``FeatureFlagRegistry.load_from_db``
then replaces the code registry with the DB rows — so a fresh install picks up
the new declaration and an upgraded one keeps the stale row:

1. ``schema_version_history`` was ``team``. It is now ``community``: schema
   versioning is provenance, and gating it withheld the write path from every
   Community author.
2. ``pipeline_delete`` was retired entirely. It never gated anything in the
   backend (``DELETE /api/v1/pipelines/{id}`` is guarded by the Community RBAC
   permission ``pipeline.delete``); the stale row is deleted so the admin
   Feature Flags surface cannot advertise a flag that gates nothing. This
   mirrors the FAR-1819 retirement of ``pipeline_diff_rollback``.

Idempotency and safety:

- The ``schema_version_history`` UPDATE is narrowed by ``tier_id = 'team'``, so
  re-running it matches nothing and is a no-op. On a fresh database the row does
  not exist yet (the seed runs at application boot, after migrations), so it is
  likewise a no-op rather than an error.
- The ``pipeline_delete`` DELETE is a no-op when the row is already gone.
- Both statements are dialect-portable: the table is unqualified (no
  ``public.`` prefix) and no Postgres-only function is used, so the SQLite
  target of ``env.py`` can run them too.

Downgrade: the exact reverse — the tier UPDATE is guarded on
``tier_id = 'community'`` and the row is re-inserted with its historical
description, tier and ``is_active=true``, guarded by ``NOT EXISTS`` so a repeat
is a no-op.

Revision ID: 0360_free_schema_history_retire_pipeline_delete
Revises: 0359_registry_sourced_drop_index
Create Date: 2026-11-22
"""

from alembic import op

revision: str = "0360_free_schema_history_retire_pipeline_delete"
down_revision: str | None = "0359_registry_sourced_drop_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'community' WHERE name = 'schema_version_history' AND tier_id = 'team'")
    op.execute("DELETE FROM feature_flag_catalog WHERE name = 'pipeline_delete'")


def downgrade() -> None:
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'team' WHERE name = 'schema_version_history' AND tier_id = 'community'")
    op.execute("INSERT INTO feature_flag_catalog (name, description, tier_id, is_active) SELECT 'pipeline_delete', 'Allow hard-deleting pipelines from the UI', 'team', TRUE WHERE NOT EXISTS (SELECT 1 FROM feature_flag_catalog WHERE name = 'pipeline_delete')")
