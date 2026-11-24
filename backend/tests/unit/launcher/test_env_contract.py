"""FAR-2563/FAR-2650: the canonical env contract, and the gate that stops it drifting.

Three separate incidents were one defect: a service-contract environment
variable was renamed on one side of the producer/consumer boundary and not the
other, so the value was written and never read.

* FAR-2554 - the pre-rebrand bin-dir name exported while the resolver read the
  canonical ``FARNA_CONTROL_BUNDLED_BIN_DIR``, so the boot fell through to a
  directory no bundle produces.
* FAR-2560/2561 - two ``APP_DB_NAME`` constants.
* FAR-2563 - the bundle launchers exported the pre-rebrand SPA-serving spelling
  while ``farna.api.main`` read the canonical ``FARNA_CONTROL_SERVE_SPA`` /
  ``FARNA_CONTROL_FRONTEND_DIST``, so the built SPA was never mounted.

The names live in one registry (:mod:`farna.launcher.env_contract`) and this
module is the structural gate over it. There is ONE namespace now: a service
script that writes or reads a name outside ``FARNA_CONTROL_*`` fails here, as
does a Python reader that takes a registered name by re-declaring it. A rename
on one side of a producer/consumer boundary therefore fails here rather than in
an installed app.

The patterns capture the canonical prefix and the check runs over the matched
name set, not the raw file text: a canonical name appearing only in a log line,
a comment or a wide-string literal that is not an argument to a write call must
never satisfy the gate (it was tried twice - an ``echo`` of the canonical name
masked an export that still used a stale spelling, and an unanchored
``L"NAME"`` pattern let a canonical name inside a ``/* comment */`` mask a C
producer that still wrote the stale name).

Comment stripping is language-aware (:func:`strip_comments`): C block comments
are removed from C sources only, because in shell ``/*`` is glob and
parameter-expansion syntax (``${FROM_FILE%/*}``, ``[[ "$target" = /* ]]``) and
stripping to the next ``*/`` deleted tens of live shell lines - hard gates
included - so an export inside that span passed the gate unseen (FAR-2563
review). Everywhere else only full-line ``#`` comments are dropped, so a name
in a ``#`` comment still cannot count as a write. A shell file that EMBEDS C is
scanned as shell text: ``scripts/macos/build-native-bundle.sh`` compiles its
launcher heredoc, and that build has its own gate over the compiled source
(``spa_env_contract_gate``), so the names are still held to the registry there.
A stripper that eats live code fails loudly:
``test_stripping_keeps_live_code`` pins the known load-bearing lines of each
guarded file against the stripped text.

The gate has three halves, all structural:

* producers - the service scripts (shell / C / Kubernetes) that supply process
  environment to the app;
* readers - the Python modules that consume it. A registered name must be read
  through :func:`farna.launcher.env_contract.read_env` (or, for the one
  documented single-name reader, the canonical literal) and must never be
  re-declared outside the registry;
* documentation - every ``FARNA_CONTROL_*`` knob ``docs/configuration-reference.md``
  publishes must be accepted by SOME mechanism: this registry, a
  ``farna.settings`` alias, or a literal read by ``farna/`` or by an explicitly
  listed out-of-package reader script. A documented spelling that nothing
  accepts is the FAR-2563 LAN-origins defect (a silent 403), so
  ``DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS`` remains as the explicit place to record
  a genuine gap: it is empty today (the burn-down is closed), and the companion
  test fails if an entry is stale, so it can never become a licence to pass.

Scope: the launcher / installer / container-entrypoint / Helm / API surface,
i.e. the places that supply process environment to the app directly and the
modules that read it.

Windows note: ``.github/workflows/windows-artifacts.yml`` DOES compile
``scripts/windows/modulo-launcher.c`` - on every push to ``main`` that touches
it, on a weekly schedule and on dispatch - so the C is compiled in CI. What the
default Alpha release path does not do is package or boot the Windows bundle:
``.github/workflows/release-unified.yml`` runs ``windows-native-artifacts`` and
``windows-installer`` only when ``include_windows=true``. A compile also cannot
compare the launcher's names with the names Python reads, so the anchored C
pattern below is the contract gate either way.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from farna.launcher import env_contract
from tests.helpers.env_prefixes import RETIRED_ENV_PREFIX

REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON_SOURCE_ROOT = REPO_ROOT / "backend" / "src" / "farna"
SETTINGS_MODULE = PYTHON_SOURCE_ROOT / "settings.py"
CONFIGURATION_REFERENCE = REPO_ROOT / "docs" / "configuration-reference.md"

#: The canonical namespace. The retired spelling comes from the one shared
#: helper (assembled there) so this file itself holds no bare legacy name for a
#: source sweep to trip over.
CANONICAL_PREFIX = env_contract.CANONICAL_PREFIX
LEGACY_PREFIX = RETIRED_ENV_PREFIX

#: Shell ``export NAME=...``.
SHELL_EXPORT_WRITE = r"^\s*export\s+({names})="
#: C ``setenv("NAME", ...)`` - the literal is the call's FIRST argument.
C_SETENV_WRITE = r'setenv\("({names})"'
#: Windows ``append_environment_entry(block, len, &used, L"NAME", value)`` -
#: anchored to the NAME argument (the fourth, after three comma-separated
#: arguments). An unanchored ``L"NAME"`` pattern also matched the inherited-env
#: allowlist and, worse, a ``/* comment */``, so a canonical name mentioned in
#: prose satisfied a producer that still wrote the stale name.
C_APPEND_ENV_WRITE = r'append_environment_entry\([^,()]+,\s*[^,()]+,\s*[^,()]+,\s*L"({names})"'
#: Kubernetes env entry ``- name: NAME``.
K8S_ENV_WRITE = r"^\s*-\s*name:\s+({names})\s*$"
#: Shell ``${NAME:-...}`` / ``${NAME}`` expansion.
SHELL_READ = r"\$\{({names})[:\}]"

#: Every service-contract name carries the canonical prefix; a stale one is a
#: gate failure, not a second accepted spelling.
_NAME_CLASS = r"FARNA_CONTROL_[A-Z0-9_]+"
_ANY_NAME_CLASS = rf"(?:{LEGACY_PREFIX}|FARNA_CONTROL_)[A-Z0-9_]+"
_ENV_NAME_RE = re.compile(rf"^(?:{LEGACY_PREFIX}|FARNA_CONTROL_)[A-Z0-9_]+$")
_LEGACY_NAME_RE = re.compile(r"^" + LEGACY_PREFIX + r"[A-Z0-9_]+$")


def _pattern(template: str) -> str:
    """Expand the ``{names}`` placeholder (plain replace - the templates contain regex braces)."""
    return template.replace("{names}", _NAME_CLASS)


#: A service script is any file that WRITES or READS an environment variable the
#: shipped app resolves. Each entry is deliberately explicit: adding a new
#: producer is a review gate in itself.
@dataclass(frozen=True)
class ServiceScript:
    relative_path: str
    writes: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()

    @property
    def path(self) -> Path:
        return REPO_ROOT / self.relative_path


_SERVICE_SCRIPTS: tuple[ServiceScript, ...] = (
    # macOS .app bundle: generates and compiles the C launcher.
    ServiceScript("scripts/macos/build-native-bundle.sh", writes=(_pattern(C_SETENV_WRITE),)),
    # Windows bundled launcher (``append_environment_entry(..., L"NAME", ...)``).
    ServiceScript("scripts/windows/modulo-launcher.c", writes=(_pattern(C_APPEND_ENV_WRITE),)),
    # Fly container entrypoint (promotes the /tmp env-file contract).
    ServiceScript(
        "deploy/fly/entrypoint.sh",
        writes=(_pattern(SHELL_EXPORT_WRITE),),
        reads=(_pattern(SHELL_READ),),
    ),
    # Linux tarball installer.
    ServiceScript(
        "scripts/install.sh",
        writes=(_pattern(SHELL_EXPORT_WRITE),),
        reads=(_pattern(SHELL_READ),),
    ),
    # macOS installer.
    ServiceScript("scripts/macos/install.sh", reads=(_pattern(SHELL_READ),)),
    # docker-compose smoke test (operator-facing knobs).
    ServiceScript("scripts/smoke-compose.sh", reads=(_pattern(SHELL_READ),)),
    # Helm chart pods: the env entries the app containers receive.
    ServiceScript("deploy/helm/farna-control/templates/backend-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
    ServiceScript("deploy/helm/farna-control/templates/saq-runner-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
    ServiceScript("deploy/helm/farna-control/templates/saq-system-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
)

#: The canonical names each producer is REQUIRED to write, in the same production
#: context the ``writes`` patterns above describe. This is the positive half of
#: the gate: an empty match set must never read as "all clean", which a
#: negative-only check cannot distinguish from a broken regex.
_REQUIRED_CANONICAL_PRODUCTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "scripts/macos/build-native-bundle.sh",
        _pattern(C_SETENV_WRITE),
        (env_contract.SERVE_SPA_ENV, env_contract.FRONTEND_DIST_ENV),
    ),
    (
        "scripts/windows/modulo-launcher.c",
        _pattern(C_APPEND_ENV_WRITE),
        (env_contract.SERVE_SPA_ENV, env_contract.FRONTEND_DIST_ENV),
    ),
    ("deploy/fly/entrypoint.sh", _pattern(SHELL_EXPORT_WRITE), (env_contract.SYSTEM_DATABASE_URL_ENV,)),
    (
        "scripts/install.sh",
        _pattern(SHELL_EXPORT_WRITE),
        (env_contract.INSTALL_ROOT_ENV, env_contract.INSTALL_BIN_DIR_ENV),
    ),
    (
        "scripts/macos/install.sh",
        _pattern(SHELL_READ),
        (env_contract.INSTALL_ROOT_ENV, env_contract.DATA_DIR_ENV),
    ),
    (
        "deploy/helm/farna-control/templates/backend-deployment.yaml",
        _pattern(K8S_ENV_WRITE),
        (env_contract.SYSTEM_DATABASE_URL_ENV,),
    ),
    (
        "deploy/helm/farna-control/templates/saq-system-deployment.yaml",
        _pattern(K8S_ENV_WRITE),
        (env_contract.SYSTEM_DATABASE_URL_ENV,),
    ),
)

#: C-family block comments (``/* ... */``) and full-line ``//`` comments.
_C_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_C_LINE_COMMENT_RE = re.compile(r"^[ \t]*//.*$", re.MULTILINE)
#: A FULL-LINE ``#`` comment (shell, YAML). Never an inline one: mid-line ``#``
#: is normal shell parameter syntax (``${VAR#prefix}``, ``${#array[@]}``).
_HASH_LINE_COMMENT_RE = re.compile(r"^[ \t]*#.*$", re.MULTILINE)
#: Extensions whose ``/* ... */`` really is a comment.
_C_SUFFIXES = frozenset({".c", ".h"})


def strip_comments(text: str, relative_path: str) -> str:
    """Remove only the comment syntax the file's OWN language has.

    Both directions of the gate match on the stripped text, so a name inside a
    comment can satisfy neither the drift check nor the positive
    "must be produced" check - the false pass an unanchored ``L"NAME"`` pattern
    allowed (FAR-2563 review).

    Stripping ``/* ... */`` from a SHELL script is data loss, not cleanup: in
    shell ``/*`` is glob and parameter-expansion syntax (``${FROM_FILE%/*}``,
    ``[[ "$target" = /* ]]``), so the block-comment regex deleted every live
    line up to the next ``*/`` - hard gates included - and an export inside the
    span passed the gate unseen (FAR-2563 review). C block comments are
    therefore stripped for C sources only; everywhere else only full-line ``#``
    comments are dropped, so a name in a shebang or a ``#`` comment cannot count
    as a write. (A shell file that embeds C is scanned as shell text; the macOS
    bundle's launcher heredoc is additionally gated at build time over the
    compiled source, so its names do not rely on this sweep alone.)
    """
    if Path(relative_path).suffix.lower() in _C_SUFFIXES:
        return _C_LINE_COMMENT_RE.sub("", _C_BLOCK_COMMENT_RE.sub("", text))
    return _HASH_LINE_COMMENT_RE.sub("", text)


def _matched_names(text: str, pattern: str, *, relative_path: str) -> set[str]:
    return set(re.findall(pattern, strip_comments(text, relative_path), re.MULTILINE))


def contract_problems(
    text: str,
    *,
    writes: tuple[str, ...] = (),
    reads: tuple[str, ...] = (),
    relative_path: str,
) -> list[str]:
    """Return every non-canonical production/consumption in ``text``.

    A name outside ``FARNA_CONTROL_*`` is a drift report: there is no second
    accepted spelling. ``relative_path`` selects the comment syntax stripped
    before matching (see :func:`strip_comments`); synthetic C text must say so,
    or its comments are treated as live code.
    """
    problems: list[str] = []
    for verb, patterns in (("writes", writes), ("reads", reads)):
        for pattern in patterns:
            stripped = strip_comments(text, relative_path)
            names = set(re.findall(_ANY_NAME_CLASS, stripped, re.MULTILINE))
            for name in sorted(names):
                if _LEGACY_NAME_RE.match(name):
                    problems.append(f"{verb} non-canonical name {name!r}: service-contract names must be FARNA_CONTROL_* (a rename is a clean cutover, not an alias)")
    return problems


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------

_REGISTRY_ENV_CONSTANTS: tuple[str, ...] = tuple(value for name, value in vars(env_contract).items() if name.endswith("_ENV") and isinstance(value, str))


def test_registry_names_are_canonical() -> None:
    assert _REGISTRY_ENV_CONSTANTS, "the registry must expose at least one *_ENV constant"
    for name in _REGISTRY_ENV_CONSTANTS:
        assert name.startswith(CANONICAL_PREFIX), name


def test_registry_has_no_legacy_alias_table() -> None:
    """The compatibility table is gone; reviving it would re-open the drift."""
    for attribute in ("LEGACY_ALIASES", "LEGACY_ONLY_EXEMPT", "LEGACY_PREFIX"):
        assert not hasattr(env_contract, attribute), f"env_contract.{attribute} must not return"


def test_candidate_names_is_the_single_name() -> None:
    for name in _REGISTRY_ENV_CONSTANTS:
        assert env_contract.candidate_names(name) == (name,)


# ---------------------------------------------------------------------------
# read_env
# ---------------------------------------------------------------------------


def test_read_env_reads_the_canonical_name() -> None:
    assert env_contract.read_env({env_contract.SERVE_SPA_ENV: "1"}, env_contract.SERVE_SPA_ENV) == "1"


def test_read_env_does_not_fall_back_to_a_legacy_name() -> None:
    legacy = LEGACY_PREFIX + env_contract.SERVE_SPA_ENV[len(CANONICAL_PREFIX) :]
    assert not env_contract.read_env({legacy: "1"}, env_contract.SERVE_SPA_ENV)


def test_read_env_returns_the_default_when_nothing_is_set() -> None:
    assert env_contract.read_env({}, env_contract.FRONTEND_DIST_ENV, "/none") == "/none"


def test_read_env_treats_an_empty_value_as_unset() -> None:
    assert env_contract.read_env({env_contract.SERVE_SPA_ENV: ""}, env_contract.SERVE_SPA_ENV, "default") == "default"


# ---------------------------------------------------------------------------
# The sweep (producer + reader)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", _SERVICE_SCRIPTS, ids=[script.relative_path for script in _SERVICE_SCRIPTS])
def test_service_script_has_no_non_canonical_env_drift(script: ServiceScript) -> None:
    text = script.path.read_text(encoding="utf-8")
    assert not contract_problems(text, writes=script.writes, reads=script.reads, relative_path=script.relative_path)


@pytest.mark.parametrize(
    ("relative_path", "pattern", "required"),
    _REQUIRED_CANONICAL_PRODUCTIONS,
    ids=[relative_path for relative_path, _pattern, _required in _REQUIRED_CANONICAL_PRODUCTIONS],
)
def test_producers_emit_the_canonical_names(relative_path: str, pattern: str, required: tuple[str, ...]) -> None:
    """Positive half: the canonical name is actually produced, not merely unopposed."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    produced = _matched_names(text, pattern, relative_path=relative_path)
    for canonical in required:
        assert canonical in produced, f"{relative_path} must produce {canonical}; produced={sorted(produced)}"


