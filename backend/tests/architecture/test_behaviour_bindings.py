"""Architecture test: product-map behaviours are bound to real, non-vacuous tests.

``docs/product-map/`` is the specification for what Farna Control ships: 63
behaviour-tracker entries whose ``## Behaviors`` checklists are the claims the
product makes. Until this gate existed a claim was exactly that - a human tick -
and nothing failed when the test behind it was deleted, when it was vacuous, or
when it tested something else. The frontmatter ``status:`` was typed by hand too,
so ``covered`` meant "someone said so".

The binding format (documented in ``docs/product-map/README.md``) puts a stable
id and the proving test references on every behaviour line::

    - [x] ``GET /healthz`` returns ``{"status": "ok"}``
          <!-- behaviour: health-checks-01 | tests: backend/tests/unit/api/test_health.py::test_liveness_ok -->

This suite enforces the invariants that make the format worth having. It reads
the same implementation the regenerator uses
(``scripts/run_behaviour_bindings_baseline.py``), so the gate and the backfill
tool can never drift into enforcing different rules:

- every behaviour carries a stable, unique id, and an existing id cannot
  silently attach to different behaviour text (the baseline records an id ->
  text fingerprint for every behaviour);
- every reference a marker names RESOLVES against the collected test tree (a
  pytest node id, a test module, a registered BDD feature file, or a frontend
  ``*.spec.ts`` test title);
- no reference names a test the shared test-suite-quality lenses flag as vacuous
  (no-op ``@then``, self-asserting BDD step, duplicate test body);
- every ticked behaviour is bound to at least one clean reference, or is frozen
  in ``backend/tests/architecture/behaviour_bindings_baseline.json``;
- the baseline is shrink-only: an entry that has since become bound (or whose
  behaviour disappeared or was unticked) fails, so it can never be widened;
- the frontmatter ``status:`` equals the status derived from the bindings, so a
  hand-typed ``covered`` cannot outlive the test that backed it;
- the weak (module-only) binding ratchet can only shrink, so a new behaviour
  cannot be "proved" by pointing at a whole test module;
- the baseline has a review-by date that must not be in the past;
- the baseline and ``docs/product-map/unbound-behaviours.md`` are exactly what
  the regenerator produces, so neither can be hand-edited into a weaker claim.
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

_BINDINGS_MODULE_NAME = "far2701_behaviour_bindings"

_loader = SourceFileLoader(_BINDINGS_MODULE_NAME, str(REPO_ROOT / "scripts" / "run_behaviour_bindings_baseline.py"))
_bindings = module_from_spec(spec_from_loader(_BINDINGS_MODULE_NAME, _loader))
assert _bindings is not None, "could not create the behaviour-bindings module spec"
# Registered before execution: a ``@dataclass`` with string annotations resolves
# them through ``sys.modules[cls.__module__]``, which a bare ``exec_module``
# leaves unset.
sys.modules[_BINDINGS_MODULE_NAME] = _bindings
_loader.exec_module(_bindings)

#: Markers that reference a test which is frozen in one of the vacuity baselines.
_BASELINE_DIR = Path(__file__).resolve().parent

#: Resolved once: the removal test drives a real ``git init`` / ``git commit``.
GIT = shutil.which("git")


@functools.cache
def _audit() -> object:
    """Resolve every behaviour once per session (the parse is the expensive part)."""
    return _bindings.audit(REPO_ROOT)


@functools.cache
def _baseline() -> dict[str, dict[str, str]]:
    return _bindings.load_baseline(REPO_ROOT)


@functools.cache
def _baseline_payload() -> dict:
    return _bindings.load_baseline_payload(REPO_ROOT)


def _entries() -> list[object]:
    return _audit().entries


def _all_behaviours() -> list[tuple[object, object]]:
    """Every ``(entry, behaviour)`` pair, in file order."""
    return [(entry, behaviour) for entry in _entries() for behaviour in entry.behaviours]


def _format_unbound(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"  {entry}: {behaviour_id} - {reason}" for entry, behaviour_id, reason in rows)


def test_every_behaviour_carries_a_stable_unique_id():
    """Every behaviour bullet names an id that is present and globally unique.

    The id is what the baseline and ``Proves:`` links refer to (and what a
    behaviour keeps when it moves within an entry), so presence and uniqueness
    are the two hard requirements. Position is *not* asserted: an id travels with
    its behaviour, so a reordering must not force a renumber. That a reused id
    cannot silently point at different prose is pinned by
    ``test_behaviour_id_does_not_rebind_different_text``.
    """
    missing: list[str] = []
    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for entry in _entries():
        for behaviour in entry.behaviours:
            if not behaviour.id:
                missing.append(f"  {entry.rel}:{behaviour.start_line}")
                continue
            if behaviour.id in seen:
                duplicated.append(f"  {behaviour.id}: {seen[behaviour.id]} and {entry.rel}")
            seen[behaviour.id] = entry.rel

    assert not missing, "behaviour bullets with no `<!-- behaviour: <id> | tests: ... -->` marker. Add one with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-markers`:\n" + "\n".join(missing)
    assert not duplicated, "duplicate behaviour ids across the product map:\n" + "\n".join(duplicated)


def test_behaviour_id_does_not_rebind_different_text():
    """A behaviour id may not silently attach to different prose.

    The baseline records ``id -> fingerprint(normalised behaviour text)`` for
    every behaviour. Reusing an existing id for a different claim (a renumber
    after reordering, or a rewritten behaviour that kept its marker) changes the
    fingerprint and fails until the baseline is regenerated deliberately, which
    is the only path that can rebind a frozen id and is visible in review.
    """
    fingerprints = _baseline_payload().get("id_fingerprints")
    assert isinstance(fingerprints, dict), "behaviour_bindings_baseline.json must declare an 'id_fingerprints' mapping (regenerate with --write-baseline)"
    drifted: list[str] = []
    for entry, behaviour in _all_behaviours():
        recorded = fingerprints.get(behaviour.id)
        actual = _bindings.behaviour_text_fingerprint(behaviour.text)
        if recorded is None:
            drifted.append(f"  {entry.rel}: {behaviour.id} has no recorded text fingerprint")
        elif recorded != actual:
            drifted.append(f"  {entry.rel}: {behaviour.id} text changed since the baseline recorded its id (recorded {recorded}, now {actual})")
    assert not drifted, (
        "behaviour ids are attached to text the baseline did not record - a reuse/renumber would rebind the shrink-only baseline. Regenerate with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report` and review the diff:\n"
        + "\n".join(drifted)
    )


def test_every_referenced_binding_resolves():
    """No marker may name a test artefact that does not exist.

    A dangling node id is worse than no binding: it reads as proof while proving
    nothing - the test was renamed, moved or deleted and the tick survived it.
    """
    dangling: list[str] = []
    for entry, behaviour in _all_behaviours():
        for resolution in _audit().resolutions.get(behaviour.id, []):
            if resolution.ok:
                continue
            dangling.append(f"  {entry.rel}: {behaviour.id} -> {resolution.ref} ({resolution.detail})")
    assert not dangling, "product-map behaviours bind to test artefacts that do not resolve:\n" + "\n".join(dangling)


def test_no_behaviour_binds_a_vacuous_test():
    """A binding may not rest on a test the shared lenses call vacuous.

    ``noop_bdd_then`` (a pass-only ``@then``), ``self_asserting_bdd`` (a step that
    fabricates the response it asserts on) and ``duplicate_test_body`` (a
    copy-paste twin) all report green without exercising the behaviour. Binding a
    behaviour to one of them is a false green with a receipt.
    """
    vacuous: list[str] = []
    for entry, behaviour in _all_behaviours():
        for reason in _audit().vacuous_reasons.get(behaviour.id, []):
            vacuous.append(f"  {entry.rel}: {behaviour.id} - {reason}")

    assert not vacuous, "product-map behaviours are bound to tests the test-suite-quality lenses flag as vacuous. Bind the behaviour to a test that exercises it (or drop the reference and let the behaviour be recorded as unbound):\n" + "\n".join(vacuous)


def test_every_ticked_clean_behaviour_is_not_silently_unbound():
    """A ticked behaviour must be bound, or frozen in the shrink-only baseline.

    This is the gap the product map could previously never report: a ticked
    behaviour whose text names no test at all. Each one is either bound now or
    listed in ``behaviour_bindings_baseline.json`` with a reason.
    """
    baseline = _baseline()
    new_unbound = [(info["entry"], behaviour_id, info["reason"]) for behaviour_id, info in _audit().unbound.items() if behaviour_id not in baseline]
    assert not new_unbound, (
        "ticked product-map behaviours with no resolving, non-vacuous test binding that are NOT in "
        "backend/tests/architecture/behaviour_bindings_baseline.json. Bind each to the test that proves it "
        "(the marker format is documented in docs/product-map/README.md):\n" + _format_unbound(sorted(new_unbound))
    )


def test_baseline_can_only_shrink():
    """A frozen entry that is now bound (or gone, or unticked) must leave the baseline.

    Without this the baseline could be widened by anyone whose behaviour fails,
    and a stale entry would keep excusing a behaviour that is already proven.
    """
    baseline = _baseline()
    unbound = set(_audit().unbound)
    by_id = {behaviour.id: (entry, behaviour) for entry, behaviour in _all_behaviours()}

    now_bound: list[str] = []
    missing: list[str] = []
    unticked: list[str] = []
    for behaviour_id, info in baseline.items():
        if behaviour_id not in by_id:
            missing.append(f"  {behaviour_id} ({info.get('entry', '?')}) - no such behaviour")
            continue
        entry, behaviour = by_id[behaviour_id]
        if not behaviour.ticked:
            unticked.append(f"  {entry.rel}: {behaviour_id} is unticked, so it needs no binding")
            continue
        if behaviour_id not in unbound:
            now_bound.append(f"  {entry.rel}: {behaviour_id} - it now has a clean binding")

    assert not missing, "baseline entries for behaviours that no longer exist:\n" + "\n".join(missing)
    assert not unticked, "baseline entries for behaviours that are no longer ticked:\n" + "\n".join(unticked)
    assert not now_bound, "behaviour_bindings_baseline.json entries that are no longer unbound - the baseline is shrink-only. Regenerate it with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report`:\n" + "\n".join(now_bound)


def test_frontmatter_status_matches_derivation():
    """Every entry's frontmatter ``status:`` must equal the status its bindings derive.

    The frontmatter value used to be the only statement of coverage, typed by
    hand. It is now a measurement: ``covered`` only when every behaviour is
    ticked, resolving, non-vacuous, and not frozen in the baseline; ``partial``
    when at least one behaviour is ticked but not all are proven; ``gap`` when
    nothing is ticked. The regenerator rewrites it (``--write-status``), and this
    test fails if a hand edit drifts from what the bindings say - including a
    ``covered`` that survives a behaviour the baseline still excuses.
    """
    drifted: list[str] = []
    for entry in _entries():
        derived = _audit().derived.get(entry.rel, "gap")
        if entry.status != derived:
            drifted.append(f"  {entry.rel}: frontmatter says {entry.status!r}, the bindings derive {derived!r}")
    assert not drifted, "product-map frontmatter status disagrees with the status derived from the behaviour bindings. Regenerate it with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-status`:\n" + "\n".join(drifted)


def test_weak_bindings_ratchet_only_shrinks():
    """A module-only binding is a loan: the frozen list may not grow.

    ``weak_bindings`` in the baseline lists every ticked behaviour counted as
    bound by a whole test module rather than a named test. A NEW weak binding
    fails (name the test instead), and a listed one that is no longer weak (it
    gained a node binding, or stopped being bound) fails as a stale entry, so the
    list can only shrink.
    """
    recorded = set(_baseline_payload().get("weak_bindings") or [])
    current = set(_audit().weak_bindings)
    added = sorted(current - recorded)
    stale = sorted(recorded - current)
    assert not added, "weak (module-only) bindings were added. Bind a named test instead of the whole module:\n" + "\n".join(f"  {behaviour_id}" for behaviour_id in added)
    assert not stale, (
        "weak_bindings entries that are no longer weak (they gained a node binding, stopped being bound, or vanished) - the ratchet is shrink-only. Regenerate with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report`:\n"
        + "\n".join(f"  {behaviour_id}" for behaviour_id in stale)
    )


def test_baseline_review_date_is_current():
    """The baseline names its owner and a review date that has not passed.

    A frozen list with no owner and no expiry is a permanent exemption. The
    baseline carries ``ticket`` and ``review_by``; once the date passes the gate
    fails until a human re-reviews the debt and moves it forward.
    """
    payload = _baseline_payload()
    ticket = payload.get("ticket")
    assert isinstance(ticket, str), "behaviour_bindings_baseline.json must declare the owning 'ticket'"
    assert ticket.strip(), "behaviour_bindings_baseline.json must declare the owning 'ticket'"
    review_by = payload.get("review_by")
    assert isinstance(review_by, str), "behaviour_bindings_baseline.json must declare an ISO 'review_by' date"
    assert review_by.strip(), "behaviour_bindings_baseline.json must declare an ISO 'review_by' date"
    due = _bindings.review_due_error(review_by)
    assert due is None, due


def test_baseline_file_is_the_canonical_rendering():
    """The baseline on disk is exactly what the regenerator emits.

    Governance metadata (``ticket``/``review_by``) is rendered from the payload
    itself, matching ``write_outputs``: a reviewer moves ``review_by`` forward by
    editing the baseline, not the script constant, and the file must stay
    canonical afterwards.
    """
    current = (REPO_ROOT / _bindings.BASELINE_RELPATH).read_text(encoding="utf-8")
    payload = _baseline_payload()
    expected = _bindings.render_baseline(
        _baseline(),
        fingerprints=_bindings.id_fingerprints(_entries()),
        weak_bindings=sorted(_audit().weak_bindings),
        approved_removals=payload.get(_bindings.BASELINE_APPROVED_REMOVALS_KEY) or [],
        ticket=payload.get("ticket") or _bindings.BASELINE_TICKET,
        review_by=payload.get("review_by") or _bindings.BASELINE_REVIEW_BY,
    )
    assert current == expected, "backend/tests/architecture/behaviour_bindings_baseline.json is not in canonical form - regenerate it with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report`"


def test_unbound_report_is_generated_from_the_baseline():
    """``docs/product-map/unbound-behaviours.md`` is the generated, current view."""
    current = (REPO_ROOT / _bindings.REPORT_RELPATH).read_text(encoding="utf-8")
    expected = _bindings.render_report(_audit())
    assert current == expected, "docs/product-map/unbound-behaviours.md does not match the bindings - regenerate it with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report`"


def test_baseline_ratchet_direction_is_shrink_only():
    """The base-revision comparison treats an ADDED entry as a loosening.

    ``scripts/run_behaviour_bindings_baseline.py --check-base`` runs in CI (the
    "Behaviour-bindings baseline may only shrink" step): this pins its direction
    without needing git, so an inverted comparison (removals failing, additions
    passing) cannot ship.
    """
    added = _bindings.baseline_loosenings({"plugins-01", "plugins-02"}, {"plugins-01", "plugins-02", "plugins-03"})
    removed = _bindings.baseline_loosenings({"plugins-01", "plugins-02"}, {"plugins-01"})
    unchanged = _bindings.baseline_loosenings({"plugins-01"}, {"plugins-01"})

    assert added == ["plugins-03"], f"an added baseline entry must be a loosening, got {added!r}"
    assert removed == [], f"a removed baseline entry is a tightening and must not fail, got {removed!r}"
    assert unchanged == [], f"an unchanged baseline must not fail, got {unchanged!r}"


def test_vacuity_lenses_are_wired_to_the_frozen_baselines():
    """The binding gate really sees what the vacuity baselines were built from.

    Proves the reuse: every module named in ``noop_bdd_then_baseline.txt`` must
    still be reported as carrying a pass-only ``@then`` step by the shared lens.
    A silent break here (an import scoped away, a renamed helper) would turn the
    vacuity rejection into a no-op while every other test stayed green.
    """
    noop_baseline = _BASELINE_DIR / "noop_bdd_then_baseline.txt"
    entries = [line.strip() for line in noop_baseline.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]
    if not entries:
        pytest.skip("noop_bdd_then_baseline.txt is empty, so there is nothing to cross-check")
    modules = sorted({f"{_bindings.TESTS_DIR}/{line.split(':', 1)[0]}" for line in entries})
    flags = _bindings.vacuity_flags(REPO_ROOT, modules)
    unwired = sorted(rel for rel in modules if not flags.noop_then.get(rel))
    assert not unwired, "the no-op @then lens reports nothing for modules the frozen baseline still lists (the vacuity rejection is not wired):\n" + "\n".join(f"  {rel}" for rel in unwired)


def test_frontend_spec_bindings_resolve():
    """A ``frontend/**/*.spec.ts::<title>`` binding resolves by file + test title.

    Without this ref kind a frontend behaviour could not be bound at all, so the
    whole frontend half of the product map was unbound by construction. The
    resolver indexes the two frontend test roots, matches the declared test title,
    and rejects a title that does not exist.
    """
    spec_rel = "frontend/src/__tests__/FarnaLabsProbe.spec.ts"
    index = _bindings.TestIndex.build(REPO_ROOT)
    index.specs[spec_rel] = spec_rel
    index._titles[spec_rel] = ("renders the probe",)

    ok = _bindings.resolve_ref(f"{spec_rel}::renders the probe", index, REPO_ROOT, set())
    missing = _bindings.resolve_ref(f"{spec_rel}::something else", index, REPO_ROOT, set())
    unnamed = _bindings.resolve_ref(f"{spec_rel}", index, REPO_ROOT, set())
    traversal = _bindings.resolve_ref("frontend/src/../../backend/src/farna/main.spec.ts::x", index, REPO_ROOT, set())

    assert ok.ok, f"a real spec test title must resolve, got {ok.detail}"
    assert not missing.ok, "an absent spec test title must not resolve"
    assert not unnamed.ok, "a spec reference must name a test title"
    assert not traversal.ok, "a '..' traversal must not resolve to a spec outside the test roots"


def test_frontend_spec_bindings_ignore_skipped_and_describe_titles(tmp_path: Path):
    """A skipped/fixme/todo test or a suite ``describe`` title is not proof.

    ``_TS_TEST_CALL_RE`` matches every test call and the ``describe`` wrapper
    alike, so without filtering a behaviour could be "covered" by a test that
    never runs (`it.skip`), a parked test (`it.fixme`/`test.todo`), a test inside
    a ``describe.skip`` block, or a Playwright ``test.describe`` suite title.
    """
    spec_rel = "frontend/src/__tests__/Probe.spec.ts"
    path = tmp_path / spec_rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "import { describe, it, test } from 'vitest'\n\ndescribe('Group', () => {\n  it('runs', () => {})\n  it.skip('skipped one', () => {})\n  it.fixme('fixme one', () => {})\n  test.todo('todo one')\n})\n\ndescribe.skip('Disabled', () => {\n  it('inside disabled describe', () => {})\n})\n",
        encoding="utf-8",
    )
    index = _bindings.TestIndex(root=tmp_path)
    index.specs[spec_rel] = spec_rel

    titles = index.spec_titles(spec_rel)
    assert titles == ("runs",), f"only the runnable test title may be indexed, got {titles!r}"

    ok = _bindings.resolve_ref(f"{spec_rel}::runs", index, tmp_path, set())
    assert ok.ok, f"a runnable spec test title must resolve, got {ok.detail}"
    for rejected in ("Group", "skipped one", "fixme one", "todo one", "inside disabled describe"):
        resolution = _bindings.resolve_ref(f"{spec_rel}::{rejected}", index, tmp_path, set())
        assert not resolution.ok, f"a skipped/describe title ({rejected!r}) must not resolve as proof"


def test_behaviour_ok_requires_at_least_one_resolving_ref():
    """A non-empty ref list is not enough: one reference must actually resolve."""
    behaviour = _bindings.Behaviour(
        id="probe-01",
        ticked=True,
        text="- [x] probe",
        refs=("backend/tests/does_not_exist.py",),
        start_line=1,
        end_line=1,
    )
    dead = {"probe-01": [_bindings.Resolution(ref="backend/tests/does_not_exist.py", ok=False, kind="module", detail="no such module")]}
    live = {"probe-01": [_bindings.Resolution(ref="backend/tests/does_not_exist.py", ok=True, kind="module", detail="ok")]}

    assert not _bindings._behaviour_ok(behaviour, dead, {}, frozenset()), "a behaviour whose only reference fails to resolve must not count as covered"
    assert _bindings._behaviour_ok(behaviour, live, {}, frozenset()), "a behaviour with a resolving reference must count as covered"
    assert not _bindings._behaviour_ok(behaviour, live, {}, frozenset({"probe-01"})), "a baselined-unbound behaviour must count as unbound for status purposes"


def test_shared_git_show_distinguishes_absent_path_from_git_failure(monkeypatch):
    """The shared base reader returns ``None`` only for a genuinely absent path.

    ``--check-base`` now reuses ``run_check_ratchet_baseline.read_baseline_at_rev``
    instead of a second bespoke git helper. A transient git failure must still
    fail closed: treating it as "the path did not exist at the base revision"
    would silently disable the shrink-only ratchet for that run.
    """
    import subprocess
    from types import SimpleNamespace

    ratchet = _bindings._load_shared_script("run_check_ratchet_baseline", "run_check_ratchet_baseline.py")

    def fake_run(args, **_kwargs):
        if args[1] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        if args[1] == "cat-file":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        raise AssertionError(f"git show must not be attempted for an absent path: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ratchet.read_baseline_at_rev(REPO_ROOT, "HEAD", "no/such/path.json") is None

    def failing_show(args, **_kwargs):
        if args[1] == "rev-parse":
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        if args[1] == "cat-file":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=128, stdout="", stderr="fatal: unable to read tree")

    monkeypatch.setattr(subprocess, "run", failing_show)
    with pytest.raises(ratchet.RatchetError, match="could not read"):
        ratchet.read_baseline_at_rev(REPO_ROOT, "HEAD", "backend/tests/architecture/behaviour_bindings_baseline.json")


def _probe_product_map(root: Path) -> Path:
    """Build a throwaway repo holding one product-map entry and its test module."""
    # Assembled rather than written whole: a bare feat-* literal in this source
    # file is scanned by the feature-graph test and would have to resolve
    # against the REAL manifest, which a throwaway id does not.
    feature_id = "feat-" + "probe"
    entry = root / _bindings.PRODUCT_MAP_DIR / "probe.md"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        f"---\nid: {feature_id}\nstatus: gap\n---\n\n# Probe\n\n## Behaviors\n\n- [x] the probe test runs (backend/tests/unit/test_probe.py::test_probe)\n- [x] a ticked behaviour that cites no test at all\n\n",
        encoding="utf-8",
    )
    test_module = root / "backend" / "tests" / "unit" / "test_probe.py"
    test_module.parent.mkdir(parents=True, exist_ok=True)
    test_module.write_text("def test_probe():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    # ``load_quality_lenses`` imports this module from the root it is handed, so
    # the temp tree needs its own copy for a cold run.
    quality = root / "backend" / "tests" / "architecture" / "test_test_suite_quality.py"
    quality.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_BASELINE_DIR / "test_test_suite_quality.py", quality)
    # ``--all`` audits the tree, which resolves registered BDD features through
    # this module; copy it so a cold run needs nothing from the real repo.
    gaps = root / "backend" / "tests" / "architecture" / "test_product_map_feature_gaps.py"
    shutil.copyfile(_BASELINE_DIR / "test_product_map_feature_gaps.py", gaps)
    baseline = root / _bindings.BASELINE_RELPATH
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(_bindings.render_baseline({}), encoding="utf-8")
    return entry


def test_regeneration_runs_against_a_throwaway_product_map(tmp_path: Path):
    """``--all`` regenerates a temp product map without crashing.

    ``assign_markers`` used to call ``vacuity_flags`` with an argument the
    function no longer took, so ``--write-markers`` and ``--all`` died with a
    ``TypeError`` before writing anything: the documented way to add a marker
    (the fix every other failing message points at) was broken. This runs the
    real CLI in a subprocess against a temp tree, so the vacuity lenses, the
    marker writer, the baseline and the report generator all execute for real -
    and the run cannot poison this process's module caches.

    The tree is seeded with an EMPTY baseline on purpose: ``--all`` reads the
    baseline before rewriting it.
    """
    entry = _probe_product_map(tmp_path)

    completed = subprocess.run(  # noqa: S603 - fixed argv; the repo's own script, no shell
        [sys.executable, str(REPO_ROOT / "scripts" / "run_behaviour_bindings_baseline.py"), "--root", str(tmp_path), "--all"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert completed.returncode == 0, f"regeneration exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    text = entry.read_text(encoding="utf-8")
    assert "<!-- behaviour: probe-01 | tests: backend/tests/unit/test_probe.py::test_probe -->" in text, text
    assert "<!-- behaviour: probe-02 | tests: none -->" in text, text
    assert "status: partial" in text, text
    baseline = json.loads((tmp_path / _bindings.BASELINE_RELPATH).read_text(encoding="utf-8"))
    assert "probe-02" in baseline["entries"], baseline
    report = (tmp_path / _bindings.REPORT_RELPATH).read_text(encoding="utf-8")
    assert "`probe-02`" in report, report


def test_write_markers_allocates_a_unique_id_for_a_mid_entry_insert(tmp_path: Path):
    """A bullet inserted mid-entry must not duplicate the id below it.

    ``assign_markers`` numbered an unmarked bullet by its POSITION
    (``<stem>-<ordinal>``). Inserting a bullet in the middle therefore handed the
    new bullet the ordinal of the bullet it pushed down, which still carried that
    id in its own marker: ``--write-markers`` emitted two ``probe-02`` markers
    (``probe-01, probe-02, probe-02, probe-03``) and could never repair the map
    again. The regenerator now allocates the first unused suffix, so the inserted
    bullet takes ``probe-04`` and every id stays unique.

    The real CLI runs in a subprocess (as the regeneration test does) so the
    vacuity-lens module imported for the temp tree cannot poison this process.
    """
    _probe_product_map(tmp_path)
    entry = tmp_path / _bindings.PRODUCT_MAP_DIR / "probe.md"
    entry.write_text(
        "---\nid: " + "feat-" + "probe" + "\nstatus: partial\n---\n\n# Probe\n\n## Behaviors\n\n"
        "- [x] the first probe behaviour\n"
        "      <!-- behaviour: probe-01 | tests: backend/tests/unit/test_probe.py::test_probe -->\n"
        "- [x] an inserted behaviour that carried no marker when it landed\n"
        "- [x] the second probe behaviour\n"
        "      <!-- behaviour: probe-02 | tests: none -->\n"
        "- [x] the third probe behaviour\n"
        "      <!-- behaviour: probe-03 | tests: none -->\n\n",
        encoding="utf-8",
    )

    completed = subprocess.run(  # noqa: S603 - fixed argv; the repo's own script, no shell
        [sys.executable, str(REPO_ROOT / "scripts" / "run_behaviour_bindings_baseline.py"), "--root", str(tmp_path), "--write-markers"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert completed.returncode == 0, f"--write-markers exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    ids = re.findall(r"<!-- behaviour: (?P<id>[^ ]+) \|", entry.read_text(encoding="utf-8"))
    assert ids == ["probe-01", "probe-04", "probe-02", "probe-03"], f"the inserted bullet must take the first unused suffix, got {ids!r}"
    assert len(ids) == len(set(ids)), f"the regenerator emitted duplicate behaviour ids: {ids!r}"


# ---------------------------------------------------------------------------
# Deleting a behaviour is invisible unless the id sets are compared
# ---------------------------------------------------------------------------
def _git(root: Path, *args: str) -> str:
    """Run git in the throwaway repo and return its stdout."""
    assert GIT is not None, "git must be on PATH: --check-base compares against a git revision"
    completed = subprocess.run(  # noqa: S603 - resolved git binary, fixed argv, throwaway repo under tmp_path
        [GIT, *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, f"git {' '.join(args)} failed in {root}:\n{completed.stdout}\n{completed.stderr}"
    return completed.stdout


def _init_probe_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "behaviour-probe")
    _git(root, "config", "user.email", "behaviour-probe@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")


def _write_probe_baseline(
    root: Path,
    *,
    fingerprints: dict[str, str],
    unbound: dict[str, dict[str, str]] | None = None,
    approved: list[str] | None = None,
) -> None:
    """Write a canonical baseline document describing exactly these ids."""
    path = root / _bindings.BASELINE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _bindings.render_baseline(unbound or {}, fingerprints=fingerprints, weak_bindings=[], approved_removals=approved or []),
        encoding="utf-8",
        newline="\n",
    )


def _commit_probe_baseline(root: Path) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "behaviour baseline")
    return _git(root, "rev-parse", "HEAD").strip()


def test_check_base_fails_when_a_behaviour_is_deleted_with_its_marker(tmp_path: Path, capsys):
    """Deleting a behaviour AND its marker must fail ``--check-base``.

    Every other bindings test and the shrink-only ratchet read the id sets the
    current tree produces, so a deleted behaviour simply stopped appearing in the
    baseline entries, the weak ratchet and the fingerprint index: nothing failed
    while a documented behaviour vanished from the map. ``--check-base`` now
    compares the id set against the base revision, and the baseline's
    ``approved_removals`` key is the only way to allow an intended removal.

    The throwaway repo makes this a real git comparison through the real
    ``check_base`` (no monkeypatched ``git show``).
    """
    root = tmp_path / "repo"
    _init_probe_repo(root)
    first = _bindings.behaviour_text_fingerprint("- [x] the first probe behaviour")
    second = _bindings.behaviour_text_fingerprint("- [x] the second probe behaviour")
    unbound = {"probe-02": {"entry": "docs/product-map/probe.md", "reason": "behaviour text names no test"}}

    _write_probe_baseline(root, fingerprints={"probe-01": first, "probe-02": second}, unbound=unbound)
    base = _commit_probe_baseline(root)

    # The deletion under test: probe-02 and its marker are gone, so the id is
    # absent from the entries and from the fingerprint index alike.
    _write_probe_baseline(root, fingerprints={"probe-01": first})
    capsys.readouterr()
    assert _bindings.check_base(root, base) == 1, "deleting a behaviour together with its marker must fail --check-base"
    assert "REMOVED probe-02" in capsys.readouterr().err

    # The explicit exemption: the same deletion passes once the id is approved.
    _write_probe_baseline(root, fingerprints={"probe-01": first}, approved=["probe-02"])
    capsys.readouterr()
    assert _bindings.check_base(root, base) == 0, "an approved removal must pass --check-base"
    assert "1 approved removal(s): probe-02" in capsys.readouterr().err


def test_write_baseline_preserves_an_approved_removal(tmp_path: Path):
    """Regenerating the baseline must not silently drop the removal exemption.

    ``approved_removals`` is the only thing that lets a deletion through, so a
    ``--write-baseline`` that rewrote the document from the audit alone would
    erase the exemption in the same change that needs it - and the gate would fail
    on a tree the author had just regenerated.
    """
    _probe_product_map(tmp_path)
    baseline_path = tmp_path / _bindings.BASELINE_RELPATH
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    payload[_bindings.BASELINE_APPROVED_REMOVALS_KEY] = ["probe-99"]
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")

    completed = subprocess.run(  # noqa: S603 - fixed argv; the repo's own script, no shell
        [sys.executable, str(REPO_ROOT / "scripts" / "run_behaviour_bindings_baseline.py"), "--root", str(tmp_path), "--write-baseline"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert completed.returncode == 0, f"--write-baseline exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    regenerated = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert regenerated[_bindings.BASELINE_APPROVED_REMOVALS_KEY] == ["probe-99"], "regenerating the baseline dropped the approved-removal exemption"


def test_write_baseline_preserves_ticket_and_review_by(tmp_path: Path):
    """Regenerating the baseline must not reset its governance metadata.

    ``docs/product-map/README.md`` tells a reviewer to move ``review_by`` forward;
    a regenerator that always emitted the script constants would silently undo
    that (reproduced: FAR-9999/2030-12-31 reverted to FAR-2701/2027-06-30).
    """
    _probe_product_map(tmp_path)
    baseline_path = tmp_path / _bindings.BASELINE_RELPATH
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    payload["ticket"] = "FAR-9999"
    payload["review_by"] = "2030-12-31"
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8", newline="\n")

    completed = subprocess.run(  # noqa: S603 - fixed argv; the repo's own script, no shell
        [sys.executable, str(REPO_ROOT / "scripts" / "run_behaviour_bindings_baseline.py"), "--root", str(tmp_path), "--write-baseline"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    assert completed.returncode == 0, f"--write-baseline exited {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    regenerated = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert regenerated["ticket"] == "FAR-9999", "regenerating the baseline reset its owning ticket"
    assert regenerated["review_by"] == "2030-12-31", "regenerating the baseline reset its review-by date"


# ---------------------------------------------------------------------------
# The workflow properties this gate depends on
# ---------------------------------------------------------------------------
def _load_workflow(filename: str) -> dict:
    """Parse a workflow file, tolerating PyYAML's boolean parse of the ``on`` key."""
    path = REPO_ROOT / ".github" / "workflows" / filename
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f".github/workflows/{filename} is not a YAML mapping"
    return data


