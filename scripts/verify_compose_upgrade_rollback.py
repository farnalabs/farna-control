#!/usr/bin/env python3
"""Run the disposable Docker Compose upgrade/failure/recovery acceptance matrix.

The harness deliberately runs against a disposable compose project and records
host, image, command, outcome, and container-log evidence.  It proves the
operator-visible sequence rather than asserting that a compose command returned
zero: clean install (old tag), upgrade (new tag, which takes a verified
pre-upgrade snapshot), an intentionally broken upgrade (invalid SECRET_KEY),
the previous image STARTING on the newer database and being refused by the
startup guard (exit 78 and the one-line refusal), restoring that snapshot with
the shipped helper, and restarting the previous release on the restored
database with the data written before the upgrade still present.

Migrations are forward-only and an older release must never serve a database a
newer one migrated (FAR-2659), so putting the previous image back on the
forward-migrated database is an INVALID state: the documented recovery
(FAR-2667) is restore the snapshot, then start the release that owns the
database.  The matrix asserts that documented path end to end.

Usage (from an extracted compose release directory):

    python verify_compose_upgrade_rollback.py \
      --compose-file docker-compose.prod.yml \
      --old-tag v0.3.1 --new-tag v0.3.2 \
      --evidence-dir evidence/compose-upgrade

The compose directory must contain the generated `.env` file.  The harness
always tears down its project, including volumes, in a finally block.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess  # nosec B404 - commands are fixed docker/curl argv lists
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ValidationError(RuntimeError):
    """Raised when the matrix cannot prove one of its required transitions."""


@dataclass(frozen=True)
class CommandResult:
    """Captured result for one command in the evidence transcript."""

    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ChildLogRead:
    """One read of a single backend child log file from inside the container."""

    stream: str
    path: str
    result: CommandResult

    @property
    def readable(self) -> bool:
        """Whether the container was up and the child log file could be read."""
        return self.result.returncode == 0

    @property
    def text(self) -> str:
        return f"{self.result.stdout}\n{self.result.stderr}"

    @property
    def source(self) -> str:
        """The exact command that was tried, for a failure message."""
        return shlex.join(self.result.argv)


@dataclass(frozen=True)
class ChildLogEvidence:
    """The terminal read of every backend child log file, plus poll count."""

    reads: tuple[ChildLogRead, ...]
    attempts: int

    @property
    def text(self) -> str:
        return "\n".join(read.text for read in self.reads)

    @property
    def unreadable(self) -> tuple[ChildLogRead, ...]:
        return tuple(read for read in self.reads if not read.readable)


def _tail_excerpt(text: str, *, max_lines: int = 4, limit: int = 300) -> str:
    """Collapse *text* to its last few non-empty lines for a failure message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    excerpt = " | ".join(lines[-max_lines:])
    if not excerpt:
        return "<empty>"
    if len(excerpt) > limit:
        return "..." + excerpt[-limit:]
    return excerpt


def validate_tag(tag: str, *, name: str) -> str:
    """Require a non-empty immutable-looking release tag."""
    value = tag.strip()
    if not value:
        raise ValidationError(f"{name} must not be empty")
    if value.lower() in {"latest", "stable"}:
        raise ValidationError(f"{name} must be immutable, not {value!r}")
    return value


#: Taint barrier for the operator-supplied compose project name (S8705).
#:
#: The harness forwards the project name into a ``docker compose argv``. Bound
#: it to its legitimate character set with ``re.fullmatch`` and pass only the
#: fresh match result to the child process, so a crafted value can never be
#: parsed as a CLI flag or carry an argument-injection payload
#: (pythonsecurity:S8705). Mirrors the established pattern in
#: ``scripts/fast_lane_classify.py``.
_PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _safe_project_name(value: str) -> str:
    """Return *value* only when it is a safe compose project name."""
    matched = _PROJECT_NAME_RE.fullmatch(value or "")
    if matched is None:
        raise ValidationError("--project-name must contain only letters, numbers, and hyphens")
    return matched.group(0)


