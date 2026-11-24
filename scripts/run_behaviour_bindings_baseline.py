#!/usr/bin/env python3
"""Product-map behaviour bindings: the format, the resolver, and the regenerator.

The product map (``docs/product-map/``) is the specification for what Farna
Control ships. Its ``## Behaviors`` checklists used to be a *claim*: a ``[x]``
box and a hand-typed ``status:`` in the frontmatter, with nothing tying either
to a test. A behaviour could keep its tick after the test that proved it was
deleted, and a vacuous test (a pass-only BDD ``@then``, a self-asserting step,
a copy-pasted duplicate body) satisfied the claim just as well as a real one.

This module is the single implementation of the binding format. It is consumed
by:

* ``backend/tests/architecture/test_behaviour_bindings.py`` - the architecture
  gate (every ticked behaviour resolves to a test that is not vacuous, and the
  shrink-only baseline in
  ``backend/tests/architecture/behaviour_bindings_baseline.json`` can only
  shrink);
* ``scripts/run_behaviour_bindings_baseline.py`` itself (``--write-markers``,
  ``--write-baseline``, ``--write-report``) - the mechanical backfill and the
  regenerator.

Keeping both on one implementation is deliberate: a second parser is how a gate
and its regenerator drift into enforcing different rules.

Binding format
--------------
Every behaviour bullet ends with one HTML comment holding its stable id and the
test references that prove it::

    - [x] ``GET /healthz`` returns ``{"status": "ok"}`` ...
          <!-- behaviour: health-checks-01 | tests: backend/tests/unit/api/test_health.py::test_liveness_ok -->

* The id is ``<entry-file-stem>-<NN>`` (``health-checks-01``), stable because it
  is written into the file: moving a behaviour takes its id with it, and a
  renumbering shows up as a baseline stale-entry failure rather than silently
  rebinding.
* ``tests:`` takes a comma-separated list of three reference kinds, or ``none``:

  - ``<file>.py::<test_name>`` (node binding, the strong form; a trailing
    ``*`` glob is allowed when backfilled from prose),
  - ``<file>.py`` (module binding: the behaviour is proven by that module's
    suite - the weaker form, tolerated while the graph is backfilled),
  - ``<file>.feature`` (feature binding: the behaviour is proven by a BDD
    feature file that a step module registers).
* The comment is invisible when the markdown renders, so the behaviour text
  reads exactly as before.

A behaviour that is ticked but has no resolvable, non-vacuous binding is
recorded in the baseline with a one-line reason (see
``docs/product-map/unbound-behaviours.md`` for the generated, current list).

Why not frontmatter?
--------------------
A frontmatter ``bindings:`` map keyed by an id would still need the id on the
behaviour line to be stable, and multi-line behaviours make "the line" a
positional guess. An inline comment keeps the id, the claim and the proof in the
same place, so a reviewer editing a behaviour sees the test it rests on.

How references are resolved
---------------------------
Statically, against the test tree - the same approach the existing product-map
architecture tests take (they parse ``tests/`` rather than collecting it). A
``pytest --collect-only`` pass would import every test module; parsing the
modules is deterministic, side-effect free, and fast enough to run on every
architecture-suite invocation. ``TestIndex`` indexes module paths and the
``test_*`` functions each module defines.

Usage
-----
    # Regenerate the bindings, the baseline and the report after editing entries
    uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --all

    # Just add missing markers (mechanical backfill from prose/prose refs)
    uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-markers

    # Just refresh the shrink-only baseline and the report
    uv run --project backend --no-sync python scripts/run_behaviour_bindings_baseline.py --write-baseline --write-report
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import ModuleType

import yaml

#: Repo-relative locations this module owns.
PRODUCT_MAP_DIR = "docs/product-map"
GRAPH_INDEX = "docs/product-map/README.md"
TESTS_DIR = "backend/tests"
BDD_FEATURES_DIR = "backend/tests/bdd/features"
BASELINE_RELPATH = "backend/tests/architecture/behaviour_bindings_baseline.json"
REPORT_RELPATH = "docs/product-map/unbound-behaviours.md"

#: ``<!-- behaviour: <id> | tests: <refs|none> -->``
MARKER_RE = re.compile(r"<!--\s*behaviour:\s*(?P<id>[A-Za-z0-9][A-Za-z0-9._-]*)\s*\|\s*tests:\s*(?P<refs>.*?)\s*-->")
_BULLET_RE = re.compile(r"^- \[(?P<tick>[ xX])\] ")
_HEADING_RE = re.compile(r"^##\s+Behaviou?rs\s*$")
_ANY_HEADING_RE = re.compile(r"^##\s+")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_NODE_TOKEN_RE = re.compile(r"(?P<file>[A-Za-z0-9_./-]+\.py)::(?P<name>[A-Za-z0-9_*]+)")
_MODULE_TOKEN_RE = re.compile(r"(?P<file>[A-Za-z0-9_./-]+\.py)\b")
_FEATURE_TOKEN_RE = re.compile(r"(?P<file>[A-Za-z0-9_./-]+\.feature)\b")
_FUNC_NAME_RE = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>test_[A-Za-z0-9_]+)", re.MULTILINE)

NONE_TOKEN = "none"


# ---------------------------------------------------------------------------
# Entry + behaviour parsing
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Behaviour:
    """One ``- [x]`` item: its id, its claim, and the refs it names."""

    id: str
    ticked: bool
    text: str
    refs: tuple[str, ...]
    start_line: int
    end_line: int

    @property
    def headline(self) -> str:
        """The behaviour's first line with the checkbox and marker removed.

        Truncated (the report needs a readable one-liner) and right-stripped: a
        cut mid-word can otherwise leave a trailing space, which the repository's
        trailing-whitespace hook rewrites - making the generated file differ from
        what the regenerator emits.
        """
        first = self.text.splitlines()[0] if self.text else ""
        return first.removeprefix("- [x] ").removeprefix("- [X] ").removeprefix("- [ ] ")[:160].rstrip()


@dataclass(frozen=True)
class Entry:
    """One behaviour-tracker entry under ``docs/product-map/``."""

    path: Path
    rel: str
    feature_id: str
    status: str | None
    behaviours: tuple[Behaviour, ...]
    frontmatter: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """Whether one binding reference resolves, and why not when it does not."""

    ref: str
    ok: bool
    kind: str
    detail: str


@dataclass
class TestIndex:
    """Lazy index over ``backend/tests``: modules, and functions per module."""

    root: Path
    modules: dict[str, str] = field(default_factory=dict)
    by_basename: dict[str, list[str]] = field(default_factory=dict)
    _functions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _scenario_modules: dict[str, bool] = field(default_factory=dict)
    _all_functions: dict[str, tuple[str, ...]] | None = None

    @classmethod
    def build(cls, root: Path) -> TestIndex:
        index = cls(root=root)
        tests = root / TESTS_DIR
        for path in sorted(tests.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            index.modules[rel] = rel
            index.by_basename.setdefault(path.name, []).append(rel)
        return index

    def functions_in(self, rel: str) -> tuple[str, ...]:
        """Every ``test_*`` function defined in *rel* (cached)."""
        if rel not in self._functions:
            try:
                text = (self.root / rel).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                text = ""
            self._functions[rel] = tuple(_FUNC_NAME_RE.findall(text))
        return self._functions[rel]

    def has_module(self, rel: str) -> bool:
        return rel in self.modules

    def registers_scenarios(self, rel: str) -> bool:
        """True when a BDD step module loads feature files via ``scenarios(...)``.

        A step module defines step functions, not ``test_*`` functions: its
        executable tests are the scenarios pytest-bdd generates from the
        feature files it registers.
        """
        if rel in self._scenario_modules:
            return self._scenario_modules[rel]
        try:
            text = (self.root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        self._scenario_modules[rel] = "scenarios(" in text
        return self._scenario_modules[rel]

    def resolve_module(self, ref: str) -> str | None:
        """Normalise a module/file reference to a repo-relative path."""
        cleaned = ref.strip().strip("`").lstrip("./")
        for candidate in (cleaned, f"{TESTS_DIR}/{cleaned}"):
            if candidate in self.modules:
                return candidate
        if "/" not in cleaned:
            matches = self.by_basename.get(cleaned, [])
            if len(matches) == 1:
                return matches[0]
        return None

    def all_function_modules(self) -> dict[str, tuple[str, ...]]:
        """Every test function name -> the modules that define it (cached)."""
        if self._all_functions is None:
            found: dict[str, list[str]] = {}
            for rel in self.modules:
                for name in self.functions_in(rel):
                    found.setdefault(name, []).append(rel)
            self._all_functions = {name: tuple(sorted(dict.fromkeys(rels))) for name, rels in found.items()}
        return self._all_functions


def _frontmatter(lines: list[str]) -> tuple[dict, int]:
    """Return ``(frontmatter, index of the closing '---')``."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
    if end == 0:
        return {}, 0
    try:
        loaded = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError:
        loaded = None
    return (loaded if isinstance(loaded, dict) else {}), end


