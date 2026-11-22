"""Shared harness for the pipeline team-gate contract tests.

``test_clone_lock_timeout_order.py``, ``test_pipeline_clone_move_team_gate.py``,
``test_pipeline_graph_node_patch_team_gate.py`` and
``test_pipeline_graph_snapshot_team_gate.py`` all drive the REAL FastAPI
dependency stack against a MOCKED AsyncSession: a tenant principal is injected
through ``get_current_user``, the session through ``get_db_session``, and the
routes are then observed over ``TestClient``. That arrangement - the principal,
the settings stub, the session double's transaction/RULE contracts and the
client - is defined once here.

What stays in each contract module is what makes it a contract: the statements
its session must answer, the rows it serves, and every response expectation.
The two-layer team gate every one of them exercises is:

* the request-time ``require_team_membership_or_admin`` dependency (this
  harness's session answers its ``team_memberships`` probe), and
* ``_reapply_team_gate_inside_mutation_txn`` inside the mutation transaction
  (the ``FROM pipelines ... FOR UPDATE`` re-select),

which is why ``make_gate_session`` dispatches on STATEMENT TEXT rather than on
call position: a statement-order change must not be able to silently swap one
layer's answer for the other's.

Nothing here asserts.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from farna.api.dependencies import _get_engine, get_db_session, get_plan_context
from farna.api.main import app
from farna.auth.dependencies import get_current_user
from farna.auth.jwt import AuthenticatedPrincipal
from farna.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble

VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEAM_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
PREFIX = "farna.api.routes.pipelines."
NON_MEMBER_DETAIL = "Not a member of the team that owns this resource"

#: Sentinel for "the caller did not supply this answer" - ``None`` is itself a
#: meaningful answer for several of the session's readers.
_UNSET = object()


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=VALID_32,
        fernet_key=VALID_32,
        modulo_admin_password="testpass",
    )


def result(*, first: Any = None, scalar_one_or_none: Any = None) -> MagicMock:
    """A plain ``MagicMock`` result with every sync reader typed to its contract.

    ``scalars().all()`` defaults to no rows; a test that needs rows overrides
    it. Never build these as children of the AsyncMock session - those are
    AsyncMocks and turn a synchronous read into a never-awaited coroutine.
    """
    res = MagicMock()
    res.first.return_value = first
    res.scalar_one_or_none.return_value = scalar_one_or_none
    scalars = MagicMock()
    scalars.all.return_value = []
    res.scalars.return_value = scalars
    return res


def make_session(*, dialect: str = "sqlite") -> AsyncMock:
    """The house ``AsyncSession`` double, minus its statement answers.

    Every SYNC member of a real ``AsyncSession`` is typed sync (``begin`` /
    ``begin_nested`` / ``in_transaction`` / ``add`` / ``get_bind``) so a route
    that calls them synchronously cannot orphan a coroutine, and the bind
    reports *dialect* so the Postgres-only ``set_config('lock_timeout', ...)``
    statement is or is not issued by the route itself.
    """
    session = AsyncMock()
    configure_rls_preamble(session)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    session.info = {}
    bind = MagicMock()
    bind.dialect.name = dialect
    session.get_bind = MagicMock(return_value=bind)
    return session


def make_gate_session(
    *,
    is_member: bool,
    resolver_scope: tuple[uuid.UUID | None, str],
    locked_row: Any,
    resolver_row: Any = _UNSET,
    extra_queries: Callable[[object, str], Any] | None = None,
    dialect: str = "sqlite",
) -> AsyncMock:
    """Session double answering the two-layer team gate (and anything extra).

    Answers, dispatched on SQL text:

    * ``set_config`` (RLS org/user context) -> no-op,
    * ``authz_enforce`` (permission kill-switch read) -> enforce default,
    * ``team_memberships`` -> a membership row iff ``is_member``; every probe is
      counted in ``session.membership_query_count``, so a test can prove WHICH
      layer asked (an org-scoped request-time gate asks none, so a count there
      is the in-txn re-check's),
    * ``FROM pipelines ... FOR UPDATE`` (the in-txn gate's locked re-select) ->
      ``locked_row``,
    * ``FROM pipelines`` (no ``FOR UPDATE``) -> ``resolver_scope`` via
      ``.first()`` (the request-time team-scope resolver), and ``resolver_row``
      via ``.scalar_one_or_none()`` when supplied (the clone source read shares
      that statement shape).

    ``extra_queries(stmt, sql)`` is consulted FIRST so a contract module can
    answer its own tables; return ``None`` for statements it does not own. Use
    it only for SQL that none of the rules above match - the base rules are
    matched on substrings and must stay unambiguous.

    ``dialect`` defaults to ``sqlite`` so the Postgres-only lock-timeout
    statement is not issued; the ordering test that needs it asks for
    ``postgresql``.
    """
    session = make_session(dialect=dialect)
    session.membership_query_count = 0

    async def _execute(stmt: object, *_args: Any, **_kwargs: Any) -> MagicMock:
        sql = str(stmt)
        if extra_queries is not None:
            extra = extra_queries(stmt, sql)
            if extra is not None:
                return extra
        if "set_config" in sql:
            return result()
        if "authz_enforce" in sql:
            return result(scalar_one_or_none=None)
        if "team_memberships" in sql:
            session.membership_query_count += 1
            return result(first=uuid.uuid4() if is_member else None)
        if isinstance(stmt, Select) and "FROM pipelines" in sql:
            if "FOR UPDATE" in sql.upper():
                return result(scalar_one_or_none=locked_row)
            resolved = result(first=resolver_scope)
            if resolver_row is not _UNSET:
                resolved.scalar_one_or_none.return_value = resolver_row
            return resolved
        return result()

    session.execute = AsyncMock(side_effect=_execute)
    return session


@contextmanager
def client_for(session: AsyncMock, role: str) -> Generator[TestClient, None, None]:
    """A ``TestClient`` with the auth, plan, settings and session deps overridden.

    ``role`` becomes the tenant principal's ``org_role`` (``admin`` is the
    org-admin bypass case, anything else is a plain org member). Overrides are
    cleared on exit, as the routes' dependency overrides are process-global.
    """

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{role}@test",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role=role,
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@contextmanager
def patched(patches: list) -> Generator[None, None, None]:
    """Enter every patch in *patches* for the block."""
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield
