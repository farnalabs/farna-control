"""Structural gate: the FAR-954 throttle-status annotation job must survive edits.

CI does not exercise ``.github/workflows/deploy.yml`` itself (it only runs on
push to main), so this test is the only gate that keeps a throttled no-op run
visibly distinct from a real deployment. The sibling
``test_deploy_workflow_rehearsal.py`` exists for the same reason.

FAR-954: a throttled deploy run concludes SUCCESS without deploying anything,
so the run must carry a loud summary annotation. ``deploy-throttle-status``
reads ``needs.deploy-throttle.outputs.should_deploy`` and writes the
throttled/deploying distinction into the step summary.

Legibility follow-up (throttle skip visibility): the run additionally carries
(1) a verdict summary written by the ``deploy-throttle`` job itself, including
WHY it skipped and WHEN the next deploy is expected (``skip_reason`` /
``next_deploy`` outputs), (2) a dynamic job display name so the skip is
visible in the run's job list without opening a log, and (3) a ``::warning::``
(not ``::notice::``) annotation on the skip path.
"""

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_JOB_ID = "deploy-throttle-status"
_SUMMARY_STEP_NAME = "Write throttle/deploy summary annotation"
_SHOULD_DEPLOY = "${{ needs.deploy-throttle.outputs.should_deploy }}"


def _workflow() -> dict:
    return yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))


def _summary_step() -> dict:
    job = _workflow()["jobs"][_JOB_ID]
    steps = job["steps"]
    names = [step.get("name", "") for step in steps]
    return steps[names.index(_SUMMARY_STEP_NAME)]


def _throttle_check_step() -> dict:
    job = _workflow()["jobs"]["deploy-throttle"]
    steps = job["steps"]
    ids = [step.get("id", "") for step in steps]
    return steps[ids.index("check")]


def test_throttle_status_job_exists_and_always_runs() -> None:
    job = _workflow()["jobs"][_JOB_ID]
    assert job["if"] == "always()", f"{_JOB_ID}: must run even when the throttle skips every deploy job"
    assert "deploy-throttle" in job["needs"], f"{_JOB_ID}: must depend on deploy-throttle to read should_deploy"


def test_throttle_status_keys_off_should_deploy() -> None:
    step = _summary_step()
    assert step["env"]["SHOULD_DEPLOY"] == _SHOULD_DEPLOY, f"{_JOB_ID}: annotation must key off needs.deploy-throttle.outputs.should_deploy"
    script = step["run"]
    assert 'if [ "$SHOULD_DEPLOY" = "true" ]' in script
    assert "THROTTLED" in script, "the throttled branch must label the run unmistakably"
    assert "NO DEPLOYMENT" in script


def test_deployed_summary_interpolates_short_sha_without_sed_patch() -> None:
    """The deployed branch must not rely on a post-hoc ``sed`` placeholder patch.

    FAR-954 review: the summary is appended into ``$GITHUB_STEP_SUMMARY``; a
    quoted heredoc plus a ``sed`` pass is fragile (a placeholder that the regex
    fails to match renders verbatim). The computed short SHA must be
    interpolated by the shell itself, so the heredoc must stay unquoted and the
    ``sed`` cleanup pass must not return.
    """
    script = _summary_step()["run"]
    assert "<<'EOF'" not in script, "deployed summary must use an unquoted heredoc so ${SHORT_SHA} expands"
    assert "sed -i" not in script, "drop the sed placeholder patch and write the value directly"
    assert "**Deploying SHA:** ${SHORT_SHA}" in script, "deployed summary must interpolate the computed short SHA into the heredoc"


def test_throttle_job_publishes_skip_reason_and_next_deploy() -> None:
    """The throttle job must tell readers WHY it skipped and WHEN the next deploy is."""
    workflow = _workflow()
    outputs = workflow["jobs"]["deploy-throttle"]["outputs"]
    assert "skip_reason" in outputs, "deploy-throttle must export skip_reason for the status job"
    assert "next_deploy" in outputs, "deploy-throttle must export next_deploy for the status job"

    check_step = _throttle_check_step()
    script = check_step["run"]
    assert "$GITHUB_STEP_SUMMARY" in script, "the throttle job itself must write a verdict job summary"
    assert "skip_reason=in_progress" in script, "the in-progress skip must record its reason"
    assert "skip_reason=interval_throttled" in script, "the interval skip must record its reason"
    assert "next_deploy=" in script, "every skip/deploy branch must record when the next deploy is expected"
    # The next-eligibility timestamp must derive from the SAME anchor the skip
    # used - never from `now` - or the summary can disagree with the throttle.
    assert "NEXT_TS=$((LAST_TS + INTERVAL_HOURS * 3600))" in script, "next-deploy estimate must be anchor + interval, computed from the skip's own anchor"
    # Annotations must state the skip explicitly, not just 'skipping deploy'.
    assert "This run will NOT deploy" in script


def test_status_job_display_name_reflects_throttle_outcome() -> None:
    """The job's display name must be dynamic so the skip shows in the job list.

    A run's name (``run-name``) is fixed at run creation - before the throttle
    decides - and GitHub has no distinct terminal run state we can set without
    failing the run, so the dynamic job name is the at-a-glance signal.
    """
    name = _workflow()["jobs"][_JOB_ID]["name"]
    assert "needs.deploy-throttle.outputs.should_deploy" in name, "status job name must key off should_deploy so THROTTLED - NO DEPLOY is visible at a glance"
    assert "THROTTLED - NO DEPLOY" in name
    assert "DEPLOYING" in name


