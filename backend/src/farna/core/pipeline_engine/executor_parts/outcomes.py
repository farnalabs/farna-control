"""Node-output and terminal-outcome classification for the executor.

Extracted verbatim from ``executor.py`` (step 1 of the package split in
``GAPS-harden-w2-executor.md`` §5). Every name stays importable from
``executor``; patch targets keep working through :func:`._ref` and
:data:`._log` (see ``support``).
"""

from __future__ import annotations

import uuid
from typing import Any

from farna.core.hitl_manager.gate_coalescing import SUPERSEDE_REASON
from farna.core.node_output_split import node_telemetry
from farna.core.pipeline_engine.error_codes import _CODE_CONNECTOR_FAILED, _CODE_SANDBOX_NO_OUTPUT_JSON
from farna.core.pipeline_engine.event_broker import RunEventBroker
from farna.core.pipeline_engine.model_backend_errors import classify_provider_error
from farna.core.pipeline_engine.node_runner import SandboxNodeFailedError
from farna.core.pipeline_engine.retry_policy import _ERROR_CODE_AGENT_FAILED, sandbox_failure_error_code

from .support import _log, _ref, _sanitize_detail, _StreamContext, _StreamState


def _map_lg_event(
    lg_event: dict[str, Any],
    node_ids: set[str],
) -> tuple[str, dict[str, Any]] | None:
    """Map a LangGraph astream_events event to (event_type, payload) or None."""
    event_kind = lg_event.get("event", "")
    name = lg_event.get("name", "")

    if name not in node_ids:
        return None

    if event_kind == "on_chain_start":
        return "node_started", {"node_id": name}
    if event_kind == "on_chain_end":
        return "node_completed", {"node_id": name}
    if event_kind == "on_chain_error":
        data = lg_event.get("data")
        error = data.get("error", "") if isinstance(data, dict) else ""
        return "node_failed", {"node_id": name, "error": str(error)}
    return None


def _node_output_stall_reason(node_output: Any) -> str | None:
    """Return the stall_reason carried by a captured sandbox-agent node output.

    NEW FORMAT (split envelope): node_output has ``telemetry`` key with
    ``stall_reason`` directly.

    LEGACY FORMAT: node_output has ``artifacts`` and ``output`` keys; the
    inner ``output`` dict carries ``stall_reason``.

    Returns None for non-dict / garbage output and for nodes that did not stall,
    so non-sandbox node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    # New split format: telemetry channel carries stall_reason
    telemetry = node_output.get("telemetry")
    if isinstance(telemetry, dict):
        reason = telemetry.get("stall_reason")
        return reason if isinstance(reason, str) and reason else None
    # Legacy format fallback: inner output dict carries stall_reason
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    reason = inner.get("stall_reason")
    return reason if isinstance(reason, str) and reason else None


def _node_output_agent_failure(node_output: Any) -> str | None:
    """Return a reason string when a captured node output self-reported failure.

    NEW FORMAT (split envelope): node_output has ``telemetry`` key with
    ``agent_status``/``agent_outcome`` directly.

    LEGACY FORMAT: node_output has ``artifacts`` and ``output`` keys; the
    inner ``output`` dict carries ``agent_status``/``agent_outcome``.

    A1 elevation fires when ``agent_status == "failed"`` OR ``outcome == "failed"``:
    a self-declared failure must NEVER land the run ``complete``, regardless of
    the exit code (§15.4). Returns None for non-dict / non-failed output, so
    non-sandbox node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    # New split format: telemetry channel carries agent_status/agent_outcome
    telemetry = node_output.get("telemetry")
    if isinstance(telemetry, dict):
        if telemetry.get("agent_status") != "failed" and telemetry.get("agent_outcome") != "failed":
            return None
        reason = telemetry.get("error") or telemetry.get("summary")
        if not isinstance(reason, str) or not reason:
            return "agent self-reported failure"
        return reason
    # Legacy format fallback
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    if inner.get("agent_status") != "failed" and inner.get("agent_outcome") != "failed":
        return None
    reason = inner.get("error") or inner.get("summary")
    if not isinstance(reason, str) or not reason:
        return "agent self-reported failure"
    return reason


def _connector_artifact_matches(
    artifact: Any,
    *,
    marked: bool,
    expected_node_id: str | None,
) -> bool:
    """Return whether an artifact is a trusted connector failure."""
    if not isinstance(artifact, dict) or artifact.get("status") != "failed":
        return False
    if marked:
        return True
    artifact_node_id = artifact.get("node_id")
    if expected_node_id is not None:
        return artifact_node_id == expected_node_id
    return isinstance(artifact_node_id, str) and bool(artifact_node_id)