def test_the_sweep_sees_the_real_canonical_names_it_claims_to_check() -> None:
    """A sweep whose regexes match nothing must never read as clean."""
    install = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    produced = _matched_names(install, _pattern(SHELL_EXPORT_WRITE), relative_path="scripts/install.sh")
    assert {env_contract.INSTALL_ROOT_ENV, env_contract.INSTALL_BIN_DIR_ENV} <= produced

    macos_install = (REPO_ROOT / "scripts" / "macos" / "install.sh").read_text(encoding="utf-8")
    consumed = _matched_names(macos_install, _pattern(SHELL_READ), relative_path="scripts/macos/install.sh")
    assert {env_contract.INSTALL_ROOT_ENV, env_contract.DATA_DIR_ENV} <= consumed


def test_the_sweep_covers_the_windows_producer() -> None:
    """This sweep is the name-level contract gate for the Windows producer.

    ``.github/workflows/windows-artifacts.yml`` does compile
    ``modulo-launcher.c`` - on push to ``main`` that touches it, on a weekly
    schedule and on dispatch - but a compile cannot compare the names here with
    the names Python reads, and the default Alpha release path skips
    ``windows-native-artifacts`` / ``windows-installer`` unless
    ``include_windows=true``, so nothing packages or boots the Windows bundle
    there. The anchored pattern must therefore see exactly the two SPA names
    the launcher appends AND must not count the inherited-env allowlist
    (``L"SYSTEMROOT"`` and friends are names, but they are not writes).
    """
    launcher = (REPO_ROOT / "scripts" / "windows" / "modulo-launcher.c").read_text(encoding="utf-8")
    written = _matched_names(launcher, _pattern(C_APPEND_ENV_WRITE), relative_path="scripts/windows/modulo-launcher.c")

    # Exactly the three service-contract appends: the inherited-env allowlist
    # (L"SYSTEMROOT" and friends) is not a write, and the name class excludes
    # PATH/PYTHONPATH/PYTHONHOME.
    assert written == {
        "FARNA_CONTROL_BUNDLED_BIN_DIR",
        env_contract.SERVE_SPA_ENV,
        env_contract.FRONTEND_DIST_ENV,
    }, written


