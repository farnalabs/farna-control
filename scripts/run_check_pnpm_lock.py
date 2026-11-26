#!/usr/bin/env python3
"""Cross-platform pre-push wrapper for the frontend pnpm lockfile freshness check.

Runs ``pnpm install --frozen-lockfile --ignore-scripts`` from ``frontend/`` —
byte-for-byte the command CI's ``Frontend and WCAG`` and ``Frontend unit suite
(merge-queue parity)`` jobs run.  Exit 0 when the lockfile is in sync with the
manifests, 1 when it has drifted.

Why this exists
---------------
``ERR_PNPM_OUTDATED_LOCKFILE`` was a recurring red-CI push: three failed
``pnpm install --frozen-lockfile`` steps in the frontend job and three more in
the merge-queue-parity job in the last 80 failed runs.  Every one was a
``frontend/package.json`` (or ``frontend/pnpm-workspace.yaml``) edit without the
matching ``frontend/pnpm-lock.yaml`` update — deterministic, and detectable
before the push in under two seconds.

Note the two manifests that feed the lockfile: pnpm v10+ reads ``overrides``
ONLY from ``frontend/pnpm-workspace.yaml``, so an override added to
``package.json`` silently changes nothing — and a ``pnpm-workspace.yaml``
override that was not installed produces exactly the lockfile drift this gate
reports.

This gate is FAIL-CLOSED: if it cannot be executed at all (pnpm missing, a
timeout, an unexpected failure) it exits 1 rather than passing silently.  Only
``--frozen-lockfile`` is ever used, so running this gate can never "fix" the
drift it is reporting.

Usage::

    uv run --project backend --no-sync --frozen python scripts/run_check_pnpm_lock.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"

# A cold install (empty pnpm store) can take several minutes on a slow link;
# a warm one is ~40 s.  The bound makes a hang a reported failure.
_TIMEOUT_SECONDS = 900

# The exact command, kept as a constant so the rerun hint and the invocation
# can never drift apart.
_INSTALL_ARGS: tuple[str, ...] = ("install", "--frozen-lockfile", "--ignore-scripts")

_FIX_HINT = "frontend/pnpm-lock.yaml is out of date with frontend/package.json (or frontend/pnpm-workspace.yaml). Regenerate it with `cd frontend && pnpm install --lockfile-only` and commit frontend/pnpm-lock.yaml."

_OUTDATED_MARKER = "ERR_PNPM_OUTDATED_LOCKFILE"

# Keep a successful install's output short: a full `pnpm install` transcript is
# dozens of lines on every push.  A failure prints everything.
_SUCCESS_TAIL_LINES = 5


def _rerun_command() -> str:
    return "cd frontend && pnpm " + " ".join(_INSTALL_ARGS)


def _fail(message: str) -> int:
    print(f"FAILED: {message}", file=sys.stderr)
    print(f"Rerun locally with:\n    {_rerun_command()}", file=sys.stderr)
    return 1


def _tail(text: str, lines: int) -> str:
    """Return the last *lines* non-empty lines of *text* (with a trailing newline)."""
    kept = [line for line in text.splitlines() if line.strip()][-lines:]
    return "\n".join(kept) + ("\n" if kept else "")


def resolve_pnpm_argv() -> list[str] | None:
    """Return the argv that runs pnpm, or ``None`` when pnpm is unavailable.

    On Windows ``shutil.which("pnpm")`` resolves to ``pnpm.CMD``, which
    ``CreateProcess`` cannot launch directly — ``subprocess.run(["pnpm", ...])``
    raises ``FileNotFoundError`` even though pnpm is installed.  Route a
    ``.cmd``/``.bat`` shim through the command interpreter.  The arguments are
    module constants (never user input), so there is nothing to inject.
    """
    resolved = shutil.which("pnpm")
    if resolved is None:
        return None
    argv = [resolved, *_INSTALL_ARGS]
    if Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        argv = [comspec, "/c", *argv]
    return argv


def main() -> int:
    print("Checking frontend pnpm lockfile freshness...", file=sys.stderr)

    argv = resolve_pnpm_argv()
    if argv is None:
        return _fail("`pnpm` is not installed or not on PATH - cannot verify frontend/pnpm-lock.yaml (failing closed).")

    try:
        result = subprocess.run(  # NOSONAR - fixed argv, no shell
            argv,
            cwd=str(FRONTEND_DIR),
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return _fail(f"`pnpm install --frozen-lockfile` did not finish within {_TIMEOUT_SECONDS}s (failing closed).")
    except OSError as exc:
        return _fail(f"could not run `pnpm install --frozen-lockfile`: {exc} (failing closed).")

    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if _OUTDATED_MARKER in combined:
            return _fail(f"{_FIX_HINT} (`pnpm install --frozen-lockfile` exited {result.returncode})")
        return _fail(f"`pnpm install --frozen-lockfile --ignore-scripts` failed (exited {result.returncode}). Fix the install failure above; a lockfile that cannot install cleanly cannot be merged.")

    sys.stderr.write(_tail(combined, _SUCCESS_TAIL_LINES))
    print("frontend/pnpm-lock.yaml is in sync and installs cleanly with --frozen-lockfile", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
