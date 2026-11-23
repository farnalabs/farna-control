"""Architecture gate: the design-system card/header contract cannot drift (FAR-2710).

The 2026-11-23 design-system audit found views and components hand-rolling card
shells, card headers and raw PrimeVue dialogs instead of composing the shared
primitives in ``frontend/src/components/ui`` and ``frontend/src/components/shared``.
Prose alone did not stop the drift. This gate gives it the same structural
enforcement ``appselect-guard.spec.ts`` gives native ``<select>``: a scan over
``frontend/src/**/*.vue`` that fails CI on a new offender. It runs here in the
architecture suite on backend-heavy PRs, every push to main and
``workflow_dispatch``, and it is mirrored by
``frontend/src/__tests__/design-system-guard.spec.ts``, which runs in the
frontend unit suite on every frontend-touching PR - so a frontend-only PR that
introduces drift is caught at PR time, not only after merge. Keep the two scans
and their counts in step: they read the same baseline.

Two rules
---------
1. **Absolute rule (no allowlist).** A ``DataCard`` or ``SectionCard`` opening
   tag that carries BOTH a title attribute and ``padding="none"`` fails. That
   is the exact defect FAR-2694 fixed: the card renders its own header, so the
   padding override leaves that header flush against the card edge.
2. **Shrinking ratchet.** Three objective drift fingerprints are counted per
   file and compared with the checked-in baseline
   ``frontend/design-system-baseline.json``. A file's count may equal or fall
   below its baseline; it may never exceed it. A baseline entry whose count has
   fallen to ZERO must be deleted (the ratchet only shrinks). A reduction that
   has not yet reached zero does not fail, but is emitted as a warning so it is
   visible in the test output. A file absent from the baseline has baseline 0.

The three ratchet patterns (one-line example each)
--------------------------------------------------
* ``card_container`` - a hand-rolled card shell: ``<div class="rounded-lg border border-border p-4">``.
  Matches a ``<div>`` whose class carries the standalone ``card`` token, or
  carries a ``rounded``/``rounded-*`` token AND a ``border``/``border-*`` token
  AND a ``p-<n>``/``p-px``/``p-[...]`` padding token.
* ``card_header`` - a hand-rolled CardHeader title: ``<h3 class="text-base font-semibold">``.
  Matches an ``<h2>``/``<h3>`` whose class carries ``font-semibold`` together
  with ``text-base`` or ``text-lg``.
* ``raw_dialog`` - a raw PrimeVue dialog import: ``import Dialog from 'primevue/dialog'``.
  Any import form of the module (default, named, namespace, aliased or bare)
  matches.

Anti-false-positive discipline: these patterns are deliberately objective, not
semantic. A legitimate design-call that happens to match (canvas nodes, diff
tables, auth cards) stays in the baseline unchanged - the ratchet exists to stop
NEW drift, not to relitigate the audit. Files under ``frontend/src/components/ui``
and ``frontend/src/components/shared`` (the primitives themselves), plus
``frontend/src/components/PageTabs.vue`` and ``frontend/src/__tests__`` are out
of scope by construction.

Regenerating the baseline after a sweep
---------------------------------------
``frontend/design-system-baseline.json`` is generated from the current tree with
only non-zero entries, sorted deterministically. After a sweep lowers counts,
re-run the generator and delete entries that reached zero::

    cd backend && uv run python - <<'PY'
    import json
    from pathlib import Path
    from tests.architecture.test_design_system_conformance import BASELINE_PATH, collect_counts

    BASELINE_PATH.write_text(json.dumps(collect_counts(), indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    PY
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
FRONTEND_SRC: Final = REPO_ROOT / "frontend" / "src"
BASELINE_PATH: Final = REPO_ROOT / "frontend" / "design-system-baseline.json"

#: The shared primitives (and their own tests) are the target state, not drift.
EXCLUDED_DIRS: Final = (
    FRONTEND_SRC / "components" / "ui",
    FRONTEND_SRC / "components" / "shared",
    FRONTEND_SRC / "__tests__",
)
EXCLUDED_FILES: Final = frozenset({FRONTEND_SRC / "components" / "PageTabs.vue"})

#: Every ratchet fingerprint, in a stable order for deterministic reporting.
PATTERNS: Final = ("card_container", "card_header", "raw_dialog")

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
#: ``(?<![-\w])`` keeps ``title-class``/``header-class``/``card-class`` values
#: out of the token set; only a real ``class`` (or ``:class``) attribute counts.
_CLASS_ATTR = re.compile(r"""(?<![-\w])(?::class|class)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.DOTALL)
_CLASS_TOKEN_SEPARATORS = re.compile(r"[\s,]+")
_CLASS_TOKEN_NOISE = re.compile(r"""[\[\]'"`{}()]""")
_PADDING_TOKEN = re.compile(r"p-(?:\d+(?:\.\d+)?|px|\[[^\]]+\])")
_TITLE_ATTR = re.compile(r"(?<![-\w])title\s*=")
_PADDING_NONE_LITERAL = 'padding="none"'
_PADDING_NONE = re.compile(r"""(?<![-\w])padding\s*=\s*(?:"none"|'none'|"'none'"|'"none"')""")
_RAW_DIALOG_IMPORT = re.compile(r"""^\s*import\s+(?:[^\n]*?\bfrom\s+)?["']primevue/dialog["']""", re.MULTILINE)


