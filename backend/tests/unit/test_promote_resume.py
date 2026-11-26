"""FAR-2802: a failed promote can be resumed, fail-closed.

``promote`` pushes the GA tag ``v<X.Y.Z>`` before the rest of the GA publication
chain runs. A failure after that point cannot be completed by re-dispatching a
plain promote: safety 2 refuses because the tag exists, and safety 4 requires
``origin/main`` to equal the RC commit, which is false once the promote PR has
squash-merged. This suite pins the explicit ``resume`` input and the two code
paths it adds:

* the ``resolve-target`` resume branch that resolves the release target from the
  frozen GA tag instead of ``origin/main``; and
* the ``Promote to GA and lift freeze`` resume branch that re-derives the release
  identity (freeze == GA commit, single-parent squash merge, validated RC parent)
  and performs no tag or freeze mutation at all.

The guard is executed for real against a stubbed ``git`` so each check is proven
to fire, not merely to be present in the YAML.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"
RELEASE_DOCS = REPO_ROOT / "docs" / "release-process.md"

PROMOTE_STEP = "Promote to GA and lift freeze"
RESOLVE_STEP = "Resolve the target commit (pin or origin/main)"
VERSION = "0.2.0"
GA_TAG = f"v{VERSION}"

_RESUME_HEADER = 'if [ "${RESUME:-false}" = "true" ]; then'

#: Fixed 40-character commit identities so a command line can be matched whole.
GA_SHA = "1" * 40
RC_SHA = "2" * 40
VALIDATED_SHA = RC_SHA
DECOY_SHA = "3" * 40
EXTRA_PARENT_SHA = "4" * 40


# ---------------------------------------------------------------------------
# Workflow readers
# ---------------------------------------------------------------------------


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "release-unified.yml did not parse as a mapping"
    return loaded


def _dispatch_block() -> dict[str, Any]:
    """The ``workflow_dispatch`` mapping.

    PyYAML resolves a bare ``on:`` key to the boolean ``True``, so the key can be
    either spelling depending on the loader version.
    """
    for key, value in _workflow().items():
        if key is True or key == "on":
            return dict(value["workflow_dispatch"])
    raise AssertionError("release-unified.yml has no workflow_dispatch trigger")


def _job_step(job: str, name: str) -> dict[str, Any]:
    steps = _workflow()["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step in job {job!r}, found {len(matches)}"
    return dict(matches[0])


def _release_step(name: str) -> dict[str, Any]:
    return _job_step("release", name)


def _release_job_run(name: str) -> str:
    run = _release_step(name).get("run")
    assert isinstance(run, str), f"{name!r} must contain a shell run block"
    return run


def _resolve_job_run() -> str:
    run = _job_step("resolve-target", RESOLVE_STEP).get("run")
    assert isinstance(run, str), f"{RESOLVE_STEP!r} must contain a shell run block"
    return run


def _resume_block(script: str) -> str:
    """The resume branch of a run block, from its guard to its terminator.

    The block scalar is de-indented when parsed, so top-level statements (and
    the branch's own ``fi``/``elif``) sit at column 0 while nested ones are
    indented; only the unindented terminator can end the slice. A branch that
    ends in ``fi`` keeps that ``fi`` so the slice is executable on its own.
    """
    start = script.index(_RESUME_HEADER)
    rest = script[start:]
    fi_index = rest.find("\nfi\n")
    elif_index = rest.find("\nelif ")
    if fi_index != -1 and (elif_index == -1 or fi_index < elif_index):
        return rest[: fi_index + len("\nfi")]
    assert elif_index != -1, "the resume branch has no terminator"
    return rest[:elif_index]


def _resolve_resume_violations(script: str) -> list[str]:
    problems: list[str] = []
    if _RESUME_HEADER not in script:
        problems.append("has no resume branch")
        return problems
    block = _resume_block(script)
    if 'if [ "$MODE" != "promote" ]; then' not in block:
        problems.append("does not reject resume for a non-promote mode")
    if "resume_unsupported_mode" not in block:
        problems.append("does not fail loudly on a non-promote resume")
    if '"refs/tags/${GA_TAG}"' not in block:
        problems.append("does not prove the GA tag exists on origin")
    if "git fetch --quiet --force --tags origin" not in block:
        problems.append("does not fetch tags before resolving the GA tag")
    if 'git rev-parse --verify "${GA_TAG}^{commit}"' not in block:
        problems.append("does not resolve the GA tag to a commit")
    if 'echo "target_pinned=true"' not in block:
        problems.append("does not report target_pinned=true")
    if 'echo "target_sha=${RESUME_TARGET}"' not in block:
        problems.append("does not report the GA commit as the target")
    if "origin/main" in block and "git rev-parse origin/main" in block:
        problems.append("resolves origin/main on a resume")
    return problems


def _promote_resume_violations(script: str) -> list[str]:
    problems: list[str] = []
    if _RESUME_HEADER not in script:
        problems.append("has no resume branch")
        return problems
    block = _resume_block(script)
    if "git fetch --tags --force origin" not in block:
        problems.append("does not fetch tags")
    if 'git ls-remote --exit-code origin "refs/tags/${GA_TAG}"' not in block:
        problems.append("does not require the GA tag on origin")
    if 'git rev-parse --verify "${GA_TAG}^{commit}"' not in block:
        problems.append("does not resolve the GA tag to a commit")
    if "refs/heads/release-freeze" not in block:
        problems.append("does not read release-freeze")
    if 'if [ "$FREEZE_SHA" != "$GA_SHA" ]; then' not in block:
        problems.append("does not require release-freeze to equal the GA commit")
    if 'git rev-list --parents -n1 "$GA_SHA"' not in block:
        problems.append("does not read the GA commit's parents")
    if 'if [ "$parent_count" != "1" ]; then' not in block:
        problems.append("does not require a single-parent GA commit")
    if 'git rev-parse "${GA_SHA}^"' not in block:
        problems.append("does not resolve the RC parent")
    if 'git tag --list "${GA_TAG}-rc*" --sort=-v:refname' not in block:
        problems.append("does not pick the highest rc tag at the RC commit")
    if 'VALIDATED_TAG="validated/${RC_TAG}"' not in block:
        problems.append("does not derive the validated marker from the rc tag")
    if 'if [ "$VAL_SHA" != "$RC_SHA" ]; then' not in block:
        problems.append("does not require the validated marker to point at the RC parent")
    if 'git checkout --detach "$GA_SHA"' not in block:
        problems.append("does not check out the GA commit")
    if 'echo "promote_sha=$GA_SHA"' not in block:
        problems.append("does not emit promote_sha as the GA commit")
    if 'echo "ga_tag=$GA_TAG"' not in block:
        problems.append("does not emit ga_tag")
    if 'echo "rc_tag=$RC_TAG"' not in block:
        problems.append("does not emit the resume rc_tag")
    if "::notice::Resume" not in block:
        problems.append("does not print a resume banner")
    return problems


def _resume_write_violations(block: str) -> list[str]:
    forbidden = {
        "creates an annotated tag": "git tag -a",
        "pushes the GA tag": 'git push origin "$GA_TAG"',
        "pushes or deletes a marker": "marker_git push",
        "deletes a remote ref": "--delete",
        "moves release-freeze": "git branch -f release-freeze",
        "creates a promote PR": "gh pr create",
        "merges a promote PR": "gh pr merge",
        "checks the publication hold": "release-publication-hold",
    }
    return [f"{label} ({needle!r})" for label, needle in forbidden.items() if needle in block]


# ---------------------------------------------------------------------------
# Structural pins
# ---------------------------------------------------------------------------


def test_resume_input_exists_and_defaults_false() -> None:
    resume = _dispatch_block()["inputs"]["resume"]
    assert resume["type"] == "boolean"
    assert resume["required"] is False
    assert resume["default"] is False
    assert "mode=promote" in resume["description"]


def test_resume_is_rejected_for_every_non_promote_mode() -> None:
    problems = _resolve_resume_violations(_resolve_job_run())
    assert not problems, "; ".join(problems)
    step = _job_step("resolve-target", RESOLVE_STEP)
    assert step["env"]["RESUME"] == "${{ inputs.resume }}"
    assert step["env"]["VERSION"] == "${{ inputs.version }}"


def test_promote_step_re_derives_the_release_identity_on_resume() -> None:
    problems = _promote_resume_violations(_release_job_run(PROMOTE_STEP))
    assert not problems, "; ".join(problems)
    assert _release_step(PROMOTE_STEP)["env"]["RESUME"] == "${{ inputs.resume }}"


# ---------------------------------------------------------------------------
# Structural pins: a step must declare the shell and env its run block spends
# ---------------------------------------------------------------------------

DRY_RUN_JOB = "dry-run"
DRY_RUN_BUILD_STEP = "Build all images (no push)"
IMAGE_NAMESPACE = "farnalabs/farna-control"


def _dry_run_step(name: str) -> dict[str, Any]:
    return _job_step(DRY_RUN_JOB, name)


def _inherited_env_levels() -> list[dict[str, Any]]:
    """The workflow- and job-level env maps a step env would inherit from."""
    workflow = _workflow()
    return [
        dict(workflow.get("env") or {}),
        dict(workflow["jobs"][DRY_RUN_JOB].get("env") or {}),
    ]


def _image_namespace_violations(step: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    env = dict(step.get("env") or {})
    script = str(step.get("run", ""))
    if env.get("IMAGE_NAMESPACE") != IMAGE_NAMESPACE:
        problems.append("does not declare IMAGE_NAMESPACE in its own env block")
    if "$IMAGE_NAMESPACE" not in script:
        problems.append("does not use IMAGE_NAMESPACE in its run block")
    if any("IMAGE_NAMESPACE" in level for level in _inherited_env_levels()):
        problems.append("relies on an inherited IMAGE_NAMESPACE instead of declaring it")
    if "RESUME" in env:
        problems.append("declares RESUME, which this step never reads")
    return problems


def test_dry_run_image_build_declares_its_image_namespace() -> None:
    problems = _image_namespace_violations(_dry_run_step(DRY_RUN_BUILD_STEP))
    assert not problems, "; ".join(problems)


def test_promote_step_declares_its_bash_shell() -> None:
    assert _release_step(PROMOTE_STEP).get("shell") == "bash"


def test_resume_branch_never_mutates_the_tag_or_the_freeze() -> None:
    block = _resume_block(_release_job_run(PROMOTE_STEP))
    problems = _resume_write_violations(block)
    assert not problems, "; ".join(problems)


def test_failed_resumed_promote_leaves_the_freeze_untouched() -> None:
    step = _release_step("Re-establish release freeze after failed GA publication")
    assert step["env"]["RESUME"] == "${{ inputs.resume }}"
    branch = _resume_block(_release_job_run("Re-establish release freeze after failed GA publication"))
    problems = _resume_write_violations(branch)
    assert not problems, "; ".join(problems)
    assert "leaving release-freeze untouched" in branch


def test_release_docs_document_the_resume_dispatch() -> None:
    docs = RELEASE_DOCS.read_text(encoding="utf-8")
    assert "### Resuming a failed promote" in docs
    assert "-f mode=promote -f version=0.4.0 -f resume=true" in docs
    assert "only valid with `mode=promote`" in docs
    assert "release-freeze` on origin must resolve to that exact GA commit" in docs
    assert "exactly one parent" in docs
    assert "squash-merged" in docs
    assert "validated/<rc>" in docs
    assert "never creates, moves or deletes the GA tag" in docs
    # The docs must scope the resume to the release job and name re-run-failed-jobs
    # as the recovery path for the downstream publication jobs (FAR-2802 review).
    assert "The resume covers the `release` job, not the downstream publication jobs" in docs
    assert "Re-run failed jobs" in docs
    assert "never read `release-freeze`" in docs


#: Publication jobs that run with ``needs: release``, i.e. after the release job
#: has already lifted ``release-freeze``.
_DOWNSTREAM_PUBLICATION_JOBS = ("publish-pypi", "promote-compose", "attest-artefacts")


def _job_needs(job: str) -> set[str]:
    needs = _workflow()["jobs"][job].get("needs", [])
    if isinstance(needs, str):
        return {needs}
    return set(needs)


def test_downstream_publication_jobs_are_recovered_by_rerun_not_resume() -> None:
    """A re-run of the failed downstream jobs recovers PyPI / compose / attestation.

    The ``release`` job lifts ``release-freeze`` in its final step, so the resume
    guard - which requires the freeze to sit at the GA commit - can never reach a
    job that runs later with ``needs: release``. Those jobs must therefore be
    re-runnable with the freeze already gone: they must run after ``release`` and
    must not read ``release-freeze`` at all. That is what makes "Re-run failed
    jobs" (not a ``resume=true`` dispatch) the recovery path the docs describe.
    """
    jobs = _workflow()["jobs"]

    # The freeze is lifted inside the release job, before these jobs can start.
    release_steps = [step.get("name") for step in jobs["release"]["steps"]]
    assert "Lift release freeze after successful GA publication" in release_steps

    for job in _DOWNSTREAM_PUBLICATION_JOBS:
        assert "release" in _job_needs(job), f"{job} must run after the release job (needs: release)"
        for step in jobs[job]["steps"]:
            run = step.get("run")
            if isinstance(run, str):
                # The real dependency signal is a query of the marker ref, not a
                # prose mention in a comment.
                assert "refs/heads/release-freeze" not in run, f"{job} must not query release-freeze: it runs after the release job lifts it, so re-running the failed job must succeed with the freeze absent"


# ---------------------------------------------------------------------------
# Executable harness: the resume guard against a stubbed git
# ---------------------------------------------------------------------------

_GIT_STUB = """#!/usr/bin/env bash
set -uo pipefail
cmd="$*"

case "$cmd" in
  "fetch --tags --force origin") exit 0 ;;
  "rev-parse HEAD") cat "$STUB_HEAD_FILE"; exit 0 ;;
  "checkout --detach $STUB_GA_SHA") printf '%s' "$STUB_GA_SHA" > "$STUB_HEAD_FILE"; exit 0 ;;
  "rev-list --parents -n1 $STUB_GA_SHA")
    if [ "${STUB_PARENT_EXIT:-0}" != "0" ]; then
      echo "fatal: bad object $STUB_GA_SHA" >&2
      exit "${STUB_PARENT_EXIT}"
    fi
    printf '%s\\n' "$STUB_PARENT_LINE"
    exit 0
    ;;
  "rev-parse $STUB_GA_SHA^") printf '%s\\n' "$STUB_RC_SHA"; exit 0 ;;
  "tag --list $STUB_GA_TAG-rc* --sort=-v:refname") printf '%s\\n' "$STUB_RC_TAGS"; exit 0 ;;
