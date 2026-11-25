"""Contract tests for the Docker Compose upgrade/failure/recovery harness.

The rollback leg no longer puts the previous image back on a forward-migrated
database (an invalid state the FAR-2659 startup guard refuses). It asserts the
documented recovery instead: the new release's verified pre-upgrade snapshot,
the guard's distinct refusal (exit 78 and the one-line message), the shipped
``compose-restore.sh`` restore, and the previous release serving the data
written before the upgrade.

Each contract case below breaks exactly one link of that chain and requires the
leg to fail; the happy-path case pins the exact evidence sequence a reader sees.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from farna.db import alignment

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "verify_compose_upgrade_rollback.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/verify_compose_upgrade_rollback.py)")

loader = SourceFileLoader("verify_compose_upgrade_rollback", str(script_path))
spec = spec_from_file_location("verify_compose_upgrade_rollback", script_path, loader=loader)
if spec is None:
    raise RuntimeError("Could not load verify_compose_upgrade_rollback.py")
module = module_from_spec(spec)
sys.modules["verify_compose_upgrade_rollback"] = module
loader.exec_module(module)

Evidence = module.Evidence
Matrix = module.Matrix
ValidationError = module.ValidationError
build_compose_command = module.build_compose_command
validate_tag = module.validate_tag
safe_project_name = module._safe_project_name

#: The real startup failure the published image emits for a 28-byte SECRET_KEY
#: (backend/src/farna/settings.py::_secret_key_is_strong, surfaced by pydantic).
SECRET_KEY_FAILURE = "pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings\nsecret_key\n  Value error, SECRET_KEY must be at least 32 bytes; got 28\n"

#: What the container's own stdout/stderr actually carries: the entrypoint's
#: echoes and supervisord's own INFO lines. The backend child's output is
#: redirected to its log files, so this never holds the startup failure.
CONTAINER_LOG_BODY = "Running database migrations...\nStarting supervisord...\n"

_RESTORE_STDOUT = (
    "Verifying snapshot /backups/pre-upgrade-dump-v1.1.0-20260101-101500 before touching anything...\n"
    'Stopping the writers (compose service "farna-control")...\n'
    "Restoring /backups/pre-upgrade-dump-v1.1.0-20260101-101500 ...\n"
    "\n"
    "Restore complete. Writers stay stopped.\n"
    "\n"
    "Start the previous release with:\n"
    "  ./compose-rollback.sh <previous-version>   (e.g. ./compose-rollback.sh v0.3.0)\n"
    "No automatic restart was performed.\n"
)


class Flow:
    """A fake docker/compose world that models the whole matrix.

    One attribute per contract link, so a test breaks exactly one thing. The
    fake keeps DATABASE STATE (``db_state``) and APPLICATION STATE (``mode``)
    separately, because the leg's whole point is that they diverge: the database
    can be migrated forward while the application is stopped.
    """

    def __init__(self) -> None:
        self.before: tuple[str, ...] = ("0355_old",)
        self.after: tuple[str, ...] = ("0359_new",)
        self.restored: tuple[str, ...] = self.before
        self.snapshot_path = "/backups/pre-upgrade-dump-v1.1.0-20260101-101500"
        self.snapshot_lines: list[str] = [
            "Running database migrations...",
            f"{module._SNAPSHOT_VERIFIED_PREFIX}{self.snapshot_path}",
            f"  {module._RESTORE_COMMAND_PREFIX}./{module._RESTORE_HELPER} --confirm {self.snapshot_path}",
            "",
        ]
        self.no_snapshot_lines: list[str] = [
            "Running database migrations...",
            f"{module._NO_SNAPSHOT_MARKER} (fresh database or no migration pending) — see the line above.",
        ]
        self.upgrade_log_lines: list[str] = self.snapshot_lines
        self.guard_rc: int = module._EXIT_DATABASE_AHEAD
        self.guard_message: str = ""
        self.guard_module_present = True
        self.guard_probe_stderr = "ModuleNotFoundError: No module named 'farna.db.alignment'\n"
        self.predecessor_version = "1.0.0"
        self.row_in_db = False
        self.lose_row_after_restore = False
        self.snapshot_dir_present = True
        self.running_services = ""
        self.restore_rc = 0
        self.restore_stdout = _RESTORE_STDOUT
        self.rollback_rc = 0
        self.failed_upgrade_healthy = False
        self.db_state = "none"
        self.mode = "none"
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []
        self.restore_calls: list[tuple[str, ...]] = []

    def refusal_text(self) -> str:
        revisions = ", ".join(self.after)
        return (
            f"{module._REFUSAL_PREFIX}{revisions} is not known to this release (farna-control {self.predecessor_version}); "
            "migrations are forward-only, so this release will not run against a database from a newer release. "
            f"Recover with: {module._DOCUMENTED_RESTORE_COMMAND} and then start the release that owns the database, "
            "or deploy that release."
        )

    def _revisions(self) -> tuple[str, ...]:
        if self.db_state == "restored":
            return self.restored
        if self.db_state == "after":
            return self.after
        return self.before

    def _curl_status(self) -> str:
        if self.mode == "broken" and not self.failed_upgrade_healthy:
            return "502"
        return "200"

    def _logs(self) -> str:
        if self.mode == "upgraded":
            return "\n".join(self.upgrade_log_lines) + "\n"
        if self.mode == "broken":
            return CONTAINER_LOG_BODY
        if self.mode == "rolled-back":
            return "Starting supervisord...\n"
        return ""

    def _psql(self, statement: str) -> SimpleNamespace:
        if "INSERT INTO rc_matrix_upgrade_probe" in statement:
            self.row_in_db = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "SELECT note FROM rc_matrix_upgrade_probe" in statement:
            present = self.row_in_db and not (self.db_state == "restored" and self.lose_row_after_restore)
            return SimpleNamespace(returncode=0, stdout=(module._PROBE_VALUE + "\n") if present else "", stderr="")
        if "SELECT version_num" in statement:
            return SimpleNamespace(returncode=0, stdout="\n".join(self._revisions()) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def _guard(self) -> SimpleNamespace:
        message = self.guard_message or self.refusal_text()
        return SimpleNamespace(returncode=self.guard_rc, stdout=message + "\n", stderr="")

    def _helper(self, argv: tuple[str, ...]) -> SimpleNamespace:
        helper = Path(argv[1]).name
        if helper == module._RESTORE_HELPER:
            self.restore_calls.append(argv)
            if self.restore_rc == 0:
                self.db_state = "restored"
            return SimpleNamespace(returncode=self.restore_rc, stdout=self.restore_stdout, stderr="")
        if helper == module._ROLLBACK_HELPER:
            if self.rollback_rc == 0:
                self.mode = "rolled-back"
            return SimpleNamespace(returncode=self.rollback_rc, stdout="Starting the previous release\n", stderr="")
        return SimpleNamespace(returncode=127, stdout="", stderr=f"unexpected helper: {argv!r}")

    def runner(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        call = tuple(argv)
        self.calls.append(call)
        self.envs.append(kwargs.get("env") or {})
        env = kwargs.get("env") or {}
        if argv[0] == "curl":
            return SimpleNamespace(returncode=0, stdout=self._curl_status(), stderr="")
        if Path(argv[0]).stem in {"bash", "sh"}:
            return self._helper(call)
        if argv[0] != "docker":
            return SimpleNamespace(returncode=127, stdout="", stderr=f"unexpected command: {argv!r}")
        if "config" in argv:
            tag = env.get("TAG", "")
            return SimpleNamespace(returncode=0, stdout=f"image: ghcr.io/farnalabs/farna-control:{tag}\nSECRET_KEY: intentionally-invalid-secret", stderr="")
        if "run" in argv:
            if "--entrypoint" in argv and "python" in argv:
                if self.guard_module_present:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=1, stdout="", stderr=self.guard_probe_stderr)
            if "--entrypoint" in argv and "farna-control" in argv:
                return SimpleNamespace(returncode=0, stdout=f"farna-control {self.predecessor_version}\n", stderr="")
            return self._guard()
        if "exec" in argv and "postgres" in argv:
            return self._psql(argv[-1])
        if "exec" in argv and any("test -d" in arg for arg in argv):
            return SimpleNamespace(returncode=0 if self.snapshot_dir_present else 1, stdout="", stderr="")
        if "exec" in argv:
            body = SECRET_KEY_FAILURE if argv[-1].endswith("backend.err.log") else ""
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        if "logs" in argv:
            return SimpleNamespace(returncode=0, stdout=self._logs(), stderr="")
        if "ps" in argv:
            return SimpleNamespace(returncode=0, stdout=self.running_services, stderr="")
        if "stop" in argv:
            self.mode = "stopped"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "up" in argv:
            if "--force-recreate" in argv:
                self.mode = "broken"
            elif env.get("TAG") == "v1.0.0":
                self.mode = "healthy"
                self.db_state = "before"
            else:
                self.mode = "upgraded"
                self.db_state = "after"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _flow(tmp_path: Path) -> Flow:
    """A happy-path fake with the shipped helpers materialised beside the compose file."""
    flow = Flow()
    (tmp_path / module._RESTORE_HELPER).write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / module._ROLLBACK_HELPER).write_text("#!/bin/sh\n", encoding="utf-8")
    return flow


@pytest.fixture(autouse=True)
def _posix_shells(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve bash/sh deterministically so the suite is host-independent.

    The matrix resolves a real host shell only to launch the shipped helpers;
    the fake runner never executes one. Windows has no POSIX sh on PATH, so the
    verdict must not depend on the machine running the tests.
    """
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/usr/bin/{name}")


