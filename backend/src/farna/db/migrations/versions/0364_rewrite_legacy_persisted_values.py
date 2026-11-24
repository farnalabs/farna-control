"""Rewrite the last persisted legacy values from ``modulo`` to ``farna_control``.

0362 renamed the remaining legacy *database object* names. Values that live
inside rows could not be renamed by an ``ALTER ... RENAME``, so this migration
rewrites them. The product is Farna Control; ``modulo`` is the retired name.

Rewritten values
----------------
* ``library_primitives.source`` — the CHECK vocabulary allowed ``'modulo'`` as
  the native-library provenance. Widened to accept both values, every
  ``'modulo'`` row rewritten to ``'farna_control'``, then narrowed so
  ``'modulo'`` is no longer accepted. ``ck_library_primitives_source_fields``
  carries the same literal in one branch, so it is widened/rewritten/narrowed
  with it; a rewritten row would violate that constraint if it were left
  narrow.
* ``lifecycle_map_stages.stage_type`` — the CHECK vocabulary allowed
  ``'modulo'`` for a stage backed by a Farna Control pipeline. Same
  widen/rewrite/narrow treatment. ``lifecycle_maps.content_json`` holds the
  same vocabulary inside its ``stages[].type`` array, and the projection table
  is re-derived from it on every save (``normalize_content`` validates against
  ``STAGE_TYPES``), so a stored map that kept ``'modulo'`` would fail its next
  save. Its stage types are therefore rewritten in the same migration.
* ``library_primitives.content_json`` for ``primitive_type='lifecycle_map'``
  rows — imported lifecycle maps are also stored as library primitives whose
  ``content_json`` carries the stage graph. Three shapes exist: a direct
  ``stages[].type``; the export envelope's ``export.content_json.stages[].type``
  and ``export.versions[].stages[].type``. Copy-to-adapt re-validates that
  stored graph through ``normalize_content`` (``materialize_map_from_primitive``
  → ``normalize_content`` → ``_normalise_stage``), so a pre-deploy import that
  kept the retired stage type would 422 with no repair path. All three shapes
  are rewritten, bounded by a containment predicate and order-preserving like
  the ``lifecycle_maps`` rewrite.
* the registry sentinel organisation seeded by 0353 — its display name was
  ``Modulo``. Renamed to ``Farna Control``. Its slug is deliberately NOT
  changed: no production code resolves the sentinel by slug, and every
  reference addresses it by the fixed id
  ``00000000-0000-0000-0000-000000000001`` (``MODULO_REGISTRY_ORG_ID``), so
  rewriting an inert slug would only risk an operator script for no gain.

Deliberately NOT rewritten
--------------------------
``_modulo.work_item`` inside ``runs.input_payload``. It is a reserved inbound
key, not a value production code ever writes: it was introduced (FAR-142) in
the same change as ``_strip_reserved_keys``, and every run-creation path
funnels through ``create_run`` / ``coalesce_pending_run``, which strip it
before the payload is hashed or stored. A stored row therefore cannot contain
it, so a rewrite of a hot append-only table could only ever match zero rows —
cost without convergence. The key stays in ``_RESERVED_INPUT_PAYLOAD_KEYS`` so
an inbound payload that still carries it is stripped rather than persisted.

Safety
------
* Every statement is idempotent: each equality matches nothing on a re-run, and
  every constraint is dropped ``IF EXISTS`` before it is re-added, so the final
  shape is the same whether the migration starts from the legacy shape, the
  final shape, or a partially-migrated one.
* Nothing here changes a column type or rewrites a hot table. The ``runs``
  table is not touched. ``library_primitives`` and ``lifecycle_map_stages`` are
  small operator-owned tables; adding a CHECK scans each once under a brief
  ``ACCESS EXCLUSIVE`` lock inside the deploy's migration window.
* The ``lifecycle_maps`` update is bounded by a containment predicate, so it
  touches only rows whose stages still carry the old value. The same holds for
  each ``library_primitives.content_json`` shape.
* The whole migration runs in one transaction, so the widen/rewrite/narrow
  window is never visible to another session.

Downgrade: the exact reverse (new -> old) under the same widen/rewrite/narrow
shape, narrowing to the LEGACY vocabulary and restoring the ``content_json``
stage types.

Revision ID: 0364_rewrite_legacy_persisted_values
Revises: 0363_lower_operational_flag_tiers
Create Date: 2026-11-23
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0364_rewrite_legacy_persisted_values"
down_revision: str | None = "0363_lower_operational_flag_tiers"
branch_labels = None
depends_on = None

#: The retired and the current vocabulary value. Fixed, code-owned literals.
_LEGACY = "modulo"
_CURRENT = "farna_control"

#: The 0353-seeded registry sentinel organisation, addressed by its fixed id.
_SENTINEL_ORG_ID = "00000000-0000-0000-0000-000000000001"
_SENTINEL_NAME_BEFORE = "Modulo"
_SENTINEL_NAME_AFTER = "Farna Control"

# ---------------------------------------------------------------------------
# library_primitives.source
#
# Two named CHECK constraints carry the vocabulary:
#   ck_library_primitives_source        -> the value list
#   ck_library_primitives_source_fields -> a per-source field-shape rule
# Both are dropped and re-added (idempotent by construction) rather than
# altered, because PostgreSQL has no ALTER CONSTRAINT for a CHECK expression.
# Each is materialised in three shapes: WIDENED (both values, used around the
# rewrite), LEGACY-only and CURRENT-only (the narrowed end state, chosen by
# direction).
# ---------------------------------------------------------------------------

_SOURCES_WIDENED = "'local', 'registry', 'modulo', 'farna_control', 'community'"
_SOURCES_LEGACY = "'local', 'registry', 'modulo', 'community'"
_SOURCES_CURRENT = "'local', 'registry', 'farna_control', 'community'"

_DROP_SOURCE_CHECK_SQL = """
ALTER TABLE public.library_primitives
    DROP CONSTRAINT IF EXISTS ck_library_primitives_source
