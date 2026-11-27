"""Unit coverage for the integration suite's connection-leak quarantine.

The quarantine lives in ``tests/integration/conftest.py`` and only executes under
Docker-backed integration tests, so its classification rules are pinned here on
plain objects. A change that WIDENS the quarantine (swallowing an unraisable it
does not own) or NARROWS it (letting the derived ``unclosed transport`` warning
of an already-quarantined connection leak fail the run again) must fail this
module.

The rules under test (``_leak_disposition``):

* an unclosed asyncpg connection is always quarantined and recorded;
* an ``unclosed transport`` warning is a DERIVED symptom of that leak, so it is
  quarantined too - but only outside strict mode and only for an item that also
  quarantined a connection leak;
* a transport warning with no connection leak behind it (a client nobody closes)
  and every other unraisable stay untouched and are raised as pytest would.
"""

from __future__ import annotations

import os
import types
from typing import Any
from unittest.mock import patch

import pytest

_ASYNCPG_CONNECTION_MESSAGE = "unclosed connection <asyncpg.connection.Connection object at 0x1>; please make sure to close it"
_TRANSPORT_MESSAGE = "unclosed transport <_SelectorSocketTransport fd=29>"


@pytest.fixture(scope="module")
def leak_quarantine() -> Any:
    """Import the integration conftest with its import-time effects undone.

    Importing it pins integration-only environment defaults (``setdefault``) and
    swaps the asyncio event-loop policy on Windows. Both are process-global, so
    neither may leak into a unit session: ``patch.dict(os.environ)`` restores the
    exact pre-import environment, and the policy swap is stubbed out outright so
    there is nothing to restore. The module object itself stays imported, which
    is all the classifier tests need.
    """
    with patch.dict(os.environ), patch("asyncio.set_event_loop_policy"):
        from tests.integration import conftest as integration_conftest

    return integration_conftest


def _unraisable(exc_value: BaseException) -> types.SimpleNamespace:
    """Build the ``sys.unraisablehook`` argument shape for *exc_value*."""
    return types.SimpleNamespace(
        exc_value=exc_value,
        exc_type=type(exc_value),
        exc_traceback=None,
        err_msg=None,
        object=None,
    )


def _connection_warning() -> types.SimpleNamespace:
    return _unraisable(ResourceWarning(_ASYNCPG_CONNECTION_MESSAGE))


def _transport_warning() -> types.SimpleNamespace:
    return _unraisable(ResourceWarning(_TRANSPORT_MESSAGE))


def test_asyncpg_connection_warning_is_quarantined(leak_quarantine: Any) -> None:
    disposition = leak_quarantine._leak_disposition(_connection_warning(), strict=False, has_quarantined_connection=False)

    assert disposition is leak_quarantine._LeakDisposition.QUARANTINE_CONNECTION


def test_derived_transport_warning_is_quarantined_in_non_strict_mode(leak_quarantine: Any) -> None:
    disposition = leak_quarantine._leak_disposition(_transport_warning(), strict=False, has_quarantined_connection=True)

    assert disposition is leak_quarantine._LeakDisposition.QUARANTINE_TRANSPORT


def test_derived_transport_warning_is_raised_in_strict_mode(leak_quarantine: Any) -> None:
    """Strict mode keeps the hard failure, so a leak cannot be normalised away."""
    disposition = leak_quarantine._leak_disposition(_transport_warning(), strict=True, has_quarantined_connection=True)

    assert disposition is leak_quarantine._LeakDisposition.RAISE


def test_transport_warning_without_a_quarantined_connection_is_raised(leak_quarantine: Any) -> None:
    """A transport leak with no connection leak behind it is not ours to swallow."""
    disposition = leak_quarantine._leak_disposition(_transport_warning(), strict=False, has_quarantined_connection=False)

    assert disposition is leak_quarantine._LeakDisposition.RAISE


@pytest.mark.parametrize(
    "unraisable",
    [
        pytest.param(_unraisable(RuntimeError("boom in __del__")), id="non-resource-warning"),
        pytest.param(_unraisable(ResourceWarning("unclosed file <_io.TextIOWrapper name='x'>")), id="other-resource-warning"),
        pytest.param(_unraisable(ResourceWarning("unclosed connection <sqlalchemy.pool.base._ConnectionFairy>")), id="non-asyncpg-connection"),
        pytest.param(_unraisable(RuntimeError(_ASYNCPG_CONNECTION_MESSAGE)), id="asyncpg-message-wrong-type"),
        pytest.param(_unraisable(ResourceWarning("unclosed transport")), id="transport-message-without-repr"),
    ],
)
def test_unrelated_unraisable_is_left_untouched(leak_quarantine: Any, unraisable: Any) -> None:
    disposition = leak_quarantine._leak_disposition(unraisable, strict=False, has_quarantined_connection=True)

    assert disposition is leak_quarantine._LeakDisposition.RAISE


def test_classifier_identifies_only_the_two_owned_shapes(leak_quarantine: Any) -> None:
    assert leak_quarantine._classify_unraisable_leak(_connection_warning()) is leak_quarantine._LeakShape.ASYNCPG_CONNECTION
    assert leak_quarantine._classify_unraisable_leak(_transport_warning()) is leak_quarantine._LeakShape.DERIVED_TRANSPORT
    assert leak_quarantine._classify_unraisable_leak(_unraisable(RuntimeError("boom"))) is None
    assert leak_quarantine._classify_unraisable_leak(_unraisable(ResourceWarning("unclosed file <x>"))) is None