# ---------------------------------------------------------------------------
# Mutation proof: the gate actually fails on the incidents it was written for
# ---------------------------------------------------------------------------


def test_gate_flags_the_far_2563_launcher_drift() -> None:
    """Mutation: the exact pre-fix launcher body must be reported, not passed."""
    stale = LEGACY_PREFIX + "SERVE_SPA"
    stale_dist = LEGACY_PREFIX + "FRONTEND_DIST"
    pre_fix = f'setenv("PYTHONPATH", src, 1);\nsetenv("{stale}", "1", 1);\nsetenv("{stale_dist}", frontend_dist, 1);\n'
    problems = contract_problems(pre_fix, writes=(_pattern(C_SETENV_WRITE),), relative_path="scripts/macos/build-native-bundle.sh")

    assert len(problems) == 2, problems


def test_gate_flags_a_windows_write_that_uses_a_stale_name() -> None:
    """Mutation: the Windows shape, with pre-rebrand names, is drift."""
    stale = LEGACY_PREFIX + "SERVE_SPA"
    stale_dist = LEGACY_PREFIX + "FRONTEND_DIST"
    drifted = f'if (!append_environment_entry(block, LEN, &used, L"{stale}", L"1") ||\n    !append_environment_entry(block, LEN, &used, L"{stale_dist}", dist)) {{\n'
    problems = contract_problems(drifted, writes=(_pattern(C_APPEND_ENV_WRITE),), relative_path="synthetic.c")

    assert len(problems) == 2, problems


