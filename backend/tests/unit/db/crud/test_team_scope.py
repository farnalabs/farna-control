"""Unit tests for ``farna.db.crud.team_scope`` (FAR-1515).

The connector re-scope gate (``farna.api.routes.connectors``) locates the
pipelines that bind a connector through ``pipelines_binding_connector``. That
helper is a cheap text-contains prefilter read through ``team_blind_org_scope``;
off Postgres there is no team RLS policy, so the scope helper issues no SQL and
the query runs directly. The test double's bind reports ``sqlite`` to exercise
that path without a database.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.sql.elements import False_

from farna.db.crud.team_scope import (
    BlindPipelineScope,
    TeamBlindScopeOrgMismatchError,
    account_team_scope_clause,
    pipeline_team_scope_team_blind,
    pipelines_binding_connector,
    team_blind_org_scope,
    viewer_run_scope_visible,
)
from farna.db.models.pipeline import Pipeline

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_FOREIGN_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


def _pg_session(guc_prev: tuple[object, object]) -> AsyncMock:
    """A postgres-dialect session double whose GUC read returns *guc_prev*.

    Every ``execute`` returns the GUC result: the widen/restore statements'
    result is unused by the helper, so their assertions go through the call
    log (``await_args_list``) instead.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "postgresql"
    session.get_bind = MagicMock(return_value=bind)
    guc_result = MagicMock()
    guc_result.one.return_value = guc_prev
    session.execute = AsyncMock(return_value=guc_result)
    return session


def _executed_sql(session: AsyncMock) -> list[str]:
    return [str(call.args[0]) for call in session.execute.await_args_list]


async def test_team_blind_org_scope_refuses_a_foreign_org() -> None:
    """The widen may not SET the org: a mismatched argument is an authority grant.

    ADR 070 §3: no escape may widen past ``organisation_id``. Before this
    assertion the helper wrote the ARGUMENT into ``app.organisation_id``, so a
    caller passing a different org silently crossed the tenant boundary.
    """
    session = _pg_session((str(_ORG_ID), ""))

    with pytest.raises(TeamBlindScopeOrgMismatchError) as excinfo:
        async with team_blind_org_scope(session, _FOREIGN_ORG_ID):
            pass

    assert excinfo.value.requested_org_id == _FOREIGN_ORG_ID
    assert excinfo.value.session_org_id == str(_ORG_ID)
    assert str(_FOREIGN_ORG_ID) in str(excinfo.value)
    # Exactly the GUC read ran: nothing may be widened or rewritten.
    assert session.execute.await_count == 1, _executed_sql(session)
    assert str(_FOREIGN_ORG_ID) not in "".join(_executed_sql(session))


async def test_team_blind_org_scope_refuses_when_the_session_has_no_org() -> None:
    """An unset org context is refused, never adopted from the argument."""
    session = _pg_session(("", ""))

    with pytest.raises(TeamBlindScopeOrgMismatchError) as excinfo:
        async with team_blind_org_scope(session, _ORG_ID):
            pass

    assert excinfo.value.session_org_id in ("", None)
    assert "<unset>" in str(excinfo.value)
    assert session.execute.await_count == 1, _executed_sql(session)


async def test_team_blind_org_scope_widens_only_the_execution_context_for_a_matching_org() -> None:
    """The legitimate path: same org, widen the TEAM clause, restore afterwards."""
    session = _pg_session((str(_ORG_ID), ""))
    entered = False

    async with team_blind_org_scope(session, _ORG_ID):
        entered = True

    assert entered
    assert session.execute.await_count == 3, _executed_sql(session)
    widen_call = session.execute.await_args_list[1]
    assert "app.execution_context', 'true'" in str(widen_call.args[0])
    assert widen_call.args[1] == {"oid": str(_ORG_ID)}, "the widened read must stay org-scoped"
    restore_call = session.execute.await_args_list[2]
    assert restore_call.args[1] == {"oid": str(_ORG_ID), "exec_ctx": ""}, "the caller's GUCs must be restored"


