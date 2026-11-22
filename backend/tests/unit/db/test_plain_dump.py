"""FAR-2667: the shared plain-dump pipeline — completeness and replay fixes.

These tests pin the behaviours the review proved passable before:

* the completion trailer must be the FINAL non-empty line (a dump truncated
  inside COPY data whose last row holds the trailer text must be refused);
* the tail window is sized so a trailing comment block cannot hide a missing
  trailer;
* the preamble rewrite handles ``SELECT pg_catalog.set_config(...)`` and the
  ``\\restrict`` / ``\\unrestrict`` psql meta-commands a newer pg_dump emits;
* the rewrite STREAMS (a multi-GB dump must never be read into memory);
* the one pipeline normalises a SQLAlchemy ``+asyncpg`` URL and keeps the
  password out of both argv and the child's DSN.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from farna.db import plain_dump
from farna.db.plain_dump import (
    DUMP_COMPLETE_MARKER,
    DumpError,
    assert_complete,
    dump_plain_sql,
    strip_unsupported_preamble_settings,
)

_TRAILER = b"--\n-- PostgreSQL database dump\n--\n\n" + DUMP_COMPLETE_MARKER + b"\n\n"


def _dump(tmp_path: Path, payload: bytes) -> Path:
    path = tmp_path / "database.sql"
    path.write_bytes(payload)
    return path


class TestCompletenessIsTheFinalLine:
    def test_a_clean_dump_passes(self, tmp_path: Path) -> None:
        path = _dump(tmp_path, _TRAILER)
        assert assert_complete(path) == len(_TRAILER)

    def test_the_trailer_mid_file_with_trailing_junk_is_refused(self, tmp_path: Path) -> None:
        """The exact bypass the review demonstrated: truncation inside COPY data
        whose last value happens to be the trailer text."""
        payload = b"COPY public.t (c) FROM stdin;\n-- PostgreSQL database dump complete\nanother data row\n"
        with pytest.raises(DumpError, match="INCOMPLETE"):
            assert_complete(_dump(tmp_path, payload))

    def test_a_long_trailing_comment_block_cannot_hide_the_missing_trailer(self, tmp_path: Path) -> None:
        payload = b"-- PostgreSQL database dump complete\n" + (b"-- padding comment line\n" * 5000)
        with pytest.raises(DumpError, match="INCOMPLETE"):
            assert_complete(_dump(tmp_path, payload))

    def test_a_truncated_dump_is_refused(self, tmp_path: Path) -> None:
        payload = b"--\n-- PostgreSQL database dump\n--\n\nCREATE TABLE t (id int);\n"
        with pytest.raises(DumpError, match="INCOMPLETE"):
            assert_complete(_dump(tmp_path, payload))

    def test_pg_dump_17_unrestrict_after_the_trailer_is_accepted(self, tmp_path: Path) -> None:
        payload = _TRAILER + b"\\unrestrict abc123DEF\n"
        assert assert_complete(_dump(tmp_path, payload)) == len(payload)

    def test_the_real_pg_dump_17_footer_is_accepted(self, tmp_path: Path) -> None:
        """The shipped layout: the marker is followed by a ``--`` separator and
        ``\\unrestrict`` — found by the Docker proof (FAR-2667 review round)."""
        payload = b"\\restrict abc\n--\n-- PostgreSQL database dump\n--\n\nSELECT 1;\n--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict abc\n"
        assert assert_complete(_dump(tmp_path, payload)) == len(payload)

    def test_an_empty_dump_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(DumpError, match="EMPTY"):
            assert_complete(_dump(tmp_path, b""))

    def test_a_custom_format_archive_is_refused_by_name(self, tmp_path: Path) -> None:
        with pytest.raises(DumpError, match="CUSTOM-format"):
            assert_complete(_dump(tmp_path, b"PGDMP" + b"\x00" * 64))


class TestPreambleRewrite:
    def test_set_config_naming_an_unknown_guc_is_dropped(self, tmp_path: Path) -> None:
        """A newer client can emit set_config instead of SET (M3)."""
        payload = b"--\n-- PostgreSQL database dump\n--\n\nSELECT pg_catalog.set_config('transaction_timeout', '0', false);\nSELECT pg_catalog.set_config('statement_timeout', '0', false);\n\nSELECT 1;\n" + _TRAILER
        path = _dump(tmp_path, payload)
        removed = strip_unsupported_preamble_settings(path, {"statement_timeout"})
        assert removed == ["transaction_timeout"]
        text = path.read_text(encoding="utf-8")
        assert "transaction_timeout" not in text
        assert "pg_catalog.set_config('statement_timeout'" in text
        assert "SELECT 1;" in text

    def test_restrict_and_unrestrict_meta_commands_are_removed(self, tmp_path: Path) -> None:
        payload = b"\\restrict abc123\n--\n-- PostgreSQL database dump\n--\n\nSELECT 1;\n" + _TRAILER + b"\\unrestrict abc123\n"
        path = _dump(tmp_path, payload)
        removed = strip_unsupported_preamble_settings(path, set())
        assert "\\restrict" in removed
        assert "\\unrestrict" in removed
        text = path.read_text(encoding="utf-8")
        assert "\\restrict" not in text
        assert "\\unrestrict" not in text
        # The completeness marker must survive the rewrite.
        assert assert_complete(path) == len(text.encode("utf-8"))

    def test_a_set_inside_a_function_body_is_never_touched(self, tmp_path: Path) -> None:
        payload = b"--\n-- PostgreSQL database dump\n--\n\nCREATE FUNCTION f() RETURNS void AS $$\nSET transaction_timeout = 0;\n$$ LANGUAGE sql;\n" + _TRAILER
        path = _dump(tmp_path, payload)
        assert not strip_unsupported_preamble_settings(path, set())
        assert b"SET transaction_timeout = 0;" in path.read_bytes()

    def test_the_rewrite_streams_and_never_reads_the_whole_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """M9: a multi-GB dump must not be pulled into memory."""
        payload = b"SET transaction_timeout = 0;\nSELECT 1;\n" + _TRAILER
        path = _dump(tmp_path, payload)

        def _boom(_self: Path) -> bytes:
            raise AssertionError("the dump must be streamed, not read into memory")

        monkeypatch.setattr(Path, "read_bytes", _boom)
        assert strip_unsupported_preamble_settings(path, set()) == ["transaction_timeout"]
        with Path(path).open("rb") as handle:
            assert b"transaction_timeout" not in handle.read()


class TestSharedPipeline:
    def test_dump_plain_sql_normalises_the_url_and_hides_the_password(self, tmp_path: Path) -> None:
        output = tmp_path / "database.sql"
        seen: dict[str, Any] = {}

        def _run(argv: list[str], **kwargs: Any) -> MagicMock:
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(_TRAILER)
            return MagicMock(returncode=0, stderr=b"")

        with patch.object(plain_dump, "server_guc_names", lambda _url, **_kw: set()), patch("farna.db.plain_dump.subprocess.run", side_effect=_run):
            size = dump_plain_sql("postgresql+asyncpg://owner:sup3r@db:5432/farna_control", output, pg_dump="/usr/bin/pg_dump")

        assert size == len(_TRAILER)
        assert seen["argv"][0] == "/usr/bin/pg_dump"
        assert seen["argv"][-1] == "postgresql://owner@db:5432/farna_control"
        assert "sup3r" not in " ".join(seen["argv"])
        assert seen["env"]["PGPASSWORD"] == "sup3r"

    def test_dump_plain_sql_refuses_a_truncated_dump(self, tmp_path: Path) -> None:
        def _run(_argv: list[str], **kwargs: Any) -> MagicMock:
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(b"CREATE TABLE t (id int);\n")
            return MagicMock(returncode=0, stderr=b"")

        with patch.object(plain_dump, "server_guc_names", lambda _url, **_kw: set()), patch("farna.db.plain_dump.subprocess.run", side_effect=_run), pytest.raises(DumpError, match="INCOMPLETE"):
            dump_plain_sql("postgresql://owner@db:5432/x", tmp_path / "database.sql", pg_dump="/usr/bin/pg_dump")

    def test_dump_plain_sql_reports_a_failed_client(self, tmp_path: Path) -> None:
        def _run(_argv: list[str], **_kwargs: Any) -> MagicMock:
            return MagicMock(returncode=1, stderr=b"pg_dump: connection to server failed")

        with patch("farna.db.plain_dump.subprocess.run", side_effect=_run), pytest.raises(DumpError, match="connection to server failed"):
            dump_plain_sql("postgresql://owner@db:5432/x", tmp_path / "database.sql", pg_dump="/usr/bin/pg_dump")

    def test_dump_plain_sql_times_out(self, tmp_path: Path) -> None:
        def _run(_argv: list[str], **kwargs: Any) -> MagicMock:
            raise subprocess.TimeoutExpired(cmd="pg_dump", timeout=kwargs.get("timeout"))

        with patch("farna.db.plain_dump.subprocess.run", side_effect=_run), pytest.raises(DumpError, match="timed out"):
            dump_plain_sql("postgresql://owner@db:5432/x", tmp_path / "database.sql", pg_dump="/usr/bin/pg_dump")
