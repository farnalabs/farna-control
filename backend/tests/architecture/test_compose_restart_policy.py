"""Architecture test: every long-running Compose service survives a transient exit.

FAR-2808. A container that exits stays exited: nothing in Compose brings it back
on its own, so one transient failure leaves a stack that still *looks* up -
``docker compose ps`` shows the other containers running - while one role is
dead. The measured case: on a clean-volume first boot ``backend`` exited 1 with
``ConnectionRefusedError`` to ``db:5432`` seconds after Compose reported the
database healthy (the probe had gone green against Postgres's temporary,
socket-only initdb server), and the API stayed dead for the life of the stack.

``restart: unless-stopped`` is the fix, and the exact value is load-bearing:

* ``unless-stopped`` restarts on exit AND on daemon restart, EXCEPT for a
  container that was explicitly stopped - which is what ``docker compose
  stop``/``down`` and the recovery path (``scripts/compose-restore.sh`` stops
  the writers and leaves the stack down) require.
* ``always`` would resurrect a deliberately stopped container when the Docker
  daemon restarts, undoing that explicit stop.
* no policy (or ``"no"``) is the FAR-2808 bug: a one-shot exit is permanent.

Rather than a curated allow-list of service names (which silently skipped
anything not written down - the ``frontend`` gap was exactly that), this test
ENUMERATES every service in each shipped standalone Compose file and subtracts a
documented exemption set. A service added later is therefore covered without
anyone remembering to list it, and a newly added ``docker-compose*.yml`` that is
not classified here fails ``test_every_shipped_compose_file_is_classified``
instead of slipping past the guard.

Documented exemptions:

* ``deploy/compose/docker-compose.test.yml`` - a CI-only ephemeral rig, not part
  of the product stack.
* Services opted into the ``observability`` profile in
  ``docker-compose.local.yml`` - developer tooling, not the product stack.

Overlay files (``deploy/compose/runner.yml``, ``deploy/compose/runner-ci.yml``)
are deliberately NOT enumerated: they are partial overlays applied on top of a
base file (``-f docker-compose.yml -f ...``), so their service stubs inherit the
base service's policy and checking them here would wrongly demand the key on a
stub. ``docker-socket-proxy`` already carries the policy itself, and
``bundled-runner`` is a one-shot build-target/network holder (``restart: "no"``
by design). They are named ``runner*.yml`` rather than ``docker-compose*.yml``,
so they are not caught by the classification check below.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Repo root (four levels up from this test file: tests/architecture/ -> tests/ -> backend/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: The only policy that self-heals an exit without defeating a deliberate stop.
_EXPECTED_RESTART = "unless-stopped"

#: Standalone product-stack Compose files whose long-running services must self-heal.
_STACK_COMPOSE_FILES: list[Path] = [
    _REPO_ROOT / "docker-compose.yml",
    _REPO_ROOT / "docker-compose.local.yml",
    _REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml",
]

#: Documented whole-file exemptions (see the module docstring).
_EXEMPT_COMPOSE_FILES: frozenset[Path] = frozenset(
    {
        _REPO_ROOT / "deploy" / "compose" / "docker-compose.test.yml",
    }
)

#: Documented profile exemptions: opt-in developer tooling, not the product stack.
_EXEMPT_PROFILES: frozenset[str] = frozenset({"observability"})

#: Where a newly added standalone Compose file would live. Every
#: ``docker-compose*.yml`` here must be classified as a stack file or an
#: exemption, so a new file cannot silently escape the restart-policy guard.
_COMPOSE_SEARCH_DIRS: tuple[Path, ...] = (_REPO_ROOT, _REPO_ROOT / "deploy" / "compose")


def _service_is_exempt(definition: dict) -> bool:
    """True when a service opts into a documented, non-product profile."""
    profiles = definition.get("profiles") or []
    return bool(_EXEMPT_PROFILES.intersection(profiles))


def _enumerated_services() -> list[tuple[Path, str]]:
    """Every non-exempt service across the standalone Compose stack files."""
    services: list[tuple[Path, str]] = []
    for compose_path in _STACK_COMPOSE_FILES:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
        for service, definition in (compose.get("services") or {}).items():
            if _service_is_exempt(definition or {}):
                continue
            services.append((compose_path, service))
    return services


_COMPOSE_SERVICES = _enumerated_services()


def test_every_shipped_compose_file_is_classified() -> None:
    """A new ``docker-compose*.yml`` must be classified, or CI fails (FAR-2808)."""
    discovered: set[Path] = set()
    for directory in _COMPOSE_SEARCH_DIRS:
        discovered.update(directory.glob("docker-compose*.yml"))

    classified = set(_STACK_COMPOSE_FILES) | set(_EXEMPT_COMPOSE_FILES)
    unclassified = discovered - classified
    assert not unclassified, f"Unclassified Compose file(s) {sorted(str(p.relative_to(_REPO_ROOT)) for p in unclassified)}: add each to _STACK_COMPOSE_FILES (and give its long-running services `restart: {_EXPECTED_RESTART}`) or document why it is exempt in _EXEMPT_COMPOSE_FILES (FAR-2808)."


@pytest.mark.parametrize(
    ("compose_path", "service"),
    _COMPOSE_SERVICES,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{s}" for p, s in _COMPOSE_SERVICES],
)
def test_long_running_service_restarts_after_a_transient_exit(compose_path: Path, service: str) -> None:
    """A service that exits transiently must come back on its own (FAR-2808)."""
    relative = compose_path.relative_to(_REPO_ROOT)
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    restart = compose["services"][service].get("restart")

    assert restart == _EXPECTED_RESTART, (
        f"{relative} services.{service} must declare `restart: {_EXPECTED_RESTART}`, got {restart!r}. "
        "Without it a transient exit is permanent and `docker compose ps` presents a half-dead stack as up "
        "(FAR-2808); `always` instead would resurrect a deliberately stopped container on daemon restart, "
        "defeating `docker compose stop` and the recovery path."
    )
