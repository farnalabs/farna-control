"""The ACCOUNT boundary on the in-app notification WRITE paths (ADR 070 §1).

``notifications`` is declared ACCOUNT-scoped (``target_user_id``) and
``dismissals`` (``dismissed_by_user_id``): a ``scope='user'`` notification
belongs to the one account it names, and a ``scope='admin'`` notification is
readable only by an org admin. The READ paths enforce that with
``db.crud.notifications._visible_to_user_clause`` - ``get_notification``,
the dashboard, the inbox list and the unread badge all hide a row the caller
may not see.

The WRITE paths did not. ``dismiss_notification`` (which ``review_later``
delegates to) fetched the row on ``(organisation_id, id)`` alone, so any
account in the same organisation could:

* dismiss another account's private (``scope='user'``) notification for
  themselves - an existence oracle on a row their own ``GET`` answers 404 for;
* ESCALATE that to ``dismiss_scope='scope'`` when the notification is
  ``any_scope``-dismissible, hiding it for the org - including from the very
  account it was addressed to. That is a write to another account's row;
* dismiss an ``scope='admin'`` notification they cannot read.

PROVE-THE-FIX: every assertion below fails on the pre-fix code (the dismiss
answers 200 and the other account's inbox loses the row). After the fix an
invisible row is refused exactly like a missing id - ``400`` with
``"Notification not found"`` - so the write cannot confirm existence either.

Real endpoints on a real SQLite session (no CRUD mocking): the defect is in the
WHERE clause, so a mock would reproduce nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.api.dependencies import get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_tenant_user, get_current_user
from farna.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from farna.db.models.account import Account
from farna.db.models.base import Base
from farna.db.models.notification import Dismissal, Notification, NotificationPreference
from farna.db.models.org_membership import OrgMembership
from farna.settings import Settings, get_settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
#: Account A - the attacker/credential holder in every test.
_USER_A = uuid.UUID("00000000-0000-0000-0000-000000000002")
#: Account B - the owner of the private notification.
_USER_B = uuid.UUID("00000000-0000-0000-0000-000000000003")

_BASE = "/api/v1/notifications/in-app"
_DEFAULT_INBOX = f"{_BASE}?page=1&page_size=20"

#: ``scope='user'`` addressed to B; ``any_scope`` makes a scope dismissal legal
#: for anyone who can SEE the row - which A cannot.
_PRIVATE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
#: ``scope='admin'`` - hidden from a viewer by the read clause.
_ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
#: ``scope='org'`` - genuinely org-visible; A must still be able to dismiss it.
_ORG_VISIBLE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000b3")

_TABLES = [
    Account.__table__,
    Notification.__table__,
    NotificationPreference.__table__,
    Dismissal.__table__,
    # _visible_to_user_clause reads org_memberships (EXISTS) for the admin arm.
    OrgMembership.__table__,
]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        admin_password="testpass",
    )


def _account(account_id: uuid.UUID, *, email: str) -> Account:
    now = datetime.now(UTC)
    return Account(
        id=account_id,
        email=email,
        display_name=email.split("@", maxsplit=1)[0],
        auth_provider="local",
        preferences={},
        active=True,
        is_system_admin=False,
        is_break_glass=False,
        created_at=now,
        updated_at=now,
    )


def _membership(account_id: uuid.UUID) -> OrgMembership:
    return OrgMembership(
        organisation_id=_ORG_ID,
        account_id=account_id,
        role="viewer",
        joined_at=datetime.now(UTC),
    )


def _notification(
    *,
    notification_id: uuid.UUID,
    scope: str,
    target_user_id: uuid.UUID | None,
    title: str,
) -> Notification:
    now = datetime.now(UTC)
    return Notification(
        id=notification_id,
        organisation_id=_ORG_ID,
        scope=scope,
        target_user_id=target_user_id,
        level="warning",
        category="hitl.awaiting",
        title=title,
        body="A governance run is waiting for human review.",
        action_url=None,
        dismiss_strategy="any_scope",
        dismissible_at_scope=True,
        expires_at=now + timedelta(hours=72),
        created_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )


@pytest.fixture
def client(tmp_path: Path) -> Generator[tuple[TestClient, SimpleNamespace], None, None]:
    """One org, two ``viewer`` accounts, and three notification scopes.

    Yields ``(client, actor)``; set ``actor.account_id`` to switch the caller
    (both accounts are plain viewers in the SAME organisation).
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'notif_account_boundary.db'}", echo=False)
    seeded = False

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        nonlocal seeded
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
        async with maker() as session:
            if not seeded:
                async with session.begin():
                    session.add(_account(_USER_A, email="a@example.com"))
                    session.add(_account(_USER_B, email="b@example.com"))
                    session.add(_membership(_USER_A))
                    session.add(_membership(_USER_B))
                    session.add(
                        _notification(
                            notification_id=_PRIVATE_ID,
                            scope="user",
                            target_user_id=_USER_B,
                            title="Private to account B",
                        )
                    )
                    session.add(
                        _notification(
                            notification_id=_ADMIN_ID,
                            scope="admin",
                            target_user_id=None,
                            title="Admins only",
                        )
                    )
                    session.add(
                        _notification(
                            notification_id=_ORG_VISIBLE_ID,
                            scope="org",
                            target_user_id=None,
                            title="Org-wide",
                        )
                    )
                seeded = True
            yield session

    actor = SimpleNamespace(account_id=_USER_A, username="user-a", org_role="viewer")

    def _current_user() -> AuthenticatedPrincipal:
        return AuthenticatedPrincipal(
            username=actor.username,
            organisation_id=_ORG_ID,
            account_id=actor.account_id,
            org_role=actor.org_role,
        )

    def _current_tenant_user() -> TenantPrincipal:
        return TenantPrincipal(
            username=actor.username,
            organisation_id=_ORG_ID,
            account_id=actor.account_id,
            org_role=actor.org_role,
        )

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_current_user] = _current_user
    app.dependency_overrides[get_current_tenant_user] = _current_tenant_user
    app.dependency_overrides[get_plan_context] = lambda: mock_plan

    try:
        with (
            patch("farna.api.routes.in_app_notifications.set_rls_org", new=AsyncMock()),
            patch("farna.api.routes.in_app_notifications.set_rls_user_context", new=AsyncMock()),
        ):
            yield TestClient(app), actor
    finally:
        asyncio.run(engine.dispose())
        app.dependency_overrides.clear()


