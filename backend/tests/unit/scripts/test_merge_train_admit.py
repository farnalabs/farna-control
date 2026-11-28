"""Unit tests for .github/scripts/merge_train_admit.py.

Focus: the two security validators the Sonar pythonsecurity rules flagged (repo
slug validation for the command-argument path, path confinement for the manifest
output), plus the `fast_tier_state` head-narrowing behaviour. No network, no git:
`_gh_json` is monkeypatched where a call would otherwise shell out.
"""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


def _load(script_name: str):
    for parent in Path(__file__).resolve().parents:
        script_path = parent / ".github" / "scripts" / script_name
        if script_path.exists():
            break
    else:
        raise RuntimeError(f"Could not find .github/scripts/{script_name}")
    module_name = script_name.removesuffix(".py").replace("-", "_")
    loader = SourceFileLoader(module_name, str(script_path))
    module = module_from_spec(spec_from_loader(module_name, loader))
    loader.exec_module(module)
    return module


admit = _load("merge_train_admit.py")


def test_validate_repo_accepts_owner_name_slugs() -> None:
    assert admit.validate_repo("farnalabs/farna-control") == "farnalabs/farna-control"
    assert admit.validate_repo("owner/repo.with.dots_and-dashes") == "owner/repo.with.dots_and-dashes"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "farnalabs",
        "farnalabs/",
        "/repo",
        "a/b/c",
        "owner/name extra",
        "owner/na;me",
        "owner/$(id)",
    ],
    ids=[
        "empty",
        "no-slash",
        "trailing-slash",
        "leading-slash",
        "two-slashes",
        "space-in-name",
        "semicolon",
        "shell-substitution",
    ],
)
def test_validate_repo_rejects_bad_slug(bad: str) -> None:
    with pytest.raises(SystemExit):
        admit.validate_repo(bad)


def test_resolve_within_accepts_path_inside_base(tmp_path: Path) -> None:
    base = (tmp_path / "workspace").resolve()
    base.mkdir()
    accepted = base / "train_manifest.json"
    resolved = admit.resolve_within(str(accepted), (base,))
    assert resolved == accepted
    assert resolved.is_relative_to(base)


def test_resolve_within_rejects_parent_escape(tmp_path: Path) -> None:
    base = (tmp_path / "workspace").resolve()
    base.mkdir()
    escape = base / ".." / "escape.json"
    with pytest.raises(SystemExit):
        admit.resolve_within(str(escape), (base,))


def test_main_rejects_bad_repo_before_doing_anything(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "manifest.json"
    monkeypatch.setattr(sys, "argv", ["merge_train_admit.py", "--out", str(out), "--repo", "owner name"])
    with pytest.raises(SystemExit):
        admit.main()


def test_fast_tier_state_green_when_head_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        admit,
        "_gh_json",
        lambda _args: [{"headSha": "abc123", "conclusion": "success", "status": "completed"}],
    )
    state, _detail = admit.fast_tier_state("feature", "abc123")
    assert state == "green"


def test_fast_tier_state_waits_when_only_an_older_head_ran(monkeypatch) -> None:
    monkeypatch.setattr(
        admit,
        "_gh_json",
        lambda _args: [{"headSha": "oldsha", "conclusion": "success", "status": "completed"}],
    )
    state, detail = admit.fast_tier_state("feature", "newsha")
    assert state == "waiting"
    assert "newsha" in detail


def test_fast_tier_state_failed_on_red_conclusion(monkeypatch) -> None:
    monkeypatch.setattr(
        admit,
        "_gh_json",
        lambda _args: [{"headSha": "abc123", "conclusion": "failure", "status": "completed"}],
    )
    state, _detail = admit.fast_tier_state("feature", "abc123")
    assert state == "failed"


def test_fast_tier_state_waits_when_no_runs_object(monkeypatch) -> None:
    monkeypatch.setattr(admit, "_gh_json", lambda _args: None)
    state, _detail = admit.fast_tier_state("feature", "abc123")
    assert state == "waiting"


def _evaluate_approved_pr(monkeypatch, *, commit_date: str, api_error: bool = False) -> tuple[str, str]:
    """Run ``evaluate`` for an approved, green PR whose head was committed at ``commit_date``."""
    reviews = [{"state": "APPROVED", "submitted_at": "2026-11-28T07:51:11Z", "commit_id": "abc123"}]
    commits = [{"sha": "abc123", "parents": [{"sha": "p"}], "commit": {"committer": {"date": commit_date}}}]

    def fake_run(argv, **_kwargs):
        if api_error:
            raise RuntimeError("HTTP 502")
        return json.dumps(reviews if "/reviews?" in argv[-1] else commits)

    detail = {"reviewDecision": "APPROVED", "headRefOid": "abc123", "reviews": [{"state": "APPROVED", "author": {"login": "farna-reviewbot"}}]}
    monkeypatch.setattr(admit, "_run", fake_run)
    monkeypatch.setattr(admit, "_gh_json", lambda _args: detail)
    monkeypatch.setattr(admit, "same_repo_ok", lambda _repo, _number: (True, ""))
    monkeypatch.setattr(admit, "dropped_within_hour", lambda _repo, _number, _now: (False, ""))
    monkeypatch.setattr(admit, "fast_tier_state", lambda _branch, _sha: ("green", ""))
    return admit.evaluate("owner/name", {"number": 3648, "baseRefName": "main", "labels": []}, admit._utc_now())


def test_evaluate_admits_a_covering_approval(monkeypatch) -> None:
    state, _detail = _evaluate_approved_pr(monkeypatch, commit_date="2026-11-28T07:00:00Z")
    assert state == "admit"


def test_evaluate_skips_a_stale_approval_even_when_commit_id_equals_head(monkeypatch, capsys) -> None:
    """FAR-2871: the merge step would refuse it, so it must not take a train slot."""
    state, detail = _evaluate_approved_pr(monkeypatch, commit_date="2026-11-28T09:11:43Z")
    assert state == "skip"
    assert "approval stale" in detail
    assert "::warning::PR #3648 is NOT admitted" in capsys.readouterr().out


def test_evaluate_fails_closed_when_the_approval_cannot_be_verified(monkeypatch) -> None:
    state, detail = _evaluate_approved_pr(monkeypatch, commit_date="2026-11-28T07:00:00Z", api_error=True)
    assert state == "skip"
    assert "could not verify" in detail
