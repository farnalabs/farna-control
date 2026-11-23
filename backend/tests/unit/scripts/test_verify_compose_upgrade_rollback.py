"""Contract tests for the Docker Compose upgrade/rollback acceptance harness."""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

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


def test_matrix_records_every_required_transition(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    failed_upgrade_env: dict[str, str] | None = None

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        nonlocal failed_upgrade_env
        calls.append(tuple(argv))
        if argv[0] == "docker" and "--force-recreate" in argv:
            failed_upgrade_env = kwargs["env"]
            assert failed_upgrade_env["SECRET_KEY"] == module._INVALID_SECRET_KEY
            assert failed_upgrade_env["TAG"] == "v1.1.0"
        if argv[0] == "curl":
            phase = next(item for item in reversed(calls) if item[0] == "docker" and "up" in item)
            # The real failed-upgrade mode is an import-time crash-loop, so the
            # all-in-one proxy answers 502 (not 503) while the backend restarts.
            status = "502" if "--force-recreate" in phase else "200"
            return SimpleNamespace(returncode=0, stdout=status, stderr="")
        if argv[0] == "docker" and "exec" in argv:
            # Only the backend child's stderr log carries the startup failure;
            # supervisord redirects it out of the container log entirely.
            body = SECRET_KEY_FAILURE if argv[-1].endswith("backend.err.log") else ""
            return SimpleNamespace(returncode=0, stdout=body, stderr="")
        if argv[0] == "docker" and "logs" in argv:
            # Supplementary context only: the real container log never contains
            # the backend child's traceback. The matrix may only pass because it
            # reads the child log.
            return SimpleNamespace(returncode=0, stdout=CONTAINER_LOG_BODY, stderr="")
        if argv[0] == "docker" and "config" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="image: ghcr.io/farnalabs/farna-control:v1.1.0\nSECRET_KEY: intentionally-invalid-secret",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    matrix.run()

    names = [json.loads(line)["name"] for line in evidence.transcript.read_text(encoding="utf-8").splitlines()]
    assert names == [
        "docker-version",
        "compose-config-old",
        "compose-config-invalid-secret",
        "clean-install-old",
        "clean-install-ready-attempt-1",
        "clean-install-ready-assertion",
        "upgrade-new",
        "upgrade-ready-attempt-1",
        "upgrade-ready-assertion",
        "failed-upgrade",
        "failed-upgrade-not-ready-attempt-1",
        "failed-upgrade-not-ready-assertion",
        "failed-upgrade-backend-log-stdout-attempt-1",
        "failed-upgrade-backend-log-stderr-attempt-1",
        "failed-upgrade-logs",
        "rollback-old",
        "rollback-ready-attempt-1",
        "rollback-ready-assertion",
        "rollback-logs",
    ]
    assert failed_upgrade_env is not None
    (evidence.directory / "RESULT").write_text(
        "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification\n",
        encoding="utf-8",
    )
    assert "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification" in (evidence.directory / "RESULT").read_text(encoding="utf-8")


def _unused_runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
    raise AssertionError(f"log-source assertion must not shell out: {argv}")


def _matrix(tmp_path: Path, runner: Any = _unused_runner, *, timeout: int = 5) -> Any:
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


def test_failed_upgrade_reads_the_backend_child_log_not_the_container_log(tmp_path: Path) -> None:
    """The fix under test: the container log can never satisfy the assertion."""
    matrix = _matrix(tmp_path)
    container_log = _container_log()

    assert module._INVALID_SECRET_KEY_FAILURE not in container_log.stdout
    matrix._assert_failed_upgrade_startup_failure(_child_log_evidence(stderr_body=SECRET_KEY_FAILURE), container_log)


def test_failed_upgrade_rejects_a_child_log_without_the_secret_key_failure(tmp_path: Path) -> None:
    matrix = _matrix(tmp_path)
    child_logs = _child_log_evidence(stderr_body="ModuleNotFoundError: No module named 'farna'\n")

    with pytest.raises(ValidationError, match="readable but it did not contain the startup failure"):
        matrix._assert_failed_upgrade_startup_failure(child_logs, _container_log())


def test_failed_upgrade_rejects_an_unreadable_child_log(tmp_path: Path) -> None:
    """A stopped container must fail loudly, never pass on the container log."""
    matrix = _matrix(tmp_path)
    child_logs = _child_log_evidence(stderr_returncode=1)

    with pytest.raises(ValidationError, match="did not stay up") as excinfo:
        matrix._assert_failed_upgrade_startup_failure(child_logs, _container_log())

    message = str(excinfo.value)
    assert "/var/log/supervisor/backend.out.log" in message
    assert "/var/log/supervisor/backend.err.log" in message


def test_failed_upgrade_failure_names_both_log_sources(tmp_path: Path) -> None:
    """A running container with a silent child log is distinguishable from an exited one."""
    matrix = _matrix(tmp_path)

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

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=1,
        runner=runner,
    )
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

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=1,
        runner=runner,
    )
    assert matrix.ready("startup-ready", expected=True) is True
    assert attempts == 3


def test_ready_times_out_after_transient_connection_refusal(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=7, stdout="", stderr="connection refused")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=1,
        runner=runner,
    )
    with pytest.raises(ValidationError, match="before deadline"):
        matrix.ready("startup-ready", expected=True)


def test_matrix_rejects_a_healthy_failed_upgrade(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "curl":
            return SimpleNamespace(returncode=0, stdout="200", stderr="")
        if argv[0] == "docker" and "config" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="image: ghcr.io/farnalabs/farna-control:v1.1.0\nSECRET_KEY: intentionally-invalid-secret",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    with pytest.raises(ValidationError, match="not ready"):
        matrix.run()


def test_matrix_records_failed_compose_commands_as_failures(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "docker" and "version" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    with pytest.raises(ValidationError, match="docker-version failed"):
        matrix.run()
    rows = [json.loads(line) for line in evidence.transcript.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["returncode"] == 1
    assert rows[0]["outcome"] == "failed:1"
