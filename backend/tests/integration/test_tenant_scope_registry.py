"""ADR 070 §1 — the DECLARED tenant scope of every table must match the schema.

Why this file exists
--------------------
``test_rls_policy_gate.py`` gates the SHAPE of an RLS policy: org equality first,
no fail-open top-level ``OR``, and an explicit exemption list for the six tables
that carry a documented widened arm. It cannot see intent, so a brand-new model
with an ``organisation_id`` column plus a plain ``rls_org_isolation`` policy
passes every one of its assertions — which is exactly how the 93-tables-org-only
/ 11-tables-team-only split in ``0351_baseline_rls.py`` came about.

This file supplies the missing half: ``farna.db.tenant_scope`` declares one of
``team`` / ``account`` / ``org_global`` for every table that carries an
``organisation_id`` column, and the assertions here make the declaration and the
database mutually binding:

* a NEW tenant table that nobody declared fails the gate (so "silently defaults
  to org-wide" is impossible);
* a declaration naming a table that does not exist fails (stale entries);
* a ``team`` declaration without an ``rls_team_isolation`` policy fails, and so
  does a table that carries the team policy but is declared something else;
* a declared ownership column that does not exist fails;
* an ``org_global`` declaration without a real written reason fails;
* a table on the policy gate's exemption list cannot be declared ``team`` or
  ``account`` (the two gates must not disagree).

This is a FAIL gate from day one — no warn mode (repo rule: a gate that cannot
fail is not a gate). It passes on the current tree because the registry is
complete; the non-vacuity of each assertion is proven by mutating the registry,
which is recorded in the delivery notes.

The assertions here are deliberately written against the registry's DATA and the
live catalog, not against the dataclass validators: if a future change weakens
``TableScope.__post_init__``, these tests still catch the bad entry.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from farna.db import tenant_scope
from farna.db.tenant_scope import (
    ADR_070_TEAM_TABLE_COUNT,
    TableScope,
    TenantScope,
    UnknownTenantTableError,
)
from tests.integration.test_rls_policy_gate import _EXEMPTIONS

pytestmark = pytest.mark.integration

# An org-wide declaration must be a real sentence. Re-derived here on purpose
# (not imported from the module) so loosening the module's validator cannot
# loosen the gate.
_MIN_JUSTIFICATION_CHARS = 40
_PLACEHOLDER_JUSTIFICATIONS = frozenset({"", "-", "na", "nil", "none", "tba", "tbd", "todo", "not applicable", "to be decided"})
_PLACEHOLDER_MARKERS = ("n/a", "tbd", "tba", "todo", "to be decided", "not applicable", "fixme", "xxx", "???")

_TEAM_POLICY = "rls_team_isolation"


# ---------------------------------------------------------------------------
# Live schema census
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaCensus:
    """What the database actually looks like, in the three shapes the gate needs."""

    tenant_tables: frozenset[str]
    # table -> the policy names it carries (empty when RLS is on with no policy).
    policies: dict[str, frozenset[str]]
    # table -> every column name, from information_schema.
    columns: dict[str, frozenset[str]]

    def policies_of(self, table: str) -> frozenset[str]:
        return self.policies.get(table, frozenset())

    def columns_of(self, table: str) -> frozenset[str]:
        return self.columns.get(table, frozenset())

    @property
    def team_policy_tables(self) -> frozenset[str]:
        return frozenset(table for table in self.tenant_tables if _TEAM_POLICY in self.policies_of(table))


_TENANT_TABLES_SQL = """
    SELECT c.relname AS table_name
      FROM pg_class c
     WHERE c.relkind IN ('r', 'p')
       AND c.relnamespace = 'public'::regnamespace
       AND EXISTS (
             SELECT 1 FROM pg_attribute a
              WHERE a.attrelid = c.oid
                AND a.attname = 'organisation_id'
                AND a.attnum > 0
                AND NOT a.attisdropped
           )
     ORDER BY c.relname
"""

_POLICIES_SQL = """
    SELECT c.relname AS table_name, p.polname AS policy_name
      FROM pg_class c
      JOIN pg_policy p ON p.polrelid = c.oid
     WHERE c.relkind IN ('r', 'p')
       AND c.relnamespace = 'public'::regnamespace
     ORDER BY c.relname, p.polname
"""

_COLUMNS_SQL = """
    SELECT table_name, column_name
      FROM information_schema.columns
     WHERE table_schema = 'public'
