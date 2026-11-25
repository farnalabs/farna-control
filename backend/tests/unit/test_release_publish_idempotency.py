"""Contract checks for idempotent bundle publication on a recut (FAR-2789).

A bundle release is keyed on the version alone (``bundle-v<X.Y.Z>``), so a
recut of a version that already published must REPLACE the release assets
rather than refuse. These checks pin that contract against the workflow's own
construction: the upload order is read from the arrays the script builds, not
from a hand-copied expectation, so a reordering that drops the
manifest/signature-last guarantee fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-unified.yml"

PUBLISH_JOB = "publish-release"
UPLOAD_STEP = "Create GitHub Release with assets"
VERIFY_STEP = "Verify the published release assets and manifest signature"
TAG_STEP = "Push the bundle tag"

MANIFEST_NAME = "release-artefacts/RELEASE_MANIFEST.json"
SIGNATURE_NAME = "release-artefacts/RELEASE_MANIFEST.json.sig"
SUMS_NAME = "release-artefacts/SHA256SUMS"

_EXPECTED_TAG_ENV = "bundle-v${{ inputs.version }}"


def _workflow() -> dict[str, Any]:
    loaded = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), "release-unified.yml did not parse as a mapping"
    return loaded


def _step_names() -> list[str]:
    return [str(step.get("name")) for step in _workflow()["jobs"][PUBLISH_JOB]["steps"]]


def _single_step(step_name: str) -> dict[str, Any]:
    steps = [step for step in _workflow()["jobs"][PUBLISH_JOB]["steps"] if step.get("name") == step_name]
    assert len(steps) == 1, f"expected exactly one {step_name!r} step, found {len(steps)}"
    return dict(steps[0])


def _publication_arms(script: str) -> tuple[str, str]:
    """Split the publication script into its existing-release and create arms.

    The YAML block scalar is de-indented when it is parsed, so the ``else`` of
    the ``if gh release view`` is at column 0. Slicing from the guard keeps this
    off the unrelated ``newest_version`` decision earlier in the same step.
    """
    start = script.index('if gh release view "$TAG"')
    existing, create = script[start:].split("\nelse\n", maxsplit=1)
    return existing, create


def _logical_lines(script: str) -> list[str]:
    """One entry per shell command, with backslash continuations joined."""
    return [line.strip() for line in script.replace("\\\n", " ").splitlines() if line.strip()]


def _upload_commands(script: str) -> list[str]:
    return [line for line in _logical_lines(script) if line.startswith("gh release upload")]


def _array_items(script: str, name: str) -> list[str]:
    """The string literals of the bash array ``name`` as the script declares it."""
    start = script.index(f"{name}=(")
    end = script.index(")", start)
    return re.findall(r'"([^"]+)"', script[start:end])


def test_existing_release_replaces_assets_with_clobber() -> None:
    script = _single_step(UPLOAD_STEP)["run"]
    existing, create = _publication_arms(script)

    uploads = _upload_commands(existing)
    assert uploads, "the recut arm must re-upload the assets"
    for command in uploads:
        assert "--clobber" in command.split(), f"every recut upload must clobber the existing asset: {command!r}"

    assert not _upload_commands(create), "a first publish must go through the atomic gh release create, never a separate upload"
    assert "--clobber" not in create


def test_manifest_and_signature_are_written_last() -> None:
    script = _single_step(UPLOAD_STEP)["run"]
    assets = _array_items(script, "ASSETS")
    manifest_assets = _array_items(script, "MANIFEST_ASSETS")

    assert len(manifest_assets) == 3, f"the manifest trio must stay exactly three assets, got {manifest_assets}"

    # The split IS the replacement order: the manifest trio never rides in the
    # binary/portable group that is uploaded first.
    for name in manifest_assets:
        assert name not in assets, f"{name} must be uploaded after the binary/portable assets, not with them"

    # The trio's own order puts the manifest second-last and the signature last,
    # so the completeness marker and its signature are the final writes.
    assert manifest_assets[0] == SUMS_NAME
    assert manifest_assets[1] == MANIFEST_NAME
    assert manifest_assets[2] == SIGNATURE_NAME

    # openapi.json is not a manifest artefact: it belongs to the first group.
    assert "release-artefacts/openapi.json" in assets

    # And the recut arm writes the groups in that order: the portable set first,
    # then a loop over the manifest trio (whose iteration order is the array
    # above), so the signature is the final asset write.
    existing, _ = _publication_arms(script)
    logical = _logical_lines(existing)
    uploads = _upload_commands(existing)
    assert "${ASSETS[@]}" in uploads[0], "the portable/binary assets must be replaced first"

    trio_loop = next(line for line in logical if line.startswith("for ") and "${MANIFEST_ASSETS[@]}" in line)
    trio_loop_index = logical.index(trio_loop)
    assert trio_loop_index > logical.index(uploads[0]), "the manifest trio must be uploaded after the portable set"
    loop_var = trio_loop.split()[1]
    loop_body = logical[trio_loop_index + 1]
    assert loop_body.startswith("gh release upload")
    assert f'"${loop_var}"' in loop_body
    assert "--clobber" in loop_body.split()


def test_post_upload_verification_proves_the_published_bytes() -> None:
    names = _step_names()
    assert VERIFY_STEP in names, "publication must be followed by a post-upload verification step"
    assert names.index(VERIFY_STEP) == names.index(UPLOAD_STEP) + 1, "verification must run straight after the upload"

    verify = _single_step(VERIFY_STEP)
    assert verify["if"] == "steps.ticket-gate.outputs.publish == 'true'"

    script = verify["run"]
    # The expected set is the inventory the upload step recorded, so the proof
    # covers exactly what this run published.
    assert "expected-release-assets.txt" in script
    assert "expected-release-assets.txt" in _single_step(UPLOAD_STEP)["run"]
    # Every published asset must exist at the local byte size.
    assert 'gh release view "$TAG"' in script
    assert "--json assets" in script
    assert "stat -c %s" in script
    # And the PUBLISHED manifest/signature pair must verify under the checked-in
    # trust store, the same helper the pre-publish check uses.
    assert "verify_release_from_files" in script
    assert "RELEASE_MANIFEST.json.sig" in script
    # Any failure is loud.
    assert "::error::" in script


def test_bundle_tag_remains_keyed_on_the_version_alone() -> None:
    upload = _single_step(UPLOAD_STEP)
    tag = upload["env"]["TAG"]
    assert tag == _EXPECTED_TAG_ENV
    assert "rc" not in tag.lower(), "the bundle tag must never carry an RC qualifier"

    bundle = _single_step(TAG_STEP)["run"]
    assert 'BUNDLE_TAG="bundle-v${VERSION}"' in bundle
    assert "bundle-v${RC_TAG}" not in bundle


def test_latest_decision_is_unchanged() -> None:
    script = _single_step(UPLOAD_STEP)["run"]
    logical = "\n".join(_logical_lines(script))

    assert 'gh release list --repo "$GITHUB_REPOSITORY" --limit 1000 --json tagName' in logical
    # Only a plain X.Y.Z bundle is a candidate for Latest: an RC-qualified tag
    # must not match this filter.
    assert r"""sed -n 's/^bundle-v\([0-9][0-9.]*\)$/\1/p'""" in logical
    assert 'if [ "$newest_version" = "$VERSION" ]; then' in logical
    assert "PUBLISH_ARGS=(--prerelease=false --latest)" in script
    assert "PUBLISH_ARGS=(--prerelease=false --latest=false)" in script


def test_recut_never_deletes_the_published_release() -> None:
    script = _single_step(UPLOAD_STEP)["run"]
    assert "gh release delete" not in script


def test_the_pre_fix_refusal_is_gone_and_replacement_is_used() -> None:
    """Prove-the-fix: fails against the pre-FAR-2789 workflow text.

    The old step warned ``uploading assets without clobbering`` and never passed
    ``--clobber``, so both assertions below are false on that version.
    """
    script = _single_step(UPLOAD_STEP)["run"]
    existing, _ = _publication_arms(script)

    assert "without clobbering" not in script
    assert "--clobber" in existing.split()
    assert 'gh release upload "$TAG" "${ASSETS[@]}"' in existing
