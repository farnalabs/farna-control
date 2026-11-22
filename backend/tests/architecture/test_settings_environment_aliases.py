"""Structural checks for canonical FARNA_CONTROL_ environment aliases.

The settings layer has one namespace. Every field that carries a product env
name declares exactly one ``FARNA_CONTROL_<NAME>`` alias, where ``<NAME>`` is
the upper-cased field name; the field itself is named after the knob (no
``modulo_`` prefix). Renaming the field and its alias in the same edit is what
stops the two drifting apart, and the tests below fail if either half moves
alone.

There is no legacy pre-rebrand spelling and no compatibility alias: the
product ships to exactly two first-party installs, so a rename is a clean
cutover.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from farna.settings import Settings

_FORBIDDEN_PREFIX = "FARNA_CONTROL_MODULO_"
_LEGACY_PREFIX = "MODULO" + "_"

#: Fields whose canonical alias is deliberately NOT the field-name upper-case
#: spelling. Each entry states why; a new entry is a review decision, not a
#: convenience.
_ALIAS_EXCEPTIONS: dict[str, str] = {
    "environment": "the published knob is FARNA_CONTROL_ENV, not FARNA_CONTROL_ENVIRONMENT",
}


def _alias_names(field: Any) -> tuple[str, ...]:
    validation_alias = field.validation_alias
    choices = getattr(validation_alias, "choices", None)
    if choices is not None:
        return tuple(choices)
    if isinstance(validation_alias, str):
        return (validation_alias,)
    return ()


def test_no_settings_field_keeps_the_legacy_modulo_prefix() -> None:
    """The field name is the knob name; no ``modulo_`` prefix survives.

    ``populate_by_name=True`` resolves a field's name as an environment key, so
    a legacy-prefixed field would keep a bare pre-rebrand spelling alive behind
    the canonical alias - the residual this rename removes.
    """
    legacy_fields = [name for name in Settings.model_fields if name.startswith("modulo_")]

    assert not legacy_fields, f"settings fields must not keep the legacy prefix: {legacy_fields}"


def test_every_product_alias_is_the_canonical_farna_control_name() -> None:
    """One declared alias per field: ``FARNA_CONTROL_`` + the upper-cased field name."""
    checked = 0
    for name, field in Settings.model_fields.items():
        aliases = _alias_names(field)
        if not aliases or not aliases[0].startswith("FARNA_CONTROL_"):
            continue
        assert isinstance(field.validation_alias, str), f"{name} must declare a single string alias, not an AliasChoices set"
        expected = "FARNA_CONTROL_" + name.upper()
        if name in _ALIAS_EXCEPTIONS:
            expected = "FARNA_CONTROL_ENV"
        assert field.validation_alias == expected, f"{name} must declare {expected!r}, not {field.validation_alias!r}"
        checked += 1

    assert checked > 60, f"expected the product alias sweep to cover the settings surface, checked={checked}"


def test_every_alias_exception_is_still_live() -> None:
    """A stale exception would quietly exempt a field that has since moved."""
    for name in _ALIAS_EXCEPTIONS:
        assert name in Settings.model_fields, f"{name} is no longer a settings field - delete the exception"


def test_no_product_alias_uses_the_doubled_prefix() -> None:
    """Doubled names are not accepted as settings aliases."""
    for name, field in Settings.model_fields.items():
        aliases = _alias_names(field)
        assert _FORBIDDEN_PREFIX not in aliases, name


def test_bare_legacy_env_name_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare legacy spelling no longer resolves through the field-name fallback."""
    monkeypatch.delenv("FARNA_CONTROL_DB", raising=False)
    monkeypatch.setenv(_LEGACY_PREFIX + "DB", "sqlite")
    monkeypatch.setenv("FARNA_CONTROL_ALLOW_SQLITE", "1")

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="x" * 32,
    )

    assert settings.db == "postgres"


def test_canonical_env_name_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_LEGACY_PREFIX + "DB", raising=False)
    monkeypatch.setenv("FARNA_CONTROL_DB", "sqlite")
    monkeypatch.setenv("FARNA_CONTROL_ALLOW_SQLITE", "1")

    settings = Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="x" * 32,
    )

    assert settings.db == "sqlite"


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
