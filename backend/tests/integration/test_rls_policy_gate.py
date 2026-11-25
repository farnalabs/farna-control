"""ADR 070 §2/§6 — a database-level gate that cannot pass on a broken RLS policy.

Why this file exists
--------------------
``test_rls_isolation.py`` asserts that a policy **exists** with a given name. Every
one of the six fail-open / global-asset policies shipped today passes those
assertions, because "exists" says nothing about what the predicate *does*. ADR 070
§2 therefore requires enforcement to be **behavioural**, and the fast structural
half to be a real parse of the deparsed predicate rather than a ``.contains``
check.

Part 1 (structural) — every table in ``pg_class`` with ``relrowsecurity`` and an
``organisation_id`` column must have a policy whose **top-level** shape is::

    (organisation_id = current_setting('app.organisation_id')) AND (<rest>)

or the single-clause form ``organisation_id = current_setting(...)``, or — since
migration 0365 — the scoped pre-auth form::

    (organisation_id = current_setting('app.organisation_id')) OR
    (current_setting('app.bootstrap_context', true) = 'true')

A top-level ``OR`` with any arm that is neither the org equality nor an explicit
bootstrap grant is the fail-open shape (``... OR current_setting(...) IS NULL`` /
``... OR organisation_id IS NULL``) and fails. An org equality that is not one of
the top-level arms fails. The expression is tokenised (string / dollar-quoted /
quoted-identifier aware), balanced-paren stripped and split on top-level
``AND``/``OR``; no substring matching is used. Tables with RLS but no policy fail
unless they are on the checked-in exception list.

The four pre-auth tables (``oauth_clients``, ``oauth_consent_states``,
``org_memberships``, ``token_families``) were exceptions while their NULL arm
stood. Migration 0365 replaced that arm with the bootstrap grant, so they left the
exception list and are now pinned by ``_BOOTSTRAP_GRANT_TABLES``: the gate fails if
one of them regains *any* ``IS NULL`` arm
(``test_no_policy_regains_an_org_context_is_null_arm``), and
``test_global_asset_tables_keep_their_named_null_arm`` keeps the two deliberate
``organisation_id IS NULL`` global-asset arms named as such (ADR 070 §6).

Part 2 (behavioural) — for ``pipelines``, ``runs``, ``connector_instances``,
``model_backends`` and ``system_maps`` this seeds two organisations (two teams
each) and asserts, as a NON-superuser role so RLS actually applies, that an org-B
context returns zero org-A rows across the full GUC matrix (cross-org, cross-org +
admin, cross-org + execution context, org unset) and — for the team-scoped
tables — that a team-B member returns zero team-A rows inside the *same* org.
Every context carries a positive control so a policy that simply denies
everything cannot pass. The pre-auth bootstrap grant is proved behaviourally in
``test_rls_bootstrap_context.py``.

Scope note: ``runs`` is org-scoped today (ADR 070 Phase 1B flips the runs family
to the team predicate). It is therefore exercised across orgs only; the cross-team
assertion is deliberately not applied to it, and its current scope is pinned
explicitly so the expectation cannot silently invert.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# The checked-in exception list (ADR 070 §2 and §6)
# ---------------------------------------------------------------------------
# Every entry is an explicit, reviewed act: a deliberate reason plus the ADR
# clause that authorises it. An exemption is only valid while the listed table
# still has the *documented* fail-open shape — if the policy is tightened, the
# gate fails and the entry must be deleted (see
# ``test_exemptions_are_real_and_current``). No other table may be exempt.
@dataclass(frozen=True)
class Exemption:
    """One deliberate, reasoned exemption from the org-first invariant."""

    reason: str
    adr: str
    # Which arm this table is allowed to carry. Used to prove the exemption is
    # still describing reality rather than papering over an unrelated break.
    arm: str


_GLOBAL_ASSET = "global asset catalogue: NULL-organisation rows are built-in, shared assets readable by every organisation. The NULL arm is DELIBERATE and NAMED as the global catalogue (not a side effect); it is recorded in ADR 070 §6."

_EXEMPTIONS: dict[str, Exemption] = {
    # ── Global-asset shape: (... = current_org) OR (organisation_id IS NULL) ─
    "assistant_context_sources": Exemption(reason=_GLOBAL_ASSET, adr="ADR 070 §6 (global asset catalogue)", arm="organisation_id_is_null"),
    "assistant_skills": Exemption(reason=_GLOBAL_ASSET, adr="ADR 070 §6 (global asset catalogue)", arm="organisation_id_is_null"),
}

# ── The four pre-auth tables that LEFT the exception list in migration 0365 ──
# They used to carry ``(... = current_org) OR (current_org IS NULL)``, which the
# pool-checkout reset turned into an accidental god-mode ("no context" meant
# "every row"). 0365 replaced that arm with an explicit, transaction-scoped
# ``app.bootstrap_context = 'true'`` grant, so they are no longer exempt — they
# are pinned here instead, and the gate fails if any of them regains an IS NULL
# arm (``test_no_policy_regains_an_org_context_is_null_arm``).
#
#   oauth_clients         — pre-auth authorize lookup by client_id, before any org exists
#   oauth_consent_states  — anonymous consent-context read by state, before any org exists
#   org_memberships       — login/SSO/refresh role resolution, pinned to (account, org)
#   token_families        — refresh rotation + credential-change revocation, pinned to (family, account)
_BOOTSTRAP_GRANT_TABLES: dict[str, str] = {
    "oauth_clients": "pre-auth OAuth client lookup by client_id (authorize / token / refresh legs)",
    "oauth_consent_states": "anonymous consent-context read by state (the org is a column of the row)",
    "org_memberships": "login/SSO/refresh live-role resolution, pinned to (account, org)",
    "token_families": "refresh-token rotation and credential-change revocation, pinned to (family_id, account_id)",
}

# The tenant-table census is deliberate: if a table drops out of RLS (or a new
# tenant table appears without one), the count changes and this gate must be
# updated as a reviewed act rather than silently drifting. Currently 102 public
# tables carry both ``relrowsecurity`` and ``organisation_id``. Five further
# tables carry an ``organisation_id`` column but do NOT enable RLS and are
# therefore outside ADR 070's invariant by definition: the three runtime-managed
# LangGraph checkpoint tables, ``invitations`` (consumed before any principal
# exists) and ``run_node_outputs_quarantine`` (ops-only remediation evidence).
_EXPECTED_TENANT_TABLE_COUNT = 102

# The exception list is now only the two deliberate global-asset catalogues.
_REQUIRED_EXEMPTIONS: dict[str, str] = {
    "assistant_context_sources": "organisation_id_is_null",
    "assistant_skills": "organisation_id_is_null",
}


# ---------------------------------------------------------------------------
# Parse primitives — balanced-parenthesis, literal-aware top-level structure
# ---------------------------------------------------------------------------
# PostgreSQL's ``pg_get_expr`` output is fully parenthesised for binary
# operators, so the top-level operator of a predicate is unambiguous once
# parentheses and quoted spans are handled. We mask literal spans (preserving
# character offsets) so a keyword inside ``'... OR ...'`` can never be mistaken
# for an operator, then scan the masked copy for depth-0 ``AND`` / ``OR``.


_DOLLAR_QUOTE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_ORG_GUC = re.compile(r"current_setting\s*\(\s*'app\.organisation_id'")
_BOOTSTRAP_GUC = re.compile(r"current_setting\s*\(\s*'app\.bootstrap_context'")
_LITERAL_TRUE = re.compile(r"^'true'(?:::text)?$")
_IS_NULL = re.compile(r"(?<![A-Za-z0-9_$])IS\s+NULL(?![A-Za-z0-9_$])", re.IGNORECASE)


def _mask_literals(expr: str) -> str:
    """Return *expr* with quoted spans replaced by ``_`` (same length, same offsets)."""
    masked = list(expr)
    length = len(expr)
    index = 0
    while index < length:
        char = expr[index]
        if char == "'":
            cursor = index + 1
            while cursor < length:
                if expr[cursor] == "'":
                    # A doubled quote is an escaped quote, not a terminator.
                    if cursor + 1 < length and expr[cursor + 1] == "'":
                        cursor += 2
                        continue
                    break
                cursor += 1
            end = min(cursor + 1, length)
            for position in range(index, end):
                masked[position] = "_"
            index = end
            continue
        if char == '"':
            cursor = expr.find('"', index + 1)
            end = length if cursor == -1 else cursor + 1
            for position in range(index, end):
                masked[position] = "_"
            index = end
            continue
        if char == "$":
            match = _DOLLAR_QUOTE.match(expr, index)
            if match is not None:
                tag = match.group(0)
                closing = expr.find(tag, match.end())
                end = length if closing == -1 else closing + len(tag)
                for position in range(index, end):
                    masked[position] = "_"
                index = end
                continue
        index += 1
    return "".join(masked)


def _matching_paren(masked: str, open_index: int) -> int:
    """Index of the ``)`` matching the ``(`` at *open_index*, or -1."""
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == "(":
            depth += 1
        elif masked[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return -1


def _unwrap(expr: str) -> str:
    """Strip balanced outer parentheses that wrap the whole expression."""
    inner = expr.strip()
    while inner.startswith("("):
        close = _matching_paren(_mask_literals(inner), 0)
        if close != len(inner) - 1:
            break
        inner = inner[1:close].strip()
    return inner


def _depth_at(masked: str, index: int) -> int:
    return masked.count("(", 0, index) - masked.count(")", 0, index)


def _top_level_operators(expr: str) -> list[tuple[int, str]]:
    """Return ``(offset, 'AND'|'OR')`` for every depth-0 boolean operator."""
    masked = _mask_literals(expr)
    operators: list[tuple[int, str]] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_$])(AND|OR)(?![A-Za-z0-9_$])", masked):
        if _depth_at(masked, match.start()) == 0:
            operators.append((match.start(), match.group(1)))
    return operators


def _split_on_operator(expr: str, operator: str) -> tuple[str, ...]:
    """Split *expr* on every depth-0 occurrence of *operator*."""
    parts: list[str] = []
    cursor = 0
    for offset, found in _top_level_operators(expr):
        if found != operator:
            continue
        parts.append(expr[cursor:offset].strip())
        cursor = offset + len(operator)
    parts.append(expr[cursor:].strip())
    return tuple(parts)


def _bare_column(expr: str) -> str | None:
    """Return the unqualified identifier *expr* names, or None if it is not one."""
    inner = _unwrap(expr).strip('"')
    if "." in inner:
        inner = inner.rsplit(".", 1)[-1].strip('"')
    return inner if _BARE_IDENTIFIER.match(inner) else None


def _top_level_equals(expr: str) -> int:
    """Offset of the single depth-0 ``=`` in *expr*, or -1."""
    masked = _mask_literals(expr)
    offsets = [index for index, char in enumerate(masked) if char == "=" and _depth_at(masked, index) == 0 and (index == 0 or masked[index - 1] not in "<>=!") and (index + 1 >= len(masked) or masked[index + 1] != "=")]
    return offsets[0] if len(offsets) == 1 else -1


def is_org_equality(expr: str) -> bool:
    """True when *expr* is ``organisation_id = <app.organisation_id GUC>``.

    Either operand order is accepted (PostgreSQL emits the column on the left,
    but the invariant is about the comparison, not its spelling). The other side
    must read the org GUC, optionally wrapped in ``NULLIF``/a cast.
    """
    inner = _unwrap(expr)
    equals_at = _top_level_equals(inner)
    if equals_at == -1:
        return False
    left = inner[:equals_at].strip()
    right = inner[equals_at + 1 :].strip()
    return any(_bare_column(column_side) == "organisation_id" and _ORG_GUC.search(guc_side) is not None for column_side, guc_side in ((left, right), (right, left)))


def is_bootstrap_grant(expr: str) -> bool:
    """True when *expr* is ``current_setting('app.bootstrap_context', true) = 'true'``.

    This is the ONLY arm allowed to widen a pre-auth policy (ADR 070 §2,
    migration 0365). It is an equality against the literal ``'true'``, so an
    unset GUC (NULL) or the post-checkout reset value ``''`` fails closed —
    unlike an ``IS NULL`` arm, which widens precisely when no context exists.
    """
    inner = _unwrap(expr)
    if is_org_equality(inner):
        return False
    equals_at = _top_level_equals(inner)
    if equals_at == -1:
        return False
    left = inner[:equals_at].strip()
    right = inner[equals_at + 1 :].strip()
    return any(_BOOTSTRAP_GUC.search(guc_side) is not None and _LITERAL_TRUE.match(literal_side) is not None for guc_side, literal_side in ((left, right), (right, left)))


def has_is_null_arm(expr: str) -> bool:
    """True when *expr* carries an ``IS NULL`` test (literal-aware)."""
    return _IS_NULL.search(_mask_literals(_unwrap(expr))) is not None


@dataclass(frozen=True)
class PredicateShape:
    """The top-level structure of one policy predicate."""

    operator: str  # "AND" | "OR" | "SINGLE"
    conjuncts: tuple[str, ...]

    @property
    def org_first(self) -> bool:
        if self.operator == "OR":
            return False
        return is_org_equality(self.conjuncts[0])

    @property
    def bootstrap_grant(self) -> bool:
        """The scoped pre-auth form: org equality OR explicit bootstrap grant.

        Everything that is not the org equality must be a bootstrap-grant arm —
        one stray arm (an ``IS NULL``, a ``TRUE``, anything else) and this is
        False, so the shape cannot be smuggled in alongside a widened arm.
        """
        if self.operator != "OR":
            return False
        if not any(is_org_equality(conjunct) for conjunct in self.conjuncts):
            return False
        others = [conjunct for conjunct in self.conjuncts if not is_org_equality(conjunct)]
        return bool(others) and all(is_bootstrap_grant(conjunct) for conjunct in others)

    def is_acceptable(self) -> bool:
        return self.org_first or self.bootstrap_grant

    def describe(self) -> str:
        if self.operator == "OR":
            return "top-level OR with an arm that is neither the org equality nor an explicit bootstrap grant (fail-open shape)"
        if not self.org_first:
            return f"org equality is not the first conjunct: first is {self.conjuncts[0]!r}"
        return "ok"


def parse_predicate(expr: str) -> PredicateShape:
    """Parse the top-level boolean structure of a deparsed policy predicate."""
    inner = _unwrap(expr)
    operators = _top_level_operators(inner)
    if any(found == "OR" for _, found in operators):
        return PredicateShape(operator="OR", conjuncts=_split_on_operator(inner, "OR"))
    if operators:
        return PredicateShape(operator="AND", conjuncts=_split_on_operator(inner, "AND"))
    return PredicateShape(operator="SINGLE", conjuncts=(inner,))


# ---------------------------------------------------------------------------
# Structural gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRow:
    name: str
    command: str
    qual: str | None
    with_check: str | None

    def predicates(self) -> tuple[str, ...]:
        """Every non-null predicate this policy applies (USING and WITH CHECK)."""
        return tuple(p for p in (self.qual, self.with_check) if p is not None)


@dataclass(frozen=True)
class TablePolicies:
    table: str
    policies: tuple[PolicyRow, ...]


async def _load_tenant_policy_census(db_engine: AsyncEngine) -> dict[str, TablePolicies]:
    """Every RLS-enabled public table carrying an ``organisation_id`` column."""
    statement = text(
        """
        SELECT c.relname AS table_name,
               p.polname,
               p.polcmd,
               pg_get_expr(p.polqual, p.polrelid) AS qual,
               pg_get_expr(p.polwithcheck, p.polrelid) AS with_check
          FROM pg_class c
          LEFT JOIN pg_policy p ON p.polrelid = c.oid
         WHERE c.relkind IN ('r', 'p')
           AND c.relnamespace = 'public'::regnamespace
           AND c.relrowsecurity
           AND EXISTS (
                 SELECT 1 FROM pg_attribute a
                  WHERE a.attrelid = c.oid
                    AND a.attname = 'organisation_id'
                    AND a.attnum > 0
                    AND NOT a.attisdropped
               )
         ORDER BY c.relname, p.polname
        """
    )
    async with db_engine.connect() as conn:
        rows = (await conn.execute(statement)).fetchall()

    census: dict[str, list[PolicyRow]] = {}
    for table_name, policy_name, command, qual, with_check in rows:
        bucket = census.setdefault(table_name, [])
        if policy_name is None:  # LEFT JOIN: RLS enabled, no policy at all
            continue
        bucket.append(
            PolicyRow(
                name=policy_name,
                command=command.decode() if isinstance(command, bytes) else str(command),
                qual=qual,
                with_check=with_check,
            )
        )
    return {table: TablePolicies(table=table, policies=tuple(policies)) for table, policies in census.items()}


@pytest_asyncio.fixture(scope="session")
async def tenant_policy_census(db_engine: AsyncEngine) -> dict[str, TablePolicies]:
    """Session-cached census so the 102-table sweep queries the catalog once."""
    return await _load_tenant_policy_census(db_engine)


def _policy_failures(policies: TablePolicies) -> list[str]:
    """Every way *policies* violates the org-first invariant, as human text."""
    if not policies.policies:
        return [f"{policies.table}: RLS is enabled but the table has no policy"]
    failures: list[str] = []
    for policy in policies.policies:
        predicates = policy.predicates()
        if not predicates:
            failures.append(f"{policies.table}.{policy.name}: policy has no predicate (unconditional allow)")
            continue
        for predicate in predicates:
            shape = parse_predicate(predicate)
            if not shape.is_acceptable():
                failures.append(f"{policies.table}.{policy.name} ({policy.command}): {shape.describe()} — {predicate}")
    return failures


def test_tenant_table_census_is_complete(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """The gate must sweep every RLS tenant table, and the population is pinned.

    The count is a deliberate baseline (ADR 070 §2: "today's state is
    enumerable, so baseline it and fail"). If it changes, this test fails and the
    reviewer must decide whether the new/changed table is correctly scoped —
    that makes "a table quietly lost RLS" impossible to miss.
    """
    assert len(tenant_policy_census) == _EXPECTED_TENANT_TABLE_COUNT, f"expected {_EXPECTED_TENANT_TABLE_COUNT} RLS-enabled tables with an organisation_id column, found {len(tenant_policy_census)}: {sorted(tenant_policy_census)}"
    stale = sorted(set(_EXEMPTIONS) - set(tenant_policy_census))
    assert not stale, f"exception list names tables that are not RLS tenant tables: {stale}"


def test_every_tenant_table_policy_is_org_first(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """Every table in scope must shape its policy as ``org equality [AND ...]``.

    Exempt tables are checked separately (``test_exemptions_are_real_and_current``)
    so they cannot hide an unrelated break.
    """
    failures: list[str] = []
    for table, policies in sorted(tenant_policy_census.items()):
        if table in _EXEMPTIONS:
            continue
        failures.extend(_policy_failures(policies))
    assert not failures, "RLS policy shape gate failed:\n" + "\n".join(failures)


def _widened_arm(predicate: str) -> str | None:
    """Which widening arm a top-level-OR predicate carries, or None.

    ``bootstrap_grant`` is a named, scoped widening (migration 0365) and is
    legitimate on ``_BOOTSTRAP_GRANT_TABLES``; the two ``*_is_null`` arms are the
    fail-open shapes this gate exists to keep out.
    """
    if parse_predicate(predicate).operator != "OR":
        return None
    tail = _split_on_operator(_unwrap(predicate), "OR")[-1]
    if has_is_null_arm(tail):
        return "org_context_is_null" if _ORG_GUC.search(tail) else "organisation_id_is_null"
    if is_bootstrap_grant(tail):
        return "bootstrap_grant"
    return None


def test_exemptions_are_real_and_current(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """Each exemption must still describe the shape it is excused for.

    Guards both directions: an entry with no reason/ADR is rejected, and an entry
    whose policy has since been tightened is rejected so the list can only shrink
    as Phase 0 lands its scoped bootstrap reads. This is what stops the exception
    list becoming a permanent blanket.
    """
    problems: list[str] = []
    for table, exemption in sorted(_EXEMPTIONS.items()):
        if not exemption.reason.strip() or not exemption.adr.strip():
            problems.append(f"{table}: exemption lacks a reason or ADR reference")
            continue
        policies = tenant_policy_census.get(table)
        if policies is None:
            problems.append(f"{table}: exempted but not an RLS tenant table")
            continue
        found = {_widened_arm(predicate) for policy in policies.policies for predicate in policy.predicates()}
        if not found:
            problems.append(f"{table}: exempted but has no policy predicate at all")
        elif exemption.arm not in found:
            problems.append(f"{table}: exemption arm {exemption.arm!r} is no longer present (found {sorted(arm for arm in found if arm)}); the exemption is stale — remove it")
    assert not problems, "exception list is out of date:\n" + "\n".join(problems)


def test_global_asset_tables_keep_their_named_null_arm(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """The two global-asset catalogues must still be the ONLY ``IS NULL`` widenings.

    ADR 070 §6 names them as an intentional global catalogue — a NULL-organisation
    row is a built-in asset every org can read. Keeping the expectation explicit
    stops the exemption from silently describing a different table, and stops a
    third table acquiring the same arm without a reviewed ADR entry.
    """
    offenders: dict[str, list[str]] = {}
    for table, policies in sorted(tenant_policy_census.items()):
        arms = {_widened_arm(predicate) for policy in policies.policies for predicate in policy.predicates()}
        if "organisation_id_is_null" in arms and table not in _REQUIRED_EXEMPTIONS:
            offenders[table] = sorted(arm for arm in arms if arm)
    assert not offenders, f"only the named global-asset catalogue may carry an organisation_id IS NULL arm; found {offenders}"


def test_exception_list_covers_the_global_asset_pair() -> None:
    """The two ADR §6 tables must both be enumerated, with the documented arm."""
    missing = sorted(set(_REQUIRED_EXEMPTIONS) - set(_EXEMPTIONS))
    assert not missing, f"exception list is missing the ADR-named global-asset tables: {missing}"
    wrong = {table: (_EXEMPTIONS[table].arm, arm) for table, arm in _REQUIRED_EXEMPTIONS.items() if table in _EXEMPTIONS and _EXEMPTIONS[table].arm != arm}
    assert not wrong, f"exception list records the wrong arm for: {wrong}"


def test_no_policy_regains_an_org_context_is_null_arm(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """No policy anywhere may widen on ``current_setting('app.organisation_id') IS NULL``.

    The pool-checkout reset writes ``app.organisation_id = ''`` and
    ``NULLIF('','')`` is NULL, so that arm means "any unbound session reads every
    row". Migration 0365 removed the last four (and with them the four exception
    entries); this test is the tripwire that fails if any of them — or any other
    table — regains it, whatever the exemption list says.

    ``test_every_tenant_table_policy_is_org_first`` already rejects the shape by
    construction (the arm is not an org equality and not a bootstrap grant); this
    one names the defect precisely so a regression reports the class, not just
    "top-level OR".
    """
    offenders: dict[str, list[str]] = {}
    for table, policies in sorted(tenant_policy_census.items()):
        for policy in policies.policies:
            for predicate in policy.predicates():
                if _widened_arm(predicate) == "org_context_is_null":
                    offenders.setdefault(table, []).append(f"{policy.name}: {predicate}")
    assert not offenders, "fail-open org-context arm regained (ADR 070 §2 forbids it; use app.bootstrap_context instead):\n" + "\n".join(f"{table}: {rows}" for table, rows in offenders.items())


def test_bootstrap_grant_tables_use_the_scoped_arm(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """The four pre-auth tables must hold the scoped bootstrap grant, and no others.

    Both directions matter. If one of the four drifts back to a fail-open arm the
    gate says so; if a table that is NOT on the reviewed list acquires the grant,
    a pre-auth read has been widened without this test being updated.
    """
    problems: list[str] = []
    carrying: set[str] = set()
    for table, policies in sorted(tenant_policy_census.items()):
        arms = {_widened_arm(predicate) for policy in policies.policies for predicate in policy.predicates()}
        if "bootstrap_grant" in arms:
            carrying.add(table)
        expected = table in _BOOTSTRAP_GRANT_TABLES
        actual = "bootstrap_grant" in arms
        if expected and not actual:
            problems.append(f"{table}: is on the reviewed pre-auth list but carries no bootstrap grant (arms: {sorted(arm for arm in arms if arm)}) — {_BOOTSTRAP_GRANT_TABLES[table]}")
        if actual and not expected:
            problems.append(f"{table}: carries an app.bootstrap_context grant but is not on the reviewed pre-auth list — add it with a reason, or remove the arm")
    assert not problems, "bootstrap-grant register is out of date:\n" + "\n".join(problems)
    assert carrying == set(_BOOTSTRAP_GRANT_TABLES), f"expected exactly {sorted(_BOOTSTRAP_GRANT_TABLES)}, found {sorted(carrying)}"


# ---------------------------------------------------------------------------
# Parser self-tests — the parse must not be vacuous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "expected_operator", "expected_org_first"),
    [
        # The shape 91 org-scoped tables use today.
        ("(organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)", "SINGLE", True),
        # The team predicate: org equality is the FIRST conjunct of a top-level AND.
        (
            "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (((visibility)::text = 'org'::text) OR (owner_team_id IS NULL)))",
            "AND",
            True,
        ),
        # The fail-open shapes shipped today.
        (
            "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL))",
            "OR",
            False,
        ),
        ("((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) OR (organisation_id IS NULL))", "OR", False),
        # A keyword inside a string literal must NOT be read as a top-level operator.
        (
            "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid) AND (name = 'A OR B') AND (owner_team_id IS NOT NULL))",
            "AND",
            True,
        ),
        # Org check present but not first — must fail.
        ("(((visibility)::text = 'org'::text) AND (organisation_id = current_setting('app.organisation_id')))", "AND", False),
        # No org gate at all (parent-subquery form) — must fail.
        ("(install_id IN (SELECT collection_install.install_id FROM collection_install))", "SINGLE", False),
        # A non-null-safe org GUC reference still counts as the org equality (it
        # fails closed by raising rather than by returning rows).
        ("(organisation_id = (current_setting('app.organisation_id'::text))::uuid)", "SINGLE", True),
    ],
    ids=[
        "single-org-equality",
        "and-team-predicate",
        "or-org-context-is-null",
        "or-organisation-id-is-null",
        "keyword-inside-string-literal",
        "org-check-not-first",
        "no-org-gate-subquery",
        "non-null-safe-org-guc",
    ],
)
def test_parser_reads_top_level_structure(predicate: str, expected_operator: str, expected_org_first: bool) -> None:
    """Pin the parser's reading of every shape that exists in the schema."""
    shape = parse_predicate(predicate)
    assert shape.operator == expected_operator
    assert shape.org_first is expected_org_first


_ORG_ARM = "(organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"
_BOOTSTRAP_ARM = "(current_setting('app.bootstrap_context'::text, true) = 'true'::text)"


@pytest.mark.parametrize(
    ("predicate", "acceptable", "bootstrap_grant"),
    [
        # The 0365 scoped pre-auth shape — the ONLY widening this gate allows.
        (f"({_ORG_ARM} OR {_BOOTSTRAP_ARM})", True, True),
        # Operand order is irrelevant to the invariant.
        (f"({_ORG_ARM} OR ('true'::text = current_setting('app.bootstrap_context'::text, true)))", True, True),
        # The fail-open shapes must still fail, including when the bootstrap arm
        # is ALSO present — a second widening arm cannot be smuggled in.
        (
            f"({_ORG_ARM} OR (NULLIF(current_setting('app.organisation_id'::text, true), ''::text) IS NULL))",
            False,
            False,
        ),
        (f"({_ORG_ARM} OR (organisation_id IS NULL))", False, False),
        (f"({_ORG_ARM} OR {_BOOTSTRAP_ARM} OR (organisation_id IS NULL))", False, False),
        # A bootstrap-only policy is not org-gated at all — must fail.
        (f"(({_BOOTSTRAP_ARM}))", False, False),
        # An always-true arm is not a bootstrap grant.
        (f"({_ORG_ARM} OR true)", False, False),
        # Comparing the GUC to something other than the literal 'true' fails closed
        # but is not the reviewed shape; it must not pass the gate.
        (f"({_ORG_ARM} OR (current_setting('app.bootstrap_context'::text, true) IS NOT NULL))", False, False),
        # The plain org-scoped form stays acceptable and is not a bootstrap grant.
        (_ORG_ARM, True, False),
    ],
    ids=[
        "scoped-bootstrap-grant",
        "scoped-bootstrap-grant-reversed-operands",
        "org-context-is-null-arm",
        "organisation-id-is-null-arm",
        "bootstrap-plus-null-arm",
        "bootstrap-only-no-org-gate",
        "always-true-arm",
        "bootstrap-is-not-null-arm",
        "plain-org-equality",
    ],
)
def test_parser_accepts_only_the_scoped_bootstrap_widening(predicate: str, acceptable: bool, bootstrap_grant: bool) -> None:
    """Pin the accept-set: org equality, or org equality OR a literal-'true' bootstrap grant."""
    shape = parse_predicate(predicate)
    assert shape.is_acceptable() is acceptable
    assert shape.bootstrap_grant is bootstrap_grant


# ---------------------------------------------------------------------------
# Part 2 — behavioural proof on a representative subset
# ---------------------------------------------------------------------------
# Seeding reuses the patterns already established in test_rls_isolation.py:
# superuser inserts for the fixture rows (RLS is bypassed for setup) and a
# ``SET LOCAL ROLE`` non-superuser connection for every assertion, so RLS
# actually applies. No new seeding framework is introduced.

# Tables the behavioural matrix covers, and which of them carry the team
# predicate today. ``runs`` is org-scoped until ADR 070 Phase 1B, so it is
# checked structurally for its scope and behaviourally across orgs only.
_BEHAVIOURAL_TABLES: tuple[str, ...] = ("pipelines", "runs", "connector_instances", "model_backends", "system_maps")
_TEAM_SCOPED_TABLES: frozenset[str] = frozenset({"pipelines", "connector_instances", "model_backends", "system_maps"})

_ROW_SELECTS: dict[str, str] = {
    "pipelines": "SELECT id::text FROM pipelines WHERE id = ANY(:ids)",
    "runs": "SELECT id::text FROM runs WHERE id = ANY(:ids)",
    "connector_instances": "SELECT id::text FROM connector_instances WHERE id = ANY(:ids)",
    "model_backends": "SELECT id::text FROM model_backends WHERE id = ANY(:ids)",
    "system_maps": "SELECT id::text FROM system_maps WHERE id = ANY(:ids)",
}

_ORGANISATION_INSERT = "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
_ACCOUNT_INSERT = "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) VALUES (:id, :email, :name, 'local', true, 'hash')"
_ORG_MEMBERSHIP_INSERT = "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'operator')"
_TEAM_INSERT = "INSERT INTO teams (id, organisation_id, name, account_id, notification_endpoints, settings) VALUES (:id, :oid, :name, :aid, '[]'::jsonb, '{}'::jsonb)"
_TEAM_MEMBERSHIP_INSERT = "INSERT INTO team_memberships (id, organisation_id, team_id, account_id, role) VALUES (:id, :oid, :tid, :aid, 'operator')"
_PIPELINE_INSERT = "INSERT INTO pipelines (id, organisation_id, name, account_id, visibility, owner_team_id) VALUES (:id, :oid, :name, :aid, 'team', :tid)"
_SNAPSHOT_INSERT = (
    "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
    "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
    "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
    "VALUES (:id, :pid, :oid, 1, '{}'::json, '[]'::json, '[]'::json, '[]'::json, '[]'::json, '{}'::json, '{}'::json)"
)
_RUN_INSERT = "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, status, run_number, input_hash, langgraph_thread_id, owner_team_id) VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', :number, :hash, :thread, :tid)"
_CONNECTOR_INSERT = "INSERT INTO connector_instances (id, organisation_id, name, connector_type_id, account_id, visibility, owner_team_id, credentials_ciphertext, config_json, allowed_operations) VALUES (:id, :oid, :name, 'generic_http', :aid, 'team', :tid, :secret, '{}'::jsonb, '[]'::jsonb)"
_MODEL_BACKEND_INSERT = (
    "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, model_id, default_params, visibility, owner_team_id, credentials_ciphertext, status, account_id) VALUES (:id, :oid, :name, :name, 'opencode', 'test-model', '{}'::jsonb, 'team', :tid, :secret, 'active', :aid)"
)
_SYSTEM_MAP_INSERT = "INSERT INTO system_maps (id, organisation_id, name, account_id, visibility, owner_team_id) VALUES (:id, :oid, :name, :aid, 'team', :tid)"


@dataclass(frozen=True)
class TenantSeed:
    """Two orgs, two teams each, and one team-private row per team per table."""

    org_a: uuid.UUID
    org_b: uuid.UUID
    account_a1: uuid.UUID
    account_a2: uuid.UUID
    account_b1: uuid.UUID
    account_b2: uuid.UUID
    team_a1: uuid.UUID
    team_a2: uuid.UUID
    team_b1: uuid.UUID
    team_b2: uuid.UUID
    rows: dict[str, dict[str, uuid.UUID]]  # table -> {"a1","a2","b1","b2"}

    def ids(self, table: str) -> list[uuid.UUID]:
        return list(self.rows[table].values())


async def _seed_tenant_world(db_engine: AsyncEngine) -> TenantSeed:
    ids = {name: uuid.uuid4() for name in ("org_a", "org_b", "account_a1", "account_a2", "account_b1", "account_b2", "team_a1", "team_a2", "team_b1", "team_b2")}
    rows: dict[str, dict[str, uuid.UUID]] = {table: {alias: uuid.uuid4() for alias in ("a1", "a2", "b1", "b2")} for table in _BEHAVIOURAL_TABLES}
    snapshots = {alias: uuid.uuid4() for alias in ("a1", "a2", "b1", "b2")}
    slug = f"rls-gate-{ids['org_a'].hex[:10]}"

    async with db_engine.connect() as conn, conn.begin():
        for org_key, label in (("org_a", "A"), ("org_b", "B")):
            await conn.execute(text(_ORGANISATION_INSERT), {"id": str(ids[org_key]), "name": f"RLS Gate Org {label}", "slug": f"{slug}-{label.lower()}"})
        for account_key, org_key in (("account_a1", "org_a"), ("account_a2", "org_a"), ("account_b1", "org_b"), ("account_b2", "org_b")):
            await conn.execute(text(_ACCOUNT_INSERT), {"id": str(ids[account_key]), "email": f"{slug}-{account_key}@example.com", "name": f"{slug}-{account_key}"})
            await conn.execute(text(_ORG_MEMBERSHIP_INSERT), {"id": str(uuid.uuid4()), "aid": str(ids[account_key]), "oid": str(ids[org_key])})
        for team_key, org_key, owner_key in (("team_a1", "org_a", "account_a1"), ("team_a2", "org_a", "account_a2"), ("team_b1", "org_b", "account_b1"), ("team_b2", "org_b", "account_b2")):
            await conn.execute(text(_TEAM_INSERT), {"id": str(ids[team_key]), "oid": str(ids[org_key]), "name": f"{slug}-{team_key}", "aid": str(ids[owner_key])})
            await conn.execute(text(_TEAM_MEMBERSHIP_INSERT), {"id": str(uuid.uuid4()), "oid": str(ids[org_key]), "tid": str(ids[team_key]), "aid": str(ids[owner_key])})

        # ``uq_runs_org_run_number`` makes (organisation_id, run_number) unique,
        # so the two runs inside one org must use distinct numbers.
        for alias, org_key, team_key, owner_key, run_number in (
            ("a1", "org_a", "team_a1", "account_a1", 1),
            ("a2", "org_a", "team_a2", "account_a2", 2),
            ("b1", "org_b", "team_b1", "account_b1", 1),
            ("b2", "org_b", "team_b2", "account_b2", 2),
        ):
            org_id = str(ids[org_key])
            team_id = str(ids[team_key])
            account_id = str(ids[owner_key])
            await conn.execute(text(_PIPELINE_INSERT), {"id": str(rows["pipelines"][alias]), "oid": org_id, "name": f"{slug}-pipeline-{alias}", "aid": account_id, "tid": team_id})
            await conn.execute(text(_SNAPSHOT_INSERT), {"id": str(snapshots[alias]), "pid": str(rows["pipelines"][alias]), "oid": org_id})
            await conn.execute(
                text(_RUN_INSERT),
                {"id": str(rows["runs"][alias]), "oid": org_id, "pid": str(rows["pipelines"][alias]), "sid": str(snapshots[alias]), "number": run_number, "hash": "e" * 64, "thread": f"{slug}-run-{alias}", "tid": team_id},
            )
            await conn.execute(
                text(_CONNECTOR_INSERT),
                {"id": str(rows["connector_instances"][alias]), "oid": org_id, "name": f"{slug}-connector-{alias}", "aid": account_id, "tid": team_id, "secret": b"\x00"},
            )
            await conn.execute(
                text(_MODEL_BACKEND_INSERT),
                {"id": str(rows["model_backends"][alias]), "oid": org_id, "name": f"{slug}-backend-{alias}", "tid": team_id, "aid": account_id, "secret": b"\x00"},
            )
            await conn.execute(text(_SYSTEM_MAP_INSERT), {"id": str(rows["system_maps"][alias]), "oid": org_id, "name": f"{slug}-map-{alias}", "aid": account_id, "tid": team_id})

    return TenantSeed(
        org_a=ids["org_a"],
        org_b=ids["org_b"],
        account_a1=ids["account_a1"],
        account_a2=ids["account_a2"],
        account_b1=ids["account_b1"],
        account_b2=ids["account_b2"],
        team_a1=ids["team_a1"],
        team_a2=ids["team_a2"],
        team_b1=ids["team_b1"],
        team_b2=ids["team_b2"],
        rows=rows,
    )


async def _cleanup_tenant_world(db_engine: AsyncEngine, seed: TenantSeed) -> None:
    """Delete the seeded world as superuser, in FK-dependency order."""
    org_ids = [str(seed.org_a), str(seed.org_b)]
    account_ids = [str(seed.account_a1), str(seed.account_a2), str(seed.account_b1), str(seed.account_b2)]
    async with db_engine.connect() as conn, conn.begin():
        for statement in (
            "DELETE FROM runs WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM pipeline_snapshots WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM pipelines WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM connector_instances WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM model_backends WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM system_maps WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM team_memberships WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM teams WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM org_memberships WHERE organisation_id = ANY(:orgs)",
        ):
            await conn.execute(text(statement), {"orgs": org_ids})
        await conn.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})
        await conn.execute(text("DELETE FROM organisations WHERE id = ANY(:ids)"), {"ids": org_ids})


