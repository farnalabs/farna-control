"""The 0357 migration rebrands ONLY the exact seeded library copy (PR #3300).

The migration is a pair of data-only UPDATEs. These tests pin the two properties
that matter:

* it fires on the two values the pre-rebrand seed produced — the ``'modulo'``
  author every seeded built-in carried, and the single seeded description that
  named the brand; and
* it does NOT fire on anything else: an operator-edited description, a
  registry-pulled row (whose ``checksum`` covers its description), or a copy that
  forked from a registry row (whose author identity is still ``'modulo'``).

The statements are also EXECUTED against an in-memory SQLite database rather
than only read as text: ``env.py`` selects the sqlite dialect, so a statement
that merely looks portable is not evidence. The real-Postgres rehearsal of the
whole chain lives in ``tests/integration/db/test_migration_rehearsal.py`` (CI
only — this workstation has no Docker), so the unit tier pins the row selection,
the portable execution, idempotence, downgrade symmetry and the graph shape.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic import op

from farna.core.library_service._seed_data import _BUILTIN_PRIMITIVES
from farna.core.library_service.install import _build_bundle_from_pins

_BRAND_NOUN = "Modulo"

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0357_rebrand_library_seed_copy.py"

_SQLITE_DDL = """
CREATE TABLE library_primitives (
    id integer primary key,
    source text NOT NULL,
    description text,
    author text NOT NULL,
    forked_from text
)
"""

# Seeded rows. Each id exercises one branch of the two guards.
_LOCAL_BUILTIN_COPY_ID = 1  # local copy of an in-memory builtin -> BOTH rewritten
_USER_EDITED_COPY_ID = 2  # operator appended to the description -> only author rewritten
_REGISTRY_PULL_ID = 3  # source='registry' -> never rewritten (checksum covers description)
_LOCAL_REGISTRY_COPY_ID = 4  # local copy forked from a registry row -> author keeps 'modulo'
_SENTINEL_COLLECTION_ID = 5  # source='modulo' -> author rewritten, description does not match
_USER_AUTHORED_ID = 6  # unrelated author -> untouched

_FORKED_FROM_ID = "11111111-1111-1111-1111-111111111111"
_USER_EDITED_SUFFIX = " My own note."
_SENTINEL_DESCRIPTION = "One-click AI PR review for GitHub repos."


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0357_rebrand_library_seed_copy", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(monkeypatch: pytest.MonkeyPatch, run: Callable[[], None]) -> list[tuple[str, dict[str, str]]]:
    """Run ``run`` with ``op.execute`` captured; return (sql, bound params) pairs.

    The migration builds each statement as ``sa.text(...).bindparams(...)``, so
    the captured object carries both the SQL text and the values. Returning them
    together lets the tests execute exactly what production executes.
    """
    executed: list[sa.TextClause] = []
    monkeypatch.setattr(op, "execute", executed.append)

    run()

    statements: list[tuple[str, dict[str, str]]] = []
    for clause in executed:
        compiled = clause.compile()
        statements.append((str(clause), dict(compiled.params)))
    return statements


def _seed_rows(module: ModuleType) -> list[tuple[int, str, str, str, str | None]]:
    """Return the (id, source, description, author, forked_from) rows to insert."""
    return [
        (_LOCAL_BUILTIN_COPY_ID, "local", module._WORKFLOW_DESCRIPTION_BEFORE, module._SEED_AUTHOR_BEFORE, None),
        (
            _USER_EDITED_COPY_ID,
            "local",
            module._WORKFLOW_DESCRIPTION_BEFORE + _USER_EDITED_SUFFIX,
            module._SEED_AUTHOR_BEFORE,
            None,
        ),
        (_REGISTRY_PULL_ID, "registry", module._WORKFLOW_DESCRIPTION_BEFORE, module._SEED_AUTHOR_BEFORE, None),
        (_LOCAL_REGISTRY_COPY_ID, "local", _SENTINEL_DESCRIPTION, module._SEED_AUTHOR_BEFORE, _FORKED_FROM_ID),
        (_SENTINEL_COLLECTION_ID, "modulo", _SENTINEL_DESCRIPTION, module._SEED_AUTHOR_BEFORE, None),
        (_USER_AUTHORED_ID, "local", _SENTINEL_DESCRIPTION, "operator-authored", None),
    ]


def _apply(connection: sqlite3.Connection, statements: list[tuple[str, dict[str, str]]]) -> None:
    for sql, params in statements:
        connection.execute(sql, params)
    connection.commit()


def _snapshot(connection: sqlite3.Connection) -> dict[int, tuple[str, str | None, str, str | None]]:
    rows = connection.execute("SELECT id, source, description, author, forked_from FROM library_primitives").fetchall()
    return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}


def _open_seeded(module: ModuleType) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(_SQLITE_DDL)
    connection.executemany(
        "INSERT INTO library_primitives (id, source, description, author, forked_from) VALUES (?, ?, ?, ?, ?)",
        _seed_rows(module),
    )
    return connection


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0357_rebrand_library_seed_copy"
    assert module.down_revision == "0356_soft_delete_community_library_primitives"


def test_upgrade_statements_are_equality_guarded_and_portable(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()

    statements = _capture(monkeypatch, module.upgrade)

    assert len(statements) == 2
    joined = "\n".join(sql for sql, _params in statements)
    # Equality-guarded, never a blanket text replacement.
    assert "REPLACE(" not in joined.upper()
    assert "LIKE " not in joined.upper()
    assert ":before" in joined
    # Data-only: no DDL, no schema prefix, no Postgres-only now().
    assert "ALTER TABLE" not in joined.upper()
    assert "DELETE FROM" not in joined.upper()
    assert "public." not in joined
    assert "now()" not in joined.lower()
    # Every statement carries a WHERE clause — a blank UPDATE is impossible.
    for sql, _params in statements:
        assert "WHERE" in sql


def test_upgrade_rewrites_only_the_exact_seeded_values(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    statements = _capture(monkeypatch, module.upgrade)

    connection = _open_seeded(module)
    try:
        _apply(connection, statements)
        rows = _snapshot(connection)
    finally:
        connection.close()

    # Fires on the exact pre-rebrand pair.
    builtin_copy = rows[_LOCAL_BUILTIN_COPY_ID]
    assert builtin_copy[1] == module._WORKFLOW_DESCRIPTION_AFTER
    assert builtin_copy[2] == module._SEED_AUTHOR_AFTER

    # An operator-edited description keeps the operator's text; its author is a
    # byte-identical seed value, so that column is still rewritten.
    edited = rows[_USER_EDITED_COPY_ID]
    assert edited[1] == module._WORKFLOW_DESCRIPTION_BEFORE + _USER_EDITED_SUFFIX
    assert edited[2] == module._SEED_AUTHOR_AFTER

    # Registry provenance is never touched — its checksum covers the description
    # and the registry still identifies itself as author 'modulo'.
    registry = rows[_REGISTRY_PULL_ID]
    assert registry[1] == module._WORKFLOW_DESCRIPTION_BEFORE
    assert registry[2] == module._SEED_AUTHOR_BEFORE

    # A local copy forked from a registry row keeps the registry author.
    forked = rows[_LOCAL_REGISTRY_COPY_ID]
    assert forked[1] == _SENTINEL_DESCRIPTION
    assert forked[2] == module._SEED_AUTHOR_BEFORE

    # The persisted built-in collection row loses its old author but its
    # description never matched the workflow text.
    sentinel = rows[_SENTINEL_COLLECTION_ID]
    assert sentinel[1] == _SENTINEL_DESCRIPTION
    assert sentinel[2] == module._SEED_AUTHOR_AFTER

    # An unrelated author is left alone.
    authored = rows[_USER_AUTHORED_ID]
    assert authored[2] == "operator-authored"


def test_upgrade_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    statements = _capture(monkeypatch, module.upgrade)

    connection = _open_seeded(module)
    try:
        _apply(connection, statements)
        first_pass = _snapshot(connection)
        _apply(connection, statements)
        second_pass = _snapshot(connection)
    finally:
        connection.close()

    assert first_pass == second_pass


def test_downgrade_reverses_exactly_the_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_migration()
    upgrade_statements = _capture(monkeypatch, module.upgrade)
    downgrade_statements = _capture(monkeypatch, module.downgrade)

    assert len(downgrade_statements) == len(upgrade_statements)

    connection = _open_seeded(module)
    try:
        before = _snapshot(connection)
        _apply(connection, upgrade_statements)
        _apply(connection, downgrade_statements)
        after = _snapshot(connection)
    finally:
        connection.close()

    # Only the rows the upgrade rewrote come back; every other row is identical
    # because it was never eligible in either direction.
    assert after == before


def test_brand_strings_differ_only_by_the_brand_noun() -> None:
    module = _load_migration()

    assert module._SEED_AUTHOR_BEFORE == "modulo"
    assert module._SEED_AUTHOR_AFTER == "Farna Control"
    assert module._WORKFLOW_DESCRIPTION_BEFORE.replace("Modulo", "Farna Control") == module._WORKFLOW_DESCRIPTION_AFTER
    assert module._WORKFLOW_DESCRIPTION_BEFORE.count("Modulo") == 1
    assert "Modulo" not in module._WORKFLOW_DESCRIPTION_AFTER


def test_constants_cover_a_real_seeded_definition() -> None:
    """The hard-coded strings must be the seed's own values, whichever side of
    the rebrand ``main`` sits on.

    Before PR #3300 the live definition equals the OLD constant; afterwards it
    equals the NEW one. Either way the migration's constants must contain it —
    that is what keeps the hard-coded strings honest without depending on the
    rebrand landing first.
    """
    module = _load_migration()

    live_descriptions = {primitive.description for primitive in _BUILTIN_PRIMITIVES if primitive.slug == "simplest-workflow"}
    assert live_descriptions <= {module._WORKFLOW_DESCRIPTION_BEFORE, module._WORKFLOW_DESCRIPTION_AFTER}

    live_authors = {primitive.author for primitive in _BUILTIN_PRIMITIVES}
    assert live_authors <= {module._SEED_AUTHOR_BEFORE, module._SEED_AUTHOR_AFTER}


def test_no_collection_install_bundle_carries_the_brand_noun() -> None:
    """Evidence that the migration needs no agent/schema/pipeline statement.

    Every built-in collection is projected through the same bundle builder the
    installer uses (``install_collection`` -> ``materialize_import``), and no
    name / description / prompt_template it emits carries the brand noun. If a
    future seed edit puts brand text into a pin, this fails and forces the
    question of whether a migration is needed for the materialised copy too.
    """
    offenders: list[str] = []
    by_slug = {primitive.slug: primitive for primitive in _BUILTIN_PRIMITIVES}
    collections = [primitive for primitive in _BUILTIN_PRIMITIVES if primitive.primitive_type == "library_collection"]
    assert collections

    for collection in collections:
        pins = [by_slug[pin["slug"]] for pin in (collection.content_json or {}).get("manifest_pins", [])]
        bundle = _build_bundle_from_pins(pins)
        # ``install_collection`` overrides the synthesised pipeline's name.
        rendered_names = [f"Collection: {collection.name}"]
        rendered_names.append(bundle["pipeline"].get("name") or "")
        rendered_descriptions = [bundle["pipeline"].get("description") or ""]
        rendered_prompt_templates: list[str] = []
        for agent in bundle["agents"]:
            rendered_names.append(agent.get("name") or "")
            rendered_descriptions.append(agent.get("description") or "")
            rendered_prompt_templates.append(agent.get("prompt_template") or "")
        for schema in bundle["schemas"]:
            rendered_names.append(schema.get("name") or "")
            rendered_descriptions.append(schema.get("description") or "")

        for label, values in (
            ("name", rendered_names),
            ("description", rendered_descriptions),
            ("prompt_template", rendered_prompt_templates),
        ):
            if any(_BRAND_NOUN in value for value in values):
                offenders.append(f"{collection.slug}.{label}")

    assert not offenders
