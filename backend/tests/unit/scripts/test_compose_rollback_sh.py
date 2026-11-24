"""Execute ``scripts/compose-rollback.sh``'s service derivation for real (FAR-2667).

``compose-rollback.sh`` is the ONE documented way back to the previous image for
the compose path, and its central safety property is that it never guesses which
service to start: it asks compose for the resolved configuration and requires
exactly one service whose image is a Farna Control release. The sibling packaged
tests (``test_package_compose_release.py``) read the helper's heredoc but
substitute a literal for ``${app_service}`` before parsing, so they stay green
when the derivation is deleted or its ``awk`` stops matching after a ``docker
compose config`` output change. A helper that then exits 2 mid-incident would do
so with CI green - the failure mode this repo has already shipped twice for this
helper family (see the docstring in ``test_compose_restore_wiring.py``).

These tests therefore RUN the shipped helper under a real POSIX shell with a
stubbed ``docker`` first on PATH. The stub answers ``compose ... config`` with a
canned resolved document, records every invocation, and makes ``up`` a no-op
that captures the merged override file, so no daemon is ever touched.

Each case is chosen so deleting the derivation fails it:

* zero matches must exit 2 with the actionable message (a literal would proceed);
* two matches must exit 2 naming the count (a literal would proceed);
* the one-match cases name the app service something the helper cannot guess, so
  a pinned literal fails both stdout and the ``up -d`` argument;
* ``FARNA_CONTROL_APP_SERVICE`` must be honoured AND must skip the derivation
  entirely, proven by the docker log containing no ``config`` invocation.

The pinned compose file and the bundle's ``SHA256SUMS`` entry are asserted
unchanged after the run: that is what makes the one-shot override safe.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "compose-rollback.sh").is_file():
            return parent
    raise RuntimeError("Could not find repo root (scripts/compose-rollback.sh)")


REPO_ROOT = _repo_root()
HELPER = REPO_ROOT / "scripts" / "compose-rollback.sh"
REPO_COMPOSE = REPO_ROOT / "deploy" / "compose" / "docker-compose.prod.yml"

#: The packager lives beside the helper; reuse it so the packaged-layout case
#: runs against a genuinely packaged bundle rather than a hand-built mirror.
_SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from package_compose_release import build_compose_release  # noqa: E402

#: Every Farna Control release image, per the compose the helper inspects.
IMAGE_PREFIX = "ghcr.io/farnalabs/farna-control:"

#: The service key the packaged bundle's compose declares.
PACKAGED_APP_SERVICE = "farna-control"

#: This module executes a shipped script under a real POSIX shell with stubbed
#: binaries first on PATH, so it needs executable-bit semantics and a POSIX
#: PATH. Declared once as a marker; ``backend/tests/conftest.py`` is the single
#: platform gate that skips it on Windows. Linux CI runs it for real.
pytestmark = pytest.mark.posix_only

DOCKER_STUB = """\
#!/usr/bin/env bash
# Test stub for compose-rollback.sh: never touches a container daemon.
# `config` prints the canned resolved configuration; `up` records the last -f
# (the helper's one-shot override, which it deletes on exit) and succeeds.
set -u

printf '%s\\n' "$*" >> "${STUB_LOG}"

sub=""
last_f=""
prev=""
for a in "$@"; do
  if [ "${prev}" = "-f" ]; then
    last_f="$a"
  fi
  if [ -z "${sub}" ]; then
    case "$a" in
      config|up) sub="$a" ;;
      *) ;;
    esac
  fi
  prev="$a"
done

case "${sub}" in
  config)
    cat "${STUB_CONFIG}"
    exit 0
    ;;
  up)
    if [ -n "${last_f}" ]; then
      cat "${last_f}" > "${STUB_OVERRIDE_CAPTURE}"
    fi
    exit 0
    ;;
  *)
    printf 'unexpected docker invocation: %s\\n' "$*" >&2
    exit 3
    ;;
