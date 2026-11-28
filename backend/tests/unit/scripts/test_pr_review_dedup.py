"""Unit tests for .github/scripts/pr-review-dedup.py (FAR-2111).

The script is the trigger-side dedup decision for ``pr-review.yml``: it answers
"has this head SHA already been reviewed?" from the PR's reviews plus commits and
prints exactly one ``skip=true`` / ``skip=false`` line.

FAR-2111 made the reviewer start on PR readiness (``pull_request_target``:
``opened`` / ``ready_for_review``) instead of only on product-CI completion, so
the same head SHA now has two dispatch paths. These tests pin the mechanism that
coalesces them:

* the decision is by head SHA and consult NO review identity — a review posted
  by any identity (``farna-reviewbot`` today) counts, which is what lets the
  late completion dispatch be skipped;
* a first review, and a genuinely new non-merge commit, still dispatch;
* unreadable input fails OPEN (dispatch) — a review must never be dropped on a
  load error.
"""

from __future__ import annotations

import contextlib
import io
import json
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / ".github" / "scripts" / "pr-review-dedup.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find .github/scripts/pr-review-dedup.py")

_loader = SourceFileLoader("pr_review_dedup", str(script_path))
dedup = module_from_spec(spec_from_loader("pr_review_dedup", _loader))
_loader.exec_module(dedup)

HEAD = "a" * 40
PREVIOUS = "b" * 40

#: Default timeline: commit, then review, then (where a test needs it) new work.
BEFORE_REVIEW = "2026-11-28T07:00:00Z"
REVIEW_AT = "2026-11-28T07:51:11Z"
AFTER_REVIEW = "2026-11-28T09:11:43Z"
LATER_STILL = "2026-11-28T12:54:38Z"


def _review(state: str, commit_id: str, login: str = "farna-reviewbot", at: str = REVIEW_AT) -> dict:
    return {
        "state": state,
        "submitted_at": at,
        "commit_id": commit_id,
        "user": {"login": login},
        "author_association": "MEMBER",
        "body": "a multi-line body\nwith a pipe | and a\nsecond line",
    }


def _commit(sha: str, parents: int = 1, date: str = BEFORE_REVIEW) -> dict:
    return {"sha": sha, "parents": [{"sha": "p" * 40}] * parents, "commit": {"committer": {"date": date}}}


def test_skips_when_the_head_sha_has_already_been_reviewed() -> None:
    """The completion dispatch for a head the early run already reviewed is skipped."""
    skip, reason = dedup.decide([_review("APPROVED", HEAD)], [_commit(HEAD)], HEAD)
    assert skip is True
    assert "same SHA" in reason


def test_the_skip_is_independent_of_who_posted_the_review() -> None:
    """No review identity is consulted, so any reviewer's review coalesces the dispatch.

    ``pr-review.yml`` posts through ``farna-reviewbot``; the decision must not
    depend on that login (or on any allowlist of it), or a rebrand / token
    rotation would silently start double-reviewing every head SHA.
    """
    for login in ("farna-reviewbot", "farnalabs", "github-actions[bot]"):
        skip, _ = dedup.decide([_review("CHANGES_REQUESTED", HEAD, login=login)], [_commit(HEAD)], HEAD)
        assert skip is True


def test_dispatches_the_first_review() -> None:
    skip, reason = dedup.decide([], [_commit(HEAD)], HEAD)
    assert skip is False
    assert "first review" in reason


