"""FAR-2850: the ``start --detach`` handshake must mean "the stack is usable".

The detach handshake used to be written in ``_serve_until_shutdown`` BEFORE the
API lifespan ran, so ``ready`` meant "uvicorn is about to start". The app role
``farna_control_app`` (created by ``bootstrap_roles`` inside the lifespan) and
the schema at head did not exist yet, so a seed issued straight afterwards
failed with 28P01.

These tests pin the fixed contract:

* the launcher defers ``ready`` to the API lifespan callback, after its boot
  work creates the role and migrates the schema;
* ``_serve_api`` writes ``ready`` only when the API lifespan fires the one-shot
  startup-ready callback, and fails the handshake closed with a clear message
  when startup never completes;
* the API lifespan fires that callback at the very end of its startup phase.
"""

from __future__ import annotations

import os
import sys
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import farna.api.main as main
import farna.launcher.entry as entry_module
from farna.launcher.entry import run_start


class _ServePhaseSupervisor:
    """Minimal supervisor double for the serve-phase teardown helpers."""

    degraded_reason: str | None = None

    def shutdown(self) -> None:
        return None

    def monitor_thread_alive(self) -> bool:
        return True


def _use_posix_detach_path_with_a_fake_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the POSIX detach path without forking or running a real boot.

    Both forks take the grandchild branch and ``_run_foreground`` is replaced
    only to skip the heavy boot (initdb, Postgres, Redis). The replacement
    delegates to the REAL ``_serve_until_shutdown`` so the readiness-handshake
    decision under test is the production one.
    """
    monkeypatch.setattr(sys, "platform", "linux")  # POSIX detach path on any host
    # os.fork / os.setsid are POSIX-only attributes; define them on Windows too.
    monkeypatch.setattr(os, "fork", lambda: 0, raising=False)  # both forks take the child path
    monkeypatch.setattr(os, "setsid", lambda: None, raising=False)
    monkeypatch.setattr(entry_module, "_redirect_stdio", lambda log: None)

    def _run_foreground(
        data_dir: Any,
        *,
        bin_dir: Any = None,
        clear_degraded: bool = False,
        serve: Any = None,
        ready_fd: int | None = None,
    ) -> int:
        return entry_module._serve_until_shutdown(
            _ServePhaseSupervisor(),
            entry_module._RunState(),
            SimpleNamespace(api_port=18000),
            serve=serve,
            ready_fd=ready_fd,
            degraded_path=data_dir / "degraded.json",
            data_dir=data_dir,
        )

    monkeypatch.setattr(entry_module, "_run_foreground", _run_foreground)


def test_detach_does_not_report_ready_before_the_serve_phase_creates_the_app_role(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Regression (FAR-2850): ``ready`` must not fire before the app role exists.

    The serve phase (the API lifespan in production) creates the app role
    lazily, so the detached handshake must not be written before it is entered.
    On the pre-fix code the handshake was written first and this test fails.
    """
    _use_posix_detach_path_with_a_fake_boot(monkeypatch)

    role_usable: list[bool] = []
    ready_written_before_role: list[bool] = []

    real_write = os.write

    def _spy_write(fd: int, data: bytes) -> int:
        if bytes(data) == b"ready\n" and not role_usable:
            ready_written_before_role.append(True)
        try:
            return real_write(fd, data)
        except OSError:
            return 0

    monkeypatch.setattr(os, "write", _spy_write)

    def _fake_serve_api(host: str, port: int, stop_event: threading.Event, force_exit: threading.Event, *, ready_fd: int | None = None) -> None:
        # Entering the serve phase is where the API lifespan runs bootstrap_roles;
        # the app role must not exist yet, and only then may ``ready`` fire.
        assert not role_usable
        role_usable.append(True)
        if ready_fd is not None:
            os.write(ready_fd, b"ready\n")

    monkeypatch.setattr(entry_module, "_serve_api", _fake_serve_api)

    code = run_start(tmp_path / "data", detach=True)

    assert code == 0
    assert role_usable == [True]
    # FAILS on the pre-FAR-2850 code, which wrote ``ready`` before the serve phase.
    assert not ready_written_before_role


