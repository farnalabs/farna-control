"""FAR-2803: the boot environment-profile seed must land a profile for the TENANT org.

What went wrong on every fresh install: ``_seed_environment_profiles`` ran as the
application role (``farna_control_app``, ``LOGIN NOBYPASSRLS``) and

* never bound an RLS context, so the INSERT carried no ``app.organisation_id`` and
  ``environment_profiles.rls_team_isolation`` rejected it ("new row violates
  row-level security policy for table environment_profiles"); and
* even with a context it read ``Organisation … order_by(created_at).limit(1)``,
  which picks the sentinel "Orphan (unattributed errors)" org the baseline
  migration seeds first - the trap ``first_tenant_organisation`` exists to avoid
  (FAR-2581).

A mocked session cannot see either defect: the unit tests for this seeder use a
fake session, so no policy and no org ordering ever runs. This module uses a real
Postgres 16, the real Alembic chain, and the real application role, so both
defects fail it.

The database is driven through the same steps a fresh boot performs: migrations
(sentinel orgs), ``ensure_default_organisation`` (the tenant org), the admin
account, then the seeder itself with the real engine/session factory.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.community.postgres import PostgresContainer

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]

#: The runtime role the app boots as: LOGIN, NOSUPERUSER, NOBYPASSRLS, so the
#: table policies actually apply (a superuser session would bypass them and hide
#: the bug). Migration 0352 grants it the table privileges it needs.
_APP_ROLE = "farna_control_app"
_APP_PASSWORD = "apppass"


@dataclass(frozen=True)
class _BootSeedDatabase:
    """A migrated database plus the identity the boot would give a fresh install."""

    #: asyncpg URL of the container superuser (assertions only - bypasses RLS).
    superuser_url: str
    #: asyncpg URL as ``farna_control_app``, the role the seeder runs as in production.
    app_url: str
    #: The tenant organisation ``ensure_default_organisation`` created.
    tenant_org_id: uuid.UUID


def _async_url(raw_url: str) -> str:
    return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1).replace("psycopg2", "asyncpg")


def _sync_url(asyncpg_url: str) -> str:
    from farna.db.migrations import env as migration_env

    return migration_env._to_sync_url(asyncpg_url)


def _with_credentials(database_url: str, user: str, password: str) -> str:
    prefix, _, rest = database_url.partition("://")
    host_part, _, db = rest.partition("/")
    host = host_part.split("@")[-1]
    return f"{prefix}://{quote(user)}:{quote(password)}@{host}/{db}"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option("script_location", str(BACKEND_ROOT / "src" / "farna" / "db" / "migrations"))
    # Skip fileConfig so env.py does not disable every module logger (see the
    # integration conftest's migrated_db_url).
    config.config_file_name = None
    return config


def _provision_runtime_roles(sync_url: str) -> None:
    """Create the roles BEFORE the migration, so 0352's guarded GRANTs apply.

    ``0352_baseline_privileges`` only grants where the role already exists, so a
    chain run without them leaves ``farna_control_app`` unable to read the tables
    (permission denied instead of the RLS failure this module is about).
    """
    engine = sa.create_engine(sync_url)
    try:
        with engine.connect() as conn:
            for role in ("farna_control_migrate", "farna_control_breakglass", _APP_ROLE):
                conn.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
            conn.execute(sa.text("CREATE ROLE farna_control_migrate NOSUPERUSER NOLOGIN BYPASSRLS"))
            conn.execute(sa.text("CREATE ROLE farna_control_breakglass LOGIN BYPASSRLS PASSWORD 'bgpass'"))
            conn.execute(sa.text(f"CREATE ROLE {_APP_ROLE} NOSUPERUSER NOBYPASSRLS LOGIN PASSWORD '{_APP_PASSWORD}'"))
            conn.commit()
    finally:
        engine.dispose()


async def _seed_default_tenant(superuser_url: str) -> uuid.UUID:
    """Do what a fresh boot does before the profile seed: the tenant org + an admin."""
    from farna.db.seed import ensure_default_organisation

    engine = create_async_engine(superuser_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session, session.begin():
            org, _created = await ensure_default_organisation(session)
            tenant_org_id: uuid.UUID = org.id
        async with factory() as session, session.begin():
            await session.execute(
                sa.text(
                    "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) VALUES (:id, 'admin', 'Admin', 'x', 'local', true)",
                ),
                {"id": str(uuid.uuid4())},
            )
        return tenant_org_id
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def boot_seed_db() -> Iterator[_BootSeedDatabase]:
    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    try:
        superuser_url = _async_url(pg.get_connection_url())
        _provision_runtime_roles(_sync_url(superuser_url))

        with pytest.MonkeyPatch.context() as patch:
            # env.py resolves the migration target from DATABASE_URL/_ADMIN_URL.
            patch.setenv("DATABASE_URL", superuser_url)
            patch.setenv("DATABASE_ADMIN_URL", superuser_url)
            command.upgrade(_alembic_config(superuser_url), "heads")

        tenant_org_id = asyncio.run(_seed_default_tenant(superuser_url))
        yield _BootSeedDatabase(
            superuser_url=superuser_url,
            app_url=_with_credentials(superuser_url, _APP_ROLE, _APP_PASSWORD),
            tenant_org_id=tenant_org_id,
        )
    finally:
        pg.stop()


async def _run_boot_seed(database: _BootSeedDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the REAL seeder as ``farna_control_app`` with a real engine and factory."""
    from farna.api import dependencies, main
    from farna.settings import Settings

    engine = create_async_engine(database.app_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # BOTH names: the seeder imports them from farna.api.dependencies inside
        # the function, and other boot paths reach them through the module.
        monkeypatch.setattr(dependencies, "get_or_create_engine", lambda _settings: engine)
        monkeypatch.setattr(dependencies, "get_or_create_session_factory", lambda _engine: factory)
        monkeypatch.setattr(main, "get_or_create_engine", lambda _settings: engine)
        # The seams above are patched, so this Settings only has to be constructible;
        # the values mirror the integration conftest's explicit construction.
        settings = Settings(
            database_url=database.app_url,
            secret_key="a" * 32,
            fernet_key="a" * 32,
            csrf_enabled=False,
            auth_rate_limit_enabled=False,
            redis_url="",
            admin_password="",
        )
        await main._seed_environment_profiles(settings)
    finally:
        await engine.dispose()


def _profiles(database: _BootSeedDatabase) -> list[tuple[str, str, str]]:
    """Every environment profile (read as the superuser so RLS cannot hide a row)."""
    engine = sa.create_engine(_sync_url(database.superuser_url))
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT name, provider_type, organisation_id::text FROM environment_profiles ORDER BY name")).fetchall()
        return [(str(name), str(provider_type), str(org_id)) for name, provider_type, org_id in rows]
    finally:
        engine.dispose()


async def test_boot_seed_creates_the_bundled_runner_profile_for_the_tenant_org(
    boot_seed_db: _BootSeedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FAR-2803: a fresh boot seeds the shipped profile, in the tenant org, under RLS."""
    from farna.db.bundled_runner_template import BUNDLED_RUNNER_PROVIDER_TYPE, TEMPLATE_PROFILE_NAME
    from farna.db.crud.organisation import _SENTINEL_ORG_IDS

    await _run_boot_seed(boot_seed_db, monkeypatch)

    assert _profiles(boot_seed_db) == [(TEMPLATE_PROFILE_NAME, BUNDLED_RUNNER_PROVIDER_TYPE, str(boot_seed_db.tenant_org_id))]

    # The exact defect the old code had: the first org by created_at is the
    # "Orphan (unattributed errors)" sentinel, so the row existed but belonged to
    # no tenant (and was RLS-invisible to every real session).
    seeded_org = uuid.UUID(_profiles(boot_seed_db)[0][2])
    assert seeded_org == boot_seed_db.tenant_org_id
    assert seeded_org not in _SENTINEL_ORG_IDS


async def test_boot_seed_is_idempotent_across_boots(
    boot_seed_db: _BootSeedDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running the seed must not duplicate or collide on uq_environment_profiles_org_name.

    This pins the ordering inside the seeder: ``set_rls_org`` has to come BEFORE
    the existence read, or the already-seeded row is RLS-invisible and the insert
    collides with the unique index on a second boot.
    """
    await _run_boot_seed(boot_seed_db, monkeypatch)
    await _run_boot_seed(boot_seed_db, monkeypatch)

    assert len(_profiles(boot_seed_db)) == 1