def _behaviour_blocks(lines: list[str], start: int) -> list[tuple[int, int]]:
    """Return ``(first, last)`` inclusive line indexes of each behaviour bullet."""
    blocks: list[tuple[int, int]] = []
    current: int | None = None
    for index in range(start, len(lines)):
        line = lines[index]
        if _ANY_HEADING_RE.match(line):
            if current is not None:
                blocks.append((current, index - 1))
            break
        if _BULLET_RE.match(line):
            if current is not None:
                blocks.append((current, index - 1))
            current = index
            continue
        if current is None:
            continue
        if line.strip() == "":
            continue
        if line.startswith((" ", "\t")):
            continue
        blocks.append((current, index - 1))
        current = None
    else:
        if current is not None:
            blocks.append((current, len(lines) - 1))
    return blocks


def _trimmed_block_end(lines: list[str], first: int, last: int) -> int:
    """Last index of the block that carries content (trailing blanks dropped)."""
    end = last
    while end > first and lines[end].strip() == "":
        end -= 1
    return end


def parse_entry(path: Path, root: Path) -> Entry:
    """Parse one behaviour-tracker entry, including every behaviour marker."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    frontmatter, fm_end = _frontmatter(lines)
    feature_id = str(frontmatter.get("id") or "")
    status = frontmatter.get("status")
    status = str(status) if status is not None else None

    heading = next((i for i in range(fm_end, len(lines)) if _HEADING_RE.match(lines[i])), None)
    behaviours: list[Behaviour] = []
    if heading is not None:
        for first, last in _behaviour_blocks(lines, heading + 1):
            block = lines[first : last + 1]
            marker = MARKER_RE.search("\n".join(block))
            body = "\n".join(line for line in block if not MARKER_RE.search(line))
            refs: tuple[str, ...] = ()
            if marker is not None:
                raw = marker.group("refs").strip()
                refs = () if raw in {"", NONE_TOKEN} else tuple(part.strip() for part in raw.split(",") if part.strip())
            behaviours.append(
                Behaviour(
                    id=marker.group("id") if marker is not None else "",
                    ticked=block[0][3].lower() == "x",
                    text=body.strip("\n"),
                    refs=refs,
                    start_line=first + 1,
                    end_line=_trimmed_block_end(lines, first, last) + 1,
                )
            )
    return Entry(
        path=path,
        rel=path.relative_to(root).as_posix(),
        feature_id=feature_id,
        status=status,
        behaviours=tuple(behaviours),
        frontmatter=frontmatter,
    )


def entry_paths(root: Path) -> list[Path]:
    """Every behaviour-tracker entry (``*.md`` except the graph index and the report)."""
    base = root / PRODUCT_MAP_DIR
    support = {(root / GRAPH_INDEX).resolve(), (root / REPORT_RELPATH).resolve()}
    return sorted(path for path in base.rglob("*.md") if path.resolve() not in support)


def load_entries(root: Path) -> list[Entry]:
    """Parse every behaviour-tracker entry under ``docs/product-map/``."""
    return [parse_entry(path, root) for path in entry_paths(root)]


# ---------------------------------------------------------------------------
# Binding resolution
# ---------------------------------------------------------------------------
def _registered_bdd_features(root: Path) -> set[str]:
    """BDD feature files a step module wires up via ``scenarios(...)``.

    Reuses ``test_product_map_feature_gaps._registered_bdd_features`` so the
    binding gate and the product-map BDD citations agree on what "registered"
    means (a feature file that no step module loads contributes nothing).
    """
    key = str(root.resolve())
    if key not in _REGISTERED_CACHE:
        module = _load_module(root, "test_product_map_feature_gaps", "backend/tests/architecture/test_product_map_feature_gaps.py")
        _REGISTERED_CACHE[key] = {path.relative_to(root).as_posix() for path in module._registered_bdd_features()}
    return _REGISTERED_CACHE[key]


_REGISTERED_CACHE: dict[str, set[str]] = {}


def _feature_fully_deselected(root: Path, rel: str) -> bool:
    module = _load_module(root, "test_product_map_feature_gaps", "backend/tests/architecture/test_product_map_feature_gaps.py")
    return module._feature_scenarios_fully_deselected(root / rel)


_LOADED_MODULES: dict[str, ModuleType] = {}


def _load_module(root: Path, name: str, relpath: str) -> ModuleType:
    """Import a repo module by path, once per process.

    The module is registered in ``sys.modules`` before execution: a
    ``@dataclass`` whose annotations are strings (``from __future__ import
    annotations``) resolves them through ``sys.modules[cls.__module__]``, which
    is absent for a bare ``exec_module`` and raises ``AttributeError``.
    """
    if name in _LOADED_MODULES:
        return _LOADED_MODULES[name]
    loader = SourceFileLoader(name, str(root / relpath))
    module = module_from_spec(spec_from_loader(name, loader))
    assert module is not None
    sys.modules[name] = module
    try:
        loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    _LOADED_MODULES[name] = module
    return module


def resolve_ref(ref: str, index: TestIndex, root: Path, registered_features: set[str] | None = None) -> Resolution:
    """Resolve one binding reference to a concrete test artefact."""
    ref = ref.strip()
    if not ref:
        return Resolution(ref=ref, ok=False, kind="none", detail="empty binding reference")
    if "::" in ref:
        return _resolve_node_ref(ref, index)
    if ref.endswith(".feature"):
        features = registered_features if registered_features is not None else _registered_bdd_features(root)
        if ref not in features:
            return Resolution(ref=ref, ok=False, kind="feature", detail="feature file is not registered by any scenarios(...) step module")
        if _feature_fully_deselected(root, ref):
            return Resolution(ref=ref, ok=False, kind="feature", detail="every scenario in the feature file is @awaiting-implementation")
        return Resolution(ref=ref, ok=True, kind="feature", detail="registered feature file with executing scenarios")
    if ref.endswith(".py"):
        return _resolve_module_ref(ref, index)
    return Resolution(ref=ref, ok=False, kind="none", detail="not a pytest node id, a test module or a BDD feature file")


def _resolve_node_ref(ref: str, index: TestIndex) -> Resolution:
    parts = ref.split("::")
    file_part, name = parts[0], parts[-1]
    rel = index.resolve_module(file_part)
    if rel is None:
        return Resolution(ref=ref, ok=False, kind="node", detail=f"test module {file_part!r} does not exist under {TESTS_DIR}")
    functions = index.functions_in(rel)
    if name in functions:
        return Resolution(ref=ref, ok=True, kind="node", detail=f"node resolves in {rel}")
    if "*" in name:
        matches = [candidate for candidate in functions if fnmatch.fnmatchcase(candidate, name)]
        if matches:
            return Resolution(ref=ref, ok=True, kind="node", detail=f"glob resolves to {len(matches)} test(s) in {rel}")
        return Resolution(ref=ref, ok=False, kind="node", detail=f"no test in {rel} matches {name!r}")
    return Resolution(ref=ref, ok=False, kind="node", detail=f"{name!r} is not defined in {rel}")


def _resolve_module_ref(ref: str, index: TestIndex) -> Resolution:
    rel = index.resolve_module(ref)
    if rel is None:
        return Resolution(ref=ref, ok=False, kind="module", detail=f"test module {ref!r} does not exist under {TESTS_DIR}")
    if not index.functions_in(rel) and not index.registers_scenarios(rel):
        return Resolution(ref=ref, ok=False, kind="module", detail=f"{rel} defines no test function and registers no BDD scenarios")
    if index.registers_scenarios(rel) and not index.functions_in(rel):
        return Resolution(ref=ref, ok=True, kind="module", detail=f"{rel} registers pytest-bdd scenarios")
    return Resolution(ref=ref, ok=True, kind="module", detail=f"module has {len(index.functions_in(rel))} test function(s)")


def sibling_glob_matches(ref: str, index: TestIndex) -> tuple[str, ...]:
    """Expand a globbed node ref into concrete node refs (empty when none)."""
    file_part, _, name = ref.partition("::")
    rel = index.resolve_module(file_part)
    if rel is None or "*" not in name:
        return ()
    return tuple(f"{rel}::{candidate}" for candidate in index.functions_in(rel) if fnmatch.fnmatchcase(candidate, name))


# ---------------------------------------------------------------------------
# Vacuity: reuse the test-suite-quality lenses
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VacuityFlags:
    """Per-module vacuous test artefacts, as the shared lenses report them."""

    noop_then: dict[str, frozenset[str]]
    self_asserting: dict[str, frozenset[str]]
    duplicate_body: dict[str, frozenset[str]]

    def reasons_for(self, rel: str, name: str) -> list[str]:
        """Why a ``(module, function)`` binding is vacuous; empty when it is fine."""
        reasons: list[str] = []
        if name in self.noop_then.get(rel, frozenset()):
            reasons.append("no-op @then step (pass-only body): the scenario certifies nothing")
        if name in self.self_asserting.get(rel, frozenset()):
            reasons.append("self-asserting BDD step (fabricates the response it asserts on)")
        if name in self.duplicate_body.get(rel, frozenset()):
            reasons.append("duplicate test body (copy-paste twin of an earlier test)")
        return reasons

    def all_vacuous(self, rel: str, functions: tuple[str, ...]) -> bool:
        """True when every test in *functions* is flagged for *rel*."""
        if not functions:
            return False
        return all(self.reasons_for(rel, name) for name in functions)


def load_quality_lenses(root: Path, scope: list[str]) -> ModuleType:
    """Import ``test_test_suite_quality`` without paying its whole-tree warm-up.

    The module warms an AST cache for every test module in the tree at import
    time (~70s unscoped on this workstation). ``_iter_test_modules`` honours
    ``FARNA_CONTROL_TEST_STYLE_SCOPE`` (an ``os.pathsep`` list of ``.py`` paths),
    so scoping the import caps that warm-up while still running the REAL lenses -
    not a reimplementation and not a reading of the frozen baseline text.

    The scope is capped at ``_SCOPE_CAP`` paths because Windows caps a single
    environment variable at 32767 characters; the cap costs nothing, since the
    lens functions are handed the trees the gate already parsed.
    """
    env_var = "FARNA_CONTROL_TEST_STYLE_SCOPE"
    previous = os.environ.get(env_var)
    scoped = sorted({str(root / rel) for rel in scope if rel.endswith(".py")})[:_SCOPE_CAP]
    os.environ[env_var] = os.pathsep.join(scoped)
    try:
        return _load_module(root, "test_test_suite_quality", "backend/tests/architecture/test_test_suite_quality.py")
    finally:
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous


#: Paths handed to the quality module's import-time warm-up (see above).
_SCOPE_CAP = 80


def vacuity_flags(root: Path, modules: list[str], duplicate_scope: list[str] | None = None) -> VacuityFlags:
    """Run the shared vacuity lenses over the modules that carry bindings.

    Every bound module is checked for no-op ``@then`` steps and self-asserting
    BDD steps (the two claims a *module* binding can be fooled by). The
    duplicate-body lens - by far the most expensive, it fingerprints every test
    body - runs only over ``duplicate_scope``: the modules a NODE binding points
    at, where a bound duplicate twin is a precise, nameable defect.
    """
    scope = sorted(set(modules))
    lens = load_quality_lenses(root, scope)
    duplicate_scope_set = set(scope if duplicate_scope is None else duplicate_scope)
    # A ``@then``/``@given`` step decorator can only exist in a module that
    # imports pytest_bdd, so the two BDD lenses are skipped for the rest (they
    # walk every node of the module for nothing). The duplicate-body lens runs
    # only where a NODE binding names a specific test.
    scanned = sorted({rel for rel in scope if _imports_pytest_bdd(root, rel)} | duplicate_scope_set)
    noop: dict[str, frozenset[str]] = {}
    asserting: dict[str, frozenset[str]] = {}
    duplicates: dict[str, frozenset[str]] = {}
    for rel in scanned:
        tree = lens._parse(root / rel)
        if tree is None:
            continue
        noop_names = _noop_step_function_names(lens, tree)
        if noop_names:
            noop[rel] = frozenset(noop_names)
        asserting_names = {name for name, _line, _detail in lens._self_asserting_bdd_step_violation_functions(tree)}
        if asserting_names:
            asserting[rel] = frozenset(asserting_names)
        if rel in duplicate_scope_set:
            duplicate_names = {second for _scope_name, _first, second, _line in lens._duplicate_test_body_functions(tree)}
            if duplicate_names:
                duplicates[rel] = frozenset(duplicate_names)
    return VacuityFlags(noop_then=noop, self_asserting=asserting, duplicate_body=duplicates)


def _imports_pytest_bdd(root: Path, rel: str) -> bool:
    """True when *rel* imports pytest_bdd (the only place a step decorator lives)."""
    key = f"{root.resolve()}|{rel}"
    if key not in _PYTEST_BDD_CACHE:
        try:
            _PYTEST_BDD_CACHE[key] = "pytest_bdd" in (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            _PYTEST_BDD_CACHE[key] = False
    return _PYTEST_BDD_CACHE[key]


_PYTEST_BDD_CACHE: dict[str, bool] = {}


def _noop_step_function_names(lens: ModuleType, tree: object) -> set[str]:
    """Function names of pass-only ``@then`` steps (the no-op lens' own keys)."""
    violations = lens._noop_bdd_then_violation_functions(tree)
    if not violations:
        return set()
    step_text_to_name = _then_step_function_map(lens, tree)
    return {step_text_to_name[text] for text, _line in violations if text in step_text_to_name}


def _then_step_function_map(lens: ModuleType, tree: object) -> dict[str, str]:
    """Map each ``@then`` step's Gherkin text to its Python function name."""
    mapping: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "then" not in lens._bdd_step_decorator_names(node):
            continue
        text = lens._then_step_text(node)
        if text is not None:
            mapping[text] = node.name
    return mapping


def bound_modules(entries: list[Entry]) -> list[str]:
    """Every ``.py`` module named by a binding (node or module form)."""
    return [ref.partition("::")[0] for entry in entries for behaviour in entry.behaviours for ref in behaviour.refs if ref.endswith(".py")]


def node_bound_modules(entries: list[Entry]) -> list[str]:
    """Every ``.py`` module named by a NODE binding (``file::test``)."""
    return [ref.partition("::")[0] for entry in entries for behaviour in entry.behaviours for ref in behaviour.refs if "::" in ref]


# ---------------------------------------------------------------------------
# Baseline + derived status
# ---------------------------------------------------------------------------
def load_baseline(root: Path) -> dict[str, dict[str, str]]:
    """The shrink-only baseline: id -> {entry, reason}."""
    path = root / BASELINE_RELPATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{BASELINE_RELPATH} must declare an 'entries' mapping")
    return {str(key): dict(value) for key, value in entries.items()}


def render_baseline(entries: dict[str, dict[str, str]]) -> str:
    payload = {
        "schema_version": 1,
        "description": "Product-map behaviours that cannot be bound to a resolving, non-vacuous test yet. Shrink-only: see docs/product-map/README.md.",
        "generated_by": "scripts/run_behaviour_bindings_baseline.py --write-baseline",
        "entries": {key: entries[key] for key in sorted(entries)},
    }
    return json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=False) + "\n"


