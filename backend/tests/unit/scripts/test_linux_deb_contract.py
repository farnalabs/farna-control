"""FAR-2579 — layout and safety invariants of the farna-control .deb.

``scripts/linux/build-deb.sh`` packages the native bundle; the real proof is
the end-to-end Docker/CI run (``scripts/linux/validate-deb.sh`` driven by
``.github/workflows/linux-deb.yml`` on Ubuntu 22.04 and 24.04). These tests pin
the contract that run depends on, so a refactor cannot silently:

* move the wrapper off ``/usr/bin/farna-control`` or the bundle off
  ``/opt/farna-control/versions/<version>`` + ``current``;
* let a maintainer script reach a per-user data dir (remove/purge must keep
  every user's ``~/.local/share/modulo/data``);
* drop the declared base dependencies;
* let the wrapper inherit the tarball install's ``FARNA_CONTROL_INSTALL_ROOT``
  or run the in-app upgrader against apt-owned files;
* stop validating on both supported Ubuntu releases.

The maintainer scripts and the wrapper are extracted from the builder's
heredocs and EXECUTED (POSIX ``sh``) where that is the stronger check.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BUILD_DEB = REPO_ROOT / "scripts" / "linux" / "build-deb.sh"
VALIDATE_DEB = REPO_ROOT / "scripts" / "linux" / "validate-deb.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "linux-deb.yml"

_HEREDOC_RE = re.compile(
    r"cat > \"(?P<target>[^\"]+)\" <<'(?P<tag>[A-Z_]+)'\n(?P<body>.*?)\n(?P=tag)\n",
    re.DOTALL,
)


def _builder_text() -> str:
    return BUILD_DEB.read_text(encoding="utf-8")


def _heredoc(target_suffix: str) -> str:
    """Return the body of the quoted heredoc written to a path ending in ``target_suffix``."""
    matches = [m.group("body") for m in _HEREDOC_RE.finditer(_builder_text()) if m.group("target").endswith(target_suffix)]
    assert len(matches) == 1, f"expected exactly one heredoc writing *{target_suffix}"
    return matches[0]


def _sh() -> str:
    for candidate in (r"C:\Program Files\Git\usr\bin\sh.exe", r"C:\Program Files\Git\bin\sh.exe"):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("sh")
    if found is None:
        pytest.skip("a POSIX sh is required to execute the packaged scripts")
    return found


def _run_sh(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed shell and a repository-owned script body
        [_sh(), str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


# --- layout -----------------------------------------------------------------


def test_builder_installs_wrapper_at_usr_bin() -> None:
    assert 'WRAPPER_PATH="/usr/bin/farna-control"' in _builder_text()


def test_builder_lays_bundle_under_opt_versions_with_current_symlink() -> None:
    text = _builder_text()
    assert 'INSTALL_PREFIX="/opt/farna-control"' in text
    assert 'VERSIONED="${PKGROOT}${INSTALL_PREFIX}/versions/${VERSION}"' in text
    assert 'ln -s "versions/${VERSION}" "${PKGROOT}${INSTALL_PREFIX}/current"' in text


def test_builder_output_name_is_debian_canonical() -> None:
    assert 'DEB="${OUTDIR}/${PACKAGE}_${VERSION}_amd64.deb"' in _builder_text()


def test_builder_verifies_bundle_checksums_before_packaging() -> None:
    text = _builder_text()
    verify = text.index("sha256sum --check --strict --quiet SHA256SUMS")
    build = text.index("dpkg-deb --root-owner-group")
    assert verify < build


def test_wrapper_execs_current_launcher() -> None:
    wrapper = _heredoc("${WRAPPER_PATH}")
    assert "INSTALL_ROOT=/opt/farna-control" in wrapper
    assert 'exec "${INSTALL_ROOT}/current/launcher" "$@"' in wrapper


# --- dependencies -----------------------------------------------------------


def test_base_depends_are_declared() -> None:
    match = re.search(r"^BASE_DEPENDS=\((?P<deps>[^)]*)\)$", _builder_text(), re.MULTILINE)
    assert match is not None
    declared = set(re.findall(r'"([^"]+)"', match.group("deps")))
    assert declared == {"ca-certificates", "openssl"}


def test_libc6_is_versioned_from_the_scanned_glibc_floor() -> None:
    text = _builder_text()
    assert 'packages["libc6"]=1' in text
    assert 'depends+=("libc6 (>= ${glibc_floor})")' in text
    assert "Depends: ${DEPENDS}" in text


def test_unresolvable_system_libraries_fail_the_build() -> None:
    assert re.search(r"if \[\[ \"\$\{#unresolved\[@\]\}\" -gt 0 \]\]; then\n\s+die ", _builder_text())


# --- maintainer scripts never touch user data -------------------------------

_MAINTAINER_SCRIPTS = ("/DEBIAN/postinst", "/DEBIAN/prerm", "/DEBIAN/postrm")
_USER_DATA_MARKERS = ("$HOME", "${HOME}", "/home", "/root", ".local/share", "modulo/data", "getent", "/etc/passwd")


@pytest.mark.parametrize("script", _MAINTAINER_SCRIPTS)
def test_maintainer_script_never_references_user_data(script: str) -> None:
    body = _heredoc(script)
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    leaked = [marker for marker in _USER_DATA_MARKERS if marker in code]
    assert not leaked, f"{script} references user-data locations: {leaked}"


@pytest.mark.parametrize("script", _MAINTAINER_SCRIPTS)
def test_maintainer_script_deletions_stay_inside_opt(script: str) -> None:
    body = _heredoc(script)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not re.search(r"\b(rm|rmdir|find)\b", stripped):
            continue
        targets = re.findall(r"(/[\w./-]+)", stripped)
        assert targets, f"deletion with no explicit absolute target in {script}: {stripped}"
        outside = [t for t in targets if not t.startswith("/opt/farna-control") and t != "/dev/null"]
        assert not outside, f"{script} deletes outside /opt/farna-control: {stripped}"


def test_postrm_purge_leaves_a_data_dir_untouched(tmp_path: Path) -> None:
    script = tmp_path / "postrm"
    script.write_text(_heredoc("/DEBIAN/postrm") + "\n", encoding="utf-8", newline="\n")
    home = tmp_path / "home"
    data_dir = home / ".local" / "share" / "modulo" / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text("{}", encoding="utf-8")
    env = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / ".local" / "share")}
    for action in ("remove", "purge"):
        result = _run_sh(script, action, env=env)
        assert result.returncode == 0, result.stderr
    assert (data_dir / "state.json").is_file()


@pytest.mark.parametrize("script", _MAINTAINER_SCRIPTS)
def test_maintainer_script_rejects_unknown_action(script: str, tmp_path: Path) -> None:
    path = tmp_path / "maint"
    path.write_text(_heredoc(script) + "\n", encoding="utf-8", newline="\n")
    result = _run_sh(path, "bogus-action", env=dict(os.environ))
    assert result.returncode == 1
    assert "unknown argument" in result.stderr


# --- wrapper behaviour ------------------------------------------------------


def _wrapper_with_fake_root(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the wrapper with its install root pointed at a fake launcher tree."""
    fake_root = tmp_path / "opt-farna-control"
    launcher = fake_root / "current" / "launcher"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\necho "ROOT=${FARNA_CONTROL_INSTALL_ROOT}"\necho "ARGS=$*"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    body = _heredoc("${WRAPPER_PATH}").replace("INSTALL_ROOT=/opt/farna-control", f"INSTALL_ROOT={fake_root.as_posix()}")
    wrapper = tmp_path / "farna-control"
    wrapper.write_text(body + "\n", encoding="utf-8", newline="\n")
    return wrapper, fake_root


