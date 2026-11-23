"""Unit tests for POST /api/v1/webhooks/stripe — signature verification + fulfilment dispatch.

Signature tests use the REAL Stripe signature scheme (HMAC-SHA256 over
``"<timestamp>.<raw_body>"`` with the webhook secret) signed in-test and passed
through the actual endpoint — no mock bypasses signature verification.
"""

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from farna.api.main import app
from farna.api.routes import stripe_webhook
from farna.core.license import (
    _LICENSE_PUBLIC_KEY_HEX,
    RENEWAL_MODE_AUTO,
    RENEWAL_MODE_MANUAL,
    parse_and_verify,
    set_public_key,
)
from farna.core.registry.crypto import generate_keypair
from farna.core.stripe_fulfilment import _processed_event_ids
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_TEST_SECRET = "whsec_test_123"

_FULFIL_MODULE = "farna.api.routes.stripe_webhook.fulfil_team_purchase"

_KP = generate_keypair()
_LICENSE_KEY_PATTERN = re.compile(r"FARNA_CONTROL_LICENSE_KEY=(\S+)")


@pytest.fixture(autouse=True)
def _stub_audit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """FAR-1516: stub the fresh ``audit_session`` the ``audited_system(...)``
    dependency writes on (a real engine — no database in the unit tier); the
    dependency itself still runs."""
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    @asynccontextmanager
    async def _factory() -> AsyncGenerator[AsyncMock, None]:
        yield session

    monkeypatch.setattr("farna.core.audit_coverage._shared_session_factory", lambda: _factory)


def _make_stripe_settings(
    secret_key: str = "sk_test_123",
    webhook_secret: str = _TEST_SECRET,
    *,
    license_private_key: str | None = None,
    redis_url: str | None = None,
) -> Settings:
    overrides: dict[str, str] = {}
    if license_private_key is not None:
        overrides["license_private_key"] = license_private_key
    if redis_url is not None:
        overrides["redis_url"] = redis_url
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        admin_password="testpass",
        stripe_secret_key=secret_key,
        stripe_webhook_secret=webhook_secret,
        **overrides,
    )


def _sign(body: bytes, *, secret: str = _TEST_SECRET, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.".encode() + body
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _checkout_event(event_id: str, *, payment_status: str = "paid", email: str = "bob@acme.com") -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{event_id}",
                "mode": "subscription",
                "payment_status": payment_status,
                "customer_email": email,
                "customer_details": {"email": email, "name": "Acme Inc"},
            }
        },
    }


def _invoice_event(event_id: str = "evt_inv_paid", *, email: str = "bob@acme.com", name: str = "Acme Inc") -> dict:
    return {
        "id": event_id,
        "type": "invoice.paid",
        "data": {"object": {"id": f"in_{event_id}", "customer_email": email, "customer_name": name}},
    }


def _invoice_event_with_subscription(event_id: str, subscription: str | None) -> dict:
    """An ``invoice.paid`` event carrying Stripe's ``Invoice.subscription``.

    That field is the id of the subscription the invoice was raised for, or
    ``null`` for a one-off invoice — the signal FAR-2136 reads.
    """
    event = _invoice_event(event_id)
    event["data"]["object"]["subscription"] = subscription
    return event


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = _make_stripe_settings
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def disabled_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = lambda: _make_stripe_settings(secret_key="", webhook_secret="")
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def fulfilment_client() -> Generator[TestClient, None, None]:
    """A client whose webhook runs the REAL fulfilment path (FAR-2136).

    Only ``get_settings`` is overridden: the signing key and the trust anchor
    are a matching test keypair, and the idempotency fallback is used
    (``redis_url=""``) so no Redis is needed. Outbound email is patched per
    test, so the key the test parses is one that was actually signed and
    emailed through ``fulfil_team_purchase``.
    """
    app.dependency_overrides[get_settings] = lambda: _make_stripe_settings(
        license_private_key=_KP["private_key"],
        redis_url="",
    )
    _processed_event_ids.clear()
    set_public_key(_KP["public_key"])
    try:
        yield TestClient(app)
    finally:
        _processed_event_ids.clear()
        set_public_key(_LICENSE_PUBLIC_KEY_HEX)
        app.dependency_overrides.clear()