def _scanned_files() -> list[Path]:
    """Every in-scope ``.vue`` file, sorted for deterministic failures."""
    files: list[Path] = []
    for path in sorted(FRONTEND_SRC.rglob("*.vue")):
        if path in EXCLUDED_FILES:
            continue
        if any(excluded == path or excluded in path.parents for excluded in EXCLUDED_DIRS):
            continue
        files.append(path)
    return files


def _opening_tags(text: str, names: tuple[str, ...]) -> list[str]:
    """Return every ``<name ...>`` opening tag, quote-aware so ``>`` in a value is safe."""
    tags: list[str] = []
    pattern = re.compile(r"<(?:" + "|".join(names) + r")(?=[\s/>])", re.IGNORECASE)
    for match in pattern.finditer(text):
        index = match.end()
        quote: str | None = None
        while index < len(text):
            char = text[index]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in {'"', "'"}:
                quote = char
            elif char == ">":
                break
            index += 1
        tags.append(text[match.start() : index + 1])
    return tags


def _class_tokens(tag: str) -> set[str]:
    """Split every static/dynamic ``class`` value on the tag into bare tokens."""
    tokens: set[str] = set()
    for match in _CLASS_ATTR.finditer(tag):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        for token in _CLASS_TOKEN_SEPARATORS.split(_CLASS_TOKEN_NOISE.sub(" ", raw)):
            if token:
                tokens.add(token)
    return tokens


def _is_card_container(tag: str) -> bool:
    tokens = _class_tokens(tag)
    if "card" in tokens:
        return True
    rounded = any(token == "rounded" or token.startswith("rounded-") for token in tokens)
    bordered = any(token == "border" or token.startswith("border-") for token in tokens)
    padded = any(_PADDING_TOKEN.fullmatch(token) for token in tokens)
    return rounded and bordered and padded


def _is_card_header(tag: str) -> bool:
    tokens = _class_tokens(tag)
    weighted = "font-semibold" in tokens
    sized = "text-base" in tokens or "text-lg" in tokens
    return weighted and sized


def _count_patterns(text: str) -> dict[str, int]:
    """Count the three ratchet fingerprints in one ``.vue`` file."""
    stripped = _HTML_COMMENT.sub("", text)
    return {
        "card_container": sum(1 for tag in _opening_tags(stripped, ("div",)) if _is_card_container(tag)),
        "card_header": sum(1 for tag in _opening_tags(stripped, ("h2", "h3")) if _is_card_header(tag)),
        "raw_dialog": len(_RAW_DIALOG_IMPORT.findall(stripped)),
    }


def collect_counts() -> dict[str, dict[str, int]]:
    """Per-pattern, per-file counts with non-zero entries only (the baseline shape)."""
    counts: dict[str, dict[str, int]] = {pattern: {} for pattern in PATTERNS}
    for path in _scanned_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for pattern, count in _count_patterns(path.read_text(encoding="utf-8")).items():
            if count:
                counts[pattern][relative] = count
    return {pattern: dict(sorted(counts[pattern].items())) for pattern in PATTERNS}


def _load_baseline() -> dict[str, dict[str, int]]:
    """Read and validate the checked-in baseline JSON."""
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError(f"{BASELINE_PATH} must be a JSON object keyed by pattern")
    unknown = sorted(set(raw) - set(PATTERNS))
    if unknown:
        raise AssertionError(f"{BASELINE_PATH} names unknown pattern(s): {unknown}; expected {list(PATTERNS)}")
    baseline: dict[str, dict[str, int]] = {pattern: {} for pattern in PATTERNS}
    for pattern in PATTERNS:
        entries = raw.get(pattern, {})
        if not isinstance(entries, dict):
            raise AssertionError(f"{BASELINE_PATH} pattern {pattern!r} must map file paths to counts")
        for path, count in entries.items():
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise AssertionError(f"{BASELINE_PATH} entry {pattern}/{path} must be a positive integer, got {count!r}")
            baseline[pattern][path] = count
    return baseline


