"""Rebrand the remaining legacy database object names.

The product is Farna Control. Two database objects still carried the retired
name ``modulo``. This migration renames them so a fresh install only ever
materialises the new names and an upgraded install converges on the same
shape.

Renamed database objects:

* schema ``modulo_internal`` -> ``farna_internal`` (an empty internal schema;
  no object is created in it, so the rename is the whole change).
* table ``modulo_journey_facts`` -> ``farna_journey_facts``, plus its primary
  key, unique constraint, foreign key and the two indexes that carried the old
  table prefix.

The compliance pack mapping key ``modulo_control`` -> ``farna_control`` is a
shipped YAML/model key, not a database object. It changes in the same delivery
but not in this migration.

Renames are metadata-only (``ALTER ... RENAME``): row data, table OIDs, grants,
RLS policies, index definitions and constraint definitions are all preserved.
Nothing here rewrites a table or changes a column type.

Idempotency: every rename is guarded on the old name existing and the new name
not existing, so a re-run, or a database that already reached the new shape,
is a no-op. A fresh install runs the baseline (old names) and then this
migration, so it lands on the new names too.

Deliberately out of scope: role names. The runtime roles are already
``farna_control_app`` / ``farna_control_system`` / ``farna_control`` (single
definition in ``farna.db.url_utils``), and the launcher adopts a legacy
``modulo`` bundle-owner role by renaming it. The provider-created login role
(``staging_modulo`` on staging and its production equivalent) is an external
credential, not a schema object this chain can safely rename; removing it is an
operator follow-up recorded with the delivery.

Downgrade: the exact reverse renames, under the same existence guards.

Revision ID: 0362_rename_legacy_db_objects
Revises: 0360_free_schema_history_retire_pipeline_delete
Create Date: 2026-11-22
"""

from alembic import op

revision: str = "0362_rename_legacy_db_objects"
down_revision: str | None = "0360_free_schema_history_retire_pipeline_delete"
branch_labels = None
depends_on = None

#: Old and new names are fixed, code-owned constants. They are substituted into
#: the static templates below (never into an f-string), so no value reaches DDL
#: without being one of these literals.
_OLD_SCHEMA = "modulo_internal"
_NEW_SCHEMA = "farna_internal"
_OLD_TABLE = "modulo_journey_facts"
_NEW_TABLE = "farna_journey_facts"

_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("modulo_journey_facts_pkey", "farna_journey_facts_pkey"),
    ("uq_modulo_journey_facts_run_writer", "uq_farna_journey_facts_run_writer"),
    ("modulo_journey_facts_organisation_id_fkey", "farna_journey_facts_organisation_id_fkey"),
)

_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_modulo_journey_facts_org_created", "ix_farna_journey_facts_org_created"),
    ("ix_modulo_journey_facts_organisation_id", "ix_farna_journey_facts_organisation_id"),
)

_RENAME_SCHEMA_SQL = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '__OLD__')
       AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '__NEW__') THEN
        ALTER SCHEMA "__OLD__" RENAME TO "__NEW__";
    END IF;
END $$;
"""

_RENAME_TABLE_SQL = """
DO $$
BEGIN
    IF to_regclass('public.__OLD__') IS NOT NULL
       AND to_regclass('public.__NEW__') IS NULL THEN
        ALTER TABLE public."__OLD__" RENAME TO "__NEW__";
    END IF;
END $$;
"""

# Constraint renames are executed against the table that actually owns the
# constraint, resolved at run time, so the guard holds even if the table rename
# was already applied by an earlier attempt.
_RENAME_CONSTRAINT_SQL = """
DO $$
DECLARE
    owner_table regclass;
BEGIN
    SELECT conrelid::regclass INTO owner_table
    FROM pg_constraint
    WHERE conname = '__OLD__'
      AND connamespace = 'public'::regnamespace;
    IF owner_table IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname = '__NEW__'
             AND connamespace = 'public'::regnamespace
       ) THEN
        EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I', owner_table, '__OLD__', '__NEW__');
    END IF;
END $$;
"""

_RENAME_INDEX_SQL = """
DO $$
BEGIN
    IF to_regclass('public.__OLD__') IS NOT NULL
       AND to_regclass('public.__NEW__') IS NULL THEN
        ALTER INDEX public."__OLD__" RENAME TO "__NEW__";
    END IF;
END $$;
"""


def _rendered(template: str, old: str, new: str) -> str:
    """Substitute one fixed old/new pair into a static SQL template."""
    return template.replace("__OLD__", old).replace("__NEW__", new)


def _rename_table_objects(old_table: str, new_table: str) -> None:
    op.execute(_rendered(_RENAME_TABLE_SQL, old_table, new_table))


def _rename_schema(old_schema: str, new_schema: str) -> None:
    op.execute(_rendered(_RENAME_SCHEMA_SQL, old_schema, new_schema))


def _rename_constraints(pairs: tuple[tuple[str, str], ...]) -> None:
    for old, new in pairs:
        op.execute(_rendered(_RENAME_CONSTRAINT_SQL, old, new))


def _rename_indexes(pairs: tuple[tuple[str, str], ...]) -> None:
    for old, new in pairs:
        op.execute(_rendered(_RENAME_INDEX_SQL, old, new))


def upgrade() -> None:
    _rename_schema(_OLD_SCHEMA, _NEW_SCHEMA)
    _rename_table_objects(_OLD_TABLE, _NEW_TABLE)
    _rename_constraints(_CONSTRAINTS)
    _rename_indexes(_INDEXES)


def downgrade() -> None:
    _rename_indexes(tuple((new, old) for old, new in _INDEXES))
    _rename_constraints(tuple((new, old) for old, new in _CONSTRAINTS))
    _rename_table_objects(_NEW_TABLE, _OLD_TABLE)
    _rename_schema(_NEW_SCHEMA, _OLD_SCHEMA)
