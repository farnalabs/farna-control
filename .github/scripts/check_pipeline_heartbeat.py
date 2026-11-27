#!/usr/bin/env python3
"""Pipeline heartbeat: fail loudly when a scheduled workflow stops firing.

Why this exists
---------------
The estate's fallback pollers were retuned to slow cadences (merge queue
hourly, watchdogs hourly or every 2 hours, macOS staple every 3 hours) because
GitHub demonstrably DROPS high-frequency cron events on busy repositories. A
slow poller is only safe if something notices when a scheduled workflow stops
firing altogether: otherwise the fallback dies silently and nothing recovers
the work it existed to pick up.

This is that one monitor. It reads ``.github/pipeline-heartbeat.json`` (the
declared cadence of every scheduled pipeline), asks the Actions API for the
most recent qualifying run of each workflow, and fails the run with one
``::error::`` per pipeline that has been silent for longer than its declared
window.

Evidence rule
-------------
A run counts as evidence that a pipeline is alive when the workflow was started
by the scheduler or by a human: ``schedule`` or ``workflow_dispatch``. A
``push`` / ``pull_request`` / ``repository_dispatch`` run proves the workflow
file exists but says nothing about whether the cron still fires - and three of
this repo's scheduled workflows also run on push or pull_request, so counting
those would let a dead schedule hide behind unrelated activity.

The monitor is deliberately fail-LOUD in both directions:

* a silent pipeline exits 1 with an ``::error::`` naming the workflow, how long
  it has been silent, and the declared window;
* a workflow whose run history cannot be read (HTTP error, unreadable payload,
  a manifest entry naming a workflow that does not exist) also exits 1, because
  a monitor that cannot see must never report an all-clear.

A workflow with no qualifying run at all is silent by definition: ``None`` from
the API is "has never run", never "no news is good news".

Testability
-----------
Every decision is a pure function (:func:`parse_manifest`, :func:`assess`,
:func:`evaluate`) driven by an explicit ``now``, so
``backend/tests/unit/scripts/test_check_pipeline_heartbeat.py`` exercises every
threshold with no network, no clock and no GitHub. Only the thin HTTP seam
(:func:`fetch_last_run`) talks to the API, and it takes its opener as an
argument so a fake response can drive it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The manifest the workflow reads. Relative to the repository root (the
#: workflow runs from a checkout of the default branch).
DEFAULT_MANIFEST = Path(".github") / "pipeline-heartbeat.json"

DEFAULT_API_URL = "https://api.github.com"

#: Trigger events that count as evidence a pipeline is alive. See the module
#: docstring: a push / pull_request / repository_dispatch run is NOT evidence
#: that the scheduler still fires.
EVIDENCE_EVENTS: tuple[str, ...] = ("schedule", "workflow_dispatch")

#: Request timeout, seconds. Bounded so a hung API call fails the job instead of
#: holding the run open until its job-level timeout.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: The only scheme this monitor will build a request for (GitHub's API, including
#: enterprise installations, is https).
_ALLOWED_URL_SCHEMES = frozenset({"https"})

#: Exit codes: 1 = a pipeline is silent or unreadable (the finding), 2 = the
#: monitor could not run at all (bad manifest, missing token, bad arguments).
EXIT_FINDING = 1
EXIT_USAGE = 2

#: The three mutually exclusive states a monitored pipeline can be in.
FRESH = "fresh"
SILENT = "silent"
UNREADABLE = "unreadable"

_REQUIRED_ENTRY_KEYS = frozenset({"workflow", "expected_max_silence_minutes"})
_OPTIONAL_ENTRY_KEYS = frozenset({"note"})


class ManifestError(ValueError):
    """The heartbeat manifest is malformed.

    Raised loudly instead of defaulting: a manifest typo that silently widened a
    window (or dropped an entry) would disable the check it exists to perform.
    """


class GithubApiError(RuntimeError):
    """The Actions API could not answer for one workflow.

    Distinct from "silent": silence is a measured fact, an unreadable history is
    an absence of measurement. Both fail the run, with different wording.
    """


@dataclass(frozen=True)
class Pipeline:
    """One declared scheduled pipeline."""

    workflow: str
    expected_max_silence_minutes: int
    note: str = ""


@dataclass(frozen=True)
class HeartbeatVerdict:
    """One pipeline's state: exactly one of fresh, silent or unreadable.

    The three are mutually exclusive by construction (``state`` is a single
    value) rather than by two booleans, so an unreadable history can never also
    be reported as healthy *or* as a measured silence. Collapsing "could not
    read" into "silent" invents a finding; collapsing it into "healthy" invents
    an all-clear.
    """

    workflow: str
    state: str
    expected_max_silence_minutes: int
    last_run_at: datetime | None = None
    age_minutes: int | None = None
    note: str = ""
    #: Why the run history could not be read (only set when state is UNREADABLE).
    detail: str = ""

    @property
    def silent(self) -> bool:
        return self.state == SILENT

    @property
    def unreadable(self) -> bool:
        return self.state == UNREADABLE

    @property
    def reason(self) -> str:
        """A sentence naming the measurement and the window."""
        if self.unreadable:
            return f"its run history could not be read: {self.detail}"
        if self.last_run_at is None:
            return f"no scheduled or manually dispatched run was ever recorded (window {self.expected_max_silence_minutes} min)"
        return f"last qualifying run started {self.age_minutes} min ago (window {self.expected_max_silence_minutes} min)"

    @property
    def annotation(self) -> str:
        """The Actions annotation for this pipeline (``::error::`` unless fresh)."""
        if self.unreadable:
            return f"::error::Pipeline heartbeat could not read the run history of {self.workflow}: {self.detail}"
        level = "error" if self.silent else "notice"
        state = "is SILENT" if self.silent else "is healthy"
        suffix = f" {self.note}" if self.note else ""
        return f"::{level}::Scheduled pipeline {self.workflow} {state}: {self.reason}.{suffix}".rstrip()


# ---------------------------------------------------------------------------
# Manifest parsing (pure)
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> tuple[Pipeline, ...]:
    """Read and validate the manifest at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read heartbeat manifest {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"heartbeat manifest {path} is not valid JSON: {exc}") from exc
    return parse_manifest(document)


