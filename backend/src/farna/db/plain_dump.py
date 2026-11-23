"""Plain-format PostgreSQL dumps: the ONE producer, completeness proof, replay fixes.

FAR-2667. Three concerns live here because BOTH dump producers
(``farna-control backup`` and the pre-upgrade upgrade snapshot) must agree on
them — and, after review, because a pipeline implemented twice drifts:

1. **The pipeline itself.** :func:`dump_plain_sql` is the only code that runs
   ``pg_dump``. It pins the argv, creates the output 0600 (a mid-dump SIGKILL
   never leaves the SQL readable at umask mode), applies the replay rewrite,
   then proves the dump complete — in that order, once.

2. **Completeness.** ``pg_dump --format=plain`` writes its completion trailer
   LAST. A dump that is cut short is still a syntactically valid SQL script, so
   "the file is non-empty" is not proof — the trailer is. ``-Fc`` has the same
   property via ``pg_restore --list``; this release keeps plain format (one
   format, replayable with psql, and existing archives stay readable), so the
   trailer is the equivalent proof. The trailer must be the FINAL non-empty
   line, not a substring somewhere in the tail: a dump truncated inside COPY
   data whose last row happens to contain the trailer text would otherwise
   pass.

3. **Replay compatibility.** pg_dump emits the settings of the CLIENT it was
   built from as a preamble. A client NEWER than the server writes settings the
   server does not have — observed: pg_dump 17 writes
   ``SET transaction_timeout = 0;`` (and the equivalent
   ``SELECT pg_catalog.set_config(...)`` form) and a PostgreSQL 16 server
   rejects it with ``unrecognized configuration parameter``, so the dump is
   unrestorable into the very server it came from. pg_dump 17 also wraps the
   whole dump in ``\\restrict`` / ``\\unrestrict`` psql meta-commands, which an
   OLDER psql rejects outright. The preamble is therefore rewritten to drop
   settings the target server does not know and the meta-commands are removed
   explicitly. Only the PREAMBLE is rewritten line-by-line: the copy stops at
   the first real statement, so a function body that happens to contain a
   ``SET`` line is never modified. The rewrite STREAMS (a multi-GB dump must
   not be read into memory) via a 0600 temp file and an atomic replace.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess  # nosec B404 — every exec below is a fully argv-pinned pg_dump call (never shell=True)
import tempfile
from pathlib import Path
from typing import BinaryIO

#: pg_dump's own completion trailer for a plain (-Fp) dump.
DUMP_COMPLETE_MARKER = b"-- PostgreSQL database dump complete"
#: pg_dump custom-format archives start with this magic; refused by name.
PGDMP_MAGIC = b"PGDMP"
#: The dump is read back from its END only; 64 KiB is far more than the trailer
#: plus the trailing psql meta-commands a newer pg_dump appends, and small
#: enough that a truncated megabyte dump is never mistaken for a whole one.
_TAIL_BYTES = 65536
_HEADER_BYTES = 64
_PRIVATE_MODE = 0o600

_PREAMBLE_SET = re.compile(rb"^SET ([A-Za-z_][A-Za-z0-9_.]*) = ")
# pg_dump >= 17 writes some GUCs through set_config instead of SET.
_PREAMBLE_SET_CONFIG = re.compile(rb"^SELECT pg_catalog\.set_config\(\s*'([A-Za-z_][A-Za-z0-9_.]*)'\s*,")
# psql meta-commands pg_dump 17 wraps the dump in; an older psql rejects them.
_META_COMMAND = re.compile(rb"^\\(?:restrict|unrestrict)(?:\s+\S+)?\s*$")
_PREAMBLE_PREFIXES = (b"--", b"\\")
_PREAMBLE_SELECT = b"SELECT pg_catalog.set_config("
_DUMP_TIMEOUT_SECONDS = 1800


class DumpError(ValueError):
    """A dump could not be produced, or cannot be proven complete/replayable.

    A ``ValueError`` so callers that already translated the plain-``ValueError``
    contract of :func:`assert_complete` keep working unchanged.
    """


def _needs_path_lookup(executable: str) -> bool:
    return not any(sep and sep in executable for sep in (os.sep, os.altsep))


def resolve_pg_dump(executable: str = "pg_dump") -> str:
    """Return the pg_dump to exec, or raise :class:`DumpError` naming the fix.

    A caller that names a PATH (the native bundle passes its own
    ``pg/ bin/pg_dump``) is taken at its word — the directory is a statement of
    where the binary is. A bare name is looked up so a missing client fails with
    "install the PostgreSQL client tools" rather than an ENOENT traceback.
    """
    if not _needs_path_lookup(executable):
        return executable
    found = shutil.which(executable)
    if found is None:
        raise DumpError(f"Required system tool {executable!r} not found on PATH. Install the PostgreSQL client tools.")
    return found


def _read_head_and_tail(dump_path: Path) -> tuple[bytes, bytes, int]:
    try:
        size = dump_path.stat().st_size
        with dump_path.open("rb") as handle:
            head = handle.read(_HEADER_BYTES)
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read(_TAIL_BYTES)
    except OSError as exc:
        raise DumpError(f"the dump at {dump_path} cannot be read back for verification: {exc}") from exc
    return head, tail, size


def _last_non_meta_line(tail: bytes) -> bytes | None:
    """The last meaningful line of *tail*.

    "Meaningful" skips the lines pg_dump appends AFTER the completion marker:
    a bare ``--`` comment separator and the ``\\unrestrict <token>`` psql
    meta-command that a newer pg_dump (17+) wraps a dump in. The real layout at
    the end of a pg_dump 17 plain dump is::

        --
        -- PostgreSQL database dump complete
        --
        \\unrestrict <token>

    so the marker is the last non-empty COMMENT-BODY line, not literally the
    last line of the file.
    """
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    index = len(lines) - 1
    while index >= 0 and (lines[index] == b"--" or _META_COMMAND.match(lines[index])):
        index -= 1
    return lines[index] if index >= 0 else None


def assert_complete(dump_path: Path) -> int:
    """Prove a plain-format dump finished; return its size in bytes.

    Raises :class:`DumpError` (a ``ValueError``) with an actionable message —
    callers translate it into their own error type. A custom-format archive is
    refused by NAME so a ``-Fc`` artifact dropped into a plain-SQL restore path
    says so instead of failing inside psql.

    The trailer must be the FINAL non-empty line (trailing
    ``\\unrestrict``-style meta-commands excepted). A substring match would be
    satisfied by a dump truncated inside COPY data whose data happens to
    contain the trailer text as a value — see the module docstring.
    """
    head, tail, size = _read_head_and_tail(dump_path)
    if size <= 0:
        raise DumpError(f"the dump at {dump_path} is EMPTY")
    if head.startswith(PGDMP_MAGIC):
        raise DumpError(f"{dump_path.name} is a pg_dump CUSTOM-format archive (PGDMP); this restore path replays plain SQL dumps only")
    last = _last_non_meta_line(tail)
    if last != DUMP_COMPLETE_MARKER:
        last_repr = last.decode(errors="replace")[:120] if last is not None else "<no non-empty line>"
        raise DumpError(f"the dump at {dump_path} is INCOMPLETE — pg_dump's completion marker ({DUMP_COMPLETE_MARKER.decode()!r}) is not the final non-empty line of the file (last non-empty line: {last_repr!r}), so the dump did not finish (truncated write, killed process or a full disk)")
    return size


def server_guc_names(db_url: str, *, timeout_seconds: int = 15) -> set[str]:
    """Every GUC the TARGET server knows (``pg_settings``), as text names.

    Raises :class:`DumpError` when the server cannot be reached: without the
    list the compatibility rewrite cannot be applied safely, and guessing is
    worse than refusing. The password never reaches the DSN (and therefore
    never an error message) — see :func:`farna.db.url_utils.psycopg_connect_args`.
    """
    import psycopg

    from farna.db.url_utils import psycopg_connect_args

    dsn, password = psycopg_connect_args(db_url)
    try:
        with psycopg.connect(dsn, connect_timeout=timeout_seconds, password=password) as conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM pg_settings")
            rows = cur.fetchall()
    except Exception as exc:
        raise DumpError(f"cannot read the target server's settings for the dump compatibility rewrite: {exc}") from exc
    return {row[0] for row in rows if row and isinstance(row[0], str)}


def _classify_preamble_line(stripped: bytes, supported: set[str]) -> tuple[str, str | None] | None:
    """Classify one REWRITE-RELEVANT preamble line (``None`` when it is not one).

    Returns ``("keep" | "drop", token)`` for a ``SET`` / ``set_config`` /
    ``\\restrict`` line and ``None`` for everything else, so the callers can
    tell "a preamble line that needs a decision" from "a comment or the first
    real statement".
    """
    meta = _META_COMMAND.match(stripped)
    if meta is not None:
        return "drop", stripped.split()[0].decode("ascii")
    for pattern in (_PREAMBLE_SET, _PREAMBLE_SET_CONFIG):
        match = pattern.match(stripped)
        if match is not None:
            name = match.group(1).decode("ascii")
            return ("keep", name) if name in supported else ("drop", name)
    if stripped.startswith(_PREAMBLE_SELECT):
        # A set_config form we cannot parse names a GUC we cannot check, so it
        # is never passed through blindly: it is dropped from the preamble.
        return "drop", "<unparsed set_config>"
    return None


def _is_preamble_only_line(stripped: bytes) -> bool:
    """A comment / blank line that keeps the preamble open."""
    return not stripped or stripped.startswith(_PREAMBLE_PREFIXES)


def _preamble_rewrites(dump_path: Path, supported: set[str]) -> list[str]:
    """Walk only the PREAMBLE, collecting the lines that must be removed."""
    removed: list[str] = []
    with dump_path.open("rb") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            decision = _classify_preamble_line(stripped, supported)
            if decision is not None:
                action, token = decision
                if action == "drop" and token is not None:
                    removed.append(token)
                continue
            if _is_preamble_only_line(stripped):
                continue
            break  # the first real statement ends the preamble
    return removed


def _strip_preamble_streaming(dump_path: Path, supported: set[str]) -> list[str]:
    """Rewrite the preamble in a streaming pass; returns the removed tokens.

    The file is copied to a 0600 temp sibling (line by line — never whole-file
    in memory) skipping the removed preamble lines, then atomically replaced.
    Untouched files are never rewritten.
    """
    removed = _preamble_rewrites(dump_path, supported)
    if not removed:
        return []
    fd, temp_name = tempfile.mkstemp(prefix=f".{dump_path.name}.", suffix=".rewrite", dir=str(dump_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        seen_statement = False
        with dump_path.open("rb") as source, _open_private(temp_path) as target:
            for raw_line in source:
                if not seen_statement:
                    stripped = raw_line.strip()
                    decision = _classify_preamble_line(stripped, supported)
                    if decision is not None:
                        if decision[0] == "drop":
                            continue
                        target.write(raw_line)
                        continue
                    if _is_preamble_only_line(stripped):
                        target.write(raw_line)
                        continue
                    seen_statement = True
                target.write(raw_line)
        temp_path.replace(dump_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise DumpError(f"the dump at {dump_path} could not be rewritten for replay compatibility: {exc}") from exc
    return removed


def _strip_trailing_meta_commands(dump_path: Path) -> bool:
    """Drop a trailing ``\\unrestrict <token>`` (pg_dump 17+); True when changed.

    Only whole lines inside the FINAL ``_TAIL_BYTES`` are considered, so a
    meta-command-shaped string inside COPY data earlier in the file is never
    touched. The file is rewritten through a 0600 temp sibling and atomically
    replaced — never read into memory.
    """
    size = dump_path.stat().st_size
    start = max(0, size - _TAIL_BYTES)
    with dump_path.open("rb") as handle:
        handle.seek(start)
        tail = handle.read(_TAIL_BYTES)
    lines = tail.splitlines(keepends=True)
    kept = [line for line in lines if not _META_COMMAND.match(line.strip())]
    if len(kept) == len(lines):
        return False
    fd, temp_name = tempfile.mkstemp(prefix=f".{dump_path.name}.", suffix=".tail", dir=str(dump_path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with dump_path.open("rb") as source, _open_private(temp_path) as target:
            remaining = start
            while remaining > 0:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    break
                target.write(chunk)
                remaining -= len(chunk)
            target.write(b"".join(kept))
        temp_path.replace(dump_path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp_path.unlink()
        raise DumpError(f"the dump at {dump_path} could not be rewritten for an older psql: {exc}") from exc
    return True


def strip_unsupported_preamble_settings(dump_path: Path, supported: set[str]) -> list[str]:
    """Drop replay-incompatible preamble statements; return the removed tokens.

    Removed: ``SET <guc> = ...`` / ``SELECT pg_catalog.set_config('<guc>', ...)``
    naming a GUC the target server does not have, and the ``\\restrict`` /
    ``\\unrestrict`` psql meta-commands pg_dump 17 wraps a dump in (an older
    psql rejects them). The list is empty when nothing needed rewriting.
    """
    removed = _strip_preamble_streaming(dump_path, supported)
    if _strip_trailing_meta_commands(dump_path):
        removed.append("\\unrestrict")
    return removed


def _open_private(path: Path) -> BinaryIO:
    """Open *path* for truncating write at 0600 (the dump stays private)."""
    handle_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    return os.fdopen(handle_fd, "wb")


def dump_plain_sql(
    db_url: str,
    output: Path,
    *,
    pg_dump: str = "pg_dump",
    timeout_seconds: int = _DUMP_TIMEOUT_SECONDS,
) -> int:
    """The ONE plain-dump pipeline both producers call; returns the dump size.

    Order is part of the contract: dump -> replay rewrite -> completeness
    proof. The rewrite happens before the proof so the file that is verified is
    the file a restore will replay. The dump URL is normalised to a libpq
    scheme and stripped of its password (it goes to the child as ``PGPASSWORD``
    only), so a SQLAlchemy ``postgresql+asyncpg://`` URL works here too.
    """
    from farna.db.url_utils import normalise_libpq_url, pg_child_env, split_postgres_credentials

    executable = resolve_pg_dump(pg_dump)
    dsn, password = split_postgres_credentials(normalise_libpq_url(db_url))
    argv = [executable, "--clean", "--if-exists", "--no-owner", "--no-acl", "--format=plain", dsn]
    fd = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            completed = subprocess.run(  # nosec B603 — pinned argv, trusted synthesized input  # noqa: S603  # NOSONAR S8705 — argv[0] is resolve_pg_dump()'s validated path, the flags are literals, and dsn comes from the operator's own DATABASE_URL; the executable is a trusted path from this binary, never an untrusted caller-supplied CLI argument, and there is no shell
                argv,
                stdout=out,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
                env=pg_child_env(password),
            )
    except subprocess.TimeoutExpired as exc:
        raise DumpError(f"pg_dump timed out after {timeout_seconds}s") from exc
    if completed.returncode != 0:
        stderr_text = completed.stderr.decode(errors="replace").strip()
        raise DumpError(stderr_text or f"pg_dump exited with code {completed.returncode}")
    remove_unsupported = strip_unsupported_preamble_settings(output, server_guc_names(db_url))
    if remove_unsupported:
        import logging

        logging.getLogger(__name__).warning("dump.preamble_rewritten removed=%s", ",".join(remove_unsupported))
    return assert_complete(output)
