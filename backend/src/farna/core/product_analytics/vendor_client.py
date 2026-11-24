"""Shared outbound HTTP helper for the product-analytics dump.

Provides retry/timeout/backoff/429 logic (extracted from the Notifier
pattern) and HMAC signing.  Every exception is caught inside the
caller — the client itself never re-raises.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from farna.core.ssrf import pinned_async_client

_log = logging.getLogger(__name__)

# Retry configuration — mirrors Notifier RETRY_DELAYS.
MAX_ATTEMPTS = 4  # 1 initial + 3 retries
RETRY_DELAYS = [1.0, 5.0, 30.0]

# Per-request deadline (design doc §8).
REQUEST_TIMEOUT = 30.0


def _canonical_payload_bytes(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes for the payload sub-object.

    The receiver verifies the HMAC over
    ``canonical_json(payload, sort_keys=True, separators=(",", ":"))`` — the
    payload **sub-object** only, never the whole request body.  This is the
    sender's byte-for-byte mirror of that canonical form.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign_outbound_batch(
    secret: str,
    payload: dict[str, Any],
    timestamp: float,
    sequence: int,
) -> str:
    """Compute the HMAC-SHA256 hex digest for an outbound vendor batch.

    This mirrors the receiver's ``hmac_verify.compute_hmac`` exactly::

        def compute_hmac(secret, payload_bytes, timestamp, sequence):
            message = payload_bytes + str(timestamp).encode() + str(sequence).encode()
            return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

    ``payload`` is the aggregate payload **sub-object** that is placed at
    ``body["payload"]`` — never the whole envelope.  The signed message is::

        canonical_json(payload, sort_keys=True, separators=(",", ":"))
        + str(timestamp).encode()
        + str(sequence).encode()

    There is **no separator** between the timestamp and the sequence: the bytes
    are the canonical JSON immediately followed by ``str(timestamp)`` and then
    ``str(sequence)``.  ``timestamp`` renders as a plain float (``str(1.5)`` ->
    ``"1.5"``), never ``%.6f``.  Do not insert a colon, pipe or any other
    delimiter — the live receiver would reject the signature.

    The signed ``timestamp`` must be the same freshly captured float sent in the
    ``X-Farna-Timestamp`` header and the ``timestamp`` envelope field, because
    the receiver rejects ``abs(now - timestamp) > 300``.

    It is **NOT** interchangeable with the rotation-request signer in
    :mod:`farna.core.product_analytics.hmac_verify`: that module signs with a
    ``|<timestamp:.6f>|<sequence>`` format under a separate cross-SDK contract.
    Never reuse this helper to sign or verify rotation requests, and never reuse
    the ``hmac_verify`` helper for outbound batches.
    """
    message = _canonical_payload_bytes(payload) + str(timestamp).encode() + str(sequence).encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_batch_envelope(
    payload: dict[str, Any],
    timestamp: float,
    sequence: int,
    hmac_hex: str,
) -> dict[str, Any]:
    """Wrap the aggregate payload in the receiver's required ``BatchRequest``.

    The envelope fields (``instance_id``, ``date`` and ``schema_version``) are
    sourced from the aggregate payload, which ``metrics_dump._build_payload``
    always populates.
    """
    instance_id = str(payload["instance_id"])
    return {
        "instance_id": instance_id,
        # The batch is deliberately instance-level: one row aggregates every
        # consenting org, so ``org_id`` carries the INSTANCE id. Do NOT switch
        # this to per-org rows — that changes the privacy posture and is a
        # product decision, not a bug fix.
        "org_id": instance_id,
        "date": str(payload["date"]),
        "schema_version": int(payload["schema_version"]),
        "sequence": int(sequence),
        "payload": payload,
        "timestamp": timestamp,
        "hmac": hmac_hex,
    }


class VendorClient:
    """HTTP client for posting metrics batches to the vendor endpoint.

    Handles retry with exponential backoff, 429 Retry-After, and
    per-request timeouts.  All exceptions are caught and surfaced as
    return values — never re-raised.
    """

    def __init__(self, endpoint_url: str, instance_secret: str) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._instance_secret = instance_secret
        self._http_client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = await pinned_async_client(
                self._endpoint_url,
                timeout=httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=30.0),
            )
        return self._http_client

    def _standard_delay(self, attempt: int) -> float:
        """Backoff delay for the given (1-based) attempt."""
        return RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]

    def _delay_for_429(self, resp: httpx.Response, attempt: int) -> float:
        """Retry-After delay for a 429, falling back to the standard backoff."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after is None:
            return self._standard_delay(attempt)
        try:
            return min(float(retry_after), 60.0)
        except (ValueError, TypeError):
            return self._standard_delay(attempt)

    def _describe_error(self, exc: Exception) -> str:
        """Human-readable message for a transport-level failure."""
        if isinstance(exc, TimeoutError):
            return f"Timeout after {REQUEST_TIMEOUT}s"
        if isinstance(exc, ValueError):
            return f"Blocked outbound URL: {exc}"
        return f"RequestError: {exc}"

    def _log_attempt_failure(self, attempt: int, last_error: str) -> None:
        _log.warning(
            "product_analytics.vendor_post_attempt_failed",
            extra={"attempt": attempt, "max_attempts": MAX_ATTEMPTS, "last_error": last_error},
        )

    async def _post_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        signature: str,
        body: bytes,
        timestamp: float,
        sequence: int,
    ) -> httpx.Response:
        """Execute a single POST attempt.

        Transport/timeout errors propagate to the caller, which owns the
        failure message and backoff.  On success the caller inspects the
        returned response.

        ``X-Farna-Secret`` is sent on EVERY attempt: the receiver requires it on
        first contact to bootstrap a previously-unknown instance (TOFU), and
        prefers it for verification thereafter.
        """
        return await asyncio.wait_for(
            client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Farna-Secret": self._instance_secret,
                    "X-Farna-Signature": signature,
                    "X-Farna-Timestamp": str(timestamp),
                    "X-Farna-Sequence": str(sequence),
                    "User-Agent": "Modulo-MetricsDump/1.0",
                },
            ),
            timeout=REQUEST_TIMEOUT,
        )

    async def _classify_response(
        self,
        resp: httpx.Response,
        attempt: int,
    ) -> tuple[tuple[bool, int, str | None] | None, str, bool]:
        """Classify a completed response for the retry loop.

        Returns ``(terminal, http_error, retried_after_429)``:

        - ``terminal`` — the final ``(success, status_code, error_message)``
          tuple for a success or terminal-400 response, else ``None``.
        - ``http_error`` — the ``HTTP <status>: ...`` message for a
          non-success response; ``""`` (unused) on terminal responses.
        - ``retried_after_429`` — True when the 429 Retry-After backoff sleep
          already ran and the loop must skip the standard backoff step.
        """
        response_code = resp.status_code
        if resp.is_success:
            terminal: tuple[bool, int, str | None] = (True, response_code, None)
            return terminal, "", False
        # Every non-2xx is logged at ERROR with the status and a truncated body.
        # A 422's validation detail is exactly what makes a contract mismatch
        # findable; the previous warning-only path hid it.
        response_body = resp.text
        _log.error(
            "product_analytics.vendor_post_rejected",
            extra={
                "status_code": response_code,
                "attempt": attempt,
                "response_body": str(response_body)[:500],
            },
        )
        http_error = f"HTTP {response_code}: {str(response_body)[:200]}"
        if response_code == 400:
            terminal = (False, response_code, f"HTTP 400 (terminal): {str(response_body)[:200]}")
            return terminal, "", False
        if response_code == 429:
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(self._delay_for_429(resp, attempt))
            return None, http_error, True
        return None, http_error, False

    async def post_batch(
        self,
        payload: dict[str, Any],
        timestamp: float,
        sequence: int,
    ) -> tuple[bool, int | None, str | None]:
        """POST a metrics batch to the vendor.

        ``payload`` is the aggregate payload dict built by
        ``metrics_dump._build_payload``; it is wrapped in the receiver's
        ``BatchRequest`` envelope and signed over the payload sub-object.

        Returns ``(success, status_code, error_message)``.
        """
        signature = sign_outbound_batch(self._instance_secret, payload, timestamp, sequence)
        envelope = build_batch_envelope(payload, timestamp, sequence, signature)
        body = json.dumps(envelope, default=str, separators=(",", ":")).encode()
        url = f"{self._endpoint_url}/api/v1/batch"

        try:
            client = await self._get_client()
        except ValueError as exc:
            return False, None, self._describe_error(exc)

        last_error: str | None = None
        response_code: int | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            retried_after_429 = False
            try:
                resp = await self._post_once(client, url, signature, body, timestamp, sequence)
            except (TimeoutError, httpx.RequestError) as exc:
                last_error = self._describe_error(exc)
                response_code = None
            except Exception as exc:
                last_error = f"Unexpected: {exc}"
                response_code = None
            else:
                response_code = resp.status_code
                terminal, http_error, retried_after_429 = await self._classify_response(resp, attempt)
                if terminal is not None:
                    return terminal
                last_error = http_error

            if retried_after_429:
                continue

            if attempt < MAX_ATTEMPTS:
                self._log_attempt_failure(attempt, last_error)
                await asyncio.sleep(self._standard_delay(attempt))

        return False, response_code, last_error

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None