def parse_manifest(document: Any) -> tuple[Pipeline, ...]:
    """Validate a manifest document and return its pipelines.

    Top-level keys are ``pipelines`` plus any ``_``-prefixed commentary key.
    Unknown per-entry keys are rejected, so a typo like
    ``expected_max_silence_minute`` fails the run instead of being ignored.
    """
    if not isinstance(document, dict):
        raise ManifestError("heartbeat manifest root must be a JSON object")

    unexpected = sorted(str(key) for key in document if key != "pipelines" and not str(key).startswith("_"))
    if unexpected:
        raise ManifestError("unexpected top-level manifest key(s): " + ", ".join(unexpected))

    entries = document.get("pipelines")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("heartbeat manifest must carry a non-empty 'pipelines' list")

    seen: set[str] = set()
    pipelines: list[Pipeline] = []
    for index, entry in enumerate(entries):
        pipeline = _parse_entry(index, entry, seen)
        seen.add(pipeline.workflow)
        pipelines.append(pipeline)
    return tuple(pipelines)


def _parse_entry(index: int, entry: Any, seen: set[str]) -> Pipeline:
    where = f"pipelines[{index}]"
    if not isinstance(entry, dict):
        raise ManifestError(f"{where} must be a JSON object")

    keys = {str(key) for key in entry}
    missing = sorted(_REQUIRED_ENTRY_KEYS - keys)
    if missing:
        raise ManifestError(f"{where} is missing required key(s): " + ", ".join(missing))
    unknown = sorted(keys - _REQUIRED_ENTRY_KEYS - _OPTIONAL_ENTRY_KEYS)
    if unknown:
        raise ManifestError(f"{where} has unexpected key(s): " + ", ".join(unknown))

    workflow = entry["workflow"]
    if not isinstance(workflow, str) or not workflow.strip():
        raise ManifestError(f"{where}.workflow must be a non-empty string")
    workflow = workflow.strip()
    if "/" in workflow or "\\" in workflow:
        raise ManifestError(f"{where}.workflow must be a workflow filename, not a path: {workflow!r}")
    if not workflow.endswith((".yml", ".yaml")):
        raise ManifestError(f"{where}.workflow must name a .yml or .yaml file: {workflow!r}")
    if workflow in seen:
        raise ManifestError(f"{where}.workflow is declared twice: {workflow!r}")

    window = entry["expected_max_silence_minutes"]
    # bool is an int subclass; a JSON `true` here is a typo, not a window.
    if isinstance(window, bool) or not isinstance(window, int):
        raise ManifestError(f"{where}.expected_max_silence_minutes must be an integer number of minutes")
    if window <= 0:
        raise ManifestError(f"{where}.expected_max_silence_minutes must be positive, got {window}")

    note = entry.get("note", "")
    if not isinstance(note, str):
        raise ManifestError(f"{where}.note must be a string")

    return Pipeline(workflow=workflow, expected_max_silence_minutes=window, note=note.strip())


