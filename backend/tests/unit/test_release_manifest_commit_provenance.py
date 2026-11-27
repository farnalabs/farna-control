"""FAR-2795: the signed release manifest records the BUILT commit, not the workflow ref.

A live ``bundle-v0.3.330`` manifest carried ``generated_at`` = the commit ``main``
pointed at when the workflow was dispatched, while the RC tag and the checked-out
build source were a different commit. ``generated_at`` is the de facto
source-commit field (schema v1), so every manifest writer must record the exact
commit the job checked out - captured as ``SOURCE_COMMIT="$(git rev-parse HEAD)"``
in the repo root before any ``cd`` - and never the dispatch ref (``GITHUB_SHA``).

These tests parse the workflow YAML and pin those two properties on each of the
four writers, and pin the field's documented meaning in ``manifest.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

BUNDLE_RELEASE = WORKFLOWS / "bundle-release.yml"
RELEASE_UNIFIED = WORKFLOWS / "release-unified.yml"
MACOS_STAPLE_RETRY = WORKFLOWS / "macos-staple-retry.yml"
MANIFEST_MODULE = REPO_ROOT / "backend" / "src" / "farna" / "launcher" / "manifest.py"

# Source-commit capture expression every writer must emit in the repo root.
CAPTURE = 'SOURCE_COMMIT="$(git rev-parse HEAD)"'
PASSED_INTO_GENERATED_AT = 'generated_at=os.environ["SOURCE_COMMIT"]'
PRE_FIX_DISPATCH_REF = 'generated_at=os.environ.get("GITHUB_SHA"'

# (workflow path, job key, step name, the first "cd <dir>" the run performs).
WRITERS: tuple[tuple[Path, str, str, str], ...] = (
    (BUNDLE_RELEASE, "bundle", "Generate and sign the SIGNED release manifest", "cd backend"),
    (RELEASE_UNIFIED, "final-release-manifest", "Build final signed manifest after every platform artifact", "cd release-artefacts"),
    (RELEASE_UNIFIED, "promote-compose", "Generate SHA256SUMS and sign the final GA manifest", "cd release-artefacts"),
    (MACOS_STAPLE_RETRY, "publish-stapled", "Regenerate SHA256SUMS and the signed release manifest", "cd release-assets"),
)


def _load_workflow(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step_run(workflow: dict[str, Any], job: str, step_name: str) -> str:
    """Return the ``run:`` script of exactly one named step (fail if not exactly one)."""
    steps = workflow["jobs"][job]["steps"]
    matches = [step for step in steps if step.get("name") == step_name]
    assert len(matches) == 1, f"expected exactly one {step_name!r} step in job {job!r}, found {len(matches)}"
    run = matches[0].get("run")
    assert isinstance(run, str), f"step {step_name!r} has no run script"
    return run


def _violations(run: str, first_cd: str) -> list[str]:
    """List the source-commit-provenance contract violations in one writer's run text."""
    problems: list[str] = []
    if CAPTURE not in run:
        problems.append("does not capture SOURCE_COMMIT from the checked-out commit")
    elif first_cd in run and run.index(CAPTURE) > run.index(first_cd):
        problems.append("captures SOURCE_COMMIT after cd, not in the repo root")
    if PASSED_INTO_GENERATED_AT not in run:
        problems.append("does not pass SOURCE_COMMIT into generated_at")
    for line in run.splitlines():
        if "generated_at" in line and "GITHUB_SHA" in line:
            problems.append("passes GITHUB_SHA (the dispatch ref) into generated_at")
    return problems


@pytest.mark.parametrize(("workflow_path", "job", "step_name", "first_cd"), WRITERS, ids=[f"{p.name}:{j}" for p, j, _s, _c in WRITERS])
def test_manifest_writer_records_the_checked_out_commit(workflow_path: Path, job: str, step_name: str, first_cd: str) -> None:
    run = _step_run(_load_workflow(workflow_path), job, step_name)
    problems = _violations(run, first_cd)
    assert not problems, f"{workflow_path.name}:{job}:{step_name}: " + "; ".join(problems)


@pytest.mark.parametrize("workflow_path", [BUNDLE_RELEASE, RELEASE_UNIFIED, MACOS_STAPLE_RETRY], ids=["bundle-release", "release-unified", "macos-staple-retry"])
def test_no_manifest_writer_records_the_dispatch_ref(workflow_path: Path) -> None:
    text = workflow_path.read_text(encoding="utf-8")
    assert PRE_FIX_DISPATCH_REF not in text


def test_every_writer_inventory_is_covered() -> None:
    # Four writers exist today; the parametrised set above must cover each of
    # them, so a new writer cannot slip in unpinned. The manifest passes
    # generated_at through os.environ["SOURCE_COMMIT"] exactly once per writer.
    sites = 0
    for workflow_path in (BUNDLE_RELEASE, RELEASE_UNIFIED, MACOS_STAPLE_RETRY):
        sites += workflow_path.read_text(encoding="utf-8").count(PASSED_INTO_GENERATED_AT)
    assert sites == len(WRITERS)


def test_manifest_module_documents_the_field_meaning() -> None:
    text = MANIFEST_MODULE.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "generated_at" in text
    assert "source commit sha" in lowered
    assert "not a timestamp" in lowered
