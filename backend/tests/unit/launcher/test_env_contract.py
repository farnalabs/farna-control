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
not the raw file text: a canonical name appearing only in a log line or a comment
must never satisfy the gate (it was tried - an ``echo`` of the canonical name
masked an export that still used the legacy spelling).

Scope: the launcher / installer / container-entrypoint / Helm surface, i.e. the
places that supply process environment to the app directly. Operator knobs the
app resolves through ``Settings`` aliases (``MODULO_DB``, ``MODULO_USERS``, ...)
are a different mechanism - ``farna.settings._product_aliases`` owns those pairs,
so a compose file setting one is not drift.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from farna.launcher import env_contract

REPO_ROOT = Path(__file__).resolve().parents[4]

#: Shell ``export NAME=...`` (both spellings).
SHELL_EXPORT_WRITE = r"^\s*export\s+({names})="
#: C ``setenv("NAME", ...)`` and wide-string ``L"NAME"`` literals.
C_SETENV_WRITE = r'setenv\("({names})"'
C_WIDE_LITERAL_WRITE = r'L"({names})"'
#: Kubernetes env entry ``- name: NAME``.
K8S_ENV_WRITE = r"^\s*-\s*name:\s+({names})\s*$"
#: Shell ``${NAME:-...}`` / ``${NAME}`` expansion.
SHELL_READ = r"\$\{({names})[:\}]"

#: The two prefixes a service-contract name may carry.
_NAME_CLASS = r"(?:MODULO_|FARNA_CONTROL_)[A-Z0-9_]+"


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
    # Windows bundled launcher (wide-string literals in its clean env block).
    ServiceScript("scripts/windows/modulo-launcher.c", writes=(_pattern(C_WIDE_LITERAL_WRITE),)),
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
        _pattern(C_WIDE_LITERAL_WRITE),
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


def _matched_names(text: str, pattern: str) -> set[str]:
    return set(re.findall(pattern, text, re.MULTILINE))


def contract_problems(text: str, *, writes: tuple[str, ...] = (), reads: tuple[str, ...] = ()) -> list[str]:
    """Return every one-sided ``MODULO_*`` production/consumption in ``text``.

    A ``MODULO_*`` name passes only when it is a registered legacy alias AND its
    canonical name is produced in the SAME context (same match set for that
    pattern kind), or when it is a documented exemption. Everything else is a
    drift report.
    """
    mapping = _legacy_to_canonical()
    problems: list[str] = []
    for verb, participle, patterns in (("writes", "written", writes), ("reads", "read", reads)):
        for pattern in patterns:
            names = _matched_names(text, pattern)
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
    assert not contract_problems(text, writes=script.writes, reads=script.reads)


@pytest.mark.parametrize(
    ("relative_path", "pattern", "required"),
    _REQUIRED_CANONICAL_PRODUCTIONS,
    ids=[relative_path for relative_path, _pattern, _required in _REQUIRED_CANONICAL_PRODUCTIONS],
)
def test_producers_emit_the_canonical_names(relative_path: str, pattern: str, required: tuple[str, ...]) -> None:
    """Positive half: the canonical name is actually produced, not merely unopposed."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    produced = _matched_names(text, pattern)
    for canonical in required:
        assert canonical in produced, f"{relative_path} must produce {canonical}; produced={sorted(produced)}"


def test_the_sweep_sees_the_real_modulo_names_it_claims_to_check() -> None:
    """A sweep whose regexes match nothing must never read as clean.

    The legacy names below are the ones this gate exists for; if the patterns
    stop matching them the gate would pass vacuously.
    """
    install = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    produced = _matched_names(install, _pattern(SHELL_EXPORT_WRITE))
    assert {"MODULO_INSTALL_ROOT", "MODULO_BIN_DIR"} <= produced

    macos_install = (REPO_ROOT / "scripts" / "macos" / "install.sh").read_text(encoding="utf-8")
    consumed = _matched_names(macos_install, _pattern(SHELL_READ))
    assert {"MODULO_INSTALL_ROOT", "MODULO_DATA_DIR"} <= consumed


# ---------------------------------------------------------------------------
# Mutation proof: the gate actually fails on the incidents it was written for
# ---------------------------------------------------------------------------


def test_gate_flags_the_far_2563_launcher_drift() -> None:
    """Mutation: the exact pre-fix launcher body must be reported, not passed."""
    pre_fix = 'setenv("PYTHONPATH", src, 1);\nsetenv("MODULO_SERVE_SPA", "1", 1);\nsetenv("MODULO_FRONTEND_DIST", frontend_dist, 1);\n'
    problems = contract_problems(pre_fix, writes=(_pattern(C_SETENV_WRITE),))

    assert len(problems) == 2, problems
    assert any(env_contract.SERVE_SPA_ENV in problem for problem in problems)
    assert any(env_contract.FRONTEND_DIST_ENV in problem for problem in problems)


def test_gate_flags_a_legacy_read_with_no_canonical_read() -> None:
    """Mutation: a reader that only accepts the pre-rebrand spelling is drift."""
    drifted = 'INSTALL_ROOT="${MODULO_INSTALL_ROOT:-/opt}"\n'
    problems = contract_problems(drifted, reads=(_pattern(SHELL_READ),))

    assert len(problems) == 1, problems
    assert env_contract.INSTALL_ROOT_ENV in problems[0]


def test_gate_flags_an_echoed_canonical_name_that_is_not_exported() -> None:
    """Mutation: naming the canonical variable in a log line must not satisfy the gate.

    This exact shape was live during the fix: the entrypoint echoed the canonical
    name while still exporting only the legacy one, and a file-text check passed.
    """
    drifted = 'export MODULO_SYSTEM_DATABASE_URL="$SYSTEM_URL"\necho "FARNA_CONTROL_SYSTEM_DATABASE_URL fixed"\n'
    problems = contract_problems(drifted, writes=(_pattern(SHELL_EXPORT_WRITE),))

    assert len(problems) == 1, problems
    assert env_contract.SYSTEM_DATABASE_URL_ENV in problems[0]


def test_gate_flags_an_unregistered_legacy_name() -> None:
    """Mutation: a brand-new MODULO_-only variable must be registered or renamed."""
    invented = 'export MODULO_BRAND_NEW_KNOB="1"\n'
    problems = contract_problems(invented, writes=(_pattern(SHELL_EXPORT_WRITE),))

    assert len(problems) == 1, problems
    assert "MODULO_BRAND_NEW_KNOB" in problems[0]


def test_gate_passes_a_correctly_paired_export() -> None:
    """The shipped installer pattern: canonical export plus a compatibility alias."""
    paired = 'export FARNA_CONTROL_INSTALL_ROOT="${INSTALL_ROOT}"\nexport MODULO_INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT}"\n'
    assert not contract_problems(paired, writes=(_pattern(SHELL_EXPORT_WRITE),))


def test_gate_passes_a_paired_read() -> None:
    paired = 'INSTALL_ROOT="${FARNA_CONTROL_INSTALL_ROOT:-${MODULO_INSTALL_ROOT:-/opt}}"\n'
    assert not contract_problems(paired, reads=(_pattern(SHELL_READ),))


def test_gate_passes_an_exempt_knob() -> None:
    exempt = 'X="${MODULO_MACOS_REHEARSAL_ALLOW_UNSTAPLED_TICKET:-}"\n'
    assert not contract_problems(exempt, reads=(_pattern(SHELL_READ),))
