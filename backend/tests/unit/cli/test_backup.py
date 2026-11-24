"""Tests for the farna-control backup/restore CLI tool."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote_plus

import click
import psycopg
import pytest
from click.testing import CliRunner
from psycopg.types.json import Jsonb

from farna.cli.backup import (
    _db_has_modulo_tables,
    _export_checkpoint_blobs_sync,
    _export_checkpoint_writes_sync,
    _export_checkpoints_sync,
    _export_credentials_references_sync,
    _fernet_key_hash,
    _file_checksum,
    _human_size,
    _re_encrypt_credentials_sync,
    _resolve_url,
    _restore_checkpoint_blobs_sync,
    _restore_checkpoint_writes_sync,
    _restore_checkpoints_sync,
    _run_pg_dump,
    _run_psql,
    _serialise_for_json,
    _write_json,
    cli,
)

_TEST_APP_DB_URL = "postgresql+asyncpg://modulo:pw@127.0.0.1:5432/farna_control"

#: A COMPLETE plain pg_dump: the completion trailer is what proves the dump
#: finished (FAR-2667). Fixtures write this instead of an arbitrary string so
#: they model an archive the real producer emits.
_COMPLETE_DUMP = b"-- SQL dump\n-- PostgreSQL database dump complete\n"

#: A dump whose write was cut short: no completion trailer.
_TRUNCATED_DUMP = b"-- SQL dump\nCREATE TABLE t (id int);\n"


def _complete_dump_run(argv: list[str], **kwargs: Any) -> MagicMock:
    """A fake pg_dump that streams a COMPLETE dump to the open file handle."""
    stdout = kwargs.get("stdout")
    if stdout is not None:
        stdout.write(_COMPLETE_DUMP)
    return MagicMock(returncode=0, stderr=b"")


def _truncated_dump_run(argv: list[str], **kwargs: Any) -> MagicMock:
    """A fake pg_dump that exits 0 after writing only part of the dump."""
    stdout = kwargs.get("stdout")
    if stdout is not None:
        stdout.write(_TRUNCATED_DUMP)
    return MagicMock(returncode=0, stderr=b"")


@pytest.fixture(autouse=True)
def _pin_server_gucs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the target server's GUC list: these tests never touch a database.

    `transaction_timeout` is deliberately ABSENT so a fixture dump that
    contains it exercises the compatibility rewrite.
    """
    from farna.db import plain_dump

    monkeypatch.setattr(
        plain_dump,
        "server_guc_names",
        lambda _url, **_kwargs: {"statement_timeout", "lock_timeout", "client_encoding", "row_security"},
    )


def _write_restorable_dump(archive: Path, *, content: bytes = _COMPLETE_DUMP) -> None:
    """Write database.sql and record its checksum in the archive's manifest.

    FAR-2667 makes restore refuse a dump the manifest records no checksum for
    (a foreign or hand-assembled archive), so every restorable fixture must
    cover its own dump — exactly as `farna-control backup` does.
    """
    (archive / "database.sql").write_bytes(content)
    manifest_path = archive / "backup-info.json"
    if not manifest_path.exists():
        # The test writes the manifest itself afterwards (it supplies its own
        # checksum map), so leave the ordering to it.
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("file_checksums", {})["database.sql"] = hashlib.sha256(content).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


# ── _resolve_url ──────────────────────────────────────────────────────────────