def _stopped_events() -> tuple[threading.Event, threading.Event]:
    """A stop/force-exit pair already set, so the serve watcher exits at once."""
    stop_event = threading.Event()
    stop_event.set()
    force_exit = threading.Event()
    force_exit.set()
    return stop_event, force_exit


def test_serve_api_writes_ready_only_when_the_lifespan_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ready`` is written when the startup-ready callback fires, not before."""
    import uvicorn

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            self.started = False
            self.should_exit = False
            self.force_exit = False

        def run(self) -> None:
            # The API lifespan finished its startup phase: fire the one-shot signal.
            main._signal_startup_ready()

    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)

    stop_event, force_exit = _stopped_events()
    read_fd, write_fd = os.pipe()
    try:
        entry_module._serve_api("127.0.0.1", 18000, stop_event, force_exit, ready_fd=write_fd)
        os.close(write_fd)
        write_fd = None
        handshake = os.read(read_fd, 4096)
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)

    assert handshake == b"ready\n"
    assert main._startup_ready_callback is None


def test_serve_api_fails_the_handshake_closed_when_startup_never_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A startup that never completes fails the handshake loudly with the log hint."""
    import uvicorn

    class _NeverStartedServer:
        def __init__(self, config: Any) -> None:
            self.started = False
            self.should_exit = False
            self.force_exit = False

        def run(self) -> None:
            # Startup aborted: the lifespan never reaches the readiness signal.
            return None

    monkeypatch.setattr(uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(uvicorn, "Server", _NeverStartedServer)

    stop_event, force_exit = _stopped_events()
    read_fd, write_fd = os.pipe()
    try:
        entry_module._serve_api("127.0.0.1", 18000, stop_event, force_exit, ready_fd=write_fd)
        os.close(write_fd)
        write_fd = None
        handshake = os.read(read_fd, 4096)
    finally:
        if write_fd is not None:
            os.close(write_fd)
        os.close(read_fd)

    assert handshake.startswith(b"failed: ")
    assert b"launcher.log" in handshake
    assert main._startup_ready_callback is None


@pytest.mark.anyio
async def test_lifespan_fires_the_startup_ready_signal_at_the_end_of_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API lifespan fires the launcher's readiness callback at the end of startup.

    Every boot seam is patched so this exercises only the lifespan's control
    flow: the callback must fire during startup (before serving begins) and be
    consumed exactly once.
    """
    fired: list[bool] = []
    main.set_startup_ready_callback(lambda: fired.append(True))
    try:
        monkeypatch.setattr(main, "get_settings", lambda: object())
        monkeypatch.setattr(main, "_configure_license_and_otel", lambda settings: None)
        monkeypatch.setattr(main, "_discover_plugins", lambda settings: None)
        monkeypatch.setattr(main, "_configure_capacity_platform_probe", lambda settings: None)
        monkeypatch.setattr(main, "_run_boot_guards_and_seeds", AsyncMock())
        monkeypatch.setattr(main, "_register_shutdown_manager", lambda settings: None)
        monkeypatch.setattr(main, "_start_background_tasks", AsyncMock(return_value={}))
        monkeypatch.setattr(main, "_teardown_tasks", AsyncMock())
        monkeypatch.setattr("farna.core.license_bootstrap.restore_persisted_license", AsyncMock())
        monkeypatch.setattr("farna.core.health_alerts.install_event_loop_stall_attribution", MagicMock())

        async with main._lifespan(main.app):
            assert fired == [True]
            assert main._startup_ready_callback is None
    finally:
        main.set_startup_ready_callback(None)

    # One-shot: a second signal attempt after startup cannot fire it again.
    main._signal_startup_ready()
    assert fired == [True]
