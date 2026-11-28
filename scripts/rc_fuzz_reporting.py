"""Process-wide recorder for the RC fuzz status policy (FAR-2800).

Split out of :mod:`rc_fuzz_checks` for one measured reason: Schemathesis'
``SCHEMATHESIS_HOOKS`` loader re-executes the extension module from a fresh
module object every time it resolves its configuration (observed four times for a
single CLI run), so per-module counters would be split four ways and the ``atexit``
that writes the report would overwrite the populated report with an empty one.
This module is imported through the normal import machinery, so Python caches it
in ``sys.modules``: one state, one registration, one report.

The recorded data is credential-free by construction - an operation label, a
status code, a boolean.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading
from collections import Counter
from pathlib import Path

from rc_fuzz_policy import PASS_NAMES, Finding, findings_payload, render_report

PASS = os.environ.get("RC_FUZZ_PASS", "")
if PASS not in PASS_NAMES:
    raise RuntimeError(f"RC_FUZZ_PASS must be one of {PASS_NAMES}, got {PASS!r}; refusing to run without a pass")

_LOCK = threading.Lock()
_TALLY: Counter[int] = Counter()
_FINDINGS: Counter[tuple[str, int, bool]] = Counter()


def record(*, operation: str, status_code: int, credential_sent: bool, is_finding: bool) -> None:
    """Record one classified response."""
    with _LOCK:
        _TALLY[status_code] += 1
        if is_finding:
            _FINDINGS[(operation, status_code, credential_sent)] += 1


def snapshot() -> tuple[list[Finding], dict[int, int]]:
    """The distinct findings and the status tally recorded so far."""
    with _LOCK:
        findings = [Finding(operation=operation, status_code=status_code, credential_sent=credential_sent, count=count) for (operation, status_code, credential_sent), count in _FINDINGS.items()]
        return findings, dict(_TALLY)


def _write_reports() -> None:
    findings, tally = snapshot()
    report = render_report(PASS, findings, tally)
    # The report is printed as well as written: the CI log then carries the
    # verdict even if the artefact upload is the step that fails.
    sys.stdout.write(report)
    sys.stdout.flush()
    report_path = os.environ.get("RC_FUZZ_REPORT_PATH")
    if report_path:
        Path(report_path).write_text(report, encoding="utf-8")
    findings_path = os.environ.get("RC_FUZZ_FINDINGS_JSON")
    if findings_path:
        payload = findings_payload(PASS, findings, tally)
        Path(findings_path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


atexit.register(_write_reports)
