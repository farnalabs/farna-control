"""Tests for the RC fuzz status policy (FAR-2800).

The policy is the only thing that decides whether an RC fuzz response is a
finding, and it is the only place the two passes differ, so every row of the
table has its own test. A silent edit to the table fails here rather than
turning a release red (or green) for the wrong reason.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rc_fuzz_policy import (
    AUTHENTICATED,
    EXPECTED_STATUS_CODES,
    PASS_NAMES,
    REDACTED,
    UNAUTHENTICATED,
    Finding,
    Verdict,
    classify,
    findings_payload,
    leaked_paths,
    main,
    render_report,
    scrub_text,
    scrub_tree,
)

# ---------------------------------------------------------------------------
# One test per row of the policy table
# ---------------------------------------------------------------------------


def test_unauthenticated_401_is_expected() -> None:
    assert classify(UNAUTHENTICATED, 401, credential_sent=False) is Verdict.EXPECTED


def test_unauthenticated_402_is_expected() -> None:
    assert classify(UNAUTHENTICATED, 402, credential_sent=False) is Verdict.EXPECTED


def test_unauthenticated_403_is_a_finding() -> None:
    # 403 is NOT allowed in the unauthenticated pass: the plan gate on this stack
    # answers 402 (observed across a full sweep), so a 403 is not explained by the
    # gate and must be surfaced rather than waved through.
    assert classify(UNAUTHENTICATED, 403, credential_sent=False) is Verdict.FINDING


def test_unauthenticated_500_is_a_finding() -> None:
    assert classify(UNAUTHENTICATED, 500, credential_sent=False) is Verdict.FINDING


def test_authenticated_401_is_a_finding() -> None:
    # The invariant: a request that presented the valid token and was still
    # refused is an authorisation bug.
    assert classify(AUTHENTICATED, 401, credential_sent=True) is Verdict.FINDING


def test_authenticated_402_is_expected() -> None:
    # The plan/entitlement gate is not an authentication answer: a paid feature
    # refusing an authenticated caller on an unentitled org is expected.
    assert classify(AUTHENTICATED, 402, credential_sent=True) is Verdict.EXPECTED


def test_authenticated_500_is_a_finding() -> None:
    assert classify(AUTHENTICATED, 500, credential_sent=True) is Verdict.FINDING


@pytest.mark.parametrize("pass_name", PASS_NAMES)
@pytest.mark.parametrize("status_code", [200, 201, 204, 299])
def test_2xx_is_never_a_finding(pass_name: str, status_code: int) -> None:
    credential_sent = pass_name == AUTHENTICATED
    assert classify(pass_name, status_code, credential_sent=credential_sent) is Verdict.EXPECTED


# ---------------------------------------------------------------------------
# The remaining rows and the guard rails around them
# ---------------------------------------------------------------------------


def test_authenticated_401_without_the_credential_is_a_finding() -> None:
    # The authenticated pass must carry the token on every request. A missing
    # token is therefore a harness failure, not an excuse to allow a 401 through.
    assert classify(AUTHENTICATED, 401, credential_sent=False) is Verdict.FINDING


def test_authenticated_404_is_expected() -> None:
    # The sweep generates resource identifiers that do not exist.
    assert classify(AUTHENTICATED, 404, credential_sent=True) is Verdict.EXPECTED


def test_authenticated_403_is_a_finding() -> None:
    assert classify(AUTHENTICATED, 403, credential_sent=True) is Verdict.FINDING


@pytest.mark.parametrize("status_code", [400, 405, 409, 428])
def test_other_4xx_is_a_finding_in_the_unauthenticated_pass(status_code: int) -> None:
    assert classify(UNAUTHENTICATED, status_code, credential_sent=False) is Verdict.FINDING


def test_authenticated_422_is_expected() -> None:
    # The sweep generates malformed queries; 422 is the documented rejection. A
    # POSITIVE case answered 422 is still caught by `positive_data_acceptance`,
    # which is why allowing the status here loses no signal.
    assert classify(AUTHENTICATED, 422, credential_sent=True) is Verdict.EXPECTED


@pytest.mark.parametrize("status_code", [400, 405, 409, 428])
def test_other_4xx_is_a_finding_in_the_authenticated_pass(status_code: int) -> None:
    assert classify(AUTHENTICATED, status_code, credential_sent=True) is Verdict.FINDING


@pytest.mark.parametrize("status_code", [300, 301, 304])
def test_a_redirect_is_a_finding(status_code: int) -> None:
    # An API GET answering a redirect is not "expected" in either pass; only 2xx
    # is waved through, so a 3xx surfaces rather than silently passing.
    assert classify(UNAUTHENTICATED, status_code, credential_sent=False) is Verdict.FINDING


def test_an_unknown_pass_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown fuzz pass"):
        classify("semi-authenticated", 200, credential_sent=False)


def test_a_credential_in_the_unauthenticated_pass_is_rejected() -> None:
    # Mixing the passes up would silently weaken the 401 invariant, so it is an
    # error rather than a classification.
    with pytest.raises(ValueError, match="must never carry a credential"):
        classify(UNAUTHENTICATED, 401, credential_sent=True)


def test_the_expected_table_is_pinned() -> None:
    # Pinning the data, not just the function: widening any row to make a run
    # green is then a visible test change, reviewed as a policy change.
    assert {
        (UNAUTHENTICATED, False): frozenset({401, 402}),
        (AUTHENTICATED, False): frozenset({402, 404, 422}),
        (AUTHENTICATED, True): frozenset({402, 404, 422}),
    } == EXPECTED_STATUS_CODES


# ---------------------------------------------------------------------------
# The per-pass report
# ---------------------------------------------------------------------------


def test_the_report_lists_distinct_findings_and_the_status_tally() -> None:
    findings = [
        Finding(operation="GET /api/v1/agents", status_code=500, credential_sent=True, count=2),
        Finding(operation="GET /api/v1/pipelines", status_code=401, credential_sent=True),
    ]

    report = render_report(AUTHENTICATED, findings, {200: 12, 401: 3, 500: 2})

    assert "pass: authenticated" in report
    assert "Distinct findings: 2" in report
    assert "500  GET /api/v1/agents  (credential presented) x2" in report
    assert "401  GET /api/v1/pipelines  (credential presented) x1" in report
    assert "Result: FINDINGS" in report
    assert "401: 3" in report
    assert "500: 2" in report


def test_the_report_is_sorted_so_two_runs_compare_by_eye() -> None:
    findings = [
        Finding(operation="GET /z", status_code=500, credential_sent=True),
        Finding(operation="GET /a", status_code=422, credential_sent=True),
    ]

    report = render_report(AUTHENTICATED, findings, {})

    assert report.index("422  GET /a") < report.index("500  GET /z")


def test_a_clean_pass_says_clean_and_still_shows_the_tally() -> None:
    report = render_report(UNAUTHENTICATED, [], {401: 7610, 402: 28})

    assert "Distinct findings: 0" in report
    assert "Result: CLEAN" in report
    assert "401: 7610" in report


def test_a_pass_with_no_recorded_responses_says_so() -> None:
    report = render_report(UNAUTHENTICATED, [], {})

    assert "(no responses recorded)" in report


def test_the_findings_payload_round_trips_through_json() -> None:
    findings = [Finding(operation="GET /api/v1/agents", status_code=500, credential_sent=True, count=3)]

    payload = json.loads(json.dumps(findings_payload(AUTHENTICATED, findings, {500: 3})))

    assert payload["pass"] == AUTHENTICATED
    assert payload["findings"] == [{"operation": "GET /api/v1/agents", "status_code": 500, "credential_sent": True, "count": 3}]
    assert payload["status_tally"] == {"500": 3}


def test_the_report_never_mentions_a_credential_value() -> None:
    # The report is built from an operation label, a status and a boolean only -
    # there is no field a token could travel in - so a token-shaped string in an
    # operation label is the only way to get one in, and that is the scrub's job.
    report = render_report(AUTHENTICATED, [Finding(operation="GET /api/v1/x", status_code=401, credential_sent=True)], {401: 1})

    assert "Bearer" not in report
    assert "credential presented" in report


# ---------------------------------------------------------------------------
# Credential scrubbing (Schemathesis writes request headers into its NDJSON)
# ---------------------------------------------------------------------------


def test_scrub_text_replaces_every_occurrence() -> None:
    text, count = scrub_text("Authorization: Bearer TOK\nand TOK again", "TOK")

    assert count == 2
    assert text == f"Authorization: Bearer {REDACTED}\nand {REDACTED} again"


def test_scrub_text_with_an_absent_secret_changes_nothing() -> None:
    text, count = scrub_text("nothing to see", "TOK")

    assert count == 0
    assert text == "nothing to see"


def test_scrub_refuses_an_empty_secret() -> None:
    with pytest.raises(ValueError, match="empty secret"):
        scrub_text("text", "")


def test_scrub_tree_rewrites_files_and_reports_them(tmp_path: Path) -> None:
    leaked = tmp_path / "events.ndjson"
    leaked.write_text('{"headers": {"Authorization": "Bearer TOK-123"}}', encoding="utf-8")
    clean = tmp_path / "junit.xml"
    clean.write_text("<testsuites />", encoding="utf-8")

    rewritten = scrub_tree(tmp_path, "TOK-123")

    assert rewritten == [leaked]
    assert "TOK-123" not in leaked.read_text(encoding="utf-8")
    assert REDACTED in leaked.read_text(encoding="utf-8")
    assert clean.read_text(encoding="utf-8") == "<testsuites />"


def test_scrub_tree_handles_binary_files_without_corrupting_them(tmp_path: Path) -> None:
    binary = tmp_path / "consoles.bin"
    binary.write_bytes(b"\x00\xff" + b"TOK-123" + b"\xfe\x00")

    scrub_tree(tmp_path, "TOK-123")

    assert binary.read_bytes() == b"\x00\xff" + REDACTED.encode() + b"\xfe\x00"


def test_leaked_paths_finds_a_secret_anywhere_in_the_tree(tmp_path: Path) -> None:
    (tmp_path / "stack").mkdir()
    leak = tmp_path / "stack" / "containers.log"
    leak.write_text("Authorization: Bearer TOK-123", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("nothing here", encoding="utf-8")

    assert leaked_paths(tmp_path, "TOK-123") == [leak]


def test_leaked_paths_refuses_an_empty_secret(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty secret"):
        leaked_paths(tmp_path, "")


def test_the_scrub_command_rewrites_a_leak_and_proves_it_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "events.ndjson").write_text("Bearer TOK-123", encoding="utf-8")
    monkeypatch.setenv("FUZZ_JWT", "TOK-123")

    assert main(["scrub", "--evidence-dir", str(tmp_path)]) == 0
    assert not leaked_paths(tmp_path, "TOK-123")


def test_the_scrub_command_refuses_to_pass_without_a_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "events.ndjson").write_text("Bearer TOK-123", encoding="utf-8")
    monkeypatch.delenv("FUZZ_JWT", raising=False)

    assert main(["scrub", "--evidence-dir", str(tmp_path)]) == 2


def test_the_scrub_command_is_a_no_op_without_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FUZZ_JWT", "TOK-123")

    assert main(["scrub", "--evidence-dir", str(tmp_path / "missing")]) == 0


def test_the_report_command_renders_a_findings_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    findings_file = tmp_path / "findings.json"
    findings_file.write_text(
        json.dumps(
            {
                "pass": AUTHENTICATED,
                "findings": [{"operation": "GET /api/v1/x", "status_code": 500, "credential_sent": True, "count": 1}],
                "status_tally": {"500": 1},
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["report", "--pass-name", AUTHENTICATED, "--findings", str(findings_file)])

    assert exit_code == 1
    assert "500  GET /api/v1/x" in capsys.readouterr().out


def test_the_report_command_is_clean_for_an_empty_findings_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    findings_file = tmp_path / "findings.json"
    findings_file.write_text(json.dumps({"pass": UNAUTHENTICATED, "findings": [], "status_tally": {}}), encoding="utf-8")

    exit_code = main(["report", "--pass-name", UNAUTHENTICATED, "--findings", str(findings_file)])

    assert exit_code == 0
    assert "Result: CLEAN" in capsys.readouterr().out


def test_the_secret_environment_variable_name_is_the_one_the_workflow_exports() -> None:
    # The workflow scrubs with FUZZ_JWT exported from $GITHUB_ENV; a rename on one
    # side alone would silently scrub nothing (and prove nothing).
    from rc_fuzz_policy import SECRET_ENV_VAR

    assert SECRET_ENV_VAR == "FUZZ_JWT"
