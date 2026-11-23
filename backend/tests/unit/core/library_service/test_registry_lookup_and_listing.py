"""Lookup fallbacks and listing over the built-in registry.

Removing the community library must not remove the in-code built-in registry:
``get_primitive`` / ``get_primitive_by_slug`` / ``_re_read_primitive`` still
fall back to it on a DB miss, and ``list_primitives`` still serves it on the
``source="farna_control"`` and default (merged) paths, while every other ``source``
value (local / registry) reaches the SQL filter with an exact COUNT. The
``community`` source is retired (migration 0356), so it is no longer a live
DB-only source and is not exercised here.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from farna.core.library_service import (
    _BUILTIN_PRIMITIVES,
    _filter_builtins,
    _re_read_primitive,
    get_primitive,
    get_primitive_by_slug,
    list_primitives,
)
from farna.db.crud.base import PageResult

_MODULE = "farna.core.library_service"
_BUILTIN = _BUILTIN_PRIMITIVES[0]


def _session() -> MagicMock:
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _slug_session(row: object | None) -> MagicMock:
    session = _session()
    result = MagicMock()
    result.scalars.return_value.first.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


def _prim(*, tier: str = "native") -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.tier = tier
    return p


# ---------------------------------------------------------------------------
# get_primitive
# ---------------------------------------------------------------------------


class TestGetPrimitiveRegistryFallback:
    async def test_db_row_wins(self) -> None:
        row = _prim()
        with (
            patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
            patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=row)),
        ):
            assert await get_primitive(_session(), uuid.uuid4(), row.id) is row

    async def test_db_miss_falls_back_to_builtin_registry(self) -> None:
        with (
            patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
            patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=None)),
        ):
            assert await get_primitive(_session(), uuid.uuid4(), _BUILTIN.id) is _BUILTIN

    async def test_unknown_id_returns_none(self) -> None:
        with (
            patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
            patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=None)),
        ):
            assert await get_primitive(_session(), uuid.uuid4(), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# get_primitive_by_slug
# ---------------------------------------------------------------------------


class TestGetPrimitiveBySlugRegistryFallback:
    async def test_db_row_wins(self) -> None:
        row = _prim()
        with patch(f"{_MODULE}.set_rls_org", new=AsyncMock()):
            result = await get_primitive_by_slug(_slug_session(row), uuid.uuid4(), _BUILTIN.primitive_type, _BUILTIN.slug)
        assert result is row

    async def test_db_miss_falls_back_to_builtin_registry(self) -> None:
        with patch(f"{_MODULE}.set_rls_org", new=AsyncMock()):
            result = await get_primitive_by_slug(_slug_session(None), uuid.uuid4(), _BUILTIN.primitive_type, _BUILTIN.slug)
        assert result is _BUILTIN

    async def test_matching_version_returns_builtin(self) -> None:
        with patch(f"{_MODULE}.set_rls_org", new=AsyncMock()):
            result = await get_primitive_by_slug(
                _slug_session(None),
                uuid.uuid4(),
                _BUILTIN.primitive_type,
                _BUILTIN.slug,
                version=_BUILTIN.version,
            )
        assert result is _BUILTIN

    async def test_mismatched_version_returns_none(self) -> None:
        with patch(f"{_MODULE}.set_rls_org", new=AsyncMock()):
            result = await get_primitive_by_slug(
                _slug_session(None),
                uuid.uuid4(),
                _BUILTIN.primitive_type,
                _BUILTIN.slug,
                version="999.0",
            )
        assert result is None

    async def test_unknown_slug_returns_none(self) -> None:
        with patch(f"{_MODULE}.set_rls_org", new=AsyncMock()):
            result = await get_primitive_by_slug(_slug_session(None), uuid.uuid4(), "workflow", "no-such-slug")
        assert result is None


# ---------------------------------------------------------------------------
# _re_read_primitive
# ---------------------------------------------------------------------------


class TestReReadPrimitive:
    async def test_db_row_is_in_db(self) -> None:
        row = _prim()
        with patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=row)):
            prim, in_db = await _re_read_primitive(_session(), uuid.uuid4(), row.id)
        assert prim is row
        assert in_db is True

    async def test_builtin_is_not_in_db(self) -> None:
        with patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=None)):
            prim, in_db = await _re_read_primitive(_session(), uuid.uuid4(), _BUILTIN.id)
        assert prim is _BUILTIN
        assert in_db is False

    async def test_missing_raises_lookup_error(self) -> None:
        with (
            patch(f"{_MODULE}.get_library_primitive", new=AsyncMock(return_value=None)),
            pytest.raises(LookupError, match="not found"),
        ):
            await _re_read_primitive(_session(), uuid.uuid4(), uuid.uuid4())


# ---------------------------------------------------------------------------
# _filter_builtins
# ---------------------------------------------------------------------------


class TestFilterBuiltins:
    def test_no_filter_returns_whole_registry(self) -> None:
        assert len(_filter_builtins()) == len(_BUILTIN_PRIMITIVES)

    def test_primitive_types_takes_precedence(self) -> None:
        result = _filter_builtins(primitive_types=["schema"], primitive_type="agent")
        assert result
        assert {p.primitive_type for p in result} == {"schema"}

    def test_single_primitive_type(self) -> None:
        result = _filter_builtins(primitive_type="schema")
        assert result
        assert {p.primitive_type for p in result} == {"schema"}

    def test_search_matches_name_case_insensitively(self) -> None:
        term = _BUILTIN.name.upper()
        result = _filter_builtins(search=f"  {term}  ")
        assert _BUILTIN in result

    def test_blank_search_is_ignored(self) -> None:
        assert len(_filter_builtins(search="   ")) == len(_BUILTIN_PRIMITIVES)


# ---------------------------------------------------------------------------
# list_primitives
# ---------------------------------------------------------------------------


def _org_page(items: list[object], total: int, next_cursor: str | None = None) -> PageResult:
    return PageResult(items=items, total=total, page=1, page_size=20, next_cursor=next_cursor)


async def _list(lister: AsyncMock, registry: list[object] | None = None, **kwargs: object) -> PageResult:
    with (
        patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
        patch(f"{_MODULE}.list_library_primitives", new=lister),
        patch(f"{_MODULE}._BUILTIN_PRIMITIVES", registry if registry is not None else [_BUILTIN]),
    ):
        return await list_primitives(_session(), uuid.uuid4(), **kwargs)


class TestListPrimitivesSourceFilter:
    async def test_db_only_source_reaches_sql_filter_with_exact_total(self) -> None:
        # ``registry`` is the only live DB-only source now that the legacy
        # ``community`` source is retired (migration 0356).
        source = "registry"
        row = _prim()
        lister = AsyncMock(return_value=_org_page([row], total=7))
        result = await _list(lister, source=source)
        assert lister.await_args.kwargs["source"] == source
        assert result.items == [row]
        assert result.total == 7
        assert result.has_more is True

    async def test_local_source_excludes_builtins(self) -> None:
        row = _prim()
        lister = AsyncMock(return_value=_org_page([row], total=1))
        result = await _list(lister, source="local")
        assert lister.await_args.kwargs["source"] == "local"
        assert result.items == [row]
        assert result.total == 1
        assert result.has_more is False

    async def test_builtin_source_appends_whole_registry(self) -> None:
        in_dev = _prim(tier="in_dev")
        lister = AsyncMock(return_value=_org_page([], total=0))
        result = await _list(lister, registry=[_BUILTIN, in_dev], source="farna_control")
        assert lister.await_args.kwargs["source"] == "farna_control"
        assert result.items == [_BUILTIN]
        assert result.total == 1

    async def test_empty_excluded_tiers_keeps_in_dev(self) -> None:
        in_dev = _prim(tier="in_dev")
        lister = AsyncMock(return_value=_org_page([], total=0))
        result = await _list(lister, registry=[_BUILTIN, in_dev], source="farna_control", excluded_tiers=[])
        assert result.items == [_BUILTIN, in_dev]


class TestListPrimitivesDefaultMerge:
    async def test_org_page_merged_with_registry(self) -> None:
        org_row = _prim()
        org_in_dev = _prim(tier="in_dev")
        in_dev = _prim(tier="in_dev")
        lister = AsyncMock(return_value=_org_page([org_row, org_in_dev], total=3, next_cursor="c2"))
        result = await _list(lister, registry=[_BUILTIN, in_dev])
        assert lister.await_args.kwargs["source"] is None
        assert lister.await_args.kwargs["excluded_tiers"] == ["in_dev"]
        assert result.items == [org_row, _BUILTIN]
        assert result.total == 4
        assert result.has_more is True
        assert result.next_cursor == "c2"


class TestListPrimitivesDegradesOnDbFailure:
    async def test_programming_error_serves_builtins(self, caplog: pytest.LogCaptureFixture) -> None:
        lister = AsyncMock(side_effect=ProgrammingError("stmt", {}, Exception("no table")))
        with caplog.at_level("WARNING", logger=_MODULE):
            result = await _list(lister)
        assert result.items == [_BUILTIN]
        assert result.total == 1
        assert any("DB not migrated" in rec.message for rec in caplog.records)

    async def test_unexpected_error_is_logged_and_builtins_served(self, caplog: pytest.LogCaptureFixture) -> None:
        lister = AsyncMock(side_effect=RuntimeError("boom"))
        with caplog.at_level("ERROR", logger=_MODULE):
            result = await _list(lister)
        assert result.items == [_BUILTIN]
        assert any("DB query failed" in rec.message for rec in caplog.records)

    async def test_cancellation_propagates(self) -> None:
        lister = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _list(lister)
