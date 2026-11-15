"""Focused module-boundary tests for rules outside import-linter's graph contract."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "farna"
_PROTECTED_RUNTIME_MODULES = {
    "farna.core.runtime_provider.docker",
    "farna.core.runtime_provider.e2b",
    "farna.core.runtime_provider.k8s",
    "farna.core.runtime_provider.local",
    "farna.core.runtime_provider.local_docker",
}
_RUNTIME_PROVIDER_PACKAGE = SRC / "core" / "runtime_provider"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            result.add(node.module)
    return result


def test_concrete_runtime_providers_are_imported_only_inside_provider_package() -> None:
    violations: list[str] = []
    for path in SRC.rglob("*.py"):
        if _RUNTIME_PROVIDER_PACKAGE in path.parents or path.parent == _RUNTIME_PROVIDER_PACKAGE:
            continue
        for imported in _imports(path):
            if imported in _PROTECTED_RUNTIME_MODULES:
                violations.append(f"{path.relative_to(SRC)} imports {imported}")
    assert not violations, "Concrete runtime providers must stay behind RuntimeProviderHub:\n" + "\n".join(violations)
