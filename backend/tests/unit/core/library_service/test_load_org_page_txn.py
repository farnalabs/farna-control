"""FAR-2286: the org page load in ``list_primitives`` must reuse a caller-owned transaction."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from farna.core.library_service import list_primitives
from farna.db.crud.base import PageResult

_MODULE = "farna.core.library_service"


def _session(*, in_transaction: bool) -> MagicMock:
    session = MagicMock()
    session.in_transaction = MagicMock(return_value=in_transaction)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    return session


async def _load(session: MagicMock, page_result: PageResult) -> PageResult:
    with (
        patch(f"{_MODULE}.set_rls_org", new=AsyncMock()),
        patch(f"{_MODULE}.list_library_primitives", new=AsyncMock(return_value=page_result)),
    ):
        return await list_primitives(session, uuid.uuid4(), source="local")


async def test_active_transaction_is_reused_not_nested():
    session = _session(in_transaction=True)
    expected = PageResult(items=[MagicMock()], total=1, page=1, page_size=20)

    org_page = await _load(session, expected)

    assert org_page.total == 1
    session.begin.assert_not_called()


async def test_begins_transaction_when_none_active():
    session = _session(in_transaction=False)
    expected = PageResult(items=[MagicMock()], total=1, page=1, page_size=20)

    org_page = await _load(session, expected)

    assert org_page.total == 1
    session.begin.assert_called_once_with()
