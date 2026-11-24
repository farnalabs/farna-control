"""farna-control backup/restore: CLI for self-hosted backup and disaster recovery.

Usage:
  farna-control backup [--db-url <url>] [--output-dir <path>] [--data-dir <path>] [--include-secrets]
  farna-control restore <backup-dir> [--db-url <url>] [--app-db-url <url>] [--yes]
               [--previous-fernet-key <key>] [--dry-run] [--data-dir <path>]
               [--replace-cluster]

Data-safety semantics (FAR-672 / ADR 031 Decision 6/7):

* The backup manifest is VERSIONED (``manifest_version``). Legacy archives
  (no ``manifest_version`` field) restore fine; an archive written by a
  NEWER Modulo than this server supports is refused.
* Launcher data-dir artefacts ride along when ``--data-dir`` is given:
  the credential-free ``state.json`` always; the 0600 secrets file and the
  credential-bearing conf profile ONLY with the explicit opt-in flag
  ``--include-secrets`` (loud warning); the TLS keypair when present.
  Redis/SAQ queue data is ephemeral and EXCLUDED by design — documented in
  the manifest.
* The default archive contains no cleartext FERNET_KEY/SECRET_KEY-class
  material and no launcher secrets: safe to move through ordinary storage.
  ``--include-secrets`` produces a complete DR/machine-migration archive.
* A backup against a data dir takes the exclusive data-dir lock (refusing
  with the holder PID while a start is running) and persists the
  last-successful-backup timestamp in the HMAC'd state.json
  (``last_backup_at``, forward-compatible optional field).
* Restore verifies EVERYTHING before it touches anything (manifest version,
  instance identity, disk pre-flight, checksums) and refuses to run against
  a data dir whose launcher is still running.
* Instance identity: populated-instance identifier (state.json HMAC) must
  match the archive; a mismatch refuses unless ``--replace-cluster`` is
  given (with a loud confirmation printing both identifiers). Fresh /
  uninitialized targets are exempt — disaster recovery and machine
  migration must keep working.
* After the database restore, the promoted bootstrap/bootstrap_role posture
  path re-runs for the APPLICATION role — resolved from ``--app-db-url``, else
  ``DATABASE_URL``, never from ``--db-url`` (the admin connection) — and a
  posture failure FAILS the restore instead of warning. The Postgres
  collation-version table is checked too: an INCOMPATIBLE collation-version
  drift (different libc family, e.g. the compose musl -> native glibc path)
  HARD REFUSES the restore, while a compatible-but-older same-family drift
  warns and proceeds.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import stat
import subprocess  # nosec B404 -- subprocess is used only to shell out to pg_dump/psql with a hardcoded argv list (never shell=True, never user-supplied commands); see _run_pg_dump/_run_psql
import sys
import time
import urllib.parse
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

import click
import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from farna.cli.apply import register_apply
from farna.db.url_utils import _POSTGRES_ASYNC_PREFIX
from farna.settings import get_settings

_log = logging.getLogger(__name__)

# Repeated error message and backup-artifact filenames (S1192). Constants are
# pure aliases — the exported file names are part of the on-disk backup layout.
_ERR_PSYCOPG_UNAVAILABLE = "psycopg library is not available"
_ALEMBIC_INI = "alembic.ini"
_DB_SQL_FILE = "database.sql"
_BLOBS_JSON_FILE = "checkpoint_blobs.json"
_CHECKPOINTS_JSON_FILE = "checkpoints.json"
_WRITES_JSON_FILE = "checkpoint_writes.json"
_CREDS_JSON_FILE = "credentials_references.json"

# A restore takes a pre-restore safety dump of the TARGET first (or the
# operator passes --no-safety-dump explicitly): a mid-import failure must
# leave the previous cluster restorable.
_SAFETY_DUMP_PREFIX = "pre-restore-dump-"

# Versioned manifest (FAR-672): 2 adds the launcher-snapshot section. The
# restore path accepts 1 (legacy) and 2; anything newer is refused.
MANIFEST_VERSION = 2
_BACKUP_INFO_FILE = "backup-info.json"

# Launcher data-dir artefact names (ADR 031 Decision 6 layout). These are the
# on-disk contract names inside the data dir AND the backup layout.
_LAUNCHER_STATE_FILE = "state.json"
_LAUNCHER_SECRETS_FILE = "secrets.json"
_LAUNCHER_CONF_FILE = "config.env"

# What a native run resolves from the data dir's own config.env: the two
# endpoints and the key the installation actually encrypts credentials with.
_LAUNCHER_CONFIG_KEYS = frozenset({"DATABASE_ADMIN_URL", "DATABASE_URL", "FERNET_KEY"})

# Hosts that can only mean "this machine": a target on one of these is the
# bundled cluster when its port is the bundled port (see _target_is_bundled).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_PG_PORT = 5432

# The restore window: how long to wait for a started bundled cluster to accept
# connections, how long a fast shutdown may take, and where its server log
# lands (inside the data dir, beside launcher.log).
_RESTORE_PG_READY_TIMEOUT = 180.0
_RESTORE_PG_STOP_TIMEOUT = 60.0
_RESTORE_PG_KILL_TIMEOUT = 30.0
_RESTORE_PG_LOG = "restore-postgres.log"
_RESTORE_PROBE_INTERVAL = 0.2
_TLS_DIRNAME = "tls"

# A backup placed inside a cloud-synced folder risks silent corruption (sync
# churn mid-dump) and unencrypted storage of a credential-free-but-sensitive
# archive. Detected by comparing each path COMPONENT (case-folded, exact
# equality) against the known vendor roots — substring matching would
# false-positive on ordinary names like "dropbox-migrations-backup-tmp".
_CLOUD_SYNC_VENDOR_ROOTS = frozenset({"dropbox", "onedrive", "icloud", "googledrive"})

# A Windows drive-letter path in an archive manifest names a file outside the
# archive (the same shape the bundle-integrity gate refuses).
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")

# The launcher-owned public surface (consumed by farna.cli.main; vulture's
# dead-code gate special-cases __all__).
__all__ = ["MANIFEST_VERSION", "backup", "cli", "restore"]


# ── URL helpers ──────────────────────────────────────────────────────────────

# SQLAlchemy asyncpg driver scheme, shared by the URL normalisers below and
# imported from db/url_utils.py so the contract has a single declaration.


def _launcher_config_values(data_dir: Path | None) -> dict[str, str]:
    """Read the database endpoints the launcher itself runs with.

    A bootstrapped data dir carries ``config.env`` — the launcher's OWN pinned
    settings, written at every boot — holding ``DATABASE_URL``/``DATABASE_ADMIN_URL``
    for the bundled cluster (with the generated credentials). It is the only
    place a native operator's shell can learn the bundled endpoints from: the
    launcher exports them to its children, never to the operator's shell, and
    the generated passwords appear nowhere else the operator can read without
    the data dir.

    Only the keys this module needs are returned (the two endpoints and
    ``FERNET_KEY``), parsed with the same dotenv rules the launcher's own source
    uses (blank lines and ``#`` comments ignored, quotes stripped). An absent or
    unreadable file yields an empty mapping: the caller then falls back to the
    environment/Settings and says so, rather than treating "no config" as an
    endpoint.
    """
    values: dict[str, str] = {}
    if data_dir is None:
        return values
    try:
        text = (data_dir / _LAUNCHER_CONF_FILE).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return values
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key in _LAUNCHER_CONFIG_KEYS and value:
            values.setdefault(key, value)
    return values


def _resolve_url(db_url: str | None, *, data_dir: Path | None = None) -> str:
    """Resolve the maintenance connection string for pg_dump / psql / psycopg.

    Order: an explicit ``--db-url`` (an operator's argv), then an exported
    ``DATABASE_ADMIN_URL``, then — when a data dir is in play — that data dir's
    own launcher config (``config.env``), then the configured ``DATABASE_URL``.

    ``DATABASE_ADMIN_URL`` sits SECOND on purpose. It is the owner/migration
    role, and it is the role these commands need: the application role is
    ``NOBYPASSRLS``, so a dump taken through ``DATABASE_URL`` is
    trailer-complete but ROW-PARTIAL, and a restore through it cannot
    DROP/CREATE the schema. Resolving it from the environment also keeps the
    superuser password OFF every command line — a recovery helper can invoke
    ``restore`` with no URL argument at all, so the password never appears in a
    host argv (world-readable via ``/proc/<pid>/cmdline``).

    The data-dir arm is what makes a NATIVE ``--data-dir`` run target the
    bundled cluster at all: a native operator's shell has no
    ``DATABASE_ADMIN_URL`` (the launcher only exports it to its own children),
    so relying on the environment silently resolved the process defaults —
    a different, usually empty database. It sits BELOW an exported variable so
    an operator override still wins.

    Normalised through the ONE shared libpq normaliser (``farna.db.url_utils``):
    the application is configured with ``postgresql+asyncpg://`` while
    ``psycopg``/``pg_dump``/``psql`` speak libpq only, and the pre-upgrade
    snapshot path normalises the same way — so the two dump producers cannot
    disagree about which schemes work.
    """
    launcher_admin_url = _launcher_config_values(data_dir).get("DATABASE_ADMIN_URL")
    raw = db_url or os.environ.get("DATABASE_ADMIN_URL") or launcher_admin_url or get_settings().database_url
    from farna.db.url_utils import normalise_libpq_url

    try:
        return normalise_libpq_url(raw)
    except ValueError as exc:
        raise click.ClickException(f"Cannot use this DATABASE_URL: {exc}. `farna-control backup`/`restore` talk to PostgreSQL through pg_dump, psql and psycopg, which accept only postgresql:// URLs.") from exc


# ── Fernet helpers ───────────────────────────────────────────────────────────


def _fernet_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── JSON serialisation helpers ────────────────────────────────────────────────


def _serialise_for_json(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def _serialise_export_row(row: dict[str, Any]) -> dict[str, Any]:
    serialised: dict[str, Any] = {}
    for key, val in row.items():
        if isinstance(val, uuid.UUID):
            serialised[key] = str(val)
        elif isinstance(val, bytes | memoryview):
            serialised[key] = bytes(val).hex()
        elif isinstance(val, datetime):
            serialised[key] = val.isoformat()
        else:
            serialised[key] = val
    return serialised


def _parse_org_id(raw: str | None) -> uuid.UUID | None:
    if raw:
        try:
            return uuid.UUID(raw)
        except (ValueError, TypeError):
            raise RuntimeError(f"Invalid organisation_id: {raw!r}") from None
    return None


def _jsonb(value: Any) -> Jsonb | None:
    """Re-adapt an exported JSON value for a JSONB parameter.

    ``SELECT *`` returns JSONB columns as decoded Python objects, so the
    restore path receives dicts/lists (and any other JSON scalar). psycopg has
    no default dumper for those (``ProgrammingError: cannot adapt type
    'dict'``), which made a restore of any archive containing a checkpoint row
    fail outright. The value must be wrapped explicitly; ``None`` stays SQL
    NULL rather than becoming ``Jsonb(None)`` (which would decode to JSON
    ``'null'``).
    """
    return None if value is None else Jsonb(value)


def _write_json(path: Path, data: Any) -> None:
    """Atomically-tight JSON write: the file is CREATED 0600 (never briefly
    exposed at umask mode for the whole serialisation), Windows no-op."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=_serialise_for_json)


# ── Metadata helpers ─────────────────────────────────────────────────────────


def _get_backend_dir() -> Path:
    resolved = Path(__file__).resolve()
    for candidate in (resolved.parent.parent.parent, resolved.parent.parent.parent.parent):
        if (candidate / "pyproject.toml").exists() and (candidate / _ALEMBIC_INI).exists():
            return candidate
    return resolved.parent.parent.parent.parent


def _get_schema_versions() -> list[str]:
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = _get_backend_dir()
        alembic_ini = backend_dir / _ALEMBIC_INI
        if not alembic_ini.exists():
            return ["unknown"]
        cfg = Config(str(alembic_ini))
        script = ScriptDirectory.from_config(cfg)
        return sorted(script.get_heads())
    except Exception:
        _log.exception("Failed to read schema versions")
        return ["unknown"]


#: One ``scheme://user:secret@`` userinfo run. A driver that renders the DSN it
#: was handed puts the password exactly here.
_URL_CREDENTIAL_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^:/@\s]*):(?P<secret>[^@]*)@")


class _SanitisedDatabaseError(RuntimeError):
    """A driver error whose text carries no credential.

    Raised by :func:`_connect` in place of the driver's own exception. The
    message is the driver's, scrubbed by :func:`_redact_secrets`, and the
    original exception type name is preserved so the transcript still says what
    failed (``OperationalError: connection refused``) without the DSN.
    """

    def __init__(self, message: str, original_type: str) -> None:
        super().__init__(f"{original_type}: {message}")
        self.original_type = original_type


def _redact_secrets(text: str, password: str | None = None) -> str:
    """Scrub a connection password out of text bound for a terminal or a log.

    Two layers, because a password reaches error text in more than one shape:

    * the ``scheme://user:secret@`` userinfo run a driver renders from the DSN
      it was given; and
    * the LITERAL password value, in its raw and its percent-encoded forms — a
      password containing a space or a bad percent escape (``pa%zzword``) is
      not matchable by the URL pattern, so scrubbing it needs the value.

    Passwords never travel in the DSN these commands hand to psycopg (see
    :func:`_connect`), so this is the second line of defence, not the first.
    """
    redacted = _URL_CREDENTIAL_RE.sub(lambda match: f"{match.group('scheme')}{match.group('user')}:***@", text)
    if not password:
        return redacted
    for candidate in {password, urllib.parse.quote(password, safe=""), urllib.parse.quote_plus(password)}:
        if candidate:
            redacted = redacted.replace(candidate, "***")
    return redacted


def _redact_url_secrets(text: str, raw_url: str) -> str:
    """Scrub *raw_url*'s own password and any URL userinfo out of *text*."""
    try:
        _dsn, password = _decredential_url(raw_url)
    except Exception:  # an unparseable URL has no password to scrub
        password = None
    return _redact_secrets(text, password)


