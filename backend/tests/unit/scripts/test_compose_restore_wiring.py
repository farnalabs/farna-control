"""FAR-2667: structural wiring tests for the compose recovery helper.

The restore path shipped non-functional twice because nothing exercised the
helper or the entrypoint wiring. These are fast, Docker-free static checks that
pin what makes the recovery work:

* every one-off container overrides the image ENTRYPOINT and runs the installed
  ``farna-control`` executable (the all-in-one image ENTRYPOINT is
  ``/entrypoint.sh``, which ignores its arguments, migrates and execs
  supervisord — and the bare token ``restore`` is not an installed command);
* the script never reads or passes the database URL, so the operator's shell
  (``set -u``) needs no compose-only variable and no superuser password ever
  reaches a host command line;
* both entrypoints print the helper path the shipped bundle actually contains
  (``./compose-restore.sh``, not ``scripts/compose-restore.sh``);
* the installer and the compose/supervisor boots cannot bypass the shared
  migration-pending check, and the Fly boot does not pretend to take a snapshot
  it can never take (its release stage migrates before machines boot).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "compose-restore.sh").exists():
            return parent
    raise RuntimeError("Could not find repo root (scripts/compose-restore.sh)")


_REPO_ROOT = _repo_root()
_HELPER = _REPO_ROOT / "scripts" / "compose-restore.sh"


def _text() -> str:
    return _HELPER.read_text(encoding="utf-8")


def _executable() -> str:
    """The helper's non-comment, non-blank lines.

    The explanatory comments name the very things the code must not do (they
    document ``--entrypoint`` and the environment variable the container
    receives), so only executable text can evidence a defect.
    """
    return "\n".join(line for line in _text().splitlines() if line.strip() and not line.lstrip().startswith("#"))


def test_helper_runs_under_strict_shell_mode() -> None:
    assert "set -Eeuo pipefail" in _text()


def test_every_one_off_run_overrides_the_image_entrypoint() -> None:
    """The pre-fix helper appended `restore ...` to /entrypoint.sh, which
    ignored it, migrated the live database and hung."""
    executable = _executable()
    runs = executable.count("compose run")
    overrides = executable.count("--entrypoint")
    assert runs >= 3, "the helper runs presence, dry-run, target and restore steps"
    assert overrides == runs, "EVERY `compose run` must override the image ENTRYPOINT; without --entrypoint the all-in-one image's /entrypoint.sh ignores the arguments, migrates the live database and execs supervisord"


def test_the_restore_steps_run_the_installed_cli() -> None:
    executable = _executable()
    assert executable.count("--entrypoint farna-control") >= 3
    # `restore` alone is not an installed command; it must reach the CLI as a
    # SUBCOMMAND after an explicit --entrypoint.
    for invocation in re.findall(r"compose run[^\n]*--entrypoint (\S+)", executable):
        assert invocation in {"farna-control", "sh"}, invocation
    # A `compose run` that relies on the image ENTRYPOINT is the shape that
    # silently started supervisord instead of restoring.
    for line in executable.splitlines():
        if "compose run" in line:
            assert "--entrypoint" in line, line


def test_the_verification_steps_run_the_documented_flags() -> None:
    executable = _executable()
    assert 'restore "$container_snapshot" --dry-run' in executable
    assert "--verify-target-schema" in executable
    assert "--safety-dump-dir /backups" in executable
    assert "--no-deps" in executable, "the one-off container must never restart the database"


@pytest.mark.parametrize(
    "forbidden",
    ["DATABASE_ADMIN_URL", "DATABASE_URL", "DATABASE_", "--db-url", "PGPASSWORD", "PGPASSFILE", "postgres://", "postgresql://"],
    ids=[
        "database-admin-url",
        "database-url",
        "database-prefix",
        "db-url-flag",
        "pgpassword",
        "pgpassfile",
        "postgres-scheme",
        "postgresql-scheme",
    ],
)
def test_the_helper_never_references_a_connection_url_or_credential(forbidden: str) -> None:
    """The one-off container inherits the URL from the service environment.

    `set -u` therefore cannot abort on a variable only the container has, and
    the superuser password never reaches a host argv or the shell history.
    """
    assert forbidden not in _executable(), f"{forbidden!r} must not appear in compose-restore.sh's executable text: the one-off container inherits the URL from the service environment, so the operator's shell needs no such variable and no superuser password reaches a host argv or the shell history"


@pytest.mark.parametrize("relative_path", ["backend/entrypoint.sh", "deploy/supervisor/entrypoint.sh"])
def test_entrypoints_print_the_shipped_helper_path(relative_path: str) -> None:
    text = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "./compose-restore.sh --confirm" in text
    assert "scripts/compose-restore.sh" not in text, f"{relative_path} must not print a path the shipped bundle does not contain"


def test_supervisor_snapshot_is_gated_on_the_snapshot_dir() -> None:
    """A non-compose all-in-one boot without a snapshot dir must not require
    DATABASE_ADMIN_URL on every (pristine / already-at-head) boot."""
    text = (_REPO_ROOT / "deploy" / "supervisor" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'if [ -n "${FARNA_CONTROL_SNAPSHOT_DIR:-}" ]' in text
    assert "${FARNA_CONTROL_SNAPSHOT_DIR:-/backups}" not in text


class TestTheInstallerCannotBypassThePendingCheck:
    """MAJOR 7: ``install.sh`` is the caller that dumped unconditionally."""

    def _install_text(self) -> str:
        return (_REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    def test_the_dump_is_told_which_bundle_it_is_upgrading_to(self) -> None:
        """The helper runs the OLD install, so it must be told the NEW bundle."""
        assert '--target-bundle "${bundle_dir}"' in self._install_text()

    def test_an_empty_snapshot_path_is_reported_as_a_skip(self) -> None:
        text = self._install_text()
        assert 'if [[ -z "${snapshot_path}" ]]; then' in text
        assert "No migration is pending against the new bundle" in text


class TestTheFlyBootCannotTakeTheSnapshot:
    """FAR-2667: Fly is the EXPLICITLY unsupported path, not a dead opt-in.

    ``deploy/fly/release.sh`` is the single migrator and runs before any
    machine boots, so by the time ``deploy/fly/entrypoint.sh`` runs there is no
    pending migration and a snapshot step there could never fire. The boot used
    to carry a ``FARNA_CONTROL_SNAPSHOT_DIR`` opt-in that the docs described as
    working; it could not, so it is gone and the docs say Fly is unsupported.
    """

    def test_the_fly_boot_carries_no_snapshot_step_or_opt_in(self) -> None:
        text = (_REPO_ROOT / "deploy" / "fly" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "farna.launcher.upgrade" not in text
        assert "FARNA_CONTROL_SNAPSHOT_DIR" not in text

    def test_the_release_stage_is_the_migrator_that_makes_the_boot_too_late(self) -> None:
        text = (_REPO_ROOT / "deploy" / "fly" / "release.sh").read_text(encoding="utf-8")
        assert "alembic upgrade heads" in text
        assert "farna.db.alignment" in text
