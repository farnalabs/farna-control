"""FAR-2569 — the installer's generated activation file (env.sh) is EXECUTED.

``scripts/install.sh`` writes ``<install-root>/env.sh`` from an UNQUOTED
heredoc, so every ``${...}`` in that body expands at INSTALL time. The
published ``bundle-v0.3.328`` linux-amd64 artifact aborted in the finalise
stage on a clean Ubuntu 22.04 (glibc 2.35) install with::

    scripts/install.sh: line 1081: FARNA_CONTROL_INSTALL_ROOT: unbound variable

because two body lines named variables that do not exist at install time — they
were meant as literal text for the generated file — so ``set -u`` killed the
installer AFTER ``current`` had been re-pointed. The PATH shim and ``env.sh``
were never written, leaving an unusable install.

The pre-existing text assertion could not catch it: it pinned the INSTALLER
SOURCE and asserted those buggy lines verbatim, so the defect was encoded as
the expected value. Text matching cannot distinguish "the script contains this
line" from "the generated file is correct"; only running the finalise stage
can. Hence this suite drives the REAL installer end to end (offline
``--from-bundle``, with a test-generated ed25519 trust key substituted into a
temporary copy of the script) and then:

* asserts the generated ``env.sh`` carries the RESOLVED install paths and no
  unexpanded install-time names;
* SOURCES the generated file under ``set -euo pipefail`` and asserts the
  exported values and PATH — including that PATH gains no empty component (an
  empty PATH entry means "current directory", which the escaped-name bug also
  produced);
* re-runs the same install with the historical defect restored and asserts the
  finalise stage fails, proving this gate detects the class rather than
  pinning today's bytes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_REPO_ROOT = Path(__file__).resolve().parents[4]
_INSTALL_SH = _REPO_ROOT / "scripts" / "install.sh"

#: This module executes the shipped bash installer, so it needs a real POSIX
#: shell (``backend/tests/conftest.py`` skips it on Windows, Linux CI runs it).
#: ``install.sh`` is the Linux installer and refuses Darwin in preflight by
#: design, so the suite also pins Linux rather than skipping at run time.
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
#: Host/installer-derived values that must not leak in from the machine running
#: the suite: an isolated install needs an isolated environment.
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
    "FARNA_CONTROL_INSTALL_ROOT",
    "FARNA_CONTROL_BIN_DIR",
    "FARNA_CONTROL_DATA_DIR",
    "FARNA_CONTROL_LEGACY_SHIM_PATH",
)


@dataclass(frozen=True)
class _Release:
    """A minimal, correctly signed offline release laid out the way the operator gets it."""

    bundle_dir: Path
    tarball_name: str


def _bash() -> str:
    resolved = shutil.which("bash")
    assert resolved is not None, "the installer is a bash script; bash must be on PATH to test it"
    return resolved


def _require_openssl_3() -> str:
    """install.sh verifies the manifest with ``pkeyutl -rawin`` (OpenSSL 3.0+)."""
    resolved = shutil.which("openssl")
    if resolved is None:
        pytest.skip("openssl is not installed; install.sh requires OpenSSL 3.0+ at install time")
    version = subprocess.run(  # noqa: S603 - resolved absolute path to the system openssl
        [resolved, "version"], capture_output=True, text=True, check=True, timeout=30
    )
    tokens = version.stdout.split()
    assert "OpenSSL" in tokens, f"unexpected `openssl version` output: {version.stdout!r}"
    major = int(tokens[tokens.index("OpenSSL") + 1].split(".")[0])
    if major < 3:
        pytest.skip(f"install.sh requires OpenSSL 3.0+; this host has {major}.x")
    return resolved


def _host_arch() -> str:
    machine = platform.machine().lower()
    resolved = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    assert resolved is not None, f"unsupported test host architecture: {machine}"
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signed_release(tmp_path: Path, *, trust_key: Ed25519PrivateKey) -> _Release:
    """Lay out ``--from-bundle``'s contract: bundle dir + adjacent signed tarball.

    ``install.sh`` requires the extracted bundle directory to be named after the
    archive (``farna-control-<version>-linux-<arch>``) and the signed manifest plus the
    tarball to sit BESIDE it. ``--from-bundle`` never extracts the tarball, so
    its bytes only need to match the manifest's sha256.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True)
    tarball_name = f"farna-control-{_VERSION_NUM}-linux-{_host_arch()}.tar.gz"
    tarball = artifacts / tarball_name
    tarball.write_bytes(b"placeholder-tarball-for-from-bundle-mode\n")

    bundle_dir = artifacts / tarball_name.removesuffix(".tar.gz")
    bundle_dir.mkdir()
    (bundle_dir / "VERSION").write_text(f"{_RELEASE_TAG}\n", encoding="utf-8")
    launcher = bundle_dir / "launcher"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    # The bundle's internal manifest lists every shipped file with a "./" prefix.
    sums = "".join(f"{_sha256(bundle_dir / name)}  ./{name}\n" for name in ("VERSION", "launcher"))
    (bundle_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")

    manifest_payload = json.dumps(
        {
            "manifest_version": 1,
            "release": _RELEASE_TAG,
            "artifacts": [{"name": tarball_name, "sha256": _sha256(tarball)}],
        }
    ).encode()
    (artifacts / "RELEASE_MANIFEST.json").write_bytes(manifest_payload)
    signature = {"key_id": _TRUST_KEY_ID, "signature": trust_key.sign(manifest_payload).hex()}
    (artifacts / "RELEASE_MANIFEST.json.sig").write_text(json.dumps(signature), encoding="utf-8")
    return _Release(bundle_dir=bundle_dir, tarball_name=tarball_name)


def _installer_with_test_trust_key(tmp_path: Path, *, trust_key: Ed25519PrivateKey) -> Path:
    """A copy of the shipped installer whose embedded CURRENT key is the test key.

    The production trust store is compiled into the script, so an offline
    install in a test can only pass by substituting a key it holds the private
    half of. Only that ONE line changes — the finalise heredoc under test is
    the shipped text, byte for byte.
    """
    source = _INSTALL_SH.read_text(encoding="utf-8")
    match = _TRUST_KEY_LINE.search(source)
    assert match is not None, "install.sh must declare TRUST_KEY_CURRENT_B64 on its own line"
    assert match.group("key"), "this release provisions no CURRENT trust key; the install could never verify"
    public_b64 = base64.b64encode(trust_key.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).decode()
    patched = source[: match.start("key")] + public_b64 + source[match.end("key") :]
    assert patched != source, "the test trust key replaced nothing — the installer's key line changed shape"
    script = tmp_path / "install-with-test-trust-key.sh"
    script.write_text(patched, encoding="utf-8")
    script.chmod(0o755)
    return script


def _install_env(tmp_path: Path) -> dict[str, str]:
    """An isolated HOME, install root, shim dir and data dir, all inside tmp.

    ``FARNA_CONTROL_INSTALL_ROOT`` / ``FARNA_CONTROL_BIN_DIR`` are deliberately
    NOT set: a real operator install leaves them unset and takes the defaults
    under ``$HOME``. Exporting them here would define the very names the
    historical heredoc referenced, masking the unbound-variable abort this
    suite exists to catch.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in _ENV_KEYS_TO_DROP}
    env.update(
        {
            "HOME": str(home),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
            # A real /usr/local/bin/modulo on the host must not gate this run.
            "FARNA_CONTROL_LEGACY_SHIM_PATH": str(tmp_path / "no-legacy-shim"),
        }
    )
    return env


def _default_install_root(env: dict[str, str]) -> Path:
    """Where an install with this environment lays itself out (install.sh's defaults)."""
    return Path(env["HOME"]) / ".local" / "opt" / "farna-control"


def _default_bin_dir(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "bin"


def _run_install(script: Path, release: _Release, env: dict[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - resolved bash and test-owned paths
        [_bash(), str(script), "--from-bundle", str(release.bundle_dir)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _diagnose(result: subprocess.CompletedProcess[str]) -> str:
    return f"exit {result.returncode}\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def test_finalise_writes_a_usable_env_sh_under_set_u(tmp_path: Path) -> None:
    """The shipped finalise stage must produce env.sh and must not abort under `set -u`."""
    _require_openssl_3()
    trust_key = Ed25519PrivateKey.generate()
    release = _signed_release(tmp_path, trust_key=trust_key)
    script = _installer_with_test_trust_key(tmp_path, trust_key=trust_key)
    env = _install_env(tmp_path)

    result = _run_install(script, release, env, tmp_path)
    assert result.returncode == 0, f"the installer failed before finalise could write env.sh:\n{_diagnose(result)}"

    install_root = _default_install_root(env)
    bin_dir = _default_bin_dir(env)
    activation = install_root / "env.sh"
    assert activation.is_file(), "finalise did not write the activation file"
    text = activation.read_text(encoding="utf-8")

    # The generated file carries RESOLVED paths, not install-time names.
    assert f'export FARNA_CONTROL_INSTALL_ROOT="{install_root}"' in text
    assert f'export FARNA_CONTROL_BIN_DIR="{bin_dir}"' in text
    assert f'export FARNA_CONTROL_INSTALL_ROOT="{install_root}"' in text
    assert f'export FARNA_CONTROL_BIN_DIR="{bin_dir}"' in text
    assert "${INSTALL_ROOT}" not in text
    assert "${BIN_DIR}" not in text
    assert "${FARNA_CONTROL_INSTALL_ROOT}" not in text
    assert "${FARNA_CONTROL_BIN_DIR}" not in text

    # The consuming shell's own PATH stays a literal reference (never expanded
    # at install time), and the bin dir IS resolved.
    assert 'case ":${PATH}:" in' in text
    assert f'*":{bin_dir}:"*)' in text
    assert f'export PATH="{bin_dir}:${{PATH}}"' in text

    # The shim keeps its argv passthrough (a `$@` eaten at install time would
    # make `farna-control <args>` silently drop every argument), and it states
    # the install root so `farna-control upgrade` resolves THIS install even
    # when it is not at the default root (FAR-2571).
    shim = (bin_dir / "farna-control").read_text(encoding="utf-8")
    assert f'exec "{install_root}/current/launcher" "$@"' in shim
    assert f'export FARNA_CONTROL_INSTALL_ROOT="{install_root}"' in shim

    # EXECUTE the generated file the way an operator does: a strict shell
    # (`set -euo pipefail`) must source it without an unbound-variable abort,
    # and the exported values must be the resolved paths.
    sourced = subprocess.run(  # noqa: S603 - resolved bash and a test-generated file
        [
            _bash(),
            "-c",
            'set -euo pipefail\nsource "$1"\nprintf "%s\\n" "${FARNA_CONTROL_INSTALL_ROOT}" "${FARNA_CONTROL_BIN_DIR}" "${FARNA_CONTROL_INSTALL_ROOT}" "${FARNA_CONTROL_BIN_DIR}" "${PATH}"',
            "env-sh-harness",
            str(activation),
        ],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": env["HOME"]},
        timeout=60,
        check=False,
    )
    assert sourced.returncode == 0, f"sourcing the generated env.sh failed under `set -u`:\n{_diagnose(sourced)}"
    exported = sourced.stdout.splitlines()
    assert len(exported) == 5, f"unexpected sourcing output: {sourced.stdout!r}"
    assert exported[0] == str(install_root)
    assert exported[1] == str(bin_dir)
    assert exported[2] == str(install_root)
    assert exported[3] == str(bin_dir)
    # The last line is the PATH a sourcing shell ends up with: the bin dir
    # prepended exactly once, and no empty component (which would mean "the
    # current directory" is on PATH).
    assert exported[4] == f"{bin_dir}:/usr/bin:/bin"
    assert "::" not in exported[4], "PATH gained an EMPTY component (an empty PATH entry means the current directory)"
    assert not exported[4].startswith(":"), "PATH starts with an empty component (an empty PATH entry means the current directory)"


def test_the_historical_unbound_heredoc_fails_the_finalise_stage(tmp_path: Path) -> None:
    """Proof this gate detects the FAR-2569 class, not merely today's bytes.

    The published defect is restored in a copy of the script and the SAME
    end-to-end install is run: it must abort in finalise with an unbound
    variable, exactly as reported by the operator.
    """
    _require_openssl_3()
    trust_key = Ed25519PrivateKey.generate()
    release = _signed_release(tmp_path, trust_key=trust_key)
    script = _installer_with_test_trust_key(tmp_path, trust_key=trust_key)

    source = script.read_text(encoding="utf-8")
    restored = source
    for fixed, historical in (
        ('export FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"', 'export FARNA_CONTROL_INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT}"'),
        ('export FARNA_CONTROL_BIN_DIR="${BIN_DIR}"', 'export FARNA_CONTROL_BIN_DIR="${FARNA_CONTROL_BIN_DIR}"'),
    ):
        assert fixed in restored, f"the fix this test guards is gone from install.sh: {fixed}"
        restored = restored.replace(fixed, historical)
    assert restored != source, "restoring the historical defect changed nothing"
    mutated = tmp_path / "install-historical-defect.sh"
    mutated.write_text(restored, encoding="utf-8")
    mutated.chmod(0o755)

    env = _install_env(tmp_path)
    result = _run_install(mutated, release, env, tmp_path)
    combined = result.stdout + result.stderr

    assert result.returncode != 0, f"the historical defect did NOT fail the install:\n{_diagnose(result)}"
    assert "FARNA_CONTROL_INSTALL_ROOT: unbound variable" in combined
    assert "Failing stage    : finalise (PATH shim and summary)" in combined
    # The failure lands AFTER `current` was re-pointed: that is why the install
    # is unusable rather than merely noisy.
    assert (_default_install_root(env) / "current").is_symlink()


def test_a_pre_rename_install_root_and_shim_are_migrated(tmp_path: Path) -> None:
    """FAR-2571: an install created before the rename is migrated, not orphaned.

    The rebrand moved the user install root and the PATH shim. A machine that
    ran the pre-rename installer must not end up with two half-installs: the old
    root is carried over, the marker is renamed, and the old shim is removed.
    """
    _require_openssl_3()
    trust_key = Ed25519PrivateKey.generate()
    release = _signed_release(tmp_path, trust_key=trust_key)
    script = _installer_with_test_trust_key(tmp_path, trust_key=trust_key)
    env = _install_env(tmp_path)

    install_root = _default_install_root(env)
    bin_dir = _default_bin_dir(env)
    legacy_root = Path(env["HOME"]) / ".local" / "opt" / "modulo"
    (legacy_root / "versions" / "0.9.0").mkdir(parents=True)
    (legacy_root / ".modulo-native-install").write_text("managed_by=install.sh\n", encoding="utf-8")
    legacy_shim = bin_dir / "modulo"
    bin_dir.mkdir(parents=True, exist_ok=True)
    legacy_shim.write_text(f'#!/bin/sh\nexec "{legacy_root}/current/launcher" "$@"\n', encoding="utf-8")
    legacy_shim.chmod(0o755)

    result = _run_install(script, release, env, tmp_path)
    assert result.returncode == 0, f"the installer failed while migrating a pre-rename install:\n{_diagnose(result)}"
    assert (install_root / "versions" / "0.9.0").is_dir(), "the pre-rename version tree was not carried over"
    assert not legacy_root.exists(), "the pre-rename install root was left behind (a half-migrated install)"
    assert (install_root / ".farna-control-native-install").is_file(), "the install marker was not renamed"
    assert not legacy_shim.exists(), "the pre-rename PATH shim was left behind"
    assert (bin_dir / "farna-control").is_file(), "the farna-control PATH shim was not written"


def test_an_unrelated_modulo_shim_is_left_untouched(tmp_path: Path) -> None:
    """FAR-2571: only a shim that targets the legacy root is removed.

    A user-owned binary at ``~/.local/bin/modulo`` is unrelated to the installer
    and must survive a fresh install unchanged.
    """
    _require_openssl_3()
    trust_key = Ed25519PrivateKey.generate()
    release = _signed_release(tmp_path, trust_key=trust_key)
    script = _installer_with_test_trust_key(tmp_path, trust_key=trust_key)
    env = _install_env(tmp_path)

    install_root = _default_install_root(env)
    bin_dir = _default_bin_dir(env)
    bin_dir.mkdir(parents=True, exist_ok=True)
    user_shim = bin_dir / "modulo"
    user_content = "#!/bin/sh\necho user-owned\n"
    user_shim.write_text(user_content, encoding="utf-8")
    user_shim.chmod(0o755)

    result = _run_install(script, release, env, tmp_path)
    assert result.returncode == 0, f"the installer failed on an unrelated modulo shim:\n{_diagnose(result)}"
    assert user_shim.is_file(), "the installer deleted an unrelated user-owned modulo shim"
    assert user_shim.read_text(encoding="utf-8") == user_content, "the installer rewrote an unrelated user-owned modulo shim"
    assert (install_root / "current").is_symlink(), "the install did not complete"


def test_two_install_roots_is_refused_rather_than_guessed(tmp_path: Path) -> None:
    """FAR-2571: both a pre-rename and a post-rename root -> refuse, never guess."""
    _require_openssl_3()
    trust_key = Ed25519PrivateKey.generate()
    release = _signed_release(tmp_path, trust_key=trust_key)
    script = _installer_with_test_trust_key(tmp_path, trust_key=trust_key)
    env = _install_env(tmp_path)

    install_root = _default_install_root(env)
    legacy_root = Path(env["HOME"]) / ".local" / "opt" / "modulo"
    legacy_root.mkdir(parents=True)
    install_root.mkdir(parents=True)

    result = _run_install(script, release, env, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"the installer guessed between two install roots:\n{_diagnose(result)}"
    assert "found BOTH a pre-rebrand install" in combined
    assert legacy_root.is_dir(), "the ambiguous pre-rename root was mutated"
