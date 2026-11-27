"""Unit tests for .github/scripts/merge_train_build.py.

Focus: the path-confinement validator (the manifest/output paths the Sonar
pythonsecurity S8707 rule flagged), and the pre-merge migration logic that was
split out of `premerge_collision` / `main` (prefix collisions, the multi-head
chain checks, and the state writer) so the refactor's behaviour stays pinned. No
network, no git is needed: the tested helpers are pure or take their data as
arguments.
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


build = _load("merge_train_build.py")


def test_resolve_within_accepts_path_inside_base(tmp_path: Path) -> None:
    base = (tmp_path / "workspace").resolve()
    base.mkdir()
    accepted = base / "state.json"
    resolved = build.resolve_within(str(accepted), (base,))
    assert resolved == accepted
    assert resolved.is_relative_to(base)


def test_resolve_within_rejects_parent_escape(tmp_path: Path) -> None:
    base = (tmp_path / "workspace").resolve()
    base.mkdir()
    escape = base / ".." / "escape.json"
    with pytest.raises(SystemExit):
        build.resolve_within(str(escape), (base,))


def test_chain_collision_accepts_single_chain_rooted_on_main_head() -> None:
    added_rev = {"r2": "backend/x/db/migrations/versions/2_r2.py"}
    added_down = {"r2": "r1"}
    collides, detail = build._chain_collision(7, added_rev, added_down, ["r2"], "r1")
    assert collides is False
    assert not detail


def test_chain_collision_rejects_two_independent_roots() -> None:
    added_rev = {"r2": "2_r2.py", "r3": "3_r3.py"}
    added_down = {"r2": "r1", "r3": "r1"}
    collides, detail = build._chain_collision(8, added_rev, added_down, ["r2", "r3"], "r1")
    assert collides is True
    assert detail


def test_chain_collision_rejects_wrong_down_revision() -> None:
    added_rev = {"r2": "2_r2.py"}
    added_down = {"r2": "not-main-head"}
    collides, detail = build._chain_collision(9, added_rev, added_down, ["r2"], "r1")
    assert collides is True
    assert "a second Alembic head" in detail[0]


def test_chain_collision_rejects_a_down_revision_cycle() -> None:
    added_rev = {"r2": "2_r2.py", "r3": "3_r3.py"}
    added_down = {"r2": "r3", "r3": "r2"}
    collides, detail = build._chain_collision(10, added_rev, added_down, ["r2", "r3"], "r1")
    assert collides is True
    assert "cycle" in detail[0]


def test_chain_collision_no_added_migrations_is_clean() -> None:
    collides, detail = build._chain_collision(11, {}, {}, [], "r1")
    assert collides is False
    assert not detail


def test_prefix_collision_flags_a_second_add_of_the_same_prefix() -> None:
    diff = [
        ("A", f"{build.MIG_DIR}/0100_first.py", ""),
        ("A", f"{build.MIG_DIR}/0100_second.py", ""),
    ]
    collides, detail, claim, _release = build._prefix_collisions(3, diff, {})
    assert collides is True
    assert detail
    assert claim.get("0100") == "0100_first.py"


def test_prefix_collision_distinct_prefixes_are_clean() -> None:
    diff = [
        ("A", f"{build.MIG_DIR}/0100_first.py", ""),
        ("A", f"{build.MIG_DIR}/0101_second.py", ""),
    ]
    collides, detail, claim, _release = build._prefix_collisions(4, diff, {})
    assert collides is False
    assert not detail
    assert claim == {"0100": "0100_first.py", "0101": "0101_second.py"}


def test_prefix_collision_flags_add_against_a_held_prefix() -> None:
    diff = [("A", f"{build.MIG_DIR}/0100_new.py", "")]
    collides, detail, _claim, _release = build._prefix_collisions(5, diff, {"0100": "0100_old.py"})
    assert collides is True
    assert "0100_new.py" in detail[0]


def test_prefix_collision_flags_a_rename_onto_a_held_prefix() -> None:
    diff = [("R100", f"{build.MIG_DIR}/0100_old.py", f"{build.MIG_DIR}/0100_new.py")]
    collides, detail, claim, release = build._prefix_collisions(6, diff, {"0100": "0100_other.py"})
    assert collides is True
    assert "renames onto 0100_new.py" in detail[0]
    assert not claim
    assert release.get("0100") == "0100_old.py"


def test_prefix_collision_rename_to_a_free_prefix_claims_it() -> None:
    diff = [("R100", f"{build.MIG_DIR}/0100_old.py", f"{build.MIG_DIR}/0101_new.py")]
    collides, detail, claim, release = build._prefix_collisions(7, diff, {})
    assert collides is False
    assert not detail
    assert claim == {"0101": "0101_new.py"}
    assert release == {"0100": "0100_old.py"}


def test_prefix_collision_delete_frees_its_prefix_for_a_later_add() -> None:
    # A delete stages a release of 0100, so the subsequent add of the same prefix
    # is judged against the RELEASED state and does not collide.
    diff = [
        ("D", f"{build.MIG_DIR}/0100_old.py", ""),
        ("A", f"{build.MIG_DIR}/0100_new.py", ""),
    ]
    collides, detail, claim, _release = build._prefix_collisions(8, diff, {"0100": "0100_old.py"})
    assert collides is False
    assert not detail
    assert claim == {"0100": "0100_new.py"}


def test_finish_writes_state_and_returns_success(tmp_path: Path) -> None:
    out = tmp_path / "state.json"
    rc = build._finish({"success": True, "excluded_prs": []}, out, ["a detail"], [1, 2])
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["merged_prs"] == [1, 2]
    assert data["migration_collision_details"] == "a detail"


def test_finish_writes_state_and_returns_failure(tmp_path: Path) -> None:
    out = tmp_path / "state.json"
    rc = build._finish({"success": False, "excluded_prs": [{"number": 3}]}, out, [], [])
    assert rc == 1
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["excluded_prs"] == [{"number": 3}]


def _escape_bases(monkeypatch, tmp_path: Path) -> Path:
    """Point the process temp dir at a dir under tmp_path; return the repo dir."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    isolated_tmp = (tmp_path / "isolated_tmp").resolve()
    isolated_tmp.mkdir()
    monkeypatch.setattr(build.tempfile, "gettempdir", lambda: str(isolated_tmp))
    return repo


def test_main_rejects_a_manifest_outside_the_allowed_bases(monkeypatch, tmp_path: Path) -> None:
    repo = _escape_bases(monkeypatch, tmp_path)
    escape_manifest = tmp_path / "escape_manifest.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge_train_build.py",
            "--manifest",
            str(escape_manifest),
            "--out",
            str(repo / "state.json"),
            "--branch",
            "b",
            "--repo-root",
            str(repo),
        ],
    )
    with pytest.raises(SystemExit):
        build.main()


def test_main_rejects_an_out_path_outside_the_allowed_bases(monkeypatch, tmp_path: Path) -> None:
    repo = _escape_bases(monkeypatch, tmp_path)
    manifest = repo / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    escape_out = tmp_path / "escape_state.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "merge_train_build.py",
            "--manifest",
            str(manifest),
            "--out",
            str(escape_out),
            "--branch",
            "b",
            "--repo-root",
            str(repo),
        ],
    )
    with pytest.raises(SystemExit):
        build.main()
