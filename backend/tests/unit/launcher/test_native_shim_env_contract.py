"""FAR-2564: every env name a native shim SETS must be a name Python READS.

Third occurrence of one drift class. The rebrand moved the readers onto the
``FARNA_CONTROL_`` prefix but left the native launcher shims exporting the
pre-rebrand ``MODULO_`` names, and a launcher export nothing reads is silent:
the value is dropped and the app falls back to its default.

* FAR-2554 — ``MODULO_BUNDLED_BIN_DIR``: the resolver never saw the bin dir,
  so first boot looked in ``<sys.prefix>/bundled/bin`` and failed.
* FAR-2561 — the app database name: the API connected to a database first
  boot never created.
* FAR-2564 — ``MODULO_SERVE_SPA`` / ``MODULO_FRONTEND_DIST``: ``main.py`` read
  ``FARNA_CONTROL_*``, kept the API mount at ``/``, and the installed app
  answered the SPA's root URL with API JSON (release recut 44261486386,
  ``spa_root_html=FAILED``). The macOS shim exported the pre-rebrand spelling;
  the Linux bundle launcher exported NEITHER name, so the shipped
  ``linux-amd64`` bundle had the same symptom with no drift to blame — a
  missing half of the contract is invisible to a "sets a name nobody reads"
  guard alone. That is why the Linux launcher's SPA exports are now asserted
  by name AND value below.

Each fix closed one pair. This guard closes the class: it derives the set of
names the real shims SET and the set the real Python code READS, then fails on
any name a shim exports that nothing consumes. Adding a new shim variable, or
deleting a Python read, breaks it.

Only names in the project namespace (``FARNA_CONTROL_`` / ``MODULO_``) are
compared: OS names such as ``PATH``/``PYTHONPATH``/``TEMP`` are infrastructure,
not product configuration.

Two exempt classes, both explicit and both required to state a reason:

* **Shell-only installer surface** — a name ``install.sh`` exports for shell
  integrations with no Python consumer (and none wanted).
* **Intentional legacy aliases** — a ``MODULO_`` name kept beside its
  ``FARNA_CONTROL_`` canonical name for older shell integrations. Because the
  canonical namespace is ``FARNA_CONTROL_``, ANY ``MODULO_`` name a shim sets
  must be allowlisted here, even if some Python code happens to read it, and
  its canonical counterpart must still be set — so a new pre-rebrand export
  cannot slip in behind an existing read.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

# The project namespace. The canonical side is listed first deliberately.
_NAMESPACED = r"(?:FARNA_CONTROL|MODULO)_[A-Z0-9_]+"

_MACOS_BUNDLE = "scripts/macos/build-native-bundle.sh"
_WINDOWS_LAUNCHER = "scripts/windows/modulo-launcher.c"
_LINUX_BUNDLE = ".github/workflows/bundle-release.yml"
_LINUX_INSTALLER = "scripts/install.sh"

PYTHON_SOURCE_DIR = "backend/src"

#: Shim-set names that legitimately have no Python reader, or that keep a
#: legacy spelling on purpose. EVERY entry states why; the guard fails when an
#: entry is no longer set anywhere (a stale entry would hide the next drift).
_ALLOWED_UNREAD_SHIM_NAMES: dict[str, str] = {
    "FARNA_CONTROL_BIN_DIR": ("install.sh's shim-directory export, written into the generated activation file (env.sh) for shell integrations. It is a shell install-path input: no Python code consumes it and none should, so there is nothing to rename and no reader to add."),
    "MODULO_INSTALL_ROOT": ("Intentional compatibility alias, exported beside FARNA_CONTROL_INSTALL_ROOT in the generated activation file for older shell integrations (documented at its export site). Python reads only the FARNA_CONTROL_ canonical name."),
    "MODULO_BIN_DIR": ("Intentional compatibility alias, exported beside FARNA_CONTROL_BIN_DIR in the generated activation file for older shell integrations (documented at its export site). Python reads neither name; the canonical one exists for shell consumers."),
}


def _text(relative_path: str) -> str:
    path = REPO_ROOT / relative_path
    assert path.is_file(), relative_path
    return path.read_text(encoding="utf-8")


def _macos_launcher_source(bundle_script: str) -> str:
    """The ``launcher.c`` heredoc body exactly as the bundle build compiles it."""
    lines = bundle_script.splitlines()
    opener = next(line for line in lines if "launcher.c" in line and "<<'EOF'" in line)
    start = bundle_script.index(opener) + len(opener) + 1
    end = bundle_script.index("\nEOF\n", start)
    return bundle_script[start:end] + "\n"


def _linux_launcher_source(workflow: str) -> str:
    """The ``launcher`` heredoc body exactly as the Linux bundle ships it.

    The heredoc lives inside a YAML block scalar, so every body line carries
    that scalar's indentation (and the terminator is indented too). Strip the
    common prefix the YAML block indicator removes before the shell runs, so
    the result is the file written to ``$BUNDLE/launcher``.
    """
    match = re.search(r"<<'LAUNCHER'\n(?P<body>.*?)\n[ \t]*LAUNCHER[ \t]*\n", workflow, re.DOTALL)
    assert match is not None, "launcher heredoc not found"
    lines = match.group("body").splitlines()
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    assert indents, "launcher heredoc body is empty"
    strip = min(indents)
    return "\n".join(line[strip:] for line in lines) + "\n"


def _names_set_by_macos_launcher(text: str) -> set[str]:
    """``setenv("NAME", ...)`` inside the launcher the build compiles."""
    return set(re.findall(rf'setenv\("({_NAMESPACED})"', _macos_launcher_source(text)))


def _names_set_by_windows_launcher(text: str) -> set[str]:
    """Wide-string literals the launcher writes into the child environment.

    The inherited-allowlist entries are also ``L"..."`` literals, but they are
    OS names outside the project namespace, so the pattern's namespace filter
    keeps them out.
    """
    return set(re.findall(rf'L"({_NAMESPACED})"', text))


def _names_exported_by_linux_bundle(text: str) -> set[str]:
    """``export NAME=`` lines in the bundle launcher wrapper heredoc."""
    return set(re.findall(rf"^\s*export\s+({_NAMESPACED})=", text, re.MULTILINE))


def _names_set_by_linux_installer(text: str) -> set[str]:
    """Names install.sh puts into an environment.

    Two shapes: the ``export`` lines (including the ones it bakes into the
    generated activation file) and the command-scoped assignments it prefixes
    onto a helper invocation.
    """
    exported = set(re.findall(rf"^\s*export\s+({_NAMESPACED})=", text, re.MULTILINE))
    command_scoped = set(re.findall(rf"^\s+({_NAMESPACED})=", text, re.MULTILINE))
    return exported | command_scoped


_SHIM_EXTRACTORS: dict[str, Callable[[str], set[str]]] = {
    _MACOS_BUNDLE: _names_set_by_macos_launcher,
    _WINDOWS_LAUNCHER: _names_set_by_windows_launcher,
    _LINUX_BUNDLE: _names_exported_by_linux_bundle,
    _LINUX_INSTALLER: _names_set_by_linux_installer,
}

# Python read shapes: a literal passed to an env lookup, or a module-level
# ``*_ENV`` constant whose value is a namespaced name AND which is consumed by
# an env lookup somewhere in the source tree.
_LITERAL_READ_PATTERNS: tuple[str, ...] = (
    rf'os\.environ\.get\(\s*"({_NAMESPACED})"',
    rf'os\.environ\[\s*"({_NAMESPACED})"\s*\]',
    rf'os\.getenv\(\s*"({_NAMESPACED})"',
)

# A declaration alone is NOT a read: the constant must be consumed by a lookup
# (that is what a "read" means here, and it is what the drift removed).
_ENV_CONSTANT_PATTERN = rf'^\s*(?P<ident>[A-Za-z_][A-Za-z0-9_]*_ENV)\s*(?::[^=\n]+)?=\s*"(?P<name>{_NAMESPACED})"'


def _has_consuming_reference(identifier: str, corpus: str) -> bool:
    ident = re.escape(identifier)
    patterns = (
        rf"\.get\(\s*{ident}\b",
        rf"\.pop\(\s*{ident}\b",
        rf"\.setdefault\(\s*{ident}\b",
        rf"os\.getenv\(\s*{ident}\b",
        rf"environ\[\s*{ident}\s*\]",
    )
    return any(re.search(pattern, corpus) for pattern in patterns)


def shim_set_names() -> dict[str, set[str]]:
    """Every namespaced env name each real shim file sets, per file."""
    return {relative_path: extractor(_text(relative_path)) for relative_path, extractor in _SHIM_EXTRACTORS.items()}


def python_read_names() -> set[str]:
    """Every namespaced env name the real Python sources read."""
    sources = [path.read_text(encoding="utf-8") for path in sorted((REPO_ROOT / PYTHON_SOURCE_DIR).rglob("*.py"))]
    assert sources, f"no Python sources found under {PYTHON_SOURCE_DIR}"
    corpus = "\n".join(sources)
    reads: set[str] = set()
    for text in sources:
        for pattern in _LITERAL_READ_PATTERNS:
            reads |= set(re.findall(pattern, text))
        for identifier, name in re.findall(_ENV_CONSTANT_PATTERN, text, re.MULTILINE):
            if _has_consuming_reference(identifier, corpus):
                reads.add(name)
    return reads


def contract_violations(
    shim_names: Mapping[str, set[str]],
    python_reads: set[str],
    allowed: Mapping[str, str] = _ALLOWED_UNREAD_SHIM_NAMES,
) -> list[str]:
    """Every name a shim sets that Python does not read, unless exempted."""
    violations: list[str] = []
    set_anywhere = set().union(*shim_names.values()) if shim_names else set()
    for relative_path, names in shim_names.items():
        for name in sorted(names):
            if name.startswith("MODULO_"):
                if name not in allowed:
                    violations.append(f"{relative_path} sets {name!r}: the canonical namespace is FARNA_CONTROL_; a pre-rebrand MODULO_ export must be an allowlisted compatibility alias with a stated reason")
                continue
            if name not in python_reads and name not in allowed:
                violations.append(f"{relative_path} sets {name!r} but no Python code reads it")
    for name in sorted(allowed):
        if name not in set_anywhere:
            violations.append(f"allowlist entry {name!r} is stale: no shim sets it any more")
        if name.startswith("MODULO_"):
            canonical = f"FARNA_CONTROL_{name[len('MODULO_') :]}"
            if canonical not in set_anywhere:
                violations.append(f"legacy alias {name!r} is allowlisted but nothing sets its canonical {canonical!r}")
    return violations


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_every_shim_env_name_is_read_by_python() -> None:
    """No shim may set a name the application never reads."""
    violations = contract_violations(shim_set_names(), python_read_names())

    assert not violations, "\n".join(violations)


def test_shim_extractors_find_the_shipped_names() -> None:
    """Anti-vacuity: each extractor parses its real file, and the SPA pair is fixed.

    Without this, an extractor that silently matched nothing would let the
    guard pass while proving nothing.
    """
    sets = shim_set_names()

    assert sets[_MACOS_BUNDLE] == {
        "FARNA_CONTROL_BUNDLED_BIN_DIR",
        "FARNA_CONTROL_FRONTEND_DIST",
        "FARNA_CONTROL_SERVE_SPA",
    }
    assert sets[_WINDOWS_LAUNCHER] == {"FARNA_CONTROL_BUNDLED_BIN_DIR"}
    assert sets[_LINUX_BUNDLE] == {
        "FARNA_CONTROL_BUNDLED_BIN_DIR",
        "FARNA_CONTROL_FRONTEND_DIST",
        "FARNA_CONTROL_SERVE_SPA",
    }
    assert sets[_LINUX_INSTALLER] == {
        "FARNA_CONTROL_BIN_DIR",
        "FARNA_CONTROL_BUNDLED_BIN_DIR",
        "FARNA_CONTROL_INSTALL_ROOT",
        "MODULO_BIN_DIR",
        "MODULO_INSTALL_ROOT",
    }


def test_linux_launcher_serves_the_bundled_spa() -> None:
    """The Linux bundle launcher must export the SPA contract, by name and value.

    FAR-2564 gap: the shipped ``linux-amd64`` launcher exported ``PATH`` /
    ``PYTHONPATH`` / ``FARNA_CONTROL_BUNDLED_BIN_DIR`` and NO SPA variables,
    even though the bundle ships ``frontend-dist``. ``farna.api.main`` then kept
    the API mount at ``/`` and the web UI never loaded. A set-membership check
    alone would still pass if the VALUE pointed at a directory nothing stages,
    so pin the exact export lines and prove they agree with the staging step.
    """
    workflow = _text(_LINUX_BUNDLE)
    launcher = _linux_launcher_source(workflow)

    assert "export FARNA_CONTROL_SERVE_SPA=1\n" in launcher
    assert 'export FARNA_CONTROL_FRONTEND_DIST="$BUNDLE_ROOT/frontend-dist"\n' in launcher

    # The comment must keep naming the Python reader and the symptom, so the
    # next reader can find the consumer without re-deriving it.
    assert "farna.api.main" in launcher
    assert "_SERVE_SPA_ENV" in launcher
    assert "_FRONTEND_DIST_ENV" in launcher

    # The exported path must be the directory the assembly step actually
    # stages, not merely a relative name that looks right.
    staged = re.search(r'mkdir -p "\$BUNDLE/(?P<dir>[^"]+)"\n\s*cp -a dist/\. "\$BUNDLE/(?P=dir)/"', workflow)
    assert staged is not None, "frontend dist staging step not found"
    exported = re.search(r'export FARNA_CONTROL_FRONTEND_DIST="\$BUNDLE_ROOT/(?P<dir>[^"]+)"', launcher)
    assert exported is not None, launcher

    assert exported.group("dir") == staged.group("dir")


def test_python_read_extractor_sees_the_spa_seam() -> None:
    """The SPA seam is a consumed ``*_ENV`` constant, so pin that read shape.

    ``main.py`` reads its two env names through module constants rather than
    inline ``os.environ`` literals; an extractor that only understood literals
    would report the seam as unread.
    """
    reads = python_read_names()

    assert "FARNA_CONTROL_SERVE_SPA" in reads
    assert "FARNA_CONTROL_FRONTEND_DIST" in reads
    assert "FARNA_CONTROL_BUNDLED_BIN_DIR" in reads
    assert "FARNA_CONTROL_INSTALL_ROOT" in reads
    assert "MODULO_SERVE_SPA" not in reads


# ---------------------------------------------------------------------------
# Mutation tests: prove the guard fails on the shapes it exists to catch.
# These feed the pure guard EXPLICIT inputs, so they pin the rule itself and do
# not depend on the shipped tree.
# ---------------------------------------------------------------------------

_SPA_NAMES = {"FARNA_CONTROL_FRONTEND_DIST", "FARNA_CONTROL_SERVE_SPA"}


def test_guard_rejects_a_pre_rebrand_spa_export() -> None:
    """The exact FAR-2564 defect: the launcher exports MODULO_SERVE_SPA."""
    shim_sets = {_MACOS_BUNDLE: {"MODULO_SERVE_SPA", "FARNA_CONTROL_FRONTEND_DIST"}}

    violations = contract_violations(shim_sets, _SPA_NAMES)

    assert any("MODULO_SERVE_SPA" in violation for violation in violations)


def test_guard_rejects_a_new_shim_variable_python_does_not_read() -> None:
    """A brand-new export with no reader must not ship silently."""
    shim_sets = {_WINDOWS_LAUNCHER: {"FARNA_CONTROL_UNREAD_EXPERIMENT"}}

    violations = contract_violations(shim_sets, _SPA_NAMES)

    assert any("FARNA_CONTROL_UNREAD_EXPERIMENT" in violation for violation in violations)


def test_guard_rejects_a_removed_python_read() -> None:
    """Deleting the read side re-creates the drift for the remaining export."""
    reads = _SPA_NAMES - {"FARNA_CONTROL_FRONTEND_DIST"}
    shim_sets = {_MACOS_BUNDLE: set(_SPA_NAMES)}

    violations = contract_violations(shim_sets, reads)

    assert any("FARNA_CONTROL_FRONTEND_DIST" in violation for violation in violations)


def test_guard_rejects_a_stale_allowlist_entry() -> None:
    """An exemption that no longer matches anything hides the next drift."""
    allowed = dict(_ALLOWED_UNREAD_SHIM_NAMES)
    allowed["MODULO_GONE_AWAY"] = "a stale exemption"

    violations = contract_violations({_MACOS_BUNDLE: set(_SPA_NAMES)}, _SPA_NAMES, allowed)

    assert any("MODULO_GONE_AWAY" in violation for violation in violations)


def test_guard_rejects_a_legacy_alias_without_its_canonical_name() -> None:
    """A compatibility alias must still travel with its canonical export."""
    allowed = {"MODULO_BIN_DIR": "legacy alias"}
    shim_sets = {_LINUX_INSTALLER: {"MODULO_BIN_DIR"}}

    violations = contract_violations(shim_sets, set(), allowed)

    assert any("FARNA_CONTROL_BIN_DIR" in violation for violation in violations)
