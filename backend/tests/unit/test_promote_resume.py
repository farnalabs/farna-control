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
