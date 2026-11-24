"""Behaviour pins for the persisted-value rebrand migration (dual-write/dual-read).

Companion to ``backend/tests/architecture/test_persisted_identifier_contract.py``.
That module pins the exact *literals*; this one exercises the *code paths* that
protect them, because a constant assertion alone cannot catch a changed read
path:

* the sandbox runner stamps BOTH the new and the legacy synthetic-failure
  markers (dual-write), so a run finalized by an older or newer instance still
  sees the marker;
* the finalize predicate treats the new AND the legacy marker as the same
  terminal/synthetic signal (dual-read), including a stored legacy telemetry row
  from a prior segment;
* the credential issuer accepts the legacy self-minted provenance value while
  writing the brand-free one, and its issuer companions agree.

Kept brand-free on purpose: it references the legacy values through the exported
constants, so the exact legacy literal lives in one baselined place (the contract
test) rather than being re-spelled here.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from farna.core.credential_issuer import (
    ISSUANCE_PROVENANCE_SELF_MINTED,
    ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY,
    ISSUANCE_PROVENANCES,
    IssuedRunCredential,
    default_credential_issuer,
)
from farna.core.pipeline_engine.executor import PipelineExecutor
from farna.core.pipeline_engine.node_runner import (
    LEGACY_SYNTHETIC_FAILURE_MARKERS,
    SYNTHETIC_FAILURE_MARKER,
    _build_sandbox_node_envelope,
    _SandboxNodeOutput,
)

_LEGACY_SYNTHETIC_MARKER = LEGACY_SYNTHETIC_FAILURE_MARKERS[0]


def _credential(provenance: str) -> IssuedRunCredential:
    return IssuedRunCredential(run_id=uuid.uuid4(), node_id="n1", secret="s3cr3t", provenance=provenance)


def test_legacy_self_minted_provenance_is_still_accepted() -> None:
    """The read side keeps accepting the pre-rebrand value until the drop release."""
    assert ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY in ISSUANCE_PROVENANCES
    credential = _credential(ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY)
    assert credential.provenance == ISSUANCE_PROVENANCE_SELF_MINTED_LEGACY


def test_default_issuer_writes_brand_free_companions() -> None:
    """The provenance value, the issuer name and the handle's issuer agree."""
    issuer = default_credential_issuer()
    assert issuer.provenance == ISSUANCE_PROVENANCE_SELF_MINTED
    assert issuer.name == "farna_control"
    described = issuer.describe()
    assert described["issuer"] == "farna_control"
    assert described["provenance"] == ISSUANCE_PROVENANCE_SELF_MINTED
    # The handle default is the same brand-free issuer, so an issued credential
    # carries it without the caller setting it.
    assert _credential(ISSUANCE_PROVENANCE_SELF_MINTED).issuer == "farna_control"


def test_synthetic_failure_envelope_dual_writes_both_markers() -> None:
    """The runner's synthetic-failure envelope carries both marker keys."""
    envelope = _build_sandbox_node_envelope(
        node_id="node-x",
        output=_SandboxNodeOutput(
            status="failed",
            summary="boom",
            exit_code=-1,
            wall_clock_time_ms=1,
            cost_estimate_usd=0.0,
            modulo_synthetic_failure=True,
        ),
        exclude_from_output=frozenset(),
    )
    telemetry = envelope["telemetry"]
    assert telemetry[SYNTHETIC_FAILURE_MARKER] is True
    for legacy_marker in LEGACY_SYNTHETIC_FAILURE_MARKERS:
        assert telemetry[legacy_marker] is True


def test_predicate_treats_new_and_legacy_markers_as_synthetic() -> None:
    """The finalize predicate accepts either key, and only on a failed status."""
    assert PipelineExecutor._is_sandbox_synthetic_failure_view({"status": "failed", SYNTHETIC_FAILURE_MARKER: True}) is True
    assert PipelineExecutor._is_sandbox_synthetic_failure_view({"status": "failed", _LEGACY_SYNTHETIC_MARKER: True}) is True
    # Precision boundaries: no marker, or a non-failed status, never downgrades.
    assert PipelineExecutor._is_sandbox_synthetic_failure_view({"status": "failed"}) is False
    assert PipelineExecutor._is_sandbox_synthetic_failure_view({"status": "completed", SYNTHETIC_FAILURE_MARKER: True}) is False


def test_downgrade_reads_a_stored_legacy_telemetry_row() -> None:
    """A prior-segment row that only carries the legacy key still downgrades."""
    executor = PipelineExecutor(MagicMock())
    status, error_code, error_detail = executor._downgrade_masked_sandbox_failures(
        run_id=uuid.uuid4(),
        final_status="complete",
        error_code=None,
        error_detail=None,
        completed_node_outputs={},
        node_type_map={"node-x": "sandbox_agent"},
        stored_node_outputs={},
        stored_node_telemetry={"node-x": {"status": "failed", _LEGACY_SYNTHETIC_MARKER: True}},
    )
    assert status == "failed"
    assert error_code == "sandbox.agent_failed"
    assert error_detail == "Sandbox agent node(s) failed: node-x"