def test_gate_is_not_satisfied_by_canonical_names_inside_a_c_comment() -> None:
    """Mutation (reverse): naming the canon in a comment must not mask a stale write.

    This is the exact false pass the unanchored ``L"NAME"`` pattern allowed: the
    canonical names appeared in a ``/* comment */`` while the code still wrote
    the stale spelling, and both the drift check and
    ``test_producers_emit_the_canonical_names`` went green.
    """
    stale = LEGACY_PREFIX + "SERVE_SPA"
    stale_dist = LEGACY_PREFIX + "FRONTEND_DIST"
    masked = f'/* FARNA_CONTROL_SERVE_SPA FARNA_CONTROL_FRONTEND_DIST */\n/* setenv("FARNA_CONTROL_SERVE_SPA", "1", 1); */\nif (!append_environment_entry(block, LEN, &used, L"{stale}", L"1") ||\n    !append_environment_entry(block, LEN, &used, L"{stale_dist}", dist)) {{\n'
    writes = (_pattern(C_APPEND_ENV_WRITE),)

    problems = contract_problems(masked, writes=writes, relative_path="synthetic.c")
    assert len(problems) == 2, problems
    produced = _matched_names(masked, writes[0], relative_path="synthetic.c")
    assert env_contract.SERVE_SPA_ENV not in produced
    assert env_contract.FRONTEND_DIST_ENV not in produced
    # ...nor does a commented-out setenv() call count for the macOS shape.
    assert env_contract.SERVE_SPA_ENV not in _matched_names(masked, _pattern(C_SETENV_WRITE), relative_path="synthetic.c")


def test_a_shell_glob_span_is_not_treated_as_a_c_block_comment() -> None:
    """Mutation: the FAR-2563 review's false pass, reproduced exactly.

    In shell ``${FROM_FILE%/*}`` opens nothing and ``${bundle_dir##*/}`` closes
    nothing - both are parameter expansions. A block-comment stripper deleted
    every line between them, hard gates included, so an export in that span was
    invisible to the gate. This is the real install.sh shape, reduced.
    """
    shell = 'from_file_dir="${FROM_FILE%/*}"\nexport FARNA_CONTROL_DATA_DIR="/opt"\nbundle_dir_name="${bundle_dir##*/}"\n'
    stripped = strip_comments(shell, "scripts/install.sh")
    assert 'export FARNA_CONTROL_DATA_DIR="/opt"' in stripped

    produced = _matched_names(shell, _pattern(SHELL_EXPORT_WRITE), relative_path="scripts/install.sh")
    assert "FARNA_CONTROL_DATA_DIR" in produced


def test_a_shell_test_operator_is_not_treated_as_a_c_comment_opener() -> None:
    """The build-native-bundle.sh shape: ``[[ "$target" = /* ]]`` opened a span.

    The closing ``*/`` there is a real C comment terminator inside the launcher
    heredoc, so the old stripper swallowed everything between the two - the
    symlink-escape guard and the ``setenv`` lines with it.
    """
    shell = 'if [[ "$target" = /* ]]; then\n  setenv("FARNA_CONTROL_SERVE_SPA", "1", 1);\nfi\n/* launcher comment */\n'
    produced = _matched_names(shell, _pattern(C_SETENV_WRITE), relative_path="scripts/macos/build-native-bundle.sh")

    assert "FARNA_CONTROL_SERVE_SPA" in produced


#: Load-bearing lines a comment stripper must leave INTACT, per guarded file.
#: If a future stripper starts eating live code again (the FAR-2563 review's
#: ``/*``-in-shell defect), these disappear from the stripped text and this
#: table fails loudly instead of silently re-opening the false pass.
_STRIPPED_TEXT_MUST_KEEP: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "scripts/install.sh",
        (
            'from_file_dir="${FROM_FILE%/*}"',
            "the SIGNED release manifest must sit beside the tarball",
            'verify_shipped_manifest "${manifest_file}" "${signature_file}" "${FROM_FILE}" "bundle-v${VERSION_NUM}"',
            'export FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"',
            'export FARNA_CONTROL_BIN_DIR="${BIN_DIR}"',
        ),
    ),
    (
        "scripts/macos/build-native-bundle.sh",
        (
            '[[ "$target" = /* ]]',
            "symlink escapes app bundle",
            'setenv("FARNA_CONTROL_BUNDLED_BIN_DIR", bin_dir, 1);',
            'setenv("FARNA_CONTROL_SERVE_SPA", "1", 1);',
            'setenv("FARNA_CONTROL_FRONTEND_DIST", frontend_dist, 1);',
        ),
    ),
    (
        "scripts/windows/modulo-launcher.c",
        (
            'L"FARNA_CONTROL_BUNDLED_BIN_DIR"',
            'L"FARNA_CONTROL_SERVE_SPA"',
            'L"FARNA_CONTROL_FRONTEND_DIST"',
        ),
    ),
    (
        "deploy/fly/entrypoint.sh",
        ('export FARNA_CONTROL_SYSTEM_DATABASE_URL="$SYSTEM_URL"',),
    ),
)


