"""Architecture test: GitHub workflow Python pins hold one unified, supported version.

Renovate matches an ``actions/setup-python`` ``python-version`` input one workflow
pin at a time. PR #3548 bumped only the single-quoted Copy consistency pin to
3.15 while the other ci.yml pins (and every other workflow) stayed on 3.12;
3.15 is outside ``backend/pyproject.toml``'s ``requires-python`` range and has no
zstandard cp315 wheel, so the prod ``--no-build`` image broke. #3551 regenerated
the same bump. This test pins the invariant: every literal workflow
``python-version`` is the SAME version, and that version sits inside
``requires-python`` so a lone drift can never land again.
"""

import re
from pathlib import Path

import pytest

PRODUCT = Path(__file__).resolve().parent.parent.parent.parent  # Product/
PYPROJECT = PRODUCT / "backend" / "pyproject.toml"
WORKFLOWS = PRODUCT / ".github" / "workflows"

REQUIRES_PYTHON = re.compile(r'requires-python\s*=\s*">=3\.(\d+)(?:,<3\.(\d+))?"')
# Literal ``python-version: "3.12"`` / '3.12' / 3.12 pins only. Non-literal
# values (e.g. ``${{ matrix.python }}``) are expressions, not driftable pins.
PYTHON_VERSION = re.compile(r"""python-version:\s*["']?(\d+)\.(\d+)["']?""")


def test_workflow_python_pins_are_unified_and_in_range() -> None:
    """Every literal workflow ``python-version`` matches ``requires-python`` and the others."""
    if not PYPROJECT.exists():
        pytest.skip("backend/pyproject.toml not found")

    requires = REQUIRES_PYTHON.search(PYPROJECT.read_text(encoding="utf-8"))
    assert requires, "Could not parse requires-python in backend/pyproject.toml"
    min_minor = int(requires.group(1))
    max_minor = int(requires.group(2)) if requires.group(2) else None
    expected = f"3.{min_minor}"
    allowed = f">=3.{min_minor}" + (f",<3.{max_minor}" if max_minor is not None else "")

    pins: list[tuple[Path, str]] = []
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        for major, minor in PYTHON_VERSION.findall(workflow.read_text(encoding="utf-8")):
            pins.append((workflow.relative_to(PRODUCT), f"{major}.{minor}"))

    if not pins:
        pytest.skip("No literal python-version pins found in workflows")

    drifted = [(rel, version) for rel, version in pins if version != expected]
    assert not drifted, f"Workflow python-version pins must all be unified at {expected} and inside backend/pyproject.toml's requires-python ({allowed}); a single Renovate bump of one pin is exactly how #3548/#3551 broke the prod image. Drifted pins: " + ", ".join(
        f"{rel} ({version})" for rel, version in drifted
    )
