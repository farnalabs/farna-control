"""FAR-2667 review round: the shipped recovery helper cannot lie about itself.

The independent verification pass found two CRITICALS in
``scripts/compose-restore.sh``, and both were invisible to every Python test:

* the ``docker compose run`` calls did not override the image ENTRYPOINT, and
  the all-in-one image's ``/entrypoint.sh`` ignores its arguments, re-takes a
  snapshot, runs ``alembic upgrade heads`` and execs supervisord. The
  "read-only verification" step therefore MIGRATED the live database and hung,
  and the restore restored nothing. The bare token ``restore`` is not an
  installed command either — only ``farna-control`` is.
* ``$DATABASE_ADMIN_URL`` is not bound in the operator's shell (it exists only
  as the service's ``environment:`` entry), so ``set -u`` aborted every
  invocation before anything was verified. Exporting it to work around that
  put the superuser password on a host command line — the exact exposure the
  dump path works to avoid.

These tests read the SHIPPED script, so a future edit that reintroduces either
defect fails here without needing Docker.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

for _parent in Path(__file__).resolve().parents:
    _COMPOSE_RESTORE = _parent / "scripts" / "compose-restore.sh"
    if _COMPOSE_RESTORE.exists():
        _REPO_ROOT = _parent
        break
else:
    raise RuntimeError("Could not find repo root (scripts/compose-restore.sh)")


def _text() -> str:
    return _COMPOSE_RESTORE.read_text(encoding="utf-8")


def _executable() -> str:
    """The script's non-comment, non-blank lines.

    The explanatory comments name the very things the code must not do (they
    document ``--entrypoint`` and the environment variable the container
    receives), so only executable text can evidence a defect.
    """
    return "\n".join(line for line in _text().splitlines() if line.strip() and not line.lstrip().startswith("#"))


class TestTheOneOffContainerRunsTheCli:
    def test_every_one_off_run_overrides_the_image_entrypoint(self) -> None:
        """CRITICAL 1: /entrypoint.sh ignores its arguments and migrates."""
        executable = _executable()
        runs = executable.count("compose run")
        overrides = executable.count("--entrypoint")
        assert runs >= 3, "the helper runs presence, dry-run, target and restore steps"
        assert overrides == runs, "EVERY `compose run` must override the image ENTRYPOINT; without --entrypoint the all-in-one image's /entrypoint.sh ignores the arguments, migrates the live database and execs supervisord"

    def test_the_restore_steps_run_the_installed_cli(self) -> None:
        executable = _executable()
        assert executable.count("--entrypoint farna-control") >= 3
        # `restore` alone is not an installed command; it must reach the CLI as
        # a SUBCOMMAND after an explicit --entrypoint.
        for invocation in re.findall(r"compose run[^\n]*--entrypoint (\S+)", executable):
            assert invocation in {"farna-control", "sh"}, invocation

    def test_the_verification_steps_run_the_documented_flags(self) -> None:
        executable = _executable()
        assert 'restore "$container_snapshot" --dry-run' in executable
        assert "--verify-target-schema" in executable
        assert "--safety-dump-dir /backups" in executable
        assert "--no-deps" in executable, "the one-off container must never restart the database"


class TestNoCredentialOnTheHostCommandLine:
    """CRITICAL 2: the script must not read or pass the URL at all."""

    @pytest.mark.parametrize(
        "forbidden",
        ["DATABASE_ADMIN_URL", "DATABASE_URL", "DATABASE_", "--db-url", "PGPASSWORD", "PGPASSFILE", "postgres://", "postgresql://"],
    )
    def test_the_script_never_references_a_connection_url_or_credential(self, forbidden: str) -> None:
        assert forbidden not in _executable(), (
            f"{forbidden!r} must not appear in compose-restore.sh's executable text: the one-off container inherits the URL from the service environment, so the operator's shell needs no such variable (set -u cannot abort) and no superuser password reaches a host argv or the shell history"
        )

    def test_the_cli_is_left_to_default_to_the_container_environment(self) -> None:
        """The restore invocations pass only the snapshot and the flags."""
        executable = _executable()
        assert "--db-url" not in executable
        assert "$app_service" in executable


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


class TestTheFlyEntrypointTakesTheSnapshotThroughTheSameHelper:
    """MAJOR 8: no upgrade path may silently skip the recovery point."""

    def test_the_fly_boot_uses_the_shared_helper(self) -> None:
        text = (_REPO_ROOT / "deploy" / "fly" / "entrypoint.sh").read_text(encoding="utf-8")
        assert "python3 -m farna.launcher.upgrade" in text
        # Gated on the operator-provided snapshot directory: Fly has no
        # snapshot volume by default, and the docs say so.
        assert "FARNA_CONTROL_SNAPSHOT_DIR" in text
        assert "cannot dump as the owner role" in text
