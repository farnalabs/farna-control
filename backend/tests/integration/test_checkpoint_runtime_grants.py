"""FAR-2828 — runtime-created checkpoint tables must be usable by NON-owner roles.

The LangGraph checkpointer tables (``checkpoints``, ``checkpoint_blobs``,
``checkpoint_writes``, ``checkpoint_migrations``) are NOT Alembic objects: the
backend's own ``ModuloPostgresSaver.setup()`` creates them inside the lifespan,
as the APP role (``DATABASE_URL``). Before the fix the privilege template in
``farna.db.bootstrap_role`` only scoped ``ALTER DEFAULT PRIVILEGES`` to the role
ISSUING it (the admin/owner), so on a fresh install nothing granted the app
role's runtime tables to anyone else: ``saq-system`` logged

    permission denied for table checkpoints

every 5 minutes from the stale-run-recovery sweep, whose zero-progress arm
carries a ``NOT EXISTS (SELECT 1 FROM checkpoints cp ...)`` leg
(``core.cron_helpers._ZERO_PROGRESS_SHAPE_SQL``), and the arm could therefore
never run. The same tables are rewritten by Fernet rotation and purged by run
retention / org deletion, all on the cross-org system role.

These tests prove the CAPABILITY on a real Postgres (not the SQL text): the
system role reads, updates and deletes rows in tables it does not own.

Why a private database per test: ``pg_default_acl`` is per-database and the
defaults are keyed by the CREATOR role, so the scenario must be "the boot
privilege pass ran, THEN the app role created the tables". The integration
suite shares one Postgres across xdist workers, so dropping and recreating
``checkpoints`` in the shared schema would race the checkpoint tests (and other
tests create it as the superuser, which would not reproduce the bug at all). A
throwaway database makes the ordering exact and leaves the shared schema
untouched.

On Windows, pytest-asyncio runs the ``ProactorEventLoop``, which psycopg's async
connection refuses; the saver therefore runs on a dedicated
``SelectorEventLoop`` thread, as ``test_checkpoint_isolation.py`` does.
"""

from __future__ import annotations

import asyncio
import selectors
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.fernet import Fernet

from farna.core.pipeline_engine.modulo_saver import ModuloPostgresSaver
from farna.db.bootstrap_role import bootstrap_roles
from tests.integration.conftest import _with_credentials

pytestmark = [pytest.mark.integration]

_FERNET_KEY = Fernet.generate_key().decode()

_saver_loop: asyncio.AbstractEventLoop | None = None


def _run_saver(coro: Any) -> Any:
    """Run a psycopg-backed saver coroutine on a dedicated SelectorEventLoop."""
    global _saver_loop
    if _saver_loop is None or _saver_loop.is_closed():
        _saver_loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
        threading.Thread(target=_saver_loop.run_forever, name="far2828-saver-loop", daemon=True).start()
    future = asyncio.run_coroutine_threadsafe(coro, _saver_loop)
    return future.result(timeout=180)


def _asyncpg_dsn(url: str) -> str:
    """Convert a SQLAlchemy asyncpg URL into the DSN ``asyncpg.connect`` takes."""
    return url.replace("postgresql+asyncpg://", "postgres://", 1)


def _psycopg_url(url: str) -> str:
    """``from_conn_string`` connects via psycopg (v3), not the asyncpg driver."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_database(url: str, database: str) -> str:
    """Return *url* pointed at another database on the same server."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


@dataclass(frozen=True)
class _ProbeDb:
    """URLs for one throwaway database, one per role under test."""

    database: str
    admin_url: str
    app_url: str
    system_url: str


