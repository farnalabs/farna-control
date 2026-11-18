"""Structural checks for canonical FARNA_CONTROL_ environment aliases."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from farna.settings import Settings

_FORBIDDEN_PREFIX = "FARNA_CONTROL_MODULO_"


def _alias_names(field: Any) -> tuple[str, ...]:
    validation_alias = field.validation_alias
    choices = getattr(validation_alias, "choices", None)
    if choices is not None:
        return tuple(choices)
    if isinstance(validation_alias, str):
        return (validation_alias,)
    return ()


def test_modulo_settings_have_single_canonical_prefix() -> None:
    modulo_fields = {name: field for name, field in Settings.model_fields.items() if name.startswith("modulo_")}

    assert modulo_fields
    for name, field in modulo_fields.items():
        aliases = _alias_names(field)
        assert aliases, f"{name} must declare validation_alias"
        assert aliases[0].startswith("FARNA_CONTROL_")
        assert not aliases[0].startswith("FARNA_CONTROL_MODULO_")


def test_no_product_canonical_alias_uses_doubled_prefix() -> None:
    """Doubled names are not accepted as settings aliases."""
    for name, field in Settings.model_fields.items():
        aliases = _alias_names(field)
        if name.startswith("modulo_"):
            assert _FORBIDDEN_PREFIX not in aliases


def test_every_modulo_field_declares_exactly_the_canonical_alias() -> None:
    """One declared alias per field: the canonical ``FARNA_CONTROL_<NAME>`` string.

    This is the contract the legacy-alias removal establishes: no field carries an
    ``AliasChoices`` set any more, so there is no second DECLARED spelling. The
    alias must equal ``FARNA_CONTROL_`` + the upper-cased field name (minus the
    ``modulo_`` prefix) exactly, so a wrong or duplicated canonical spelling
    cannot slip through. It does NOT by itself reject a bare ``MODULO_<NAME>``
    environment variable -- see ``test_bare_modulo_env_name_is_still_read_via_the_field_name``.
    """
    for name, field in Settings.model_fields.items():
        if not name.startswith("modulo_"):
            continue
        assert isinstance(field.validation_alias, str), f"{name} must declare a single string alias, not an AliasChoices set"
        expected = "FARNA_CONTROL_" + name.removeprefix("modulo_").upper()
        assert field.validation_alias == expected, f"{name} must declare {expected!r}, not {field.validation_alias!r}"


def test_bare_modulo_field_name_is_still_read_via_the_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """KNOWN RESIDUAL for ``modulo_*`` FIELDS: a bare ``MODULO_<NAME>`` env var is resolved.

    ``populate_by_name=True`` makes pydantic-settings accept the FIELD NAME as an
    environment key, and every settings field is named ``modulo_*`` (so
    ``modulo_db`` -> ``MODULO_DB``). Removing the explicit legacy alias therefore
    cannot reject the bare spelling on its own. Turning ``populate_by_name`` off
    WOULD reject it, but that also drops the name-keyed constructor kwargs pinned by
    FAR-1500. This test makes the coupling explicit: if the flag ever changes, it
    fails here and the intent has to be restated deliberately rather than assumed.

    This is NOT a claim about every legacy spelling. ``environment``'s legacy alias
    was ``MODULO_ENV`` (not ``MODULO_ENVIRONMENT``), so it is not revived by the
    field-name fallback -- see ``test_legacy_modulo_env_name_is_not_read``.
    """
    monkeypatch.delenv("FARNA_CONTROL_DB", raising=False)
    monkeypatch.setenv("MODULO_DB", "sqlite")

    assert Settings.model_config["populate_by_name"] is True
    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="x" * 32,
    )

    assert settings.modulo_db == "sqlite"


def test_legacy_modulo_env_name_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """``MODULO_ENV`` is deliberately dropped, while the field name still resolves.

    ``environment`` was the only field whose legacy alias was not the
    ``MODULO_<FIELD-NAME>`` spelling: the canonical alias is ``FARNA_CONTROL_ENV``
    and the legacy one was ``MODULO_ENV`` (the field-name spelling is
    ``ENVIRONMENT``). Removing the alias set therefore drops ``MODULO_ENV`` rather
    than leaving it resolvable through ``populate_by_name``. This pins that
    outcome: an operator still wiring the old name silently loses the value.
    """
    monkeypatch.delenv("FARNA_CONTROL_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("MODULO_ENV", "production")

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="x" * 32,
    )

    assert settings.environment == "development"

    monkeypatch.delenv("MODULO_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "production")

    assert (
        Settings(
            database_url="postgresql+asyncpg://localhost/test",
            secret_key="a" * 32,
            fernet_key="x" * 32,
        ).environment
        == "production"
    )


def test_doubled_environment_prefix_is_absent_from_source_tree() -> None:
    """The canonical shape is FARNA_CONTROL_<NAME>; a doubled FARNA_CONTROL_MODULO_ name is never valid."""
    repo_root = Path(__file__).resolve().parents[3]
    guard_file = Path(__file__).resolve()
    git_executable = shutil.which("git")
    if git_executable is None:
        raise AssertionError("Unable to enumerate tracked source files: git is unavailable")
    try:
        tracked_files = subprocess.run(  # noqa: S603
            [git_executable, "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise AssertionError(f"Unable to enumerate tracked source files with git: {exc}") from exc

    suffixes = (".py", ".sh", ".md", ".yaml", ".yml", ".toml", ".env")
    source_files = [repo_root / relative_path for relative_path in tracked_files if relative_path.endswith(suffixes) or ".env." in Path(relative_path).name]
    source_files = [path for path in source_files if path.resolve() != guard_file.resolve()]
    offenders = [path for path in source_files if _FORBIDDEN_PREFIX in path.read_text(encoding="utf-8", errors="replace")]
    assert not offenders, "Forbidden FARNA_CONTROL_MODULO_ environment names found in source files; use FARNA_CONTROL_<NAME>."
