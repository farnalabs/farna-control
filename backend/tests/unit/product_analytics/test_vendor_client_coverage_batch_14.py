"""Additional vendor-client error and retry coverage."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.core.model_backend_endpoints import INSECURE_HTTP_NOT_ALLOWED
from farna.core.product_analytics.vendor_client import MAX_ATTEMPTS, VendorClient


def _payload() -> dict:
    """Minimal aggregate payload carrying the envelope fields the client reads."""
    return {
        "schema_version": 3,
        "date": "2026-08-10",
        "instance_id": "instance-123",
        "errors": [],
    }


@pytest.mark.asyncio
async def test_insecure_http_endpoint_refused_before_any_request(caplog: pytest.LogCaptureFixture):
    """A plain-``http`` endpoint must never receive the instance secret.

    ``farna.core.ssrf`` deliberately allows ``http`` (a local model server is a
    legitimate target for other callers), so the client enforces its own floor:
    every request carries ``X-Farna-Secret``, which would be in cleartext over
    plain HTTP. The refusal must happen before the transport is touched.
    """
    client = VendorClient("http://vendor.example", "shh-secret")
    existing_http = AsyncMock(is_closed=False)
    client._http_client = existing_http

    with (
        patch(
            "farna.core.product_analytics.vendor_client.pinned_async_client",
            new_callable=AsyncMock,
        ) as build_client,
        caplog.at_level(logging.ERROR, logger="farna.core.product_analytics.vendor_client"),
    ):
        result = await client.post_batch(_payload(), 1.0, 2)

    assert result == (False, None, f"{INSECURE_HTTP_NOT_ALLOWED}: vendor.example")
    # Nothing was sent and no transport was even built.
    existing_http.post.assert_not_awaited()
    build_client.assert_not_awaited()
    records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert any(getattr(record, "reason", None) == INSECURE_HTTP_NOT_ALLOWED for record in records)


@pytest.mark.asyncio
async def test_malformed_endpoint_url_is_refused_without_raising():
    """A URL ``urlsplit`` cannot parse is refused, never propagated as an error.

    ``_endpoint_scheme_and_host`` catches ``ValueError`` (e.g. an invalid IPv6
    literal) and yields an empty scheme, so ``post_batch`` refuses the endpoint
    through its normal insecure-endpoint path rather than crashing the SAQ job.
    """
    client = VendorClient("https://[::1", "shh-secret")
    existing_http = AsyncMock(is_closed=False)
    client._http_client = existing_http

    result = await client.post_batch(_payload(), 1.0, 2)

    assert result == (False, None, f"{INSECURE_HTTP_NOT_ALLOWED}: https://[::1")
    existing_http.post.assert_not_awaited()


def test_success_terminal_treats_unreadable_body_as_accepted(caplog: pytest.LogCaptureFixture):
    """A response whose body cannot be read must not break the never-raises contract."""

    class _UnreadableBodyResponse:
        status_code = 200
        is_success = True

        @property
        def text(self) -> str:
            raise RuntimeError("decode failure")

    client = VendorClient("https://vendor.example", "secret")
    with caplog.at_level(logging.WARNING, logger="farna.core.product_analytics.vendor_client"):
        result = client._success_terminal(_UnreadableBodyResponse(), 200)

    assert result == (True, 200, None)
    assert any(record.message == "product_analytics.vendor_success_body_unparseable" for record in caplog.records)


@pytest.mark.asyncio
async def test_malformed_payload_returns_envelope_error_without_raising():
    """A payload missing an envelope key becomes ``EnvelopeError``, not a crash."""
    client = VendorClient("https://vendor.example", "secret")
    existing_http = AsyncMock(is_closed=False)
    client._http_client = existing_http

    result = await client.post_batch({"date": "2026-08-10"}, 1.0, 2)

    assert result[0] is False
    assert result[1] is None
    assert result[2].startswith("EnvelopeError: ")
    existing_http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_vendor_client_retries_429_and_honours_retry_after():
    client = VendorClient("https://vendor.example", "secret")
    responses = []
    for _ in range(MAX_ATTEMPTS - 1):
        response = MagicMock(status_code=429, is_success=False, text="busy", headers={"Retry-After": "999"})
        responses.append(response)
    responses.append(MagicMock(status_code=200, is_success=True, text="", headers={}))
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(side_effect=responses)
    with patch("farna.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await client.post_batch(_payload(), 1.0, 2)
    assert result == (True, 200, None)
    assert sleep.await_args_list[0].args == (60.0,)
    assert client._http_client.post.await_count == MAX_ATTEMPTS
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_invalid_retry_after_falls_back_and_timeout_is_described():
    client = VendorClient("https://vendor.example", "secret")
    response = MagicMock(status_code=429, is_success=False, text="busy", headers={"Retry-After": "not-a-number"})
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(side_effect=TimeoutError())
    with patch("farna.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_batch(_payload(), 1.0, 2)
    assert result[0] is False
    assert result[2] == "Timeout after 30.0s"
    assert client._delay_for_429(response, 1) == 1.0
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_unexpected_exception_and_closed_client_recreation(monkeypatch):
    client = VendorClient("https://vendor.example", "secret")
    closed = AsyncMock(is_closed=True)
    replacement = AsyncMock(is_closed=False)
    client._http_client = closed
    monkeypatch.setattr(
        "farna.core.product_analytics.vendor_client.pinned_async_client",
        AsyncMock(return_value=replacement),
    )
    replacement.post = AsyncMock(side_effect=RuntimeError("unexpected"))
    with patch("farna.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock):
        success, code, error = await client.post_batch(_payload(), 1.0, 2)
    assert success is False
    assert code is None
    assert error == "Unexpected: unexpected"
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_sends_x_farna_headers_and_no_x_modulo_headers():
    client = VendorClient("https://vendor.example", "secret")
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(return_value=MagicMock(status_code=200, is_success=True, text="", headers={}))
    result = await client.post_batch(_payload(), 1.0, 7)
    assert result == (True, 200, None)
    headers = client._http_client.post.await_args.kwargs["headers"]
    assert {"X-Farna-Secret", "X-Farna-Signature", "X-Farna-Timestamp", "X-Farna-Sequence"} <= set(headers)
    assert headers["X-Farna-Secret"] == "secret"
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Farna-Timestamp"] == "1.0"
    assert headers["X-Farna-Sequence"] == "7"
    assert headers["User-Agent"] == "Farna-Control-MetricsDump/1.0"
    legacy = [name for name in headers if name.lower().startswith("x-farna-control-")]
    assert len(legacy) == 0
    await client.close()


@pytest.mark.asyncio
async def test_vendor_client_signs_body_with_receiver_no_separator_layout():
    """The posted ``hmac`` matches the receiver's real byte layout.

    Written out independently as the receiver computes it
    (``farnalabs/farna-control-telemetry``, ``hmac_verify.compute_hmac``)::

        message = payload_bytes + str(timestamp).encode() + str(sequence).encode()

    The same freshly captured float must be signed, sent in the envelope and
    sent in ``X-Farna-Timestamp`` — the receiver rejects a timestamp older than
    300 seconds, so signing a different value would fail live.
    """
    secret = "instance-secret"
    timestamp = 1700000000.5
    sequence = 20260810
    client = VendorClient("https://vendor.example", secret)
    client._http_client = AsyncMock(is_closed=False)
    client._http_client.post = AsyncMock(return_value=MagicMock(status_code=200, is_success=True, text="", headers={}))

    result = await client.post_batch(_payload(), timestamp, sequence)

    assert result == (True, 200, None)
    call_kwargs = client._http_client.post.await_args.kwargs
    body = json.loads(call_kwargs["content"])
    payload_bytes = json.dumps(body["payload"], sort_keys=True, separators=(",", ":")).encode()
    receiver_message = payload_bytes + str(timestamp).encode() + str(sequence).encode()
    receiver_mac = hmac.new(secret.encode(), receiver_message, hashlib.sha256).hexdigest()

    assert body["hmac"] == receiver_mac
    assert call_kwargs["headers"]["X-Farna-Signature"] == receiver_mac
    assert body["timestamp"] == timestamp
    assert call_kwargs["headers"]["X-Farna-Timestamp"] == str(timestamp)
    await client.close()


def _assert_no_secret_in_records(records: list[logging.LogRecord], secret: str) -> None:
    """Assert no value carried on any captured record contains ``secret``.

    ``caplog.text`` only renders ``record.getMessage()``, so the vendor body
    (attached as an ``extra=`` attribute) never appears there. Inspecting every
    attribute of every record is what actually proves the log line is clean.
    """
    assert records
    for record in records:
        for value in record.__dict__.values():
            assert secret not in str(value)


@pytest.mark.asyncio
async def test_vendor_error_body_echoing_the_secret_is_redacted(caplog: pytest.LogCaptureFixture):
    """A rejection whose body quotes the secret must not leak it — logged or returned.

    The receiver can echo what it refused (``HTTP 500: secret rejected <secret>``).
    That text reaches the ERROR log *and* the failure tuple that is persisted,
    served by the transparency API and emailed to the operator, so the client
    redacts at the point the body is read (FAR-2740).
    """
    secret = "shh-secret"
    client = VendorClient("https://vendor.example", secret)
    client._http_client = AsyncMock(is_closed=False)
    response = MagicMock(
        status_code=500,
        is_success=False,
        text=f"HTTP 500: secret rejected {secret}",
        headers={},
    )
    client._http_client.post = AsyncMock(return_value=response)

    with (
        patch("farna.core.product_analytics.vendor_client.asyncio.sleep", new_callable=AsyncMock),
        caplog.at_level(logging.WARNING, logger="farna.core.product_analytics.vendor_client"),
    ):
        success, code, error = await client.post_batch(_payload(), 1.0, 2)

    assert success is False
    assert code == 500
    assert error is not None
    assert secret not in error
    assert "[redacted]" in error
    # The status and a recognisable reason survive the redaction.
    assert "500" in error
    assert "secret rejected" in error

    _assert_no_secret_in_records(caplog.records, secret)
    rejected = [record for record in caplog.records if record.message == "product_analytics.vendor_post_rejected"]
    assert rejected
    assert "[redacted]" in rejected[0].response_body
    await client.close()


def test_success_terminal_unrecognised_status_echoing_secret_is_redacted(caplog: pytest.LogCaptureFixture):
    """A 2xx body status echoing the secret is redacted from log AND message."""
    secret = "shh-secret"
    client = VendorClient("https://vendor.example", secret)
    response = MagicMock(
        status_code=200,
        is_success=True,
        text=json.dumps({"status": f"echo {secret}"}),
        headers={},
    )

    with caplog.at_level(logging.ERROR, logger="farna.core.product_analytics.vendor_client"):
        success, code, error = client._success_terminal(response, 200)

    assert success is False
    assert code == 200
    assert error is not None
    assert secret not in error
    assert "[redacted]" in error
    _assert_no_secret_in_records(caplog.records, secret)
