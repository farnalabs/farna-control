"""Contract gate for FAR-2800: rc-validate selects the deploy run by property.

CI never executes ``rc-validate.yml`` on a pull request (it is dispatched during
a release), so these assertions are the only thing that keeps the fix in place.

What is pinned:

* the recency heuristic is GONE - no tag-creation timestamp, no
  ``sort_by(.created_at)`` over deploy runs;
* the selector script is the discovery path, and the caller-supplied
  ``deploy_run_id`` still takes precedence over it;
* ``deploy.yml`` records the identity the selector reads, gated on the
  machine-readable ``deployed`` signal and a non-empty ``release_ref``;
* the artifact-name prefix and the production-deploy job name agree between the
  workflow and the selector (drift between the two would silently select
  nothing);
* the hard deployment-identity check and the Schemathesis step are untouched.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RC_VALIDATE = REPO_ROOT / ".github" / "workflows" / "rc-validate.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
SELECTOR_SCRIPT = REPO_ROOT / ".github" / "scripts" / "select_rc_deploy_run.py"

_loader = SourceFileLoader("select_rc_deploy_run_contract", str(SELECTOR_SCRIPT))
selector = module_from_spec(spec_from_loader("select_rc_deploy_run_contract", _loader))
sys.modules["select_rc_deploy_run_contract"] = selector
_loader.exec_module(selector)

_PEN_TEST_JOB = "pen-test"
_IDENTITY_STEP = "Verify live deployment serves the RC commit"
_FUZZ_STEP = "Schemathesis fuzz against live RC"
_RECORD_JOB = "record-release-identity"


def _workflow(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{path.name} is not a mapping"
    return loaded


def _step(workflow: dict[str, Any], job_id: str, step_name: str) -> dict[str, Any]:
    steps = (workflow["jobs"][job_id] or {}).get("steps") or []
    names = [step.get("name", "") for step in steps]
    assert step_name in names, f"{job_id}: no step named {step_name!r}"
    return steps[names.index(step_name)]


# ---------------------------------------------------------------------------
# rc-validate: the recency heuristic is gone
# ---------------------------------------------------------------------------


def test_recency_discovery_is_gone() -> None:
    text = RC_VALIDATE.read_text(encoding="utf-8")
    assert "sort_by(.created_at)" not in text, "the recency selection over deploy runs must stay deleted"
    assert "rc_tag_created_at" not in text, "the tag-creation-timestamp helper must stay deleted"
    assert "actions/workflows/deploy.yml/runs?event=workflow_dispatch" not in text, "rc-validate must never enumerate deploy runs by creation time again"


def test_selector_script_is_the_discovery_path() -> None:
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    assert ".github/scripts/select_rc_deploy_run.py" in script
    assert "--rc-tag" in script
    assert "--github-output" in script
    # A missing selector must fail loudly, never fall back to a heuristic.
    assert "is missing from this checkout" in script


def test_caller_supplied_run_id_still_wins_over_discovery() -> None:
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    discovery_guard = 'if [ -z "$DEPLOY_RUN_ID" ]; then'
    assert discovery_guard in script, "discovery must only run when the caller did not pass deploy_run_id"
    assert "supplied by the caller" in script


def test_the_three_no_run_diagnoses_read_differently() -> None:
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    assert "not_deployed)" in script
    assert "unknown)" in script
    assert "has not been deployed: no deploy.yml run records shipping it" in script
    assert "NOT evidence that the RC was never deployed" in script


def test_deploy_failed_fails_fast_with_a_named_cause() -> None:
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    assert 'DEPLOY_VERDICT" = "deploy_failed"' in script
    assert "finished without a successful production deploy" in script


def test_resolution_is_re_polled_so_a_late_record_is_not_missed() -> None:
    """The identity record is written after the deploy run's throttle job.

    Resolving once at step start would turn "the RC has not been deployed" into
    a race against a deploy dispatched moments earlier, so resolution must be
    re-runnable and called again inside the wait loop.
    """
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    assert "resolve_deploy_run() {" in script
    # Called from inside the loop while nothing is selected, not only up front.
    loop_idx = script.index("while :; do")
    assert "resolve_deploy_run" in script[loop_idx:], "the loop must re-resolve while nothing is selected"


def test_deploy_failed_waits_for_a_settle_window_before_failing() -> None:
    """A deploy dispatched seconds ago has no record yet - do not name it failed."""
    script = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)["run"]
    assert "FAILED_POLLS=${FAILED_POLLS + 1}" in script or "FAILED_POLLS=$((FAILED_POLLS + 1))" in script
    assert '[ "$FAILED_POLLS" -ge 2 ]' in script


# ---------------------------------------------------------------------------
# rc-validate: the hard identity check and the fuzz step stay hard
# ---------------------------------------------------------------------------


def test_the_commit_identity_check_stays_hard() -> None:
    """Refusing to fuzz a deployment serving another commit is correct."""
    step = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _IDENTITY_STEP)
    script = step["run"]
    assert "/api/v1/deployment" in script
    assert ".git_sha" in script
    assert 'RC_SHA=$(git rev-parse "${RC_TAG}^{commit}")' in script
    # The mismatch path must still be a hard failure.
    assert "reported success but ${BASE_URL} serves" in script
    assert "exit 1" in script
    assert "continue-on-error" not in step


def test_the_fuzzing_step_cannot_be_skipped() -> None:
    step = _step(_workflow(RC_VALIDATE), _PEN_TEST_JOB, _FUZZ_STEP)
    assert "schemathesis run" in step["run"]
    assert "if" not in step, "the fuzz step must not gain a condition that can skip it"
    assert "continue-on-error" not in step


# ---------------------------------------------------------------------------
# deploy.yml: the identity record the selector reads
# ---------------------------------------------------------------------------


def test_deploy_records_the_release_identity_it_is_about_to_deploy() -> None:
    job = _workflow(DEPLOY_WORKFLOW)["jobs"][_RECORD_JOB]
    condition = str(job.get("if") or "")
    assert "needs.deploy-throttle.outputs.deployed == 'true'" in condition, "a throttled run deploys nothing and must never record an identity"
    assert "needs.deploy-throttle.outputs.requested_ref != ''" in condition, "a main deploy has no release_ref and must never record an identity"
    assert "deploy-throttle" in (job.get("needs") or [])


def test_the_recorded_artifact_name_matches_the_selector_prefix() -> None:
    job = _workflow(DEPLOY_WORKFLOW)["jobs"][_RECORD_JOB]
    upload = job["steps"][-1]
    name = str(upload.get("with", {}).get("name") or "")
    assert name == "${{ steps.identity.outputs.name }}"
    # The name is computed from the selector's prefix plus the throttle's ref.
    producer = job["steps"][0]["run"]
    assert selector.ARTIFACT_NAME_PREFIX in producer, "deploy.yml must build the artifact name with the selector's prefix"
    assert "release-identity-${REQUESTED_REF}" in producer
    assert selector.ARTIFACT_NAME_PREFIX == "release-identity-"


def test_the_selector_watches_the_same_production_job_name() -> None:
    """The selector's "did this run ship?" signal must be the real job name."""
    production = _workflow(DEPLOY_WORKFLOW)["jobs"]["deploy-production"]
    assert production["name"] == selector.DEPLOY_JOB_NAME
