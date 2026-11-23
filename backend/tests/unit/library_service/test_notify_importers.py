"""Unit tests for ``notify_importers_of_update`` (FAR-759 update back-propagation).

When a new version in a version group is published (e.g. a collection via
``POST /libraries/collections/{id}/publish``), every auto-update recipient —
fork copies and installed-collection rows — is stamped with
``update_available_version_id``. Relocated from the deleted
``test_contribution_flow.py`` when the community library was removed; the
back-propagation itself is not community-specific.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from farna.core.library_service import notify_importers_of_update

from .helpers import mock_session as _mock_session


def _fake_primitive(**overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = uuid.uuid4()
    p.version = "1.0"
    p.auto_update = True
    p.version_group_id = None
    p.forked_from = None
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _scalars_result(values: list[object]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=values)
    return r


class TestNotifyImportersOfUpdate:
    async def test_noop_when_primitive_missing(self) -> None:
        session = _mock_session()

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("farna.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            patch("farna.core.library_service.mark_update_available", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        updater.assert_not_called()

    async def test_noop_when_no_version_group(self) -> None:
        session = _mock_session()
        prim = _fake_primitive()
        prim.version_group_id = None

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("farna.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("farna.core.library_service.mark_update_available", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        updater.assert_not_called()

    async def test_updates_auto_update_copies(self) -> None:
        session = _mock_session()
        org_id = uuid.uuid4()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.1")
        prim.version_group_id = group_id

        copy1 = _fake_primitive()
        copy1.auto_update = True
        copy2 = _fake_primitive()
        copy2.auto_update = False

        # Query order: fork copies first, then installed-collection rows.
        session.execute = AsyncMock(side_effect=[_scalars_result([copy1, copy2]), _scalars_result([])])

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("farna.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("farna.core.library_service.mark_update_available", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, org_id, uuid.uuid4())

        # Only the auto_update copy is touched, and the caller's org rides into
        # the clamped stamp (FAR-2696).
        updater.assert_awaited_once()
        assert updater.await_args.kwargs["org_id"] == org_id
        assert updater.await_args.kwargs["recipient_id"] == copy1.id
        assert updater.await_args.kwargs["version_id"] == prim.id

    async def test_updates_installed_collection_rows(self) -> None:
        """FAR-759: installed collection rows in the version group are stamped too."""
        session = _mock_session()
        org_id = uuid.uuid4()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.1")
        prim.version_group_id = group_id

        installed = _fake_primitive()
        installed.auto_update = True

        # Fork query empty; installed-collection query returns the row.
        session.execute = AsyncMock(side_effect=[_scalars_result([]), _scalars_result([installed])])

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("farna.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("farna.core.library_service.mark_update_available", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, org_id, uuid.uuid4())

        updater.assert_awaited_once()
        assert updater.await_args.kwargs["org_id"] == org_id
        assert updater.await_args.kwargs["recipient_id"] == installed.id
        assert updater.await_args.kwargs["version_id"] == prim.id

    async def test_per_copy_update_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.1")
        prim.version_group_id = group_id

        copy = _fake_primitive()
        copy.auto_update = True

        session.execute = AsyncMock(side_effect=[_scalars_result([copy]), _scalars_result([])])

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("farna.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch(
                "farna.core.library_service.mark_update_available",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError("boom"),
            ),
            caplog.at_level("ERROR", logger="farna.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("failed to update copy" in rec.message and str(copy.id) in rec.message for rec in caplog.records)

    async def test_programming_error_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "farna.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            caplog.at_level("WARNING", logger="farna.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("failed (DB not migrated)" in rec.message for rec in caplog.records)

    async def test_cancellation_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "farna.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

    async def test_generic_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()

        with (
            patch("farna.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "farna.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            caplog.at_level("ERROR", logger="farna.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("unexpected error" in rec.message for rec in caplog.records)
