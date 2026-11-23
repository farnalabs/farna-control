"""Org-scoped CRUD for LibraryPrimitive.

Two isolation postures live in this module (FAR-2696):

* **Clamped helpers** apply an explicit ``organisation_id`` predicate, so they
  stay isolated on a connection whose role bypasses RLS (``BYPASSRLS``) or on a
  database whose policy is missing. ``mark_update_available`` (FAR-2696) is one:
  its REQUIRED org rides on the UPDATE statement itself, not only on a
  preceding SELECT.
* **Point helpers** — ``get_library_primitive``, ``update_library_primitive``,
  ``soft_delete_library_primitive``, ``restore_library_primitive`` and
  ``copy_to_adapt`` — are keyed by a primitive id the caller has already
  resolved through an org-scoped path (an RLS-bound route transaction, or the
  clamped ``core.library_service.get_primitive(..., explicit_org_filter=...)``
  resolver). PostgreSQL row-level security is the INTENDED boundary for them:
  the ``rls_org_isolation`` policy on ``library_primitives`` restricts every
  row to ``organisation_id = current_setting('app.organisation_id')``.

This is a recorded decision (FAR-2696), not an oversight. The point helpers
are reached only from ``api.routes.library`` and ``core.library_service``; the
install / grant / upgrade / uninstall paths resolve through the clamped
``get_primitive`` / ``get_primitive_by_slug`` instead, never through these
helpers. The residual risk of the decision is a ``BYPASSRLS`` session (an
integration-test superuser or a misconfigured deployment) or a missing/disabled
policy: a caller that must stay isolated under that posture passes the org to
the clamped resolver, or uses a clamped helper such as
``mark_update_available`` for a write. Threading a required org through the
point helpers was rejected because it would change a security-critical
signature used by the library read, write, delete, restore and copy routes
while the id handed in is already resolved in-org before it arrives; the
integrity-critical fan-out write that could not rely on that property was
given its own clamped helper instead.
"""

import logging
import uuid
from typing import Any, Required, TypedDict, Unpack

from sqlalchemy import false, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from farna.db.crud.base import PageResult, apply_updates
from farna.db.crud.pagination import CursorPaginator
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.soft_delete import include_soft_deleted

logger = logging.getLogger(__name__)


async def copy_to_adapt(
    session: AsyncSession,
    *,
    primitive_id: uuid.UUID,
    target_org_id: uuid.UUID,
    target_team_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
) -> LibraryPrimitive | None:
    """Copy a primitive into ``target_org_id`` as a fork (point read; see module docstring).

    The source read is the RLS-bound point read: ``trg_library_primitives_forked_from_tenant``
    requires a fork's ``forked_from`` to reference a primitive in the SAME
    organisation as the new row, so the source is in ``target_org_id`` by
    construction. No production caller — the service-level
    ``core.library_service.copy_to_adapt`` is the one the route uses; this
    remains for the pre-service test coverage.
    """
    source = await get_library_primitive(session, primitive_id)
    if source is None:
        return None

    version_parts = source.version.split(".")
    try:
        minor = int(version_parts[-1]) + 1
    except (ValueError, IndexError):
        minor = 1
    version_parts[-1] = str(minor)
    new_version = ".".join(version_parts) if len(version_parts) > 1 else f"{minor}.0"

    copied = LibraryPrimitive(
        organisation_id=target_org_id,
        source="local",
        primitive_type=source.primitive_type,
        name=source.name,
        slug=f"{source.slug}-copy",
        description=source.description,
        author=source.author,
        version=new_version,
        tags=list(source.tags) if source.tags else [],
        content_json=dict(source.content_json),
        source_url=None,
        forked_from=primitive_id,
        checksum=None,
        ed25519_signature=None,
        verified=None,
        download_count=None,
        average_rating=None,
        review_count=None,
        owner_team_id=target_team_id,
        visibility="org",
        account_id=account_id,
        tier=source.tier,
    )
    session.add(copied)
    await session.flush()

    result = await session.execute(select(LibraryPrimitive).where(LibraryPrimitive.id == copied.id))
    return result.scalar_one_or_none()


