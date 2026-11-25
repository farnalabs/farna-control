"""Build the GitHub issue body for a failing provider contract probe run (FAR-2703).

The nightly probe (``scripts/provider_contract_probe.py``) writes
``contract-goldens/report.json`` and exits non-zero on drift, on a probe error or
on a run that attempted nothing. A red scheduled run is easy to miss, so the
workflow's final ``if: failure()`` step opens (or refreshes) one de-duplicated
issue carrying this body.

Why this is a script and not a heredoc in the workflow
======================================================

It shipped as an inline ``python3 - <<'PY'`` heredoc, and a defect lived there
unseen: the bootstrap predicate only checked ``attempted == 0`` plus the skip
reasons, so a real ``error`` row that never issued a request (a malformed golden,
say) classified the run as the expected bootstrap red and suppressed the issue -
turning a genuine failure into silence. Nothing executed the heredoc, so nothing
could catch it. A module can be unit-tested (``backend/tests/unit/tools/
test_provider_contract_probe_report.py``); a heredoc cannot.

Bootstrap is the deliberate state in which no golden has been recorded live yet:
zero attempts and every skip is "not yet recorded". That red is expected and must
not page every night. Every other shape must produce a body, and in particular an
``error`` or ``unavailable`` row is NEVER bootstrap, however few probes were
attempted.

Usage (from the workflow step, stdin is not used)::

    python3 scripts/provider_contract_probe_report.py REPORT BODY_FILE RUN_URL

Exit codes: 0 = body written (or deliberately nothing to raise), 1 = the body
file could not be written, 2 = wrong argument count. An empty body file is the
signal to raise nothing; the caller tests it with ``[ ! -s ... ]``.

Stdlib only: this runs on the runner before the project's virtualenv is on the
path for this step, so it must not import a third-party package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: Summary counters rendered in the issue, in report order.
SUMMARY_KEYS = ("attempted", "passed", "drifted", "unavailable", "errors", "skipped", "skipped_unexcused")

#: Row statuses that always need a human, and can never be the bootstrap red.
FAILURE_STATUSES = frozenset({"drift", "error", "unavailable"})

#: The skip reason the probe emits while no golden has been recorded live yet.
_UNRECORDED_REASON = "not yet recorded"


def _header(run_url: str) -> str:
    return f"Nightly provider contract probe failed. Latest failing run: {run_url}"


def _is_bootstrap(summary: dict[str, Any], results: list[dict[str, Any]]) -> bool:
    """Return True only for the expected red: nothing recorded live yet.

    Bootstrap requires ALL of: no probe issued a request, at least one skip, every
    skip's reason says the golden is not yet recorded, and the run has no error
    and no unavailable probe.

    The error/unavailable clauses are the whole point. A row can be an ``error``
    without issuing a request (a malformed golden entry is reported before the
    call is made), so ``attempted == 0`` alone is not evidence that this is the
    bootstrap red: it can also be a broken run. Suppressing that would hide a
    real failure until someone opened the artifact by hand.
    """
    if summary.get("attempted", 0) != 0:
        return False
    if summary.get("errors", 0) != 0 or summary.get("unavailable", 0) != 0:
        return False
    # The counters are derived from the rows, so check the rows too: an
    # inconsistent or truncated summary must not be able to excuse a failure.
    if any(row.get("status") in FAILURE_STATUSES for row in results):
        return False
    skipped = [row for row in results if row.get("status") == "skipped"]
    if not skipped:
        return False
    return all(_UNRECORDED_REASON in (row.get("reason") or "") for row in skipped)


def _detail(row: dict[str, Any]) -> str:
    """Render one problem row's detail cell; ``|`` is escaped for the table."""
    if row.get("drift"):
        detail = ", ".join(f"{item.get('kind')} {item.get('path')}" for item in row["drift"])
    elif row.get("additions"):
        detail = "added " + ", ".join(item.get("path", "") for item in row["additions"])
    elif row.get("error"):
        detail = str(row["error"])
    elif row.get("reason"):
        detail = str(row["reason"])
    else:
        detail = ""
    return detail.replace("|", "\\|")


def _needs_a_human(row: dict[str, Any]) -> bool:
    """True for a problem row: a failure, or a skip nobody has excused."""
    return row.get("status") in FAILURE_STATUSES or (row.get("status") == "skipped" and not row.get("allowed"))


def build_body(report_path: Path, run_url: str) -> str:
    """Return the issue body for a failing run, or ``""`` when nothing should be raised.

    ``""`` is returned only for the bootstrap red. Everything else - including a
    run that died before writing a report - produces a non-empty body.

    ``report_path`` is a CLI argument fixed by the calling workflow
    (``contract-goldens/report.json``), so Sonar's S8707 path-traversal taint
    flow is a false positive: a caller that can pass the argument can already
    read that path directly. Same convention as
    ``scripts/validate_release_tag.py``.
    """
    header = _header(run_url)
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))  # NOSONAR pythonsecurity:S8707
    except (OSError, ValueError):
        # The probe never got to write: install or setup failed. The job is red
        # for a reason a human still has to see.
        return f"{header}\n\nThe run failed before the probe could write a report (install or setup step).\n"

    summary = report.get("summary") if isinstance(report, dict) else None
    results = report.get("results") if isinstance(report, dict) else None
    if not isinstance(summary, dict) or not isinstance(results, list):
        return f"{header}\n\nThe probe report is malformed (no summary/results), so no per-probe verdict could be read.\n"

    if _is_bootstrap(summary, results):
        return ""

    lines = [
        header,
        "",
        "Summary: " + ", ".join(f"{key}={summary.get(key, 0)}" for key in SUMMARY_KEYS),
    ]
    problems = [row for row in results if _needs_a_human(row)]
    if problems:
        lines.extend(["", "| Probe | Status | Detail |", "| --- | --- | --- |"])
        lines.extend(f"| {row.get('provider')}/{row.get('operation')} | {row.get('status')} | {_detail(row)} |" for row in problems)
    lines.extend(["", "Full JSON report: the `provider-contract-report` artifact on that run."])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("usage: provider_contract_probe_report.py REPORT BODY_FILE RUN_URL", file=sys.stderr)
        return 2
    report_path, body_path, run_url = Path(args[0]), Path(args[1]), args[2]

    body = build_body(report_path, run_url)
    if not body:
        # An empty body file is the caller's signal to raise nothing.
        return 0
    try:
        # ``body_path`` is a CLI argument the workflow points at a ``mktemp``
        # file, so Sonar's S8707 taint flow is a false positive (the caller can
        # already write that path directly). Same convention as
        # ``scripts/render_validation_compose.py``.
        body_path.write_text(body, encoding="utf-8")  # NOSONAR pythonsecurity:S8707
    except OSError as exc:
        print(f"error: could not write the issue body to {body_path}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