def _connect(raw_url: str, *, timeout: int = 10, row_factory: Any | None = None) -> Any:
    """psycopg connect with the password OUT of the DSN.

    EVERY psycopg connection in this module goes through here — the read-only
    probes, the JSON exports and the checkpoint/credential restore writes
    alike. psycopg quotes the DSN verbatim in its error text and these errors
    are echoed and logged, so a password left in the DSN becomes a password in
    the operator's terminal and the logs. The password is lifted out
    (``psycopg_connect_args``) and handed to the driver as its ``password=``
    argument, and any error that still carries it — a driver rendering a DSN,
    a password with a space or a bad percent escape — is scrubbed by
    :func:`_redact_secrets` before it is re-raised.
    """
    from farna.db.url_utils import psycopg_connect_args

    dsn, password = psycopg_connect_args(raw_url)
    connect_kwargs: dict[str, Any] = {"connect_timeout": timeout, "password": password}
    if row_factory is not None:
        connect_kwargs["row_factory"] = row_factory
    try:
        return psycopg.connect(dsn, **connect_kwargs)
    except Exception as exc:  # every driver error can quote its input
        raise _SanitisedDatabaseError(_redact_secrets(str(exc), password), type(exc).__name__) from None


def _get_db_version(raw_url: str) -> str:
    if psycopg is None:
        return "unknown"
    try:
        with _connect(raw_url, timeout=5) as conn:
            row = conn.execute("SELECT version()").fetchone()
            return row[0] if row else "unknown"
    except Exception:
        _log.exception("Failed to read DB version")
        return "unknown"


# ── pg_dump / psql helpers ────────────────────────────────────────────────────


def _check_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"Required system tool '{name}' not found on PATH. Install PostgreSQL client tools.")


def _decredential_url(raw_url: str) -> tuple[str, str | None]:
    """Split the password out of a PostgreSQL URL.

    Returns ``(url_without_password, password)``. pg_dump/psql must never
    receive the password as an argv element: argv is world-readable via
    ``/proc/<pid>/cmdline`` while the child runs. The password travels in the
    child-scoped ``PGPASSWORD`` env var instead (see
    :func:`farna.db.url_utils.pg_child_env`).

    Delegates to :func:`farna.db.url_utils.split_postgres_credentials` — the
    ONE implementation the pre-upgrade snapshot shares, so a password holding
    ``#``, ``?``, ``/`` or ``%`` cannot reparse to a different target in one
    producer and not the other.
    """
    from farna.db.url_utils import split_postgres_credentials

    return split_postgres_credentials(raw_url)


def _run_pg_dump(raw_url: str, output: Path, timeout: int = 300) -> None:
    """Dump through the ONE shared plain-SQL pipeline (FAR-2667).

    The argv, the 0600 file creation, the client-newer-than-server preamble
    rewrite and the completeness proof all live in
    :func:`farna.db.plain_dump.dump_plain_sql`, which the pre-upgrade snapshot
    also calls — a pipeline written twice drifts, and the two producers must
    agree on what a usable dump is.
    """
    from farna.db import plain_dump

    try:
        plain_dump.dump_plain_sql(raw_url, output, timeout_seconds=timeout)
    except ValueError as exc:
        raise RuntimeError(f"pg_dump failed: {exc}") from exc


def assert_plain_dump_complete(dump_path: Path) -> None:
    """Refuse a dump that is truncated, empty, or not plain SQL (FAR-2667).

    Delegates to :mod:`farna.db.plain_dump` — the ONE implementation the
    pre-upgrade snapshot path also uses, so the two producers cannot disagree
    on what "complete" means.
    """
    from farna.db import plain_dump

    try:
        plain_dump.assert_complete(dump_path)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _run_psql(raw_url: str, input_path: Path, timeout: int = 600) -> None:
    _check_tool("psql")
    from farna.db.url_utils import pg_child_env

    url_without_password, password = _decredential_url(raw_url)
    # -X (no ~/.psqlrc): an operator's psqlrc could `\set ON_ERROR_STOP off`,
    # and psql would then exit 0 after a rolled-back restore. The explicit
    # -v ON_ERROR_STOP=1 below is authoritative only when nothing else can
    # change it, so the rc file is never read.
    # -1 (single transaction): the --clean dump replays its DROPs and CREATEs
    # atomically — a mid-import failure leaves the target database exactly as
    # it was, not half-dropped. Combined with the pre-restore safety dump
    # (see _take_safety_dump) a failed restore stays recoverable.
    cmd = ["psql", "-X", "-q", "-1", "-v", "ON_ERROR_STOP=1", url_without_password]
    try:
        with input_path.open("rb") as f:
            result = subprocess.run(  # nosec B603 -- cmd is a hardcoded argv list with trusted input, never shell=True  # noqa: S603
                cmd, stdin=f, capture_output=True, check=False, timeout=timeout, env=pg_child_env(password)
            )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"psql restore timed out after {timeout}s") from exc
    if result.returncode != 0:
        # psql is handed a password-free URL (the password reaches it as
        # PGPASSWORD), but libpq warnings can still quote a connection string,
        # so the stderr text is scrubbed of both shapes before it is raised
        # (it is printed and logged by the caller).
        raise RuntimeError(f"psql restore failed: {_redact_secrets(result.stderr.decode(errors='replace').strip(), password)}")


def _take_safety_dump(raw_url: str, timeout: int = 300, output_dir: Path | None = None) -> Path | None:
    """pg_dump the CURRENT target before it is wiped (restore safety net).

    A mid-import psql failure must leave the pre-restore cluster restorable:
    the dump lands in a private-mode directory under *output_dir* (default:
    the current directory) and its path is reported. Any dump failure refuses
    the restore (the operator may pass --no-safety-dump explicitly to proceed
    without one). The ONLY tolerated degradation is pg_dump not being
    installed at all — in which case a loud warning is issued and the restore
    continues bare-backed: psql needs the dump replayed anyway, so a
    pg_dump-less machine's restore dies on the very next step without ever
    touching the database.
    """
    if shutil.which("pg_dump") is None:
        click.echo(
            "WARNING: pg_dump is not installed — proceeding WITHOUT a pre-restore safety dump. The restore will fail on the psql step before touching the database; install the PostgreSQL client tools for a full safety net (or pass --no-safety-dump to silence this).",
            err=True,
        )
        return None
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = output_dir if output_dir is not None else Path()
    base.mkdir(parents=True, exist_ok=True)
    suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
    safety_dir = base / f"{_SAFETY_DUMP_PREFIX}{ts}-{suffix}"
    while safety_dir.exists():
        suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
        safety_dir = base / f"{_SAFETY_DUMP_PREFIX}{ts}-{suffix}"
    try:
        safety_dir.mkdir()
        _run_pg_dump(raw_url, safety_dir / _DB_SQL_FILE, timeout=timeout)
    except Exception as exc:
        shutil.rmtree(safety_dir, ignore_errors=True)
        raise click.ClickException(f"Taking the pre-restore safety dump FAILED — refusing to wipe the current data (nothing was restored). Fix the dump failure, or pass --no-safety-dump to explicitly proceed without a safety net. Original error: {_redact_url_secrets(str(exc), raw_url)}") from exc
    click.echo(f"Pre-restore safety dump taken: {safety_dir} (the previous cluster stays restorable)")
    return safety_dir


# ── Sync data export (via psycopg) ────────────────────────────────────────────


def _export_checkpoint_blobs_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM checkpoint_blobs ORDER BY organisation_id, thread_id, checkpoint_ns, channel, version")
        return [_serialise_export_row(row) for row in cur]


def _export_checkpoints_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM checkpoints ORDER BY organisation_id, thread_id, checkpoint_ns, checkpoint_id")
        return [_serialise_export_row(row) for row in cur]


def _export_checkpoint_writes_sync(raw_url: str) -> list[dict[str, Any]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM checkpoint_writes ORDER BY organisation_id, thread_id, checkpoint_ns, checkpoint_id, task_id, idx")
        return [_serialise_export_row(row) for row in cur]


_CREDENTIALS_TABLES: list[str] = ["connector_instances", "model_backends"]


def _export_credentials_references_sync(raw_url: str) -> dict[str, list[dict[str, Any]]]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    result: dict[str, list[dict[str, Any]]] = {}
    with _connect(raw_url, row_factory=dict_row) as conn:
        for table in _CREDENTIALS_TABLES:
            rows: list[dict[str, Any]] = []
            with conn.cursor() as cur:
                sql = f"SELECT id, organisation_id, name, credentials_ciphertext FROM {table} ORDER BY id"  # nosec B608  # noqa: S608 -- table comes from a fixed whitelist
                cur.execute(sql)
                for row in cur:
                    org_id = row.get("organisation_id")
                    row["id"] = str(row["id"])
                    row["organisation_id"] = str(org_id) if org_id is not None else None
                    if isinstance(row.get("credentials_ciphertext"), bytes | memoryview):
                        ct = bytes(row["credentials_ciphertext"])
                        row["credentials_ciphertext"] = ct.hex()  # nosemgrep: credential-not-in-state
                    rows.append(row)
            result[table] = rows
    return result


def _restore_checkpoint_blobs_sync(raw_url: str, blobs: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_blobs CASCADE")
            for row in blobs:
                blob: bytes | None = None
                raw_blob = row.get("blob")
                if raw_blob is not None:
                    blob = bytes.fromhex(raw_blob) if raw_blob else b""
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoint_blobs (organisation_id, thread_id, checkpoint_ns, channel, version, type, blob) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("channel"),
                        row.get("version"),
                        row.get("type"),
                        blob,
                    ),
                )
        conn.commit()
    return len(blobs)


def _restore_checkpoints_sync(raw_url: str, checkpoints: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoints CASCADE")
            for row in checkpoints:
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoints (organisation_id, thread_id, checkpoint_ns, checkpoint_id,  parent_checkpoint_id, type, checkpoint, metadata) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("checkpoint_id"),
                        row.get("parent_checkpoint_id"),
                        row.get("type"),
                        _jsonb(row.get("checkpoint")),
                        _jsonb(row.get("metadata")),
                    ),
                )
        conn.commit()
    return len(checkpoints)


def _restore_checkpoint_writes_sync(raw_url: str, writes: list[dict[str, Any]]) -> int:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    with _connect(raw_url) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE checkpoint_writes CASCADE")
            for row in writes:
                blob: bytes | None = None
                raw_blob = row.get("blob")
                if raw_blob is not None:
                    blob = bytes.fromhex(raw_blob) if raw_blob else b""
                org_uuid = _parse_org_id(row.get("organisation_id"))
                cur.execute(
                    "INSERT INTO checkpoint_writes (organisation_id, thread_id, checkpoint_ns, checkpoint_id,  task_id, idx, channel, type, blob) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        org_uuid,
                        row.get("thread_id"),
                        row.get("checkpoint_ns"),
                        row.get("checkpoint_id"),
                        row.get("task_id"),
                        row.get("idx"),
                        row.get("channel"),
                        row.get("type"),
                        blob,
                    ),
                )
        conn.commit()
    return len(writes)


def _re_encrypt_credentials_sync(
    raw_url: str,
    creds: dict[str, list[dict[str, Any]]],
    old_fernet_key: str,
    new_fernet_key: str,
) -> dict[str, int]:
    if psycopg is None:
        raise RuntimeError(_ERR_PSYCOPG_UNAVAILABLE)
    old_fernet = Fernet(old_fernet_key.encode())
    new_fernet = Fernet(new_fernet_key.encode())
    counts: dict[str, int] = {}

    with _connect(raw_url) as conn:
        with conn.cursor() as cur:
            for table, rows in creds.items():
                if table not in _CREDENTIALS_TABLES:
                    _log.warning("Skipping unknown credentials table: %s", table)
                    continue
                rekeyed = 0
                for row in rows:
                    hex_ct = row.get("credentials_ciphertext", "")
                    if not hex_ct:
                        continue
                    old_ct = bytes.fromhex(hex_ct)
                    try:
                        plaintext = old_fernet.decrypt(old_ct)
                    except InvalidToken as exc:
                        raise RuntimeError(f"Failed to decrypt {table} row {row.get('id', '?')}: --previous-fernet-key may be wrong") from exc
                    new_ct = new_fernet.encrypt(plaintext)
                    try:
                        row_id = uuid.UUID(row["id"])
                    except (ValueError, TypeError):
                        _log.warning("Invalid UUID in credentials row: %s", row.get("id", "?"))
                        continue
                    cur.execute(
                        f"UPDATE {table} SET credentials_ciphertext = %s WHERE id = %s",  # nosec B608  # noqa: S608 -- table is whitelist-validated
                        (new_ct, row_id),
                    )
                    rekeyed += 1
                counts[table] = rekeyed
        conn.commit()
    return counts


# ── Size helper ──────────────────────────────────────────────────────────────


def _print_size(backup_dir: Path) -> None:
    try:
        total = sum(f.stat().st_size for f in backup_dir.rglob("*") if f.is_file())
    except OSError as exc:
        _log.warning("Could not compute backup size: %s", exc)
        return
    click.echo(f"Total size: {_human_size(total)}")


def _human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# ── Data-safety helpers (FAR-672 / ADR 031 Decisions 6/7) ─────────────────────


def _warn_cloud_sync_output(backup_dir: Path) -> None:
    """Loud warning when the archive lands inside a cloud-synced folder.

    Dropbox/OneDrive/iCloud sync churn can corrupt a dump mid-write and
    syncs the archive (however secret-free) off the machine automatically.
    """
    lowered_parts = [part.casefold() for part in backup_dir.parts]
    # Component equality (case-folded): "Dropbox", "Dropbox (2)"-style vendor
    # roots are matched by name; ordinary names that merely CONTAIN the word
    # do not warn.
    hit = sorted(root for root in _CLOUD_SYNC_VENDOR_ROOTS if root in lowered_parts)
    if hit:
        click.echo(
            f"WARNING: backup target {backup_dir} is inside a cloud-synced folder ({', '.join(hit)}). Sync churn can corrupt the dump mid-write and the archive leaves the machine automatically. Back up to a local directory and copy the finished archive explicitly instead.",
            err=True,
        )


