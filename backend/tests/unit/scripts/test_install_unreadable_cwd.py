"""FAR-2589 — ``scripts/install.sh`` must not depend on its working directory.

Run from a directory the user can enter but not read (a common shape for a
shared or locked-down parent directory), the installer aborted in the extract
stage::

    find: Failed to restore initial working directory: Permission denied

because GNU ``find`` opens ``.`` to restore it afterwards. The fix records the
invocation directory, resolves every caller-supplied relative path against it,
and moves to ``/`` before doing any work.

This suite drives the REAL ``--from-file`` path (a genuine tarball, a signed
manifest checked against a test trust key substituted into a temporary copy of
the installer) from an unreadable cwd, passing the tarball as a RELATIVE path
so the "relative --from-file still resolves against where you ran it" contract
is pinned too. A negative control re-runs the install with the ``cd /`` line
removed and asserts the historical failure comes back, so the suite detects the
class rather than pinning today's bytes.
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
    "MODULO_INSTALL_ROOT",
    "MODULO_BIN_DIR",
    "MODULO_DATA_DIR",
    "MODULO_LEGACY_SHIM_PATH",
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
    artifacts.mkdir(parents=True)
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


def _run_from_unreadable_cwd(tmp_path: Path, *, leave_cwd: bool) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    _require_preconditions()
    trust_key = Ed25519PrivateKey.generate()
    tarball = _signed_tarball(tmp_path / "artifacts", trust_key)
    script = _installer(tmp_path, trust_key, leave_cwd=leave_cwd)
    env = _install_env(tmp_path)
    locked = tmp_path / "artifacts" / "locked"
    locked.mkdir()
    # Enter (x) but not list (r): exactly the cwd the field report ran from.
    locked.chmod(0o311)
    try:
        result = subprocess.run(  # noqa: S603 - resolved bash and test-owned paths
            [_bash(), str(script), "--from-file", f"../{tarball.name}"],
            cwd=locked,
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


def test_install_from_an_unreadable_cwd_with_a_relative_from_file(tmp_path: Path) -> None:
    result, env = _run_from_unreadable_cwd(tmp_path, leave_cwd=True)

    assert result.returncode == 0, _diagnose(result)
    assert _FIND_CWD_ERROR not in result.stderr
    shim = Path(env["HOME"]) / ".local" / "bin" / "farna-control"
    assert shim.is_file(), _diagnose(result)
    current = Path(env["HOME"]) / ".local" / "opt" / "farna-control" / "current"
    assert (current / "VERSION").read_text(encoding="utf-8") == f"{_RELEASE_TAG}\n"


def test_negative_control_the_unfixed_installer_fails_from_an_unreadable_cwd(tmp_path: Path) -> None:
    """Without the `cd /`, the same install reproduces the field failure."""
    result, _env = _run_from_unreadable_cwd(tmp_path, leave_cwd=False)

    assert result.returncode != 0, _diagnose(result)
    assert _FIND_CWD_ERROR in result.stderr, _diagnose(result)
