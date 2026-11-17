"""Lookup fallbacks and listing over the built-in modulo registry.

Removing the community library must not remove the in-code modulo registry:
``get_primitive`` / ``get_primitive_by_slug`` / ``_re_read_primitive`` still
fall back to it on a DB miss, and ``list_primitives`` still serves it on the
``source="modulo"`` and default (merged) paths.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from farna.core.library_service import (
    _MODULO_PRIMITIVES,
    _filter_modulo,
    _re_read_primitive,
    get_primitive,
    get_primitive_by_slug,
    list_primitives,
)
from farna.db.crud.base import PageResult

_MODULE = "farna.core.library_service"
_BUILTIN = _MODULO_PRIMITIVES[0]


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

    async def test_db_miss_falls_back_to_modulo_registry(self) -> None:
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

    async def test_db_miss_falls_back_to_modulo_registry(self) -> None:
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
# _filter_modulo
# ---------------------------------------------------------------------------


class TestFilterModulo:
    def test_no_filter_returns_whole_registry(self) -> None:
        assert len(_filter_modulo()) == len(_MODULO_PRIMITIVES)

    def test_primitive_types_takes_precedence(self) -> None:
        result = _filter_modulo(primitive_types=["schema"], primitive_type="agent")
        assert result
        assert {p.primitive_type for p in result} == {"schema"}

    def test_single_primitive_type(self) -> None:
        result = _filter_modulo(primitive_type="schema")
        assert result
        assert {p.primitive_type for p in result} == {"schema"}

    def test_search_matches_name_case_insensitively(self) -> None:
        term = _BUILTIN.name.upper()
        result = _filter_modulo(search=f"  {term}  ")
        assert _BUILTIN in result

    def test_blank_search_is_ignored(self) -> None:
        assert len(_filter_modulo(search="   ")) == len(_MODULO_PRIMITIVES)


# ---------------------------------------------------------------------------
# list_primitives
# ---------------------------------------------------------------------------


class TestListPrimitivesModuloSource:
    async def test_paginates_registry_with_has_more(self) -> None:
        native = [p for p in _MODULO_PRIMITIVES if p.tier != "in_dev"]
        result = await list_primitives(_session(), uuid.uuid4(), source="modulo", page=1, page_size=1)
        assert len(result.items) == 1
        assert result.total == len(native)
        assert result.has_more is (len(native) > 1)
        assert result.next_cursor is None

    async def test_last_page_has_no_more(self) -> None:
        result = await list_primitives(_session(), uuid.uuid4(), source="modulo", page=1, page_size=10_000)
        assert result.has_more is False

    async def test_default_excludes_in_dev_tier(self) -> None:
        in_dev = _prim(tier="in_dev")
        with patch(f"{_MODULE}._MODULO_PRIMITIVES", [_BUILTIN, in_dev]):
            result = await list_primitives(_session(), uuid.uuid4(), source="modulo")
        assert in_dev not in result.items
        assert _BUILTIN in result.items

    async def test_empty_excluded_tiers_keeps_in_dev(self) -> None:
        in_dev = _prim(tier="in_dev")
        with patch(f"{_MODULE}._MODULO_PRIMITIVES", [_BUILTIN, in_dev]):
            result = await list_primitives(_session(), uuid.uuid4(), source="modulo", excluded_tiers=[])
        assert in_dev in result.items


class TestListPrimitivesDefaultMerge:
    async def test_org_page_merged_with_registry(self) -> None:
        org_row = _prim()
        in_dev = _prim(tier="in_dev")
        org_page = PageResult(items=[org_row], total=3, page=1, page_size=20, next_cursor="c2")
        lister = AsyncMock(return_value=org_page)
        with (
            patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
            patch(f"{_MODULE}.list_library_primitives", new=lister),
            patch(f"{_MODULE}._MODULO_PRIMITIVES", [_BUILTIN, in_dev]),
        ):
            result = await list_primitives(_session(), uuid.uuid4())
        assert result.items == [org_row, _BUILTIN]
        assert result.total == 4
        assert result.has_more is True
        assert result.next_cursor == "c2"
        assert lister.await_args.kwargs["source"] is None

    async def test_empty_excluded_tiers_keeps_in_dev_builtins(self) -> None:
        in_dev = _prim(tier="in_dev")
        org_page = PageResult(items=[], total=0, page=1, page_size=20)
        with (
            patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
            patch(f"{_MODULE}.list_library_primitives", new=AsyncMock(return_value=org_page)),
            patch(f"{_MODULE}._MODULO_PRIMITIVES", [in_dev]),
        ):
            result = await list_primitives(_session(), uuid.uuid4(), excluded_tiers=[])
        assert result.items == [in_dev]
        assert result.has_more is False
