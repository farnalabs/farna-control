"""Run at container startup AND from the native launcher to prepare the database.

Promoted from ``deploy/fly/bootstrap_db.py`` (FAR-671 / ADR 031 Decision 2):
the deploy path now imports this module through a thin shim so the container
and native boots share the exact logic and cannot drift. Dependency-light by
contract - importable WITHOUT SQLAlchemy/models; asyncpg-level only.

1. Fix DATABASE_ADMIN_URL for the SQLAlchemy async driver and fix the runtime
   DATABASE_URL for backwards compatibility
2. Create the alembic_version table with VARCHAR(255) for branch migrations
3. Resolve the system database URL from the accepted aliases, in priority order:
   FARNA_CONTROL_SYSTEM_DATABASE_URL, MODULO_SYSTEM_DATABASE_URL, then
   FARNA_CONTROL_MODULO_SYSTEM_DATABASE_URL. An explicitly configured value is
   fixed and published under the canonical name. Otherwise, derive a value from
   DATABASE_URL for the entrypoint env-file contract only; never publish that
   derived credential into the canonical application setting. The result is
   written to /tmp/system_database_url.env.
4. Write the fixed DATABASE_ADMIN_URL / DATABASE_URL / system database URL to
   files for the shell script
"""

import asyncio
import os
import secrets as _secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg

# split_postgres_sslmode is imported, not defined, here (FAR-1469): the
# canonical helper lives in ``farna.db.url_utils`` — a stdlib-only LEAF module
# — so importing it adds no dependency beyond what this dependency-light boot
# path already takes (fix_database_url / derive_system_database_url) and there
# is no import cycle. One implementation, no interim copy.
from farna.db.url_utils import derive_system_database_url, fix_database_url, split_postgres_sslmode

__all__ = ["main"]

_TMP_NAME_ATTEMPTS = 8


def _write_env_file(path: str, content: str) -> None:
    """Write *content* to *path* with owner-only (0o600) permissions.

    These files hold database URLs that include credentials, so they must not be
    world-/group-readable even though they live in the world-writable ``/tmp``
    directory (S5443). The consuming shell scripts run as the same user, so the
    restrictive mode does not break them.

    The fixed *path* is a predictable name in a world-writable directory, so a
    plain create/truncate would let an attacker pre-create (or symlink) it and
    read the credentials. Instead the content is written to a RANDOM-named
    sibling created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` (a pre-created or
    symlinked temp name can never be adopted or followed), fsynced, and then
    atomically renamed over the contract path — the renamed inode carries the
    0600 mode and replaces whatever (file or symlink) squatted there. A symlink
    on the contract path is rejected loudly rather than followed.
    """
    target = Path(path)
    last_error: OSError | None = None
    for _ in range(_TMP_NAME_ATTEMPTS):
        tmp_path = target.parent / f"{target.name}.tmp-{_secrets.token_hex(8)}"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError as exc:
            last_error = exc  # virtually impossible random-name collision; retry
            continue
        try:
            os.write(fd, content.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        if target.is_symlink():
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"refusing to write {path}: a symlink squats on the contract path")
        tmp_path.replace(target)
        return
    raise RuntimeError(f"could not secure a private temp file next to {path}") from last_error


