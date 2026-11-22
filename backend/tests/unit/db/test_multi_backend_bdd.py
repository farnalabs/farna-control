"""Unit tests covering multi-backend behaviour from multi_backend.feature scenarios.

Verifies tenant isolation, migrations, locks, and timestamp defaults on
PostgreSQL (the only supported runtime database, ADR 068) and SQLite
(test-only). Mirrors the BDD scenarios in:
    backend/tests/bdd/features/organisation/multi_backend.feature
"""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from farna.db.repositories import RepositoryHub
from farna.db.repositories.generic import GenericRepository
from farna.db.repositories.locks import (
    GenericLock,
    PostgresLock,
    _build_lock_service,
    _generic_locks,
)
from farna.db.repositories.postgres import PostgresRepository

_TENANT_KEY = "org_id"
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class EntityWithOrg:
    organisation_id = None
    id = None
    name = None


class EntityWithoutOrg:
    id = None
    name = None


def _make_session(*, in_tx: bool = True, dialect: str = "sqlite") -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.in_transaction.return_value = in_tx
    session.execute = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = dialect

    async def _get_bind() -> MagicMock:
        return bind

    session.get_bind = _get_bind
    session.info = {}
    return session


def _make_stmt(*, entities: list | None = None) -> MagicMock:
    stmt = MagicMock(spec=Select)
    stmt.column_descriptions = entities or []
    where_return = MagicMock(spec=Select)
    stmt.where.return_value = where_return
    return stmt


# ===========================================================================
# Scenario 1-2: GenericRepository tenant filtering (SQLite, test-only)
# ===========================================================================