"""


async def _load_census(db_engine: AsyncEngine) -> SchemaCensus:
    async with db_engine.connect() as conn:
        tenant_rows = (await conn.execute(text(_TENANT_TABLES_SQL))).fetchall()
        policy_rows = (await conn.execute(text(_POLICIES_SQL))).fetchall()
        column_rows = (await conn.execute(text(_COLUMNS_SQL))).fetchall()

    policies: dict[str, set[str]] = {}
    for table_name, policy_name in policy_rows:
        policies.setdefault(table_name, set()).add(policy_name)
    columns: dict[str, set[str]] = {}
    for table_name, column_name in column_rows:
        columns.setdefault(table_name, set()).add(column_name)
    return SchemaCensus(
        tenant_tables=frozenset(row[0] for row in tenant_rows),
        policies={table: frozenset(names) for table, names in policies.items()},
        columns={table: frozenset(names) for table, names in columns.items()},
    )


@pytest_asyncio.fixture(scope="session")
async def schema_census(db_engine: AsyncEngine) -> SchemaCensus:
    """One catalog read for the whole module (three queries, session-cached)."""
    return await _load_census(db_engine)


# ---------------------------------------------------------------------------
# Completeness in both directions
# ---------------------------------------------------------------------------


def test_every_tenant_table_declares_a_scope(schema_census: SchemaCensus) -> None:
    """A table with ``organisation_id`` that nobody declared fails the gate.

    This is the assertion ADR 070 §1 exists for: a new tenant table must not be
    able to inherit org-wide visibility by default. Adding a table therefore
    requires choosing a scope (``team`` is the default; ``org_global`` needs a
    written reason in the same edit).
    """
    assert schema_census.tenant_tables, "the tenant-table census is empty - the catalog query is broken, so this gate would pass vacuously"
    declared = tenant_scope.all_tables()
    undeclared = sorted(schema_census.tenant_tables - declared)
    assert not undeclared, f"{len(undeclared)} table(s) carry an organisation_id column but declare no tenant scope: {undeclared}. Add one TableScope per table to farna/db/tenant_scope.py (ADR 070 §1: the default is 'team'; 'org_global' needs a written justification)."


def test_no_registry_entry_is_stale(schema_census: SchemaCensus) -> None:
    """A declaration naming a table that does not exist (any more) fails the gate.

    A dropped or renamed table leaves a declaration that reads as a reviewed
    decision about nothing; it must be deleted rather than left to rot.
    """
    declared = tenant_scope.all_tables()
    assert declared, "the tenant-scope registry is empty - every assertion in this module would pass vacuously"
    stale = sorted(declared - schema_census.tenant_tables)
    assert not stale, f"{len(stale)} registry entry/entries name a table that is not a public table carrying an organisation_id column: {stale}. Remove the entry (or fix the table name) in farna/db/tenant_scope.py."


def test_registry_covers_the_whole_census_exactly(schema_census: SchemaCensus) -> None:
    """The two sets are equal: no undeclared tenant table and no stale entry."""
    declared = tenant_scope.all_tables()
    assert declared == schema_census.tenant_tables, f"registry and schema disagree: undeclared={sorted(schema_census.tenant_tables - declared)}, stale={sorted(declared - schema_census.tenant_tables)}"


# ---------------------------------------------------------------------------
# team: the declaration and the policy must agree
# ---------------------------------------------------------------------------


def test_team_entries_carry_the_team_policy(schema_census: SchemaCensus) -> None:
    """Every table declared ``team`` must actually be enforced by the team predicate."""
    team_tables = tenant_scope.team_tables()
    assert team_tables, "no table is declared 'team' - either the registry lost its team class or every declaration drifted to org_global"
    failures = []
    for table in sorted(team_tables):
        found = sorted(schema_census.policies_of(table))
        if _TEAM_POLICY not in found:
            failures.append(f"{table}: declared 'team' but its policies are {found or 'none'} instead of {_TEAM_POLICY!r}")
    assert not failures, "declared team scope is not enforced by the database:\n" + "\n".join(failures)


def test_tables_with_the_team_policy_are_declared_team(schema_census: SchemaCensus) -> None:
    """The converse: a table carrying ``rls_team_isolation`` cannot be declared anything else.

    Without this half, a table could quietly lose its declaration (or be
    misdeclared ``org_global``) while the policy kept enforcing a team boundary —
    the registry would then describe a different product than the one running.
    """
    enforced = schema_census.team_policy_tables
    assert enforced, f"no table carries {_TEAM_POLICY!r} - the policy catalog read is broken, so this assertion would pass vacuously"
    declared_team = tenant_scope.team_tables()
    misdeclared = sorted(enforced - declared_team)
    assert not misdeclared, f"table(s) carry {_TEAM_POLICY!r} but are not declared 'team': {misdeclared}. Declare them 'team' (or change their policy in a reviewed migration)."


def test_team_table_count_matches_the_adr() -> None:
    """ADR 070 §1 pins 11 team tables; changing the split is a reviewed act."""
    team_tables = sorted(tenant_scope.team_tables())
    assert len(team_tables) == ADR_070_TEAM_TABLE_COUNT, f"ADR 070 §1 names {ADR_070_TEAM_TABLE_COUNT} team-scoped tables, the registry declares {len(team_tables)}: {team_tables}. If this is ADR 070 Phase 1B (the runs family flipping), update ADR_070_TEAM_TABLE_COUNT and the ADR in the same change."


# ---------------------------------------------------------------------------
# Ownership columns
# ---------------------------------------------------------------------------


def test_declared_ownership_columns_exist(schema_census: SchemaCensus) -> None:
    """Every entry that names an ownership column must name one that exists.

    Covers ``team`` (``owner_team_id``) and ``account`` (``account_id`` or the
    principal/parent column), so a rename in a migration cannot leave the
    registry pointing at a column that is gone.
    """
    named = {table: entry.owner_column for table, entry in tenant_scope.registry().items() if entry.owner_column is not None}
    assert named, "no registry entry names an ownership column - the team/account declarations are broken"
    failures = []
    for table, column in sorted(named.items()):
        available = schema_census.columns_of(table)
        if column not in available:
            failures.append(f"{table}: declares ownership column {column!r}, which does not exist (columns: {sorted(available)})")
    assert not failures, "declared ownership columns are not in the schema:\n" + "\n".join(failures)


def test_team_entries_declare_the_owner_team_column() -> None:
    """A ``team`` entry names ``owner_team_id``: that is the column ADR 070 §1 requires."""
    failures = []
    for table in sorted(tenant_scope.team_tables()):
        column = tenant_scope.ownership_column(table)
        if column != "owner_team_id":
            failures.append(f"{table}: declared team ownership column {column!r}, expected 'owner_team_id'")
    assert not failures, "team scope must be carried by owner_team_id (ADR 070 §1):\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# org_global: a written reason, every time
# ---------------------------------------------------------------------------


def test_org_global_entries_carry_a_real_justification() -> None:
    """No org-wide table may be excused with a placeholder.

    Checks a length floor and two placeholder tiers (exact answers and
    abbreviated markers) against the registry's own stored text — not against
    the dataclass validator, so weakening the validator cannot weaken this.
    """
    org_global = sorted(tenant_scope.org_global_tables())
    assert org_global, "no table is declared 'org_global' - the registry read is broken, so this assertion would pass vacuously"
    failures = []
    for table in org_global:
        text_value = tenant_scope.justification_of(table)
        normalised = " ".join(text_value.split()).lower().strip(" .,:;!-_")
        if normalised in _PLACEHOLDER_JUSTIFICATIONS:
            failures.append(f"{table}: justification {text_value!r} is a placeholder")
            continue
        if len(text_value.strip()) < _MIN_JUSTIFICATION_CHARS:
            failures.append(f"{table}: justification is {len(text_value.strip())} characters (< {_MIN_JUSTIFICATION_CHARS})")
            continue
        for marker in _PLACEHOLDER_MARKERS:
            if marker in normalised:
                failures.append(f"{table}: justification contains the placeholder marker {marker!r}")
                break
    assert not failures, "org_global entries without a real written reason (ADR 070 §1):\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# The two gates agree
# ---------------------------------------------------------------------------


def test_policy_gate_exemptions_are_never_declared_team_or_account() -> None:
    """A table with a documented widened policy arm cannot be declared narrowed.

    The policy gate excuses a table because its predicate carries an extra
    widening arm (pre-auth or global-asset), which is the opposite of a team or
    account boundary. Declaring one of those tables ``team``/``account`` would
    make the registry promise a boundary the database does not enforce.
    """
    exemptions = frozenset(_EXEMPTIONS)
    assert exemptions, "the policy gate's exemption list is empty - the agreement check would pass vacuously"
    narrowed = sorted(exemptions & (tenant_scope.team_tables() | tenant_scope.account_tables()))
    assert not narrowed, f"table(s) exempted by the policy gate are declared 'team' or 'account': {narrowed}. The two gates must agree (ADR 070 §2 / §6)."


def test_policy_gate_exemptions_are_declared_org_global() -> None:
    """Every exemption is a declared org_global table, with its reason on record."""
    exemptions = frozenset(_EXEMPTIONS)
    assert exemptions, "the policy gate's exemption list is empty - the agreement check would pass vacuously"
    org_global = tenant_scope.org_global_tables()
    missing = sorted(exemptions - org_global)
    assert not missing, f"table(s) exempted by the policy gate are not declared 'org_global': {missing}"


# ---------------------------------------------------------------------------
# The registry's own invariants (no database needed)
# ---------------------------------------------------------------------------


def test_scope_classes_are_exhaustive_and_disjoint() -> None:
    """Every declaration is in exactly one class and the classes partition the registry."""
    team = tenant_scope.team_tables()
    account = tenant_scope.account_tables()
    org_global = tenant_scope.org_global_tables()
    assert not (team & account), f"tables declared both team and account: {sorted(team & account)}"
    assert not (team & org_global), f"tables declared both team and org_global: {sorted(team & org_global)}"
    assert not (account & org_global), f"tables declared both account and org_global: {sorted(account & org_global)}"
    assert team | account | org_global == tenant_scope.all_tables(), "a declaration exists outside the three ADR 070 §1 classes"


def test_unknown_table_raises() -> None:
    """An undeclared table must fail loudly, not resolve to a default."""
    with pytest.raises(UnknownTenantTableError, match="no declared tenant scope"):
        tenant_scope.scope_of("definitely_not_a_table")


def test_unknown_table_error_is_a_key_error() -> None:
    """The lookup contract stays a ``KeyError`` so callers can catch it as one."""
    assert issubclass(UnknownTenantTableError, KeyError)
    with pytest.raises(KeyError):
        tenant_scope.scope_of("definitely_not_a_table")


def test_a_bare_string_scope_is_rejected() -> None:
    """A typo'd scope must fail at construction (the dataclass is the typo guard)."""
    with pytest.raises(TypeError, match="is not a TenantScope"):
        TableScope("pipelines", "team")  # type: ignore[arg-type]


