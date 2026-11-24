"""Metrics dump -- product analytics SAQ cron job.

The SAQ cron ticks every 10 minutes; a per-instance jitter offset (aligned to
the 10-minute grid, spread across a 6-hour window) opens a 10-minute execution
window so each instance actually performs its (daily) dump on exactly one tick.
Builds an aggregate payload from all consenting orgs -- including the curated
frontend events aggregated out of ``metrics_staging`` (FAR-1717) -- and POSTs it
to the vendor endpoint.  Watermark advances only on full success.

Every tick also purges ``metrics_staging`` (FAR-1717): rows already covered by a
successful dump (watermark passed their date) and rows older than the retention
bound are deleted, so the staging table cannot grow unbounded even when a dump
gate skips (instance switch off, no consenting orgs, persistent vendor failure).
"""

from __future__ import annotations

import html
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from farna.core.cost_controller.system_config import (
    acquire_kv_lock,
    read_system_config,
    write_system_config,
)
from farna.core.product_analytics.consent import is_instance_analytics_enabled, is_org_consenting
from farna.core.product_analytics.constants import (
    DUMP_COUNT_KEY,
    DUMP_LAST_FAILURE_AT_KEY,
    DUMP_LAST_FAILURE_SUMMARY_KEY,
    DUMP_WATERMARK_KEY,
    coerce_dump_count,
)
from farna.core.product_analytics.vendor_client import VendorClient

_log = logging.getLogger(__name__)

__all__ = ["DUMP_COUNT_KEY", "DUMP_WATERMARK_KEY", "coerce_dump_count", "metrics_dump"]

# Schema version -- bumped when the payload shape changes. v3 renamed the
# error rollup from ``error_stats`` to the receiver's ``errors`` list.
SCHEMA_VERSION: int = 3

# Backfill cap (design doc section 8).
_BACKFILL_MAX_DAYS = 14

# ``metrics_staging`` retention bound (FAR-1717). Consumed rows are purged as
# soon as the watermark passes their date; this cap additionally bounds rows
# that were NEVER consumed (instance switch off, no consenting orgs, vendor
# endpoint failing) so the staging table cannot grow without bound.
_STAGING_RETENTION_DAYS = 30

# Jitter constants — each instance picks a random offset within a 6-hour
# window so that multiple instances don't all dump at the same minute.
_DUMP_WINDOW_MINUTES = 360  # 6 hours
_DUMP_EXECUTION_WINDOW_MINUTES = 10  # each instance gets 10 min to complete
_OFFSET_KEY = "product_analytics_dump_offset_minutes"

#: Cap on the persisted/spoken failure summary (FAR-2740). A vendor error body
#: can be long; the durable record and the operator email only need enough to
#: identify the failure. Never includes the instance secret — only the status
#: code and the error text the vendor client already returned.
_MAX_FAILURE_SUMMARY_LEN = 500

#: Type of the ``on_failure`` sink passed into ``_dump_date_range``.
FailureSink = Callable[[int | None, str | None], None]

#: Fixed failure reason for a dump that cannot even attempt a delivery because
#: the vendor endpoint URL or instance secret is unset (FAR-2740). Carries no
#: configuration values, so it is safe to persist and email verbatim.
_MISSING_VENDOR_CONFIG_REASON = "missing_vendor_config: vendor endpoint URL or instance secret is not configured"


def _parse_iso_date(value: str) -> date:
    """Parse an ISO date *or* datetime string to a ``date``.

    ``settings_json`` stores ``level_changed_at`` as a full ISO datetime
    (``consent.apply_consent_action`` / ``set_level`` write ``now.isoformat()``),
    whereas the dump watermark stores a date-only string.  ``date.fromisoformat``
    rejects any string carrying a time component, so a datetime string is parsed
    with ``datetime.fromisoformat`` and narrowed to its date.
    """
    try:
        return date.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value).date()


async def _increment_dump_count(session: AsyncSession) -> None:
    """Increment the successful-dump counter under its advisory lock.

    Called only on a successful (non-skipped) dump, in the SAME transaction
    that advances the watermark, so a crash between the two cannot count a dump
    that never advanced the watermark.
    """
    await acquire_kv_lock(session, DUMP_COUNT_KEY)
    current = coerce_dump_count(await read_system_config(session, DUMP_COUNT_KEY))
    await write_system_config(session, DUMP_COUNT_KEY, current + 1)


