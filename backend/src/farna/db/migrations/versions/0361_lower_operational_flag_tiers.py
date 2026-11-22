"""Lower five operational flags to Community (FAR-2665).

FAR-2665 separates entitlement from feature flags. Five flags that used to
declare a Team tier are operational or integration capability, not
organisation-scale governance:

* ``runtime_config``   — runtime configuration overrides
* ``rate_limits``      — API rate limits
* ``plugin_management``— manage plugins, connectors and node categories
* ``error_tracking``   — external error tracking and alerting integrations
* ``error_forwarders`` — dispatch errors to external services via webhooks

A Community install must be able to configure runtime settings and rate limits,
forward errors and manage plugins, so withholding these stranded a self-hoster
on pages they could reach but not use.

Why a migration at all, when the resolver no longer reads a tier from
``feature_flag_catalog``: the catalogue column still feeds the admin Feature
Flags surface and any operator inspection, so a stale ``tier_id='team'`` row
would advertise a capability that is now free. The boot seed now reconciles
``description`` / ``tier_id`` / ``depends_on`` from code (``ON CONFLICT DO
UPDATE``), and this migration makes the same correction for the rows that
change in THIS release, on an install that upgrades before the next boot seed.

Idempotency and safety:

- the ``tier_id = 'team'`` predicate narrows the statement, so re-running it
  matches nothing and is a no-op. On a fresh database the rows do not exist yet
  (the seed runs at application boot, after migrations), so it is likewise a
  no-op rather than an error.
- the statement is dialect-portable: the table is unqualified (no ``public.``
  prefix) and no Postgres-only function is used, so the SQLite target of
  ``env.py`` can run it too.

Downgrade: the exact reverse UPDATE, guarded on ``tier_id = 'community'`` so it
is idempotent in the same way.

Revision ID: 0361_lower_operational_flag_tiers
Revises: 0360_free_schema_history_retire_pipeline_delete
Create Date: 2026-11-22
"""

from alembic import op

revision: str = "0361_lower_operational_flag_tiers"
down_revision: str | None = "0360_free_schema_history_retire_pipeline_delete"
branch_labels = None
depends_on = None

_LOWERED_FLAGS = (
    "runtime_config",
    "rate_limits",
    "plugin_management",
    "error_tracking",
    "error_forwarders",
)


def upgrade() -> None:
    # A fixed literal statement (no query construction); ruff's S608 refuses
    # anything assembled, which is exactly the guarantee a migration needs.
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'community' WHERE name IN ('runtime_config', 'rate_limits', 'plugin_management', 'error_tracking', 'error_forwarders') AND tier_id = 'team'")


def downgrade() -> None:
    op.execute("UPDATE feature_flag_catalog SET tier_id = 'team' WHERE name IN ('runtime_config', 'rate_limits', 'plugin_management', 'error_tracking', 'error_forwarders') AND tier_id = 'community'")