class TestResolveUrl:
    def test_strips_asyncpg_prefix(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost:5432/db"
        assert _resolve_url(url) == "postgresql://user:pass@localhost:5432/db"

    def test_strips_psycopg_prefix(self) -> None:
        url = "postgresql+psycopg://user:pass@localhost:5432/db"
        assert _resolve_url(url) == "postgresql://user:pass@localhost:5432/db"

    def test_plain_postgresql_passthrough(self) -> None:
        url = "postgresql://user:pass@localhost:5432/db"
        assert _resolve_url(url) == url

    def test_a_non_postgres_scheme_is_refused(self) -> None:
        # The result feeds pg_dump/psql/psycopg, which speak libpq only: a
        # non-Postgres URL used to pass through and fail deep inside a driver.
        # It is refused here with one legible line instead.
        import click

        with pytest.raises(click.ClickException, match="PostgreSQL"):
            _resolve_url("sqlite:///local.db")

    @patch("farna.cli.backup.get_settings")
    def test_none_fallback(self, mock_settings: MagicMock) -> None:
        mock_settings.return_value.database_url = "postgresql+asyncpg://fallback/db"
        result = _resolve_url(None)
        assert result == "postgresql://fallback/db"


# ── database-table presence probe ─────────────────────────────────────────────


class TestDbHasModuloTables:
    @patch("psycopg.connect")
    def test_reports_populated_database(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_cur.fetchone.return_value = (3,)

        assert _db_has_modulo_tables("postgresql://localhost/db") is True

        sql = mock_cur.execute.call_args[0][0]
        assert "information_schema.tables" in sql

    @patch("psycopg.connect")
    def test_reports_empty_database(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn
        mock_cur.fetchone.return_value = (0,)

        assert _db_has_modulo_tables("postgresql://localhost/db") is False

    @patch("psycopg.connect", side_effect=RuntimeError("connection refused"))
    def test_treats_probe_failure_as_populated(self, mock_connect: MagicMock) -> None:
        # Cannot verify the target ⇒ conservative True (require --replace-cluster).
        assert _db_has_modulo_tables("postgresql://localhost/db") is True

    @patch("farna.cli.backup.psycopg", None)
    def test_treats_missing_driver_as_populated(self) -> None:
        assert _db_has_modulo_tables("postgresql://localhost/db") is True


# ── _fernet_key_hash ──────────────────────────────────────────────────────────


class TestFernetKeyHash:
    def test_deterministic(self) -> None:
        # Golden value pins the exact digest prefix so a change in the hash
        # algorithm or truncation fails loudly instead of silently altering
        # the key fingerprint used by the backup manifest.
        assert _fernet_key_hash("test-key-1234") == "da742ddc966de5b0"

    def test_length(self) -> None:
        h = _fernet_key_hash("any-key")
        assert len(h) == 16

    def test_different_keys_produce_different_hashes(self) -> None:
        assert _fernet_key_hash("key-a") != _fernet_key_hash("key-b")

    def test_empty_key(self) -> None:
        h = _fernet_key_hash("")
        assert isinstance(h, str)
        assert len(h) == 16


# ── _serialise_for_json ──────────────────────────────────────────────────────


class TestSerialiseForJson:
    def test_uuid_to_str(self) -> None:
        uid = uuid.uuid4()
        result = _serialise_for_json(uid)
        assert result == str(uid)
        assert isinstance(result, str)

    def test_bytes_to_hex(self) -> None:
        result = _serialise_for_json(b"\x00\xff\xab")
        assert result == "00ffab"

    def test_bytes_empty(self) -> None:
        result = _serialise_for_json(b"")
        assert result == ""

    def test_datetime_to_isoformat(self) -> None:
        dt = datetime.now(UTC)
        result = _serialise_for_json(dt)
        assert result == dt.isoformat()

    def test_int_passthrough(self) -> None:
        assert _serialise_for_json(42) == 42

    def test_float_passthrough(self) -> None:
        assert _serialise_for_json(3.14) == pytest.approx(3.14)

    def test_str_passthrough(self) -> None:
        assert _serialise_for_json("hello") == "hello"

    def test_list_passthrough(self) -> None:
        val = [1, 2, 3]
        assert _serialise_for_json(val) is val

    def test_dict_passthrough(self) -> None:
        val = {"a": 1}
        assert _serialise_for_json(val) is val

    def test_none_passthrough(self) -> None:
        assert _serialise_for_json(None) is None


# ── _human_size ──────────────────────────────────────────────────────────────


class TestHumanSize:
    def test_zero_bytes(self) -> None:
        assert _human_size(0) == "0.0 B"

    def test_bytes(self) -> None:
        assert _human_size(512) == "512.0 B"
        assert _human_size(1023) == "1023.0 B"

    def test_kilobytes(self) -> None:
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(2048) == "2.0 KB"
        assert _human_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert _human_size(1024 * 1024) == "1.0 MB"
        assert _human_size(2.5 * 1024 * 1024) == "2.5 MB"

    def test_gigabytes(self) -> None:
        assert _human_size(1024**3) == "1.0 GB"

    def test_terabytes(self) -> None:
        assert _human_size(1024**4) == "1.0 TB"
        assert _human_size(2 * 1024**4) == "2.0 TB"


# ── _write_json ───────────────────────────────────────────────────────────────


class TestWriteJson:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        _write_json(path, {"key": "value", "num": 42})
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"key": "value", "num": 42}

    def test_writes_indented(self, tmp_path: Path) -> None:
        path = tmp_path / "pretty.json"
        _write_json(path, {"a": 1})
        text = path.read_text(encoding="utf-8")
        assert '"a": 1' in text

    def test_serialises_uuid_via_default(self, tmp_path: Path) -> None:
        uid = uuid.uuid4()
        path = tmp_path / "uuid.json"
        _write_json(path, {"id": uid})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == str(uid)

    def test_serialises_bytes_via_default(self, tmp_path: Path) -> None:
        path = tmp_path / "bytes.json"
        _write_json(path, {"blob": b"\x00\xff"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["blob"] == "00ff"

    def test_serialises_datetime_via_default(self, tmp_path: Path) -> None:
        dt = datetime.now(UTC)
        path = tmp_path / "dt.json"
        _write_json(path, {"ts": dt})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["ts"] == dt.isoformat()


# ── _run_pg_dump ──────────────────────────────────────────────────────────────


class TestRunPgDump:
    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_success(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "pg_dump"
        mock_run.side_effect = _complete_dump_run

        output = tmp_path / "dump.sql"
        _run_pg_dump("postgresql://user:pass@localhost/db", output)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pg_dump"
        assert "--clean" in cmd
        assert "--if-exists" in cmd
        assert "--no-owner" in cmd
        assert "--no-acl" in cmd
        assert "--format=plain" in cmd
        assert mock_run.call_args[1]["stdout"] is not None  # the dump is streamed to a file handle
        assert mock_run.call_args[1]["timeout"] == 300

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_truncated_dump_is_refused(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        """FAR-2667: a zero exit is not proof of a complete dump. A file with no
        pg_dump completion trailer is refused at dump time."""
        mock_which.return_value = "pg_dump"
        mock_run.side_effect = _truncated_dump_run

        output = tmp_path / "dump.sql"
        with pytest.raises(RuntimeError, match="INCOMPLETE"):
            _run_pg_dump("postgresql://user:pass@localhost/db", output)

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_preamble_settings_the_server_lacks_are_dropped(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        """A client NEWER than the server writes GUCs the server rejects; the
        dump must be replayable by the release that took it (observed: pg_dump
        17 writes ``SET transaction_timeout``, PostgreSQL 16 refuses it)."""
        mock_which.return_value = "pg_dump"

        def _run(argv: list[str], **kwargs: Any) -> MagicMock:
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\nSET transaction_timeout = 0;\nSET client_encoding = 'UTF8';\n\nSELECT 1;\n--\n-- PostgreSQL database dump complete\n\n")
            return MagicMock(returncode=0, stderr=b"")

        mock_run.side_effect = _run
        output = tmp_path / "dump.sql"
        _run_pg_dump("postgresql://user:pass@localhost/db", output)
        text = output.read_text(encoding="utf-8")
        assert "transaction_timeout" not in text
        assert "SET statement_timeout = 0;" in text
        assert "SELECT 1;" in text
        assert "-- PostgreSQL database dump complete" in text

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "pg_dump"
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr.decode.return_value = "connection to server failed"
        mock_run.return_value = mock_process

        output = tmp_path / "dump.sql"
        with pytest.raises(RuntimeError, match="pg_dump failed: connection to server failed"):
            _run_pg_dump("postgresql://user:pass@localhost/db", output)

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "pg_dump"
        mock_run.side_effect = _complete_dump_run

        output = tmp_path / "dump.sql"
        _run_pg_dump("postgresql://user:pass@localhost/db", output, timeout=600)

        assert mock_run.call_args[1]["timeout"] == 600


class TestStripUnsupportedPreambleSettings:
    """The FAR-2667 compatibility rewrite streams the body instead of reading
    the whole dump into memory twice (which doubled peak memory on a
    multi-GB database)."""

    def _write(self, tmp_path: Path, payload: bytes) -> Path:
        dump = tmp_path / "database.sql"
        dump.write_bytes(payload)
        return dump

    def test_no_removal_leaves_the_file_byte_for_byte(self, tmp_path: Path) -> None:
        from farna.db import plain_dump

        payload = b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n\nSELECT 1;\n"
        dump = self._write(tmp_path, payload)
        assert not plain_dump.strip_unsupported_preamble_settings(dump, {"statement_timeout"})
        assert dump.read_bytes() == payload

    def test_removal_streams_the_body_unchanged(self, tmp_path: Path) -> None:
        from farna.db import plain_dump

        body = b"SELECT 1;\n" + (b"-- body line\n" * 1000)
        payload = b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\nSET transaction_timeout = 0;\n\n" + body
        dump = self._write(tmp_path, payload)

        removed = plain_dump.strip_unsupported_preamble_settings(dump, {"statement_timeout"})

        assert removed == ["transaction_timeout"]
        assert dump.read_bytes() == b"--\n-- PostgreSQL database dump\n--\n\nSET statement_timeout = 0;\n\n" + body
        assert not list(tmp_path.glob(".*preamble-rewrite"))

    def test_preamble_only_dump_is_rewritten_without_a_body(self, tmp_path: Path) -> None:
        from farna.db import plain_dump

        payload = b"-- PostgreSQL database dump\nSET transaction_timeout = 0;\n-- PostgreSQL database dump complete\n"
        dump = self._write(tmp_path, payload)

        removed = plain_dump.strip_unsupported_preamble_settings(dump, set())

        assert removed == ["transaction_timeout"]
        assert dump.read_bytes() == b"-- PostgreSQL database dump\n-- PostgreSQL database dump complete\n"

    def test_replace_failure_cleans_up_the_temp_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.db import plain_dump

        dump = self._write(tmp_path, b"--\nSET transaction_timeout = 0;\n\nSELECT 1;\n")
        before = sorted(p.name for p in tmp_path.iterdir())

        def _boom(_self: Path, _target: Any) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(Path, "replace", _boom)
        with pytest.raises(plain_dump.DumpError, match="could not be rewritten"):
            plain_dump.strip_unsupported_preamble_settings(dump, {"statement_timeout"})
        # No temp file (or any other debris) survives the failed rewrite.
        assert sorted(p.name for p in tmp_path.iterdir()) == before


# ── _run_psql ─────────────────────────────────────────────────────────────────


class TestRunPsql:
    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_success(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "psql"
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        input_path = tmp_path / "restore.sql"
        input_path.write_text("RESTORE;")

        _run_psql("postgresql://user:pass@localhost/db", input_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "-q" in cmd
        assert "-v" in cmd
        assert "ON_ERROR_STOP=1" in cmd
        assert mock_run.call_args[1]["timeout"] == 600

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "psql"
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr.decode.return_value = "syntax error at line 1"
        mock_run.return_value = mock_process

        input_path = tmp_path / "fail.sql"
        input_path.write_text("BAD SQL;")

        with pytest.raises(RuntimeError, match="psql restore failed: syntax error at line 1"):
            _run_psql("postgresql://user:pass@localhost/db", input_path)

    @patch.object(shutil, "which")
    @patch("farna.cli.backup.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path) -> None:
        mock_which.return_value = "psql"
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        input_path = tmp_path / "restore.sql"
        input_path.write_text("RESTORE;")

        _run_psql("postgresql://user:pass@localhost/db", input_path, timeout=1200)

        assert mock_run.call_args[1]["timeout"] == 1200


# ── _export_checkpoint_blobs_sync ─────────────────────────────────────────────


class TestExportCheckpointBlobsSync:
    @patch("psycopg.connect")
    def test_returns_formatted_rows(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row1 = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "thread_id": "thread-1",
            "checkpoint_ns": "ns1",
            "channel": "default",
            "version": 1,
            "type": "json",
            "blob": b"\x00\xff",
        }
        row2 = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
            "thread_id": "thread-2",
            "checkpoint_ns": "ns2",
            "channel": "other",
            "version": 2,
            "type": "json",
            "blob": None,
        }
        mock_cur.__iter__.return_value = [row1, row2]

        result = _export_checkpoint_blobs_sync("postgresql://localhost/db")

        assert len(result) == 2
        assert result[0]["organisation_id"] == "00000000-0000-0000-0000-000000000001"
        assert result[0]["thread_id"] == "thread-1"
        assert result[0]["blob"] == "00ff"
        assert result[1]["organisation_id"] == "00000000-0000-0000-0000-000000000002"
        assert result[1]["blob"] is None

        mock_connect.assert_called_once()
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "checkpoint_blobs" in sql
        assert "ORDER BY" in sql

    @patch("psycopg.connect")
    def test_handles_memoryview_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "thread_id": "t1",
            "checkpoint_ns": "ns",
            "channel": "ch",
            "version": 1,
            "type": "json",
            "blob": memoryview(b"\xab\xcd"),
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoint_blobs_sync("postgresql://localhost/db")

        assert result[0]["blob"] == "abcd"


# ── _export_checkpoints_sync ──────────────────────────────────────────────────


class TestExportCheckpointsSync:
    @patch("psycopg.connect")
    def test_returns_formatted_rows(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoint_id = uuid.uuid4()
        org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        row = {
            "organisation_id": org_id,
            "thread_id": "thread-1",
            "checkpoint_ns": "ns1",
            "checkpoint_id": str(checkpoint_id),
            "parent_checkpoint_id": None,
            "checkpoint": {"configurable": {"temperature": 0.7}},
            "metadata": {"source": "test"},
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoints_sync("postgresql://localhost/db")

        assert len(result) == 1
        assert result[0]["organisation_id"] == str(org_id)
        assert result[0]["thread_id"] == "thread-1"
        assert result[0]["checkpoint_id"] == str(checkpoint_id)
        assert result[0]["checkpoint"] == {"configurable": {"temperature": 0.7}}
        assert result[0]["metadata"] == {"source": "test"}

        mock_connect.assert_called_once()
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "checkpoints" in sql
        assert "ORDER BY" in sql

    @patch("psycopg.connect")
    def test_handles_none_organisation_id(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row = {
            "organisation_id": None,
            "thread_id": "t1",
            "checkpoint_ns": "ns",
            "checkpoint_id": "cp1",
            "parent_checkpoint_id": None,
            "checkpoint": None,
            "metadata": None,
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoints_sync("postgresql://localhost/db")

        assert result[0]["organisation_id"] is None
        assert result[0]["checkpoint"] is None


# ── _export_checkpoint_writes_sync ────────────────────────────────────────────


class TestExportCheckpointWritesSync:
    @patch("psycopg.connect")
    def test_returns_formatted_rows(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        org_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        row = {
            "organisation_id": org_id,
            "thread_id": "thread-1",
            "checkpoint_ns": "ns1",
            "checkpoint_id": "cp1",
            "task_id": "task1",
            "idx": 0,
            "channel": "default",
            "type": "json",
            "blob": b"\xab\xcd",
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoint_writes_sync("postgresql://localhost/db")

        assert len(result) == 1
        assert result[0]["organisation_id"] == str(org_id)
        assert result[0]["thread_id"] == "thread-1"
        assert result[0]["blob"] == "abcd"

        mock_connect.assert_called_once()
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "checkpoint_writes" in sql
        assert "ORDER BY" in sql

    @patch("psycopg.connect")
    def test_handles_null_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row = {
            "organisation_id": None,
            "thread_id": "t1",
            "checkpoint_ns": "ns",
            "checkpoint_id": "cp1",
            "task_id": "task1",
            "idx": 0,
            "channel": "default",
            "type": "json",
            "blob": None,
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoint_writes_sync("postgresql://localhost/db")

        assert result[0]["blob"] is None

    @patch("psycopg.connect")
    def test_handles_memoryview_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row = {
            "organisation_id": None,
            "thread_id": "t1",
            "checkpoint_ns": "ns",
            "checkpoint_id": "cp1",
            "task_id": "task1",
            "idx": 0,
            "channel": "default",
            "type": "json",
            "blob": memoryview(b"\xca\xfe"),
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoint_writes_sync("postgresql://localhost/db")

        assert result[0]["blob"] == "cafe"


# ── _export_credentials_references_sync ───────────────────────────────────────


class TestExportCredentialsReferencesSync:
    @patch("psycopg.connect")
    def test_returns_dict_with_two_tables(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                "name": "My Connector",
                "credentials_ciphertext": b"\xca\xfe",
            },
        ]

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
                "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                "name": "My Model",
                "credentials_ciphertext": b"\xba\xbe",
            },
        ]

        mock_conn.cursor.side_effect = [cur1, cur2]

        result = _export_credentials_references_sync("postgresql://localhost/db")

        assert set(result.keys()) == {"connector_instances", "model_backends"}
        assert len(result["connector_instances"]) == 1
        assert result["connector_instances"][0]["id"] == "00000000-0000-0000-0000-000000000001"
        assert result["connector_instances"][0]["credentials_ciphertext"] == "cafe"
        assert len(result["model_backends"]) == 1
        assert result["model_backends"][0]["credentials_ciphertext"] == "babe"

    @patch("psycopg.connect")
    def test_returns_empty_dicts_for_empty_tables(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = []

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = []

        mock_conn.cursor.side_effect = [cur1, cur2]

        result = _export_credentials_references_sync("postgresql://localhost/db")

        assert result == {"connector_instances": [], "model_backends": []}

    @patch("psycopg.connect")
    def test_executes_correct_sql_for_each_table(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = []

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = []

        mock_conn.cursor.side_effect = [cur1, cur2]

        _export_credentials_references_sync("postgresql://localhost/db")

        sql1 = cur1.execute.call_args[0][0]
        assert "connector_instances" in sql1
        sql2 = cur2.execute.call_args[0][0]
        assert "model_backends" in sql2


# ── _restore_checkpoint_blobs_sync ────────────────────────────────────────────


class TestRestoreCheckpointBlobsSync:
    @patch("psycopg.connect")
    def test_truncates_and_inserts(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "00ff",
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 1
        mock_cur.execute.assert_any_call("TRUNCATE TABLE checkpoint_blobs CASCADE")
        insert_call = mock_cur.execute.call_args_list[1]
        assert "INSERT INTO checkpoint_blobs" in insert_call[0][0]
        mock_conn.commit.assert_called_once()

    @patch("psycopg.connect")
    def test_handles_null_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": None,
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 1
        insert_call = mock_cur.execute.call_args_list[1]
        params = insert_call[0][1]
        assert params[-1] is None

    @patch("psycopg.connect")
    def test_returns_count(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "00",
            },
            {
                "organisation_id": "00000000-0000-0000-0000-000000000002",
                "thread_id": "t2",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 2,
                "type": "json",
                "blob": "ff",
            },
            {
                "organisation_id": "00000000-0000-0000-0000-000000000003",
                "thread_id": "t3",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 3,
                "type": "json",
                "blob": None,
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 3
        assert mock_cur.execute.call_count == 4  # 1 TRUNCATE + 3 INSERT


# ── _restore_checkpoints_sync ─────────────────────────────────────────────────


class TestRestoreCheckpointsSync:
    @patch("psycopg.connect")
    def test_truncates_and_inserts(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "checkpoint": {"key": "val"},
                "metadata": {"src": "test"},
            },
        ]

        result = _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)

        assert result == 1
        mock_cur.execute.assert_any_call("TRUNCATE TABLE checkpoints CASCADE")
        insert_call = mock_cur.execute.call_args_list[1]
        assert "INSERT INTO checkpoints" in insert_call[0][0]
        mock_conn.commit.assert_called_once()

    @patch("psycopg.connect")
    def test_handles_null_org_id(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": None,
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "checkpoint": None,
                "metadata": None,
            },
        ]

        result = _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)

        assert result == 1
        insert_call = mock_cur.execute.call_args_list[1]
        params = insert_call[0][1]
        assert params[0] is None

    @patch("psycopg.connect")
    def test_returns_count(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": None,
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "checkpoint": None,
                "metadata": None,
            },
            {
                "organisation_id": None,
                "thread_id": "t2",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp2",
                "parent_checkpoint_id": None,
                "checkpoint": None,
                "metadata": None,
            },
        ]

        result = _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)

        assert result == 2
        assert mock_cur.execute.call_count == 3  # 1 TRUNCATE + 2 INSERT

    @patch("psycopg.connect")
    def test_inserts_nullable_type_column(self, mock_connect: MagicMock) -> None:
        """FAR-2022: ``checkpoints.type`` is nullable, so omitting it from the
        INSERT silently dropped it on every restore. Pin the column AND the
        parameter position so a re-break fails here.
        """
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "type": "msgpack",
                "checkpoint": {"key": "val"},
                "metadata": {"src": "test"},
            },
        ]

        result = _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)

        assert result == 1
        sql, params = mock_cur.execute.call_args_list[1][0]
        columns = [column.strip() for column in sql[sql.index("(") + 1 : sql.index(")")].split(",")]
        assert "type" in columns
        assert params[columns.index("type")] == "msgpack"

    @patch("psycopg.connect")
    def test_wraps_json_columns_as_jsonb(self, mock_connect: MagicMock) -> None:
        """The JSON exports decode to dicts; psycopg does not adapt a bare dict
        for a jsonb parameter, so the restore must wrap them explicitly."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "type": "msgpack",
                "checkpoint": {"key": "val"},
                "metadata": {"src": "test"},
            },
        ]

        result = _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)

        assert result == 1
        sql, params = mock_cur.execute.call_args_list[1][0]
        columns = [column.strip() for column in sql[sql.index("(") + 1 : sql.index(")")].split(",")]
        checkpoint_value = params[columns.index("checkpoint")]
        metadata_value = params[columns.index("metadata")]
        assert isinstance(checkpoint_value, Jsonb)
        assert checkpoint_value.obj == {"key": "val"}
        assert isinstance(metadata_value, Jsonb)
        assert metadata_value.obj == {"src": "test"}


# ── _restore_checkpoint_writes_sync ───────────────────────────────────────────


class TestRestoreCheckpointWritesSync:
    @patch("psycopg.connect")
    def test_truncates_and_inserts(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        writes = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "task_id": "task1",
                "idx": 0,
                "channel": "ch",
                "type": "json",
                "blob": "abcd",
            },
        ]

        result = _restore_checkpoint_writes_sync("postgresql://localhost/db", writes)

        assert result == 1
        mock_cur.execute.assert_any_call("TRUNCATE TABLE checkpoint_writes CASCADE")
        insert_call = mock_cur.execute.call_args_list[1]
        assert "INSERT INTO checkpoint_writes" in insert_call[0][0]
        mock_conn.commit.assert_called_once()

    @patch("psycopg.connect")
    def test_handles_null_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        writes = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "task_id": "task1",
                "idx": 0,
                "channel": "ch",
                "type": "json",
                "blob": None,
            },
        ]

        result = _restore_checkpoint_writes_sync("postgresql://localhost/db", writes)

        assert result == 1
        insert_call = mock_cur.execute.call_args_list[1]
        params = insert_call[0][1]
        assert params[-1] is None


# ── _re_encrypt_credentials_sync ──────────────────────────────────────────────


class TestReEncryptCredentialsSync:
    @patch("farna.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_re_encrypts_rows(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_old.decrypt.return_value = b"plaintext_data"
        mock_new.encrypt.return_value = b"new_ciphertext"
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "credentials_ciphertext": "cafe",
                },
            ],
        }

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", creds, "old-key-1234567890", "new-key-1234567890")

        assert result == {"connector_instances": 1}
        mock_old.decrypt.assert_called_once()
        mock_new.encrypt.assert_called_once_with(b"plaintext_data")
        mock_conn.commit.assert_called_once()
        update_call = mock_cur.execute.call_args[0]
        assert "UPDATE connector_instances" in update_call[0]
        assert update_call[1][0] == b"new_ciphertext"

    @patch("farna.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_skips_rows_with_empty_ciphertext(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {"id": "00000000-0000-0000-0000-000000000001", "credentials_ciphertext": ""},
                {"id": "00000000-0000-0000-0000-000000000002", "credentials_ciphertext": "cafe"},
            ],
        }

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", creds, "old-key", "new-key")

        assert result == {"connector_instances": 1}
        assert mock_old.decrypt.call_count == 1
        assert mock_new.encrypt.call_count == 1

    @patch("farna.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_processes_multiple_tables(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_old.decrypt.return_value = b"data"
        mock_new.encrypt.return_value = b"ct"
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {"id": "00000000-0000-0000-0000-000000000001", "credentials_ciphertext": "aa"},
            ],
            "model_backends": [
                {"id": "00000000-0000-0000-0000-000000000002", "credentials_ciphertext": "bb"},
            ],
        }

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", creds, "old", "new")

        assert result == {"connector_instances": 1, "model_backends": 1}
        assert mock_cur.execute.call_count == 2  # 2 UPDATEs (one per table)
        mock_conn.commit.assert_called_once()

    @patch("farna.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_handles_empty_creds_dict(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", {}, "old", "new")

        assert result == {}
        mock_conn.commit.assert_called_once()


# ── CLI: backup ───────────────────────────────────────────────────────────────


class TestBackupCli:
    @patch("farna.cli.backup._print_size")
    @patch("farna.cli.backup._export_checkpoint_writes_sync")
    @patch("farna.cli.backup._export_checkpoints_sync")
    @patch("farna.cli.backup._export_credentials_references_sync")
    @patch("farna.cli.backup._export_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_pg_dump")
    @patch("farna.cli.backup._get_schema_versions")
    @patch("farna.cli.backup._get_db_version")
    @patch("farna.cli.backup.get_settings")
    def test_backup_success(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_export_cp: MagicMock,
        mock_export_cw: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_get_db_version.return_value = "PostgreSQL 16.0 (Debian 16.0-1)"
        mock_get_schema_versions.return_value = ["abc123def456"]
        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}
        mock_export_cp.return_value = []
        mock_export_cw.return_value = []

        def _fake_pg_dump(raw_url: str, output: Path, **kwargs: Any) -> None:
            output.write_text("-- pg_dump output")

        mock_pg_dump.side_effect = _fake_pg_dump

        runner = CliRunner()
        backup_dir = tmp_path / "mybackup"
        result = runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Backup complete" in result.output
        assert "Backup directory" in result.output
        assert "Running pg_dump" in result.output
        assert "database.sql written" in result.output

        mock_pg_dump.assert_called_once()
        mock_export_blobs.assert_called_once()
        mock_export_creds.assert_called_once()
        mock_export_cp.assert_called_once()
        mock_export_cw.assert_called_once()
        mock_print_size.assert_called_once()

    @patch("farna.cli.backup._print_size")
    @patch("farna.cli.backup._export_checkpoint_writes_sync")
    @patch("farna.cli.backup._export_checkpoints_sync")
    @patch("farna.cli.backup._export_credentials_references_sync")
    @patch("farna.cli.backup._export_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_pg_dump")
    @patch("farna.cli.backup._get_schema_versions")
    @patch("farna.cli.backup._get_db_version")
    @patch("farna.cli.backup.get_settings")
    def test_backup_writes_manifest(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_export_cp: MagicMock,
        mock_export_cw: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev123"]
        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}
        mock_export_cp.return_value = []
        mock_export_cw.return_value = []

        def _fake_pg_dump(raw_url: str, output: Path, **kwargs: Any) -> None:
            output.write_text("-- pg_dump output")

        mock_pg_dump.side_effect = _fake_pg_dump

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        manifest_path = backup_dir / "backup-info.json"
        assert manifest_path.exists(), "backup-info.json not written"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["backup_type"] == "full"
        assert manifest["db_version"] == "PostgreSQL 16.0"
        assert manifest["schema_versions"] == ["rev123"]
        assert manifest["fernet_key_hash"] == _fernet_key_hash(fernet_key)
        assert "timestamp" in manifest
        assert "file_checksums" in manifest
        assert manifest["file_checksums"]["database.sql"] is not None

    @patch("farna.cli.backup.get_settings")
    def test_backup_failure_raises_click_exception(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.database_url = "postgresql://localhost/db"

        runner = CliRunner()
        backup_dir = tmp_path / "failbackup"
        result = runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)

    @patch("farna.cli.backup._print_size")
    @patch("farna.cli.backup._export_checkpoint_writes_sync")
    @patch("farna.cli.backup._export_checkpoints_sync")
    @patch("farna.cli.backup._export_credentials_references_sync")
    @patch("farna.cli.backup._export_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_pg_dump")
    @patch("farna.cli.backup._get_schema_versions")
    @patch("farna.cli.backup._get_db_version")
    @patch("farna.cli.backup.get_settings")
    def test_backup_writes_blobs_and_creds_json(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_export_cp: MagicMock,
        mock_export_cw: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "a" * 32
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev1"]

        def _fake_pg_dump(raw_url: str, output: Path, **kwargs: Any) -> None:
            output.write_text("-- pg_dump output")

        mock_pg_dump.side_effect = _fake_pg_dump

        sample_blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "abcd",
            },
        ]
        sample_creds = {
            "connector_instances": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "organisation_id": "00000000-0000-0000-0000-000000000010",
                    "name": "Test Connector",
                    "credentials_ciphertext": "cafe",
                },
            ],
            "model_backends": [],
        }
        mock_export_blobs.return_value = sample_blobs
        mock_export_creds.return_value = sample_creds
        mock_export_cp.return_value = []
        mock_export_cw.return_value = []

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        blobs_path = backup_dir / "checkpoint_blobs.json"
        assert blobs_path.exists()
        loaded_blobs = json.loads(blobs_path.read_text(encoding="utf-8"))
        assert len(loaded_blobs) == 1
        assert loaded_blobs[0]["blob"] == "abcd"

        creds_path = backup_dir / "credentials_references.json"
        assert creds_path.exists()
        loaded_creds = json.loads(creds_path.read_text(encoding="utf-8"))
        assert len(loaded_creds["connector_instances"]) == 1

    @patch("farna.cli.backup._print_size")
    @patch("farna.cli.backup._export_checkpoint_writes_sync")
    @patch("farna.cli.backup._export_checkpoints_sync")
    @patch("farna.cli.backup._export_credentials_references_sync")
    @patch("farna.cli.backup._export_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_pg_dump")
    @patch("farna.cli.backup._get_schema_versions")
    @patch("farna.cli.backup._get_db_version")
    @patch("farna.cli.backup.get_settings")
    def test_backup_with_explicit_db_url(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_export_cp: MagicMock,
        mock_export_cw: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "a" * 32
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev1"]

        def _fake_pg_dump(raw_url: str, output: Path, **kwargs: Any) -> None:
            output.write_text("-- pg_dump output")

        mock_pg_dump.side_effect = _fake_pg_dump

        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}
        mock_export_cp.return_value = []
        mock_export_cw.return_value = []

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        result = runner.invoke(
            cli,
            [
                "backup",
                "--output-dir",
                str(backup_dir),
                "--db-url",
                "postgresql://custom:pass@host/db",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        mock_pg_dump.assert_called_once()
        url_arg = mock_pg_dump.call_args[0][0]
        assert "custom" in url_arg
        assert "host" in url_arg


# ── CLI: restore ──────────────────────────────────────────────────────────────


class TestRestoreCli:
    @pytest.fixture(autouse=True)
    def _stub_posture_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """These tests cover the restore phases BELOW the role-posture re-run.

        The posture step needs a live cluster; its failure/success semantics are
        covered in test_backup_restore_semantics.py.
        """
        monkeypatch.setattr("farna.cli.backup._ensure_restore_posture", lambda *_args, **_kwargs: None)

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_success(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 5
        mock_restore_cp.return_value = 3
        mock_restore_cw.return_value = 7
        mock_re_encrypt.return_value = {}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": ["rev1"],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoint_blobs.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Restore complete" in result.output
        assert "Restoring database schema" in result.output
        assert "restored" in result.output

        mock_psql.assert_called_once()
        mock_restore_blobs.assert_called_once()
        mock_restore_cp.assert_called_once()
        mock_restore_cw.assert_called_once()
        mock_re_encrypt.assert_not_called()

    def test_restore_missing_manifest(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # The app role URL is resolved before the manifest is read, so it must
        # be supplied explicitly (the ambient test DATABASE_URL is sqlite).
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes", "--app-db-url", _TEST_APP_DB_URL])

        assert result.exit_code != 0
        assert "backup-info.json not found" in result.output

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_skips_database_sql_when_missing(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_re_encrypt.return_value = {}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0
        assert "No database.sql found" in result.output
        mock_psql.assert_not_called()

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_requires_previous_fernet_key_when_key_changed(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "--previous-fernet-key" in result.output

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_with_re_encryption(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0
        mock_re_encrypt.return_value = {"connector_instances": 2, "model_backends": 1}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "restore",
                str(tmp_path),
                "--yes",
                "--previous-fernet-key",
                old_fernet_key,
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Re-encrypting" in result.output
        assert "2 connector_instances re-encrypted" in result.output
        assert "1 model_backends re-encrypted" in result.output
        mock_re_encrypt.assert_called_once()
        args = mock_re_encrypt.call_args[0]
        assert args[2] == old_fernet_key  # previous ferret key
        assert args[3] == new_fernet_key  # current ferret key

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_skips_re_encryption_when_key_unchanged(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0
        assert "FERNET_KEY unchanged" in result.output
        mock_re_encrypt.assert_not_called()

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_confirmation_prompt(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path)], input="y\n")

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "OVERWRITE" in result.output
        assert "Restore complete" in result.output

    @patch("farna.cli.backup.get_settings")
    def test_restore_failure(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        (tmp_path / "backup-info.json").write_text(json.dumps({"timestamp": "now"}), encoding="utf-8")
        (tmp_path / "database.sql").write_text("", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_skips_re_encryption_when_creds_json_missing(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "restore",
                str(tmp_path),
                "--yes",
                "--previous-fernet-key",
                old_fernet_key,
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert mock_re_encrypt.call_count == 0


# ── _file_checksum ────────────────────────────────────────────────────────────


class TestFileChecksum:
    def test_returns_sha256_hex(self, tmp_path: Path) -> None:
        path = tmp_path / "test.bin"
        path.write_bytes(b"hello world")
        h = _file_checksum(path)
        assert h == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert len(h) == 64

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        h = _file_checksum(path)
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ── CLI: restore with integrity checks ────────────────────────────────────────


class TestRestoreIntegrity:
    @pytest.fixture(autouse=True)
    def _stub_posture_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """These tests cover archive integrity, not the role-posture re-run."""
        monkeypatch.setattr("farna.cli.backup._ensure_restore_posture", lambda *_args, **_kwargs: None)

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_with_checksum_verification(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        db_sql = tmp_path / "database.sql"
        db_sql.write_text("-- SQL", encoding="utf-8")

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
            "file_checksums": {
                "database.sql": _file_checksum(db_sql),
            },
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "All file checksums verified" in result.output

    @patch("farna.cli.backup.get_settings")
    def test_restore_fails_on_checksum_mismatch(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        # The app role URL is resolved from configuration BEFORE the archive
        # checks, so the patched settings need a real string (a MagicMock
        # attribute is refused and would mask the checksum assertion).
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        db_sql = tmp_path / "database.sql"
        db_sql.write_text("-- SQL", encoding="utf-8")

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            "file_checksums": {
                "database.sql": "0000000000000000000000000000000000000000000000000000000000000000",
            },
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "Checksum mismatch" in result.output

    @patch("farna.cli.backup.get_settings")
    def test_restore_fails_on_missing_manifest_file(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        _write_restorable_dump(tmp_path)
        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "file_checksums": {
                "database.sql": _file_checksum(tmp_path / "database.sql"),
                "nonexistent.json": "...schecksum...",
            },
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text("[]", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "not found on disk" in result.output

    @patch("farna.cli.backup._take_safety_dump", return_value=Path("pre-restore-dump-fake"))
    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_refuses_a_dump_the_manifest_does_not_cover(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        mock_safety_dump: MagicMock,
        tmp_path: Path,
    ) -> None:
        """FAR-2667: a manifest with no checksum for database.sql cannot prove
        this dump belongs to this archive — a FOREIGN archive is refused
        BEFORE the wipe, never replayed over the live database."""
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL
        mock_restore_blobs.return_value = 0
        mock_restore_cp.return_value = 0
        mock_restore_cw.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_bytes(_COMPLETE_DUMP)

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "records no checksum for it" in result.output
        mock_psql.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_fails_on_corrupt_json(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoints.json").write_text("{bad json}", encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "Corrupt JSON" in result.output

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_previews_steps_without_db_changes(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": ["rev1"],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        _write_restorable_dump(tmp_path)
        (tmp_path / "checkpoint_blobs.json").write_text(json.dumps([{"id": "b1"}, {"id": "b2"}]), encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text(json.dumps([{"id": "c1"}]), encoding="utf-8")
        (tmp_path / "checkpoint_writes.json").write_text(json.dumps([]), encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": [], "model_backends": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "DRY RUN" in result.output
        assert "WOULD restore database schema and data via psql" in result.output
        assert "WOULD restore 2 checkpoint blob records" in result.output
        assert "WOULD restore 1 checkpoint records" in result.output
        assert "WOULD restore 0 checkpoint write records" in result.output
        assert "FERNET_KEY unchanged" in result.output
        assert "Dry run complete" in result.output

        mock_psql.assert_not_called()
        mock_restore_blobs.assert_not_called()
        mock_restore_cp.assert_not_called()
        mock_restore_cw.assert_not_called()
        mock_re_encrypt.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_skips_full_db_restore_when_no_sql(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "No database.sql found — WOULD skip full DB restore" in result.output
        assert "No checkpoint_blobs.json found — WOULD skip" in result.output
        assert "No credentials_references.json found — WOULD skip credential restore" in result.output
        mock_psql.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_requires_previous_fernet_key_when_key_changed(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text(
            '{"connector_instances": [{"id": "r1", "credentials_ciphertext": "abcd"}]}',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run"])

        assert result.exit_code != 0
        assert "--previous-fernet-key" in result.output
        assert "WOULD re-encrypt" not in result.output
        mock_re_encrypt.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_previews_re_encryption(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text(
            json.dumps(
                {
                    "connector_instances": [
                        {"id": "r1", "credentials_ciphertext": "abcd"},
                        {"id": "r2", "credentials_ciphertext": ""},
                    ],
                    "model_backends": [{"id": "m1", "credentials_ciphertext": "abcd"}],
                }
            ),
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run", "--previous-fernet-key", old_fernet_key])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "WOULD re-encrypt 1 connector_instances credentials" in result.output
        assert "WOULD re-encrypt 1 model_backends credentials" in result.output
        assert "Dry run complete" in result.output
        mock_re_encrypt.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_still_fails_on_corrupt_json(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "checkpoints.json").write_text("{bad json}", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run"])

        assert result.exit_code != 0
        assert "Corrupt JSON" in result.output
        mock_psql.assert_not_called()

    @patch("farna.cli.backup._restore_checkpoint_writes_sync")
    @patch("farna.cli.backup._restore_checkpoints_sync")
    @patch("farna.cli.backup._re_encrypt_credentials_sync")
    @patch("farna.cli.backup._restore_checkpoint_blobs_sync")
    @patch("farna.cli.backup._run_psql")
    @patch("farna.cli.backup.get_settings")
    def test_restore_dry_run_skips_confirmation_prompt(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        mock_restore_cp: MagicMock,
        mock_restore_cw: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_settings.return_value.database_url = _TEST_APP_DB_URL

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--dry-run"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "OVERWRITE the current database" not in result.output
        assert "Dry run complete" in result.output


# ── _restore_checkpoints_sync: invalid UUID handling ──────────────────────────


class TestRestoreDumpIntegrity:
    """FAR-2667: a corrupt or foreign dump is refused BEFORE the wipe, and the
    restored cluster is left stopped with the exact start command printed."""

    @pytest.fixture(autouse=True)
    def _stub_posture_step(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("farna.cli.backup._ensure_restore_posture", lambda *_args, **_kwargs: None)

    def _archive(self, tmp_path: Path, dump: bytes, *, attested: bool = True) -> None:
        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "schema_versions": [],
            "file_checksums": {"database.sql": hashlib.sha256(dump).hexdigest()},
        }
        if attested:
            manifest["dump_complete"] = True
            manifest["dump_format"] = "plain"
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_bytes(dump)

    def test_refuses_a_truncated_dump_even_when_the_checksum_matches(self, tmp_path: Path) -> None:
        """The checksum proves the bytes are the ones the manifest describes; the
        completion trailer proves those bytes are a WHOLE dump. Both must hold."""
        truncated = b"-- dump\nCREATE TABLE t (id int);\n"
        self._archive(tmp_path, truncated, attested=True)
        with (
            patch("farna.cli.backup._run_psql") as psql,
            patch("farna.cli.backup.get_settings", **{"return_value.fernet_key": "a" * 32, "return_value.database_url": _TEST_APP_DB_URL}),
        ):
            result = CliRunner().invoke(cli, ["restore", str(tmp_path), "--yes"])
        assert result.exit_code != 0
        assert "INCOMPLETE" in result.output
        psql.assert_not_called()

    def test_refuses_a_custom_format_archive_by_name(self, tmp_path: Path) -> None:
        self._archive(tmp_path, b"PGDMP" + b"\x00" * 64, attested=True)
        with (
            patch("farna.cli.backup._run_psql") as psql,
            patch("farna.cli.backup.get_settings", **{"return_value.fernet_key": "a" * 32, "return_value.database_url": _TEST_APP_DB_URL}),
        ):
            result = CliRunner().invoke(cli, ["restore", str(tmp_path), "--yes"])
        assert result.exit_code != 0
        assert "CUSTOM-format" in result.output
        psql.assert_not_called()

    def test_warns_when_the_archive_predates_completeness_attestation(self, tmp_path: Path) -> None:
        """An older archive can only be checksum-verified; say so loudly rather
        than implying a proof that does not exist."""
        self._archive(tmp_path, b"-- dump\n", attested=False)
        with (
            patch("farna.cli.backup._run_psql"),
            patch("farna.cli.backup._take_safety_dump", return_value=Path("fake")),
            patch("farna.cli.backup._get_schema_versions", return_value=[]),
            patch("farna.cli.backup._db_has_modulo_tables", return_value=False),
            patch("farna.cli.backup.get_settings", **{"return_value.fernet_key": "a" * 32, "return_value.database_url": _TEST_APP_DB_URL}),
        ):
            result = CliRunner().invoke(cli, ["restore", str(tmp_path), "--yes"])
        assert "predates dump-completion attestation" in result.output


class TestRestoreStartGuidance:
    """FAR-2667 item 5: restore stops the writers itself and leaves them
    stopped, printing the exact start command and never auto-restarting."""

    def _options(self, *, data_dir: Path | None) -> Any:
        from farna.cli.backup import _RestoreOptions

        return _RestoreOptions(
            backup_dir=Path("archive"),
            db_url=None,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir,
            replace_cluster=False,
            no_safety_dump=False,
        )

    def test_native_start_command_names_the_data_dir(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.cli.backup import _print_start_guidance

        _print_start_guidance(self._options(data_dir=tmp_path / "data"), stopped=True)
        out = capsys.readouterr().out
        assert f"farna-control start --data-dir {tmp_path / 'data'}" in out
        assert "still stopped" in out
        assert "No automatic restart was performed" in out

    def test_container_start_command_is_the_rollback_helper(self, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.cli.backup import _print_start_guidance

        _print_start_guidance(self._options(data_dir=None), stopped=False)
        out = capsys.readouterr().out
        # The SHIPPED bundle layout (docker-compose.prod.yml at the top level).
        # `TAG=<previous> docker compose ... up -d` does NOT roll back there: the
        # packager rewrites the image line to a LITERAL release tag, so TAG only
        # sets FARNA_CONTROL_VERSION and the command restarted the release that
        # just failed. compose-rollback.sh merges a one-line image override.
        assert "./compose-rollback.sh <previous-version>" in out
        assert "TAG=<previous-version> docker compose" not in out
        assert "No automatic restart was performed" in out

    def test_stop_writers_is_a_no_op_without_a_live_holder(self, tmp_path: Path) -> None:
        from farna.cli.backup import _stop_launcher_writers

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        assert _stop_launcher_writers(data_dir) is False

    def test_stop_writers_refuses_when_the_launcher_survives(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import click

        from farna.cli import backup as backup_mod
        from farna.launcher import upgrade as upgrade_module

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "data.lock").write_text(json.dumps({"pid": 4242, "mode": "serve"}), encoding="utf-8")
        monkeypatch.setattr(backup_mod.os, "name", "posix")

        def _still_running(_data_dir: Path, **_kwargs: Any) -> None:
            raise upgrade_module.UpgradeError("still locked by holder PID 4242")

        monkeypatch.setattr(upgrade_module, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(upgrade_module, "stop_running_stack", _still_running)
        with pytest.raises(click.ClickException, match="still running"):
            backup_mod._stop_launcher_writers(data_dir)

    def test_stop_writers_stops_a_live_launcher(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from farna.cli import backup as backup_mod
        from farna.launcher import upgrade as upgrade_module

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "data.lock").write_text(json.dumps({"pid": 4242, "mode": "serve"}), encoding="utf-8")
        monkeypatch.setattr(backup_mod.os, "name", "posix")
        stopped: list[Path] = []
        monkeypatch.setattr(upgrade_module, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(upgrade_module, "stop_running_stack", lambda data_dir, **kwargs: stopped.append(data_dir))
        assert backup_mod._stop_launcher_writers(data_dir) is True
        assert stopped == [data_dir]

    def test_an_interrupt_mid_restore_still_prints_the_start_command(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Ctrl-C is a BaseException, so guidance that lives in `except` is never printed.

        The interrupt lands AFTER the writers were stopped and the replay began —
        exactly the state where a silent exit leaves a dark stack.
        """
        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        options = backup_mod._RestoreOptions(
            backup_dir=archive,
            db_url=None,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir,
            replace_cluster=False,
            no_safety_dump=False,
        )
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(backup_mod, "_stop_launcher_writers", lambda _d: True)
        monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: None)

        def _interrupted(*_args: Any, **_kwargs: Any) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(backup_mod, "_restore_cluster", _interrupted)

        with pytest.raises(KeyboardInterrupt):
            backup_mod._run_restore(options)

        captured = capsys.readouterr()
        assert f"farna-control start --data-dir {data_dir}" in captured.err
        assert "did NOT complete" in captured.err
        assert "still stopped" in captured.err


#: A syntactically valid Fernet key for the restore paths under test.
_TEST_FERNET_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _restore_archive(root: Path) -> Path:
    """A minimal restorable archive: manifest + checksummed complete dump."""
    archive = root / "archive"
    archive.mkdir(exist_ok=True)
    (archive / "database.sql").write_bytes(_COMPLETE_DUMP)
    manifest = {
        "timestamp": "2024-01-01T00:00:00+00:00",
        "backup_type": "full",
        "db_version": "PostgreSQL 16.0",
        "schema_versions": ["rev1"],
        "file_checksums": {"database.sql": hashlib.sha256(_COMPLETE_DUMP).hexdigest()},
    }
    (archive / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
    (archive / "checkpoints.json").write_text("[]", encoding="utf-8")
    return archive


def _pid_running(pid: int) -> bool:
    """Liveness of a process this test did NOT spawn (its parent already exited)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TestNoPasswordReachesCapturedOutput:
    """CRITICAL 3: probes handed the password-bearing DSN to psycopg.

    psycopg quotes the DSN it was given in its error text, and the restore
    transcript prints and logs that text. Two shapes are proven here: a
    password containing a SPACE and one containing a BAD PERCENT ESCAPE (which
    ``urlsplit`` happily unquotes to something the URL regex cannot match).
    """

    @staticmethod
    def _echoing_driver(observed: dict[str, Any], password: str) -> Any:
        def _connect(dsn: str, **kwargs: Any) -> Any:
            observed["dsn"] = dsn
            observed["password"] = kwargs.get("password")
            raise psycopg.ProgrammingError(f'invalid connection string "{dsn}"; password={kwargs.get("password")} ({password})')

        return _connect

    @pytest.mark.parametrize("password", ["correct horse battery", "pa%zzword"])
    def test_the_driver_is_handed_a_credential_free_dsn(self, monkeypatch: pytest.MonkeyPatch, password: str) -> None:
        from urllib.parse import quote

        from farna.cli import backup as backup_mod

        raw = f"postgresql+asyncpg://farna_control:{quote(password, safe='')}@127.0.0.1:5432/farna_control"
        observed: dict[str, Any] = {}
        monkeypatch.setattr(backup_mod.psycopg, "connect", self._echoing_driver(observed, password))

        with pytest.raises(RuntimeError) as excinfo:
            backup_mod._export_checkpoints_sync(raw)

        assert observed["password"] == password, "the credential must travel as the connect argument"
        assert password not in observed["dsn"]
        assert quote(password, safe="") not in observed["dsn"]
        message = str(excinfo.value)
        assert password not in message
        assert quote(password, safe="") not in message
        assert quote_plus(password) not in message
        assert "ProgrammingError" in message, "the original driver failure must stay legible"

    @pytest.mark.parametrize("password", ["correct horse battery", "pa%zzword"])
    def test_the_restore_transcript_never_prints_the_password(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], password: str) -> None:
        from urllib.parse import quote

        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        raw = f"postgresql+asyncpg://farna_control:{quote(password, safe='')}@127.0.0.1:5432/farna_control"
        observed: dict[str, Any] = {}
        monkeypatch.setenv("DATABASE_ADMIN_URL", raw)
        monkeypatch.setattr(backup_mod.psycopg, "connect", self._echoing_driver(observed, password))
        monkeypatch.setattr(backup_mod, "_run_psql", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "_ensure_restore_posture", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        monkeypatch.setattr(backup_mod, "get_settings", lambda: type("S", (), {"fernet_key": _TEST_FERNET_KEY, "database_url": _TEST_APP_DB_URL})())

        options = backup_mod._RestoreOptions(
            backup_dir=archive,
            db_url=None,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=None,
            replace_cluster=False,
            no_safety_dump=True,
        )
        with pytest.raises(click.ClickException):
            backup_mod._run_restore(options)

        captured = capsys.readouterr()
        transcript = captured.out + captured.err
        assert "Restore failed" in transcript
        assert password not in transcript
        assert quote(password, safe="") not in transcript

    def test_every_psycopg_connection_goes_through_the_one_helper(self) -> None:
        """The ``_connect`` docstring's "every probe goes through here" claim."""
        import inspect

        from farna.cli import backup as backup_mod

        source = inspect.getsource(backup_mod)
        assert source.count("psycopg.connect(") == 1, "only _connect may open a psycopg connection"
        assert source.count("_connect(") >= 8, "the probes, exports and restore writes all route through _connect"


class TestStartCommandPrintsOnTheFailurePath:
    """MAJOR 4: a failure after the stop must still print the start command."""

    def test_a_restore_that_fails_after_stopping_prints_the_start_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_stop_launcher_writers", lambda _d: True)
        monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        # The safety dump runs BEFORE the stop (a bundled Postgres dies with the
        # launcher), so this test fails the step that follows the stop — the
        # case where the stack is genuinely dark.
        monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: None)

        def _failing_restore_cluster(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("psql exploded")

        monkeypatch.setattr(backup_mod, "_restore_cluster", _failing_restore_cluster)
        monkeypatch.setattr(backup_mod, "get_settings", lambda: type("S", (), {"fernet_key": _TEST_FERNET_KEY, "database_url": _TEST_APP_DB_URL})())

        options = backup_mod._RestoreOptions(
            backup_dir=archive,
            db_url=_TEST_APP_DB_URL,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir,
            replace_cluster=False,
            no_safety_dump=False,
        )
        with pytest.raises(click.ClickException):
            backup_mod._run_restore(options)

        err = capsys.readouterr().err
        assert f"farna-control start --data-dir {data_dir}" in err, "the failure path must hand the operator the exact start command"
        assert "did NOT complete" in err
        assert "still stopped" in err

    def test_a_failure_before_the_stop_still_prints_the_start_command(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """Even a pre-stop failure must leave the operator a command to run."""
        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)

        def _failing_safety_dump(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("pg_dump exploded")

        monkeypatch.setattr(backup_mod, "_safety_dump", _failing_safety_dump)
        monkeypatch.setattr(backup_mod, "get_settings", lambda: type("S", (), {"fernet_key": _TEST_FERNET_KEY, "database_url": _TEST_APP_DB_URL})())

        options = backup_mod._RestoreOptions(
            backup_dir=archive,
            db_url=_TEST_APP_DB_URL,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir,
            replace_cluster=False,
            no_safety_dump=False,
        )
        with pytest.raises(click.ClickException):
            backup_mod._run_restore(options)

        err = capsys.readouterr().err
        assert f"farna-control start --data-dir {data_dir}" in err

    def test_the_success_path_still_prints_it_on_stdout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setattr(backup_mod, "_stop_launcher_writers", lambda _d: True)
        monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "_restore_cluster", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "_restore_data_dir", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "_ensure_restore_posture", lambda *_a, **_k: None)
        monkeypatch.setattr(backup_mod, "get_settings", lambda: type("S", (), {"fernet_key": _TEST_FERNET_KEY, "database_url": _TEST_APP_DB_URL})())

        options = backup_mod._RestoreOptions(
            backup_dir=archive,
            db_url=_TEST_APP_DB_URL,
            app_db_url=None,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir,
            replace_cluster=False,
            no_safety_dump=False,
        )
        backup_mod._run_restore(options)

        out = capsys.readouterr().out
        assert f"farna-control start --data-dir {data_dir}" in out


class TestRestoreCheckpointsSyncErrorPaths:
    @patch("psycopg.connect")
    def test_raises_on_invalid_uuid(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        checkpoints = [
            {
                "organisation_id": "not-a-valid-uuid",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "parent_checkpoint_id": None,
                "checkpoint": None,
                "metadata": None,
            },
        ]

        with pytest.raises(RuntimeError, match="Invalid organisation_id"):
            _restore_checkpoints_sync("postgresql://localhost/db", checkpoints)


class TestRestoreCheckpointWritesSyncErrorPaths:
    @patch("psycopg.connect")
    def test_raises_on_invalid_uuid(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        writes = [
            {
                "organisation_id": "not-a-valid-uuid",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "checkpoint_id": "cp1",
                "task_id": "task1",
                "idx": 0,
                "channel": "ch",
                "type": "json",
                "blob": None,
            },
        ]

        with pytest.raises(RuntimeError, match="Invalid organisation_id"):
            _restore_checkpoint_writes_sync("postgresql://localhost/db", writes)


class TestNativeRestoreServesItsOwnBundledTarget:
    """FAR-2667: a --data-dir restore stops the launcher, and with it the bundled
    PostgreSQL that the dump and the replay both need.

    The fix has two halves, both pinned here: the native run resolves the
    bundled endpoints from the data dir's OWN ``config.env`` (a native operator's
    shell exports none of them, and Settings would refuse to construct without
    them), and the restore keeps ONLY the bundled cluster up for the dump and
    replay window, then leaves the writers stopped.

    The real-connection proof against a real PostgreSQL server lives in
    ``tests/integration/test_restore_bundled_cluster_window.py``.
    """

    @staticmethod
    def _options(
        tmp_path: Path,
        *,
        db_url: str | None = None,
        app_db_url: str | None = None,
        data_dir: Path | None = None,
        backup_dir: Path | None = None,
    ) -> Any:
        from farna.cli import backup as backup_mod

        return backup_mod._RestoreOptions(
            backup_dir=backup_dir if backup_dir is not None else tmp_path,
            db_url=db_url,
            app_db_url=app_db_url,
            yes=True,
            previous_fernet_key=None,
            dry_run=False,
            data_dir=data_dir if data_dir is not None else tmp_path / "data",
            replace_cluster=False,
            no_safety_dump=False,
        )

    @staticmethod
    def _bootstrapped_data_dir(tmp_path: Path) -> Path:
        """A data dir whose config.env names the bundled cluster and its key."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "config.env").write_text(
            "\n".join(
                [
                    "DATABASE_URL=postgresql+asyncpg://farna_control_app:apppw@127.0.0.1:15432/farna_control",
                    "DATABASE_ADMIN_URL=postgresql+asyncpg://farna_control:ownerpw@127.0.0.1:15432/farna_control",
                    "FERNET_KEY=launcher-persisted-key",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return data_dir

    def test_the_bundled_endpoints_and_key_come_from_the_data_dirs_own_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A native shell exports nothing, so the data dir is the only source."""
        from farna.cli import backup as backup_mod

        # Exactly the native operator shell: no DATABASE_* exported at all.
        monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        data_dir = self._bootstrapped_data_dir(tmp_path)
        options = self._options(tmp_path, data_dir=data_dir)

        assert backup_mod._resolve_url(options.db_url, data_dir=options.data_dir) == "postgresql://farna_control:ownerpw@127.0.0.1:15432/farna_control"
        assert backup_mod._resolve_restore_app_url(None, options.app_db_url, data_dir=options.data_dir) == ("postgresql+asyncpg://farna_control_app:apppw@127.0.0.1:15432/farna_control")
        assert backup_mod._restore_fernet_key(options.data_dir) == "launcher-persisted-key"

    def test_an_exported_url_outranks_the_data_dir_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator override still wins over the installation's pinned config."""
        from farna.cli import backup as backup_mod

        data_dir = self._bootstrapped_data_dir(tmp_path)
        monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql://admin:pw@db.example.com:5432/farna_control")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://farna_control_app:pw@db.example.com:5432/farna_control")

        assert backup_mod._resolve_url(None, data_dir=data_dir) == "postgresql://admin:pw@db.example.com:5432/farna_control"
        assert backup_mod._resolve_restore_app_url(None, None, data_dir=data_dir) == "postgresql+asyncpg://farna_control_app:pw@db.example.com:5432/farna_control"

    def test_the_bundled_target_is_recognised_only_on_the_bundled_loopback_endpoint(self) -> None:
        from farna.cli import backup as backup_mod

        launcher_admin = "postgresql+asyncpg://farna_control:ownerpw@127.0.0.1:15432/farna_control"
        assert backup_mod._target_is_bundled_cluster("postgresql://farna_control:ownerpw@127.0.0.1:15432/farna_control", launcher_admin) is True
        assert backup_mod._target_is_bundled_cluster("postgresql://farna_control:ownerpw@localhost:15432/farna_control", launcher_admin) is True
        # A different port, a remote host, an unparseable target, or no launcher
        # config at all (a fresh/migrated data dir) are all "not the bundled
        # cluster": the restore must not start a cluster for them.
        assert backup_mod._target_is_bundled_cluster("postgresql://farna_control:ownerpw@127.0.0.1:5432/farna_control", launcher_admin) is False
        assert backup_mod._target_is_bundled_cluster("postgresql://farna_control:ownerpw@db.example.com:15432/farna_control", launcher_admin) is False
        assert backup_mod._target_is_bundled_cluster("postgresql://[bad:5432/farna_control", launcher_admin) is False
        assert backup_mod._target_is_bundled_cluster("postgresql://farna_control:ownerpw@127.0.0.1:15432/farna_control", None) is False

    def test_live_holder_is_none_off_posix_or_when_the_pid_is_gone(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod
        from farna.launcher import upgrade as upgrade_module

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (tmp_path / "data.lock").write_text(json.dumps({"pid": 4242, "mode": "serve"}), encoding="utf-8")

        monkeypatch.setattr(backup_mod.os, "name", "nt")
        assert backup_mod._live_launcher_holder(data_dir) is None, "off POSIX there is no verified-liveness seam"

        monkeypatch.setattr(backup_mod.os, "name", "posix")
        monkeypatch.setattr(upgrade_module, "_pid_alive", lambda _pid: False)
        assert backup_mod._live_launcher_holder(data_dir) is None, "a stale lock record is not a live launcher"

    def test_a_bundled_restore_keeps_only_the_cluster_up_across_dump_and_replay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stop takes the bundled cluster down, so the window brings it back
        BEFORE the dump and the replay, and closes it again afterwards."""
        import contextlib as contextlib_module

        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = self._bootstrapped_data_dir(tmp_path)
        order: list[str] = []

        @contextlib_module.contextmanager
        def _window(cluster_data_dir: Path, _raw_url: str) -> Any:
            assert cluster_data_dir == data_dir, "the window must serve the data dir the stop tore down"
            order.append("window_up")
            try:
                yield
            finally:
                order.append("window_down")

        monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(backup_mod, "_stop_launcher_writers", lambda _d: order.append("stop") or True)
        monkeypatch.setattr(backup_mod, "_bundled_cluster_for_restore", _window)
        monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: order.append("safety_dump"))
        monkeypatch.setattr(backup_mod, "_restore_cluster", lambda *_a, **_k: order.append("replay"))
        monkeypatch.setattr(backup_mod, "_ensure_restore_posture", lambda *_a, **_k: order.append("posture"))
        monkeypatch.setattr(backup_mod, "_restore_data_dir", lambda *_a, **_k: order.append("adopt"))

        backup_mod._run_restore(self._options(tmp_path, data_dir=data_dir, backup_dir=archive))

        assert order == ["stop", "window_up", "safety_dump", "replay", "posture", "window_down", "adopt"], order

    def test_an_external_target_never_starts_the_bundled_cluster(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A target the stop did not take down must not be shadowed by a cluster
        this command started."""
        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        data_dir = self._bootstrapped_data_dir(tmp_path)
        order: list[str] = []
        window = MagicMock()
        monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
        monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
        monkeypatch.setattr(backup_mod, "_stop_launcher_writers", lambda _d: order.append("stop") or True)
        monkeypatch.setattr(backup_mod, "_bundled_cluster_for_restore", window)
        monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: order.append("safety_dump"))
        monkeypatch.setattr(backup_mod, "_restore_cluster", lambda *_a, **_k: order.append("replay"))
        monkeypatch.setattr(backup_mod, "_ensure_restore_posture", lambda *_a, **_k: order.append("posture"))
        monkeypatch.setattr(backup_mod, "_restore_data_dir", lambda *_a, **_k: order.append("adopt"))
        options = self._options(
            tmp_path,
            db_url="postgresql://admin:pw@db.example.com:5432/farna_control",
            app_db_url=_TEST_APP_DB_URL,
            data_dir=data_dir,
            backup_dir=archive,
        )

        backup_mod._run_restore(options)

        assert window.call_count == 0, "an external target needs no bundled cluster"
        assert order == ["stop", "safety_dump", "replay", "posture", "adopt"], order

    def test_with_an_explicit_url_the_replay_connects_really_after_the_stop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        from farna.cli import backup as backup_mod

        archive = _restore_archive(tmp_path)
        # Only database.sql: the REAL _restore_cluster then does one real
        # connection (via _run_psql) instead of also replaying JSON exports.
        (archive / "checkpoints.json").unlink()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        order: list[str] = []

        # A real listener stands in for the explicit admin database. The REAL
        # _restore_cluster runs below and its _run_psql opens a REAL TCP
        # connection, so the test fails if the target is not reachable after the
        # stop — the exact defect the fully-mocked tests could not catch.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            monkeypatch.setattr(backup_mod, "_validate_targets", lambda *_a, **_k: True)
            monkeypatch.setattr(backup_mod, "_live_launcher_holder", lambda _d: MagicMock(pid=4242))
            monkeypatch.setattr(backup_mod, "_acquire_data_lock", lambda *_a, **_k: MagicMock())
            monkeypatch.setattr(backup_mod, "_safety_dump", lambda *_a, **_k: order.append("safety_dump"))

            def _stop(_data_dir: Path) -> bool:
                # The stop tears down the bundled server; the EXPLICIT target is a
                # different, already-running listener and must stay reachable.
                order.append("stop")
                return True

            monkeypatch.setattr(backup_mod, "_stop_launcher_writers", _stop)

            def _run_psql(_url: str, _input: Path, **_kwargs: Any) -> None:
                with socket.create_connection(("127.0.0.1", port), timeout=2):
                    pass
                order.append("connect")

            monkeypatch.setattr(backup_mod, "_run_psql", _run_psql)
            # The collation probe is a second connection after the SQL replay;
            # it is not the ordering under test.
            monkeypatch.setattr(backup_mod, "_check_collation_versions_sync", lambda _url: None)
            monkeypatch.setattr(backup_mod, "_restore_data_dir", lambda *_a, **_k: None)
            monkeypatch.setattr(backup_mod, "_ensure_restore_posture", lambda *_a, **_k: None)
            monkeypatch.setattr(backup_mod, "get_settings", lambda: type("S", (), {"fernet_key": _TEST_FERNET_KEY, "database_url": _TEST_APP_DB_URL})())

            options = backup_mod._RestoreOptions(
                backup_dir=archive,
                db_url=f"postgresql://admin:pw@127.0.0.1:{port}/farna_control",
                app_db_url=_TEST_APP_DB_URL,
                yes=True,
                previous_fernet_key=None,
                dry_run=False,
                data_dir=data_dir,
                replace_cluster=False,
                no_safety_dump=False,
            )
            backup_mod._run_restore(options)
        finally:
            listener.close()

        # The dump and the replay now run INSIDE the window, i.e. AFTER the stop
        # (the window is what keeps the target reachable past it).
        assert order == ["stop", "safety_dump", "connect"], order


class TestBundledClusterWindowInternals:
    """The pieces the restore window is built from, pinned one at a time.

    ``_run_restore``'s ordering test and the real-connection integration test
    both stand in for ``_bundled_cluster_for_restore`` as a WHOLE, which is how
    a break inside it can stay invisible. These pin its seams: how the target
    endpoint is parsed, whose configuration the child may inherit, how
    "accepting connections" is decided, and that the cluster this window starts
    is stopped on EVERY exit path.
    """

    _RAW_URL = "postgresql://farna_control:ownerpw@127.0.0.1:15432/farna_control"

    @staticmethod
    def _bootstrapped_data_dir(tmp_path: Path) -> Path:
        """A data dir the launcher has initialised (pgdata/PG_VERSION present)."""
        data_dir = tmp_path / "data"
        pgdata = data_dir / "pgdata"
        pgdata.mkdir(parents=True)
        (pgdata / "PG_VERSION").write_text("16\n", encoding="utf-8")
        return data_dir

    @staticmethod
    def _bin_dir(tmp_path: Path) -> Path:
        """A bin dir holding the one binary the window executes."""
        from farna.launcher.initdb import _binary

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        Path(_binary(bin_dir, "postgres")).write_text("#!/bin/sh\n", encoding="utf-8")
        return bin_dir

    def _armed_window(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, MagicMock]:
        """Arm a window that WOULD start a cluster, with a fake child process."""
        from farna.cli import backup as backup_mod
        from farna.launcher import entry as entry_module

        data_dir = self._bootstrapped_data_dir(tmp_path)
        bin_dir = self._bin_dir(tmp_path)
        monkeypatch.setattr(entry_module, "resolve_bin_dir", lambda: bin_dir)
        # The operator's own shell may carry service endpoints; the bundled
        # child must not inherit them (the supervisor's allowlist is the
        # contract this window reuses).
        monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql://leak:leak@evil.example.com/farna_control")
        monkeypatch.setenv("DATABASE_URL", "postgresql://leak:leak@evil.example.com/farna_control")
        monkeypatch.setattr(backup_mod, "_port_accepting", lambda *_a, **_k: False)
        monkeypatch.setattr(backup_mod, "_bundled_cluster_ready", lambda *_a, **_k: True)
        process = MagicMock()
        process.poll.return_value = None
        # A real Popen always has an int pid, and the window records it in the
        # restore-window pid file so a SIGKILLed window is identifiable later.
        process.pid = 4242
        monkeypatch.setattr(backup_mod.subprocess, "Popen", MagicMock(return_value=process))
        return data_dir, process

    def test_the_endpoint_parses_the_port_and_refuses_an_unparseable_url(self) -> None:
        from farna.cli import backup as backup_mod

        assert backup_mod._postgres_endpoint("postgresql://u:p@db.example.com/farna_control") == ("db.example.com", 5432)
        assert backup_mod._postgres_endpoint("postgresql://u:p@127.0.0.1:15432/farna_control") == ("127.0.0.1", 15432)
        # A malformed authority or a non-numeric port is "not a PostgreSQL
        # target" -- never a guessed endpoint the window would start a cluster on.
        assert backup_mod._postgres_endpoint("postgresql://[bad:5432/farna_control") is None
        assert backup_mod._postgres_endpoint("postgresql://u:p@127.0.0.1:notaport/farna_control") is None
        assert backup_mod._postgres_endpoint("not-a-url") is None

    def test_the_launcher_config_supplies_only_the_keys_the_restore_needs(self, tmp_path: Path) -> None:
        from farna.cli import backup as backup_mod

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.env").write_text(
            "\n".join(
                [
                    "# the launcher's pinned configuration",
                    "",
                    'DATABASE_URL="postgresql+asyncpg://app:pw@127.0.0.1:15432/farna_control"',
                    "DATABASE_ADMIN_URL='postgresql+asyncpg://owner:pw@127.0.0.1:15432/farna_control'",
                    "FERNET_KEY=persisted-key",
                    "SECRET_KEY=must-not-be-exposed",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        values = backup_mod._launcher_config_values(data_dir)

        assert values == {
            "DATABASE_URL": "postgresql+asyncpg://app:pw@127.0.0.1:15432/farna_control",
            "DATABASE_ADMIN_URL": "postgresql+asyncpg://owner:pw@127.0.0.1:15432/farna_control",
            "FERNET_KEY": "persisted-key",
        }
        # No data dir (the compose path) and an un-bootstrapped one are both
        # "nothing to read", never an error and never an endpoint.
        assert not backup_mod._launcher_config_values(None)
        assert not backup_mod._launcher_config_values(tmp_path / "missing")

    def test_port_accepting_is_a_real_connection_attempt(self) -> None:
        import socket

        from farna.cli import backup as backup_mod

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            assert backup_mod._port_accepting("127.0.0.1", port) is True
            # Port 1 on loopback: nothing listens there, so the probe reports the
            # endpoint unavailable rather than assuming a server is present.
            assert backup_mod._port_accepting("127.0.0.1", 1) is False
        finally:
            listener.close()

    def test_readiness_without_the_bundled_probe_falls_back_to_a_real_connection(self, tmp_path: Path) -> None:
        import socket

        from farna.cli import backup as backup_mod

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            port = listener.getsockname()[1]
            assert backup_mod._bundled_cluster_ready("127.0.0.1", port, bin_dir) is True
            assert backup_mod._bundled_cluster_ready("127.0.0.1", 1, bin_dir) is False
        finally:
            listener.close()

    def test_readiness_uses_the_bundled_pg_isready_and_its_exit_status(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace

        from farna.cli import backup as backup_mod
        from farna.launcher import initdb

        probe = tmp_path / "pg_isready"
        probe.write_text("", encoding="utf-8")
        monkeypatch.setattr(initdb, "_binary", lambda _bin_dir, _name: str(probe))
        calls: list[list[str]] = []

        def _accepting(argv: list[str], **_kwargs: Any) -> Any:
            calls.append(argv)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(backup_mod.subprocess, "run", _accepting)

        assert backup_mod._bundled_cluster_ready("127.0.0.1", 15432, tmp_path) is True
        assert calls == [[str(probe), "-h", "127.0.0.1", "-p", "15432"]]

        # "Listening but not answering" (57P03 during crash recovery) is exactly
        # what pg_isready reports, and it must keep the replay from starting.
        monkeypatch.setattr(backup_mod.subprocess, "run", lambda *_a, **_k: SimpleNamespace(returncode=1))
        assert backup_mod._bundled_cluster_ready("127.0.0.1", 15432, tmp_path) is False

        def _unexecutable(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("cannot execute the probe")

        monkeypatch.setattr(backup_mod.subprocess, "run", _unexecutable)
        # An unexecutable probe is NOT readiness: fail towards not-replaying.
        assert backup_mod._bundled_cluster_ready("127.0.0.1", 15432, tmp_path) is False

    def test_the_server_log_tail_is_the_last_lines_or_a_clear_marker(self, tmp_path: Path) -> None:
        from farna.cli import backup as backup_mod

        log_path = tmp_path / "restore-postgres.log"
        log_path.write_text("\n".join(f"line-{n}" for n in range(1, 26)), encoding="utf-8")

        assert backup_mod._logged_tail(log_path).splitlines() == [f"line-{n}" for n in range(6, 26)]
        assert backup_mod._logged_tail(tmp_path / "absent.log") == "<no server log>"
        empty = tmp_path / "empty.log"
        empty.write_text("", encoding="utf-8")
        assert backup_mod._logged_tail(empty) == "<server log is empty>"

    def test_the_window_waits_for_an_accepting_cluster_and_fails_loudly_otherwise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        log_path = tmp_path / "restore-postgres.log"
        log_path.write_text("FATAL: could not bind to address\n", encoding="utf-8")
        readiness = MagicMock(return_value=True)
        monkeypatch.setattr(backup_mod, "_bundled_cluster_ready", readiness)
        process = MagicMock()
        process.poll.return_value = None

        backup_mod._await_bundled_cluster(process, "127.0.0.1", 15432, tmp_path, log_path)

        readiness.assert_called_once_with("127.0.0.1", 15432, tmp_path)

        # A cluster that EXITS before answering is reported with its own log
        # tail: the operator needs the reason, not a bare timeout.
        process.poll.return_value = 3
        with pytest.raises(click.ClickException) as exited:
            backup_mod._await_bundled_cluster(process, "127.0.0.1", 15432, tmp_path, log_path)
        assert "status 3" in exited.value.message
        assert "FATAL: could not bind to address" in exited.value.message

        # And the wait is bounded: with the deadline already spent it refuses
        # instead of hanging on a cluster that never answers.
        monkeypatch.setattr(backup_mod, "_RESTORE_PG_READY_TIMEOUT", 0.0)
        monkeypatch.setattr(backup_mod, "_bundled_cluster_ready", MagicMock(return_value=False))
        with pytest.raises(click.ClickException) as timed_out:
            backup_mod._await_bundled_cluster(process, "127.0.0.1", 15432, tmp_path, log_path)
        assert "did not accept connections" in timed_out.value.message

    def test_the_window_polls_a_cluster_that_is_not_ready_yet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        log_path = tmp_path / "restore-postgres.log"
        log_path.write_text("", encoding="utf-8")
        readiness = MagicMock(side_effect=[False, True])
        monkeypatch.setattr(backup_mod, "_bundled_cluster_ready", readiness)
        monkeypatch.setattr(backup_mod, "_RESTORE_PROBE_INTERVAL", 0.0)
        process = MagicMock()
        process.poll.return_value = None

        backup_mod._await_bundled_cluster(process, "127.0.0.1", 15432, tmp_path, log_path)

        # "Not ready on the first probe" is a POLL, not a failure: a cluster that
        # is still starting must be waited for, not reported as dead.
        assert readiness.call_count == 2

    def test_stopping_the_cluster_escalates_and_never_leaves_a_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        log_path = Path(backup_mod._RESTORE_PG_LOG)

        # Already gone: nothing to signal, nothing to escalate.
        exited = MagicMock()
        exited.poll.return_value = 0
        backup_mod._stop_bundled_cluster(exited, log_path)
        exited.terminate.assert_not_called()
        exited.kill.assert_not_called()

        # POSIX fast shutdown (SIGINT) is the first rung on the packaged host.
        monkeypatch.setattr(sys, "platform", "linux")
        graceful = MagicMock()
        graceful.poll.return_value = None
        backup_mod._stop_bundled_cluster(graceful, log_path)
        graceful.send_signal.assert_called_once_with(signal.SIGINT)
        graceful.terminate.assert_not_called()

        # A fast shutdown that does not finish escalates terminate -> kill.
        stubborn = MagicMock()
        stubborn.poll.return_value = None
        stubborn.wait.side_effect = [
            subprocess.TimeoutExpired("postgres", 60),
            subprocess.TimeoutExpired("postgres", 30),
            None,
        ]
        backup_mod._stop_bundled_cluster(stubborn, log_path)
        stubborn.terminate.assert_called_once()
        stubborn.kill.assert_called_once()

    def test_the_window_starts_only_postgres_with_the_supervisors_argv_and_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod
        from farna.launcher import entry as entry_module
        from farna.launcher.initdb import postgres_server_argv

        data_dir, process = self._armed_window(tmp_path, monkeypatch)
        popen = backup_mod.subprocess.Popen

        with backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            pass

        bin_dir = entry_module.resolve_bin_dir()
        assert popen.call_args.args[0] == postgres_server_argv(data_dir / "pgdata", entry_module.POSTGRES_HOST, 15432, bin_dir)
        child_env = popen.call_args.kwargs["env"]
        assert "DATABASE_ADMIN_URL" not in child_env
        assert "DATABASE_URL" not in child_env
        assert "PATH" in child_env
        assert (data_dir / backup_mod._RESTORE_PG_LOG).exists()
        # The cluster this window started is stopped again when it closes.
        process.wait.assert_called_once()

    def test_the_window_stops_the_cluster_even_when_the_restore_body_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        data_dir, process = self._armed_window(tmp_path, monkeypatch)

        with pytest.raises(RuntimeError, match="replay blew up"), backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            raise RuntimeError("replay blew up")

        process.wait.assert_called_once()

    def test_the_window_reuses_a_cluster_that_is_already_accepting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        data_dir = self._bootstrapped_data_dir(tmp_path)
        monkeypatch.setattr(backup_mod, "_port_accepting", lambda *_a, **_k: True)
        popen = MagicMock()
        monkeypatch.setattr(backup_mod.subprocess, "Popen", popen)

        with backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            pass

        # A cluster that was ALREADY up is not this command's to stop, and is
        # not restarted: the restore only stops what it started.
        popen.assert_not_called()

    def test_the_window_reports_a_cluster_it_cannot_spawn(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        data_dir, _process = self._armed_window(tmp_path, monkeypatch)

        def _cannot_spawn(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("permission denied")

        monkeypatch.setattr(backup_mod.subprocess, "Popen", _cannot_spawn)
        # A spawn failure names the binary and shows the server log's tail, so
        # the operator can tell a missing permission from a broken cluster.
        with pytest.raises(click.ClickException, match="Cannot start the bundled PostgreSQL"), contextlib.ExitStack() as stack:
            stack.enter_context(backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL))

    def test_the_window_refuses_a_data_dir_it_cannot_serve(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod
        from farna.launcher import entry as entry_module

        monkeypatch.setattr(backup_mod, "_port_accepting", lambda *_a, **_k: False)
        # Not bootstrapped: there is no cluster to bring back, and the refusal
        # tells the operator how to get one.
        empty_data_dir = tmp_path / "fresh"
        empty_data_dir.mkdir()
        with pytest.raises(click.ClickException, match="no initialised cluster"), contextlib.ExitStack() as stack:
            stack.enter_context(backup_mod._bundled_cluster_for_restore(empty_data_dir, self._RAW_URL))

        # A target that is not a PostgreSQL endpoint at all is refused: starting
        # a cluster for a URL this command cannot parse would be a guess.
        with pytest.raises(click.ClickException, match="Cannot parse"), contextlib.ExitStack() as stack:
            stack.enter_context(backup_mod._bundled_cluster_for_restore(tmp_path / "unused", "not-a-url"))

        # Bootstrapped, but this host has no bundled postgres binary.
        data_dir = self._bootstrapped_data_dir(tmp_path)
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        monkeypatch.setattr(entry_module, "resolve_bin_dir", lambda: empty_bin)
        with pytest.raises(click.ClickException, match="missing at"), contextlib.ExitStack() as stack:
            stack.enter_context(backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL))

    def test_the_window_records_the_child_pid_and_clears_it_on_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SIGKILL runs no handler, so the pid file IS the record of the child."""
        from farna.cli import backup as backup_mod
        from farna.launcher.supervisor import RESTORE_WINDOW_PIDFILE

        data_dir, _process = self._armed_window(tmp_path, monkeypatch)
        pidfile = data_dir / RESTORE_WINDOW_PIDFILE

        with backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            # Recorded while the cluster is up (before the readiness wait), so a
            # kill at ANY point in the window leaves an identifiable pid.
            assert pidfile.read_text(encoding="ascii").strip() == "4242"

        assert not pidfile.exists(), "a window that stopped its cluster leaves no record behind"

    def test_the_window_handles_the_stop_signals_and_restores_the_previous_handlers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from farna.cli import backup as backup_mod

        data_dir, _process = self._armed_window(tmp_path, monkeypatch)
        before = {signum: signal.getsignal(signum) for signum in backup_mod._WINDOW_SIGNALS}

        with backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            inside = {signum: signal.getsignal(signum) for signum in backup_mod._WINDOW_SIGNALS}

        after = {signum: signal.getsignal(signum) for signum in backup_mod._WINDOW_SIGNALS}
        # Every stop signal is covered for the window, and the operator's
        # handlers (pytest's, the shell's) are back afterwards.
        assert all(inside[signum] is not before[signum] for signum in backup_mod._WINDOW_SIGNALS)
        assert after == before

    def test_a_real_sigterm_stops_the_cluster_before_the_window_unwinds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A timeout/OOM/service-manager stop must not leave the postmaster up."""
        from farna.cli import backup as backup_mod
        from farna.launcher.supervisor import RESTORE_WINDOW_PIDFILE

        if sys.platform == "win32":
            pytest.skip("no POSIX signal delivery to the test process")
        data_dir, process = self._armed_window(tmp_path, monkeypatch)
        pidfile = data_dir / RESTORE_WINDOW_PIDFILE

        with pytest.raises(KeyboardInterrupt), backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            os.kill(os.getpid(), signal.SIGTERM)

        # The handler stopped the child itself, then the ordinary exit path ran.
        process.send_signal.assert_called_with(signal.SIGINT)
        process.wait.assert_called()
        assert not pidfile.exists()

    def test_a_second_signal_does_not_interrupt_the_stop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """A second Ctrl-C must not abort the shutdown it is trying to reach."""
        from farna.cli import backup as backup_mod

        if sys.platform == "win32":
            pytest.skip("no POSIX signal delivery to the test process")
        data_dir, _process = self._armed_window(tmp_path, monkeypatch)
        real_stop = backup_mod._stop_bundled_cluster
        stop_calls: list[int] = []

        def _stop_and_signal_again(proc: Any, log_path: Path) -> None:
            stop_calls.append(1)
            if len(stop_calls) == 1:
                os.kill(os.getpid(), signal.SIGTERM)
            real_stop(proc, log_path)

        monkeypatch.setattr(backup_mod, "_stop_bundled_cluster", _stop_and_signal_again)

        with caplog.at_level(logging.WARNING, logger="farna.cli.backup"), pytest.raises(KeyboardInterrupt), backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
            os.kill(os.getpid(), signal.SIGTERM)

        assert stop_calls, "the handler must run the real stop"
        assert "restore.window_signal_ignored" in caplog.text

    def test_a_leftover_window_postmaster_is_stopped_by_pid_before_the_window(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """The SIGKILL residual: the NEXT restore stops the recorded pid itself."""
        from farna.cli import backup as backup_mod
        from farna.launcher.supervisor import RESTORE_WINDOW_PIDFILE

        if sys.platform == "win32":
            pytest.skip("the packaged native launcher is POSIX-only; Windows cannot signal a foreign pid")
        data_dir = self._bootstrapped_data_dir(tmp_path)
        # An ORPHAN in its OWN SESSION (its parent exits, and it holds no pipe
        # back to this process) — exactly how the window spawns its postmaster —
        # with "postgres" as its last argv entry, which is what the identity gate
        # reads: the record is only honoured for a process that is still postgres.
        launcher = "import subprocess, sys; print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', 'postgres'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)"
        orphan_pid = int(subprocess.check_output([sys.executable, "-c", launcher], text=True, timeout=30).strip())  # noqa: S603 - fixed argv, test-controlled
        time.sleep(0.5)  # let the orphan bind its cmdline before it is inspected
        pidfile = data_dir / RESTORE_WINDOW_PIDFILE
        pidfile.write_text(f"{orphan_pid}\n", encoding="ascii")
        monkeypatch.setattr(backup_mod, "_port_accepting", lambda *_a, **_k: True)
        try:
            with backup_mod._bundled_cluster_for_restore(data_dir, self._RAW_URL):
                pass

            out = capsys.readouterr().out
            assert f"PID {orphan_pid}" in out, "the leftover is reported by pid, not a port-in-use mystery"
            # Fast shutdown, and the record goes with it.
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and _pid_running(orphan_pid):
                time.sleep(0.05)
            assert not _pid_running(orphan_pid), "the leftover postmaster must actually be stopped"
            assert not pidfile.exists()
        finally:
            with contextlib.suppress(OSError):
                os.kill(orphan_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