def test_a_team_entry_needs_an_ownership_column() -> None:
    """``team`` without a column would enforce nothing; construction refuses it."""
    with pytest.raises(ValueError, match="must name the column"):
        TableScope("pipelines", TenantScope.TEAM)


def test_an_org_global_entry_needs_a_justification() -> None:
    """Org-wide without a reason is the silent default ADR 070 §1 removes."""
    with pytest.raises(ValueError, match="must carry a written justification"):
        TableScope("pipelines", TenantScope.ORG_GLOBAL)


def test_a_placeholder_justification_is_rejected() -> None:
    """The banned-phrase list bites, so a placeholder cannot be committed."""
    with pytest.raises(ValueError, match="placeholder"):
        TableScope("pipelines", TenantScope.ORG_GLOBAL, justification="n/a")


def test_a_too_short_justification_is_rejected() -> None:
    """A length floor rejects a shrug that is not an exact banned phrase."""
    with pytest.raises(ValueError, match="characters"):
        TableScope("pipelines", TenantScope.ORG_GLOBAL, justification="it is shared")


def test_a_narrowed_entry_rejects_a_justification() -> None:
    """Only an org_global entry carries a justification - no half-declared entries."""
    with pytest.raises(ValueError, match="only an org_global entry"):
        TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id", justification="Pipelines belong to a team, which is why this sentence exists at all.")


def test_a_duplicate_declaration_is_rejected() -> None:
    """Two declarations for one table must fail rather than silently pick a winner."""
    duplicate = (
        TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id"),
        TableScope("pipelines", TenantScope.ORG_GLOBAL, justification="A second declaration for the same table, which the registry must refuse outright."),
    )
    with pytest.raises(ValueError, match="declared more than once"):
        tenant_scope._validate_registry(duplicate)


def test_helpers_refuse_a_columnless_class() -> None:
    """``ownership_column`` and ``justification_of`` refuse the class that has no such field."""
    with pytest.raises(ValueError, match="no ownership column"):
        tenant_scope.ownership_column("runs")
    with pytest.raises(ValueError, match="carries no justification"):
        tenant_scope.justification_of("pipelines")


def test_tables_in_scope_rejects_a_bare_string() -> None:
    """The helper cannot be called with an untyped scope either."""
    with pytest.raises(TypeError, match="is not a TenantScope"):
        tenant_scope.tables_in_scope("team")  # type: ignore[arg-type]
