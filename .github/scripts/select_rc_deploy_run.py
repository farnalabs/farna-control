#!/usr/bin/env python3
"""Select the ``deploy.yml`` run that actually shipped an RC (FAR-2800).

Why this exists
---------------
``rc-validate.yml`` used to discover the deploy run for an RC by CREATION TIME:
the newest ``workflow_dispatch`` deploy run created at or after the tag. That is
a heuristic, not a property, so it could select a run that had shipped something
else. On 2026-11-26 it picked run 45725660070 - a main deploy that the throttle
skipped, so it shipped nothing at all - and production served 5ad651dc where
``v0.3.330-rc3`` is 8eee7e93. Validation then failed on deployment identity
instead of validating anything.

What it selects on instead
--------------------------
The property is the deploy run's own ``release-identity-<rc_tag>`` artifact.
``deploy.yml``'s ``record-release-identity`` job uploads it when, and only when,
the run has passed the throttle's machine-readable ``deployed == 'true'`` gate
(FAR-2152) AND carries a non-empty ``release_ref``. The artifact therefore
exists for a run that set out to deploy that exact RC tag, and cannot exist for
a throttled no-op run or for a main deploy. GitHub's ``actions/artifacts``
``name=`` filter returns the artifact together with its ``workflow_run.id``,
which is the run id: an exact string match, with no log parsing and no
inference from timestamps.

Why not the run API's ``inputs``
--------------------------------
GitHub documents an ``inputs`` field on the workflow-run object, but this
repository's runs do not carry it: the raw ``GET /actions/runs/{id}`` body
contains no ``inputs`` key at all. That was checked with and without an explicit
``X-GitHub-Api-Version: 2022-11-28`` header, against five ``workflow_dispatch``
runs across two workflows (2026-11-26). So ``deploy.yml`` records the identity
itself rather than relying on a field GitHub does not return here.

Verdicts (a reader must be able to tell these apart)
---------------------------------------------------
``selected``        a run shipped this RC; its id is reported
``deploy_pending``  a run records this RC and has not finished yet
``deploy_failed``   every run recording this RC finished without deploying it
``not_deployed``    no run records shipping this RC at all
``unknown``         the Actions API could not be read, so nothing is claimed

``deploy_failed`` and ``not_deployed`` say different things: the first means the
RC's own deploy ran and lost, the second means nothing ever tried. Neither is
reported as a wrong deployment SHA, and ``unknown`` never masquerades as
``not_deployed`` - a failed API read is not evidence that nothing was deployed.

Determinism
-----------
Several runs can record shipping the same RC (a recut re-dispatches the deploy).
Among them the HIGHEST RUN ID wins. Run ids are unique and monotonically
increasing, so this is a total order with no ties - determinism, not recency -
and it is also the right answer: deploy runs serialise on the
``deploy-pipeline-<ref>`` concurrency group, so the last run to ship the image is
the one production serves and that no later dispatch has superseded. Every
candidate is reported so a reader can see why one was chosen.

Tested by ``backend/tests/unit/scripts/test_select_rc_deploy_run.py``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

#: Artifact-name prefix. ``deploy.yml`` builds the same name from this prefix and
#: the throttle's ``requested_ref`` output; a structural test pins the agreement
#: so the producer and this selector cannot drift apart silently.
ARTIFACT_NAME_PREFIX = "release-identity-"

#: The production deploy job. Its conclusion is the "did this run ship?" signal.
DEPLOY_JOB_NAME = "Deploy to control.farna.dev"

#: 100 artifacts per page. One RC tag has a handful at most; the bound exists so
#: a pathological name can never page forever.
_MAX_ARTIFACT_PAGES = 10

#: Artifact names reject ``: < > | * ? \ / "`` and a leading/trailing dot. Git
#: refs already reject most of those, but this guard means a malformed tag fails
#: loudly instead of silently selecting nothing.
_ARTIFACT_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_GH_RUNNER = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Candidate:
    """One deploy run that records shipping the RC."""

    run_id: int
    deploy_job_conclusion: str
    run_status: str
    run_conclusion: str

    @property
    def state(self) -> str:
        """``shipped`` / ``failed`` / ``pending`` - derived, never assumed."""
        if self.deploy_job_conclusion == "success":
            return "shipped"
        if self.run_status == "completed":
            return "failed"
        return "pending"

    def describe(self) -> str:
        deploy = self.deploy_job_conclusion or "not finished"
        run = f"{self.run_status or 'unknown'}/{self.run_conclusion or '-'}"
        return f"run {self.run_id}: deploy job={deploy}, run={run} -> {self.state}"


@dataclass(frozen=True)
class Selection:
    """The verdict plus every candidate that was weighed."""

    verdict: str
    run_id: int | None
    candidates: tuple[Candidate, ...] = ()
    unreadable_run_ids: tuple[int, ...] = ()


def release_identity_artifact_name(rc_tag: str) -> str:
    """The artifact name ``deploy.yml`` uploads for ``rc_tag``."""
    if not _ARTIFACT_SAFE.match(rc_tag):
        msg = f"rc tag {rc_tag!r} cannot be used in an artifact name"
        raise ValueError(msg)
    return f"{ARTIFACT_NAME_PREFIX}{rc_tag}"


def _gh_api(endpoint: str, *, runner: _GH_RUNNER) -> Any | None:
    """GET ``endpoint`` via ``gh api`` and return the parsed JSON, or ``None``.

    ``None`` means "could not read" and is deliberately distinct from an empty
    result: an unreadable API must never be reported as "nothing was deployed".
    """
    try:
        completed = runner(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def candidate_run_ids(
    repo: str,
    rc_tag: str,
    *,
    runner: _GH_RUNNER = subprocess.run,
) -> tuple[int, ...] | None:
    """Run ids carrying the RC's identity artifact, or ``None`` if unreadable."""
    name = release_identity_artifact_name(rc_tag)
    run_ids: set[int] = set()
    for page in range(1, _MAX_ARTIFACT_PAGES + 1):
        payload = _gh_api(
            f"repos/{repo}/actions/artifacts?name={quote(name)}&per_page=100&page={page}",
            runner=runner,
        )
        if not isinstance(payload, dict):
            return None
        artifacts = payload.get("artifacts") or []
        if not isinstance(artifacts, list):
            return None
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            workflow_run = artifact.get("workflow_run") or {}
            run_id = workflow_run.get("id")
            if isinstance(run_id, int):
                run_ids.add(run_id)
        total = payload.get("total_count")
        if not artifacts or not isinstance(total, int) or page * 100 >= total:
            break
    return tuple(sorted(run_ids))


