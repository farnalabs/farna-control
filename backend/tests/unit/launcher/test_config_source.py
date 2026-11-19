"""Unit tests for the launcher config source (FAR-671 slice 2).

Locks: the env > state.json(+secrets) > defaults priority (both via the
pydantic-settings source class and via the pinned dotenv file), the import
hygiene rule (no ``farna.settings`` import at module import time), the
0600 pinned-config write with symlink rejection, and the un-bootstrapped
refusal.
"""

import ast
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from farna.launcher.config_source import (
    CONFIG_ENV_FILENAME,
    LauncherConfigError,
    LauncherConfigSource,
    compose_config,
    load_launcher_config_inputs,
    render_env_file,
    resolve_pinned_runtime_keys,
    write_pinned_env_file,
)
from farna.launcher.secrets_file import LauncherSecrets
from farna.launcher.state import LauncherState, save_state
from farna.settings import get_settings
from tests.unit.launcher.test_launcher_shared import bypass_platform_seams, launcher_state_reset

FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SECRET_KEY = "a" * 32


@pytest.fixture(autouse=True)
def _reset_launcher_state() -> Iterator[None]:
    """The shared snapshot/restore body lives in the launcher-test module."""
    yield from launcher_state_reset()


@pytest.fixture(autouse=True)
def _bypass_platform_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the Windows-refusal seams this module's tests exercise."""
    bypass_platform_seams(monkeypatch, "farna.launcher.secrets_file")


def _secrets() -> LauncherSecrets:
    return LauncherSecrets(postgres_password="pg-pw", redis_password="redis-pw", state_hmac_key=bytes(range(32)))


def _state() -> LauncherState:
    return LauncherState(postgres_port=15432, redis_port=16379, api_port=18000)


# ---------------------------------------------------------------------------
# Import hygiene
# ---------------------------------------------------------------------------


