"""Unit tests for scripts/run_check_pnpm_lock.py (frontend lockfile gate).

The gate shells out to ``pnpm install --frozen-lockfile --ignore-scripts`` in
``frontend/`` and maps the exit code 1:1 — the same command CI's frontend and
merge-queue-parity jobs run.  These tests pin the exact argv/cwd, both verdict
arms, and the fail-closed paths (a gate that cannot run must never pass).

They also pin the Windows shim handling: ``shutil.which("pnpm")`` resolves to
``pnpm.CMD``, which ``CreateProcess`` cannot launch, so a naive
``subprocess.run(["pnpm", ...])`` raises ``FileNotFoundError`` on every Windows
push and reports "pnpm is not installed" while pnpm is installed.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_check_pnpm_lock.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_check_pnpm_lock.py)")

_loader = SourceFileLoader("run_check_pnpm_lock", str(script_path))
_spec = spec_from_loader("run_check_pnpm_lock", _loader)
assert _spec is not None
mod = module_from_spec(_spec)
_loader.exec_module(mod)


class _Completed:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_pnpm(monkeypatch, resolved: str | None = "/usr/local/bin/pnpm") -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda _name: resolved)


def _install_run(monkeypatch, result: _Completed) -> list[tuple[list[str], dict]]:
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        return result

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# argv resolution (POSIX binary vs Windows .CMD shim)
# ---------------------------------------------------------------------------
def test_resolve_pnpm_argv_runs_the_exact_ci_command(monkeypatch):
    _install_pnpm(monkeypatch, "/usr/local/bin/pnpm")
    assert mod.resolve_pnpm_argv() == [
        "/usr/local/bin/pnpm",
        "install",
        "--frozen-lockfile",
        "--ignore-scripts",
    ]


def test_resolve_pnpm_argv_routes_a_windows_shim_through_the_command_interpreter(monkeypatch):
    _install_pnpm(monkeypatch, r"C:\Program Files\nodejs\pnpm.CMD")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    argv = mod.resolve_pnpm_argv()
    assert argv is not None
    assert argv[:3] == [r"C:\Windows\System32\cmd.exe", "/c", r"C:\Program Files\nodejs\pnpm.CMD"]
    assert argv[3:] == ["install", "--frozen-lockfile", "--ignore-scripts"]


def test_resolve_pnpm_argv_returns_none_when_pnpm_is_absent(monkeypatch):
    _install_pnpm(monkeypatch, None)
    assert mod.resolve_pnpm_argv() is None


# ---------------------------------------------------------------------------
# Verdict arms
# ---------------------------------------------------------------------------
def test_main_returns_zero_when_the_lockfile_is_in_sync(monkeypatch, capsys):
    monkeypatch.setattr(mod, "FRONTEND_DIR", Path("/repo/frontend"))
    calls = _install_run(monkeypatch, _Completed(0, stdout="Lockfile is up to date\nDone in 41.2s\n"))
    _install_pnpm(monkeypatch)

    assert mod.main() == 0

    assert calls[0][1]["cwd"] == str(Path("/repo/frontend"))
    assert calls[0][1]["check"] is False
    assert calls[0][1]["timeout"] == mod._TIMEOUT_SECONDS
    assert "in sync and installs cleanly" in capsys.readouterr().err


def test_main_returns_one_on_lockfile_drift(monkeypatch, capsys):
    _install_pnpm(monkeypatch)
    _install_run(monkeypatch, _Completed(1, stderr='[ERR_PNPM_OUTDATED_LOCKFILE] Cannot install with "frozen-lockfile"\n'))

    assert mod.main() == 1

    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "out of date with frontend/package.json" in err
    assert "pnpm install --lockfile-only" in err
    assert "Rerun locally with:" in err


def test_main_reports_a_non_lockfile_failure_without_the_drift_hint(monkeypatch, capsys):
    """A network/registry failure is not lockfile drift; do not mislabel it."""
    _install_pnpm(monkeypatch)
    _install_run(monkeypatch, _Completed(1, stderr="ERR_PNPM_META_FETCH_FAIL\n"))

    assert mod.main() == 1

    err = capsys.readouterr().err
    assert "Fix the install failure above" in err
    assert "out of date with frontend/package.json" not in err


def test_main_prints_the_tail_of_a_successful_install(monkeypatch, capsys):
    _install_pnpm(monkeypatch)
    _install_run(monkeypatch, _Completed(0, stdout="line-1\nline-2\nline-3\nline-4\nline-5\nline-6\nline-7\n"))

    assert mod.main() == 0

    err = capsys.readouterr().err
    assert "line-7" in err
    assert "line-1" not in err


# ---------------------------------------------------------------------------
# Fail-closed paths
# ---------------------------------------------------------------------------
def test_main_fails_closed_when_pnpm_is_missing(monkeypatch, capsys):
    _install_pnpm(monkeypatch, None)
    calls = _install_run(monkeypatch, _Completed(0))

    assert mod.main() == 1

    assert not calls
    err = capsys.readouterr().err
    assert "not installed or not on PATH" in err
    assert "failing closed" in err


def test_main_fails_closed_on_timeout(monkeypatch, capsys):
    _install_pnpm(monkeypatch)

    def fake_run(cmd, **kwargs):
        raise mod.subprocess.TimeoutExpired(cmd, mod._TIMEOUT_SECONDS)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.main() == 1

    err = capsys.readouterr().err
    assert "did not finish within" in err
    assert "failing closed" in err


def test_main_fails_closed_on_oserror(monkeypatch, capsys):
    _install_pnpm(monkeypatch)

    def fake_run(cmd, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod.main() == 1

    err = capsys.readouterr().err
    assert "could not run `pnpm install --frozen-lockfile`" in err
    assert "failing closed" in err