def _summarise_dump_failure(status_code: int | None, error: str | None, *, secret: str | None = None) -> str:
    """Build the durable/spoken failure summary — status code plus a truncated message.

    Deliberately narrow: only the HTTP status and the error text the vendor
    client already produced. The vendor's error text can echo the response body
    verbatim (``HTTP <code>: <body>``), so any occurrence of the instance secret
    is redacted BEFORE the summary is truncated or persisted. The summary is
    written to ``system_config``, logged, returned by the transparency API and
    emailed to the operator, so a leak here would surface in four places
    (FAR-2740).
    """
    code = "no_status" if status_code is None else str(status_code)
    detail = (error or "unknown error").strip() or "unknown error"
    if secret:
        detail = detail.replace(secret, "[redacted]")
    return f"status={code}: {detail}"[:_MAX_FAILURE_SUMMARY_LEN]


async def _record_dump_failure(
    factory: Any,
    status_code: int | None,
    error: str | None,
    *,
    secret: str | None = None,
) -> None:
    """Persist the most recent dump failure so the transparency page can show it.

    Writes the failure timestamp and a truncated summary under the keys declared
    in :mod:`constants` (FAR-2740). The previous record is overwritten, so the
    stored failure always describes the LATEST attempt; the next fully
    successful dump removes it (:func:`_clear_dump_failure`). ``secret`` is used
    only to redact the summary and is never persisted itself.
    """
    summary = _summarise_dump_failure(status_code, error, secret=secret)
    recorded_at = datetime.now(UTC).isoformat()
    async with factory() as session, session.begin():
        await write_system_config(session, DUMP_LAST_FAILURE_AT_KEY, recorded_at)
        await write_system_config(session, DUMP_LAST_FAILURE_SUMMARY_KEY, summary)
    _log.warning(
        "product_analytics.dump_failure_recorded",
        extra={"status_code": status_code, "summary": summary},
    )


async def _clear_dump_failure(factory: Any) -> None:
    """Remove the stored failure record after a fully successful dump.

    Deleting (rather than writing an empty value) keeps "present" meaning
    "a real, un-superseded failure" — the transparency route renders an absent
    key as ``None`` (FAR-2740).
    """
    from farna.db.crud.system_config import delete_config

    async with factory() as session, session.begin():
        await delete_config(session, DUMP_LAST_FAILURE_AT_KEY)
        await delete_config(session, DUMP_LAST_FAILURE_SUMMARY_KEY)


async def _raise_dump_failure_alert(
    settings: Any,
    status_code: int | None,
    error: str | None,
    *,
    secret: str | None = None,
) -> None:
    """Tell the operator about a failed dump through the shared alert channel.

    Reuses ``core.health_alerts.send_operator_alert`` — the SAME SMTP config,
    recipient parse, environment allowlist and deployment-context footer the
    readiness alert uses — rather than growing a second notification system
    (FAR-2740). Imported lazily so the dump's module graph does not pull
    ``redis``/``email_service`` in at import time (the same import discipline
    the rest of this module follows).

    The body states what is actually true in BOTH failure shapes: the jitter
    gate only opens once a day, so the retry is at the next daily window (not
    the next 10-minute tick), and a partial-success run advances the watermark
    and dump count for the days it did deliver BEFORE this alert is sent — so
    the last successful dump date is not necessarily unchanged.

    Best-effort: the helper never raises for a send/config problem, so a mail
    failure can never change the dump's watermark or retry behaviour. The
    durable record from :func:`_record_dump_failure` is the source of truth.
    ``secret`` is used only to redact the summary.
    """
    from farna.core.health_alerts import send_operator_alert

    summary = _summarise_dump_failure(status_code, error, secret=secret)
    subject = "[Farna Control] Product analytics dump failed"
    retry_note = "The instance will retry at its next daily dump window. Any days delivered before the failure are recorded and are not re-sent, so the last successful dump date and dump count may already include a partial delivery."
    body_text = f"Farna Control failed to deliver the product analytics batch to farnalabs.\n{summary}\n{retry_note}\nSee the product analytics transparency page for the recorded failure; the next fully successful dump clears it."
    # HTML BODY FRAGMENT: ``send_operator_alert`` wraps it in the alert
    # document and appends the shared deployment context + host footer, exactly
    # as the readiness channel does.
    body_html = f"<h2>Farna Control: product analytics dump failed</h2><p>{html.escape(summary)}</p><p>{html.escape(retry_note)}</p><p>See the product analytics transparency page for the recorded failure; the next fully successful dump clears it.</p>"
    await send_operator_alert(
        settings=settings,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
    )


