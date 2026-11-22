"""Plain-format PostgreSQL dumps: completeness proof and replay compatibility.

FAR-2667. Two concerns live here because BOTH dump producers
(``farna-control backup`` and the pre-upgrade snapshot) must agree on them:

1. **Completeness.** ``pg_dump --format=plain`` writes its completion trailer
   LAST. A dump that is cut short is still a syntactically valid SQL script, so
   "the file is non-empty" is not proof — the trailer is. ``-Fc`` has the same
   property via ``pg_restore --list``; this release keeps plain format (one
   format, replayable with psql, and existing archives stay readable), so the
   trailer is the equivalent proof.

2. **Replay compatibility.** pg_dump emits the settings of the CLIENT it was
   built from as a preamble. A client NEWER than the server writes settings the
   server does not have — observed: pg_dump 17 writes
   ``SET transaction_timeout = 0;`` and a PostgreSQL 16 server rejects it with
   ``unrecognized configuration parameter``, which makes the dump unrestorable
   into the very server it was taken from. The preamble is therefore rewritten
   to drop settings the target server does not know. Only the PREAMBLE is
   touched: the rewrite stops at the first real statement, so a function body
   that happens to contain a ``SET`` line is never modified.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import BinaryIO

#: pg_dump's own completion trailer for a plain (-Fp) dump.
DUMP_COMPLETE_MARKER = b"-- PostgreSQL database dump complete"
#: pg_dump custom-format archives start with this magic; refused by name.
PGDMP_MAGIC = b"PGDMP"
_TAIL_BYTES = 4096
_PRIVATE_MODE = 0o600

_PREAMBLE_SET = re.compile(rb"^SET ([A-Za-z_][A-Za-z0-9_]*) = ")
_PREAMBLE_PREFIXES = (b"--", b"\\")
_PREAMBLE_SELECT = b"SELECT pg_catalog.set_config("


def _read_head_and_tail(dump_path: Path) -> tuple[bytes, bytes, int]:
    try:
        size = dump_path.stat().st_size
        with dump_path.open("rb") as handle:
            head = handle.read(len(PGDMP_MAGIC))
            handle.seek(max(0, size - _TAIL_BYTES))
            tail = handle.read(_TAIL_BYTES)
    except OSError as exc:
        raise ValueError(f"the dump at {dump_path} cannot be read back for verification: {exc}") from exc
    return head, tail, size


def assert_complete(dump_path: Path) -> int:
    """Prove a plain-format dump finished; return its size in bytes.

    Raises :class:`ValueError` with an actionable message — callers translate
    it into their own error type. A custom-format archive is refused by NAME so
    a ``-Fc`` artifact dropped into a plain-SQL restore path says so instead of
    failing inside psql.
    """
    head, tail, size = _read_head_and_tail(dump_path)
    if size <= 0:
        raise ValueError(f"the dump at {dump_path} is EMPTY")
    if head == PGDMP_MAGIC:
        raise ValueError(f"{dump_path.name} is a pg_dump CUSTOM-format archive (PGDMP); this restore path replays plain SQL dumps only")
    if DUMP_COMPLETE_MARKER not in tail:
        raise ValueError(f"the dump at {dump_path} is INCOMPLETE — pg_dump's completion marker ({DUMP_COMPLETE_MARKER.decode()!r}) is missing from its final {_TAIL_BYTES} bytes, so the dump did not finish (truncated write, killed process or a full disk)")
    return size


def server_guc_names(db_url: str, *, timeout_seconds: int = 15) -> set[str]:
    """Every GUC the TARGET server knows (``pg_settings``), as text names.

    Raises :class:`ValueError` when the server cannot be reached: without the
    list the compatibility rewrite cannot be applied safely, and guessing is
    worse than refusing.
    """
    import psycopg

    try:
        with psycopg.connect(db_url, connect_timeout=timeout_seconds) as conn, conn.cursor() as cur:
            cur.execute("SELECT name FROM pg_settings")
            rows = cur.fetchall()
    except Exception as exc:
        raise ValueError(f"cannot read the target server's settings for the dump compatibility rewrite: {exc}") from exc
    return {row[0] for row in rows if row and isinstance(row[0], str)}


def strip_unsupported_preamble_settings(dump_path: Path, supported: set[str]) -> list[str]:
    """Drop preamble ``SET`` statements naming a GUC the server does not have.

    Returns the removed GUC names (empty when the dump needed no rewrite, which
    is the normal case for a client whose major matches the server). The file
    is rewritten only when something was removed, byte-for-byte otherwise.
    """
    raw = dump_path.read_bytes()
    if b"SET " not in raw:
        return []
    kept: list[bytes] = []
    removed: list[str] = []
    in_preamble = True
    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if in_preamble:
            is_preamble_line = not stripped or stripped.startswith(_PREAMBLE_PREFIXES) or stripped.startswith(_PREAMBLE_SELECT)
            match = _PREAMBLE_SET.match(stripped)
            if match is not None:
                name = match.group(1).decode("ascii")
                if name not in supported:
                    removed.append(name)
                    continue
                kept.append(line)
                continue
            if not is_preamble_line:
                in_preamble = False
        kept.append(line)
    if not removed:
        return []
    payload = b"".join(kept)
    fd = _open_private(dump_path)
    with fd:
        fd.write(payload)
    return removed


def _open_private(path: Path) -> BinaryIO:
    """Open *path* for truncating write at 0600 (the dump stays private)."""
    handle_fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
    return os.fdopen(handle_fd, "wb")
