"""Unit tests for scripts/run_affected_tests.py selection + suite grouping.

Guards two regressions in the pre-push affected-tests gate:

1. Over-selection by stem: ``backend/src/api/me.py`` must not glob-match
   ``test_metrics_ingest.py`` (bare ``test_{stem}*`` prefix matching used to).
2. Cross-suite process mixing: suites (unit / integration / bdd) must be run
   as SEPARATE pytest processes — ``tests/bdd/conftest.py`` sets
   ``DATABASE_URL``/``FARNA_CONTROL_DB`` at import time, and when a BDD file and an
   integration file ran in one process, the BDD env leaked into the
   integration tests (sqlite engine vs testcontainers Postgres). The runner's
   grouping loop maps each suite to one ``_run_pytest`` call, so the output
   of ``_group_by_suite`` directly determines the process split.
"""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_affected_tests.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_affected_tests.py)")

_loader = SourceFileLoader("run_affected_tests", str(script_path))
_spec = spec_from_loader("run_affected_tests", _loader)
assert _spec is not None
mod = module_from_spec(_spec)
_loader.exec_module(mod)


def _make_backend_tree(tmp_path: Path) -> None:
    (tmp_path / "backend" / "tests" / "unit" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "integration").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "bdd" / "steps").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True, exist_ok=True)


def test_me_stem_matches_genuinely_affected_tests(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    for name in (
        "test_me.py",
        "test_me_password.py",
        "test_me_hitl_email_preferences.py",
        "test_metrics_ingest.py",
    ):
        (tmp_path / "backend" / "tests" / "unit" / "api" / name).touch()
    (tmp_path / "backend" / "src" / "api" / "me.py").touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files(["backend/src/api/me.py"])

    assert sorted(Path(p).name for p in selected) == [
        "test_me.py",
        "test_me_hitl_email_preferences.py",
        "test_me_password.py",
    ]


def test_metrics_only_tree_selects_nothing_for_me_change(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    (tmp_path / "backend" / "tests" / "integration" / "test_metrics_ingest.py").touch()
    (tmp_path / "backend" / "src" / "api" / "me.py").touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files(["backend/src/api/me.py"])

    assert not selected


def test_changed_test_files_still_selected_directly(tmp_path, monkeypatch):
    _make_backend_tree(tmp_path)
    test_file = "backend/tests/unit/api/test_me_password.py"
    (tmp_path / test_file).touch()

    monkeypatch.chdir(tmp_path)
    selected = mod._select_test_files([test_file])

    assert test_file in selected


def test_suite_grouping_separates_bdd_from_integration():
    paths = [
        "backend/tests/bdd/steps/test_email_settings.py",
        "backend/tests/integration/test_metrics_ingest.py",
        "backend/tests/unit/api/test_me_password.py",
    ]

    grouped = mod._group_by_suite(paths)

    assert sorted(grouped) == ["bdd", "integration", "unit"]
    assert grouped["bdd"] == ["backend/tests/bdd/steps/test_email_settings.py"]
    assert grouped["integration"] == ["backend/tests/integration/test_metrics_ingest.py"]
    assert grouped["unit"] == ["backend/tests/unit/api/test_me_password.py"]


def test_suite_grouping_covers_integration_bdd_subdir_as_integration():
    grouped = mod._group_by_suite(["backend/tests/integration/bdd/test_x.py"])

    assert grouped == {"integration": ["backend/tests/integration/bdd/test_x.py"]}


def test_grouping_loop_runs_one_pytest_process_per_suite(monkeypatch):
    """Each suite group maps to exactly one _run_pytest call, mirroring CI timeouts."""
    calls: list[tuple[list[str], int]] = []

    def fake_run(paths, timeout=120):
        calls.append((paths, timeout))
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    test_paths = [
        "backend/tests/bdd/steps/test_email_settings.py",
        "backend/tests/integration/test_metrics_ingest.py",
        "backend/tests/unit/api/test_me_password.py",
    ]
    grouped = mod._group_by_suite(test_paths)
    worst = 0
    for suite, paths in grouped.items():
        code = mod._run_pytest(paths, timeout=mod.SUITE_TIMEOUTS.get(suite, 120))
        if code != 0:
            worst = code

    assert worst == 0
    assert len(calls) == 3
    for paths, _timeout in calls:
        assert len(paths) == 1
    integration_calls = [c for c in calls if c[0][0].startswith("backend/tests/integration")]
    assert integration_calls[0][1] == 300


def test_main_propagates_worst_group_exit_code(monkeypatch):
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/tests/unit/api/x.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: ["backend/tests/unit/api/x.py"])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: {"unit": paths})
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, timeout: 3)

    rc = mod.main(["--base", "origin/main"])

    assert rc == 3