async def get_library_primitive(
    session: AsyncSession,
    primitive_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> LibraryPrimitive | None:
    """Read one primitive by id (point helper — RLS is the boundary; see module docstring).

    The id must have been resolved through an org-scoped path before it reaches
    here. Use ``core.library_service.get_primitive(..., explicit_org_filter=org_id)``
    instead when the caller must stay isolated on a ``BYPASSRLS`` session.
    """
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id)
    stmt = include_soft_deleted(stmt) if include_deleted else stmt.where(LibraryPrimitive.deleted_at.is_(None))
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_list_conditions(
    *,
    org_id: uuid.UUID,
    primitive_type: str | None,
    primitive_types: list[str] | None,
    search: str | None,
    source: str | None,
    excluded_tiers: list[str] | None,
) -> list[ColumnElement[bool]]:
    """Build the WHERE predicates shared by the cursor and offset list paths.

    Both branches narrow with exactly this chain, so the COUNT, the offset row
    page and the keyset page all describe the same universe.

    The returned list always holds at least the soft-delete predicate, so
    callers may apply ``.where(*conditions)`` unconditionally.

    FAR-2692: the ``organisation_id`` predicate is unconditional and the
    argument is required. An optional org argument let a caller silently fall
    back to an RLS-only read, which a ``BYPASSRLS`` (or misconfigured-RLS)
    connection turns into a cross-tenant list. This clamp cannot hide the
    Native library built-ins: ``source='modulo'`` primitives are merged from the
    in-code registry in ``core.library_service.list_primitives`` and are not
    rows in the caller's slice, and the ``library_primitives`` RLS policy
    already restricts rows to ``organisation_id = current_setting(...)`` — so
    the clamp reproduces the visibility RLS already enforces.
    """
    tiers = ["in_dev"] if excluded_tiers is None else excluded_tiers

    conditions: list[ColumnElement[bool]] = [
        LibraryPrimitive.deleted_at.is_(None),
        LibraryPrimitive.organisation_id == org_id,
    ]

    if primitive_types:
        conditions.append(LibraryPrimitive.primitive_type.in_(primitive_types))
    elif primitive_type is not None:
        conditions.append(LibraryPrimitive.primitive_type == primitive_type)

    if search is not None and search.strip():
        term = f"%{search.strip()}%"
        conditions.append(LibraryPrimitive.name.ilike(term))

    if source == "community":
        # Community-sourced primitives are retired (FAR-2592): the community
        # library surface was removed and migration 0356 soft-deleted every
        # live row. The parameter stays accepted for compatibility but resolves
        # to no rows, including any legacy row still live in a not-yet-migrated
        # database. A literal false predicate keeps the COUNT, the row page and
        # the cursor path on the same (empty) universe.
        conditions.append(false())
    elif source is None:
        # Excluding the retired source from the merged listing keeps a legacy
        # row (or one written by a stale process) from surfacing as an
        # unlabelled native item. Applied in SQL, not as a Python post-filter,
        # so ``total`` stays exact.
        conditions.append(LibraryPrimitive.source != "community")
    else:
        # Pushed into the shared ``conditions`` so the COUNT, the row page and
        # the cursor path all narrow to the same source-filtered universe — a
        # Python-side post-filter cannot make ``total`` exact.
        conditions.append(LibraryPrimitive.source == source)

    if tiers:
        conditions.append(~LibraryPrimitive.tier.in_(tiers))

    return conditions


async def _page_by_cursor(
    session: AsyncSession,
    *,
    conditions: list[ColumnElement[bool]],
    cursor: str,
    page: int,
    page_size: int,
) -> PageResult[LibraryPrimitive]:
    paginator = CursorPaginator()
    stmt = select(LibraryPrimitive).where(*conditions)
    cp = await paginator.paginate(
        session,
        stmt,
        cursor=cursor,
        limit=page_size,
        model=LibraryPrimitive,
        compute_total=True,
    )
    return PageResult(
        items=cp.items,
        total=cp.total or 0,
        page=page,
        page_size=page_size,
        next_cursor=cp.next_cursor,
        has_more=cp.has_more,
    )