esac

if [ "$cmd" = "ls-remote --exit-code origin refs/tags/$STUB_GA_TAG" ]; then
  case "$STUB_GA_STATE" in
    present) printf '%s\\t%s\\n' "$STUB_GA_SHA" "refs/tags/$STUB_GA_TAG"; exit 0 ;;
    absent) exit 2 ;;
    *) echo "fatal: unable to access the remote" >&2; exit 128 ;;
  esac
fi

if [ "$cmd" = "ls-remote --exit-code origin refs/heads/release-freeze" ]; then
  case "$STUB_FREEZE_STATE" in
    present) printf '%s\\trefs/heads/release-freeze\\n' "$STUB_FREEZE_SHA"; exit 0 ;;
    absent) exit 2 ;;
    *) echo "fatal: unable to access the remote" >&2; exit 128 ;;
  esac
fi

if [ "$cmd" = "ls-remote --exit-code origin refs/tags/validated/$STUB_RC_TAG" ]; then
  case "$STUB_VALIDATED_STATE" in
    present) printf '%s\\trefs/tags/validated/%s\\n' "$STUB_VALIDATED_SHA" "$STUB_RC_TAG"; exit 0 ;;
    absent) exit 2 ;;
    *) echo "fatal: unable to access the remote" >&2; exit 128 ;;
  esac