"""

_ADD_SOURCE_CHECK_SQL = """
ALTER TABLE public.library_primitives
    ADD CONSTRAINT ck_library_primitives_source
    CHECK (source IN (__SOURCES__))
"""

_DROP_SOURCE_FIELDS_CHECK_SQL = """
ALTER TABLE public.library_primitives
    DROP CONSTRAINT IF EXISTS ck_library_primitives_source_fields
"""

#: One non-registry provenance branch. ``local`` uses the strictest evidence
#: shape; ``modulo``/``farna_control`` share it verbatim.
_STRICT_EVIDENCE_BRANCH = """
(source = '__SOURCE__' AND source_url IS NULL AND checksum IS NULL
    AND ed25519_signature IS NULL AND verified IS NULL
    AND download_count IS NULL AND average_rating IS NULL
    AND review_count IS NULL)
"""
_LEGACY_BRANCH = _STRICT_EVIDENCE_BRANCH.replace("__SOURCE__", _LEGACY)
_CURRENT_BRANCH = _STRICT_EVIDENCE_BRANCH.replace("__SOURCE__", _CURRENT)

_ADD_SOURCE_FIELDS_CHECK_SQL = """
ALTER TABLE public.library_primitives
    ADD CONSTRAINT ck_library_primitives_source_fields
    CHECK (
        (source = 'local' AND source_url IS NULL AND checksum IS NULL
            AND ed25519_signature IS NULL AND verified IS NULL
            AND download_count IS NULL AND average_rating IS NULL
            AND review_count IS NULL)
        OR __EXTRA_BRANCHES__
        OR (source = 'community' AND source_url IS NULL AND checksum IS NULL
            AND ed25519_signature IS NULL
            AND download_count IS NULL AND average_rating IS NULL
            AND review_count IS NULL)
        OR (source = 'registry' AND owner_team_id IS NULL
            AND visibility = 'org' AND forked_from IS NULL
            AND source_url IS NOT NULL AND checksum IS NOT NULL
            AND verified IS NOT NULL AND download_count IS NOT NULL)
    )
"""

_REWRITE_SOURCE_SQL = "UPDATE public.library_primitives SET source = :after WHERE source = :before"

# ---------------------------------------------------------------------------
# lifecycle_map_stages.stage_type + lifecycle_maps.content_json
# ---------------------------------------------------------------------------

_STAGES_WIDENED = "'modulo', 'farna_control', 'external', 'manual', 'placeholder'"
_STAGES_LEGACY = "'modulo', 'external', 'manual', 'placeholder'"
_STAGES_CURRENT = "'farna_control', 'external', 'manual', 'placeholder'"

_DROP_STAGE_CHECK_SQL = """
ALTER TABLE public.lifecycle_map_stages
    DROP CONSTRAINT IF EXISTS ck_lifecycle_map_stages_type
"""

_ADD_STAGE_CHECK_SQL = """
ALTER TABLE public.lifecycle_map_stages
    ADD CONSTRAINT ck_lifecycle_map_stages_type
    CHECK (stage_type IN (__STAGES__))