esac
"""


def _resolved_config(services: dict[str, str]) -> str:
    """A ``docker compose config`` document: resolved, flat, no ``${}`` left.

    The trailing top-level keys are deliberate - they are what ends the
    ``services:`` section the helper's ``awk`` walks.
    """
    lines = ["name: farna-control", "services:"]
    for name, image in services.items():
        lines.append(f"  {name}:")
        lines.append(f"    image: {image}")
    lines.extend(
        [
            "networks:",
            "  default:",
            "    name: farna-control_default",
            "volumes:",
            "  postgres_data:",
        ]
    )
    return "\n".join(lines) + "\n"


def _make_env(tmp_path: Path, config_text: str, *, app_service: str | None = None) -> dict[str, str]:
    """A clean, minimal environment: no inherited compose file or service name."""
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    stub = stub_dir / "docker"
    # LF regardless of host: the shebang is the first thing the shell reads.
    stub.write_text(DOCKER_STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)

    config_path = tmp_path / "resolved-compose-config.yml"
    config_path.write_text(config_text, encoding="utf-8")

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    env = {
        "PATH": f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_dir),
        "STUB_LOG": str(tmp_path / "docker.log"),
        "STUB_CONFIG": str(config_path),
        "STUB_OVERRIDE_CAPTURE": str(tmp_path / "captured-override.yml"),
    }
    if app_service is not None:
        env["FARNA_CONTROL_APP_SERVICE"] = app_service
    return env


def _run_helper(helper: Path, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, repo script under test
        ["sh", str(helper), *args],  # noqa: S607
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _invocations(tmp_path: Path) -> list[str]:
    log = tmp_path / "docker.log"
    return log.read_text(encoding="utf-8").splitlines() if log.is_file() else []


def _ups(tmp_path: Path) -> list[str]:
    return [line for line in _invocations(tmp_path) if " up -d " in line]


def _captured_override(tmp_path: Path) -> dict:
    capture = tmp_path / "captured-override.yml"
    if not capture.is_file():
        raise AssertionError("the helper must run `docker compose ... up -d` with its override merged in")
    parsed = yaml.safe_load(capture.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"the captured override is not a YAML mapping: {parsed!r}")
    return parsed


def _sums_digest(sums_bytes: bytes, filename: str) -> str:
    for line in sums_bytes.decode("utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if separator and name == filename:
            return digest
    raise AssertionError(f"SHA256SUMS carries no entry for {filename}")


def _extract_bundle(tmp_path: Path) -> Path:
    """Build and unpack a real release bundle: helper beside a pinned compose."""
    tarball = build_compose_release(version="0.4.0", output_dir=tmp_path / "dist")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    with tarfile.open(tarball, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(bundle, filter="data")
        else:
            tar.extractall(bundle)  # noqa: S202
    return bundle


def test_packaged_bundle_derives_the_service_and_leaves_the_verified_files_intact(tmp_path: Path) -> None:
    """The shipped layout: helper beside a literal-pinned compose and SHA256SUMS.

    The packaged compose carries a literal image tag so the artifact can be
    checksummed, so the helper must derive the service from this run's resolved
    configuration and must leave both shipped files byte-identical.
    """
    bundle = _extract_bundle(tmp_path)
    compose_path = bundle / "docker-compose.prod.yml"
    sums_path = bundle / "SHA256SUMS"

    packaged = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    pinned_image = packaged["services"][PACKAGED_APP_SERVICE]["image"]
    assert pinned_image == f"{IMAGE_PREFIX}v0.4.0"

    compose_digest_before = hashlib.sha256(compose_path.read_bytes()).hexdigest()
    sums_bytes_before = sums_path.read_bytes()
    # The packaged artifact's own integrity claim, before the helper runs.
    assert _sums_digest(sums_bytes_before, "docker-compose.prod.yml") == compose_digest_before

    env = _make_env(
        tmp_path,
        _resolved_config(
            {
                PACKAGED_APP_SERVICE: pinned_image,
                "postgres": "postgres:16-alpine",
                "redis": "redis:8-alpine",
            }
        ),
    )
    proc = _run_helper(bundle / "compose-rollback.sh", bundle, env, "v0.3.0")

    assert proc.returncode == 0, proc.stderr
    assert f"Starting the previous release: {PACKAGED_APP_SERVICE} from {IMAGE_PREFIX}v0.3.0" in proc.stdout
    assert _captured_override(tmp_path) == {"services": {PACKAGED_APP_SERVICE: {"image": f"{IMAGE_PREFIX}v0.3.0"}}}
    ups = _ups(tmp_path)
    assert len(ups) == 1
    assert ups[0].endswith(f"up -d {PACKAGED_APP_SERVICE}")

    # The one-shot override is only safe because the verified artifact is left
    # exactly as packaged, and still verifies against its own checksum.
    assert hashlib.sha256(compose_path.read_bytes()).hexdigest() == compose_digest_before
    assert sums_path.read_bytes() == sums_bytes_before
    assert _sums_digest(sums_path.read_bytes(), "docker-compose.prod.yml") == compose_digest_before


def test_repo_layout_starts_the_service_derived_from_the_resolved_config(tmp_path: Path) -> None:
    """The derived name - not a literal - reaches stdout and ``up -d``.

    The resolved configuration deliberately names the app service something the
    helper cannot have baked in, so replacing the derivation with a literal
    fails both the printed line and the ``up -d`` argument.
    """
    derived = "farna-control-api"
    repo_digest_before = hashlib.sha256(REPO_COMPOSE.read_bytes()).hexdigest()
    env = _make_env(
        tmp_path,
        _resolved_config(
            {
                derived: f"{IMAGE_PREFIX}v0.4.0",
                "postgres": "postgres:16-alpine",
                "redis": "redis:8-alpine",
                "watchdog": "farna-control-watchdog:local",
            }
        ),
    )

    proc = _run_helper(HELPER, REPO_ROOT, env, "v0.3.0")

    assert proc.returncode == 0, proc.stderr
    assert f"Starting the previous release: {derived} from {IMAGE_PREFIX}v0.3.0" in proc.stdout
    assert _captured_override(tmp_path) == {"services": {derived: {"image": f"{IMAGE_PREFIX}v0.3.0"}}}
    ups = _ups(tmp_path)
    assert len(ups) == 1
    assert ups[0].endswith(f"up -d {derived}")
    # The repo's compose file is the pinned artifact here too: never edited.
    assert hashlib.sha256(REPO_COMPOSE.read_bytes()).hexdigest() == repo_digest_before


def test_no_matching_service_exits_2_with_an_actionable_message(tmp_path: Path) -> None:
    """Nothing to derive is refused, never guessed - and the message says why."""
    env = _make_env(tmp_path, _resolved_config({"postgres": "postgres:16-alpine", "redis": "redis:8-alpine"}))

    proc = _run_helper(HELPER, REPO_ROOT, env, "v0.3.0")

    assert proc.returncode == 2
    assert "cannot identify the Farna Control service" in proc.stderr
    assert "0 image match(es)" in proc.stderr
    assert "deploy/compose/docker-compose.prod.yml" in proc.stderr
    assert "FARNA_CONTROL_APP_SERVICE" in proc.stderr
    assert not _ups(tmp_path), "a refused derivation must never start anything"


def test_two_matching_services_exit_2_naming_the_count(tmp_path: Path) -> None:
    """Ambiguity is refused too: two release images cannot be told apart."""
    env = _make_env(
        tmp_path,
        _resolved_config(
            {
                "farna-control-api": f"{IMAGE_PREFIX}v0.4.0",
                "farna-control-worker": f"{IMAGE_PREFIX}v0.4.0",
                "postgres": "postgres:16-alpine",
            }
        ),
    )

    proc = _run_helper(HELPER, REPO_ROOT, env, "v0.3.0")

    assert proc.returncode == 2
    assert "cannot identify the Farna Control service" in proc.stderr
    assert "2 image match(es)" in proc.stderr
    assert not _ups(tmp_path), "an ambiguous derivation must never start anything"


def test_app_service_override_skips_derivation_entirely(tmp_path: Path) -> None:
    """A customised bundle is honoured without asking compose to resolve at all.

    The canned configuration matches nothing, so any run of the derivation would
    refuse with exit 2; the docker log additionally proves ``compose config``
    was never invoked.
    """
    env = _make_env(tmp_path, _resolved_config({"postgres": "postgres:16-alpine"}), app_service="custom-api")

    proc = _run_helper(HELPER, REPO_ROOT, env, "v0.3.0")

    assert proc.returncode == 0, proc.stderr
    assert f"Starting the previous release: custom-api from {IMAGE_PREFIX}v0.3.0" in proc.stdout
    assert _captured_override(tmp_path) == {"services": {"custom-api": {"image": f"{IMAGE_PREFIX}v0.3.0"}}}
    ups = _ups(tmp_path)
    assert len(ups) == 1
    assert ups[0].endswith("up -d custom-api")
    invocations = _invocations(tmp_path)
    assert len(invocations) == 1, "the override must skip `docker compose config` entirely"