async def _page_by_offset(
    session: AsyncSession,
    *,
    conditions: list[ColumnElement[bool]],
    page: int,
    page_size: int,
) -> PageResult[LibraryPrimitive]:
    try:
        count_stmt = select(func.count()).select_from(LibraryPrimitive).where(*conditions)
        total = (await session.execute(count_stmt)).scalar_one()
    except SQLAlchemyError:
        logger.exception("count query failed")
        raise

    offset = (page - 1) * page_size
    try:
        items_stmt = select(LibraryPrimitive).where(*conditions).order_by(LibraryPrimitive.created_at.desc()).offset(offset).limit(page_size)
        items = list((await session.execute(items_stmt)).scalars())
    except SQLAlchemyError:
        logger.exception("items query failed")
        raise

    has_more = (offset + len(items)) < total
    return PageResult(items=items, total=total, page=page, page_size=page_size, has_more=has_more)


async def list_library_primitives(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    page: int = 1,
    page_size: int = 20,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
    source: str | None = None,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[LibraryPrimitive]:
    """List the caller's library primitives.

    ``org_id`` is REQUIRED keyword-only (FAR-2692): it is applied as an
    explicit ``organisation_id`` predicate on the shared condition list, so the
    COUNT, the offset page and the cursor page are all clamped. A caller can no
    longer fall back to the RLS-only read that a ``BYPASSRLS`` connection
    exposes. Built-ins are unaffected — see ``_build_list_conditions``.
    """

    conditions = _build_list_conditions(
        org_id=org_id,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        source=source,
        excluded_tiers=excluded_tiers,
    )

    if cursor is not None:
        return await _page_by_cursor(
            session,
            conditions=conditions,
            cursor=cursor,
            page=page,
            page_size=page_size,
        )

    return await _page_by_offset(session, conditions=conditions, page=page, page_size=page_size)


class CreateLibraryPrimitiveOptions(TypedDict, total=False):
    """Keyword-only options accepted by :func:`create_library_primitive` (python:S107).

    Grouped behind ``**options: Unpack[CreateLibraryPrimitiveOptions]`` so the
    function stays inside the S107 parameter budget without changing a single
    caller. The ten ``Required`` keys were keyword parameters WITHOUT defaults
    before the grouping, so omitting one must still fail loudly (``TypeError``,
    exactly like a missing keyword parameter) instead of persisting a silent
    default — ``visibility`` in particular must never default itself. The two
    remaining keys keep their original defaults; the body applies them via
    ``options.get(name, default)``.
    """

    source_url: Required[str | None]
    forked_from: Required[uuid.UUID | None]
    checksum: Required[str | None]
    ed25519_signature: Required[str | None]
    verified: Required[bool | None]
    download_count: Required[int | None]
    average_rating: Required[float | None]
    review_count: Required[int | None]
    owner_team_id: Required[uuid.UUID | None]
    visibility: Required[str]
    auto_update: bool
    tier: str


async def create_library_primitive(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    source: str,
    primitive_type: str,
    name: str,
    slug: str,
    description: str | None,
    author: str,
    version: str,
    tags: list[str],
    content_json: dict[str, Any],
    account_id: uuid.UUID | None,
    **options: Unpack[CreateLibraryPrimitiveOptions],
) -> LibraryPrimitive:
    """Create a library primitive; required metadata rides on the option group."""
    try:
        source_url = options["source_url"]
        forked_from = options["forked_from"]
        checksum = options["checksum"]
        ed25519_signature = options["ed25519_signature"]
        verified = options["verified"]
        download_count = options["download_count"]
        average_rating = options["average_rating"]
        review_count = options["review_count"]
        owner_team_id = options["owner_team_id"]
        visibility = options["visibility"]
    except KeyError as exc:
        raise TypeError(f"create_library_primitive() missing required keyword argument: {exc.args[0]!r}") from None
    auto_update = options.get("auto_update", True)
    tier = options.get("tier", "native")
    unknown_options = set(options) - set(CreateLibraryPrimitiveOptions.__annotations__)
    if unknown_options:
        raise TypeError(f"unexpected create_library_primitive options: {sorted(unknown_options)}")

    primitive = LibraryPrimitive(
        organisation_id=org_id,
        source=source,
        primitive_type=primitive_type,
        name=name,
        slug=slug,
        description=description,
        author=author,
        version=version,
        tags=tags,
        content_json=content_json,
        source_url=source_url,
        forked_from=forked_from,
        checksum=checksum,
        ed25519_signature=ed25519_signature,
        verified=verified,
        download_count=download_count,
        average_rating=average_rating,
        review_count=review_count,
        owner_team_id=owner_team_id,
        visibility=visibility,
        account_id=account_id,
        auto_update=auto_update,
        tier=tier,
    )
    session.add(primitive)
    await session.flush()
    return primitive


async def update_library_primitive(
    session: AsyncSession,
    primitive_id: uuid.UUID,
    updates: dict[str, Any],
) -> LibraryPrimitive | None:
    """Apply an update to one primitive by id (point helper — RLS is the boundary).

    See the module docstring. The read-modify-write is keyed by an id already
    resolved in-org; ``mark_update_available`` is the clamped single-statement
    variant for the integrity-critical update fan-out.
    """
    primitive = await get_library_primitive(session, primitive_id)
    if primitive is None:
        return None
    apply_updates(primitive, updates)
    await session.flush()
    return primitive


async def mark_update_available(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    recipient_id: uuid.UUID,
    version_id: uuid.UUID,
) -> LibraryPrimitive | None:
    """Stamp ``update_available_version_id`` on a recipient IN ``org_id``.

    FAR-2696: the update fan-out (``core.library_service._mark_update_recipients``)
    used to stamp through the id-only ``update_library_primitive``, so a
    ``BYPASSRLS`` session could stamp a row in another organisation. This
    variant carries a REQUIRED org and applies it as an ``organisation_id``
    predicate on the UPDATE itself — the write, not merely the preceding
    SELECT, is clamped. Returns the stamped row, or ``None`` when no live row
    in ``org_id`` matches ``recipient_id`` (foreign and soft-deleted rows are
    skipped, exactly as the point helper's ``deleted_at`` filter skipped them).
    """
    result = await session.execute(
        update(LibraryPrimitive)
        .where(
            LibraryPrimitive.id == recipient_id,
            LibraryPrimitive.organisation_id == org_id,
            LibraryPrimitive.deleted_at.is_(None),
        )
        .values(update_available_version_id=version_id)
        .returning(LibraryPrimitive)
    )
    await session.flush()
    return result.scalar_one_or_none()


async def list_primitives_by_version_group(
    session: AsyncSession,
    version_group_id: uuid.UUID,
    *,
    org_id: uuid.UUID,
) -> list[LibraryPrimitive]:
    """List every version in a version group, clamped to the caller's org.

    FAR-2692: the parent ``version_group_id`` alone is not an org boundary —
    carry an explicit ``organisation_id`` predicate so a ``BYPASSRLS`` (or
    misconfigured-RLS) session cannot read another org's versions for a group
    id it guessed. ``org_id`` is REQUIRED so a future caller cannot fall back
    to the parent-id-only shape (same method as ``list_ratings_for_primitive``,
    FAR-2686).
    """
    stmt = select(LibraryPrimitive).where(LibraryPrimitive.version_group_id == version_group_id, LibraryPrimitive.organisation_id == org_id).order_by(LibraryPrimitive.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars())


async def soft_delete_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    """Soft-delete one primitive by id (point helper — RLS is the boundary).

    See the module docstring. The id is resolved in-org on the route before it
    arrives here.
    """
    result = await session.execute(update(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id, LibraryPrimitive.deleted_at.is_(None)).values(deleted_at=func.now()).returning(LibraryPrimitive))
    await session.flush()
    return result.scalar_one_or_none()


async def restore_library_primitive(session: AsyncSession, primitive_id: uuid.UUID) -> LibraryPrimitive | None:
    """Restore one soft-deleted primitive by id (point helper — RLS is the boundary).

    See the module docstring.
    """
    result = await session.execute(update(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id, LibraryPrimitive.deleted_at.is_not(None)).values(deleted_at=None).returning(LibraryPrimitive))
    await session.flush()
    return result.scalar_one_or_none()