def _on_block(workflow: dict) -> dict:
    on = workflow.get("on", workflow.get(True))
    assert isinstance(on, dict), "the workflow declares no trigger mapping"
    return on


def _job_named(workflow: dict, name: str) -> dict:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "the workflow declares no jobs mapping"
    for job in jobs.values():
        if isinstance(job, dict) and job.get("name") == name:
            return job
    raise AssertionError(f"no job named {name!r}")


def test_prove_the_fix_workflow_reruns_on_a_body_edit_without_a_path_filter():
    """The marker gate must fire on a body edit, on every PR.

    The gate's fix is a line added to the PR BODY, so it only works if the
    workflow listens for ``edited`` - and a ``paths:`` filter would skip exactly
    the product PRs the check exists for. Both are load-bearing: without them the
    check is green because it never ran.
    """
    workflow = _load_workflow("prove-the-fix.yml")
    pull_request = _on_block(workflow).get("pull_request")
    assert isinstance(pull_request, dict), "prove-the-fix.yml must trigger on pull_request"
    types = pull_request.get("types")
    assert isinstance(types, list), "prove-the-fix.yml pull_request must declare an explicit types list"
    assert "edited" in types, f"prove-the-fix.yml must re-run when the PR body is edited, got types={types!r}"
    assert "paths" not in pull_request, "prove-the-fix.yml must not carry a paths: filter - the gate applies to every PR"
    assert "paths-ignore" not in pull_request, "prove-the-fix.yml must not carry a paths-ignore: filter - it would skip product PRs"