def _matrix(tmp_path: Path, runner: Any, *, timeout: int = 5) -> Any:
    return Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=Evidence(tmp_path / "evidence"),
        port="18080",
        timeout=timeout,
        runner=runner,
    )


def _rows(matrix: Any) -> dict[str, dict[str, Any]]:
    transcript = matrix.evidence.transcript.read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["name"]: json.loads(line) for line in transcript}


def _row_text(matrix: Any, rows: dict[str, dict[str, Any]], name: str) -> str:
    return (matrix.evidence.directory / rows[name]["stdout"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Contract pins: the harness mirrors the shipped guard and recovery contract
# ---------------------------------------------------------------------------


def test_documented_restore_command_matches_the_guard_contract() -> None:
    """The refusal check must pin the string the shipped guard actually emits."""
    assert module._DOCUMENTED_RESTORE_COMMAND == alignment.RECOVERY_COMMAND


def test_exit_status_matches_the_guard_contract() -> None:
    assert module._EXIT_DATABASE_AHEAD == alignment.EXIT_DATABASE_AHEAD


def test_refusal_prefix_matches_the_guard_message() -> None:
    result = alignment.check_alignment({"9999_future"}, {"0001_baseline"})
    assert alignment.format_refusal(result).startswith(module._REFUSAL_PREFIX)


def test_a_missing_helper_shell_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unresolvable shell must fail loudly, never fall through to a bare name."""
    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    with pytest.raises(ValidationError, match="is not on PATH"):
        module._helper_shell("bash")


# ---------------------------------------------------------------------------
# The happy path: the exact sequence and the evidence a reader sees
# ---------------------------------------------------------------------------


def test_matrix_records_the_documented_recovery_transitions(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    matrix.run()

    rows = _rows(matrix)
    assert list(rows) == [
        "docker-version",
        "compose-config-old",
        "compose-config-invalid-secret",
        "clean-install-old",
        "clean-install-ready-attempt-1",
        "clean-install-ready-assertion",
        "database-revisions-before-upgrade",
        "write-pre-upgrade-row",
        "pre-upgrade-row-before-upgrade",
        "upgrade-new",
        "upgrade-ready-attempt-1",
        "upgrade-ready-assertion",
        "upgrade-logs",
        "upgrade-snapshot-presence",
        "upgrade-snapshot-assertion",
        "database-revisions-after-upgrade",
        "failed-upgrade",
        "failed-upgrade-not-ready-attempt-1",
        "failed-upgrade-not-ready-assertion",
        "failed-upgrade-backend-log-stdout-attempt-1",
        "failed-upgrade-backend-log-stderr-attempt-1",
        "failed-upgrade-logs",
        "bad-release-stop",
        "app-running-check",
        "predecessor-alignment-guard-probe",
        "predecessor-version",
        "previous-image-on-newer-database",
        "guard-refusal-assertion",
        "database-revisions-after-guard-refusal",
        "restore-pre-upgrade-snapshot",
        "restore-outcome",
        "database-revisions-after-restore",
        "start-previous-release",
        "rollback-ready-attempt-1",
        "rollback-ready-assertion",
        "pre-upgrade-row-after-recovery",
        "rollback-logs",
    ]

    # The artefact a reader opens must show the snapshot, the refusal, the
    # restore outcome and the data assertion - not just a green summary.
    assert flow.snapshot_path in _row_text(matrix, rows, "upgrade-snapshot-assertion")
    assert f"./{module._RESTORE_HELPER} --confirm {flow.snapshot_path}" in _row_text(matrix, rows, "upgrade-snapshot-assertion")
    assert flow.refusal_text() in _row_text(matrix, rows, "guard-refusal-assertion")
    assert "Restore complete. Writers stay stopped." in _row_text(matrix, rows, "restore-outcome")
    assert module._ROLLBACK_HELPER in _row_text(matrix, rows, "restore-outcome")
    assert rows["pre-upgrade-row-after-recovery"]["returncode"] == 0

    assert matrix.result_line.startswith("PASS ")
    assert "guard refusal (exit 78)" in matrix.result_line
    assert "restore pre-upgrade snapshot" in matrix.result_line


def test_the_helpers_run_inside_the_matrix_project(tmp_path: Path) -> None:
    """A helper on a different compose project would restore an EMPTY database."""
    flow = _flow(tmp_path)
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    matrix.run()

    assert len(flow.restore_calls) == 1
    restore_index = flow.calls.index(flow.restore_calls[0])
    restore_env = flow.envs[restore_index]
    assert restore_env["COMPOSE_PROJECT_NAME"] == "proof-123"
    assert restore_env["COMPOSE_FILE"] == str(tmp_path / "docker-compose.prod.yml")
    assert restore_env["TAG"] == "v1.1.0"

    rollback_call = next(call for call in flow.calls if Path(call[0]).stem == "sh" and call[2] == "v1.0.0")
    rollback_env = flow.envs[flow.calls.index(rollback_call)]
    assert rollback_env["COMPOSE_PROJECT_NAME"] == "proof-123"


def test_the_documented_restore_is_given_the_snapshot_the_release_printed(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    matrix.run()

    assert flow.restore_calls[0][-1] == flow.snapshot_path


# ---------------------------------------------------------------------------
# Failure condition 1: the verified snapshot is missing
# ---------------------------------------------------------------------------


def test_the_leg_fails_when_the_upgrade_never_reports_a_snapshot(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.upgrade_log_lines = ["Running database migrations...", "Starting supervisord..."]
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="did not report a verified pre-upgrade snapshot"):
        matrix.run()


def test_the_leg_fails_when_a_migrated_database_has_no_snapshot(tmp_path: Path) -> None:
    """The release migrated the database but documented "no snapshot needed"."""
    flow = _flow(tmp_path)
    flow.upgrade_log_lines = flow.no_snapshot_lines
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="took no verified pre-upgrade snapshot"):
        matrix.run()

    assert not flow.restore_calls


def test_the_leg_fails_when_the_printed_snapshot_is_absent(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.snapshot_dir_present = False
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="directory is not present"):
        matrix.run()


# ---------------------------------------------------------------------------
# Failure condition 2: the restore command is not the documented one
# ---------------------------------------------------------------------------


def test_the_leg_fails_when_the_printed_restore_command_is_not_documented(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.upgrade_log_lines = [
        "Running database migrations...",
        f"{module._SNAPSHOT_VERIFIED_PREFIX}{flow.snapshot_path}",
        f"  {module._RESTORE_COMMAND_PREFIX}docker compose run --rm farna-control restore {flow.snapshot_path}",
        "",
    ]
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="documented recovery command"):
        matrix.run()


def test_the_leg_fails_when_the_restore_helper_is_not_shipped(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    (tmp_path / module._RESTORE_HELPER).unlink()
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="documented recovery helper is missing"):
        matrix.run()


def test_the_leg_fails_when_the_restore_did_not_report_completion(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.restore_stdout = "Restoring ...\n"
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="did not report completion"):
        matrix.run()


def test_the_leg_fails_when_the_restore_does_not_print_the_rollback_helper(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.restore_stdout = "Restore complete. Writers stay stopped.\n"
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="did not print the shipped start command"):
        matrix.run()


# ---------------------------------------------------------------------------
# Failure condition 3: the guard does not refuse
# ---------------------------------------------------------------------------


def test_the_leg_fails_when_the_guard_does_not_refuse(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.guard_rc = 1
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="must refuse a database migrated by a newer release with exit 78"):
        matrix.run()


def test_the_leg_fails_when_the_refusal_exits_elsewhere(tmp_path: Path) -> None:
    """An alembic 'Can't locate revision' crash is not the documented refusal."""
    flow = _flow(tmp_path)
    flow.guard_rc = 0
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="it exited 0"):
        matrix.run()


def test_the_leg_fails_when_the_refusal_names_the_wrong_revision(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.guard_message = f"{module._REFUSAL_PREFIX}0000_stale is not known to this release (farna-control {flow.predecessor_version}); Recover with: {module._DOCUMENTED_RESTORE_COMMAND}"
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="must name the database revision and the running release version"):
        matrix.run()


def test_the_leg_fails_when_the_refusal_omits_the_documented_restore(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.guard_message = f"{module._REFUSAL_PREFIX}{', '.join(flow.after)} is not known to this release (farna-control {flow.predecessor_version}); migrations are forward-only."
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="must name the documented recovery command"):
        matrix.run()


def test_the_leg_fails_when_the_stack_is_still_running(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.running_services = module._APP_SERVICE
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="bad release is still running"):
        matrix.run()


def test_an_undecidable_guard_probe_fails_closed(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.guard_module_present = False
    flow.guard_probe_stderr = "PermissionError: cannot read site-packages\n"
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="could not determine whether the predecessor image ships"):
        matrix.run()


# ---------------------------------------------------------------------------
# Failure condition 4: the pre-upgrade row is gone after the recovery
# ---------------------------------------------------------------------------


def test_the_leg_fails_when_the_pre_upgrade_row_is_gone(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.lose_row_after_restore = True
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="missing or changed after the recovery"):
        matrix.run()


def test_the_leg_fails_when_the_restore_did_not_revert_the_database(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.restored = flow.after
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    with pytest.raises(ValidationError, match="database-revisions-after-restore"):
        matrix.run()


# ---------------------------------------------------------------------------
# Applicability: a predecessor that predates the guard, and a no-migration pair
# ---------------------------------------------------------------------------


def test_a_predecessor_without_the_guard_is_recorded_as_not_applicable(tmp_path: Path) -> None:
    """bundle-v0.3.330 is the first image that ships FAR-2659.

    The refusal cannot be asserted against an image that predates the guard; the
    recovery is still proven end to end, and the evidence says exactly why the
    refusal row is absent.
    """
    flow = _flow(tmp_path)
    flow.guard_module_present = False
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    matrix.run()

    rows = _rows(matrix)
    assert rows["guard-refusal-assertion"]["outcome"] == "guard-refusal-not-applicable"
    assert "previous-image-on-newer-database" not in rows
    assert "restore-pre-upgrade-snapshot" in rows
    assert matrix.result_line.startswith("PASS ")
    assert "guard refusal" not in matrix.result_line


def test_a_pair_without_a_migration_records_recovery_not_required(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.after = flow.before
    flow.upgrade_log_lines = flow.no_snapshot_lines
    matrix = _matrix(tmp_path, flow.runner, timeout=10)

    matrix.run()

    rows = _rows(matrix)
    assert rows["recovery-not-required"]["outcome"] == "recovery-not-required"
    assert "restore-pre-upgrade-snapshot" not in rows
    assert "start-previous-release" in rows
    assert matrix.result_line.startswith("PASS ")


# ---------------------------------------------------------------------------
# Existing legs keep their coverage
# ---------------------------------------------------------------------------


def test_validate_tag_rejects_moving_tags() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        validate_tag("latest", name="tag")
    with pytest.raises(ValidationError, match="empty"):
        validate_tag("  ", name="tag")


@pytest.mark.parametrize("value", ["proof-123", "farna-control-upgrade-4321", "A1_b2"])
def test_safe_project_name_accepts_bounded_values(value: str) -> None:
    assert safe_project_name(value) == value


@pytest.mark.parametrize("value", ["", "-oops", "has space", "semi;colon", "--privileged"])
def test_safe_project_name_rejects_argv_injection(value: str) -> None:
    with pytest.raises(ValidationError, match="letters, numbers, and hyphens"):
        safe_project_name(value)


def test_build_compose_command_is_project_isolated(tmp_path: Path) -> None:
    command = build_compose_command(tmp_path / "docker-compose.prod.yml", "proof-123", "up", "-d")
    assert command[:6] == [
        "docker",
        "compose",
        "-p",
        "proof-123",
        "-f",
        str(tmp_path / "docker-compose.prod.yml"),
    ]
    assert command[-2:] == ["up", "-d"]


def _child_log_read(stream: str, path: str, *, body: str, returncode: int = 0, stderr: str = "") -> Any:
    return module.ChildLogRead(
        stream=stream,
        path=path,
        result=module.CommandResult(
            f"failed-upgrade-backend-log-{stream}-attempt-1",
            ("docker", "compose", "-p", "proof-123", "exec", "-T", "farna-control", "cat", path),
            returncode,
            body,
            stderr,
        ),
    )


def _child_log_evidence(*, stdout_body: str = "", stderr_body: str = "", stderr_returncode: int = 0) -> Any:
    stdout_path = module._BACKEND_CHILD_LOGS[0][1]
    stderr_path = module._BACKEND_CHILD_LOGS[1][1]
    failing_stderr = "" if stderr_returncode == 0 else 'service "farna-control" is not running'
    return module.ChildLogEvidence(
        reads=(
            _child_log_read("stdout", stdout_path, body=stdout_body),
            _child_log_read(
                "stderr",
                stderr_path,
                body=stderr_body,
                returncode=stderr_returncode,
                stderr=failing_stderr,
            ),
        ),
        attempts=1,
    )


def _container_log(body: str = CONTAINER_LOG_BODY) -> Any:
    return module.CommandResult(
        "failed-upgrade-logs",
        (
            "docker",
            "compose",
            "-p",
            "proof-123",
            "-f",
            "docker-compose.prod.yml",
            "logs",
            "--no-color",
            "--tail=200",
            "farna-control",
        ),
        0,
        body,
        "",
    )


def _unused_runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
    raise AssertionError(f"log-source assertion must not shell out: {argv}")


def test_failed_upgrade_reads_the_backend_child_log_not_the_container_log(tmp_path: Path) -> None:
    """The container log can never satisfy the failure assertion."""
    matrix = _matrix(tmp_path, _unused_runner)
    container_log = _container_log()

    assert module._INVALID_SECRET_KEY_FAILURE not in container_log.stdout
    matrix._assert_failed_upgrade_startup_failure(_child_log_evidence(stderr_body=SECRET_KEY_FAILURE), container_log)


def test_failed_upgrade_rejects_a_child_log_without_the_secret_key_failure(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path, _unused_runner)
    child_logs = _child_log_evidence(stderr_body="ModuleNotFoundError: No module named 'farna'\n")

    with pytest.raises(ValidationError, match="readable but it did not contain the startup failure"):
        matrix._assert_failed_upgrade_startup_failure(child_logs, _container_log())


def test_failed_upgrade_rejects_an_unreadable_child_log(tmp_path: Path) -> None:
    """A stopped container must fail loudly, never pass on the container log."""
    matrix = _matrix(tmp_path, _unused_runner)
    child_logs = _child_log_evidence(stderr_returncode=1)

    with pytest.raises(ValidationError, match="did not stay up") as excinfo:
        matrix._assert_failed_upgrade_startup_failure(child_logs, _container_log())

    message = str(excinfo.value)
    assert "/var/log/supervisor/backend.out.log" in message
    assert "/var/log/supervisor/backend.err.log" in message


def test_failed_upgrade_failure_names_both_log_sources(tmp_path: Path) -> None:
    """A running container with a silent child log is distinguishable from an exited one."""
    matrix = _matrix(tmp_path, _unused_runner)

    with pytest.raises(ValidationError, match="did not contain the expected invalid SECRET_KEY startup failure") as excinfo:
        matrix._assert_failed_upgrade_startup_failure(_child_log_evidence(), _container_log())

    message = str(excinfo.value)
    assert "did not stay up" not in message
    assert "readable but it did not contain the startup failure" in message
    assert "backend.out.log" in message
    assert "backend.err.log" in message
    assert "logs --no-color --tail=200 farna-control" in message
    assert "Starting supervisord..." in message


def test_backend_child_log_poll_waits_for_the_crash_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "_CHILD_LOG_POLL_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(module, "_CHILD_LOG_POLL_MAX_SECONDS", 0.01)
    polls = 0

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal polls
        if argv[0] == "docker" and "exec" in argv:
            if argv[-1].endswith("backend.out.log"):
                polls += 1
            # The backend has not crashed yet on the first two reads.
            body = SECRET_KEY_FAILURE if polls >= 3 and argv[-1].endswith("backend.err.log") else ""
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = _matrix(tmp_path, runner)._capture_backend_child_logs()

    assert module._INVALID_SECRET_KEY_FAILURE in evidence.text
    assert evidence.attempts == 3


def test_backend_child_log_poll_stops_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A silent crash-loop is not polled forever: the deadline ends the read."""
    monkeypatch.setattr(module, "_CHILD_LOG_POLL_INITIAL_SECONDS", 0.01)
    monkeypatch.setattr(module, "_CHILD_LOG_POLL_MAX_SECONDS", 0.01)
    monkeypatch.setattr(module, "_CHILD_LOG_MINIMUM_WAIT_SECONDS", 0.0)

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = _matrix(tmp_path, runner, timeout=0)._capture_backend_child_logs()

    assert module._INVALID_SECRET_KEY_FAILURE not in evidence.text
    assert evidence.attempts >= 1


def test_evidence_redacts_secret_literals(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FARNA_CONTROL_USERS=admin@example.com:admin-password\nODD_CREDENTIAL=not-obvious-secret\n",
        encoding="utf-8",
    )
    secrets = module._secret_values_from_file(env_file)
    assert {"admin@example.com:admin-password", "not-obvious-secret"} <= secrets
    evidence = Evidence(tmp_path / "evidence", secrets=secrets)
    evidence.record(
        module.CommandResult(
            "compose-config",
            ("docker", "compose", "config", "admin@example.com:admin-password"),
            0,
            "FARNA_CONTROL_USERS=admin@example.com:admin-password\n",
            "ODD_CREDENTIAL=not-obvious-secret\n",
        ),
        outcome="success",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in evidence.directory.iterdir())
    assert "admin@example.com:admin-password" not in rendered
    assert "not-obvious-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_credential_redaction_keeps_operator_tags_and_port(tmp_path: Path) -> None:
    env = {
        "TAG": "v1.1.0",
        "PORT": "18080",
        "COMPOSE_FILE": "/bundle/docker-compose.prod.yml",
        "SECRET_KEY": "super-secret-value",
        "POSTGRES_PASSWORD": "db-password",
        "FARNA_CONTROL_USERS": "admin@example.com:admin-password",
    }
    secrets = module._secret_values(env)
    assert secrets == {"super-secret-value", "db-password"}
    evidence = Evidence(tmp_path / "evidence")
    evidence.add_secrets(secrets)
    evidence.record(
        module.CommandResult(
            "compose-config",
            ("docker", "compose", "config", "http://127.0.0.1:18080/healthz/ready"),
            0,
            ("image: ghcr.io/farnalabs/farna-control:v1.1.0\nSECRET_KEY: super-secret-value\nPOSTGRES_PASSWORD: db-password\n"),
            "",
        ),
        outcome="success",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in evidence.directory.iterdir())
    assert "ghcr.io/farnalabs/farna-control:v1.1.0" in rendered
    assert "127.0.0.1:18080" in rendered
    assert "super-secret-value" not in rendered
    assert "db-password" not in rendered
    assert "[REDACTED]" in rendered


def test_ready_accepts_proxy_502_for_failed_upgrade(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="502", stderr="")

    matrix = _matrix(tmp_path, runner, timeout=1)
    assert matrix.ready("failed-upgrade-not-ready", expected=False) is False


def test_evidence_rejects_stale_directory(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    (directory / "RESULT").write_text("old run\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="not empty"):
        Evidence(directory)


def test_ready_polls_transient_connection_refusal(tmp_path: Path) -> None:
    attempts = 0

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return SimpleNamespace(returncode=7, stdout="", stderr="connection refused")
        return SimpleNamespace(returncode=0, stdout="200", stderr="")

    matrix = _matrix(tmp_path, runner, timeout=1)
    assert matrix.ready("startup-ready", expected=True) is True
    assert attempts == 3


def test_ready_times_out_after_transient_connection_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The healthy-start floor is disabled here so the case stays fast."""
    monkeypatch.setattr(module, "_READY_MINIMUM_WAIT_SECONDS", 0)

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=7, stdout="", stderr="connection refused")

    matrix = _matrix(tmp_path, runner, timeout=1)
    with pytest.raises(ValidationError, match="before deadline"):
        matrix.ready("startup-ready", expected=True)


def test_matrix_rejects_a_healthy_failed_upgrade(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    flow.failed_upgrade_healthy = True
    matrix = _matrix(tmp_path, flow.runner, timeout=1)

    with pytest.raises(ValidationError, match="not ready"):
        matrix.run()


def test_matrix_records_failed_compose_commands_as_failures(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "docker" and "version" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    matrix = _matrix(tmp_path, runner, timeout=10)
    with pytest.raises(ValidationError, match="docker-version failed"):
        matrix.run()
    rows = [json.loads(line) for line in matrix.evidence.transcript.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["returncode"] == 1
    assert rows[0]["outcome"] == "failed:1"
