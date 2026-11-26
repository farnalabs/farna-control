"""Unit tests for ``.github/scripts/select_rc_deploy_run.py`` (FAR-2800).

``rc-validate.yml`` used to discover the deploy run for an RC by CREATION TIME -
the newest ``workflow_dispatch`` deploy run created at or after the tag. That is
a heuristic, so it selected runs that had shipped something else (2026-11-26: it
watched a throttled main deploy that shipped nothing, and the leg failed on
deployment identity instead of validating anything).

The selector now reads the deploy run's own ``release-identity-<rc_tag>``
artifact. These tests pin that selection, the four-way diagnosis split, and -
most importantly - that the old recency rule picks a DIFFERENT, wrong run on the
same fixture (the prove-the-fix test at the bottom).

No network, no GitHub: ``gh`` is a fake runner.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from typing import Any

import pytest

for parent in Path(__file__).resolve().parents:
    _script_path = parent / ".github" / "scripts" / "select_rc_deploy_run.py"
    if _script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/select_rc_deploy_run.py")

_loader = SourceFileLoader("select_rc_deploy_run", str(_script_path))
selector = module_from_spec(spec_from_loader("select_rc_deploy_run", _loader))
sys.modules["select_rc_deploy_run"] = selector
_loader.exec_module(selector)

_REPO = "farnalabs/farna-control"
_RC_TAG = "v0.3.330-rc3"
_RC_SHA = "8eee7e93110d11dbfc8953957e9d4da680a53285"


class _FakeGh:
    """A fake ``gh api`` serving the three endpoints the selector reads."""

    def __init__(
        self,
        *,
        artifact_runs: tuple[int, ...] = (),
        deploy_job: dict[int, str] | None = None,
        run_state: dict[int, tuple[str, str]] | None = None,
        artifacts_fail: bool = False,
        unreadable_runs: tuple[int, ...] = (),
    ) -> None:
        self.artifact_runs = artifact_runs
        self.deploy_job = deploy_job or {}
        self.run_state = run_state or {}
        self.artifacts_fail = artifacts_fail
        self.unreadable_runs = unreadable_runs
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        endpoint = argv[2] if len(argv) > 2 else ""

        if "/actions/artifacts?" in endpoint:
            if self.artifacts_fail:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
            payload = {
                "total_count": len(self.artifact_runs),
                "artifacts": [{"name": f"release-identity-{_RC_TAG}", "workflow_run": {"id": run_id}} for run_id in self.artifact_runs],
            }
            return subprocess.CompletedProcess(argv, 0, _json(payload), "")

        run_id = _run_id_from(endpoint)
        if run_id in self.unreadable_runs:
            return subprocess.CompletedProcess(argv, 1, "", "boom")

        if endpoint.endswith("/jobs?per_page=100"):
            conclusion = self.deploy_job.get(run_id)
            jobs = []
            if conclusion is not None:
                jobs.append({"name": selector.DEPLOY_JOB_NAME, "conclusion": conclusion})
            return subprocess.CompletedProcess(argv, 0, _json({"jobs": jobs}), "")

        status, conclusion = self.run_state.get(run_id, ("completed", "success"))
        payload = {"status": status, "conclusion": conclusion}
        return subprocess.CompletedProcess(argv, 0, _json(payload), "")


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload)


def _run_id_from(endpoint: str) -> int:
    marker = "/actions/runs/"
    if marker not in endpoint:
        return -1
    return int(endpoint.split(marker)[1].split("/", maxsplit=1)[0])


# ---------------------------------------------------------------------------
# The property itself
# ---------------------------------------------------------------------------


def test_artifact_name_is_the_rc_tag_under_the_documented_prefix() -> None:
    assert selector.release_identity_artifact_name(_RC_TAG) == f"release-identity-{_RC_TAG}"


@pytest.mark.parametrize("bad_tag", ["v1.2.3-rc1/evil", "v1.2.3 rc1", "", "-rc1", ".v1-rc1"])
def test_artifact_name_rejects_an_unsafe_tag(bad_tag: str) -> None:
    with pytest.raises(ValueError, match="artifact name"):
        selector.release_identity_artifact_name(bad_tag)


def test_candidate_run_ids_reads_the_repo_wide_name_filter() -> None:
    gh = _FakeGh(artifact_runs=(45615510904,))
    assert selector.candidate_run_ids(_REPO, _RC_TAG, runner=gh) == (45615510904,)
    endpoint = gh.calls[0][2]
    assert f"name={selector.release_identity_artifact_name(_RC_TAG)}" in endpoint
    assert "per_page=100" in endpoint


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def test_selects_the_one_correct_candidate_among_several_newer_wrong_ones() -> None:
    """Only the run that records shipping the RC is a candidate at all.

    The newer runs in this fixture are main deploys: they produced no
    ``release-identity`` record, so the artifacts query never returns them and
    the correct (older) RC run is selected. Creation time plays no part.
    """
    gh = _FakeGh(
        artifact_runs=(45615510904,),
        deploy_job={45615510904: "success"},
        run_state={45615510904: ("completed", "success")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "selected"
    assert result.run_id == 45615510904
    assert [candidate.run_id for candidate in result.candidates] == [45615510904]
    assert "45615510904" in "\n".join(selector.render(result, _RC_TAG))


def test_no_candidate_reports_not_deployed() -> None:
    """No identity record anywhere: nothing has tried to ship this RC."""
    gh = _FakeGh(artifact_runs=())
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "not_deployed"
    assert result.run_id is None
    assert not result.candidates
    narrative = "\n".join(selector.render(result, _RC_TAG))
    assert "has not been deployed" in narrative
    assert "Nothing has tried to ship this RC" in narrative


def test_a_matching_candidate_that_failed_reports_deploy_failed() -> None:
    """A record exists and its deploy job lost: a deploy failure, not "not deployed"."""
    gh = _FakeGh(
        artifact_runs=(45615510904,),
        deploy_job={45615510904: "failure"},
        run_state={45615510904: ("completed", "failure")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "deploy_failed"
    assert result.run_id is None
    assert [candidate.run_id for candidate in result.candidates] == [45615510904]
    narrative = "\n".join(selector.render(result, _RC_TAG))
    assert "finished without a successful production deploy" in narrative
    assert "has not been deployed" in narrative


def test_a_candidate_whose_deploy_job_was_skipped_counts_as_failed() -> None:
    """A completed run that skipped the production deploy shipped nothing."""
    gh = _FakeGh(
        artifact_runs=(7,),
        deploy_job={7: "skipped"},
        run_state={7: ("completed", "success")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "deploy_failed"


def test_several_correct_candidates_pick_the_highest_run_id_deterministically() -> None:
    """Determinism, not recency: the highest run id wins and all are reported."""
    gh = _FakeGh(
        artifact_runs=(300, 100, 200),
        deploy_job={100: "success", 200: "success", 300: "success"},
        run_state={100: ("completed", "success"), 200: ("completed", "success"), 300: ("completed", "success")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "selected"
    assert result.run_id == 300
    assert [candidate.run_id for candidate in result.candidates] == [100, 200, 300]
    narrative = "\n".join(selector.render(result, _RC_TAG))
    assert "3 runs record shipping" in narrative
    assert "run 100:" in narrative
    assert "run 200:" in narrative


def test_a_shipped_candidate_beats_a_newer_pending_one() -> None:
    gh = _FakeGh(
        artifact_runs=(100, 200),
        deploy_job={100: "success"},
        run_state={100: ("completed", "success"), 200: ("in_progress", "")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "selected"
    assert result.run_id == 100


def test_an_unfinished_candidate_is_watched_not_declared_failed() -> None:
    gh = _FakeGh(
        artifact_runs=(500,),
        deploy_job={},
        run_state={500: ("in_progress", "")},
    )
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "deploy_pending"
    assert result.run_id == 500


def test_an_unreadable_artifacts_api_is_unknown_not_not_deployed() -> None:
    """A failed API read must never be reported as "nothing was deployed"."""
    gh = _FakeGh(artifacts_fail=True)
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "unknown"
    narrative = "\n".join(selector.render(result, _RC_TAG))
    assert "does NOT mean the RC has not been deployed" in narrative


def test_an_unreadable_run_with_no_shipped_candidate_is_unknown() -> None:
    gh = _FakeGh(artifact_runs=(9,), unreadable_runs=(9,))
    result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert result.verdict == "unknown"
    assert result.unreadable_run_ids == (9,)


def test_a_missing_gh_binary_is_unknown() -> None:
    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    assert selector.select(_REPO, _RC_TAG, runner=missing).verdict == "unknown"


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_writes_verdict_and_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "github_output"

    def fake_select(repo: str, rc_tag: str, **kwargs: Any) -> Any:
        assert repo == _REPO
        assert rc_tag == _RC_TAG
        return selector.Selection("selected", 45615510904, ())

    monkeypatch.setattr(selector, "select", fake_select)
    exit_code = selector.main(["--repo", _REPO, "--rc-tag", _RC_TAG, "--github-output", str(out_path)])
    assert exit_code == 0
    written = out_path.read_text(encoding="utf-8")
    assert "verdict=selected" in written
    assert "deploy_run_id=45615510904" in written


def test_cli_writes_an_empty_run_id_when_nothing_is_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out_path = tmp_path / "github_output"

    def fake_select(repo: str, rc_tag: str, **kwargs: Any) -> Any:
        return selector.Selection("not_deployed", None, ())

    monkeypatch.setattr(selector, "select", fake_select)
    assert selector.main(["--repo", _REPO, "--rc-tag", _RC_TAG, "--github-output", str(out_path)]) == 0
    assert "deploy_run_id=\n" in out_path.read_text(encoding="utf-8")


def test_cli_refuses_an_unsafe_tag(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = selector.main(["--repo", _REPO, "--rc-tag", "v1.2.3-rc1/evil"])
    assert exit_code == 2
    assert "artifact name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Prove-the-fix: the OLD rule picks a different, wrong run on the same fixture
# ---------------------------------------------------------------------------

#: The fixture the old discovery path worked on: deploy runs with creation
#: times, of which exactly one carried the RC.
_DEPLOY_RUNS = [
    {"id": 45615510904, "created_at": "2026-11-25T12:37:18Z", "release_ref": _RC_TAG, "released": False},
    {"id": 45725660070, "created_at": "2026-11-26T00:01:12Z", "release_ref": "", "released": False},
    {"id": 45762495894, "created_at": "2026-11-26T05:34:33Z", "release_ref": "", "released": False},
]

_RC_TAG_CREATED_AT = "2026-11-25T12:32:10Z"


def _old_recency_selection(runs: list[dict[str, Any]], rc_created_at: str) -> int | None:
    """The FAR-2800 bug, verbatim: newest dispatch run at or after the tag."""
    eligible = [run for run in runs if run["created_at"] >= rc_created_at]
    eligible.sort(key=lambda run: run["created_at"], reverse=True)
    return eligible[0]["id"] if eligible else None


def test_prove_the_fix_the_old_rule_picks_a_newer_run_that_shipped_something_else() -> None:
    old_pick = _old_recency_selection(_DEPLOY_RUNS, _RC_TAG_CREATED_AT)
    # The old rule names the newest run - a main deploy, and in the live incident
    # a THROTTLED one that shipped nothing at all.
    assert old_pick == 45762495894

    # The new rule considers only the run that records shipping the RC.
    gh = _FakeGh(
        artifact_runs=(45615510904,),
        deploy_job={45615510904: "failure"},
        run_state={45615510904: ("completed", "failure")},
    )
    new_result = selector.select(_REPO, _RC_TAG, runner=gh)
    assert new_result.run_id != old_pick
    # ...and because that run lost, the honest verdict is "the deploy failed" -
    # not "here is a run that reported success while serving another commit".
    assert new_result.verdict == "deploy_failed"