def test_account_team_scope_clause_missing_account_is_fail_closed() -> None:
    """An absent viewer account renders an always-false predicate, never an unscoped read."""
    clause = account_team_scope_clause(Pipeline.owner_team_id, None)

    assert isinstance(clause, False_)


def test_account_team_scope_clause_allows_org_rows_and_membership_rows() -> None:
    account_id = uuid.uuid4()
    sql = str(account_team_scope_clause(Pipeline.owner_team_id, account_id))

    assert "owner_team_id IS NULL" in sql
    assert "team_memberships" in sql
    assert "account_id" in sql


def _visible_session(row: object) -> tuple[AsyncMock, list[object]]:
    session = AsyncMock()
    captured: list[object] = []

    async def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        captured.append(stmt)
        result = MagicMock()
        result.first = MagicMock(return_value=row)
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session, captured


async def test_viewer_run_scope_visible_true_when_membership_covers_the_run() -> None:
    summary = MagicMock()
    session, captured = _visible_session(summary)

    visible = await viewer_run_scope_visible(session, uuid.uuid4(), uuid.uuid4())

    assert visible is True
    sql = str(captured[0])
    assert "team_memberships" in sql, "the run visibility read must carry the membership clamp"
    assert "COALESCE" in sql.upper(), "the run's stamped owner must take precedence over the pipeline owner"


async def test_viewer_run_scope_visible_false_when_outside_the_membership_boundary() -> None:
    session, _captured = _visible_session(None)

    visible = await viewer_run_scope_visible(session, uuid.uuid4(), uuid.uuid4())

    assert visible is False


async def test_pipelines_binding_connector_returns_the_prefilter_rows() -> None:
    connector_id = uuid.uuid4()
    first, second = MagicMock(), MagicMock()

    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.scalars = MagicMock(return_value=[first, second])
    session.execute = AsyncMock(return_value=result)

    rows = await pipelines_binding_connector(session, _ORG_ID, connector_id)

    assert rows == [first, second]
    assert session.execute.await_count == 1


async def test_pipelines_binding_connector_returns_empty_when_nothing_binds() -> None:
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    result = MagicMock()
    result.scalars = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=result)

    rows = await pipelines_binding_connector(session, _ORG_ID, uuid.uuid4())

    assert rows == []
    assert session.execute.await_count == 1


def _first_result(row: object) -> MagicMock:
    result = MagicMock()
    result.first = MagicMock(return_value=row)
    return result


async def test_pipeline_team_scope_team_blind_non_postgres_absent_row_returns_none() -> None:
    # Off Postgres there is no team RLS policy, so the plain org-scoped read IS
    # the team-blind view. An absent row must resolve to None (not raise).
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result(None))

    with patch("farna.db.rls._ensure_active_transaction", AsyncMock(return_value="sqlite")):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope is None
    assert session.execute.await_count == 1


async def test_pipeline_team_scope_team_blind_postgres_reads_then_clears_guc() -> None:
    # On Postgres the read flips the execution-context GUC on, reads the row,
    # then clears the GUC in the finally so the rest of the txn stays
    # caller-facing. Two execute calls: the read + the GUC clear.
    owner = uuid.uuid4()
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result((owner, "team")))

    with (
        patch("farna.db.rls._ensure_active_transaction", AsyncMock(return_value="postgresql")),
        patch("farna.db.rls.set_rls_execution_context", new_callable=AsyncMock) as mock_set,
    ):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope == BlindPipelineScope(owner_team_id=owner, visibility="team")
    mock_set.assert_awaited_once_with(session)
    assert session.execute.await_count == 2


async def test_pipeline_team_scope_team_blind_postgres_absent_row_returns_none() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_first_result(None))

    with (
        patch("farna.db.rls._ensure_active_transaction", AsyncMock(return_value="postgresql")),
        patch("farna.db.rls.set_rls_execution_context", new_callable=AsyncMock),
    ):
        scope = await pipeline_team_scope_team_blind(session, _PIPELINE_ID)

    assert scope is None