fi

if [ "$cmd" = "rev-parse --verify $STUB_GA_TAG^{commit}" ]; then
  if [ "$STUB_GA_STATE" != "present" ]; then
    exit 128
  fi
  printf '%s\\n' "$STUB_GA_SHA"
  exit 0
fi

if [ "$cmd" = "rev-parse --verify validated/$STUB_RC_TAG^{commit}" ]; then
  if [ "$STUB_VALIDATED_STATE" != "present" ]; then
    exit 128
  fi
  printf '%s\\n' "$STUB_VALIDATED_SHA"
  exit 0
fi

if [ "$cmd" = "rev-parse $STUB_RC_TAG^{commit}" ]; then
  printf '%s\\n' "$STUB_RC_SHA"
  exit 0
fi

if [ "$cmd" = "rev-parse $STUB_DECOY_TAG^{commit}" ]; then
  printf '%s\\n' "$STUB_DECOY_SHA"
  exit 0
fi

echo "unexpected git call: $cmd" >&2
exit 127
"""

_DECOY_TAG = f"{GA_TAG}-rc10"
_RC_TAG = f"{GA_TAG}-rc2"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute the promote resume guard")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _run_resume_guard(tmp_path: Path, **scenario: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "git", _GIT_STUB)
    head_file = tmp_path / "head"
    head_file.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VERSION": VERSION,
            "RESUME": "true",
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "STUB_HEAD_FILE": str(head_file),
            "STUB_GA_TAG": GA_TAG,
            "STUB_RC_TAG": _RC_TAG,
            "STUB_DECOY_TAG": _DECOY_TAG,
            "STUB_GA_SHA": GA_SHA,
            "STUB_RC_SHA": RC_SHA,
            "STUB_DECOY_SHA": DECOY_SHA,
            "STUB_VALIDATED_SHA": VALIDATED_SHA,
            "STUB_FREEZE_SHA": GA_SHA,
            "STUB_PARENT_LINE": f"{GA_SHA} {RC_SHA}",
            "STUB_RC_TAGS": f"{_DECOY_TAG}\n{_RC_TAG}",
            "STUB_GA_STATE": "present",
            "STUB_FREEZE_STATE": "present",
            "STUB_VALIDATED_STATE": "present",
            "STUB_PARENT_EXIT": "0",
        }
    )
    env.update(scenario)
    script = "set -euo pipefail\n" + 'GA_TAG="v${VERSION}"\n' + _resume_block(_release_job_run(PROMOTE_STEP)) + "\n"
    result = _run_bash(script, cwd=tmp_path, env=env)
    output = tmp_path / "output"
    return result, output.read_text(encoding="utf-8") if output.exists() else ""


@pytest.mark.posix_only
def test_resume_guard_happy_path_emits_the_frozen_identity(tmp_path: Path) -> None:
    result, output = _run_resume_guard(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"ga_tag={GA_TAG}" in output
    assert f"rc_tag={_RC_TAG}" in output
    assert f"promote_sha={GA_SHA}" in output
    assert f"Resume {GA_TAG}" in result.stdout
    assert GA_SHA in result.stdout
    assert RC_SHA in result.stdout


@pytest.mark.posix_only
def test_resume_guard_picks_the_highest_rc_tag_at_the_rc_commit(tmp_path: Path) -> None:
    # rc10 sorts before rc2 under --sort=-v:refname but sits at a decoy commit,
    # so the loop must skip it and keep rc2, which is the RC parent.
    result, output = _run_resume_guard(tmp_path)
    assert result.returncode == 0, result.stderr + result.stdout
    assert f"rc_tag={_RC_TAG}" in output
    assert _DECOY_TAG not in output


@pytest.mark.posix_only
def test_resume_guard_fails_closed_when_the_freeze_has_moved(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_FREEZE_SHA=DECOY_SHA)
    assert result.returncode != 0
    assert "release-freeze is at" in result.stdout
    assert f"not the GA commit {GA_SHA}" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_fails_closed_when_the_freeze_is_absent(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_FREEZE_STATE="absent")
    assert result.returncode != 0
    assert "release-freeze is absent on origin" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_fails_closed_when_the_freeze_query_errors(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_FREEZE_STATE="error")
    assert result.returncode != 0
    assert "Could not query release-freeze" in result.stdout
    assert "unknown freeze state" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_rejects_a_multi_parent_ga_commit(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_PARENT_LINE=f"{GA_SHA} {RC_SHA} {EXTRA_PARENT_SHA}")
    assert result.returncode != 0
    assert "has 2 parent(s)" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_rejects_a_root_ga_commit(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_PARENT_LINE=GA_SHA)
    assert result.returncode != 0
    assert "has 0 parent(s)" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_fails_closed_on_an_unreadable_parent_listing(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_PARENT_EXIT="128")
    assert result.returncode != 0
    assert "Could not read the parents of the GA commit" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_rejects_a_missing_validated_marker(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_VALIDATED_STATE="absent")
    assert result.returncode != 0
    assert f"no validation marker validated/{_RC_TAG}" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_rejects_a_validated_marker_at_the_wrong_commit(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_VALIDATED_SHA=DECOY_SHA)
    assert result.returncode != 0
    assert f"points at {DECOY_SHA}" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_fails_closed_when_the_ga_tag_query_errors(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_GA_STATE="error")
    assert result.returncode != 0
    assert "Could not query the GA tag" in result.stdout


@pytest.mark.posix_only
def test_resume_guard_rejects_a_missing_ga_tag(tmp_path: Path) -> None:
    result, _ = _run_resume_guard(tmp_path, STUB_GA_STATE="absent")
    assert result.returncode != 0
    assert "does not exist on origin" in result.stdout


# ---------------------------------------------------------------------------
# Idempotent GA publication on a resume (FAR-2802 second slice)
# ---------------------------------------------------------------------------

HELM_STEP = "Push Helm chart to OCI registry"
PYPI_STEP = "Check whether this version is already published on PyPI"
PYPI_PUBLISH_STEP = "Publish farna-control via Trusted Publishing"
FREEZE_LIFT_STEP = "Lift release freeze after successful GA publication"


def _helm_violations(script: str) -> list[str]:
    problems: list[str] = []
    if "helm pull" not in script:
        problems.append("does not ask the registry whether the version exists")
    if "--destination" not in script:
        problems.append("does not pull to a scratch destination")
    if "farna-control-${VERSION_NUM}.tgz" not in script:
        problems.append("does not read the pulled chart archive")
    if "Chart.yaml" not in script:
        problems.append("does not read the published Chart.yaml")
    if "PUBLISHED_VERSION" not in script:
        problems.append("does not read the published chart version")
    if "PUBLISHED_APP_VERSION" not in script:
        problems.append("does not read the published appVersion")
    if 'if [ "$PUBLISHED_VERSION" != "$VERSION_NUM" ]' not in script:
        problems.append("does not require the published version to match")
    if "not found|manifest unknown|no such manifest" not in script:
        problems.append("does not distinguish a not-found pull from any other failure")
    if "failed for a reason other than an absent version" not in script:
        problems.append("does not fail closed on a non-not-found pull error")
    if "skipping the push" not in script:
        problems.append("does not skip the push when the version is already published")
    pull_index = script.find("helm pull")
    push_index = script.find("helm push")
    if push_index == -1:
        problems.append("has no helm push")
    elif push_index < pull_index:
        problems.append("pushes before checking whether the version exists")
    return problems


def _pypi_violations(script: str) -> list[str]:
    problems: list[str] = []
    if "https://pypi.org/pypi/farna-control/${VERSION}/json" not in script:
        problems.append("does not query the PyPI JSON API for the version")
    if "curl " not in script:
        problems.append("does not use curl for the query")
    if "--write-out '%{http_code}'" not in script:
        problems.append("does not capture the HTTP status")
    if 'echo "publish=false" >> "$GITHUB_OUTPUT"' not in script:
        problems.append("does not set publish=false when already published")
    if 'echo "publish=true" >> "$GITHUB_OUTPUT"' not in script:
        problems.append("does not set publish=true when unpublished")
    if "404)" not in script:
        problems.append("does not treat 404 as unpublished")
    if "200)" not in script:
        problems.append("does not handle a 200 response")
    if "different file set" not in script:
        problems.append("does not fail loudly when the published file set differs")
    if "Unexpected PyPI response" not in script:
        problems.append("does not fail closed on an unexpected status")
    if "sha256" in script.lower():
        problems.append("compares file hashes instead of filenames")
    return problems


def _freeze_lift_violations(script: str) -> list[str]:
    problems: list[str] = []
    if "refs/heads/release-freeze" not in script:
        problems.append("does not query release-freeze")
    if "freeze_rc" not in script:
        problems.append("does not branch on the query exit code")
    if "already absent" not in script:
        problems.append("does not tolerate an already-absent freeze")
    if 'marker_git push "$MARKER_REMOTE" --delete refs/heads/release-freeze' not in script:
        problems.append("does not lift the freeze with the admin marker credential")
    if "unknown hold" not in script:
        problems.append("does not fail closed on an unqueryable freeze")
    return problems


def test_helm_push_skips_an_existing_version_and_fails_closed() -> None:
    script = _release_job_run(HELM_STEP)
    problems = _helm_violations(script)
    assert not problems, "; ".join(problems)


def test_pypi_check_is_a_step_that_gates_the_publish_action() -> None:
    step = _job_step("publish-pypi", PYPI_STEP)
    problems = _pypi_violations(str(step.get("run", "")))
    assert not problems, "; ".join(problems)
    assert step["id"] == "pypi-state"
    publish = _job_step("publish-pypi", PYPI_PUBLISH_STEP)
    assert publish["if"] == "steps.pypi-state.outputs.publish == 'true'"
    names = [other.get("name") for other in _workflow()["jobs"]["publish-pypi"]["steps"]]
    assert names.index(PYPI_STEP) < names.index(PYPI_PUBLISH_STEP)


def test_freeze_lift_tolerates_an_absent_freeze_but_not_an_unknown_one() -> None:
    script = _release_job_run(FREEZE_LIFT_STEP)
    problems = _freeze_lift_violations(script)
    assert not problems, "; ".join(problems)


def test_resume_and_failed_resume_never_lift_the_freeze() -> None:
    for name in (PROMOTE_STEP, "Re-establish release freeze after failed GA publication"):
        block = _resume_block(_release_job_run(name))
        assert "--delete refs/heads/release-freeze" not in block, name


def test_freeze_cleanup_cannot_lift_the_freeze_on_a_promote() -> None:
    condition = _workflow()["jobs"]["freeze-cleanup"]["if"]
    assert "inputs.mode == 'cut'" in condition
    assert "inputs.mode == 'recut'" in condition
    assert "promote" not in condition


def test_release_docs_document_the_idempotent_publication_paths() -> None:
    docs = RELEASE_DOCS.read_text(encoding="utf-8")
    assert "publication steps are idempotent" in docs
    assert "never overwritten" in docs
    assert "https://pypi.org/pypi/farna-control/<version>/json" in docs
    assert "not-found error means" in docs
    assert "freeze already being absent" in docs


# ---------------------------------------------------------------------------
# Executable harness: Helm and PyPI idempotency against stubbed tools
# ---------------------------------------------------------------------------

_HELM_STUB = """#!/usr/bin/env bash
set -uo pipefail
sub="$1"
shift
case "$sub" in
  registry|login)
    cat >/dev/null
    exit 0
    ;;
  pull)
    dest=""
    ver=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --version) ver="$2"; shift 2 ;;
        --destination) dest="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    stage="$dest/stage/farna-control"
    case "${HELM_MODE:-absent}" in
      present|mismatch)
        mkdir -p "$stage"
        app="$ver"
        if [ "$HELM_MODE" = "mismatch" ]; then app="9.9.9"; fi
        {
          printf 'apiVersion: v2\\n'
          printf 'name: farna-control\\n'
          printf 'version: %s\\n' "$ver"
          printf 'appVersion: "%s"\\n' "$app"
        } > "$stage/Chart.yaml"
        tar -czf "$dest/farna-control-$ver.tgz" -C "$dest/stage" farna-control
        exit 0
        ;;
      absent)
        echo 'Error: failed to pull "oci://ghcr.io/farnalabs/farna-control/charts/farna-control": not found' >&2
        exit 1
        ;;
      *)
        echo 'Error: failed to authorize: 401 Unauthorized' >&2
        exit 1
        ;;
    esac
    ;;
  package)
    mkdir -p /tmp/helm-pkg
    echo "package $*" >> "$STUB_HELM_LOG"
    exit 0
    ;;
  push)
    echo "push $*" >> "$STUB_HELM_LOG"
    exit 0
    ;;
