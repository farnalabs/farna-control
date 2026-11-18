"""FAR-2563: the rebranded env contract, and the gate that stops it drifting again.

Three separate incidents were one defect: a service-contract environment
variable was renamed on one side of the producer/consumer boundary and not the
other, so the value was written and never read.

* FAR-2554 - ``MODULO_BUNDLED_BIN_DIR`` exported, ``FARNA_CONTROL_...`` read.
* FAR-2560/2561 - two ``APP_DB_NAME`` constants.
* FAR-2563 - the bundle launchers exported ``MODULO_SERVE_SPA`` /
  ``MODULO_FRONTEND_DIST`` while ``farna.api.main`` read the
  ``FARNA_CONTROL_`` spelling, so the built SPA was never mounted.

The names now live in one registry (:mod:`farna.launcher.env_contract`) and this
module is the structural gate over it: every ``MODULO_*`` name a service script
writes or reads must be a registered legacy alias whose canonical counterpart is
produced in the SAME production context, or a documented exemption. Reintroducing
a one-sided rename fails here rather than in an installed app.

The patterns capture BOTH prefixes and the check runs over the matched name set,
not the raw file text: a canonical name appearing only in a log line, a comment
or a wide-string literal that is not an argument to a write call must never
satisfy the gate (it was tried twice - an ``echo`` of the canonical name masked
an export that still used the legacy spelling, and an unanchored ``L"NAME"``
pattern let a canonical name inside a ``/* comment */`` mask a C producer that
still wrote the legacy name).

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
  through :func:`farna.launcher.env_contract.read_env` (so both spellings work)
  and must never be re-declared outside the registry;
* documentation - every ``MODULO_*`` knob ``docs/configuration-reference.md``
  publishes must be accepted by SOME mechanism: this registry, a
  ``farna.settings._product_aliases`` declaration, or a literal legacy read.
  A documented spelling that nothing accepts is the FAR-2563 LAN-origins defect
  (a silent 403), so those are listed explicitly in
  ``DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS`` - a burn-down list of known gaps, not a
  licence: removing an entry is the fix, and an entry that becomes live fails.

Scope: the launcher / installer / container-entrypoint / Helm / API surface,
i.e. the places that supply process environment to the app directly and the
modules that read it. Operator knobs the app resolves through ``Settings``
aliases (``MODULO_DB``, ``MODULO_USERS``, ...) are a different mechanism -
``farna.settings._product_aliases`` owns those pairs, so a compose file setting
one is not drift; the documentation half above accepts that mechanism.

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

REPO_ROOT = Path(__file__).resolve().parents[4]
PYTHON_SOURCE_ROOT = REPO_ROOT / "backend" / "src" / "farna"
SETTINGS_MODULE = PYTHON_SOURCE_ROOT / "settings.py"
CONFIGURATION_REFERENCE = REPO_ROOT / "docs" / "configuration-reference.md"

#: Shell ``export NAME=...`` (both spellings).
SHELL_EXPORT_WRITE = r"^\s*export\s+({names})="
#: C ``setenv("NAME", ...)`` - the literal is the call's FIRST argument.
C_SETENV_WRITE = r'setenv\("({names})"'
#: Windows ``append_environment_entry(block, len, &used, L"NAME", value)`` -
#: anchored to the NAME argument (the fourth, after three comma-separated
#: arguments). An unanchored ``L"NAME"`` pattern also matched the inherited-env
#: allowlist and, worse, a ``/* comment */``, so a canonical name mentioned in
#: prose satisfied a producer that still wrote the legacy name.
C_APPEND_ENV_WRITE = r'append_environment_entry\([^,()]+,\s*[^,()]+,\s*[^,()]+,\s*L"({names})"'
#: Kubernetes env entry ``- name: NAME``.
K8S_ENV_WRITE = r"^\s*-\s*name:\s+({names})\s*$"
#: Shell ``${NAME:-...}`` / ``${NAME}`` expansion.
SHELL_READ = r"\$\{({names})[:\}]"

#: The two prefixes a service-contract name may carry.
_NAME_CLASS = r"(?:MODULO_|FARNA_CONTROL_)[A-Z0-9_]+"
_ENV_NAME_RE = re.compile(r"^(?:MODULO_|FARNA_CONTROL_)[A-Z0-9_]+$")


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
    ServiceScript("deploy/helm/modulo/templates/backend-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
    ServiceScript("deploy/helm/modulo/templates/saq-runner-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
    ServiceScript("deploy/helm/modulo/templates/saq-system-deployment.yaml", writes=(_pattern(K8S_ENV_WRITE),)),
)

#: The canonical names each producer is REQUIRED to write, in the same production
#: context the ``writes`` patterns above describe. This is the positive half of
#: the gate: an empty legacy match set must never read as "all clean", which a
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
        "deploy/helm/modulo/templates/backend-deployment.yaml",
        _pattern(K8S_ENV_WRITE),
        (env_contract.SYSTEM_DATABASE_URL_ENV,),
    ),
    (
        "deploy/helm/modulo/templates/saq-system-deployment.yaml",
        _pattern(K8S_ENV_WRITE),
        (env_contract.SYSTEM_DATABASE_URL_ENV,),
    ),
)


def _legacy_to_canonical() -> dict[str, str]:
    return {legacy: canonical for canonical, aliases in env_contract.LEGACY_ALIASES.items() for legacy in aliases}


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
    """Return every one-sided ``MODULO_*`` production/consumption in ``text``.

    A ``MODULO_*`` name passes only when it is a registered legacy alias AND its
    canonical name is produced in the SAME context (same match set for that
    pattern kind), or when it is a documented exemption. Everything else is a
    drift report.

    ``relative_path`` selects the comment syntax stripped before matching (see
    :func:`strip_comments`); synthetic C text must say so, or its comments are
    treated as live code.
    """
    mapping = _legacy_to_canonical()
    problems: list[str] = []
    for verb, participle, patterns in (("writes", "written", writes), ("reads", "read", reads)):
        for pattern in patterns:
            names = _matched_names(text, pattern, relative_path=relative_path)
            for legacy in sorted(name for name in names if name.startswith(env_contract.LEGACY_PREFIX)):
                canonical = mapping.get(legacy)
                if canonical is not None:
                    if canonical in names:
                        continue
                    problems.append(f"{verb} unpaired legacy name {legacy!r}: {canonical!r} is not {participle} in the same context (a producer must emit the canonical name; a reader must accept both)")
                    continue
                if legacy in env_contract.LEGACY_ONLY_EXEMPT:
                    continue
                problems.append(f"{verb} unregistered legacy name {legacy!r}: add the pair to farna.launcher.env_contract.LEGACY_ALIASES, rename it to the FARNA_CONTROL_* spelling, or document it in LEGACY_ONLY_EXEMPT")
    return problems


# ---------------------------------------------------------------------------
# The registry itself
# ---------------------------------------------------------------------------


def test_every_canonical_name_is_farna_control_prefixed() -> None:
    for canonical in env_contract.LEGACY_ALIASES:
        assert canonical.startswith(env_contract.CANONICAL_PREFIX), canonical


def test_every_legacy_alias_is_modulo_prefixed() -> None:
    for aliases in env_contract.LEGACY_ALIASES.values():
        for legacy in aliases:
            assert legacy.startswith(env_contract.LEGACY_PREFIX), legacy


def test_an_alias_maps_to_the_name_with_the_same_suffix() -> None:
    """A pair that is not suffix-identical is a different variable, not a rename."""
    for canonical, aliases in env_contract.LEGACY_ALIASES.items():
        suffix = canonical[len(env_contract.CANONICAL_PREFIX) :]
        for legacy in aliases:
            assert legacy[len(env_contract.LEGACY_PREFIX) :] == suffix, (canonical, legacy)


def test_no_name_is_both_registered_and_exempt() -> None:
    registered = set(_legacy_to_canonical())
    overlap = registered & set(env_contract.LEGACY_ONLY_EXEMPT)
    assert not overlap, overlap


def test_every_exemption_states_a_reason() -> None:
    for legacy, reason in env_contract.LEGACY_ONLY_EXEMPT.items():
        assert reason.strip(), legacy


# ---------------------------------------------------------------------------
# read_env
# ---------------------------------------------------------------------------


def test_read_env_prefers_the_canonical_name() -> None:
    source = {env_contract.SERVE_SPA_ENV: "canonical", "MODULO_SERVE_SPA": "legacy"}
    assert env_contract.read_env(source, env_contract.SERVE_SPA_ENV) == "canonical"


def test_read_env_falls_back_to_the_legacy_alias() -> None:
    """An already-installed bundle still exports the pre-rebrand name."""
    assert env_contract.read_env({"MODULO_SERVE_SPA": "1"}, env_contract.SERVE_SPA_ENV) == "1"


def test_read_env_ignores_an_empty_canonical_value() -> None:
    source = {env_contract.SERVE_SPA_ENV: "", "MODULO_SERVE_SPA": "1"}
    assert env_contract.read_env(source, env_contract.SERVE_SPA_ENV) == "1"


def test_read_env_returns_the_default_when_nothing_is_set() -> None:
    assert env_contract.read_env({}, env_contract.FRONTEND_DIST_ENV, "/none") == "/none"


def test_read_env_leaves_an_unregistered_name_alone() -> None:
    """An unpaired name has no aliases, so only its own value can satisfy it."""
    assert not env_contract.read_env({"MODULO_FRONTEND_DIST": "/x"}, "FARNA_CONTROL_UNREGISTERED")


# ---------------------------------------------------------------------------
# The sweep (producer + reader)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", _SERVICE_SCRIPTS, ids=[script.relative_path for script in _SERVICE_SCRIPTS])
def test_service_script_has_no_one_sided_rebrand_drift(script: ServiceScript) -> None:
    text = script.path().read_text(encoding="utf-8")
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


def test_the_sweep_sees_the_real_modulo_names_it_claims_to_check() -> None:
    """A sweep whose regexes match nothing must never read as clean.

    The legacy names below are the ones this gate exists for; if the patterns
    stop matching them the gate would pass vacuously.
    """
    install = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    produced = _matched_names(install, _pattern(SHELL_EXPORT_WRITE), relative_path="scripts/install.sh")
    assert {"MODULO_INSTALL_ROOT", "MODULO_BIN_DIR"} <= produced

    macos_install = (REPO_ROOT / "scripts" / "macos" / "install.sh").read_text(encoding="utf-8")
    consumed = _matched_names(macos_install, _pattern(SHELL_READ), relative_path="scripts/macos/install.sh")
    assert {"MODULO_INSTALL_ROOT", "MODULO_DATA_DIR"} <= consumed


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
    pre_fix = 'setenv("PYTHONPATH", src, 1);\nsetenv("MODULO_SERVE_SPA", "1", 1);\nsetenv("MODULO_FRONTEND_DIST", frontend_dist, 1);\n'
    problems = contract_problems(pre_fix, writes=(_pattern(C_SETENV_WRITE),), relative_path="scripts/macos/build-native-bundle.sh")

    assert len(problems) == 2, problems
    assert any(env_contract.SERVE_SPA_ENV in problem for problem in problems)
    assert any(env_contract.FRONTEND_DIST_ENV in problem for problem in problems)


def test_gate_flags_a_windows_write_that_uses_the_legacy_names() -> None:
    """Mutation: the Windows shape, with the pre-rebrand names, is drift."""
    drifted = 'if (!append_environment_entry(block, LEN, &used, L"MODULO_SERVE_SPA", L"1") ||\n    !append_environment_entry(block, LEN, &used, L"MODULO_FRONTEND_DIST", dist)) {\n'
    problems = contract_problems(drifted, writes=(_pattern(C_APPEND_ENV_WRITE),), relative_path="synthetic.c")

    assert len(problems) == 2, problems


def test_gate_is_not_satisfied_by_canonical_names_inside_a_c_comment() -> None:
    """Mutation (reverse): naming the canon in a comment must not mask a legacy write.

    This is the exact false pass the unanchored ``L"NAME"`` pattern allowed: the
    canonical names appeared in a ``/* comment */`` while the code still wrote
    the legacy spelling, and both the drift check and
    ``test_producers_emit_the_canonical_names`` went green.
    """
    masked = '/* FARNA_CONTROL_SERVE_SPA FARNA_CONTROL_FRONTEND_DIST */\n/* setenv("FARNA_CONTROL_SERVE_SPA", "1", 1); */\nif (!append_environment_entry(block, LEN, &used, L"MODULO_SERVE_SPA", L"1") ||\n    !append_environment_entry(block, LEN, &used, L"MODULO_FRONTEND_DIST", dist)) {\n'
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
    shell = 'from_file_dir="${FROM_FILE%/*}"\nexport MODULO_BRAND_NEW_KNOB="1"\nbundle_dir_name="${bundle_dir##*/}"\n'
    stripped = strip_comments(shell, "scripts/install.sh")
    assert 'export MODULO_BRAND_NEW_KNOB="1"' in stripped

    problems = contract_problems(shell, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="scripts/install.sh")
    assert len(problems) == 1, problems
    assert "MODULO_BRAND_NEW_KNOB" in problems[0]


def test_a_shell_test_operator_is_not_treated_as_a_c_comment_opener() -> None:
    """The build-native-bundle.sh shape: ``[[ "$target" = /* ]]`` opened a span.

    The closing ``*/`` there is a real C comment terminator inside the launcher
    heredoc, so the old stripper swallowed everything between the two - the
    symlink-escape guard and the ``setenv`` lines with it.
    """
    shell = 'if [[ "$target" = /* ]]; then\n  setenv("MODULO_BRAND_NEW_KNOB", "1", 1);\nfi\n/* launcher comment */\n'
    problems = contract_problems(shell, writes=(_pattern(C_SETENV_WRITE),), relative_path="scripts/macos/build-native-bundle.sh")

    assert len(problems) == 1, problems
    assert "MODULO_BRAND_NEW_KNOB" in problems[0]


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
    text = '# export FARNA_CONTROL_INSTALL_ROOT="/opt"\n# export MODULO_INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT}"\n'
    produced = _matched_names(text, _pattern(SHELL_EXPORT_WRITE), relative_path="scripts/install.sh")

    assert not produced


def test_gate_flags_a_legacy_read_with_no_canonical_read() -> None:
    """Mutation: a reader that only accepts the pre-rebrand spelling is drift."""
    drifted = 'INSTALL_ROOT="${MODULO_INSTALL_ROOT:-/opt}"\n'
    problems = contract_problems(drifted, reads=(_pattern(SHELL_READ),), relative_path="scripts/install.sh")

    assert len(problems) == 1, problems
    assert env_contract.INSTALL_ROOT_ENV in problems[0]


def test_gate_flags_an_echoed_canonical_name_that_is_not_exported() -> None:
    """Mutation: naming the canonical variable in a log line must not satisfy the gate.

    This exact shape was live during the fix: the entrypoint echoed the canonical
    name while still exporting only the legacy one, and a file-text check passed.
    """
    drifted = 'export MODULO_SYSTEM_DATABASE_URL="$SYSTEM_URL"\necho "FARNA_CONTROL_SYSTEM_DATABASE_URL fixed"\n'
    problems = contract_problems(drifted, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="deploy/fly/entrypoint.sh")

    assert len(problems) == 1, problems
    assert env_contract.SYSTEM_DATABASE_URL_ENV in problems[0]


def test_gate_flags_an_unregistered_legacy_name() -> None:
    """Mutation: a brand-new MODULO_-only variable must be registered or renamed."""
    invented = 'export MODULO_BRAND_NEW_KNOB="1"\n'
    problems = contract_problems(invented, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="scripts/install.sh")

    assert len(problems) == 1, problems
    assert "MODULO_BRAND_NEW_KNOB" in problems[0]


def test_gate_passes_a_correctly_paired_export() -> None:
    """The shipped installer pattern: canonical export plus a compatibility alias."""
    paired = 'export FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"\nexport MODULO_INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT}"\n'
    assert not contract_problems(paired, writes=(_pattern(SHELL_EXPORT_WRITE),), relative_path="scripts/install.sh")


def test_gate_passes_a_paired_read() -> None:
    paired = 'INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT:-${MODULO_INSTALL_ROOT:-/opt}}"\n'
    assert not contract_problems(paired, reads=(_pattern(SHELL_READ),), relative_path="scripts/install.sh")


def test_gate_passes_an_exempt_knob() -> None:
    exempt = 'X="${MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET:-}"\n'
    assert not contract_problems(exempt, reads=(_pattern(SHELL_READ),), relative_path="scripts/macos/install.sh")


# ---------------------------------------------------------------------------
# The Python reader half: a registered name is read only through read_env
# ---------------------------------------------------------------------------

#: Call attributes that read an environment mapping (``source.get("NAME")``,
#: ``os.environ.get`` / ``getenv``).
_READ_CALL_ATTRS = frozenset({"get", "getenv"})
#: Project helpers that read one env var by name.
_READ_CALL_NAMES = frozenset({"override_or", "override_int_or", "override_float_or"})
_ROUTED_CALL_NAME = "read_env"


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


def _module_bindings(tree: ast.Module, project: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    """Resolve ``identifier -> env name`` for one module.

    Handles registry imports (``from ...env_contract import SERVE_SPA_ENV``),
    imports from a project module that re-exports a registry name (``from
    ...host_origin import LAN_ORIGINS_ENV``), literals
    (``X = "FARNA_CONTROL_Y"``) and aliases of any of them
    (``_SERVE_SPA_ENV = SERVE_SPA_ENV``, ``X = env_contract.Y``), so a reader
    hiding behind a constant is still identified - the shape the pre-fix
    LAN-origins read used.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            imported = getattr(env_contract, alias.name, None) if node.module.endswith("env_contract") else None
            if not isinstance(imported, str):
                imported = (project or {}).get(node.module, {}).get(alias.name)
            if isinstance(imported, str):
                bindings[alias.asname or alias.name] = imported
    assignments = _assignments(tree)
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if target in bindings or value is None:
                continue
            resolved = _literal_env_name(value)
            if resolved is None and isinstance(value, ast.Name):
                resolved = bindings.get(value.id)
            elif resolved is None and isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "env_contract":
                attribute = getattr(env_contract, value.attr, None)
                resolved = attribute if isinstance(attribute, str) else None
            if resolved:
                bindings[target] = resolved
                changed = True
    return bindings


def _key_name(node: ast.expr | None, bindings: dict[str, str]) -> str | None:
    literal = _literal_env_name(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    return None


def env_reads(source: str, relative_path: str, project: dict[str, dict[str, str]] | None = None) -> list[EnvRead]:
    """Enumerate every environment read in one module's source."""
    tree = ast.parse(source)
    bindings = _module_bindings(tree, project)
    reads: list[EnvRead] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            call = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if call == _ROUTED_CALL_NAME:
                name = _key_name(node.args[1] if len(node.args) > 1 else None, bindings)
                if name:
                    reads.append(EnvRead(name, relative_path, True))
            elif call in _READ_CALL_ATTRS or call in _READ_CALL_NAMES:
                name = _key_name(node.args[0] if node.args else None, bindings)
                if name:
                    reads.append(EnvRead(name, relative_path, False))
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
            name = _key_name(node.slice, bindings)
            if name:
                reads.append(EnvRead(name, relative_path, False))
    return reads


def _python_modules() -> list[Path]:
    return sorted(PYTHON_SOURCE_ROOT.rglob("*.py"))


def _dotted_module_name(path: Path) -> str:
    parts = path.relative_to(REPO_ROOT).with_suffix("").parts
    return ".".join(parts[parts.index("farna") :])


def _project_bindings() -> dict[str, dict[str, str]]:
    """Every module's own ``identifier -> env name`` map, for re-export resolution."""
    project: dict[str, dict[str, str]] = {}
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
    return reads


def _registry_owned_names() -> set[str]:
    """Every canonical name the registry owns, plus each legacy alias."""
    return set(env_contract.LEGACY_ALIASES) | {alias for aliases in env_contract.LEGACY_ALIASES.values() for alias in aliases}


#: Registered names whose readers deliberately take the CANONICAL spelling only.
#: FAR-1519: an ambient legacy system URL inherited from an earlier process must
#: not reset an existing system-role password, so the bootstrap reader is
#: single-name by design (cross-referenced from ``env_contract.LEGACY_ALIASES``).
_SINGLE_NAME_BY_DESIGN: dict[str, str] = {
    env_contract.SYSTEM_DATABASE_URL_ENV: ("FAR-1519: bootstrap/bootstrap_role/entry/config_source read the canonical name only, so an ambient MODULO_SYSTEM_DATABASE_URL cannot reset the system-role password"),
}

#: Known bypasses keyed by (relative path, canonical name). Every entry is an
#: OPEN DEFECT with its fix - not a licence: the sweep reports every other read,
#: so a new bypass cannot appear here without a review decision.
_KNOWN_READ_BYPASSES: dict[tuple[str, str], str] = {
    ("backend/src/farna/launcher/upgrade.py", env_contract.INSTALL_ROOT_ENV): ("reads FARNA_CONTROL_INSTALL_ROOT directly; route it through read_env so an install made by a pre-rebrand install.sh (whose env.sh exported only MODULO_INSTALL_ROOT) still finds its root"),
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
        if (read.relative_path, read.name) in _KNOWN_READ_BYPASSES:
            continue
        problems.append(f"{read.relative_path} reads {read.name!r} directly: registered names must be read through read_env so the legacy spelling keeps working (a direct read silently ignores it)")
    return problems


def registered_redeclarations(source: str, relative_path: str) -> list[str]:
    """Return every re-declaration of a registry-owned name outside the registry."""
    registry = _registry_owned_names()
    problems: list[str] = []
    for target, value in _assignments(ast.parse(source)):
        name = _literal_env_name(value)
        if name is None or name not in registry:
            continue
        if (relative_path, name) in _KNOWN_READ_BYPASSES:
            continue
        problems.append(f"{relative_path} declares {target} = {name!r}: import it from farna.launcher.env_contract so the registry stays the ONE definition")
    return problems


def _registry_module_path() -> str:
    return (PYTHON_SOURCE_ROOT / "launcher" / "env_contract.py").relative_to(REPO_ROOT).as_posix()


def test_python_readers_of_registered_names_go_through_the_registry() -> None:
    problems = reader_problems(_python_reads())
    assert not problems, problems


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

    ``MODULO_LAN_ORIGINS`` was documented as the operator knob while the
    middleware read the canonical name directly, so the documented spelling was
    a silent 403. The synthetic module is the same shape, one constant deeper.
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


# ---------------------------------------------------------------------------
# The documented half: a published knob must be accepted by SOMETHING
# ---------------------------------------------------------------------------

#: Documented ``MODULO_*`` knobs that NO mechanism accepts today: not a registry
#: alias, not a ``farna.settings._product_aliases`` declaration, and not read as
#: a literal anywhere in ``farna/``. Each is a real gap of the FAR-2563 class -
#: an operator who sets the documented spelling is silently ignored - and the
#: fix is to make it live and DELETE its line here (the companion test fails on
#: a stale entry). ``MODULO_ENV`` is the one deliberate exception: it was dropped
#: on purpose (only ``FARNA_CONTROL_ENV`` is read) and the docs say so, so it
#: stays here rather than being made live.
DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS: frozenset[str] = frozenset(
    {
        "MODULO_ADMIN_TOKEN",
        "MODULO_AGENT_MINT_BUDGET_LIMIT",
        "MODULO_AGENT_MINT_BUDGET_WINDOW_MINUTES",
        "MODULO_BACKUP_PASSPHRASE",
        "MODULO_CRON_STREAK_MIN_WINDOW_HOURS",
        "MODULO_DOCKER_ALLOW_INSECURE_ENDPOINT",
        "MODULO_DOCKER_HOST",
        "MODULO_E2B_API_KEY",
        "MODULO_ENV",
        "MODULO_KUBERNETES_ENABLED",
        "MODULO_KUBERNETES_NAMESPACE",
        "MODULO_KUBERNETES_SERVICE_ACCOUNT",
        "MODULO_MAX_RATE_USD",
        "MODULO_MAX_REPORTABLE_BAND_USD",
        "MODULO_MAX_REPORTABLE_USD_MIN",
        "MODULO_MAX_SELF_REPORTED_USD",
        "MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS",
        "MODULO_PRODUCT_ANALYTICS_ENABLED",
        "MODULO_PRODUCT_ANALYTICS_ENDPOINT_URL",
        "MODULO_PRODUCT_ANALYTICS_INSTANCE_SECRET",
        "MODULO_RUNNER_MACHINE_ID",
        "MODULO_SCHEMA_VALIDATOR_MODE",
        "MODULO_STREAK_DEACTIVATE_KILL_SWITCH",
    }
)


#: Both prefixes a documented service-contract knob may carry. The rebrand made
#: the canonical ``FARNA_CONTROL_`` spelling the bulk of the published page, so a
#: scrape floor counted over the legacy ``MODULO_`` names alone would measure the
#: upgrade section, not the contract.
_DOCUMENTED_ENV_NAME_RE = re.compile(r"\b(?:MODULO_|FARNA_CONTROL_)[A-Z0-9_]+\b")


def _documented_env_knobs() -> set[str]:
    text = CONFIGURATION_REFERENCE.read_text(encoding="utf-8")
    return {match.group(0) for match in _DOCUMENTED_ENV_NAME_RE.finditer(text) if not match.group(0).endswith("_")}


def _documented_modulo_knobs() -> set[str]:
    return {name for name in _documented_env_knobs() if name.startswith(env_contract.LEGACY_PREFIX)}


def _settings_legacy_aliases() -> set[str]:
    """The ``MODULO_*`` spellings ``farna.settings`` declares via _product_aliases."""
    tree = ast.parse(SETTINGS_MODULE.read_text(encoding="utf-8"))
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_product_aliases":
            for argument in node.args:
                name = _literal_env_name(argument)
                if name and name.startswith(env_contract.LEGACY_PREFIX):
                    aliases.add(name)
    return aliases


def _accepted_legacy_knobs() -> set[str]:
    accepted = {alias for aliases in env_contract.LEGACY_ALIASES.values() for alias in aliases}
    accepted |= _settings_legacy_aliases()
    accepted |= {read.name for read in _python_reads() if read.name.startswith(env_contract.LEGACY_PREFIX)}
    return accepted


def test_every_documented_legacy_knob_is_accepted_somewhere() -> None:
    """A documented spelling no reader accepts is the FAR-2563 403, not docs."""
    documented = _documented_modulo_knobs()
    # Floor over BOTH prefixes: it guards against the scrape silently losing the
    # page, and after the rebrand only the canonical FARNA_CONTROL_ half is large.
    all_documented = _documented_env_knobs()
    assert len(all_documented) > 90, len(all_documented)

    missing = sorted(documented - _accepted_legacy_knobs() - DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS)
    assert not missing, f"documented MODULO_* knobs no mechanism accepts (register an alias, add a Settings alias, or list a real gap): {missing}"


def test_the_documented_knob_gap_list_holds_only_live_gaps() -> None:
    """Burn-down discipline: a fixed knob must be deleted from the list."""
    accepted = _accepted_legacy_knobs()
    documented = _documented_modulo_knobs()

    assert not sorted(DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS & accepted), "now accepted - delete these entries"
    assert not sorted(DOCUMENTED_KNOBS_WITHOUT_AN_ALIAS - documented), "no longer documented - delete these entries"