# ---------------------------------------------------------------------------
# Decision (pure)
# ---------------------------------------------------------------------------


def assess(pipeline: Pipeline, *, last_run_at: datetime | None, now: datetime) -> HeartbeatVerdict:
    """Decide whether one pipeline is silent.

    ``last_run_at`` is the most recent qualifying run's creation time (aware,
    UTC), or ``None`` when no qualifying run exists. ``None`` is silent: a
    workflow that has never run has never proven it can run.

    The boundary is inclusive (``age >= window`` is silent), matching the rest
    of the estate's threshold checks: a pipeline exactly at its declared window
    has been silent for that long, not "almost".
    """
    if last_run_at is None:
        return HeartbeatVerdict(
            workflow=pipeline.workflow,
            state=SILENT,
            expected_max_silence_minutes=pipeline.expected_max_silence_minutes,
            last_run_at=None,
            age_minutes=None,
            note=pipeline.note,
        )

    # A future timestamp (clock skew, or a run recorded with a bad clock) is
    # fresh rather than ancient: clamp so the age can never render negative.
    age_minutes = max(0, int((now - last_run_at).total_seconds() // 60))
    return HeartbeatVerdict(
        workflow=pipeline.workflow,
        state=SILENT if age_minutes >= pipeline.expected_max_silence_minutes else FRESH,
        expected_max_silence_minutes=pipeline.expected_max_silence_minutes,
        last_run_at=last_run_at,
        age_minutes=age_minutes,
        note=pipeline.note,
    )


def mark_unreadable(pipeline: Pipeline, detail: str) -> HeartbeatVerdict:
    """The verdict for a pipeline whose run history could not be read.

    Not a silence (nothing was measured) and never healthy: the run fails with
    the read error so the gap is visible instead of guessed at.
    """
    return HeartbeatVerdict(
        workflow=pipeline.workflow,
        state=UNREADABLE,
        expected_max_silence_minutes=pipeline.expected_max_silence_minutes,
        note=pipeline.note,
        detail=detail,
    )


def evaluate(
    pipelines: Sequence[Pipeline],
    *,
    last_runs: Mapping[str, datetime | None],
    errors: Mapping[str, str] | None = None,
    now: datetime,
) -> list[HeartbeatVerdict]:
    """Assess every declared pipeline, in manifest order.

    A workflow absent from ``last_runs`` has never produced a qualifying run.
    A workflow named in ``errors`` could not be read at all, which is its own
    state: it is never folded into silence or into fresh.
    """
    errors = errors or {}
    verdicts: list[HeartbeatVerdict] = []
    for pipeline in pipelines:
        detail = errors.get(pipeline.workflow)
        if detail:
            verdicts.append(mark_unreadable(pipeline, detail))
            continue
        verdicts.append(assess(pipeline, last_run_at=last_runs.get(pipeline.workflow), now=now))
    return verdicts


def silent_verdicts(verdicts: Sequence[HeartbeatVerdict]) -> list[HeartbeatVerdict]:
    """The measured silences, in manifest order."""
    return [verdict for verdict in verdicts if verdict.silent]


def unreadable_verdicts(verdicts: Sequence[HeartbeatVerdict]) -> list[HeartbeatVerdict]:
    """The pipelines whose run history could not be read, in manifest order."""
    return [verdict for verdict in verdicts if verdict.unreadable]


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


def parse_timestamp(raw: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime.

    ``None`` for an absent or unparseable value; the caller decides whether that
    is silence or an error.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# GitHub API (the only impure seam)
# ---------------------------------------------------------------------------


def _http_json(url: str, *, token: str, opener: Callable[..., Any], timeout: float) -> Any:
    """GET ``url`` and decode its JSON body. Every failure is a GithubApiError."""
    # S310: refuse anything but https before the request is built. The URL is
    # assembled from the API base plus a workflow name the manifest validated
    # (no separators), so a non-https scheme can only come from an --api-url
    # override - which must not be able to turn this into a file:// read.
    if urllib.parse.urlsplit(url).scheme not in _ALLOWED_URL_SCHEMES:
        raise GithubApiError(f"refusing a non-https API URL: {url}")
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "farna-control-pipeline-heartbeat",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise GithubApiError(f"HTTP {exc.code} from {url}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GithubApiError(f"could not reach {url}: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GithubApiError(f"{url} did not answer with JSON: {exc}") from exc


def fetch_last_run(
    workflow: str,
    *,
    repo: str,
    token: str,
    api_url: str = DEFAULT_API_URL,
    opener: Callable[..., Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> datetime | None:
    """The most recent qualifying run's creation time for ``workflow``.

    One request per accepted event (``schedule`` and ``workflow_dispatch``), each
    asking for a single run, because the API can filter on ``event`` - so a
    burst of push-triggered runs of the same workflow can never bury the
    scheduled evidence we are looking for. Returns the newest of the answers, or
    ``None`` when neither event has ever produced a run.
    """
    http_opener = opener or urllib.request.urlopen
    base = api_url.rstrip("/")
    encoded = urllib.parse.quote(workflow, safe="")
    newest: datetime | None = None
    for event in EVIDENCE_EVENTS:
        url = f"{base}/repos/{repo}/actions/workflows/{encoded}/runs?event={event}&per_page=1"
        payload = _http_json(url, token=token, opener=http_opener, timeout=timeout)
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise GithubApiError(f"{url} did not answer with a 'workflow_runs' list")
        if not runs:
            continue
        first = runs[0]
        created_at = parse_timestamp(first.get("created_at") if isinstance(first, dict) else None)
        if created_at is None:
            # A run exists but its timestamp is unreadable: reporting "never ran"
            # would be a false alert, and reporting "fresh" would be a false
            # all-clear. Neither is acceptable, so fail loudly instead.
            raise GithubApiError(f"{url} returned a run with an unreadable created_at")
        if newest is None or created_at > newest:
            newest = created_at
    return newest


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_summary(verdicts: Sequence[HeartbeatVerdict]) -> str:
    """A Markdown table for the workflow's step summary."""
    lines = [
        "## Pipeline heartbeat",
        "",
        "| Workflow | Last qualifying run | Window | State |",
        "|----------|--------------------|--------|-------|",
    ]
    for verdict in verdicts:
        if verdict.unreadable:
            last = "not read"
            state = "**UNREADABLE**"
        else:
            last = "never" if verdict.age_minutes is None else f"{verdict.age_minutes} min ago"
            state = "**SILENT**" if verdict.silent else "ok"
        lines.append(f"| `{verdict.workflow}` | {last} | {verdict.expected_max_silence_minutes} min | {state} |")
    silent = silent_verdicts(verdicts)
    unreadable = unreadable_verdicts(verdicts)
    lines.extend(
        [
            "",
            f"{len(verdicts) - len(silent) - len(unreadable)}/{len(verdicts)} pipelines inside their window ({len(silent)} silent, {len(unreadable)} unreadable).",
        ]
    )
    return "\n".join(lines)


def _write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(markdown + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail when a scheduled pipeline has stopped firing.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="heartbeat manifest path")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""), help="owner/name (default: $GITHUB_REPOSITORY)")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="API token (default: $GITHUB_TOKEN)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="GitHub API base URL")
    parser.add_argument("--now", default="", help="ISO-8601 clock override for a dry run (default: the real clock)")
    args = parser.parse_args(argv)

    now = datetime.now(UTC) if not args.now else parse_timestamp(args.now)
    if now is None:
        print(f"::error::--now is not an ISO-8601 timestamp: {args.now}", file=sys.stderr)
        return EXIT_USAGE

    try:
        pipelines = load_manifest(args.manifest)
    except ManifestError as exc:
        print(f"::error::Pipeline heartbeat cannot run: {exc}")
        return EXIT_USAGE

    if not args.repo or not args.token:
        print("::error::Pipeline heartbeat cannot run: GITHUB_REPOSITORY and GITHUB_TOKEN (or --repo/--token) are required. Without them the run history is unreadable, so no all-clear is possible.")
        return EXIT_USAGE

    last_runs: dict[str, datetime | None] = {}
    errors: dict[str, str] = {}
    for pipeline in pipelines:
        try:
            last_runs[pipeline.workflow] = fetch_last_run(
                pipeline.workflow,
                repo=args.repo,
                token=args.token,
                api_url=args.api_url,
            )
        except GithubApiError as exc:
            errors[pipeline.workflow] = str(exc)

    verdicts = evaluate(pipelines, last_runs=last_runs, errors=errors, now=now)

    # An unreadable history is reported first and as its own failure: it is not a
    # silence, and it must never be echoed as one.
    unreadable = unreadable_verdicts(verdicts)
    for verdict in unreadable:
        print(verdict.annotation)
    silent = silent_verdicts(verdicts)
    for verdict in silent:
        print(verdict.annotation)

    _write_step_summary(render_summary(verdicts))

    if silent or unreadable:
        print(f"::error::{len(silent)} of {len(verdicts)} scheduled pipeline(s) are silent and {len(unreadable)} could not be read.")
        return EXIT_FINDING

    print(f"::notice::All {len(verdicts)} scheduled pipelines fired inside their window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
