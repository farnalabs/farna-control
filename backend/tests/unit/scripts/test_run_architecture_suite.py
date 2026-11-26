"""Unit tests for scripts/run_architecture_suite.py.

The architecture suite is the single largest class of red-CI push (27 of the
30 ``Test (Backend) non-unit suites`` job failures in the last 80 failed
runs), so this gate has to be right in two directions:

1. It must run the EXACT CI invocation — same flags, same working directory,
   same warning gate — or it will pass locally and fail in CI.
2. Its scope guard must refuse anything wider than ``tests/architecture``: a
   gate that can be widened to ``tests/`` is a 40-minute suite behind every
   push, and it stops being a pre-push gate at all.
"""

from __future__ import annotations

import subprocess
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_architecture_suite.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_architecture_suite.py)")

_loader = SourceFileLoader("run_architecture_suite", str(script_path))
_spec = spec_from_loader("run_architecture_suite", _loader)
assert _spec is not None
mod = module_from_spec(_spec)
_loader.exec_module(mod)


# ---------------------------------------------------------------------------
# Scope guard
# ---------------------------------------------------------------------------
def test_scope_accepts_the_architecture_root():
    assert mod.validate_scope("tests/architecture") is None


def test_scope_accepts_a_path_inside_the_architecture_root():
    assert mod.validate_scope("tests/architecture/test_workflow_structure.py") is None


def test_scope_rejects_the_whole_test_tree():
    assert mod.validate_scope("tests/") is not None


def test_scope_rejects_the_backend_root():
    assert mod.validate_scope(".") is not None


def test_scope_rejects_a_sibling_suite():
    assert mod.validate_scope("tests/integration") is not None


def test_scope_rejects_a_prefixed_impostor_directory():
    """``tests/architecture_extra`` must not be admitted by a prefix check."""
    assert mod.validate_scope("tests/architecture_extra") is not None


def test_scope_normalises_windows_separators_before_checking():
    assert mod.validate_scope("tests\\architecture") is None


def test_scope_rejects_a_windows_sibling_suite():
    assert mod.validate_scope("tests\\integration") is not None


# ---------------------------------------------------------------------------
# Command construction — must be the exact CI invocation
# ---------------------------------------------------------------------------
def test_command_starts_with_the_lock_safe_uv_prefix():
    command = mod.build_command("tests/architecture")
    assert command[:5] == ["uv", "run", "--no-build", "--no-sync", "--frozen"]


def test_command_runs_pytest_on_the_architecture_package():
    command = mod.build_command("tests/architecture")
    assert "pytest" in command
    assert "tests/architecture" in command


def test_command_carries_the_unraisable_warning_gate():
    """CI escalates the warning and disables only the summary; both flags.

    Pinned by tests/architecture/test_ci_pytest_commands_carry_the_warning_gate.py
    for the same reason: without the escalation the run is structurally blind.
    """
    command = mod.build_command("tests/architecture")
    escalation = command.index("error::pytest.PytestUnraisableExceptionWarning")
    assert command[escalation - 1] == "-W"
    assert "--disable-warnings" in command


def test_command_uses_the_ci_parallel_runner():
    command = mod.build_command("tests/architecture")
    runner = command.index("-n")
    assert command[runner + 1] == "auto"


def test_rerun_command_reproduces_the_same_pytest_invocation():
    rerun = mod._rerun_command("tests/architecture")
    assert rerun.startswith("uv run --project backend")
    assert "pytest tests/architecture" in rerun
    assert "error::pytest.PytestUnraisableExceptionWarning" in rerun


# ---------------------------------------------------------------------------
# main(): scope refusal, propagation, rerun hint
# ---------------------------------------------------------------------------
def test_main_refuses_a_wide_scope_without_running_anything(capsys):
    with patch.object(mod.subprocess, "run") as run:
        exit_code = mod.main(["--scope", "tests/"])
    run.assert_not_called()
    assert exit_code == 2
    assert "refusing scope" in capsys.readouterr().err


def test_main_list_prints_the_command_and_runs_nothing(capsys):
    with patch.object(mod.subprocess, "run") as run:
        exit_code = mod.main(["--list"])
    run.assert_not_called()
    assert exit_code == 0
    assert "pytest tests/architecture" in capsys.readouterr().out


def test_main_propagates_a_suite_failure_and_prints_the_rerun_hint(capsys):
    with patch.object(mod.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=1)):
        exit_code = mod.main([])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Rerun locally with:" in err
    assert "pytest tests/architecture" in err


def test_main_reports_success_without_a_rerun_hint(capsys):
    with patch.object(mod.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0)):
        exit_code = mod.main([])
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "architecture suite passed" in err
    assert "Rerun locally with:" not in err


def test_main_fails_closed_when_uv_is_missing(capsys):
    with patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("uv")):
        exit_code = mod.main([])
    assert exit_code == 1
    assert "`uv` is not installed" in capsys.readouterr().err


def test_main_fails_closed_on_a_timeout_and_prints_the_rerun_hint(capsys):
    timeout = subprocess.TimeoutExpired(cmd="pytest", timeout=1)
    with patch.object(mod.subprocess, "run", side_effect=timeout):
        exit_code = mod.main(["--timeout-seconds", "1"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "did not finish within 1s" in err
    assert "Rerun locally with:" in err


def test_main_runs_with_the_backend_working_directory():
    with patch.object(mod.subprocess, "run", return_value=subprocess.CompletedProcess(args=[], returncode=0)) as run:
        mod.main([])
    assert run.call_args.kwargs["cwd"] == str(mod.BACKEND_DIR)


# ---------------------------------------------------------------------------
# Child environment: isolated pytest temp root, no git hook context
# ---------------------------------------------------------------------------
def test_child_env_points_pytest_at_an_isolated_existing_temp_root(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/some/other/repo/.git")
    env = mod._child_env()
    try:
        temp_root = env["PYTEST_DEBUG_TEMPROOT"]
        assert Path(temp_root).is_dir()
        assert "GIT_DIR" not in env
    finally:
        mod._remove_temp_root(env)


def test_child_env_temp_root_is_removed_after_use():
    env = mod._child_env()
    temp_root = Path(env["PYTEST_DEBUG_TEMPROOT"])
    mod._remove_temp_root(env)
    assert not temp_root.exists()