def _helper_shell(name: str) -> str:
    """Absolute path to a POSIX shell used to run the shipped recovery helpers.

    Resolved through PATH deliberately: on Windows ``CreateProcess`` searches
    the system directory BEFORE PATH for a bare executable name, so ``bash``
    silently resolves to WSL's bash instead of the POSIX shell that can read the
    release bundle's paths. Passing the resolved absolute path removes that
    ambiguity; on the CI runner it resolves to ``/usr/bin/bash``.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise ValidationError(f"the {name} shell needed to run the shipped recovery helpers is not on PATH")
    return resolved


def build_compose_command(compose_file: Path, project_name: str, *args: str, env_file: Path | None = None) -> list[str]:
    """Build an isolated compose command using the selected environment file."""
    command = ["docker", "compose", "-p", project_name]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    command.extend(["-f", str(compose_file), *args])
    return command


_INVALID_SECRET_KEY = "intentionally-invalid-secret"

#: The specific startup failure an intentionally short SECRET_KEY must produce
#: (backend/src/farna/settings.py::_secret_key_is_strong). The assertion matches
#: this message, never "any error" or "any exit".
_INVALID_SECRET_KEY_FAILURE = "SECRET_KEY must be at least"

#: Placeholder for a value that was never observed: no HTTP status code, no
#: matching release log line, or an empty revision set. Kept as one constant so
#: the wording of "nothing was there" cannot drift across the assertions.
_ABSENT_VALUE = "<none>"

#: The all-in-one image runs supervisord, which dup2()s each child program's
#: streams onto per-program log files (deploy/supervisor/supervisord.conf).
#: The backend child's output therefore NEVER reaches the container's
#: stdout/stderr, so ``docker compose logs`` cannot show a backend startup
#: traceback - which is why the failure assertion reads these files from
#: inside the container instead. uvicorn reports on stderr and the pydantic
#: ``Settings`` traceback goes to stderr too, so both files are read: asserting
#: on backend.out.log alone would silently miss the failure.
_BACKEND_CHILD_LOGS: tuple[tuple[str, str], ...] = (
    ("stdout", "/var/log/supervisor/backend.out.log"),
    ("stderr", "/var/log/supervisor/backend.err.log"),
)

#: Backoff for polling the crash-looping backend child log: the failed-upgrade
#: ``up -d`` returns before the backend has even imported ``farna.api.main``,
#: so the first read can legitimately be empty.
_CHILD_LOG_POLL_INITIAL_SECONDS = 1.0
_CHILD_LOG_POLL_MAX_SECONDS = 5.0

#: Floor for how long the child log is polled before the failure is declared.
#: The backend reaches the ``Settings()`` call that rejects the key only after
#: its whole module tree has imported, which is the slowest point of a cold
#: start (measured: minutes on a cold, heavily loaded host), so this wait gets a
#: generous floor instead of reusing the per-command timeout.
_CHILD_LOG_MINIMUM_WAIT_SECONDS = 300

#: The packaged prod compose's application service (FAR-2651 rename). The
#: matrix always runs the bundle under test, whose compose names it this.
_APP_SERVICE = "farna-control"

#: The packaged prod compose's database service and the bootstrap database and
#: owner role it creates (deploy/compose/docker-compose.prod.yml). The recovery
#: leg asks the DATABASE which revisions are recorded, from inside the database
#: container, rather than trusting any application-reported state.
_POSTGRES_SERVICE = "postgres"
_POSTGRES_DB = "farna_control"
_POSTGRES_USER = "farna_control"

#: FAR-2659: the distinct status the startup guard returns when the database was
#: migrated by a newer release (78 = EX_CONFIG). A refusal must be
#: distinguishable from a crash, so the matrix asserts this exact status.
_EXIT_DATABASE_AHEAD = 78

#: The one-line prefix the startup guard writes when it refuses. That single
#: line must name the database revision, the running release version and the
#: documented recovery command (backend/src/farna/db/alignment.py::format_refusal).
_REFUSAL_PREFIX = "REFUSED: database revision "

#: The documented recovery command the guard names (FAR-2659). Mirrored from
#: ``farna.db.alignment.RECOVERY_COMMAND`` because this harness runs in the
#: release workflow without the backend installed; pinned equal by
#: backend/tests/unit/scripts/test_verify_compose_upgrade_rollback.py.
_DOCUMENTED_RESTORE_COMMAND = "farna-control restore <backup-dir> --yes"

#: The helpers the packaged release ships beside the compose file (FAR-2667).
_RESTORE_HELPER = "compose-restore.sh"
_ROLLBACK_HELPER = "compose-rollback.sh"

#: The all-in-one entrypoint's own lines (deploy/supervisor/entrypoint.sh) that
#: carry the verified pre-upgrade snapshot and the documented restore command.
#: The entrypoint echoes them before ``exec supervisord``, so they are the
#: first lines of the container log.
_SNAPSHOT_VERIFIED_PREFIX = "Pre-upgrade snapshot verified: "
_RESTORE_COMMAND_PREFIX = "Restore a bad release with: "
_NO_SNAPSHOT_MARKER = "No pre-upgrade snapshot was needed"
_SNAPSHOT_DIR_PREFIX = "pre-upgrade-dump-"

#: A row written into the database BEFORE the upgrade and read AFTER the
#: documented recovery. Values are literals, never interpolated from operator
#: input, so the statement needs no quoting of its own.
_PROBE_TABLE = "rc_matrix_upgrade_probe"
_PROBE_KEY = "pre-upgrade-row"
_PROBE_VALUE = "written-before-the-upgrade"

#: A restore replays a whole database dump through psql; give it more headroom
#: than a single compose command so a slow CI runner cannot time it out.
_RESTORE_TIMEOUT_SECONDS = 600

#: Floor for how long a HEALTHY readiness state is polled. A cold start applies
#: the whole migration chain (and, on the upgrade, the new release's migrations
#: too), which legitimately outlives one compose command's timeout on a loaded
#: runner - measured at ~100s for a local 0359 -> 0364 upgrade. A not-ready
#: assertion is not floored: it is satisfied by any reachable non-200.
_READY_MINIMUM_WAIT_SECONDS = 300


def _redact(text: str, secrets: set[str]) -> str:
    """Redact configured secret values without emitting them to evidence."""

    redacted = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _parse_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip():
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


# Only credential-bearing variables are redacted from the evidence transcript.
# Operator flags such as TAG and PORT are documented provenance (the transcript
# must show which immutable images ran), so their values are never in this set.
_CREDENTIAL_NAME_PARTS = ("SECRET", "PASSWORD", "TOKEN", "FERNET", "CREDENTIAL", "PRIVATE_KEY", "API_KEY")


def _is_credential_name(name: str) -> bool:
    upper = name.strip().upper()
    return any(part in upper for part in _CREDENTIAL_NAME_PARTS) or upper.endswith("_KEY")


def _secret_values(env: dict[str, str] | None) -> set[str]:
    """Collect values of credential-bearing variables only.

    ``TAG``/``PORT``/``COMPOSE_FILE`` and other operator flags are deliberately
    excluded: redacting them would rewrite the transcript's own provenance.
    """
    if env is None:
        return set()
    return {value for name, value in env.items() if value and _is_credential_name(name)}


def _secret_values_from_file(path: Path) -> set[str]:
    """Redact every configured env-file value, including non-obvious credentials."""
    return {value for value in _parse_env_values(path).values() if value}


class Evidence:
    """Write bounded, machine-readable command evidence and human-readable logs."""

    def __init__(self, directory: Path, *, secrets: set[str] | None = None) -> None:
        self.directory = directory
        if self.directory.exists():
            if any(self.directory.iterdir()):
                raise ValidationError(f"evidence directory is not empty: {self.directory}")
        else:
            self.directory.mkdir(parents=True)
        self.transcript = self.directory / "commands.jsonl"
        self._counter = 0
        self._secrets = set(secrets or ())

    def add_secrets(self, secrets: set[str]) -> None:
        self._secrets.update(secrets)

    def redact(self, text: str) -> str:
        """Redact configured credential values from text bound for a log or error."""
        return _redact(text, self._secrets)

    def record(self, result: CommandResult, *, outcome: str) -> None:
        self._counter += 1
        stem = f"{self._counter:02d}-{result.name}"
        stdout = _redact(result.stdout, self._secrets)
        stderr = _redact(result.stderr, self._secrets)
        argv = tuple(_redact(value, self._secrets) for value in result.argv)
        (self.directory / f"{stem}.stdout").write_text(stdout, encoding="utf-8")
        (self.directory / f"{stem}.stderr").write_text(stderr, encoding="utf-8")
        row = {
            "at": datetime.now(UTC).isoformat(),
            "name": result.name,
            "argv": list(argv),
            "command": shlex.join(argv),
            "returncode": result.returncode,
            "outcome": outcome,
            "stdout": f"{stem}.stdout",
            "stderr": f"{stem}.stderr",
        }
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def write_metadata(self, *, old_tag: str, new_tag: str, project_name: str, compose_file: Path) -> None:
        metadata = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "host": platform.node(),
            "os": platform.platform(),
            "python": sys.version,
            "compose_file": str(compose_file.resolve()),
            "old_tag": old_tag,
            "new_tag": new_tag,
            "project_name": project_name,
            "cwd": str(Path.cwd()),
        }
        (self.directory / "metadata.json").write_text(_redact(json.dumps(metadata, indent=2, sort_keys=True) + "\n", self._secrets), encoding="utf-8")


class Matrix:
    """Execute the compose acceptance sequence with explicit failure assertions."""

    def __init__(
        self,
        *,
        compose_file: Path,
        compose_dir: Path,
        env_file: Path,
        project_name: str,
        old_tag: str,
        new_tag: str,
        evidence: Evidence,
        port: str,
        timeout: int,
        runner: Any = subprocess.run,
    ) -> None:
        self.compose_file = compose_file
        self.compose_dir = compose_dir
        self.env_file = env_file
        self.project_name = project_name
        self.old_tag = old_tag
        self.new_tag = new_tag
        self.evidence = evidence
        self.port = port
        self.timeout = timeout
        self.runner = runner
        #: What this run actually proved, written to the RESULT artefact on success.
        self.result_line = ""

    def command(
        self,
        name: str,
        argv: list[str],
        *,
        expected: str = "success",
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> CommandResult:
        """Run one command, record it, and assert its requested outcome.

        ``expected`` is ``"success"``, ``"failure"``, or ``"captured"``:
        ``"captured"`` records whatever happened without asserting an outcome,
        because the caller asserts on the captured result itself - a non-zero
        exit there is information (a stopped container, for example), not a
        validation failure of its own.

        ``timeout`` overrides the per-command bound for a step that legitimately
        runs longer than one compose command (a database restore).
        """
        self.evidence.add_secrets(_secret_values(env))
        result = self.runner(
            argv,
            cwd=self.compose_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout if timeout is not None else self.timeout,
            check=False,
        )
        captured = CommandResult(name, tuple(argv), result.returncode, result.stdout or "", result.stderr or "")
        self.evidence.record(captured, outcome=expected if result.returncode == 0 else f"failed:{result.returncode}")
        if expected == "success" and result.returncode != 0:
            raise ValidationError(f"{name} failed with exit {result.returncode}; see evidence/{name}.stderr")
        if expected == "failure" and result.returncode == 0:
            raise ValidationError(f"{name} unexpectedly succeeded; the failed-upgrade proof is absent")
        return captured

    def compose(self, name: str, tag: str, *args: str, expected: str = "success", secret_key: str | None = None) -> CommandResult:
        env = dict(os.environ)
        env["TAG"] = tag
        env["PORT"] = self.port
        env["COMPOSE_FILE"] = str(self.compose_file)
        if secret_key is not None:
            env["SECRET_KEY"] = secret_key
        argv = build_compose_command(self.compose_file, self.project_name, *args, env_file=self.env_file)
        return self.command(name, argv, expected=expected, env=env)

    def _readiness_outcome(self, returncode: int) -> str:
        """Label one readiness poll for the evidence transcript."""
        if returncode == 0:
            return "success"
        return f"unreachable:{returncode}"

    def _readiness_reached(self, *, code: str, reachable: bool, expected: bool) -> bool:
        """Return whether a poll satisfies the requested readiness transition."""
        is_ready = reachable and code == "200"
        if expected:
            return is_ready
        return reachable and code.isdigit() and not is_ready

    def _record_readiness_assertion(self, name: str, url: str, code: str, is_ready: bool) -> bool:
        """Record the terminal readiness assertion and report the ready state."""
        outcome = "ready" if is_ready else "not-ready"
        self.evidence.record(
            CommandResult(name + "-assertion", (url,), 0, f"HTTP {code}\n", ""),
            outcome=outcome,
        )
        return is_ready

    def ready(self, name: str, *, expected: bool) -> bool:
        """Poll readiness until the requested state or the bounded deadline.

        ``expected=True`` waits for a healthy HTTP 200. ``expected=False`` waits
        until the readiness endpoint is reachable but no longer reports 200 -
        the documented "readiness is no longer HTTP 200" contract. That covers
        the real failed-upgrade failure mode: an invalid ``SECRET_KEY`` crashes
        the backend before it binds its port, so the all-in-one proxy answers
        ``502`` for the whole crash-loop window rather than a bare ``503``, and
        any reachable non-200 status satisfies the assertion.
        """
        url = f"http://127.0.0.1:{self.port}/healthz/ready"
        argv = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, min(10, self.timeout))),
            "--output",
            os.devnull,
            "--write-out",
            "%{http_code}",
            url,
        ]
        # Only a healthy wait gets the cold-start floor (see the constant).
        floor = _READY_MINIMUM_WAIT_SECONDS if expected else self.timeout
        deadline = time.monotonic() + max(self.timeout, floor)
        poll_interval = min(0.25, max(0.01, self.timeout / 10))
        attempt = 0
        last_code = _ABSENT_VALUE
        last_returncode = 0
        while True:
            attempt += 1
            raw = self.runner(
                argv,
                cwd=self.compose_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, min(10, self.timeout)),
                check=False,
            )
            result = CommandResult(name + f"-attempt-{attempt}", tuple(argv), raw.returncode, raw.stdout or "", raw.stderr or "")
            self.evidence.record(result, outcome=self._readiness_outcome(raw.returncode))
            code = result.stdout.strip()
            last_code = code or _ABSENT_VALUE
            last_returncode = raw.returncode
            reachable = raw.returncode == 0
            is_ready = reachable and code == "200"
            if self._readiness_reached(code=code, reachable=reachable, expected=expected):
                return self._record_readiness_assertion(name, url, code, is_ready)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        wanted = "ready (HTTP 200)" if expected else "not ready (any reachable non-200)"
        raise ValidationError(f"{name}: expected {wanted} before deadline, received HTTP {last_code} (curl exit {last_returncode})")

    def _assert_failed_upgrade_config(self, text: str, name: str) -> None:
        if self.new_tag not in text:
            raise ValidationError(f"{name} compose config did not use the requested new tag")
        if _INVALID_SECRET_KEY not in text:
            raise ValidationError(f"{name} compose config did not receive the intentionally invalid SECRET_KEY")

    def _read_backend_child_logs(self, attempt: int) -> tuple[ChildLogRead, ...]:
        """Read every backend child log file from inside the container."""
        return tuple(
            ChildLogRead(
                stream=stream,
                path=path,
                result=self.compose(
                    f"failed-upgrade-backend-log-{stream}-attempt-{attempt}",
                    self.new_tag,
                    "exec",
                    "-T",
                    "farna-control",
                    "cat",
                    path,
                    expected="captured",
                ),
            )
            for stream, path in _BACKEND_CHILD_LOGS
        )

    def _capture_backend_child_logs(self) -> ChildLogEvidence:
        """Poll the backend child logs until the invalid-key failure appears.

        The failed-upgrade container is a crash-loop: ``up -d`` returns as soon
        as the container is created, so an immediate read can land before the
        backend has even imported ``farna.api.main``. Poll (bounded by
        ``--timeout``, floored at ``_CHILD_LOG_MINIMUM_WAIT_SECONDS``) until a
        child log carries the specific invalid-SECRET_KEY failure; never stop
        early on "any error" or "any non-zero exit".
        """
        deadline = time.monotonic() + max(self.timeout, _CHILD_LOG_MINIMUM_WAIT_SECONDS)
        interval = _CHILD_LOG_POLL_INITIAL_SECONDS
        attempt = 0
        while True:
            attempt += 1
            evidence = ChildLogEvidence(reads=self._read_backend_child_logs(attempt), attempts=attempt)
            if _INVALID_SECRET_KEY_FAILURE in evidence.text:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 2, _CHILD_LOG_POLL_MAX_SECONDS)
        return evidence

    def _assert_failed_upgrade_startup_failure(self, child_logs: ChildLogEvidence, container_log: CommandResult) -> None:
        """Require the invalid-SECRET_KEY startup failure in the backend child log.

        The container log is supplementary context only: it is recorded in the
        evidence transcript and quoted on failure, but it can never satisfy the
        assertion, because supervisord redirects the backend child's output
        away from it.
        """
        if _INVALID_SECRET_KEY_FAILURE in child_logs.text:
            return
        unreadable = child_logs.unreadable
        if unreadable:
            details = "; ".join(f"{read.source} -> {_tail_excerpt(read.text, max_lines=1, limit=160)} (exit {read.result.returncode})" for read in unreadable)
            child_state = f"unreadable, the container did not stay up: {details}"
        else:
            child_state = "readable but it did not contain the startup failure"
        raise ValidationError(
            "failed-upgrade logs did not contain the expected invalid SECRET_KEY startup failure. "
            f"Sources tried ({child_logs.attempts} read attempt(s)): backend child log "
            f"{'; '.join(read.source for read in child_logs.reads)} [{child_state}]; "
            f"container log {shlex.join(container_log.argv)} "
            f"[{_tail_excerpt(self.evidence.redact(container_log.stdout))}]"
        )

    # ------------------------------------------------------------------
    # The documented recovery leg (FAR-2674)
    #
    # Under the agreed upgrade contract (FAR-2659) migrations are forward-only:
    # putting the PREVIOUS image back on a database a NEWER release migrated is
    # an invalid state the startup guard refuses, so the only documented
    # recovery is restoring the verified pre-upgrade snapshot (FAR-2667) and
    # starting the release that owns the database. Each helper below asserts one
    # link of that chain against real published images and real container state.
    # ------------------------------------------------------------------

    def _record_assertion(self, name: str, detail: str, *, outcome: str) -> None:
        """Record a synthetic evidence row for an assertion on captured output."""
        self.evidence.record(CommandResult(name, (), 0, detail.rstrip("\n") + "\n", ""), outcome=outcome)

    def _psql(self, name: str, statement: str, *, expected: str = "success") -> CommandResult:
        """Run one psql statement in the database container without a shell.

        The statement is a fixed argv element (no shell), and every statement
        this harness issues is built from the module's own literals, so neither
        operator input nor container output reaches SQL.
        """
        return self.compose(
            name,
            self.new_tag,
            "exec",
            "-T",
            _POSTGRES_SERVICE,
            "psql",
            "-U",
            _POSTGRES_USER,
            "-d",
            _POSTGRES_DB,
            "-v",
            "ON_ERROR_STOP=1",
            "-tA",
            "-c",
            statement,
            expected=expected,
        )

    def _database_revisions(self, name: str) -> tuple[str, ...]:
        """Read the revision(s) alembic records in the live database."""
        result = self._psql(name, "SELECT version_num FROM alembic_version ORDER BY version_num")
        return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _write_pre_upgrade_row(self) -> None:
        """Write the row the recovery must still hold afterwards."""
        statement = f"CREATE TABLE {_PROBE_TABLE} (probe_key text PRIMARY KEY, note text NOT NULL); INSERT INTO {_PROBE_TABLE} (probe_key, note) VALUES ('{_PROBE_KEY}', '{_PROBE_VALUE}');"
        self._psql("write-pre-upgrade-row", statement)

    def _assert_pre_upgrade_row(self, name: str, *, after_recovery: bool) -> None:
        """Require the row written before the upgrade to be present.

        A restore that silently did nothing, restored the wrong snapshot, or
        left the database empty cannot satisfy this.
        """
        result = self._psql(
            name,
            f"SELECT note FROM {_PROBE_TABLE} WHERE probe_key = '{_PROBE_KEY}'",
            expected="captured",
        )
        value = result.stdout.strip()
        if result.returncode != 0 or value != _PROBE_VALUE:
            where = "after the recovery" if after_recovery else "before the upgrade"
            raise ValidationError(f"{name}: the pre-upgrade marker row is missing or changed {where} (psql exit {result.returncode}, value {value!r}, expected {_PROBE_VALUE!r}); the database does not hold the row written before the upgrade")

    def _assert_app_stopped(self) -> None:
        """Require the application container to be down."""
        result = self.compose("app-running-check", self.new_tag, "ps", "--status", "running", "--services")
        running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        if _APP_SERVICE in running:
            raise ValidationError(f"the bad release is still running ({_APP_SERVICE}); the stack must be down with the database migrated forward before the guard refusal and the restore are asserted")

    def _assert_upgrade_snapshot(self, upgrade_logs: CommandResult) -> str | None:
        """Require the new release's verified snapshot and its documented restore line.

        Returns the in-container snapshot path, or ``None`` when the release
        documented that no migration was pending (no recovery point is then
        owed). A release that migrated the database but printed no snapshot is
        caught by :meth:`_recovery_required`, so this method never turns a
        missing recovery point into a pass.
        """
        text = f"{upgrade_logs.stdout}\n{upgrade_logs.stderr}"
        lines = [line.strip() for line in text.splitlines()]
        verified = [line for line in lines if line.startswith(_SNAPSHOT_VERIFIED_PREFIX)]
        if not verified:
            if any(_NO_SNAPSHOT_MARKER in line for line in lines):
                self._record_assertion(
                    "upgrade-snapshot-assertion",
                    "the release reported no migration pending, so it took no pre-upgrade snapshot",
                    outcome="snapshot-not-required",
                )
                return None
            raise ValidationError(f"the new release did not report a verified pre-upgrade snapshot: expected {_SNAPSHOT_VERIFIED_PREFIX!r}<path> (or {_NO_SNAPSHOT_MARKER!r} when no migration is pending) in its log. Last lines: {_tail_excerpt(text)}")
        if len(verified) != 1:
            raise ValidationError(f"expected exactly one {_SNAPSHOT_VERIFIED_PREFIX!r} line, found {len(verified)}: {verified!r}")
        snapshot = verified[0][len(_SNAPSHOT_VERIFIED_PREFIX) :].strip()
        if not snapshot:
            raise ValidationError("the release printed an empty pre-upgrade snapshot path")
        if f"/{_SNAPSHOT_DIR_PREFIX}" not in snapshot:
            raise ValidationError(f"the printed pre-upgrade snapshot path {snapshot!r} is not a {_SNAPSHOT_DIR_PREFIX}* directory on the snapshot volume; the documented restore cannot target it")
        restore_lines = [line for line in lines if line.startswith(_RESTORE_COMMAND_PREFIX)]
        documented = f"./{_RESTORE_HELPER} --confirm {snapshot}"
        if len(restore_lines) != 1 or documented not in restore_lines[0]:
            found = restore_lines[0] if restore_lines else _ABSENT_VALUE
            raise ValidationError(f"the release did not print the documented recovery command for the snapshot it took: expected a {_RESTORE_COMMAND_PREFIX!r} line naming {documented!r}, found {found!r}")
        presence = self.compose(
            "upgrade-snapshot-presence",
            self.new_tag,
            "exec",
            "-T",
            _APP_SERVICE,
            "sh",
            "-c",
            'test -d "$1"',
            "sh",
            snapshot,
            expected="captured",
        )
        if presence.returncode != 0:
            raise ValidationError(f"the release printed pre-upgrade snapshot {snapshot!r} but that directory is not present in the container (exit {presence.returncode}); the recovery point is missing")
        self._record_assertion(
            "upgrade-snapshot-assertion",
            f"snapshot: {snapshot}\nrestore: {restore_lines[0]}\npresent: true",
            outcome="snapshot-verified",
        )
        return snapshot

    def _recovery_required(self, revisions_before: tuple[str, ...], revisions_after: tuple[str, ...], snapshot: str | None) -> bool:
        """Whether this pair migrated the database and therefore owes a recovery point."""
        before = ", ".join(revisions_before) or _ABSENT_VALUE
        after = ", ".join(revisions_after) or _ABSENT_VALUE
        if revisions_after == revisions_before:
            detail = f"no migration ran ({before} unchanged), so no snapshot was owed and the documented restore has nothing to recover; the previous image is started on the unchanged schema"
            if snapshot is not None:
                detail = f"no migration ran ({before} unchanged) although the release took snapshot {snapshot}; the recovery leg is skipped"
            self._record_assertion("recovery-not-required", detail, outcome="recovery-not-required")
            return False
        if snapshot is None:
            raise ValidationError(f"the new release migrated the database ({before} -> {after}) but took no verified pre-upgrade snapshot; the documented recovery has no recovery point to restore")
        return True

    def _assert_revisions_unchanged(self, name: str, expected: tuple[str, ...]) -> None:
        """Require the database to still record the expected revision(s)."""
        actual = self._database_revisions(name)
        if actual != expected:
            raise ValidationError(f"{name}: database revisions are {', '.join(actual) or '<none>'}, expected {', '.join(expected) or '<none>'}")

    def _predecessor_ships_alignment_guard(self) -> bool:
        """Does the predecessor image carry the FAR-2659 startup guard?

        Recorded as evidence either way: the refusal assertion can only be made
        against an image that ships the guard, and the first image that does is
        bundle-v0.3.330. A probe that fails for any OTHER reason is a hard
        failure - an undecidable capability must never pass as "not applicable".
        """
        result = self.compose(
            "predecessor-alignment-guard-probe",
            self.old_tag,
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "python",
            _APP_SERVICE,
            "-c",
            "import farna.db.alignment",
            expected="captured",
        )
        if result.returncode == 0:
            return True
        text = f"{result.stdout}\n{result.stderr}"
        if "No module named 'farna.db.alignment'" in text:
            self._record_assertion(
                "guard-refusal-assertion",
                "the predecessor image predates the database alignment guard (no farna.db.alignment), so a refusal cannot be asserted for this release pair",
                outcome="guard-refusal-not-applicable",
            )
            return False
        raise ValidationError(f"could not determine whether the predecessor image ships the database alignment guard: 'import farna.db.alignment' exited {result.returncode}: {_tail_excerpt(text)}")

    def _predecessor_version(self) -> str:
        """The version the predecessor image reports for itself."""
        result = self.compose(
            "predecessor-version",
            self.old_tag,
            "run",
            "--rm",
            "-T",
            "--no-deps",
            "--entrypoint",
            "farna-control",
            _APP_SERVICE,
            "version",
        )
        parts = result.stdout.strip().split()
        if len(parts) != 2 or parts[0] != "farna-control":
            raise ValidationError(f"could not read the predecessor's reported version from {result.stdout.strip()!r}")
        return parts[1]

    def _assert_guard_refusal(self, *, predecessor_version: str, revisions_after: tuple[str, ...]) -> str:
        """Require the previous image to refuse a database from a newer release.

        Proves all four parts of the contract: the distinct status, exactly one
        line, the database revision and running release version it names, and
        the documented recovery command it points at.
        """
        refusal = self.compose(
            "previous-image-on-newer-database",
            self.old_tag,
            "run",
            "--rm",
            "-T",
            "--no-deps",
            _APP_SERVICE,
            expected="captured",
        )
        text = f"{refusal.stdout}\n{refusal.stderr}"
        if refusal.returncode != _EXIT_DATABASE_AHEAD:
            raise ValidationError(f"the previous image (farna-control {predecessor_version}) must refuse a database migrated by a newer release with exit {_EXIT_DATABASE_AHEAD}; it exited {refusal.returncode}. Last lines: {_tail_excerpt(text)}")
        refused = [line.strip() for line in text.splitlines() if line.strip().startswith(_REFUSAL_PREFIX)]
        if len(refused) != 1:
            raise ValidationError(f"expected exactly one {_REFUSAL_PREFIX!r}... line from the guard, found {len(refused)}. Last lines: {_tail_excerpt(text)}")
        message = refused[0]
        expected_revisions = ", ".join(revisions_after)
        expected_lead = f"{_REFUSAL_PREFIX}{expected_revisions} is not known to this release (farna-control {predecessor_version})"
        if not message.startswith(expected_lead):
            raise ValidationError(f"the guard refusal must name the database revision and the running release version: expected it to start {expected_lead!r}, got {message!r}")
        if _DOCUMENTED_RESTORE_COMMAND not in message:
            raise ValidationError(f"the guard refusal must name the documented recovery command {_DOCUMENTED_RESTORE_COMMAND!r}: {message!r}")
        self._record_assertion(
            "guard-refusal-assertion",
            f"exit: {_EXIT_DATABASE_AHEAD}\nmessage: {message}",
            outcome="refused",
        )
        return message

    def _helper_env(self, *, tag: str | None = None) -> dict[str, str]:
        """Environment for the shipped bundle helpers.

        ``COMPOSE_PROJECT_NAME`` is load-bearing: without it the helpers would
        derive a new project (and therefore new, EMPTY volumes) from the
        directory name, and the restore would then target a database that never
        held the snapshot.
        """
        env = dict(os.environ)
        env["PORT"] = self.port
        env["COMPOSE_FILE"] = str(self.compose_file)
        env["COMPOSE_PROJECT_NAME"] = self.project_name
        if tag is not None:
            env["TAG"] = tag
        return env

    def _restore_snapshot(self, snapshot: str) -> None:
        """Restore the snapshot through the shipped, documented helper."""
        helper = self.compose_dir / _RESTORE_HELPER
        if not helper.is_file():
            raise ValidationError(f"the documented recovery helper is missing: {helper}; the release bundle must ship {_RESTORE_HELPER} beside the compose file")
        try:
            result = self.command(
                "restore-pre-upgrade-snapshot",
                [_helper_shell("bash"), helper.as_posix(), "--confirm", snapshot],
                expected="success",
                env=self._helper_env(tag=self.new_tag),
                timeout=max(self.timeout, _RESTORE_TIMEOUT_SECONDS),
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError(f"the documented restore timed out after {max(self.timeout, _RESTORE_TIMEOUT_SECONDS)}s") from exc
        output = f"{result.stdout}\n{result.stderr}"
        if "Restore complete. Writers stay stopped." not in output:
            raise ValidationError(f"the documented restore did not report completion; refusing to start the previous release on an unverified database. Output: {_tail_excerpt(output)}")
        if _ROLLBACK_HELPER not in output:
            raise ValidationError(f"the documented restore did not print the shipped start command ({_ROLLBACK_HELPER}); Output: {_tail_excerpt(output)}")
        detail = "\n".join(line.strip() for line in output.splitlines() if line.strip() and ("Restore complete" in line or _ROLLBACK_HELPER in line or "Restoring" in line))
        self._record_assertion("restore-outcome", detail or "<no restore output>", outcome="restored")

    def _start_previous_release(self) -> None:
        """Start the previous release through the shipped, documented helper."""
        helper = self.compose_dir / _ROLLBACK_HELPER
        if not helper.is_file():
            raise ValidationError(f"the documented rollback helper is missing: {helper}; the release bundle must ship {_ROLLBACK_HELPER} beside the compose file")
        self.command(
            "start-previous-release",
            [_helper_shell("sh"), helper.as_posix(), self.old_tag],
            expected="success",
            env=self._helper_env(),
        )

    def _describe_result(self, *, recovery_required: bool, guard_refused: bool) -> str:
        """One line a reader can trust about what this run actually proved."""
        steps = [
            "clean-install",
            "upgrade (verified pre-upgrade snapshot)" if recovery_required else "upgrade (no migration pending)",
            "intentionally failed upgrade",
            "bad-release stop",
        ]
        if guard_refused:
            steps.append(f"guard refusal (exit {_EXIT_DATABASE_AHEAD})")
        if recovery_required:
            steps.append("restore pre-upgrade snapshot")
        steps.append("previous release ready with pre-upgrade data")
        return "PASS " + " -> ".join(steps)

    def run(self) -> None:
        self.evidence.write_metadata(
            old_tag=self.old_tag,
            new_tag=self.new_tag,
            project_name=self.project_name,
            compose_file=self.compose_file,
        )
        self.command("docker-version", ["docker", "version"], expected="success")
        self.command(
            "compose-config-old",
            build_compose_command(self.compose_file, self.project_name, "config", env_file=self.env_file),
            expected="success",
            env={**os.environ, "TAG": self.old_tag, "PORT": self.port},
        )
        config = self.compose(
            "compose-config-invalid-secret",
            self.new_tag,
            "config",
            expected="success",
            secret_key=_INVALID_SECRET_KEY,
        )
        config_text = f"{config.stdout}\n{config.stderr}"
        self._assert_failed_upgrade_config(config_text, "invalid-secret")

        # 1. Upgrade phase: the previous release, then the new release, both
        #    proven ready. The pre-upgrade row is written while the OLD release
        #    serves, so it is inside the snapshot the new release takes.
        self.compose("clean-install-old", self.old_tag, "up", "-d", "--remove-orphans")
        self.ready("clean-install-ready", expected=True)
        revisions_before = self._database_revisions("database-revisions-before-upgrade")
        self._write_pre_upgrade_row()
        self._assert_pre_upgrade_row("pre-upgrade-row-before-upgrade", after_recovery=False)

        self.compose("upgrade-new", self.new_tag, "up", "-d", "--remove-orphans")
        self.ready("upgrade-ready", expected=True)
        # ``--no-log-prefix`` is load-bearing: the entrypoint's own lines are
        # parsed below, and compose would otherwise prefix each with
        # "<service>  | ".
        upgrade_logs = self.compose("upgrade-logs", self.new_tag, "logs", "--no-color", "--no-log-prefix", _APP_SERVICE)
        snapshot = self._assert_upgrade_snapshot(upgrade_logs)
        revisions_after = self._database_revisions("database-revisions-after-upgrade")

        # The invalid key is deliberately too short for Settings. --force-recreate
        # ensures Compose replaces the healthy new container instead of treating
        # the command as a no-op. A successful command is not enough: readiness
        # must become unavailable before the recovery is accepted. The backend
        # crash-loops before binding its port, so the proxy serves 502 for the
        # whole window - assert "no longer 200" rather than a single status code.
        self.compose(
            "failed-upgrade",
            self.new_tag,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            _APP_SERVICE,
            expected="success",
            secret_key=_INVALID_SECRET_KEY,
        )
        self.ready("failed-upgrade-not-ready", expected=False)
        # supervisord writes the backend child's streams to its own log files,
        # so the container log below is recorded as supplementary context only.
        # The assertion reads the backend child log from inside the container.
        child_logs = self._capture_backend_child_logs()
        failed_container_logs = self.compose("failed-upgrade-logs", self.new_tag, "logs", "--no-color", "--tail=200", _APP_SERVICE)
        self._assert_failed_upgrade_startup_failure(child_logs, failed_container_logs)

        # 2. Simulate a bad release: stop it, leaving the stack DOWN with the
        #    database migrated forward - the state the previous image must
        #    refuse and the documented recovery must resolve.
        self.compose("bad-release-stop", self.new_tag, "stop", _APP_SERVICE)
        self._assert_app_stopped()
        recovery_required = self._recovery_required(revisions_before, revisions_after, snapshot)

        # 3. The startup guard refuses the previous image on the newer database.
        guard_refused = False
        if recovery_required and self._predecessor_ships_alignment_guard():
            predecessor_version = self._predecessor_version()
            self._assert_guard_refusal(predecessor_version=predecessor_version, revisions_after=revisions_after)
            # The guard is read-only: it must have refused BEFORE migrating.
            self._assert_revisions_unchanged("database-revisions-after-guard-refusal", revisions_after)
            guard_refused = True

        # 4. Restore the pre-upgrade snapshot the new release took.
        if recovery_required and snapshot is not None:
            self._restore_snapshot(snapshot)
            self._assert_revisions_unchanged("database-revisions-after-restore", revisions_before)

        # 5. Start the previous image and prove it serves the pre-upgrade data.
        self._start_previous_release()
        self.ready("rollback-ready", expected=True)
        self._assert_pre_upgrade_row("pre-upgrade-row-after-recovery", after_recovery=True)
        self.compose("rollback-logs", self.old_tag, "logs", "--no-color", "--tail=200", _APP_SERVICE)
        self.result_line = self._describe_result(recovery_required=recovery_required, guard_refused=guard_refused)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--old-tag", required=True)
    parser.add_argument("--new-tag", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-name", default=f"farna-control-upgrade-{os.getpid()}")
    parser.add_argument("--port", default="18080")
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def _run_cleanup(
    *,
    cleanup: list[str],
    compose_dir: Path,
    cleanup_env: dict[str, str],
    timeout: int,
    evidence: Evidence,
    primary_error: BaseException | None,
) -> None:
    """Tear down the disposable compose project without masking a primary error."""
    try:
        result = subprocess.run(
            cleanup,
            cwd=compose_dir,
            env=cleanup_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )  # nosec B603 - fixed compose cleanup command
        evidence.record(
            CommandResult("cleanup", tuple(cleanup), result.returncode, result.stdout or "", result.stderr or ""),
            outcome="cleanup-success" if result.returncode == 0 else f"cleanup-failed:{result.returncode}",
        )
        if result.returncode != 0 and primary_error is None:
            raise ValidationError(f"cleanup failed with exit {result.returncode}")
    except subprocess.TimeoutExpired as exc:
        if primary_error is None:
            raise ValidationError("cleanup timed out; owned containers and volumes may remain") from exc
        print(f"cleanup timed out after primary failure: {exc}", file=sys.stderr)
    except OSError as exc:
        if primary_error is None:
            raise ValidationError(f"cleanup could not run: {exc}") from exc
        print(f"cleanup could not run after primary failure: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    compose_file = args.compose_file.resolve()
    if not compose_file.is_file():
        raise ValidationError(f"compose file does not exist: {compose_file}")
    compose_dir = compose_file.parent
    env_file = (args.env_file or compose_dir / ".env").resolve()
    if not env_file.is_file():
        raise ValidationError(f"compose environment file does not exist: {env_file}")
    old_tag = validate_tag(args.old_tag, name="--old-tag")
    new_tag = validate_tag(args.new_tag, name="--new-tag")
    if old_tag == new_tag:
        raise ValidationError("--old-tag and --new-tag must be different")
    project_name = _safe_project_name(args.project_name)

    evidence_dir = args.evidence_dir.resolve()
    evidence_secrets = _secret_values(dict(os.environ)) | _secret_values_from_file(env_file)
    # Never redact the operator flags the harness itself sets, even if an env
    # file happens to define the same value: they are documented provenance.
    evidence_secrets -= {old_tag, new_tag, args.port}
    evidence_secrets.discard("")
    evidence = Evidence(evidence_dir, secrets=evidence_secrets)
    matrix = Matrix(
        compose_file=compose_file,
        compose_dir=compose_dir,
        env_file=env_file,
        project_name=project_name,
        old_tag=old_tag,
        new_tag=new_tag,
        evidence=evidence,
        port=args.port,
        timeout=args.timeout,
    )
    cleanup_env = {**os.environ, "TAG": old_tag, "PORT": args.port}
    cleanup = build_compose_command(
        compose_file,
        project_name,
        "down",
        "--volumes",
        "--remove-orphans",
        env_file=env_file,
    )
    primary_error: BaseException | None = None
    try:
        matrix.run()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        _run_cleanup(
            cleanup=cleanup,
            compose_dir=compose_dir,
            cleanup_env=cleanup_env,
            timeout=args.timeout,
            evidence=evidence,
            primary_error=primary_error,
        )

    result_file = evidence.directory / "RESULT"
    if primary_error is not None:
        return 1
    if result_file.exists():
        return 0
    if not matrix.result_line:
        raise ValidationError("the matrix completed without recording what it proved")
    result_file.write_text(matrix.result_line + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, subprocess.CalledProcessError) as exc:
        print(f"upgrade/rollback validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