async def _get_or_create_system_config(factory: Any, key: str, create: Any) -> str:
    """Get-or-create a string-valued ``system_config`` entry.

    Shared by the jitter offset and the instance id: read inside a transaction;
    if absent, generate via ``create`` (a zero-arg callable returning ``str``)
    and persist.  Returns the stored value as a string.
    """
    async with factory() as session, session.begin():
        value = await read_system_config(session, key)
        if value is None:
            value = create()
            await write_system_config(session, key, value)
    return str(value)


async def _should_dump_now(factory: Any, now: datetime | None = None) -> bool:
    """Check if it's this instance's turn to dump based on stored jitter offset.

    The SAQ cron ticks every ``_DUMP_EXECUTION_WINDOW_MINUTES`` (10 min), so the
    instance must pick an offset *aligned* to that grid within the
    ``_DUMP_WINDOW_MINUTES`` (6h) window.  Because the offset is a multiple of
    the tick interval, exactly one cron fire per day lands inside the
    ``[offset, offset + 10)`` execution window and performs the dump.  A legacy
    unaligned offset (drawn before this alignment) is realigned on first read so
    its window is centred on a real tick.

    ``now`` is injectable for testing; it defaults to ``datetime.now(UTC)``.
    """
    import secrets as _secrets

    if now is None:
        now = datetime.now(UTC)
    current_minute_of_day = now.hour * 60 + now.minute

    slots = _DUMP_WINDOW_MINUTES // _DUMP_EXECUTION_WINDOW_MINUTES

    def _create_offset() -> str:
        value = str(_secrets.randbelow(slots) * _DUMP_EXECUTION_WINDOW_MINUTES)
        _log.info("product_analytics.jitter_offset_set", extra={"offset_minutes": int(value)})
        return value

    offset = int(await _get_or_create_system_config(factory, _OFFSET_KEY, _create_offset))

    # Realign a legacy unaligned offset so the window sits on a real tick.
    if offset % _DUMP_EXECUTION_WINDOW_MINUTES != 0:
        offset = (offset // _DUMP_EXECUTION_WINDOW_MINUTES) * _DUMP_EXECUTION_WINDOW_MINUTES
        async with factory() as session, session.begin():
            await write_system_config(session, _OFFSET_KEY, str(offset))

    return offset <= current_minute_of_day < offset + _DUMP_EXECUTION_WINDOW_MINUTES


async def _purge_staging(factory: Any, *, now: datetime | None = None) -> int:
    """Purge consumed and expired ``metrics_staging`` rows (FAR-1717).

    Called on EVERY ``metrics_dump`` tick (the cron fires every 10 minutes)
    before the jitter gate, so the staging table is bounded even when a dump
    gate skips. One DELETE bounds both classes of row:

    * **Consumed** — a row dated ``D`` was included in the payload for ``D``
      once the dump for ``D`` succeeded (the watermark never rewinds, so dates
      ``<= watermark`` are never re-dumped). The cutoff is
      ``start_of_day(min(watermark + 1 day, today))``: when the watermark is
      behind today, every date through it is fully dumped and purgeable; when
      the watermark IS today, rows dated today are held back (cap at ``today``)
      because events staged AFTER today's dump moment have not been delivered
      and must not be deleted by their own dump day.
    * **Expired** — rows older than ``_STAGING_RETENTION_DAYS`` are dropped even
      if never consumed (instance switch off, no consenting orgs, vendor
      endpoint failing), which is what makes retention bounded rather than
      "until the next successful dump".

    ``now`` is injectable for testing; defaults to ``datetime.now(UTC)``.
    Returns the number of rows deleted (0 when nothing matched).
    """
    from farna.db.models.metrics_staging import MetricsStaging

    if now is None:
        now = datetime.now(UTC)

    cutoff = now - timedelta(days=_STAGING_RETENTION_DAYS)
    async with factory() as session, session.begin():
        watermark = await read_system_config(session, DUMP_WATERMARK_KEY)
        if watermark is not None:
            watermark_date = _parse_iso_date(str(watermark))
            consumed_through = min(watermark_date + timedelta(days=1), now.date())
            consumed_cutoff = datetime.combine(consumed_through, datetime.min.time(), tzinfo=UTC)
            cutoff = max(cutoff, consumed_cutoff)
        result = await session.execute(delete(MetricsStaging).where(MetricsStaging.recorded_at < cutoff))
        deleted = max(int(result.rowcount or 0), 0)

    if deleted:
        _log.info("product_analytics.staging_purged", extra={"rows": deleted, "cutoff": cutoff.isoformat()})
    return deleted


async def metrics_dump(_ctx: dict[str, Any]) -> dict[str, Any]:
    """SAQ system-cron job -- daily metrics dump.

    The SAQ cron ticks every 10 minutes (``*/10 * * * *``); the per-instance
    jitter gate (``_should_dump_now``) opens a 10-minute execution window so the
    dump runs on exactly one tick per day, spread across a 6-hour window.
    unique=True, system session factory.
    Skips when: no consenting orgs or instance switch off.

    Every tick FIRST purges ``metrics_staging`` (``_purge_staging``: consumed
    rows plus the retention bound), so staged events are bounded even on ticks
    where the jitter/consent/vendor gates below skip the actual dump.
    """
    from farna.core.saq_worker import _make_system_session_factory
    from farna.settings import get_settings

    settings = get_settings()
    # System session factory (farna_control_system role, LOGIN, BYPASSRLS) — REQUIRED.
    # _build_payload reads TEAM-SCOPED tables (pipelines, model_backends,
    # connector_instances, environment_profiles, library_primitives) across ALL
    # consenting orgs with NO set_rls_org context. Under the farna_control_app role
    # (NOBYPASSRLS) the strict rls_org_isolation policy filters those reads to
    # the (empty) app.organisation_id, silently returning ZERO rows. The system
    # factory bypasses RLS so the multi-org aggregation sees every row. Do NOT
    # swap to _make_session_factory.
    factory = _make_system_session_factory()

    # Staging purge runs on EVERY tick, before the jitter gate — a gate that
    # skips must not leave consumed/expired rows behind (FAR-1717).
    staging_purged = await _purge_staging(factory)

    # Jitter gate — each instance has a random offset within a 6-hour window.
    if not await _should_dump_now(factory):
        _log.debug("product_analytics.jitter_skip")
        return {"skipped": "jitter_skip", "staging_purged": staging_purged}

    # Instance-level gate (design doc section 5).
    if not await _check_instance_switch(factory):
        _log.info("product_analytics.instance_switch_off")
        return {"skipped": "instance_switch_off", "staging_purged": staging_purged}

    # Collect the consenting organisations. The loop is deliberately dumb: the
    # instance switch (above) is the only instance-level gate, and per-org
    # consent is the only per-org gate. Analytics never changes an org's tier.
    async with factory() as session, session.begin():
        orgs = await _get_consenting_orgs(session)
    if not orgs:
        _log.info("product_analytics.no_consenting_orgs")
        return {"skipped": "no_consenting_orgs", "staging_purged": staging_purged}

    # Determine the dump window from the watermark and consent dates.
    dump_date = datetime.now(UTC).date()
    start_date, last_dumped = await _resolve_start_date(factory, orgs, dump_date)
    if start_date > dump_date:
        _log.info("product_analytics.up_to_date", extra={"last_dumped": str(last_dumped)})
        return {"skipped": "up_to_date", "last_dumped": str(last_dumped), "staging_purged": staging_purged}

    # Build and send per-day payloads.
    endpoint_url = settings.product_analytics_endpoint_url
    instance_secret = settings.product_analytics_instance_secret
    if not endpoint_url or not instance_secret:
        # An instance that can NEVER deliver must be visible, not silently
        # skipped: record a fixed-reason failure (no configuration values) and
        # alert through the same channel as a delivery failure (FAR-2740).
        _log.warning("product_analytics.missing_vendor_config")
        await _record_dump_failure(factory, None, _MISSING_VENDOR_CONFIG_REASON)
        await _raise_dump_failure_alert(settings, None, _MISSING_VENDOR_CONFIG_REASON)
        return {"skipped": "missing_vendor_config", "staging_purged": staging_purged}

    failures: list[tuple[int | None, str | None]] = []
    succeeded_dates = await _dump_date_range(
        factory,
        orgs,
        start_date,
        dump_date,
        endpoint_url,
        instance_secret,
        on_failure=lambda status_code, error: failures.append((status_code, error)),
    )

    # Advance watermark (design doc section 8).
    if succeeded_dates:
        new_watermark = max(succeeded_dates)
        async with factory() as session, session.begin():
            await acquire_kv_lock(session, DUMP_WATERMARK_KEY)
            await write_system_config(session, DUMP_WATERMARK_KEY, new_watermark.isoformat())
            await _increment_dump_count(session)

    # Durable failure record + operator alert (FAR-2740). A failure record for
    # an attempt that also delivered earlier days is kept: the attempt did NOT
    # fully succeed, so the page must still show why it stopped. Only a fully
    # successful pass clears the record.
    if failures:
        status_code, error = failures[-1]
        await _record_dump_failure(factory, status_code, error, secret=instance_secret)
        await _raise_dump_failure_alert(settings, status_code, error, secret=instance_secret)
    elif succeeded_dates:
        await _clear_dump_failure(factory)

    return {
        "dumped_dates": [str(d) for d in succeeded_dates],
        "org_count": len(orgs),
        "staging_purged": staging_purged,
    }


async def _resolve_start_date(
    factory: Any,
    orgs: list[dict[str, Any]],
    dump_date: date,
) -> tuple[date, date | None]:
    """Resolve the first date that still needs dumping.

    Returns ``(start_date, last_dumped)`` where ``last_dumped`` is the previous
    watermark (or ``None`` on first run).  On a fresh instance the start is the
    earliest consent date, capped by the backfill window; otherwise it is the
    day after the last successfully dumped date.
    """
    async with factory() as session, session.begin():
        last_dumped = await read_system_config(session, DUMP_WATERMARK_KEY)

    if last_dumped is not None:
        if isinstance(last_dumped, str):
            last_dumped = _parse_iso_date(last_dumped)
        return last_dumped + timedelta(days=1), last_dumped

    earliest_consent = min(
        (o["level_changed_at"] for o in orgs if o["level_changed_at"] is not None),
        default=dump_date,
    )
    backfill_start = max(earliest_consent, dump_date - timedelta(days=_BACKFILL_MAX_DAYS))
    return backfill_start, None


async def _dump_date_range(
    factory: Any,
    orgs: list[dict[str, Any]],
    start_date: date,
    dump_date: date,
    endpoint_url: str,
    instance_secret: str,
    *,
    on_failure: FailureSink | None = None,
) -> list[date]:
    """Build and POST one payload per day in ``[start_date, dump_date]``.

    Stops early (leaving later dates undumped) on the first failed POST so the
    watermark is not advanced past un-sent data.  Returns the dates whose POST
    succeeded, in order.

    ``on_failure`` (FAR-2740) is an optional sink invoked with
    ``(status_code, error)`` on the failed POST. ``metrics_dump`` uses it to
    persist the failure and alert the operator without this function having to
    own durable state; direct callers (and its unit tests) may omit it.
    """
    client = VendorClient(endpoint_url, instance_secret)
    succeeded_dates: list[date] = []
    try:
        current_date = start_date
        while current_date <= dump_date:
            eligible_orgs = [o for o in orgs if o["level_changed_at"] is None or o["level_changed_at"] <= current_date]
            if not eligible_orgs:
                current_date += timedelta(days=1)
                continue

            payload = await _build_payload(factory, eligible_orgs, current_date)
            timestamp = time.time()
            sequence = int(current_date.strftime("%Y%m%d"))

            success, status_code, error = await client.post_batch(payload, timestamp, sequence)
            if success:
                succeeded_dates.append(current_date)
                _log.info(
                    "product_analytics.dump_success",
                    extra={"date": str(current_date), "org_count": len(eligible_orgs)},
                )
            else:
                _log.warning(
                    "product_analytics.dump_failed",
                    extra={"date": str(current_date), "status_code": status_code, "error": error},
                )
                if on_failure is not None:
                    on_failure(status_code, error)
                break

            current_date += timedelta(days=1)
    finally:
        # Teardown must never surface: a raising close() would replace the
        # body's exception (or discard the returned date list) — log and swallow.
        try:
            await client.close()
        except Exception:
            _log.exception("product_analytics.dump_client_close_failed")
    return succeeded_dates


async def _check_instance_switch(factory: Any) -> bool:
    """Return True when the instance-level analytics master switch is ON.

    Delegates to :func:`consent.is_instance_analytics_enabled` so the dump gate
    applies the SAME precedence as every other reader: an explicit operator
    value (stored ``system_config`` then the ``FARNA_CONTROL_PRODUCT_ANALYTICS_ENABLED``
    env var) wins, otherwise the installed licence's ``product_analytics_required``
    claim supplies the default, otherwise OFF. The coercion is fail-closed
    (``"false"`` / ``"0"`` / ``"no"`` are OFF), so a stored string can never run
    the dump while the consent surface reports the switch OFF. The transparency
    endpoint delegates to the same helper, so the reported switch and the dump
    gate cannot disagree.
    """
    async with factory() as session, session.begin():
        return await is_instance_analytics_enabled(session)


async def _get_consenting_orgs(session: AsyncSession) -> list[dict[str, Any]]:
    """Return the active organisations that have consented to the ``all`` level.

    Deliberately the ONLY per-org filter: read the level through the shared
    canonical predicate so the dump's consent set and the transparency
    endpoint's reported posture cannot drift (both call
    ``consent.is_org_consenting``). Nothing here inspects a licence or a plan
    tier.
    """
    from farna.db.models.organisation import Organisation

    result = await session.execute(select(Organisation.id, Organisation.settings_json).where(Organisation.status == "active"))
    orgs: list[dict[str, Any]] = []
    for row in result:
        if not is_org_consenting(row.settings_json):
            continue
        pa = (row.settings_json or {}).get("product_analytics", {})
        level_changed_at = pa.get("level_changed_at")
        if isinstance(level_changed_at, str):
            level_changed_at = _parse_iso_date(level_changed_at)
        orgs.append({"id": row.id, "level_changed_at": level_changed_at})
    return orgs


async def _build_payload(
    factory: Any,
    orgs: list[dict[str, Any]],
    target_date: date,
) -> dict[str, Any]:
    org_ids = [o["id"] for o in orgs]

    async with factory() as session, session.begin():
        entity_counts = await _count_entities(session, org_ids)
        run_stats = await _aggregate_run_stats(session, org_ids, target_date)
        error_stats = await _aggregate_error_stats(session, org_ids, target_date)
        staged_event_counts = await _aggregate_staged_events(session, org_ids, target_date)
        integration_inventory = await _build_integration_inventory(session, org_ids)

    return {
        "schema_version": SCHEMA_VERSION,
        "date": target_date.isoformat(),
        "timestamp": datetime.now(UTC).isoformat(),
        "instance_id": await _get_or_create_instance_id(factory),
        "org_count": len(orgs),
        "entity_counts": entity_counts,
        "run_stats": run_stats,
        "errors": _map_errors(error_stats),
        "event_counts": staged_event_counts,
        "integration_inventory": integration_inventory,
        "instance_metadata": _build_instance_metadata(factory),
    }


def _map_errors(error_stats: dict[str, Any]) -> list[dict[str, Any]]:
    """Map ``_aggregate_error_stats`` output to the receiver's ``errors`` shape.

    The receiver reads ``payload["errors"]`` as a list of
    ``{"family": <str>, "count": <int>}``. Our aggregation groups errors by
    ``ErrorGroup.level_peak``, which is the only family discriminator available,
    so each level becomes one entry. Without this mapping the error rollup is
    silently dropped: the receiver looks for ``errors`` and never reads
    ``error_stats``.
    """
    by_level = error_stats.get("by_level") or {}
    return [{"family": str(family), "count": int(count)} for family, count in by_level.items()]


async def _count_entities(session: AsyncSession, org_ids: list[uuid.UUID]) -> dict[str, int]:
    from farna.db.models.agent import Agent
    from farna.db.models.connector_instance import ConnectorInstance
    from farna.db.models.environment_profile import EnvironmentProfile
    from farna.db.models.eval import Eval
    from farna.db.models.model_backend import ModelBackend
    from farna.db.models.pipeline import Pipeline
    from farna.db.models.schema import Schema
    from farna.db.models.team import Team
    from farna.db.models.trigger import Trigger

    counts: dict[str, int] = {}

    result = await session.execute(select(func.count()).select_from(Pipeline).where(Pipeline.organisation_id.in_(org_ids), Pipeline.deleted_at.is_(None)))
    counts["pipelines"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Agent).where(Agent.organisation_id.in_(org_ids)))
    counts["agents"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Schema).where(Schema.organisation_id.in_(org_ids)))
    counts["schemas"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Team).where(Team.organisation_id.in_(org_ids)))
    counts["teams"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(ModelBackend).where(ModelBackend.organisation_id.in_(org_ids)))
    counts["model_backends"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(ConnectorInstance).where(ConnectorInstance.organisation_id.in_(org_ids)))
    counts["connector_instances"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(Trigger).where(Trigger.organisation_id.in_(org_ids)))
    counts["triggers"] = result.scalar_one() or 0

    # FAR-1729: count the live ``evals`` table — the frozen ``eval_definitions``
    # snapshot stops at the cutover, so counting it under-reports every
    # post-cutover definition. The ``eval_definitions`` KEY is retained as the
    # analytics contract name for this metric.
    result = await session.execute(select(func.count()).select_from(Eval).where(Eval.organisation_id.in_(org_ids), Eval.deleted_at.is_(None)))
    counts["eval_definitions"] = result.scalar_one() or 0

    result = await session.execute(select(func.count()).select_from(EnvironmentProfile).where(EnvironmentProfile.organisation_id.in_(org_ids)))
    counts["environment_profiles"] = result.scalar_one() or 0

    counts["orgs"] = len(org_ids)

    return counts


