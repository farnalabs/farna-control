"""FAR-2589 — ``scripts/install.sh`` must not depend on its working directory.

Run from a directory the user cannot search (``sudo -u <user>`` from another
user's 0700 home is the common shape), the installer's extract stage
reported::

    find: Failed to restore initial working directory: Permission denied

because GNU ``find`` saves and restores its starting directory. Measured with
findutils 4.8 (Ubuntu 22.04) and 4.9 (Debian 12): a cwd of mode 0000 (or
another user's 0700 directory) triggers it; an enter-only 0311/0111 cwd does
NOT. The fix records the invocation directory, anchors every caller-supplied
relative path at it, and moves to ``/`` before doing any work.

This suite drives the REAL ``--from-file`` path (a genuine tarball, a signed
manifest checked against a test trust key substituted into a temporary copy of
the installer):

* from a mode-0000 cwd (absolute tarball path) - the failing field shape;
* the same with the ``cd /`` line removed - a negative control proving the
  suite reproduces the historical find error rather than pinning today's
  bytes;
* from an enter-only cwd with a RELATIVE ``../<tarball>`` path - pinning that
  relative ``--from-file`` still resolves against where the installer was run.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.helpers.env_prefixes import retired_env_name

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"

pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.skipif(
        platform.system() != "Linux",
        reason="scripts/install.sh installs Linux only; on Darwin it refuses in preflight by design",
    ),
]

_VERSION_NUM = "1.2.3"
_RELEASE_TAG = f"bundle-v{_VERSION_NUM}"
_TRUST_KEY_ID = "modulo-2026-a"
_TRUST_KEY_LINE = re.compile(r'^TRUST_KEY_CURRENT_B64="(?P<key>[^"]*)"$', re.MULTILINE)
_LEAVE_CWD_LINE = "\ncd /\n"
_FIND_CWD_ERROR = "Failed to restore initial working directory"
_ENV_KEYS_TO_DROP = (
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "VERSION",
    "SECRET_KEY",
    "FERNET_KEY",
    "FARNA_CONTROL_INSTALL_ROOT",
    "FARNA_CONTROL_BIN_DIR",
    "FARNA_CONTROL_DATA_DIR",
    "FARNA_CONTROL_LEGACY_SHIM_PATH",
    "FARNA_CONTROL_LEGACY_INSTALL_ROOT",
    "FARNA_CONTROL_LEGACY_USER_SHIM",
)


def _bash() -> str:
    resolved = shutil.which("bash")
    assert resolved is not None, "the installer is a bash script; bash must be on PATH to test it"
    return resolved


def _require_preconditions() -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads any directory, so an unreadable cwd cannot be reproduced as root")
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is not installed; install.sh requires OpenSSL 3.0+ at install time")
    version = subprocess.run(  # noqa: S603 - resolved absolute path to the system openssl
        [openssl, "version"], capture_output=True, text=True, check=True, timeout=30
    )
    tokens = version.stdout.split()
    assert "OpenSSL" in tokens, f"unexpected `openssl version` output: {version.stdout!r}"
    if int(tokens[tokens.index("OpenSSL") + 1].split(".")[0]) < 3:
        pytest.skip("install.sh requires OpenSSL 3.0+")


def _host_arch() -> str:
    machine = platform.machine().lower()
    resolved = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    assert resolved is not None, f"unsupported test host architecture: {machine}"
    return resolved


def _add_file(archive: tarfile.TarFile, name: str, payload: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = mode
    archive.addfile(info, io.BytesIO(payload))


def _signed_tarball(artifacts: Path, trust_key: Ed25519PrivateKey) -> Path:
    """A real ``farna-control-<v>-linux-<arch>.tar.gz`` plus its signed manifest."""
    artifacts.mkdir(parents=True, exist_ok=True)
    tarball = artifacts / f"farna-control-{_VERSION_NUM}-linux-{_host_arch()}.tar.gz"
    top = tarball.name.removesuffix(".tar.gz")
    files = {"VERSION": (f"{_RELEASE_TAG}\n".encode(), 0o644), "launcher": (b"#!/bin/sh\nexit 0\n", 0o755)}
    sums = "".join(f"{hashlib.sha256(payload).hexdigest()}  ./{name}\n" for name, (payload, _mode) in files.items())
    files["SHA256SUMS"] = (sums.encode(), 0o644)
    with tarfile.open(tarball, "w:gz") as archive:
        directory = tarfile.TarInfo(top)
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        for name, (payload, mode) in files.items():
            _add_file(archive, f"{top}/{name}", payload, mode)

    manifest = json.dumps(
        {
            "manifest_version": 1,
            "release": _RELEASE_TAG,
            "artifacts": [{"name": tarball.name, "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest()}],
        }
    ).encode()
    (artifacts / "RELEASE_MANIFEST.json").write_bytes(manifest)
    signature = {"key_id": _TRUST_KEY_ID, "signature": trust_key.sign(manifest).hex()}
    (artifacts / "RELEASE_MANIFEST.json.sig").write_text(json.dumps(signature), encoding="utf-8")
    return tarball


def _installer(tmp_path: Path, trust_key: Ed25519PrivateKey, *, leave_cwd: bool = True) -> Path:
    """The shipped installer with the test trust key (and optionally without the fix)."""
    source = _INSTALL_SH.read_text(encoding="utf-8")
    match = _TRUST_KEY_LINE.search(source)
    assert match is not None, "install.sh must declare TRUST_KEY_CURRENT_B64 on its own line"
    public_b64 = base64.b64encode(trust_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
    patched = source[: match.start("key")] + public_b64 + source[match.end("key") :]
    if not leave_cwd:
        assert patched.count(_LEAVE_CWD_LINE) == 1, "install.sh no longer leaves its cwd with a bare `cd /` line"
        patched = patched.replace(_LEAVE_CWD_LINE, "\n", 1)
    script = tmp_path / ("install.sh" if leave_cwd else "install-without-fix.sh")
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)
    return script


def _install_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS_TO_DROP}
    env.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            "FARNA_CONTROL_LEGACY_SHIM_PATH": str(tmp_path / "no-legacy-shim"),
        }
    )
    return env


def _invocation_dir(tmp_path: Path) -> Path:
    """The directory the installer is started from (pre-create it to seed files)."""
    return tmp_path / "artifacts" / "locked"


def _run_install(
    tmp_path: Path,
    *,
    cwd_mode: int,
    relative: bool,
    leave_cwd: bool = True,
    env_overrides: dict[str, str | None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the installer from ``artifacts/locked`` after chmod-ing it to ``cwd_mode``.

    The wrapper enters the directory BEFORE dropping its permissions (a cwd of
    mode 0000 cannot be entered afterwards), then execs the installer, which
    inherits that cwd exactly as a `sudo -u` from a private directory does.
    ``env_overrides`` sets (or, with ``None``, removes) environment variables.
    """
    _require_preconditions()
    trust_key = Ed25519PrivateKey.generate()
    tarball = _signed_tarball(tmp_path / "artifacts", trust_key)
    script = _installer(tmp_path, trust_key, leave_cwd=leave_cwd)
    env = _install_env(tmp_path)
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    locked = _invocation_dir(tmp_path)
    locked.mkdir(exist_ok=True)
    tarball_arg = f"../{tarball.name}" if relative else str(tarball)
    wrapper = 'cd -- "$1" && chmod "$2" . && shift 2 && exec "$@"'
    try:
        result = subprocess.run(  # noqa: S603 - resolved bash and test-owned paths
            [_bash(), "-c", wrapper, "wrapper", str(locked), f"{cwd_mode:04o}", _bash(), str(script), "--from-file", tarball_arg],
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
    finally:
        locked.chmod(0o755)
    return result, env


def _diagnose(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def _assert_installed(result: subprocess.CompletedProcess[str], env: dict[str, str]) -> None:
    assert result.returncode == 0, _diagnose(result)
    assert _FIND_CWD_ERROR not in result.stderr
    shim = Path(env["HOME"]) / ".local" / "bin" / "farna-control"
    assert shim.is_file(), _diagnose(result)
    current = Path(env["HOME"]) / ".local" / "opt" / "farna-control" / "current"
    assert (current / "VERSION").read_text(encoding="utf-8") == f"{_RELEASE_TAG}\n"


def test_install_from_a_cwd_without_search_permission(tmp_path: Path) -> None:
    result, env = _run_install(tmp_path, cwd_mode=0o000, relative=False)

    _assert_installed(result, env)


def test_negative_control_the_unfixed_installer_hits_the_find_error_from_that_cwd(tmp_path: Path) -> None:
    """Without the `cd /`, the same install emits the field-reported find error.

    Only the message is pinned: the extract-stage find feeds a process
    substitution, whose exit status bash ignores, so this minimal harness
    still completes the install after the error (observed on Debian 12).
    """
    result, _env = _run_install(tmp_path, cwd_mode=0o000, relative=False, leave_cwd=False)

    assert _FIND_CWD_ERROR in result.stderr, _diagnose(result)


def test_relative_from_file_resolves_against_the_invocation_directory(tmp_path: Path) -> None:
    """An enter-only cwd keeps `..` resolvable; the relative tarball must still install."""
    result, env = _run_install(tmp_path, cwd_mode=0o311, relative=True)

    _assert_installed(result, env)


# --- every path override is anchored at the invocation directory -------------


def test_relative_data_dir_override_is_anchored_at_the_invocation_directory(tmp_path: Path) -> None:
    """The installer writes config.env into the data dir AFTER leaving its cwd,
    so a relative override must still land beside where it was run, not under /."""
    overrides = {"FARNA_CONTROL_DATA_DIR": "relative-data"}
    result, env = _run_install(tmp_path, cwd_mode=0o755, relative=False, env_overrides=overrides)

    _assert_installed(result, env)
    assert (_invocation_dir(tmp_path) / "relative-data" / "config.env").is_file(), _diagnose(result)


def test_relative_legacy_shim_override_is_anchored_at_the_invocation_directory(tmp_path: Path) -> None:
    """An unmanaged legacy shim named by a relative override must still be found
    (and refused without --force); resolved against / it would be missed."""
    overrides = {"FARNA_CONTROL_LEGACY_SHIM_PATH": "relative-shim/modulo"}
    shim = _invocation_dir(tmp_path) / "relative-shim" / "modulo"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result, _env = _run_install(tmp_path, cwd_mode=0o755, relative=False, env_overrides=overrides)

    assert result.returncode != 0, _diagnose(result)
    assert f"found an existing {shim} that this installer did not create" in result.stderr, _diagnose(result)
    assert shim.is_file()


def test_a_stale_legacy_data_dir_name_is_ignored(tmp_path: Path) -> None:
    """A pre-rebrand override name is not honoured: the cutover has one spelling."""
    legacy = retired_env_name("DATA_DIR")
    result, env = _run_install(tmp_path, cwd_mode=0o755, relative=False, env_overrides={legacy: "relative-data"})

    _assert_installed(result, env)
    assert not (_invocation_dir(tmp_path) / "relative-data").exists(), _diagnose(result)


def test_a_stale_legacy_shim_path_name_is_ignored(tmp_path: Path) -> None:
    """A pre-rebrand shim-path override cannot make the installer refuse a shim."""
    legacy = retired_env_name("LEGACY_SHIM_PATH")
    shim = _invocation_dir(tmp_path) / "relative-shim" / "modulo"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    result, env = _run_install(tmp_path, cwd_mode=0o755, relative=False, env_overrides={legacy: "relative-shim/modulo"})

    _assert_installed(result, env)


#: An assignment whose value comes straight from a path-override variable. Every
#: such override must go through absolute_from_invocation instead. (HOME and
#: TMPDIR are anchored once, before the installer leaves its cwd, so values
#: derived from them are already absolute.)
_UNANCHORED_OVERRIDE = re.compile(
    r"""^\s*(?:local\s+)?[A-Za-z_][A-Za-z0-9_]*="\$\{(?:FARNA_CONTROL_[A-Z0-9_]*(?:DIR|ROOT|SHIM|PATH)|XDG_[A-Z]+_HOME)\b""",
    re.MULTILINE,
)


def test_install_sh_anchors_every_path_override() -> None:
    """Static guard for the class: a NEW raw override would resolve against /."""
    offenders = [line.strip() for line in _UNANCHORED_OVERRIDE.findall(_INSTALL_SH.read_text(encoding="utf-8"))]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "historical",
    [
        '  local legacy_shim="${FARNA_CONTROL_LEGACY_SHIM_PATH:-/usr/local/bin/modulo}"',
        '  DATA_DIR="${FARNA_CONTROL_DATA_DIR}"',
        '  STATE_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/farna-control"',
    ],
)
def test_unanchored_override_guard_flags_the_reviewed_lines(historical: str) -> None:
    """Negative control: the guard catches the exact lines the review flagged."""
    assert _UNANCHORED_OVERRIDE.findall(historical)
