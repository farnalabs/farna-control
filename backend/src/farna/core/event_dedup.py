"""Shared Redis event-id dedup for Stripe webhook work.

Fulfilment (``core.stripe_fulfilment``) and operator alerts
(``api.routes.stripe_webhook``) need the same primitive: "has this Stripe event
id already been handled?". Redis is the durable shared store and a
process-local set is the single-instance fallback; the only difference between
the two callers is the key namespace and the TTL. The implementation therefore
lives here once and is parameterised, so the two uses cannot drift apart.

Fail-open by design: a Redis error reports "not seen" (and logs), preferring a
duplicate email/fulfilment retry over silently dropping the event. The check
and the claim are separate calls so a caller can send first and claim only on
success, leaving a failed send unmarked for Stripe's retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import MutableSet

from redis.asyncio import Redis

from farna.settings import Settings

_log = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 2.0
_SOCKET_TIMEOUT_SECONDS = 2.0


def _client(settings: Settings) -> Redis | None:
    """A Redis client for *settings*, or None when Redis is not configured."""
    if not settings.redis_url:
        return None
    return Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    )


async def is_event_claimed(
    settings: Settings,
    event_id: str,
    *,
    key_prefix: str,
    fallback: MutableSet[str],
    log_namespace: str,
) -> bool:
    """Read-only check for whether *event_id* has already been handled.

    Does NOT claim. A Redis failure reports "not seen" so a retried event may
    be handled again rather than dropped silently.
    """
    redis = _client(settings)
    if redis is not None:
        try:
            return bool(await redis.exists(f"{key_prefix}{event_id}"))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("%s.redis_check_failed event_id=%s", log_namespace, event_id, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()
    return event_id in fallback


async def claim_event_id(
    settings: Settings,
    event_id: str,
    *,
    key_prefix: str,
    ttl_seconds: int,
    fallback: MutableSet[str],
    log_namespace: str,
) -> bool:
    """Atomically claim *event_id*. True if newly claimed, False if already seen.

    Called only AFTER a successful send so a transient failure leaves the
    event unclaimed and Stripe's retry can re-attempt.
    """
    redis = _client(settings)
    if redis is not None:
        try:
            claimed = await redis.set(
                f"{key_prefix}{event_id}",
                "1",
                nx=True,
                ex=ttl_seconds,
            )
            return bool(claimed)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("%s.redis_claim_failed event_id=%s", log_namespace, event_id, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await redis.aclose()
    if event_id in fallback:
        return False
    fallback.add(event_id)
    return True