async def _aggregate_run_stats(
    session: AsyncSession,
    org_ids: list[uuid.UUID],
    target_date: date,
) -> dict[str, Any]:
    from farna.db.models.run import TERMINAL_STATUSES
    from farna.db.models.run_daily_facts import RunDailyFact

    # "complete" is the success signal; per the raw-status-complete guard
    # (agent-failure UX design §15.2) it must not be treated as a bare
    # status comparison. Derive it as the complement of the other terminal
    # statuses (single source of truth: farna.db.models.run.TERMINAL_STATUSES)
    # rather than matching the success literal directly.
    _non_complete_terminal = tuple(TERMINAL_STATUSES - {"complete"})

    result = await session.execute(
        select(
            func.count().label("total_runs"),
            func.sum(sa.case((RunDailyFact.status.in_(_non_complete_terminal), 0), else_=1)).label("complete"),
            func.sum(sa.case((RunDailyFact.status == "failed", 1), else_=0)).label("failed"),
            func.sum(sa.case((RunDailyFact.status == "cancelled", 1), else_=0)).label("cancelled"),
            func.sum(sa.case((RunDailyFact.status == "stalled", 1), else_=0)).label("stalled"),
            func.coalesce(func.sum(RunDailyFact.total_cost_usd), 0).label("total_cost_usd"),
            func.coalesce(func.sum(RunDailyFact.total_tokens), 0).label("total_tokens"),
        ).where(
            RunDailyFact.organisation_id.in_(org_ids),
            RunDailyFact.run_date == target_date,
        )
    )
    row = result.one()
    return {
        "total_runs": row.total_runs or 0,
        "complete": row.complete or 0,
        "failed": row.failed or 0,
        "cancelled": row.cancelled or 0,
        "stalled": row.stalled or 0,
        "total_cost_usd": str(row.total_cost_usd),
        "total_tokens": row.total_tokens or 0,
    }


