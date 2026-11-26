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
import shlex
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


# FAR-2808: every shipped Postgres the app waits on, EXCEPT the CI-only
# docker-compose.test.yml rig (not part of the cold-install path).
_COMPOSE_DB_PROBE: list[tuple[Path, str]] = [
    (_REPO_ROOT / "docker-compose.yml", "db"),
    (_REPO_ROOT / "docker-compose.local.yml", "db-local"),
    (_REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml", "postgres"),
]

#: A value libpq treats as a TCP host. A leading ``/`` would select a Unix
#: domain socket directory instead, which is what this test forbids.
_TCP_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _pg_isready_host(tokens: list[str]) -> str | None:
    """Return the ``pg_isready`` host, accepting every form libpq accepts.

    libpq (and therefore ``pg_isready``) accepts the host as ``-h HOST``,
    ``--host HOST``, ``-hHOST`` and ``--host=HOST``. Pinning only the short
    separated form would fail a correct probe written any other way, so all four
    are handled here. Returns ``None`` when no host is given (the probe would
    then use the default Unix socket, which this test forbids).
    """
    for index, token in enumerate(tokens):
        if token in ("-h", "--host"):
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token.startswith("--host="):
            return token.split("=", 1)[1]
        if token.startswith("-h") and not token.startswith("--") and token != "-h":
            return token[2:]
    return None


@pytest.mark.parametrize(
    ("compose_path", "service"),
    _COMPOSE_DB_PROBE,
    ids=[f"{p.relative_to(_REPO_ROOT)}:{s}" for p, s in _COMPOSE_DB_PROBE],
)
def test_shipped_db_probe_waits_for_the_realtime_server(compose_path: Path, service: str) -> None:
    """The DB probe must cross TCP, not the Unix socket (FAR-2808).

    On a first run the image's entrypoint starts a SECOND, temporary server to
    run ``initdb`` and create the app role/database, and starts it SOCKET-ONLY
    (``-c listen_addresses=''``), then stops it before ``exec``-ing the real
    server - which the image configures with ``listen_addresses = '*'``. A
    ``pg_isready`` with no ``-h`` therefore used the Unix socket and answered
    ``0`` (accepting connections) against the temporary server: the probe went
    green, the app services started, the temporary server went away, and the
    first connection failed with ``ConnectionRefusedError``. ``start_period``
    cannot help - the probe legitimately SUCCEEDED and then the server went away.

    Pinning a TCP host closes the window, because the temporary server never
    listens on TCP. It also makes the probe mean what the deployment needs: the
    same ``db:5432`` TCP path the application uses. Note that naming the user
    and database (``-U``/``-d``) does NOT close it - ``pg_isready`` reports
    "accepting connections" for a wrong user or database too - so the host flag
    (``-h``/``--host``, in any of libpq's forms) is the load-bearing part here.
    """
    relative = compose_path.relative_to(_REPO_ROOT)
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    test = compose["services"][service]["healthcheck"]["test"]
    assert isinstance(test, list), f"{relative} services.{service}.healthcheck.test must be the ['CMD-SHELL', '<command>'] form, got {test!r}"
    assert len(test) == 2, f"{relative} services.{service}.healthcheck.test must be the ['CMD-SHELL', '<command>'] form, got {test!r}"

    tokens = shlex.split(str(test[1]))
    assert tokens, f"{relative} services.{service} healthcheck command is empty: {test[1]!r}"
    assert tokens[0] == "pg_isready", f"{relative} services.{service} healthcheck must run pg_isready, got {test[1]!r}"

    host = _pg_isready_host(tokens)
    assert host is not None, (
        f"{relative} services.{service} healthcheck runs pg_isready with no -h/--host, so it probes the default Unix socket and answers during the temporary socket-only initdb server; the app services then start into a connection window that closes under them (FAR-2808): {test[1]!r}"
    )
    assert host in _TCP_LOOPBACK_HOSTS, f"{relative} services.{service} healthcheck must probe a TCP loopback host ({sorted(_TCP_LOOPBACK_HOSTS)}), got {host!r}; a socket path or a non-loopback host does not prove the db:5432 TCP path the app uses (FAR-2808)"
