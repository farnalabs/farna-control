"""Unit tests for ``.github/scripts/check_pipeline_heartbeat.py``.

The heartbeat is the one thing that notices when a scheduled workflow stops
firing at all, so its judgement must be proved rather than hoped for: a monitor
that is too eager pages on a late tick, and one that is too lax is exactly the
blind spot it exists to close.

Every decision is exercised here with no network, no clock and no GitHub, plus
two seams that are worth pinning because they are where the monitor could
quietly stop seeing:

* **Which trigger events count as evidence.** A ``push`` or ``pull_request`` run
  proves the workflow file is alive but says nothing about the scheduler, and
  several monitored workflows also run on push. The API is therefore asked for
  exactly the ``schedule`` and ``workflow_dispatch`` runs, and that is asserted
  on the URLs the script builds.
* **The shipped manifest.** It must declare every workflow that carries a
  ``schedule:`` trigger, no entry may name a workflow that is not scheduled, and
  each window must be sane against that workflow's real cadence - a manifest
  that drifts from the tree silently disables the check it exists to perform.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import pytest
import yaml

for parent in Path(__file__).resolve().parents:
    _script_path = parent / ".github" / "scripts" / "check_pipeline_heartbeat.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/check_pipeline_heartbeat.py")

_REPO_ROOT = parent

_loader = SourceFileLoader("check_pipeline_heartbeat", str(_script_path))
heartbeat = module_from_spec(spec_from_loader("check_pipeline_heartbeat", _loader))
# dataclasses resolves the defining module through sys.modules, so the loaded
# module must be registered before it executes.
sys.modules["check_pipeline_heartbeat"] = heartbeat
_loader.exec_module(heartbeat)

_NOW = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)

_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_MANIFEST = _REPO_ROOT / ".github" / "pipeline-heartbeat.json"

#: The monitor itself is the one scheduled workflow the manifest cannot declare:
#: a scheduler that has stopped firing cannot run the monitor that would report
#: it, so self-monitoring can only raise a false alert on its own first run.
_SELF_WORKFLOW = "pipeline-heartbeat.yml"

_WEEKLY_WINDOW_MINUTES = 8 * 24 * 60


def _run(created_at: str) -> dict[str, Any]:
    return {"created_at": created_at, "event": "schedule", "status": "completed"}


# ---------------------------------------------------------------------------
# The four decisions that matter
# ---------------------------------------------------------------------------


def test_a_silent_workflow_fails() -> None:
    """A daily pipeline last seen three days ago has stopped firing."""
    pipeline = heartbeat.Pipeline("dast.yml", expected_max_silence_minutes=3000)
    last_run_at = _NOW - timedelta(minutes=3 * 24 * 60)

    verdict = heartbeat.assess(pipeline, last_run_at=last_run_at, now=_NOW)

    assert verdict.silent is True
    assert verdict.age_minutes == 3 * 24 * 60
    assert "dast.yml" in verdict.annotation
    assert "::error::" in verdict.annotation


def test_a_fresh_workflow_passes() -> None:
    """An hourly pipeline seen 20 minutes ago is healthy."""
    pipeline = heartbeat.Pipeline("merge-queue.yml", expected_max_silence_minutes=180)
    last_run_at = _NOW - timedelta(minutes=20)

    verdict = heartbeat.assess(pipeline, last_run_at=last_run_at, now=_NOW)

    assert verdict.silent is False
    assert verdict.age_minutes == 20
    assert "::notice::" in verdict.annotation


def test_a_workflow_that_has_never_run_fails() -> None:
    """No qualifying run at all is silence, not an all-clear."""
    pipeline = heartbeat.Pipeline("provider-contract-probe.yml", expected_max_silence_minutes=3000)

    verdict = heartbeat.assess(pipeline, last_run_at=None, now=_NOW)

    assert verdict.silent is True
    assert verdict.age_minutes is None
    assert "no scheduled or manually dispatched run" in verdict.reason


def test_a_weekly_workflow_inside_its_eight_day_window_passes() -> None:
    """A weekly job is not silent at 7.5 days: one skipped Monday is not an alert."""
    pipeline = heartbeat.Pipeline(
        "mutation-testing.yml",
        expected_max_silence_minutes=_WEEKLY_WINDOW_MINUTES,
    )
    last_run_at = _NOW - timedelta(days=7, hours=12)

    verdict = heartbeat.assess(pipeline, last_run_at=last_run_at, now=_NOW)

    assert verdict.silent is False
    assert verdict.age_minutes == 7 * 24 * 60 + 12 * 60


def test_a_weekly_workflow_past_its_eight_day_window_fails() -> None:
    """The other half of the weekly boundary: eight days and a minute is silent."""
    pipeline = heartbeat.Pipeline(
        "windows-artifacts.yml",
        expected_max_silence_minutes=_WEEKLY_WINDOW_MINUTES,
    )
    last_run_at = _NOW - timedelta(minutes=_WEEKLY_WINDOW_MINUTES + 1)

    verdict = heartbeat.assess(pipeline, last_run_at=last_run_at, now=_NOW)

    assert verdict.silent is True


def test_the_window_boundary_is_inclusive() -> None:
    """At exactly the declared window a pipeline counts as silent, as elsewhere."""
    pipeline = heartbeat.Pipeline("audit-chain-monitor.yml", expected_max_silence_minutes=780)

    at_window = heartbeat.assess(pipeline, last_run_at=_NOW - timedelta(minutes=780), now=_NOW)
    one_minute_under = heartbeat.assess(pipeline, last_run_at=_NOW - timedelta(minutes=779), now=_NOW)

    assert at_window.silent is True
    assert one_minute_under.silent is False


def test_a_future_timestamp_is_fresh_not_ancient() -> None:
    """Clock skew must not manufacture an alert, and must not render a negative age."""
    pipeline = heartbeat.Pipeline("web-watchdog.yml", expected_max_silence_minutes=180)

    verdict = heartbeat.assess(pipeline, last_run_at=_NOW + timedelta(minutes=15), now=_NOW)

    assert verdict.silent is False
    assert verdict.age_minutes == 0


def test_evaluate_reports_only_the_silent_pipelines() -> None:
    """One pass over a mixed manifest names exactly the pipelines that stopped."""
    pipelines = (
        heartbeat.Pipeline("merge-queue.yml", 180),
        heartbeat.Pipeline("tier1b-nightly.yml", 3000),
        heartbeat.Pipeline("signing-canary.yml", 3000),
    )
    last_runs = {
        "merge-queue.yml": _NOW - timedelta(minutes=90),
        "tier1b-nightly.yml": _NOW - timedelta(days=4),
        # signing-canary.yml is absent: it has never run.
    }

    verdicts = heartbeat.evaluate(pipelines, last_runs=last_runs, now=_NOW)
    silent = heartbeat.silent_verdicts(verdicts)

    assert [verdict.workflow for verdict in silent] == ["tier1b-nightly.yml", "signing-canary.yml"]


def test_an_unreadable_history_is_never_reported_as_silence() -> None:
    """PROVE-THE-FIX: 'could not read' and 'silent' are different findings.

    Collapsing them invents a reading the monitor never took (and prints a
    misleading "never recorded"), so the unreadable state is its own verdict.
    """
    pipelines = (
        heartbeat.Pipeline("merge-queue.yml", 180),
        heartbeat.Pipeline("dast.yml", 3000),
    )
    last_runs = {"dast.yml": _NOW - timedelta(minutes=30)}

    verdicts = heartbeat.evaluate(pipelines, last_runs=last_runs, errors={"merge-queue.yml": "HTTP 404"}, now=_NOW)
    unreadable = heartbeat.unreadable_verdicts(verdicts)

    assert [verdict.workflow for verdict in unreadable] == ["merge-queue.yml"]
    assert not heartbeat.silent_verdicts(verdicts)
    assert unreadable[0].silent is False
    assert "could not read the run history" in unreadable[0].annotation
    assert "SILENT" not in unreadable[0].annotation


def test_the_step_summary_distinguishes_unreadable_from_silent() -> None:
    """The table an operator reads must not blur the two failure modes."""
    verdicts = (
        heartbeat.assess(heartbeat.Pipeline("merge-queue.yml", 180), last_run_at=None, now=_NOW),
        heartbeat.mark_unreadable(heartbeat.Pipeline("dast.yml", 3000), "HTTP 403"),
        heartbeat.assess(heartbeat.Pipeline("web-watchdog.yml", 180), last_run_at=_NOW, now=_NOW),
    )

    summary = heartbeat.render_summary(verdicts)

    assert "**SILENT**" in summary
    assert "**UNREADABLE**" in summary
    assert "1/3 pipelines inside their window (1 silent, 1 unreadable)." in summary


# ---------------------------------------------------------------------------
# API seam: what counts as evidence, and what happens when it cannot be read
# ---------------------------------------------------------------------------


def _opener(responses: dict[str, list[dict[str, Any]]], seen: list[str]) -> Any:
    """A fake urlopen answering per requested ``event`` and recording the URL."""

    def opener(request: Any, timeout: float | None = None) -> io.BytesIO:
        url = request.full_url
        seen.append(url)
        event = "workflow_dispatch" if "event=workflow_dispatch" in url else "schedule"
        return io.BytesIO(json.dumps({"workflow_runs": responses[event]}).encode("utf-8"))

    return opener


def test_fetch_last_run_asks_only_for_scheduler_or_human_runs() -> None:
    """The evidence rule: push / pull_request / repository_dispatch are excluded."""
    seen: list[str] = []
    opener = _opener({"schedule": [], "workflow_dispatch": []}, seen)

    heartbeat.fetch_last_run("k8s-conformance.yml", repo="farnalabs/farna-control", token="t", opener=opener)

    assert seen == [
        "https://api.github.com/repos/farnalabs/farna-control/actions/workflows/k8s-conformance.yml/runs?event=schedule&per_page=1",
        "https://api.github.com/repos/farnalabs/farna-control/actions/workflows/k8s-conformance.yml/runs?event=workflow_dispatch&per_page=1",
    ]


def test_fetch_last_run_takes_the_newest_of_the_two_events() -> None:
    """A manual dispatch after the last scheduled run is the newer evidence."""
    seen: list[str] = []
    opener = _opener(
        {
            "schedule": [_run("2026-11-01T00:00:00Z")],
            "workflow_dispatch": [_run("2026-11-20T00:00:00Z")],
        },
        seen,
    )

    newest = heartbeat.fetch_last_run("merge-queue.yml", repo="farnalabs/farna-control", token="t", opener=opener)

    assert newest == datetime(2026, 11, 20, tzinfo=UTC)


def test_fetch_last_run_returns_none_when_neither_event_has_run() -> None:
    """Never run is ``None`` - the caller decides that it means silent."""
    opener = _opener({"schedule": [], "workflow_dispatch": []}, [])

    newest = heartbeat.fetch_last_run("dast.yml", repo="farnalabs/farna-control", token="t", opener=opener)

    assert newest is None


def test_fetch_last_run_fails_loud_on_an_api_error() -> None:
    """A monitor that cannot read must raise, never report an all-clear."""

    def opener(request: Any, timeout: float | None = None) -> io.BytesIO:
        raise urllib.error.HTTPError(request.full_url, 403, "rate limited", {}, None)

    with pytest.raises(heartbeat.GithubApiError, match="HTTP 403"):
        heartbeat.fetch_last_run("dast.yml", repo="farnalabs/farna-control", token="t", opener=opener)


def test_fetch_last_run_fails_loud_when_a_run_has_no_readable_timestamp() -> None:
    """A run whose created_at cannot be parsed is an error, not 'never ran'."""

    def opener(request: Any, timeout: float | None = None) -> io.BytesIO:
        return io.BytesIO(json.dumps({"workflow_runs": [{"created_at": "not-a-timestamp"}]}).encode("utf-8"))

    with pytest.raises(heartbeat.GithubApiError, match="unreadable created_at"):
        heartbeat.fetch_last_run("dast.yml", repo="farnalabs/farna-control", token="t", opener=opener)


def test_fetch_last_run_refuses_a_non_https_api_url() -> None:
    """An overridden base URL must not be able to turn the read into a local file read."""

    def opener(request: Any, timeout: float | None = None) -> io.BytesIO:
        raise AssertionError("the opener must not be called for a rejected scheme")

    with pytest.raises(heartbeat.GithubApiError, match="non-https"):
        heartbeat.fetch_last_run("dast.yml", repo="farnalabs/farna-control", token="t", api_url="file:///etc", opener=opener)


# ---------------------------------------------------------------------------
# Manifest validation: a typo must fail the run, not disable the check
# ---------------------------------------------------------------------------


def test_load_manifest_reads_the_shipped_manifest() -> None:
    """The artefact the workflow actually reads parses and carries entries."""
    pipelines = heartbeat.load_manifest(_MANIFEST)

    assert pipelines
    assert all(pipeline.expected_max_silence_minutes > 0 for pipeline in pipelines)


def test_manifest_rejects_an_unknown_entry_key() -> None:
    """A misspelt window key would silently drop a required field, so reject it."""
    document = {"pipelines": [{"workflow": "dast.yml", "expected_max_silence_minutse": 3000}]}

    with pytest.raises(heartbeat.ManifestError):
        heartbeat.parse_manifest(document)


def test_manifest_rejects_a_bool_window() -> None:
    """JSON ``true`` is an int subclass; it is a typo, not a window."""
    document = {"pipelines": [{"workflow": "dast.yml", "expected_max_silence_minutes": True}]}

    with pytest.raises(heartbeat.ManifestError):
        heartbeat.parse_manifest(document)


def test_manifest_rejects_a_duplicate_workflow() -> None:
    document = {
        "pipelines": [
            {"workflow": "dast.yml", "expected_max_silence_minutes": 3000},
            {"workflow": "dast.yml", "expected_max_silence_minutes": 60},
        ]
    }

    with pytest.raises(heartbeat.ManifestError):
        heartbeat.parse_manifest(document)


def test_manifest_rejects_a_path_instead_of_a_filename() -> None:
    """The API takes a filename; a path would 404 and read as 'never run'."""
    document = {"pipelines": [{"workflow": ".github/workflows/dast.yml", "expected_max_silence_minutes": 3000}]}

    with pytest.raises(heartbeat.ManifestError):
        heartbeat.parse_manifest(document)


# ---------------------------------------------------------------------------
# The shipped manifest must keep matching the workflow tree
# ---------------------------------------------------------------------------


def _scheduled_workflows() -> dict[str, list[str]]:
    """Every workflow filename carrying a ``schedule:`` trigger, and its crons."""
    scheduled: dict[str, list[str]] = {}
    for path in sorted(list(_WORKFLOWS.glob("*.yml")) + list(_WORKFLOWS.glob("*.yaml"))):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        on_block = document.get("on", document.get(True)) if isinstance(document, dict) else None
        if not isinstance(on_block, dict) or "schedule" not in on_block:
            continue
        entries = on_block.get("schedule") or []
        crons = [entry["cron"] for entry in entries if isinstance(entry, dict) and entry.get("cron")]
        scheduled[path.name] = crons
    return scheduled


def _minimum_interval_minutes(cron: str) -> int:
    """The shortest gap a five-field cron can fire on, in minutes.

    Only the shapes this repo uses are modelled (``*``, ``*/N``, and literal
    hours with an optional day-of-week), so an unsupported expression fails
    loudly here instead of being silently mis-estimated.
    """
    fields = cron.split()
    assert len(fields) == 5, f"unsupported cron expression: {cron!r}"
    _minute, hour, day_of_month, _month, day_of_week = fields
    if hour.startswith("*/"):
        return int(hour[2:]) * 60
    if hour == "*":
        return 60
    if day_of_month == "*" and day_of_week != "*":
        return 7 * 24 * 60
    if day_of_month == "*" and day_of_week == "*":
        return 24 * 60
    raise AssertionError(f"unsupported cron expression: {cron!r}")


def test_the_manifest_declares_every_scheduled_workflow() -> None:
    """A new scheduled workflow must be declared, or it is silently unwatched."""
    scheduled = _scheduled_workflows()
    assert _SELF_WORKFLOW in scheduled, f"{_SELF_WORKFLOW} no longer carries a schedule: trigger - the self-monitoring exemption is stale"
    declared = {pipeline.workflow for pipeline in heartbeat.load_manifest(_MANIFEST)}

    missing = sorted(set(scheduled) - declared - {_SELF_WORKFLOW})
    assert not missing, "scheduled workflow(s) missing from .github/pipeline-heartbeat.json: " + ", ".join(missing)


def test_the_manifest_declares_no_workflow_that_is_not_scheduled() -> None:
    """A stale entry names a workflow that can never produce the evidence."""
    scheduled = _scheduled_workflows()
    declared = {pipeline.workflow for pipeline in heartbeat.load_manifest(_MANIFEST)}

    phantom = sorted(declared - set(scheduled))
    assert not phantom, "manifest entry/entries name workflow(s) with no schedule: trigger: " + ", ".join(phantom)


def test_every_window_is_sane_against_its_workflow_cadence() -> None:
    """Short enough to catch a dead pipeline, long enough not to page on a late tick."""
    scheduled = _scheduled_workflows()
    problems: list[str] = []
    for pipeline in heartbeat.load_manifest(_MANIFEST):
        interval = min(_minimum_interval_minutes(cron) for cron in scheduled[pipeline.workflow])
        window = pipeline.expected_max_silence_minutes
        if window < interval + 60:
            problems.append(f"{pipeline.workflow}: window {window} min is under one interval plus an hour (interval {interval} min) - an ordinary late or skipped tick would alert")
        if window > interval * 2 + 24 * 60:
            problems.append(f"{pipeline.workflow}: window {window} min is over two intervals plus a day (interval {interval} min) - a dead pipeline would stay invisible for too long")
    assert not problems, "heartbeat window(s) out of band:\n" + "\n".join(problems)
