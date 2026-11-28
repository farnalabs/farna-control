"""Contract gate for the retargeted RC validation fuzz leg (FAR-2800).

CI never executes ``rc-validate.yml`` on a pull request (it is dispatched during
a release), so these assertions are the only thing that keeps the leg's shape in
place.

What is pinned:

* the leg fuzzes the RC's OWN disposable stack - no production host is named
  anywhere in the workflow, and no deployment-only secret is required;
* the artefact under test is the immutable RC tag (never ``latest``);
* the stack is booted the same way the other acceptance legs boot theirs (the
  packaged release bundle plus the shipped ``init-env.sh``), waits on the app's
  own ``/healthz/ready`` with a bounded budget, and a boot failure reports the
  container logs rather than a bare timeout;
* account, organisation and credential are bootstrapped from the stack itself
  through the shipped smoke path - never from a production credential;
* the sweep runs TWICE, as two matrix entries on their own stacks: an
  unauthenticated pass that presents no credential and an authenticated pass that
  presents the seeded token on every request (FAR-2800's policy);
* the status policy itself lives in ``scripts/rc_fuzz_policy.py`` and is applied
  by the ``SCHEMATHESIS_HOOKS`` extension, replacing the built-in
  ``status_code_conformance`` check;
* the fuzz step is blocking (no ``if:``, no ``continue-on-error``) and bounded;
* teardown is ``if: always()`` and runs last, with the logs and the Schemathesis
  report scrubbed of the credential, then uploaded as artefacts;
* the deploy-identity machinery merged for the wrong premise (FAR-2800's
  selector, its deploy.yml identity job and its ``deploy_run_id`` input) is gone,
  and nothing references it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RC_VALIDATE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "rc-validate.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
POLICY_MODULE = REPO_ROOT / "scripts" / "rc_fuzz_policy.py"
CHECKS_MODULE = REPO_ROOT / "scripts" / "rc_fuzz_checks.py"

_FUZZ_JOB = "rc-fuzz"
_MARKER_JOB = "rc-validated"
_FUZZ_STEP = "Schemathesis fuzz against the RC's own stack"
_READY_STEP = "Wait for the stack to report ready"
_BOOTSTRAP_STEP = "Bootstrap the fuzz account, organisation and credential"
_BOOT_STEP = "Start the RC's own disposable stack"
_PACKAGE_STEP = "Package the exact RC compose bundle"
_SCRUB_STEP = "Scrub the credential out of the evidence"
_UPLOAD_STEP = "Upload the fuzz report and stack logs"
_TEARDOWN_STEP = "Tear down the disposable stack"

_REMOVED_FILES = (
    ".github/scripts/select_rc_deploy_run.py",
    "backend/tests/unit/scripts/test_select_rc_deploy_run.py",
    "backend/tests/unit/test_rc_validate_deploy_selection.py",
)

#: Tokens that existed only for the removed deploy-identity machinery.
_REMOVED_TOKENS = (
    "select_rc_deploy_run",
    "release-identity",
    "record-release-identity",
    "deploy_run_id",
    "RC_PENTEST",
)

#: The removed dispatch input. Scoped to workflow files: ``wait_minutes`` is
#: also an unrelated local variable in an installer test.
_REMOVED_DISPATCH_INPUTS = ("wait_minutes",)

#: These two contract modules must NAME the removed tokens to assert their
#: absence, so they are never part of their own reference scan.
_SCAN_EXCLUDED = frozenset(
    {
        "backend/tests/unit/test_rc_validate_fuzz_leg.py",
        "backend/tests/unit/test_release_dispatch_and_deploy_identity.py",
    }
)


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RC_VALIDATE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "rc-validate.yml must parse to a mapping"
    return loaded


def _job(job_id: str) -> dict[str, Any]:
    job = _workflow()["jobs"][job_id]
    assert isinstance(job, dict)
    return job


def _fuzz_job() -> dict[str, Any]:
    return _job(_FUZZ_JOB)


def _steps() -> list[dict[str, Any]]:
    steps = _fuzz_job().get("steps") or []
    assert isinstance(steps, list)
    return steps


def _step(step_name: str) -> dict[str, Any]:
    matches = [step for step in _steps() if step.get("name") == step_name]
    assert len(matches) == 1, f"expected exactly one {step_name!r} step, found {len(matches)}"
    return dict(matches[0])


def _run(step_name: str) -> str:
    return str(_step(step_name).get("run") or "")


def _job_text() -> str:
    """The fuzz job's own YAML text, so a token check cannot match a sibling job."""
    text = RC_VALIDATE_WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"\n  {_FUZZ_JOB}:")
    return text[start:]