def derive_status(behaviours: tuple[Behaviour, ...], vacuous_ids: frozenset[str], vacuous: dict[str, list[str]]) -> str:
    """Derive an entry's coverage status from its bindings, not from its frontmatter.

    ``covered`` requires every behaviour ticked AND carrying at least one binding
    that resolves and is not vacuous. ``gap`` is nothing ticked or bound.
    """
    if not behaviours:
        return "gap"
    any_progress = False
    all_covered = True
    for behaviour in behaviours:
        if _behaviour_ok(behaviour, vacuous_ids, vacuous):
            any_progress = True
        else:
            all_covered = False
            if behaviour.ticked and behaviour.refs:
                any_progress = True
    if all_covered:
        return "covered"
    return "partial" if any_progress else "gap"


def _behaviour_ok(behaviour: Behaviour, vacuous_ids: frozenset[str], vacuous: dict[str, list[str]]) -> bool:
    """A behaviour counts as covered when ticked and its bindings resolve cleanly."""
    if not behaviour.ticked:
        return False
    if behaviour.id in vacuous_ids:
        return False
    if not behaviour.refs:
        return False
    return not vacuous.get(behaviour.id)


# ---------------------------------------------------------------------------
# Mechanical backfill: derive markers from prose
# ---------------------------------------------------------------------------
def entry_hints(entry: Entry) -> tuple[str, ...]:
    """The repo-relative test files the entry's frontmatter already cites.

    ``unit-tests:`` / ``bdd:`` are the entry-level coverage claim; they are the
    disambiguator when a behaviour names a bare ``test_foo.py`` that exists in
    more than one package under ``backend/tests``.
    """
    hints: list[str] = []
    for field_name in ("unit-tests", "bdd"):
        value = entry.frontmatter.get(field_name) or []
        if isinstance(value, str):
            value = [value]
        hints.extend(item.strip().lstrip("./") for item in value if isinstance(item, str) and item.strip())
    return tuple(dict.fromkeys(hints))


