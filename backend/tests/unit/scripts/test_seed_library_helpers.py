"""The hand-run library seed helpers agree with the shipped rebrand migrations.

Two developer helpers write built-in library rows by hand, outside the normal
registry path:

* ``scripts/gen_seed_sql.py`` renders ``INSERT`` statements for the built-in
  ``pipeline_template`` primitives.
* ``scripts/seed_library_templates.py`` persists the same primitives through
  ``create_library_primitive``.

Both previously wrote the retired provenance/author literals. The retired
source is not in the current ``ck_library_primitives_source`` vocabulary
(migration 0364 rewrote the stored rows and narrowed it), so the generated SQL
would be rejected by the CHECK; and the manual seed used a local source with a
non-null ``forked_from``, a shape the 0357 author rewrite deliberately excludes,
so every run reintroduced the retired author.

These tests pin the helpers to the CURRENT canonical values without needing a
database: the source vocabulary is read from the live ORM CHECK constraint, the
rewrite targets are read from the migration modules, and the migration's own
``UPDATE`` statements are executed against in-memory SQLite to prove the seed's
row is already in the post-rewrite state. The retired literals are never spelled
here — they are read from the migration module (brand-consistency gate).
"""

from __future__ import annotations

import importlib.util
import re
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import cast

import gen_seed_sql
import seed_library_templates
from sqlalchemy import CheckConstraint, Table

from farna.core.library_service import _BUILTIN_PRIMITIVES
from farna.db.models.library_primitive import LibraryPrimitive

_MIGRATIONS = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions"

_SQLITE_DDL = """
CREATE TABLE library_primitives (
    id integer primary key,
    source text NOT NULL,
    description text,
    author text NOT NULL,
    forked_from text
)
"""

_FORKED_FROM_ID = "11111111-1111-1111-1111-111111111111"


def _load_migration(stem: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{stem}", _MIGRATIONS / f"{stem}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_source_vocabulary() -> frozenset[str]:
    """Allowed ``library_primitives.source`` values from the live ORM constraint."""
    for constraint in cast(Table, LibraryPrimitive.__table__).constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == "ck_library_primitives_source":
            match = re.search(r"IN \((.*)\)", str(constraint.sqltext))
            assert match is not None
            return frozenset(re.findall(r"'([^']+)'", match.group(1)))
    raise AssertionError("ck_library_primitives_source is missing from the LibraryPrimitive model")


def _pipeline_template() -> LibraryPrimitive:
    templates = [p for p in _BUILTIN_PRIMITIVES if p.primitive_type == "pipeline_template"]
    assert templates
    return templates[0]


def _sqlite_portable(sql: str) -> str:
    """Drop the migration's hard-coded ``public`` schema so SQLite can execute it."""
    return sql.replace("public.", "")


def test_generated_seed_sql_uses_a_source_the_current_check_accepts() -> None:
    legacy_values_0364 = _load_migration("0364_rewrite_legacy_persisted_values")
    template = _pipeline_template()

    sql = gen_seed_sql.build_insert(template)
    source = gen_seed_sql._row_values(template)["source"].strip("'")

    assert source in _current_source_vocabulary()
    assert source == legacy_values_0364._CURRENT
    assert f"'{source}'" in sql


def test_generated_seed_sql_carries_the_current_author() -> None:
    rebrand_0357 = _load_migration("0357_rebrand_library_seed_copy")
    template = _pipeline_template()

    sql = gen_seed_sql.build_insert(template)
    author = gen_seed_sql._row_values(template)["author"].strip("'")

    assert author == rebrand_0357._SEED_AUTHOR_AFTER
    assert f"'{author}'" in sql


def test_manual_seed_values_are_the_migrations_rewrite_targets() -> None:
    rebrand_0357 = _load_migration("0357_rebrand_library_seed_copy")
    legacy_values_0364 = _load_migration("0364_rewrite_legacy_persisted_values")

    kwargs = seed_library_templates.build_primitive_kwargs(_pipeline_template())

    assert kwargs["author"] == rebrand_0357._SEED_AUTHOR_AFTER
    assert kwargs["source"] == legacy_values_0364._CURRENT
    assert kwargs["source"] in _current_source_vocabulary()
    assert kwargs["forked_from"] is None


def test_old_seed_shape_escaped_the_rewrite_but_the_new_shape_is_already_converged() -> None:
    rebrand_0357 = _load_migration("0357_rebrand_library_seed_copy")
    legacy_values_0364 = _load_migration("0364_rewrite_legacy_persisted_values")
    kwargs = seed_library_templates.build_primitive_kwargs(_pipeline_template())

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_SQLITE_DDL)
        connection.executemany(
            "INSERT INTO library_primitives (id, source, description, author, forked_from) VALUES (?, ?, ?, ?, ?)",
            [
                # The retired helper's shape: local source + non-null forked_from.
                (1, "local", "unrelated operator description", rebrand_0357._SEED_AUTHOR_BEFORE, _FORKED_FROM_ID),
                # A row still carrying the retired source value.
                (3, legacy_values_0364._LEGACY, "unrelated operator description", "operator-authored", None),
                # The current helper's shape.
                (2, kwargs["source"], "unrelated operator description", kwargs["author"], kwargs["forked_from"]),
            ],
        )
        connection.execute(
            rebrand_0357._AUTHOR_SQL,
            {"before": rebrand_0357._SEED_AUTHOR_BEFORE, "after": rebrand_0357._SEED_AUTHOR_AFTER},
        )
        connection.execute(
            _sqlite_portable(legacy_values_0364._REWRITE_SOURCE_SQL),
            {"before": legacy_values_0364._LEGACY, "after": legacy_values_0364._CURRENT},
        )
        authors = dict(connection.execute("SELECT id, author FROM library_primitives").fetchall())
        sources = dict(connection.execute("SELECT id, source FROM library_primitives").fetchall())
    finally:
        connection.close()

    # The old shape is NOT matched by 0357's guard, which is why it survived the
    # migration and reintroduced the retired author on the next manual run.
    assert authors[1] == rebrand_0357._SEED_AUTHOR_BEFORE
    # The seed now writes the post-rewrite author, so 0357 never has to touch it.
    assert authors[2] == kwargs["author"]
    # A legacy-sourced row converges to exactly the value the seed writes.
    assert sources[3] == kwargs["source"]
    # A current-sourced row is already in the post-0364 state.
    assert sources[2] == kwargs["source"]
