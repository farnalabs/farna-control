"""FAR-2667: the plain-dump pipeline's error and edge paths.

These complement ``test_plain_dump.py``. The changed-lines coverage gate
measures the review-hardening lines in :mod:`farna.db.plain_dump`; the cases
below pin the branches the happy-path suite never reaches:

* the psycopg server-GUC probe (both its success shape and its refusal);
* a bare executable name that is not on PATH;
* an unparsed ``set_config`` preamble form that must still be dropped;
* the trailing-meta-command rewrite when the file is larger than the tail
  window (so the middle of the dump is streamed through), and its failure.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from farna.db import plain_dump
from farna.db.plain_dump import (
    DUMP_COMPLETE_MARKER,
    DumpError,
    _classify_preamble_line,
    _strip_trailing_meta_commands,
    resolve_pg_dump,
    server_guc_names,
)

_TRAILER = b"--\n-- PostgreSQL database dump\n--\n\n" + DUMP_COMPLETE_MARKER + b"\n\n"


def _context(value: Any) -> Any:
    class _Ctx:
        def __enter__(self) -> Any:
            return value

        def __exit__(self, *_exc: object) -> None:
            return None

    return _Ctx()


class _FakeCursor:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def cursor(self) -> Any:
        return _context(_FakeCursor(self._rows))


def _install_fake_psycopg(monkeypatch: pytest.MonkeyPatch, rows: list[Any] | None = None, *, raises: Exception | None = None) -> None:
    def _connect(*_args: object, **_kwargs: object) -> Any:
        if raises is not None:
            raise raises
        return _context(_FakeConnection(rows or []))

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=_connect))


class TestServerGucNames:
    def test_reads_and_filters_the_server_guc_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_psycopg(monkeypatch, [("statement_timeout",), (None,), ("lock_timeout",), (7,)])
        assert server_guc_names("postgresql://owner:secret@db:5432/farna_control") == {"statement_timeout", "lock_timeout"}

    def test_an_unreachable_server_raises_a_dump_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_psycopg(monkeypatch, raises=RuntimeError("connection refused"))
        with pytest.raises(DumpError, match="cannot read the target server's settings"):
            server_guc_names("postgresql://owner@db:5432/farna_control")


class TestResolvePgDump:
    def test_a_bare_name_missing_from_path_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        with pytest.raises(DumpError, match="Install the PostgreSQL client tools"):
            resolve_pg_dump("pg_dump")

    def test_an_explicit_path_is_taken_at_its_word(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: "/should/not/be/used")
        assert resolve_pg_dump("/opt/bundle/pg/bin/pg_dump") == "/opt/bundle/pg/bin/pg_dump"

    def test_a_bare_name_found_on_path_is_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/pg_dump")
        assert resolve_pg_dump("pg_dump") == "/usr/bin/pg_dump"


class TestPreambleClassification:
    def test_an_unparseable_set_config_is_dropped(self) -> None:
        assert _classify_preamble_line(b"SELECT pg_catalog.set_config('not-a-parseable-form')", set()) == ("drop", "<unparsed set_config>")


class TestTrailingMetaCommandRewrite:
    def test_large_dump_streams_the_middle_through_the_rewrite(self, tmp_path: Path) -> None:
        """A dump bigger than the tail window exercises the chunked copy."""
        padding = b"-- padding line to push the file past the tail window\n" * 2000
        payload = padding + _TRAILER + b"\\unrestrict abc123DEF\n"
        dump = tmp_path / "database.sql"
        dump.write_bytes(payload)
        assert dump.stat().st_size > 65536
        assert _strip_trailing_meta_commands(dump) is True
        rewritten = dump.read_bytes()
        assert b"\\unrestrict" not in rewritten
        assert rewritten.startswith(padding)
        assert rewritten.endswith(_TRAILER)

    def test_a_shrinking_source_stops_the_middle_copy(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A source that ends early must not spin: the copy breaks on a short read."""
        padding = b"-- padding line to push the file past the tail window\n" * 2000
        dump = tmp_path / "database.sql"
        dump.write_bytes(padding + _TRAILER + b"\\unrestrict abc123DEF\n")

        class _EmptySource:
            def read(self, _size: int) -> bytes:
                return b""

            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

        real_open = Path.open
        reads = {"count": 0}

        def _open(path: Path, *args: Any, **kwargs: Any) -> Any:
            if path == dump and args and args[0] == "rb":
                reads["count"] += 1
                if reads["count"] > 1:
                    return _EmptySource()
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _open)
        assert _strip_trailing_meta_commands(dump) is True
        assert b"\\unrestrict" not in dump.read_bytes()

    def test_a_failed_rewrite_raises_a_dump_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        dump = tmp_path / "database.sql"
        dump.write_bytes(_TRAILER + b"\\unrestrict abc123DEF\n")

        def _boom(_path: Path) -> Any:
            raise OSError("disk full")

        monkeypatch.setattr(plain_dump, "_open_private", _boom)
        with pytest.raises(DumpError, match="could not be rewritten"):
            _strip_trailing_meta_commands(dump)