def main() -> None:
    # Step 1: Fix DATABASE_ADMIN_URL and DATABASE_URL
    admin_url = os.environ.get("DATABASE_ADMIN_URL") or os.environ.get("DATABASE_URL", "")
    original = admin_url
    admin_url = fix_database_url(admin_url)
    os.environ["DATABASE_ADMIN_URL"] = admin_url
    if admin_url != original:
        # FAR-1441: sslmode is no longer silently stripped — it is extracted
        # from the pre-fix URL and honoured on the admin connect below.
        msg = "Fixed DATABASE_ADMIN_URL for the async driver (scheme rewrite; sslmode, when present, is honoured via the ssl connect arg)"
        print(msg)  # noqa: T201

    # Also fix DATABASE_URL (the runtime URL) for backwards compat
    runtime_url = os.environ.get("DATABASE_URL", "")
    if runtime_url:
        runtime_url = fix_database_url(runtime_url)
        os.environ["DATABASE_URL"] = runtime_url

    # Step 2: Create alembic_version table with VARCHAR(255)
    # Branch migration IDs exceed the default VARCHAR(32).

    async def _bootstrap(admin_dsn: str, ssl_arg: bool | str | None) -> None:
        pg_url = admin_dsn.replace("postgresql+asyncpg://", "postgres://")
        connect_kwargs: dict[str, Any] = {}
        if ssl_arg is not None:
            connect_kwargs["ssl"] = ssl_arg
        conn = await asyncpg.connect(pg_url, **connect_kwargs)
        try:
            await conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (  version_num VARCHAR(255) NOT NULL PRIMARY KEY)")
            print("alembic_version table ready (VARCHAR(255))")  # noqa: T201
        finally:
            await conn.close()

    # FAR-1441: honour the operator's TLS posture on the admin bootstrap
    # connection. The sslmode is read from the PRE-fix URL (fix_database_url
    # rewrites the legacy scheme and strips what the driver cannot parse); a
    # Postgres URL always gets an EXPLICIT ssl value (never asyncpg's
    # fail-open ``prefer`` default), while a non-Postgres admin URL passes
    # through unchanged with no ssl connect arg.
    admin_dsn, admin_ssl = original, None
    if urlsplit(original).scheme.startswith("postgres"):
        admin_dsn, admin_ssl = split_postgres_sslmode(original)

    try:
        asyncio.run(_bootstrap(admin_dsn, admin_ssl))
    except Exception as exc:
        print(  # noqa: T201
            f"WARNING: Could not bootstrap alembic_version: [{type(exc).__name__}] {exc}",
            file=sys.stderr,
        )

    # Step 3: Write the fixed URLs to files for the shell (credentials - 0o600)
    # The /tmp paths are the container-entrypoint contract (consumed by the
    # shell scripts). The predictable-name hazards of a world-writable /tmp
    # are mitigated inside _write_env_file: exclusive-create random temp
    # sibling (O_CREAT|O_EXCL|O_NOFOLLOW, 0600) atomically renamed over the
    # contract path, symlinked contract paths rejected. The /tmp prefix here
    # is the deployment contract, not an unchecked temp usage.
    # /tmp is the container-entrypoint contract (consumed by the shell scripts); the
    # world-writable-dir hazard is mitigated inside _write_env_file via
    # O_CREAT|O_EXCL|O_NOFOLLOW + 0o600 + atomic rename over the contract path +
    # symlinked-contract-path rejection, so the credential files cannot be pre-created
    # or symlink-hijacked by another /tmp writer. The suppression comment below covers S5443.
    _write_env_file("/tmp/database_url.env", runtime_url)  # nosec B108  # noqa: S108  # NOSONAR S5443
    _write_env_file("/tmp/database_admin_url.env", admin_url)  # nosec B108  # noqa: S108  # NOSONAR S5443

    # MODULO_SYSTEM_DATABASE_URL: the farna_control_system role (LOGIN, BYPASSRLS) URL used
    # by system crons (metrics_dump, analytics_facts_maintenance, journey_reconcile,
    # retention_cleanup, dispatcher_reconcile) and the SSO pre-auth provider lookup.
    # If set, fix it like the others. If not, derive the URL for the entrypoint's
    # env-file contract, but do not pass that derived credential into role
    # reconciliation: bootstrap_role.py must preserve an existing system-role
    # password on warm boots without an explicit system URL.
    configured_system_url = next(
        (
            os.environ.get(name, "")
            for name in (
                "FARNA_CONTROL_SYSTEM_DATABASE_URL",
                "MODULO_SYSTEM_DATABASE_URL",
                "FARNA_CONTROL_MODULO_SYSTEM_DATABASE_URL",
            )
            if os.environ.get(name, "")
        ),
        "",
    )
    if configured_system_url:
        system_url = fix_database_url(configured_system_url)
        os.environ["FARNA_CONTROL_SYSTEM_DATABASE_URL"] = system_url
        # Short-lived container bootstrap env file, consistent with the
        # /tmp/database_url.env and /tmp/database_admin_url.env files written above.
        _write_env_file("/tmp/system_database_url.env", system_url)  # nosec B108  # noqa: S108  # NOSONAR S5443
    elif runtime_url:
        system_url = derive_system_database_url(runtime_url)
        if system_url:
            # The derived URL is for the entrypoint env-file contract only.
            # Do not publish it as the canonical application credential: its
            # password comes from the app URL and is not a verified system-role
            # credential. bootstrap_role.py reads only the explicit legacy alias.
            print(  # noqa: T201
                "WARNING: no system database URL is configured; derived system URL is written to the entrypoint env file only and is not published as an application credential.",
                file=sys.stderr,
            )
            _write_env_file("/tmp/system_database_url.env", system_url)  # nosec B108  # noqa: S108  # NOSONAR S5443
        else:
            print(  # noqa: T201
                "WARNING: cannot derive MODULO_SYSTEM_DATABASE_URL (DATABASE_URL has no usable password/userinfo) - system crons will fall back to farna_control_app",
                file=sys.stderr,
            )
