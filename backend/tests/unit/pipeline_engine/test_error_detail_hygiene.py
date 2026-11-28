"""FAR-2821: a failed run's stored error_detail is a reason, not a traceback.

``runs.error_detail`` is API-exposed (rendered on the Runs list and served by
the API), so it must never carry a raw Python traceback or internal source
paths. The full traceback is retained in the server log; the run row carries a
short, user-facing reason that preserves the error-code semantics.
"""

from __future__ import annotations

import logging
import uuid

from farna.core.pipeline_engine.error_codes import map_legacy_code
from farna.core.pipeline_engine.executor import PipelineExecutor

_EXECUTOR_LOGGER = "farna.core.pipeline_engine.executor"


def _raise_serialization_type_error() -> None:
    """Raise the kind of TypeError the state-serialization path surfaces.

    Raised from a helper (rather than inline) so the caught exception carries a
    real ``__traceback__`` — exactly the shape that used to be stored.
    """
    raise TypeError("Object of type set is not JSON serializable")


def test_state_serialization_failure_detail_is_readable_not_a_traceback(caplog):
    """The execute() failure write site stores a readable reason, not a traceback."""
    try:
        _raise_serialization_type_error()
    except TypeError as exc:
        caught = exc

    # The handler does not touch instance state, so bypass __init__ (no DB).
    executor = object.__new__(PipelineExecutor)
    with caplog.at_level(logging.ERROR, logger=_EXECUTOR_LOGGER):
        status, error_code, error_detail, _usage, _eligible = executor._execute_handle_stream_exceptions(
            caught,
            run_id=uuid.uuid4(),
            node_token_usage=None,
            retry_policy_eligible=True,
        )

    assert status == "failed"
    # Error-code semantics are unchanged: the write-site code is the exception
    # class name, and presentation still resolves the dotted taxonomy code.
    assert error_code == "TypeError"
    assert map_legacy_code(error_code) == "harness.state_serialization"

    assert error_detail is not None
    assert "Traceback (most recent call last)" not in error_detail
    assert "/app/src/" not in error_detail
    assert ".py" not in error_detail
    # Still a readable, human-facing reason.
    assert "TypeError" in error_detail
    assert "not JSON serializable" in error_detail

    # The full diagnostic is retained in the log, never on the run row.
    assert "Traceback (most recent call last)" in caplog.text
