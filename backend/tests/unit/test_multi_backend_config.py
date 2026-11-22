"""Tests for Settings model custom validators — URL rewriting and backend detection.

PostgreSQL 16 is the only supported runtime database (ADR 068). SQLite is
test-only and requires the ``FARNA_CONTROL_ALLOW_SQLITE=1`` opt-in.
"""

import pytest

from farna.settings import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    # Pin the backend and the SQLite test-only flag so an ambient value (e.g.
    # a sibling suite's conftest) can never change what these tests assert.
    monkeypatch.setenv("FARNA_CONTROL_DB", overrides.pop("FARNA_CONTROL_DB", "postgres"))
    monkeypatch.setenv("FARNA_CONTROL_ALLOW_SQLITE", overrides.pop("FARNA_CONTROL_ALLOW_SQLITE", "0"))
    monkeypatch.setenv("DATABASE_URL", overrides.pop("DATABASE_URL", "postgresql+asyncpg://farna_control:farna_control@localhost:5432/farna_control"))
    monkeypatch.setenv("SECRET_KEY", overrides.pop("SECRET_KEY", "a" * 32))
    monkeypatch.setenv("FERNET_KEY", overrides.pop("FERNET_KEY", "a" * 32))
    # The system DSN is validated too; clear both spellings by default so an
    # ambient value cannot flip a rejection test into a pass.
    system_url = overrides.pop("FARNA_CONTROL_SYSTEM_DATABASE_URL", None)
    if system_url is None:
        monkeypatch.delenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", system_url)
    for k, v in overrides.items():
        monkeypatch.setenv(k, v)


class TestMultiBackendCustomValidators:
    def test_invalid_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, FARNA_CONTROL_DB="oracle")
        with pytest.raises(ValueError, match="FARNA_CONTROL_DB must be"):
            Settings()

    @pytest.mark.parametrize("unsupported", ["mariadb", "mysql", "MariaDB", "MYSQL"])
    def test_mysql_family_backends_are_rejected(self, monkeypatch: pytest.MonkeyPatch, unsupported: str) -> None:
        """ADR 068 removes the MySQL family entirely."""
        _base_env(monkeypatch, FARNA_CONTROL_DB=unsupported)
        with pytest.raises(ValueError, match="ADR 068"):
            Settings()

    def test_sqlite_mode_requires_the_test_only_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, FARNA_CONTROL_DB="sqlite")
        with pytest.raises(ValueError, match="FARNA_CONTROL_ALLOW_SQLITE"):
            Settings()

    def test_sqlite_mode_accepted_with_the_test_only_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, FARNA_CONTROL_DB="sqlite", FARNA_CONTROL_ALLOW_SQLITE="1")
        settings = Settings()
        assert settings.db == "sqlite"
        assert settings.allow_sqlite is True

    def test_sqlite_mode_auto_sets_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, FARNA_CONTROL_DB="sqlite", FARNA_CONTROL_ALLOW_SQLITE="1")
        settings = Settings()
        assert settings.database_url == "sqlite+aiosqlite:///./farna.db"

    def test_postgres_protocol_rewritten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, DATABASE_URL="postgres://modulo:modulo@localhost:5432/modulo")
        settings = Settings()
        assert settings.database_url.startswith("postgresql+asyncpg://")

    def test_ssl_mode_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, DATABASE_URL="postgresql+asyncpg://modulo:modulo@localhost:5432/modulo?sslmode=disable")
        settings = Settings()
        assert "sslmode=disable" in settings.database_url

    def test_modulo_db_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, FARNA_CONTROL_DB="SQLite", FARNA_CONTROL_ALLOW_SQLITE="1")
        settings = Settings()
        assert settings.db == "sqlite"

    def test_postgres_mode_preserves_given_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="postgresql+asyncpg://custom:pass@remote-host:5432/mydb",
            FARNA_CONTROL_DB="postgres",
        )
        settings = Settings()
        assert "custom" in settings.database_url
        assert "remote-host" in settings.database_url
        assert "mydb" in settings.database_url


class TestDsnSchemeGuard:
    """The guard inspects the DSN scheme, not only ``FARNA_CONTROL_DB``.

    A SQLite DSN under ``db=postgres`` selects the Postgres repository
    (no injected tenant filter) against a SQLite engine while
    ``register_tenant_filter`` skips — the unscoped-worker posture ADR 068
    forbids. Every half-configured combination must fail at Settings
    construction.
    """

    def test_sqlite_dsn_with_default_backend_rejected_even_with_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The opt-in alone is not enough: the knob must also say sqlite."""
        _base_env(
            monkeypatch,
            DATABASE_URL="sqlite+aiosqlite:///./should-not-start.db",
            FARNA_CONTROL_DB="postgres",
            FARNA_CONTROL_ALLOW_SQLITE="1",
        )
        with pytest.raises(ValueError, match="not fully enabled"):
            Settings()

    def test_sqlite_dsn_with_sqlite_backend_but_no_opt_in_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="sqlite+aiosqlite:///./should-not-start.db",
            FARNA_CONTROL_DB="sqlite",
            FARNA_CONTROL_ALLOW_SQLITE="0",
        )
        with pytest.raises(ValueError, match="FARNA_CONTROL_ALLOW_SQLITE"):
            Settings()

    def test_sqlite_dsn_with_sqlite_backend_and_opt_in_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="sqlite+aiosqlite:///./accepted-test.db",
            FARNA_CONTROL_DB="sqlite",
            FARNA_CONTROL_ALLOW_SQLITE="1",
        )
        settings = Settings()
        assert settings.db == "sqlite"
        assert settings.database_url == "sqlite+aiosqlite:///./accepted-test.db"

    def test_mysql_dsn_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(monkeypatch, DATABASE_URL="mysql+aiomysql://modulo:modulo@localhost:3306/modulo")
        with pytest.raises(ValueError, match="ADR 068"):
            Settings()

    def test_sqlite_system_dsn_without_opt_in_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty SQLite system URL needs the same test-only opt-in."""
        _base_env(
            monkeypatch,
            FARNA_CONTROL_SYSTEM_DATABASE_URL="sqlite+aiosqlite:///./system-should-not-start.db",
        )
        with pytest.raises(ValueError, match="FARNA_CONTROL_SYSTEM_DATABASE_URL"):
            Settings()

    def test_sqlite_system_dsn_with_opt_in_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            DATABASE_URL="sqlite+aiosqlite:///./accepted-test.db",
            FARNA_CONTROL_DB="sqlite",
            FARNA_CONTROL_ALLOW_SQLITE="1",
            FARNA_CONTROL_SYSTEM_DATABASE_URL="sqlite+aiosqlite:///./accepted-system-test.db",
        )
        settings = Settings()
        assert settings.system_database_url == "sqlite+aiosqlite:///./accepted-system-test.db"

    def test_postgres_system_dsn_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _base_env(
            monkeypatch,
            FARNA_CONTROL_SYSTEM_DATABASE_URL="postgresql+asyncpg://farna_control_system:pw@localhost:5432/farna_control",
        )
        assert Settings().db == "postgres"
