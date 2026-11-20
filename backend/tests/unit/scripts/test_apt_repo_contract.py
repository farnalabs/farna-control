"""FAR-2579 phase 2 — contract of the signed apt repository at apt.farna.dev.

``scripts/linux/build-apt-repo.sh`` builds the signed static site and
``.github/workflows/apt-repo.yml`` publishes it to Cloudflare Pages, then
installs from it on Ubuntu 22.04 and 24.04. The real proof is that run (and
the local Docker harness used to develop it); these tests pin the contract it
depends on, so a refactor cannot silently:

* change the keyring path the docs tell users to trust via ``signed-by``;
* stop signing the Release file (InRelease clearsigned + detached Release.gpg);
* point the pool redirects anywhere but the GitHub release download URL;
* inline a secret, drop the reviewed-key fingerprint check, or unpin an action;
* stop dispatching the refresh after a GITHUB_TOKEN-created release;
* (FAR-2590) re-download every released .deb on each run, or reuse a live
  index stanza that the reviewed key's signature and the release's SHA256SUMS
  do not both confirm.

Where the tools exist (Linux CI with gnupg, apt-utils and dpkg) the builder is
also EXECUTED end to end against a throwaway key.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
BUILDER = REPO_ROOT / "scripts" / "linux" / "build-apt-repo.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "apt-repo.yml"
RELEASE_UNIFIED = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
STAPLE_RETRY = REPO_ROOT / ".github" / "workflows" / "macos-staple-retry.yml"
SYSTEM_REQUIREMENTS = REPO_ROOT / "docs" / "system-requirements.md"
PUBLIC_KEY = REPO_ROOT / "deploy" / "apt" / "farna-control-archive-keyring.asc"

KEYRING_PATH = "/usr/share/keyrings/farna-control-archive-keyring.gpg"
SOURCES_LINE = f"deb [signed-by={KEYRING_PATH}] https://apt.farna.dev stable main"
RELEASE_DOWNLOAD_BASE = "https://github.com/farnalabs/farna-control/releases/download"
SECRETS = ("APT_REPO_GPG_PRIVATE_KEY", "APT_REPO_GPG_PASSPHRASE", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
_SHA_PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _builder() -> str:
    return BUILDER.read_text(encoding="utf-8")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_workflow_text())


def _openpgp_v4_fingerprint(armored: str) -> str:
    """Fingerprint of the first (primary) key packet of an armored public key."""
    body_lines = []
    in_body = False
    for line in armored.splitlines():
        if line.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            in_body = True
            continue
        if line.startswith("-----END"):
            break
        if in_body and line.strip() and ":" not in line and not line.startswith("="):
            body_lines.append(line.strip())
    data = base64.b64decode("".join(body_lines))
    tag_byte = data[0]
    assert tag_byte & 0x80, "not an OpenPGP packet"
    if tag_byte & 0x40:  # new-format header
        assert tag_byte & 0x3F == 6, "first packet is not a public key"
        first = data[1]
        if first < 192:
            length, offset = first, 2
        elif first < 224:
            length, offset = ((first - 192) << 8) + data[2] + 192, 3
        else:
            length, offset = int.from_bytes(data[2:6], "big"), 6
    else:  # old-format header
        assert (tag_byte >> 2) & 0x0F == 6, "first packet is not a public key"
        length_type = tag_byte & 0x03
        size = {0: 1, 1: 2, 2: 4}[length_type]
        length, offset = int.from_bytes(data[1 : 1 + size], "big"), 1 + size
    packet = data[offset : offset + length]
    assert packet[0] == 4, "only v4 keys are expected"
    return hashlib.sha1(b"\x99" + length.to_bytes(2, "big") + packet, usedforsecurity=False).hexdigest().upper()


# --- docs: the trust anchor users configure --------------------------------


def test_docs_install_the_keyring_where_signed_by_points() -> None:
    docs = SYSTEM_REQUIREMENTS.read_text(encoding="utf-8")
    assert "curl -fsSL https://apt.farna.dev/farna-control-archive-keyring.gpg" in docs
    assert f"sudo tee {KEYRING_PATH} >/dev/null" in docs
    assert SOURCES_LINE in docs
    assert "sudo apt install farna-control" in docs


def test_docs_cover_upgrade_and_pinning() -> None:
    docs = SYSTEM_REQUIREMENTS.read_text(encoding="utf-8")
    assert "sudo apt upgrade" in docs
    assert "sudo apt install farna-control=<version>" in docs
    assert "sudo apt-mark hold farna-control" in docs


def test_docs_fingerprint_matches_the_committed_public_key() -> None:
    fingerprint = _openpgp_v4_fingerprint(PUBLIC_KEY.read_text(encoding="ascii"))
    grouped = " ".join(fingerprint[i : i + 4] for i in range(0, 40, 4))
    grouped = grouped[:24] + " " + grouped[24:]  # gpg's double space between the halves
    assert grouped in SYSTEM_REQUIREMENTS.read_text(encoding="utf-8")


def test_committed_key_is_public_only() -> None:
    text = PUBLIC_KEY.read_text(encoding="ascii")
    assert text.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert "PRIVATE KEY" not in text


def test_builder_install_page_uses_the_same_sources_line() -> None:
    text = _builder()
    assert 'KEYRING_NAME="${PACKAGE}-archive-keyring"' in text
    assert 'echo "deb [signed-by=/usr/share/keyrings/${KEYRING_NAME}.gpg] ${PUBLIC_URL} ${SUITE} ${COMPONENT}"' in text


# --- builder: signing and redirects ----------------------------------------


def test_release_is_clearsigned_and_detached_signed() -> None:
    text = _builder()
    assert '--clearsign \\\n  --output "${SITE}/dists/${SUITE}/InRelease" "${RELEASE}"' in text
    assert '--armor --detach-sign \\\n  --output "${SITE}/dists/${SUITE}/Release.gpg" "${RELEASE}"' in text


def test_builder_proves_both_signatures_against_the_published_keyring() -> None:
    text = _builder()
    assert 'gpgv --keyring "${SITE}/${KEYRING_NAME}.gpg" "${SITE}/dists/${SUITE}/InRelease"' in text
    assert 'gpgv --keyring "${SITE}/${KEYRING_NAME}.gpg" "${SITE}/dists/${SUITE}/Release.gpg" "${RELEASE}"' in text


def test_builder_enforces_the_reviewed_key_fingerprint() -> None:
    text = _builder()
    assert '[[ "${expected_fprs[0]}" = "${FPR}" ]]' in text
    assert "does not match the reviewed public key" in text


def test_redirects_target_the_release_download_url() -> None:
    text = _builder()
    assert f'RELEASE_DOWNLOAD_BASE="${{APT_REPO_RELEASE_DOWNLOAD_BASE:-{RELEASE_DOWNLOAD_BASE}}}"' in text
    assert 'redirects+=("/${POOL_DIR}/${file} ${RELEASE_DOWNLOAD_BASE}/${tag}/${file} 302")' in text
    assert 'POOL_DIR="pool/${COMPONENT}/f/${PACKAGE}"' in text


def test_builder_never_publishes_deb_files() -> None:
    # Cloudflare Pages caps files at 25 MiB: debs must stay out of the site.
    assert not re.search(r"(cp|mv|ln)[^\n]*\$\{SITE\}/\$\{POOL_DIR\}", _builder())


# --- workflow ----------------------------------------------------------------


def test_workflow_triggers_on_release_and_dispatch() -> None:
    triggers = _workflow()[True]  # PyYAML parses the bare `on` key as True
    assert triggers["release"]["types"] == ["published"]
    assert "workflow_dispatch" in triggers


def test_workflow_uses_secrets_not_literals() -> None:
    text = _workflow_text()
    for name in SECRETS:
        assert f"${{{{ secrets.{name} }}}}" in text, f"{name} is not read from secrets"
    assert "BEGIN PGP PRIVATE KEY" not in text
    assert not re.search(r"(api[_-]?token|account[_-]?id)\s*:\s*['\"]?[0-9a-f]{20,}", text, re.IGNORECASE)


def test_workflow_signs_with_the_reviewed_public_key() -> None:
    assert "APT_REPO_PUBLIC_KEY_FILE: deploy/apt/farna-control-archive-keyring.asc" in _workflow_text()


def test_workflow_actions_are_pinned_by_sha() -> None:
    uses = [step["uses"] for job in _workflow()["jobs"].values() for step in job.get("steps", []) if "uses" in step]
    assert uses
    unpinned = [ref for ref in uses if not _SHA_PINNED_USES.match(ref)]
    assert not unpinned, f"actions not pinned to a commit SHA: {unpinned}"


def test_workflow_permissions_are_read_only() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, f"{name} widens the workflow permissions"


def test_wrangler_is_pinned_to_an_exact_version_and_project() -> None:
    text = _workflow_text()
    run_scripts = "\n".join(step.get("run", "") for job in _workflow()["jobs"].values() for step in job.get("steps", []))
    assert re.search(r'WRANGLER_VERSION: "\d+\.\d+\.\d+"', text)
    assert 'npm install --global --ignore-scripts "wrangler@${WRANGLER_VERSION}"' in run_scripts
    assert "wrangler pages deploy site" in run_scripts
    assert not re.search(r"\bnpx\b", run_scripts), "wrangler must be installed explicitly, not fetched on demand"
    assert "PAGES_PROJECT: farna-control-apt" in text
    assert '--project-name "${PAGES_PROJECT}" --branch main' in run_scripts


def test_workflow_verifies_every_deb_against_release_sha256sums() -> None:
    text = _workflow_text()
    assert 'sha256sum --check --strict -) \\\n              || { echo "::error::${tag}/${deb} does not match' in text


def test_post_deploy_check_installs_the_documented_way_on_both_releases() -> None:
    verify = _workflow()["jobs"]["verify"]
    assert verify["strategy"]["matrix"]["image"] == ["ubuntu:22.04", "ubuntu:24.04"]
    script = "\n".join(step.get("run", "") for step in verify["steps"])
    assert f'echo "deb [signed-by={KEYRING_PATH}] ${{REPO_URL}} stable main"' in script
    assert f"tee {KEYRING_PATH} >/dev/null" in script
    assert "apt-get install -y farna-control" in script
    assert "farna-control --version" in script
    assert "bash scripts/linux/validate-deb.sh --installed" in script


def test_public_url_is_a_single_setting_with_a_pages_fallback() -> None:
    env = _workflow()["env"]
    assert env["APT_REPO_PUBLIC_URL"] == "https://apt.farna.dev"
    assert env["APT_REPO_PAGES_URL"] == "https://farna-control-apt.pages.dev"


@pytest.mark.parametrize("workflow", [RELEASE_UNIFIED, STAPLE_RETRY], ids=["release-unified", "macos-staple-retry"])
def test_release_publishers_dispatch_the_refresh(workflow: Path) -> None:
    job = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["dispatch-apt-repo"]
    assert job["permissions"] == {"actions": "write", "contents": "read"}
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert 'gh workflow run apt-repo.yml --repo "$REPO" --ref main' in script


# --- builder, executed -------------------------------------------------------

_TOOLS = ("bash", "gpg", "gpgv", "gpgconf", "apt-ftparchive", "dpkg-deb")


def _require_tools() -> None:
    missing = [tool for tool in _TOOLS if shutil.which(tool) is None]
    if missing or os.name == "nt":
        pytest.skip(f"needs a Linux host with {', '.join(_TOOLS)} (missing: {missing or 'POSIX host'})")


def _run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed tools and repository-owned scripts
        cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=120, check=False
    )


def _dummy_deb(root: Path, version: str) -> Path:
    pkg = root / f"pkg-{version}"
    (pkg / "DEBIAN").mkdir(parents=True)
    (pkg / "DEBIAN" / "control").write_text(
        f"Package: farna-control\nVersion: {version}\nArchitecture: amd64\nMaintainer: Test <t@example.invalid>\nDescription: test\n",
        encoding="utf-8",
    )
    deb = root / "debs" / f"farna-control_{version}_amd64.deb"
    deb.parent.mkdir(exist_ok=True)
    result = _run(["dpkg-deb", "--root-owner-group", "--build", str(pkg), str(deb)])
    assert result.returncode == 0, result.stderr
    return deb


def _throwaway_key(root: Path) -> tuple[str, Path]:
    home = root / "gnupg-gen"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    gen = ["gpg", "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
    result = _run([*gen, "--quick-gen-key", "Test Key <test@example.invalid>", "rsa2048", "sign", "1d"], env=env)
    assert result.returncode == 0, result.stderr
    private = _run([*gen, "--armor", "--export-secret-keys"], env=env).stdout
    public = root / "public.asc"
    public.write_text(_run(["gpg", "--batch", "--armor", "--export"], env=env).stdout, encoding="ascii")
    _run(["gpgconf", "--kill", "gpg-agent"], env=env)
    return private, public


def test_builder_end_to_end_signs_orders_and_redirects(tmp_path: Path) -> None:
    _require_tools()
    old = _dummy_deb(tmp_path, "0.3.300")
    new = _dummy_deb(tmp_path, "0.3.310")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(f"bundle-v0.3.300\t{old}\nbundle-v0.3.310\t{new}\n", encoding="utf-8")
    private, public = _throwaway_key(tmp_path)
    site = tmp_path / "site"
    env = {**os.environ, "APT_REPO_GPG_PRIVATE_KEY": private, "APT_REPO_PUBLIC_KEY_FILE": str(public)}
    result = _run(["bash", str(BUILDER), str(site), str(manifest)], env=env)
    assert result.returncode == 0, result.stdout + result.stderr

    packages = (site / "dists/stable/main/binary-amd64/Packages").read_text(encoding="utf-8")
    versions = re.findall(r"^Version: (.+)$", packages, re.MULTILINE)
    assert versions == ["0.3.310", "0.3.300"]
    assert "Filename: pool/main/f/farna-control/farna-control_0.3.310_amd64.deb" in packages
    assert f"SHA256: {hashlib.sha256(new.read_bytes()).hexdigest()}" in packages

    redirects = (site / "_redirects").read_text(encoding="utf-8")
    assert (f"/pool/main/f/farna-control/farna-control_0.3.310_amd64.deb {RELEASE_DOWNLOAD_BASE}/bundle-v0.3.310/farna-control_0.3.310_amd64.deb 302") in redirects
    assert not list(site.rglob("*.deb"))

    keyring = site / "farna-control-archive-keyring.gpg"
    inrelease = site / "dists/stable/InRelease"
    assert _run(["gpgv", "--keyring", str(keyring), str(inrelease)]).returncode == 0
    release = site / "dists/stable/Release"
    assert _run(["gpgv", "--keyring", str(keyring), str(site / "dists/stable/Release.gpg"), str(release)]).returncode == 0
    tampered = tmp_path / "InRelease.tampered"
    tampered.write_text(inrelease.read_text(encoding="utf-8").replace("Label: Farna Control", "Label: Farna Kontrol"), encoding="utf-8")
    assert _run(["gpgv", "--keyring", str(keyring), str(tampered)]).returncode != 0


def test_builder_refuses_a_key_that_is_not_the_reviewed_one(tmp_path: Path) -> None:
    _require_tools()
    deb = _dummy_deb(tmp_path, "0.3.300")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(f"bundle-v0.3.300\t{deb}\n", encoding="utf-8")
    private, _ = _throwaway_key(tmp_path)
    env = {**os.environ, "APT_REPO_GPG_PRIVATE_KEY": private, "APT_REPO_PUBLIC_KEY_FILE": str(PUBLIC_KEY)}
    result = _run(["bash", str(BUILDER), str(tmp_path / "site"), str(manifest)], env=env)
    assert result.returncode != 0
    assert "does not match the reviewed public key" in result.stderr


# --- FAR-2590: incremental refresh (reuse the live index) -------------------

PLANNER = REPO_ROOT / "scripts" / "linux" / "plan-apt-repo.sh"
LIB = REPO_ROOT / "scripts" / "linux" / "apt-repo-lib.sh"
VERSIONS = ("0.3.300", "0.3.310", "0.3.320")


def test_workflow_plans_reuse_and_downloads_only_the_plan() -> None:
    collect = next(step for step in _workflow()["jobs"]["build"]["steps"] if step.get("id") == "collect")
    script = collect["run"]
    assert collect["env"]["APT_REPO_PUBLIC_KEY_FILE"] == "deploy/apt/farna-control-archive-keyring.asc"
    assert '"${APT_REPO_PUBLIC_URL}" "${APT_REPO_PAGES_URL}"' in script
    assert '"${base}/dists/stable/InRelease"' in script
    assert '"${base}/dists/stable/main/binary-amd64/Packages"' in script
    assert "bash scripts/linux/plan-apt-repo.sh candidates.tsv previous plan" in script
    # The only .deb download loop reads the plan, never the full release list.
    deb_downloads = [line for line in script.splitlines() if '-o "${dir}/${deb}"' in line]
    assert len(deb_downloads) == 1
    assert "done < plan/download.tsv" in script
    assert "bash scripts/linux/build-apt-repo.sh site manifest.tsv plan/reuse.tsv" in _workflow_text()


def test_planner_and_builder_share_the_stanza_rules() -> None:
    source_line = 'source "$(dirname -- "${BASH_SOURCE[0]}")/apt-repo-lib.sh"'
    assert source_line in _builder()
    assert source_line in PLANNER.read_text(encoding="utf-8")
    lib = LIB.read_text(encoding="utf-8")
    assert "apt_stanza_problem()" in lib
    assert "apt_sums_entry()" in lib


def test_planner_verifies_the_live_index_before_reusing_it() -> None:
    text = PLANNER.read_text(encoding="utf-8")
    assert 'gpgv --keyring "${WORK}/keyring.gpg" --output "${signed}" "${PREVIOUS}/InRelease"' in text
    assert "is not the one InRelease signs" in text
    assert 'apt_stanza_problem "${stanza_file}" "${deb}" "${want_sha}"' in text


def _sums(root: Path, tag: str, deb: Path, sha: str | None = None) -> Path:
    sums = root / "sums" / tag / "SHA256SUMS"
    sums.parent.mkdir(parents=True, exist_ok=True)
    digest = sha or hashlib.sha256(deb.read_bytes()).hexdigest()
    sums.write_text(f"{'0' * 64}  farna-control-other.tar.gz\n{digest}  {deb.name}\n", encoding="utf-8")
    return sums


def _build(site: Path, manifest: Path, env: dict[str, str], reuse: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["bash", str(BUILDER), str(site), str(manifest)]
    if reuse is not None:
        cmd.append(str(reuse))
    return _run(cmd, env=env)


def _plan(candidates: Path, previous: Path, out: Path, public: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "APT_REPO_PUBLIC_KEY_FILE": str(public)}
    return _run(["bash", str(PLANNER), str(candidates), str(previous), str(out)], env=env)


def _published(tmp_path: Path) -> dict:
    """A live repository listing 0.3.300 and 0.3.310; release 0.3.320 is new."""
    debs = {version: _dummy_deb(tmp_path, version) for version in VERSIONS}
    private, public = _throwaway_key(tmp_path)
    env = {**os.environ, "APT_REPO_GPG_PRIVATE_KEY": private, "APT_REPO_PUBLIC_KEY_FILE": str(public)}
    manifest = tmp_path / "manifest-live.tsv"
    manifest.write_text("".join(f"bundle-v{v}\t{debs[v]}\n" for v in VERSIONS[:2]), encoding="utf-8")
    live = tmp_path / "live"
    result = _build(live, manifest, env)
    assert result.returncode == 0, result.stdout + result.stderr
    previous = tmp_path / "previous"
    previous.mkdir()
    shutil.copy(live / "dists/stable/InRelease", previous / "InRelease")
    shutil.copy(live / "dists/stable/main/binary-amd64/Packages", previous / "Packages")
    return {"debs": debs, "env": env, "public": public, "previous": previous}


def _candidates(tmp_path: Path, debs: dict[str, Path], shas: dict[str, str] | None = None) -> Path:
    candidates = tmp_path / "candidates.tsv"
    lines = []
    for version in reversed(VERSIONS):  # the releases API lists newest first
        sums = _sums(tmp_path, f"bundle-v{version}", debs[version], (shas or {}).get(version))
        lines.append(f"bundle-v{version}\t{debs[version].name}\t{sums}\n")
    candidates.write_text("".join(lines), encoding="utf-8")
    return candidates


def _tags(path: Path) -> list[str]:
    return [line.split("\t")[0] for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_incremental_refresh_reuses_indexed_versions_and_downloads_only_new_ones(tmp_path: Path) -> None:
    _require_tools()
    live = _published(tmp_path)
    debs = live["debs"]
    plan = tmp_path / "plan"
    result = _plan(_candidates(tmp_path, debs), live["previous"], plan, live["public"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (plan / "download.tsv").read_text(encoding="utf-8") == "bundle-v0.3.320\tfarna-control_0.3.320_amd64.deb\n"
    assert _tags(plan / "reuse.tsv") == ["bundle-v0.3.310", "bundle-v0.3.300"]

    # The reused .debs are not needed any more: only the new one is on disk.
    debs["0.3.300"].unlink()
    debs["0.3.310"].unlink()
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(f"bundle-v0.3.320\t{debs['0.3.320']}\n", encoding="utf-8")
    site = tmp_path / "site"
    result = _build(site, manifest, live["env"], plan / "reuse.tsv")
    assert result.returncode == 0, result.stdout + result.stderr

    packages = (site / "dists/stable/main/binary-amd64/Packages").read_text(encoding="utf-8")
    assert re.findall(r"^Version: (.+)$", packages, re.MULTILINE) == ["0.3.320", "0.3.310", "0.3.300"]
    assert f"SHA256: {hashlib.sha256(debs['0.3.320'].read_bytes()).hexdigest()}" in packages
    for stanza in (live["previous"] / "Packages").read_text(encoding="utf-8").strip().split("\n\n"):
        assert stanza in packages  # reused byte for byte
    redirects = (site / "_redirects").read_text(encoding="utf-8")
    for version in VERSIONS:
        deb = f"farna-control_{version}_amd64.deb"
        assert f"/pool/main/f/farna-control/{deb} {RELEASE_DOWNLOAD_BASE}/bundle-v{version}/{deb} 302" in redirects
    keyring = site / "farna-control-archive-keyring.gpg"
    assert _run(["gpgv", "--keyring", str(keyring), str(site / "dists/stable/InRelease")]).returncode == 0


def test_incremental_refresh_discards_a_stanza_the_release_sha256sums_does_not_confirm(tmp_path: Path) -> None:
    _require_tools()
    live = _published(tmp_path)
    plan = tmp_path / "plan"
    candidates = _candidates(tmp_path, live["debs"], {"0.3.300": "f" * 64})
    result = _plan(candidates, live["previous"], plan, live["public"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tags(plan / "download.tsv") == ["bundle-v0.3.320", "bundle-v0.3.300"]
    assert _tags(plan / "reuse.tsv") == ["bundle-v0.3.310"]
    assert "indexed stanza discarded: stanza SHA256 is" in result.stdout


def test_incremental_refresh_first_run_downloads_everything(tmp_path: Path) -> None:
    _require_tools()
    debs = {version: _dummy_deb(tmp_path, version) for version in VERSIONS}
    _, public = _throwaway_key(tmp_path)
    empty = tmp_path / "previous"
    empty.mkdir()
    plan = tmp_path / "plan"
    result = _plan(_candidates(tmp_path, debs), empty, plan, public)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tags(plan / "download.tsv") == [f"bundle-v{v}" for v in reversed(VERSIONS)]
    assert (plan / "reuse.tsv").stat().st_size == 0


def test_incremental_refresh_ignores_a_live_index_that_does_not_verify(tmp_path: Path) -> None:
    _require_tools()
    live = _published(tmp_path)
    packages = live["previous"] / "Packages"
    packages.write_text(packages.read_text(encoding="utf-8").replace("Description: test", "Description: tampered"), encoding="utf-8")
    plan = tmp_path / "plan"
    result = _plan(_candidates(tmp_path, live["debs"]), live["previous"], plan, live["public"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tags(plan / "download.tsv") == [f"bundle-v{v}" for v in reversed(VERSIONS)]
    assert "ignoring the previous index (full rebuild)" in result.stderr


def test_incremental_refresh_ignores_a_live_index_signed_by_another_key(tmp_path: Path) -> None:
    _require_tools()
    live = _published(tmp_path)
    plan = tmp_path / "plan"
    result = _plan(_candidates(tmp_path, live["debs"]), live["previous"], plan, PUBLIC_KEY)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _tags(plan / "download.tsv") == [f"bundle-v{v}" for v in reversed(VERSIONS)]
    assert "does not verify against the reviewed key" in result.stderr


def test_builder_refuses_a_reused_stanza_the_release_sha256sums_does_not_confirm(tmp_path: Path) -> None:
    _require_tools()
    live = _published(tmp_path)
    first = (live["previous"] / "Packages").read_text(encoding="utf-8").strip().split("\n\n")[0]
    stanza = tmp_path / "old.stanza"
    stanza.write_text(first + "\n", encoding="utf-8")
    deb_name = first.split("Filename: pool/main/f/farna-control/", 1)[1].split("\n", 1)[0]
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'e' * 64}  {deb_name}\n", encoding="utf-8")
    reuse = tmp_path / "reuse.tsv"
    reuse.write_text(f"bundle-v0\t{stanza}\t{sums}\n", encoding="utf-8")
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("# every version reused\n", encoding="utf-8")
    result = _build(tmp_path / "site", manifest, live["env"], reuse)
    assert result.returncode != 0
    assert f"refusing the stanza for {deb_name}: stanza SHA256 is" in result.stderr
