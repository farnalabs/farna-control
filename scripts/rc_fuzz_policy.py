"""The RC validation fuzz leg's HTTP-status policy (FAR-2800).

The fuzz leg sweeps the release candidate's OWN disposable stack twice - once
with no credential at all, once with the seeded account's Bearer token on every
request - and judges each response against the policy in this module. The policy
lives here, as data plus one small function, so that it is testable and cannot
drift between the two passes or between the workflow's prose and its behaviour.

The table
=========

``2xx`` is never a finding in either pass: a documented, valid request that is
answered is exactly what the sweep wants to see.

A request that carried **no credential** is expected to be refused, in either
pass (the coverage phase strips and rewrites the ``Authorization`` header on
purpose, so a 401 for such a request is the correct answer, not a bug):

* ``401`` - the request was not authenticated. Expected by definition.
* ``402`` - the plan/entitlement gate answered instead of the handler. Expected
  on this stack, whose seeded organisation deliberately has no paid entitlement.

A request that carried **the seeded token** is expected to get:

* ``402`` - the plan/entitlement gate is not an authentication answer, so a paid
  feature refusing an authenticated caller is expected, not an authorisation bug.
* ``404`` - the sweep generates resource identifiers that do not exist; "not
  found" is the correct answer to a GET for a random id.
* ``422`` - the sweep generates malformed queries; "unprocessable" is the
  documented answer to a request the schema rejects. Whether the REJECTION was
  appropriate is `negative_data_rejection`'s job, and whether a VALID request was
  wrongly rejected is `positive_data_acceptance`'s job - both still run, and a
  positive case answered 422 is still a finding through that check (measured on
  v0.3.330-rc3: 33 of the 265 422s were positive cases and were reported by
  `positive_data_acceptance`, not silently dropped).

Everything else is a finding. In particular:

* ``401`` **with the token presented** is the invariant this policy exists for:
  a valid credential that is rejected is an authorisation bug, in every pass.
* ``5xx`` is a finding in both passes.
* ``403`` is a finding in both passes. The unauthenticated sweep establishes that
  this stack's plan gate answers ``402``, not ``403``, so a ``403`` is not
  explained by the plan gate - it is left as a finding rather than silently
  allowed. (Measured on ``v0.3.330-rc3``: authenticated ``403``s are role
  denials, e.g. ``Permission 'system.config.manage' requires system admin role``
  for an organisation admin. Allowing that class is a policy decision that
  belongs to the release owner, not to this change.)
* Any other 4xx or a 3xx is a finding too: an endpoint that answers with
  something the sweep did not ask for is doing something worth surfacing.

Two things are deliberately outside the status policy and stay the business of
Schemathesis' own checks: the coverage phase's deliberate probes of HTTP methods
the specification does not declare (``unsupported_method``), and any response
body/header/schema mismatch. The policy only ever classifies a status code.

Credential handling
===================

The token is never part of the policy data and is never rendered: reports name
only the operation, the status and whether the credential was presented. The
``scrub`` subcommand rewrites a secret out of an evidence tree and then proves it
is gone, so a Schemathesis artefact that happened to record the request headers
cannot ship the credential.

Usage
=====

    python scripts/rc_fuzz_policy.py scrub --evidence-dir <dir>

reads the secret from ``FUZZ_JWT`` and exits non-zero when it is still present.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

#: Pass name: no credential is presented on any request.
UNAUTHENTICATED = "unauthenticated"
#: Pass name: the seeded account's Bearer token is presented on every request.
AUTHENTICATED = "authenticated"

PASS_NAMES: tuple[str, ...] = (UNAUTHENTICATED, AUTHENTICATED)

#: The policy, as data: for each (pass, whether the exact credential was
#: presented) the status codes that are an EXPECTED answer. Everything outside
#: 2xx and this table is a finding. Adding a code here is the one-line change a
#: reviewer must scrutinise - widening it to make a run green is exactly what the
#: ticket forbids.
EXPECTED_STATUS_CODES: dict[tuple[str, bool], frozenset[int]] = {
    # No credential presented - in either pass. The coverage phase strips the
    # header on purpose, so this row also covers the authenticated pass' own
    # unauthenticated probes.
    (UNAUTHENTICATED, False): frozenset({401, 402}),
    # The authenticated pass also generates requests with NO credential: the
    # coverage phase deliberately emits a MISSING_PARAMETER probe for the required
    # ``Authorization`` header and strips the header for it. Such a request was not
    # authenticated, so a 401 is the CORRECT answer to it - this row is what stops
    # that correct refusal being reported as a defect. The invariant (a 401 with
    # the token PRESENTED is a bug) lives on the next row and is untouched: this
    # row only covers requests whose credential was absent or mutated.
    (AUTHENTICATED, False): frozenset({401, 402, 404, 422}),
    # The seeded token was presented: the gates and the fuzzer's own malformed
    # input are expected answers; a 401 is NOT (it is the bug this leg hunts).
    (AUTHENTICATED, True): frozenset({402, 404, 422}),
}

#: Replacement written over a secret found in an evidence file.
REDACTED = "[REDACTED]"

#: Environment variable the `scrub` subcommand reads the secret from.
SECRET_ENV_VAR = "FUZZ_JWT"


class Verdict(StrEnum):
    """What the policy makes of one response."""

    EXPECTED = "expected"
    FINDING = "finding"


@dataclass(frozen=True, slots=True)
class Finding:
    """One distinct finding: an operation, a status, and the credential state."""

    operation: str
    status_code: int
    credential_sent: bool
    count: int = 1

    def describe(self) -> str:
        credential = "credential presented" if self.credential_sent else "no credential"
        return f"{self.status_code}  {self.operation}  ({credential}) x{self.count}"


def classify(pass_name: str, status_code: int, *, credential_sent: bool) -> Verdict:
    """Classify one response of one pass.

    Raises ``ValueError`` for an unknown pass and for a credential reported in the
    unauthenticated pass - both mean the caller has mixed the two passes up, and a
    silent default would hide exactly that mistake.
    """
    if pass_name not in PASS_NAMES:
        raise ValueError(f"unknown fuzz pass {pass_name!r}; expected one of {PASS_NAMES}")
    if pass_name == UNAUTHENTICATED and credential_sent:
        raise ValueError("the unauthenticated pass must never carry a credential")
    if 200 <= status_code < 300:
        return Verdict.EXPECTED
    expected = EXPECTED_STATUS_CODES[(pass_name, credential_sent)]
    return Verdict.EXPECTED if status_code in expected else Verdict.FINDING


def render_report(pass_name: str, findings: Sequence[Finding], status_tally: Mapping[int, int]) -> str:
    """Render the per-pass failure report (the artefact, and the CI log entry)."""
    lines = [
        f"RC validation fuzz - pass: {pass_name}",
        "",
        "Policy: 2xx never a finding; in either pass a request without the credential is expected",
        "to get 401/402; with the seeded token 402/404/422 are expected; everything else is a",
        "finding (401 WITH the token, any 5xx and 403 included).",
        "",
    ]
    if findings:
        lines.append(f"Distinct findings: {len(findings)}")
        ordered = sorted(findings, key=lambda item: (item.operation, item.status_code, item.credential_sent))
        lines.extend(f"  {finding.describe()}" for finding in ordered)
        lines.append("")
        lines.append("Result: FINDINGS")
    else:
        lines.append("Distinct findings: 0")
        lines.append("")
        lines.append("Result: CLEAN")
    lines.append("")
    lines.append("Status tally (every recorded response):")
    if status_tally:
        for status_code, count in sorted(status_tally.items()):
            lines.append(f"  {status_code}: {count}")
    else:
        lines.append("  (no responses recorded)")
    return "\n".join(lines) + "\n"


def findings_payload(pass_name: str, findings: Iterable[Finding], status_tally: Mapping[int, int]) -> dict[str, object]:
    """Machine-readable form of the same report, for the uploaded artefact."""
    return {
        "pass": pass_name,
        "findings": [
            {
                "operation": finding.operation,
                "status_code": finding.status_code,
                "credential_sent": finding.credential_sent,
                "count": finding.count,
            }
            for finding in sorted(findings, key=lambda item: (item.operation, item.status_code, item.credential_sent))
        ],
        "status_tally": {str(status_code): count for status_code, count in sorted(status_tally.items())},
    }


# ---------------------------------------------------------------------------
# Credential scrubbing
# ---------------------------------------------------------------------------


def scrub_text(text: str, secret: str) -> tuple[str, int]:
    """Replace every occurrence of ``secret`` in ``text``; return the text and the count."""
    if not secret:
        raise ValueError("refusing to scrub with an empty secret - it would prove nothing")
    occurrences = text.count(secret)
    if not occurrences:
        return text, 0
    return text.replace(secret, REDACTED), occurrences


def _evidence_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


def scrub_tree(root: Path, secret: str) -> list[Path]:
    """Scrub every file under ``root``; return the paths that were rewritten.

    Files are read and written as bytes so a binary artefact cannot be corrupted
    by a decode/encode round trip.
    """
    if not secret:
        raise ValueError("refusing to scrub with an empty secret - it would prove nothing")
    needle = secret.encode("utf-8")
    rewritten: list[Path] = []
    for path in _evidence_files(root):
        payload = path.read_bytes()
        if needle not in payload:
            continue
        path.write_bytes(payload.replace(needle, REDACTED.encode("utf-8")))
        rewritten.append(path)
    return rewritten


def leaked_paths(root: Path, secret: str) -> list[Path]:
    """Return every file under ``root`` that still contains ``secret``."""
    if not secret:
        raise ValueError("refusing to scan with an empty secret - a clean result would prove nothing")
    needle = secret.encode("utf-8")
    return [path for path in _evidence_files(root) if needle in path.read_bytes()]


def _scrub_command(evidence_dir: Path) -> int:
    secret = os.environ.get(SECRET_ENV_VAR, "")
    if not evidence_dir.is_dir():
        if not secret:
            print(f"no evidence directory at {evidence_dir} and no {SECRET_ENV_VAR}; nothing to scrub")
            return 0
        print(f"no evidence directory at {evidence_dir}; nothing to scrub")
        return 0
    if not secret:
        print(
            f"::error::{SECRET_ENV_VAR} is empty but evidence exists at {evidence_dir} - the credential cannot be proven absent from the artefacts, so this is a failure.",
            file=sys.stderr,
        )
        return 2
    rewritten = scrub_tree(evidence_dir, secret)
    for path in rewritten:
        print(f"scrubbed the credential from {path}")
    leaks = leaked_paths(evidence_dir, secret)
    if leaks:
        for path in leaks:
            print(f"::error::the credential is still present in {path}", file=sys.stderr)
        return 1
    print(f"credential scrub complete: {len(rewritten)} file(s) rewritten, 0 leak(s) remaining")
    return 0


def _render_command(pass_name: str, findings_path: Path) -> int:
    """Print the report from a findings JSON (used to re-render without the run).

    ``findings_path`` is the ``--findings`` CLI argument, a path the operator (or a
    workflow step) chooses explicitly - it is not untrusted input from any request.
    A caller who can pass this argument can already read that path directly, so
    Sonar's S8707 path-traversal taint flow is a false positive here. Same
    convention as ``scripts/provider_contract_probe_report.py``.
    """
    payload = json.loads(findings_path.read_text(encoding="utf-8"))  # NOSONAR pythonsecurity:S8707
    findings = [Finding(**item) for item in payload.get("findings", [])]
    tally = {int(code): count for code, count in (payload.get("status_tally") or {}).items()}
    report = render_report(pass_name, findings, tally)
    sys.stdout.write(report)
    return 0 if not findings else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RC validation fuzz status policy helpers")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrub = subparsers.add_parser("scrub", help="scrub the credential out of an evidence tree and prove it is gone")
    scrub.add_argument("--evidence-dir", required=True, type=Path)

    render = subparsers.add_parser("report", help="render a report from a findings JSON file")
    render.add_argument("--pass-name", required=True, choices=PASS_NAMES)
    render.add_argument("--findings", required=True, type=Path)

    args = parser.parse_args(argv)
    if args.command == "scrub":
        return _scrub_command(args.evidence_dir)
    return _render_command(args.pass_name, args.findings)


if __name__ == "__main__":
    sys.exit(main())
