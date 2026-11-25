# copy-consistency: allow-file
"""Tests for ``scripts/copy_consistency.py`` (FAR-2778).

The file carries the allow-file marker because its fixtures are the very
strings the checker reports; a fixture corpus is not copy.

Ported from the canonical ``harness/tools/tests/test_copy_consistency.py`` so
the vendored checker's CLI contract is proven in this repo, not only by
``--self-test`` (which calls ``scan_paths`` directly and never exercises
``main()``'s exit codes).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

for _parent in Path(__file__).resolve().parents:
    _candidate = _parent / "scripts" / "copy_consistency.py"
    if _candidate.exists():
        SCRIPT = _candidate
        break
else:  # pragma: no cover - the repository always ships the script
    raise RuntimeError("Could not locate scripts/copy_consistency.py")

SPEC = importlib.util.spec_from_file_location("copy_consistency", SCRIPT)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["copy_consistency"] = MODULE
SPEC.loader.exec_module(MODULE)


def rule_names(
    text: str,
    path: str = "docs/copy.md",
    *,
    customer_copy: bool = False,
) -> set[str]:
    return {finding.rule for finding in MODULE.scan_text(text, path, customer_copy=customer_copy)}


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv: this interpreter and a repo-owned script
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("rule", "text", "customer_copy"),
    [
        ("app-farna-dev", "Open app.farna.dev in a browser.", False),
        ("old-repo-url", "Clone github.com/farnalabs/modulo first.", False),
        (
            "old-repo-url",
            "Call api.github.com/repos/farnalabs/modulo today.",
            False,
        ),
        ("retired-host", "The old site is modulo.run.", False),
        ("opt-in-only", "The dashboard is opt-in only.", True),
        ("milestone-label", "Dashboards are planned for GA.", True),
        ("milestone-label", "Retries leave Beta scope later.", True),
        ("cd-product-name", "Run cd Farna Control before the first task.", False),
        ("retired-product-name", "Modulo governs the pipeline run.", True),
    ],
)
def test_each_rule_fires_on_its_fixture(rule: str, text: str, customer_copy: bool) -> None:
    assert rule_names(text, customer_copy=customer_copy) == {rule}


@pytest.mark.parametrize(
    "text",
    [
        "Send X-Modulo-Webhook-Secret with each request.",
        "Send X-Modulo-Timestamp with each request.",
        "Download Modulo-1.2.3-macos-arm64.dmg.",
        "Download modulo-1.2.3-linux-x64.tar.gz.",
        "The pinned artefact is Modulo-2.0.0.",
        "Download Modulo-<version>-macos-arm64.dmg today.",
        "Notes live in Repos/modulo/backend/README.md.",
        "The repo is farnalabs/farna-control and the site is farna.dev.",
    ],
)
def test_exempt_identifiers_do_not_fire(text: str) -> None:
    # Checked in the strict mode, where the brand rule actually runs.
    assert rule_names(text, customer_copy=True) == set()


def test_hyphenated_brand_compound_is_not_an_artefact() -> None:
    assert rule_names("Modulo-based governance.", customer_copy=True) == {"retired-product-name"}


@pytest.mark.parametrize(
    "text",
    [
        "Write to admin@modulo.run for workspace access.",
        "Contact security@modulo.run about an incident.",
        "Support is at support@modulo.run.",
        "Container labels include modulo.run.id, modulo.org.id, modulo.node.id.",
        "The assertion is that copy must not carry @modulo.run.",
    ],
)
def test_host_identifiers_are_not_reported(text: str) -> None:
    # An email address and a label key are exact identifiers, not a host.
    for customer_copy in (False, True):
        assert "retired-host" not in rule_names(text, customer_copy=customer_copy)


@pytest.mark.parametrize(
    "text",
    [
        "Read https://modulo.run/x for details.",
        "Open modulo.run/x in a browser.",
        "The old site is modulo.run.",
        "The legacy modulo.run domain still redirects.",
        "Visit modulo.run: it redirects to farna.dev.",
    ],
)
def test_real_host_forms_still_fire(text: str) -> None:
    assert "retired-host" in rule_names(text)


def test_host_rule_reports_the_host_not_the_adjacent_email() -> None:
    findings = MODULE.scan_text(
        "Mail support@modulo.run or read https://modulo.run/docs.",
        "docs/copy.md",
    )
    hosts = [finding for finding in findings if finding.rule == "retired-host"]
    assert [finding.text for finding in hosts] == ["modulo.run"]


def test_customer_rules_follow_the_flag_not_the_path() -> None:
    text = "Modulo governs it. The demo is opt-in only. Ship in Beta scope."
    for path in ("docs/overview.md", "backend/src/service.py", "notes.txt"):
        assert rule_names(text, path) == set()
        assert rule_names(text, path, customer_copy=True) == {
            "milestone-label",
            "opt-in-only",
            "retired-product-name",
        }


def test_hint_names_the_flag_when_it_is_absent(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "copy.md").write_text("Farna Control runs it.\n", encoding="utf-8")
    plain = run_cli(str(docs))
    assert plain.returncode == 0
    assert "--customer-copy" in plain.stderr
    flagged = run_cli("--customer-copy", str(docs))
    assert flagged.returncode == 0
    assert flagged.stderr == ""


def test_line_marker_suppresses_one_line(tmp_path: Path) -> None:
    copy_file = tmp_path / "docs" / "copy.md"
    copy_file.parent.mkdir()
    copy_file.write_text(
        "Modulo here. <!-- copy-consistency: allow -->\nModulo reported.\n",
        encoding="utf-8",
    )
    findings = MODULE.scan_text(
        copy_file.read_text(encoding="utf-8"),
        str(copy_file),
        customer_copy=True,
    )
    assert [finding.line for finding in findings] == [2]


def test_file_marker_suppresses_the_whole_file(tmp_path: Path) -> None:
    copy_file = tmp_path / "docs" / "copy.md"
    copy_file.parent.mkdir()
    copy_file.write_text(
        "# copy-consistency: allow-file\nModulo is quoted here.\n",
        encoding="utf-8",
    )
    findings = MODULE.scan_text(
        copy_file.read_text(encoding="utf-8"),
        str(copy_file),
        customer_copy=True,
    )
    assert not findings


def test_clean_directory_exits_zero(tmp_path: Path) -> None:
    copy_file = tmp_path / "docs" / "copy.md"
    copy_file.parent.mkdir()
    copy_file.write_text("Farna Control runs the pipeline.\n", encoding="utf-8")
    result = run_cli(str(tmp_path))
    assert result.returncode == 0
    assert "0 violations in 1 files" in result.stdout


def test_violation_exits_one_and_names_the_file(tmp_path: Path) -> None:
    copy_file = tmp_path / "docs" / "copy.md"
    copy_file.parent.mkdir()
    copy_file.write_text("Modulo governs it.\nThe site is modulo.run.\n", encoding="utf-8")
    result = run_cli("--customer-copy", str(tmp_path))
    assert result.returncode == 1
    assert "2 violations in 1 files" in result.stdout
    assert "docs/copy.md:1: retired-product-name: Modulo" in result.stdout
    assert "docs/copy.md:2: retired-host: modulo.run" in result.stdout


def test_allow_modulo_run_flag_suppresses_the_host_rule(tmp_path: Path) -> None:
    copy_file = tmp_path / "docs" / "copy.md"
    copy_file.parent.mkdir()
    copy_file.write_text("The old modulo.run host redirects to farna.dev.\n", encoding="utf-8")
    for mode in ([], ["--customer-copy"]):
        assert run_cli(*mode, str(copy_file)).returncode == 1
        assert run_cli(*mode, "--allow-modulo-run", str(copy_file)).returncode == 0


def test_milestone_label_and_opt_in_only_need_the_flag(tmp_path: Path) -> None:
    copy_file = tmp_path / "copy.md"
    copy_file.write_text(
        "Dashboards are planned for GA. The demo is opt-in only.\n",
        encoding="utf-8",
    )
    plain = run_cli(str(copy_file))
    assert plain.returncode == 0
    assert "milestone-label" not in plain.stdout
    flagged = run_cli("--customer-copy", str(copy_file))
    assert flagged.returncode == 1
    assert "milestone-label: planned for GA" in flagged.stdout
    assert "opt-in-only: opt-in only" in flagged.stdout


def test_retired_product_name_needs_the_flag(tmp_path: Path) -> None:
    copy_file = tmp_path / "copy.md"
    copy_file.write_text("Modulo governs the run.\n", encoding="utf-8")
    assert run_cli(str(copy_file)).returncode == 0
    assert run_cli("--customer-copy", str(copy_file)).returncode == 1


def test_universal_rules_fire_in_both_modes(tmp_path: Path) -> None:
    copy_file = tmp_path / "copy.md"
    copy_file.write_text(
        "Open app.farna.dev. Clone github.com/farnalabs/modulo. Run cd Farna Control.\n",
        encoding="utf-8",
    )
    for mode in ([], ["--customer-copy"]):
        result = run_cli(*mode, str(copy_file))
        assert result.returncode == 1
        assert "app-farna-dev: app.farna.dev" in result.stdout
        assert "old-repo-url: github.com/farnalabs/modulo" in result.stdout
        assert "cd-product-name: cd Farna Control" in result.stdout


def test_directory_under_agents_worktrees_is_scanned(tmp_path: Path) -> None:
    # Every delivery worktree lives under .agents/worktrees, so a skip rule
    # matching the scan root's own ancestors made the scan a silent no-op.
    src = tmp_path / ".agents" / "worktrees" / "demo" / "src"
    src.mkdir(parents=True)
    (src / "copy.md").write_text("The old site is modulo.run.\n", encoding="utf-8")
    result = run_cli(str(src))
    assert result.returncode == 1
    assert "1 violations in 1 files" in result.stdout
    assert "retired-host: modulo.run" in result.stdout


def test_nested_skip_directories_are_still_skipped(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    (docs / "node_modules" / "pkg").mkdir(parents=True)
    (docs / ".git").mkdir()
    (docs / "copy.md").write_text("Farna Control runs the pipeline.\n", encoding="utf-8")
    nested = "Modulo governs the run.\n"
    (docs / "node_modules" / "pkg" / "readme.md").write_text(nested, encoding="utf-8")
    (docs / ".git" / "hooks.md").write_text(nested, encoding="utf-8")
    result = run_cli(str(docs))
    assert result.returncode == 0
    assert "0 violations in 1 files" in result.stdout


def test_explicit_file_under_agents_is_scanned(tmp_path: Path) -> None:
    worktree = tmp_path / ".agents" / "worktrees" / "demo"
    worktree.mkdir(parents=True)
    # An explicitly named file is scanned whatever its suffix.
    copy_file = worktree / "notes.txt"
    copy_file.write_text("The old site is modulo.run.\n", encoding="utf-8")
    result = run_cli(str(copy_file))
    assert result.returncode == 1
    assert "1 violations in 1 files" in result.stdout


def test_empty_directory_exits_two(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_cli(str(empty))
    assert result.returncode == 2
    assert "no scannable files under" in result.stderr
    assert "0 violations" not in result.stdout


def test_directory_with_only_unsupported_suffixes_exits_two(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "logo.png").write_bytes(b"\x89PNG\r\n")
    result = run_cli(str(assets))
    assert result.returncode == 2
    assert "no scannable files under" in result.stderr
    assert "0 violations" not in result.stdout


def test_missing_path_exits_two(tmp_path: Path) -> None:
    result = run_cli(str(tmp_path / "does-not-exist"))
    assert result.returncode == 2
    assert "no such file or directory" in result.stderr


def test_no_paths_exits_two() -> None:
    result = run_cli()
    assert result.returncode == 2


def test_self_test_exits_zero() -> None:
    result = run_cli("--self-test")
    assert result.returncode == 0
    assert "self-test OK" in result.stdout
