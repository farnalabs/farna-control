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
* the fuzz step is blocking (no ``if:``, no ``continue-on-error``);
* teardown is ``if: always()`` and runs last, with the logs and the Schemathesis
  report uploaded as artefacts;
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

_PEN_TEST_JOB = "pen-test"
_FUZZ_STEP = "Schemathesis fuzz against the RC's own stack"
_READY_STEP = "Wait for the stack to report ready"
_BOOTSTRAP_STEP = "Bootstrap the fuzz account, organisation and credential"
_BOOT_STEP = "Start the RC's own disposable stack"
_PACKAGE_STEP = "Package the exact RC compose bundle"
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


def _pen_test() -> dict[str, Any]:
    job = _workflow()["jobs"][_PEN_TEST_JOB]
    assert isinstance(job, dict)
    return job


def _steps() -> list[dict[str, Any]]:
    steps = _pen_test().get("steps") or []
    assert isinstance(steps, list)
    return steps


def _step(step_name: str) -> dict[str, Any]:
    matches = [step for step in _steps() if step.get("name") == step_name]
    assert len(matches) == 1, f"expected exactly one {step_name!r} step, found {len(matches)}"
    return dict(matches[0])


def _run(step_name: str) -> str:
    return str(_step(step_name).get("run") or "")


def _job_text() -> str:
    """The job's own YAML text, so a token check cannot match a sibling job."""
    text = RC_VALIDATE_WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"\n  {_PEN_TEST_JOB}:")
    # pen-test is the last job in the file.
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
    assert f"\n  {_PEN_TEST_JOB}:" in text


def test_the_leg_still_runs_after_both_acceptance_legs() -> None:
    needs = _pen_test().get("needs")

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
    # v1 severity policy is unchanged: every check, GET only, blocking on any
    # finding. The example budget is bounded by measurement (a full GET sweep
    # runs ~3358 cases in ~16 minutes at 5 examples per operation).
    assert '--header "Authorization: Bearer ${FUZZ_JWT}"' in script
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


# ---------------------------------------------------------------------------
# The fuzz step is blocking; teardown and evidence are unconditional
# ---------------------------------------------------------------------------


def test_the_fuzz_step_cannot_be_skipped_or_made_non_blocking() -> None:
    step = _step(_FUZZ_STEP)

    assert "if" not in step, "the fuzz step must run whenever its predecessors succeed"
    assert "continue-on-error" not in step
    assert _pen_test().get("continue-on-error") is None
    assert step.get("timeout-minutes") is not None, "a hung fuzz run must be bounded"


def test_teardown_is_always_and_runs_last() -> None:
    step = _step(_TEARDOWN_STEP)
    names = [candidate.get("name") for candidate in _steps()]

    assert step["if"] == "always()"
    assert "--volumes" in str(step["run"])
    assert "--remove-orphans" in str(step["run"])
    # Logs are captured while the containers exist, so teardown comes last.
    assert names[-1] == _TEARDOWN_STEP
    assert names.index(_UPLOAD_STEP) < names.index(_TEARDOWN_STEP)


def test_the_report_and_logs_are_uploaded_as_an_artefact() -> None:
    step = _step(_UPLOAD_STEP)

    assert step["if"] == "always()"
    assert step["with"]["path"] == "${{ runner.temp }}/rc-fuzz-evidence"
    assert step["with"]["name"] == "alpha-rc-fuzz-${{ github.run_id }}"


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