def test_status_skip_path_uses_warning_annotation_with_reason_and_next_deploy() -> None:
    step = _summary_step()
    assert step["env"]["SKIP_REASON"] == "${{ needs.deploy-throttle.outputs.skip_reason }}", "status job must consume the throttle job's skip_reason, not re-derive the cause"
    assert step["env"]["NEXT_DEPLOY"] == "${{ needs.deploy-throttle.outputs.next_deploy }}", "status job must consume the throttle job's next_deploy, not re-derive the time"
    script = step["run"]
    assert '"::warning::THROTTLED' in script, "the skip must be a warning-level annotation so it is not lost among notices"
    assert "Next deploy expected:" in script, "the skip summary must say when the next deploy is expected"
    assert "Another deploy run is already in progress" in script, "in-progress batching must be named as the reason"
    assert "under the throttle interval" in script, "interval throttling must be named as the reason"
    # force_deploy must still deploy (and say so) - the bypass must survive.
    assert "force_deploy=true" in script


def test_force_deploy_bypass_still_deploys() -> None:
    """force_deploy short-circuits the throttle before any check or Actions read.

    The force branch is evaluated first: ahead of the in-progress check, the
    commit-fix bypass and the throttle-anchor scan (which makes 1 + up to 50
    Actions API reads). The emergency bypass must never wait on the Actions API,
    so this ordering is pinned; an edit that hoists the anchor scan back above
    force re-opens a hang on a slow Actions API (FAR-954 review finding 1).
    """
    script = _throttle_check_step()["run"]
    force_idx = script.index('if [ "$FORCE_DEPLOY" = "true" ]')
    in_progress_idx = script.index('if [ -n "$IN_PROGRESS" ]')
    assert force_idx < in_progress_idx, "force_deploy must exit before the in-progress/throttle checks - the bypass order is the policy"
    anchor_idx = script.index("gh run list")
    assert force_idx < anchor_idx, "force_deploy must exit before the anchor's Actions reads - the emergency bypass must never wait on the Actions API"
    # The bypass must publish should_deploy=true to $GITHUB_OUTPUT. The write
    # may be a single individually-redirected echo, or the writes may be
    # gathered into one redirected group (actionlint SC2129) - both emit the
    # identical key/value, so scope the check to the force branch and accept
    # either form rather than pinning the exact redirect layout.
    force_branch = script[force_idx:in_progress_idx]
    assert "should_deploy=true" in force_branch, "the force_deploy bypass must publish should_deploy=true"
    assert '"$GITHUB_OUTPUT"' in force_branch, "the force_deploy bypass must write the output to $GITHUB_OUTPUT"
    assert "Force deploy requested" in force_branch
    # The force path still emits the legibility output so the summary reports it
    # as unknown rather than fabricating an anchor.
    assert "last_deploy=${LAST}" in force_branch


def test_last_deploy_is_declared_and_emitted_on_every_branch() -> None:
    """Every branch that publishes should_deploy must also publish last_deploy.

    FAR-954 hoisted the anchor observation so every path can report WHEN the
    last real deploy was. The status job consumes
    ``needs.deploy-throttle.outputs.last_deploy``; a missing declaration or a
    branch that forgets to emit it silently degrades the summary to "unknown"
    (force_deploy is the one path that deliberately reports unknown, but still
    emits the key). Pin the declaration and one emission per branch.
    """
    outputs = _workflow()["jobs"]["deploy-throttle"]["outputs"]
    assert "last_deploy" in outputs, "deploy-throttle must export last_deploy for the status job"
    assert outputs["last_deploy"] == "${{ steps.check.outputs.last_deploy }}", "last_deploy must pass through the check step's output"

    script = _throttle_check_step()["run"]
    should_writes = script.count('echo "should_deploy=')
    last_writes = script.count('echo "last_deploy=${LAST}"')
    assert should_writes > 0, "the throttle step must still write should_deploy"
    assert last_writes == should_writes, f"every should_deploy branch must emit last_deploy too: {should_writes} should_deploy write(s) vs {last_writes} last_deploy write(s)"


def test_status_summary_renders_last_deploy_and_prod_main_gap() -> None:
    """The status summary must consume last_deploy and render every gap state.

    FAR-954 legibility added (1) the last real-deploy line, (2) the
    production-vs-origin/main gap with a branch per readable/unreadable state,
    and (3) the explicit verdict lines. Nothing else gates these strings, so
    assert them here; a future edit that drops a branch stays red.
    """
    step = _summary_step()
    env = step["env"]
    assert env["LAST_DEPLOY"] == "${{ needs.deploy-throttle.outputs.last_deploy }}", "summary must consume the throttle's last_deploy, not re-derive it"
    for key in ("PROD_SHA", "MAIN_SHA", "COMMITS_BEHIND"):
        assert key in env, f"summary must consume {key} from the read-only state step"

    script = step["run"]
    # Last real deploy: passed through, rendered as unknown only when absent.
    assert "**Last real deploy:**" in script
    assert "unknown on this path" in script
    # All five prod-vs-main gap renderings.
    assert "current with origin/main" in script, "prod == origin/main branch must survive"
    assert "commit(s) ahead" in script, "prod-behind-origin/main branch must survive"
    assert "gap unknown: compare API unavailable" in script, "compare-API-unavailable branch must survive"
    assert "origin/main unreadable" in script, "main-unreadable branch must survive"
    assert "unreadable - origin/main is" in script, "prod-unreadable branch must survive"
    # The three verdict lines a reader scans for.
    assert "DEPLOY VERDICT: YES - this run is deploying" in script
    assert "DEPLOY VERDICT: THROTTLE CHECK FAILED" in script
    assert "DEPLOY VERDICT: NO DEPLOY — throttled (FAR-954)." in script