# ---------------------------------------------------------------------------
# The leg targets the RC's own stack, not a deployed host
# ---------------------------------------------------------------------------


def test_the_workflow_names_no_production_host() -> None:
    text = RC_VALIDATE_WORKFLOW.read_text(encoding="utf-8")

    assert "control.farna.dev" not in text, "the fuzz leg must not depend on a deployed environment"
    assert "RC_PENTEST_USERNAME" not in text, "the leg must not need a deployment-only credential secret"
    assert "RC_PENTEST_PASSWORD" not in text
    assert "secrets." not in _job_text(), "the fuzz leg must bootstrap every credential it needs on the runner"
    assert f"\n  {_FUZZ_JOB}:" in text


def test_the_leg_still_runs_after_both_acceptance_legs() -> None:
    needs = _fuzz_job().get("needs")

    assert needs == ["clean-host-compose", "docker-upgrade-rollback"]


def test_the_fuzz_target_is_the_local_stack() -> None:
    script = _run(_FUZZ_STEP)

    assert "schemathesis run" in script
    # The loopback target, never a deployed host.
    assert '--url "http://127.0.0.1:${PORT}"' in script
    # The app's public surface does not serve the schema, so it is read from the
    # container's own loopback and written into the evidence directory.
    assert 'docker compose -f docker-compose.prod.yml exec -T "$APP_SERVICE"' in script
    assert '"http://127.0.0.1:8000/openapi.json"' in script
    assert 'schemathesis run "$evidence_dir/schemathesis/openapi.json"' in script
    assert "if not paths:" in script
    # The other built-in checks are unchanged; the example budget is still bounded
    # by measurement (a full GET sweep runs ~3358 cases in ~16 minutes at 5
    # examples per operation).
    assert "--include-method GET" in script
    assert "--checks all" in script
    assert "--max-examples 5" in script
    # The report is an artefact even when the checks fail. These are the report
    # formats Schemathesis 4.25.2 actually offers (junit + the NDJSON event
    # stream); 3.x spellings like --base-url/--method/--report-json-path do not
    # exist in it and would exit 2 with "No such option".
    assert "--report junit,ndjson" in script
    assert '--report-junit-path "$evidence_dir/schemathesis/junit.xml"' in script
    assert '--report-ndjson-path "$evidence_dir/schemathesis/events.ndjson"' in script


def test_the_target_image_comes_from_the_rc_tag_and_never_latest() -> None:
    script = _run(_PACKAGE_STEP)
    job_text = _job_text()

    assert '--tag "$RC_TAG"' in script
    assert '--version "$version"' in script
    assert 'grep -F "image: ghcr.io/farnalabs/farna-control:${RC_TAG}"' in script
    # No floating reference: the image reference always carries the RC tag.
    assert "farna-control:latest" not in job_text
    assert "farna-control:stable" not in job_text
    assert "TAG=latest" not in job_text
    # The image is pulled by tag from this RC's bundle, never rebuilt locally.
    assert "docker build" not in job_text


# ---------------------------------------------------------------------------
# Two passes, two stacks: the policy and its inputs
# ---------------------------------------------------------------------------


def test_the_sweep_runs_as_two_passes_on_their_own_stacks() -> None:
    matrix = _fuzz_job()["strategy"]["matrix"]["include"]
    env = _fuzz_job()["env"]

    passes = [entry["pass"] for entry in matrix]
    assert passes == ["unauthenticated", "authenticated"]
    # Distinct loopback ports, and a compose project per pass: neither pass can
    # reach the other's stack, so the authenticated pass' state mutations cannot
    # affect anything else.
    ports = [entry["port"] for entry in matrix]
    assert len(set(ports)) == len(ports)
    assert "${{ matrix.pass }}" in env["COMPOSE_PROJECT_NAME"]
    assert env["RC_FUZZ_PASS"] == "${{ matrix.pass }}"
    # Both passes must report even if the first fails.
    assert _fuzz_job()["strategy"]["fail-fast"] is False