def _minted_key(mock_send: MagicMock) -> str:
    """The licence key carried by the email the fulfilment sent."""
    body_text = mock_send.call_args.args[4]
    match = _LICENSE_KEY_PATTERN.search(body_text)
    assert match is not None, "the fulfilment email carried no FARNA_CONTROL_LICENSE_KEY"
    return match.group(1)


def _post(client: TestClient, body: bytes, signature: str | bytes | None) -> object:
    headers = {"Stripe-Signature": signature} if signature is not None else {}
    return client.post("/api/v1/webhooks/stripe", content=body, headers=headers)


class TestSignatureVerification:
    def test_valid_signature_accepted(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        assert resp.json()["event_id"] == "evt_inv_paid"
        mock_fulfil.assert_awaited_once()

    def test_invalid_signature_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, "t=1,v1=deadbeef")
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_tampered_body_rejected(self, client: TestClient) -> None:
        # Sign one payload, then send a different one — must fail closed.
        signed_body = json.dumps(_invoice_event(event_id="evt_a")).encode()
        sent_body = json.dumps(_invoice_event(event_id="evt_b")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, sent_body, _sign(signed_body))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_stale_timestamp_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        stale = int(time.time()) - 10_000
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body, timestamp=stale))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_missing_signature_header_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, None)
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_malformed_signature_header_rejected(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, "not-a-stripe-signature")
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_non_ascii_signature_header_rejected_without_raising(self, client: TestClient) -> None:
        """A latin-1-decoded non-ASCII signature must be a 400, not a 500.

        ``hmac.compare_digest`` raises TypeError on str operands containing
        non-ASCII characters. The header bytes are sent raw so Starlette
        decodes 0xE9 to 'é' exactly as it would from a real client.
        """
        body = json.dumps(_invoice_event()).encode()
        timestamp = str(int(time.time())).encode()
        signature = b"t=" + timestamp + b",v1=\xe9"
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, signature)
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()

    def test_non_json_body_rejected(self, client: TestClient) -> None:
        body = b"not json"
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 400
        mock_fulfil.assert_not_awaited()


