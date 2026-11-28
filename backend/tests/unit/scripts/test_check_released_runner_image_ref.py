"""Unit tests for ``scripts/check_released_runner_image_ref.py`` (FAR-2853).

The release gate is what stops a release from shipping the all-zero
placeholder digest (or a mutable tag, or the wrong repository) as the seeded
Bundled Runner image reference. These tests exercise the REAL module loaded
from the repository script - the validation function, the source extractor and
``_main`` - so a regression in any arm fails here rather than at release time.
"""

from __future__ import annotations

import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

for _parent in Path(__file__).resolve().parents:
    _script_path = _parent / "scripts" / "check_released_runner_image_ref.py"
    if _script_path.exists():
        break
else:  # pragma: no cover - the repository always ships the script
    raise RuntimeError("Could not locate scripts/check_released_runner_image_ref.py")

_loader = SourceFileLoader("check_released_runner_image_ref", str(_script_path))
_module = module_from_spec(spec_from_loader("check_released_runner_image_ref", _loader))
assert _module is not None, "could not build the check_released_runner_image_ref module spec"
sys.modules[_loader.name] = _module
_loader.exec_module(_module)

_REAL_REF = "ghcr.io/farnalabs/farna-control/runner-opencode@sha256:" + "a" * 64
_PLACEHOLDER_REF = "ghcr.io/farnalabs/farna-control/runner-opencode@sha256:" + "0" * 64


def _write_template(tmp_path: Path, image_ref: str) -> Path:
    template = tmp_path / "bundled_runner_template.py"
    template.write_text(f'BUNDLED_RUNNER_IMAGE_REF = "{image_ref}"\n', encoding="utf-8")
    return template


def test_real_digest_passes() -> None:
    assert _module.released_runner_image_ref_error(_REAL_REF) is None


def test_placeholder_digest_fails_and_names_the_value() -> None:
    error = _module.released_runner_image_ref_error(_PLACEHOLDER_REF)
    assert error is not None
    assert _PLACEHOLDER_REF in error


@pytest.mark.parametrize(
    "bad_ref",
    [
        "ghcr.io/farnalabs/farna-control/runner-opencode@sha256:abc",
        "ghcr.io/farnalabs/farna-control/runner-opencode:latest",
        "ghcr.io/farnalabs/farna-control/runner-opencode@sha256:" + "A" * 64,
    ],
)
def test_malformed_digest_fails_and_names_the_value(bad_ref: str) -> None:
    error = _module.released_runner_image_ref_error(bad_ref)
    assert error is not None
    assert bad_ref in error


def test_wrong_repository_fails_and_names_the_value() -> None:
    bad_ref = "ghcr.io/farnalabs/farna-control/backend@sha256:" + "a" * 64
    error = _module.released_runner_image_ref_error(bad_ref)
    assert error is not None
    assert bad_ref in error


def test_legacy_modulo_runner_name_fails() -> None:
    bad_ref = "modulo-runner:opencode@sha256:" + "a" * 64
    error = _module.released_runner_image_ref_error(bad_ref)
    assert error is not None
    assert bad_ref in error


def test_extract_returns_the_assignment_value() -> None:
    source = 'OTHER = "x"\nBUNDLED_RUNNER_IMAGE_REF = ' + f'"{_REAL_REF}"' + "\n"
    assert _module.extract_image_ref(source) == _REAL_REF


def test_extract_rejects_a_missing_assignment() -> None:
    with pytest.raises(ValueError, match="BUNDLED_RUNNER_IMAGE_REF"):
        _module.extract_image_ref('SOMETHING_ELSE = "x"\n')


def test_main_passes_on_a_real_injected_source(tmp_path: Path) -> None:
    template = _write_template(tmp_path, _REAL_REF)
    assert _module._main(["--template-path", str(template)]) == 0


def test_main_fails_on_a_placeholder_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    template = _write_template(tmp_path, _PLACEHOLDER_REF)
    assert _module._main(["--template-path", str(template)]) == 1
    assert _PLACEHOLDER_REF in capsys.readouterr().err


def test_main_fails_when_the_template_is_missing(tmp_path: Path) -> None:
    assert _module._main(["--template-path", str(tmp_path / "absent.py")]) == 1
