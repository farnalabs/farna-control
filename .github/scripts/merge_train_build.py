#!/usr/bin/env python3
"""Merge-train build (phase 1 of the merge train).

Given the train manifest written by merge_train_admit.py, this builds the train
branch from the base branch, squash-merges the admitted PRs in order, runs the
uv.lock freshness check and the migration-heads hard gate with per-PR attribution,
and writes a state JSON for the merge job.

Phase 1 notes
-------------
- This DUPLICATES the migration-heads / lock logic that lives inline in the
  existing merge-queue.yml `queue` job. Duplication is deliberate for phase 1:
  the old job is left byte-identical (except its flag guard) until the train is
  proven, then deleted at rollout step 3.
- On a migration-head failure the script attributes each offending head to the
  PR that introduced it, DROPS those PR(s) and rebuilds, exactly as the queue
  does, so one malformed PR cannot hold the rest of the train hostage.
- Bisection on a VALIDATION failure (a red combined branch with no attributable
  PR) is PHASE 2; see the TODO in merge-queue.yml's `merge` job failure path.

Exit code: 0 when the branch is ready to validate, 1 when the build failed. The
state file is written on BOTH the success and the failure path so the merge job
can triage a failed build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

MIG_DIR = "backend/src/farna/db/migrations/versions"
MAX_DROP_ROUNDS = 3
#: The base ref the train branch is built from and diffed against.
ORIGIN_MAIN = "origin/main"


def resolve_within(raw_path: str, bases: Sequence[Path]) -> Path:
    """Resolve ``raw_path`` and require it to sit inside one of ``bases``.

    Raises ``SystemExit`` for anything that escapes the allowed directories
    (e.g. ``../escape``), so a manifest/output path cannot be read or written
    outside the workspace / process temp dir.
    """
    resolved = Path(raw_path).resolve()
    for base in bases:
        if resolved.is_relative_to(base.resolve()):
            return resolved
    allowed = ", ".join(str(base) for base in bases)
    raise SystemExit(f"::error::refusing path {raw_path!r}: outside {allowed}")


def git(*args: str, cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def run(argv: list[str], cwd: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def commit_squash(message: str, cwd: str) -> None:
    # Read the bot identity from the workflow-level env (MERGE_BOT_GIT_NAME /
    # MERGE_BOT_GIT_EMAIL, defined once in merge-queue.yml) so the train commit
    # author matches the queue job. The fallbacks keep local/test runs working
    # when those variables are unset.
    name = os.environ.get("MERGE_BOT_GIT_NAME", "modulo-merge-bot")
    email = os.environ.get("MERGE_BOT_GIT_EMAIL", "bot@modulo.run")
    git(
        "-c",
        f"user.name={name}",
        "-c",
        f"user.email={email}",
        "commit",
        "-q",
        "-m",
        message,
        cwd=cwd,
        check=True,
    )


def mig_first_literal(content: str, field: str) -> str:
    """First string literal of a module-level `revision` / `down_revision`
    assignment. Handles a type annotation and both quote styles; tuple form
    returns the first parent; `None` returns ''."""
    pat = re.compile(r"^[ \t]*" + re.escape(field) + r"(\s*:[^=]*)?\s*=\s*(.*)$", re.MULTILINE)
    m = pat.search(content)
    if not m:
        return ""
    value = m.group(2)
    dq = re.search(r'"([^"]*)"', value)
    if dq:
        return dq.group(1)
    sq = re.search(r"'([^']*)'", value)
    if sq:
        return sq.group(1)
    return ""


def main_migration_graph(repo_root: str) -> tuple[dict[str, str], str | None, str]:
    """Return (prefix_holder, main_head, problem). prefix_holder maps a numeric
    prefix to its basename on the base branch; main_head is the single childless
    revision or None when the graph is not a clean single chain."""
    holder: dict[str, str] = {}
    ls = git("ls-tree", "--name-only", ORIGIN_MAIN, "--", f"{MIG_DIR}/", cwd=repo_root)
    files = [f for f in ls.stdout.splitlines() if f.endswith(".py")]
    for path in files:
        base = Path(path).name
        prefix = base.split("_", 1)[0]
        if prefix:
            holder[prefix] = base

    seen: dict[str, str] = {}
    has_child: dict[str, int] = {}
    problem = ""
    for path in files:
        content = git("show", f"{ORIGIN_MAIN}:{path}", cwd=repo_root).stdout
        if not content:
            problem = f"could not read {path} from main"
            break
        rev = mig_first_literal(content, "revision")
        if not rev:
            problem = f"{path} on main has no parseable revision"
            break
        if rev in seen:
            problem = f"revision {rev} is defined by more than one file on main"
            break
        down = mig_first_literal(content, "down_revision")
        seen[rev] = path
        if down:
            has_child[down] = 1
    if problem:
        return holder, None, problem
    heads = [rev for rev in seen if rev not in has_child]
    if len(heads) == 1:
        return holder, heads[0], ""
    return holder, None, f"main's migration graph has {len(heads)} head(s), not exactly 1"


def pr_diff_status(repo_root: str, num: int) -> list[tuple[str, str, str]]:
    """`git diff --name-status origin/main...pr-N -- MIG_DIR` as (status, path, extra)."""
    proc = git("diff", "--name-status", f"{ORIGIN_MAIN}...pr-{num}", "--", MIG_DIR, cwd=repo_root)
    out: list[tuple[str, str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            out.append((status, parts[1], parts[2]))
        elif len(parts) >= 2:
            out.append((status, parts[1], ""))
    return out


def _effective_prefix(
    prefix: str,
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> str:
    """The basename currently holding ``prefix``, honouring staged claims/releases."""
    if prefix in staged_claim:
        return staged_claim[prefix]
    if prefix in prefix_holder:
        if prefix in staged_release and staged_release[prefix] == prefix_holder[prefix]:
            return ""
        return prefix_holder[prefix]
    return ""


def _release_prefix(staged_release: dict[str, str], base: str) -> None:
    """Record that ``base`` frees its numeric prefix (a delete, or a rename source)."""
    if base:
        staged_release[base.split("_", 1)[0]] = base


def _claim_or_report(
    prefix: str,
    base: str,
    message_template: str,
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> str | None:
    """Claim ``prefix`` for ``base``, or return a collision line.

    A collision is reported (with ``{holder}`` substituted) when the prefix is
    already held by a different basename; otherwise ``base`` is staged as the
    prefix's holder and ``None`` is returned.
    """
    holder = _effective_prefix(prefix, prefix_holder, staged_claim, staged_release)
    if holder and holder != base:
        return message_template.format(holder=holder)
    staged_claim[prefix] = base
    return None


def _rename_collision(
    num: int,
    path: str,
    extra: str,
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> str | None:
    """Stage a rename entry (release its source prefix, claim its target)."""
    _release_prefix(staged_release, Path(path).name)
    new_base = Path(extra or "").name
    new_prefix = new_base.split("_", 1)[0] if new_base else ""
    if not new_prefix:
        return None
    template = f"PR #{num}: renames onto {new_base}, prefix {new_prefix} is already held by {{holder}}"
    return _claim_or_report(new_prefix, new_base, template, prefix_holder, staged_claim, staged_release)


def _add_collision(
    num: int,
    path: str,
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> str | None:
    """Stage an add entry (claim its numeric prefix) and report any collision."""
    base = Path(path).name
    prefix = base.split("_", 1)[0] if base else ""
    if not prefix:
        return None
    template = f"PR #{num}: adds {base}, numeric prefix {prefix} is already held by {{holder}}"
    return _claim_or_report(prefix, base, template, prefix_holder, staged_claim, staged_release)


def _prefix_collisions(
    num: int,
    diff: list[tuple[str, str, str]],
    prefix_holder: dict[str, str],
) -> tuple[bool, list[str], dict[str, str], dict[str, str]]:
    """Detect numeric-prefix collisions across a PR's migration diff.

    Returns (collides, detail_lines, staged_claim, staged_release); the caller
    applies the staged maps only when it accepts the PR.

    Two passes: stage every release (a delete, or a rename's source) BEFORE any
    claim. `git diff --name-status` orders by path, so a same-prefix replace
    emits the add (``A 0100_new.py``) *before* the delete (``D 0100_old.py``);
    a single pass would see main's old file still holding the prefix and
    falsely report a collision. Releasing first makes an add-then-delete and a
    delete-then-add see the same released state.
    """
    detail: list[str] = []
    staged_claim: dict[str, str] = {}
    staged_release: dict[str, str] = {}

    # Pass 1: releases only, so a prefix this PR's own diff frees is free to
    # claim in pass 2 regardless of git's path ordering.
    for status, path, _extra in diff:
        if status == "D" or status.startswith("R"):
            _release_prefix(staged_release, Path(path).name)

    # Pass 2: claims only.
    for status, path, extra in diff:
        if status.startswith("R"):
            message = _rename_collision(num, path, extra, prefix_holder, staged_claim, staged_release)
        elif status == "D":
            message = None
        else:
            message = _add_collision(num, path, prefix_holder, staged_claim, staged_release)
        if message:
            detail.append(message)
    return bool(detail), detail, staged_claim, staged_release


def _newly_added_migration_path(repo_root: str, status: str, path: str, extra: str) -> str:
    """The migration path a diff entry ADDS under MIG_DIR, or '' if none.

    An add is the new path; a rename is the new path unless its old path is an
    existing migration on main (which means it is a move, not a new file)."""
    if status == "A":
        new_path = path
    elif status.startswith("R"):
        new_path = extra or ""
        if path.startswith(MIG_DIR + "/") and (git("cat-file", "-e", f"{ORIGIN_MAIN}:{path}", cwd=repo_root).returncode == 0):
            return ""
    else:
        return ""
    if not new_path or not new_path.startswith(MIG_DIR + "/"):
        return ""
    return new_path


def _parse_added_migrations(
    repo_root: str,
    num: int,
    diff: list[tuple[str, str, str]],
    main_head: str,
) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """Parse the migrations a PR ADDS into (added_rev, added_down, order, detail).

    ``detail`` carries a line per added migration whose ``revision`` could not be
    parsed (a collision the caller must surface)."""
    added_rev: dict[str, str] = {}
    added_down: dict[str, str] = {}
    order: list[str] = []
    detail: list[str] = []
    for status, path, extra in diff:
        new_path = _newly_added_migration_path(repo_root, status, path, extra)
        if not new_path:
            continue
        content = git("show", f"pr-{num}:{new_path}", cwd=repo_root).stdout
        rev = mig_first_literal(content, "revision")
        if not rev:
            detail.append(f"PR #{num}: adds {Path(new_path).name} but its revision could not be parsed - cannot show it chains onto main's head '{main_head}'")
            continue
        added_rev[rev] = new_path
        added_down[rev] = mig_first_literal(content, "down_revision")
        order.append(rev)
    return added_rev, added_down, order, detail


def _chain_collision(
    num: int,
    added_rev: dict[str, str],
    added_down: dict[str, str],
    order: list[str],
    main_head: str,
) -> tuple[bool, list[str]]:
    """Check the added migrations form one chain rooted on main's head."""
    if not order:
        return False, []
    roots = [rev for rev in order if not added_down[rev] or added_down[rev] not in added_rev]
    if not roots:
        return True, [f"PR #{num}: added migrations ({', '.join(order)}) form a down_revision cycle; cannot reach main's head '{main_head}'"]
    if len(roots) > 1:
        return True, [f"PR #{num}: adds {len(roots)} independent migrations ({', '.join(roots)}) that do not chain onto each other - multiple Alembic heads"]
    first = roots[0]
    if added_down[first] != main_head:
        return True, [f"PR #{num}: adds {Path(added_rev[first]).name} with down_revision '{added_down[first]}', but main's head is '{main_head}' - a second Alembic head"]
    return False, []


def premerge_collision(
    repo_root: str,
    num: int,
    prefix_holder: dict[str, str],
    main_head: str | None,
) -> tuple[bool, list[str], dict[str, str], dict[str, str]]:
    """Per-PR pre-merge migration gate. Returns (collides, detail_lines,
    staged_claim, staged_release). The caller applies the staged maps to
    prefix_holder only when it accepts the PR."""
    diff = pr_diff_status(repo_root, num)
    collides, detail, staged_claim, staged_release = _prefix_collisions(num, diff, prefix_holder)

    # Multi-head check on what THIS PR adds (only when main's head is known).
    if main_head:
        added_rev, added_down, order, parse_detail = _parse_added_migrations(repo_root, num, diff, main_head)
        if parse_detail:
            collides = True
            detail.extend(parse_detail)
        chain_collides, chain_detail = _chain_collision(num, added_rev, added_down, order, main_head)
        collides = collides or chain_collides
        detail.extend(chain_detail)

    return collides, detail, staged_claim, staged_release


def apply_staged(
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> None:
    for prefix, base in staged_release.items():
        if prefix_holder.get(prefix) == base:
            prefix_holder.pop(prefix, None)
    prefix_holder.update(staged_claim)


def alembic_heads(repo_root: str) -> tuple[list[str], str]:
    proc = run(
        ["uv", "run", "--no-build", "--no-sync", "python", "-m", "alembic", "heads"],
        cwd=str(Path(repo_root) / "backend"),
    )
    text = (proc.stdout or "") + (proc.stderr or "")
    heads = []
    for line in text.splitlines():
        if "(head)" in line or "(effective head)" in line:
            token = line.strip().split(" ", 1)[0]
            if token:
                heads.append(token)
    return sorted(set(heads)), text


def rebuild_without(
    repo_root: str,
    branch: str,
    accepted_order: list[int],
    offenders: set[int],
) -> tuple[list[int], list[dict]]:
    """Recreate `branch` from origin/main, squash-merging every accepted PR not in
    offenders. Returns (remaining_accepted, new_conflicts)."""
    git("checkout", "-B", branch, ORIGIN_MAIN, cwd=repo_root, check=True)
    remaining: list[int] = []
    conflicts: list[dict] = []
    for num in accepted_order:
        if num in offenders:
            continue
        git("fetch", "origin", f"+refs/pull/{num}/head:refs/heads/pr-{num}", cwd=repo_root)
        if git("rev-parse", "--verify", "-q", f"pr-{num}", cwd=repo_root).returncode != 0:
            conflicts.append({"number": num, "reason": "PR head unavailable during rebuild"})
            continue
        merged = git("merge", "--squash", f"pr-{num}", cwd=repo_root)
        if merged.returncode != 0:
            git("merge", "--abort", cwd=repo_root)
            git("reset", "--hard", "HEAD", cwd=repo_root)
            conflicts.append({"number": num, "reason": "merge conflict during rebuild"})
            continue
        if git("diff", "--cached", "--quiet", cwd=repo_root).returncode == 0:
            # No staged change relative to the rebuilt branch (already present).
            continue
        commit_squash(f"squash! Merge PR #{num}", repo_root)
        remaining.append(num)
    return remaining, conflicts


def _admit_prs(
    repo_root: str,
    branch: str,
    admitted: list,
    main_head: str | None,
    prefix_holder: dict[str, str],
    state: dict,
    migration_details: list[str],
) -> list[int]:
    """Fetch and squash-merge each admitted PR in order; return the accepted order."""
    accepted_order: list[int] = []
    for pr in admitted:
        num = int(pr["number"])
        git("fetch", "origin", f"+refs/pull/{num}/head:refs/heads/pr-{num}", cwd=repo_root)
        if git("rev-parse", "--verify", "-q", f"pr-{num}", cwd=repo_root).returncode != 0:
            state["excluded_prs"].append({"number": num, "reason": "PR head unavailable"})
            continue

        collides, detail, claim, release = premerge_collision(repo_root, num, prefix_holder, main_head)
        if collides:
            state["excluded_prs"].append(
                {
                    "number": num,
                    "reason": "migration collision (would produce multiple Alembic heads)",
                }
            )
            migration_details.extend(detail)
            continue

        git("reset", "--hard", "HEAD", cwd=repo_root)
        merged = git("merge", "--squash", f"pr-{num}", cwd=repo_root)
        if merged.returncode != 0:
            git("merge", "--abort", cwd=repo_root)
            git("reset", "--hard", "HEAD", cwd=repo_root)
            state["excluded_prs"].append({"number": num, "reason": "merge conflict with main or an earlier PR"})
            continue
        if git("diff", "--cached", "--quiet", cwd=repo_root).returncode == 0:
            # Nothing to merge (content already present) - not counted as merged.
            print(f"PR #{num}: no changes to merge (content already present) - skipped.")
            continue
        commit_squash(f"squash! Merge PR #{num}", repo_root)
        # Accepted: commit this PR's staged migration claims/releases so a later
        # PR in the train is judged against what the batch holds.
        apply_staged(prefix_holder, claim, release)
        accepted_order.append(num)
        print(f"PR #{num}: squash-merged into the train branch.")
    return accepted_order


def _prs_touching_file(repo_root: str, accepted_order: list[int], head_file: str) -> list[int]:
    """Accepted PRs whose diff against main touches ``head_file``."""
    found: list[int] = []
    for cand in accepted_order:
        if git("rev-parse", "--verify", "-q", f"pr-{cand}", cwd=repo_root).returncode != 0:
            continue
        diff = git(
            "diff",
            "--name-only",
            f"{ORIGIN_MAIN}...pr-{cand}",
            "--",
            head_file,
            cwd=repo_root,
        ).stdout
        if head_file in diff.splitlines():
            found.append(cand)
    return found


def _attribute_heads(
    repo_root: str,
    branch: str,
    heads: list[str],
    accepted_order: list[int],
) -> tuple[set[int], list[str], list[str]]:
    """Attribute each train-branch head to the accepted PR(s) that introduced it.

    Returns (offenders, unattributed, detail_lines); unattributed heads are those
    not traceable to a train PR (e.g. already on main)."""
    offenders: set[int] = set()
    unattributed: list[str] = []
    details: list[str] = []
    for rev in heads:
        head_file = f"{MIG_DIR}/{rev}.py"
        if git("cat-file", "-e", f"{branch}:{head_file}", cwd=repo_root).returncode != 0:
            found = _find_migration_file_by_revision(repo_root, rev)
            if found:
                head_file = found
        if git("cat-file", "-e", f"{ORIGIN_MAIN}:{head_file}", cwd=repo_root).returncode == 0:
            continue  # already on main - not attributable to a train PR
        found_prs = _prs_touching_file(repo_root, accepted_order, head_file)
        if not found_prs:
            unattributed.append(f"{rev}({head_file})")
            continue
        for pr_num in found_prs:
            offenders.add(pr_num)
            details.append(f"PR #{pr_num}: adds {head_file} (revision '{rev}'), which branches the migration graph into multiple Alembic heads on the train branch.")
    return offenders, unattributed, details


def _choose_drop_set(
    repo_root: str,
    branch: str,
    accepted_order: list[int],
    offenders: set[int],
) -> set[int]:
    """Minimal drop: if several offenders, the single PR whose removal restores
    one head wins; otherwise drop all offenders."""
    if len(offenders) <= 1:
        return offenders
    for candidate in sorted(offenders):
        rebuild_without(repo_root, branch, accepted_order, {candidate})
        cand_heads, _ = alembic_heads(repo_root)
        if len(cand_heads) == 1:
            print(f"Minimal migration-head drop: PR #{candidate} alone restores one head.")
            return {candidate}
    return offenders


def _resolve_multi_heads(
    repo_root: str,
    branch: str,
    accepted_order: list[int],
    state: dict,
    migration_details: list[str],
) -> tuple[list[int], str]:
    """Drop and rebuild until exactly one Alembic head remains.

    Returns (accepted_order, failure_description); failure_description is "" on
    success."""
    heads, _ = alembic_heads(repo_root)
    print(f"Alembic heads on the train branch: {len(heads)} ({', '.join(heads)})")
    rounds = 0
    while len(heads) != 1:
        if rounds >= MAX_DROP_ROUNDS:
            print("::error::Could not resolve the Alembic multi-head graph.")
            return accepted_order, (f"Alembic multi-head graph could not be resolved after {MAX_DROP_ROUNDS} drop-and-rebuild rounds (heads: {', '.join(heads)})")
        rounds += 1

        offenders, unattributed, details = _attribute_heads(repo_root, branch, heads, accepted_order)
        migration_details.extend(details)
        if not offenders:
            print("::error::Multi-head graph not attributable to a train PR.")
            return accepted_order, (f"Alembic reports multiple heads but none is attributable to a train PR (unattributed: {', '.join(unattributed) or 'none'})")

        chosen = _choose_drop_set(repo_root, branch, accepted_order, offenders)
        print(f"Dropping migration-head offender(s): {sorted(chosen)}")
        for pr_num in sorted(chosen):
            state["excluded_prs"].append(
                {
                    "number": pr_num,
                    "reason": "adds a migration that branches the graph into multiple Alembic heads",
                }
            )
        accepted_order, new_conflicts = rebuild_without(repo_root, branch, accepted_order, chosen)
        state["excluded_prs"].extend(new_conflicts)
        heads, _ = alembic_heads(repo_root)
        print(f"After rebuild: {len(heads)} Alembic head(s) ({', '.join(heads)})")
    return accepted_order, ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    repo_root = str(Path(args.repo_root).resolve())
    bases = (Path(repo_root), Path(tempfile.gettempdir()))
    manifest_path = resolve_within(args.manifest, bases)
    out_path = resolve_within(args.out, bases)
    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    admitted = manifest.get("admitted", [])

    state: dict = {
        "success": False,
        "branch_ref": args.branch,
        "base_ref": ORIGIN_MAIN,
        "merged_prs": [],
        "excluded_prs": [],
        "admitted_prs": [int(p["number"]) for p in admitted],
        "failure_description": "",
        "migration_collision_details": "",
    }
    migration_details: list[str] = []

    try:
        git("fetch", "origin", "main", cwd=repo_root)
        git("checkout", "-B", args.branch, ORIGIN_MAIN, cwd=repo_root, check=True)

        prefix_holder, main_head, problem = main_migration_graph(repo_root)
        if main_head:
            print(f"Migration multi-head gate active: main's head is '{main_head}'.")
        else:
            print(f"::warning::Migration multi-head gate SKIPPED - {problem}. A PR that forks the graph at a free prefix will only be caught by the post-merge heads gate.")

        accepted_order = _admit_prs(repo_root, args.branch, admitted, main_head, prefix_holder, state, migration_details)

        # uv.lock freshness on the combined branch.
        lock = run(["uv", "lock", "--check"], cwd=str(Path(repo_root) / "backend"))
        if lock.returncode != 0:
            state["failure_description"] = "backend/uv.lock stale on the train branch (run: uv lock in backend/)"
            print("::error::backend/uv.lock is stale on the train branch.")
            return _finish(state, out_path, migration_details, accepted_order)

        sync = run(["uv", "sync", "--frozen"], cwd=str(Path(repo_root) / "backend"))
        if sync.returncode != 0:
            state["failure_description"] = "uv sync --frozen failed on the train branch"
            print("::error::uv sync --frozen failed on the train branch.")
            return _finish(state, out_path, migration_details, accepted_order)

        accepted_order, heads_failure = _resolve_multi_heads(repo_root, args.branch, accepted_order, state, migration_details)
        if heads_failure:
            state["failure_description"] = heads_failure
            return _finish(state, out_path, migration_details, accepted_order)

        state["merged_prs"] = accepted_order
        state["success"] = True
        return _finish(state, out_path, migration_details, accepted_order)

    except Exception as exc:
        state["failure_description"] = f"train build failed: {exc}"
        print(f"::error::train build failed: {exc}", file=sys.stderr)
        return _finish(state, out_path, migration_details, state.get("merged_prs", []))


def _find_migration_file_by_revision(repo_root: str, rev: str) -> str:
    """Locate a migration file by its `revision` assignment (the filename stem is
    usually, but not always, the revision id)."""
    listing = git("ls-tree", "-r", "--name-only", "HEAD", "--", f"{MIG_DIR}/", cwd=repo_root)
    pattern = re.compile(rf'^revision(\s*:[^=]*)?\s*=\s*["\']{re.escape(rev)}["\']')
    for path in listing.stdout.splitlines():
        if not path.endswith(".py"):
            continue
        content = git("show", f"HEAD:{path}", cwd=repo_root).stdout
        if pattern.search(content):
            return path
    return ""


def _finish(
    state: dict,
    out_path: Path,
    migration_details: list[str],
    merged: list[int],
) -> int:
    state["merged_prs"] = merged
    state["migration_collision_details"] = "\n".join(migration_details)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    ok = bool(state.get("success"))
    print(f"Train state written: success={ok}, merged={merged}, excluded={[e.get('number') for e in state.get('excluded_prs', [])]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