def describe_candidate(
    repo: str,
    run_id: int,
    *,
    runner: _GH_RUNNER = subprocess.run,
) -> Candidate | None:
    """Read one run's deploy-job conclusion and run status, or ``None``."""
    jobs_payload = _gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", runner=runner)
    if not isinstance(jobs_payload, dict):
        return None
    deploy_conclusion = ""
    for job in jobs_payload.get("jobs") or []:
        if isinstance(job, dict) and job.get("name") == DEPLOY_JOB_NAME:
            deploy_conclusion = str(job.get("conclusion") or "").lower()
            break
    run_payload = _gh_api(f"repos/{repo}/actions/runs/{run_id}", runner=runner)
    if not isinstance(run_payload, dict):
        return None
    return Candidate(
        run_id=run_id,
        deploy_job_conclusion=deploy_conclusion,
        run_status=str(run_payload.get("status") or "").lower(),
        run_conclusion=str(run_payload.get("conclusion") or "").lower(),
    )


def select(repo: str, rc_tag: str, *, runner: _GH_RUNNER = subprocess.run) -> Selection:
    """Pick the deploy run that shipped ``rc_tag``, never the newest one."""
    run_ids = candidate_run_ids(repo, rc_tag, runner=runner)
    if run_ids is None:
        return Selection("unknown", None)

    candidates: list[Candidate] = []
    unreadable: list[int] = []
    for run_id in run_ids:
        candidate = describe_candidate(repo, run_id, runner=runner)
        if candidate is None:
            unreadable.append(run_id)
        else:
            candidates.append(candidate)
    weighed = tuple(sorted(candidates, key=lambda candidate: candidate.run_id))

    # A run that actually shipped this RC wins outright.
    shipped = [candidate for candidate in weighed if candidate.state == "shipped"]
    if shipped:
        chosen = max(shipped, key=lambda candidate: candidate.run_id)
        return Selection("selected", chosen.run_id, weighed, tuple(unreadable))

    # Nothing shipped yet, but something is still trying: watch it.
    pending = [candidate for candidate in weighed if candidate.state == "pending"]
    if pending:
        chosen = max(pending, key=lambda candidate: candidate.run_id)
        return Selection("deploy_pending", chosen.run_id, weighed, tuple(unreadable))

    # A candidate we could not read might have shipped - do not claim otherwise.
    if unreadable:
        return Selection("unknown", None, weighed, tuple(unreadable))

    if any(candidate.state == "failed" for candidate in weighed):
        return Selection("deploy_failed", None, weighed)

    return Selection("not_deployed", None, weighed)


