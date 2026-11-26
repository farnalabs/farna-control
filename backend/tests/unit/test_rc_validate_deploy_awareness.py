"""Contract pins for the deploy-aware RC validation path (FAR-2790).

rc-validate used to report every "the RC is not live" outcome as a deployment
SHA mismatch. In FAR-2790 the forced RC3 deploy died in `Pre-deploy gate` (an
integration shard returned HTTP 500s), both deploy legs were skipped, nothing
shipped, and the only visible symptom was fifteen minutes of "deployment SHA
mismatch" - which reads like a wrong endpoint or a stale metadata field rather
than a deploy that never ran.

These tests pin the fix: the optional ``deploy_run_id`` / ``wait_minutes`` input
pair, the ``actions: read`` permission the deploy-run read needs, a wall-clock
deadline, three DISTINCT failure messages (deploy failed / deploy succeeded but
the live SHA disagrees / no deploy run resolvable), the release-side hand-off of
the run id behind a warning fallback, and - as a regression guard on the field
the check trusts - that deploy.yml bakes ``GIT_SHA`` from the commit it
checked out.
"""

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RC_VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rc-validate.yml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_VERIFY_STEP = "Verify live deployment serves the RC commit"
_DISPATCH_STEP = "Dispatch Deploy and rc-validate"

#: The identity deploy.yml checks out for the run it is deploying. The RC
#: deploy passes release_ref/release_sha and this expression resolves to them,
#: so the tree (and therefore the baked GIT_SHA) is the RC commit.
_CANONICAL_DEPLOY_REF = "${{ needs.deploy-throttle.outputs.requested_ref || needs.deploy-throttle.outputs.run_sha }}"

#: One unique phrase per cause. They must land on separate ::error:: lines, so
#: a reader can tell "the deploy did not ship" from "the deploy shipped and the
#: endpoint disagrees" from "rc-validate could not find the deploy at all".
_DEPLOY_FAILED_MARKER = "the deploy itself did not ship"
_MISMATCH_MARKER = "reported success but"
_NO_RUN_MARKER = "No deploy run could be resolved"


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path} must parse to a mapping"
    return loaded


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML resolves the unquoted `on` key to boolean True (YAML 1.1).
    raw = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "workflow must declare at least one trigger"
    return raw


def _rc_validate() -> dict[str, Any]:
    return _load(RC_VALIDATE_WORKFLOW)


def _rc_inputs() -> dict[str, Any]:
    return _triggers(_rc_validate())["workflow_dispatch"]["inputs"]


def _pen_test() -> dict[str, Any]:
    return _rc_validate()["jobs"]["pen-test"]


def _step(workflow: dict[str, Any], job_id: str, step_name: str) -> dict[str, Any]:
    steps = workflow["jobs"][job_id]["steps"]
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, f"expected exactly one {step_name!r} step, found {len(matches)}"
    return dict(matches[0])


def _verify_script() -> str:
    return str(_step(_rc_validate(), "pen-test", _VERIFY_STEP)["run"])


def _dispatch_script() -> str:
    return str(_step(_load(RELEASE_WORKFLOW), "release", _DISPATCH_STEP)["run"])


# ---------------------------------------------------------------------------
# rc-validate inputs and permissions
# ---------------------------------------------------------------------------


def test_rc_validate_declares_an_optional_deploy_run_id_input() -> None:
    inputs = _rc_inputs()

    assert "rc_tag" in inputs
    deploy_run_id = inputs["deploy_run_id"]
    assert deploy_run_id["required"] is False
    assert deploy_run_id["type"] == "string"


def test_rc_validate_wait_minutes_input_defaults_to_90() -> None:
    wait_minutes = _rc_inputs()["wait_minutes"]

    assert wait_minutes["required"] is False
    assert wait_minutes["type"] == "string"
    assert str(wait_minutes["default"]) == "90"


def test_pen_test_job_can_read_the_deploy_run_it_watches() -> None:
    permissions = _pen_test()["permissions"]

    # Reading another workflow run's status and job list needs actions: read.
    assert permissions["actions"] == "read"
    # The job still owns the validated/<rc_tag> marker tag.
    assert permissions["contents"] == "write"


# ---------------------------------------------------------------------------
# Wait budget and the deploy-run cause signal
# ---------------------------------------------------------------------------


def test_wait_deadline_is_driven_by_wait_minutes_not_a_fixed_cap() -> None:
    script = _verify_script()

    assert "WAIT_MINUTES_INPUT" in script
    assert "+ WAIT_MINUTES * 60" in script
    # fail closed on a garbage or out-of-range budget rather than guessing
    assert "-lt 15" in script
    assert "-gt 180" in script
    # the old hard-coded 30 x 30s (15 minute) loop is gone
    assert "seq 1 30" not in script
    assert "after 15 minutes" not in script


def test_the_step_reads_the_deployment_record_it_verifies() -> None:
    script = _verify_script()

    assert "/api/v1/deployment" in script
    # The whole check rests on this field being the baked commit sha.
    assert ".git_sha // empty" in script


def test_a_concluded_failed_deploy_fails_before_the_deadline() -> None:
    script = _verify_script()

    assert 'if [ "$RUN_STATUS" = "completed" ]' in script
    assert "failure|cancelled)" in script
    # The cause branch sits inside the poll loop AHEAD of the deadline check, so
    # a deploy that already died fails on the first poll instead of burning the
    # whole wait budget and then reporting a SHA mismatch.
    assert script.index("failure|cancelled)") < script.index('if [ "$(date -u +%s)" -ge "$DEADLINE" ]')