@pytest.mark.parametrize(
    ("relative_path", "kept_lines"),
    _STRIPPED_TEXT_MUST_KEEP,
    ids=[relative_path for relative_path, _kept in _STRIPPED_TEXT_MUST_KEEP],
)
def test_stripping_keeps_live_code(relative_path: str, kept_lines: tuple[str, ...]) -> None:
    """Anti-regression: stripping may delete comments, never live code."""
    stripped = strip_comments((REPO_ROOT / relative_path).read_text(encoding="utf-8"), relative_path)
    missing = [line for line in kept_lines if line not in stripped]

    assert not missing, f"{relative_path}: comment stripping deleted live code: {missing}"


def test_a_commented_out_shell_export_is_not_a_production() -> None:
    """Full-line ``#`` comments are still stripped, so a commented export cannot pass."""
    text = '# export FARNA_CONTROL_INSTALL_ROOT="/opt"\n# export FARNA_CONTROL_INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT}"\n'
    produced = _matched_names(text, _pattern(SHELL_EXPORT_WRITE), relative_path="scripts/install.sh")

    assert not produced


def test_gate_flags_a_stale_read_with_no_canonical_read() -> None:
    """Mutation: a reader that only accepts a pre-rebrand spelling is drift."""
    stale = LEGACY_PREFIX + "INSTALL_ROOT"
    drifted = f'INSTALL_ROOT="${{{stale}:-/opt}}"\n'
    problems = contract_problems(drifted, reads=(_pattern(SHELL_READ),), relative_path="scripts/install.sh")

    assert len(problems) == 1, problems


def test_gate_flags_an_echoed_canonical_name_that_is_not_exported() -> None:
    """Mutation: naming the canonical variable in a log line must not satisfy the gate.

    This exact shape was live during the fix: the entrypoint echoed the canonical
    name while still exporting only the stale one, and a file-text check passed.
    """
    stale = LEGACY_PREFIX + "SYSTEM_DATABASE_URL"
    drifted = f'export {stale}="$SYSTEM_URL"\necho "FARNA_CONTROL_SYSTEM_DATABASE_URL fixed"\n'
    problems = contract_problems(drifted, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="deploy/fly/entrypoint.sh")

    assert len(problems) == 1, problems


def test_gate_flags_an_invented_non_canonical_name() -> None:
    """Mutation: a brand-new name outside the namespace must be renamed."""
    invented = f'export {LEGACY_PREFIX}BRAND_NEW_KNOB="1"\n'
    problems = contract_problems(invented, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="scripts/install.sh")

    assert len(problems) == 1, problems


def test_gate_passes_a_canonical_export() -> None:
    exported = 'export FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"\n'
    assert not contract_problems(exported, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="scripts/install.sh")


def test_gate_passes_a_canonical_read() -> None:
    read = 'INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT:-/opt}"\n'
    assert not contract_problems(read, reads=(_pattern(SHELL_READ),), relative_path="scripts/install.sh")


# ---------------------------------------------------------------------------
# The Python reader half: a registered name is read only through read_env
# ---------------------------------------------------------------------------

#: Call attributes that read an environment mapping (``source.get("NAME")``,
#: ``os.environ.get`` / ``getenv``).
_READ_CALL_ATTRS = frozenset({"get", "getenv"})
#: Project helpers that read one env var by name.
_READ_CALL_NAMES = frozenset({"override_or", "override_int_or", "override_float_or"})
_ROUTED_CALL_NAME = "read_env"
#: CLI option constructors whose ``envvar=`` keyword IS the environment read.
_ENVVAR_OPTION_NAMES = frozenset({"option", "Option"})
#: The modules that own those constructors (``click.option`` / ``typer.Option``).
_CLI_MODULES = frozenset({"click", "typer"})


@dataclass(frozen=True)
class EnvRead:
    """One environment read in the Python source."""

    name: str
    relative_path: str
    through_registry: bool


