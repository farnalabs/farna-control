"""Verify the docker-compose POSTGRES_PASSWORD wiring shared by the app services.

`docker-compose.yml` derives every app service's database URL from the same
`POSTGRES_PASSWORD` the `db` service creates the cluster with: `DATABASE_URL`,
`DATABASE_ADMIN_URL` and `FARNA_CONTROL_SYSTEM_DATABASE_URL` all interpolate
`${POSTGRES_PASSWORD:-changeme}`. Each service built from `./backend` therefore
has to declare that variable with the `db` service's value, or its connection
URL points at a password the cluster rejects.

The assertions below pin exactly that contract: the `db` service declares
`POSTGRES_PASSWORD`, and `backend`, `saq-runner` and `saq-system` (the services
built from `./backend`, which therefore run `backend/entrypoint.sh`) declare
the same value.

The container entrypoint no longer runs a password-authenticated psql step of
its own (FAR-2777: `migrations/env.py` pre-widens
`alembic_version.version_num`), so this test guards the URL/password contract
across the compose file rather than a wiring link into the entrypoint.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"

# Services built from ./backend run entrypoint.sh and must carry POSTGRES_PASSWORD.
_APP_SERVICES = ("backend", "saq-runner", "saq-system")


@pytest.mark.skipif(
    not _COMPOSE_PATH.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_compose_file_present():
    assert _COMPOSE_PATH.is_file()


def _load_compose() -> dict:
    with _COMPOSE_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.mark.skipif(
    not _COMPOSE_PATH.exists(),
    reason="docker-compose.yml not found (running outside repo checkout)",
)
def test_app_services_declare_postgres_password():
    compose = _load_compose()
    services = compose.get("services", {})

    db_env = services["db"].get("environment", {})
    db_password = db_env.get("POSTGRES_PASSWORD")
    assert db_password, "db service must declare POSTGRES_PASSWORD"

    for service in _APP_SERVICES:
        assert service in services, f"missing service {service} in compose file"
        env = services[service].get("environment", {})
        assert "POSTGRES_PASSWORD" in env, f"{service} must declare POSTGRES_PASSWORD so entrypoint.sh uses the same password as the db service"
        assert env["POSTGRES_PASSWORD"] == db_password, f"{service} POSTGRES_PASSWORD ({env['POSTGRES_PASSWORD']}) must match the db service value ({db_password})"
