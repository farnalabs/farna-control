"""ADR 070 §2/§3 — the last two unprotected tenant tables, on real Postgres.

Why this file exists
--------------------
``invitations`` and ``run_node_outputs_quarantine`` carried an
``organisation_id`` column with ``relrowsecurity = false``. That means an app
session holding org A's context could read org B's invitation row (and its
Bearer-equivalent token hash) and org B's quarantined node output — there was
simply no policy to stop it.

Migration ``0368_invitations_quarantine_rls`` enables RLS and adds:

* ``run_node_outputs_quarantine`` — the plain single-clause org equality;
* ``invitations`` — the org equality OR the scoped, transaction-scoped
  ``app.bootstrap_context`` grant landed by 0367, because the accept-by-token
  read happens before any principal exists.

This module proves the change against a real database and the REAL Alembic chain,
as a NON-superuser role (RLS never applies to superusers):

1. **Before** 0368 an org-A session reads org B's invitation and quarantine row
   (the gap, demonstrated rather than inferred from the SQL text).
2. **After** 0368 the same probe returns zero foreign rows, from both the
   cross-org context and the pool-checkout reset shape (``app.organisation_id =
   ''``), while an org's own rows stay visible (positive control).
3. The pre-auth accept lookup (``crud.invitations.get_valid_by_token_hash``)
   still resolves its invitation **inside** ``bootstrap_read``, returns ``None``
   without the grant, and pins the unique ``token_hash`` (a different hash
   resolves nothing, even under the grant).
4. Invite management still works for an org member: ``list_pending_for_org`` and
   ``revoke_invitation`` succeed in the caller's org, and RLS — not just the
   ``WHERE`` clause — stops an org-A session from listing or revoking org B's
   row.
5. The **downgrade** drops both policies and disables RLS, restoring the exact
   pre-0368 state (cross-org reads leak again), and the upgrade re-applies.

The container is shared by the module; every test normalises the chain to the
revision it needs, so the module does not depend on test order.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

from farna.db.crud.invitations import (
    get_valid_by_token_hash,
    list_pending_for_org,
    revoke_invitation,
)
from farna.db.rls import bootstrap_read, set_rls_org

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[2]
_HEAD = "0368_invitations_quarantine_rls"
_PRE_CHANGE = "0367_scoped_bootstrap_pre_auth_policies"

_APP_ROLE = "farna_control_app"
_APP_PASSWORD = "apppass"

_TABLES = ("invitations", "run_node_outputs_quarantine")

#: ``ANY(CAST(:ids AS text[]))`` rather than a bare ``ANY(:ids)``: the explicit
#: cast keeps the parameter's type unambiguous on the sync psycopg driver.
_INVITATION_ID_SQL = "SELECT id::text FROM invitations WHERE id::text = ANY(CAST(:ids AS text[]))"
_QUARANTINE_ID_SQL = "SELECT run_id::text FROM run_node_outputs_quarantine WHERE run_id::text = ANY(CAST(:ids AS text[]))"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "farna" / "db" / "migrations"))
    config.config_file_name = None
    return config


def _sync_url(asyncpg_url: str) -> str:
    from farna.db.migrations import env as migration_env

    return migration_env._to_sync_url(asyncpg_url)


def _with_credentials(database_url: str, user: str, password: str) -> str:
    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


@dataclass(frozen=True)
class RlsDb:
    """One throwaway database plus the URLs needed to drive both directions."""

    sync_url: str
    app_async_url: str
    app_sync_url: str
    config: Config


@pytest.fixture(scope="module")
def rls_container() -> Iterator[tuple[str, str, str, str, Config]]:
    """One real Postgres for the module, with the production role model created.

    No migration is applied here: the per-test fixture pins the alembic target
    through ``monkeypatch`` before any ``command.upgrade`` runs, so an ambient
    ``DATABASE_ADMIN_URL`` can never divert this module into another database.
    """
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    raw_async = pg.get_connection_url().replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")
    sync_url = _sync_url(raw_async)
    app_async_url = _with_credentials(raw_async, _APP_ROLE, _APP_PASSWORD)
    app_sync_url = _with_credentials(sync_url, _APP_ROLE, _APP_PASSWORD)

    engine = sa.create_engine(sync_url)
    with engine.connect() as conn:
        conn.execute(sa.text('DROP ROLE IF EXISTS "farna_control_migrate"'))
        conn.execute(sa.text('DROP ROLE IF EXISTS "farna_control_breakglass"'))
        conn.execute(sa.text(f'DROP ROLE IF EXISTS "{_APP_ROLE}"'))
        conn.execute(sa.text("CREATE ROLE farna_control_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
        conn.execute(sa.text("CREATE ROLE farna_control_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
        conn.execute(sa.text(f"CREATE ROLE {_APP_ROLE} NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD '{_APP_PASSWORD}'"))
        conn.commit()
    engine.dispose()

    from farna.db.bootstrap_role import bootstrap_roles

    asyncio.run(bootstrap_roles(raw_async, app_async_url))
    try:
        yield raw_async, sync_url, app_async_url, app_sync_url, _alembic_config(raw_async)
    finally:
        pg.stop()


@pytest.fixture
def rls_db(rls_container: tuple[str, str, str, str, Config], monkeypatch: pytest.MonkeyPatch) -> RlsDb:
    """Pin the alembic target to this module's container for the test's duration."""
    raw_async, sync_url, app_async_url, app_sync_url, config = rls_container
    monkeypatch.setenv("DATABASE_URL", raw_async)
    monkeypatch.setenv("DATABASE_ADMIN_URL", raw_async)
    monkeypatch.setenv("FARNA_CONTROL_BREAK_GLASS_DATABASE_URL", _with_credentials(raw_async, "farna_control_breakglass", "bgpass"))
    return RlsDb(sync_url=sync_url, app_async_url=app_async_url, app_sync_url=app_sync_url, config=config)


def _has_alembic_version_table(sync_url: str) -> bool:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT to_regclass('public.alembic_version')")).scalar() is not None
    finally:
        engine.dispose()


def _alembic_version(sync_url: str) -> str | None:
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _ensure_pre(db: RlsDb) -> None:
    """Leave the chain at 0367 (uninitialised databases get the whole chain)."""
    if not _has_alembic_version_table(db.sync_url):
        command.upgrade(db.config, _PRE_CHANGE)
        return
    if _alembic_version(db.sync_url) != _PRE_CHANGE:
        command.downgrade(db.config, _PRE_CHANGE)


def _ensure_head(db: RlsDb) -> None:
    """Leave the chain at 0368 (a no-op when already there)."""
    command.upgrade(db.config, _HEAD)


def _superuser(db: RlsDb) -> sa.Engine:
    return sa.create_engine(db.sync_url)


@dataclass(frozen=True)
class Seed:
    """Two orgs, one inviter account each, one invitation and one quarantine row each."""

    org_a: uuid.UUID
    org_b: uuid.UUID
    invitation_a: uuid.UUID
    invitation_b: uuid.UUID
    token_a: str
    token_b: str
    quarantine_a: str
    quarantine_b: str


def _seed(engine: sa.Engine) -> Seed:
    """Seed as the superuser (RLS bypassed for setup), with unique keys per test."""
    slug = f"rls-0368-{uuid.uuid4().hex[:10]}"
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    account_a, account_b = uuid.uuid4(), uuid.uuid4()
    invitation_a, invitation_b = uuid.uuid4(), uuid.uuid4()
    # token_hash is a SHA-256 hex digest: 64 chars, and UNIQUE across the table.
    token_a = uuid.uuid4().hex + uuid.uuid4().hex
    token_b = uuid.uuid4().hex + uuid.uuid4().hex
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    expires = datetime.now(UTC) + timedelta(hours=24)

    with engine.begin() as conn:
        for org_id, label in ((org_a, "A"), (org_b, "B")):
            conn.execute(
                sa.text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
                {"id": org_id, "name": f"RLS 0368 Org {label}", "slug": f"{slug}-{label.lower()}"},
            )
        for account_id, label in ((account_a, "A"), (account_b, "B")):
            conn.execute(
                sa.text("INSERT INTO accounts (id, email, display_name) VALUES (:id, :email, :name)"),
                {"id": account_id, "email": f"{slug}-{label.lower()}@example.com", "name": f"RLS 0368 {label}"},
            )
        for invitation_id, org_id, account_id, token, label in (
            (invitation_a, org_a, account_a, token_a, "a"),
            (invitation_b, org_b, account_b, token_b, "b"),
        ):
            conn.execute(
                sa.text("INSERT INTO invitations (id, organisation_id, email, display_name, org_role, token_hash, invited_by, expires_at) VALUES (:id, :org, :email, :name, 'operator', :token, :by, :exp)"),
                {
                    "id": invitation_id,
                    "org": org_id,
                    "email": f"invited-{slug}-{label}@example.com",
                    "name": "Invited",
                    "token": token,
                    "by": account_id,
                    "exp": expires,
                },
            )
        for run_id, org_id in ((run_a, org_a), (run_b, org_b)):
            conn.execute(
                sa.text("INSERT INTO run_node_outputs_quarantine (run_id, organisation_id) VALUES (:run, :org)"),
                {"run": run_id, "org": org_id},
            )

    return Seed(
        org_a=org_a,
        org_b=org_b,
        invitation_a=invitation_a,
        invitation_b=invitation_b,
        token_a=token_a,
        token_b=token_b,
        quarantine_a=str(run_a),
        quarantine_b=str(run_b),
    )


def _own_id(seed: Seed, table: str) -> str:
    return str(seed.invitation_a) if table == "invitations" else seed.quarantine_a


def _foreign_id(seed: Seed, table: str) -> str:
    return str(seed.invitation_b) if table == "invitations" else seed.quarantine_b


@contextmanager
def _app_connection(app_sync_url: str, *, org: uuid.UUID | None = None, empty_org: bool = False) -> Iterator[sa.Connection]:
    """A NON-superuser connection under one org-context shape.

    ``empty_org=True`` writes the pool-checkout reset value ``''`` explicitly — the
    shape that makes a bare ``''::uuid`` cast raise — while ``org`` binds a real
    org. Neither sets ``app.bootstrap_context``: only the pre-auth accept path does,
    through ``db.rls.bootstrap_read`` (exercised through the real CRUD helper in
    ``test_accept_by_token_needs_the_bootstrap_grant_and_pins_the_hash``).
    """
    engine = sa.create_engine(app_sync_url)
    try:
        with engine.connect() as conn, conn.begin():
            if org is not None:
                conn.execute(sa.text("SELECT set_config('app.organisation_id', :v, true)"), {"v": str(org)})
            elif empty_org:
                conn.execute(sa.text("SELECT set_config('app.organisation_id', '', true)"))
            yield conn
    finally:
        engine.dispose()


def _visible(
    db: RlsDb,
    seed: Seed,
    table: str,
    *,
    org: uuid.UUID | None = None,
    empty_org: bool = False,
) -> set[str]:
    """Ids of *table* a NON-superuser app session can see under the given context."""
    if table == "invitations":
        statement = _INVITATION_ID_SQL
        ids = [str(seed.invitation_a), str(seed.invitation_b)]
    else:
        statement = _QUARANTINE_ID_SQL
        ids = [seed.quarantine_a, seed.quarantine_b]
    with _app_connection(db.app_sync_url, org=org, empty_org=empty_org) as conn:
        return {row[0] for row in conn.execute(sa.text(statement), {"ids": ids})}


def _rls_enabled(db: RlsDb) -> dict[str, bool]:
    engine = _superuser(db)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT c.relname, c.relrowsecurity FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relname = ANY(:names)"),
                {"names": list(_TABLES)},
            ).fetchall()
            return {name: bool(enabled) for name, enabled in rows}
    finally:
        engine.dispose()


def _policies(db: RlsDb) -> dict[str, set[str]]:
    engine = _superuser(db)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = ANY(:names)"),
                {"names": list(_TABLES)},
            ).fetchall()
            found: dict[str, set[str]] = {}
            for table, policy in rows:
                found.setdefault(table, set()).add(policy)
            return found
    finally:
        engine.dispose()


def _protected_state() -> dict[str, bool]:
    return dict.fromkeys(_TABLES, True)


def _unprotected_state() -> dict[str, bool]:
    return dict.fromkeys(_TABLES, False)


def _expected_policies() -> dict[str, set[str]]:
    return {table: {"rls_org_isolation"} for table in _TABLES}


# ---------------------------------------------------------------------------
# 1. The gap, and its closure
# ---------------------------------------------------------------------------


def test_cross_org_reads_are_blocked_only_after_0368(rls_db: RlsDb) -> None:
    """BEFORE the migration org A reads org B's rows; AFTER it cannot.

    This is the defect itself, demonstrated on a real database instead of inferred
    from the absence of a policy. Both tables are probed, and every "after" probe
    carries a positive control (org A still sees org A's row) so a policy that
    simply denied everything would fail just as loudly.
    """
    _ensure_pre(rls_db)
    engine = _superuser(rls_db)
    try:
        seed = _seed(engine)
    finally:
        engine.dispose()

    assert _rls_enabled(rls_db) == _unprotected_state()
    assert not _policies(rls_db)

    for table in _TABLES:
        # BEFORE: an org-A session reads BOTH orgs' rows. This is the gap.
        leaked = _visible(rls_db, seed, table, org=seed.org_a)
        assert leaked == {_own_id(seed, table), _foreign_id(seed, table)}, f"{table}: expected the pre-0368 cross-org leak to be demonstrable, got {sorted(leaked)}"

    _ensure_head(rls_db)

    assert _rls_enabled(rls_db) == _protected_state()
    assert _policies(rls_db) == _expected_policies()
    assert _alembic_version(rls_db.sync_url) == _HEAD

    for table in _TABLES:
        own, foreign = _own_id(seed, table), _foreign_id(seed, table)

        org_a_visible = _visible(rls_db, seed, table, org=seed.org_a)
        assert org_a_visible == {own}, f"{table}: org-A context must see exactly its own row, got {sorted(org_a_visible)}"

        org_b_visible = _visible(rls_db, seed, table, org=seed.org_b)
        assert org_b_visible == {foreign}, f"{table}: org-B context must see exactly its own row, got {sorted(org_b_visible)}"

        # The pool-checkout reset writes app.organisation_id = ''. Both tables must
        # fail CLOSED (zero rows) on it, not raise on an ''::uuid cast.
        assert not _visible(rls_db, seed, table, empty_org=True), f"{table}: the post-checkout reset shape must read zero rows"


# ---------------------------------------------------------------------------
# 2. The pre-auth accept lookup
# ---------------------------------------------------------------------------


async def _lookup_by_token(app_async_url: str, token_hash: str, *, bootstrap: bool):
    """The real accept-by-token read, as the app role, with or without the grant."""
    engine = create_async_engine(app_async_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            if bootstrap:
                async with bootstrap_read(session):
                    return await get_valid_by_token_hash(session, token_hash)
            return await get_valid_by_token_hash(session, token_hash)
    finally:
        await engine.dispose()


def test_accept_by_token_needs_the_bootstrap_grant_and_pins_the_hash(rls_db: RlsDb) -> None:
    """The pre-auth accept path is authorised by the grant, and pinned to the hash.

    The calling principal holds no organisation context, so without the grant the
    database returns zero rows; ``bootstrap_read`` is the only thing that makes the
    lookup resolve. The grant widens the *predicate*, not the query — the helper's
    ``token_hash`` equality is what keeps the read to one exact row, so a hash that
    matches nothing resolves nothing even inside the grant window.
    """
    _ensure_head(rls_db)
    engine = _superuser(rls_db)
    try:
        seed = _seed(engine)
    finally:
        engine.dispose()

    resolved = asyncio.run(_lookup_by_token(rls_db.app_async_url, seed.token_a, bootstrap=True))
    assert resolved is not None, "the pre-auth accept lookup must succeed inside bootstrap_read"
    assert resolved.id == seed.invitation_a
    assert resolved.organisation_id == seed.org_a

    denied = asyncio.run(_lookup_by_token(rls_db.app_async_url, seed.token_a, bootstrap=False))
    assert denied is None, "without the explicit grant an unbound session must read zero invitations"

    unknown = asyncio.run(_lookup_by_token(rls_db.app_async_url, "0" * 64, bootstrap=True))
    assert unknown is None, "the lookup is pinned to the exact token_hash; a non-matching hash must resolve nothing"


# ---------------------------------------------------------------------------
# 3. Invite management in the normal org context
# ---------------------------------------------------------------------------


async def _list_then_revoke(app_async_url: str, seed: Seed) -> tuple[int, int, bool, bool]:
    """List+revoke as an org-A session; return what each call affected.

    Returns ``(items_for_own_org, items_when_asking_for_org_b, revoked_own,
    revoked_foreign)``. The last one is the RLS backstop: the ``WHERE`` clause
    would match org B's row, but an org-A session cannot see it, so the callee
    cannot be tricked into a cross-org revoke by a caller-supplied org id.
    """
    engine = create_async_engine(app_async_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            await set_rls_org(session, seed.org_a)
            own_items, own_total = await list_pending_for_org(session, org_id=seed.org_a)
            assert own_total == len(own_items)
            # Asking for the OTHER org explicitly must still return nothing: RLS,
            # not the caller's parameter, is the backstop.
            foreign_items, _foreign_total = await list_pending_for_org(session, org_id=seed.org_b)
            foreign_revoked = await revoke_invitation(session, invitation_id=seed.invitation_b, org_id=seed.org_b)
            own_revoked = await revoke_invitation(session, invitation_id=seed.invitation_a, org_id=seed.org_a)
            return len(own_items), len(foreign_items), own_revoked, foreign_revoked
    finally:
        await engine.dispose()


def test_org_member_invite_list_and_revoke_are_org_bounded(rls_db: RlsDb) -> None:
    """List/revoke still work under the new policy — and stay inside the org."""
    _ensure_head(rls_db)
    engine = _superuser(rls_db)
    try:
        seed = _seed(engine)
    finally:
        engine.dispose()

    listed_own, listed_foreign, revoked_own, revoked_foreign = asyncio.run(_list_then_revoke(rls_db.app_async_url, seed))

    assert listed_own == 1, "an org-A member must list org A's single live invitation"
    assert listed_foreign == 0, "an org-A session must not list org B's invitations, even when asked for org B"
    assert revoked_own is True, "revoking the caller's own org invitation must still work"
    assert revoked_foreign is False, "an org-A session must not revoke an org-B invitation (RLS hides the row)"


# ---------------------------------------------------------------------------
# 4. Reversibility
# ---------------------------------------------------------------------------


def test_0368_downgrade_disables_rls_and_drops_the_policies(rls_db: RlsDb) -> None:
    """The downgrade restores the exact pre-0368 state: no policy, RLS disabled."""
    _ensure_head(rls_db)
    engine = _superuser(rls_db)
    try:
        seed = _seed(engine)
    finally:
        engine.dispose()

    assert _rls_enabled(rls_db) == _protected_state()
    for table in _TABLES:
        assert _visible(rls_db, seed, table, org=seed.org_a) == {_own_id(seed, table)}

    command.downgrade(rls_db.config, _PRE_CHANGE)

    assert _alembic_version(rls_db.sync_url) == _PRE_CHANGE
    assert _rls_enabled(rls_db) == _unprotected_state()
    assert not _policies(rls_db)
    for table in _TABLES:
        assert _visible(rls_db, seed, table, org=seed.org_a) == {_own_id(seed, table), _foreign_id(seed, table)}, f"{table}: RLS must be off again after the downgrade"

    # ...and the upgrade is re-appliable on the same database (no partial state).
    _ensure_head(rls_db)
    assert _rls_enabled(rls_db) == _protected_state()
    assert _policies(rls_db) == _expected_policies()
