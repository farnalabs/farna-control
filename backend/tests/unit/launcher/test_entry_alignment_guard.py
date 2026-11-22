"""FAR-2659: the native launcher refuses a database from a newer release.

``farna.launcher.entry._refuse_on_database_ahead`` is the launcher's thin
wrapper around the shared ``farna.db.alignment`` guard. It must:

* return the guard's distinct exit status and the refusal line when the
  database is ahead (after tearing the supervisor down);
* return ``None`` (proceed) when the database is behind or at head;
* fail OPEN when the check itself cannot run.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from farna.db import alignment
from farna.launcher import entry


def _supervisor() -> MagicMock:
    return MagicMock()


def test_ahead_returns_distinct_status_and_shuts_the_supervisor_down(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    supervisor = _supervisor()

    def _ahead(_url: str) -> alignment.AlignmentResult:
        raise alignment.DatabaseAheadError("REFUSED: database revision 0359_new is not known to this release")

    monkeypatch.setattr(alignment, "database_url_from_env", lambda: "postgresql://example/db")
    monkeypatch.setattr(alignment, "ensure_release_aligned", _ahead)

    status = entry._refuse_on_database_ahead(supervisor, ready_fd=None)

    assert status == alignment.EXIT_DATABASE_AHEAD
    supervisor.shutdown.assert_called_once()
    assert "0359_new" in capsys.readouterr().err


def test_ahead_also_reports_the_refusal_on_the_detached_handshake(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()

    def _ahead(_url: str) -> alignment.AlignmentResult:
        raise alignment.DatabaseAheadError("REFUSED: database revision 0359_new is not known to this release")

    monkeypatch.setattr(alignment, "database_url_from_env", lambda: "postgresql://example/db")
    monkeypatch.setattr(alignment, "ensure_release_aligned", _ahead)

    try:
        entry._refuse_on_database_ahead(_supervisor(), ready_fd=write_fd)
    finally:
        os.close(write_fd)
    try:
        handshake = os.read(read_fd, 4096).decode()
    finally:
        os.close(read_fd)

    assert handshake.startswith("failed: ")
    assert "0359_new" in handshake


def test_aligned_database_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = _supervisor()
    monkeypatch.setattr(alignment, "database_url_from_env", lambda: "postgresql://example/db")
    monkeypatch.setattr(alignment, "ensure_release_aligned", lambda _url: alignment.check_alignment({"0001"}, {"0001", "0002"}))

    assert entry._refuse_on_database_ahead(supervisor, ready_fd=None) is None
    supervisor.shutdown.assert_not_called()


def test_unrunnable_check_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = _supervisor()

    def _cannot(_url: str) -> alignment.AlignmentResult:
        raise alignment.AlignmentCheckError("connection refused")

    monkeypatch.setattr(alignment, "database_url_from_env", lambda: "postgresql://example/db")
    monkeypatch.setattr(alignment, "ensure_release_aligned", _cannot)

    assert entry._refuse_on_database_ahead(supervisor, ready_fd=None) is None
    supervisor.shutdown.assert_not_called()


def test_no_database_url_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alignment, "database_url_from_env", lambda: None)

    assert entry._refuse_on_database_ahead(_supervisor(), ready_fd=None) is None


def test_service_unit_does_not_restart_into_the_same_refusal(tmp_path: Path) -> None:
    """The systemd unit must not loop on the ahead-refusal status (FAR-2659)."""
    from farna.launcher.service import render_unit

    unit = render_unit(tmp_path / "farna-control")

    assert f"RestartPreventExitStatus={alignment.EXIT_DATABASE_AHEAD}" in unit