def _connector_failure_artifact_reason(
    node_output: dict[str, Any],
    expected_node_id: str | None = None,
) -> str | None:
    """Return the first failure reason from a runner-stamped connector output's artifacts."""
    artifacts = node_output.get("artifacts")
    if not isinstance(artifacts, list):
        return None
    marked = node_output.get("connector_failure") is True
    for artifact in artifacts:
        if not _connector_artifact_matches(
            artifact,
            marked=marked,
            expected_node_id=expected_node_id,
        ):
            continue
        error = artifact.get("error")
        return error if isinstance(error, str) and error else "Connector reported failure"
    return None


def _connector_artifact_error_code(artifacts: Any) -> str | None:
    """Return the first non-empty code from a failed connector artifact."""
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if not isinstance(artifact, dict) or artifact.get("status") != "failed":
            continue
        code = artifact.get("error_code")
        if isinstance(code, str) and code:
            return code
    return None


def _node_output_connector_error_code(node_output: Any) -> str:
    """Return the error code for a connector failure envelope."""
    if not isinstance(node_output, dict):
        return _CODE_CONNECTOR_FAILED
    code = node_output.get("error_code")
    if isinstance(code, str) and code:
        return code
    return _connector_artifact_error_code(node_output.get("artifacts")) or _CODE_CONNECTOR_FAILED


def _node_output_connector_failure(
    node_output: Any,
    node_type: str | None = None,
    expected_node_id: str | None = None,
) -> str | None:
    """Return a connector/dispatch failure reason from a captured envelope."""
    if not isinstance(node_output, dict):
        return None
    marked = node_output.get("connector_failure") is True
    if not marked and node_type not in {"connector", "dispatch"}:
        return None
    return _connector_failure_artifact_reason(node_output, expected_node_id=expected_node_id) or ("Connector reported failure" if marked else None)