def test_only_the_authenticated_pass_presents_the_credential() -> None:
    script = _run(_FUZZ_STEP)

    # The header is added conditionally on the pass, never unconditionally (that
    # would make the unauthenticated pass meaningless) and never from a file the
    # second pass could forget to read.
    assert 'if [ "$RC_FUZZ_PASS" = "authenticated" ]; then' in script
    assert 'auth_args+=(--header "Authorization: Bearer ${FUZZ_JWT}")' in script
    assert '"${auth_args[@]}"' in script


def test_the_status_policy_lives_in_one_module_and_replaces_the_builtin_check() -> None:
    script = _run(_FUZZ_STEP)

    assert POLICY_MODULE.is_file(), "the status policy module must exist"
    assert CHECKS_MODULE.is_file(), "the Schemathesis extension must exist"
    # Loaded through the documented extension hook, from the harness checkout (the
    # policy belongs to the validator, not to the artefact under test).
    assert 'export SCHEMATHESIS_HOOKS="$GITHUB_WORKSPACE/rc-harness/scripts/rc_fuzz_checks.py"' in script
    # The built-in status check is replaced, not merely supplemented: it is the one
    # that counted a correct 401/402 refusal as a defect.
    assert "--exclude-checks status_code_conformance" in script
    # Per-pass artefacts, written by the extension at interpreter exit.
    assert 'export RC_FUZZ_REPORT_PATH="$evidence_dir/schemathesis/policy-report.txt"' in script
    assert 'export RC_FUZZ_FINDINGS_JSON="$evidence_dir/schemathesis/findings.json"' in script
    # A run that died before writing them produced no verdict and must not pass.
    assert 'test -s "$evidence_dir/schemathesis/policy-report.txt"' in script
    assert 'test -s "$evidence_dir/schemathesis/findings.json"' in script


def test_the_sweep_is_bounded_so_two_passes_fit() -> None:
    script = _run(_FUZZ_STEP)
    step = _step(_FUZZ_STEP)
    job = _fuzz_job()

    # A per-step budget AND a job budget; two passes are separate jobs, so each
    # gets the full step budget instead of sharing one.
    assert step.get("timeout-minutes") == 45
    assert job.get("timeout-minutes") == 75
    # A request timeout, so a slow answer cannot consume the whole budget.
    assert "--request-timeout 15" in script
    # No retry storm: a slow answer stays visible instead of spending 4x the
    # timeout on it (measured: retries turned one 10s timeout into 40s).
    assert "--request-retries 0" in script
    # Load pinned to the runner's vCPU count.
    assert "--workers 2" in script
    # The SSE endpoint never completes its body, so it is excluded by name (its
    # OpenAPI entry advertises application/json, so content type cannot be used).
    assert '--exclude-path "/api/v1/events"' in script


