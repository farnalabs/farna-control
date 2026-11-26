"""FAR-2827: the stable and version-pinned native-installer entry points.

``deploy/caddy/Caddyfile`` publishes ``/install.sh`` for a Caddy-fronted
deployment. These checks read the *real* Caddyfile and pin the two things that
make the advertised URL trustworthy:

* an exact-path ``/install.sh`` handler that redirects to the checked-in
  ``scripts/install.sh`` on the default branch, plus a ``?version=<X.Y.Z>`` form
  that redirects to the same file at the release tag ``bundle-v<X.Y.Z>``, with a
  malformed version refused rather than silently served from the default branch;
* the release-tag convention itself, cross-checked against the pipeline that
  CREATES ``bundle-v<version>`` tags and the installer that CONSUMES them, so
  the redirect cannot drift to a tag shape nothing publishes.

The live URL can only be observed after a deploy; these are the static and
contract-level gates. The post-deploy curl checks are recorded in
``docs/product-map/infra/native-single-install.md``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Walk up from this test file to the repository root."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "deploy" / "caddy" / "Caddyfile").is_file():
            return parent
    raise RuntimeError("Could not locate the repo root (deploy/caddy/Caddyfile)")


_REPO_ROOT = _repo_root()
_CADDYFILE = _REPO_ROOT / "deploy" / "caddy" / "Caddyfile"
_INSTALLER = _REPO_ROOT / "scripts" / "install.sh"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-unified.yml"

#: Directive order that must be preserved: the installer handle sits after every
#: backend route and before the SPA catch-all, so it can neither shadow nor be
#: captured by any of them.
_PROTECTED_ORDER = (
    "\n    @health {",
    "\n    @api {",
    "\n    @wellknown {",
    "\n    @mcp {",
    "\n    @ws {",
    "\n    handle @installer {",
    "\n    handle {",
)


def _read(path: Path) -> str:
    """Read a repository file as UTF-8 text."""
    return path.read_text(encoding="utf-8")


def _brace_block(text: str, opening: str) -> str:
    """Return the brace-matched body of the block that starts at ``opening``."""
    start = text.index(opening) + len(opening)
    depth = 1
    index = start
    while depth:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return text[start : index - 1]


def _installer_block() -> str:
    """The real ``handle @installer`` block from the checked-in Caddyfile."""
    return _brace_block(_read(_CADDYFILE), "\n    handle @installer {")


def _redirect_targets(block: str) -> list[str]:
    """Every redirect target in ``block``, in file order."""
    return re.findall(r"redir\s+\S+\s+(\S+)\s+302", block)


def test_installer_rule_is_exact_path_and_sits_between_backend_routes_and_spa() -> None:
    """The rule matches only ``/install.sh`` and keeps the protected path order."""
    caddyfile = _read(_CADDYFILE)

    matcher = _brace_block(caddyfile, "\n    @installer {")
    assert re.search(r"^\s*path\s+/install\.sh\s*$", matcher, re.MULTILINE)
    assert "*" not in matcher

    assert caddyfile.count("\n    handle @installer {") == 1
    positions = [caddyfile.index(marker) for marker in _PROTECTED_ORDER]
    assert positions == sorted(positions), "the installer handle must sit after every backend route and before the SPA catch-all"


def test_installer_redirects_to_the_checked_in_script() -> None:
    """Stable and pinned forms redirect to scripts/install.sh; a bad version is refused."""
    block = _installer_block()
    targets = _redirect_targets(block)
    assert len(targets) == 2

    stable = [target for target in targets if target.endswith("/farnalabs/farna-control/main/scripts/install.sh")]
    pinned = [target for target in targets if "{query.version}" in target]
    assert len(stable) == 1
    assert len(pinned) == 1
    assert pinned[0].endswith("/scripts/install.sh")

    assert "not query version=*" in block
    assert re.search(r"respond\s+\"[^\"]*X\.Y\.Z[^\"]*\"\s+400", block)


def test_pinned_target_follows_the_release_tag_convention() -> None:
    """The URL's tag shape is the prefix the pipeline creates and the installer consumes."""
    block = _installer_block()

    matcher = re.search(r"\{query\.version\}\.matches\('([^']+)'\)", block)
    assert matcher is not None
    version_pattern = matcher.group(1)
    assert re.fullmatch(version_pattern, "0.3.329")
    assert re.fullmatch(version_pattern, "0.0.1")
    assert re.fullmatch(version_pattern, "0.3") is None
    assert re.fullmatch(version_pattern, "0.3.329-rc.1") is None
    assert re.fullmatch(version_pattern, "../evil") is None

    installer = _read(_INSTALLER)
    consumer = re.search(r"\$\{RELEASE_TAG#([A-Za-z0-9-]+)\}", installer)
    assert consumer is not None, "scripts/install.sh no longer derives the version from a bundle tag"
    tag_prefix = consumer.group(1)

    workflow = _read(_RELEASE_WORKFLOW)
    producers = set(re.findall(r'"refs/tags/([A-Za-z0-9-]+)\$\{VERSION\}"', workflow))
    assert producers, "release-unified.yml no longer creates any version tag"
    assert tag_prefix in producers, f"nothing creates a {tag_prefix}<version> tag"

    pinned = [target for target in _redirect_targets(block) if "{query.version}" in target]
    assert len(pinned) == 1
    assert f"/{tag_prefix}{{query.version}}/" in pinned[0]


def test_three_release_tags_carry_the_checked_in_installer() -> None:
    """The three newest bundle tags match the convention and ship scripts/install.sh."""
    listing = subprocess.run(
        [  # noqa: S607 - fixed argv: git on PATH, repository checkout
            "git",
            "tag",
            "--list",
            "bundle-v*",
            "--sort=-v:refname",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if listing.returncode != 0:
        pytest.skip("git is unavailable or this checkout has no tag list")
    tags = [line.strip() for line in listing.stdout.splitlines() if line.strip()]
    if len(tags) < 3:
        pytest.skip("fewer than three bundle-v* tags in this checkout (shallow clone)")

    for tag in tags[:3]:
        assert re.fullmatch(r"bundle-v[0-9]+\.[0-9]+\.[0-9]+", tag)
        present = subprocess.run(  # noqa: S603 - argv is a fixed git command; the tag comes from git tag output
            [  # noqa: S607 - fixed argv: git on PATH, repository checkout
                "git",
                "cat-file",
                "-e",
                f"{tag}:scripts/install.sh",
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert present.returncode == 0, f"{tag} does not ship scripts/install.sh"