def _connector_failure_views(
    node_id: str,
    completed_node_outputs: dict[str, Any],
    stored_node_outputs: dict[str, Any],
    stored_node_telemetry: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return captured connector envelopes for one node, in precedence order."""
    views: list[dict[str, Any]] = []
    completed = completed_node_outputs.get(node_id)
    if isinstance(completed, dict):
        views.append(completed)
    stored = node_telemetry(stored_node_telemetry, stored_node_outputs, node_id)
    if isinstance(stored, dict):
        views.append(stored)
    return views


def _connector_failure_for_node(
    node_id: str,
    node_type_map: dict[str, str],
    completed_node_outputs: dict[str, Any],
    stored_node_outputs: dict[str, Any],
    stored_node_telemetry: dict[str, Any],
) -> tuple[str, str] | None:
    """Return ``(reason, code)`` for a captured connector failure, if present."""
    node_type = node_type_map.get(node_id)
    if node_type not in {"connector", "dispatch"}:
        return None
    for view in _connector_failure_views(node_id, completed_node_outputs, stored_node_outputs, stored_node_telemetry):
        reason = _node_output_connector_failure(view, node_type, expected_node_id=node_id)
        if reason is not None:
            return reason, _node_output_connector_error_code(view)
    return None


def _record_connector_failure(
    state: _StreamState,
    ctx: _StreamContext,
    node_id: str,
    output: dict[str, Any],
) -> None:
    """Latch a connector failure observed in a completed node envelope."""
    node_type = ctx.node_type_map.get(node_id) if ctx.node_type_map else None
    reason = _node_output_connector_failure(output, node_type, expected_node_id=node_id)
    if reason is not None:
        state.connector_failure_reason = reason
        state.connector_failure_code = _node_output_connector_error_code(output)


def _node_output_sandbox_session_lost(node_output: Any) -> str | None:
    """Return a reason string when a captured node output carries the
    session-lost marker.

    NEW FORMAT (split envelope): node_output has ``telemetry`` key with
    ``sandbox_session_lost`` directly.

    LEGACY FORMAT: node_output has ``artifacts`` and ``output`` keys; the
    inner ``output`` dict carries ``sandbox_session_lost``.

    node_runner stamps ``sandbox_session_lost: True`` on the output dict ONLY
    for the E2B wrapper's fallback echo (a dead opencode session). It is NOT an
    agent verdict — the agent never wrote output.json — so it must be routed to
    the retryable ``sandbox.no_output_json``, never the non-retryable
    ``agent.failed`` A1 elevation. Returns None for non-dict / non-marked
    output, so ordinary node outputs are never misread.
    """
    if not isinstance(node_output, dict):
        return None
    # New split format: telemetry channel
    telemetry = node_output.get("telemetry")
    if isinstance(telemetry, dict):
        if telemetry.get("sandbox_session_lost") is not True:
            return None
        reason = telemetry.get("error") or telemetry.get("summary")
        if not isinstance(reason, str) or not reason:
            return "No output from agent - session interrupted"
        return reason
    # Legacy format fallback
    inner = node_output.get("output")
    if not isinstance(inner, dict):
        return None
    if inner.get("sandbox_session_lost") is not True:
        return None
    reason = inner.get("error") or inner.get("summary")
    if not isinstance(reason, str) or not reason:
        return "No output from agent - session interrupted"
    return reason


def _node_output_text_surfaces(inner: dict[str, Any]) -> list[str]:
    """Top-level retained text surfaces of one envelope view.

    Scans the wrapper's text keys and the STRING members of ``output_json`` only
    (the wrapper's shape, and bounded by the stdout retention cap).
    """
    parts: list[str] = []
    for key in ("error", "summary", "error_message", "no_output_reason", "agent_stdout", "agent_stderr"):
        value = inner.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    output_json = inner.get("output_json")
    if isinstance(output_json, dict):
        parts.extend(v for v in output_json.values() if isinstance(v, str) and v)
    return parts


def _node_output_sandbox_session_lost_text(node_output: Any) -> str:
    """The retained text surfaces of a session-lost envelope.

    NEW FORMAT (split envelope): node_output has ``output`` (pure return) and
    ``telemetry`` keys. The ``telemetry`` carries the text surfaces.

    LEGACY FORMAT: node_output has ``artifacts`` and ``output`` keys.

    ``_node_output_sandbox_session_lost`` returns the marker REASON, which for
    the E2B wrapper's dead-session echo is the fixed PLACEHOLDER ("No output
    from agent - session interrupted"). The terminal provider error that killed
    the opencode session lives elsewhere in the retained text: the wrapper
    writes ``output.json`` as ``{"status": "failed", "summary": <placeholder>,
    "no_output_reason": <tail of the opencode output>}``, so an HTTP 402
    "Insufficient account funds" refusal is in ``no_output_reason`` (a JSON
    string — parsed here, so its inner quotes are real) and/or the captured
    stdout. This helper collects those surfaces so the caller can CLASSIFY the
    real cause instead of masking a non-retryable billing fault as the
    retryable ``sandbox.no_output_json``.

    Walks both envelope views — the telemetry channel and artifacts (legacy).
    Only top-level STRING members of ``output_json`` are scanned (the wrapper's
    shape, and bounded by the stdout retention cap). Returns "" for any shape
    that carries no text, so the caller falls back to the marker reason.
    """
    if not isinstance(node_output, dict):
        return ""
    candidates: list[Any] = []
    # New split format: telemetry channel
    telemetry = node_output.get("telemetry")
    if isinstance(telemetry, dict):
        candidates.append(telemetry)
    # Legacy format: artifacts and output
    artifacts = node_output.get("artifacts")
    if isinstance(artifacts, list):
        candidates.extend(a.get("output") for a in artifacts if isinstance(a, dict))
    legacy_output = node_output.get("output")
    if isinstance(legacy_output, dict):
        candidates.append(legacy_output)
    parts: list[str] = []
    for inner in candidates:
        if isinstance(inner, dict):
            parts.extend(_node_output_text_surfaces(inner))
    return "\n".join(parts)


def _should_skip_retry(node_id: str | None, markers: Any, run_id: Any) -> bool:
    """Guard B decision: should a transient node failure be suppressed
    as a retry because the run's delivery was already made?

    ``node_id`` is ``exc.node_id`` (only ``SandboxNodeFailedError`` carries it)
    or None — a None node_id (or a plain ``NodeCancelledError``) disables the
    gate. ``markers`` is the run's ALREADY-LOADED reassembled raw-output markers
    (the ``run_node_outputs`` store leg's markers — never
    a fresh SELECT). Fires only when any marker whose attempt_key embeds
    ``:node:<node_id>:`` and ``:run:<run_id>:`` (delimiters, never substring)
    carries ``delivery_done is True``. Non-dict ``markers`` (including
    MagicMock test doubles) are ignored — the gate stays silent.
    """
    if node_id is None:
        return False
    delivery_done: bool = _ref("_marker_delivery_done_for_node")(markers, run_id, node_id)
    return delivery_done


def _node_output_has_idempotency_gate(node_output: Any) -> bool:
    """True when a captured node output carries the idempotency-gate
    skip marker (``output_json.idempotency_gate``) — such a node must NOT
    re-fire agent_signal triggers.

    Keyed ONLY on the marker — never on ``status == "skipped"`` (template-error
    skips fire today and must keep firing). Handles both the outer
    ``{"output": {...}}`` envelope and the artifact-wrapped envelope returned by
    the sandbox node body (guard A / guard B shape).
    """
    if not isinstance(node_output, dict):
        return False
    candidates: list[Any] = [node_output.get("output")]
    artifacts = node_output.get("artifacts")
    if isinstance(artifacts, list):
        candidates.extend(a.get("output") for a in artifacts if isinstance(a, dict))
    for inner in candidates:
        if not isinstance(inner, dict):
            continue
        output_json = inner.get("output_json")
        if isinstance(output_json, dict) and output_json.get("idempotency_gate"):
            return True
    return False


def _is_supersede_rejection(claim: Any) -> bool:
    """True for a coalesced-supersede SYSTEM rejection.

    The committed ``decision_payload.reason`` is the supersede marker AND no
    account decided it (``decided_by`` is NULL - the supersede close-out never
    stamps one), so a human-typed reason can never spoof the label.
    """
    payload = claim.decision_payload
    reason = payload.get("reason") if isinstance(payload, dict) else None
    return reason == SUPERSEDE_REASON and claim.decided_by is None and claim.account_id is None


def _is_terminating_rejection_output(name: str, output: Any) -> bool:
    """True when a gate node's chain-end output is a stamped terminate-rejection.

    The gate's artifact must be for THIS gate (``node_id == name``),
    carry ``result == "rejected"`` and the compile-time
    ``reject_disposition == "terminate"`` stamp. (A rejected gate that routes or
    proceeds carries a different disposition and never ends the run.)
    """
    if not isinstance(output, dict):
        return False
    artifacts = output.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    return any(isinstance(a, dict) and a.get("node_id") == name and a.get("result") == "rejected" and a.get("reject_disposition") == "terminate" for a in artifacts)


def _record_chain_end_output(
    completed_node_outputs: dict[str, Any] | None,
    name: str,
    data: Any,
    run_trace_id: str | None,
    bridge: Any,
    lg_run_id: Any,
) -> Any | None:
    """Stamp and store a completed node's output envelope.

    Stamps the node's real OTel span id (and the run's trace id) onto a
    shallow copy of the envelope, stores it into ``completed_node_outputs``
    and returns the (possibly stamped) output so the caller can extract the
    stall / agent-failure / session-lost markers. Returns ``None`` when there
    is no envelope to capture (nothing stored).
    """
    if completed_node_outputs is None:
        return None
    output = data.get("output") if isinstance(data, dict) else None
    if output is None:
        return None
    # Stamp the node's real OTel span id (and the run's trace id)
    # onto a shallow copy of the envelope. The node_output_split splitter
    # folds unknown top-level envelope keys into the node_telemetry entry, so
    # these surface in the per-node span column — never in the pure return.
    node_span_id = bridge.span_id_for_run(lg_run_id)
    if isinstance(output, dict) and (node_span_id or run_trace_id):
        stamped = dict(output)
        if node_span_id:
            stamped["otel_span_id"] = node_span_id
        if run_trace_id:
            stamped["otel_trace_id"] = run_trace_id
        output = stamped
    completed_node_outputs[name] = output
    return output


def _record_node_markers(
    output: Any,
    broker: RunEventBroker,
    name: str,
) -> tuple[str | None, str | None, str | None]:
    """Extract stall / agent-failure / session-lost markers from a node output.

    Publishes ``run_stalled`` when the output carries a stall reason and
    returns the three marker reasons so the caller can latch them onto the
    run's terminal status.
    """
    stall_reason = _node_output_stall_reason(output)
    if stall_reason:
        broker.publish(
            "run_stalled",
            {
                "node_id": name,
                "stall_reason": _sanitize_detail(stall_reason, limit=5000),
            },
        )
    agent_failure = _node_output_agent_failure(output)
    session_lost = _node_output_sandbox_session_lost(output)
    return stall_reason, agent_failure, session_lost


def _terminal_failure(
    broker: RunEventBroker,
    status: str,
    code: str,
    detail: str,
    node_token_usage: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, dict[str, Any] | None]:
    """Publish ``run_failed`` and return the terminal failure 4-tuple."""
    broker.publish("run_failed", {"error": code, "detail": detail})
    return status, code, detail, node_token_usage or None


def _try_agent_failure_elevation(
    broker: RunEventBroker,
    run_id: uuid.UUID,
    agent_failure_reason: str,
    usage: dict[str, Any] | None,
) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
    """Attempt agent failure elevation (A1); return terminal tuple or None.

    Fail-open: any error logs a warning and returns None so the caller
    continues to the next terminalization check (stall).
    """
    try:
        from farna.settings import get_settings

        if get_settings().agent_failure_elevation_enabled:
            return _terminal_failure(
                broker,
                "failed",
                _ERROR_CODE_AGENT_FAILED,
                _sanitize_detail(agent_failure_reason, limit=5000),
                usage,
            )
    except Exception:
        _log.warning(
            "agent_failure_elevation.failed_open",
            extra={"run_id": str(run_id), "detail": agent_failure_reason},
            exc_info=True,
        )
    return None


def _stream_terminal_reason(
    state: _StreamState,
    broker: RunEventBroker,
    run_id: uuid.UUID,
) -> tuple[str, str | None, str | None, dict[str, Any] | None] | None:
    """Return the terminal 4-tuple for a captured stall / agent-failure /
    session-lost node, or ``None`` when the run completed normally.

    Extracted from ``_stream_graph``'s post-loop tail (pure refactor). A dead
    sandbox session routes to the retryable ``sandbox.no_output_json`` — unless
    the envelope's retained text classifies to a provider error code
    (``model.insufficient_funds`` for an HTTP 402 billing refusal) —
    a self-reported agent failure elevates to ``agent.failed``, and a node that
    stalled takes priority (existing precedence preserved). ``None`` means the
    caller returns ``complete``; ``run_completed`` is published later, only after
    finalize commits the terminal status, by
    ``_finalize_then_publish_completed``.
    """
    usage = state.node_token_usage or None
    if state.session_lost_reason and not state.stall_reason:
        # The marker reason is the wrapper's fixed PLACEHOLDER, so the
        # provider classification must run against the envelope's RETAINED text
        # (output.json's ``no_output_reason`` / the stdout tail), falling back to
        # the reason itself for callers that only carry that. A session killed
        # by an HTTP 402 funds refusal is a NON-retryable billing fault — it
        # must surface as ``model.insufficient_funds`` rather than be masked as
        # the retryable ``sandbox.no_output_json`` (whose every retry re-hits the
        # same wall). Fail open: no signature -> today's retryable code,
        # unchanged.
        _classified = classify_provider_error(state.session_lost_retained_text or state.session_lost_reason)
        return _terminal_failure(
            broker,
            "failed",
            _classified or _CODE_SANDBOX_NO_OUTPUT_JSON,
            _sanitize_detail(state.session_lost_reason, limit=5000),
            usage,
        )
    if state.connector_failure_reason and not state.stall_reason:
        code = state.connector_failure_code or _CODE_CONNECTOR_FAILED
        return _terminal_failure(
            broker,
            "failed",
            code,
            _sanitize_detail(state.connector_failure_reason, limit=5000),
            usage,
        )
    if state.agent_failure_reason and not state.stall_reason:
        agent_failure_result = _try_agent_failure_elevation(broker, run_id, state.agent_failure_reason, usage)
        if agent_failure_result is not None:
            return agent_failure_result
    if state.stall_reason:
        return _terminal_failure(
            broker,
            "stalled",
            "executor_stalled",
            _sanitize_detail(state.stall_reason, limit=5000),
            usage,
        )
    return None


def _sandbox_failure_class_code(exc: BaseException) -> str | None:
    """The failure's OWN canonical sandbox class code, or None (FAR-2562).

    ``sandbox_failure_error_code`` resolves the class code used by the terminal
    write, which the registry's per-class attempt cap is keyed on. It applies
    ONLY to a ``SandboxNodeFailedError``: a plain ``NodeCancelledError`` carries
    no sandbox class, so its behaviour is unchanged. The FAR-136 hang death
    ("likely hung") is also excluded — its terminal code stays the generic
    ``node_cancelled``, so capping it under the no-output class would silently
    change that code.
    """
    if not isinstance(exc, SandboxNodeFailedError):
        return None
    if "likely hung" in str(exc):
        return None
    return sandbox_failure_error_code(exc)
