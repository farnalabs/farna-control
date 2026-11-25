"""Verify the docker-compose database-credential wiring shared by the app services.

`docker-compose.yml` creates the `db` cluster with
`POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-changeme}`. Every app service
(`backend`, `saq-runner`, `saq-system`) reaches that cluster through a
`DATABASE_URL` (plus `DATABASE_ADMIN_URL` / `FARNA_CONTROL_SYSTEM_DATABASE_URL`
where present) that interpolates the SAME project-level `${POSTGRES_PASSWORD}`
variable.

The interpolation happens at ``docker compose`` parse time from the project
environment (the ``.env`` file or the host shell), never from a service's own
``environment:`` block. What matters is therefore that the URL references the
same variable the `db` service uses: if one side hardcodes a different value, or
drops the default, the URL credential and the cluster credential drift apart and
every connection is rejected.

Nothing in ``backend/src`` reads ``POSTGRES_PASSWORD`` at runtime: ``Settings``
consumes the already-interpolated ``DATABASE_URL``-family values, and the retired
psql ``alembic_version`` widening (removed in FAR-2777: ``migrations/env.py``
pre-widens the column) is the only code that ever read it. The app services
therefore do not declare ``POSTGRES_PASSWORD`` themselves; this test guards the
URL/cluster contract across the compose file rather than a per-service env var.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_PATH = _REPO_ROOT / "docker-compose.yml"

#: The project-environment interpolation the cluster and the URL must share.
_PASSWORD_INTERPOLATION = "${POSTGRES_PASSWORD:-changeme}"

# Services built from ./backend that reach the `db` cluster.
_APP_SERVICES = ("backend", "saq-runner", "saq-system")

#: Database URL keys an app service may declare (at least DATABASE_URL).
_DATABASE_URL_KEYS = ("DATABASE_URL", "DATABASE_ADMIN_URL", "FARNA_CONTROL_SYSTEM_DATABASE_URL")


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
def test_app_services_database_urls_share_the_db_service_password():
    compose = _load_compose()
    services = compose.get("services", {})

    db_env = services["db"].get("environment", {})
    db_password = db_env.get("POSTGRES_PASSWORD")
    assert db_password == _PASSWORD_INTERPOLATION, f"db service must create the cluster with the shared {_PASSWORD_INTERPOLATION} interpolation so the app URLs can reference the same credential, got {db_password!r}"

    for service in _APP_SERVICES:
        assert service in services, f"missing service {service} in compose file"
        env = services[service].get("environment", {})

        url_keys = [key for key in _DATABASE_URL_KEYS if key in env]
        assert "DATABASE_URL" in url_keys, f"{service} must declare DATABASE_URL"

        for key in url_keys:
            value = env[key]
            assert _PASSWORD_INTERPOLATION in value, f"{service} {key} must interpolate the same {_PASSWORD_INTERPOLATION} the db service uses, so the URL credential and the cluster credential can never disagree, got {value!r}"
