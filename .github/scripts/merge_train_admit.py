#!/usr/bin/env python3
"""Merge-train admission (phase 1 of the merge train).

Collection logic for the `merge train` path in .github/workflows/merge-queue.yml
(behind the MERGE_TRAIN_ENABLED repo variable). It replicates the existing
queue job's collection gates onto a train manifest so the two paths cannot drift:

  - same-repo author gate   (delegated to .github/scripts/same-repo-author-guard.py)
  - base branch is main
  - APPROVED by farnalabs / farna-reviewbot, and reviewDecision == APPROVED
  - approval still covers the head (the merge step's SHA-pin guard, FAR-2871)
  - fast tier ("CI: Fast Validation") green PINNED TO THE PR's CURRENT HEAD SHA
  - not a hotfix (label `hotfix` absent)
  - not dropped this hour (no `merge-queue-failed` label applied in the last 60m)

Admission is gated on the cheap/fast tier ONLY, never a full suite: the combined
branch is re-validated in full by the reusable `merge-validate.yml` before
anything merges.

Sizing caps, read from repo variables by the calling job:
  TRAIN_TARGET (default 6)  desired train size (admit at most this many)
  TRAIN_MAX    (default 8)  hard ceiling on a train
  TRAIN_WINDOW_MINUTES (default 30)  the batching window, recorded in the
                            manifest. Phase 1 is a single admit pass (the build
                            proceeds immediately); the value is carried so the
                            phase-2 multi-tick batching can time-box a train.

This script never fails the run for one PR: any per-PR error skips that PR with
a warning. It exits 0 even when nothing is admitted, so the build job can gate on
the manifest's size.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

FAST_TIER_WORKFLOW = "CI: Fast Validation"
APPROVER_LOGINS = {"farnalabs", "farna-reviewbot"}
HOTFIX_LABEL = "hotfix"
FAILED_LABEL = "merge-queue-failed"
DROP_WINDOW_SECONDS = 3600

#: A GitHub repository slug is ``owner/name``; both halves are word-ish tokens.
#: Validated before the value reaches any ``gh`` argv (see ``validate_repo``).
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _load_approve_guard() -> Any:
    """Import the sibling merge guard (its hyphenated name rules out ``import``)."""
    path = Path(__file__).with_name("merge-queue-approve-guard.py")
    spec = spec_from_file_location("merge_queue_approve_guard", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the merge guard from {path}")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approve_guard = _load_approve_guard()


def validate_repo(repo: str) -> str:
    """Return ``repo`` only if it is a safe ``owner/name`` slug.

    The value is interpolated into ``gh`` argv (never a shell). Validating it up
    front keeps a malformed ``--repo`` / ``GITHUB_REPOSITORY`` from reaching any
    subprocess and fails loudly instead.
    """
    if not _REPO_PATTERN.fullmatch(repo):
        raise SystemExit(f"::error::invalid repo {repo!r}: expected 'owner/name' (letters, digits, '.', '_' or '-')")
    return repo


def resolve_within(raw_path: str, bases: Sequence[Path]) -> Path:
    """Resolve ``raw_path`` and require it to sit inside one of ``bases``.

    Raises ``SystemExit`` for anything that escapes the allowed directories
    (e.g. ``../escape``), so an unexpected path cannot be read or written
    outside the workspace / process temp dir.
    """
    resolved = Path(raw_path).resolve()
    for base in bases:
        if resolved.is_relative_to(base.resolve()):
            return resolved
    allowed = ", ".join(str(base) for base in bases)
    raise SystemExit(f"::error::refusing path {raw_path!r}: outside {allowed}")


def _run(argv: list[str], *, check: bool = False, input_text: str | None = None) -> str:
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        input=input_text,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    return proc.stdout


def _gh_json(args: list[str]) -> object:
    out = _run(["gh", *args], check=True)
    return json.loads(out) if out.strip() else None


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _parse_ts(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def dropped_within_hour(repo: str, number: int, now: dt.datetime) -> tuple[bool, str]:
    """Return (dropped, note). Fails OPEN (returns False) on any read error, so a
    just-dropped PR may be re-admitted at worst — a redundant validation, never a
    stuck PR."""
    jq_filter = f'.[] | select(.event == "labeled" and .label.name == "{FAILED_LABEL}") | .created_at'
    try:
        out = _run(
            [
                "gh",
                "api",
                "--paginate",
                f"repos/{repo}/issues/{number}/timeline?per_page=100",
                "--jq",
                jq_filter,
            ],
            check=True,
        )
    except Exception as exc:  # fail open by design
        return False, f"could not read the PR timeline ({exc}); admitting (fail open)"
    stamps = [s for s in (line.strip() for line in out.splitlines()) if s]
    if not stamps:
        return False, "no merge-queue-failed label on record"
    parsed = [t for t in (_parse_ts(s) for s in stamps) if t is not None]
    if not parsed:
        return False, "merge-queue-failed label timestamps unparseable; admitting (fail open)"
    latest = max(parsed)
    age = (now - latest).total_seconds()
    if age < DROP_WINDOW_SECONDS:
        return True, f"dropped from a train {int(age // 60)}m ago (within the 1h cooldown)"
    return False, f"last drop was {int(age // 60)}m ago (outside the 1h cooldown)"


def same_repo_ok(repo: str, number: int) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        tmp = fh.name
    try:
        pr_json = _run(["gh", "api", f"repos/{repo}/pulls/{number}"], check=True)
        Path(tmp).write_text(pr_json, encoding="utf-8")
        out = _run(["python3", ".github/scripts/same-repo-author-guard.py", tmp, repo]).strip()
        verdict, _, reason = out.partition("|")
        return verdict == "ok", reason
    except Exception as exc:  # fail closed: skip the PR
        return False, f"author guard could not run ({exc})"
    finally:
        with contextlib.suppress(OSError):
            Path(tmp).unlink()


def approval_covers_head(repo: str, number: int) -> tuple[bool, str]:
    """Apply the merge step's SHA-pin guard. Fails CLOSED: an unverifiable PR is
    not admitted, so it cannot take a train slot it would be skipped from."""
    try:
        pages = {kind: _run(["gh", "api", "--paginate", f"repos/{repo}/pulls/{number}/{kind}?per_page=100"], check=True) for kind in ("reviews", "commits")}
        verdict, reason = approve_guard.decide(
            approve_guard.parse_concatenated(pages["reviews"]),
            approve_guard.parse_concatenated(pages["commits"]),
        )
    except Exception as exc:  # fail closed: skip the PR
        return False, f"could not verify the approval ({exc})"
    return verdict == "ok", reason


def fast_tier_state(branch: str, head_sha: str) -> tuple[str, str]:
    """Return (state, detail) where state is one of: green, waiting, failed.

    Pinned to head_sha: a run for an older commit never counts for this head."""
    if not head_sha:
        return "waiting", "head SHA unresolved"
    try:
        runs_raw = _gh_json(
            [
                "run",
                "list",
                "--branch",
                branch,
                "--workflow",
                FAST_TIER_WORKFLOW,
                "--commit",
                head_sha,
                "--limit",
                "5",
                "--json",
                "conclusion,status,headSha",
            ]
        )
    except Exception as exc:
        return "waiting", f"could not read fast-tier runs ({exc})"
    if not isinstance(runs_raw, list):
        return "waiting", "no fast-tier runs returned"
    runs: list[dict[str, object]] = [r for r in runs_raw if isinstance(r, dict)]
    matching = [r for r in runs if r.get("headSha") == head_sha]
    if not matching:
        return "waiting", f"no fast-tier run for head {head_sha[:12]}"
    run = matching[0]
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "green", "fast tier green at head"
    if conclusion in (None, "", "null") or run.get("status") in (
        "in_progress",
        "queued",
        "pending",
        "requested",
        "waiting",
    ):
        return "waiting", f"fast tier {conclusion or run.get('status') or 'pending'}"
    return "failed", f"fast tier conclusion {conclusion}"


def evaluate(repo: str, pr: dict, now: dt.datetime) -> tuple[str, str]:
    """Return (state, detail) for one PR: state is admit | waiting | skip."""
    number = int(pr["number"])
    labels = {lbl.get("name") for lbl in (pr.get("labels") or [])}

    if pr.get("baseRefName") != "main":
        return "skip", f"base branch '{pr.get('baseRefName')}' is not main"

    if HOTFIX_LABEL in labels:
        return "skip", "carries the hotfix label (a hotfix is not a train candidate)"

    ok, reason = same_repo_ok(repo, number)
    if not ok:
        return "skip", f"same-repo author gate: {reason}"

    detail = _gh_json(["pr", "view", str(number), "--json", "reviews,reviewDecision,headRefOid"])
    if not isinstance(detail, dict):
        return "waiting", "could not read review/head detail"
    if detail.get("reviewDecision") != "APPROVED":
        return "waiting", f"reviewDecision is '{detail.get('reviewDecision')}'"

    approvers = {(r.get("author") or {}).get("login") for r in (detail.get("reviews") or []) if r.get("state") == "APPROVED"}
    if not (approvers & APPROVER_LOGINS):
        return "waiting", f"no APPROVED review from {'/'.join(sorted(APPROVER_LOGINS))}"

    covered, guard_note = approval_covers_head(repo, number)
    if not covered:
        print(f"::warning::PR #{number} is NOT admitted to the train - approval check: {guard_note}")
        return "skip", f"approval does not cover the head: {guard_note}"

    dropped, drop_note = dropped_within_hour(repo, number, now)
    if dropped:
        return "skip", drop_note

    head_sha = detail.get("headRefOid") or ""
    state, ft_detail = fast_tier_state(pr.get("headRefName", ""), head_sha)
    if state == "waiting":
        return "waiting", ft_detail
    if state == "failed":
        return "skip", ft_detail

    return "admit", "approved and fast-tier green at head"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="manifest JSON output path")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--target", type=int, default=int(os.environ.get("TRAIN_TARGET", "6")))
    parser.add_argument("--max", type=int, default=int(os.environ.get("TRAIN_MAX", "8")))
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=int(os.environ.get("TRAIN_WINDOW_MINUTES", "30")),
    )
    args = parser.parse_args()

    if not args.repo:
        print("::error::GITHUB_REPOSITORY is empty; cannot collect PRs.", file=sys.stderr)
        return 1
    repo = validate_repo(args.repo)
    out_path = resolve_within(args.out, (Path.cwd(), Path(tempfile.gettempdir())))

    cap = max(1, min(args.target, args.max))
    now = _utc_now()
    opened_at = now
    closes_at = now + dt.timedelta(minutes=args.window_minutes)

    try:
        prs = _gh_json(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,headRefName,baseRefName,createdAt,labels",
            ]
        )
    except Exception as exc:
        print(f"::error::could not list open PRs: {exc}", file=sys.stderr)
        return 1
    if not isinstance(prs, list):
        prs = []
    prs.sort(key=lambda p: p.get("createdAt") or "")

    eligible: list[dict] = []
    waiting: list[dict] = []
    skipped: list[dict] = []

    for pr in prs:
        number = int(pr["number"])
        try:
            state, detail = evaluate(repo, pr, now)
        except Exception as exc:  # one PR never fails the pass
            state, detail = "skip", f"error evaluating PR: {exc}"
        record = {
            "number": number,
            "title": pr.get("title", ""),
            "head_ref": pr.get("headRefName", ""),
            "detail": detail,
        }
        if state == "admit":
            eligible.append(record)
            print(f"  ADMIT  #{number} {record['title']} -- {detail}")
        elif state == "waiting":
            waiting.append(record)
            print(f"  wait   #{number} -- {detail}")
        else:
            skipped.append(record)
            print(f"  skip   #{number} -- {detail}")

    admitted = eligible[:cap]
    manifest = {
        "repo": repo,
        "cap": cap,
        "train_target": args.target,
        "train_max": args.max,
        "window_minutes": args.window_minutes,
        "opened_at": opened_at.isoformat(),
        "closes_at": closes_at.isoformat(),
        "admitted": admitted,
        "waiting": waiting,
        "skipped": skipped,
        "eligible_count": len(eligible),
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"Admitted {len(admitted)} PR(s) of {len(eligible)} eligible (cap {cap}, target {args.target}, max {args.max}, window {args.window_minutes}m); {len(waiting)} waiting, {len(skipped)} skipped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