def test_no_settings_import_at_module_time_by_ast() -> None:
    from farna.launcher import config_source

    source = Path(config_source.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("farna.settings"):
            raise AssertionError("config_source must not import farna.settings at module level")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("farna.settings")


def test_no_settings_module_loaded_after_import_by_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import sys, farna.launcher.config_source; print('farna.settings' in sys.modules)"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_compose_config_derives_all_bundled_endpoints() -> None:
    composed = compose_config(_state(), _secrets())
    assert composed["DATABASE_URL"] == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"
    assert composed["SECRET_KEY"]
    assert composed["FERNET_KEY"]
    assert composed["REDIS_URL"] == "redis://:redis-pw@127.0.0.1:16379/0"
    system_url = composed["FARNA_CONTROL_SYSTEM_DATABASE_URL"]
    assert system_url.startswith("postgresql+asyncpg://farna_control_system:pg-pw@127.0.0.1:15432/farna_control")


def test_compose_config_separates_application_from_owner_role() -> None:
    """FAR-2570: the app role is NOT the initdb superuser / owner role.

    ``initdb`` creates its ``-U`` role (``farna_control``) as a Postgres
    SUPERUSER. Wiring it into ``DATABASE_URL`` made the application run as a
    superuser: RLS was bypassed, the accounts UPDATE allow-list could not hold
    (``has_column_privilege`` answers true for every column for a superuser),
    and every boot failed the role-posture assertion.
    """
    from farna.db.url_utils import APP_ROLE, OWNER_ROLE

    composed = compose_config(_state(), _secrets())

    assert urlsplit(composed["DATABASE_URL"]).username == APP_ROLE
    assert urlsplit(composed["DATABASE_URL"]).username == "farna_control_app"
    # DDL / migrations / the role bootstrap need the owner role.
    assert urlsplit(composed["DATABASE_ADMIN_URL"]).username == OWNER_ROLE
    assert urlsplit(composed["DATABASE_ADMIN_URL"]).username == "farna_control"
    assert urlsplit(composed["DATABASE_URL"]).username != urlsplit(composed["DATABASE_ADMIN_URL"]).username
    # The system URL keeps the app password and swaps only the role.
    assert urlsplit(composed["FARNA_CONTROL_SYSTEM_DATABASE_URL"]).username == "farna_control_system"


def test_pinned_config_preserves_existing_operator_keys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("FERNET_KEY", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = data_dir / CONFIG_ENV_FILENAME
    path.write_text("SECRET_KEY=operator-secret\nFERNET_KEY=operator-fernet\n", encoding="utf-8")

    write_pinned_env_file(data_dir, _state(), _secrets())

    rendered = path.read_text(encoding="utf-8")
    assert "SECRET_KEY=operator-secret\n" in rendered
    assert "FERNET_KEY=operator-fernet\n" in rendered


def test_pinned_runtime_keys_match_installer_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / CONFIG_ENV_FILENAME
    path.write_text("SECRET_KEY=file-secret\nFERNET_KEY=file-fernet\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY", "environment-secret")
    monkeypatch.setenv("FERNET_KEY", "environment-fernet")

    resolved = resolve_pinned_runtime_keys(
        {"SECRET_KEY": "generated-secret", "FERNET_KEY": "generated-fernet"},
        path,
    )

    assert resolved["SECRET_KEY"] == "environment-secret"
    assert resolved["FERNET_KEY"] == "environment-fernet"


def test_compose_config_is_password_free_on_state_alone() -> None:
    payload = _state().to_payload()
    joined = repr(payload)
    assert "pg-pw" not in joined
    assert "redis-pw" not in joined


# ---------------------------------------------------------------------------
# Single source of truth for the app database name (FAR-2561)
# ---------------------------------------------------------------------------

_APP_DB_NAME_IDENTIFIERS = frozenset({"APP_DB_NAME", "_APP_DB_NAME"})


def _module_level_app_db_name_literals() -> dict[str, list[str]]:
    """Every module-level ``APP_DB_NAME`` / ``_APP_DB_NAME`` string literal in the package."""
    from farna.launcher import config_source as config_source_module

    package_dir = Path(config_source_module.__file__).resolve().parent
    defined: dict[str, list[str]] = {}
    for path in sorted(package_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if not isinstance(value, ast.Constant):
                continue
            if not isinstance(value.value, str):
                continue
            named = [t for t in targets if isinstance(t, ast.Name) and t.id in _APP_DB_NAME_IDENTIFIERS]
            if named:
                defined.setdefault(path.name, []).append(value.value)
    return defined


def test_app_database_name_has_exactly_one_definition() -> None:
    """FAR-2561: exactly ONE module-level literal may define the app database name.

    Three independent literals existed (``config_source`` said ``modulo`` while
    ``entry`` created ``farna_control``), so the composed URL and the database
    first boot actually created could — and did — disagree. A re-declared
    literal anywhere in the package must fail this test, not drift silently.
    """
    defined = _module_level_app_db_name_literals()

    assert defined == {"entry.py": ["farna_control"]}


def test_app_database_name_consumers_resolve_to_the_same_object() -> None:
    """FAR-2561: composers IMPORT the one constant.

    An equal-but-separately-declared literal would pass an equality check while
    still being free to drift on the next edit, so this pins object identity.
    """
    import farna.launcher.config_source as config_source_module
    import farna.launcher.entry as entry_module
    import farna.launcher.upgrade as upgrade_module

    assert entry_module.APP_DB_NAME == "farna_control"
    assert config_source_module.APP_DB_NAME is entry_module.APP_DB_NAME
    assert upgrade_module.APP_DB_NAME is entry_module.APP_DB_NAME


def test_composed_database_urls_carry_the_single_app_database_name() -> None:
    """FAR-2561: every composed URL names the database the launcher creates.

    This is the release-blocking defect: first boot created ``farna_control``,
    the pinned ``DATABASE_URL`` said ``modulo``, and the API never became
    healthy (``database "modulo" does not exist``).
    """
    composed = compose_config(_state(), _secrets())

    assert urlsplit(composed["DATABASE_URL"]).path == "/farna_control"
    assert urlsplit(composed["DATABASE_ADMIN_URL"]).path == "/farna_control"
    # The system URL is DERIVED from DATABASE_URL (only the role is swapped), so
    # it shares the app database name — pinned here so "the system role gets its
    # own database" can only ever be a deliberate, tested decision.
    assert urlsplit(composed["FARNA_CONTROL_SYSTEM_DATABASE_URL"]).path == "/farna_control"


# ---------------------------------------------------------------------------
# Source class (direct Settings construction)
# ---------------------------------------------------------------------------


def test_source_omits_keys_the_environment_already_sets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    real_create = sf.load_or_create
    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgres://operator-overridden/db")
    source = LauncherConfigSource(data_dir)
    resolved = source()
    assert "database_url" not in resolved  # env wins — the state value is dropped
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"
    real_create(data_dir / "secrets.json")  # keep the tmp dir realistic for later asserts


def test_source_redis_url_environment_wins_over_composed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An operator REDIS_URL env var must drop the composed redis value.

    The source returns init-kwargs (which outrank env), so any composed key
    whose environment variable is already set must be omitted — otherwise the
    bundled Redis password would shadow the operator's explicit choice.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://operator:secret@cache.example.com:6379/7")
    resolved = LauncherConfigSource(data_dir)()
    assert "redis_url" not in resolved  # the operator's env wins
    assert resolved["database_url"] == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"


def test_source_system_database_url_environment_wins_over_composed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """FARNA_CONTROL_SYSTEM_DATABASE_URL set (without DATABASE_URL) drops the composed one."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", "postgresql://user:pass@host:5432/modulo")
    resolved = LauncherConfigSource(data_dir)()
    assert "modulo_system_database_url" not in resolved  # the operator's env wins
    assert resolved["database_url"] == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_derives_system_url_from_the_operators_database_url(clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The app/system URL pair is atomic: an operator DATABASE_URL must never
    end up paired with a system URL still pointing at the bundled Postgres."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:op-pass@db.example.com:5432/modulo")
    monkeypatch.delenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", raising=False)
    resolved = LauncherConfigSource(data_dir)()
    assert "database_url" not in resolved
    system = resolved.get("modulo_system_database_url", "")
    expected_prefixes = (
        "postgresql://farna_control_system:op-pass@db.example.com:5432/modulo",
        "postgresql+asyncpg://farna_control_system:op-pass@db.example.com:5432/modulo",
    )
    assert system.startswith(expected_prefixes)
    assert "127.0.0.1" not in system
    assert "pg-pw" not in system
    assert "modulo_system_database_url" in resolved
    assert "farna_control_system_database_url" not in resolved


def test_source_includes_everything_without_environment(clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    resolved = LauncherConfigSource(data_dir)()
    assert resolved["database_url"] == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"
    assert resolved["redis_url"] == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_skips_empty_composed_values(clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A present-but-blank composed key is omitted rather than injected blank.

    De-stubbed (review finding): the composition with a NO-PASSWORD URL is
    the case that caused the regression — ``compose_config`` returns a
    real dict whose ``FARNA_CONTROL_SYSTEM_DATABASE_URL`` is present-but-blank —
    and the source must omit it, never write ``modulo_system_database_url=''``.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    password_free = LauncherSecrets(postgres_password="", redis_password="redis-pw", state_hmac_key=bytes(range(32)))
    monkeypatch.setattr(sf, "load_or_create", lambda _path: password_free)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FARNA_CONTROL_SYSTEM_DATABASE_URL", raising=False)
    composed = compose_config(_state(), password_free)
    assert not composed["FARNA_CONTROL_SYSTEM_DATABASE_URL"]  # blank key IS present in the composition
    resolved = LauncherConfigSource(data_dir)()
    assert "modulo_system_database_url" not in resolved  # blank value omitted, not injected
    assert resolved  # the non-blank composed values are still injected
    assert all(resolved.values())


def test_source_settings_roundtrip_honours_priority(clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from farna.settings import Settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    save_state(_state(), data_dir / "state.json", _secrets().state_hmac_key)
    import farna.launcher.secrets_file as sf

    monkeypatch.setattr(sf, "load_or_create", lambda _path: _secrets())
    resolved = LauncherConfigSource(data_dir)()
    settings = Settings(
        secret_key=SECRET_KEY,
        fernet_key=FERNET_KEY,
        _env_file=None,
        **resolved,  # type: ignore[arg-type]
    )
    assert settings.database_url == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"
    assert settings.redis_url == "redis://:redis-pw@127.0.0.1:16379/0"


def test_source_refuses_unbootstrapped_data_dir(tmp_path: Path) -> None:
    with pytest.raises(LauncherConfigError, match="not bootstrapped"):
        LauncherConfigSource(tmp_path / "data")()


def test_load_inputs_refuses_missing_state(tmp_path: Path) -> None:
    with pytest.raises(LauncherConfigError, match="not bootstrapped"):
        load_launcher_config_inputs(tmp_path / "data")


# ---------------------------------------------------------------------------
# Pinned env file
# ---------------------------------------------------------------------------


def test_render_env_file_is_sorted_dotenv() -> None:
    composed = compose_config(_state(), _secrets())
    rendered = render_env_file(composed)
    lines = rendered.splitlines()
    keys = [line.partition("=")[0] for line in lines]
    assert keys == sorted(keys)


def test_write_pinned_env_file_content_and_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    assert path.name == CONFIG_ENV_FILENAME
    content = path.read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control" in content
    assert "REDIS_URL=redis://:redis-pw@127.0.0.1:16379/0" in content
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


def test_write_pinned_env_file_warns_when_clobbering_different_content(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = write_pinned_env_file(data_dir, _state(), _secrets())
    first.write_text("MANUALLY EDITED=1\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="farna.launcher.config_source"):
        write_pinned_env_file(data_dir, _state(), _secrets())
    assert any("config_env_overwritten" in record.message for record in caplog.records)
    rewritten = first.read_text(encoding="utf-8")
    assert "DATABASE_URL=" in rewritten  # the composed content won


def test_write_pinned_env_file_no_warning_on_identical_rewrite(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    first = write_pinned_env_file(data_dir, _state(), _secrets())
    with caplog.at_level(logging.WARNING, logger="farna.launcher.config_source"):
        second = write_pinned_env_file(data_dir, _state(), _secrets())
    assert not any("config_env_overwritten" in record.message for record in caplog.records)
    assert second.read_text(encoding="utf-8") == first.read_text(encoding="utf-8")


def test_write_pinned_env_file_refuses_symlink_squat(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("symlink semantics are POSIX-only")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("innocent")
    squat = data_dir / CONFIG_ENV_FILENAME
    squat.symlink_to(victim)
    with pytest.raises(RuntimeError, match="symlink"):
        write_pinned_env_file(data_dir, _state(), _secrets())
    assert victim.read_text(encoding="utf-8") == "innocent"


def test_pinned_env_file_feeds_get_settings(clean_launcher_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from farna.settings import pin_env_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    pin_env_file(str(path))
    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://farna_control_app:pg-pw@127.0.0.1:15432/farna_control"
    assert settings.redis_url == "redis://:redis-pw@127.0.0.1:16379/0"


def test_environment_overrides_the_pinned_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """env > state.json: an operator DATABASE_URL beats the composed one."""
    from farna.settings import pin_env_file

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    path = write_pinned_env_file(data_dir, _state(), _secrets())
    monkeypatch.setenv("DATABASE_URL", "postgres://operator-override/override")
    monkeypatch.setenv("SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("FERNET_KEY", FERNET_KEY)
    pin_env_file(str(path))
    settings = get_settings()
    assert settings.database_url == "postgresql+asyncpg://operator-override/override"
