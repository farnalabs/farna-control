#!/usr/bin/env python3
"""PR Reviewer trigger-side dedup decision logic (pr-review.yml).

Reads two JSON files (the GitHub API responses for a PR's reviews and
commits) and a head SHA, then prints exactly one line: `skip=true` or
`skip=false`.

The pr-review.yml "Skip when head already reviewed (dedup)" step calls this
instead of inline bash because
the decision needs to handle multi-line review bodies and pipe characters
in body text, which naive `cut -d'|'` parsing mangles (cut is
line-oriented, so continuation lines bleed into the parsed SHA). Keeping
the logic in a versioned script also makes it unit-testable (see
backend/tests/unit/scripts/test_pr_review_dedup.py).

FAR-2871: the reviews API ``commit_id`` is NOT proof of what was reviewed. It
can report the head at read time, so after a force-push an old review appears to
cover the new head (#3648). Whether the head is covered is therefore decided
FIRST from timestamps, using the merge queue's own guard
(``merge-queue-approve-guard.py``), and only then from ``commit_id``. Without
that, the merge queue refuses a stale approval while this script skips the
re-review, and the PR can never move.
"""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

Review = dict[str, Any]
Commit = dict[str, Any]

#: Review states that carry (or withdraw) a decision. COMMENTED is feedback only:
#: it must never hide that the decision before it no longer covers the head.
VERDICT_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


def _load_guard() -> Any:
    """Import the sibling merge guard (its hyphenated name rules out ``import``)."""
    path = Path(__file__).with_name("merge-queue-approve-guard.py")
    spec = spec_from_file_location("merge_queue_approve_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the merge guard from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def last_review(reviews: list[Review]) -> Review | None:
    """Return the most recent non-PENDING review dict, or None."""
    non_pending = [r for r in reviews if r.get("state") != "PENDING"]
    return non_pending[-1] if non_pending else None


def deciding_review(reviews: list[Review]) -> Review | None:
    """Return the review the dedup decision is anchored on, or None.

    That is the latest review in VERDICT_STATES; a PR with only comments is
    anchored on the latest of those.
    """
    verdicts = [r for r in reviews if r.get("state") in VERDICT_STATES]
    return verdicts[-1] if verdicts else last_review(reviews)


def uncovered_reason(anchor: Review, commits: list[Commit]) -> str | None:
    """Return why *anchor* does not cover the PR's commits, or None if it does.

    For an APPROVED anchor this is exactly the merge guard's rule (any non-merge
    commit committed after ``submitted_at``). For every other state a later
    MERGE commit counts too, because a merge can resolve a CHANGES_REQUESTED.
    """
    if not commits:
        return "commit list is empty"
    if anchor.get("state") == "APPROVED":
        verdict, reason = guard.decide([anchor], commits)
        return None if verdict == "ok" else f"merge guard says stale: {reason}"
    try:
        review_epoch = guard.parse_iso(anchor.get("submitted_at") or "")
    except ValueError:
        return "review timestamp is missing or unparseable"
    later = sum(1 for c in commits if guard.committed_after(c, review_epoch))
    if later:
        return f"{later} commit(s) committed after the {anchor.get('state')} review ({anchor.get('submitted_at')})"
    return None


def non_merge_commits_since(commits: list[Commit], last_sha: str) -> tuple[bool, int]:
    """Return (found_last, count_non_merge_since).

    Walk the PR commit list newest-first (reverse order) until we hit the
    commit the last review was posted against. Count commits with exactly one
    parent (non-merge). A force-push may have rewritten history so the last
    review SHA no longer exists -> found_last=False.
    """
    count = 0
    found = False
    for c in reversed(commits):
        if c.get("sha") == last_sha:
            found = True
            break
        if len(c.get("parents", []) or []) == 1:
            count += 1
    return found, count


def decide(reviews: list[Review], commits: list[Commit], head_sha: str) -> tuple[bool, str]:
    """Return (skip: bool, reason: str).

    skip=True  -> the PR Reviewer webhook should NOT fire for this head.
    skip=False -> dispatch (first review, real fix, or unverifiable state).
    """
    rev = deciding_review(reviews)
    if rev is None:
        return False, "No prior review - dispatch first review."
    if rev.get("state") == "DISMISSED":
        return False, "Latest review decision was dismissed - dispatch."
    uncovered = uncovered_reason(rev, commits)
    if uncovered:
        # Checked BEFORE commit_id, which cannot be trusted after a push.
        return False, f"Prior review does not cover the head: {uncovered} - dispatch."
    last_sha = rev.get("commit_id") or ""

    if last_sha == head_sha:
        return True, "Head already reviewed (same SHA) - skip."

    found, non_merge = non_merge_commits_since(commits, last_sha)
    if not found:
        # Force-push rewrote history (or API inconsistency). Fail open: we
        # cannot prove nothing changed, so dispatch.
        return (
            False,
            "Last review SHA not found in history (force-push?) - dispatch (fail open).",
        )
    if non_merge > 0:
        return False, f"{non_merge} non-merge commit(s) since last review - dispatch."

    # Zero non-merge commits: head moved only via merge commits (Branch Fixer
    # "merge origin/main" churn). No new PR code exists. Decide whether a
    # re-review can still change the outcome:
    state = rev.get("state") or ""
    if state == "APPROVED":
        # Approval stands: head moved only via merges, no new code. Skip.
        return True, "Only merge commits since last review (no new code) - skip."
    if state == "CHANGES_REQUESTED":
        # FAIL OPEN: a merge can resolve a CI-failure CR or realign a stale
        # base. A re-review can only re-confirm the CR (still blocked) or
        # approve (unblock) - never wrongly unblock. Skipping deadlocks the PR.
        return (
            False,
            "Prior review was a CHANGES_REQUESTED - merge may resolve it - dispatch (fail open).",
        )
    # COMMENTED / empty: no decisive prior decision, so a fresh
    # formal review after a main merge is worth the cost (fail open).
    return False, f"Prior state {state or 'empty'} is not decisive - dispatch."


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: pr-review-dedup.py <reviews.json> <commits.json> <head_sha>",
            file=sys.stderr,
        )
        return 2
    try:
        # Handles the concatenated pages `gh api --paginate` writes.
        reviews = guard.load_concatenated(argv[1])
        commits = guard.load_concatenated(argv[2])
        head_sha = argv[3]
    except Exception as exc:  # fail open on any I/O or JSON error
        print("skip=false", flush=True)
        print(f"error loading inputs: {exc} - dispatch (fail open)", file=sys.stderr)
        return 0
    skip, reason = decide(reviews, commits, head_sha)
    print("skip=true" if skip else "skip=false", flush=True)
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
