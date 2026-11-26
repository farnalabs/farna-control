"""FAR-2794: the GA compose publication is idempotent, ordered and verified.

The ``promote-compose`` job previously ran a plain ``gh release upload`` to the
GA ``v<VERSION>`` release, so a job-level retry failed when an asset already
existed and the operator was told to repair it by hand. The publish step now
uploads with ``--clobber`` in a fixed order (compose tarball, SHA256SUMS, then
the manifest, then its signature LAST) and a verification step proves every
published asset is present at its local byte size and that the published
manifest + signature verify against the checked-in trust store
(``modulo-2026-a``). The GA release stays public and is never deleted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"

PROMOTE_JOB = "promote-compose"
# The title states what the step does: the publication below is idempotent
# (--clobber on a retry) and a verification step follows it. The release
# rehearsal contract (test_deploy_workflow_rehearsal.py) keys on this exact name.
UPLOAD_STEP = "Publish the GA compose assets (idempotent, verified)"
VERIFY_STEP = "Verify the published GA compose assets"

_COMPOSE = "farna-control-${VERSION}-compose.tar.gz"
_SHA256SUMS = "release-artefacts/SHA256SUMS"
_MANIFEST = 'release-artefacts/RELEASE_MANIFEST.json"'
_SIGNATURE = 'release-artefacts/RELEASE_MANIFEST.json.sig"'


def _steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    return list(workflow["jobs"][PROMOTE_JOB]["steps"])


def _step_by_name(name: str) -> dict[str, Any]:
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step, found {len(matches)}"
    run = matches[0].get("run")
    assert isinstance(run, str), f"step {name!r} has no run script"
    return matches[0]


def _upload_violations(run: str) -> list[str]:
    problems: list[str] = []
    needles = {
        "compose tarball": _COMPOSE,
        "SHA256SUMS": _SHA256SUMS,
        "manifest": _MANIFEST,
        "signature": _SIGNATURE,
    }
    missing = [label for label, needle in needles.items() if needle not in run]
    for label in missing:
        problems.append(f"does not publish the {label}")
    if not missing:
        order = [run.index(needles[label]) for label in ("compose tarball", "SHA256SUMS", "manifest", "signature")]
        if not order[0] < order[1]:
            problems.append("does not publish the compose tarball before SHA256SUMS")
        if not order[1] < order[2]:
            problems.append("does not publish SHA256SUMS before the manifest")
        if not order[2] < order[3]:
            problems.append("does not publish the manifest before its signature (signature must be last)")

    upload_lines = [line for line in run.splitlines() if "gh release upload" in line]
    if not upload_lines:
        problems.append("has no gh release upload invocation")
    for line in upload_lines:
        if "--clobber" not in line:
            problems.append("uploads without --clobber, so a job retry fails on an existing asset")

    if "$RUNNER_TEMP" not in run:
        problems.append("does not snapshot the expected-asset inventory into RUNNER_TEMP")
    if "gh release view" not in run:
        problems.append("does not read the GA release back before uploading")
    if "gh release delete" in run:
        problems.append("deletes the GA release")
    if "by hand" in run.lower():
        problems.append("still instructs an operator to repair by hand instead of converging")
    return problems


def _verify_violations(run: str) -> list[str]:
    problems: list[str] = []
    if "verify_release_from_files" not in run:
        problems.append("does not verify the published manifest with verify_release_from_files")
    if "_KEY_ID_CURRENT" not in run:
        problems.append("does not derive the expected key id from the trust store (_KEY_ID_CURRENT)")
    if "gh release download" not in run:
        problems.append("does not download the published manifest/signature bytes to verify")
    if "gh release view" not in run:
        problems.append("does not read the release's own asset inventory")
    if "--json assets" not in run:
        problems.append("does not read asset sizes from gh release view --json assets")
    if "$RUNNER_TEMP" not in run:
        problems.append("does not read the expected-asset inventory back from RUNNER_TEMP")
    if "::error::" not in run:
        problems.append("does not fail loudly with ::error::")
    return problems


def test_promote_compose_publishes_assets_idempotently_and_ordered() -> None:
    problems = _upload_violations(_step_by_name(UPLOAD_STEP)["run"])
    assert not problems, "; ".join(problems)


def test_promote_compose_verifies_the_published_assets() -> None:
    problems = _verify_violations(_step_by_name(VERIFY_STEP)["run"])
    assert not problems, "; ".join(problems)


def test_upload_step_precedes_the_verification_step() -> None:
    names = [step.get("name") for step in _steps()]
    assert UPLOAD_STEP in names
    assert VERIFY_STEP in names
    assert names.index(UPLOAD_STEP) < names.index(VERIFY_STEP)


def test_promote_job_keeps_the_release_public_and_never_deletes_it() -> None:
    runs = "\n".join(step.get("run", "") for step in _steps() if isinstance(step.get("run"), str))
    assert "gh release delete" not in runs
    assert "--draft" not in runs
    assert "gh release edit" not in runs
