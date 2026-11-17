"""Architecture tests for the import-linter policy boundary.

Import-linter owns the module dependency graph. These tests only guard the
configuration's policy shape and exception ratchet, which are semantic rules
that import-linter does not enforce itself.
"""

from __future__ import annotations

import configparser
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[2] / ".importlinter"


_EXPECTED_CONTRACTS = {
    "api-does-not-import-langgraph-directly",
    "connectors-do-not-import-api",
    "core-does-not-import-modulo-cloud",
    "otel-bridge-is-standalone",
    "core-does-not-import-api",
    "db-does-not-import-core",
    "connectors-do-not-import-core",
    "no-concrete-runtime-provider-imports",
    "shared-utility-is-protected",
}


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CONFIG_PATH)
    return parser


def test_import_linter_configuration_declares_only_known_contracts() -> None:
    parser = _config()
    contracts = {section.removeprefix("importlinter:contract:") for section in parser.sections() if section.startswith("importlinter:contract:")}
    assert contracts == _EXPECTED_CONTRACTS


def test_every_contract_exception_is_documented() -> None:
    parser = _config()
    raw_config = CONFIG_PATH.read_text()
    undocumented: list[str] = []
    sections = raw_config.split("\n[")
    raw_sections = {section.splitlines()[0].removeprefix("[").removesuffix("]"): section for section in sections if section.startswith("importlinter:contract:")}
    for section in parser.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        ignored = parser.get(section, "ignore_imports", fallback="").splitlines()
        for raw_entry in ignored:
            entry = raw_entry.strip()
            if not entry or entry.startswith("#"):
                continue
            if " -> " not in entry:
                undocumented.append(f"{section}: malformed ignore entry {entry!r}")
        if ignored and "#" not in raw_sections[section]:
            undocumented.append(f"{section}: ignored imports require a nearby explanatory comment")
    assert not undocumented, "Every import-linter exception must be explicit and documented:\n" + "\n".join(undocumented)
