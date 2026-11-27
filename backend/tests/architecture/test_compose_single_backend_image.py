"""Guard that the three Python Compose services build ONE image, not three.

FAR-2826. ``backend``, ``saq-runner`` and ``saq-system`` are the same Dockerfile
and the same ``./backend`` build context, differing only in ``command:``. Giving
each its own ``build:`` made Compose produce three ~2 GB images and repeat both
per-image costs three times. Measured on a Windows / Docker Desktop host with
``docker compose build --no-cache backend`` (per image): ``chown -R
appuser:appuser /app`` 897 s and export/unpack 601 s, i.e. ~1500 s of the 2024 s
build paid once per image. See ``docs/compose-cold-build-performance.md``.

The invariant is therefore:

1. all three services resolve to the SAME ``image:`` — one tag, one build;
2. exactly ONE of them declares ``build:`` (``backend``), so a cold build runs
   the Dockerfile once. ``--no-cache`` disables BuildKit's cross-service cache
   reuse, so a second ``build:`` block costs a full second build, not a cache
   hit;
3. the worker services keep an explicit ``command:``. They now inherit the
   image's ``CMD`` (``./entrypoint.sh`` -> uvicorn), so a dropped ``command:``
   would silently start two more APIs instead of the SAQ workers.

This is separate from ``test_compose_default_runs_from_image.py`` (FAR-2785),
which guards *where the code comes from* (image vs bind mount). This file guards
*how many times it is built*.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Repo root (four levels up from this test file: tests/architecture/ -> tests/ -> backend/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BASE_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: The services that run the backend image.
_PYTHON_SERVICES = ("backend", "saq-runner", "saq-system")

#: The one service allowed to carry `build:` — the others reference its image.
_BUILD_OWNER = "backend"

#: The tag all three share. Local-only by design: a registry-qualified name
#: would let `up` pull a stranger's image instead of building this checkout.
_SHARED_IMAGE = "farna-control-backend:latest"

#: Services that must override the image CMD to become SAQ workers.
_WORKER_SERVICES = ("saq-runner", "saq-system")

_skip_no_compose = pytest.mark.skipif(
    not _BASE_COMPOSE.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)


def _services() -> dict:
    with _BASE_COMPOSE.open(encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("services", {})


@_skip_no_compose
def test_python_services_share_one_image() -> None:
    """One image tag for all three services, so a cold build runs the Dockerfile once."""
    services = _services()
    images = {name: (services.get(name) or {}).get("image") for name in _PYTHON_SERVICES}

    assert set(images.values()) == {_SHARED_IMAGE}, f"the Python services must all reference `image: {_SHARED_IMAGE}`, got {images}. Each distinct image is a separate ~2 GB build and a separate export/unpack (FAR-2826); see docs/compose-cold-build-performance.md."


@_skip_no_compose
def test_only_one_service_builds_the_shared_image() -> None:
    """Exactly one `build:` block, or a cold build repeats the expensive Dockerfile."""
    services = _services()
    builders = [name for name in _PYTHON_SERVICES if (services.get(name) or {}).get("build") is not None]

    assert builders == [_BUILD_OWNER], f"exactly one of {list(_PYTHON_SERVICES)} must declare `build:`, expected only {_BUILD_OWNER!r}, got {builders}. `--no-cache` disables BuildKit's cross-service cache reuse, so every extra `build:` block is a full extra image build (FAR-2826)."


@_skip_no_compose
def test_the_shared_image_is_a_local_tag_not_a_registry_ref() -> None:
    """A registry-qualified tag would be pulled, not built, on a clean machine."""
    services = _services()
    image = (services.get(_BUILD_OWNER) or {}).get("image") or ""

    assert image, f"{_BUILD_OWNER} must name the shared image it builds (FAR-2826)"
    assert "/" not in image, (
        f"`image: {image}` names a registry/namespace, so `docker compose up` would try to "
        "pull it instead of using the locally built image. Keep the shared tag local "
        "(no registry prefix) — the published artifact is the packaged release bundle "
        "(scripts/package_compose_release.py), not this dev stack (FAR-2826)."
    )


@_skip_no_compose
@pytest.mark.parametrize("service", _WORKER_SERVICES)
def test_worker_services_override_the_image_command(service: str) -> None:
    """With a shared image the workers must still override the image's CMD."""
    definition = _services().get(service) or {}
    command = definition.get("command")

    assert command, f"{service} declares no `command:`. It now runs the shared backend image, whose CMD is ./entrypoint.sh (uvicorn), so without an override this service would start a second API instead of a SAQ worker (FAR-2826)."