def _lock_artifact_mode(backup_dir: Path, artifact_names: list[str], *, user_specified_dir: bool) -> None:
    """Tighten the archive artifacts the BACKUP created to owner-only.

    Directories are chmod 0700 — the e(xecute) bit is the DIRECTORY search
    bit on POSIX: a directory at 0600 is unlistable AND unsearchable, so any
    file inside it (e.g. the TLS keypair under ``tls/``) becomes unresolvable
    and restoring it fails. Files are 0600. Each entry is tightened
    independently: one unchmoddable entry logs a warning and continues
    instead of aborting the sweep. Only artefacts THIS backup produced are
    touched — a user-supplied ``--output-dir`` root it did not create keeps
    its own mode (and contents).
    TODO(P3): Windows ACL hardening (chmod is a silent no-op there).
    """
    if os.name != "posix":
        return
    targets: list[Path] = []
    if not user_specified_dir:
        targets.append(backup_dir)
    targets.extend(backup_dir / name for name in artifact_names)
    for path in targets:
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except OSError as exc:
            _log.warning("backup_artifact_mode_failed path=%s error=%s", path, exc)


def _acquire_data_lock(data_dir: Path, *, mode: str) -> Any:
    """Take the exclusive data-dir lock, refusing while a launcher runs.

    A holder is named in the raised error (PIDs are part of the
    DataDirLockError message), so a start in flight blocks backup/restore
    with an actionable message instead of racing it.
    """
    from farna.launcher.supervisor import DataDirLock, DataDirLockError

    lock = DataDirLock(data_dir, mode=mode)
    try:
        lock.acquire()
    except DataDirLockError as exc:
        raise click.ClickException(str(exc)) from exc
    return lock


def _live_launcher_holder(data_dir: Path) -> Any | None:
    """The lock holder of a LIVE launcher for *data_dir*, or None.

    A lock record whose PID is gone is stale (the kernel released the flock when
    the process died) and is treated as "nothing running". Non-POSIX platforms
    have no verified-liveness seam, so they are also reported as "not live" —
    the data-dir lock acquisition is the authority that then refuses loudly.
    """
    from farna.launcher.supervisor import _read_lock_holder

    holder = _read_lock_holder(data_dir.parent / (data_dir.name + ".lock"))
    if holder is None or os.name != "posix":
        return None
    from farna.launcher.upgrade import _pid_alive

    return holder if _pid_alive(holder.pid) else None


def _stop_launcher_writers(data_dir: Path) -> bool:
    """Stop a LIVE launcher so the restore owns the database (FAR-2667).

    Returns True when a running launcher was stopped. Nothing to stop is a
    no-op (the data-dir lock acquired next is the authority on whether a
    writer remains). On a platform without the verified-liveness seam the
    holder is not signalled — the lock acquisition refuses loudly instead.
    """
    holder = _live_launcher_holder(data_dir)
    if holder is None:
        return False
    from farna.launcher.upgrade import UpgradeError, stop_running_stack

    click.echo(f"Stopping the running launcher (holder PID {holder.pid}) before the restore...")
    try:
        stop_running_stack(data_dir)
    except UpgradeError as exc:
        raise click.ClickException(f"Refusing to restore: the launcher for {data_dir} is still running after a stop request — {exc}") from exc
    except Exception as exc:  # LauncherError, or a platform without the stop seam
        raise click.ClickException(f"Refusing to restore: the running launcher for {data_dir} (holder PID {holder.pid}) cannot be stopped from here ({exc}). Stop it first: farna-control stop --data-dir {data_dir}") from exc
    click.echo("  Launcher stopped. It stays stopped until you start it again.")
    return True


def _postgres_endpoint(url: str) -> tuple[str, int] | None:
    """``(host, port)`` of a Postgres URL (port defaults to 5432), or None."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port or _DEFAULT_PG_PORT
    except ValueError:
        return None
    return (host, port) if host else None


def _target_is_bundled_cluster(raw_url: str, launcher_admin_url: str | None) -> bool:
    """True when *raw_url* addresses the data dir's OWN bundled cluster.

    Only then may the restore start that cluster itself (see
    :func:`_bundled_cluster_for_restore`): the bundled port on a loopback host
    is the same endpoint the launcher composed, so a stop took it down and the
    replay needs it back. A target anywhere else — an operator's explicit URL
    to a separate database — is untouched by the stop and must never be
    shadowed by a cluster this command started.
    """
    if not launcher_admin_url:
        return False
    target = _postgres_endpoint(raw_url)
    bundled = _postgres_endpoint(launcher_admin_url)
    if target is None or bundled is None:
        return False
    target_host, target_port = target
    bundled_host, bundled_port = bundled
    return target_port == bundled_port and target_host in _LOOPBACK_HOSTS and bundled_host in _LOOPBACK_HOSTS


def _port_accepting(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """True when something accepts a TCP connection on ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _bundled_cluster_ready(host: str, port: int, bin_dir: Path) -> bool:
    """True once the bundled cluster ACCEPTS CONNECTIONS (not merely listens).

    ``pg_isready`` is the launcher's own readiness signal: a postmaster that is
    still in crash recovery binds its port but answers 57P03, so a bare TCP
    accept would let the replay start too early and fail. The TCP probe is
    only the fallback for a bundle that does not ship the tool.
    """
    from farna.launcher.initdb import _binary

    probe = _binary(bin_dir, "pg_isready")
    if Path(probe).is_file():
        try:
            result = subprocess.run([probe, "-h", host, "-p", str(port)], capture_output=True, check=False, timeout=30)  # noqa: S603 -- fixed argv from the launcher's own builder
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log.warning("restore.pg_isready_failed host=%s port=%s error=%s", host, port, exc)
            return False
        return result.returncode == 0
    return _port_accepting(host, port)


def _logged_tail(log_path: Path, *, lines: int = 20) -> str:
    """The last *lines* of a server log, for an actionable failure message."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<no server log>"
    tail = [line for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(tail) or "<server log is empty>"


def _await_bundled_cluster(process: subprocess.Popen[bytes], host: str, port: int, bin_dir: Path, log_path: Path) -> None:
    """Block until the started cluster accepts connections, or fail loudly."""
    deadline = time.monotonic() + _RESTORE_PG_READY_TIMEOUT
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            raise click.ClickException(f"The bundled PostgreSQL exited (status {status}) before it accepted connections, so the restore cannot replay into it. Last lines of {log_path}:\n{_logged_tail(log_path)}")
        if _bundled_cluster_ready(host, port, bin_dir):
            return
        time.sleep(_RESTORE_PROBE_INTERVAL)
    raise click.ClickException(f"The bundled PostgreSQL did not accept connections on {host}:{port} within {_RESTORE_PG_READY_TIMEOUT:.0f}s. Last lines of {log_path}:\n{_logged_tail(log_path)}")


def _stop_bundled_cluster(process: subprocess.Popen[bytes], log_path: Path) -> None:
    """Stop the cluster this window started: fast shutdown, then escalate.

    SIGINT is Postgres's FAST shutdown — it rolls back the open transactions
    and exits cleanly, which is what the NEXT start wants (no crash-recovery
    replay). Windows cannot deliver SIGINT to a child, so it falls back to
    ``terminate()``; the packaged native launcher is POSIX-only anyway.
    """
    if process.poll() is not None:
        return
    if sys.platform != "win32":
        with contextlib.suppress(OSError, ValueError):
            process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=_RESTORE_PG_STOP_TIMEOUT)
        return
    except subprocess.TimeoutExpired:
        _log.warning("restore.bundled_cluster_fast_shutdown_timeout log=%s", log_path)
    process.terminate()
    try:
        process.wait(timeout=_RESTORE_PG_KILL_TIMEOUT)
    except subprocess.TimeoutExpired:
        _log.warning("restore.bundled_cluster_terminate_timeout log=%s", log_path)
        process.kill()
        process.wait(timeout=_RESTORE_PG_KILL_TIMEOUT)


@contextlib.contextmanager
def _bundled_cluster_for_restore(data_dir: Path, raw_url: str) -> Iterator[None]:
    """Keep the bundled PostgreSQL UP for the restore's dump-and-replay window.

    The launcher supervises the bundled cluster and its ordered teardown stops
    it (``launcher/entry.py``: the postgres child has ``shutdown_priority=1``),
    so the stop that makes a restore safe ALSO takes the target's server down —
    and the safety dump, the psql/psycopg replay, the credential re-encryption,
    the collation check and the posture re-run all connect to it. Without a
    window the documented native recovery connects to nothing.

    So: while the application writers stay stopped, start ONLY the bundled
    cluster (the supervisor's own argv, the launcher's own readiness probe) for
    the duration of the dumping and replaying, then stop it again. The operator
    still starts the release deliberately afterwards — nothing here restarts the
    stack, and the printed start command is the only way back.

    A cluster that is ALREADY accepting connections is left alone and NOT
    stopped on exit: this window only stops what it started.
    """
    from farna.launcher.entry import (
        PG_VERSION_FILE,
        PGDATA_DIRNAME,
        POSTGRES_HOST,
        _bundled_service_env,
        resolve_bin_dir,
    )
    from farna.launcher.initdb import _binary, postgres_server_argv

    endpoint = _postgres_endpoint(raw_url)
    if endpoint is None:
        raise click.ClickException(f"Cannot parse {_url_display(raw_url)} as a PostgreSQL target, so the bundled cluster cannot be started for the restore.")
    host, port = endpoint
    if _port_accepting(host, port):
        click.echo(f"  The bundled PostgreSQL is already accepting connections on {host}:{port}; the restore reuses it.")
        yield
        return

    pgdata = data_dir / PGDATA_DIRNAME
    if not (pgdata / PG_VERSION_FILE).is_file():
        raise click.ClickException(f"Cannot bring the bundled PostgreSQL back up: {pgdata} holds no initialised cluster. Start it with `farna-control start --data-dir {data_dir}` first, or restore with --db-url pointing at a database that is already running.")
    bin_dir = resolve_bin_dir()
    binary = Path(_binary(bin_dir, "postgres"))
    if not binary.is_file():
        raise click.ClickException(f"The bundled PostgreSQL server is missing at {binary}. Reinstall the release, or pass --db-url pointing at a running database.")
    log_path = data_dir / _RESTORE_PG_LOG
    click.echo(f"Starting ONLY the bundled PostgreSQL ({POSTGRES_HOST}:{port}) for the restore; the API and workers stay stopped...")
    argv = postgres_server_argv(pgdata, POSTGRES_HOST, port, bin_dir)
    # The SAME environment allowlist the supervisor hands the postgres child, so
    # the operator's shell (proxy vars, secrets, PG*) cannot leak into it.
    child_env = _bundled_service_env(dict(os.environ))
    with log_path.open("ab") as server_log:
        try:
            process = subprocess.Popen(  # nosec B603 -- argv comes from the launcher's own builder, never shell=True  # noqa: S603
                argv,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                env=child_env,
                start_new_session=True,
            )
        except OSError as exc:
            raise click.ClickException(f"Cannot start the bundled PostgreSQL ({binary}): {exc}. Last lines of {log_path}:\n{_logged_tail(log_path)}") from exc
        try:
            _await_bundled_cluster(process, POSTGRES_HOST, port, bin_dir, log_path)
            click.echo(f"  Bundled PostgreSQL is accepting connections on {POSTGRES_HOST}:{port}.")
            yield
        finally:
            _stop_bundled_cluster(process, log_path)
            click.echo("  Bundled PostgreSQL stopped again; the application writers stay stopped.")


def _start_command(opts: _RestoreOptions) -> str:
    """The exact command that starts the restored release (never run here).

    The compose form is written for the layout the operator is actually in: the
    SHIPPED bundle (``scripts/package_compose_release.py``), where
    ``docker-compose.prod.yml`` is a top-level file and ``compose-rollback.sh``
    sits beside it - not the repo's ``deploy/compose/`` path, which does not
    exist in a bundle.

    ``TAG=`` is NOT the mechanism: the bundle's compose file hard-pins
    ``image: ghcr.io/farnalabs/farna-control:<release>`` (a literal tag, so the
    file can carry a checksum), and ``TAG`` only sets ``FARNA_CONTROL_VERSION``.
    Printing ``TAG=<previous> docker compose -f ... up -d`` would therefore
    restart the release that just failed. ``compose-rollback.sh`` merges a
    one-line image override instead, which is what actually starts the previous
    release. The helper name matches ``scripts/compose-restore.sh``'s
    ``print_start_command`` so both recovery paths tell the operator the same
    thing.
    """
    if opts.data_dir is not None:
        return f"farna-control start --data-dir {opts.data_dir}"
    return "./compose-rollback.sh <previous-version>"


def _print_start_guidance(opts: _RestoreOptions, *, stopped: bool, failed: bool = False) -> None:
    """Print the exact start command; NEVER start anything (FAR-2667 item 5).

    An automatic restart would hand a half-verified database to an app that
    then refuses to boot; the operator starts it deliberately.

    ``failed`` routes the text to stderr and says the restore did NOT complete.
    It is printed on BOTH paths because the failure is exactly the moment the
    writers are already stopped and the operator needs something to copy: a
    start command that only appears on success leaves a failed native restore
    with a dark stack and no command to bring it back.
    """
    stream = sys.stderr if failed else sys.stdout
    click.echo("", file=stream)
    if stopped:
        click.echo("The launcher was stopped for the restore and is still stopped.", file=stream)
    if failed:
        click.echo("The restore did NOT complete. The database may be part-restored; read the failure above before starting anything.", file=stream)
    click.echo("Writers stay stopped. Start the release with:", file=stream)
    click.echo(f"  {_start_command(opts)}", file=stream)
    click.echo("No automatic restart was performed.", file=stream)


def _read_state_mac(state_path: Path) -> str | None:
    """Read the HMAC identity fingerprint recorded in a state.json envelope.

    The envelope's ``mac`` is an HMAC over the (credential-free) state
    payload keyed by the instance's state_hmac_key — a stable, non-secret
    fingerprint that pins an instance across boots (the key and ports do
    not change). Two machines' instances can never share it.
    """
    try:
        envelope = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"state.json at {state_path} is unreadable: {exc}") from exc
    if not isinstance(envelope, dict):
        return None
    mac = envelope.get("mac")
    return mac if isinstance(mac, str) and mac else None


def _launcher_snapshot_files(data_dir: Path, backup_dir: Path, include_secrets: bool) -> tuple[dict[str, Any], list[str]]:
    """Copy the launcher data-dir artefacts into the archive; describe them.

    Included unconditionally (when present): ``state.json`` — credential-free
    by contract. Included ONLY with ``--include-secrets``: the 0600 secrets
    file and the credential-bearing conf profile (both carry the generated
    service passwords; a default archive must never contain them). TLS
    keypair files ride along when the data dir has a ``tls/`` directory —
    NOTE in the manifest: this includes the server PRIVATE KEY even in a
    default archive (the credential-free claim is about launcher/FERNET
    secrets, not the TLS key; excluded by not pointing --data-dir at it).

    Returns ``(manifest_section, checksums)`` — checksums over every file
    copied here so the restore's verification loop covers the ride-alongs.
    The caller must snapshot state.json AFTER ``_record_last_backup``
    (ordering locked by test) so the archived file + its ``state_mac``
    describe the data dir's FINAL on-disk state.
    """
    section: dict[str, Any] = {
        "state_json_included": False,
        "state_mac": None,
        "conf_profile": {"included": False, "note": "excluded by default (carries generated credentials)"},
        "secrets_file": {"included": False, "note": "excluded by default (generated credentials)"},
        "tls_files": [],
        "tls_note": ("Rides along when the data dir has tls/ — includes the server PRIVATE KEY. Point --data-dir at a dir without tls/ (or leave --data-dir out) to keep TLS material off the archive."),
        "ephemeral": {"redis_saq_queue_data": ("EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); queued jobs do not survive a backup/restore cycle")},
    }
    checksums: list[str] = []
    _snapshot_launcher_state(data_dir, backup_dir, section, checksums)
    if include_secrets:
        _snapshot_launcher_secrets(data_dir, backup_dir, section, checksums)
    _snapshot_tls_files(data_dir, backup_dir, section, checksums)
    return section, checksums


def _snapshot_launcher_state(
    data_dir: Path,
    backup_dir: Path,
    section: dict[str, Any],
    checksums: list[str],
) -> None:
    """Copy the credential-free launcher ``state.json`` and record its MAC."""
    state_path = data_dir / _LAUNCHER_STATE_FILE
    if not state_path.exists():
        return
    shutil.copy2(state_path, backup_dir / _LAUNCHER_STATE_FILE)
    section["state_json_included"] = True
    checksums.append(_LAUNCHER_STATE_FILE)
    try:
        section["state_mac"] = _read_state_mac(state_path)
    except RuntimeError as exc:
        _log.warning("backup_state_identity_unreadable path=%s error=%s", state_path, exc)


def _snapshot_launcher_secrets(
    data_dir: Path,
    backup_dir: Path,
    section: dict[str, Any],
    checksums: list[str],
) -> None:
    """Copy the credential-bearing launcher files (``--include-secrets`` only)."""
    secrets_path = data_dir / _LAUNCHER_SECRETS_FILE
    if secrets_path.exists():
        shutil.copy2(secrets_path, backup_dir / _LAUNCHER_SECRETS_FILE)
        section["secrets_file"]["included"] = True
        checksums.append(_LAUNCHER_SECRETS_FILE)
    conf_path = data_dir / _LAUNCHER_CONF_FILE
    if conf_path.exists():
        shutil.copy2(conf_path, backup_dir / _LAUNCHER_CONF_FILE)
        section["conf_profile"]["included"] = True
        checksums.append(_LAUNCHER_CONF_FILE)
    # The loud warning fires ONLY when at least one secret-bearing file
    # was actually copied — a --include-secrets run over a data dir with
    # no secrets/conf on disk stayed credential-free.
    if section["secrets_file"]["included"] or section["conf_profile"]["included"]:
        click.echo(
            "WARNING: --include-secrets produced a FULL-fidelity archive that contains the launcher "
            "secrets file and the credential-bearing conf profile (generated Postgres/Redis passwords "
            "and the state HMAC key). Protect the archive accordingly — anyone holding it gains full "
            "access to the bundled services. A default archive excludes all of this.",
            err=True,
        )


def _snapshot_tls_files(
    data_dir: Path,
    backup_dir: Path,
    section: dict[str, Any],
    checksums: list[str],
) -> None:
    """Copy the data dir's ``tls/`` ride-alongs into the archive."""
    tls_dir = data_dir / _TLS_DIRNAME
    if not tls_dir.is_dir():
        return
    target_tls = backup_dir / _TLS_DIRNAME
    target_tls.mkdir(exist_ok=True)
    for tls_file in sorted(tls_dir.iterdir()):
        if tls_file.is_file():
            shutil.copy2(tls_file, target_tls / tls_file.name)
            section["tls_files"].append(f"{_TLS_DIRNAME}/{tls_file.name}")
            checksums.append(f"{_TLS_DIRNAME}/{tls_file.name}")


