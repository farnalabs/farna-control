"""Guard the default Compose stack runs its code from the image, not a bind mount.

FAR-2785 measured the cold first boot on a Windows / Docker Desktop host. The
backend image installs the package editable (a ``.pth`` adds ``/app/src`` to
``sys.path``), so a ``./backend/src:/app/src`` bind mount replaces the image's
copy entirely and makes every import read the host filesystem over Docker
Desktop's slow bind-mount layer — across all three interpreter starts (role
bootstrap, ``alembic upgrade heads`` and ``uvicorn``). Removing the mount cut
the cold boot from ~20 minutes to ~3 minutes on that host.

Two invariants keep that win and keep live editing working:

1. the default ``docker-compose.yml`` must NOT bind ``/app/src`` for any Python
   service (it runs the code baked into the image); and
2. the explicit ``docker-compose.dev.yml`` override MUST bind ``/app/src`` for
   the same services, so a contributor can still live-edit with an ``-f`` flag.

The ``/app/manifest.yaml`` bind is required and must stay: the backend image
cannot bake ``frontend/src/manifest.yaml`` (its build context is ``./backend``),
and the runtime serves it (the manifest route, the assistant primer and the
documentation indexer), degrading to an empty manifest if it is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Repo root (three levels up: tests/architecture/ -> tests/ -> backend/ -> root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BASE_COMPOSE = _REPO_ROOT / "docker-compose.yml"
_DEV_COMPOSE = _REPO_ROOT / "docker-compose.dev.yml"

#: The services built from ./backend that import the package at runtime.
_PYTHON_SERVICES = ("backend", "saq-runner", "saq-system")

#: Container path the image's code lives at (and where the dev override mounts).
_SRC_TARGET = "/app/src"


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _volume_targets(service: dict) -> list[str]:
    """Return the container-side target of every volume in *service*."""
    targets: list[str] = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, str):
            parts = entry.split(":")
            # A short-form volume is "source:target[:mode]" or a bare "target".
            targets.append(parts[1] if len(parts) >= 2 else parts[0])
        elif isinstance(entry, dict) and entry.get("target"):
            targets.append(str(entry["target"]))
    return targets


@pytest.mark.skipif(
    not _BASE_COMPOSE.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_default_compose_does_not_bind_mount_backend_src() -> None:
    """The default compose must not shadow the image's /app/src with a host bind."""
    services = _load(_BASE_COMPOSE).get("services", {})
    offenders = [name for name in _PYTHON_SERVICES if _SRC_TARGET in _volume_targets(services.get(name, {}))]
    assert not offenders, f"docker-compose.yml bind-mounts /app/src for {offenders}; that serves every import from the host filesystem (FAR-2785). Put the live-edit mount in docker-compose.dev.yml instead."


@pytest.mark.skipif(
    not _DEV_COMPOSE.exists(),
    reason="docker-compose.dev.yml not found (running outside repo checkout)",
)
def test_dev_override_restores_the_live_edit_mount() -> None:
    """The dev override must re-add /app/src for every Python service."""
    services = _load(_DEV_COMPOSE).get("services", {})
    missing = [name for name in _PYTHON_SERVICES if _SRC_TARGET not in _volume_targets(services.get(name, {}))]
    assert not missing, f"docker-compose.dev.yml does not mount /app/src for {missing}; live editing would silently need a rebuild."


@pytest.mark.skipif(
    not _BASE_COMPOSE.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_default_compose_keeps_the_runtime_manifest_mount() -> None:
    """The backend serves /app/manifest.yaml at runtime; the image cannot bake it."""
    services = _load(_BASE_COMPOSE).get("services", {})
    assert "/app/manifest.yaml" in _volume_targets(services.get("backend", {})), "The backend service must keep the ./frontend/src/manifest.yaml -> /app/manifest.yaml mount: the backend build context cannot bake it, and without it the manifest route and assistant serve an empty manifest (FAR-2785)."
