"""Generate SQL INSERT statements for seeding pipeline templates.

Usage:
    uv run python -m scripts.gen_seed_sql > seed.sql

The emitted rows are built-in ``pipeline_template`` primitives, so they must
carry the current canonical provenance (``farna_control``) and author
(``Farna Control``), matching ``core/library_service/_seed_data.py::_make_native``.

The legacy provenance value is rejected by ``ck_library_primitives_source`` —
migration 0364 rewrote the stored rows and narrowed the vocabulary — so SQL
that still emitted it would fail on a current database. The legacy author is
the value migration 0357 rewrites to ``Farna Control``; emitting it again would
reintroduce the retired name.
"""

import json

from farna.core.library_service import _BUILTIN_PRIMITIVES
from farna.db.models.library_primitive import LibraryPrimitive

ORG_ID = "6a6d7112-2058-4d65-ac75-465a12a94563"

#: Current canonical provenance and author for a built-in library row. Keep
#: these aligned with ``core/library_service/_seed_data.py`` (``_make_native``)
#: and with the 0364 (source) and 0357 (author) migration rewrite targets.
SOURCE = "farna_control"
AUTHOR = "Farna Control"

_COLUMNS = (
    "id",
    "organisation_id",
    "name",
    "slug",
    "description",
    "primitive_type",
    "author",
    "version",
    "visibility",
    "source",
    "tags",
    "content_json",
    "account_id",
    "auto_update",
    "category",
    "created_at",
    "updated_at",
)


def _sql_literal(value: str) -> str:
    """Render a Python string as a single-quoted SQL literal."""
    return "'" + value.replace("'", "''") + "'"


def _row_values(template: LibraryPrimitive) -> dict[str, str]:
    """Render the INSERT value for every column, keyed by column name."""
    content = dict(template.content_json)
    tags = list(template.tags) if template.tags else []
    return {
        "id": _sql_literal(str(template.id)),
        "organisation_id": _sql_literal(ORG_ID),
        "name": _sql_literal(template.name),
        "slug": _sql_literal(template.slug),
        "description": _sql_literal(template.description or ""),
        "primitive_type": _sql_literal("pipeline_template"),
        "author": _sql_literal(AUTHOR),
        "version": _sql_literal("1.0"),
        "visibility": _sql_literal("community"),
        "source": _sql_literal(SOURCE),
        "tags": _sql_literal(json.dumps(tags)) + "::json",
        "content_json": _sql_literal(json.dumps(content)) + "::json",
        "account_id": "(SELECT id FROM accounts ORDER BY created_at LIMIT 1)",
        "auto_update": "false",
        "category": _sql_literal(content.get("category", "")),
        "created_at": "NOW()",
        "updated_at": "NOW()",
    }


def build_insert(template: LibraryPrimitive) -> str:
    """Build one ``INSERT`` statement for a built-in pipeline template."""
    values = _row_values(template)
    return f"INSERT INTO library_primitives ({', '.join(_COLUMNS)}) VALUES ({', '.join(values[column] for column in _COLUMNS)}) ON CONFLICT (id) DO NOTHING;"


def main() -> None:
    for template in _BUILTIN_PRIMITIVES:
        if template.primitive_type == "pipeline_template":
            print(build_insert(template))


if __name__ == "__main__":
    main()
