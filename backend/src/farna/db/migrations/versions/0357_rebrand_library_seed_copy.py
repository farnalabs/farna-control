"""Rebrand already-materialised built-in library copy from Modulo to Farna Control.

PR #3300 rebranded the built-in library primitives (``core/library_service/
_seed_data.py``). Those primitives are served from an in-memory registry that is
rebuilt at import, so every NEW read is already correct. Rows that were
MATERIALISED before the rebrand keep the old text, because a copy snapshots the
seed's columns rather than referencing the registry:

* ``db/crud/library_primitive.py::copy_to_adapt`` (and its
  ``core/library_service/__init__.py::_build_copy_args`` twin) inserts an org row
  with ``name`` / ``description`` / ``author`` copied verbatim from the source
  primitive. Copying an in-memory built-in yields ``source = 'local'`` with
  ``forked_from IS NULL`` (the copy records ``forked_from`` only when the source
  row exists on disk).
* ``core/library_service/install.py::_persist_collection_row`` persists a built-in
  collection's own primitive row (``source = 'modulo'``, in the modulo sentinel
  org) so ``collection_install.collection_id`` has an FK target. A later install
  does NOT repair it: the re-sync predicate (``_collection_content_differs``)
  compares only ``content_json`` / ``manifest_pins`` / ``checksum``, so a
  description- or author-only rebrand never triggers a re-write, and
  ``_resync_collection_row`` would not touch ``author`` even if it did.

This migration rewrites the two columns whose old seed value carried the brand
noun, and ONLY where the current value is byte-identical to what the old seed
produced. A row an operator has edited does not match the equality guard and is
left untouched, so no user-authored text is ever rewritten.

What is NOT touched, and why:

* ``agents`` / ``schemas`` / ``pipelines`` receive a pin's ``name`` and
  ``description`` through ``install.py::_build_bundle_from_pins``. No built-in
  primitive's ``name``, ``description`` or ``prompt_template`` contains the brand
  noun (verified by walking every string field of all 39 seeded primitives'
  ``content_json``; the only match is ``content_json -> trust_header -> source``
  = ``'modulo'``, an identifier the rebrand deliberately kept). So an installed
  collection materialises no brand text and needs no rewrite. The synthesised
  pipeline's name is ``Collection: <collection name>`` and its description is the
  literal ``"Entities installed from a library collection"``.
* ``pipelines.description`` is also written by
  ``api/routes/library.py::_build_pipeline_from_template`` (defaults to
  ``primitive.description``), but that route requires ``primitive_type =
  'pipeline_template'``. The only seeded description carrying the brand noun
  belongs to ``simplest-workflow``, a ``workflow`` primitive, so it cannot reach
  a pipeline through that path either.
* ``library_primitives`` rows with ``source = 'registry'`` are excluded from BOTH
  rewrites. The registry's own author identity is still ``'modulo'`` (PR #3300
  left registry entry authors and their signed slug payloads unchanged), and a
  pulled row's ``checksum`` column is the SHA-256 of an entry payload that
  INCLUDES ``description`` (``core/registry/__init__.py::_build_entry``), so
  rewriting that description would invalidate the row's stored integrity
  evidence.
* ``publishers.name`` — the in-code ``"Modulo Team"`` publisher is a plain
  dataclass held in a module-level dict (``core/registry/__init__.py``), never
  persisted; the ``publishers`` table is written only by
  ``db/crud/publisher.py::create_publisher`` from an admin-supplied name.
* ``library_primitives.name`` — no built-in primitive's name contains the brand
  noun, so there is nothing to match.

The author statement's ``source = 'local' AND forked_from IS NULL`` guard is what
keeps a copy OF a registry row at ``author = 'modulo'``. It is stable rather than
heuristic: ``library_primitives.forked_from`` is ``ON DELETE RESTRICT`` (baseline
0350), so the reference is never nulled out from under a copy.

Idempotent: the guard is an equality on the OLD value, so a second run matches
nothing. Data-only: no DDL, no column changes, no new tables.

Downgrade: the exact reverse mapping (new -> old) under the same equality guards.
Note that ``author = 'Farna Control'`` is also what every built-in now produces,
so the downgrade is only correct when it ships together with a code rollback; a
separately-applied downgrade would also flip rows copied after the rebrand.

Revision ID: 0357_rebrand_library_seed_copy
Revises: 0356_soft_delete_community_library_primitives
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0357_rebrand_library_seed_copy"
down_revision: str | None = "0356_soft_delete_community_library_primitives"
branch_labels = None
depends_on = None

#: The pre-rebrand author every seeded built-in carried (``_make_modulo`` ->
#: ``_build_builtin(author="modulo")``). ``'modulo'`` is also the registry
#: publishing identity, which is why the statement below carries a provenance
#: guard and not just this value equality.
_SEED_AUTHOR_BEFORE = "modulo"
_SEED_AUTHOR_AFTER = "Farna Control"

#: ``_seed_data._MODULO_PRIMITIVES`` -> ``simplest-workflow`` description, before
#: and after PR #3300. This is the ONLY seeded name/description that carried the
#: brand noun. The two strings differ only by that noun (asserted in
#: ``tests/unit/db/test_migration_0357_rebrand_library_seed_copy.py``).
_WORKFLOW_DESCRIPTION_BEFORE = (
    "The absolute simplest Modulo pipeline — a single agent that reads a "
    "freeform specification (markdown), implements the required changes in "
    "a target code path, runs a test command to validate, and optionally "
    "git-commits the result. No connectors, no cloud services, no setup "
    "beyond a model backend. Perfect for day-1 evaluation."
)
_WORKFLOW_DESCRIPTION_AFTER = (
    "The absolute simplest Farna Control pipeline — a single agent that reads a "
    "freeform specification (markdown), implements the required changes in "
    "a target code path, runs a test command to validate, and optionally "
    "git-commits the result. No connectors, no cloud services, no setup "
    "beyond a model backend. Perfect for day-1 evaluation."
)

# Both statements are statement-scoped so the value equality and the provenance
# guard are impossible to drift apart. ``source = 'modulo'`` is the persisted
# built-in collection row; ``source = 'local'`` with a NULL ``forked_from`` is a
# copy of an in-memory built-in (``copy_to_adapt`` records ``forked_from`` only
# when the source row is on disk). A local copy OF a registry row therefore
# keeps its unchanged registry author.
_AUTHOR_SQL = """
UPDATE library_primitives
SET author = :after
WHERE author = :before
  AND (
        source = 'modulo'
     OR (source = 'local' AND forked_from IS NULL)
  )
"""

_DESCRIPTION_SQL = """
UPDATE library_primitives
SET description = :after
WHERE description = :before
  AND source IN ('modulo', 'local')
"""


def _rewrite_author(before: str, after: str) -> None:
    op.execute(sa.text(_AUTHOR_SQL).bindparams(before=before, after=after))


def _rewrite_workflow_description(before: str, after: str) -> None:
    op.execute(sa.text(_DESCRIPTION_SQL).bindparams(before=before, after=after))


def upgrade() -> None:
    _rewrite_workflow_description(_WORKFLOW_DESCRIPTION_BEFORE, _WORKFLOW_DESCRIPTION_AFTER)
    _rewrite_author(_SEED_AUTHOR_BEFORE, _SEED_AUTHOR_AFTER)


def downgrade() -> None:
    _rewrite_author(_SEED_AUTHOR_AFTER, _SEED_AUTHOR_BEFORE)
    _rewrite_workflow_description(_WORKFLOW_DESCRIPTION_AFTER, _WORKFLOW_DESCRIPTION_BEFORE)
