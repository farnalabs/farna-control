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

- every behaviour carries a stable, unique id in the right ordinal position;
- every reference a marker names RESOLVES against the collected test tree (a
  pytest node id, a test module, or a registered BDD feature file);
- no reference names a test the shared test-suite-quality lenses flag as vacuous
  (no-op ``@then``, self-asserting BDD step, duplicate test body);
- every ticked behaviour is bound to at least one clean reference, or is frozen
  in ``backend/tests/architecture/behaviour_bindings_baseline.json``;
- the baseline is shrink-only: an entry that has since become bound (or whose
  behaviour disappeared or was unticked) fails, so it can never be widened;
- ``status: covered`` is derived from the bindings, not trusted: a covered entry
  may not carry a ticked behaviour that is unbound outside the baseline;
- the baseline and ``docs/product-map/unbound-behaviours.md`` are exactly what
  the regenerator produces, so neither can be hand-edited into a weaker claim.
"""

from __future__ import annotations

import functools
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

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


@functools.cache
def _audit() -> object:
    """Resolve every behaviour once per session (the parse is the expensive part)."""
    return _bindings.audit(REPO_ROOT)


@functools.cache
def _baseline() -> dict[str, dict[str, str]]:
    return _bindings.load_baseline(REPO_ROOT)


def _entries() -> list[object]:
    return _audit().entries


def _all_behaviours() -> list[tuple[object, object]]:
    """Every ``(entry, behaviour)`` pair, in file order."""
    return [(entry, behaviour) for entry in _entries() for behaviour in entry.behaviours]


def _format_unbound(rows: list[tuple[str, str, str]]) -> str:
    return "\n".join(f"  {entry}: {behaviour_id} - {reason}" for entry, behaviour_id, reason in rows)


def test_every_behaviour_carries_a_stable_unique_id():
    """Every behaviour bullet names an id, in the ordinal position it occupies.

    The id is what the baseline and ``Proves:`` links refer to, so it must be
    present, unique, and pinned to its position: a renumbering that silently
    rebinds a baseline entry to a different behaviour would make the ratchet lie.
    """
    missing: list[str] = []
    misplaced: list[str] = []
    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for entry in _entries():
        for ordinal, behaviour in enumerate(entry.behaviours, start=1):
            expected = f"{entry.path.stem}-{ordinal:02d}"
            if not behaviour.id:
                missing.append(f"  {entry.rel}:{behaviour.start_line} ({expected})")
                continue
            if behaviour.id != expected:
                misplaced.append(f"  {entry.rel}:{behaviour.start_line} has {behaviour.id!r}, expected {expected!r}")
            if behaviour.id in seen:
                duplicated.append(f"  {behaviour.id}: {seen[behaviour.id]} and {entry.rel}")
            seen[behaviour.id] = entry.rel

    assert not missing, "behaviour bullets with no `<!-- behaviour: <id> | tests: ... -->` marker. Add one with `uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-markers`:\n" + "\n".join(missing)
    assert not misplaced, "behaviour ids do not match their ordinal position (renumbering rebinds the shrink-only baseline):\n" + "\n".join(misplaced)
    assert not duplicated, "duplicate behaviour ids across the product map:\n" + "\n".join(duplicated)


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


def test_covered_status_is_derived_from_bindings():
    """``status: covered`` may not survive a ticked behaviour that no test proves.

    The frontmatter value used to be the only statement of coverage. It is now
    checked against the bindings: an entry claiming ``covered`` must have every
    behaviour ticked and bound, with any remaining shortfall frozen in the
    baseline (which can only shrink).
    """
    baseline = _baseline()
    offenders: list[str] = []
    for entry in _entries():
        if entry.status != "covered":
            continue
        for behaviour in entry.behaviours:
            if not behaviour.ticked:
                offenders.append(f"  {entry.rel}: {behaviour.id} is unticked but the entry claims status: covered")
            elif behaviour.id in _audit().unbound and behaviour.id not in baseline:
                offenders.append(f"  {entry.rel}: {behaviour.id} has no clean binding and is not in the baseline")

    assert not offenders, "entries claiming 'status: covered' carry behaviours their bindings do not prove:\n" + "\n".join(offenders)


def test_baseline_file_is_the_canonical_rendering():
    """The baseline on disk is exactly what the regenerator emits."""
    current = (REPO_ROOT / _bindings.BASELINE_RELPATH).read_text(encoding="utf-8")
    expected = _bindings.render_baseline(_baseline())
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
