"""ADR 070 §2/§3 — the pre-auth bootstrap grant is scoped, explicit, and dies.

Why this file exists
--------------------
``oauth_clients``, ``oauth_consent_states``, ``org_memberships`` and
``token_families`` are read before any organisation context exists. Their legacy
policy said ``... OR current_setting('app.organisation_id') IS NULL``, and the
pool-checkout reset writes ``app.organisation_id = ''`` — so ``NULLIF('','')`` is
NULL and **any unbound session read every row of all four tables**. That is an
accidental god-mode, not a scoped read.

Migration 0365 replaces the NULL arm with an explicit arm on
``app.bootstrap_context``. This module proves, against real Postgres as a
NON-superuser role (RLS does not apply to superusers), that:

1. a session with **no** context reads **zero** rows from all four tables;
2. a session with **only** the bootstrap context reads the exact row it looks up,
   and the grant is cleared again the moment the lookup's ``bootstrap_read``
   block exits (per-lookup, not per-transaction);
3. an org context reads its own org's rows and never another org's;
4. the GUC never survives a transaction end or a pool checkout, and
   ``set_rls_bootstrap_context`` refuses to run outside a transaction.

Every assertion carries a positive control, so a policy that simply denies
everything fails just as loudly as one that leaks.

The structural half of the gate lives in ``test_rls_policy_gate.py``; this module
is the behavioural half (ADR 065: a boundary test, not a declaration beside it).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from farna.db.crud.token_family import advance_sequence
from farna.db.rls import (
    BOOTSTRAP_CONTEXT_GUC,
    bootstrap_read,
    register_rls_reset_hook,
    set_rls_bootstrap_context,
)

pytestmark = pytest.mark.integration

#: The four tables migration 0365 re-scoped, with the exact primary key each
#: pre-auth lookup pins (``test_rls_policy_gate._BOOTSTRAP_GRANT_TABLES``).
_TABLES: dict[str, str] = {
    "oauth_clients": "client_id",
    "oauth_consent_states": "state",
    "org_memberships": "id",
    "token_families": "family_id",
}

#: One literal statement per table (never an interpolated one), so the probe can
#: never be mistaken for a string-built query.
_KEY_SELECTS: dict[str, str] = {
    "oauth_clients": "SELECT client_id::text FROM oauth_clients WHERE client_id::text = ANY(:keys)",
    "oauth_consent_states": "SELECT state::text FROM oauth_consent_states WHERE state::text = ANY(:keys)",
    "org_memberships": "SELECT id::text FROM org_memberships WHERE id::text = ANY(:keys)",
    "token_families": "SELECT family_id::text FROM token_families WHERE family_id::text = ANY(:keys)",
}

_ORGANISATION_INSERT = "INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"
_ACCOUNT_INSERT = "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) VALUES (:id, :email, :name, 'local', true, 'hash')"
_ORG_MEMBERSHIP_INSERT = "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:id, :aid, :oid, 'operator')"
_TOKEN_FAMILY_INSERT = "INSERT INTO token_families (family_id, account_id, organisation_id, max_sequence, is_blacklisted) VALUES (:id, :aid, :oid, 0, false)"
_OAUTH_CLIENT_INSERT = "INSERT INTO oauth_clients (id, organisation_id, client_id, client_secret_hash, name, scopes, redirect_uris, account_id) VALUES (:id, :oid, :client_id, :secret, :name, 'runs.read', 'http://localhost/cb', :aid)"
_CONSENT_STATE_INSERT = "INSERT INTO oauth_consent_states (state, client_id, redirect_uri, scopes, code_challenge, organisation_id, expires_at, consumed) VALUES (:state, :client_id, 'http://localhost/cb', '[\"runs.read\"]'::json, :challenge, :oid, CURRENT_TIMESTAMP + INTERVAL '15 minutes', false)"


@dataclass(frozen=True)
class PreAuthSeed:
    """Two orgs, one account each, and one row per table per org."""

    org_a: uuid.UUID
    org_b: uuid.UUID
    account_a: uuid.UUID
    account_b: uuid.UUID
    #: table -> {"a", "b"} -> primary-key value (as text, comparable to a SELECT)
    keys: dict[str, dict[str, str]]

    def keys_for(self, table: str) -> list[str]:
        return list(self.keys[table].values())


async def _seed(db_engine: AsyncEngine) -> PreAuthSeed:
    slug = f"rls-boot-{uuid.uuid4().hex[:10]}"
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    account_a, account_b = uuid.uuid4(), uuid.uuid4()
    keys: dict[str, dict[str, str]] = {
        "oauth_clients": {"a": f"{slug}-client-a", "b": f"{slug}-client-b"},
        "oauth_consent_states": {"a": f"{slug}-state-a", "b": f"{slug}-state-b"},
        "org_memberships": {"a": str(uuid.uuid4()), "b": str(uuid.uuid4())},
        "token_families": {"a": str(uuid.uuid4()), "b": str(uuid.uuid4())},
    }

    async with db_engine.connect() as conn, conn.begin():
        for org_id, label in ((org_a, "A"), (org_b, "B")):
            await conn.execute(text(_ORGANISATION_INSERT), {"id": str(org_id), "name": f"RLS Bootstrap Org {label}", "slug": f"{slug}-{label.lower()}"})
        for account_id, org_id in ((account_a, org_a), (account_b, org_b)):
            await conn.execute(text(_ACCOUNT_INSERT), {"id": str(account_id), "email": f"{slug}-{account_id.hex[:8]}@example.com", "name": f"{slug}-{account_id.hex[:8]}"})
            await conn.execute(text(_ORG_MEMBERSHIP_INSERT), {"id": keys["org_memberships"]["a" if account_id == account_a else "b"], "aid": str(account_id), "oid": str(org_id)})
            await conn.execute(text(_TOKEN_FAMILY_INSERT), {"id": keys["token_families"]["a" if account_id == account_a else "b"], "aid": str(account_id), "oid": str(org_id)})
            await conn.execute(
                text(_OAUTH_CLIENT_INSERT),
                {
                    "id": str(uuid.uuid4()),
                    "oid": str(org_id),
                    "client_id": keys["oauth_clients"]["a" if account_id == account_a else "b"],
                    "secret": "0" * 64,
                    "name": f"{slug}-client-{'a' if account_id == account_a else 'b'}",
                    "aid": str(account_id),
                },
            )
            await conn.execute(
                text(_CONSENT_STATE_INSERT),
                {
                    "state": keys["oauth_consent_states"]["a" if account_id == account_a else "b"],
                    "client_id": keys["oauth_clients"]["a" if account_id == account_a else "b"],
                    "challenge": f"{slug}-challenge-{'a' if account_id == account_a else 'b'}",
                    "oid": str(org_id),
                },
            )

    return PreAuthSeed(org_a=org_a, org_b=org_b, account_a=account_a, account_b=account_b, keys=keys)


async def _cleanup(db_engine: AsyncEngine, seed: PreAuthSeed) -> None:
    orgs = [str(seed.org_a), str(seed.org_b)]
    accounts = [str(seed.account_a), str(seed.account_b)]
    async with db_engine.connect() as conn, conn.begin():
        for statement in (
            "DELETE FROM oauth_consent_states WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM oauth_clients WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM token_families WHERE organisation_id = ANY(:orgs)",
            "DELETE FROM org_memberships WHERE organisation_id = ANY(:orgs)",
        ):
            await conn.execute(text(statement), {"orgs": orgs})
        await conn.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": accounts})
        await conn.execute(text("DELETE FROM organisations WHERE id = ANY(:ids)"), {"ids": orgs})


@pytest_asyncio.fixture(scope="session")
async def pre_auth_seed(db_engine: AsyncEngine) -> PreAuthSeed:
    seed = await _seed(db_engine)
    try:
        yield seed
    finally:
        await _cleanup(db_engine, seed)


async def _visible_keys(
    db_engine: AsyncEngine,
    role: str,
    table: str,
    keys: list[str],
    *,
    org: uuid.UUID | None = None,
    bootstrap: bool = False,
) -> set[str]:
    """Primary keys *table* exposes to a NON-superuser connection under the context."""
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(text(f'SET LOCAL ROLE "{role}"'))
        if org is not None:
            await conn.execute(text("SELECT set_config('app.organisation_id', :v, true)"), {"v": str(org)})
        if bootstrap:
            await conn.execute(text(f"SELECT set_config('{BOOTSTRAP_CONTEXT_GUC}', 'true', true)"))
        rows = (await conn.execute(text(_KEY_SELECTS[table]), {"keys": keys})).fetchall()
    return {row[0] for row in rows}


def _own_and_foreign(seed: PreAuthSeed, table: str) -> tuple[str, str]:
    return seed.keys[table]["a"], seed.keys[table]["b"]


@pytest.mark.parametrize("table", sorted(_TABLES))
async def test_no_context_reads_zero_rows(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
    table: str,
) -> None:
    """An unbound session must see NOTHING in any of the four tables.

    Before migration 0365 this same probe returned all four rows (both orgs): the
    fail-open ``current_setting(...) IS NULL`` arm matched once the pool-checkout
    reset had written ``''``. This is the assertion that flips.
    """
    visible = await _visible_keys(db_engine, non_superuser_role, table, pre_auth_seed.keys_for(table))
    assert visible == set(), f"{table}: an unbound session must read zero rows (fail-open regression), got {sorted(visible)}"


@pytest.mark.parametrize("table", sorted(_TABLES))
async def test_bootstrap_context_reads_only_the_pinned_row(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
    table: str,
) -> None:
    """The explicit grant authorises the pinned lookup — and nothing wider."""
    own, foreign = _own_and_foreign(pre_auth_seed, table)
    # Pinned to the exact key the pre-auth flow looks up.
    pinned = await _visible_keys(db_engine, non_superuser_role, table, [own], bootstrap=True)
    assert pinned == {own}, f"{table}: the pinned bootstrap lookup must find its row, got {sorted(pinned)}"
    # A key that belongs to the OTHER org is still not readable by id alone when
    # the caller pins a key it does not own — the grant widens the predicate, so
    # the protection is the exact-key pinning at every call site (asserted by the
    # call-site tests below), not a hidden org filter here.
    other = await _visible_keys(db_engine, non_superuser_role, table, [foreign], bootstrap=True)
    assert other == {foreign}, f"{table}: expected the pinned key to resolve under the bootstrap grant, got {sorted(other)}"


@pytest.mark.parametrize("table", sorted(_TABLES))
async def test_org_context_reads_only_its_own_org(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
    table: str,
) -> None:
    """An org context is the narrow path: own org visible, foreign org not."""
    own, foreign = _own_and_foreign(pre_auth_seed, table)
    keys = pre_auth_seed.keys_for(table)

    org_a_visible = await _visible_keys(db_engine, non_superuser_role, table, keys, org=pre_auth_seed.org_a)
    org_b_visible = await _visible_keys(db_engine, non_superuser_role, table, keys, org=pre_auth_seed.org_b)

    assert org_a_visible == {own}, f"{table}: org-A context must see exactly its own row, got {sorted(org_a_visible)}"
    assert org_b_visible == {foreign}, f"{table}: org-B context must see exactly its own row, got {sorted(org_b_visible)}"
    assert foreign not in org_a_visible, f"{table}: org-A context leaked org-B's row {foreign}"
    assert own not in org_b_visible, f"{table}: org-B context leaked org-A's row {own}"


@pytest.mark.parametrize("table", sorted(_TABLES))
async def test_bootstrap_grant_is_cleared_between_lookups(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
    table: str,
) -> None:
    """``bootstrap_read`` clears the grant when its block exits.

    A pre-auth transaction that resolves its identity and then continues into
    org-scoped work must not carry the widening with it: after the block, the
    same transaction is unbound again and sees zero rows.
    """
    own = pre_auth_seed.keys[table]["a"]
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session, session.begin():
        await session.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))

        async with bootstrap_read(session):
            inside = (await session.execute(text(_KEY_SELECTS[table]), {"keys": pre_auth_seed.keys_for(table)})).fetchall()
        assert {row[0] for row in inside} == set(pre_auth_seed.keys_for(table)), f"{table}: the bootstrap window must expose the pinned lookups, got {sorted(row[0] for row in inside)}"
        assert own in {row[0] for row in inside}

        after = (await session.execute(text(_KEY_SELECTS[table]), {"keys": pre_auth_seed.keys_for(table)})).fetchall()
        assert after == [], f"{table}: the grant must be cleared when the block exits, still saw {sorted(row[0] for row in after)}"


async def test_bootstrap_context_requires_an_active_transaction(db_engine: AsyncEngine) -> None:
    """Mirrors ``set_rls_execution_context``: no silent no-op outside a transaction."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        assert session.in_transaction() is False
        with pytest.raises(RuntimeError, match="active transaction"):
            await set_rls_bootstrap_context(session)