async def _aggregate_error_stats(
    session: AsyncSession,
    org_ids: list[uuid.UUID],
    target_date: date,
) -> dict[str, Any]:
    from farna.db.models.error_group import ErrorGroup

    result = await session.execute(
        select(
            ErrorGroup.level_peak,
            func.count().label("count"),
        )
        .where(
            ErrorGroup.organisation_id.in_(org_ids),
            sa.cast(ErrorGroup.last_seen, sa.Date) == target_date,
        )
        .group_by(ErrorGroup.level_peak)
    )
    by_level: dict[str, int] = {}
    total = 0
    for row in result:
        count = int(row._mapping["count"] or 0)
        by_level[row.level_peak] = count
        total += count
    return {"total": total, "by_level": by_level}


async def _aggregate_staged_events(
    session: AsyncSession,
    org_ids: list[uuid.UUID],
    target_date: date,
) -> dict[str, int]:
    """Aggregate ``metrics_staging`` events for *target_date* by event type.

    This is the dump's staged-event consumer (FAR-1717): curated frontend
    events staged by ``POST /api/v1/metrics/events`` are folded into the daily
    payload's ``event_counts`` section. Scope matches the other daily
    aggregates: the consenting ``org_ids`` for the payload date, one UTC day
    (``[target_date 00:00, target_date + 1 00:00)``).
    """
    from farna.db.models.metrics_staging import MetricsStaging

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    result = await session.execute(
        select(MetricsStaging.event_type, func.count().label("count"))
        .where(
            MetricsStaging.organisation_id.in_(org_ids),
            MetricsStaging.recorded_at >= day_start,
            MetricsStaging.recorded_at < day_end,
        )
        .group_by(MetricsStaging.event_type)
    )
    return {str(row[0]): int(row[1]) for row in result}