esac
echo "unexpected helm call: $sub $*" >&2
exit 127
"""

_CURL_STUB = """#!/usr/bin/env bash
set -uo pipefail
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --output) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "${PYPI_MODE:-unpublished}" in
  published)
    printf '%s' '{"info":{"version":"0.2.0"},"urls":[{"filename":"farna_control-0.2.0-py3-none-any.whl"},{"filename":"farna_control-0.2.0.tar.gz"}]}' > "$out"
    printf '200'
    ;;
  different)
    printf '%s' '{"info":{"version":"0.2.0"},"urls":[{"filename":"farna_control-0.2.0-py3-none-any.whl"},{"filename":"farna_control-0.2.0.tar.gz"},{"filename":"farna_control-0.2.0-py3-none-macosx_11_0_arm64.whl"}]}' > "$out"
    printf '200'
    ;;
  unpublished)
    printf '{"message":"Not Found"}' > "$out"
    printf '404'
    ;;
  *)
    printf '{"message":"boom"}' > "$out"
    printf '500'
    ;;
esac
"""


def _run_helm(tmp_path: Path, **scenario: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "helm", _HELM_STUB)
    helm_log = tmp_path / "helm.log"
    helm_log.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VERSION": VERSION,
            "GH_TOKEN": "test-token",
            "REGISTRY_USER": "tester",
            "IMAGE_NAMESPACE": "farnalabs/farna-control",
            "STUB_HELM_LOG": str(helm_log),
            "HELM_MODE": "absent",
        }
    )
    env.update(scenario)
    return _run_bash(_release_job_run(HELM_STEP), cwd=tmp_path, env=env), helm_log


@pytest.mark.posix_only
def test_helm_tolerates_an_already_published_chart(tmp_path: Path) -> None:
    result, helm_log = _run_helm(tmp_path, HELM_MODE="present")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "already published" in result.stdout
    assert not helm_log.read_text(encoding="utf-8").strip()


@pytest.mark.posix_only
def test_helm_fails_closed_when_the_published_version_disagrees(tmp_path: Path) -> None:
    result, helm_log = _run_helm(tmp_path, HELM_MODE="mismatch")
    assert result.returncode != 0
    assert "refusing to overwrite an existing chart version" in result.stdout
    assert not helm_log.read_text(encoding="utf-8").strip()


@pytest.mark.posix_only
def test_helm_pushes_when_the_version_is_absent(tmp_path: Path) -> None:
    result, helm_log = _run_helm(tmp_path, HELM_MODE="absent")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "not published yet; pushing it" in result.stdout
    assert "push " in helm_log.read_text(encoding="utf-8")


@pytest.mark.posix_only
def test_helm_fails_closed_on_a_non_not_found_pull_error(tmp_path: Path) -> None:
    result, helm_log = _run_helm(tmp_path, HELM_MODE="denied")
    assert result.returncode != 0
    assert "failed for a reason other than an absent version" in result.stdout
    assert not helm_log.read_text(encoding="utf-8").strip()


def _run_pypi(tmp_path: Path, **scenario: str) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "curl", _CURL_STUB)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "farna_control-0.2.0-py3-none-any.whl").write_text("wheel", encoding="utf-8")
    (dist / "farna_control-0.2.0.tar.gz").write_text("sdist", encoding="utf-8")
    output = tmp_path / "output"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "VERSION": VERSION,
            "GITHUB_OUTPUT": str(output),
            "PYPI_MODE": "unpublished",
        }
    )
    env.update(scenario)
    script = str(_job_step("publish-pypi", PYPI_STEP)["run"])
    result = _run_bash(script, cwd=tmp_path, env=env)
    return result, output.read_text(encoding="utf-8") if output.exists() else ""


@pytest.mark.posix_only
def test_pypi_skips_an_upload_when_the_same_file_set_is_published(tmp_path: Path) -> None:
    result, output = _run_pypi(tmp_path, PYPI_MODE="published")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "publish=false" in output
    assert "already published" in result.stdout


@pytest.mark.posix_only
def test_pypi_fails_closed_when_the_published_file_set_differs(tmp_path: Path) -> None:
    result, output = _run_pypi(tmp_path, PYPI_MODE="different")
    assert result.returncode != 0
    assert "different file set" in result.stdout
    assert "publish=" not in output


@pytest.mark.posix_only
def test_pypi_publishes_an_unpublished_version(tmp_path: Path) -> None:
    result, output = _run_pypi(tmp_path, PYPI_MODE="unpublished")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "publish=true" in output


@pytest.mark.posix_only
def test_pypi_fails_closed_on_an_unexpected_status(tmp_path: Path) -> None:
    result, output = _run_pypi(tmp_path, PYPI_MODE="error")
    assert result.returncode != 0
    assert "Unexpected PyPI response 500" in result.stdout
    assert "publish=" not in output
