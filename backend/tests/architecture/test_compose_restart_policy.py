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

This test pins the policy on every service of the shipped Compose files that is
expected to stay up, so a dropped key - or a new Compose file that forgets it -
fails CI instead of surfacing as a half-dead stack. Curated service lists
deliberately exclude: ``docker-compose.test.yml`` (a CI-only ephemeral rig) and
the opt-in ``observability`` profile services in ``docker-compose.local.yml``
(developer tooling, not the product stack).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Repo root (four levels up from this test file: tests/architecture/ -> tests/ -> backend/ -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

#: The only policy that self-heals an exit without defeating a deliberate stop.
_EXPECTED_RESTART = "unless-stopped"

# Every shipped Compose file, and each service in it that must self-heal.
_COMPOSE_SERVICES: list[tuple[Path, str]] = [
    (_REPO_ROOT / "docker-compose.yml", "db"),
    (_REPO_ROOT / "docker-compose.yml", "redis"),
    (_REPO_ROOT / "docker-compose.yml", "backend"),
    (_REPO_ROOT / "docker-compose.yml", "saq-runner"),
    (_REPO_ROOT / "docker-compose.yml", "saq-system"),
    (_REPO_ROOT / "docker-compose.local.yml", "db-local"),
    (_REPO_ROOT / "docker-compose.local.yml", "redis-local"),
    (_REPO_ROOT / "docker-compose.local.yml", "saq-runner"),
    (_REPO_ROOT / "docker-compose.local.yml", "saq-system"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "postgres"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "redis"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "farna-control"),
]


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