def test_wrapper_overrides_an_inherited_tarball_install_root(tmp_path: Path) -> None:
    wrapper, fake_root = _wrapper_with_fake_root(tmp_path)
    env = {**os.environ, "FARNA_CONTROL_INSTALL_ROOT": "/home/someone/.local/opt/farna-control", "HOME": str(tmp_path)}
    result = _run_sh(wrapper, "status", "--json", env=env)
    assert result.returncode == 0, result.stderr
    assert f"ROOT={fake_root.as_posix()}" in result.stdout
    assert "ARGS=status --json" in result.stdout


def test_wrapper_refuses_the_in_app_upgrader(tmp_path: Path) -> None:
    wrapper, _ = _wrapper_with_fake_root(tmp_path)
    result = _run_sh(wrapper, "upgrade", "bundle-v9.9.9", env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 2
    assert "managed by apt" in result.stderr
    assert "ROOT=" not in result.stdout


def test_wrapper_notes_a_coexisting_tarball_install_on_start(tmp_path: Path) -> None:
    wrapper, _ = _wrapper_with_fake_root(tmp_path)
    tarball_launcher = tmp_path / ".local" / "opt" / "farna-control" / "current" / "launcher"
    tarball_launcher.parent.mkdir(parents=True)
    tarball_launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    result = _run_sh(wrapper, "start", "--detach", env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "per-user tarball install also exists" in result.stderr


# --- workflow + validation contract -----------------------------------------


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_workflow_is_reusable_and_dispatchable() -> None:
    triggers = _workflow()[True]  # PyYAML parses the bare `on:` key as True
    assert set(triggers["workflow_call"]["inputs"]) == {"version", "bundle_artifact"}
    assert "deb_artifact" in triggers["workflow_call"]["outputs"]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"ref", "version", "release_tag", "full_check"}


def test_workflow_validates_on_both_supported_ubuntu_releases() -> None:
    validate = _workflow()["jobs"]["validate"]
    assert validate["strategy"]["matrix"]["image"] == ["ubuntu:22.04", "ubuntu:24.04"]
    assert validate["container"] == "${{ matrix.image }}"
    runs = "\n".join(step.get("run", "") for step in validate["steps"])
    assert "scripts/linux/validate-deb.sh" in runs


def test_workflow_packages_on_the_glibc_baseline() -> None:
    assert _workflow()["jobs"]["build-deb"]["container"] == "ubuntu:22.04"


def test_validation_boots_as_non_root_and_keeps_data_through_purge() -> None:
    text = VALIDATE_DEB.read_text(encoding="utf-8")
    assert 'runuser -u "${VALIDATION_USER}"' in text
    assert "as_user farna-control start --detach" in text
    for marker in ('"${BASE}/healthz"', "saq-runs saq-system", "apt-get remove -y farna-control", "apt-get purge -y farna-control"):
        assert marker in text
    purge = text.index("apt-get purge -y farna-control")
    assert '[[ -d "${DATA_DIR}" ]] || fail "user data dir ${DATA_DIR} was deleted by purge"' in text[purge:]