def _literal_env_name(node: ast.expr | None) -> str | None:
    """Return ``node``'s value when it is a service-contract env-name literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and _ENV_NAME_RE.match(node.value):
        return node.value
    return None


def _assignments(tree: ast.Module) -> list[tuple[str, ast.expr | None]]:
    """Every ``target = value`` binding in the module, nested scopes included."""
    pairs: list[tuple[str, ast.expr | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    pairs.append((target.id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            pairs.append((node.target.id, node.value))
    return pairs


def _bound_env_names(value: ast.expr, bindings: dict[str, set[str]]) -> set[str]:
    """Env names a binding value can resolve to.

    A simple conditional name (``X = A if cond else B``) folds to BOTH arms: the
    read through ``X`` may take either spelling, so each must count as accepted.
    """
    literal = _literal_env_name(value)
    if literal is not None:
        return {literal}
    if isinstance(value, ast.IfExp):
        return _bound_env_names(value.body, bindings) | _bound_env_names(value.orelse, bindings)
    if isinstance(value, ast.Name):
        return set(bindings.get(value.id, set()))
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "env_contract":
        attribute = getattr(env_contract, value.attr, None)
        return {attribute} if isinstance(attribute, str) else set()
    return set()


def _module_bindings(tree: ast.Module, project: dict[str, dict[str, set[str]]] | None = None) -> dict[str, set[str]]:
    """Resolve ``identifier -> possible env names`` for one module.

    Handles registry imports (``from ...env_contract import SERVE_SPA_ENV``),
    imports from a project module that re-exports a registry name (``from
    ...host_origin import LAN_ORIGINS_ENV``), literals
    (``X = "FARNA_CONTROL_Y"``) and aliases of any of them
    (``_SERVE_SPA_ENV = SERVE_SPA_ENV``, ``X = env_contract.Y``), so a reader
    hiding behind a constant is still identified - the shape the pre-fix
    LAN-origins read used. A conditional name binds both its arms.
    """
    bindings: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            imported = getattr(env_contract, alias.name, None) if node.module.endswith("env_contract") else None
            if isinstance(imported, str):
                bindings[alias.asname or alias.name] = {imported}
                continue
            re_exported = (project or {}).get(node.module, {}).get(alias.name)
            if re_exported:
                bindings[alias.asname or alias.name] = set(re_exported)
    assignments = _assignments(tree)
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if value is None:
                continue
            resolved = _bound_env_names(value, bindings)
            if resolved and not resolved <= bindings.get(target, set()):
                bindings[target] = bindings.get(target, set()) | resolved
                changed = True
    return bindings


def _key_names(node: ast.expr | None, bindings: dict[str, set[str]]) -> list[str]:
    """Every env name the expression may denote (a conditional names both arms)."""
    if node is None:
        return []
    literal = _literal_env_name(node)
    if literal is not None:
        return [literal]
    if isinstance(node, ast.Name):
        return sorted(bindings.get(node.id, set()))
    if isinstance(node, ast.IfExp):
        return sorted(set(_key_names(node.body, bindings)) | set(_key_names(node.orelse, bindings)))
    return []


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_envvar_option(func: ast.expr) -> bool:
    """``click.option`` / ``typer.Option`` (or the bare imported constructor)."""
    if isinstance(func, ast.Attribute):
        owner = func.value
        return func.attr in _ENVVAR_OPTION_NAMES and isinstance(owner, ast.Name) and owner.id in _CLI_MODULES
    return isinstance(func, ast.Name) and func.id in _ENVVAR_OPTION_NAMES


def _environ_key_param_indexes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[int]:
    """Indexes of parameters a function passes as an ``os.environ`` read key."""
    params = [arg.arg for arg in (*func.args.posonlyargs, *func.args.args)]
    indexes: set[int] = set()
    for child in ast.walk(func):
        key: ast.expr | None = None
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            receiver = child.func.value
            if child.func.attr == "getenv" or (child.func.attr == "get" and isinstance(receiver, ast.Attribute) and receiver.attr == "environ"):
                key = child.args[0] if child.args else None
        elif isinstance(child, ast.Subscript) and isinstance(child.value, ast.Attribute) and child.value.attr == "environ":
            key = child.slice
        if isinstance(key, ast.Name) and key.id in params:
            indexes.add(params.index(key.id))
    return indexes


def _single_name_helpers(tree: ast.Module) -> dict[str, int]:
    """Functions in this module that read exactly one env var through a parameter.

    Returns ``function name -> positional parameter index`` for the simple shape
    ``def _env_int(name, default): ... os.environ.get(name) ...``. A call to it
    then counts as a read of the literal it passes at that index, so production
    code can keep ONE name source of truth instead of duplicating the read at
    every call site - the detector carries the knowledge, not the caller.
    """
    helpers: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        indexes = _environ_key_param_indexes(node)
        if len(indexes) == 1:
            helpers[node.name] = next(iter(indexes))
    return helpers


def env_reads(source: str, relative_path: str, project: dict[str, dict[str, set[str]]] | None = None) -> list[EnvRead]:
    """Enumerate every environment read in one module's source.

    Recognises a direct read (``os.environ.get`` / ``environ[...]`` /
    ``getenv`` and the project's single-name override helpers), a registered
    read through :func:`read_env`, a CLI option's ``envvar=``
    (``click.option`` / ``typer.Option``), and a call to a local single-name
    helper (whose name parameter is read one frame down).
    """
    tree = ast.parse(source)
    bindings = _module_bindings(tree, project)
    helpers = _single_name_helpers(tree)
    reads: list[EnvRead] = []

    def add(names: list[str], *, through_registry: bool) -> None:
        for name in names:
            read = EnvRead(name, relative_path, through_registry)
            if read not in reads:
                reads.append(read)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            call = _call_name(func)
            if call == _ROUTED_CALL_NAME:
                add(_key_names(node.args[1] if len(node.args) > 1 else None, bindings), through_registry=True)
            elif call in _READ_CALL_ATTRS or call in _READ_CALL_NAMES:
                add(_key_names(node.args[0] if node.args else None, bindings), through_registry=False)
            elif _is_envvar_option(func):
                for keyword in node.keywords:
                    if keyword.arg == "envvar":
                        add(_key_names(keyword.value, bindings), through_registry=False)
            elif call in helpers:
                index = helpers[call]
                add(_key_names(node.args[index] if len(node.args) > index else None, bindings), through_registry=False)
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            add(_key_names(node.slice, bindings), through_registry=False)
    return reads


def _python_modules() -> list[Path]:
    return sorted(PYTHON_SOURCE_ROOT.rglob("*.py"))


#: Python producers/consumers of the documented contract that live OUTSIDE
#: ``backend/src/farna``. Listed explicitly - adding one is a review gate of its
#: own, exactly like ``_SERVICE_SCRIPTS`` - so a documented knob read only by a
#: helper script is proven accepted instead of parked on the burn-down list.
_EXTRA_READER_SCRIPTS: tuple[str, ...] = (
    "scripts/backup.py",
    "scripts/restore.py",
)


def _dotted_module_name(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    return ".".join(parts[parts.index("farna") :])


def _project_bindings() -> dict[str, dict[str, set[str]]]:
    """Every module's own ``identifier -> possible env names`` map, for re-export resolution."""
    project: dict[str, dict[str, set[str]]] = {}
    for path in _python_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        project[_dotted_module_name(path)] = _module_bindings(tree)
    return project


def _python_reads() -> list[EnvRead]:
    project = _project_bindings()
    reads: list[EnvRead] = []
    for path in _python_modules():
        reads.extend(
            env_reads(
                path.read_text(encoding="utf-8"),
                path.relative_to(REPO_ROOT).as_posix(),
                project,
            )
        )
    # The explicitly-listed out-of-package readers. No project re-export map:
    # they read the canonical literal directly.
    for relative_path in _EXTRA_READER_SCRIPTS:
        reads.extend(env_reads((REPO_ROOT / relative_path).read_text(encoding="utf-8"), relative_path))
    return reads


def _registry_owned_names() -> set[str]:
    """Every canonical name the registry owns."""
    return set(_REGISTRY_ENV_CONSTANTS)


#: Registered names whose readers deliberately take the canonical literal only.
#: FAR-1519: the system-role URL is resolved at bootstrap, before the registry's
#: reader helpers are usable, so its readers take the name directly.
_SINGLE_NAME_BY_DESIGN: dict[str, str] = {
    env_contract.SYSTEM_DATABASE_URL_ENV: ("FAR-1519: bootstrap/bootstrap_role/entry/config_source read the canonical name directly; there is no second spelling to fall back to"),
}


def reader_problems(reads: list[EnvRead]) -> list[str]:
    """Return every read of a registered name that bypassed :func:`read_env`."""
    registry = _registry_owned_names()
    problems: list[str] = []
    for read in reads:
        if read.name not in registry or read.through_registry:
            continue
        if read.name in _SINGLE_NAME_BY_DESIGN:
            continue
        problems.append(f"{read.relative_path} reads {read.name!r} directly: registered names must be read through read_env so the registry stays the ONE definition")
    return problems


def registered_redeclarations(source: str, relative_path: str) -> list[str]:
    """Return every re-declaration of a registry-owned name outside the registry."""
    registry = _registry_owned_names()
    problems: list[str] = []
    for target, value in _assignments(ast.parse(source)):
        name = _literal_env_name(value)
        if name is None or name not in registry:
            continue
        problems.append(f"{relative_path} declares {target} = {name!r}: import it from farna.launcher.env_contract so the registry stays the ONE definition")
    return problems


def _registry_module_path() -> str:
    return (PYTHON_SOURCE_ROOT / "launcher" / "env_contract.py").relative_to(REPO_ROOT).as_posix()


def test_python_readers_of_registered_names_go_through_the_registry() -> None:
    problems = reader_problems(_python_reads())
    assert not problems, problems


def test_the_reader_sweep_includes_the_backup_scripts() -> None:
    """Positive half: the passphrase is read outside ``farna/``, so an empty
    resolved set must never read as clean for the explicitly-listed scripts."""
    reads = _python_reads()
    passphrase_reads = [read for read in reads if read.name == "FARNA_CONTROL_BACKUP_PASSPHRASE"]

    # A subset check: the two scripts must both be seen, and a future reader is
    # welcome rather than a failure (exact equality made the sweep brittle).
    assert {"scripts/backup.py", "scripts/restore.py"} <= {read.relative_path for read in passphrase_reads}


def test_the_python_reader_sweep_sees_the_spa_and_lan_reads() -> None:
    """Positive half: an empty resolved set must never read as clean."""
    reads = _python_reads()
    assert len(reads) > 20, len(reads)
    registry = _registry_owned_names()
    mount_reads = [read for read in reads if read.name in registry and read.relative_path.endswith("api/main.py")]
    assert {read.name for read in mount_reads} == {
        env_contract.SERVE_SPA_ENV,
        env_contract.FRONTEND_DIST_ENV,
        env_contract.LAN_ORIGINS_ENV,
    }
    assert all(read.through_registry for read in mount_reads)


def test_no_module_redeclares_a_registered_name() -> None:
    registry_module = _registry_module_path()
    problems: list[str] = []
    for path in _python_modules():
        relative_path = path.relative_to(REPO_ROOT).as_posix()
        if relative_path == registry_module:
            continue
        problems.extend(registered_redeclarations(path.read_text(encoding="utf-8"), relative_path))
    assert not problems, problems


def test_gate_flags_a_python_reader_that_bypasses_the_registry() -> None:
    """Mutation: the pre-fix LAN-origins read must be reported, not passed.

    ``FARNA_CONTROL_LAN_ORIGINS`` was documented as the operator knob while the
    middleware read the name directly through a re-exported constant, so the
    documented spelling was a silent 403. The synthetic module is the same shape.
    """
    bypassing = "from farna.launcher import env_contract\n\nLAN_ORIGINS_ENV = env_contract.LAN_ORIGINS_ENV\n\n\ndef mount(source):\n    return source.get(LAN_ORIGINS_ENV)\n"
    relative_path = "backend/src/farna/synthetic_reader.py"
    reads = env_reads(bypassing, relative_path)

    assert reads == [EnvRead(env_contract.LAN_ORIGINS_ENV, relative_path, False)]
    problems = reader_problems(reads)
    assert len(problems) == 1, problems
    assert env_contract.LAN_ORIGINS_ENV in problems[0]

    routed = bypassing.replace("source.get(LAN_ORIGINS_ENV)", "read_env(source, LAN_ORIGINS_ENV)")
    assert not reader_problems(env_reads(routed, relative_path))


def test_gate_flags_a_redeclared_registered_name() -> None:
    """Mutation: a second literal definition of a registry name is drift."""
    relative_path = "backend/src/farna/synthetic_registry_copy.py"

    assert not registered_redeclarations(
        "from farna.launcher import env_contract\n\nLAN_ORIGINS_ENV = env_contract.LAN_ORIGINS_ENV\n",
        relative_path,
    )
    problems = registered_redeclarations('LAN_ORIGINS_ENV = "FARNA_CONTROL_LAN_ORIGINS"\n', relative_path)
    assert len(problems) == 1, problems
    assert env_contract.LAN_ORIGINS_ENV in problems[0]


#: The CLI option constructors whose ``envvar=`` is an environment read. Each
#: shape is a separate mutation below so a detector regression fails on the
#: exact form rather than being masked by a sibling.
_CLI_ENVVAR_SHAPES: tuple[tuple[str, str], ...] = (
    ("click", 'import click\n\n@click.option("--token", envvar="FARNA_CONTROL_ADMIN_TOKEN", default=None)\ndef cli(token: str | None) -> None: ...\n'),
    ("typer", 'import typer\n\nTOKEN = typer.Option(None, envvar="FARNA_CONTROL_ADMIN_TOKEN")\n'),
)


@pytest.mark.parametrize(("module", "source"), _CLI_ENVVAR_SHAPES, ids=[module for module, _source in _CLI_ENVVAR_SHAPES])
def test_gate_resolves_a_cli_option_envvar(module: str, source: str) -> None:
    """Mutation: a documented knob read through ``click``/``typer`` ``envvar=``
    is a real read, so it must not be parked on the burn-down list."""
    reads = env_reads(source, f"backend/src/farna/cli/synthetic_{module}.py")

    assert [read.name for read in reads] == ["FARNA_CONTROL_ADMIN_TOKEN"], reads
    assert not reads[0].through_registry


def test_gate_resolves_a_single_name_helper_parameter() -> None:
    """Mutation: the idiomatic one-name-source helper must be understood.

    ``_env_int(name, ...)`` reads ``os.environ.get(name)`` one frame down; the
    detector must attribute the call site's literal to it so production code
    does not have to duplicate the read to satisfy the gate.
    """
    source = 'import os\n\n\ndef _env_int(name: str, default: int) -> int:\n    raw = os.environ.get(name)\n    return int(raw) if raw else default\n\n\nLIMIT = "FARNA_CONTROL_AGENT_MINT_BUDGET_LIMIT"\n\n\ndef resolve() -> int:\n    return _env_int(LIMIT, 10)\n'
    reads = env_reads(source, "backend/src/farna/core/synthetic_budget.py")

    assert [read.name for read in reads] == ["FARNA_CONTROL_AGENT_MINT_BUDGET_LIMIT"], reads


def test_gate_folds_a_conditional_env_name() -> None:
    """Mutation: a ternary name binds BOTH arms, so each documented knob counts."""
    source = 'import os\n\n\ndef read(trigger_type: str) -> str | None:\n    name = "FARNA_CONTROL_CRON_STREAK_MIN_WINDOW_HOURS" if trigger_type == "cron" else "FARNA_CONTROL_ONGOING_STREAK_MIN_WINDOW_HOURS"\n    return os.environ.get(name)\n'
    reads = env_reads(source, "backend/src/farna/core/synthetic_streak.py")

    assert {read.name for read in reads} == {
        "FARNA_CONTROL_CRON_STREAK_MIN_WINDOW_HOURS",
        "FARNA_CONTROL_ONGOING_STREAK_MIN_WINDOW_HOURS",
    }, reads


# ---------------------------------------------------------------------------
# The documented half: a published knob must be accepted by SOMETHING
# ---------------------------------------------------------------------------

#: Documented knobs that NO mechanism accepts: not a registry name, not a
#: settings alias, and not read as a literal by ``farna/`` (or by one of the
#: explicitly-listed out-of-package reader scripts). It is EMPTY: the burn-down
#: is closed, and the companion test below still fails if an entry is stale, so
#: a NEW documented gap must be declared here deliberately - listing one is a
#: recorded debt, never a way to make the gate pass.
DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS: frozenset[str] = frozenset()

#: Both prefixes a published knob may carry while an old docs page is in flight;
#: the gate below only requires the canonical half to be accepted.
_DOCUMENTED_ENV_NAME_RE = re.compile(rf"\b(?:{LEGACY_PREFIX}|FARNA_CONTROL_)[A-Z0-9_]+\b")


def _documented_env_knobs() -> set[str]:
    text = CONFIGURATION_REFERENCE.read_text(encoding="utf-8")
    return {match.group(0) for match in _DOCUMENTED_ENV_NAME_RE.finditer(text) if not match.group(0).endswith("_")}


def _settings_aliases() -> set[str]:
    """The env-name literals ``farna.settings`` declares as aliases."""
    tree = ast.parse(SETTINGS_MODULE.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_field = (isinstance(func, ast.Attribute) and func.attr == "Field") or (isinstance(func, ast.Name) and func.id == "Field")
            if not is_field:
                continue
            for keyword in node.keywords:
                if keyword.arg in {"alias", "validation_alias"}:
                    name = _literal_env_name(keyword.value)
                    if name:
                        aliases.add(name)
    return aliases


def _accepted_knobs() -> set[str]:
    accepted = set(_registry_owned_names())
    accepted |= _settings_aliases()
    accepted |= {read.name for read in _python_reads()}
    return accepted


def test_every_documented_knob_is_accepted_somewhere() -> None:
    """A documented spelling no reader accepts is the FAR-2563 403, not docs."""
    documented = _documented_env_knobs()
    assert len(documented) > 90, len(documented)

    missing = sorted(documented - _accepted_knobs() - DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS)
    assert not missing, f"documented knobs no mechanism accepts (add a settings alias, a registry name, or list a real gap): {missing}"


def gap_list_problems(gaps: frozenset[str], *, accepted: set[str], documented: set[str]) -> list[str]:
    """Return every stale entry in a documented-knob gap list.

    Extracted from the gate so the predicate can be proven against a SYNTHETIC
    list: the real list is empty, and an empty set makes both membership checks
    vacuous, so the only mutation that proves the check works supplies entries.
    """
    problems: list[str] = []
    for name in sorted(gaps & accepted):
        problems.append(f"{name} is now accepted - delete it from DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS")
    for name in sorted(gaps - documented):
        problems.append(f"{name} is no longer documented - delete it from DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS")
    return problems


def test_the_documented_knob_gap_list_holds_only_live_gaps() -> None:
    """Burn-down discipline: a fixed knob must be deleted from the list."""
    problems = gap_list_problems(DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS, accepted=_accepted_knobs(), documented=_documented_env_knobs())

    assert not problems, problems


def test_the_gap_list_check_flags_stale_entries() -> None:
    """Mutation: the empty real list must not make the staleness check vacuous."""
    gaps = frozenset({"FARNA_CONTROL_NOW_ACCEPTED", "FARNA_CONTROL_NO_LONGER_DOCUMENTED", "FARNA_CONTROL_STILL_A_GAP"})
    problems = gap_list_problems(
        gaps,
        accepted={"FARNA_CONTROL_NOW_ACCEPTED"},
        documented={"FARNA_CONTROL_STILL_A_GAP", "FARNA_CONTROL_NOW_ACCEPTED"},
    )

    assert len(problems) == 2, problems
    assert any("FARNA_CONTROL_NOW_ACCEPTED" in problem for problem in problems)
    assert any("FARNA_CONTROL_NO_LONGER_DOCUMENTED" in problem for problem in problems)
