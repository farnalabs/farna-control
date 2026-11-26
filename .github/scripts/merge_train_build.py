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
state file is written on BOTH paths (a try/finally) so the merge job can triage
a failed build.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

MIG_DIR = "backend/src/farna/db/migrations/versions"
MAX_DROP_ROUNDS = 3


def git(*args: str, cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check
    )


def run(argv: list[str], cwd: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def commit_squash(message: str, cwd: str) -> None:
    git(
        "-c",
        "user.name=modulo-merge-bot",
        "-c",
        "user.email=bot@modulo.run",
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
    pat = re.compile(
        r"^[ \t]*" + re.escape(field) + r"(\s*:[^=]*)?\s*=\s*(.*)$", re.MULTILINE
    )
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
    ls = git("ls-tree", "--name-only", "origin/main", "--", f"{MIG_DIR}/", cwd=repo_root)
    files = [f for f in ls.stdout.splitlines() if f.endswith(".py")]
    for path in files:
        base = os.path.basename(path)
        prefix = base.split("_", 1)[0]
        if prefix:
            holder[prefix] = base

    seen: dict[str, str] = {}
    has_child: dict[str, int] = {}
    problem = ""
    for path in files:
        content = git("show", f"origin/main:{path}", cwd=repo_root).stdout
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
    proc = git("diff", "--name-status", f"origin/main...pr-{num}", "--", MIG_DIR, cwd=repo_root)
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


def premerge_collision(
    repo_root: str,
    num: int,
    prefix_holder: dict[str, str],
    main_head: str | None,
) -> tuple[bool, list[str], dict[str, str], dict[str, str]]:
    """Per-PR pre-merge migration gate. Returns (collides, detail_lines,
    staged_claim, staged_release). The caller applies the staged maps to
    prefix_holder only when it accepts the PR."""
    detail: list[str] = []
    staged_claim: dict[str, str] = {}
    staged_release: dict[str, str] = {}
    collides = False

    def effective(p: str) -> str:
        if p in staged_claim:
            return staged_claim[p]
        if p in prefix_holder:
            if p in staged_release and staged_release[p] == prefix_holder[p]:
                return ""
            return prefix_holder[p]
        return ""

    for status, path, extra in pr_diff_status(repo_root, num):
        if status.startswith("R"):
            old_base = os.path.basename(path)
            new_base = os.path.basename(extra or "")
            if old_base:
                staged_release[old_base.split("_", 1)[0]] = old_base
            new_prefix = new_base.split("_", 1)[0] if new_base else ""
            if new_prefix:
                holder = effective(new_prefix)
                if holder and holder != new_base:
                    collides = True
                    detail.append(
                        f"PR #{num}: renames onto {new_base}, prefix {new_prefix} is already held by {holder}"
                    )
                else:
                    staged_claim[new_prefix] = new_base
        elif status == "D":
            base = os.path.basename(path)
            if base:
                staged_release[base.split("_", 1)[0]] = base
        else:
            base = os.path.basename(path)
            prefix = base.split("_", 1)[0] if base else ""
            if not prefix:
                continue
            holder = effective(prefix)
            if holder and holder != base:
                collides = True
                detail.append(
                    f"PR #{num}: adds {base}, numeric prefix {prefix} is already held by {holder}"
                )
            else:
                staged_claim[prefix] = base

    # Multi-head check on what THIS PR adds (only when main's head is known).
    if main_head:
        added_rev: dict[str, str] = {}
        added_down: dict[str, str] = {}
        order: list[str] = []
        for status, path, extra in pr_diff_status(repo_root, num):
            new_path = ""
            if status == "A":
                new_path = path
            elif status.startswith("R"):
                new_path = extra or ""
                if path.startswith(MIG_DIR + "/"):
                    if git("cat-file", "-e", f"origin/main:{path}", cwd=repo_root).returncode == 0:
                        continue
            else:
                continue
            if not new_path or not new_path.startswith(MIG_DIR + "/"):
                continue
            content = git("show", f"pr-{num}:{new_path}", cwd=repo_root).stdout
            rev = mig_first_literal(content, "revision")
            down = mig_first_literal(content, "down_revision")
            new_base = os.path.basename(new_path)
            if not rev:
                collides = True
                detail.append(
                    f"PR #{num}: adds {new_base} but its revision could not be parsed - cannot show it chains onto main's head '{main_head}'"
                )
                continue
            added_rev[rev] = new_path
            added_down[rev] = down
            order.append(rev)
        if order:
            roots = [rev for rev in order if not added_down[rev] or added_down[rev] not in added_rev]
            if len(roots) == 0:
                collides = True
                detail.append(
                    f"PR #{num}: added migrations ({', '.join(order)}) form a down_revision cycle; cannot reach main's head '{main_head}'"
                )
            elif len(roots) > 1:
                collides = True
                detail.append(
                    f"PR #{num}: adds {len(roots)} independent migrations ({', '.join(roots)}) that do not chain onto each other - multiple Alembic heads"
                )
            else:
                first = roots[0]
                if added_down[first] != main_head:
                    collides = True
                    detail.append(
                        f"PR #{num}: adds {os.path.basename(added_rev[first])} with down_revision '{added_down[first]}', but main's head is '{main_head}' - a second Alembic head"
                    )

    return collides, detail, staged_claim, staged_release


def apply_staged(
    prefix_holder: dict[str, str],
    staged_claim: dict[str, str],
    staged_release: dict[str, str],
) -> None:
    for prefix, base in staged_release.items():
        if prefix_holder.get(prefix) == base:
            prefix_holder.pop(prefix, None)
    for prefix, base in staged_claim.items():
        prefix_holder[prefix] = base


def alembic_heads(repo_root: str) -> tuple[list[str], str]:
    proc = run(
        ["uv", "run", "--no-build", "--no-sync", "python", "-m", "alembic", "heads"],
        cwd=os.path.join(repo_root, "backend"),
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
    git("checkout", "-B", branch, "origin/main", cwd=repo_root, check=True)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    repo_root = os.path.abspath(args.repo_root)
    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    admitted = manifest.get("admitted", [])

    state: dict = {
        "success": False,
        "branch_ref": args.branch,
        "base_ref": "origin/main",
        "merged_prs": [],
        "excluded_prs": [],
        "admitted_prs": [int(p["number"]) for p in admitted],
        "failure_description": "",
        "migration_collision_details": "",
    }
    migration_details: list[str] = []

    try:
        git("fetch", "origin", "main", cwd=repo_root)
        git("checkout", "-B", args.branch, "origin/main", cwd=repo_root, check=True)

        prefix_holder, main_head, problem = main_migration_graph(repo_root)
        if main_head:
            print(f"Migration multi-head gate active: main's head is '{main_head}'.")
        else:
            print(
                f"::warning::Migration multi-head gate SKIPPED - {problem}. "
                "A PR that forks the graph at a free prefix will only be caught by the post-merge heads gate."
            )

        accepted_order: list[int] = []
        for pr in admitted:
            num = int(pr["number"])
            git(
                "fetch",
                "origin",
                f"+refs/pull/{num}/head:refs/heads/pr-{num}",
                cwd=repo_root,
            )
            if git("rev-parse", "--verify", "-q", f"pr-{num}", cwd=repo_root).returncode != 0:
                state["excluded_prs"].append(
                    {"number": num, "reason": "PR head unavailable"}
                )
                continue

            collides, detail, claim, release = premerge_collision(
                repo_root, num, prefix_holder, main_head
            )
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
                state["excluded_prs"].append(
                    {"number": num, "reason": "merge conflict with main or an earlier PR"}
                )
                continue
            if git("diff", "--cached", "--quiet", cwd=repo_root).returncode == 0:
                # Nothing to merge (content already present) - not counted as merged.
                print(f"PR #{num}: no changes to merge (content already present) - skipped.")
                continue
            commit_squash(f"squash! Merge PR #{num}", repo_root)
            # Accepted: commit this PR's staged migration claims/releases so a
            # later PR in the train is judged against what the batch holds.
            apply_staged(prefix_holder, claim, release)
            accepted_order.append(num)
            print(f"PR #{num}: squash-merged into the train branch.")

        # uv.lock freshness on the combined branch.
        lock = run(["uv", "lock", "--check"], cwd=os.path.join(repo_root, "backend"))
        if lock.returncode != 0:
            state["failure_description"] = (
                "backend/uv.lock stale on the train branch (run: uv lock in backend/)"
            )
            print("::error::backend/uv.lock is stale on the train branch.")
            return _finish(state, args.out, migration_details, repo_root, accepted_order)

        sync = run(["uv", "sync", "--frozen"], cwd=os.path.join(repo_root, "backend"))
        if sync.returncode != 0:
            state["failure_description"] = "uv sync --frozen failed on the train branch"
            print("::error::uv sync --frozen failed on the train branch.")
            return _finish(state, args.out, migration_details, repo_root, accepted_order)

        # Migration-heads hard gate with per-PR attribution.
        heads, heads_text = alembic_heads(repo_root)
        print(f"Alembic heads on the train branch: {len(heads)} ({', '.join(heads)})")
        rounds = 0
        while len(heads) != 1:
            if rounds >= MAX_DROP_ROUNDS:
                state["failure_description"] = (
                    "Alembic multi-head graph could not be resolved after "
                    f"{MAX_DROP_ROUNDS} drop-and-rebuild rounds (heads: {', '.join(heads)})"
                )
                print("::error::Could not resolve the Alembic multi-head graph.")
                return _finish(state, args.out, migration_details, repo_root, accepted_order)
            rounds += 1

            offenders: set[int] = set()
            unattributed: list[str] = []
            for rev in heads:
                head_file = f"{MIG_DIR}/{rev}.py"
                if git("cat-file", "-e", f"{args.branch}:{head_file}", cwd=repo_root).returncode != 0:
                    found = _find_migration_file_by_revision(repo_root, rev)
                    if found:
                        head_file = found
                if git("cat-file", "-e", f"origin/main:{head_file}", cwd=repo_root).returncode == 0:
                    continue  # already on main - not attributable to a train PR
                found_prs = []
                for cand in accepted_order:
                    if git("rev-parse", "--verify", "-q", f"pr-{cand}", cwd=repo_root).returncode != 0:
                        continue
                    diff = git(
                        "diff", "--name-only", f"origin/main...pr-{cand}", "--", head_file,
                        cwd=repo_root,
                    ).stdout
                    if head_file in diff.splitlines():
                        found_prs.append(cand)
                if not found_prs:
                    unattributed.append(f"{rev}({head_file})")
                    continue
                for pr_num in found_prs:
                    offenders.add(pr_num)
                    migration_details.append(
                        f"PR #{pr_num}: adds {head_file} (revision '{rev}'), which branches the migration graph into multiple Alembic heads on the train branch."
                    )

            if not offenders:
                state["failure_description"] = (
                    "Alembic reports multiple heads but none is attributable to a train PR "
                    f"(unattributed: {', '.join(unattributed) or 'none'})"
                )
                print("::error::Multi-head graph not attributable to a train PR.")
                return _finish(state, args.out, migration_details, repo_root, accepted_order)

            # Minimal drop: if several offenders, try each alone first.
            chosen: set[int] = offenders
            if len(offenders) > 1:
                for candidate in sorted(offenders):
                    remaining, _ = rebuild_without(
                        repo_root, args.branch, accepted_order, {candidate}
                    )
                    cand_heads, _ = alembic_heads(repo_root)
                    if len(cand_heads) == 1:
                        chosen = {candidate}
                        print(f"Minimal migration-head drop: PR #{candidate} alone restores one head.")
                        break
            print(f"Dropping migration-head offender(s): {sorted(chosen)}")
            for pr_num in sorted(chosen):
                state["excluded_prs"].append(
                    {
                        "number": pr_num,
                        "reason": "adds a migration that branches the graph into multiple Alembic heads",
                    }
                )
            accepted_order, new_conflicts = rebuild_without(
                repo_root, args.branch, accepted_order, chosen
            )
            state["excluded_prs"].extend(new_conflicts)
            heads, heads_text = alembic_heads(repo_root)
            print(f"After rebuild: {len(heads)} Alembic head(s) ({', '.join(heads)})")

        state["merged_prs"] = accepted_order
        state["success"] = True
        return _finish(state, args.out, migration_details, repo_root, accepted_order)

    except Exception as exc:  # noqa: BLE001
        state["failure_description"] = f"train build failed: {exc}"
        print(f"::error::train build failed: {exc}", file=sys.stderr)
        return _finish(state, args.out, migration_details, repo_root, state.get("merged_prs", []))
    finally:
        pass


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
    out_path: str,
    migration_details: list[str],
    repo_root: str,
    merged: list[int],
) -> int:
    state["merged_prs"] = merged
    state["migration_collision_details"] = "\n".join(migration_details)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, default=str)
    ok = bool(state.get("success"))
    print(
        f"Train state written: success={ok}, merged={merged}, "
        f"excluded={[e.get('number') for e in state.get('excluded_prs', [])]}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