def test_prove_the_fix_job_actually_invokes_the_checker():
    """The workflow must RUN the checker, not merely declare the right triggers.

    A trigger test alone stays green if the check step is deleted, so the job's
    steps must invoke ``scripts/run_check_prove_the_fix.py`` - otherwise the gate
    never runs and every product PR passes vacuously.
    """
    workflow = _load_workflow("prove-the-fix.yml")
    job = _job_named(workflow, "Prove the fix marker")
    steps = job.get("steps") or []
    runs = " ".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "scripts/run_check_prove_the_fix.py" in runs, "the 'Prove the fix marker' job must run scripts/run_check_prove_the_fix.py"


def test_backend_lint_job_runs_the_behaviour_bindings_gate_unconditionally():
    """The bindings gate runs on every PR: no job-level ``if:``/``needs:``.

    ``docs/product-map`` is the specification, so a PR that skips this gate could
    let a behaviour go unbound. A job-level ``if:`` or ``needs:`` is how such a
    gate silently stops running (the same failure class as a ``paths:`` filter),
    so the job must be unconditional and must actually invoke the test module.
    """
    workflow = _load_workflow("ci.yml")
    job = _job_named(workflow, "Lint (Backend)")
    assert "if" not in job, "the Lint (Backend) job must not carry a job-level if: - it would stop running on some PRs"
    assert "needs" not in job, "the Lint (Backend) job must not carry a job-level needs: - a skipped dependency would skip the gate"
    steps = job.get("steps") or []
    runs = " ".join(str(step.get("run", "")) for step in steps if isinstance(step, dict))
    assert "tests/architecture/test_behaviour_bindings.py" in runs, "the Lint (Backend) job must run tests/architecture/test_behaviour_bindings.py"


def test_backend_lint_job_checks_the_baseline_against_the_base_revision_on_prs():
    """The shrink-only ``--check-base`` ratchet must run on PRs.

    Without a step that runs ``scripts/run_behaviour_bindings_baseline.py
    --check-base``, a PR could WIDEN the baseline (add an entry to excuse a newly
    unbound behaviour) and every other bindings test would stay green. The step is
    PR-scoped because a push to main has no base revision to compare against.
    """
    workflow = _load_workflow("ci.yml")
    job = _job_named(workflow, "Lint (Backend)")
    steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
    pr_scoped = [step for step in steps if "pull_request" in str(step.get("if", "")) and "run_behaviour_bindings_baseline.py" in str(step.get("run", "")) and "--check-base" in str(step.get("run", ""))]
    assert pr_scoped, "a pull_request-scoped step of the Lint (Backend) job must run scripts/run_behaviour_bindings_baseline.py --check-base"
