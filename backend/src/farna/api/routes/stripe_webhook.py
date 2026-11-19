"""Stripe purchase webhook — verify Stripe-Signature and fulfil purchases.

URL: POST /api/v1/webhooks/stripe

Auth: the ``Stripe-Signature`` header (``t=<timestamp>,v1=<signature>``). The
signature is an HMAC-SHA256 hex digest of ``"<timestamp>.<raw_body>"`` using the
webhook secret, with a ±300s replay window. Verification is against the RAW
request body bytes and FAILS CLOSED: any missing/invalid signature returns 400
and never reaches fulfilment.

Events handled (all idempotent, keyed on the Stripe event id):
  * ``invoice.paid`` — the authoritative "money received" signal. This is the
    ONLY event that triggers fulfilment. For a card-paid subscription checkout
    Stripe sends BOTH ``checkout.session.completed`` and ``invoice.paid`` for
    the same purchase, so fulfilling on both would issue two licences/emails.
    ``checkout.session.completed`` is therefore treated purely as an ack
    (returns 200, awaits the corresponding ``invoice.paid``).
  * ``checkout.session.completed`` — acknowledged but NEVER fulfilled; the
    first payment is signalled by the matching ``invoice.paid``.

The heavy work (license generation + email) runs as a FastAPI BackgroundTask so
the response returns 200 immediately; Stripe treats a 2xx as delivered.

Operator alerts
---------------
Fulfilment is deliberately narrow (one event mints a licence), but the OPERATOR
must see every commercially meaningful event — a card-testing probe once reached
3-D Secure and nobody noticed until it was found by accident. So a fixed set of
event types (:data:`_ALERT_EVENTS`) additionally emails ``ALERT_EMAIL_TO``
through the same SMTP path the readiness/watchdog alerts use. An alert is always
ADDITIVE: it never changes what an event fulfils, and it never changes the
response.

Three rules keep the alerts safe:

  * **A delivery failure is not a webhook failure.** The alert is a background
    task whose body can never raise, so a broken SMTP server leaves the 200 (and
    therefore Stripe's "delivered") untouched; the failure is logged with the
    event id and type.
  * **Test-mode events never alert.** A ``livemode`` event is real money; a
    test-mode one is sandbox noise and must never page an operator as though it
    were a sale (see :func:`_is_live_event`).
  * **Stripe retries, so alerts are deduped by event id** on the same Redis
    substrate ``stripe_fulfilment`` uses for idempotency, under its own key
    namespace — no new table (``core.event_dedup`` is the shared helper).

Chosen licence start (FAR-2146)
-------------------------------
A purchase may state when its licence should begin, as ``starts_at`` in the
checkout/subscription metadata (an ISO 8601 date such as ``2027-03-12``). The
value is validated HERE, before the background task is queued: a past date, one
more than 90 days ahead, or an unparseable value is rejected with 422 and a
machine-readable ``error`` code (``starts_at_in_past`` /
``starts_at_too_far_ahead`` / ``starts_at_malformed``), never silently clamped.
The job is left unclaimed, so the rejection is visible in Stripe's webhook log
rather than becoming a licence that begins on the wrong day.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel

from farna.api.constants import MSG_NOT_FOUND
from farna.core.alert_context import (
    alert_context_html,
    alert_context_text,
    alert_recipients,
    alerting_enabled_for_environment,
    stamp_stdout,
)
from farna.core.audit_coverage import audited_system, bind_audit_org
from farna.core.email_service import EmailSendingError, send_email
from farna.core.event_dedup import claim_event_id, is_event_claimed
from farna.core.license import DEFAULT_RENEWAL_MODE, RENEWAL_MODE_AUTO
from farna.core.stripe_fulfilment import (
    StartsAtRejectedError,
    fulfil_team_purchase,
    resolve_starts_at,
)
from farna.core.trigger_engine import TimestampExpiredError, verify_timestamp
from farna.db.models.organisation import SYSTEM_ORG_ID
from farna.settings import Settings, get_settings

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["stripe-webhook"])

#: Stripe event type names, defined once: each is read by the subject map, the
#: amount/string maps and the dispatch branches, and a typo in one copy would
#: silently stop that event alerting (sonar S1192).
_EVENT_INVOICE_PAID = "invoice.paid"
_EVENT_CHECKOUT_COMPLETED = "checkout.session.completed"
_EVENT_CHECKOUT_EXPIRED = "checkout.session.expired"
_EVENT_PAYMENT_REQUIRES_ACTION = "payment_intent.requires_action"
_EVENT_PAYMENT_FAILED = "payment_intent.payment_failed"
_EVENT_DISPUTE_CREATED = "charge.dispute.created"
_EVENT_CHARGE_REFUNDED = "charge.refunded"
_EVENT_EARLY_FRAUD_WARNING = "radar.early_fraud_warning.created"

# The ONLY event that triggers fulfilment. Stripe sends BOTH
# ``checkout.session.completed`` and ``invoice.paid`` for a single card-paid
# subscription checkout (distinct event ids), so fulfilling on both would
# double-issue licences. ``invoice.paid`` is the authoritative "money received"
# signal; ``checkout.session.completed`` is ack-only (see the route handler).
_FULFILMENT_EVENTS = frozenset({_EVENT_INVOICE_PAID})

#: Every event type that additionally emails the operator, mapped to the human
#: framing used as the subject line's tail. ``invoice.paid`` reads as a SALE,
#: never as an incident. Deliberately generous — volume is not the concern, a
#: silently missed card-testing probe or dispute is. NOT a fulfilment list: only
#: ``invoice.paid`` mints a licence, so every alert here is additive.
_ALERT_SUBJECT_KIND = {
    _EVENT_INVOICE_PAID: "New sale",
    _EVENT_CHECKOUT_COMPLETED: "Checkout completed",
    _EVENT_CHECKOUT_EXPIRED: "Checkout expired",
    _EVENT_PAYMENT_REQUIRES_ACTION: "Payment requires action",
    _EVENT_PAYMENT_FAILED: "Payment failed",
    _EVENT_DISPUTE_CREATED: "Dispute opened",
    _EVENT_CHARGE_REFUNDED: "Refund issued",
    _EVENT_EARLY_FRAUD_WARNING: "Early fraud warning",
}

#: The alertable types. Derived from the subject map so the two can never drift
#: (a type added to one is a type in both).
_ALERT_EVENTS = frozenset(_ALERT_SUBJECT_KIND)

#: Where each alertable object carries the money. Stripe names the same amount
#: differently per object (``amount_paid`` on an Invoice, ``amount_total`` on a
#: Checkout Session, ``amount`` elsewhere); anything unlisted uses ``amount``.
#: A ``radar.early_fraud_warning.created`` object carries no amount of its own.
_AMOUNT_FIELD_BY_TYPE = {
    _EVENT_INVOICE_PAID: "amount_paid",
    _EVENT_CHECKOUT_COMPLETED: "amount_total",
    _EVENT_CHECKOUT_EXPIRED: "amount_total",
}

#: Plain string fields worth quoting per type, in display order.
_STRING_DETAIL_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    _EVENT_CHECKOUT_COMPLETED: (("Payment status", "payment_status"), ("Checkout mode", "mode")),
    _EVENT_CHECKOUT_EXPIRED: (("Payment status", "payment_status"), ("Checkout mode", "mode")),
    _EVENT_PAYMENT_REQUIRES_ACTION: (("Status", "status"),),
    _EVENT_PAYMENT_FAILED: (("Status", "status"),),
    _EVENT_DISPUTE_CREATED: (("Dispute reason", "reason"), ("Dispute status", "status")),
    _EVENT_EARLY_FRAUD_WARNING: (("Fraud type", "fraud_type"),),
}

#: Metadata keys whose NAME looks credential-shaped are never rendered: the
#: alert body is an email, and one careless ``api_key`` metadata entry must not
#: ride along into an inbox.
_SENSITIVE_METADATA_MARKERS = ("key", "secret", "token", "password", "card", "cvc")

#: Most metadata pairs the alert body will carry, and the longest value kept.
#: Bounded so a pathological metadata blob cannot bloat the email.
_METADATA_PAIR_LIMIT = 10
_METADATA_VALUE_LIMIT = 200

#: Alert dedup window. Mirrors ``stripe_fulfilment``'s idempotency TTL — far
#: past Stripe's retry window, so a redelivered event never re-emails.
_ALERT_CLAIM_TTL_SECONDS = 90 * 24 * 3600

#: Redis key namespace for alert dedup, distinct from fulfilment's, plus the
#: log namespace the shared dedup helper reports failures under.
_ALERT_KEY_PREFIX = "stripe:alerted:"
_ALERT_DEDUP_LOG_NAMESPACE = "stripe.alert"

#: Process-local fallback when Redis is unavailable (single process only, the
#: same limitation ``stripe_fulfilment`` documents for its own fallback).
_alerted_event_ids: set[str] = set()

#: The inline error-path alert is bounded so a slow/retrying SMTP server cannot
#: hold the 422 open. The normal alert runs as a background task instead; this
#: bound is only for the refused-``starts_at`` path, which drops its queued
#: task and must still alert. A timed-out send is logged and abandoned —
#: Stripe's retry re-runs the whole path.
_INLINE_ALERT_TIMEOUT_SECONDS = 5.0

_DASHBOARD_BASE = "https://dashboard.stripe.com"


class StripeWebhookResponse(BaseModel):
    received: bool
    event_id: str


def verify_stripe_signature(
    secret: str,
    signature_header: str | None,
    payload: bytes,
) -> bool:
    """Verify a ``Stripe-Signature`` header against the raw request body.

    Header format: ``t=<unix_ts>,v1=<hex_hmac_sha256>`` where the HMAC input is
    ``"<unix_ts>.<raw_body>"`` keyed with *secret*. Returns True only when the
    signature matches (constant-time) AND the timestamp is within the ±300s
    replay window (replay protection). Fails closed on any malformed input.
    """
    if not secret or not signature_header:
        return False
    parts: dict[str, str] = {}
    for item in signature_header.split(","):
        key, _, value = item.partition("=")
        if key and value:
            parts[key] = value
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    # Reuse the shared ±300s replay-window check from core/trigger_engine rather
    # than re-implementing timestamp tolerance inline (see review feedback).
    try:
        verify_timestamp(timestamp)
    except TimestampExpiredError:
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    # Compare as UTF-8 bytes: ``hmac.compare_digest`` raises ``TypeError`` on str
    # operands containing non-ASCII characters. ``signature`` is
    # attacker-controlled and arrives latin-1-decoded from the header, so a high
    # byte must fail closed with False instead of escaping as an uncaught 500.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))


def _event_object(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    return obj if isinstance(obj, dict) else {}


def _renewal_mode_for(event: dict[str, Any]) -> str:
    """Return the ``renewal_mode`` the purchase's licence must carry (FAR-2136).

    Stripe's Invoice object already answers this in the event: ``subscription``
    holds the subscription id (or the expanded subscription object) for an
    invoice raised by a subscription, and is ``null`` for a one-off invoice.
    That single field is the whole signal — no Stripe API call and no
    seller-side record of what a customer bought.

    Staleness is accepted deliberately, not engineered around: a subscription
    cancelled AFTER this purchase leaves the minted key claiming ``"auto"``,
    and a self-hosted instance has no phone-home to learn otherwise. The
    consequence is mild and already covered — the customer does not renew, the
    licence lapses into the delivery grace window, and the grace dialog offers
    BOTH the renew path and the buy-a-new-licence path. Tracking cancellation
    would add seller-side state (and a callback the instance cannot receive)
    for a case that flow already handles.
    """
    return RENEWAL_MODE_AUTO if _event_object(event).get("subscription") else DEFAULT_RENEWAL_MODE


def _metadata_sources(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Every place Stripe can carry our metadata for an event's object.

    Which object holds it depends on how the purchase was created, so all three
    shapes are returned, in precedence order:

    1. the object's own ``metadata``;
    2. ``subscription_details.metadata`` (the subscription's metadata, copied
       onto the invoice);
    3. ``parent.subscription_details.metadata`` (the newer Invoice API shape).

    ONE definition, shared by the licence-start read (FAR-2146) and the operator
    alert body, so the two can never disagree about where ``starts_at`` lives.
    """
    candidates: list[Any] = [obj.get("metadata")]
    subscription_details = obj.get("subscription_details")
    if isinstance(subscription_details, dict):
        candidates.append(subscription_details.get("metadata"))
    parent = obj.get("parent")
    if isinstance(parent, dict):
        parent_subscription_details = parent.get("subscription_details")
        if isinstance(parent_subscription_details, dict):
            candidates.append(parent_subscription_details.get("metadata"))
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _extract_starts_at(event: dict[str, Any]) -> str | None:
    """Read the chosen licence start from the event's metadata (FAR-2146).

    Returns the first non-blank ``starts_at`` string from any
    :func:`_metadata_sources` location, or ``None`` when no object states one —
    which mints from now, exactly as before FAR-2146.
    """
    for metadata in _metadata_sources(_event_object(event)):
        raw = metadata.get("starts_at")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _extract_customer(event: dict[str, Any]) -> tuple[str | None, str]:
    """Extract ``(customer_email, org_name)`` from a checkout session or invoice event."""
    obj = _event_object(event)
    email: str | None = obj.get("customer_email") or None
    if not email:
        customer_details = obj.get("customer_details")
        if isinstance(customer_details, dict):
            email = customer_details.get("email") or None

    name: str | None = None
    customer_details = obj.get("customer_details")
    if isinstance(customer_details, dict):
        name = customer_details.get("name") or None
    if not name:
        name = obj.get("customer_name") or None

    # Prefer a stable org identifier. The Stripe customer id never changes for a
    # given buyer, whereas a display name or email can vary between purchases —
    # build_team_payload derives org_id from org_name, so an unstable name
    # would fragment a customer into multiple orgs across purchases.
    customer_id = obj.get("customer")
    if name:
        org_name = name
    elif isinstance(customer_id, str) and customer_id:
        org_name = f"Customer {customer_id}"
    elif email:
        org_name = f"Customer {email}"
    else:
        org_name = "Modulo Team License customer"
    return email, org_name


