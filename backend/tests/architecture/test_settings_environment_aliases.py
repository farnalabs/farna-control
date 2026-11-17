"""Structural checks for canonical FARNA_CONTROL_ environment aliases."""

from __future__ import annotations

import shutil
import subprocess
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
    assert not offenders, "Forbidden FARNA_CONTROL_MODULO_ environment names found in source files; use FARNA_CONTROL_<NAME> and keep bare MODULO_* as the accepted legacy name."
