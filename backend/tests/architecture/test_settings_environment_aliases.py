"""Structural checks for canonical FARNA_CONTROL_ environment aliases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def test_doubled_environment_prefix_is_absent_from_source_tree() -> None:
    """The canonical shape is FARNA_CONTROL_<NAME>; bare MODULO_* remains legacy."""
    repo_root = Path(__file__).resolve().parents[3]
    guard_file = Path(__file__).resolve()
    source_files = [path for path in repo_root.rglob("*") if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts and path != guard_file and path.suffix in {".py", ".sh", ".md", ".yaml", ".yml", ".toml", ".env"}]
    offenders = [path for path in source_files if _FORBIDDEN_PREFIX in path.read_text(encoding="utf-8", errors="replace")]
    assert not offenders, "Forbidden FARNA_CONTROL_MODULO_ environment names found in source files; use FARNA_CONTROL_<NAME> and keep bare MODULO_* as the accepted legacy name."