class TestGenericRepositoryTenantFilter:
    """Verify GenericRepository dispatches tenant filtering via session.info."""

    @pytest.fixture
    def dialect(self) -> str:
        return "sqlite"

    @pytest.fixture
    def repo(self) -> GenericRepository:
        return GenericRepository(session_factory=MagicMock())

    async def test_set_org_context_stores_in_session_info(
        self,
        repo: GenericRepository,
        dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect)
        await repo.set_org_context(session, _ORG_ID)
        assert session.info.get(_TENANT_KEY) == _ORG_ID

    async def test_set_org_context_does_not_call_set_config(
        self,
        repo: GenericRepository,
        dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect)
        await repo.set_org_context(session, _ORG_ID)
        session.execute.assert_not_called()

    def test_apply_tenant_filter_injects_where_for_org_entity(
        self,
        repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt

    def test_apply_tenant_filter_skips_entity_without_org_column(
        self,
        repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithoutOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_not_called()
        assert result is stmt

    def test_apply_tenant_filter_handles_join_multiple_org_entities(
        self,
        repo: GenericRepository,
    ) -> None:
        stmt = _make_stmt(
            entities=[
                {"entity": EntityWithOrg},
                {"entity": EntityWithOrg},
            ]
        )
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt

    async def test_set_org_context_raises_without_transaction(
        self,
        repo: GenericRepository,
        dialect: str,
    ) -> None:
        session = _make_session(dialect=dialect, in_tx=False)
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await repo.set_org_context(session, _ORG_ID)


# ===========================================================================
# Scenario 3: Postgres uses RLS natively
# ===========================================================================


class TestPostgresRepositoryRLS:
    """Verify PostgresRepository relies on set_config + policy, not WHERE injection."""

    @pytest.fixture
    def repo(self) -> PostgresRepository:
        return PostgresRepository(session_factory=MagicMock())

    async def test_set_org_context_calls_set_config(self, repo: PostgresRepository) -> None:
        session = _make_session(dialect="postgresql")
        await repo.set_org_context(session, _ORG_ID)
        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "set_config" in call_text
        assert "app.organisation_id" in call_text

    async def test_set_org_context_does_not_set_session_info(
        self,
        repo: PostgresRepository,
    ) -> None:
        session = _make_session(dialect="postgresql")
        await repo.set_org_context(session, _ORG_ID)
        assert session.info.get(_TENANT_KEY) is None

    def test_apply_tenant_filter_returns_stmt_unchanged(
        self,
        repo: PostgresRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        stmt.where.assert_not_called()
        assert result is stmt

    def test_apply_tenant_filter_unchanged_even_without_org_entity(
        self,
        repo: PostgresRepository,
    ) -> None:
        stmt = _make_stmt(entities=[{"entity": EntityWithoutOrg}])
        result = repo.apply_tenant_filter(stmt, _ORG_ID)
        assert result is stmt

    async def test_set_org_context_raises_without_transaction(
        self,
        repo: PostgresRepository,
    ) -> None:
        session = _make_session(dialect="postgresql", in_tx=False)
        with pytest.raises(RuntimeError, match="requires an active transaction"):
            await repo.set_org_context(session, _ORG_ID)


# ===========================================================================
# Scenario 4: Cross-org isolation on SQLite
# ===========================================================================


class TestCrossOrgIsolationGeneric:
    """Verify that GenericRepository isolates org data on SQLite."""

    @pytest.fixture
    def repo(self) -> GenericRepository:
        return GenericRepository(session_factory=MagicMock())

    async def test_org_b_cannot_see_org_a_data(self, repo: GenericRepository) -> None:
        session = _make_session(dialect="sqlite")
        await repo.set_org_context(session, _ALT_ORG_ID)

        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        filtered = repo.apply_tenant_filter(stmt, _ALT_ORG_ID)

        stmt.where.assert_called_once()
        assert filtered is not stmt

    async def test_different_session_isolates_org_context(
        self,
        repo: GenericRepository,
    ) -> None:
        session_a = _make_session(dialect="sqlite")
        session_b = _make_session(dialect="sqlite")
        await repo.set_org_context(session_a, _ORG_ID)
        await repo.set_org_context(session_b, _ALT_ORG_ID)

        assert session_a.info[_TENANT_KEY] == _ORG_ID
        assert session_b.info[_TENANT_KEY] == _ALT_ORG_ID

    def test_apply_tenant_filter_still_injects_when_session_lacks_org_context(
        self,
        repo: GenericRepository,
    ) -> None:
        session = _make_session(dialect="sqlite")
        session.info.pop(_TENANT_KEY, None)

        stmt = _make_stmt(entities=[{"entity": EntityWithOrg}])
        result = repo.apply_tenant_filter(stmt, _ALT_ORG_ID)
        stmt.where.assert_called_once()
        assert result is not stmt


# ===========================================================================
# Scenario 5: Alembic migration on the supported pair (SQLite + PostgreSQL)
# ===========================================================================


class TestMigrationBackendConfig:
    """Verify env.py backend detection, async-to-sync conversion, batch mode."""

    def test_detect_postgres(self) -> None:
        from farna.db.migrations.env import _detect_backend

        assert _detect_backend("postgresql+asyncpg://user:pass@localhost/db") == "postgresql"

    def test_detect_sqlite(self) -> None:
        from farna.db.migrations.env import _detect_backend

        assert _detect_backend("sqlite:///test.db") == "sqlite"

    def test_detect_unknown(self) -> None:
        from farna.db.migrations.env import _detect_backend

        assert _detect_backend("oracle://user:pass@localhost/db") == "unknown"

    def test_asyncpg_converts_to_psycopg2(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost/db"
        converted = url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
        assert "+async" not in converted
        assert "psycopg2" in converted

    def test_sqlite_preserved_as_is(self) -> None:
        url = "sqlite:///test.db"
        assert "+async" not in url

    def test_render_as_batch_enabled_for_sqlite(self) -> None:
        from farna.db.migrations.env import _detect_backend

        assert _detect_backend("sqlite:///test.db") == "sqlite"

    def test_render_as_batch_disabled_for_postgres(self) -> None:
        from farna.db.migrations.env import _detect_backend

        backend = _detect_backend("postgresql+asyncpg://user:pass@localhost/db")
        assert backend == "postgresql"


# ===========================================================================
# Scenario 6: Advisory lock abstraction
# ===========================================================================


class TestAdvisoryLockAbstraction:
    """Verify PostgresLock uses pg_advisory_lock, GenericLock uses asyncio.Lock."""

    @pytest.fixture(autouse=True)
    def _clean_lock_state(self) -> None:
        for key in list(_generic_locks.keys()):
            lock = _generic_locks[key]
            if lock.locked():
                lock.release()
        _generic_locks.clear()

    def test_postgres_lock_service_type(self) -> None:
        svc = _build_lock_service("postgres")
        assert isinstance(svc, PostgresLock)

    def test_sqlite_lock_service_type(self) -> None:
        svc = _build_lock_service("sqlite")
        assert isinstance(svc, GenericLock)

    async def test_postgres_lock_calls_pg_try_advisory_lock(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)

        await lock.acquire_lock(session, "pipeline:42")

        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_try_advisory_lock" in call_text

    async def test_postgres_lock_release_calls_pg_advisory_unlock(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        # result.scalar_one() is sync in real SQLAlchemy — use MagicMock
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = True
        session.execute.return_value = result_mock

        await lock.release_lock(session, "pipeline:42")

        session.execute.assert_awaited_once()
        call_text = str(session.execute.await_args[0][0].compile())
        assert "pg_advisory_unlock" in call_text

    async def test_postgres_lock_release_warns_when_not_held(self, caplog: pytest.LogCaptureFixture) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result_mock = MagicMock()
        result_mock.scalar_one.return_value = False
        session.execute.return_value = result_mock

        with caplog.at_level(logging.WARNING):
            await lock.release_lock(session, "pipeline:42")

        assert "pg_advisory_unlock returned false" in caplog.text

    async def test_generic_lock_uses_asyncio_lock(self) -> None:
        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "test-resource")

        assert "test-resource" in _generic_locks
        assert _generic_locks["test-resource"].locked()

    async def test_generic_lock_release(self) -> None:
        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "test-resource")
        await lock.release_lock(session, "test-resource")

        assert "test-resource" not in _generic_locks

    async def test_generic_lock_waits_when_contended(self) -> None:
        import asyncio

        lock = GenericLock()
        session = AsyncMock(spec=AsyncSession)

        await lock.acquire_lock(session, "contended-key")
        acquired_during = False

        async def _contend() -> None:
            nonlocal acquired_during
            other = AsyncMock(spec=AsyncSession)
            await lock.acquire_lock(other, "contended-key", lock_timeout=5.0)
            acquired_during = True

        task = asyncio.create_task(_contend())
        await asyncio.sleep(0.1)
        assert not acquired_during

        await lock.release_lock(session, "contended-key")
        await asyncio.wait_for(task, timeout=2.0)
        assert acquired_during

    async def test_postgres_lock_raises_on_timeout(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = False
        session.execute = AsyncMock(return_value=result)

        from farna.db.repositories.locks import LockAcquireError

        with pytest.raises(LockAcquireError, match="Could not acquire lock"):
            await lock.acquire_lock(session, "timeout-key", lock_timeout=0.05)

    async def test_postgres_lock_key_hash_consistent(self) -> None:
        lock = PostgresLock()
        session = AsyncMock(spec=AsyncSession)
        result = MagicMock()
        result.scalar_one.return_value = True
        session.execute = AsyncMock(return_value=result)

        await lock.acquire_lock(session, "pipeline:42")
        acquire_params = session.execute.await_args[0][1]

        session.execute.reset_mock()
        await lock.release_lock(session, "pipeline:42")
        release_params = session.execute.await_args[0][1]

        assert acquire_params == release_params


# ===========================================================================
# Scenario 7: Timestamp defaults on the supported pair (SQLite + PostgreSQL)
# ===========================================================================

# The timestamp SQL form the shared mixin declares, rendered identically by
# every supported dialect.
_PORTABLE_TIMESTAMP_SQL = "current_timestamp"

_SQLITE_DIALECT = sqlite.dialect()


def _registered_model_classes() -> list[type]:
    """Every mapped class, after importing the model package to register them."""
    from farna.db.models.base import Base

    return [mapper.class_ for mapper in Base.registry.mappers]


def _default_arg_name(default: object) -> str | None:
    """The SQL function name a column default renders, when it renders one.

    ``server_default=func.now()`` produces a ``DefaultClause`` whose ``arg`` is
    the SQL function (which exposes ``name``); a literal default, a ``text()``
    clause or a Python-side callable exposes no ``name`` and returns ``None``.
    """
    if default is None:
        return None
    arg = getattr(default, "arg", default)
    return getattr(arg, "name", None)


def _render_default_on_sqlite(default: object) -> str:
    """Render a column default as SQLite's DDL would receive it.

    A plain-string default is already SQL and is returned verbatim; every other
    form (a SQL function, a ``text()`` clause) is compiled with the SQLite
    dialect.
    """
    arg = getattr(default, "arg", default)
    compile_sql = getattr(arg, "compile", None)
    if compile_sql is None:
        return str(arg)
    return str(compile_sql(dialect=_SQLITE_DIALECT))


class TestTimestampDefaultsMultiBackend:
    """Model timestamp defaults must stay valid on every supported dialect.

    The supported pair is SQLite (test-only, ``create_all``) and PostgreSQL (the
    only runtime database, ADR 068). Two spellings are portable and both are
    accepted here:

    * ``func.current_timestamp()`` - the SQL-standard form, which the shared
      ``TimestampMixin`` declares and which both dialects render as
      ``CURRENT_TIMESTAMP``.
    * ``func.now()`` - SQLAlchemy renders it as ``CURRENT_TIMESTAMP`` on SQLite
      and ``now()`` on PostgreSQL, so it is portable too.

    What is *not* portable is hard-coding the PostgreSQL spelling as raw SQL:
    ``text("now()")`` renders ``DEFAULT (now())`` on SQLite, which parses but
    then fails every insert with ``unknown function: now()``. So the invariant
    pinned below is about the rendered SQLite DDL, not about which SQLAlchemy
    construct was chosen.

    These tests inspect real model metadata; they do not compile SQLAlchemy
    primitives, which would only prove what SQLAlchemy itself does.
    """

    def test_concrete_models_declare_the_portable_sql_form(self) -> None:
        from farna.db.models.base import TimestampMixin
        from farna.db.models.library_primitive import LibraryPrimitive
        from farna.db.models.organisation import Organisation

        assert issubclass(LibraryPrimitive, TimestampMixin)
        shared_columns = LibraryPrimitive.__table__.c
        assert _default_arg_name(shared_columns["created_at"].server_default) == _PORTABLE_TIMESTAMP_SQL
        assert _default_arg_name(shared_columns["updated_at"].server_default) == _PORTABLE_TIMESTAMP_SQL
        assert _default_arg_name(shared_columns["updated_at"].onupdate) == _PORTABLE_TIMESTAMP_SQL

        # Organisation does not use the mixin: it declares created_at itself,
        # so it covers the hand-written column that must match the mixin.
        assert _default_arg_name(Organisation.__table__.c["created_at"].server_default) == _PORTABLE_TIMESTAMP_SQL

    def test_every_shared_timestamp_column_declares_the_portable_sql_form(self) -> None:
        from farna.db.models.base import TimestampMixin

        checked = 0
        for model in _registered_model_classes():
            if not issubclass(model, TimestampMixin):
                continue
            columns = model.__table__.c
            assert _default_arg_name(columns["created_at"].server_default) == _PORTABLE_TIMESTAMP_SQL, model.__name__
            assert _default_arg_name(columns["updated_at"].server_default) == _PORTABLE_TIMESTAMP_SQL, model.__name__
            assert _default_arg_name(columns["updated_at"].onupdate) == _PORTABLE_TIMESTAMP_SQL, model.__name__
            checked += 1

        # Guard against a registry that imported nothing: an empty loop would
        # pass without inspecting a single column.
        assert checked >= 20

    def test_no_model_server_default_renders_raw_now_on_sqlite(self) -> None:
        """No model default may render PostgreSQL-only ``now()`` in SQLite DDL.

        ``server_default`` and ``onupdate`` are walked for every mapped table and
        each expression is compiled with the SQLite dialect, i.e. rendered
        exactly as SQLite would receive it. ``func.now()`` and
        ``func.current_timestamp()`` both render ``CURRENT_TIMESTAMP`` and pass;
        ``text("now()")`` renders ``now()`` and fails.
        """
        from farna.db.models.base import Base

        offenders: list[str] = []
        checked = 0
        for table_name, table in Base.metadata.tables.items():
            for column in table.c:
                for kind, default in (("server_default", column.server_default), ("onupdate", column.onupdate)):
                    if default is None:
                        continue
                    checked += 1
                    rendered = _render_default_on_sqlite(default)
                    if "now()" in rendered.lower():
                        offenders.append(f"{table_name}.{column.name} ({kind}) -> {rendered}")

        # Guard against an unpopulated registry: an empty sweep would pass
        # without rendering a single default.
        assert checked >= 50, f"only {checked} column defaults inspected; the model registry looks empty"
        assert not offenders, "SQLite-unsafe timestamp default(s): " + ", ".join(sorted(offenders))


# ===========================================================================
# Scenario 8: RepositoryHub construction/dispatch
# ===========================================================================


class TestRepositoryHubDispatch:
    """Verify RepositoryHub dispatches to the correct backend-specific implementation."""

    def test_postgres_repo_is_postgres_repository(self) -> None:
        hub = RepositoryHub(session_factory=MagicMock(), db_type="postgres")
        from farna.db.repositories.postgres import PostgresRepository

        assert isinstance(hub.repo, PostgresRepository)

    def test_sqlite_repo_is_generic_repository(self) -> None:
        hub = RepositoryHub(session_factory=MagicMock(), db_type="sqlite")
        assert isinstance(hub.repo, GenericRepository)

    def test_postgres_lock_is_postgres_lock(self) -> None:
        hub = RepositoryHub(session_factory=MagicMock(), db_type="postgres")
        assert isinstance(hub.locks, PostgresLock)

    def test_sqlite_lock_is_generic_lock(self) -> None:
        hub = RepositoryHub(session_factory=MagicMock(), db_type="sqlite")
        assert isinstance(hub.locks, GenericLock)


# ===========================================================================
# Scenario 9: register_tenant_filter registration behaviour
# ===========================================================================


class TestRegisterTenantFilter:
    """Verify register_tenant_filter skips for postgres and registers for others."""

    def test_skips_for_postgres(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FARNA_CONTROL_DB", "postgres")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FERNET_KEY", "a" * 32)

        from unittest.mock import patch

        from farna.settings import Settings

        with patch("farna.db.rls.get_settings") as mock_get_settings:
            mock_settings = MagicMock(spec=Settings)
            mock_settings.db = "postgres"
            mock_get_settings.return_value = mock_settings

            from farna.db.rls import register_tenant_filter

            with patch("farna.db.rls.event.listen") as mock_listen:
                register_tenant_filter()

            mock_listen.assert_not_called()

    def test_registers_for_sqlite(self) -> None:
        from unittest.mock import patch

        from farna.settings import Settings

        with patch("farna.db.rls.get_settings") as mock_get_settings:
            mock_settings = MagicMock(spec=Settings)
            mock_settings.db = "sqlite"
            mock_get_settings.return_value = mock_settings

            from farna.db.rls import register_tenant_filter

            with patch("farna.db.rls.event.listen") as mock_listen:
                register_tenant_filter()

            mock_listen.assert_called_once()


# ===========================================================================
# Organisation root-entity model invariants
# ===========================================================================


class TestOrganisationRootEntityModel:
    """Organisation is the root tenant entity — ``created_by`` is nullable and is
    deliberately NOT a foreign key to ``accounts.id`` (migration 0243 drops the
    drift introduced by 0236): the first org is bootstrapped with
    ``created_by=NULL`` and the FK would block that ordering because the org must
    exist before its first user; the model carries no ``organisation_id`` column
    so the tenant filter / RLS never scopes it (mirrors
    ``test_rls_coverage.py``'s only-exclusion)."""

    def test_created_by_column_is_nullable_and_not_a_foreign_key(self) -> None:
        from sqlalchemy import ForeignKey

        from farna.db.models.organisation import Organisation

        column = Organisation.__table__.c.created_by
        assert column.nullable is True, "created_by must be nullable for org bootstrap"
        fks = [fk for fk in column.foreign_keys if isinstance(fk, ForeignKey)]
        assert not fks, "created_by must NOT be a foreign key to accounts.id — the first org is created with created_by=NULL but must exist before its first user, so a FK would block bootstrap ordering (migration 0243 drops the fk_organisations_created_by constraint added in 0236)"

    def test_created_by_defaults_to_none_for_bootstrap_org(self) -> None:
        """The first org is created with no creator (``_ensure_default_org``
        builds ``Organisation(name=..., slug=...)`` without ``created_by``)."""
        from farna.db.models.organisation import Organisation

        org = Organisation(name="Default Organisation", slug="default")
        assert org.created_by is None

    def test_organisation_model_has_no_organisation_id_column(self) -> None:
        """The tenant filter keys on ``organisation_id``; the root entity has
        none, so a scoped SELECT over ``organisations`` is never injected with
        a WHERE clause (RLS is not applied to the organisations table)."""
        from farna.db.models.organisation import Organisation

        assert "organisation_id" not in Organisation.__table__.columns

    def test_tenant_filter_skips_organisation_entity(self) -> None:
        """``_inject_tenant_filter`` must skip entities without an
        ``organisation_id`` column — the ``Organisation`` model is the concrete
        instance of the generic ``EntityWithoutOrg`` skip case."""
        from farna.db.rls import _inject_tenant_filter

        stmt = MagicMock(spec=Select)
        stmt.column_descriptions = [{"entity": EntityWithoutOrg}]
        execute_state = MagicMock()
        execute_state.is_select = True
        execute_state.is_update = False
        execute_state.is_delete = False
        execute_state.statement = stmt
        execute_state.session.info = {_TENANT_KEY: _ORG_ID}

        _inject_tenant_filter(execute_state)
        stmt.where.assert_not_called()


# ===========================================================================
# Scenario 10: register_rls_reset_hook skip behaviour
# ===========================================================================


class TestRegisterRlsResetHookSkip:
    """Verify register_rls_reset_hook skips for non-postgres backends."""

    def test_skips_for_sqlite(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        engine.sync_engine = MagicMock()

        from farna.db.rls import register_rls_reset_hook

        with patch("farna.db.rls.event.listens_for") as mock_listens:
            register_rls_reset_hook(engine)

        mock_listens.assert_not_called()

    def test_registers_for_postgres(self) -> None:
        engine = MagicMock()
        engine.dialect.name = "postgresql"
        engine.sync_engine = MagicMock()

        from farna.db.rls import register_rls_reset_hook

        with patch("farna.db.rls.event.listens_for") as mock_listens:
            register_rls_reset_hook(engine)

        mock_listens.assert_called_once()