"""

_REWRITE_STAGE_SQL = "UPDATE public.lifecycle_map_stages SET stage_type = :after WHERE stage_type = :before"

#: A JSONB array cannot be updated element-wise in one statement, so the array is
#: rebuilt. The containment predicate limits the statement to rows that actually
#: carry the old value and makes a re-run a no-op. ``jsonb_set`` is only reached
#: for an element whose ``type`` matches, so a malformed element is left alone.
#: ``WITH ORDINALITY`` plus ``ORDER BY ord`` pins the rebuilt array to its original
#: element order: stage order is positional downstream, and an unordered
#: ``jsonb_agg`` may return rows in any order and silently reorder the stages.
_REWRITE_STAGE_CONTENT_SQL = """
UPDATE public.lifecycle_maps
SET content_json = jsonb_set(
        content_json,
        '{stages}',
        (
            SELECT jsonb_agg(
                       CASE
                           WHEN stage ->> 'type' = CAST(:before AS text)
                               THEN jsonb_set(stage, '{type}', to_jsonb(CAST(:after AS text)))
                           ELSE stage
                       END
                       ORDER BY ord
                   )
            FROM jsonb_array_elements(content_json -> 'stages') WITH ORDINALITY AS staged(stage, ord)
        )
    )
WHERE jsonb_typeof(content_json -> 'stages') = 'array'
  AND content_json -> 'stages' @> jsonb_build_array(jsonb_build_object('type', CAST(:before AS text)))
"""

# ---------------------------------------------------------------------------
# library_primitives.content_json (primitive_type='lifecycle_map')
#
# Imported maps are persisted as library primitives with the stage graph nested
# in ``content_json`` (``import_export.py``). ``materialize_map_from_primitive``
# re-validates that graph through ``normalize_content``, so a retired stage type
# must be rewritten here as well. Three shapes exist and each is rewritten in its
# own bounded, order-preserving statement:
#   * a direct ``stages[].type`` (the graph IS the content);
#   * ``export.content_json.stages[].type`` (the envelope's canonical content);
#   * ``export.versions[].stages[].type`` (each exported version's snapshot).
# ``WITH ORDINALITY`` + ``ORDER BY`` keeps positional stage order, and the
# containment predicate makes every statement a no-op on a re-run.
# ---------------------------------------------------------------------------

_REWRITE_PRIMITIVE_DIRECT_STAGES_SQL = """
UPDATE public.library_primitives
SET content_json = jsonb_set(
        content_json,
        '{stages}',
        (
            SELECT jsonb_agg(
                       CASE
                           WHEN stage ->> 'type' = CAST(:before AS text)
                               THEN jsonb_set(stage, '{type}', to_jsonb(CAST(:after AS text)))
                           ELSE stage
                       END
                       ORDER BY ord
                   )
            FROM jsonb_array_elements(content_json -> 'stages') WITH ORDINALITY AS staged(stage, ord)
        )
    )
WHERE primitive_type = 'lifecycle_map'
  AND jsonb_typeof(content_json -> 'stages') = 'array'
  AND content_json -> 'stages' @> jsonb_build_array(jsonb_build_object('type', CAST(:before AS text)))
"""

_REWRITE_PRIMITIVE_EXPORT_CONTENT_STAGES_SQL = """
UPDATE public.library_primitives
SET content_json = jsonb_set(
        content_json,
        '{export,content_json,stages}',
        (
            SELECT jsonb_agg(
                       CASE
                           WHEN stage ->> 'type' = CAST(:before AS text)
                               THEN jsonb_set(stage, '{type}', to_jsonb(CAST(:after AS text)))
                           ELSE stage
                       END
                       ORDER BY ord
                   )
            FROM jsonb_array_elements(content_json -> 'export' -> 'content_json' -> 'stages') WITH ORDINALITY AS staged(stage, ord)
        )
    )
WHERE primitive_type = 'lifecycle_map'
  AND jsonb_typeof(content_json -> 'export' -> 'content_json' -> 'stages') = 'array'
  AND content_json -> 'export' -> 'content_json' -> 'stages' @> jsonb_build_array(jsonb_build_object('type', CAST(:before AS text)))
"""

_REWRITE_PRIMITIVE_EXPORT_VERSION_STAGES_SQL = """
UPDATE public.library_primitives
SET content_json = jsonb_set(
        content_json,
        '{export,versions}',
        (
            SELECT jsonb_agg(
                       CASE
                           WHEN jsonb_typeof(version -> 'stages') = 'array'
                               THEN jsonb_set(
                                        version,
                                        '{stages}',
                                        (
                                            SELECT jsonb_agg(
                                                       CASE
                                                           WHEN stage ->> 'type' = CAST(:before AS text)
                                                               THEN jsonb_set(stage, '{type}', to_jsonb(CAST(:after AS text)))
                                                           ELSE stage
                                                       END
                                                       ORDER BY stage_ord
                                                   )
                                            FROM jsonb_array_elements(version -> 'stages') WITH ORDINALITY AS staged(stage, stage_ord)
                                        )
                                    )
                           ELSE version
                       END
                       ORDER BY version_ord
                   )
            FROM jsonb_array_elements(content_json -> 'export' -> 'versions') WITH ORDINALITY AS versioned(version, version_ord)
        )
    )