def test_matches_stem_word_boundary():
    assert mod._matches_stem("test_me.py", "me")
    assert mod._matches_stem("test_me_password.py", "me")
    assert not mod._matches_stem("test_metrics_ingest.py", "me")
    assert mod._matches_stem("test_me_2023.py", "me")


# ---------------------------------------------------------------------------
# Pre-push profile (--prepush): bounds, deferrals, guards
# ---------------------------------------------------------------------------
def _make_prepush_tree(tmp_path: Path) -> None:
    """A fake repo tree with one runnable (unit) and one Docker (integration) test."""
    for rel in (
        "backend/tests/unit/api/test_me.py",
        "backend/tests/integration/test_me_db.py",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "backend" / "src" / "api").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "src" / "api" / "me.py").touch()


def test_collect_changed_files_returns_none_when_scope_is_unknown(monkeypatch):
    """Unknown scope is distinct from an empty diff (never conflated)."""
    monkeypatch.setattr(mod, "_resolve_merge_base", lambda _ref: None)
    assert mod._collect_changed_files("origin/main") is None


def test_collect_changed_files_returns_empty_list_for_an_empty_diff(monkeypatch):
    """An empty diff is "nothing to do", not "scope unknown"."""

    def fake_git(*args):
        if args[0] == "merge-base":
            return "abc123"
        return ""

    monkeypatch.setattr(mod, "_git", fake_git)
    assert not mod._collect_changed_files("origin/main")


def test_prepush_refuses_when_the_scope_cannot_be_resolved(monkeypatch, capsys):
    """A gate that cannot see the diff must not silently skip nor widen to the full suite."""
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: None)
    monkeypatch.setattr(mod, "_run_pytest", lambda *args, **kwargs: 0)

    assert mod.main(["--prepush"]) == 2

    err = capsys.readouterr().err
    assert "cannot resolve the changed-path scope" in err
    assert "never falls back to the full backend suite" in err


def test_non_prepush_still_skips_quietly_when_the_scope_is_unknown(monkeypatch, capsys):
    """The ad hoc (non pre-push) invocation keeps its historical exit 0."""
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: None)

    assert mod.main([]) == 0

    assert "skipping" in capsys.readouterr().err


def test_prepush_runs_the_unit_suite_and_defers_the_docker_suite(tmp_path, monkeypatch, capsys):
    _make_prepush_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/src/api/me.py"])
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: False)
    run_calls: list[dict] = []

    def fake_run(paths, **kwargs):
        run_calls.append({"paths": paths, **kwargs})
        return 0

    monkeypatch.setattr(mod, "_run_pytest", fake_run)

    assert mod.main(["--prepush"]) == 0

    suites_run = [call["paths"][0].split("/")[0] for call in run_calls]
    assert suites_run == ["tests"]
    integration_paths = [call["paths"] for call in run_calls if call["paths"][0].startswith("tests/integration")]
    assert not integration_paths
    err = capsys.readouterr().err
    assert "deferred to CI: suite [integration]" in err
    assert "needs Docker/testcontainers" in err


def test_prepush_defers_bdd_e2e_docker_and_load_suites_with_a_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    grouped = {
        "unit": ["backend/tests/unit/test_a.py"],
        "bdd": ["backend/tests/bdd/steps/test_b.py"],
        "e2e": ["backend/tests/e2e/test_c.py"],
        "docker": ["backend/tests/docker/test_d.py"],
        "load": ["backend/tests/load/test_e.py"],
    }
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/src/api/me.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: [p for paths in grouped.values() for p in paths])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: grouped)
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: False)
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, **kwargs: 0)

    assert mod.main(["--prepush"]) == 0

    err = capsys.readouterr().err
    for suite in ("bdd", "e2e", "docker", "load"):
        assert f"deferred to CI: suite [{suite}]" in err