# ---------------------------------------------------------------------------
# Operator alerts
#
# One email per commercially meaningful Stripe event (see _ALERT_EVENTS). The
# rendering helpers below read ONLY fields Stripe already hands us — never a
# card number, CVC or expanded payment-method object — and everything is
# HTML-escaped because the values are third-party data rendered into an email.
# ---------------------------------------------------------------------------


def _is_live_event(event: dict[str, Any], settings: Settings) -> bool:
    """Is this event about REAL money?

    Stripe stamps ``livemode`` on every event and that flag is authoritative
    when present: ``True`` is a live charge, ``False`` is test mode. When the
    flag is missing or not a bool the ACCOUNT mode decides — the configured
    secret key's ``sk_live_`` prefix, which is how this instance identifies
    itself to Stripe.

    The fallback is deliberately asymmetric. An absent flag on a live-keyed
    instance alerts, because hiding a real charge is far worse than one extra
    email; an absent flag on a test-keyed instance stays quiet, because a
    sandbox must never page an operator as though money moved.
    """
    livemode = event.get("livemode")
    if isinstance(livemode, bool):
        return livemode
    return settings.stripe_secret_key.startswith("sk_live_")


def _alert_customer_email(obj: dict[str, Any]) -> str | None:
    """The buyer's email, from wherever this event's object carries it.

    Depends on the object: an Invoice has ``customer_email``, a Checkout
    Session ``customer_details.email``, a PaymentIntent ``receipt_email`` and a
    Charge ``billing_details.email``.
    """
    candidates: list[Any] = [obj.get("customer_email"), obj.get("receipt_email")]
    for holder_field in ("customer_details", "billing_details"):
        holder = obj.get(holder_field)
        if isinstance(holder, dict):
            candidates.append(holder.get("email"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


#: Stripe's currencies whose minor unit is NOT 1/100. Everything else uses the
#: default exponent of 2. Sourced from Stripe's currency documentation; a
#: currency added there later falls back to 2, which is the only safe default.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)
_THREE_DECIMAL_CURRENCIES = frozenset({"bhd", "iqd", "jod", "kwd", "lyd", "omr", "tnd"})


def _currency_exponent(code: str) -> int:
    """How many decimal places *code*'s minor unit has (JPY 0, KWD 3, else 2)."""
    if code in _ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in _THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def _format_money(amount: int, currency: str | None) -> str:
    """``4900``/``gbp`` -> ``"49.00 GBP (4,900 minor units)"``.

    Both numbers are shown on purpose: the major-unit figure is what an
    operator reads, and the minor-unit figure is exactly what Stripe sent. The
    major figure honours the currency's exponent — dividing everything by 100
    would report 4900 JPY as ``49.00 JPY`` and 4900 KWD as ``49.00 KWD`` — so a
    zero- or three-decimal currency is never misreported.
    """
    code = (currency or "").strip().upper() or "unknown currency"
    exponent = _currency_exponent(code.casefold())
    major = amount / (10**exponent)
    return f"{major:,.{exponent}f} {code} ({amount:,} minor units)"


def _int_field(obj: dict[str, Any], field: str) -> int | None:
    value = obj.get(field)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _primary_amount(event_type: str, obj: dict[str, Any]) -> tuple[int, str | None] | None:
    """The amount this event is about: (minor units, currency), or None."""
    field = _AMOUNT_FIELD_BY_TYPE.get(event_type, "amount")
    amount = _int_field(obj, field)
    if amount is None:
        return None
    currency = obj.get("currency")
    return amount, currency if isinstance(currency, str) else None


def _metadata_entries(metadata: dict[str, Any], seen: set[str]) -> Iterator[tuple[str, str]]:
    """Renderable ``(label, value)`` entries from one metadata dict.

    Marks every key it inspects in *seen*, so a key repeated on a later object
    is skipped (the first location wins, matching ``_extract_starts_at``'s
    precedence). Non-string/blank entries and credential-shaped KEYS are
    dropped: the alert body leaves the process as mail, so one careless
    ``api_key`` entry must not ride along.
    """
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            continue
        if key in seen:
            continue
        seen.add(key)
        if any(marker in key.lower() for marker in _SENSITIVE_METADATA_MARKERS):
            continue
        yield f"Metadata {key}", value[:_METADATA_VALUE_LIMIT]


def _metadata_pairs(obj: dict[str, Any]) -> list[tuple[str, str]]:
    """The metadata we set on the purchase (``starts_at`` and friends).

    Bounded (``_METADATA_PAIR_LIMIT`` keys) so a pathological metadata blob
    cannot bloat the email. See :func:`_metadata_entries` for the per-key rules.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for metadata in _metadata_sources(obj):
        for entry in _metadata_entries(metadata, seen):
            pairs.append(entry)
            if len(pairs) >= _METADATA_PAIR_LIMIT:
                return pairs
    return pairs


def _failure_summary(obj: dict[str, Any]) -> str | None:
    """The decline reason on a failed PaymentIntent — never card data.

    ``last_payment_error`` carries ``type`` / ``code`` / ``decline_code`` /
    ``message`` and, in some shapes, an expanded payment method. Only the four
    failure fields are read; nothing card-shaped is ever copied.
    """
    error = obj.get("last_payment_error")
    if not isinstance(error, dict):
        return None
    parts = [f"{field}={value}" for field in ("type", "code", "decline_code", "message") if isinstance(value := error.get(field), str) and value]
    return ", ".join(parts) or None


def _next_action_summary(obj: dict[str, Any]) -> str | None:
    """The action the payer must take (``use_stripe_sdk`` for 3-D Secure)."""
    action = obj.get("next_action")
    if not isinstance(action, dict):
        return None
    action_type = action.get("type")
    return action_type if isinstance(action_type, str) and action_type else None


def _related_id(obj: dict[str, Any], field: str) -> str | None:
    """A related object's id, whether Stripe sent an id or an expanded object."""
    value = obj.get(field)
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        nested = value.get("id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _dashboard_url(event_type: str, obj: dict[str, Any]) -> str | None:
    """The Stripe dashboard page for the event's object, when it has one.

    Alerts are only ever sent for LIVE events (:func:`_is_live_event`), so the
    live dashboard is the right destination and no ``/test`` prefix applies. A
    Checkout Session has no dashboard page of its own, so its PaymentIntent is
    linked instead; an Early Fraud Warning links the payment it concerns.
    """
    object_id = obj.get("id")
    if not isinstance(object_id, str) or not object_id:
        return None
    if event_type == _EVENT_INVOICE_PAID:
        return f"{_DASHBOARD_BASE}/invoices/{object_id}"
    if event_type.startswith("checkout.session."):
        payment_intent = _related_id(obj, "payment_intent")
        return f"{_DASHBOARD_BASE}/payments/{payment_intent}" if payment_intent else None
    if event_type == _EVENT_DISPUTE_CREATED:
        return f"{_DASHBOARD_BASE}/disputes/{object_id}"
    if event_type == _EVENT_EARLY_FRAUD_WARNING:
        related = _related_id(obj, "payment_intent") or _related_id(obj, "charge")
        return f"{_DASHBOARD_BASE}/payments/{related}" if related else None
    if event_type == _EVENT_CHARGE_REFUNDED:
        # A charge id resolves on the payments page, but the PaymentIntent is
        # the unambiguous link when the event carries one.
        related = _related_id(obj, "payment_intent") or object_id
        return f"{_DASHBOARD_BASE}/payments/{related}"
    return f"{_DASHBOARD_BASE}/payments/{object_id}"


def _amount_detail_lines(event_type: str, obj: dict[str, Any]) -> list[tuple[str, str]]:
    """The money an event is about: the primary amount plus, on a refund, the refunded amount."""
    details: list[tuple[str, str]] = []
    amount = _primary_amount(event_type, obj)
    if amount is not None:
        details.append(("Amount", _format_money(*amount)))
    if event_type == _EVENT_CHARGE_REFUNDED:
        refunded = _int_field(obj, "amount_refunded")
        if refunded is not None:
            currency = obj.get("currency")
            details.append(("Refunded", _format_money(refunded, currency if isinstance(currency, str) else None)))
    return details


def _identity_detail_lines(obj: dict[str, Any]) -> list[tuple[str, str]]:
    """Who paid and which objects the event names (buyer email plus every id)."""
    details: list[tuple[str, str]] = []
    email = _alert_customer_email(obj)
    if email:
        details.append(("Customer", email))
    object_id = obj.get("id")
    if isinstance(object_id, str) and object_id:
        details.append(("Object", object_id))
    for label, field in (("Payment intent", "payment_intent"), ("Charge", "charge"), ("Subscription", "subscription")):
        related = _related_id(obj, field)
        if related:
            details.append((label, related))
    return details


def _status_detail_lines(event_type: str, obj: dict[str, Any]) -> list[tuple[str, str]]:
    """The per-type status fields, decline reason and payer next action."""
    details: list[tuple[str, str]] = []
    for label, field in _STRING_DETAIL_FIELDS.get(event_type, ()):
        value = obj.get(field)
        if isinstance(value, str) and value:
            details.append((label, value))
    failure = _failure_summary(obj)
    if failure:
        details.append(("Failure detail", failure))
    next_action = _next_action_summary(obj)
    if next_action:
        details.append(("Next action", next_action))
    return details


def _alert_detail_lines(event: dict[str, Any]) -> list[tuple[str, str]]:
    """The commercially useful facts of *event*, as (label, value) pairs."""
    event_type = event["type"]
    obj = _event_object(event)
    details: list[tuple[str, str]] = [("Event", f"{event.get('id')} ({event_type})")]
    details.extend(_amount_detail_lines(event_type, obj))
    details.extend(_identity_detail_lines(obj))
    details.extend(_status_detail_lines(event_type, obj))
    details.extend(_metadata_pairs(obj))
    return details


def _render_event_alert(settings: Settings, event: dict[str, Any], note: str | None = None) -> tuple[str, str, str]:
    """Render one event's ``(subject, body_html, body_text)``.

    ``note`` carries something the event itself cannot say — today, that a paid
    invoice's licence was REFUSED — and both the subject and the body flag it,
    so an operator never reads a blocked purchase as a completed sale.
    """
    event_type = event["type"]
    kind = _ALERT_SUBJECT_KIND[event_type]
    details = _alert_detail_lines(event)
    if note:
        details.append(("Note", note))
    values = dict(details)

    subject_prefix = "[Modulo Billing] ACTION REQUIRED" if note else "[Modulo Billing]"
    subject_parts = [f"{subject_prefix} {kind}"]
    if "Amount" in values:
        # The readable figure only — the minor-unit form belongs in the body.
        subject_parts.append(values["Amount"].split(" (")[0])
    if "Customer" in values:
        subject_parts.append(values["Customer"])
    subject = " — ".join(subject_parts)

    link = _dashboard_url(event_type, _event_object(event))
    text_lines = [f"{label}: {value}" for label, value in details]
    if link:
        text_lines.append(f"Dashboard: {link}")
    body_text = "\n".join([f"Modulo billing: {kind}", "", *text_lines, "", alert_context_text(settings)])

    rows = "".join(f"<tr><th align='left'>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in details)
    if link:
        escaped_link = html.escape(link, quote=True)
        rows += f"<tr><th align='left'>Dashboard</th><td><a href='{escaped_link}'>{escaped_link}</a></td></tr>"
    body_html = f"<html><body><h2>Modulo billing: {html.escape(kind)}</h2><table cellpadding='4'>{rows}</table>{alert_context_html(settings)}</body></html>"
    return subject, body_html, body_text


async def _deliver_event_alert(settings: Settings, event: dict[str, Any], note: str | None = None) -> None:
    """Send one operator alert, or explain in the log why none was sent."""
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")

    if not alerting_enabled_for_environment(settings):
        _log.info(
            "stripe.alert.skipped_environment event_id=%s event=%s environment=%s",
            event_id,
            event_type,
            settings.environment,
        )
        return
    recipients = alert_recipients(settings)
    if not (settings.smtp_host and recipients):
        _log.info("stripe.alert.skipped_unconfigured event_id=%s event=%s", event_id, event_type)
        return
    if not _is_live_event(event, settings):
        _log.info("stripe.alert.skipped_test_mode event_id=%s event=%s livemode=%r", event_id, event_type, event.get("livemode"))
        return
    if await is_event_claimed(
        settings,
        event_id,
        key_prefix=_ALERT_KEY_PREFIX,
        fallback=_alerted_event_ids,
        log_namespace=_ALERT_DEDUP_LOG_NAMESPACE,
    ):
        _log.info("stripe.alert.duplicate event_id=%s event=%s", event_id, event_type)
        return

    subject, body_html, body_text = _render_event_alert(settings, event, note)
    try:
        delivered = await asyncio.to_thread(send_email, settings, recipients, subject, body_html, body_text)
    except EmailSendingError:
        _log.exception("stripe.alert.send_failed event_id=%s event=%s", event_id, event_type)
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("stripe.alert.send_failed_unexpected event_id=%s event=%s", event_id, event_type)
        return
    if not delivered:
        _log.error("stripe.alert.send_failed event_id=%s event=%s — email service declined the send", event_id, event_type)
        return

    await claim_event_id(
        settings,
        event_id,
        key_prefix=_ALERT_KEY_PREFIX,
        ttl_seconds=_ALERT_CLAIM_TTL_SECONDS,
        fallback=_alerted_event_ids,
        log_namespace=_ALERT_DEDUP_LOG_NAMESPACE,
    )
    # fly logs renders JSON logs unreliably, so an alert also gets a plain
    # stdout stamp — best effort, and it never carries ALERT_CONTEXT.
    stamp_stdout(
        f"[stripe-alert] {_ALERT_SUBJECT_KIND.get(event_type, event_type)} sent | event_id={event_id} type={event_type}",
        logger=_log,
        log_event="stripe.alert.stamp_print_failed",
    )


async def _send_event_alert(settings: Settings, event: dict[str, Any], note: str | None = None) -> None:
    """Email the operator about one Stripe event. NEVER raises.

    Runs as a BACKGROUND task so the 200 has already been sent: the webhook's
    contract is "delivered", and a slow or broken SMTP server must never turn a
    delivery into a Stripe-retried failure. Every failure is logged with the
    event id and type so the miss is visible.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    try:
        await _deliver_event_alert(settings, event, note)
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception("stripe.alert.failed event_id=%s event=%s", event_id, event_type)


async def _send_event_alert_bounded(settings: Settings, event: dict[str, Any], note: str | None = None) -> None:
    """Deliver the error-path alert without holding the response open.

    The refused-``starts_at`` path raises a 422, which drops the queued
    background task, so this alert has to run inline. It is BOUNDED rather than
    fully detached: the response waits at most ``_INLINE_ALERT_TIMEOUT_SECONDS``
    for the send, and a slow or retrying SMTP server is logged and abandoned
    (Stripe's retry re-runs the whole path and can alert again).
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    try:
        await asyncio.wait_for(_send_event_alert(settings, event, note), timeout=_INLINE_ALERT_TIMEOUT_SECONDS)
    except TimeoutError:
        _log.error(
            "stripe.alert.inline_timeout event_id=%s event=%s timeout=%ss",
            event_id,
            event_type,
            _INLINE_ALERT_TIMEOUT_SECONDS,
        )


async def _dispatch_fulfilment(background_tasks: BackgroundTasks, settings: Settings, event: dict[str, Any]) -> None:
    """Queue fulfilment for a paid ``invoice.paid`` event, or refuse a bad start."""
    event_type = event["type"]
    customer_email, org_name = _extract_customer(event)
    if not customer_email:
        _log.warning(
            "stripe.webhook.no_customer_email event=%s event_id=%s — no fulfilment attempted",
            event_type,
            event["id"],
        )
        return
    renewal_mode = _renewal_mode_for(event)
    starts_at = _extract_starts_at(event)
    # Validate the customer-chosen start AT THE BOUNDARY (FAR-2146), so a bad
    # date is reported synchronously to the caller instead of disappearing into
    # a background task. Nothing is minted and the event is not claimed: the
    # rejection is explicit rather than a silent clamp to "now" or to the
    # 90-day horizon.
    try:
        resolve_starts_at(starts_at)
    except StartsAtRejectedError as exc:
        _log.exception(
            "stripe.webhook.starts_at_rejected event=%s event_id=%s reason=%s value=%r detail=%s",
            event_type,
            event["id"],
            exc.reason,
            exc.value,
            exc.detail,
        )
        # The 422 response drops the queued background task, yet this is
        # exactly the case an operator must hear about: the invoice was PAID
        # and no licence was issued. Deliver the alert here instead, bounded so
        # a slow SMTP server cannot hold the 422 open — the alert never raises,
        # and the event-id dedupe means Stripe's retries cannot duplicate it.
        await _send_event_alert_bounded(
            settings,
            event,
            note=f"The licence was NOT issued: the invoice was paid but the chosen licence start was rejected ({exc.reason}). The customer must be fulfilled manually.",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"error": exc.reason, "detail": exc.detail},
        ) from exc
    _log.info(
        "stripe.webhook.fulfilling event=%s event_id=%s renewal_mode=%s starts_at=%s",
        event_type,
        event["id"],
        renewal_mode,
        starts_at,
    )
    background_tasks.add_task(
        fulfil_team_purchase,
        settings,
        event_id=event["id"],
        customer_email=customer_email,
        org_name=org_name,
        renewal_mode=renewal_mode,
        starts_at=starts_at,
    )


# FAR-1516: signature-verified ingress with no tenant at the request boundary
# (the org is resolved later, inside the fulfilment background task), so the
# receipt is recorded with a SYSTEM actor under the system sentinel org. The org
# is published only AFTER the signature verifies — an event must never claim
# signature_verified for a request that failed verification.
@router.post(
    "/stripe",
    dependencies=[
        Depends(
            audited_system(
                "stripe_webhook_received",
                "stripe_event",
                actor_source="signature_verified",
            )
        )
    ],
)
async def stripe_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
) -> StripeWebhookResponse:
    if not settings.stripe_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=MSG_NOT_FOUND)

    raw_body = await request.body()
    signature_header = request.headers.get("Stripe-Signature")
    if not signature_header:
        _log.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")
    if not verify_stripe_signature(settings.stripe_webhook_secret, signature_header, raw_body):
        _log.warning("stripe.webhook.invalid_signature")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid signature")
    bind_audit_org(request, SYSTEM_ORG_ID)

    try:
        event = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON payload",
        ) from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid event payload")

    event_type = event["type"]
    if event_type in _ALERT_EVENTS:
        # Queue the operator alert FIRST. Background tasks run in order and a
        # task that raises skips the rest, so the alert must never sit behind
        # the fulfilment work it is reporting on. Unlisted (unknown or
        # unsubscribed) types get no alert and keep the log-and-200 behaviour.
        background_tasks.add_task(_send_event_alert, settings, event)

    if event_type == _EVENT_CHECKOUT_COMPLETED:
        # Deliberate no-op: checkout completion is NOT a fulfilment event —
        # invoice.paid is the authoritative "money received" signal. Stripe
        # sends both for a card-paid subscription; fulfilling on both would
        # generate two licence keys for one purchase. Log for observability
        # and return without dispatching.
        _log.info("stripe.webhook.checkout_completed_noop event_id=%s", event["id"])
        return StripeWebhookResponse(received=True, event_id=event["id"])

    if event_type in _FULFILMENT_EVENTS:
        await _dispatch_fulfilment(background_tasks, settings, event)
    else:
        _log.info("stripe.webhook.unhandled_event event=%s event_id=%s", event_type, event["id"])

    return StripeWebhookResponse(received=True, event_id=event["id"])