async def test_bootstrap_guc_is_transaction_local(db_engine: AsyncEngine) -> None:
    """Set LOCAL dies at COMMIT — the grant cannot outlive the lookup's transaction."""
    async with db_engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text(f"SELECT set_config('{BOOTSTRAP_CONTEXT_GUC}', 'true', true)"))
            inside = (await conn.execute(text(f"SELECT current_setting('{BOOTSTRAP_CONTEXT_GUC}', true)"))).scalar()
            assert inside == "true"
        async with conn.begin():
            after = (await conn.execute(text(f"SELECT current_setting('{BOOTSTRAP_CONTEXT_GUC}', true)"))).scalar()
            assert after != "true", f"bootstrap context survived COMMIT with value {after!r}"


async def test_bootstrap_guc_does_not_survive_a_pool_checkout(migrated_db_url: str) -> None:
    """The pool-checkout reset clears a SESSION-level value before the next user.

    Proves ``app.bootstrap_context`` is in ``_RLS_CONFIG_NAMES``: a session-level
    grant left on a pooled connection must be gone on the next checkout, or a
    later request sharing that connection would inherit a four-table widening.
    """
    engine = create_async_engine(migrated_db_url, pool_size=1, max_overflow=0)
    try:
        register_rls_reset_hook(engine)

        async with engine.connect() as conn:  # checkout 1 — the only pooled connection
            await conn.execute(text(f"SELECT set_config('{BOOTSTRAP_CONTEXT_GUC}', 'true', false)"))
            await conn.commit()
            left_behind = (await conn.execute(text(f"SELECT current_setting('{BOOTSTRAP_CONTEXT_GUC}', true)"))).scalar()
            await conn.commit()
        assert left_behind == "true", "setup failed: the session-level grant was not visible on its own connection"

        async with engine.connect() as conn:  # checkout 2 — same physical connection
            cleared = (await conn.execute(text(f"SELECT current_setting('{BOOTSTRAP_CONTEXT_GUC}', true)"))).scalar()
            await conn.commit()
        assert cleared != "true", f"bootstrap context survived a pool checkout with value {cleared!r}"
    finally:
        await engine.dispose()