def _failure(title: str, details: list[str]) -> str:
    """Join a headline with its indented per-file detail lines."""
    return "\n".join([title, *details])


def _titled_cards_with_padding_none(text: str) -> list[str]:
    """DataCard/SectionCard opening tags that combine a title with padding="none"."""
    matches: list[str] = []
    for tag in _opening_tags(_HTML_COMMENT.sub("", text), ("DataCard", "SectionCard")):
        if _TITLE_ATTR.search(tag) is None:
            continue
        if _PADDING_NONE.search(tag) is None:
            continue
        matches.append(" ".join(tag.split()))
    return matches


def test_titled_card_padding_detector_fires_on_inline_samples() -> None:
    """Positive control: the absolute rule must flag a titled padding="none" card."""
    assert _titled_cards_with_padding_none('<DataCard title="x" padding="none" />')
    assert _titled_cards_with_padding_none("""<SectionCard title='x' :padding="'none'" />""")
    assert not _titled_cards_with_padding_none('<!-- <DataCard title="x" padding="none" /> -->')
    assert not _titled_cards_with_padding_none('<DataCard padding="none" />')
    assert not _titled_cards_with_padding_none('<DataCard title="x" padding="sm" />')


def test_raw_dialog_import_detector_matches_every_import_form() -> None:
    """Positive control: every raw module import form is counted, nothing else is."""
    assert _count_patterns("import Dialog from 'primevue/dialog'")["raw_dialog"] == 1
    assert _count_patterns("import * as Dialog from 'primevue/dialog'")["raw_dialog"] == 1
    assert _count_patterns("import { Dialog } from 'primevue/dialog'")["raw_dialog"] == 1
    assert _count_patterns("import X from 'primevue/button'")["raw_dialog"] == 0


def test_no_titled_card_overrides_its_padding_to_none() -> None:
    """A DataCard/SectionCard with a title must not also set padding="none"."""
    offenders: list[str] = []
    for path in _scanned_files():
        for one_line in _titled_cards_with_padding_none(path.read_text(encoding="utf-8")):
            offenders.append(f"  {path.relative_to(REPO_ROOT).as_posix()}: {one_line}")
    title = f"A DataCard/SectionCard combines a title with {_PADDING_NONE_LITERAL}. The card renders its own header, so the override leaves that header flush against the card edge (the FAR-2694 defect). Drop the padding override, or use the primitive's own padding scale instead:"
    assert not offenders, _failure(title, offenders)


def test_design_system_drift_never_exceeds_the_baseline() -> None:
    """The ratchet: no file grows, and resolved entries shrink to deletion."""
    baseline = _load_baseline()
    actual = collect_counts()

    regressions: list[str] = []
    reductions: list[str] = []
    zeroed: list[str] = []

    for pattern in PATTERNS:
        for path, count in actual[pattern].items():
            allowed = baseline[pattern].get(path, 0)
            if count > allowed:
                kind = "new" if allowed == 0 else "grew"
                regressions.append(f"  {pattern}: {path}: {count} > {allowed} ({kind})")
        for path, allowed in baseline[pattern].items():
            count = actual[pattern].get(path, 0)
            if count == 0:
                zeroed.append(f"  {pattern}: {path}: {allowed} -> 0")
            elif count < allowed:
                reductions.append(f"  {pattern}: {path}: {allowed} -> {count}")

    baseline_name = BASELINE_PATH.relative_to(REPO_ROOT).as_posix()
    for line in reductions:
        warnings.warn(
            f"Design-system baseline can shrink further (not a failure): lower or remove this entry in {baseline_name}:\n{line}",
            stacklevel=1,
        )

    grew_title = f"A file gained more hand-rolled card/header/dialog drift than {baseline_name} allows. Use the shared primitives (DataCard/SectionCard/CardHeader and the shared dialog wrapper) instead of raising the baseline:"
    zero_title = f"A baseline entry has fallen to zero, so the ratchet must shrink. Delete these entries from {baseline_name} (the sweep has landed, or the pattern was renamed - fix the baseline rather than leaving a stale allowance):"
    assert not regressions, _failure(grew_title, regressions)
    assert not zeroed, _failure(zero_title, zeroed)
