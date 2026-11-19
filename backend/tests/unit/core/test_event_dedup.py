"""Tests for farna.core.event_dedup — shared Redis event-id dedup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.core.event_dedup import claim_event_id, is_event_claimed
from farna.settings import Settings

_VALID_32 = "a" * 32

_PREFIX = "stripe:test:"
_TTL = 90 * 24 * 3600


def _make_settings(redis_url: str | None = "") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url=redis_url,
    )


@pytest.fixture
def fallback() -> set[str]:
    return set()


def _redis_client() -> MagicMock:
    client = MagicMock()
    client.exists = AsyncMock(return_value=0)
    client.set = AsyncMock(return_value=True)
    client.aclose = AsyncMock()
    return client


class TestLocalFallback:
    async def test_claim_is_idempotent_and_check_reads_it(self, fallback: set[str]) -> None:
        settings = _make_settings()

        assert await is_event_claimed(settings, "evt_1", key_prefix=_PREFIX, fallback=fallback, log_namespace="stripe.test") is False
        assert (
            await claim_event_id(
                settings,
                "evt_1",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )
            is True
        )
        assert await is_event_claimed(settings, "evt_1", key_prefix=_PREFIX, fallback=fallback, log_namespace="stripe.test") is True
        assert (
            await claim_event_id(
                settings,
                "evt_1",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )
            is False
        )


class TestRedis:
    async def test_check_uses_the_prefixed_key(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.exists.return_value = 1
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await is_event_claimed(
                _make_settings("redis://localhost:6379/0"),
                "evt_2",
                key_prefix=_PREFIX,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True
        client.exists.assert_awaited_once_with("stripe:test:evt_2")
        client.aclose.assert_awaited_once()

    async def test_claim_sets_nx_with_the_ttl(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.set.return_value = "OK"
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await claim_event_id(
                _make_settings("redis://localhost:6379/0"),
                "evt_3",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True
        client.set.assert_awaited_once_with("stripe:test:evt_3", "1", nx=True, ex=_TTL)
        client.aclose.assert_awaited_once()

    async def test_a_failed_claim_reports_not_newly_claimed(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.set.return_value = None
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await claim_event_id(
                _make_settings("redis://localhost:6379/0"),
                "evt_4",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is False
        assert "evt_4" not in fallback

    async def test_check_failure_falls_back_to_the_local_set(self, fallback: set[str]) -> None:
        fallback.add("evt_5")
        client = _redis_client()
        client.exists.side_effect = RuntimeError("redis down")
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await is_event_claimed(
                _make_settings("redis://localhost:6379/0"),
                "evt_5",
                key_prefix=_PREFIX,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True

    async def test_claim_failure_falls_back_to_the_local_set(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.set.side_effect = RuntimeError("redis down")
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await claim_event_id(
                _make_settings("redis://localhost:6379/0"),
                "evt_6",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True
        assert "evt_6" in fallback

    async def test_check_client_construction_failure_falls_back_to_the_local_set(self, fallback: set[str]) -> None:
        fallback.add("evt_10")
        with patch(
            "farna.core.event_dedup.Redis.from_url",
            new=MagicMock(side_effect=ValueError("Redis URL must specify one of the following schemes")),
        ):
            claimed = await is_event_claimed(
                _make_settings("not-a-redis-url"),
                "evt_10",
                key_prefix=_PREFIX,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True

    async def test_claim_client_construction_failure_falls_back_to_the_local_set(self, fallback: set[str]) -> None:
        with patch(
            "farna.core.event_dedup.Redis.from_url",
            new=MagicMock(side_effect=ValueError("Redis URL must specify one of the following schemes")),
        ):
            claimed = await claim_event_id(
                _make_settings("not-a-redis-url"),
                "evt_11",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is True
        assert "evt_11" in fallback

    async def test_a_close_failure_is_suppressed(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.exists.return_value = 0
        client.aclose.side_effect = RuntimeError("close failed")
        with patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)):
            claimed = await is_event_claimed(
                _make_settings("redis://localhost:6379/0"),
                "evt_7",
                key_prefix=_PREFIX,
                fallback=fallback,
                log_namespace="stripe.test",
            )

        assert claimed is False

    async def test_cancellation_is_not_swallowed(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.exists.side_effect = asyncio.CancelledError()
        with (
            patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)),
            pytest.raises(asyncio.CancelledError),
        ):
            await is_event_claimed(
                _make_settings("redis://localhost:6379/0"),
                "evt_8",
                key_prefix=_PREFIX,
                fallback=fallback,
                log_namespace="stripe.test",
            )

    async def test_a_cancelled_claim_is_not_swallowed(self, fallback: set[str]) -> None:
        client = _redis_client()
        client.set.side_effect = asyncio.CancelledError()
        with (
            patch("farna.core.event_dedup.Redis.from_url", new=MagicMock(return_value=client)),
            pytest.raises(asyncio.CancelledError),
        ):
            await claim_event_id(
                _make_settings("redis://localhost:6379/0"),
                "evt_9",
                key_prefix=_PREFIX,
                ttl_seconds=_TTL,
                fallback=fallback,
                log_namespace="stripe.test",
            )