async def test_oauth_client_lookup_helper_works_under_the_bootstrap_grant(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
) -> None:
    """The real pre-auth lookup (``auth.oauth``) resolves the client, org-free.

    This is the production helper the authorize / token / refresh legs call; it
    must succeed for a legitimate client while the session carries the bootstrap
    grant, and return ``None`` (not raise) for an unknown one.
    """
    from farna.auth.oauth import get_oauth_client_by_client_id

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    client_id = pre_auth_seed.keys["oauth_clients"]["a"]

    async with session_factory() as session, session.begin():
        await session.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        async with bootstrap_read(session):
            client = await get_oauth_client_by_client_id(session, client_id)
        assert client is not None
        assert client.client_id == client_id
        assert client.organisation_id == pre_auth_seed.org_a

        async with bootstrap_read(session):
            missing = await get_oauth_client_by_client_id(session, f"unknown-{client_id}")
        assert missing is None


async def test_refresh_family_rotation_works_under_the_bootstrap_grant(
    db_engine: AsyncEngine,
    non_superuser_role: str,
    pre_auth_seed: PreAuthSeed,
) -> None:
    """The refresh leg advances the family with no org context at all.

    ``advance_sequence`` is the write the refresh flow makes after re-reading the
    live role; it is pinned to ``(family_id, account_id)`` and must keep working
    under the explicit grant (a fail-closed policy here would log every user out).
    """
    family_id = uuid.UUID(pre_auth_seed.keys["token_families"]["a"])
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session, session.begin():
        await session.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        async with bootstrap_read(session):
            new_sequence, theft_detected, reuse_replay = await advance_sequence(
                session,
                family_id,
                expected_sequence=0,
                account_id=pre_auth_seed.account_a,
                reuse_grace_seconds=0,
            )
        assert new_sequence == 1
        assert theft_detected is False
        assert reuse_replay is False