def test_the_step_can_discover_the_deploy_run_when_none_is_supplied() -> None:
    script = _verify_script()

    assert "DEPLOY_RUN_ID_INPUT" in script
    # FAR-2800: discovery is delegated to the tested selector script, which
    # picks the run by WHAT IT DEPLOYED (its release-identity record) rather
    # than by creation time. The old creation-time heuristic is gone for good.
    assert ".github/scripts/select_rc_deploy_run.py" in script
    assert '--rc-tag "$RC_TAG"' in script
    # A selected run is announced so the wait loop can watch it.
    assert "Watching deploy run" in script
    assert "actions/workflows/deploy.yml/runs" not in script
    assert "/git/ref/tags/" not in script


# ---------------------------------------------------------------------------
# Three distinct failure messages
# ---------------------------------------------------------------------------


def test_the_three_failure_messages_are_distinct() -> None:
    script = _verify_script()
    error_lines = [line for line in script.splitlines() if "::error::" in line]

    deploy_failed = [line for line in error_lines if _DEPLOY_FAILED_MARKER in line]
    mismatch = [line for line in error_lines if _MISMATCH_MARKER in line]
    no_run = [line for line in error_lines if _NO_RUN_MARKER in line]

    assert deploy_failed
    assert mismatch
    assert no_run
    assert len(deploy_failed) == 1
    assert len(mismatch) == 1
    assert len(no_run) == 1
    assert set(deploy_failed).isdisjoint(mismatch)
    assert set(deploy_failed).isdisjoint(no_run)
    assert set(mismatch).isdisjoint(no_run)


def test_the_deploy_failed_message_names_the_run_and_rules_out_a_sha_problem() -> None:
    script = _verify_script()
    deploy_failed_line = next(line for line in script.splitlines() if _DEPLOY_FAILED_MARKER in line)

    assert "::error::" in deploy_failed_line
    assert "${DEPLOY_RUN_ID}" in deploy_failed_line
    assert "${RUN_CONCLUSION}" in deploy_failed_line
    # The whole point of the ticket: this must not read as a wrong SHA.
    assert "NOT a wrong deployment SHA" in deploy_failed_line
    # ... and the jobs that explain a pre-deploy failure are reported.
    for job_name in (
        "Pre-deploy gate",
        "Deploy to staging",
        "Staging E2E (smoke + WCAG + regression)",
        "Deploy to control.farna.dev",
    ):
        assert job_name in script


def test_the_success_mismatch_message_names_the_served_and_expected_sha() -> None:
    script = _verify_script()
    mismatch_line = next(line for line in script.splitlines() if _MISMATCH_MARKER in line)

    assert "::error::" in mismatch_line
    assert "${DEPLOY_RUN_ID}" in mismatch_line
    assert "${LAST_DEPLOYED}" in mismatch_line
    assert "expected ${RC_SHA}" in mismatch_line


def test_the_no_deploy_run_message_keeps_the_wait_and_prints_the_record() -> None:
    script = _verify_script()
    no_run_line = next(line for line in script.splitlines() if _NO_RUN_MARKER in line)

    assert "::error::" in no_run_line
    assert "${WAIT_MINUTES} minutes" in no_run_line
    assert "${LAST_INFO:-<empty>}" in no_run_line


def test_the_verification_names_production_as_the_environment() -> None:
    assert _pen_test()["env"]["BASE_URL"] == "https://control.farna.dev"
    assert "Verifying the RC is live on production (${BASE_URL})" in _verify_script()


# ---------------------------------------------------------------------------
# Release-side hand-off of the deploy run id
# ---------------------------------------------------------------------------


def test_release_resolves_the_deploy_run_and_passes_it_to_rc_validate() -> None:
    dispatch = _dispatch_script()

    assert "gh run list --workflow deploy.yml --event workflow_dispatch" in dispatch
    assert '-f deploy_run_id="${DEPLOY_RUN_ID}"' in dispatch


def test_release_falls_back_to_a_plain_validation_dispatch_with_a_warning() -> None:
    dispatch = _dispatch_script()
    lines = dispatch.splitlines()
    rc_dispatch_lines = [line.strip() for line in lines if "gh workflow run rc-validate.yml" in line]
    with_id = [line for line in rc_dispatch_lines if "deploy_run_id" in line]
    without_id = [line for line in rc_dispatch_lines if "deploy_run_id" not in line]

    assert len(with_id) == 1
    assert len(without_id) == 1
    assert '-f deploy_run_id="${DEPLOY_RUN_ID}"' in with_id[0]

    warning = "::warning::Could not resolve the deploy run id"
    resolve_start = next(i for i, line in enumerate(lines) if 'DEPLOY_RUN_ID=""' in line)
    warning_at = next(i for i, line in enumerate(lines) if warning in line)
    fallback_at = next(i for i, line in enumerate(lines) if line.strip() == without_id[0])
    # resolve -> warn -> fall back, and the resolution never fails the release.
    assert resolve_start < warning_at
    assert warning_at < fallback_at
    assert "exit 1" not in "\n".join(lines[resolve_start:warning_at])


def test_the_deploy_is_still_dispatched_before_validation() -> None:
    dispatch = _dispatch_script()

    assert dispatch.index('gh workflow run "Deploy: Staging + Prod"') < dispatch.index("gh workflow run rc-validate.yml")


# ---------------------------------------------------------------------------
# Regression guard: the deployment field is trustworthy
# ---------------------------------------------------------------------------


def test_deploy_workflow_bakes_git_sha_from_the_checked_out_release_commit() -> None:
    """``/api/v1/deployment`` reports ``git_sha`` baked at image-build time.

    That field only answers "which commit is live" while deploy.yml (a) checks
    out the requested release identity and (b) passes the sha of THAT checkout
    into the build. A future change to a rebuild-sensitive field (the deploy
    run's own ``github.sha``, the dispatch ref) would make every RC validation
    compare the wrong value while still looking green.
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