def derive_marker_refs(behaviour: Behaviour, entry: Entry, index: TestIndex, hints: tuple[str, ...] = ()) -> tuple[list[str], list[str]]:
    """Derive binding refs from a behaviour's prose; returns ``(refs, notes)``.

    Sources, in order: ``<file>.py::<test>`` node citations, ``.feature``
    citations, test-module citations (path or basename), and a bare
    ``test_<name>`` that resolves uniquely in the tree. Frontmatter
    ``unit-tests:``/``bdd:`` paths are used only to disambiguate a basename that
    matches several packages - never to bind a behaviour that names no test at
    all, because that would launder an untested behaviour into a bound one.
    """
    refs: list[str] = []
    notes: list[str] = []

    def _add(candidate: str) -> None:
        if candidate not in refs:
            refs.append(candidate)

    node_modules: set[str] = set()
    for token in _BACKTICK_RE.findall(behaviour.text) + [behaviour.text]:
        for node in _NODE_TOKEN_RE.finditer(token):
            name = node.group("name")
            if not name.startswith("test_") and "::" not in node.group(0):
                continue
            rel = _resolve_test_module(node.group("file"), index, hints)
            if rel is None:
                if _looks_like_test_module(node.group("file")):
                    notes.append(f"cites {node.group('file')}, which resolves to no test module under {TESTS_DIR}")
                continue
            if name in index.functions_in(rel):
                _add(f"{rel}::{name}")
                node_modules.add(rel)
            elif "*" in name:
                matches = [f"{rel}::{candidate}" for candidate in index.functions_in(rel) if fnmatch.fnmatchcase(candidate, name)]
                for match in matches[:6]:
                    _add(match)
                if matches:
                    node_modules.add(rel)
                else:
                    notes.append(f"cites {name!r}, which matches no test in {rel}")
            else:
                notes.append(f"cites {name!r}, which is not defined in {rel}")
        for feature in _FEATURE_TOKEN_RE.finditer(token):
            resolved = _resolve_feature_path(feature.group("file"), entry, index)
            if resolved is not None:
                _add(resolved)
        for candidate in _candidate_modules(token, index, hints):
            if candidate not in node_modules:
                _add(candidate)

    if not refs:
        _add_bare_function_refs(behaviour.text, index, hints, _add)

    return refs, notes


