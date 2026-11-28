"""ADR 070 §3 — ``background_session`` is the ONE background session factory.

What these tests pin, and why each matters:

* the grant is applied INSIDE the transaction (org + ``app.execution_context``),
  which is what makes a background read survive the Phase 1B team predicate;
* the transaction boundary is the caller's: COMMIT on a clean exit, ROLLBACK
  when the body raises (the exception still reaches the caller's handler);
* the session comes from the caller's factory/engine when one is supplied, so a
  converted call site keeps its exact pool (and its test doubles) — the factory
  is not a second engine;
* an org-less call is REFUSED: the grant may never widen past
  ``organisation_id`` (ADR 070 §3).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import farna.db.background as background
from farna.db.background import apply_background_grant, background_session

_ORG = uuid.uuid4()


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    """A private in-memory DB with one bare table (no ORM/RLS in the way)."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE probe (id INTEGER)"))
    yield engine
    await engine.dispose()


async def _count_probe_rows(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int((await connection.execute(text("SELECT count(*) FROM probe"))).scalar_one())


async def test_background_session_applies_org_and_execution_context(sqlite_engine: AsyncEngine) -> None:
    """The grant lands on the session BEFORE the caller's first statement."""
    async with background_session(_ORG, engine=sqlite_engine) as session:
        assert session.info["org_id"] == _ORG
        assert session.info["execution_context"] is True
        await session.execute(text("INSERT INTO probe (id) VALUES (1)"))

    # A clean exit COMMITS — the caller's body is one committed transaction.
    assert await _count_probe_rows(sqlite_engine) == 1


async def _insert_then_fail(engine: AsyncEngine) -> None:
    """One transaction that writes and then raises, to exercise the rollback."""
    async with background_session(_ORG, engine=engine) as session:
        await session.execute(text("INSERT INTO probe (id) VALUES (1)"))
        raise RuntimeError("boom")


async def test_background_session_rolls_back_when_the_body_raises(sqlite_engine: AsyncEngine) -> None:
    """The body's failure rolls the transaction back AND propagates."""
    with pytest.raises(RuntimeError, match="boom"):
        await _insert_then_fail(sqlite_engine)

    assert await _count_probe_rows(sqlite_engine) == 0


async def test_background_session_rejects_an_org_less_call(sqlite_engine: AsyncEngine) -> None:
    """No organisation_id means no background session — never a cross-org one."""
    with pytest.raises(ValueError, match="organisation_id"):
        async with background_session(None, engine=sqlite_engine):
            pass  # pragma: no cover - unreachable


async def test_background_session_org_only_when_execution_context_is_disabled(sqlite_engine: AsyncEngine) -> None:
    """``execution_context=False`` keeps the session team-filtered on purpose."""
    async with background_session(_ORG, engine=sqlite_engine, execution_context=False) as session:
        assert session.info["org_id"] == _ORG
        assert session.info.get("execution_context") is None


async def test_background_session_coerces_a_string_org_id(sqlite_engine: AsyncEngine) -> None:
    """Call sites pass JSON-sourced ids; the session must hold a real UUID."""
    async with background_session(str(_ORG), engine=sqlite_engine) as session:
        assert session.info["org_id"] == _ORG


async def test_background_session_prefers_the_callers_session_factory(sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller-supplied factory is used verbatim — no engine is rebuilt."""
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)
    builder = MagicMock(side_effect=AssertionError("a caller-supplied factory must not be replaced"))
    monkeypatch.setattr(background, "async_sessionmaker", builder)

    async with background_session(_ORG, session_factory=factory) as session:
        await session.execute(text("INSERT INTO probe (id) VALUES (7)"))

    builder.assert_not_called()
    assert await _count_probe_rows(sqlite_engine) == 1


async def test_background_session_builds_its_factory_from_the_callers_engine(sqlite_engine: AsyncEngine) -> None:
    """An engine-only call site keeps ITS engine (the pooled shared engine)."""
    async with background_session(_ORG, engine=sqlite_engine) as session:
        assert isinstance(session, AsyncSession)
        assert session.get_bind().dialect.name == "sqlite"


async def test_background_session_falls_back_to_the_process_engine(sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no factory and no engine, the process session factory is used.

    That is the common case for background code that has no engine of its own:
    it must reuse ``farna.db.session.AsyncSessionLocal`` rather than build a
    second engine/pool (ADR 070 §3). The process factory is swapped for the
    private SQLite factory so the test never touches the real shared engine.
    """
    import farna.db.session as db_session

    process_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)
    monkeypatch.setattr(db_session, "AsyncSessionLocal", process_factory)

    async with background_session(_ORG) as session:
        assert isinstance(session, AsyncSession)
        assert session.info["org_id"] == _ORG
        await session.execute(text("INSERT INTO probe (id) VALUES (11)"))

    assert await _count_probe_rows(sqlite_engine) == 1


async def test_apply_background_grant_sets_both_markers_on_an_open_transaction(sqlite_engine: AsyncEngine) -> None:
    """The shared grant helper is org + execution context, in that order."""
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await apply_background_grant(session, _ORG)
        assert session.info["org_id"] == _ORG
        assert session.info["execution_context"] is True


async def test_the_identity_and_grant_helpers_record_the_tripwire_markers(sqlite_engine: AsyncEngine) -> None:
    """The evidence the tripwire reads is written by the RLS helpers themselves.

    ``db.rls_tripwire`` is a SYNC ``do_orm_execute`` listener: it cannot read the
    server-side GUCs, so it reads these in-process markers. Without this the
    tripwire would fire on every correctly-scoped request.
    """
    from farna.db.rls import set_rls_user_context

    account_id = uuid.uuid4()
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False, autobegin=False)
    async with factory() as session, session.begin():
        await apply_background_grant(session, _ORG)
        await set_rls_user_context(session, account_id, "operator")
        assert session.info["user_id"] == account_id
        assert session.info["org_role"] == "operator"
        assert session.info["execution_context"] is True
