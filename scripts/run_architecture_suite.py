#!/usr/bin/env python3
"""Run the backend architecture suite locally, exactly as CI runs it.

CI's ``Test (Backend) non-unit suites`` job ran an architecture scan that failed
27 times in the last 80 failed CI runs — by far the largest single class of
red-CI push. Every one of those was a deterministic structural gate (raw SQL
f-strings, sensitive-data logging, product-map/testid drift, login-field drift,
Dockerfile Python-version drift, ...) that could have been caught before the
push. This script is the pre-push half of that job; the ``prepush-architecture``
hook wires it behind ``run_prepush_gated_check.py`` so it only runs when
``backend/src`` or ``frontend/src`` changed.

The suite needs no database and no Docker, so it is safe to run in the E2B
sandbox and on a developer machine alike.

Why a dedicated runner rather than the raw pytest line
-----------------------------------------------------
Two things the raw command cannot do:

1. **Give pytest its own temp root.** pytest derives ``tmp_path`` roots from
   ``PYTEST_DEBUG_TEMPROOT`` (else the global ``pytest-of-<user>`` directory).
   On a machine where that global root carries a stale/dangling
   ``pytest-current`` symlink, pytest's numbered-directory cleanup raises
   ``PermissionError`` at session end and the run exits **1 with every test
   green** — a false failure that makes the gate untrustworthy. A per-run root
   also stops two concurrent agents sharing one machine from fighting over the
   same numbered directories.
2. **Print the exact command to rerun** on failure, so a red pre-push gate is
   actionable without reading ``.pre-commit-config.yaml``.

Scope guard (never the full backend suite)
------------------------------------------
``--scope`` may only name a path inside ``tests/architecture``. Anything wider
(``tests/``, ``.``, ``backend/tests``) is refused loudly with exit 2: this gate
exists to run one small suite, and silently widening it would put a
40-minute suite behind every push.

Docker: not required, nothing is deferred.

Usage::

    uv run --project backend --no-sync --frozen python scripts/run_architecture_suite.py
    uv run --project backend --no-sync --frozen python scripts/run_architecture_suite.py --list
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

# The only tree this gate may run. A gate that can be widened to the whole
# backend is a gate that eventually runs the whole backend on every push.
_PERMITTED_SCOPE_ROOT = "tests/architecture"

# Wall-clock bound. CI gives the whole non-unit job 30 minutes; the suite is a
# few minutes warm. A bound makes a hung run a reported failure instead of a
# session-killing wait.
_DEFAULT_TIMEOUT_SECONDS = 1800

# Byte-for-byte the CI invocation (ci.yml "Run architecture tests"), plus the
# FAR-2308 ``--frozen`` lock-safety flag that never changes what is analysed.
_PYTEST_ARGS: tuple[str, ...] = (
    "pytest",
    "{scope}",
    "-q",
    "--tb=short",
    "--timeout=120",
    "-W",
    "error::pytest.PytestUnraisableExceptionWarning",
    "--disable-warnings",
    "-n",
    "auto",
)

_UV_PREFIX: tuple[str, ...] = ("uv", "run", "--no-build", "--no-sync", "--frozen")


def _rerun_command(scope: str) -> str:
    """Return the exact command a developer can paste to reproduce a failure."""
    return "uv run --project backend --no-build --no-sync --frozen " + " ".join(part.format(scope=scope) for part in _PYTEST_ARGS)


def build_command(scope: str) -> list[str]:
    """Return the argv for the architecture-suite run."""
    return [*_UV_PREFIX, *(part.format(scope=scope) for part in _PYTEST_ARGS)]


def validate_scope(scope: str) -> str | None:
    """Return an error message when *scope* is outside the architecture suite.

    The scope is normalised to POSIX separators first so a Windows invocation
    cannot sidestep the check, then required to equal or sit under
    ``tests/architecture``.
    """
    normalised = scope.replace("\\", "/").strip()
    normalised = normalised.removeprefix("./")
    normalised = normalised.rstrip("/")
    if normalised == _PERMITTED_SCOPE_ROOT or normalised.startswith(f"{_PERMITTED_SCOPE_ROOT}/"):
        return None
    return f"refusing scope {scope!r}: this gate may only run {_PERMITTED_SCOPE_ROOT}/ (running the wider backend suite is never a pre-push gate — that is CI's job)"


def _child_env() -> dict[str, str]:
    """Return the environment for the pytest child process.

    Strips the git hook-context variables (they make git in a subprocess target
    the wrong repository) and gives pytest a per-run temp root.
    """
    # The shared hook-context helper lives in ``scripts/``; importing it via the
    # package keeps the env-var list in one place.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.git_hook_env import strip_git_hook_context_vars

    env = strip_git_hook_context_vars(os.environ)
    # Only fall back to USERPROFILE when HOME is genuinely absent (Windows);
    # an empty HOME breaks uv/git resolution in the subprocess.
    if "HOME" not in env and "USERPROFILE" in env:
        env["HOME"] = env["USERPROFILE"]
    temp_root = tempfile.mkdtemp(prefix="farna-arch-pytest-")
    env["PYTEST_DEBUG_TEMPROOT"] = temp_root
    return env


def _remove_temp_root(env: dict[str, str]) -> None:
    """Best-effort removal of this run's temp root (never the global one)."""
    temp_root = env.get("PYTEST_DEBUG_TEMPROOT")
    if temp_root:
        shutil.rmtree(temp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--scope",
        default=_PERMITTED_SCOPE_ROOT,
        help=f"path (relative to backend/) to run; must be inside {_PERMITTED_SCOPE_ROOT} (default: %(default)s)",
    )
    parser.add_argument("--timeout-seconds", type=int, default=_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--list", action="store_true", help="print the command and exit without running it")
    parsed = parser.parse_args(argv)

    error = validate_scope(parsed.scope)
    if error is not None:
        print(f"FAILED: {error}", file=sys.stderr)
        return 2

    command = build_command(parsed.scope)
    if parsed.list:
        print(" ".join(command))
        return 0

    print(f"running: {' '.join(command)}", file=sys.stderr)
    print(f"from:    {BACKEND_DIR}{os.sep}", file=sys.stderr)

    env = _child_env()
    try:
        result = subprocess.run(  # NOSONAR - fixed argv, no shell
            command,
            cwd=str(BACKEND_DIR),
            env=env,
            check=False,
            timeout=parsed.timeout_seconds,
        )
    except FileNotFoundError:
        print("FAILED: `uv` is not installed or not on PATH — cannot run the architecture suite.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(
            f"FAILED: architecture suite did not finish within {parsed.timeout_seconds}s (failing closed, not passing silently).",
            file=sys.stderr,
        )
        print(f"Rerun locally with:\n    {_rerun_command(parsed.scope)}", file=sys.stderr)
        return 1
    finally:
        _remove_temp_root(env)

    if result.returncode != 0:
        print(
            f"\nFAILED: architecture suite exited {result.returncode}. Rerun locally with:\n    {_rerun_command(parsed.scope)}",
            file=sys.stderr,
        )
    else:
        print("architecture suite passed", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