@pytest_asyncio.fixture(scope="session")
async def rls_tenant_seed(db_engine: AsyncEngine) -> TenantSeed:
    """Session-scoped tenant world; every behavioural test reads it, none mutate it."""
    seed = await _seed_tenant_world(db_engine)
    try:
        yield seed
    finally:
        await _cleanup_tenant_world(db_engine, seed)


@dataclass(frozen=True)
class RlsContext:
    """One GUC combination to evaluate a policy under."""

    label: str
    org: uuid.UUID | None
    user_id: uuid.UUID | None = None
    org_role: str | None = None
    execution_context: bool = False


async def _visible_row_ids(db_engine: AsyncEngine, role: str, table: str, ids: list[uuid.UUID], context: RlsContext) -> set[str]:
    """Row ids *table* exposes to a NON-superuser connection under *context*."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
        if context.org is not None:
            await conn.execute(text("SELECT set_config('app.organisation_id', :v, true)"), {"v": str(context.org)})
        if context.user_id is not None:
            await conn.execute(text("SELECT set_config('app.user_id', :v, true)"), {"v": str(context.user_id)})
        if context.org_role is not None:
            await conn.execute(text("SELECT set_config('app.org_role', :v, true)"), {"v": context.org_role})
        if context.execution_context:
            await conn.execute(text("SELECT set_config('app.execution_context', 'true', true)"))
        rows = (await conn.execute(text(_ROW_SELECTS[table]), {"ids": [str(row_id) for row_id in ids]})).fetchall()
    return {row[0] for row in rows}


def _org_contexts(seed: TenantSeed) -> dict[str, RlsContext]:
    """The required GUC matrix; the org-unset context sets no org GUC at all."""
    return {
        "cross-org": RlsContext(label="cross-org", org=seed.org_b),
        "cross-org+admin": RlsContext(label="cross-org+admin", org=seed.org_b, org_role="admin"),
        "cross-org+execution-context": RlsContext(label="cross-org+execution-context", org=seed.org_b, execution_context=True),
        "org-unset": RlsContext(label="org-unset", org=None),
    }


def test_behavioural_subset_scope_metadata_is_as_expected(tenant_policy_census: dict[str, TablePolicies]) -> None:
    """Pin which behavioural tables carry the team predicate today.

    Without this, a Phase 1B flip (runs becoming team-scoped) would change what
    the matrix below *means* while still passing. Making the expectation explicit
    keeps the two in step.
    """
    for table in _TEAM_SCOPED_TABLES:
        names = {policy.name for policy in tenant_policy_census[table].policies}
        assert names == {"rls_team_isolation"}, f"{table}: expected only rls_team_isolation, found {sorted(names)}"
    runs_names = {policy.name for policy in tenant_policy_census["runs"].policies}
    assert runs_names == {"rls_org_isolation"}, f"runs is expected to be org-scoped until ADR 070 Phase 1B; found {sorted(runs_names)}. If the runs family has been flipped to the team predicate, extend the cross-team assertions to runs and update this expectation."


@pytest.mark.parametrize("table", _BEHAVIOURAL_TABLES)
@pytest.mark.parametrize("context_label", ["cross-org", "cross-org+admin", "cross-org+execution-context", "org-unset"])
async def test_cross_org_matrix_returns_zero_foreign_rows(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    rls_tenant_seed: TenantSeed,
    table: str,
    context_label: str,
) -> None:
    """An org-B context (and an unset context) must expose zero org-A rows.

    The positive control differs by scope: org-scoped tables must show org B's
    rows to prove the context is not simply denied everything, while a
    team-private row additionally needs a team identity — so under a plain
    cross-org context (no user) those tables legitimately return nothing, and
    ``test_cross_team_member_reads_only_its_own_team`` supplies their positive
    control.
    """
    context = _org_contexts(rls_tenant_seed)[context_label]
    visible = await _visible_row_ids(db_engine, non_superuser_role, table, rls_tenant_seed.ids(table), context)
    a_ids = {str(rls_tenant_seed.rows[table][alias]) for alias in ("a1", "a2")}
    b_ids = {str(rls_tenant_seed.rows[table][alias]) for alias in ("b1", "b2")}

    leaked = visible & a_ids
    assert not leaked, f"{table} under {context_label}: leaked org-A rows {sorted(leaked)}"

    if context_label == "org-unset":
        assert visible == set(), f"{table} under unset org context returned rows: {sorted(visible)}"
    elif table in _TEAM_SCOPED_TABLES and context_label == "cross-org":
        # A plain org context carries no team identity, so the team arm cannot
        # match a team-private row of *either* org — the positive control for
        # these tables is the team-member probe below.
        assert visible == set(), f"{table} under {context_label}: expected no team-private rows without a team identity, got {sorted(visible)}"
    else:
        # org-scoped tables, and the admin / execution-context arms (which widen
        # within the org), must still see exactly their own org's rows.
        assert visible == b_ids, f"{table} under {context_label}: expected its own org's rows as a positive control, got {sorted(visible)}"


@pytest.mark.parametrize("table", sorted(_TEAM_SCOPED_TABLES))
async def test_cross_team_member_reads_only_its_own_team(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    rls_tenant_seed: TenantSeed,
    table: str,
) -> None:
    """A team-B member must not read team A's team-private rows in the same org.

    Each probe carries its own positive control (its own team's row is visible),
    so a policy that denies everything fails just as loudly as one that leaks.
    Both orgs are probed, and the org gate is re-asserted *inside* a member
    context — the team arm must never widen past ``organisation_id``.
    """
    all_ids = rls_tenant_seed.ids(table)
    for org_id, member_id, own_alias, other_alias, foreign_aliases, label in (
        (rls_tenant_seed.org_a, rls_tenant_seed.account_a2, "a2", "a1", ("b1", "b2"), "org-A/team-A2"),
        (rls_tenant_seed.org_b, rls_tenant_seed.account_b2, "b2", "b1", ("a1", "a2"), "org-B/team-B2"),
    ):
        context = RlsContext(label=label, org=org_id, user_id=member_id, org_role="operator")
        visible = await _visible_row_ids(db_engine, non_superuser_role, table, all_ids, context)

        own = str(rls_tenant_seed.rows[table][own_alias])
        other_team = str(rls_tenant_seed.rows[table][other_alias])
        foreign = {str(rls_tenant_seed.rows[table][alias]) for alias in foreign_aliases}

        assert own in visible, f"{table} {label}: own team row {own} is invisible — the positive control failed"
        assert other_team not in visible, f"{table} {label}: cross-team read leaked the sibling team's row {other_team}"
        assert not (visible & foreign), f"{table} {label}: cross-org rows leaked inside a member context: {sorted(visible & foreign)}"


async def test_org_scoped_runs_positive_control(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    rls_tenant_seed: TenantSeed,
) -> None:
    """``runs`` is org-scoped: a member of org A sees org A's runs and no others."""
    context = RlsContext(label="org-A/team-A2", org=rls_tenant_seed.org_a, user_id=rls_tenant_seed.account_a2, org_role="operator")
    visible = await _visible_row_ids(db_engine, non_superuser_role, "runs", rls_tenant_seed.ids("runs"), context)
    a_ids = {str(rls_tenant_seed.rows["runs"][alias]) for alias in ("a1", "a2")}
    b_ids = {str(rls_tenant_seed.rows["runs"][alias]) for alias in ("b1", "b2")}
    assert visible == a_ids, f"runs: expected exactly org A's rows in an org-A context, got {sorted(visible)}"
    assert not (visible & b_ids), "runs: cross-org leak inside a member context"
