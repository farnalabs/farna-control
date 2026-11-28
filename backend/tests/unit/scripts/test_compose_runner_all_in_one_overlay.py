"""Composition guards for the packaged Bundled Runner overlay (FAR-2854).

``deploy/compose/runner-all-in-one.yml`` is the executor overlay shipped inside
the compose release bundle. The packaged stack defines ONE all-in-one
``farna-control`` service (API + both SAQ workers) plus ``postgres`` and
``redis``; the repo-checkout overlay (``deploy/compose/runner.yml``) instead
configures ``backend`` / ``saq-runner`` / ``saq-system`` and therefore does not
fit the packaged stack.

These are pure file-text / YAML assertions - no Docker engine is required. The
live proof (boot the packaged stack with the overlay and provision a workspace
through the proxy) lives in ``.github/workflows/compose-runner-overlay.yml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve()
for _parent in _REPO_ROOT.parents:
    if (_parent / "deploy" / "compose" / "runner-all-in-one.yml").is_file():
        _REPO_ROOT = _parent
        break
else:
    raise RuntimeError("Could not find repo root (deploy/compose/runner-all-in-one.yml)")

_OVERLAY = _REPO_ROOT / "deploy" / "compose" / "runner-all-in-one.yml"
_REPO_OVERLAY = _REPO_ROOT / "deploy" / "compose" / "runner.yml"
_APP_SERVICE = "farna-control"
_PROXY_SERVICE = "docker-socket-proxy"
_EXPECTED_DOCKER_HOST = "tcp://docker-socket-proxy:2375"
_WORKSPACE_NETWORK = "modulo-runner-workspace"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def overlay() -> dict:
    return _load(_OVERLAY)


def test_targets_the_all_in_one_stack(overlay: dict) -> None:
    """The overlay must fit the packaged stack, not the repo checkout's."""
    services = overlay["services"]

    assert _APP_SERVICE in services
    assert _PROXY_SERVICE in services

    for absent in ("backend", "saq-runner", "saq-system", "postgres", "redis"):
        assert absent not in services, f"the packaged overlay must not define the repo-checkout service '{absent}'"


def test_proxy_keeps_the_production_allowlist_posture(overlay: dict) -> None:
    """Same digest and allowlist as the repo overlay; no published port."""
    repo_overlay = _load(_REPO_OVERLAY)
    repo_proxy = repo_overlay["services"][_PROXY_SERVICE]
    proxy = overlay["services"][_PROXY_SERVICE]

    assert proxy["image"] == repo_proxy["image"], "the packaged proxy digest must match deploy/compose/runner.yml (bump both together)"
    assert proxy["environment"] == repo_proxy["environment"], "the packaged proxy allowlist must match deploy/compose/runner.yml key-for-key"
    assert proxy["environment"]["LOG_LEVEL"] == "info"
    assert "ports" not in proxy, "the filtered proxy must never publish a host port"
    assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert proxy["networks"] == ["default"]


def test_only_the_app_service_gets_the_docker_endpoint(overlay: dict) -> None:
    """The all-in-one app (and its in-container workers) is the only client."""
    holders = [name for name, service in overlay["services"].items() if (service.get("environment") or {}).get("FARNA_CONTROL_DOCKER_HOST") == _EXPECTED_DOCKER_HOST]
    assert holders == [_APP_SERVICE], f"FARNA_CONTROL_DOCKER_HOST must be set on '{_APP_SERVICE}' only; got {holders}"


def test_app_waits_for_the_proxy(overlay: dict) -> None:
    depends_on = overlay["services"][_APP_SERVICE].get("depends_on") or {}
    assert _PROXY_SERVICE in depends_on


def test_workspace_network_is_pinned_by_name(overlay: dict) -> None:
    """The engine-side name must survive a Compose project prefix."""
    networks = overlay["networks"]
    assert networks[_WORKSPACE_NETWORK]["name"] == _WORKSPACE_NETWORK
    assert networks[_WORKSPACE_NETWORK]["driver"] == "bridge"

    proxy_networks = overlay["services"][_PROXY_SERVICE].get("networks") or []
    assert _WORKSPACE_NETWORK not in proxy_networks, "the proxy must never join the workspace network"

    referenced = [name for name, service in overlay["services"].items() if _WORKSPACE_NETWORK in (service.get("networks") or [])]
    assert referenced, "a service must reference the workspace network or compose will not create it (the holder service)"


def test_overlay_adds_no_privileged_or_host_namespace_surface(overlay: dict) -> None:
    """CI guard scope: no privileged container, no host namespaces, no cap_add."""
    for name, service in overlay["services"].items():
        assert service.get("privileged") is not True, f"service '{name}' must not be privileged"
        assert service.get("network_mode") != "host", f"service '{name}' must not use the host network"
        assert service.get("pid") != "host", f"service '{name}' must not share the host PID namespace"
        assert service.get("ipc") != "host", f"service '{name}' must not share the host IPC namespace"
        assert not service.get("cap_add"), f"service '{name}' must not add Linux capabilities"
        assert not service.get("ports"), f"service '{name}' must not publish host ports"