def _record_last_backup(data_dir: Path, timestamp: str) -> None:
    """Persist the last-successful-backup timestamp into the HMAC'd state.json.

    The field is optional and forward-compatible: a data dir without a
    state.json (or without readable secrets) simply skips the record with a
    logged note — the database backup itself is never at risk from here.
    """
    from farna.launcher.state import LauncherState, load_state, save_state

    state_path = data_dir / _LAUNCHER_STATE_FILE
    if not state_path.exists():
        _log.info("backup_timestamp_skipped (no state.json) data_dir=%s", data_dir)
        return
    try:
        secrets = json.loads((data_dir / _LAUNCHER_SECRETS_FILE).read_text(encoding="utf-8"))
        hmac_key = bytes.fromhex(secrets["state_hmac_key"])
        current = load_state(state_path, hmac_key)
        updated = LauncherState(
            postgres_port=current.postgres_port,
            redis_port=current.redis_port,
            api_port=current.api_port,
            schema_version=current.schema_version,
            last_backup_at=timestamp,
        )
        save_state(updated, state_path, hmac_key)
        _log.info("backup_timestamp_recorded data_dir=%s at=%s", data_dir, timestamp)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Launcher state is an enhancement, never a backup blocker.
        _log.warning("backup_timestamp_not_recorded data_dir=%s error=%s", data_dir, exc)
    except Exception:
        # Broad catch-all must never break the backup itself — but it is NOT
        # collapsed into the specific tuple above: an unknown failure here is
        # a bug and deserves the traceback in the log, at ERROR level.
        _log.exception("backup_timestamp_unexpected_failure data_dir=%s", data_dir)


