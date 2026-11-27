"""Contract pins for the release-side dispatches and the deployed image identity.

Two independent properties this module keeps true:

* **Release dispatch ordering and inputs.** A cut/recut dispatches the forced
  deploy and then the RC validation. FAR-2800 retargeted validation at the RC's
  own disposable stack, so the dispatch hands it nothing but the tag - and it is
  dispatched from ``main`` so the current validation definition runs (every job
  checks out ``inputs.rc_tag`` for the artefact under test). A dispatch from the
  RC tag would run the tag's own, older workflow copy and could never pick up a
  validation-only fix.
* **The deployed image's ``git_sha``.** ``/api/v1/deployment`` reports the sha
  baked at image-build time, so that field only answers "which commit runs here"
  while deploys check out the requested release identity and pass THAT
  checkout's sha into the build.

The live-deployment verification (and its ``deploy_run_id`` / ``wait_minutes``
inputs) is gone: an RC tag is an ancestor of ``main``, so deploying it onto a
main-tracking host is a downgrade that the FAR-2659 alignment guard refuses.
"""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_DISPATCH_STEP = "Dispatch Deploy and rc-validate"

#: The identity deploy.yml checks out for the run it is deploying. The RC
#: deploy passes release_ref/release_sha and this expression resolves to them,
#: so the tree (and therefore the baked GIT_SHA) is the RC commit.
_CANONICAL_DEPLOY_REF = "${{ needs.deploy-throttle.outputs.requested_ref || needs.deploy-throttle.outputs.run_sha }}"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must parse to a mapping"
    return loaded


def _step(workflow: dict[str, Any], job_id: str, step_name: str) -> dict[str, Any]:
    steps = workflow["jobs"][job_id]["steps"]
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, f"expected exactly one {step_name!r} step, found {len(matches)}"
    return dict(matches[0])


def _dispatch_script() -> str:
    return str(_step(_load(RELEASE_WORKFLOW), "release", _DISPATCH_STEP)["run"])


# ---------------------------------------------------------------------------
# Release dispatch: deploy first, then validation - with nothing but the tag
# ---------------------------------------------------------------------------


def test_the_deploy_is_still_dispatched_before_validation() -> None:
    dispatch = _dispatch_script()

    assert dispatch.index('gh workflow run "Deploy: Staging + Prod"') < dispatch.index("gh workflow run rc-validate.yml")


def test_release_dispatches_validation_from_main_with_only_the_rc_tag() -> None:
    dispatch = _dispatch_script()
    rc_dispatch_lines = [line.strip() for line in dispatch.splitlines() if "gh workflow run rc-validate.yml" in line]

    # Exactly one dispatch site: the deploy-run resolution and its two-way
    # fallback are gone with the live-deployment verification they fed.
    assert len(rc_dispatch_lines) == 1
    assert rc_dispatch_lines[0] == 'gh workflow run rc-validate.yml --ref main -f rc_tag="${RC_TAG}"'
    # The tag ref would run the RC's own older workflow copy.
    assert '--ref "${RC_TAG}"' not in rc_dispatch_lines[0]
    # FAR-2800: validation boots its own stack, so it observes no deploy run.
    assert "deploy_run_id" not in dispatch
    assert "gh run list --workflow deploy.yml" not in dispatch


# ---------------------------------------------------------------------------
# Regression guard: the deployment field is trustworthy
# ---------------------------------------------------------------------------


def test_deploy_workflow_bakes_git_sha_from_the_checked_out_release_commit() -> None:
    """``/api/v1/deployment`` reports ``git_sha`` baked at image-build time.

    That field only answers "which commit is live" while deploy.yml (a) checks
    out the requested release identity and (b) passes the sha of THAT checkout
    into the build. A future change to a rebuild-sensitive field (the deploy
    run's own ``github.sha``, the dispatch ref) would make every deploy-identity
    read compare the wrong value while still looking green.
    """
    workflow = _load(DEPLOY_WORKFLOW)
    checkouts = {job_id: [step for step in job.get("steps", []) if str(step.get("uses", "")).startswith("actions/checkout")] for job_id, job in workflow["jobs"].items()}

    for leg in ("deploy-staging", "deploy-production"):
        leg_checkouts = checkouts[leg]
        assert len(leg_checkouts) == 1
        assert leg_checkouts[0]["with"]["ref"] == _CANONICAL_DEPLOY_REF

    # No checkout anywhere may pin a different commit: an explicit ref that is
    # not the run's requested identity would build the wrong tree. Jobs that
    # carry no explicit ref (the failure notifiers) are left alone.
    for job_id, job_checkouts in checkouts.items():
        for checkout in job_checkouts:
            pinned = checkout.get("with", {}).get("ref")
            if pinned is None:
                continue
            assert pinned == _CANONICAL_DEPLOY_REF, f"{job_id} checks out {pinned}"


def test_deploy_workflow_passes_that_checkouts_sha_into_the_image_build() -> None:
    workflow = _load(DEPLOY_WORKFLOW)

    for leg in ("deploy-staging", "deploy-production"):
        steps = workflow["jobs"][leg]["steps"]
        captured = [step for step in steps if step.get("uses") == "./.github/actions/capture-git-metadata"]
        assert len(captured) == 1
        assert captured[0]["id"] == "git"

        builds = [str(step.get("run", "")) for step in steps if "--build-only" in str(step.get("run", ""))]
        assert len(builds) == 1
        assert '--build-arg "GIT_SHA=${{ steps.git.outputs.git-sha }}"' in builds[0]