def _add_bare_function_refs(text: str, index: TestIndex, hints: tuple[str, ...], add) -> None:
    """Bind a bare ``test_<name>`` citation when it names exactly one test."""
    for match in re.finditer(r"\b(test_[A-Za-z0-9_]+)\b", text):
        name = match.group(1)
        modules = index.all_function_modules().get(name)
        if not modules:
            continue
        hinted = [rel for rel in modules if rel in hints]
        chosen = hinted or list(modules)
        if len(chosen) == 1:
            add(f"{chosen[0]}::{name}")


def _looks_like_test_module(raw: str) -> bool:
    basename = raw.rsplit("/", 1)[-1]
    return basename.startswith("test_") and basename.endswith(".py")


def _resolve_test_module(raw: str, index: TestIndex, hints: tuple[str, ...]) -> str | None:
    """Resolve a test module reference, preferring an entry-cited path on ties."""
    rel = index.resolve_module(raw)
    if rel is not None:
        return rel
    basename = raw.rsplit("/", 1)[-1]
    candidates = index.by_basename.get(basename, [])
    hinted = [candidate for candidate in candidates if candidate in hints]
    if len(hinted) == 1:
        return hinted[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _candidate_modules(token: str, index: TestIndex, hints: tuple[str, ...] = ()) -> list[str]:
    """Test modules named in prose (path or bare basename)."""
    found: list[str] = []
    for match in _MODULE_TOKEN_RE.finditer(token):
        raw = match.group("file")
        if not raw.endswith(".py"):
            continue
        rel = _resolve_test_module(raw, index, hints)
        if rel is not None:
            found.append(rel)
    return found


def _resolve_feature_path(raw: str, entry: Entry, index: TestIndex) -> str | None:
    """Resolve a ``.feature`` reference written in a behaviour's prose."""
    cleaned = raw.strip().lstrip("./")
    candidate = index.root / cleaned
    if candidate.is_file() and cleaned.startswith(BDD_FEATURES_DIR):
        return cleaned
    sibling = entry.path.parent / cleaned
    if sibling.is_file():
        return sibling.relative_to(index.root).as_posix()
    basename = cleaned.rsplit("/", 1)[-1]
    matches = sorted((index.root / BDD_FEATURES_DIR).rglob(basename))
    if len(matches) == 1:
        return matches[0].relative_to(index.root).as_posix()
    return None


def assign_markers(root: Path) -> dict[str, int]:
    """Add a marker to every behaviour that lacks one; returns a per-entry count.

    Two passes, because a derived reference is only written when the shared
    vacuity lenses do NOT flag it: a marker that named a pass-only ``@then`` step
    would be a binding the gate rejects, so such a reference is dropped and the
    behaviour is recorded as unbound instead.
    """
    index = TestIndex.build(root)
    entries = load_entries(root)
    derived: dict[tuple[str, int], tuple[list[str], str, bool]] = {}
    candidate_modules: set[str] = set()
    for entry in entries:
        lines = entry.path.read_text(encoding="utf-8").splitlines()
        _fm, fm_end = _frontmatter(lines)
        heading = next((i for i in range(fm_end, len(lines)) if _HEADING_RE.match(lines[i])), None)
        if heading is None:
            continue
        for ordinal, (first, last) in enumerate(_behaviour_blocks(lines, heading + 1), start=1):
            block = lines[first : last + 1]
            marker_id = f"{entry.path.stem}-{ordinal:02d}"
            body = "\n".join(line for line in block if not MARKER_RE.search(line))
            behaviour = Behaviour(id=marker_id, ticked=block[0][3].lower() == "x", text=body, refs=(), start_line=first + 1, end_line=last + 1)
            refs, _notes = derive_marker_refs(behaviour, entry, index, entry_hints(entry))
            candidate_modules.update(ref.partition("::")[0] for ref in refs if ref.endswith(".py"))
            derived[(entry.rel, ordinal)] = (refs, marker_id, any(MARKER_RE.search(line) for line in block))

    modules = sorted(rel for rel in candidate_modules if index.has_module(rel))
    node_modules = sorted({ref.partition("::")[0] for (refs, _id, _has) in derived.values() for ref in refs if "::" in ref and index.has_module(ref.partition("::")[0])})
    flags = vacuity_flags(root, modules, node_modules)
    accepted = _filter_vacuous(derived, index, flags)

    written: dict[str, int] = {}
    for entry in entries:
        lines = entry.path.read_text(encoding="utf-8").splitlines()
        _fm, fm_end = _frontmatter(lines)
        heading = next((i for i in range(fm_end, len(lines)) if _HEADING_RE.match(lines[i])), None)
        if heading is None:
            continue
        affected = 0
        # Insert bottom-up: an inserted marker shifts every later line index, so a
        # top-down pass would address the wrong block after the first insertion.
        for ordinal, (first, last) in reversed(list(enumerate(_behaviour_blocks(lines, heading + 1), start=1))):
            refs, marker_id, has_marker = derived[(entry.rel, ordinal)]
            if has_marker:
                continue
            kept = accepted.get(marker_id, [])
            marker = f"      <!-- behaviour: {marker_id} | tests: {', '.join(kept) if kept else NONE_TOKEN} -->"
            insert_at = _trimmed_block_end(lines, first, last) + 1
            lines.insert(insert_at, marker)
            affected += 1
        if affected:
            entry.path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            written[entry.rel] = affected
    return written


def _filter_vacuous(derived: dict[tuple[str, int], tuple[list[str], str, bool]], index: TestIndex, flags: VacuityFlags) -> dict[str, list[str]]:
    """Drop references the vacuity lenses flag; keep the rest per behaviour id."""
    accepted: dict[str, list[str]] = {}
    for refs, marker_id, _has_marker in derived.values():
        kept = [ref for ref in refs if not _ref_is_vacuous(ref, index, flags)]
        accepted[marker_id] = kept
    return accepted


def _ref_is_vacuous(ref: str, index: TestIndex, flags: VacuityFlags) -> bool:
    """True when *ref* names a test the shared vacuity lenses flag."""
    if "::" in ref:
        rel, _, name = ref.partition("::")
        if "*" in name:
            matches = sibling_glob_matches(ref, index)
            return bool(matches) and all(flags.reasons_for(match.partition("::")[0], match.partition("::")[2]) for match in matches)
        return bool(flags.reasons_for(rel, name))
    if ref.endswith(".py"):
        return flags.all_vacuous(ref, index.functions_in(ref))
    return False


# ---------------------------------------------------------------------------
# Baseline + report generation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BindingAudit:
    """The full picture the gate and the regenerator both work from."""

    entries: list[Entry]
    resolutions: dict[str, list[Resolution]]
    vacuous_reasons: dict[str, list[str]]
    unbound: dict[str, dict[str, str]]
    derived: dict[str, str]
    index: TestIndex

    @property
    def bound_count(self) -> int:
        """Ticked behaviours with at least one clean binding."""
        total = 0
        for entry in self.entries:
            for behaviour in entry.behaviours:
                if behaviour.ticked and _clean_refs(behaviour, self.resolutions, self.vacuous_reasons):
                    total += 1
        return total

    @property
    def ticked_count(self) -> int:
        return sum(1 for entry in self.entries for behaviour in entry.behaviours if behaviour.ticked)


def _clean_refs(behaviour: Behaviour, resolutions: dict[str, list[Resolution]], vacuous: dict[str, list[str]]) -> list[str]:
    """The behaviour's refs that resolve AND are not vacuous."""
    if vacuous.get(behaviour.id):
        return []
    return [resolution.ref for resolution in resolutions.get(behaviour.id, []) if resolution.ok]


def audit(root: Path) -> BindingAudit:
    """Resolve every behaviour, flag vacuity, and record what cannot be bound."""
    entries = load_entries(root)
    index = TestIndex.build(root)
    registered = _registered_bdd_features(root)
    modules = sorted({rel for rel in bound_modules(entries) if index.has_module(rel)})
    duplicate_scope = sorted({rel for rel in node_bound_modules(entries) if index.has_module(rel)})
    flags = vacuity_flags(root, modules, duplicate_scope)

    resolutions: dict[str, list[Resolution]] = {}
    vacuous_reasons: dict[str, list[str]] = {}
    unbound: dict[str, dict[str, str]] = {}
    derived: dict[str, str] = {}

    for entry in entries:
        for behaviour in entry.behaviours:
            resolutions[behaviour.id] = [resolve_ref(ref, index, root, registered) for ref in behaviour.refs]
            reasons: list[str] = []
            for resolution in resolutions[behaviour.id]:
                if not resolution.ok:
                    continue
                if "::" in resolution.ref:
                    rel, _, name = resolution.ref.partition("::")
                    if "*" in name:
                        for match in sibling_glob_matches(resolution.ref, index):
                            rel_match, _, name_match = match.partition("::")
                            reasons.extend(flags.reasons_for(rel_match, name_match))
                    else:
                        reasons.extend(flags.reasons_for(rel, name))
                elif resolution.ref.endswith(".py"):
                    functions = index.functions_in(resolution.ref)
                    if flags.all_vacuous(resolution.ref, functions):
                        reasons.append(f"every test in {resolution.ref} is vacuous")
            vacuous_reasons[behaviour.id] = reasons

        vacuous_ids = frozenset(behaviour.id for behaviour in entry.behaviours if vacuous_reasons.get(behaviour.id))
        derived[entry.rel] = derive_status(entry.behaviours, vacuous_ids, vacuous_reasons)
        for behaviour in entry.behaviours:
            if not behaviour.ticked:
                continue
            if _clean_refs(behaviour, resolutions, vacuous_reasons):
                continue
            unbound[behaviour.id] = {"entry": entry.rel, "reason": _unbound_reason(behaviour, resolutions, vacuous_reasons, index, root)}

    return BindingAudit(entries=entries, resolutions=resolutions, vacuous_reasons=vacuous_reasons, unbound=unbound, derived=derived, index=index)


def _unbound_reason(behaviour: Behaviour, resolutions: dict[str, list[Resolution]], vacuous: dict[str, list[str]], index: TestIndex, root: Path) -> str:
    """One line explaining why a ticked behaviour carries no usable binding."""
    if not behaviour.refs:
        return "behaviour text names no test; only code paths (or nothing) are cited"
    if vacuous.get(behaviour.id):
        return "; ".join(dict.fromkeys(vacuous[behaviour.id]))
    failures = [resolution for resolution in resolutions.get(behaviour.id, []) if not resolution.ok]
    if failures:
        return failures[0].detail
    return "binding references resolve but name no proving test"


def render_report(audit_result: BindingAudit) -> str:
    """The generated ``docs/product-map/unbound-behaviours.md``."""
    lines = [
        "# Unbound behaviours",
        "",
        "Generated by `scripts/run_behaviour_bindings_baseline.py --write-report`. Do not edit by hand.",
        "",
        "Every behaviour below is ticked but cannot be bound to a test that resolves and is not",
        "vacuous. Each is frozen in `backend/tests/architecture/behaviour_bindings_baseline.json`, so",
        "the list can only shrink: binding one of them (the behaviour's marker names the test) fails",
        "the stale-entry check until the entry is removed from the baseline.",
        "",
        "A behaviour whose intended test genuinely does not exist is a real coverage gap, not a",
        "paperwork gap. Write the test and bind it; do not delete the behaviour.",
        "",
        f"Bound today: {audit_result.bound_count} of {audit_result.ticked_count} ticked behaviours.",
        "",
    ]
    by_entry: dict[str, list[tuple[str, str, str]]] = {}
    for behaviour_id, info in audit_result.unbound.items():
        by_entry.setdefault(info["entry"], []).append((behaviour_id, info["reason"], ""))
    behaviours_by_id = {behaviour.id: behaviour for entry in audit_result.entries for behaviour in entry.behaviours}
    for entry_rel in sorted(by_entry):
        lines.extend([f"## {entry_rel}", "", f"Derived status: `{audit_result.derived.get(entry_rel, 'gap')}`", ""])
        for behaviour_id, reason, _ in sorted(by_entry[entry_rel]):
            behaviour = behaviours_by_id[behaviour_id]
            lines.append(f"- `{behaviour_id}` - {behaviour.headline}")
            lines.append(f"  - why: {reason}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(root: Path, *, write_baseline: bool, write_report: bool) -> tuple[int, int]:
    """Regenerate the baseline and/or report; returns ``(bound, unbound)``."""
    audit_result = audit(root)
    if write_baseline:
        (root / BASELINE_RELPATH).write_text(render_baseline(audit_result.unbound), encoding="utf-8", newline="\n")
    if write_report:
        (root / REPORT_RELPATH).write_text(render_report(audit_result), encoding="utf-8", newline="\n")
    return audit_result.bound_count, len(audit_result.unbound)


def baseline_loosenings(base_ids: set[str], current_ids: set[str]) -> list[str]:
    """Ids a change ADDED to the baseline: the only way the ratchet can loosen."""
    return sorted(current_ids - base_ids)


def _git_show(root: Path, rev: str, relpath: str) -> str | None:
    """The file at *rev*; ``None`` when the path is absent at that revision.

    A revision that cannot be read is a hard failure: an unreadable base must
    never look like a pass (the same fail-closed rule
    ``scripts/run_check_ratchet_baseline.py`` applies).
    """
    if not rev or rev.startswith("-"):
        raise ValueError(f"refusing to pass {rev!r} to git as a revision")
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if exists.returncode != 0:
        raise ValueError(f"base revision {rev!r} does not resolve to a commit")
    shown = subprocess.run(
        ["git", "show", f"{rev}:{relpath}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if shown.returncode != 0:
        return None
    return shown.stdout


def check_base(root: Path, rev: str) -> int:
    """Fail when this change adds a baseline entry relative to *rev*.

    ``test_baseline_can_only_shrink`` catches the accidental case (an entry that
    has since become bound). This is the deliberate case: unbinding a behaviour
    and freezing it in the same change. The baseline is a shrink-only ratchet, so
    an added id is a loosening whatever else the change does.
    """
    relpath = BASELINE_RELPATH
    try:
        base_text = _git_show(root, rev, relpath)
    except ValueError as exc:
        print(f"behaviour-bindings baseline: FAILED CLOSED - {exc}", file=sys.stderr)
        return 1
    if base_text is None:
        print(f"behaviour-bindings baseline: OK - {relpath} does not exist at {rev}, so this change introduces the ratchet.", file=sys.stderr)
        return 0
    try:
        base_payload = json.loads(base_text)
        base_ids = set(base_payload.get("entries") or {})
    except (ValueError, AttributeError) as exc:
        print(f"behaviour-bindings baseline: FAILED CLOSED - cannot parse {relpath} at {rev}: {exc}", file=sys.stderr)
        return 1
    current_ids = set(load_baseline(root))
    added = baseline_loosenings(base_ids, current_ids)
    if added:
        print(
            f"behaviour-bindings baseline: FAILED - {relpath} is a shrink-only ratchet and this change ADDS {len(added)} entr(y/ies) relative to {rev}:\n"
            + "\n".join(f"  ADDED {behaviour_id}" for behaviour_id in added)
            + "\nBind the behaviour to a test instead of freezing it (docs/product-map/README.md).",
            file=sys.stderr,
        )
        return 1
    print(f"behaviour-bindings baseline: OK - no entry added relative to {rev} ({len(base_ids)} at base, {len(current_ids)} now).", file=sys.stderr)
    return 0


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Product-map behaviour bindings: backfill markers, regenerate the baseline and report.")
    parser.add_argument("--root", default=None, help="Repository root (defaults to the parent of this script's directory).")
    parser.add_argument("--write-markers", action="store_true", help="Add a marker to every behaviour bullet that lacks one.")
    parser.add_argument("--write-baseline", action="store_true", help=f"Regenerate {BASELINE_RELPATH}.")
    parser.add_argument("--write-report", action="store_true", help=f"Regenerate {REPORT_RELPATH}.")
    parser.add_argument("--all", action="store_true", help="--write-markers --write-baseline --write-report.")
    parser.add_argument("--check-base", default=None, metavar="REV", help=f"Fail when this change ADDS an entry to {BASELINE_RELPATH} relative to REV (shrink-only ratchet).")
    args = parser.parse_args(argv)

    if args.all:
        args.write_markers = args.write_baseline = args.write_report = True
    if not (args.write_markers or args.write_baseline or args.write_report or args.check_base):
        parser.error("nothing to do: pass --write-markers, --write-baseline, --write-report, --check-base or --all")

    root = Path(args.root).resolve() if args.root else _repo_root()
    if args.write_markers:
        written = assign_markers(root)
        print(f"added markers to {sum(written.values())} behaviour(s) across {len(written)} entr(y/ies)")
    if args.write_baseline or args.write_report:
        bound, unbound = write_outputs(root, write_baseline=args.write_baseline, write_report=args.write_report)
        print(f"bound {bound} ticked behaviour(s); {unbound} unbound")
    if args.check_base:
        return check_base(root, args.check_base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