def _server_data_directory(raw_url: str) -> str | None:
    """The server's own ``data_directory`` (best effort; None when unreadable)."""
    try:
        with _connect(raw_url, timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            row = cur.fetchone()
    except Exception:
        _log.warning("restore_target_data_directory_unreadable")
        return None
    return str(row[0]) if row and row[0] else None


def _disk_preflight(
    backup_dir: Path,
    data_dir: Path | None,
    *,
    target_fs: Path | None = None,
    raw_url: str | None = None,
) -> None:
    """Refuse an overwrite the disk cannot hold — BEFORE anything is wiped.

    Rough guard: the restored dump needs about its own size again in
    temporary WAL/transaction space, so 2x the database.sql size is
    required free on **the filesystem the DATABASE lives on** — measuring the
    archive's filesystem (which may be a different volume entirely, e.g.
    ``/backups``) can pass while the write still hits ENOSPC.

    Target order: the data dir (native), then ``--target-fs`` /
    ``DB_CAPACITY_PATH`` (the compose deployment mounts the database volume
    read-only for exactly this kind of measurement), then the server's own
    ``data_directory`` when it is visible from here. When none of those name a
    measurable path the archive's filesystem is measured with a loud warning —
    never silently.
    """
    db_sql = backup_dir / _DB_SQL_FILE
    if not db_sql.exists():
        return
    try:
        needed = db_sql.stat().st_size * 2
    except OSError as exc:
        _log.warning("backup_preflight_size_failed path=%s error=%s", db_sql, exc)
        return
    target_fs = _resolve_preflight_target(backup_dir, data_dir, target_fs, raw_url)
    try:
        free = shutil.disk_usage(str(target_fs)).free
    except OSError as exc:
        _log.warning("backup_preflight_statfs_failed path=%s error=%s", target_fs, exc)
        return
    if free < needed:
        raise click.ClickException(f"Disk pre-flight FAILED: the restore needs ~2x the dump size free ({_human_size(needed)}) but only {_human_size(free)} is available on {target_fs}. Free space before restoring — refusing to wipe the current data.")


def _resolve_preflight_target(backup_dir: Path, data_dir: Path | None, target_fs: Path | None, raw_url: str | None) -> Path:
    """The best available path on the DATABASE's filesystem (see _disk_preflight)."""
    if data_dir is not None:
        return data_dir
    if target_fs is not None and Path(target_fs).exists():
        return Path(target_fs)
    if raw_url is not None:
        server_dir = _server_data_directory(raw_url)
        if server_dir is not None and Path(server_dir).is_dir():
            return Path(server_dir)
    click.echo(
        "WARNING: cannot identify the database's own filesystem (no --data-dir, no --target-fs/DB_CAPACITY_PATH, "
        "and the server's data_directory is not visible from here), so the disk pre-flight measures the ARCHIVE filesystem instead. "
        "Pass --target-fs <a path on the database volume> for an accurate check.",
        err=True,
    )
    return backup_dir


def _check_instance_identity(
    data_dir: Path,
    manifest: dict[str, Any],
    replace_cluster: bool,
) -> None:
    """Refuse to restore over a DIFFERENT populated instance (verify-before-wipe).

    The archive's identity fingerprint is the source data-dir state.json's
    HMAC (recorded in the manifest). A populated target carries its own
    fingerprint. Rules:
    * fresh / uninitialized target (no state.json) — EXEMPT (with a
      database-populated probe refusing — see _check_target_db_populated):
      disaster recovery and machine migration must keep working.
    * target state.json without an identity fingerprint — REFUSED (closes
      the both-sides-None bypass: a populated target whose state.json has
      no ``mac`` cannot be proven to be or not be the archived instance,
      and ``--yes`` must not be able to suppress verification).
    * manifest without a fingerprint (legacy archive) — cannot compare;
      warn and proceed.
    * mismatch — REFUSED unless ``--replace-cluster`` (interactive-terminal
      confirmation printing both identifiers; ``--yes`` cannot suppress
      that confirmation, and a piped/non-tty stdin is refused outright).
    """
    identity = manifest.get("launcher_snapshot", {})
    archive_mac = identity.get("state_mac") if isinstance(identity, dict) else None
    target_state = data_dir / _LAUNCHER_STATE_FILE
    if not target_state.exists():
        return  # fresh / uninitialized target — exempt (DR / migration path)
    try:
        target_mac = _read_state_mac(target_state)
    except RuntimeError as exc:
        raise click.ClickException(f"The target data dir has an UNREADABLE state.json ({exc}). Use a clean data dir instead — refusing to restore over a state whose integrity cannot be checked.") from exc
    if target_mac is None:
        raise click.ClickException("The target data dir carries a populated state.json WITHOUT an identity fingerprint — integrity cannot be verified against the archive. Refusing to restore. Re-run the launcher on that data dir or use a clean data dir.")
    if archive_mac is None:
        if isinstance(identity, dict) and identity.get("state_json_included"):
            raise click.ClickException("The archive includes a state.json whose identity cannot be verified — refusing to restore over this data dir.")
        click.echo(
            "WARNING: legacy archive carries no instance identity — cannot verify this restore targets the backed-up installation. Verify manually that this is the intended target.",
            err=True,
        )
        return
    if target_mac == archive_mac:
        return
    if not replace_cluster:
        raise click.ClickException(
            "Instance identity MISMATCH — refusing to restore.\n"
            f"  Target instance: {target_mac}\n"
            f"  Archive instance: {archive_mac}\n"
            "The target data dir holds a DIFFERENT populated Farna Control instance than the backup. "
            "Pass --replace-cluster to intentionally replace it (with a confirmation), or restore "
            "onto a clean data dir."
        )
    click.echo(
        f"REPLACE CLUSTER CONFIRMATION — you are about to OVERWRITE a populated Farna Control instance:\n  Target instance (data dir): {target_mac}\n  Archive instance (backup):  {archive_mac}\nAll data in the target data dir's database will be replaced by the archive.",
        err=True,
    )
    if not _is_interactive_stdin():
        raise click.ClickException("--replace-cluster requires an INTERACTIVE confirmation (it intentionally overwrites a populated instance) — a piped/non-interactive stdin cannot approve it. Run the command directly in a terminal.")
    click.confirm("Replace the target instance with the archived one?", abort=True)


def _is_interactive_stdin() -> bool:
    """True only for a real terminal stdin — a piped ``echo y |`` must NOT be
    able to approve the destructive --replace-cluster confirmation."""
    try:
        return sys.stdin is not None and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    except (OSError, ValueError):
        return False


def _restore_launcher_files(backup_dir: Path, data_dir: Path) -> list[str]:
    """Install the archive's launcher artefacts into the target data dir.

    Present only in archives written with --data-dir (and with
    --include-secrets for the credential-bearing ones). Copying them is what
    makes DR/machine-migration work: a fresh target adopts the archive's
    credentials and ports instead of generating orphaned ones on first boot.
    Returns the names copied (for the restore transcript).
    """
    copied: list[str] = []
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in (_LAUNCHER_STATE_FILE, _LAUNCHER_SECRETS_FILE, _LAUNCHER_CONF_FILE):
        source = backup_dir / name
        if source.exists():
            shutil.copy2(source, data_dir / name)
            copied.append(name)
    tls_dir = backup_dir / _TLS_DIRNAME
    if tls_dir.is_dir():
        target_tls = data_dir / _TLS_DIRNAME
        target_tls.mkdir(exist_ok=True)
        for tls_file in sorted(tls_dir.iterdir()):
            if tls_file.is_file():
                shutil.copy2(tls_file, target_tls / tls_file.name)
                copied.append(f"{_TLS_DIRNAME}/{tls_file.name}")
    return copied


def _check_manifest_version(manifest: dict[str, Any]) -> None:
    """Refuse archives written by a NEWER Modulo (versioned manifest, FAR-672)."""
    raw_version = manifest.get("manifest_version")
    if raw_version is None:
        return  # legacy archive — accepted
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise click.ClickException(f"backup manifest_version is not an integer: {raw_version!r}")
    if raw_version > MANIFEST_VERSION:
        raise click.ClickException(f"Refusing to restore: the archive's manifest_version {raw_version} is NEWER than this Farna Control version supports (max {MANIFEST_VERSION}). Upgrade Farna Control first, then restore.")
    if raw_version < 1:
        raise click.ClickException(f"Refusing to restore: invalid manifest_version {raw_version!r}.")


def _read_and_validate_manifest(backup_dir: Path) -> dict[str, Any]:
    """Read + shape-validate the archive manifest BEFORE anything is echoed.

    A hostile/corrupt manifest must never reach the echo path (an echoed
    wrong-typed field is a crash — TypeError on f-string join, or worse, an
    operator believing a number they never wrote). Validates: the file
    exists, parses, is a JSON OBJECT, and every field echoed later carries
    the right type. Folds in the version gate (manifest_version).
    """
    manifest_path = backup_dir / _BACKUP_INFO_FILE
    if not manifest_path.exists():
        raise click.ClickException(f"{_BACKUP_INFO_FILE} not found in {backup_dir}")
    try:
        manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"The backup manifest {manifest_path} is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise click.ClickException(f"The backup manifest {manifest_path} is not a JSON object — refusing to restore from an archive whose manifest shape cannot be trusted.")
    _check_manifest_version(manifest)
    _validate_manifest_scalars(manifest)
    _validate_manifest_schema_versions(manifest)
    _validate_manifest_checksums(manifest)
    _validate_manifest_launcher_snapshot(manifest)
    return manifest


def _validate_manifest_scalars(manifest: dict[str, Any]) -> None:
    """Every echoed scalar field must carry its declared string type."""
    for key in ("timestamp", "backup_type", "db_version"):
        if key in manifest and not isinstance(manifest[key], str):
            raise click.ClickException(f"backup manifest field {key} is not a string: {manifest[key]!r}")
    if "fernet_key_hash" in manifest and not isinstance(manifest["fernet_key_hash"], str):
        raise click.ClickException(f"backup manifest field fernet_key_hash is not a string: {manifest['fernet_key_hash']!r}")


def _validate_manifest_schema_versions(manifest: dict[str, Any]) -> None:
    """``schema_versions`` must be a list of strings."""
    schema_versions = manifest.get("schema_versions", [])
    if not isinstance(schema_versions, list) or any(not isinstance(v, str) for v in schema_versions):
        raise click.ClickException(f"backup manifest field schema_versions is not a list of strings: {schema_versions!r}")


def _validate_manifest_checksums(manifest: dict[str, Any]) -> None:
    """``file_checksums`` must be a mapping of strings when present."""
    checksums = manifest.get("file_checksums")
    if checksums is not None and (not isinstance(checksums, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in checksums.items())):
        raise click.ClickException("backup manifest field file_checksums is not a mapping of strings")


def _validate_manifest_launcher_snapshot(manifest: dict[str, Any]) -> None:
    """``launcher_snapshot`` must be an object when present."""
    launcher = manifest.get("launcher_snapshot")
    if launcher is not None and not isinstance(launcher, dict):
        raise click.ClickException("backup manifest field launcher_snapshot is not an object")


def _known_revisions() -> list[str] | None:
    """Every alembic revision this binary ships (``None`` when unreadable).

    Note EVERY revision, not only the heads: an archive taken by an OLDER
    release records an older revision, which is the rollback case a pre-upgrade
    snapshot exists for. Comparing against the heads alone refuses every older
    archive — which is what made the documented recovery impossible.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = _get_backend_dir()
        alembic_ini = backend_dir / _ALEMBIC_INI
        if not alembic_ini.exists():
            return None
        script = ScriptDirectory.from_config(Config(str(alembic_ini)))
        return sorted({revision.revision for revision in script.walk_revisions()})
    except Exception:
        _log.exception("Failed to read this installation's migration tree")
        return None


def _check_schema_downgrade(manifest: dict[str, Any]) -> None:
    """Refuse restoring an archive written by a NEWER release than this binary.

    The manifest records the alembic revision(s) that were APPLIED at backup
    time. A revision this binary does not ship at all means the archive came
    from a newer release: the dump would leave the database at a revision this
    binary cannot serve, so it is refused BEFORE anything is wiped.

    An archive whose recorded revision this binary DOES ship is accepted even
    when it is behind the current head: that is the documented rollback — the
    restore puts the database back at that revision and the next start migrates
    it forward again. Archives without a schema record (or with the 'unknown'
    placeholder) cannot be compared and are allowed with a warning.
    """
    if manifest.get("manifest_version") is None:
        # Legacy archives (pre-FAR-672) did not document schema_versions as
        # alembic revisions — loud warning only; the hard gate applies to the
        # versioned manifests whose field is a genuine revision record.
        click.echo(
            "WARNING: legacy archive carries no verifiable schema version — cannot check for a downgrade.",
            err=True,
        )
        return
    archive_versions = manifest.get("schema_versions") or []
    if not archive_versions or any(not isinstance(v, str) or v == "unknown" for v in archive_versions):
        click.echo("WARNING: archive carries no verifiable schema version — cannot check for a downgrade.", err=True)
        return
    known = _known_revisions()
    if known is None:
        click.echo(
            f"WARNING: cannot read this installation's migration tree — the archive declares {', '.join(archive_versions)}. Verify manually that this binary ships those revisions before restoring.",
            err=True,
        )
        return
    unknown = sorted(set(archive_versions) - set(known))
    if unknown:
        raise click.ClickException(
            f"Refusing to restore: the archive records schema revision(s) {', '.join(unknown)} that this Farna Control does not ship — the archive was written by a NEWER release than this binary. Upgrade Farna Control to the release that wrote the archive first, then restore."
        )
    current_heads = _get_schema_versions()
    if set(archive_versions) - set(current_heads):
        click.echo(
            f"  Archive schema {', '.join(archive_versions)} is behind this release ({', '.join(current_heads)}): the restore puts the database back at the archived revision and the next start migrates it forward.",
            err=True,
        )


def _read_target_revisions(raw_url: str) -> list[str]:
    """The target database's recorded alembic revisions (``[]`` when fresh)."""
    with _connect(raw_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public.alembic_version",))
        present = cur.fetchone()
        if not present or not present[0]:
            return []
        cur.execute("SELECT version_num FROM alembic_version")
        return sorted({str(row[0]) for row in cur.fetchall() if row and row[0]})


def _known_revision_ancestry(head_revision: str) -> set[str] | None:
    """Every revision at or below *head_revision* in this binary's tree.

    ``None`` when the tree cannot be read or does not know *head_revision*:
    the caller must then refuse rather than guess a lineage.
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_ini = _get_backend_dir() / _ALEMBIC_INI
        if not alembic_ini.exists():
            return None
        script = ScriptDirectory.from_config(Config(str(alembic_ini)))
        return {revision.revision for revision in script.iterate_revisions(head_revision, "base")}
    except Exception:
        _log.warning("restore_target_ancestry_unreadable head=%s", head_revision)
        return None


def _target_descends_from_archive(target_revisions: list[str], archive_heads: list[str]) -> bool | None:
    """Is every target revision at or below an archive head?

    ``True`` — the target has MIGRATED PAST the snapshot: the normal
    "the new release migrated and then failed" rollback, which the snapshot is
    exactly the recovery point for.
    ``False`` — a target revision is unrelated to the archive's lineage, so this
    is a different deployment (or a different release train).
    ``None`` — cannot be determined from this binary's migration tree.
    """
    for target in target_revisions:
        ancestry = _known_revision_ancestry(target)
        if ancestry is None:
            return None
        if not any(head in ancestry for head in archive_heads):
            return False
    return True


def _check_target_schema_matches(manifest: dict[str, Any], raw_url: str, replace_cluster: bool) -> None:
    """Verify the TARGET database is the one the archive came from (compose path).

    ``--data-dir`` gives the restore a ``state.json`` instance identity to
    compare. The compose deployment has no data dir — which used to mean BOTH
    destructive guards were skipped and a wrong ``DATABASE_ADMIN_URL`` wiped a
    live cluster with no verification at all. With ``--verify-target-schema``
    the target's recorded schema is the identity, and three cases are told
    apart:

    * **same revisions** — restore proceeds;
    * **target AHEAD of the snapshot** (every target revision descends from an
      archive head in this binary's migration tree) — the release migrated and
      then failed, which is exactly the rollback the snapshot exists for: the
      restore proceeds, loudly noting that those migrations are being rolled
      back;
    * **anything else** — an unrelated revision, no ``alembic_version`` at all
      (not a bootstrapped Farna Control database), or a lineage this binary
      cannot verify: REFUSED.

    ``--replace-cluster`` remains the deliberate override (it is a second,
    explicit flag, so a wrapper cannot pre-supply it by accident).
    """
    archive_heads = [str(entry) for entry in (manifest.get("schema_versions") or [])]
    if not archive_heads or any(head == "unknown" for head in archive_heads):
        raise click.ClickException(
            "Refusing to restore: --verify-target-schema was requested but the archive records no verifiable schema version, "
            "so the target database cannot be proven to be the one this archive came from. Re-take the snapshot with this release, "
            "or pass --replace-cluster if you have verified the target by hand."
        )
    try:
        target_revisions = _read_target_revisions(raw_url)
    except Exception as exc:
        raise click.ClickException(f"Refusing to restore: --verify-target-schema could not read the target database's schema ({_url_display(raw_url)}: {exc}). Nothing has been changed.") from exc
    if target_revisions and set(target_revisions) == set(archive_heads):
        click.echo(f"  Target schema verified: {', '.join(target_revisions)}")
        return
    if target_revisions and _target_descends_from_archive(target_revisions, archive_heads) is True:
        click.echo(
            f"  Target schema verified: the target has migrated past the snapshot ({', '.join(target_revisions)} is ahead of {', '.join(archive_heads)}) — restoring ROLLS THOSE MIGRATIONS BACK with the data.",
            err=True,
        )
        return
    target_text = ", ".join(target_revisions) if target_revisions else "no alembic_version (not a bootstrapped Farna Control database)"
    if replace_cluster:
        click.echo(
            f"WARNING: --verify-target-schema found a MISMATCH but --replace-cluster overrides it:\n  Target: {target_text}\n  Archive: {', '.join(archive_heads)}",
            err=True,
        )
        return
    raise click.ClickException(
        "Refusing to restore: the TARGET database does not match the snapshot's recorded schema, so this is not provably the database the snapshot came from.\n"
        f"  Target database: {target_text}\n"
        f"  Snapshot:        {', '.join(archive_heads)}\n"
        "Nothing has been changed. Check DATABASE_ADMIN_URL points at the deployment the snapshot was taken from, or pass --replace-cluster if you have verified the target by hand."
    )


def _db_has_modulo_tables(raw_url: str) -> bool:
    """Does the target database already hold application tables?

    The fresh-target identity exemption is keyed on state.json's ABSENCE —
    but a POPULATED database combined with a fresh/absent data dir must NOT
    be exempt: that is someone's production data. Probes for any base table
    in the public schema; conservative on error (True = treat as populated
    ⇒ require --replace-cluster — never silently exempt what we cannot
    verify).
    """
    if psycopg is None:
        return True  # cannot verify — insist on the deliberate opt-in
    try:
        with _connect(raw_url) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            row = cur.fetchone()
    except Exception:
        _log.exception("restore_target_db_probe_failed")
        return True
    return bool(row and row[0])


def _check_archived_state_consistency(backup_dir: Path, manifest: dict[str, Any]) -> None:
    """Verify the ARCHIVED state.json matches the manifest's recorded MAC.

    Adoption copies the archive's launcher files over the target's. Before
    any copy, the archived state.json's envelope ``mac`` must equal the
    manifest's recorded ``state_mac`` — a mismatch means the archive's
    components disagree (tampered / partial / mistrustworthy archive) and
    NOTHING is adopted. Refuses when the archive is DESCRIBED as including
    a state.json that is absent from disk.
    """
    identity = manifest.get("launcher_snapshot") or {}
    if not isinstance(identity, dict):
        return
    recorded_mac = identity.get("state_mac")
    archived_state = backup_dir / _LAUNCHER_STATE_FILE
    if identity.get("state_json_included") and not archived_state.exists():
        raise click.ClickException("The archive's manifest claims a launcher state.json was backed up, but the file is NOT in the archive — refusing to adopt launcher artefacts from an incomplete archive.")
    if not archived_state.exists() or recorded_mac is None:
        return
    try:
        archived_mac = _read_state_mac(archived_state)
    except RuntimeError as exc:
        raise click.ClickException(f"The archived state.json is unreadable ({exc}) — refusing to adopt launcher artefacts from this archive.") from exc
    if archived_mac != recorded_mac:
        raise click.ClickException(f"The archive's state.json identity does NOT match the manifest's recorded fingerprint:\n  Archived state.json mac: {archived_mac}\n  Manifest state_mac:      {recorded_mac}\nRefusing to adopt launcher artefacts — the archive is inconsistent or tampered with.")


class RestorePostureError(click.ClickException):
    """The restored cluster's role/grant posture could not be established.

    A posture failure can leave ``farna_control_app`` able to bypass RLS (or missing
    the grants it needs), which is a tenant-isolation breach — so the restore
    FAILS instead of reporting success. The data itself is already restored:
    fix the reported check, then re-run the posture step with
    ``uv run python -m farna.db.bootstrap_role`` (with ``DATABASE_ADMIN_URL``
    and ``DATABASE_URL`` set) before serving from the cluster.
    """


def _url_role(url: str) -> str:
    """Return the role (URL user) of a Postgres URL, or '' when it has none."""
    return urllib.parse.unquote(urllib.parse.urlsplit(url).username or "")


_APP_URL_PREFIXES = (_POSTGRES_ASYNC_PREFIX, "postgresql+psycopg://", "postgresql://", "postgres://")


def _url_display(url: str) -> str:
    """A credential-free description of a connection URL for error messages.

    Only the scheme and the role (username) are echoed — never the userinfo,
    so a password embedded in the URL cannot reach stderr or the restore log
    (``click.ClickException`` messages are printed and logged verbatim).
    """
    try:
        parts = urllib.parse.urlsplit(url)
        role = urllib.parse.unquote(parts.username or "")
    except ValueError:
        return "<unparseable URL>"
    scheme = parts.scheme or "<no scheme>"
    return f"{scheme}:// (role {role!r})" if role else f"{scheme}://"


def _as_asyncpg_url(url: str) -> str:
    """Normalise a Postgres URL to the ``postgresql+asyncpg://`` SQLAlchemy form."""
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError:
        raise click.ClickException("Cannot use <unparseable URL> as the application connection URL — expected a postgresql:// URL. Pass --app-db-url with the app role's Postgres URL.") from None
    if parsed.scheme not in {"postgresql", "postgresql+asyncpg", "postgresql+psycopg", "postgres"} or (parsed.path == "/farna_control" and parsed.username == "user" and parsed.hostname == "host"):
        raise click.ClickException(f"Cannot use {_url_display(url)} as the application connection URL — expected a postgresql:// URL with the app_role's Postgres connection. Pass --app-db-url with the app role's Postgres URL.")
    suffix = url.split("://", 1)[1]
    if suffix.endswith("/modulo"):
        suffix = f"{suffix[: -len('/modulo')]}/farna_control"
    return _POSTGRES_ASYNC_PREFIX + suffix


def _resolve_restore_app_url(settings: Any | None, app_db_url: str | None, *, data_dir: Path | None = None) -> str:
    """Resolve the APP role URL independently of the restore target URL.

    ``bootstrap_roles`` (and its ``main()``) take the application role from
    ``DATABASE_URL``; ``--db-url`` / ``DATABASE_ADMIN_URL`` is the ADMIN
    connection the restore writes through and is legitimately a different,
    privileged role. Deriving the app URL from the target would configure the
    ADMIN role as ``farna_control_app`` and leave the real application role with
    whatever posture the restored cluster carried — so the source is always
    configuration (``--app-db-url``, else an exported ``DATABASE_URL``, else the
    data dir's own launcher config, else ``DATABASE_URL`` from settings) and
    never the target URL.

    The data-dir arm is the native operator's only source: nothing exported
    ``DATABASE_URL`` into their shell, so without it the posture step could not
    resolve the application role at all (and refused the whole restore).

    *settings* may be None, in which case Settings is constructed only if no
    explicit, exported or data-dir value exists — a native ``--data-dir`` run
    never needs it, and constructing it on a shell that exports none of the keys
    it requires would refuse the run.
    """
    exported = os.environ.get("DATABASE_URL") or None
    launcher_app_url = _launcher_config_values(data_dir).get("DATABASE_URL")
    source = app_db_url or exported or launcher_app_url
    if not source:
        source = settings.database_url if settings is not None else get_settings().database_url
    if not isinstance(source, str) or not source:
        raise click.ClickException("Cannot resolve the application role for the restore: --app-db-url was not given and DATABASE_URL is unset or empty. Pass --app-db-url with the app role's connection URL.")
    return _as_asyncpg_url(source)


@dataclass(frozen=True)
class _RestoreKeys:
    """The one Settings value a restore needs: the key it re-encrypts TO.

    Carrying just this instead of the whole Settings object lets a native
    ``--data-dir`` restore resolve the key from the installation's own pinned
    config (see :func:`_restore_fernet_key`) without constructing Settings at
    all — a native operator's shell exports none of the keys Settings requires,
    so ``get_settings()`` would refuse the run outright.
    """

    fernet_key: str


def _restore_fernet_key(data_dir: Path | None) -> str:
    """The FERNET_KEY the TARGET installation runs with.

    The data dir's ``config.env`` is authoritative when it names a key: that is
    the key the launcher persists and boots with (``install.sh`` writes it
    there, and it is what the archived ``fernet_key_hash`` is compared
    against). Without a data-dir key — the compose path, or a data dir that has
    not been bootstrapped — Settings is the source, exactly as before.
    """
    launcher_key = _launcher_config_values(data_dir).get("FERNET_KEY")
    if launcher_key:
        return launcher_key
    return get_settings().fernet_key


def _ensure_restore_posture(admin_url: str, app_url: str) -> None:
    """Re-run the promoted first-boot bootstrap/bootstrap_role ceremony.

    Reuses the promoted path (``farna.db.bootstrap_role.bootstrap_roles`` —
    ADR 031 Decision 2) so a restored database ends up with exactly the
    role/grant posture first boot would produce: farna_control_app NOBYPASSRLS,
    farna_control_system/farna_control_breakglass BYPASSRLS, grants re-applied, and the
    accounts allow-list + role-posture assertions re-run (fatal inside that
    path).

    NOT best-effort: a failure raises :class:`RestorePostureError` so
    ``farna-control restore`` exits non-zero and names the failing check. A restored
    cluster whose app role can still bypass RLS must never be reported as a
    completed restore.
    """
    import asyncio

    from farna.db.bootstrap_role import bootstrap_roles

    app_role = _url_role(app_url) or "<unknown>"
    click.echo(f"Re-applying role/grant posture for application role {app_role!r}...")
    try:
        asyncio.run(bootstrap_roles(admin_url, app_url))
    except Exception as exc:
        _log.exception("restore_posture_verification_failed")
        raise RestorePostureError(
            "Role-posture verification FAILED after the restore — the restored cluster must not "
            f"serve traffic yet. Failing check: {type(exc).__name__}: {exc}. The application role "
            f"{app_role!r} may still be able to bypass RLS or be missing its grants. Remediation: "
            "fix the reported problem, then run 'uv run python -m modulo.db.bootstrap_role' (with "
            "DATABASE_ADMIN_URL and DATABASE_URL set) to re-apply the posture before starting the "
            "application."
        ) from exc
    click.echo("  Role posture re-verified via the promoted bootstrap path")


def _collation_versions_same_family(stored: str, detected: str) -> bool:
    """True when two collation versions likely belong to the same family.

    Major-version parity (e.g. glibc 2.28 vs 2.35) is compatible drift —
    warn and proceed. A family change (e.g. musl 1.2.x -> glibc 2.x) is
    INCOMPATIBLE: the ordering semantics changed wholesale. Versions that
    cannot be parsed conservatively refuse (treated as incompatible).
    """
    stored_major = stored.split(".", 1)[0]
    detected_major = detected.split(".", 1)[0]
    if not (stored_major.isdigit() and detected_major.isdigit()):
        return False
    return stored_major == detected_major


def _check_collation_versions_sync(raw_url: str) -> None:
    """Refuse INCOMPATIBLE collation-version drift; warn and proceed on same-family drift.

    The compose -> native path moves a cluster built against an alpine/musl
    Postgres onto a Debian/glibc one: every text index built under the old
    collation version may sort differently under the new one, which corrupts
    index scans silently. Postgres records each collation's creation version
    in ``pg_collation`` and refuses to notice until asked — ask, and:

    * a DIFFERENT version family (musl -> glibc, or an unparsable version)
      is a HARD REFUSAL with the mismatch and the remediation named;
    * a compatible-but-older same-family version (glibc 2.28 -> 2.35) warns
      and proceeds.
    """
    if psycopg is None:
        return
    query = "SELECT collname, collversion, pg_collversion() AS detected_version, collprovider FROM pg_collation WHERE collversion IS NOT NULL AND collversion <> pg_collversion()"
    try:
        with _connect(raw_url) as conn, conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    except Exception:
        _log.exception("collation_version_check_failed")
        return
    for row in rows:
        name, stored, detected, provider = row[0], str(row[1]), str(row[2]), row[3]
        if _collation_versions_same_family(stored, detected):
            click.echo(
                f"COLLATION VERSION MISMATCH on collation {name!r} (provider {provider}): the cluster was "
                f"built with collation version {stored} but this Postgres runs {detected}. Text indexes "
                "created under the old version may be in the wrong order under this libc. Run 'reindexdb "
                "--all' after verifying the data. See the postgres 'locale provider / ICU & libc collation "
                "versions' documentation.",
                err=True,
            )
            continue
        raise click.ClickException(
            f"COLLATION VERSION INCOMPATIBLE on collation {name!r} (provider {provider}): the cluster was "
            f"built under collation version {stored} but this Postgres runs {detected} — a DIFFERENT "
            "collation family (the alpine/musl -> glibc compose -> native drift case). Text indexes built "
            "under the source collation can be silently mis-ordered, so the restore is REFUSED. Remediation: "
            "restore onto a target cluster whose libc/collation family matches the source, or rebuild every "
            "text index ('reindexdb --all') after reviewing the restored data. See the postgres 'locale "
            "provider / ICU & libc collation versions' documentation."
        )


# ── Dry-run preview ──────────────────────────────────────────────────────────


def _preview_restore(
    backup_dir: Path,
    manifest: dict[str, Any],
    keys: _RestoreKeys,
    previous_fernet_key: str | None,
) -> None:
    """Preview every restore step without touching the database.

    Integrity checks (checksums, JSON shape) are assumed to have already run.
    Raises the same ClickExceptions the real restore would raise for missing
    inputs, so a dry run surfaces any blocking problem before the operator
    commits to an irreversible restore.
    """
    click.echo("DRY RUN — no changes will be made.")

    _preview_db_restore(backup_dir)
    _preview_json_exports(backup_dir)
    _preview_credential_restore(backup_dir, manifest, keys, previous_fernet_key)

    click.echo("\nDry run complete — no changes were made.")


def _preview_db_restore(backup_dir: Path) -> None:
    """Describe the full-DB restore step of a dry run."""
    db_sql = backup_dir / _DB_SQL_FILE
    if db_sql.exists():
        click.echo(f"  WOULD restore database schema and data via psql from {db_sql.name}")
    else:
        click.echo("  No database.sql found — WOULD skip full DB restore")


def _preview_json_exports(backup_dir: Path) -> None:
    """Describe the checkpoint JSON export restores of a dry run."""
    for json_name, label in (
        (_BLOBS_JSON_FILE, "checkpoint blob records"),
        (_CHECKPOINTS_JSON_FILE, "checkpoint records"),
        (_WRITES_JSON_FILE, "checkpoint write records"),
    ):
        json_path = backup_dir / json_name
        if json_path.exists():
            records = json.loads(json_path.read_text(encoding="utf-8"))
            click.echo(f"  WOULD restore {len(records)} {label} from {json_name}")
        else:
            click.echo(f"  No {json_name} found — WOULD skip")


def _preview_credential_restore(
    backup_dir: Path,
    manifest: dict[str, Any],
    keys: _RestoreKeys,
    previous_fernet_key: str | None,
) -> None:
    """Describe the credential re-encryption step of a dry run."""
    creds_json = backup_dir / _CREDS_JSON_FILE
    if not creds_json.exists():
        click.echo("  No credentials_references.json found — WOULD skip credential restore")
        return
    creds: dict[str, list[dict[str, Any]]] = json.loads(creds_json.read_text(encoding="utf-8"))
    if _fernet_key_hash(keys.fernet_key) == manifest.get("fernet_key_hash", ""):
        click.echo("  FERNET_KEY unchanged — WOULD skip credential re-encryption")
        return
    if not previous_fernet_key:
        raise click.ClickException("FERNET_KEY has changed since backup. Provide --previous-fernet-key to re-encrypt credentials.")
    _preview_credential_reencryption(creds)


def _preview_credential_reencryption(creds: dict[str, list[dict[str, Any]]]) -> None:
    """Echo a per-table count of credentials that WOULD be re-encrypted."""
    total = 0
    for table, rows in creds.items():
        count = sum(1 for row in rows if row.get("credentials_ciphertext"))
        total += count
        if count:
            click.echo(f"  WOULD re-encrypt {count} {table} credentials")
    if not total:
        click.echo("  FERNET_KEY changed but no credentials carry ciphertext — nothing to re-encrypt")


# ── restore command internals ────────────────────────────────────────────────


@dataclass(frozen=True)
class _RestoreOptions:
    """The restore command's click parameters, carried as one immutable value.

    The phase helpers (`_validate_targets`, `_safety_dump`, `_restore_cluster`,
    `_restore_data_dir`) share this snapshot of what the operator asked for
    instead of threading eight positional arguments through every phase.
    """

    backup_dir: Path
    db_url: str | None
    app_db_url: str | None
    yes: bool
    previous_fernet_key: str | None
    dry_run: bool
    data_dir: Path | None
    replace_cluster: bool
    no_safety_dump: bool
    safety_dump_dir: Path | None = None
    target_fs: Path | None = None
    verify_target_schema: bool = False


def _run_restore(opts: _RestoreOptions) -> None:
    """Execute a restore from ``opts`` — the CLI command's whole body.

    Both connection URLs are resolved first (the target from ``--db-url`` / an
    exported ``DATABASE_ADMIN_URL`` / the data dir's own ``config.env`` /
    settings, the APP role from ``--app-db-url`` / ``DATABASE_URL``), so a
    configuration typo fails BEFORE anything destructive happens.

    ORDERING: every read-only verification runs FIRST — including the
    ``--dry-run`` short-circuit — and only then are the writers stopped and the
    data-dir lock taken. A preview or validation failure must have NO side
    effects: stopping a live launcher to print a plan would be a side effect
    the operator never agreed to.

    After the lock the restore runs inside ONE server window: when the target
    IS this data dir's bundled cluster, only that cluster is started (the API
    and workers stay stopped) and stopped again when the window closes, so the
    safety dump, the SQL/JSON replay, the credential re-encryption, the
    collation check and the posture re-run all have a server to talk to. The
    window matters because the stop that makes a restore safe is the same
    ordered teardown that stops the bundled PostgreSQL — the replay used to run
    against a server the same command had just stopped. Launcher data-dir
    adoption happens after the database restore succeeded, and a posture
    failure FAILS the restore. Any failure is logged, echoed, and re-raised as
    a ClickException; a data-dir lock is always released. On success the exact
    START command is printed and nothing is auto-restarted.
    """
    raw_url = _resolve_url(opts.db_url, data_dir=opts.data_dir)
    # The key the restore re-encrypts archived credentials to. Resolved from the
    # data dir's own config when there is one, so a native operator needs
    # nothing exported (see _restore_fernet_key).
    keys = _RestoreKeys(fernet_key=_restore_fernet_key(opts.data_dir))
    # Resolve the APPLICATION role URL up front. It is pure configuration
    # (``--app-db-url`` / ``DATABASE_URL``), so a typo or an unset value must
    # fail BEFORE the safety dump and the cluster wipe — never after them.
    app_url = _resolve_restore_app_url(None, opts.app_db_url, data_dir=opts.data_dir)
    bundled_target = _target_is_bundled_cluster(raw_url, _launcher_config_values(opts.data_dir).get("DATABASE_ADMIN_URL"))
    from farna.launcher.env_safety import scrub_os_environment

    scrub_os_environment()

    # Shape-validate the manifest FIRST — nothing about the archive is echoed
    # before it parses, is a JSON object, and every echoed field is typed.
    manifest = _read_and_validate_manifest(opts.backup_dir)
    click.echo(f"Backup timestamp: {manifest.get('timestamp', 'unknown')}")
    click.echo(f"Backup type: {manifest.get('backup_type', 'unknown')}")
    click.echo(f"DB version at backup: {manifest.get('db_version', 'unknown')}")
    click.echo(f"Schema versions: {', '.join(manifest.get('schema_versions', ['unknown']))}")

    # READ-ONLY verification first (nothing is stopped, nothing is locked):
    # the dry run returns here and the real run has proven the archive before
    # it takes a lock or touches the stack.
    if not _validate_targets(opts, manifest, raw_url, keys):
        return

    lock = None
    stopped = False
    try:
        if opts.data_dir is not None:
            # FAR-2667 item 5: a VIABLE restore stops the writers itself (it
            # never asks the operator to remember a stop) and leaves them
            # stopped. This runs only after the read-only verification above,
            # so a validation failure or a preview never leaves a running
            # launcher stopped. Taking the data-dir lock afterwards is what
            # proves they really are gone.
            stopped = _stop_launcher_writers(opts.data_dir)
            lock = _acquire_data_lock(opts.data_dir, mode="restore")

        # The stop above ALSO stopped the bundled PostgreSQL (the launcher
        # supervises it), and the dump and the replay both need it. Keep only
        # that cluster up for the window when it is this data dir's own target;
        # an explicitly named external database is untouched by the stop.
        data_dir = opts.data_dir
        with contextlib.ExitStack() as window:
            if data_dir is not None and bundled_target:
                window.enter_context(_bundled_cluster_for_restore(data_dir, raw_url))
            _safety_dump(opts, raw_url)
            _restore_cluster(opts, manifest, raw_url, keys)

            admin_url = os.environ.get("DATABASE_ADMIN_URL") or raw_url
            _ensure_restore_posture(admin_url, app_url)

        _restore_data_dir(opts, manifest)

        click.echo("\nRestore complete.")
        _print_start_guidance(opts, stopped=stopped)
    except click.ClickException:
        # Already a user-facing failure (including RestorePostureError) with
        # its own remediation message and non-zero exit — do not re-wrap it.
        _log.exception("Restore failed")
        _print_start_guidance(opts, stopped=stopped, failed=True)
        raise
    except Exception as exc:
        detail = _redact_url_secrets(str(exc), raw_url)
        _log.error("Restore failed: %s", detail, exc_info=exc)
        click.echo(f"Restore failed: {detail}", err=True)
        # The writers may already be stopped and the database possibly
        # part-restored. The start command must be printed on the FAILURE path
        # too, or a failed native restore leaves a dark stack and no way back.
        _print_start_guidance(opts, stopped=stopped, failed=True)
        raise click.ClickException(detail) from exc
    finally:
        if lock is not None:
            lock.release()


def _validate_targets(opts: _RestoreOptions, manifest: dict[str, Any], raw_url: str, keys: _RestoreKeys) -> bool:
    """Verify-before-wipe phase: nothing is mutated until every check passes.

    Runs the instance-identity check (when a data dir is in play), the disk
    pre-flight, the overwrite confirmation prompt, the archive checksum and
    JSON-export integrity checks, the dump-integrity gate (FAR-2667), and —
    after the --dry-run preview short-circuit — the schema-downgrade guard
    and the populated-database / ``--replace-cluster`` guard.

    Returns False when a --dry-run preview was emitted (the caller must
    stop); True when the restore may proceed to mutation.
    """
    # Verify-before-wipe ordering: identity + disk before any mutation.
    if opts.data_dir is not None:
        _check_instance_identity(opts.data_dir, manifest, opts.replace_cluster)
    elif opts.verify_target_schema:
        # No data dir means no state.json identity to compare; the target's
        # recorded schema stands in for it (see _check_target_schema_matches).
        _check_target_schema_matches(manifest, raw_url, opts.replace_cluster)
    _disk_preflight(opts.backup_dir, opts.data_dir, target_fs=opts.target_fs, raw_url=raw_url)

    if not opts.dry_run and not opts.yes:
        click.confirm("\nThis will OVERWRITE the current database. Continue?", abort=True)

    checksums = manifest.get("file_checksums")
    if checksums:
        click.echo("Verifying backup file integrity...")
        _verify_file_checksums(opts.backup_dir, checksums)
        click.echo("  All file checksums verified")

    _validate_dump_integrity(opts.backup_dir, manifest)

    _verify_json_exports(opts.backup_dir)

    if opts.dry_run:
        _preview_restore(opts.backup_dir, manifest, keys, opts.previous_fernet_key)
        return False

    _check_schema_downgrade(manifest)

    # Fresh-target exemption is about the DATA DIR, not the DATABASE: a
    # populated production database behind a fresh/absent data dir is NOT
    # eligible for the silent exemption — it must be --replace-cluster.
    if opts.data_dir is not None and _db_has_modulo_tables(raw_url) and not (opts.data_dir / _LAUNCHER_STATE_FILE).exists():
        if not opts.replace_cluster:
            raise click.ClickException(
                "The target database ALREADY CONTAINS application tables, but its data dir is fresh (no state.json) — this restore would silently overwrite an existing production database. Pass --replace-cluster if you really intend to replace it, or point at a clean database."
            )
        click.echo(
            "REPLACING an existing POPULATED database whose data dir was fresh/absent (acknowledged via --replace-cluster).",
            err=True,
        )
    return True


def _archive_member(backup_dir: Path, filename: str) -> Path:
    """Resolve a manifest-listed filename INSIDE *backup_dir* or refuse.

    ``file_checksums`` is untrusted archive content: a crafted manifest entry
    like ``../../etc/shadow`` or ``C:/Windows/win.ini`` would otherwise be
    joined onto the backup directory and HASHED — printing a digest of an
    arbitrary file to the operator's terminal. Every component must be a plain
    name, no symlink may appear on the path, and the resolved target must stay
    under the archive directory.
    """
    raw = filename.replace("\\", "/")
    if not raw or raw.startswith("/") or PureWindowsPath(filename).is_absolute() or _WINDOWS_DRIVE_PATH.match(filename):
        raise click.ClickException(f"Refusing to verify {filename!r}: the manifest names a path outside the archive.")
    if any(component in {"", ".", ".."} for component in raw.split("/")):
        raise click.ClickException(f"Refusing to verify {filename!r}: the manifest names a path outside the archive.")
    current = backup_dir
    for component in raw.split("/"):
        current = current / component
        try:
            entry_stat = current.lstat()
        except OSError:
            continue  # reported as missing below
        if stat.S_ISLNK(entry_stat.st_mode):
            raise click.ClickException(f"Refusing to verify {filename!r}: the archive entry is a symlink, so it may point outside the archive.")
    try:
        current.resolve().relative_to(backup_dir.resolve())
    except (OSError, ValueError) as exc:
        raise click.ClickException(f"Refusing to verify {filename!r}: the manifest names a path outside the archive.") from exc
    return current


def _verify_file_checksums(backup_dir: Path, checksums: dict[str, str]) -> None:
    """Refuse on any manifest-listed file that is missing, escapes or is mis-hashed."""
    for filename, expected_hash in checksums.items():
        file_path = _archive_member(backup_dir, filename)
        if not file_path.exists():
            raise click.ClickException(f"Backup file {filename} listed in manifest but not found on disk")
        actual_hash = _file_checksum(file_path)
        if actual_hash != expected_hash:
            raise click.ClickException(f"Checksum mismatch for {filename}: expected {expected_hash}, got {actual_hash}")


def _verify_json_exports(backup_dir: Path) -> None:
    """Parse-check the checkpoint/credential JSON exports BEFORE the wipe."""
    for json_name in (
        _BLOBS_JSON_FILE,
        _CHECKPOINTS_JSON_FILE,
        _WRITES_JSON_FILE,
        _CREDS_JSON_FILE,
    ):
        json_path = backup_dir / json_name
        if json_path.exists():
            try:
                json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"Corrupt JSON file {json_name}: {exc}") from exc


def _validate_dump_integrity(backup_dir: Path, manifest: dict[str, Any]) -> None:
    """Refuse a corrupt, foreign or MISSING dump BEFORE the target is wiped.

    Three gates, all fail-closed:

    0. **Presence.** An archive that describes a database dump must contain it.
       A manifest recording a ``database.sql`` checksum (or a pre-upgrade
       snapshot, which always carries one) with no file on disk used to fall
       through every check and "restore successfully" while changing nothing —
       a missing dump is worse than a truncated one, because it is silent.
    1. **Coverage.** If ``database.sql`` exists, the manifest MUST record a
       checksum for it. Without one nothing proves this dump belongs to this
       archive, so a foreign or hand-assembled directory is refused rather
       than replayed over the live database.
    2. **Completeness.** When the manifest ATTESTED completeness at dump time
       (``dump_complete``), re-verify pg_dump's completion trailer now: a file
       truncated after the manifest was written is caught even if the
       checksum was regenerated. Archives written before this attestation
       existed are verified by checksum alone, with a loud warning — they
       cannot prove more than that.
    """
    dump_path = backup_dir / _DB_SQL_FILE
    checksums = manifest.get("file_checksums")
    claims_dump = manifest.get("snapshot_for_upgrade") is True or (isinstance(checksums, dict) and _DB_SQL_FILE in checksums) or manifest.get("dump_complete") is True
    if not dump_path.exists():
        if claims_dump:
            raise click.ClickException(
                f"Refusing to restore: this archive describes a database dump ({_DB_SQL_FILE}) but the file is MISSING from {backup_dir}. "
                "A restore from it would report success while restoring nothing. Restore a complete archive written by `farna-control backup` (or the snapshot an upgrade printed)."
            )
        click.echo(
            f"WARNING: no {_DB_SQL_FILE} in this archive and no dump is recorded in its manifest — only the JSON exports will be restored.",
            err=True,
        )
        return
    if not isinstance(checksums, dict) or not isinstance(checksums.get(_DB_SQL_FILE), str):
        raise click.ClickException(f"{_DB_SQL_FILE} is present but {_BACKUP_INFO_FILE} records no checksum for it — this archive cannot be proven intact, so it is refused. Restore a complete archive written by `farna-control backup` (or the snapshot an upgrade printed).")
    if manifest.get("dump_complete") is True:
        try:
            assert_plain_dump_complete(dump_path)
        except RuntimeError as exc:
            raise click.ClickException(f"Refusing to restore: {exc}. The archive is corrupt — restore a complete snapshot.") from exc
        return
    click.echo(
        "WARNING: this archive predates dump-completion attestation — only its checksum can be verified, so a dump truncated before the checksum was taken cannot be detected. Re-take the snapshot with this release if the recovery matters.",
        err=True,
    )


def _safety_dump(opts: _RestoreOptions, raw_url: str) -> None:
    """Pre-restore safety dump of the TARGET, taken before anything is wiped.

    Skipped under --no-safety-dump (the loud explicit opt-out) and when the
    archive carries no database.sql (no cluster wipe is planned, so there is
    nothing to protect) — matching the pre-refactor placement exactly. The
    dump leaves the previous cluster restorable: psql replays the --clean
    dump's DROPs under -1 (single transaction), but a half-download or a
    broken dump must never be the only way back.
    """
    if opts.no_safety_dump or not (opts.backup_dir / _DB_SQL_FILE).exists():
        return
    _take_safety_dump(raw_url, output_dir=opts.safety_dump_dir)


def _restore_cluster(opts: _RestoreOptions, manifest: dict[str, Any], raw_url: str, keys: _RestoreKeys) -> None:
    """Restore the database cluster from the archive.

    Applies the SQL dump via psql, re-inserts the checkpoint_blobs /
    checkpoints / checkpoint_writes JSON exports, re-encrypts credentials
    when the FERNET_KEY has changed (ORDERING locked by contract: the
    ``--previous-fernet-key`` re-encryption runs AFTER the checkpoint JSON
    exports and BEFORE the collation drift check), then checks
    collation-version drift when a database.sql was applied.
    """
    backup_dir = opts.backup_dir
    db_sql = backup_dir / _DB_SQL_FILE
    if db_sql.exists():
        click.echo("Restoring database schema and data via psql...")
        _run_psql(raw_url, db_sql)
        click.echo("  Database restored from SQL dump")
    else:
        click.echo("  No database.sql found — skipping full DB restore")

    blobs_json = backup_dir / _BLOBS_JSON_FILE
    if blobs_json.exists():
        click.echo("Restoring checkpoint_blobs from JSON export...")
        blobs: list[dict[str, Any]] = json.loads(blobs_json.read_text(encoding="utf-8"))
        restored = _restore_checkpoint_blobs_sync(raw_url, blobs)
        click.echo(f"  {restored} checkpoint blob records restored")
    else:
        click.echo("  No checkpoint_blobs.json found — skipping")

    checkpoints_json = backup_dir / _CHECKPOINTS_JSON_FILE
    if checkpoints_json.exists():
        click.echo("Restoring checkpoints from JSON export...")
        checkpoints_data: list[dict[str, Any]] = json.loads(checkpoints_json.read_text(encoding="utf-8"))
        restored_cp = _restore_checkpoints_sync(raw_url, checkpoints_data)
        click.echo(f"  {restored_cp} checkpoint records restored")
    else:
        click.echo("  No checkpoints.json found — skipping")

    cwrites_json = backup_dir / _WRITES_JSON_FILE
    if cwrites_json.exists():
        click.echo("Restoring checkpoint_writes from JSON export...")
        cwrites_data: list[dict[str, Any]] = json.loads(cwrites_json.read_text(encoding="utf-8"))
        restored_cw = _restore_checkpoint_writes_sync(raw_url, cwrites_data)
        click.echo(f"  {restored_cw} checkpoint write records restored")
    else:
        click.echo("  No checkpoint_writes.json found — skipping")

    _re_encrypt_credentials_if_needed(opts, manifest, raw_url, keys)

    if db_sql.exists():
        _check_collation_versions_sync(raw_url)


def _re_encrypt_credentials_if_needed(
    opts: _RestoreOptions,
    manifest: dict[str, Any],
    raw_url: str,
    keys: _RestoreKeys,
) -> None:
    """Re-encrypt archived credentials when the FERNET_KEY changed since backup.

    ORDERING (locked by contract): called AFTER the checkpoint JSON exports
    and BEFORE the collation drift check. A no-op when the archive carries no
    credentials_references.json, and a transcript-only branch when the key is
    unchanged (parity with the pre-refactor command).
    """
    creds_json = opts.backup_dir / _CREDS_JSON_FILE
    current_key_hash = _fernet_key_hash(keys.fernet_key)
    backup_key_hash = manifest.get("fernet_key_hash", "")

    if creds_json.exists() and current_key_hash != backup_key_hash:
        if not opts.previous_fernet_key:
            raise click.ClickException("FERNET_KEY has changed since backup. Provide --previous-fernet-key to re-encrypt credentials.")
        click.echo("Re-encrypting credentials with current FERNET_KEY...")
        creds_data: dict[str, list[dict[str, Any]]] = json.loads(creds_json.read_text(encoding="utf-8"))
        counts = _re_encrypt_credentials_sync(raw_url, creds_data, opts.previous_fernet_key, keys.fernet_key)
        for table, cnt in counts.items():
            click.echo(f"  {cnt} {table} re-encrypted")
    elif current_key_hash == backup_key_hash:
        click.echo("FERNET_KEY unchanged — no credential re-encryption needed")


def _restore_data_dir(opts: _RestoreOptions, manifest: dict[str, Any]) -> None:
    """Adopt the archive's launcher artefacts into the data dir, if requested.

    ORDERING (locked by test): launcher artefacts are adopted only AFTER the
    database restore SUCCEEDS. Adopting first would leave a psql-failure data
    dir carrying the ARCHIVE's credentials against the target's OLD database —
    bricked in both directions (the old launcher no longer has its credentials;
    restarting the new one hits a database that was never restored).
    """
    if opts.data_dir is None:
        return
    _check_archived_state_consistency(opts.backup_dir, manifest)
    adopted = _restore_launcher_files(opts.backup_dir, opts.data_dir)
    if adopted:
        click.echo(f"Adopted launcher artefacts into {opts.data_dir}: {', '.join(adopted)}")


# ── CLI ──────────────────────────────────────────────────────────────────────


@click.group()
def cli() -> None:
    """Farna Control backup and restore operations."""


@cli.command()
@click.option(
    "--db-url",
    default=None,
    help="Maintenance connection URL override (default: DATABASE_ADMIN_URL, else DATABASE_URL from settings)",
)
@click.option(
    "--output-dir",
    "-o",
    default=None,
    type=click.Path(path_type=Path),
    help="Output directory (default: ./modulo-backup-<YYYYMMDD-HHMMSS>)",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=("Launcher data dir to include (and lock): copies state.json, and the TLS keypair when present. Passes the secrets file / conf profile only with --include-secrets."),
)
@click.option(
    "--include-secrets",
    is_flag=True,
    default=False,
    help=("Include the 0600 launcher secrets file and the credential-bearing conf profile in the archive. LOUD WARNING: the archive then carries the generated service passwords."),
)
def backup(db_url: str | None, output_dir: Path | None, data_dir: Path | None, include_secrets: bool) -> None:
    """Create a full backup of the Farna Control database.

    Creates a timestamped directory with a pg_dump SQL file, checkpoint_blobs
    JSON export, credentials references, and a versioned backup-info.json
    manifest. With --data-dir, the launcher state rides along (secrets only
    with --include-secrets; Redis/SAQ queue data is ephemeral and excluded
    by design). Taking a data dir requires its exclusive lock — a running
    launcher refuses with its holder PID.
    """
    raw_url = _resolve_url(db_url, data_dir=data_dir)
    settings = get_settings()
    from farna.launcher.env_safety import scrub_os_environment

    scrub_os_environment()

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    # The data-dir lock is taken BEFORE any output directory is created: a
    # refused backup (holder PID in the message) must not leave an empty
    # collision directory behind — nothing exists yet to clean up.
    lock = None
    if data_dir is not None:
        lock = _acquire_data_lock(data_dir, mode="backup")

    user_specified = False
    if output_dir is not None:
        backup_dir = output_dir
        user_specified = True
        backup_dir.mkdir(parents=True, exist_ok=True)
    else:
        user_specified = False
        suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision for the backup folder  # noqa: S311
        backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        while backup_dir.exists():
            suffix = random.randint(1000, 9999)  # nosec B311 -- not crypto, just avoiding a directory-name collision  # noqa: S311
            backup_dir = Path(f"./modulo-backup-{ts}-{suffix}")
        backup_dir.mkdir(parents=True, exist_ok=False)

    file_checksums: dict[str, str] = {}

    try:
        click.echo(f"Backup directory: {backup_dir}")
        _warn_cloud_sync_output(backup_dir)

        launcher_section: dict[str, Any] = {
            "state_json_included": False,
            "state_mac": None,
            "conf_profile": {"included": False, "note": "no data dir given"},
            "secrets_file": {"included": False, "note": "no data dir given"},
            "tls_files": [],
            "tls_note": ("no data dir given — no TLS material in the archive"),
            "ephemeral": {"redis_saq_queue_data": ("EXCLUDED by design — Redis/SAQ queue data is ephemeral (ADR 031 Decision 6); queued jobs do not survive a backup/restore cycle")},
        }
        launcher_checksums: list[str] = []
        if data_dir is not None:
            # ORDERING (locked by test): last_backup_at is recorded BEFORE the
            # launcher snapshot files are copied. _record_last_backup rewrites
            # state.json (payload + HMAC change); snapshotting first would
            # archive the PRE-rewrite file whose mac then no longer matches
            # the data dir's final on-disk state — backup→restore to the SAME
            # instance would always demand --replace-cluster. Recording first
            # makes the manifest's state_mac match the final state.
            _record_last_backup(data_dir, datetime.now(UTC).isoformat())
            launcher_section, launcher_checksums = _launcher_snapshot_files(data_dir, backup_dir, include_secrets)

        click.echo("Running pg_dump...")
        _run_pg_dump(raw_url, backup_dir / _DB_SQL_FILE)
        file_checksums[_DB_SQL_FILE] = _file_checksum(backup_dir / _DB_SQL_FILE)
        click.echo("  database.sql written")

        click.echo("Exporting checkpoint_blobs...")
        blobs = _export_checkpoint_blobs_sync(raw_url)
        _write_json(backup_dir / _BLOBS_JSON_FILE, blobs)
        file_checksums[_BLOBS_JSON_FILE] = _file_checksum(backup_dir / _BLOBS_JSON_FILE)
        click.echo(f"  {len(blobs)} checkpoint blob records exported")

        click.echo("Exporting checkpoints...")
        checkpoints = _export_checkpoints_sync(raw_url)
        _write_json(backup_dir / _CHECKPOINTS_JSON_FILE, checkpoints)
        file_checksums[_CHECKPOINTS_JSON_FILE] = _file_checksum(backup_dir / _CHECKPOINTS_JSON_FILE)
        click.echo(f"  {len(checkpoints)} checkpoint records exported")

        click.echo("Exporting checkpoint_writes...")
        cwrites = _export_checkpoint_writes_sync(raw_url)
        _write_json(backup_dir / _WRITES_JSON_FILE, cwrites)
        file_checksums[_WRITES_JSON_FILE] = _file_checksum(backup_dir / _WRITES_JSON_FILE)
        click.echo(f"  {len(cwrites)} checkpoint write records exported")

        click.echo("Exporting credentials references...")
        creds = _export_credentials_references_sync(raw_url)
        _write_json(backup_dir / _CREDS_JSON_FILE, creds)
        file_checksums[_CREDS_JSON_FILE] = _file_checksum(backup_dir / _CREDS_JSON_FILE)
        total_creds = sum(len(v) for v in creds.values())
        click.echo(f"  {total_creds} credential records referenced")

        ride_along_checksums = {name: _file_checksum(backup_dir / name) for name in launcher_checksums}
        manifest: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "manifest_version": MANIFEST_VERSION,
            "backup_type": "full",
            "db_version": _get_db_version(raw_url),
            "schema_versions": _get_schema_versions(),
            "fernet_key_hash": _fernet_key_hash(settings.fernet_key),
            "file_checksums": {**file_checksums, **ride_along_checksums},
            "launcher_snapshot": launcher_section,
            # FAR-2667: `_run_pg_dump` proved the completion trailer before
            # this manifest was written, so restore can re-verify the dump is
            # whole instead of trusting the checksum alone.
            "dump_format": "plain",
            "dump_complete": True,
        }
        _write_json(backup_dir / _BACKUP_INFO_FILE, manifest)
        click.echo("  backup-info.json written")

        artifact_names = [
            _BACKUP_INFO_FILE,
            _DB_SQL_FILE,
            _BLOBS_JSON_FILE,
            _CHECKPOINTS_JSON_FILE,
            _WRITES_JSON_FILE,
            _CREDS_JSON_FILE,
            *launcher_checksums,
        ]
        if (backup_dir / _TLS_DIRNAME).is_dir():
            artifact_names.append(_TLS_DIRNAME)
        _lock_artifact_mode(backup_dir, artifact_names, user_specified_dir=user_specified)

        click.echo(f"\nBackup complete: {backup_dir}")
        _print_size(backup_dir)

    except Exception as exc:
        _log.exception("Backup failed")
        if not user_specified and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
            click.echo(f"Cleaned up partial backup directory: {backup_dir}", err=True)
        click.echo(f"Backup failed: {exc}", err=True)
        raise click.ClickException(str(exc)) from exc
    finally:
        if lock is not None:
            lock.release()


@cli.command()
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--db-url",
    default=None,
    help="Maintenance connection URL override (default: DATABASE_ADMIN_URL, else DATABASE_URL from settings)",
)
@click.option(
    "--app-db-url",
    default=None,
    help=("Connection URL of the APPLICATION role whose RLS posture and grants the restore re-applies (default: DATABASE_URL from settings). Never derived from --db-url."),
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt")
@click.option(
    "--previous-fernet-key",
    default=None,
    help="Previous FERNET_KEY if it changed since backup (required for credential re-encryption)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Preview the restore without making any database changes",
)
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=("Launcher data dir to lock and refresh: adopts the archive's launcher artefacts (state.json / secrets.json / conf profile / TLS keypair, where included) and refuses while that data dir's launcher is running."),
)
@click.option(
    "--replace-cluster",
    is_flag=True,
    default=False,
    help=("Intentionally replace a DIFFERENT populated instance with the archive (per-instance identity mismatch otherwise refuses)."),
)
@click.option(
    "--no-safety-dump",
    is_flag=True,
    default=False,
    help=("Skip the automatic pre-restore safety dump of the TARGET. LOUD: without it, a mid-import failure would otherwise abort out of a half-dropped database."),
)
@click.option(
    "--safety-dump-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=("Directory to write the pre-restore safety dump into (default: the current directory). Set it to a persistent path when restore runs in a disposable container."),
)
@click.option(
    "--target-fs",
    type=click.Path(path_type=Path),
    envvar="DB_CAPACITY_PATH",
    default=None,
    help=("A path on the DATABASE's own filesystem, used by the disk pre-flight. Without it (and without --data-dir) the pre-flight measures the archive's filesystem, which may be a different volume. The compose deployment sets DB_CAPACITY_PATH to its database volume."),
)
@click.option(
    "--verify-target-schema",
    is_flag=True,
    default=False,
    help=("Refuse unless the TARGET database records exactly the schema revisions the archive was taken from. The compose recovery path's stand-in for the --data-dir instance-identity guard; --replace-cluster overrides it."),
)
def restore(
    backup_dir: Path,
    db_url: str | None,
    app_db_url: str | None,
    yes: bool,
    previous_fernet_key: str | None,
    dry_run: bool,
    data_dir: Path | None,
    replace_cluster: bool,
    no_safety_dump: bool,
    safety_dump_dir: Path | None,
    target_fs: Path | None,
    verify_target_schema: bool,
) -> None:
    """Restore a Farna Control database from a backup directory.

    Verifies BEFORE it wipes (manifest shape + version, instance identity,
    database-emptiness probe for fresh targets, schema downgrade guard, disk
    pre-flight, file integrity), takes a pre-restore safety dump of the
    target (or --no-safety-dump), restores the database via psql in a single
    transaction, re-inserts checkpoint_blobs from the JSON export,
    re-encrypts credentials if the FERNET_KEY has changed, re-ADOPTS the
    launcher artefacts only after the database restore SUCCEEDS, re-runs the
    promoted bootstrap role-posture check for the APPLICATION role — resolved
    from ``--app-db-url`` or ``DATABASE_URL``, never from ``--db-url`` — and
    checks collation-version drift. A role-posture failure FAILS the restore
    (non-zero exit naming the check), because a restored cluster whose app
    role can still bypass RLS must not be reported as complete. Incompatible
    collation families REFUSE the restore; same-family drift warns and
    proceeds. A corrupt or foreign dump is refused BEFORE the wipe: the
    manifest must checksum ``database.sql``, and an archive that attested
    dump-time completeness is re-verified now. With --dry-run, validates
    integrity and previews every step without touching the database. Against
    a launcher data dir, once validation passes it STOPS a running launcher
    itself, takes the exclusive lock, and leaves the stack stopped: when that
    stop took the bundled PostgreSQL down with it, only that cluster is started
    for the duration of the dump and replay and stopped again afterwards. The
    target and application URLs come from ``--db-url``/``--app-db-url``, an
    exported ``DATABASE_ADMIN_URL``/``DATABASE_URL``, or the data dir's own
    ``config.env`` (the launcher's pinned configuration), in that order — so a
    native operator does not need to export anything. On success it prints the
    exact start command and never auto-restarts. A --dry-run preview or a
    failed validation never stops a running launcher.
    """
    _run_restore(
        _RestoreOptions(
            backup_dir=backup_dir,
            db_url=db_url,
            app_db_url=app_db_url,
            yes=yes,
            previous_fernet_key=previous_fernet_key,
            dry_run=dry_run,
            data_dir=data_dir,
            replace_cluster=replace_cluster,
            no_safety_dump=no_safety_dump,
            safety_dump_dir=safety_dump_dir,
            target_fs=target_fs,
            verify_target_schema=verify_target_schema,
        )
    )


# ---------------------------------------------------------------------------
# farna-control apply (FAR-681)
# ---------------------------------------------------------------------------
# Registered once at import time so the ``farna-control apply`` subcommand is wired
# into the CLI group. Wrapped in an ``_init_once_*`` helper because the
# module-side-effects architecture test forbids bare module-level calls.
def _init_once_register_apply() -> None:
    register_apply(cli)


_init_once_register_apply()
