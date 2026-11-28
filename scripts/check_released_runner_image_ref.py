#!/usr/bin/env python3
"""Release gate: the shipped Bundled Runner image reference must be a real pin.

``backend/src/farna/db/bundled_runner_template.py`` ships
``BUNDLED_RUNNER_IMAGE_REF``. The committed default is the all-zero
placeholder digest; ``release-unified.yml`` rewrites it in the CI checkout to
the release's ``runner-opencode`` manifest digest before the all-in-one and
backend images are built. This gate runs after that injection and refuses to
let a release publish when the shipped reference is:

* the all-zero placeholder digest (the image does not exist in any registry,
  so every dispatch on a seeded profile fails at container-create); or
* not a ``@sha256:<64 lowercase hex>`` digest pin (a mutable tag is not a pin);
  or
* not the published ``ghcr.io/farnalabs/farna-control/runner-opencode``
  repository (a stale ``modulo-runner:opencode`` name or a different image
  would pull the wrong thing, or nothing).

Exit 0 when the shipped reference is valid, 1 otherwise. The offending value
is always named in the failure message. Runs from the repo root in CI and
locally: ``python scripts/check_released_runner_image_ref.py``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: The published runner image repository the shipped reference must name.
RUNNER_OPENCODE_REPOSITORY = "ghcr.io/farnalabs/farna-control/runner-opencode"

#: The all-zero placeholder digest that must never ship on a release.
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64

#: Repo-relative path of the module that owns ``BUNDLED_RUNNER_IMAGE_REF``.
DEFAULT_TEMPLATE_PATH = Path("backend", "src", "farna", "db", "bundled_runner_template.py")

#: Matches the module-level assignment, capturing the single string literal.
_IMAGE_REF_ASSIGNMENT = re.compile(r'(?m)^BUNDLED_RUNNER_IMAGE_REF\s*=\s*"([^"]*)"')

#: A reference is pinned only when it ends in ``@sha256:<64 lowercase hex>``.
_PINNED_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_repo_root() -> Path:
    """Prefer the git worktree root so the gate works from a linked worktree."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        candidate = Path(result.stdout.strip())
        if (candidate / DEFAULT_TEMPLATE_PATH).is_file():
            return candidate
    return _REPO_ROOT


def extract_image_ref(source: str) -> str:
    """Return the ``BUNDLED_RUNNER_IMAGE_REF`` string literal from ``source``.

    Raises :class:`ValueError` when the assignment is absent or not a single
    string literal, so a refactor that renames or reshapes the constant fails
    the gate loudly instead of silently validating nothing.
    """
    match = _IMAGE_REF_ASSIGNMENT.search(source)
    if match is None:
        raise ValueError("no single-string BUNDLED_RUNNER_IMAGE_REF assignment found")
    return match.group(1)


def released_runner_image_ref_error(image_ref: str) -> str | None:
    """Return a human-readable failure for ``image_ref``, or None when valid."""
    if image_ref.endswith(PLACEHOLDER_DIGEST):
        return f"BUNDLED_RUNNER_IMAGE_REF is the all-zero placeholder digest: {image_ref!r}"
    if not _PINNED_DIGEST.search(image_ref):
        return f"BUNDLED_RUNNER_IMAGE_REF is not a @sha256:<64 hex> digest pin: {image_ref!r}"
    repository = image_ref.split("@", 1)[0]
    if repository != RUNNER_OPENCODE_REPOSITORY:
        return f"BUNDLED_RUNNER_IMAGE_REF does not name the runner-opencode repository ({RUNNER_OPENCODE_REPOSITORY}): {image_ref!r}"
    return None


def read_shipped_image_ref(template_path: Path) -> str:
    """Read ``template_path`` and extract its ``BUNDLED_RUNNER_IMAGE_REF``."""
    return extract_image_ref(template_path.read_text(encoding="utf-8"))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release gate for the shipped Bundled Runner image reference.")
    parser.add_argument(
        "--template-path",
        default=None,
        help="Path to bundled_runner_template.py (default: <repo>/backend/src/farna/db/bundled_runner_template.py).",
    )
    args = parser.parse_args(argv)

    template_path = Path(args.template_path) if args.template_path else _resolve_repo_root() / DEFAULT_TEMPLATE_PATH

    try:
        image_ref = read_shipped_image_ref(template_path)
    except FileNotFoundError:
        print(f"check-released-runner-image-ref: FAILED - template not found: {template_path}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"check-released-runner-image-ref: FAILED - {template_path}: {exc}", file=sys.stderr)
        return 1

    error = released_runner_image_ref_error(image_ref)
    if error is not None:
        print(f"check-released-runner-image-ref: FAILED - {error}", file=sys.stderr)
        return 1

    print(f"check-released-runner-image-ref: OK - shipped reference is {image_ref}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