def render(selection: Selection, rc_tag: str) -> list[str]:
    """Narrative + annotation lines for the workflow log.

    Each verdict reads differently on purpose: a reader must never mistake
    "nothing tried to ship this RC" for "the RC's deploy lost".
    """
    lines = [f"Deploy-run selection for {rc_tag} (by what the run deployed, not by creation time)."]
    if selection.candidates:
        lines.append("Candidates considered (every run that records shipping this RC):")
        lines.extend(f"  - {candidate.describe()}" for candidate in selection.candidates)
    else:
        lines.append("Candidates considered: none - no deploy.yml run records shipping this RC.")
    if selection.unreadable_run_ids:
        ids = ", ".join(str(run_id) for run_id in selection.unreadable_run_ids)
        lines.append(f"  ! could not read deploy-run state for run(s): {ids}")

    if selection.verdict == "selected":
        shipped = [candidate for candidate in selection.candidates if candidate.state == "shipped"]
        lines.append(f"Selected deploy run {selection.run_id}: it records shipping {rc_tag} and its '{DEPLOY_JOB_NAME}' job concluded success.")
        if len(shipped) > 1:
            lines.append(f"{len(shipped)} runs record shipping {rc_tag}; the highest run id was chosen because deploy runs serialise on one concurrency group, so the last run to ship is the one production serves.")
    elif selection.verdict == "deploy_pending":
        lines.append(f"::warning::Deploy run {selection.run_id} records shipping {rc_tag} but has not finished; watching it for the outcome.")
    elif selection.verdict == "deploy_failed":
        lines.append(f"::error::The RC {rc_tag} has not been deployed: every deploy run that records shipping it finished without a successful production deploy (see candidates above). This is NOT a wrong deployment SHA - the RC's own deploy did not ship it; fix the deploy and re-run it.")
    elif selection.verdict == "not_deployed":
        lines.append(f"::warning::The RC {rc_tag} has not been deployed: no deploy.yml run records shipping it. Nothing has tried to ship this RC yet - this is neither a wrong deployment SHA nor a failed deploy, and it is a different diagnosis from both.")
    else:
        lines.append(f"::warning::Could not read the GitHub Actions API to select a deploy run for {rc_tag}; falling back to the wall-clock wait only. This does NOT mean the RC has not been deployed.")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select the deploy run that shipped an RC.")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--rc-tag", required=True, help="the RC tag, e.g. v0.3.330-rc3")
    parser.add_argument(
        "--github-output",
        default="",
        help="append verdict=/deploy_run_id= to this $GITHUB_OUTPUT-style file",
    )
    args = parser.parse_args(argv)

    try:
        release_identity_artifact_name(args.rc_tag)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2

    selection = select(args.repo, args.rc_tag)
    for line in render(selection, args.rc_tag):
        print(line)

    if args.github_output:
        run_id = "" if selection.run_id is None else str(selection.run_id)
        # ``args.github_output`` is a CLI argument supplied by the calling
        # workflow (rc-validate.yml passes a ``mktemp`` path); it is never user-
        # or model-supplied, so this S8707 taint flow is a false positive. The
        # values written are a fixed verdict enum and an int run id, so the
        # write cannot inject additional ``key=value`` lines either.
        with Path(args.github_output).open("a", encoding="utf-8") as handle:  # NOSONAR pythonsecurity:S8707
            handle.write(f"verdict={selection.verdict}\n")
            handle.write(f"deploy_run_id={run_id}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the workflows
    raise SystemExit(main())