@asynccontextmanager
async def _probe_database(migrated_db_url: str) -> AsyncIterator[_ProbeDb]:
    """Create a throwaway database and drop it again, whatever happens.

    Roles are cluster-global, so the suite's provisioned
    ``farna_control_app`` / ``farna_control_system`` credentials work unchanged
    against the new database; only ``pg_default_acl`` needs to start empty,
    which is exactly what a fresh database gives us.
    """
    database = f"far2828_probe_{uuid.uuid4().hex[:12]}"
    maintenance = await asyncpg.connect(_asyncpg_dsn(migrated_db_url))
    try:
        await maintenance.execute(f'CREATE DATABASE "{database}"')
        yield _ProbeDb(
            database=database,
            admin_url=_with_database(migrated_db_url, database),
            app_url=_with_credentials(_with_database(migrated_db_url, database), "farna_control_app", "apppass"),
            system_url=_with_credentials(_with_database(migrated_db_url, database), "farna_control_system", "syspass"),
        )
    finally:
        # A failed assertion can leave a psycopg connection open; DROP DATABASE
        # refuses while anyone is connected, so evict them first (throwaway DB).
        await maintenance.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            database,
        )
        await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await maintenance.close()


def _create_checkpoint_tables_as_the_app_role(app_url: str) -> None:
    """Run the REAL boot-time saver setup as ``farna_control_app``.

    This is the fresh-install path verbatim: the app role issues
    ``CREATE TABLE IF NOT EXISTS checkpoints ...`` (plus the blobs/writes
    siblings) from inside the backend lifespan, after the privilege bootstrap.
    """

    async def _setup() -> None:
        async with ModuloPostgresSaver.from_conn_string(
            app_url,
            organisation_id=uuid.uuid4(),
            fernet_key=_FERNET_KEY,
        ) as saver:
            await saver.setup()

    _run_saver(_setup())


async def _relation_owner(admin: asyncpg.Connection, table: str) -> str:
    owner = await admin.fetchval(
        "SELECT pg_get_userbyid(c.relowner) FROM pg_class c WHERE c.oid = to_regclass($1)",
        f"public.{table}",
    )
    assert owner is not None, f"{table} does not exist"
    return str(owner)


@pytest.fixture
def organisation_id() -> uuid.UUID:
    return uuid.uuid4()


async def _assert_non_owner_system_role_can_use_the_tables(
    admin: asyncpg.Connection,
    system_url: str,
    organisation_id: uuid.UUID,
) -> None:
    """Prove the system role's capabilities on tables owned by the app role.

    Each statement mirrors a real non-owner consumer:

    * the stale-run-recovery sweep's zero-progress arm reads ``checkpoints``;
    * Fernet rotation rewrites ``checkpoints`` / ``checkpoint_blobs`` /
      ``checkpoint_writes``;
    * run retention and org deletion DELETE from all three.
    """
    thread_id = f"thread-far2828-{uuid.uuid4().hex[:8]}"

    # Ownership is the point of the test: the system role's access must be a
    # GRANT, never the implicit privilege of owning the table.
    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
        assert await _relation_owner(admin, table) == "farna_control_app", f"{table} must be created by the app role, as in production"

    conn = await asyncpg.connect(_asyncpg_dsn(system_url))
    try:
        assert await conn.fetchval("SELECT current_user") == "farna_control_system"

        # The sweep's read, shaped like ``_ZERO_PROGRESS_SHAPE_SQL``'s NOT EXISTS leg.
        rows = await conn.fetch(
            "SELECT 1 FROM checkpoints cp WHERE cp.organisation_id = $1 AND cp.thread_id = $2",
            organisation_id,
            thread_id,
        )
        assert rows == []

        # Fernet rotation (SELECT + UPDATE) and retention (SELECT + DELETE).
        assert await conn.execute("UPDATE checkpoints SET metadata = metadata WHERE organisation_id = $1", organisation_id) == "UPDATE 0"
        assert await conn.execute("UPDATE checkpoint_blobs SET blob = blob WHERE organisation_id = $1", organisation_id) == "UPDATE 0"
        assert await conn.execute("UPDATE checkpoint_writes SET blob = blob WHERE organisation_id = $1", organisation_id) == "UPDATE 0"
        assert await conn.execute("DELETE FROM checkpoints WHERE organisation_id = $1", organisation_id) == "DELETE 0"
        assert await conn.execute("DELETE FROM checkpoint_blobs WHERE organisation_id = $1", organisation_id) == "DELETE 0"
        assert await conn.execute("DELETE FROM checkpoint_writes WHERE organisation_id = $1", organisation_id) == "DELETE 0"
    finally:
        await conn.close()

    # No world-readable grant may come with it. ``aclexplode`` reports grantee 0
    # for PUBLIC (``information_schema`` hides grants on roles you are not in).
    public_grants = await admin.fetchval(
        "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = to_regclass('public.checkpoints') AND a.grantee = 0",
    )
    assert public_grants == 0, "checkpoints must never be granted to PUBLIC"


