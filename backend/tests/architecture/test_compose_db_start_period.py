"""Architecture test: every shipped Compose Postgres has a cold-start grace.

FAR-2780. A cold first start runs ``initdb``, the entrypoint's CREATE DATABASE
and a full shutdown/checkpoint before ``pg_isready`` can succeed — measured at
~80s on a Windows/Docker Desktop host, which is past the 25-30s the retry
budgets allowed. With no ``start_period`` those failed probes counted against
``retries``, so the ``db`` container was marked unhealthy and the documented
first install failed with ``dependency failed to start`` on every clean volume.
The next start always worked because the volume was initialised, which made the
bug invisible to anything but a true first run.

``start_period`` is what makes a first-run init survivable: failures inside the
window do not count against ``retries``, while steady-state detection keeps its
existing ``interval``/``timeout``/``retries``. This test pins the grace on every
shipped Compose file so a dropped ``start_period`` — or a new Compose file that
forgets it — fails CI instead of surfacing as a broken clean install. It also
asserts the grace exceeds ``interval x retries``: a ``start_period`` shorter
than the retry budget would still let a slow init consume every retry and mark
the container unhealthy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Repo root (four levels up from this test file: tests/architecture/ → tests/ → backend/ → repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Every shipped Compose file that defines the Postgres the stack starts on, and
# the service name it gives that database.
_COMPOSE_DB: list[tuple[Path, str]] = [
    (_REPO_ROOT / "docker-compose.yml", "db"),
    (_REPO_ROOT / "docker-compose.local.yml", "db-local"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "postgres"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.test.yml", "db-test"),
]

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ns|us|ms|s|m|h)")
_DURATION_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}


def _duration_seconds(value: object, *, where: str) -> float:
    """Parse a Compose duration (e.g. ``120s``, ``1m30s``) into seconds."""
    assert isinstance(value, str), f"{where} must be a Compose duration string like '120s', got {value!r}"
    tokens = _DURATION_TOKEN.findall(value)
    assert tokens, f"{where} is not a valid Compose duration: {value!r}"
    assert "".join(f"{number}{unit}" for number, unit in tokens) == value.strip(), f"{where} is not a valid Compose duration: {value!r}"
    return sum(float(number) * _DURATION_SECONDS[unit] for number, unit in tokens)


@pytest.mark.parametrize(
    ("compose_path", "service"),
    _COMPOSE_DB,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{s}" for p, s in _COMPOSE_DB],
)
def test_shipped_db_healthcheck_has_a_cold_start_grace(compose_path: Path, service: str) -> None:
    """Every shipped Postgres probe must outlast a cold first-run initdb."""
    relative = compose_path.relative_to(_REPO_ROOT)
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    healthcheck = compose["services"][service]["healthcheck"]

    assert "start_period" in healthcheck, f"{relative} services.{service} has no healthcheck start_period; a cold first-run initdb outlasts the retry budget and marks the DB unhealthy, failing a clean install (FAR-2780)"

    start_period = _duration_seconds(healthcheck["start_period"], where=f"{relative} services.{service}.healthcheck.start_period")
    interval = _duration_seconds(healthcheck["interval"], where=f"{relative} services.{service}.healthcheck.interval")
    retries = healthcheck["retries"]
    assert isinstance(retries, int), f"{relative} services.{service}.healthcheck.retries must be an integer, got {retries!r}"

    retry_budget = interval * retries
    assert start_period > retry_budget, f"{relative} services.{service}.healthcheck.start_period {start_period:g}s must exceed interval x retries ({interval:g}s x {retries} = {retry_budget:g}s); a shorter grace still lets a cold start consume every retry and mark the DB unhealthy (FAR-2780)"