def _ids(resp: object) -> list[str]:
    assert resp.status_code == 200, resp.text  # type: ignore[attr-defined]
    return [str(row["id"]) for row in resp.json()["items"]]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The read boundary is the contract the writes must not bypass
# ---------------------------------------------------------------------------


def test_another_accounts_private_notification_is_not_readable(client: tuple[TestClient, SimpleNamespace]) -> None:
    """Baseline: A's own detail read answers 404 for B's private row."""
    http, _actor = client
    assert http.get(f"{_BASE}/{_PRIVATE_ID}").status_code == 404


def test_admin_scope_notification_is_not_readable_by_a_viewer(client: tuple[TestClient, SimpleNamespace]) -> None:
    """Baseline: the admin row is invisible to A, so A has no reason to act on it."""
    http, _actor = client
    assert http.get(f"{_BASE}/{_ADMIN_ID}").status_code == 404


# ---------------------------------------------------------------------------
# The write paths must enforce the same boundary
# ---------------------------------------------------------------------------


def test_dismissing_another_accounts_private_notification_is_refused(client: tuple[TestClient, SimpleNamespace]) -> None:
    """PROVE-THE-FIX: A cannot file a dismissal against B's private row.

    Pre-fix this answered 200 ``dismissed_for_self`` - an existence oracle for
    a row A's own GET answers 404 for, and a write recorded against it.
    """
    http, actor = client

    resp = http.post(f"{_BASE}/{_PRIVATE_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 400, resp.text
    assert "not found" in resp.json()["detail"]

    actor.account_id = _USER_B
    assert str(_PRIVATE_ID) in _ids(http.get(_DEFAULT_INBOX))


def test_scope_dismiss_cannot_hide_another_accounts_private_notification(client: tuple[TestClient, SimpleNamespace]) -> None:
    """PROVE-THE-FIX: the cross-account WRITE.

    ``dismiss_scope='scope'`` hides a row for the whole org (the documented
    behaviour of the scope dismissal). Allowed on a private row A cannot read,
    it let A hide B's notification from B. After the fix the escalation is
    refused and B still sees it.
    """
    http, actor = client

    resp = http.post(f"{_BASE}/{_PRIVATE_ID}/dismiss", json={"dismiss_scope": "scope"})

    assert resp.status_code == 400, resp.text

    actor.account_id = _USER_B
    assert str(_PRIVATE_ID) in _ids(http.get(_DEFAULT_INBOX))


def test_review_later_of_another_accounts_private_notification_is_refused(client: tuple[TestClient, SimpleNamespace]) -> None:
    """``review_later`` delegates to the same write, so it needs the same refusal."""
    http, actor = client

    resp = http.post(f"{_BASE}/{_PRIVATE_ID}/review-later")

    assert resp.status_code == 400, resp.text

    actor.account_id = _USER_B
    assert str(_PRIVATE_ID) in _ids(http.get(_DEFAULT_INBOX))


def test_a_viewer_cannot_dismiss_an_admin_scope_notification(client: tuple[TestClient, SimpleNamespace]) -> None:
    """PROVE-THE-FIX: an admin-scope row A cannot read is also not dismissible."""
    http, _actor = client

    resp = http.post(f"{_BASE}/{_ADMIN_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 400, resp.text


def test_org_visible_notification_is_still_dismissible(client: tuple[TestClient, SimpleNamespace]) -> None:
    """Regression guard: an ``org``-scope row IS visible to A, so the fix must not
    over-block the ordinary dismissal path."""
    http, _actor = client
    assert str(_ORG_VISIBLE_ID) in _ids(http.get(_DEFAULT_INBOX))

    resp = http.post(f"{_BASE}/{_ORG_VISIBLE_ID}/dismiss", json={"dismiss_scope": "self"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "dismissed_for_self"}
    assert str(_ORG_VISIBLE_ID) not in _ids(http.get(_DEFAULT_INBOX))