async def test_runtime_created_checkpoint_tables_are_usable_by_the_system_role(
    migrated_db_url: str,
    organisation_id: uuid.UUID,
) -> None:
    """FRESH INSTALL (the observed bug): bootstrap, then the app creates the tables.

    Without the app-scoped ``ALTER DEFAULT PRIVILEGES`` the system role is denied
    the tables the app role just created and every statement below raises
    ``asyncpg.exceptions.InsufficientPrivilegeError`` (42501).
    """
    async with _probe_database(migrated_db_url) as probe:
        admin = await asyncpg.connect(_asyncpg_dsn(probe.admin_url))
        try:
            # 1. The boot privilege pass — runs BEFORE the tables exist here, just
            #    as ``_run_migrations`` does on a brand-new database.
            await bootstrap_roles(probe.admin_url, probe.app_url)

            # 2. The backend lifespan creates them, as the app role.
            _create_checkpoint_tables_as_the_app_role(_psycopg_url(probe.app_url))

            # 3. The non-owner consumers can now use them.
            await _assert_non_owner_system_role_can_use_the_tables(admin, probe.system_url, organisation_id)
        finally:
            await admin.close()


async def test_an_install_whose_runtime_tables_already_exist_is_repaired_on_boot(
    migrated_db_url: str,
    organisation_id: uuid.UUID,
) -> None:
    """EXISTING INSTALL: the ungranted tables are repaired by the next boot.

    Simulates a database created before the fix — tables present, no grants for
    the system role (which is what ``pg_class.relacl`` showed for
    ``checkpoints``/``checkpoint_migrations`` on the reporting host) — and
    asserts that re-running the boot bootstrap restores the capability. This is
    the ``GRANT ... ON ALL TABLES`` half of the fix: it can only see a runtime
    table once that table exists.
    """
    async with _probe_database(migrated_db_url) as probe:
        admin = await asyncpg.connect(_asyncpg_dsn(probe.admin_url))
        try:
            await bootstrap_roles(probe.admin_url, probe.app_url)
            _create_checkpoint_tables_as_the_app_role(_psycopg_url(probe.app_url))

            # Rewind to the pre-fix state: the app role owns the tables and no
            # other role holds anything on them.
            await admin.execute("REVOKE ALL ON TABLE checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations FROM farna_control_system")

            conn = await asyncpg.connect(_asyncpg_dsn(probe.system_url))
            try:
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    await conn.fetch("SELECT 1 FROM checkpoints")
            finally:
                await conn.close()

            # The next boot reconciles it — no manual step, no restart ordering.
            await bootstrap_roles(probe.admin_url, probe.app_url)
            await _assert_non_owner_system_role_can_use_the_tables(admin, probe.system_url, organisation_id)

            # The boot pass is idempotent: running it again changes nothing.
            await bootstrap_roles(probe.admin_url, probe.app_url)
            await _assert_non_owner_system_role_can_use_the_tables(admin, probe.system_url, organisation_id)
        finally:
            await admin.close()
