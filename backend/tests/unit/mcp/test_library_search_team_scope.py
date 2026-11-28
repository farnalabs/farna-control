"""Team boundary on the library LISTING / SEARCH path (ADR 070 §5).

``core.library_service.list_primitives`` merged the caller's org page with the
in-code Native built-ins and applied no team predicate, so a team-A-scoped
``mk_`` API key could list — and search up — another team's team-private
primitives: their names, descriptions and tags. The by-id read was already
clamped (FAR-2682 ``_primitive_visible_to``); this module pins the listing.

Why the defect exists at all: ``library_primitives`` carries
``rls_team_isolation``, but the policy keys on the **key owner's** team
memberships, not on the key's own team. A team-A key whose owner happens to be
a member of team B therefore reads team B's team-private rows through RLS, so
only an explicit predicate can hold the boundary.

Every case below runs against in-memory SQLite: no Postgres RLS, no team
policy, so the ONLY thing that can hide the foreign row is the predicate the
lister itself builds. The rows are seeded inside ONE organisation — this is a
team boundary, never a tenant one.

Built-ins (``source="farna_control"``, the in-code registry merged by
``list_primitives``) are not team-owned rows and must stay visible to every
credential — including a team-scoped one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from farna.api import mcp_server
from farna.core.library_service import _BUILTIN_PRIMITIVES, list_primitives
from farna.db.crud.base import PageResult
from farna.db.crud.library_primitive import list_library_primitives
from farna.db.models.base import Base
from farna.db.models.library_primitive import LibraryPrimitive

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f2")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-0000000000fa")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-0000000000fb")
_API_KEY = "mk_testprefix_testsecretkey1234567890xyz"

_SERVICE = "farna.core.library_service"

# One term shared by all three rows' NAMES, so a name search is a real
# discriminator (before the fix it returns the team-B row too).
_NAME_TERM = "sharedteam"
# Terms carried ONLY by the team-B row's description / tags. The current SQL
# search matches ``name`` only, so these pin the security property (the foreign
# row can never be surfaced, and none of its description/tag text can reach a
# team-A caller's response) rather than a description/tag match.
_DESC_TERM = "betadescriptionmarker"
_TAG_TERM = "betatagmarker"


def _primitive(
    *,
    owner_team_id: uuid.UUID | None,
    name: str,
    description: str | None,
    tags: list[str],
    slug: str,
) -> LibraryPrimitive:
    return LibraryPrimitive(
        id=uuid.uuid4(),
        organisation_id=_ORG_ID,
        source="local",
        primitive_type="schema",
        name=name,
        slug=slug,
        description=description,
        author="tester",
        version="1.0",
        tags=list(tags),
        content_json={},
        # ``ck_library_primitives_source_fields`` requires every registry-style
        # field to be NULL for the ``local`` source.
        source_url=None,
        forked_from=None,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=owner_team_id,
        visibility="team" if owner_team_id is not None else "org",
        account_id=None,
        tier="native",
    )


class _Seed:
    """The three primitives every case shares: org-level, team A, team B."""

    def __init__(self) -> None:
        self.org = _primitive(
            owner_team_id=None,
            name=f"{_NAME_TERM} org schema",
            description="org-level platform row",
            tags=[_NAME_TERM, "orgtag"],
            slug="sharedteam-org-schema",
        )
        self.team_a = _primitive(
            owner_team_id=_TEAM_A,
            name=f"{_NAME_TERM} alpha schema",
            description="team-a private row",
            tags=[_NAME_TERM, "alphatag"],
            slug="sharedteam-alpha-schema",
        )
        self.team_b = _primitive(
            owner_team_id=_TEAM_B,
            name=f"{_NAME_TERM} beta schema",
            description=f"team-b private row {_DESC_TERM}",
            tags=[_NAME_TERM, _TAG_TERM],
            slug="sharedteam-beta-schema",
        )

    @property
    def all(self) -> list[LibraryPrimitive]:
        return [self.org, self.team_a, self.team_b]


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Base.metadata.create_all(sync_connection, tables=[LibraryPrimitive.__table__]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _seed(session: AsyncSession) -> _Seed:
    seed = _Seed()
    session.add_all(seed.all)
    await session.commit()
    return seed


@contextmanager
def _mcp_auth(*, team_id: uuid.UUID | None, role: str = "viewer") -> Iterator[None]:
    """Seed the MCP request ContextVars for a team-scoped (or org-wide) caller.

    Mirrors ``McpAuthMiddleware``: a team-scoped ``mk_`` key sets
    ``_ctx_team_id`` to the key's team; an org-wide key / user JWT leaves it
    ``None``. Every var set here is reset afterwards so the process-wide
    ContextVars cannot leak into another test.
    """
    vars_and_values: list[tuple[Any, Any]] = [
        (mcp_server._ctx_org_id, _ORG_ID),
        (mcp_server._ctx_role, role),
        (mcp_server._ctx_user_id, _ACCOUNT_ID),
        (mcp_server._ctx_key_id, uuid.uuid4()),
        (mcp_server._ctx_auth_token, _API_KEY),
        (mcp_server._ctx_auth_type, "api_key"),
        (mcp_server._ctx_team_id, team_id),
        (mcp_server._ctx_node_allowed_tools, None),
        (mcp_server._ctx_key_grants, None),
    ]
    tokens = [var.set(value) for var, value in vars_and_values]
    try:
        yield
    finally:
        for (var, _value), token in zip(vars_and_values, tokens, strict=True):
            var.reset(token)


def _session_context(session: AsyncSession) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@contextmanager
def _library_read_context(session: AsyncSession) -> Iterator[None]:
    """Patch the MCP handler seams: auth re-validation and the DB session."""
    with (
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session", MagicMock(return_value=_session_context(session))),
    ):
        yield


def _ids(payload: dict[str, Any]) -> set[str]:
    return {item["id"] for item in payload["items"]}


# ---------------------------------------------------------------------------
# The MCP tool (``search_library``) — the reported leak
# ---------------------------------------------------------------------------


async def test_team_a_key_lists_org_and_own_team_but_not_team_b(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    with _mcp_auth(team_id=_TEAM_A), _library_read_context(session):
        result = await mcp_server.search_library()

    ids = _ids(result)
    assert str(seed.org.id) in ids
    assert str(seed.team_a.id) in ids
    assert str(seed.team_b.id) not in ids, "a team-A key must not list team B's private primitive"
    # The Native built-ins are not team-owned: they stay visible.
    assert {str(p.id) for p in _BUILTIN_PRIMITIVES} <= ids
    assert result["total"] == 2 + len(_BUILTIN_PRIMITIVES)


async def test_team_a_key_name_search_hides_team_b(
    session: AsyncSession,
) -> None:
    """The term is in all three names, so this fails without the team predicate."""
    seed = await _seed(session)

    with _mcp_auth(team_id=_TEAM_A), _library_read_context(session):
        result = await mcp_server.search_library(search=_NAME_TERM)

    ids = _ids(result)
    assert str(seed.org.id) in ids
    assert str(seed.team_a.id) in ids
    assert str(seed.team_b.id) not in ids
    # No built-in is named/described with the term, so the page is exactly the
    # two visible rows and ``total`` proves the COUNT was narrowed too (a
    # Python-side post-filter would leave the count at 3).
    assert result["total"] == 2


@pytest.mark.parametrize("term", [_DESC_TERM, _TAG_TERM], ids=["description", "tag"])
async def test_team_a_key_cannot_reach_team_b_by_description_or_tag(
    session: AsyncSession,
    term: str,
) -> None:
    """Neither the row nor any of its description/tag text reaches team A.

    The search term is taken from the team-B row's description or tags, which
    is exactly what the audit reported as exposed. Asserting on the serialised
    response (not just the ids) pins the confidentiality property itself: no
    name, description or tag of the foreign row can appear on any search.
    """
    seed = await _seed(session)

    with _mcp_auth(team_id=_TEAM_A), _library_read_context(session):
        result = await mcp_server.search_library(search=term)

    body = json.dumps(result)
    assert str(seed.team_b.id) not in body
    assert seed.team_b.slug not in body
    assert _DESC_TERM not in body
    assert _TAG_TERM not in body
    assert str(seed.team_b.id) not in _ids(result)


async def test_org_wide_caller_still_sees_all_three(
    session: AsyncSession,
) -> None:
    """No regression: an org-wide key / JWT (``_ctx_team_id=None``) is unscoped."""
    seed = await _seed(session)

    with _mcp_auth(team_id=None), _library_read_context(session):
        result = await mcp_server.search_library()

    ids = _ids(result)
    assert {str(row.id) for row in seed.all} <= ids
    assert result["total"] == 3 + len(_BUILTIN_PRIMITIVES)


async def test_org_wide_caller_name_search_sees_all_three(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    with _mcp_auth(team_id=None), _library_read_context(session):
        result = await mcp_server.search_library(search=_NAME_TERM)

    assert {str(row.id) for row in seed.all} <= _ids(result)
    assert result["total"] == 3


# ---------------------------------------------------------------------------
# The MCP resource (``modulo://library``) — the same read, the same boundary
# ---------------------------------------------------------------------------


async def test_library_resource_applies_the_same_team_boundary(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    with _mcp_auth(team_id=_TEAM_A), _library_read_context(session):
        body = await mcp_server.resource_library()

    assert str(seed.org.id) in body
    assert str(seed.team_a.id) in body
    assert str(seed.team_b.id) not in body
    assert _DESC_TERM not in body
    assert _TAG_TERM not in body


async def test_library_detail_resource_applies_the_same_team_boundary(
    session: AsyncSession,
) -> None:
    """``resource_library_detail`` must clamp by slug like the list resource.

    The detail read resolves via ``get_primitive_by_slug``, which filters only
    ``primitive_type`` + ``slug`` (RLS keys the team policy on the key owner's
    memberships, not the key's team), so without an explicit boundary a
    team-A key reads team B's team-private primitive by slug.
    """
    seed = await _seed(session)

    with _mcp_auth(team_id=_TEAM_A), _library_read_context(session):
        foreign = await mcp_server.resource_library_detail("schema", seed.team_b.slug)
        own = await mcp_server.resource_library_detail("schema", seed.team_a.slug)
        org = await mcp_server.resource_library_detail("schema", seed.org.slug)

    # The foreign team-B row is indistinguishable from a missing one.
    assert "not found" in foreign
    assert seed.team_b.name not in foreign
    assert str(seed.team_b.id) not in foreign
    assert _DESC_TERM not in foreign
    assert _TAG_TERM not in foreign
    # The caller's own team row and an org-level row remain readable.
    assert seed.team_a.name in own
    assert seed.org.name in org


async def test_library_detail_resource_org_wide_caller_sees_team_b(
    session: AsyncSession,
) -> None:
    """No regression: an org-wide credential (``_ctx_team_id=None``) is unscoped."""
    seed = await _seed(session)

    with _mcp_auth(team_id=None), _library_read_context(session):
        body = await mcp_server.resource_library_detail("schema", seed.team_b.slug)

    assert seed.team_b.name in body
    assert _DESC_TERM in body


# ---------------------------------------------------------------------------
# The lister itself — the predicate is bound, not a post-filter
# ---------------------------------------------------------------------------


async def _record_selects(session: AsyncSession, call: Any) -> list[str]:
    captured: list[str] = []
    real_execute = session.execute

    async def recording_execute(statement: object, *args: object, **kwargs: object) -> Any:
        if isinstance(statement, Select):
            captured.append(str(statement))
        return await real_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    session.execute = recording_execute  # type: ignore[method-assign]
    try:
        await call()
    finally:
        session.execute = real_execute  # type: ignore[method-assign]
    return captured


async def test_listing_binds_the_team_predicate_into_both_statements(
    session: AsyncSession,
) -> None:
    """COUNT and row page share one condition list, so both carry the boundary."""
    await _seed(session)

    statements = await _record_selects(
        session,
        lambda: list_library_primitives(session, org_id=_ORG_ID, team_id=_TEAM_A),
    )

    assert len(statements) == 2
    for sql in statements:
        # ``owner_team_id`` alone is not a discriminator (an entity SELECT
        # projects the column); the rendered ``IS NULL`` predicate is.
        assert "owner_team_id IS NULL" in sql
        assert "organisation_id = :" in sql


async def test_listing_without_a_team_is_unfiltered(
    session: AsyncSession,
) -> None:
    """``team_id=None`` (JWT / org-wide key) adds no predicate — no regression."""
    await _seed(session)

    statements = await _record_selects(
        session,
        lambda: list_library_primitives(session, org_id=_ORG_ID),
    )
    assert len(statements) == 2
    for sql in statements:
        assert "owner_team_id IS NULL" not in sql

    page = await list_library_primitives(session, org_id=_ORG_ID)
    assert page.total == 3


async def test_listing_with_a_team_returns_the_two_visible_rows(session: AsyncSession) -> None:
    seed = await _seed(session)

    page = await list_library_primitives(session, org_id=_ORG_ID, team_id=_TEAM_A)

    assert {row.id for row in page.items} == {seed.org.id, seed.team_a.id}
    assert page.total == 2


# ---------------------------------------------------------------------------
# The service seam — built-ins survive, the org page narrows
# ---------------------------------------------------------------------------


async def test_service_team_clamp_keeps_builtins_and_hides_the_foreign_row(
    session: AsyncSession,
) -> None:
    seed = await _seed(session)

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_primitives(session, _ORG_ID, team_id=_TEAM_A)

    ids = {item.id for item in result.items}
    assert {seed.org.id, seed.team_a.id} <= ids
    assert seed.team_b.id not in ids
    assert {p.id for p in _BUILTIN_PRIMITIVES} <= ids
    assert result.total == 2 + len(_BUILTIN_PRIMITIVES)


async def test_service_without_a_team_returns_every_row(session: AsyncSession) -> None:
    seed = await _seed(session)

    with patch(f"{_SERVICE}.set_rls_org", new=AsyncMock()):
        result = await list_primitives(session, _ORG_ID)

    ids = {item.id for item in result.items}
    assert {row.id for row in seed.all} <= ids
    assert result.total == 3 + len(_BUILTIN_PRIMITIVES)


# ---------------------------------------------------------------------------
# Threading: the handlers pass the request's team through
# ---------------------------------------------------------------------------


async def test_search_library_forwards_the_callers_team() -> None:
    with (
        _mcp_auth(team_id=_TEAM_A),
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session") as mock_session,
        patch.object(mcp_server, "list_primitives", AsyncMock(return_value=PageResult(items=[], total=0, page=1, page_size=20))) as mock_list,
    ):
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        await mcp_server.search_library()

    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["team_id"] == _TEAM_A


async def test_library_resource_forwards_the_callers_team() -> None:
    with (
        _mcp_auth(team_id=_TEAM_A),
        patch.object(mcp_server, "validate_current_auth", AsyncMock(return_value=True)),
        patch.object(mcp_server, "_session") as mock_session,
        patch.object(mcp_server, "list_primitives", AsyncMock(return_value=PageResult(items=[], total=0, page=1, page_size=50))) as mock_list,
    ):
        mock_session.return_value.__aenter__.return_value = AsyncMock()
        await mcp_server.resource_library()

    assert mock_list.await_args is not None
    assert mock_list.await_args.kwargs["team_id"] == _TEAM_A