def test_prepush_caps_the_selection_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    selected = [f"backend/tests/unit/test_{index}.py" for index in range(10)]
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/src/api/me.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: list(selected))
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: {"unit": list(paths)})
    monkeypatch.setattr(mod, "_is_whole_suite", lambda value, root=None: False)
    run_calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, **kwargs: run_calls.append(paths) or 0)

    assert mod.main(["--prepush", "--max-files", "3"]) == 0

    assert len(run_calls[0]) == 3
    assert "Notice: 10 affected test files" in capsys.readouterr().err


def test_prepush_defers_remaining_suites_when_the_time_budget_is_spent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    grouped = {
        "unit": ["backend/tests/unit/test_a.py"],
        "connectors": ["backend/tests/connectors/test_b.py"],
    }
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/src/api/me.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: [p for paths in grouped.values() for p in paths])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: grouped)
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: False)
    # A budget of 1s is already spent by the time the first suite starts.
    # Replace the module's clock reference (never the global ``time`` module, so
    # no other machinery in this process is affected).
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=iter([0.0, 5.0, 5.0]).__next__))
    run_calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, **kwargs: run_calls.append(paths) or 0)

    assert mod.main(["--prepush", "--time-budget-seconds", "1"]) == 0

    assert not run_calls
    err = capsys.readouterr().err
    assert "pre-push time budget was exhausted" in err


def test_prepush_refuses_to_run_the_entire_backend_suite(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/src/api/me.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: ["backend/tests/unit/test_a.py"] * 5)
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: True)
    monkeypatch.setattr(mod, "_run_pytest", lambda *args, **kwargs: 0)

    assert mod.main(["--prepush"]) == 2

    err = capsys.readouterr().err
    assert "entire backend test suite" in err


def test_prepush_prints_the_rerun_command_on_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/tests/unit/test_a.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: ["backend/tests/unit/test_a.py"])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: {"unit": list(paths)})
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: False)
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, **kwargs: 1)

    assert mod.main(["--prepush"]) == 1

    err = capsys.readouterr().err
    assert "Rerun locally with:" in err
    assert "run_affected_tests.py --base origin/main --prepush" in err


def test_prepush_forwards_the_time_budget_as_a_wall_clock_bound(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod, "_collect_changed_files", lambda base: ["backend/tests/unit/test_a.py"])
    monkeypatch.setattr(mod, "_select_test_files", lambda changed: ["backend/tests/unit/test_a.py"])
    monkeypatch.setattr(mod, "_group_by_suite", lambda paths: {"unit": list(paths)})
    monkeypatch.setattr(mod, "_is_whole_suite", lambda selected, root=None: False)
    captured: list[dict] = []
    monkeypatch.setattr(mod, "_run_pytest", lambda paths, **kwargs: captured.append(kwargs) or 0)

    assert mod.main(["--prepush", "--time-budget-seconds", "60"]) == 0

    assert captured[0]["wall_timeout"] is not None


def test_parse_args_rejects_a_non_positive_max_files(capsys):
    assert mod._parse_args(["--prepush", "--max-files", "0"]) is None
    assert "--max-files expects a positive integer" in capsys.readouterr().err


def test_parse_args_rejects_an_unknown_flag(capsys):
    assert mod._parse_args(["--nope"]) is None
    assert "unexpected argument" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Child environment: a stale shared pytest temp root must not fail the gate
# ---------------------------------------------------------------------------
def test_run_pytest_hands_pytest_an_isolated_temp_root_and_strips_git_context(monkeypatch):
    """A dangling ``pytest-current`` symlink in the shared temp root exits 1 with every test green."""
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")

    assert mod._run_pytest(["tests/unit/x.py"], timeout=60, wall_timeout=30) == 0

    env = captured["env"]
    assert isinstance(env, dict)
    assert "GIT_DIR" not in env
    assert "PYTEST_DEBUG_TEMPROOT" in env
    assert captured["timeout"] == 30
    assert captured["cwd"] == "backend"


def test_run_pytest_removes_the_isolated_temp_root_after_the_suite(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._run_pytest(["tests/unit/x.py"]) == 1

    env = captured["env"]
    assert isinstance(env, dict)
    assert not Path(env["PYTEST_DEBUG_TEMPROOT"]).exists()


def test_run_pytest_reports_a_wall_clock_kill_as_a_failure(monkeypatch, capsys):
    def fake_run(cmd, **kwargs):
        raise mod.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert mod._run_pytest(["tests/unit/x.py"], wall_timeout=30) == 1

    assert "wall-clock bound" in capsys.readouterr().err
