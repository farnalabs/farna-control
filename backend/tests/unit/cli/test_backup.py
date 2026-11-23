"""Tests for the farna-control backup/restore CLI tool."""

from __future__ import annotations

import hashlib
import json
import shutil
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


# ── _db_has_modulo_tables ─────────────────────────────────────────────────────


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

    def test_container_start_command_is_the_compose_up(self, capsys: pytest.CaptureFixture[str]) -> None:
        from farna.cli.backup import _print_start_guidance

        _print_start_guidance(self._options(data_dir=None), stopped=False)
        out = capsys.readouterr().out
        assert "docker compose -f deploy/compose/docker-compose.prod.yml up -d modulo" in out
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
