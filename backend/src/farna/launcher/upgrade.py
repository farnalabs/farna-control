"""Installer-enforced pre-upgrade pg_dump orchestration (FAR-672 / ADR 031 Decision 7).

P1a upgrade flow = rerun the installer. The installer ENFORCES a pre-upgrade
pg_dump before it touches anything version-specific: it calls the OLD
installation's bundled runtime here (``python -m farna.launcher.upgrade``),
which resolves the BUNDLED pg_dump client, dumps the bundled app database to
a versioned snapshot directory inside the data dir, verifies the dump is
non-empty, and prints the snapshot path as its final stdout line.

On ANY dump failure the helper raises :class:`UpgradeError` and the installer
ABORTS — no binary swap happens without a verified snapshot. ``--skip-backup``
in install.sh is the operator's loud, explicit escape hatch for the SECOND
installer run (the dump already succeeded; the stack must stay stopped for
the swap — see the install.sh comment block).

The bundled stack must be REACHABLE while dumping (pg_dump talks to a live
Postgres): run this BEFORE stopping the launcher. The swap phase in
install.sh separately refuses to proceed while the data-dir lock is held
(destructive steps never race a live launcher); :func:`assert_not_held`
implements the same refusal for programmatic callers.

Platform: Linux-first (P1a). The secrets file and the data-dir lock refuse
Windows loudly on their own (TODO(P3) seams); this module degrades nothing
silently.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess  # nosec B404 — the only exec is a fully argv-pinned pg_dump invocation below (never shell=True)
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from farna.launcher import manifest as manifest_module
from farna.launcher.entry import APP_DB_NAME
from farna.launcher.env_safety import scrub_os_environment
from farna.launcher.initdb import _binary
from farna.launcher.manifest import ManifestSecurityError, ReleaseManifest
from farna.launcher.platform import binary_name
from farna.launcher.supervisor import _read_lock_holder

_log = logging.getLogger(__name__)

SNAPSHOT_PREFIX = "pre-upgrade-dump-"
SNAPSHOT_MANIFEST_NAME = "manifest.json"
SNAPSHOT_MANIFEST_VERSION = 1
#: Marker written into a snapshot directory while it is being produced. A
#: directory carrying a LIVE-pid marker is another process's in-flight write and
#: is never deleted; the snapshot lock makes that rare, this makes it safe.
SNAPSHOT_IN_PROGRESS_NAME = ".in-progress"
#: The lock file that serialises snapshot creation and pruning inside one
#: output directory (two concurrent boots must not delete each other's work).
SNAPSHOT_LOCK_NAME = ".snapshot.lock"
# ---- FAR-2667: the snapshot is proven complete at dump time ----------------
# Retention keeps this many VERIFIED snapshots (Hopper, FAR-2667 note item 3).
RETENTION_COUNT = 2
# The disk guard: free bytes at the output filesystem must be at least
# max(SNAPSHOT_MIN_FREE_BYTES, SNAPSHOT_HEADROOM_FACTOR x pg_database_size).
SNAPSHOT_MIN_FREE_BYTES = 2 * 1024**3
SNAPSHOT_HEADROOM_FACTOR = 1.25
# Completeness proof and replay-compatibility rewrite live in
# farna.db.plain_dump — ONE implementation shared with `farna-control backup`.
_GIB = 1024**3
# The restore-side manifest: `farna-control restore` hard-requires backup-info.json
# (shape-validated, checksum-verified). The snapshot carries BOTH manifest
# files so the installer's printed command works verbatim.
_RESTORE_MANIFEST_NAME = "backup-info.json"
_DUMP_NAME = "database.sql"
_PGDATA_DIRNAME = "pgdata"
_PG_VERSION_FILE = "PG_VERSION"
_POSTGRES_HOST = "127.0.0.1"
# APP_DB_NAME (the app database name) is imported from farna.launcher.entry — the
# single definition (FAR-2561). The pre-upgrade dump must target the SAME
# database the launcher creates; a local copy here was one of three independent
# literals that could drift.
_SECRETS_FILENAME = "secrets.json"
_STATE_FILENAME = "state.json"
_DUMP_TIMEOUT_SECONDS = 1800
_PRIVATE_MODE = 0o600
_DIR_PRIVATE_MODE = 0o700
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")

# The launcher-owned public surface (consumed by install.sh via the bundled
# runtime; vulture's dead-code gate special-cases __all__).
__all__ = [
    "RETENTION_COUNT",
    "SNAPSHOT_MANIFEST_NAME",
    "SNAPSHOT_PREFIX",
    "PreUpgradeSnapshot",
    "SnapshotRef",
    "UpgradeError",
    "UpgradeResult",
    "assert_not_held",
    "atomic_symlink_swap",
    "check_no_downgrade",
    "default_install_root",
    "enforce_snapshot_disk_headroom",
    "extract_and_verify_bundle",
    "fetch_release_assets",
    "live_database_revisions",
    "main",
    "migration_pending",
    "perform_upgrade",
    "pre_upgrade_dump",
    "prune_snapshots",
    "prune_versions",
    "repair_current_symlink",
    "required_snapshot_free_bytes",
    "restore_guidance",
    "snapshot_dir_lock",
    "sweep_upgrade_caches",
    "take_upgrade_snapshot",
    "verify_dump_complete",
    "write_upgrade_marker",
]


class UpgradeError(RuntimeError):
    """Raised when the enforced pre-upgrade dump cannot be produced safely."""


def _decredential_url(raw_url: str) -> tuple[str, str | None]:
    """Split the password out of the dump URL (never bare in /proc cmdline).

    Delegates to :func:`farna.db.url_utils.split_postgres_credentials` — the ONE
    implementation the backup CLI and the psycopg connect path share, so a
    password containing ``#``, ``?``, ``/`` or ``%`` cannot reparse to a
    different target in one producer and not the other.
    """
    from farna.db.url_utils import split_postgres_credentials

    return split_postgres_credentials(raw_url)


@dataclass(frozen=True)
class SnapshotRef:
    """One VERIFIED pre-upgrade snapshot (the installer aborts without one).

    Returned by :func:`take_upgrade_snapshot` — the single callable both the
    native launcher and the container entrypoint use (FAR-2667).
    """

    directory: Path
    dump_path: Path
    checksum: str
    bytes: int
    schema_heads: list[str]
    created_at: str


# The pre-FAR-2667 name; kept as the same type so every existing caller and
# the installer's printed command keep working unchanged.
PreUpgradeSnapshot = SnapshotRef


def _pid_alive(pid: int) -> bool:
    # /proc exists only on Linux (P1a); elsewhere the holder reads as gone,
    # and the Windows branch of the caller refuses before reaching here.
    return Path(f"/proc/{pid}").exists()


def assert_not_held(data_dir: Path) -> None:
    """Refuse destructive upgrade steps while the data dir is locked.

    Reads the ``<datadir>.lock`` sibling holder record and, on POSIX, checks
    the holder's /proc entry: a LIVE holder refuses (named by PID + mode);
    a dead holder's record is stale — the kernel released the flock when the
    process died — and is only logged. Windows cannot verify flock state
    until its P3 seam: refuse loudly instead of proceeding destructively.

    NOTE: the DUMP wants the bundled stack running (pg_dump talks to live
    Postgres), so the launcher legitimately holds the lock while dumping.
    This refusal is for the SWAP phase, once the snapshot is safe on disk.
    """
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + ".lock"))
    if holder is None:
        return
    if sys.platform == "win32":
        # Deferred P3: holder liveness must be verified with the Windows lock
        # primitive before any destructive step can run unguarded.
        raise UpgradeError(f"The data-dir lock record exists (holder PID {holder.pid}, mode {holder.mode!r}) and holder liveness cannot be verified on Windows yet (TODO(P3)). Stop the launcher, clear the stale lock record only if the holder process is confirmed dead, then retry.")
    if _pid_alive(holder.pid):
        raise UpgradeError(f"Refusing: data dir {data_dir} is locked by another launcher (holder PID {holder.pid}, mode {holder.mode!r}). Stop that launcher first ('farna-control stop'), then re-run — the snapshot survives; pass --skip-backup on the re-run.")
    _log.info("upgrade.stale_lock_record_ignored holder_pid=%s (process is gone)", holder.pid)


def _adopt_legacy_owner_for_dump(*, port: int, password: str) -> None:
    """Adopt a pre-FAR-2570 cluster owner before the pre-upgrade dump connects.

    A shipped bundle before FAR-2570 initialised the cluster with
    ``initdb -U modulo``; the dump must adopt (rename) that owner before
    connecting as the canonical role. Idempotent and a no-op on
    fresh/already-adopted installs. An adoption REFUSAL (:class:`InitdbError` —
    neither owner role accepts the credential, or both names exist and only the
    legacy one does) IS the diagnosis the dump would otherwise report generically
    as an authentication failure, so it aborts the upgrade with that text. Any
    other failure (an unreachable cluster) stays best effort: pg_dump below
    reports it with its own actionable error.
    """
    import asyncio

    from farna.launcher.initdb import InitdbError, adopt_legacy_owner_role

    try:
        asyncio.run(adopt_legacy_owner_role(host=_POSTGRES_HOST, port=port, password=password))
    except InitdbError as exc:
        raise UpgradeError(f"the bundled cluster's owner role cannot be resolved for the pre-upgrade dump — upgrade ABORTED, no binary swap was made: {exc}") from exc
    except Exception as exc:
        _log.warning("upgrade.owner_role_adoption_skipped error=%s", exc)


def _owner_database_url(data_dir: Path) -> tuple[str, Any]:
    """The bundled cluster's OWNER-role URL from the data dir (HMAC-verified).

    Refuses (``UpgradeError``) when the data dir is not bootstrapped. The
    password is URL-quoted: libpq reparses the URL, so a generated password
    containing ``#``, ``?``, ``/``, ``%`` or ``@`` would otherwise reparse to a
    different target — a wrong database (or a wrong password) in exactly the
    step that is supposed to protect the data.

    Run as the cluster OWNER (initdb's bootstrap role), never the application
    role: farna_control_app is NOBYPASSRLS, so a dump (or a revision probe)
    through it would read only what its session can see and could produce a
    silently incomplete picture (FAR-2570).
    """
    from urllib.parse import quote

    from farna.launcher.secrets_file import SecretsFileError, _load_existing
    from farna.launcher.state import load_state

    state_path = data_dir / _STATE_FILENAME
    if not state_path.exists():
        raise UpgradeError(f"No state.json at {state_path} — the data dir is not bootstrapped; nothing to upgrade")
    pgdata = data_dir / _PGDATA_DIRNAME
    if not (pgdata / _PG_VERSION_FILE).exists():
        raise UpgradeError(f"No initialised bundled cluster at {pgdata} — nothing to dump; upgrade aborted")

    secrets_path = data_dir / _SECRETS_FILENAME
    if not secrets_path.exists():
        # Load-ONLY: a dump helper that could GENERATE credentials would, on
        # a missing file, silently mint a fresh secrets.json and then fail on
        # state.json HMAC verification — leaving an orphan secrets file that
        # bricks the next boot. Refuse with the remedial message instead.
        raise UpgradeError(
            f"Refusing to dump: the secrets file {secrets_path} is MISSING — the pre-upgrade dump "
            "must never generate credentials. If this data dir was restored from an archive, use the "
            "archive's include-secrets bundle; otherwise the data dir is not bootstrapped. "
            "Do NOT create a secrets.json by hand."
        )
    try:
        secrets = _load_existing(secrets_path)
    except SecretsFileError as exc:
        raise UpgradeError(f"secrets file cannot be READ for the pre-upgrade dump (never generated here): {exc}") from exc
    try:
        state = load_state(state_path, secrets.state_hmac_key)
    except Exception as exc:
        raise UpgradeError(f"state.json at {state_path} cannot be verified: {exc}") from exc

    # Adopt a pre-FAR-2570 cluster before connecting as the canonical owner
    # role; the helper owns the refusal/attestation semantics.
    _adopt_legacy_owner_for_dump(port=state.postgres_port, password=secrets.postgres_password)

    from farna.launcher.initdb import BOOTSTRAP_USERNAME

    quoted_user = quote(BOOTSTRAP_USERNAME, safe="")
    quoted_password = quote(secrets.postgres_password, safe="")
    url = f"postgresql://{quoted_user}:{quoted_password}@{_POSTGRES_HOST}:{state.postgres_port}/{APP_DB_NAME}"
    return url, state


def live_database_revisions(data_dir: Path) -> list[str]:
    """The DATABASE's recorded revisions, read from the live bundled cluster.

    This is the DB side of the pre-flight checks on the ``--skip-backup`` path,
    which has no snapshot to read. Probing the DATABASE (rather than the old
    bundle's script heads) is the difference between "the revision the code
    ships" and "the revision that is actually applied" — a database already
    migrated ahead is only detectable by asking it.
    """
    url, _state = _owner_database_url(data_dir)
    return _database_revisions(url)


def pre_upgrade_dump(
    data_dir: Path,
    *,
    bin_dir: Path | None = None,
    output_dir: Path | None = None,
    release: str | None = None,
    target_heads: list[str] | None = None,
    target_bundle: Path | None = None,
) -> PreUpgradeSnapshot | None:
    """Perform and verify the NATIVE pre-upgrade pg_dump (FAR-672 / FAR-2667).

    Resolves the bundled cluster's owner-role URL from the data dir's
    HMAC-verified state.json + secrets.json, then hands off to
    :func:`take_upgrade_snapshot` — the SAME callable the container
    entrypoint uses, so the native and container paths cannot drift.

    THE PENDING CHECK LIVES HERE, not in the callers. A snapshot is owed only
    when the boot that follows can actually change the schema; taking one on
    every installer re-run rotated the recovery points away for no gain, and
    leaving the decision to each caller is how one of them (``install.sh``)
    ended up dumping unconditionally. Returns ``None`` when no migration is
    pending; every caller must report that skip.

    The comparison is against the TARGET release's revisions — ``target_heads``
    when the caller knows them, else the migration tree under *target_bundle*
    (the extracted NEW bundle, which is what ``install.sh`` has), else this
    runtime's own code. A named target whose heads cannot be read reads as
    "undecidable", which takes the snapshot: an unproven "nothing to do" must
    never cost the recovery point.

    Produces ``<data-dir>/pre-upgrade-dump-[<release>-]<timestamp>/`` beside
    the cluster (never inside ``pgdata/``), containing the pg_dump SQL file,
    a versioned snapshot manifest and the restore-compatible
    ``backup-info.json``. A dump that cannot be PROVEN complete (no
    pg_dump completion trailer) is swept and the upgrade aborts.
    """
    from farna.launcher.env_safety import scrub_os_environment

    # FIRST ACTION — the dump must never inherit a launcher-hostile PG*
    # variable (a stale PGPASSWORD/PGHOST would silently redirect it to a
    # foreign endpoint: same contract as the native boot, ADR 031 Decision 2).
    scrub_os_environment()

    url_with_password, state = _owner_database_url(data_dir)

    if _pending_for_target(url_with_password, target_heads=target_heads, target_bundle=target_bundle) is False:
        _log.info("upgrade.pre_upgrade_dump_skipped (no migration pending) data_dir=%s", data_dir)
        return None

    # `parent` is the operator-chosen data/snapshot directory, supplied by the
    # bundled installer via the local `farna-control upgrade --data-dir` CLI -- never an
    # HTTP request or agent input. The block above has already refused unless the
    # directory holds a bootstrapped state.json (HMAC-verified against the
    # secrets file) plus a secrets.json, so the path is trusted by construction,
    # not attacker-arbitrary, and no privilege boundary is crossed (the process
    # runs as the same operator that chose the path).
    parent = output_dir if output_dir is not None else data_dir
    return take_upgrade_snapshot(
        url_with_password,
        parent,
        release=release,
        bin_dir=bin_dir,
        extra_manifest={"postgres_port": state.postgres_port},
    )


def _pending_for_target(
    url: str,
    *,
    target_heads: list[str] | None,
    target_bundle: Path | None,
) -> bool | None:
    """Is a migration pending against the CODE THAT IS ABOUT TO BOOT?

    Never against the helper's own code: the installer runs the OLD
    installation's helper against the NEW bundle, and the old code's heads are
    exactly the revisions the live database already records — comparing those
    would report "nothing pending" for every real upgrade and silently drop
    the recovery point.
    """
    if target_heads is not None:
        return migration_pending(url, code_heads=list(target_heads))
    if target_bundle is not None:
        heads = sorted(_bundle_alembic_heads(target_bundle))
        return migration_pending(url, code_heads=heads) if heads else None
    return migration_pending(url)


# ---------------------------------------------------------------------------
# FAR-2667 — the ONE snapshot callable both deployment paths use
# ---------------------------------------------------------------------------


def _gib(value: float) -> str:
    """Human GB figure (one decimal) for the disk-guard message."""
    return f"{value / _GIB:.1f}"


def required_snapshot_free_bytes(database_bytes: int) -> int:
    """Free bytes the snapshot needs: max(2 GiB, 1.25 x database size).

    Hopper's FAR-2667 rule. The 1.25 x term is the dump itself plus the
    WAL/temp headroom the write costs; the 2 GiB floor keeps a tiny database
    from producing an unusably small margin.
    """
    return max(SNAPSHOT_MIN_FREE_BYTES, math.ceil(database_bytes * SNAPSHOT_HEADROOM_FACTOR))


def _database_size_bytes(db_url: str, *, timeout_seconds: int = 15) -> int:
    """``pg_database_size(current_database())`` for the dump target.

    Fails CLOSED: an unmeasurable database refuses the upgrade rather than
    skipping the disk guard (a guard that silently passes is not a guard).

    The URL is normalised to libpq and its password is moved OUT of the DSN, so
    a connection failure's error text (which quotes the DSN) can never carry
    the superuser password — and a ``postgresql+asyncpg://`` URL, which libpq
    rejects, works here.
    """
    import psycopg

    from farna.db.url_utils import psycopg_connect_args

    try:
        dsn, password = psycopg_connect_args(db_url)
        with psycopg.connect(dsn, connect_timeout=timeout_seconds, password=password) as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            row = cur.fetchone()
    except Exception as exc:
        raise UpgradeError(f"Refusing to upgrade: cannot measure the database size for the snapshot disk guard ({exc}). Nothing has been changed.") from exc
    if not row or not isinstance(row[0], (int, float)):
        raise UpgradeError("Refusing to upgrade: the database size query returned no usable figure, so the snapshot disk guard cannot run. Nothing has been changed.")
    return int(row[0])


def _database_revisions(db_url: str, *, timeout_seconds: int = 15) -> list[str]:
    """The revisions recorded in the target database's ``alembic_version``.

    This is the DATABASE side of the pre-upgrade decision: retained snapshots
    are compared against it, and it is what the snapshot records as the schema
    it captured (the dump's contents, not the code's heads). An absent table is
    a fresh database — ``[]`` — while any read failure reads as ``["unknown"]``
    so the caller fails closed instead of guessing.
    """
    import psycopg

    from farna.db.url_utils import psycopg_connect_args

    try:
        dsn, password = psycopg_connect_args(db_url)
        with psycopg.connect(dsn, connect_timeout=timeout_seconds, password=password) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            present = cur.fetchone()
            if not present or not present[0]:
                return []
            cur.execute("SELECT version_num FROM alembic_version")
            rows = cur.fetchall()
    except Exception as exc:
        _log.warning("upgrade.database_revisions_unreadable error=%s", exc)
        return ["unknown"]
    return sorted({str(row[0]) for row in rows if row and row[0]})


def _nearest_existing_path(path: Path) -> Path:
    """*path* if it exists, else its nearest existing ancestor (for statfs)."""
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def enforce_snapshot_disk_headroom(db_url: str, output_dir: Path, *, current_heads: list[str] | None = None) -> int:
    """Refuse the upgrade unless the snapshot's filesystem has room.

    Requirement: free bytes at the OUTPUT filesystem >= max(2 GiB,
    1.25 x pg_database_size(current_database())) PLUS the bytes of every
    existing snapshot retention will keep. Without that last term the guard can
    pass and the write still hits ENOSPC, because retention keeps the two newest
    snapshots plus the one recording the database's current schema (a bounded
    set — see :func:`_retention_keep_set`). Returns the required byte count when
    the guard passes. The message names the required space, the available
    space, the shortfall and that nothing has been changed.
    """
    database_bytes = _database_size_bytes(db_url)
    retained = _retained_snapshot_bytes(output_dir, current_heads)
    required = required_snapshot_free_bytes(database_bytes) + retained
    probe = _nearest_existing_path(output_dir)
    try:
        free = shutil.disk_usage(str(probe)).free
    except OSError as exc:
        raise UpgradeError(f"Refusing to upgrade: cannot measure free space at {output_dir} ({exc}). Nothing has been changed.") from exc
    if free < required:
        shortfall = required - free
        retained_note = f" ({_gib(retained)} GB of it is snapshots retention keeps)" if retained else ""
        raise UpgradeError(f"Refusing to upgrade: the snapshot needs about {_gib(required)} GB free at {output_dir}{retained_note}; {_gib(free)} GB is available. Free {_gib(shortfall)} GB and re-run. Nothing has been changed.")
    return required


def verify_dump_complete(dump_path: Path) -> int:
    """Prove a plain-format pg_dump finished; return its size in bytes.

    A truncated plain SQL dump replays as a shorter but syntactically valid
    script, so "non-empty" is not proof. pg_dump writes its completion trailer
    LAST, so its presence is the dump-time completeness proof (the -Fc
    equivalent is ``pg_restore --list``). Delegates to
    :mod:`farna.db.plain_dump` so the container/backup path cannot disagree.
    """
    from farna.db import plain_dump

    try:
        return plain_dump.assert_complete(dump_path)
    except ValueError as exc:
        raise UpgradeError(str(exc)) from exc


def _snapshot_created_at(directory: Path) -> str:
    """Best-effort creation stamp for retention ordering (manifest first)."""
    try:
        manifest = json.loads((directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    created = manifest.get("created_at") if isinstance(manifest, dict) else None
    if isinstance(created, str) and created:
        return created
    try:
        return datetime.fromtimestamp(directory.stat().st_mtime, UTC).isoformat()
    except OSError:
        return ""


def snapshot_is_intact(directory: Path) -> bool:
    """Is *directory* a usable snapshot — structurally AND by checksum?

    Retention DELETES on this answer, so it must be at least as strict as the
    restore it is protecting: a snapshot whose dump has been corrupted must be
    the one that is pruned, not the good older one beside it. The check is
    therefore: both manifests parse and describe this directory's own dump,
    they agree on its size and sha256, and the dump still ends with pg_dump's
    completion trailer.

    The dump is re-hashed (streamed, in 1 MiB chunks) — one sequential read of
    the file just written, once per upgrade, which is the cost of not keeping a
    corrupt recovery point.
    """
    if not (directory / SNAPSHOT_MANIFEST_NAME).is_file():
        return False
    if not (directory / _RESTORE_MANIFEST_NAME).is_file():
        return False
    try:
        manifest = json.loads((directory / SNAPSHOT_MANIFEST_NAME).read_text(encoding="utf-8"))
        restore_manifest = json.loads((directory / _RESTORE_MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(manifest, dict) or not isinstance(restore_manifest, dict):
        return False
    if manifest.get("snapshot_for_upgrade") is not True:
        return False
    dump_name = manifest.get("dump_name")
    if not isinstance(dump_name, str) or not dump_name:
        return False
    checksums = restore_manifest.get("file_checksums")
    if not isinstance(checksums, dict) or not isinstance(checksums.get(dump_name), str):
        return False
    expected_checksum = checksums[dump_name]
    if isinstance(manifest.get("dump_sha256"), str) and manifest["dump_sha256"] != expected_checksum:
        return False
    dump_path = directory / dump_name
    if not dump_path.is_file():
        return False
    try:
        if dump_path.stat().st_size != manifest.get("dump_bytes"):
            return False
        verify_dump_complete(dump_path)
        if _sha256_file(dump_path) != expected_checksum:
            _log.warning("upgrade.snapshot_checksum_mismatch dir=%s", directory)
            return False
    except (OSError, UpgradeError):
        return False
    return True


def snapshot_in_progress(directory: Path) -> bool:
    """True when another LIVE process is still writing *directory*.

    The snapshot lock already serialises writers, so this is the secondary
    guard for a process that died between creating the directory and taking the
    lock, or for an operator running outside the lock: a marker naming a live
    pid means the directory is in flight and prune must leave it alone.
    """
    marker = directory / SNAPSHOT_IN_PROGRESS_NAME
    if not marker.is_file():
        return False
    try:
        owner = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return owner.isdigit() and _pid_alive(int(owner))


@contextlib.contextmanager
def snapshot_dir_lock(output_dir: Path) -> Any:
    """Serialise snapshot creation and pruning inside *output_dir*.

    Two concurrent boots (or a boot racing an operator's snapshot) must never
    prune each other's in-flight directory. The lock is an advisory exclusive
    lock on ``<output_dir>/.snapshot.lock``: ``flock`` on POSIX, ``msvcrt`` on
    Windows, both released by the kernel when the holder dies — so a crashed
    snapshot never wedges the next boot.
    """
    lock_path = output_dir / SNAPSHOT_LOCK_NAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, _PRIVATE_MODE)
    try:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - exercised on Windows CI/dev only
            _win_lock(fd, unlock=False)
        yield
    finally:
        with contextlib.suppress(OSError):
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover
                _win_lock(fd, unlock=True)
        os.close(fd)


def _win_lock(fd: int, *, unlock: bool) -> None:  # pragma: no cover - Windows only
    """Blocking 1-byte msvcrt lock on *fd* (the Windows half of the snapshot lock)."""
    import msvcrt
    import time as _time

    if unlock:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
            return
        except OSError:
            _time.sleep(0.1)


def _recorded_heads(directory: Path) -> list[str]:
    """The schema heads a snapshot recorded (restore manifest first)."""
    for name in (_RESTORE_MANIFEST_NAME, SNAPSHOT_MANIFEST_NAME):
        path = directory / name
        if not path.is_file():
            continue
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        versions = body.get("schema_versions") if isinstance(body, dict) else None
        if isinstance(versions, list) and versions:
            return [str(entry) for entry in versions]
    return []


def _records_schema_state(heads: list[str], current_heads: list[str] | None) -> bool:
    """Does a snapshot's recorded schema EQUAL the database's current one?

    Unknown on either side is NOT a match: a snapshot whose schema state cannot
    be compared cannot be proven to be the recovery point this database
    depends on, and a recovery path that guesses is worse than one that
    refuses.
    """
    if not current_heads or not heads:
        return False
    if "unknown" in heads or "unknown" in current_heads:
        return False
    return set(heads) == set(current_heads)


def _snapshot_dirs(output_dir: Path) -> list[Path]:
    """Every snapshot directory, newest first."""
    if not output_dir.is_dir():
        return []
    return sorted(
        (entry for entry in output_dir.iterdir() if entry.is_dir() and entry.name.startswith(SNAPSHOT_PREFIX) and not entry.is_symlink()),
        key=_snapshot_created_at,
        reverse=True,
    )


def _snapshot_bytes(directory: Path) -> int:
    total = 0
    for entry in directory.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _retention_keep_set(
    intact_newest_first: list[Path],
    *,
    retain: int,
    current_heads: list[str] | None,
    protect: Path | None,
) -> set[Path]:
    """The snapshots retention keeps: two verified snapshots + the recovery point.

    The bounded contract (FAR-2667):

    * the newest *retain* INTACT snapshots — the interchangeable recent recovery
      points;
    * plus *protect*, the snapshot just written (never deleted);
    * plus the newest intact snapshot recording EXACTLY the database's current
      revision(s) — the recovery point for a schema that has not moved since
      the snapshot (a migration that failed before applying, or a rollback).
      Without it an operator could hold two newer snapshots of a later schema
      and no way back to the state this database is actually in.

    Everything else is superseded: an older-head snapshot is strictly worse
    than the newest snapshot that already contains its data, and keeping every
    generation is what let snapshots accumulate until the disk guard refused
    every upgrade. At most ``retain + 1`` directories survive.
    """
    keep: set[Path] = {entry.resolve() for entry in intact_newest_first[:retain]}
    if protect is not None:
        keep.add(protect.resolve())
    if current_heads:
        for entry in intact_newest_first:
            if _records_schema_state(_recorded_heads(entry), current_heads):
                keep.add(entry.resolve())
                break
    return keep


def _intact_newest_first(output_dir: Path) -> list[Path]:
    """Every INTACT snapshot directory, newest first (checksum-verified)."""
    return [entry for entry in _snapshot_dirs(output_dir) if snapshot_is_intact(entry)]


def _retained_snapshot_bytes(output_dir: Path, current_heads: list[str] | None) -> int:
    """Bytes of existing snapshots retention will KEEP (they need disk too)."""
    try:
        intact = _intact_newest_first(output_dir)
    except OSError:
        return 0
    keep = _retention_keep_set(intact, retain=RETENTION_COUNT, current_heads=current_heads, protect=None)
    return sum(_snapshot_bytes(entry) for entry in intact if entry.resolve() in keep)


def prune_snapshots(
    output_dir: Path,
    *,
    retain: int = RETENTION_COUNT,
    protect: Path | None = None,
    current_heads: list[str] | None = None,
    _lock_held: bool = False,
) -> list[str]:
    """Keep the recovery points that matter; delete only the interchangeable rest.

    Ordering contract (Hopper, FAR-2667 item 3): the caller runs this only
    AFTER the new snapshot has verified, so a run can never delete a recovery
    point before its replacement exists. *protect* is the snapshot just written
    and is never deleted.

    Keep rules (see :func:`_retention_keep_set`): the two newest intact
    snapshots, plus the newest one recording the database's CURRENT schema —
    bounded at ``retain + 1`` directories.

    * a directory another LIVE process is still writing is never touched;
    * anything left that is not intact (partial write, interrupted run, corrupt
      checksum) is debris and is swept;
    * everything else is a superseded generation and is deleted.

    Concurrent boots serialise on :func:`snapshot_dir_lock`. ``_lock_held``
    lets :func:`take_upgrade_snapshot` prune under the lock it already holds.
    """
    if not output_dir.is_dir():
        return []
    if _lock_held:
        return _prune_snapshots_locked(output_dir, retain=retain, protect=protect, current_heads=current_heads)
    with snapshot_dir_lock(output_dir):
        return _prune_snapshots_locked(output_dir, retain=retain, protect=protect, current_heads=current_heads)


def _prune_snapshots_locked(
    output_dir: Path,
    *,
    retain: int,
    protect: Path | None,
    current_heads: list[str] | None,
) -> list[str]:
    candidates = _snapshot_dirs(output_dir)
    # Verify each candidate ONCE: `snapshot_is_intact` re-hashes the dump, and
    # retention must not pay that twice per directory.
    intact = {entry: snapshot_is_intact(entry) for entry in candidates}
    keep = _retention_keep_set([entry for entry in candidates if intact[entry]], retain=retain, current_heads=current_heads, protect=protect)
    pruned: list[str] = []
    for entry in candidates:
        if entry.resolve() in keep:
            continue
        if snapshot_in_progress(entry):
            _log.info("upgrade.snapshot_kept_in_flight dir=%s", entry)
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned.append(entry.name)
        _log.info("upgrade.snapshot_pruned%s dir=%s", "" if intact[entry] else "_debris", entry)
    return pruned


def take_upgrade_snapshot(
    db_url: str,
    output_dir: Path,
    *,
    release: str | None = None,
    bin_dir: Path | None = None,
    retain: int = RETENTION_COUNT,
    extra_manifest: dict[str, Any] | None = None,
) -> SnapshotRef:
    """The ONE pre-upgrade snapshot callable (native launcher + container).

    1. Serialise on the snapshot lock (concurrent boots never prune each
       other's in-flight work).
    2. Refuse unless the output filesystem has max(2 GiB, 1.25 x db) free PLUS
       the bytes of the snapshots retention will keep.
    3. ``pg_dump --format=plain --clean --if-exists`` into
       ``<output_dir>/pre-upgrade-dump-[<release>-]<UTC-ts>/database.sql``, via
       the shared :func:`farna.db.plain_dump.dump_plain_sql` pipeline.
    4. Prove the dump complete (pg_dump's final completion trailer line),
       checksum it, record the DATABASE's revisions in both manifests.
    5. Prune: keep the newest *retain* intact snapshots plus the newest one
       recording the database's current schema (never the new one) — a bounded
       set, so successive upgrades cannot accumulate snapshots until the disk
       guard refuses every one of them.

    The directory must never sit inside the Postgres cluster: the callers
    pass the data dir (sibling of ``pgdata/``) or the ``/backups`` mount.

    Raises :class:`UpgradeError` on any failure — including an unusable output
    directory (whose message names the ownership fix, never a traceback); a
    failed dump leaves no directory behind, and the caller aborts the upgrade.
    """
    import shutil

    from farna.db import plain_dump

    if release is not None:
        # The token is interpolated into a DIRECTORY NAME. A value carrying a
        # separator or ".." would write outside the snapshot root, where prune
        # never looks: validate it as a single safe segment instead.
        release = _validate_version_segment(release)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)  # NOSONAR pythonsecurity:S8707 — operator-chosen snapshot dir
    except OSError as exc:
        raise UpgradeError(_output_dir_error(output_dir, exc)) from exc
    with snapshot_dir_lock(output_dir):
        current_heads = _database_revisions(db_url)
        enforce_snapshot_disk_headroom(db_url, output_dir, current_heads=current_heads)

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        stamp_prefix = f"{SNAPSHOT_PREFIX}{release}-" if release else SNAPSHOT_PREFIX
        snapshot_dir = output_dir / f"{stamp_prefix}{stamp}"
        counter = 0
        while snapshot_dir.exists():
            counter += 1
            snapshot_dir = output_dir / f"{stamp_prefix}{stamp}-{counter}"
        try:
            snapshot_dir.mkdir(parents=True, exist_ok=False)
            (snapshot_dir / SNAPSHOT_IN_PROGRESS_NAME).write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            raise UpgradeError(_output_dir_error(output_dir, exc)) from exc

        dump_path = snapshot_dir / _DUMP_NAME
        try:
            try:
                size = plain_dump.dump_plain_sql(db_url, dump_path, pg_dump=_resolve_pg_dump(bin_dir))
            except (plain_dump.DumpError, ValueError) as exc:
                raise UpgradeError(f"pre-upgrade dump FAILED — upgrade ABORTED, no binary swap was made: {exc}") from exc
            except UpgradeError:
                raise
            except OSError as exc:
                raise UpgradeError(_output_dir_error(snapshot_dir, exc)) from exc

            checksum = _sha256_file(dump_path)
            created_at = datetime.now(UTC).isoformat()
            schema_heads = current_heads or ["unknown"]
            manifest: dict[str, Any] = {
                "manifest_version": SNAPSHOT_MANIFEST_VERSION,
                "snapshot_for_upgrade": True,
                "dump_complete": True,
                "dump_format": "plain",
                "created_at": created_at,
                "dump_name": _DUMP_NAME,
                "dump_bytes": size,
                "dump_sha256": checksum,
                "schema_heads": schema_heads,
            }
            if extra_manifest:
                manifest.update(extra_manifest)
            (snapshot_dir / SNAPSHOT_MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            _write_restore_manifest(snapshot_dir, created_at=created_at, checksum=checksum, size=size, schema_heads=schema_heads)
            if os.name == "posix":
                # Deferred P3: Windows ACL hardening (chmod is a silent no-op
                # there). DIRECTORIES get 0700 — the e(xecute) bit is the
                # DIRECTORY search bit: at 0600 the snapshot dir is unsearchable
                # and even the dump (written moments earlier) can no longer be
                # RESOLVED to read it.
                try:
                    snapshot_dir.chmod(_DIR_PRIVATE_MODE)
                    for entry in (dump_path, snapshot_dir / SNAPSHOT_MANIFEST_NAME, snapshot_dir / _RESTORE_MANIFEST_NAME):
                        entry.chmod(_PRIVATE_MODE)
                except OSError as exc:
                    _log.warning("upgrade.snapshot_mode_failed path=%s error=%s", snapshot_dir, exc)
        except BaseException:
            # A failed dump never leaves a half-written snapshot masquerading
            # as one, and the caller never sees a directory to trust. Retention
            # is NOT run here: nothing has been verified, so nothing may be
            # deleted.
            shutil.rmtree(snapshot_dir, ignore_errors=True)
            raise
        (snapshot_dir / SNAPSHOT_IN_PROGRESS_NAME).unlink(missing_ok=True)

        # ORDERING (Hopper, FAR-2667 item 3): dump -> verify -> prune. Reached
        # only once the new snapshot is verified on disk.
        pruned = prune_snapshots(output_dir, retain=retain, protect=snapshot_dir, current_heads=current_heads, _lock_held=True)
        if pruned:
            _log.info("upgrade.snapshots_pruned names=%s", ",".join(pruned))

    snapshot = SnapshotRef(
        directory=snapshot_dir,
        dump_path=dump_path,
        checksum=checksum,
        bytes=size,
        schema_heads=schema_heads,
        created_at=created_at,
    )
    _log.info("upgrade.snapshot_created dir=%s bytes=%s pruned=%s", snapshot_dir, size, len(pruned))
    return snapshot


def _output_dir_error(path: Path, exc: OSError) -> str:
    """The actionable abort text for an unusable snapshot/output directory.

    A bare ``PermissionError`` traceback is exactly the failure the container
    path must never produce: the operator needs to know WHICH directory, WHICH
    uid, and WHAT to run. The compose deployment pre-creates and chowns
    ``/backups`` in the image and mounts it as a named volume for this reason;
    an operator-supplied bind mount must match the container user.
    """
    uid = getattr(os, "getuid", lambda: "?")()
    return (
        f"the snapshot directory {path} is not writable by this process (uid {uid}): {exc}. "
        "The container writes snapshots as its own unprivileged user, so the directory must be writable by it — "
        "use the compose deployment's snapshot volume, or pre-create a bind-mounted directory owned by the container user "
        f"(chown -R <container-uid>:<container-gid> {path}). Nothing has been changed."
    )


def _database_is_pristine(db_url: str, *, timeout_seconds: int = 15) -> bool:
    """True only for a database with NO application tables (a first install).

    A fresh ``docker compose up`` must not demand snapshot headroom or write
    a snapshot of an empty database. Ambiguity (any table present, or the
    probe failing) reads as NOT pristine, so an upgrade always takes a
    snapshot.
    """
    import psycopg

    from farna.db.url_utils import psycopg_connect_args

    try:
        dsn, password = psycopg_connect_args(db_url)
        with psycopg.connect(dsn, connect_timeout=timeout_seconds, password=password) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
            row = cur.fetchone()
    except Exception as exc:
        _log.warning("upgrade.pristine_probe_failed error=%s", exc)
        return False
    if not row or not isinstance(row[0], int):
        return False
    return int(row[0]) == 0


def _resolve_pg_bin_dir(bin_dir: Path | None) -> Path:
    """Resolve the bundled pg/ binaries directory (param > env > install default)."""
    if bin_dir is not None:
        return bin_dir
    from_env = os.environ.get("FARNA_CONTROL_BUNDLED_BIN_DIR")
    if from_env:
        return Path(from_env)
    return Path(sys.prefix) / "bundled" / "bin"


def _resolve_pg_dump(bin_dir: Path | None) -> str:
    """Resolve the pg_dump executable both deployment paths can reach.

    Order: an EXPLICIT bundled dir (param, then ``FARNA_CONTROL_BUNDLED_BIN_DIR``)
    — the native bundle always provides one — then the bundle's own
    ``<prefix>/bundled/bin``, then ``pg_dump`` on PATH. The container image
    installs the PostgreSQL client tools on PATH and ships no ``bundled/``
    tree, so without the PATH step the compose path could never take a
    snapshot. An explicitly given directory that lacks pg_dump is NOT a silent
    PATH fallback: naming a directory is a statement of where the binary is.
    """
    explicit = bin_dir
    if explicit is None:
        from_env = os.environ.get("FARNA_CONTROL_BUNDLED_BIN_DIR")
        explicit = Path(from_env) if from_env else None
    if explicit is not None:
        return _binary(explicit, "pg_dump")
    bundled = _resolve_pg_bin_dir(None)
    if (bundled / binary_name("pg_dump")).exists():
        return _binary(bundled, "pg_dump")
    on_path = shutil.which("pg_dump")
    if on_path is None:
        raise UpgradeError(f"no pg_dump found: neither the bundled {bundled / binary_name('pg_dump')} nor pg_dump on PATH. Install the PostgreSQL client tools (or run from a bundle that ships them) — the upgrade is refused because it cannot take a snapshot.")
    return on_path


def _alembic_heads() -> list[str]:
    """The OLD runtime's current alembic head(s) (unknown → placeholder)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_dir = Path(__file__).resolve().parents[3]  # backend/
        alembic_ini = backend_dir / "alembic.ini"
        if not alembic_ini.exists():
            return ["unknown"]
        script = ScriptDirectory.from_config(Config(str(alembic_ini)))
        return sorted(script.get_heads())
    except Exception as exc:
        _log.warning("upgrade.alembic_heads_unresolved error=%s", exc)
        return ["unknown"]


def migration_pending(db_url: str, *, code_heads: list[str] | None = None) -> bool | None:
    """Is the DATABASE behind the code that is about to boot?

    True  — a migration is pending: a snapshot is the recovery point.
    False — the database is already at head: nothing this boot runs can change
            the schema, so a snapshot would only churn recovery points.
    None  — the question cannot be answered (unreadable database or code tree);
            the caller takes the snapshot, because an unproven "nothing to do"
            must never cost the recovery point.

    The comparison is set equality: a database that records a revision the code
    does not ship is AHEAD, which the FAR-2659 alignment guard refuses before
    this runs — it is never silently treated as "pending".
    """
    heads = code_heads if code_heads is not None else _alembic_heads()
    if not heads or "unknown" in heads:
        return None
    db_heads = _database_revisions(db_url)
    if not db_heads or "unknown" in db_heads:
        return None
    return set(db_heads) != set(heads)


def _write_restore_manifest(snapshot_dir: Path, *, created_at: str, checksum: str, size: int, schema_heads: list[str]) -> None:
    """Write the restore-compatible manifest the installer's printed command needs.

    ``farna-control restore`` hard-requires ``backup-info.json`` (shape-validated +
    checksum-verified). Without it the installer's "restore later with" hint
    refuses immediately. The snapshot is written as a legacy-shaped archive
    (no ``manifest_version`` -> accepted) carrying the dump checksum and the
    schema version at backup time (so restore's downgrade guard can warn or
    refuse). ``fernet_key_hash`` is intentionally absent: re-encryption only
    engages when ``credentials_references.json`` exists, which the snapshot
    does not carry (the full pre-restore DB lives in the SQL dump).

    ``dump_complete`` is the dump-time completeness attestation FAR-2667 adds:
    the caller has already verified pg_dump's completion trailer, so restore
    can re-verify the SAME trailer rather than trusting the checksum alone.
    """
    restore_manifest = {
        "snapshot_for_upgrade": True,
        "manifest_version": SNAPSHOT_MANIFEST_VERSION,
        "backup_type": "pre-upgrade-snapshot",
        "timestamp": created_at,
        "schema_versions": schema_heads,
        "db_version": "unknown",
        "dump_name": _DUMP_NAME,
        "dump_bytes": size,
        "dump_format": "plain",
        "dump_complete": True,
        "file_checksums": {_DUMP_NAME: checksum},
    }
    # 0600 fd creation: the manifest is never briefly exposed at umask mode.
    fd = os.open(str(snapshot_dir / _RESTORE_MANIFEST_NAME), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(restore_manifest, f, indent=2, sort_keys=True)


def _run_dump(argv: list[str], dump_path: Path, *, child_env: dict[str, str] | None = None) -> None:
    """Run one fully-pinned argv pg_dump with output captured to *dump_path*.

    NOT used by the pre-upgrade snapshot any more (it goes through the shared
    :func:`farna.db.plain_dump.dump_plain_sql` pipeline so the two producers
    cannot drift); kept as the launcher-side runner for callers that have
    already resolved their own argv.

    Never shell=True, never operator-controlled input: the dump URL is
    composed from the launcher-owned secrets file the same first boot
    generated, with the password OUT OF ARGV (world-readable /proc cmdline)
    and inside the child-scoped env dict instead. The dump file is CREATED
    0600 so a mid-dump SIGKILL never leaves it readable at umask mode. A
    non-zero exit / timeout becomes :class:`UpgradeError`.
    TODO(P3): Windows seam (the bundled runtime is Linux-first).
    """
    fd = os.open(str(dump_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            completed = subprocess.run(  # nosec B603 —— pinned argv, trusted synthesized input  # noqa: S603
                argv,
                stdout=out,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_DUMP_TIMEOUT_SECONDS,
                env=child_env if child_env is not None else dict(os.environ),
            )
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"pg_dump timed out after {_DUMP_TIMEOUT_SECONDS}s") from exc
    if completed.returncode != 0:
        stderr_text = completed.stderr.decode(errors="replace").strip()
        raise UpgradeError(stderr_text or f"pg_dump exited with code {completed.returncode}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry both deployment paths invoke: dump, verify, print the path.

    Two modes, one callable behind them:

    * ``--data-dir`` — the NATIVE path (install.sh). The dump URL is resolved
      from the data dir's state.json + secrets.json.
    * ``--db-url`` + ``--output-dir`` — the CONTAINER path
      (``deploy/supervisor/entrypoint.sh``, before ``alembic upgrade heads``).

    A snapshot is taken only when a migration is actually PENDING. A pristine
    database (first install) and a database already at head are both no-ops:
    one diagnostic line on stderr, nothing on stdout, exit 0. The pending
    decision is made INSIDE :func:`pre_upgrade_dump`, so the native installer
    (which runs the OLD installation's helper against the NEW bundle) cannot
    bypass it; pass ``--target-bundle`` so the comparison is against the
    release that is about to boot. That is what stops a crash-looping release —
    or a plain installer re-run — from rotating its own pre-upgrade recovery
    point away.

    STDOUT CARRIES EXACTLY ONE LINE ON SUCCESS: the snapshot directory (or
    nothing at all when no snapshot was needed). install.sh captures the whole
    stdout as the path, so every diagnostic goes to stderr.

    EVERY failure exits 1 with one actionable ``ERROR:`` line — never a
    traceback: an unusable /backups directory, a missing pg_dump and a
    half-written dump all have to be legible to whoever is watching the boot.
    """
    parser = argparse.ArgumentParser(prog="python -m farna.launcher.upgrade", description="Pre-upgrade database snapshot")
    parser.add_argument("--data-dir", type=Path, default=None, help="Bundled launcher data dir (native path)")
    parser.add_argument("--bin-dir", type=Path, default=None, help="Bundled pg/ binaries dir override (packaging seam)")
    parser.add_argument("--db-url", default=None, help="Database URL to dump — use the OWNER/admin role (container path)")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory that holds snapshot directories (container path; e.g. /backups)")
    parser.add_argument("--release", default=None, help="Release token recorded in the snapshot directory name")
    parser.add_argument(
        "--target-bundle",
        type=Path,
        default=None,
        help=("Extracted NEW bundle whose migration tree decides whether a migration is pending (native path). Without it the helper's own code is the target — wrong for the installer, which runs the OLD installation's helper against the NEW bundle."),
    )
    parser.add_argument(
        "--skip-if-pristine",
        action="store_true",
        default=False,
        help="Exit 0 without a snapshot when the database has NO application tables (a fresh install is not an upgrade)",
    )
    args = parser.parse_args(argv)
    scrub_os_environment()
    if (args.data_dir is None) == (args.db_url is None):
        parser.error("pass exactly one of --data-dir (native) or --db-url (container)")
    if args.db_url is not None and args.output_dir is None:
        parser.error("--db-url requires --output-dir")
    try:
        snapshot: SnapshotRef | None
        if args.db_url is not None:
            assert args.output_dir is not None  # parser-enforced
            # The compose path passes a SQLAlchemy asyncpg URL; psycopg and
            # pg_dump require the libpq form. take_upgrade_snapshot normalises
            # (and strips the password) in the ONE shared place, so the probes
            # here go through the same helper rather than a second rewrite.
            if args.skip_if_pristine and _database_is_pristine(args.db_url):
                print("Fresh database (no application tables) — no pre-upgrade snapshot needed.", file=sys.stderr)  # noqa: T201
                return 0
            if migration_pending(args.db_url) is False:
                print("Database is already at the code's migration head — no migration is pending, so no pre-upgrade snapshot was taken.", file=sys.stderr)  # noqa: T201
                return 0
            snapshot = take_upgrade_snapshot(args.db_url, args.output_dir, release=args.release, bin_dir=args.bin_dir)
        else:
            assert args.data_dir is not None  # parser-enforced
            snapshot = pre_upgrade_dump(
                args.data_dir,
                bin_dir=args.bin_dir,
                release=args.release,
                target_bundle=args.target_bundle,
            )
    except UpgradeError as exc:
        # Actionable installer/entrypoint abort text: never a bare traceback.
        print(f"ERROR: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    except Exception as exc:  # the boot must abort legibly, never trace
        _log.exception("upgrade.snapshot_unexpected_failure")
        print(f"ERROR: the pre-upgrade snapshot failed unexpectedly ({type(exc).__name__}: {exc}) — the boot is aborted and NO migration was applied. Nothing has been changed.", file=sys.stderr)  # noqa: T201
        return 1
    if snapshot is None:
        # The shared helper made the pending decision (see its docstring): no
        # migration can change this database, so no recovery point is owed.
        print("No migration is pending against the target release — no pre-upgrade snapshot was taken.", file=sys.stderr)  # noqa: T201
        return 0
    # The installer/entrypoint captures the FINAL stdout line as the snapshot path.
    print(snapshot.directory)  # noqa: T201
    return 0


# ---------------------------------------------------------------------------
# The full `farna-control upgrade` flow (FAR-675, ADR 031 Decision 7)
#
# Stage order (destructive last):
#   1. fetch the target bundle; SIGNATURE-verify the release manifest;
#      verify the tarball sha256 against it BEFORE extraction. --from-file
#      is the offline escape hatch (operator-provided manifest + sig).
#   2. Pre-flight refusals BEFORE the swap: PG major vs PG_VERSION (hard,
#      never auto-initdb; the manual pg_upgrade remediation per the ADR's
#      bundled-PG-16-only policy); downgrade (DB revision ahead of the
#      target head); quantified disk incl. dump headroom.
#   3. ENFORCED pre-upgrade pg_dump (--skip-backup is the loud flag).
#   4. Service-aware stop (the FAR-674 systemd user unit when installed,
#      else the foreground lock holder) + no-live-process abort (exe/cwd
#      scan naming the PID - the locked-file failure class).
#   5. Atomic `current` symlink swap (POSIX rename(2); Windows
#      junction/rename swap = TODO(P3)) + the upgrade.json marker written
#      BEFORE the boot (the FAR-674 degraded-within-N seam).
#   6. Boot the new bundle's lifespan migrations via its own launcher
#      (`launcher start`) + /healthz gate; on failure: hard refusal
#      printing the snapshot path + the exact manual restore command
#      restoring BOTH binary AND data (forward-migrated-schema wording).
#      NO automatic restore in v1 - the fast-follow design is recorded in
#      ADR 031 Decision 7 (referenced in the message).
#   7. Retention: keep the last 2 version dirs; sweep caches; NEVER delete
#      a state.json-referenced dir; repair a dangling `current` symlink.
# ---------------------------------------------------------------------------

INSTALL_ROOT_ENV = "FARNA_CONTROL_INSTALL_ROOT"
UPGRADE_MARKER_FILENAME = "upgrade.json"
UPGRADE_MARKER_SCHEMA_VERSION = 1
_RETAINED_VERSIONS = 2
_CACHE_PREFIXES = (".staging-", ".downloads-", ".current.new.")
_DEFAULT_BOOT_TIMEOUT = 600.0
_BOOT_POLL_INTERVAL = 0.5
_RELEASES_DOWNLOAD_BASE = "https://github.com/farnalabs/farna-control/releases/download"
_RELEASE_MANIFEST_NAME = "RELEASE_MANIFEST.json"
_LAUNCHER_NAME = "launcher"
_PRESSURE_RESTORE_MANIFEST_NAME = _RESTORE_MANIFEST_NAME


def default_install_root() -> Path:
    """Per-user install root (install.sh's own default + env override)."""
    from_env = os.environ.get(INSTALL_ROOT_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".local" / "opt" / "farna-control"


def _strip_bundle_prefix(version_text: str) -> str:
    """The version-dir name for `bundle-v<v>` / `v<v>` / bare `<v>` targets."""
    return version_text.removeprefix("bundle-v").removeprefix("v")


_VERSION_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")


def _validate_version_segment(version_text: str) -> str:
    """Fail closed unless `version_text` is a single safe path/URL segment.

    The value is interpolated into a download filename and a release URL, so
    only `[A-Za-z0-9._-]+` is allowed; empty, dot-only (`.`/`..`), separator,
    whitespace and newline values are refused (GitHub #1176).
    """
    if _VERSION_SEGMENT_RE.fullmatch(version_text) is None or not version_text.strip("."):
        raise UpgradeError(f"invalid --version {version_text!r}: only letters, digits, '.', '_' and '-' are allowed")
    return version_text


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """0600-creation JSON write, fsynced before close.

    The fsync closes the crash window: an upgrade.json marker written just
    before a power loss must be on disk when the next boot reads it (the
    FAR-674 degraded-window seam keys off exactly this file).
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())


def _sha256_file(path: Path) -> str:
    """Chunked sha256 (release artifacts exceed memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_sort_key(name: str) -> tuple[int, int, int]:
    """Numeric version ordering (1.2.10 sorts after 1.2.9)."""
    numbers: list[int] = []
    for chunk in name.split("."):
        try:
            numbers.append(int(chunk))
        except ValueError:
            numbers.append(-1)
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


@dataclass(frozen=True)
class UpgradeResult:
    """Outcome of one completed `farna-control upgrade` run."""

    previous_version: str
    version: str
    snapshot: Path | None
    marker_path: Path
    pruned: list[str]


def write_upgrade_marker(
    data_dir: Path,
    *,
    previous_version: str,
    version: str,
    pre_upgrade_snapshot: Path | None,
    upgraded_at: float,
) -> Path:
    """Write upgrade.json BEFORE the new bundle boots (FAR-674 seam writer).

    The entry's degraded-context provider opens this marker when a degrade
    lands within ``policy.UPGRADE_DEGRADED_WINDOW_SECONDS`` of
    ``upgraded_at`` - recorded at the marker write, before any boot.
    """
    marker_path = data_dir / UPGRADE_MARKER_FILENAME
    payload = {
        "schema_version": UPGRADE_MARKER_SCHEMA_VERSION,
        "upgraded_at": upgraded_at,
        "previous_version": previous_version,
        "target_version": version,
        "pre_upgrade_snapshot": str(pre_upgrade_snapshot) if pre_upgrade_snapshot is not None else None,
    }
    _write_private_json(marker_path, payload)
    return marker_path


def restore_guidance(
    *,
    snapshot: Path | None,
    previous_version: str,
    install_root: Path,
    data_dir: Path,
    prior_link_target: str | None = None,
) -> str:
    """The exact manual restore command — and exactly what it restores.

    With a *snapshot*: BOTH the binary and the data. The attempted upgrade's
    boot may have forward-migrated the schema past the old bundle's head, so
    restoring the old binary ALONE is not sufficient — the snapshot carries the
    pre-migration rows AND the pre-migration ``alembic_version``.

    Without a *snapshot* (no migration was pending, or ``--skip-backup`` ran
    with its own verified snapshot elsewhere): ONLY the ``current`` symlink.
    The wording says so instead of claiming a data restore that the emitted
    command does not perform.

    NO automatic restore ran (none exists in v1; the fast-follow design -
    temp dir + verify + atomic exchange, gated on never-reached-healthy - is
    recorded in ADR 031 Decision 7, referenced here per the ADR policy).

    *prior_link_target* names the version dir that still holds the OLD
    binary's bytes: a re-run of the same version keeps the previous bytes
    at ``versions/<v>.prev-<ts>``, so the link target names THAT dir and
    never the fresh copy sitting at ``versions/<v>``.
    """
    fault = "NO automatic restore ran (none exists in v1; the fast-follow design is recorded in\nADR 031 Decision 7)."
    link_version = prior_link_target if prior_link_target is not None else previous_version
    link_cmd = f"ln -sfn versions/{link_version} {install_root / 'current'}"
    if snapshot is None:
        body = "Manual restore - this re-points `current` back to the previous binary ONLY.\nNo pre-upgrade snapshot was taken for this attempt (no migration was pending), so the\nDATABASE IS NOT RESTORED by this command and no data restore is needed."
        return f"{fault}\n{body}\nExact manual restore command:\n  {link_cmd}"
    head = "Manual restore - this restores BOTH the binary AND the data. The attempted upgrade's\nboot may have forward-migrated the database schema past the old bundle's migration\nhead, so restoring the old binary ALONE is not sufficient."
    restore_cmd = f"farna-control restore {snapshot} --data-dir {data_dir} --yes"
    compound = link_cmd + " && " + restore_cmd
    return f"{fault}\n{head}\nPre-upgrade snapshot: {snapshot}\nExact manual restore command:\n  {compound}"


def _temp_owner_pid(name: str) -> int | None:
    """The trailing pid suffix of a swap/sweep temp name, or None.

    The temp names the callers build always end in the ASCII-decimal
    ``os.getpid()`` suffix (``.<link>.new.<pid>`` etc.), so only the trailing
    run of ASCII digits is inspected. Hand-rolled rather than
    ``re.search(r"(\\d+)$", name)``: that anchored unbounded quantifier makes
    the engine retry every start position when the string ends in a non-digit,
    which is super-linear on a long digit run (S8786).
    """
    trimmed = name.rstrip("0123456789")
    if len(trimmed) == len(name):
        return None
    return int(name[len(trimmed) :])


def sweep_symlink_temps(link_path: Path) -> None:
    """Remove stale `.current.new.<pid>` temps from earlier crashed swaps.

    A temp owned by a LIVE pid is another concurrent swap's in-flight link —
    never swept (only the owning process's crash leaves it stale).
    """
    with contextlib.suppress(OSError):
        for stale in link_path.parent.glob(f".{link_path.name}.new.*"):
            owner_pid = _temp_owner_pid(stale.name)
            if owner_pid is not None and _pid_alive(owner_pid):
                continue
            with contextlib.suppress(OSError):
                if stale.is_symlink() or stale.is_file():
                    stale.unlink()
                elif stale.is_dir():  # pragma: no cover - unexpected
                    shutil.rmtree(stale, ignore_errors=True)


def sweep_upgrade_caches(install_root: Path) -> list[str]:
    """Sweep stale `.staging-*` / `.downloads-*` / `.current.new.*` debris.

    The download/extract caches land here when an interrupted run dies;
    retention NEVER touches a state.json-referenced (versioned) dir. Temps
    carrying a LIVE pid suffix are concurrent runs' working state — skipped.
    """
    pruned: list[str] = []
    if not install_root.is_dir():
        return pruned
    with contextlib.suppress(OSError):
        for entry in install_root.iterdir():
            if not any(entry.name.startswith(prefix) for prefix in _CACHE_PREFIXES):
                continue
            owner_pid = _temp_owner_pid(entry.name)
            if owner_pid is not None and _pid_alive(owner_pid):
                continue
            try:
                if entry.is_symlink() or entry.is_file():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
            except OSError:
                continue
            pruned.append(entry.name)
    return pruned


def atomic_symlink_swap(link_path: Path, target_value: str) -> None:
    """The POSIX crash-safe `current` rename swap (the swap primitive).

    A temp sibling symlink is built then rename(2)'d OVER the incumbent, so
    a crash mid-swap leaves either the old or the new link - never a
    missing `current`. Stale temps from earlier crashed swaps are swept
    first; a hard failure (a locked/dead target, a full volume) RAISES
    rather than half-swapping. TODO(P3): the Windows junction/rename swap
    seam substitutes for the POSIX symlink swap here.
    """
    if sys.platform == "win32":
        raise UpgradeError("the `current` symlink swap is POSIX-only (TODO(P3): the Windows seam swaps over a junction via rename)")
    sweep_symlink_temps(link_path)
    temp_link = link_path.parent / f".{link_path.name}.new.{os.getpid()}"
    with contextlib.suppress(OSError):
        temp_link.unlink()
    temp_link.symlink_to(target_value)
    try:
        temp_link.replace(link_path)
    except OSError:
        with contextlib.suppress(OSError):
            temp_link.unlink()
        raise


def no_live_process_inside(target_root: Path) -> None:
    """Refuse when a LIVE process has exe or cwd inside *target_root*.

    Covers the locked-file failure class: a still-running binary/library
    from the version dir about to be swapped. Names every offending PID.
    The UPGRADING PROCESS ITSELF is excluded: self-upgrades re-run the CLI
    whose executable shim lives inside the scanned incumbent dir — without
    the self-exclusion every self-upgrade aborts on its own process.
    POSIX-only (the /proc scan); the flow's own platform gate refuses the
    remaining platforms before reaching here.
    """
    if os.name != "posix":
        # The flow's own platform gate refuses non-POSIX first; nothing on
        # that path reads /proc (the Linux-first P1a scan shape).
        return
    resolved_root = os.path.realpath(str(target_root))
    own_pid = os.getpid()
    own_exe = os.path.realpath(sys.executable)
    offenders: list[str] = []
    for proc_entry in Path("/proc").iterdir():
        if not proc_entry.name.isdigit():
            continue
        pid = int(proc_entry.name)
        try:
            exe = str((proc_entry / "exe").readlink())
            cwd = str((proc_entry / "cwd").readlink())
        except OSError:
            continue
        if pid == own_pid or os.path.realpath(str(exe)) == own_exe:
            continue
        cwd_real = os.path.realpath(str(cwd))
        if exe == resolved_root or exe.startswith(resolved_root + os.sep) or cwd_real == resolved_root or cwd_real.startswith(resolved_root + os.sep):
            offenders.append(f"pid {proc_entry.name} (cwd={cwd}, exe={exe})")
    if offenders:
        raise UpgradeError(f"Refusing: live processes remain inside {resolved_root} - stop them first:\n  " + "\n  ".join(offenders))


def repair_current_symlink(install_root: Path) -> str | None:
    """Repair a dangling `current` symlink to last-known-good.

    Re-points (atomic_symlink_swap) to the newest remaining version dir.
    NEVER deletes anything - the resolved `current` target is the version
    bookkeeping treats as the data dir's live companion.
    """
    link = install_root / "current"
    versioned_root = install_root / "versions"
    if not versioned_root.is_dir():
        return None
    names = sorted(
        (entry.name for entry in versioned_root.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
        key=_version_sort_key,
        reverse=True,
    )
    if not names:
        return None
    if link.is_symlink():
        try:
            resolved_link = link.resolve(strict=True)
        except OSError:
            resolved_link = None
        if resolved_link is not None and resolved_link.is_dir():
            return None  # not dangling: nothing to repair
    sweep_symlink_temps(link)
    temp_link = link.parent / f".{link.name}.repair.{os.getpid()}"
    temp_link.symlink_to(f"versions/{names[0]}")
    try:
        temp_link.replace(link)
    except OSError:
        with contextlib.suppress(OSError):
            temp_link.unlink()
        raise
    return names[0]


def prune_versions(
    install_root: Path,
    *,
    current_target: Path,
    retained: int = _RETAINED_VERSIONS,
) -> list[str]:
    """Keep the newest *retained* version dirs; never delete the live one.

    `current` is RE-RESOLVED at prune time (the swap may have re-pointed it
    moments ago — pruning against the pre-swap incumbent can delete the dir
    `current` now serves) and the state.json-referenced dir
    (*current_target*) — neither is ever deleted. `.prev-<ts>` re-run
    retention dirs are pruned only at their own later cycle, never here.
    """
    versioned_root = install_root / "versions"
    protected: set[Path] = {current_target.resolve()}
    link = install_root / "current"
    if link.is_symlink():
        with contextlib.suppress(OSError):
            resolved_now = link.resolve(strict=True)
            if resolved_now.is_dir():
                protected.add(resolved_now.resolve())
    entries = sorted(
        (entry for entry in versioned_root.iterdir() if entry.is_dir() and not entry.name.startswith(".") and ".prev-" not in entry.name),
        key=lambda entry: _version_sort_key(entry.name),
        reverse=True,
    )
    pruned: list[str] = []
    walked = 0
    for entry in entries:
        if entry.resolve() in protected:
            continue
        walked += 1
        if walked <= retained:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        pruned.append(entry.name)
    return pruned


def _host_os() -> str:
    """The bundle asset's OS token (linux or darwin)."""
    return "darwin" if sys.platform == "darwin" else "linux"


def _host_arch() -> str:
    """Return the supported bundle arch token for this host.

    Unknown machine values must refuse rather than silently selecting amd64:
    downloading an artifact for the wrong architecture can pass every checksum
    gate and fail only when the native launcher is executed.
    """
    import platform

    machine = platform.machine()
    try:
        return {
            "x86_64": "amd64",
            "AMD64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }[machine]
    except KeyError as exc:
        raise UpgradeError(f"unsupported host architecture '{machine}'; supported architectures are x86_64/amd64 and aarch64/arm64") from exc


def _urllib_fetch(url: str, target: Path) -> None:
    """argv-free pinned-https fetch (urllib only - no shell, no curl argv)."""
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as handle:  # nosec B310 - pinned https release base  # noqa: S310
        shutil.copyfileobj(response, handle)


@dataclass(frozen=True)
class UpgradeDownloads:
    """The fetched, not-yet-verified release assets."""

    version: str
    tarball: Path
    manifest: Path
    signature: Path


def fetch_release_assets(
    target_version: str,
    download_dir: Path,
    *,
    fetch: Callable[[str, Path], None] | None = None,
) -> UpgradeDownloads:
    """Fetch bundle-v<v>'s tarball + signed manifest + .sig (pinned https).

    A pinned version is REQUIRED (the /releases/latest resolution is a
    TODO seam); the argv-free urllib fetch carries no shell and no curl
    argv. The signed manifest lands beside the tarball so the sha256 gate
    runs BEFORE extraction.
    """
    target_version = _validate_version_segment(target_version)
    release_tag = f"bundle-v{target_version}"
    download_dir.mkdir(parents=True, exist_ok=True)
    tarball_name = f"farna-control-{target_version}-{_host_os()}-{_host_arch()}.tar.gz"
    base = f"{_RELEASES_DOWNLOAD_BASE}/{release_tag}"
    tarball = download_dir / tarball_name
    if not tarball.resolve().is_relative_to(download_dir.resolve()):
        raise UpgradeError(f"invalid --version {target_version!r}: download path escapes the download directory")
    manifest_file = download_dir / _RELEASE_MANIFEST_NAME
    signature_file = download_dir / f"{_RELEASE_MANIFEST_NAME}.sig"
    fetch_fn = fetch if fetch is not None else _urllib_fetch
    fetch_fn(f"{base}/{tarball_name}", tarball)
    fetch_fn(f"{base}/{_RELEASE_MANIFEST_NAME}", manifest_file)
    fetch_fn(f"{base}/{_RELEASE_MANIFEST_NAME}.sig", signature_file)
    return UpgradeDownloads(version=target_version, tarball=tarball, manifest=manifest_file, signature=signature_file)


def _checksum_artifact(bundle_dir: Path, name: str) -> Path:
    """Validate a SHA256SUMS name without following untrusted path components."""
    # Native bundle checksum generators prefix every path with ./; remove
    # exactly one leading prefix before applying the path-safety validation.
    canonical_name = name.removeprefix("./")
    path = Path(canonical_name)
    raw_components = canonical_name.split("/")
    if path.is_absolute() or PureWindowsPath(name).is_absolute() or _WINDOWS_DRIVE_PATH.match(name) or "\\" in name or any(component in {"", ".", ".."} for component in raw_components):
        raise UpgradeError(f"bundle integrity check FAILED: unsafe artifact path {name!r}")

    current = bundle_dir
    for component in path.parts[:-1]:
        current = current / component
        try:
            component_stat = current.lstat()
        except OSError as exc:
            raise UpgradeError(f"bundle integrity check FAILED: unsafe artifact path {name!r}") from exc
        if stat.S_ISLNK(component_stat.st_mode) or not stat.S_ISDIR(component_stat.st_mode):
            raise UpgradeError(f"bundle integrity check FAILED: unsafe artifact path {name!r}")

    artifact = current / path.parts[-1]
    try:
        artifact_stat = artifact.lstat()
    except OSError as exc:
        raise UpgradeError(f"bundle integrity check FAILED: {name} (listed in SHA256SUMS) is missing") from exc
    if stat.S_ISLNK(artifact_stat.st_mode) or not stat.S_ISREG(artifact_stat.st_mode):
        raise UpgradeError(f"bundle integrity check FAILED: {name} (listed in SHA256SUMS) is not a regular file")
    try:
        artifact.resolve(strict=True).relative_to(bundle_dir.resolve(strict=True))
    except (OSError, ValueError) as exc:  # pragma: no cover - lstat/resolve TOCTOU guard
        raise UpgradeError(f"bundle integrity check FAILED: unsafe artifact path {name!r}") from exc
    return artifact


def extract_and_verify_bundle(tarball: Path, staging_dir: Path) -> Path:
    """Extract into *staging_dir*; assert ONE top-level dir; verify each
    file against the bundle's own SHA256SUMS (the second integrity layer)."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tarball, "r:gz") as archive:
            archive.extractall(staging_dir, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise UpgradeError(f"could not extract {tarball.name}: {exc}") from exc
    top_levels = list(staging_dir.iterdir())
    if len(top_levels) != 1 or not top_levels[0].is_dir() or top_levels[0].is_symlink():
        raise UpgradeError(f"unexpected archive layout: expected exactly one non-symlink top-level directory in {tarball.name}, found {len(top_levels)}")
    bundle_dir = top_levels[0]
    expected_prefix = tarball.name.removesuffix(".tar.gz")
    if bundle_dir.name != expected_prefix:
        raise UpgradeError(f"archive top-level directory '{bundle_dir.name}' does not match archive identity '{expected_prefix}'")
    sums_path = bundle_dir / "SHA256SUMS"
    if not sums_path.is_file():
        raise UpgradeError(f"malformed bundle: SHA256SUMS missing from {tarball.name}")
    for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        expected, _separator, name = line.partition("  ")
        if len(expected) != 64 or not name:
            raise UpgradeError(f"malformed SHA256SUMS line in {tarball.name}: {line!r}")
        artifact = _checksum_artifact(bundle_dir, name)
        actual = _sha256_file(artifact)
        if actual != expected:
            raise UpgradeError(f"bundle integrity check FAILED for {name}: expected {expected}, got {actual} - do not use this download; report it at https://github.com/farnalabs/farna-control/issues")
    _log.info("upgrade.bundle_internal_checksums_verified dir=%s", bundle_dir.name)
    return bundle_dir


def _prior_pg_bin(current_target: Path) -> Path:
    """The OLD bundle's bundled pg/ binaries dir (for the enforced dump)."""
    pg_bin = current_target / "pg" / "bin"
    if not pg_bin.is_dir():
        raise UpgradeError(f"the installed bundle carries no pg/ binaries at {pg_bin}")
    return pg_bin


def stop_running_stack(data_dir: Path, *, unit_file: Path | None = None) -> None:
    """Service-aware stop: the FAR-674 systemd user unit when installed
    (stopped FIRST - a restart=on-failure unit would otherwise respawn the
    foreground holder), then the SIGTERM via the supervisor's stop;
    refuses with the holder PID when the stack is STILL live afterwards.

    A systemctl-stop timeout is handled (logged loudly, never a traceback):
    the supervisor's own request_stop still runs, and any residual holder
    produces the refusal path below.
    """
    from farna.launcher import service as service_module
    from farna.launcher.supervisor import request_stop

    unit_path = unit_file if unit_file is not None else service_module.default_unit_path()
    if unit_path.is_file():
        _stop_service(unit_path)
    request_stop(data_dir)
    holder = _read_lock_holder(data_dir.parent / (data_dir.name + ".lock"))
    if holder is not None and _pid_alive(holder.pid):
        _restart_unit_best_effort(why="post-stop abort: restoring the stopped service")
        raise UpgradeError(f"Refusing: the data dir {data_dir} is STILL locked by a live launcher (holder PID {holder.pid}) - stop it first: farna-control stop")


def _restart_unit_best_effort(*, why: str) -> None:
    """Best-effort restart through the host service manager."""
    try:
        unit_path = _service_unit_path()
        if not unit_path.is_file():
            return
        _restart_service(unit_path, why=why)
    except Exception:  # best-effort path, never masks the primary error
        _log.exception("upgrade.unit_restart_best_effort_failed (%s)", why)


def _snapshot_schema_versions(snapshot_dir: Path) -> list[str]:
    """The pre-upgrade snapshot's recorded alembic head(s) (restore side)."""
    recorded = snapshot_dir / _RESTORE_MANIFEST_NAME
    if not recorded.is_file():
        return []
    try:
        body = json.loads(recorded.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read the snapshot's recorded schema versions: {exc}") from exc
    versions = body.get("schema_versions")
    return [str(entry) for entry in versions] if isinstance(versions, list) else []


def _bundle_alembic_revisions(bundle_dir: Path) -> set[str]:
    """Every revision id the NEW bundle's migrations tree ships."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migrations_dir = bundle_dir / "backend" / "src" / "farna" / "db" / "migrations"
        if not migrations_dir.is_dir():
            return set()
        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        script = ScriptDirectory.from_config(config)
        return {revision.revision for revision in script.walk_revisions()}
    except Exception as exc:
        _log.warning("upgrade.target_heads_unresolved error=%s", exc)
        return set()


def check_no_downgrade(
    current_heads: list[str],
    snapshot_dir: Path | None,
    bundle_dir: Path,
) -> None:
    """Refuse a downgrade: DB revision ahead of the target bundle's head.

    *current_heads* is the LIVE DB side (the caller passes the old runtime's
    live alembic probe on a skip-backup run, the snapshot's recorded heads
    otherwise). "unknown" is never pass-through: when the live side reads
    unknown and a snapshot exists, the snapshot's recorded schema versions
    answer instead - and if BOTH read unknown (or nothing at all), the
    check REFUSES blind, never guesses. The refusal carries the restore
    guidance wording.
    """
    target_heads = _bundle_alembic_revisions(bundle_dir)
    db_heads = list(current_heads)
    if snapshot_dir is not None and (not db_heads or "unknown" in db_heads):
        db_heads = _snapshot_schema_versions(snapshot_dir)
    unknown = [head for head in db_heads if head == "unknown"]
    if not db_heads or len(unknown) == len(db_heads):
        raise UpgradeError("cannot verify the database's migration revision(s): neither the live probe nor the pre-upgrade snapshot recorded them - refusing the downgrade check blind")
    unknown_to_target = [head for head in db_heads if head not in target_heads and head != "unknown"]
    if unknown_to_target:
        raise UpgradeError(
            "Refusing to DOWNGRADE: the database revision(s) "
            f"{', '.join(unknown_to_target)} are ahead of the target bundle's supported "
            "migration head.\n"
            "Restore guidance: re-point `current` back to the previous version (ln -sfn "
            "versions/<previous> <install-root>/current) and restore the pre-upgrade snapshot "
            "(farna-control restore <snapshot-dir> --yes)."
        )


def _check_pg_major(components: dict[str, str], data_dir: Path) -> None:
    """PG major vs the data dir's PG_VERSION - a mismatch HARD-refuses.

    Per ADR 031 Decision 7: bundled PostgreSQL is 16.x-only, never
    auto-initdb; the manual pg_upgrade remediation names the ADR policy
    (the pg-upgrade helper is a merge prerequisite of any future PG-major
    bump; the bump itself waits for PG 16 EOL ~Nov 2028 or a high-severity
    CVE).
    """
    pg_version = str(components.get("postgres", "")).strip()
    if not pg_version or not re.fullmatch(r"\d+(?:\.\d+)*", pg_version):
        raise UpgradeError(f"the release manifest's 'components' carry no USABLE postgres version ({pg_version!r}) - refusing the upgrade (a version the pg-major gate cannot compare is never guessed)")
    target_major = int(pg_version.split(".")[0])
    version_file = data_dir / _PGDATA_DIRNAME / _PG_VERSION_FILE
    if not version_file.exists():
        raise UpgradeError(f"no initialised bundled cluster at {data_dir / _PGDATA_DIRNAME} - refusing the upgrade (never auto-initdb over a data dir)")
    try:
        data_major = int(version_file.read_text(encoding="utf-8").strip().split(".")[0])
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read {version_file}: {exc}") from exc
    if data_major != target_major:
        raise UpgradeError(
            f"Bundled PostgreSQL major mismatch: the data dir at {data_dir} was initialised by "
            f"PostgreSQL {data_major}, but the target bundle ships PostgreSQL {target_major}.\n"
            "Per ADR 031 Decision 7, bundled PostgreSQL is 16.x-only and a PG-major mismatch is\n"
            "HARD-REFUSED - never auto-initdb. Manual remediation: run pg_upgrade against this\n"
            "data dir BEFORE upgrading (the pg-upgrade helper is a REQUIRED merge prerequisite\n"
            "of the first PG-major-bump release per the same ADR; until it ships there is no\n"
            "supported upgrade across a PG major)."
        )


def _bundled_boot_argv(install_root: Path, data_dir: Path) -> list[str]:
    """The NEW bundle's boot argv (the documented migration mechanism).

    Mechanism choice: the flow EXECs the NEW bundle's own `launcher start`
    console hook as a FOREGROUND child; the launcher's lifespan migrations
    run as part of that boot (ADR 031 Decision 2 - SAQ spawns only after
    migration-head readiness), so no separate migration exec exists. The
    /healthz gate (run_boot) decides healthy.
    """
    launcher = install_root / "current" / _LAUNCHER_NAME
    if not launcher.is_file():
        raise UpgradeError(f"the NEW bundle carries no launcher hook ({launcher}) - refusing to boot (the FAR-671 bundle layout is in place, but the launcher hook is absent)")
    return [str(launcher), "start", "--data-dir", str(data_dir)]


def run_boot(
    boot_argv: list[str],
    *,
    api_port: int,
    timeout: float = _DEFAULT_BOOT_TIMEOUT,
    poll_interval: float = _BOOT_POLL_INTERVAL,
) -> int:
    """Exec the NEW bundle's launch and gate on /healthz.

    subprocess.Popen the boot argv in its OWN process group
    (``start_new_session=True``): the bundled postgres/redis/SAQ children
    belong to the same group, so teardown never orphans them. stderr is a
    temp FILE (never a PIPE): a chatty child cannot fill a 64KB pipe and
    deadlock while /healthz is never served - a false timeout referral -
    and the failure path reads the file for diagnostics.

    Health gate = an EXACT HTTP 200 whose body is ``{"status": "ok"}``:
    /healthz is the launcher's liveness endpoint (its migration gate - the
    lifespan runs the migrations BEFORE the API serves). The current
    endpoint carries no version/head field; when the launcher ships one
    the upgrade flow should assert it here (TODO adjacent), and the boot's
    own migration readiness is additionally the launcher's OWN gate -
    /healthz freshness is honest about being liveness-only.

    The caller decides the resting state (see perform_upgrade): a healthy
    gate-restores the stack when the FAR-674 systemd unit exists (no
    systemd unit -> the flow leaves the stack stopped and says so).

    A failing boot / no health inside *timeout* -> UpgradeError; the
    teardown is a GROUP kill (SIGTERM to the group, escalated).
    """
    import urllib.request

    launcher = Path(boot_argv[0])
    stderr_allowance = 65536
    stderr_file = tempfile.TemporaryFile(prefix="modulo-upgrade-boot-stderr-")  # noqa: SIM115 - the lifetime must span Popen + the poll loop
    process = subprocess.Popen(  # noqa: S603 - pinned argv; synthesized trusted input
        boot_argv,
        cwd=str(launcher.parent),
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
        start_new_session=(sys.platform != "win32"),
    )
    health_url = f"http://127.0.0.1:{api_port}/healthz"
    started_at = time.monotonic()
    healthy = False
    try:
        while process.poll() is None:
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:  # nosec - loopback health probe
                    if response.status == 200:
                        body = json.loads(response.read().decode("utf-8", errors="replace"))
                        if isinstance(body, dict) and body.get("status") == "ok":
                            healthy = True
                            break
            except (ValueError, OSError):
                # urllib.error.URLError / HTTPError are OSError subclasses; the
                # poll loop wants to retry any probe failure with no logging.
                pass
            if time.monotonic() - started_at > timeout:
                break
            time.sleep(poll_interval)
    finally:
        _terminate_boot_process_group(process)
        stderr_file.seek(0)
        stderr_text = stderr_file.read(stderr_allowance).decode("utf-8", errors="replace").strip()
        stderr_file.close()
    if not healthy:
        raise UpgradeError(f"the NEW bundle's boot did not reach a healthy /healthz (child exit code {process.returncode}): {stderr_text[-400:] if stderr_text else '(no stderr)'}")
    return int(process.returncode or 0)


def _terminate_boot_process_group(process: subprocess.Popen[bytes]) -> None:
    """Ordered teardown of the boot child's WHOLE process group.

    The bundled postgres/redis/SAQ children share the group, so the direct
    child is never orphaned owning a live stack. SIGTERM first, a short
    wait, then SIGKILL - on every exit path (timeout AND crash).
    """
    if process.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            process_group = os.getpgid(process.pid)
            os.killpg(process_group, signal.SIGTERM)
        else:  # pragma: no cover - the platform gate refuses Windows earlier
            process.terminate()
    except OSError:  # ProcessLookupError is an OSError subclass
        pass
    try:
        process.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover
            process.kill()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):  # ProcessLookupError is an OSError subclass
        _log.exception("upgrade.boot_group_kill_incomplete pid=%s", process.pid)


def _quantified_disk_preflight(tarball: Path, data_dir: Path) -> None:
    """Quantified free-space gate (bundle staging + dump headroom).

    The bound: (tarball bytes) x 2 (staging + extract) + (the pgdata
    footprint) x 2 (the enforced pg_dump plus its headroom).
    """
    pgdata = data_dir / _PGDATA_DIRNAME
    tarball_bytes = tarball.stat().st_size
    pgdata_bytes = 0
    try:
        for entry in pgdata.rglob("*"):
            if entry.is_file():
                pgdata_bytes += entry.stat().st_size
    except OSError as exc:
        raise UpgradeError(f"cannot measure the pgdata footprint for the disk preflight: {exc}") from exc
    required = 2 * tarball_bytes + 2 * pgdata_bytes
    free = shutil.disk_usage(str(data_dir)).free
    if free < required:
        raise UpgradeError(f"Insufficient disk: {required} bytes required ({2 * tarball_bytes} bundle staging + {2 * pgdata_bytes} dump headroom), {free} bytes free on the volume holding {data_dir}")


def _move_into_place(bundle_dir: Path, install_root: Path, version: str) -> tuple[Path, Path | None]:
    """Lay the staged bundle down as versions/<version> (the v1 layout).

    A re-run over the SAME version keeps the old bytes at
    ``versions/<v>.prev-<ts>`` (never destroyed before the boot gate):
    restore_guidance then names that surviving dir as the link target.
    """
    versioned_root = install_root / "versions"
    versioned_root.mkdir(parents=True, exist_ok=True)
    target = versioned_root / version
    prior_dir: Path | None = None
    if target.exists():
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        prior_dir = versioned_root / f"{target.name}.prev-{timestamp}"
        counter = 0
        while prior_dir.exists():
            counter += 1
            prior_dir = versioned_root / f"{target.name}.prev-{timestamp}-{counter}"
        with contextlib.suppress(OSError):
            target.rename(prior_dir)
            prior_dir = prior_dir if prior_dir.is_dir() else None
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(bundle_dir, target, symlinks=True)
    shutil.rmtree(bundle_dir, ignore_errors=True)
    _log.info("upgrade.installed_target dir=%s prior=%s", target, prior_dir)
    return target, prior_dir


def _api_port_of(data_dir: Path) -> int:
    """The persisted API port (state.json via the secrets-keyed loader)."""
    from farna.launcher.secrets_file import _load_existing
    from farna.launcher.state import load_state

    secrets = _load_existing(data_dir / _SECRETS_FILENAME)
    owned_state = load_state(data_dir / _STATE_FILENAME, secrets.state_hmac_key)
    return int(owned_state.api_port)


def _resolve_current_target(install_root: Path) -> Path:
    """The incumbent (state.json-referenced) version dir - never deleted.

    A DANGLING `current` is repaired first (repair_current_symlink —
    `farna-control start` runs the same repair): an operator's bricked install
    self-heals instead of dead-ending on an internal Python function.
    """
    current_link = install_root / "current"
    if not current_link.is_symlink():
        raise UpgradeError(f"no native install to upgrade under {install_root} (no resolvable `current`) - install.sh sets one up; a populated data dir alone is not an install")
    try:
        resolved_target = current_link.resolve(strict=True)
    except OSError:
        repair_state = repair_current_symlink(install_root)
        if repair_state is None:
            raise UpgradeError(f"the `current` symlink under {install_root} is DANGLING and could not be repaired (no version dir survives) - reinstall:  bash scripts/install.sh") from None
        _log.info("upgrade.current_symlink_repaired version=%s", repair_state)
        try:
            resolved_target = current_link.resolve(strict=True)
        except OSError as exc:
            raise UpgradeError(f"the repaired `current` symlink still does not resolve: {exc}") from exc
    if not resolved_target.is_dir():
        raise UpgradeError(f"the `current` symlink resolves to a non-directory ({resolved_target})")
    return resolved_target


def assert_upgrade_platform() -> None:
    if sys.platform == "darwin":
        raise UpgradeError("macOS native artifact is not yet published; the macOS service/data/launcher seam exists, but no complete signed and notarized Darwin bundle is available for upgrade")
    if sys.platform == "win32":
        raise UpgradeError("the full farna-control upgrade flow is not implemented on Windows yet (TODO(P3)): the versioned-dir/current-symlink swap is a POSIX symlink primitive today")


def _service_unit_path() -> Path:
    from farna.launcher import service as service_module

    return service_module.default_unit_path()


def _run_service_command(argv: list[str], *, why: str) -> None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)  # noqa: S603
    except subprocess.TimeoutExpired:
        _log.warning("upgrade.service_command_timed_out argv=%s (%s)", argv, why)
        return
    if result.returncode == 0:
        _log.warning("upgrade.unit_started_after_abort argv=%s (%s)", argv, why)
        return
    detail = (result.stderr or result.stdout or "").strip()
    _log.error("upgrade.unit_restart_after_abort_FAILED rc=%s stderr=%s (%s)", result.returncode, detail[-300:], why)


def _stop_service(unit_path: Path) -> None:
    if sys.platform == "darwin":
        _run_service_command(["launchctl", "unload", "-w", str(unit_path)], why="upgrade stop")
    else:
        from farna.launcher import service as service_module

        _run_service_command(["systemctl", "--user", "stop", service_module.UNIT_FILENAME], why="upgrade stop")


def _restart_service(unit_path: Path, *, why: str) -> None:
    if sys.platform == "darwin":
        _run_service_command(["launchctl", "load", "-w", str(unit_path)], why=why)
    else:
        from farna.launcher import service as service_module

        _run_service_command(["systemctl", "--user", "start", service_module.UNIT_FILENAME], why=why)


def _resolve_upgrade_assets(
    *,
    from_file: Path | None,
    target_version: str | None,
    staging_dir: Path,
    fetch: Callable[[str, Path], None] | None,
) -> tuple[Path, str, Path, Path]:
    """(tarball, version, manifest_file, signature_file) — fetch or --from-file.

    ``--from-file`` is the offline escape hatch: the operator-provided tarball
    must sit beside its SIGNED manifest + ``.sig`` (the same embedded trust
    store verifies it). Otherwise the pinned ``target_version`` assets are
    fetched into the staging dir.
    """
    if from_file is None:
        if target_version is None:  # pragma: no cover - CLI-gated
            raise UpgradeError("a pinned target version is REQUIRED (e.g. bundle-v0.1.0)")
        downloads = fetch_release_assets(target_version, staging_dir / "downloads", fetch=fetch)
        return downloads.tarball, downloads.version, downloads.manifest, downloads.signature
    tarball = Path(from_file)
    if not tarball.is_file():
        raise UpgradeError(f"--from-file: no such file: {from_file}")
    match = re.fullmatch(
        r"(?:farna-control|modulo)-(?P<version>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<os>linux|darwin)-(?P<arch>amd64|arm64)\.tar\.gz",
        tarball.name,
    )
    if match is None:
        raise UpgradeError("--from-file archive name is malformed: expected exactly farna-control-<version>-<os>-<arch>.tar.gz (or the pre-rebrand modulo-<version>-<os>-<arch>.tar.gz for a legacy archive)")
    os_token = _host_os()
    archive_os = match.group("os")
    if archive_os != os_token:
        raise UpgradeError(f"--from-file archive OS does not match the detected host ({os_token}): {tarball.name}")
    host_arch = _host_arch()
    archive_arch = match.group("arch")
    if archive_arch != host_arch:
        raise UpgradeError(f"--from-file archive architecture '{archive_arch}' does not match the detected host architecture '{host_arch}': {tarball.name}")
    version = _validate_version_segment(match.group("version"))
    manifest_file = tarball.parent / _RELEASE_MANIFEST_NAME
    signature_file = tarball.parent / f"{_RELEASE_MANIFEST_NAME}.sig"
    if not (manifest_file.is_file() and signature_file.is_file()):
        raise UpgradeError(f"--from-file requires the SIGNED manifest beside the tarball: {manifest_file} (+ .sig) - the offline escape hatch verifies against the SAME embedded trust store (ADR 031 Decision 3)")
    return tarball, version, manifest_file, signature_file


def _bundle_alembic_heads(bundle_dir: Path) -> set[str]:
    """The NEW bundle's migration HEAD(s) (empty when it cannot be read)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        migrations_dir = bundle_dir / "backend" / "src" / "farna" / "db" / "migrations"
        if not migrations_dir.is_dir():
            return set()
        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        script = ScriptDirectory.from_config(config)
        return set(script.get_heads())
    except Exception as exc:
        _log.warning("upgrade.bundle_heads_unresolved error=%s", exc)
        return set()


def _require_existing_snapshot(data_dir: Path, *, current_heads: list[str] | None = None) -> Path:
    """A ``--skip-backup`` run must have a VERIFIED snapshot OF THE CURRENT SCHEMA.

    ``--skip-backup`` skips the DUMP, not the recovery point: it is the
    installer's second pass (the stack had to be stopped for the swap, so the
    dump taken moments earlier on the first pass is the authoritative one).
    Proceeding with no snapshot at all would remove the only way back from a
    forward-migrated schema, which is exactly what the operator is told cannot
    happen — so refuse, naming the flow.

    "Verified" now includes the SCHEMA STATE: the snapshot's recorded
    revision(s) must equal the LIVE database's current revision(s)
    (*current_heads*, read by the caller before the swap). An intact snapshot
    several migrations stale is not a recovery point for this database —
    restoring it would roll the schema back past data the database already
    holds — and returning it would point ``restore_guidance`` at an archive
    that cannot recover the current state. Unknown heads on either side cannot
    be compared, so they are refused rather than guessed.
    """
    for entry in _snapshot_dirs(data_dir):
        if not snapshot_is_intact(entry):
            continue
        recorded = _recorded_heads(entry)
        if _records_schema_state(recorded, current_heads):
            _log.info("upgrade.skip_backup_snapshot_found dir=%s", entry)
            return entry
        _log.info("upgrade.skip_backup_snapshot_stale dir=%s recorded=%s current=%s", entry, ",".join(recorded), ",".join(current_heads or []))
    raise UpgradeError(
        f"Refusing --skip-backup: no verified pre-upgrade snapshot of the CURRENT schema exists under {data_dir}"
        + (f" (the database records {', '.join(current_heads)})." if current_heads else ".")
        + " --skip-backup skips the DUMP, not the recovery point: an intact snapshot that records a DIFFERENT schema state "
        "cannot restore this database. Run the upgrade once with the stack live so a snapshot of the current state is taken, "
        "then re-run with --skip-backup only if the first pass stopped before the swap."
    )


def _run_upgrade_preflight(
    *,
    skip_backup: bool,
    data_dir: Path,
    reserve_target: Path,
    bundle_dir: Path,
    release_manifest: ReleaseManifest,
    tarball: Path,
    version: str,
) -> Path | None:
    """Pre-flight refusals + the enforced dump; returns the snapshot dir.

    Runs BEFORE anything version-specific: the PG-major gate, the downgrade
    check and the quantified disk preflight. The DB side of both checks is read
    from the LIVE DATABASE (never from the old bundle's script heads — a
    database already migrated ahead is only detectable by asking it).

    A snapshot is taken only when a migration is actually pending (the
    database's revisions differ from the TARGET bundle's heads). A database
    already at the target head cannot be changed by this boot, so no snapshot
    is taken and one line says so. On ``skip_backup`` a verified snapshot must
    already exist, and the live probe answers the downgrade check.
    """
    _check_pg_major(release_manifest.components, data_dir)
    db_heads = live_database_revisions(data_dir)
    if skip_backup:
        existing = _require_existing_snapshot(data_dir, current_heads=db_heads)
        snapshot_dir: Path | None = existing
    else:
        target_heads = _bundle_alembic_heads(bundle_dir)
        pending = _pending_against(db_heads, target_heads)
        if pending is False:
            _log.info("upgrade.no_migration_pending snapshot_skipped=true data_dir=%s heads=%s", data_dir, ",".join(sorted(target_heads)))
            snapshot_dir = None
        else:
            # The helper re-checks pending against the TARGET bundle's heads,
            # so this decision and its own can never disagree about which code
            # is being upgraded TO. An UNREADABLE bundle tree is passed as an
            # EMPTY list (never None): that reads as "undecidable" inside the
            # helper and takes the snapshot, where None would fall back to this
            # runtime's own heads and silently skip the recovery point.
            snapshot = pre_upgrade_dump(
                data_dir,
                bin_dir=_prior_pg_bin(reserve_target),
                release=version,
                target_heads=sorted(target_heads),
            )
            snapshot_dir = snapshot.directory if snapshot is not None else None
            if snapshot is None:
                _log.warning("upgrade.snapshot_skipped_after_pending_probe data_dir=%s", data_dir)
    check_no_downgrade(db_heads, snapshot_dir, bundle_dir)
    _quantified_disk_preflight(tarball, data_dir)
    return snapshot_dir


def _pending_against(db_heads: list[str], target_heads: set[str]) -> bool | None:
    """Is the database behind *target_heads*? ``None`` when either side is unknown."""
    if not db_heads or "unknown" in db_heads or not target_heads:
        return None
    return set(db_heads) != set(target_heads)


def perform_upgrade(
    data_dir: Path,
    *,
    install_root: Path,
    target_version: str | None = None,
    from_file: Path | None = None,
    skip_backup: bool = False,
    fetch: Callable[[str, Path], None] | None = None,
    boot: Callable[..., int] | None = None,
    now: Callable[[], float] = time.time,
) -> UpgradeResult:
    """The end-to-end `farna-control upgrade` flow (FAR-675, ADR 031 Decision 7).

    Stage order (destructive last):

    1. Fetch the target bundle (or point --from-file at a local tarball);
       SIGNATURE-verify the release manifest (current+next trust store);
       verify the tarball sha256 against it BEFORE extraction.
    2. Pre-flight refusals BEFORE the swap: PG major vs PG_VERSION (hard,
       never auto-initdb - the manual pg_upgrade remediation), downgrade
       (DB revision ahead of the target head), quantified disk (incl. dump
       headroom).
    3. The ENFORCED pre-upgrade pg_dump (--skip-backup is the loud flag; a
       verified snapshot is still required, else the flow refuses).
    4. The service-aware stop (the FAR-674 systemd user unit when
       installed, else the foreground lock holder); then a live-process
       exe/cwd refusal naming every PID under the incumbent version dir.
    5. The atomic `current` symlink swap (POSIX rename(2) - Windows
       refuses at the seam, TODO(P3)) + upgrade.json written BEFORE the
       boot.
    6. Boot the new bundle's lifespan migrations via `launcher start`,
       gated on /healthz. After a HEALTHY gate the FAR-674 systemd unit is
       restarted (best-effort; without the unit the flow leaves the stack
       stopped and the refusal/runbook wording says so) - the boot child's
       own process group was torn down by the /healthz gate, so the resting
       state is deliberately the systemd-managed service, never an orphaned
       boot child.
    7. On failure: the hard refusal prints the snapshot path + the EXACT
       manual restore command restoring BOTH binary AND data (explicit
       forward-migrated-schema wording). NO automatic restore runs (the
       fast-follow design is recorded in ADR 031 Decision 7 - the message
       references it). Every post-stop abort restarts the stopped unit
       best-effort: a refusal must never leave the stack DARK.
    8. Retention (the last 2 version dirs; sweep caches; never the
       state.json-referenced incumbent or the re-run .prev-<ts> copies).
    """
    if target_version is not None:
        target_version = _validate_version_segment(_strip_bundle_prefix(target_version))
    assert_upgrade_platform()
    reserve_target = _resolve_current_target(install_root)
    previous_version = reserve_target.name
    scrub_os_environment()

    with tempfile.TemporaryDirectory(prefix="modulo-upgrade-") as temp:
        staging_dir = Path(temp)
        tarball, version, manifest_file, signature_file = _resolve_upgrade_assets(
            from_file=from_file,
            target_version=target_version,
            staging_dir=staging_dir,
            fetch=fetch,
        )
        release_manifest = _verify_release_stage(manifest_file, signature_file, tarball, version)

        _log.info("upgrade.release_manifest_verified release=%s", release_manifest.release)
        bundle_dir = extract_and_verify_bundle(tarball, staging_dir / "extracted")
        # Per-binary manifest check (post-extraction): the manifest covers the
        # major bundle binaries too - release artifacts get checked against the
        # files as they sit in the REAL bundle, not just the tarball.
        _verify_extracted_bundle_artifacts(bundle_dir, release_manifest, tarball.name)

        # -- Pre-flight refusals + enforced dump (BEFORE anything version-specific)
        snapshot_dir = _run_upgrade_preflight(
            skip_backup=skip_backup,
            data_dir=data_dir,
            reserve_target=reserve_target,
            bundle_dir=bundle_dir,
            release_manifest=release_manifest,
            tarball=tarball,
            version=version,
        )

        # -- The DESTRUCTIVE phase (stages 4-8) ---------------------------
        stop_running_stack(data_dir)
        try:
            no_live_process_inside(reserve_target)
            assert_not_held(data_dir)
            _target_dir, prior_dir = _move_into_place(bundle_dir, install_root, version)
            atomic_symlink_swap(install_root / "current", f"versions/{version}")
        except UpgradeError:
            _restart_unit_best_effort(why="post-stop abort: restoring the stopped service")
            raise
        marker_path = write_upgrade_marker(
            data_dir,
            previous_version=previous_version,
            version=version,
            pre_upgrade_snapshot=snapshot_dir,
            upgraded_at=now(),
        )
        _log.info("upgrade.marker_written path=%s", marker_path)
        boot_runner = boot or run_boot
        prior_link_target = prior_dir.name if prior_dir is not None else None
        try:
            boot_runner(_bundled_boot_argv(install_root, data_dir), api_port=_api_port_of(data_dir))
        except UpgradeError as exc:
            raise UpgradeError(
                f"{exc}\n\n"
                + restore_guidance(
                    snapshot=snapshot_dir,
                    previous_version=previous_version,
                    install_root=install_root,
                    data_dir=data_dir,
                    prior_link_target=prior_link_target,
                )
            ) from exc
        _restart_unit_best_effort(why="healthy post-upgrade boot gate passed: restoring the service")
        pruned = prune_versions(install_root, current_target=reserve_target)
        pruned += sweep_upgrade_caches(install_root)

    return UpgradeResult(
        previous_version=previous_version,
        version=version,
        snapshot=snapshot_dir,
        marker_path=marker_path,
        pruned=pruned,
    )


def _verify_release_stage(manifest_file: Path, signature_file: Path, tarball: Path, version: str) -> ReleaseManifest:
    """Signature-verify the manifest + tarball sha256; assert the release tag.

    A ManifestSecurityError (tampered/garbled/missing) surfaces as
    :class:`UpgradeError` - the CLI's error handling turns THAT into a clean
    ClickException, never a raw traceback. The manifest's release field must
    equal the requested bundle tag (never sign one version and ship another).
    """
    expected_release = f"bundle-v{version}"
    try:
        release_manifest = manifest_module.verify_manifest(manifest_file.read_bytes(), manifest_module.read_signature_file(signature_file))
        expected_tarball_sha = release_manifest.checksum_for(tarball.name)
        if release_manifest.release != expected_release:
            raise UpgradeError(f"the signed release manifest describes '{release_manifest.release}', not the requested bundle {expected_release} - refusing the cross-version mismatch")
        tarball_sha = _sha256_file(tarball)
    except ManifestSecurityError as exc:
        raise UpgradeError(f"release manifest verification FAILED: {exc}") from exc
    if tarball_sha != expected_tarball_sha:
        raise UpgradeError(f"the signed release manifest sha256 MISMATCH for {tarball.name}: expected {expected_tarball_sha}, got {tarball_sha} - do not use this download; report it at https://github.com/farnalabs/farna-control/issues")
    return release_manifest


def _verify_extracted_bundle_artifacts(bundle_dir: Path, release_manifest: ReleaseManifest, tarball_name: str) -> None:
    """Post-extraction per-binary manifest gate.

    The signed manifest covers the major bundled binaries (pg/postgres,
    redis-server, ...) IN the bundle: after extraction every one is checked
    in the real layout. The tarball entry is the pre-extraction check that
    already ran - it is skipped here (the tarball is not a bundle member).
    """
    try:
        bundle_side_manifest = replace(
            release_manifest,
            artifact_checksums={name: entry for name, entry in release_manifest.artifact_checksums.items() if name != tarball_name},
        )
        manifest_module.verify_artifacts(bundle_dir, bundle_side_manifest)
    except manifest_module.ManifestSecurityError as exc:
        raise UpgradeError(f"bundle-side manifest verification FAILED after extraction: {exc}") from exc


if __name__ == "__main__":
    sys.exit(main())