WHERE primitive_type = 'lifecycle_map'
  AND jsonb_typeof(content_json -> 'export' -> 'versions') = 'array'
  AND content_json -> 'export' -> 'versions' @> jsonb_build_array(
        jsonb_build_object('stages', jsonb_build_array(jsonb_build_object('type', CAST(:before AS text))))
      )
"""

# ---------------------------------------------------------------------------
# Sentinel organisation display name
# ---------------------------------------------------------------------------

_RENAME_SENTINEL_SQL = """
UPDATE public.organisations
SET name = CAST(:after AS text)
WHERE id = CAST(:org_id AS uuid)
  AND name = CAST(:before AS text)
"""


def _replace_source_constraints(*, sources: str, extra_branches: str) -> None:
    """Install the ``library_primitives`` vocabularies selected by the caller."""
    op.execute(_DROP_SOURCE_CHECK_SQL)
    op.execute(_ADD_SOURCE_CHECK_SQL.replace("__SOURCES__", sources))
    op.execute(_DROP_SOURCE_FIELDS_CHECK_SQL)
    op.execute(_ADD_SOURCE_FIELDS_CHECK_SQL.replace("__EXTRA_BRANCHES__", extra_branches))


def _replace_stage_constraint(*, stages: str) -> None:
    """Install the ``lifecycle_map_stages`` vocabulary selected by the caller."""
    op.execute(_DROP_STAGE_CHECK_SQL)
    op.execute(_ADD_STAGE_CHECK_SQL.replace("__STAGES__", stages))


def _rewrite_source_rows(*, before: str, after: str) -> None:
    op.execute(sa.text(_REWRITE_SOURCE_SQL).bindparams(before=before, after=after))


def _rewrite_stage_rows(*, before: str, after: str) -> None:
    op.execute(sa.text(_REWRITE_STAGE_SQL).bindparams(before=before, after=after))
    op.execute(sa.text(_REWRITE_STAGE_CONTENT_SQL).bindparams(before=before, after=after))
    _rewrite_primitive_stage_contents(before=before, after=after)


def _rewrite_primitive_stage_contents(*, before: str, after: str) -> None:
    """Rewrite the legacy stage vocabulary nested in ``library_primitives.content_json``."""
    for statement in (
        _REWRITE_PRIMITIVE_DIRECT_STAGES_SQL,
        _REWRITE_PRIMITIVE_EXPORT_CONTENT_STAGES_SQL,
        _REWRITE_PRIMITIVE_EXPORT_VERSION_STAGES_SQL,
    ):
        op.execute(sa.text(statement).bindparams(before=before, after=after))


def _rename_sentinel_org(*, before: str, after: str) -> None:
    op.execute(sa.text(_RENAME_SENTINEL_SQL).bindparams(before=before, after=after, org_id=_SENTINEL_ORG_ID))


def _migrate(*, before: str, after: str, final_sources: str, final_branches: str, final_stages: str) -> None:
    """Widen both vocabularies, rewrite the rows, then narrow to the target shape."""
    _replace_source_constraints(
        sources=_SOURCES_WIDENED,
        extra_branches=_LEGACY_BRANCH + " OR " + _CURRENT_BRANCH,
    )
    _replace_stage_constraint(stages=_STAGES_WIDENED)
    _rewrite_source_rows(before=before, after=after)
    _rewrite_stage_rows(before=before, after=after)
    _replace_source_constraints(sources=final_sources, extra_branches=final_branches)
    _replace_stage_constraint(stages=final_stages)


def upgrade() -> None:
    _migrate(
        before=_LEGACY,
        after=_CURRENT,
        final_sources=_SOURCES_CURRENT,
        final_branches=_CURRENT_BRANCH,
        final_stages=_STAGES_CURRENT,
    )
    _rename_sentinel_org(before=_SENTINEL_NAME_BEFORE, after=_SENTINEL_NAME_AFTER)


def downgrade() -> None:
    _migrate(
        before=_CURRENT,
        after=_LEGACY,
        final_sources=_SOURCES_LEGACY,
        final_branches=_LEGACY_BRANCH,
        final_stages=_STAGES_LEGACY,
    )
    _rename_sentinel_org(before=_SENTINEL_NAME_AFTER, after=_SENTINEL_NAME_BEFORE)