async def _build_integration_inventory(session: AsyncSession, org_ids: list[uuid.UUID]) -> dict[str, Any]:
    from farna.db.models.connector_instance import ConnectorInstance
    from farna.db.models.model_backend import ModelBackend
    from farna.db.models.trigger import Trigger

    result = await session.execute(select(ModelBackend.provider, func.count().label("count")).where(ModelBackend.organisation_id.in_(org_ids)).group_by(ModelBackend.provider))
    model_providers: dict[str, int] = {row.provider: int(row._mapping["count"] or 0) for row in result}

    result = await session.execute(select(ConnectorInstance.connector_type_id, func.count().label("count")).where(ConnectorInstance.organisation_id.in_(org_ids)).group_by(ConnectorInstance.connector_type_id))
    connector_types: dict[str, int] = {str(row.connector_type_id): int(row._mapping["count"] or 0) for row in result}

    result = await session.execute(select(Trigger.trigger_type, func.count().label("count")).where(Trigger.organisation_id.in_(org_ids)).group_by(Trigger.trigger_type))
    trigger_types: dict[str, int] = {row.trigger_type: int(row._mapping["count"] or 0) for row in result}

    return {
        "model_providers": model_providers,
        "connector_types": connector_types,
        "trigger_types": trigger_types,
    }


async def _get_or_create_instance_id(factory: Any) -> str:
    key = "product_analytics_instance_id"

    def _create_id() -> str:
        return str(uuid.uuid4())

    return await _get_or_create_system_config(factory, key, _create_id)


def _build_instance_metadata(_factory: Any) -> dict[str, Any]:
    import os

    from farna.version import get_version

    return {
        "version": get_version(),
        "git_sha": os.environ.get("GIT_SHA", ""),
        "deployment_mode": os.environ.get("FARNA_CONTROL_DEPLOYMENT_MODE", "self-hosted"),
        "schema_version": SCHEMA_VERSION,
    }