def test_the_harness_is_checked_out_separately_from_the_artefact() -> None:
    steps = _steps()
    checkout_steps = [step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@")]

    assert len(checkout_steps) == 2, "one checkout for the RC tag, one for the policy harness"
    rc_checkout, harness_checkout = checkout_steps
    assert rc_checkout["with"]["ref"] == "${{ inputs.rc_tag }}"
    assert harness_checkout["with"]["ref"] == "${{ github.sha }}"
    assert harness_checkout["with"]["path"] == "rc-harness"
    # The harness checkout must not smuggle the deploy credentials anywhere.
    assert harness_checkout["with"].get("persist-credentials") is False


def test_the_credential_outlives_the_sweep_and_the_stack_proves_it() -> None:
    env = _fuzz_job()["env"]
    boot = _run(_BOOT_STEP)
    bootstrap = _run(_BOOTSTRAP_STEP)

    # The product default (15 minutes) is shorter than a pass, so the disposable
    # stack is given a lifetime that outlives the step budget.
    assert env["FUZZ_TOKEN_MINUTES"] == "180"
    assert "printf 'FARNA_CONTROL_ACCESS_TOKEN_MINUTES=%s\\n' \"$FUZZ_TOKEN_MINUTES\" >> .env" in boot
    # ...and the RUNNING container is asked, so a name the application does not
    # read cannot pass unnoticed.
    assert "printenv FARNA_CONTROL_ACCESS_TOKEN_MINUTES" in bootstrap
    assert '"$token_minutes" -lt "$FUZZ_TOKEN_MINUTES"' in bootstrap


# ---------------------------------------------------------------------------
# Booting, readiness and bootstrap reuse the shipped harness
# ---------------------------------------------------------------------------


def test_the_stack_is_booted_from_the_packaged_bundle() -> None:
    script = _run(_BOOT_STEP)

    # Same boot path as the clean-host and matrix legs: the shipped bundle, its
    # own generated secrets, then compose up.
    assert "./init-env.sh" in script
    assert "docker compose -f docker-compose.prod.yml up -d" in script
    # A throwaway admin is seeded into THIS stack only, from a per-run value.
    assert 'printf \'FARNA_CONTROL_USERS=%s:%s\\n\' "$FUZZ_EMAIL" "$fuzz_password" >> .env' in script
    assert "openssl rand -hex" in script
    assert 'echo "::add-mask::${fuzz_password}"' in script


def test_readiness_waits_on_the_app_endpoint_with_a_bounded_budget() -> None:
    script = _run(_READY_STEP)

    assert 'ready_url="http://127.0.0.1:${PORT}/healthz/ready"' in script
    assert "for attempt in $(seq 1 60); do" in script
    assert '"$code" = "200"' in script


def test_a_boot_failure_surfaces_the_container_logs() -> None:
    script = _run(_READY_STEP)
    diagnostics = _run("Capture stack diagnostics")

    # The readiness timeout reports what the containers said, then fails.
    assert "docker compose -f docker-compose.prod.yml logs" in script
    assert "::error::" in script
    assert script.rstrip().endswith("exit 1")
    # ...and an always-on capture step keeps the same evidence as an artefact,
    # including when a step before readiness died.
    assert _step("Capture stack diagnostics")["if"] == "always()"
    assert "docker compose -f docker-compose.prod.yml ps" in diagnostics
    assert "docker compose -f docker-compose.prod.yml logs" in diagnostics
    assert '"$evidence_dir/stack/containers.log"' in diagnostics


def test_the_bootstrap_uses_the_shipped_smoke_path() -> None:
    script = _run(_BOOTSTRAP_STEP)

    assert '"$GITHUB_WORKSPACE/scripts/smoke-compose.sh"' in script
    # Secrets travel in the environment, never in argv.
    assert 'FARNA_CONTROL_SMOKE_EMAIL="$FUZZ_EMAIL" FARNA_CONTROL_SMOKE_PASSWORD="$FUZZ_PASSWORD"' in script
    assert "--password" not in script
    # The service names and DB coordinates are taken from the packaged compose
    # (so a renamed service/user still works) and CR-stripped: a stray carriage
    # return turns the database name into "farna_control\r", which psql reports
    # as missing.
    assert "| tr -d '\\r' > \"$RUNNER_TEMP/stack-coordinates.txt\"" in script
    assert 'read -r db_service db_user db_name app_service < "$RUNNER_TEMP/stack-coordinates.txt"' in script
    assert 'echo "APP_SERVICE=${app_service}" >> "$GITHUB_ENV"' in script
    assert '--db-service "$db_service"' in script
    assert '--db-name "$db_name"' in script
    # The account is seeded by the boot seeder (which also creates the default
    # organisation); the credential is created against the stack's own API.
    assert "/api/v1/auth/login" in script
    assert "/api/v1/model-backends" in script
    assert '"$credential_status" != "201"' in script
    # The token is masked in the log and never echoed.
    assert 'echo "::add-mask::${token}"' in script


# ---------------------------------------------------------------------------
# The fuzz step is blocking; scrubbing, teardown and evidence are unconditional
# ---------------------------------------------------------------------------


def test_the_fuzz_step_cannot_be_skipped_or_made_non_blocking() -> None:
    step = _step(_FUZZ_STEP)

    assert "if" not in step, "the fuzz step must run whenever its predecessors succeed"
    assert "continue-on-error" not in step
    assert _fuzz_job().get("continue-on-error") is None
    assert step.get("timeout-minutes") is not None, "a hung fuzz run must be bounded"


def test_the_credential_is_scrubbed_before_anything_is_uploaded() -> None:
    steps = _steps()
    names = [candidate.get("name") for candidate in steps]
    scrub = _step(_SCRUB_STEP)
    upload = _step(_UPLOAD_STEP)

    assert scrub["if"] == "always()"
    # The scrub runs the tested policy helper, not an inline sed.
    assert '"$GITHUB_WORKSPACE/rc-harness/scripts/rc_fuzz_policy.py" scrub' in str(scrub["run"])
    assert '--evidence-dir "${RUNNER_TEMP}/rc-fuzz-evidence"' in str(scrub["run"])
    # ...and a failed scrub withholds the artefact rather than shipping a leak.
    assert "steps.scrub.outcome != 'failure'" in str(upload["if"])
    assert names.index(_SCRUB_STEP) < names.index(_UPLOAD_STEP)


def test_teardown_is_always_and_runs_last() -> None:
    step = _step(_TEARDOWN_STEP)
    names = [candidate.get("name") for candidate in _steps()]

    assert step["if"] == "always()"
    assert "--volumes" in str(step["run"])
    assert "--remove-orphans" in str(step["run"])
    # Logs are captured while the containers exist, so teardown comes last.
    assert names[-1] == _TEARDOWN_STEP
    assert names.index(_UPLOAD_STEP) < names.index(_TEARDOWN_STEP)


def test_the_report_and_logs_are_uploaded_per_pass() -> None:
    step = _step(_UPLOAD_STEP)

    assert step["if"].startswith("always()")
    assert step["with"]["path"] == "${{ runner.temp }}/rc-fuzz-evidence"
    # Per pass, so two artefacts never collide at the run level.
    assert step["with"]["name"] == "alpha-rc-fuzz-${{ matrix.pass }}-${{ github.run_id }}"


def test_the_validation_marker_is_a_separate_job_that_needs_every_pass() -> None:
    marker = _job(_MARKER_JOB)
    names = [step.get("name") for step in marker["steps"]]

    # Gone from the fuzz job: a matrix entry must never write the tag twice.
    assert "Record validation success (validated/<rc_tag> marker)" not in [step.get("name") for step in _steps()]
    assert marker["needs"] == [_FUZZ_JOB], "a skipped or failed pass must withhold the marker"
    assert marker["permissions"] == {"contents": "write"}
    assert "Record validation success (validated/<rc_tag> marker)" in names
    assert "validated/${RC_TAG}" in str(marker["steps"][-1]["run"])


# ---------------------------------------------------------------------------
# The removed deploy-identity machinery is gone, and nothing references it
# ---------------------------------------------------------------------------


def test_the_removed_deploy_identity_files_are_gone() -> None:
    for relative in _REMOVED_FILES:
        assert not (REPO_ROOT / relative).exists(), f"{relative} must be removed"


def test_deploy_workflow_no_longer_records_a_release_identity() -> None:
    workflow = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))

    assert isinstance(workflow, dict)
    assert "record-release-identity" not in workflow["jobs"]
    assert "release-identity" not in DEPLOY_WORKFLOW.read_text(encoding="utf-8")


def _scanned_files() -> list[Path]:
    patterns = (
        ".github/workflows/*.yml",
        ".github/scripts/*.py",
        "scripts/*.py",
        "scripts/*.sh",
        "backend/tests/unit/*.py",
        "backend/tests/unit/scripts/*.py",
        "backend/tests/architecture/*.py",
    )
    files: list[Path] = []
    for pattern in patterns:
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    assert files, "the reference scan found no files to scan"
    return files


def test_nothing_references_the_removed_deploy_identity_machinery() -> None:
    violations: list[str] = []
    for path in _scanned_files():
        key = path.relative_to(REPO_ROOT).as_posix()
        if key in _SCAN_EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        tokens = _REMOVED_TOKENS
        if key.startswith(".github/workflows/"):
            tokens = (*_REMOVED_TOKENS, *_REMOVED_DISPATCH_INPUTS)
        for token in tokens:
            if token in text:
                violations.append(f"  {key}: {token}")

    assert not violations, "removed deploy-identity machinery is still referenced:\n" + "\n".join(violations)