class TestFulfilmentDispatch:
    def test_checkout_session_completed_is_noop(self, client: TestClient) -> None:
        """checkout.session.completed is accepted (200) but NEVER fulfils — even
        when payment_status=paid. invoice.paid is the single fulfilment event
        (FAR-180)."""
        body = json.dumps(_checkout_event("evt_checkout_paid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_checkout_session_noop_regardless_of_payment_status(self, client: TestClient) -> None:
        body = json.dumps(_checkout_event("evt_unpaid", payment_status="unpaid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_checkout_then_invoice_fulfils_exactly_once(self, client: TestClient) -> None:
        """A card-paid subscription sends checkout.session.completed AND
        invoice.paid for the same purchase. Only invoice.paid may fulfil — the
        checkout no-op must not add a second fulfilment (FAR-180)."""
        body_checkout = json.dumps(_checkout_event("evt_checkout_paid")).encode()
        body_invoice = json.dumps(_invoice_event("evt_inv_paid")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp_checkout = _post(client, body_checkout, _sign(body_checkout))
            resp_invoice = _post(client, body_invoice, _sign(body_invoice))
        assert resp_checkout.status_code == 200
        assert resp_invoice.status_code == 200
        mock_fulfil.assert_awaited_once()
        assert mock_fulfil.await_args.kwargs["event_id"] == "evt_inv_paid"
        assert mock_fulfil.await_args.kwargs["customer_email"] == "bob@acme.com"
        assert mock_fulfil.await_args.kwargs["org_name"] == "Acme Inc"

    def test_invoice_paid_dispatches_with_customer_email(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event("evt_inv_1")).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_awaited_once()
        assert mock_fulfil.await_args.kwargs["customer_email"] == "bob@acme.com"
        assert mock_fulfil.await_args.kwargs["org_name"] == "Acme Inc"

    def test_event_without_customer_email_does_not_dispatch(self, client: TestClient) -> None:
        event = _invoice_event("evt_no_email", email=None, name=None)
        body = json.dumps(event).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()

    def test_unrelated_event_returns_200_without_dispatch(self, client: TestClient) -> None:
        body = json.dumps({"id": "evt_other", "type": "customer.subscription.updated", "data": {"object": {}}}).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()


class TestRenewalModeFromEvent:
    """FAR-2136 — the minted key's ``renewal_mode`` comes from the event.

    These drive the REAL route and the REAL ``fulfil_team_purchase`` (only the
    outbound email seam is patched), so the mode is asserted on a key that was
    actually signed and emailed — not on a stubbed signer's arguments.
    """

    def _fulfil(self, client: TestClient, event: dict) -> str:
        body = json.dumps(event).encode()
        with patch(
            "farna.core.stripe_fulfilment.send_email",
            new=MagicMock(return_value=True),
        ) as mock_send:
            resp = _post(client, body, _sign(body))
        assert resp.status_code == 200
        mock_send.assert_called_once()
        return _minted_key(mock_send)

    def test_subscription_invoice_mints_auto_renewal(self, fulfilment_client: TestClient) -> None:
        license_key = self._fulfil(
            fulfilment_client,
            _invoice_event_with_subscription("evt_subscription", "sub_123"),
        )

        data = parse_and_verify(license_key).license_data
        assert data is not None
        assert data.renewal_mode == RENEWAL_MODE_AUTO

    def test_one_off_invoice_mints_manual_renewal(self, fulfilment_client: TestClient) -> None:
        license_key = self._fulfil(
            fulfilment_client,
            _invoice_event_with_subscription("evt_one_off", None),
        )

        data = parse_and_verify(license_key).license_data
        assert data is not None
        assert data.renewal_mode == RENEWAL_MODE_MANUAL


class TestWebhookDisabled:
    def test_returns_404_when_stripe_not_configured(self, disabled_client: TestClient) -> None:
        body = json.dumps(_invoice_event()).encode()
        with patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil:
            resp = _post(disabled_client, body, _sign(body))
        assert resp.status_code == 404
        mock_fulfil.assert_not_awaited()


# ---------------------------------------------------------------------------
# FAR-2146 — the customer-chosen licence start
# ---------------------------------------------------------------------------


def _invoice_event_with_starts_at(
    event_id: str,
    starts_at: str,
    *,
    location: str = "metadata",
) -> dict:
    """An ``invoice.paid`` event carrying ``starts_at`` where Stripe would put it.

    Stripe exposes metadata on different objects depending on how the purchase
    was created, so the route must read all of the supported locations.
    """
    event = _invoice_event(event_id)
    obj = event["data"]["object"]
    if location == "metadata":
        obj["metadata"] = {"starts_at": starts_at}
    elif location == "subscription_details":
        obj["subscription_details"] = {"metadata": {"starts_at": starts_at}}
    elif location == "parent":
        obj["parent"] = {"subscription_details": {"metadata": {"starts_at": starts_at}}}
    else:  # pragma: no cover - guards a typo in a parametrisation
        raise AssertionError(f"unknown metadata location {location!r}")
    return event


def _future_date(days_ahead: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days_ahead)).date().isoformat()


class TestChosenStartsAt:
    """The metadata date is validated at the boundary and reaches the minted key."""

    @pytest.mark.parametrize("location", ["metadata", "subscription_details", "parent"])
    def test_the_chosen_start_is_read_from_every_supported_metadata_location(self, client: TestClient, location: str) -> None:
        starts_at = _future_date(60)
        body = json.dumps(_invoice_event_with_starts_at("evt_start", starts_at, location=location)).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        mock_fulfil.assert_awaited_once()
        assert mock_fulfil.await_args.kwargs["starts_at"] == starts_at

    def test_absent_metadata_dispatches_with_no_start(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event("evt_no_start")).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        assert mock_fulfil.await_args.kwargs["starts_at"] is None

    def test_a_past_start_is_rejected_with_a_distinguishable_reason(self, client: TestClient) -> None:
        past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        body = json.dumps(_invoice_event_with_starts_at("evt_past", past)).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "starts_at_in_past"
        assert "already passed" in resp.json()["detail"]
        mock_fulfil.assert_not_awaited()

    def test_a_start_beyond_ninety_days_is_rejected_with_its_own_reason(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event_with_starts_at("evt_far", _future_date(120))).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "starts_at_too_far_ahead"
        assert "90 days ahead" in resp.json()["detail"]
        mock_fulfil.assert_not_awaited()

    def test_a_malformed_start_is_rejected_as_malformed(self, client: TestClient) -> None:
        body = json.dumps(_invoice_event_with_starts_at("evt_bad", "next spring")).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "starts_at_malformed"
        mock_fulfil.assert_not_awaited()

    def test_a_non_dict_parent_subscription_details_dispatches_with_no_start(self, client: TestClient) -> None:
        """The newer Invoice shape is optional; an unexpected one must not crash the read."""
        event = _invoice_event("evt_odd_parent")
        event["data"]["object"]["parent"] = {"subscription_details": "not-a-dict"}
        body = json.dumps(event).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        assert mock_fulfil.await_args.kwargs["starts_at"] is None

    @pytest.mark.parametrize("raw", ["   ", 123])
    def test_a_blank_or_non_string_starts_at_metadata_value_is_absent(self, client: TestClient, raw: object) -> None:
        """Only a non-blank string states a start; anything else mints from now."""
        event = _invoice_event("evt_odd_start")
        event["data"]["object"]["metadata"] = {"starts_at": raw}
        body = json.dumps(event).encode()

        with patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil:
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        assert mock_fulfil.await_args.kwargs["starts_at"] is None

    def test_real_fulfilment_mints_a_key_carrying_the_chosen_start(self, fulfilment_client: TestClient) -> None:
        starts_at = _future_date(60)
        body = json.dumps(_invoice_event_with_starts_at("evt_real_start", starts_at)).encode()

        with patch(
            "farna.core.stripe_fulfilment.send_email",
            new=MagicMock(return_value=True),
        ) as mock_send:
            resp = _post(fulfilment_client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        data = parse_and_verify(_minted_key(mock_send)).license_data
        assert data is not None
        assert data.starts_at == f"{starts_at}T00:00:00+00:00"
        assert datetime.fromisoformat(data.expires_at) > datetime.fromisoformat(data.starts_at)


# ---------------------------------------------------------------------------
# Operator alerts — one email per commercially meaningful event
# ---------------------------------------------------------------------------

_ALERT_SEND = "farna.api.routes.stripe_webhook.send_email"


def _alert_settings(**overrides: object) -> Settings:
    """Settings with the operator-alert channel live.

    ``smtp_host`` + ``alert_email_to`` are what make an alert actually send, and
    ``sk_live_`` is the account-mode signal the livemode fallback reads.
    """
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "admin_password": "testpass",
        "stripe_secret_key": "sk_live_123",
        "stripe_webhook_secret": _TEST_SECRET,
        "smtp_host": "smtp.example.com",
        "alert_email_to": "ops@example.com",
        "environment": "production",
        "redis_url": "",
    }
    base.update(overrides)
    return Settings(**base)


def _live(event: dict) -> dict:
    """Mark an event as a real (livemode) Stripe event."""
    event["livemode"] = True
    return event


def _payment_intent_event(event_id: str, event_type: str, **obj_fields: object) -> dict:
    obj = {
        "id": f"pi_{event_id}",
        "amount": 4900,
        "currency": "gbp",
        "receipt_email": "bob@acme.com",
        "status": "requires_action" if event_type.endswith("requires_action") else "requires_payment_method",
    }
    obj.update(obj_fields)
    return _live({"id": event_id, "type": event_type, "data": {"object": obj}})


def _dispute_event(event_id: str = "evt_dispute") -> dict:
    return _live(
        {
            "id": event_id,
            "type": "charge.dispute.created",
            "data": {
                "object": {
                    "id": "dp_123",
                    "amount": 4900,
                    "currency": "gbp",
                    "reason": "fraudulent",
                    "status": "needs_response",
                    "charge": "ch_123",
                    "payment_intent": "pi_123",
                }
            },
        }
    )


def _refund_event(event_id: str = "evt_refund") -> dict:
    return _live(
        {
            "id": event_id,
            "type": "charge.refunded",
            "data": {
                "object": {
                    "id": "ch_123",
                    "amount": 4900,
                    "amount_refunded": 4900,
                    "currency": "gbp",
                    "payment_intent": "pi_123",
                    "metadata": {"starts_at": "2027-03-12"},
                }
            },
        }
    )


def _fraud_warning_event(event_id: str = "evt_fraud") -> dict:
    return _live(
        {
            "id": event_id,
            "type": "radar.early_fraud_warning.created",
            "data": {
                "object": {
                    "id": "issfr_123",
                    "charge": "ch_123",
                    "payment_intent": "pi_123",
                    "fraud_type": "made_with_stolen_card",
                }
            },
        }
    )


def _sent(mock_send: MagicMock) -> tuple[list[str], str, str, str]:
    """``(recipients, subject, body_html, body_text)`` of the single send."""
    assert mock_send.call_count == 1
    args = mock_send.call_args.args
    return args[1], args[2], args[3], args[4]


class TestOperatorAlerts:
    @pytest.fixture(autouse=True)
    def _clear_alert_dedupe(self) -> Generator[None, None, None]:
        """The process-local dedup fallback is module state; never leak it."""
        stripe_webhook._alerted_event_ids.clear()
        yield
        stripe_webhook._alerted_event_ids.clear()

    @pytest.fixture
    def alert_client(self) -> Generator[TestClient, None, None]:
        # A plain lambda, never `_alert_settings` itself: FastAPI inspects the
        # override's signature, and its `**overrides` catch-all would become a
        # required `overrides` query parameter (422 on every request).
        app.dependency_overrides[get_settings] = lambda: _alert_settings()
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    @pytest.fixture
    def staging_client(self) -> Generator[TestClient, None, None]:
        app.dependency_overrides[get_settings] = lambda: _alert_settings(environment="staging", alert_environments="production")
        try:
            yield TestClient(app)
        finally:
            app.dependency_overrides.clear()

    def test_invoice_paid_emails_a_sale_and_still_fulfils(self, alert_client: TestClient) -> None:
        """A real sale alerts the operator AND keeps the existing fulfilment."""
        event = _live(_invoice_event("evt_sale"))
        event["data"]["object"].update({"amount_paid": 4900, "currency": "gbp"})
        body = json.dumps(event).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil,
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200, resp.text
        mock_fulfil.assert_awaited_once()
        recipients, subject, _html, text = _sent(mock_send)
        assert recipients == ["ops@example.com"]
        assert subject == "[Farna Control Billing] New sale — 49.00 GBP — bob@acme.com"
        assert "Event: evt_sale (invoice.paid)" in text
        assert "Amount: 49.00 GBP (4,900 minor units)" in text
        assert "Dashboard: https://dashboard.stripe.com/invoices/in_evt_sale" in text

    def test_checkout_completed_alerts_without_fulfilling(self, alert_client: TestClient) -> None:
        body = json.dumps(_live(_checkout_event("evt_checkout"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock()) as mock_fulfil,
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        mock_fulfil.assert_not_awaited()
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Checkout completed")
        assert "Payment status: paid" in text

    def test_checkout_expired_alerts(self, alert_client: TestClient) -> None:
        event = _live(_checkout_event("evt_expired", payment_status="unpaid"))
        event["type"] = "checkout.session.expired"
        body = json.dumps(event).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Checkout expired")
        assert "Payment status: unpaid" in text

    def test_payment_intent_requires_action_alerts_with_the_next_action(self, alert_client: TestClient) -> None:
        event = _payment_intent_event(
            "evt_action",
            "payment_intent.requires_action",
            next_action={"type": "use_stripe_sdk"},
        )
        body = json.dumps(event).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Payment requires action")
        assert "Next action: use_stripe_sdk" in text
        assert "Dashboard: https://dashboard.stripe.com/payments/pi_evt_action" in text

    def test_payment_failed_alert_reports_the_decline_and_no_card_data(self, alert_client: TestClient) -> None:
        event = _payment_intent_event(
            "evt_failed",
            "payment_intent.payment_failed",
            last_payment_error={
                "type": "card_error",
                "code": "card_declined",
                "decline_code": "insufficient_funds",
                "message": "Your card has insufficient funds.",
                # Stripe can expand the payment method onto the error; a card
                # number must NEVER be copied into the email.
                "payment_method": {"id": "pm_123", "card": {"number": "4242424242424242"}},
            },
        )
        body = json.dumps(event).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Payment failed")
        assert "Failure detail: type=card_error, code=card_declined, decline_code=insufficient_funds" in text
        assert "4242424242424242" not in text
        assert "4242424242424242" not in html

    def test_dispute_alert_carries_reason_status_and_amount(self, alert_client: TestClient) -> None:
        body = json.dumps(_dispute_event()).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Dispute opened")
        assert "Dispute reason: fraudulent" in text
        assert "Dispute status: needs_response" in text
        assert "Dashboard: https://dashboard.stripe.com/disputes/dp_123" in text

    def test_refund_alert_shows_both_amounts_and_the_purchase_metadata(self, alert_client: TestClient) -> None:
        body = json.dumps(_refund_event()).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Refund issued")
        assert "Amount: 49.00 GBP (4,900 minor units)" in text
        assert "Refunded: 49.00 GBP (4,900 minor units)" in text
        assert "Metadata starts_at: 2027-03-12" in text

    def test_zero_decimal_currency_amounts_are_not_divided_by_100(self, alert_client: TestClient) -> None:
        """A JPY amount is already major units: 4900 is ¥4,900, not ¥49.00."""
        event = _payment_intent_event("evt_jpy", "payment_intent.requires_action", amount=4900, currency="jpy")
        body = json.dumps(event).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert "4,900 JPY (4,900 minor units)" in text
        assert "4,900 JPY" in subject
        assert "49.00 JPY" not in text

    def test_three_decimal_currency_amounts_use_three_decimals(self, alert_client: TestClient) -> None:
        """A KWD amount has three decimal places: 4900 is 4.900 KWD."""
        event = _payment_intent_event("evt_kwd", "payment_intent.requires_action", amount=4900, currency="kwd")
        body = json.dumps(event).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, _subject, _html, text = _sent(mock_send)
        assert "4.900 KWD (4,900 minor units)" in text

    def test_fraud_warning_alert_links_the_payment_it_concerns(self, alert_client: TestClient) -> None:
        body = json.dumps(_fraud_warning_event()).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] Early fraud warning")
        assert "Fraud type: made_with_stolen_card" in text
        assert "Dashboard: https://dashboard.stripe.com/payments/pi_123" in text

    def test_credential_shaped_metadata_is_never_rendered(self, alert_client: TestClient) -> None:
        event = _live(_invoice_event("evt_meta"))
        starts_at = _future_date(60)
        event["data"]["object"]["metadata"] = {"starts_at": starts_at, "api_key": "sk_live_leaked"}
        body = json.dumps(event).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        _recipients, _subject, html, text = _sent(mock_send)
        assert f"Metadata starts_at: {starts_at}" in text
        assert "sk_live_leaked" not in text
        assert "sk_live_leaked" not in html

    def test_alert_reads_starts_at_from_every_metadata_location(self, alert_client: TestClient) -> None:
        """The alert and the licence-start read share one metadata precedence."""
        starts_at = _future_date(60)
        event = _live(_invoice_event("evt_sub_meta"))
        event["data"]["object"]["subscription_details"] = {"metadata": {"starts_at": starts_at}}
        body = json.dumps(event).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            assert _post(alert_client, body, _sign(body)).status_code == 200

        _recipients, _subject, _html, text = _sent(mock_send)
        assert f"Metadata starts_at: {starts_at}" in text

    def test_unknown_event_sends_no_alert(self, alert_client: TestClient) -> None:
        body = json.dumps(_live({"id": "evt_other", "type": "customer.subscription.updated", "data": {"object": {}}})).encode()

        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        mock_send.assert_not_called()

    def test_alert_failure_still_returns_200_and_still_fulfils(self, alert_client: TestClient) -> None:
        """A broken SMTP server must not turn delivery into a Stripe failure.

        The alert is queued BEFORE the fulfilment task, so this also proves a
        raising alert task cannot skip the fulfilment behind it.
        """
        from farna.core.email_service import EmailSendingError

        body = json.dumps(_live(_invoice_event("evt_smtp_down"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil,
            patch(_ALERT_SEND, new=MagicMock(side_effect=EmailSendingError("smtp down"))),
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        assert resp.json() == {"received": True, "event_id": "evt_smtp_down"}
        mock_fulfil.assert_awaited_once()

    def test_a_failed_send_leaves_the_event_unclaimed_so_a_retry_alerts(self, alert_client: TestClient) -> None:
        from farna.core.email_service import EmailSendingError

        body = json.dumps(_live(_invoice_event("evt_retry"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(side_effect=EmailSendingError("smtp down"))),
        ):
            assert _post(alert_client, body, _sign(body)).status_code == 200

        # Stripe's retry of the same event id must still alert.
        with patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send:
            assert _post(alert_client, body, _sign(body)).status_code == 200

        _sent(mock_send)

    def test_a_redelivered_event_does_not_alert_twice(self, alert_client: TestClient) -> None:
        body = json.dumps(_live(_invoice_event("evt_dupe"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            assert _post(alert_client, body, _sign(body)).status_code == 200
            assert _post(alert_client, body, _sign(body)).status_code == 200

        _sent(mock_send)

    def test_test_mode_event_never_alerts(self, alert_client: TestClient) -> None:
        event = _invoice_event("evt_test_mode")
        event["livemode"] = False
        body = json.dumps(event).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 200
        mock_send.assert_not_called()

    def test_livemode_falls_back_to_the_account_key_when_the_flag_is_absent(self) -> None:
        """No ``livemode`` field: the configured account key decides.

        A live-keyed deployment alerts (never hide a real charge); a test-keyed
        one stays quiet (never page an operator about sandbox noise).
        """
        event = _invoice_event("evt_no_flag")
        assert stripe_webhook._is_live_event(event, _alert_settings()) is True
        assert stripe_webhook._is_live_event(event, _alert_settings(stripe_secret_key="sk_test_123")) is False

    def test_no_alert_when_smtp_is_unconfigured(self, client: TestClient) -> None:
        """The default (compose/local) configuration stays silent."""
        body = json.dumps(_live(_invoice_event("evt_no_smtp"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(client, body, _sign(body))

        assert resp.status_code == 200
        mock_send.assert_not_called()

    def test_no_alert_outside_the_alert_environments_allowlist(self, staging_client: TestClient) -> None:
        body = json.dumps(_live(_invoice_event("evt_staging"))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(staging_client, body, _sign(body))

        assert resp.status_code == 200
        mock_send.assert_not_called()

    def test_a_refused_starts_at_still_alerts_the_operator(self, alert_client: TestClient) -> None:
        """A paid invoice whose licence was REFUSED is the loudest case.

        The 422 response drops a queued background task, so the alert is
        delivered on the error path; the subject marks it as needing action so
        it never reads as a completed sale.
        """
        past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        body = json.dumps(_live(_invoice_event_with_starts_at("evt_refused", past))).encode()

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")) as mock_fulfil,
            patch(_ALERT_SEND, new=MagicMock(return_value=True)) as mock_send,
        ):
            resp = _post(alert_client, body, _sign(body))

        assert resp.status_code == 422, resp.text
        mock_fulfil.assert_not_awaited()
        _recipients, subject, _html, text = _sent(mock_send)
        assert subject.startswith("[Farna Control Billing] ACTION REQUIRED New sale")
        assert "Note: The licence was NOT issued" in text

    def test_a_refused_starts_at_does_not_wait_for_a_hung_smtp_server(self, alert_client: TestClient) -> None:
        """The inline error-path alert is bounded, so the 422 always returns.

        A slow/retrying SMTP server must not hold the response open: the bound
        abandons the send and logs it, leaving the refusal visible in Stripe.
        """
        past = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
        body = json.dumps(_live(_invoice_event_with_starts_at("evt_refused_hang", past))).encode()

        async def _hang(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(10)

        with (
            patch(_FULFIL_MODULE, new=AsyncMock(return_value="key")),
            patch("farna.api.routes.stripe_webhook._send_event_alert", new=AsyncMock(side_effect=_hang)),
            patch("farna.api.routes.stripe_webhook._INLINE_ALERT_TIMEOUT_SECONDS", new=0.01),
        ):
            started = time.monotonic()
            resp = _post(alert_client, body, _sign(body))
            elapsed = time.monotonic() - started

        assert resp.status_code == 422, resp.text
        assert elapsed < 2


class TestAlertRenderingEdges:
    """Branches of the alert renderers that a well-formed event never reaches."""

    @pytest.fixture(autouse=True)
    def _clear_alert_dedupe(self) -> Generator[None, None, None]:
        stripe_webhook._alerted_event_ids.clear()
        yield
        stripe_webhook._alerted_event_ids.clear()

    def test_metadata_pairs_skip_non_strings_duplicates_and_secrets(self) -> None:
        """Only renderable metadata is mailed: strings only, first location wins."""
        obj = {
            "metadata": {"count": 3, "blank": "", "api_key": "sk_live_leaked", "starts_at": "2027-03-12"},
            "subscription_details": {"metadata": {"starts_at": "2026-01-01"}},
        }
        assert stripe_webhook._metadata_pairs(obj) == [("Metadata starts_at", "2027-03-12")]

    def test_metadata_pairs_are_capped_at_ten_entries(self) -> None:
        obj = {"metadata": {f"field_{index}": f"value_{index}" for index in range(12)}}
        pairs = stripe_webhook._metadata_pairs(obj)
        assert len(pairs) == 10
        assert pairs[0] == ("Metadata field_0", "value_0")

    def test_related_id_reads_an_expanded_object_and_ignores_an_idless_one(self) -> None:
        assert stripe_webhook._related_id({"payment_intent": {"id": "pi_expanded"}}, "payment_intent") == "pi_expanded"
        assert stripe_webhook._related_id({"payment_intent": {"id": 7}}, "payment_intent") is None
        assert stripe_webhook._related_id({"payment_intent": ""}, "payment_intent") is None

    def test_dashboard_url_is_absent_without_an_object_id(self) -> None:
        assert stripe_webhook._dashboard_url("invoice.paid", {}) is None

    def test_alert_details_omit_absent_optional_facts(self) -> None:
        refund = {
            "id": "evt_refund_plain",
            "type": "charge.refunded",
            "data": {"object": {"amount": 4900, "currency": "gbp"}},
        }
        refund_labels = [label for label, _ in stripe_webhook._alert_detail_lines(refund)]
        assert "Object" not in refund_labels
        assert "Refunded" not in refund_labels

        failed = {
            "id": "evt_failed_plain",
            "type": "payment_intent.payment_failed",
            "data": {"object": {"id": "pi_plain", "amount": 100}},
        }
        failed_labels = [label for label, _ in stripe_webhook._alert_detail_lines(failed)]
        assert "Status" not in failed_labels
        assert "Failure detail" not in failed_labels

    async def test_a_declined_send_is_logged_and_leaves_the_event_unclaimed(self) -> None:
        """A 2xx-but-not-delivered email service is not a claimable success."""
        event = _live(_invoice_event("evt_declined"))

        with patch(_ALERT_SEND, new=MagicMock(return_value=False)) as mock_send:
            await stripe_webhook._send_event_alert(_alert_settings(), event)

        mock_send.assert_called_once()
        assert "evt_declined" not in stripe_webhook._alerted_event_ids

    async def test_send_event_alert_reraises_cancellation_and_swallows_unexpected_errors(self) -> None:
        """Cancellation must propagate; any other failure is logged and absorbed."""
        event = _live(_invoice_event("evt_cancel"))
        settings = _alert_settings()

        with patch(_ALERT_SEND, new=MagicMock(side_effect=asyncio.CancelledError)), pytest.raises(asyncio.CancelledError):
            await stripe_webhook._send_event_alert(settings, event)

        with patch(_ALERT_SEND, new=MagicMock(side_effect=RuntimeError("smtp exploded"))):
            await stripe_webhook._send_event_alert(settings, event)

        with patch.object(stripe_webhook, "_deliver_event_alert", new=AsyncMock(side_effect=RuntimeError("boom"))):
            await stripe_webhook._send_event_alert(settings, event)