def test_dispatches_when_a_new_non_merge_commit_landed() -> None:
    reviews = [_review("APPROVED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is False
    assert "non-merge commit" in reason


def test_dispatches_when_a_merge_only_move_followed_a_changes_requested() -> None:
    """A CR is re-reviewed after a merge-only move: it can never wrongly unblock."""
    reviews = [_review("CHANGES_REQUESTED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD, parents=2)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is False
    assert "CHANGES_REQUESTED" in reason


def test_skips_a_merge_only_move_after_an_approval() -> None:
    reviews = [_review("APPROVED", PREVIOUS)]
    commits = [_commit(PREVIOUS), _commit(HEAD, parents=2)]
    skip, reason = dedup.decide(reviews, commits, HEAD)
    assert skip is True
    assert "merge commits" in reason


def test_dispatches_when_the_reviewed_sha_was_force_pushed_away() -> None:
    reviews = [_review("APPROVED", "c" * 40)]
    skip, reason = dedup.decide(reviews, [_commit(HEAD)], HEAD)
    assert skip is False
    assert "fail open" in reason


def test_main_fails_open_on_unreadable_input(tmp_path: Path) -> None:
    """A load error must dispatch, never silently drop the review."""
    missing = tmp_path / "missing.json"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = dedup.main(["pr-review-dedup.py", str(missing), str(missing), HEAD])
    assert code == 0
    assert stdout.getvalue().strip() == "skip=false"
    assert "fail open" in stderr.getvalue()


def test_main_prints_one_line_for_a_usable_decision(tmp_path: Path) -> None:
    reviews = tmp_path / "reviews.json"
    commits = tmp_path / "commits.json"
    reviews.write_text(json.dumps([_review("APPROVED", HEAD)]), encoding="utf-8")
    commits.write_text(json.dumps([_commit(HEAD)]), encoding="utf-8")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = dedup.main(["pr-review-dedup.py", str(reviews), str(commits), HEAD])
    assert code == 0
    assert stdout.getvalue().strip() == "skip=true"


# --- FAR-2871: commit_id is not proof of what was reviewed -------------------


@pytest.mark.parametrize(
    "trailing_comment",
    [False, True],
    ids=["approval-only", "later-comment-also-on-new-head"],
)
def test_dispatches_when_a_force_push_made_the_approval_stale_but_commit_id_equals_head(trailing_comment: bool) -> None:
    """The exact #3648 shape: approved 07:51:11Z, force-pushed 09:11:43Z, and the
    API then reports the approval's ``commit_id`` as the NEW head. On the real PR
    a COMMENTED review followed at 12:54:38Z, which must not hide the staleness.
    """
    reviews = [_review("COMMENTED", PREVIOUS, at=BEFORE_REVIEW), _review("APPROVED", HEAD)]
    if trailing_comment:
        reviews.append(_review("COMMENTED", HEAD, login="farnalabs", at=LATER_STILL))
    commits = [_commit(HEAD, date=AFTER_REVIEW)]

    skip, reason = dedup.decide(reviews, commits, HEAD)

    assert skip is False
    assert "merge guard says stale" in reason
    verdict, _ = dedup.guard.decide(reviews, commits, HEAD)
    assert verdict == "stale"


@pytest.mark.parametrize("state", ["CHANGES_REQUESTED", "COMMENTED"])
def test_same_sha_does_not_skip_when_code_landed_after_a_non_approval(state: str) -> None:
    skip, reason = dedup.decide([_review(state, HEAD)], [_commit(HEAD, date=AFTER_REVIEW)], HEAD)
    assert skip is False
    assert "does not cover the head" in reason


def test_a_comment_does_not_hide_code_pushed_after_changes_requested() -> None:
    reviews = [_review("CHANGES_REQUESTED", HEAD), _review("COMMENTED", HEAD, at=LATER_STILL)]
    skip, _ = dedup.decide(reviews, [_commit(HEAD, date=AFTER_REVIEW)], HEAD)
    assert skip is False


def test_same_sha_merge_commit_after_changes_requested_dispatches() -> None:
    skip, _ = dedup.decide([_review("CHANGES_REQUESTED", HEAD)], [_commit(HEAD, parents=2, date=AFTER_REVIEW)], HEAD)
    assert skip is False


def test_same_sha_merge_commit_after_approval_still_skips() -> None:
    commits = [_commit(PREVIOUS), _commit(HEAD, parents=2, date=AFTER_REVIEW)]
    skip, _ = dedup.decide([_review("APPROVED", HEAD)], commits, HEAD)
    assert skip is True


@pytest.mark.parametrize("fresh_state", ["APPROVED", "CHANGES_REQUESTED"])
def test_skips_again_once_a_fresh_review_exists(fresh_state: str) -> None:
    """No review storm: a verdict submitted after the new code ends the re-dispatch."""
    reviews = [_review("APPROVED", HEAD), _review(fresh_state, HEAD, at=LATER_STILL)]
    skip, _ = dedup.decide(reviews, [_commit(HEAD, date=AFTER_REVIEW)], HEAD)
    assert skip is True


def test_a_comment_after_a_covering_approval_still_skips() -> None:
    reviews = [_review("APPROVED", HEAD), _review("COMMENTED", HEAD, at=LATER_STILL)]
    skip, _ = dedup.decide(reviews, [_commit(HEAD)], HEAD)
    assert skip is True


def test_a_comment_only_review_of_an_unchanged_head_skips() -> None:
    skip, _ = dedup.decide([_review("COMMENTED", HEAD)], [_commit(HEAD)], HEAD)
    assert skip is True


@pytest.mark.parametrize("trailing_comment", [False, True])
def test_a_dismissed_decision_dispatches(trailing_comment: bool) -> None:
    reviews = [_review("DISMISSED", HEAD)]
    if trailing_comment:
        reviews.append(_review("COMMENTED", HEAD, at=LATER_STILL))
    skip, reason = dedup.decide(reviews, [_commit(HEAD)], HEAD)
    assert skip is False
    assert "dismissed" in reason


def test_a_fresh_verdict_after_a_dismissal_skips() -> None:
    reviews = [_review("DISMISSED", HEAD), _review("APPROVED", HEAD, at=LATER_STILL)]
    skip, _ = dedup.decide(reviews, [_commit(HEAD)], HEAD)
    assert skip is True


def test_an_empty_commit_list_dispatches() -> None:
    """pr-review.yml writes ``[]`` when the commits fetch fails: unverifiable."""
    skip, reason = dedup.decide([_review("APPROVED", HEAD)], [], HEAD)
    assert skip is False
    assert "commit list is empty" in reason


def test_a_non_approval_without_a_timestamp_dispatches() -> None:
    skip, reason = dedup.decide([_review("CHANGES_REQUESTED", HEAD, at="")], [_commit(HEAD)], HEAD)
    assert skip is False
    assert "timestamp" in reason


@pytest.mark.parametrize(
    ("newest_commit_date", "expected"),
    [(BEFORE_REVIEW, "skip=true"), (AFTER_REVIEW, "skip=false")],
    ids=["covered", "stale-on-second-page"],
)
def test_main_reads_every_paginated_document(tmp_path: Path, newest_commit_date: str, expected: str) -> None:
    """``gh api --paginate`` can emit one JSON document per page, concatenated."""
    reviews = tmp_path / "reviews.json"
    commits = tmp_path / "commits.json"
    reviews.write_text(json.dumps([_review("COMMENTED", PREVIOUS)]) + json.dumps([_review("APPROVED", HEAD)]), encoding="utf-8")
    commits.write_text(json.dumps([_commit(PREVIOUS)]) + json.dumps([_commit(HEAD, date=newest_commit_date)]), encoding="utf-8")
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = dedup.main(["pr-review-dedup.py", str(reviews), str(commits), HEAD])
    assert code == 0
    assert stdout.getvalue().strip() == expected
