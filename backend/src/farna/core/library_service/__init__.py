"""Library service — CRUD for local library primitives and fixture contributions."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from farna.core.library_service._seed_data import (
    _BUILTIN_BY_ID,
    _BUILTIN_BY_SLUG,
    _BUILTIN_PRIMITIVES,
    BUILTIN_ORG_ID,
)
from farna.db.crud.base import PageResult
from farna.db.crud.library_primitive import (
    create_library_primitive,
    get_library_primitive,
    list_library_primitives,
    list_primitives_by_version_group,
    mark_update_available,
    update_library_primitive,
)
from farna.db.models.collection_install import CollectionInstall
from farna.db.models.library_primitive import LibraryPrimitive
from farna.db.rls import set_rls_org, set_rls_user_context
from farna.util import sanitise_log_value as _sanitise_log_value

logger = logging.getLogger(__name__)

# Repeated log-name contract (S1192).
_LOG_COMPONENT = "core.library_service"


__all__ = [
    "BUILTIN_ORG_ID",
    "CONTRIBUTION_DRAFT",
    "CONTRIBUTION_PUBLISHED",
    "CONTRIBUTION_REVIEW_QUEUE",
    "_BUILTIN_BY_ID",
    "_BUILTIN_BY_SLUG",
    "_BUILTIN_PRIMITIVES",
    "BuiltinPrimitiveReadOnlyError",
    "ContributionInvalidTransitionError",
    "ContributionNotFoundError",
    "contribute_fixture",
    "copy_to_adapt",
    "list_contribution_versions",
    "list_contributions",
    "list_org_contributions",
    "list_primitives",
    "notify_importers_of_update",
    "submit_contribution_for_review",
    "submit_contribution_version",
]


class BuiltinPrimitiveReadOnlyError(Exception):
    """Raised when a built-in primitive is adapted via MCP — browser UI only."""


class ContributionNotFoundError(LookupError):
    """Raised when a contribution primitive is not found."""


class ContributionInvalidTransitionError(ValueError):
    """Raised when an invalid contribution status transition is attempted."""


# Contribution status constants (kept for fixture contribution flow)
CONTRIBUTION_DRAFT = "draft"
CONTRIBUTION_REVIEW_QUEUE = "review_queue"
CONTRIBUTION_PUBLISHED = "published"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bump_version(version: str) -> str:
    """Increment the last segment of a version string."""
    if not version:
        logger.warning("_bump_version called with empty string, defaulting to 1.0")
        return "1.0"
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        logger.warning("_bump_version: non-numeric last segment in '%s', defaulting to 1.0", version)
        parts = ["1", "0"]
    return ".".join(parts)


def _parse_version_key(version: str | None) -> tuple[int, ...]:
    """Parse a dotted version string into a sortable tuple of ints."""
    if not version:
        return (0,)
    try:
        return tuple(int(x) for x in version.split("."))
    except ValueError:
        return (0,)


async def _with_org_txn[T](
    session: AsyncSession,
    org_id: uuid.UUID,
    fn: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run a write body inside a single RLS-scoped transaction.

    Reproduces the shared error contract of the contribution functions: a
    ProgrammingError (missing table/migration) is logged and re-raised.
    """
    try:
        return await _scoped_execute(session, org_id, fn)
    except ProgrammingError:
        logger.exception(_LOG_COMPONENT)
        raise


# ---------------------------------------------------------------------------
# Public API — lookup
# ---------------------------------------------------------------------------


async def _scoped_execute[T](
    session: AsyncSession,
    org_id: uuid.UUID,
    query: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run a read query inside an RLS-scoped transaction.

    Reuses the caller's transaction when already active (``set_rls_org`` sets a
    ``SET LOCAL`` context bound to the current transaction), otherwise opens one.
    """
    if session.in_transaction():
        await set_rls_org(session, org_id)
        return await query(session)
    async with session.begin():
        await set_rls_org(session, org_id)
        return await query(session)


async def get_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    explicit_org_filter: uuid.UUID | None = None,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id, or None.

    Resolver fallback ordering (ADR 032 §4 visibility — DB → registry):
    the org-scoped ``library_primitives`` DB row wins, then the in-code
    built-in registry. The in-code source is visible to EVERY organisation and
    carries no DB row in the org's RLS slice, so callers must not assume a
    returned primitive is DB-persisted.

    Supports being called within an existing transaction or starting its own.

    RLS-bypass caveat: the positional ``org_id`` only sets the RLS context.
    On a connection whose role bypasses RLS (integration-test superusers,
    BYPASSRLS system sessions) the context alone constrains nothing, so pass
    ``explicit_org_filter`` to add a hard ``WHERE organisation_id =`` clamp to
    the DB query. On RLS-enforcing connections the clamp is redundant but
    harmless; the in-code registry fallbacks are unaffected by either.
    """

    async def _lookup(s: AsyncSession) -> LibraryPrimitive | None:
        stmt = select(LibraryPrimitive).where(LibraryPrimitive.id == primitive_id)
        if explicit_org_filter is not None:
            stmt = stmt.where(LibraryPrimitive.organisation_id == explicit_org_filter)
        result = await s.execute(stmt)
        return result.scalar_one_or_none()

    try:
        # Default path: reuse get_library_primitive (RLS-scoped). Only use the
        # inline lookup with an explicit org clamp when one is supplied —
        # needed on connections whose role bypasses RLS.
        if explicit_org_filter is None:
            item = await _scoped_execute(session, org_id, lambda s: get_library_primitive(s, primitive_id))
        else:
            item = await _scoped_execute(session, org_id, _lookup)
    except ProgrammingError:
        logger.warning("get_primitive — DB not migrated or table missing for %s", primitive_id)
        return None
    except SQLAlchemyError:
        logger.exception("get_primitive — DB error for %s", primitive_id)
        raise
    if item is not None:
        return item
    return _BUILTIN_BY_ID.get(primitive_id)


async def get_primitive_by_slug(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_type: str,
    slug: str,
    *,
    explicit_org_filter: uuid.UUID | None = None,
    version: str | None = None,
) -> LibraryPrimitive | None:
    """Return a primitive visible to org_id by type and slug, or None.

    Resolver fallback ordering (ADR 032 §4 visibility — DB → registry):
    the org-scoped ``library_primitives`` DB row wins, then the in-code
    modulo registry. The in-code source is visible to EVERY organisation and
    carries no DB row in the org's RLS slice, so callers must not assume a
    returned primitive is DB-persisted.

    A slug may have MULTIPLE version rows in one organisation (FAR-759
    collection versioning). When ``version`` is given the lookup is exact;
    without it the NEWEST visible row wins (deterministic, never raises on
    multiples).

    Supports being called within an existing transaction or starting its own.

    RLS-bypass caveat: the positional ``org_id`` only sets the RLS context.
    On a connection whose role bypasses RLS (integration-test superusers,
    BYPASSRLS system sessions) the context alone constrains nothing, so pass
    ``explicit_org_filter`` to add a hard ``WHERE organisation_id =`` clamp to
    the DB query. On RLS-enforcing connections the clamp is redundant but
    harmless; the in-code registry fallbacks are unaffected by either.
    """

    async def _lookup(s: AsyncSession) -> LibraryPrimitive | None:
        stmt = select(LibraryPrimitive).where(
            LibraryPrimitive.primitive_type == primitive_type,
            LibraryPrimitive.slug == slug,
        )
        if explicit_org_filter is not None:
            stmt = stmt.where(LibraryPrimitive.organisation_id == explicit_org_filter)
        if version is not None:
            stmt = stmt.where(LibraryPrimitive.version == version)
        # Deterministic pick when several versions of the slug coexist:
        # newest row first (created_at desc), limit 1.
        stmt = stmt.order_by(LibraryPrimitive.created_at.desc()).limit(1)
        result = await s.execute(stmt)
        return result.scalars().first()

    try:
        item = await _scoped_execute(session, org_id, _lookup)
    except ProgrammingError:
        logger.warning(
            "get_primitive_by_slug — DB not migrated for %s/%s",
            _sanitise_log_value(primitive_type),
            _sanitise_log_value(slug),
        )
        return None
    except SQLAlchemyError:
        logger.exception(
            "get_primitive_by_slug — DB error for %s/%s",
            _sanitise_log_value(primitive_type),
            _sanitise_log_value(slug),
        )
        raise
    if item is not None:
        return item
    builtin = _BUILTIN_BY_SLUG.get((primitive_type, slug))
    if builtin is not None and version is not None and builtin.version != version:
        return None
    return builtin


# ---------------------------------------------------------------------------
# Public API — copy to adapt
# ---------------------------------------------------------------------------


async def _re_read_primitive(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> tuple[LibraryPrimitive, bool]:
    """Re-read the source inside the copy transaction to avoid TOCTOU.

    Returns ``(primitive, in_db)`` — ``in_db`` is False when the source came
    from the in-memory modulo registry (it has no ``library_primitives`` row,
    so ``forked_from`` cannot reference it: the FK would fail).
    """
    refreshed = await get_library_primitive(session, primitive_id)
    if refreshed is not None:
        return refreshed, True
    builtin = _BUILTIN_BY_ID.get(primitive_id)
    if builtin is None:
        raise LookupError(f"Primitive {primitive_id} not found for org {org_id} during copy")
    return builtin, False


async def _increment_registry_downloads(session: AsyncSession, refreshed: LibraryPrimitive) -> None:
    """Atomically increment the download count on registry primitives."""
    if refreshed.source == "registry":
        await session.execute(sa_update(LibraryPrimitive).where(LibraryPrimitive.id == refreshed.id).values(download_count=func.coalesce(LibraryPrimitive.download_count, 0) + 1))


def _build_copy_args(
    refreshed: LibraryPrimitive,
    org_id: uuid.UUID,
    new_version: str,
    target_team_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    *,
    forked_from: uuid.UUID | None,
) -> dict[str, Any]:
    """Build the create_library_primitive kwargs for an adapted org copy."""
    return {
        "org_id": org_id,
        "source": "local",
        "primitive_type": refreshed.primitive_type,
        "name": refreshed.name,
        "slug": f"{refreshed.slug}-copy",
        "description": refreshed.description,
        "author": refreshed.author,
        "version": new_version,
        "tags": list(refreshed.tags or []),
        "content_json": dict(refreshed.content_json) if refreshed.content_json is not None else {},
        "source_url": None,
        "forked_from": forked_from,
        "checksum": None,
        "ed25519_signature": None,
        "verified": None,
        "download_count": None,
        "average_rating": None,
        "review_count": None,
        "owner_team_id": target_team_id,
        "visibility": "org",
        "account_id": created_by,
        "auto_update": True,
        "tier": refreshed.tier,
    }


async def copy_to_adapt(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    target_team_id: uuid.UUID | None = None,
    created_by: uuid.UUID | None = None,
    org_role: str = "admin",
    via_mcp: bool = False,
) -> LibraryPrimitive:
    """Clone a primitive into the org workspace.

    Raises BuiltinPrimitiveReadOnlyError if via_mcp=True and the source is a built-in primitive.
    Raises LookupError if the primitive does not exist.
    """
    source = await get_primitive(session, org_id, primitive_id)
    if source is None:
        raise LookupError(f"Primitive {primitive_id} not found for org {org_id}")

    if via_mcp and source.visibility == "community":
        raise BuiltinPrimitiveReadOnlyError("Built-in primitives may only be adapted via the browser UI, not via MCP.")

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        if created_by is not None:
            await set_rls_user_context(s, created_by, org_role)
        refreshed, in_db = await _re_read_primitive(s, org_id, primitive_id)
        new_version = _bump_version(refreshed.version)
        await _increment_registry_downloads(s, refreshed)
        return await create_library_primitive(
            s,
            **_build_copy_args(
                refreshed,
                org_id,
                new_version,
                target_team_id,
                created_by,
                # ``forked_from`` has an FK on library_primitives — in-memory
                # builtin sources (modulo registry) have no row, so the
                # copy must not reference them or the insert fails with an FK
                # violation (internal_error 500 on copy_library_primitive).
                forked_from=refreshed.id if in_db else None,
            ),
        )

    return await _with_org_txn(session, org_id, _do)


# ---------------------------------------------------------------------------
# Contribution flow (fixture contributions only)
# ---------------------------------------------------------------------------


async def _create_draft_contribution(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    primitive_type: str,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    content_json: dict[str, Any],
    source_url: str | None,
    owner_team_id: uuid.UUID | None,
) -> LibraryPrimitive:
    """Create a primitive and immediately set it to draft contribution status."""

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        prim = await create_library_primitive(
            s,
            org_id=org_id,
            source="local",
            primitive_type=primitive_type,
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version="1.0",
            tags=tags,
            content_json=content_json,
            source_url=source_url,
            forked_from=None,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=owner_team_id,
            visibility="org",
            account_id=created_by,
        )
        updated = await update_library_primitive(s, prim.id, {"contribution_status": CONTRIBUTION_DRAFT})
        if updated is None:
            raise ContributionNotFoundError(f"Contribution {prim.id} not found after creation")
        return updated

    return await _with_org_txn(session, org_id, _do)


async def contribute_fixture(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Create a draft fixture contribution in the org's library.

    The fixture is stored as a test_fixture primitive with contribution_status='draft'.
    It is visible only to the submitting org.
    """
    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    return await _create_draft_contribution(
        session,
        org_id=org_id,
        created_by=created_by,
        primitive_type="test_fixture",
        name=name,
        slug=slug,
        description=description,
        tags=tags,
        content_json=content,
        source_url=None,
        owner_team_id=owner_team_id,
    )


async def submit_contribution_for_review(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    _created_by: uuid.UUID,
) -> LibraryPrimitive:
    """Move a draft fixture contribution to the review queue.

    Raises ContributionNotFoundError if the primitive does not exist.
    Raises ContributionInvalidTransitionError if the primitive is not in draft status.
    """

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        prim = await get_library_primitive(s, primitive_id)

        if prim is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if prim.contribution_status != CONTRIBUTION_DRAFT:
            raise ContributionInvalidTransitionError(f"Cannot submit contribution {primitive_id} for review: expected status '{CONTRIBUTION_DRAFT}', got '{prim.contribution_status}'")

        updated = await update_library_primitive(
            s,
            primitive_id,
            {"contribution_status": CONTRIBUTION_REVIEW_QUEUE},
        )
        if updated is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")
        return updated

    return await _with_org_txn(session, org_id, _do)


def _apply_contribution_status_filter(
    result: PageResult[LibraryPrimitive],
    contribution_status: str | None,
) -> PageResult[LibraryPrimitive]:
    if contribution_status is not None:
        result.items = [p for p in result.items if p.contribution_status == contribution_status]
        result.total = len(result.items)
    return result


async def list_contributions(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contribution_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[LibraryPrimitive]:
    """List fixture contributions scoped to the org."""
    result = await _with_org_txn(
        session,
        org_id,
        lambda s: list_library_primitives(s, org_id=org_id, page=page, page_size=page_size, primitive_type="test_fixture"),
    )
    return _apply_contribution_status_filter(result, contribution_status)


async def list_org_contributions(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    contribution_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> PageResult[LibraryPrimitive]:
    """List contributions submitted by the org, optionally filtered by status."""
    result = await _with_org_txn(
        session,
        org_id,
        lambda s: list_library_primitives(s, org_id=org_id, page=page, page_size=page_size),
    )
    return _apply_contribution_status_filter(result, contribution_status)


# ---------------------------------------------------------------------------
# Contribution versioning
# ---------------------------------------------------------------------------


async def _resolve_version_group(
    s: AsyncSession,
    existing: LibraryPrimitive,
    primitive_id: uuid.UUID,
) -> uuid.UUID:
    """Return the primitive's version group, seeding it on the seed row if absent."""
    group_id = existing.version_group_id or existing.id
    if existing.version_group_id is None:
        seed_update = await update_library_primitive(
            s,
            primitive_id,
            {"version_group_id": group_id},
        )
        if seed_update is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found for version group seeding")
    return group_id


async def submit_contribution_version(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
    *,
    created_by: uuid.UUID,
    name: str,
    slug: str,
    description: str | None,
    tags: list[str],
    fixture_map: dict[str, str],
    source_run_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    owner_team_id: uuid.UUID | None = None,
) -> LibraryPrimitive:
    """Submit a new version of an existing published fixture contribution.

    Auto-increments the minor version and creates a new draft row linked
    via version_group_id.  The new version must go through
    review_queue -> published independently.
    """
    content: dict[str, Any] = {
        "fixture_map": fixture_map,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_pipeline_id": str(source_pipeline_id) if source_pipeline_id else None,
    }

    async def _do(s: AsyncSession) -> LibraryPrimitive:
        existing = await get_library_primitive(s, primitive_id)

        if existing is None:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if existing.contribution_status != CONTRIBUTION_PUBLISHED:
            raise ContributionInvalidTransitionError(f"Cannot version contribution {primitive_id}: expected status '{CONTRIBUTION_PUBLISHED}', got '{existing.contribution_status}'")

        new_version = _bump_version(existing.version)
        group_id = await _resolve_version_group(s, existing, primitive_id)

        prim = await create_library_primitive(
            s,
            org_id=org_id,
            source="local",
            primitive_type="test_fixture",
            name=name,
            slug=slug,
            description=description,
            author=created_by.hex,
            version=new_version,
            tags=tags,
            content_json=content,
            source_url=None,
            forked_from=primitive_id,
            checksum=None,
            ed25519_signature=None,
            verified=None,
            download_count=None,
            average_rating=None,
            review_count=None,
            owner_team_id=owner_team_id,
            visibility="org",
            account_id=created_by,
        )
        updated = await update_library_primitive(
            s,
            prim.id,
            {
                "contribution_status": CONTRIBUTION_DRAFT,
                "version_group_id": group_id,
            },
        )
        if updated is None:
            raise ContributionNotFoundError(f"Contribution version {prim.id} not found after creation")
        return updated

    return await _with_org_txn(session, org_id, _do)


async def list_contribution_versions(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> list[LibraryPrimitive]:
    """Return all versions for a contribution primitive, newest first."""

    async def _do(s: AsyncSession) -> tuple[list[LibraryPrimitive], LibraryPrimitive]:
        # FAR-2696: resolve the seed through the clamped resolver rather than
        # the id-only CRUD read, so a guessed (foreign-org) contribution id
        # cannot cross orgs on a BYPASSRLS connection.
        prim = await get_primitive(s, org_id, primitive_id, explicit_org_filter=org_id)

        # ``get_primitive`` falls back to the in-memory built-in registry when
        # the org-clamped DB lookup misses. Built-ins are served from memory,
        # never from SQL, and are not contributions — they have no versions.
        # Preserve the pre-existing 404 for a built-in id instead of serving it
        # as a one-row version list.
        if prim is None or _BUILTIN_BY_ID.get(primitive_id) is prim:
            raise ContributionNotFoundError(f"Contribution {primitive_id} not found")

        if prim.version_group_id is None:
            return [prim], prim

        results = await list_primitives_by_version_group(s, prim.version_group_id, org_id=org_id)
        # Include the seed primitive (the one whose version_group_id was set to
        # its own id) — it won't appear in the version-group query because it
        # may not yet have the version_group_id set if it predates the feature.
        return results, prim

    results, prim = await _with_org_txn(session, org_id, _do)

    if not any(r.id == prim.id for r in results):
        results.append(prim)

    return sorted(results, key=lambda p: _parse_version_key(p.version), reverse=True)


async def _mark_update_recipients(s: AsyncSession, org_id: uuid.UUID, prim: LibraryPrimitive) -> None:
    """Back-propagate the new version id to every auto-update recipient.

    Two recipient classes (FAR-759): fork copies (``forked_from`` pointing at
    any version in this version group) and INSTALLED collection rows
    (``collection_install.collection_id`` referencing a version in the group)
    — the latter is how an installed collection surfaces "update available".
    Both are gated on the recipient row's ``auto_update`` flag; the stamp is
    a notification only, applying it remains an operator-initiated upgrade.

    FAR-2696: every SELECT here is clamped to ``org_id`` — the outer recipient
    predicate, the correlated version-group subquery and the
    ``collection_install`` subquery — and the stamp goes through the
    org-clamped ``mark_update_available`` write. Before this, all three reads
    and the write were org-free, so a ``BYPASSRLS`` connection stamped
    another organisation's forks. The version-group id is not an org boundary
    on its own; the org predicate is.
    """
    group_id = prim.version_group_id
    if group_id is None:
        return

    group_member_ids = select(LibraryPrimitive.id).where(
        LibraryPrimitive.version_group_id == group_id,
        LibraryPrimitive.organisation_id == org_id,
    )
    fork_stmt = select(LibraryPrimitive).where(
        LibraryPrimitive.forked_from.in_(group_member_ids),
        LibraryPrimitive.organisation_id == org_id,
    )
    installed_stmt = select(LibraryPrimitive).where(
        LibraryPrimitive.id.in_(
            select(CollectionInstall.collection_id).where(CollectionInstall.organisation_id == org_id),
        ),
        or_(
            LibraryPrimitive.version_group_id == group_id,
            LibraryPrimitive.id == group_id,
        ),
        LibraryPrimitive.organisation_id == org_id,
    )

    recipients: list[LibraryPrimitive] = []
    recipients.extend((await s.execute(fork_stmt)).scalars())
    recipients.extend((await s.execute(installed_stmt)).scalars())

    for copy in recipients:
        if not copy.auto_update:
            continue
        try:
            await mark_update_available(
                s,
                org_id=org_id,
                recipient_id=copy.id,
                version_id=prim.id,
            )
        except SQLAlchemyError:
            logger.exception("notify_importers_of_update: failed to update copy %s", copy.id)


async def notify_importers_of_update(
    session: AsyncSession,
    org_id: uuid.UUID,
    primitive_id: uuid.UUID,
) -> None:
    """Mark library entries that forked from this primitive as having an update.

    Finds all primitives that were copied (``forked_from``) from any version
    in the same version group, plus installed-collection rows referencing a
    version in the group, and sets their ``update_available_version_id`` to
    the newly published version.
    """

    async def _notify(s: AsyncSession) -> None:
        # Point read keyed by an id the caller resolved through an org-scoped
        # path (the publish route loads the draft collection in-org first).
        # The fan-out below is where the org boundary is enforced: recipients
        # and the stamp are both clamped to ``org_id`` (FAR-2696).
        prim = await get_library_primitive(s, primitive_id)
        if prim is None:
            return
        await _mark_update_recipients(s, org_id, prim)

    try:
        await _scoped_execute(session, org_id, _notify)
    except ProgrammingError:
        logger.warning("notify_importers_of_update failed (DB not migrated): %s", primitive_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("notify_importers_of_update: unexpected error for primitive %s", primitive_id)


# ---------------------------------------------------------------------------
# Public API — list primitives
# ---------------------------------------------------------------------------


def _filter_modulo(
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
) -> list[LibraryPrimitive]:
    """Filter the built-in modulo registry primitives."""
    if primitive_types:
        allowed = set(primitive_types)
        modulo = [p for p in _BUILTIN_PRIMITIVES if p.primitive_type in allowed]
    elif primitive_type is not None:
        modulo = [p for p in _BUILTIN_PRIMITIVES if p.primitive_type == primitive_type]
    else:
        modulo = list(_BUILTIN_PRIMITIVES)
    if search:
        term = search.strip().lower()
        if term:
            modulo = [p for p in modulo if term in (p.name or "").lower() or term in (p.description or "").lower()]
    return modulo


async def _load_org_page(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    primitive_type: str | None,
    primitive_types: list[str] | None,
    search: str | None,
    page: int,
    page_size: int,
    source: str | None,
    cursor: str | None,
    excluded_tiers: list[str] | None,
) -> PageResult[LibraryPrimitive]:
    """Fetch the org page, degrading gracefully to an empty page on DB failure."""
    org_page: PageResult[LibraryPrimitive] = PageResult(items=[], total=0, page=page, page_size=page_size)

    async def _query(s: AsyncSession) -> None:
        nonlocal org_page
        org_page = await list_library_primitives(
            s,
            org_id=org_id,
            page=page,
            page_size=page_size,
            primitive_type=primitive_type,
            primitive_types=primitive_types,
            search=search,
            source=source,
            cursor=cursor,
            excluded_tiers=excluded_tiers,
        )

    try:
        # Reuse the caller's transaction (routes open one before calling in);
        # nesting session.begin() raised InvalidRequestError (FAR-2286).
        await _scoped_execute(session, org_id, _query)
    except ProgrammingError:
        logger.warning("list_primitives — DB not migrated for org %s", org_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("list_primitives — DB query failed for org %s", org_id)
    return org_page


async def list_primitives(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    primitive_type: str | None = None,
    primitive_types: list[str] | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
    source: str | None = None,
    cursor: str | None = None,
    excluded_tiers: list[str] | None = None,
) -> PageResult[LibraryPrimitive]:
    """Return org-scoped and Native library primitives merged into a single page.

    ``source`` (when given) restricts the result to exactly that source
    value — e.g. ``source="modulo"`` returns only Native library built-ins,
    ``source="local"`` returns only the org's own saved primitives, and
    ``source="registry"`` returns only the org's DB rows carrying that source.
    ``source="community"`` is retired (FAR-2592): the parameter is still
    accepted for compatibility but always returns no rows, and
    community-sourced rows are excluded from the merged listing (the legacy
    rows were soft-deleted and the list predicate excludes the source). When
    omitted, every live source is merged. The filter is applied in SQL
    alongside the COUNT, so ``total`` and ``has_more`` stay exact on the
    ``?source=`` path.

    ``primitive_types`` (when given) restricts the result to any of the
    listed primitive types, e.g. ``["workflow", "agent"]``. It takes
    precedence over the single-value ``primitive_type`` filter.

    A failing org query degrades to an empty org page (logged) rather than
    failing the whole listing, so the Native built-ins are still served.
    """
    if excluded_tiers is None:
        excluded_tiers = ["in_dev"]
    org_page = await _load_org_page(
        session,
        org_id,
        primitive_type=primitive_type,
        primitive_types=primitive_types,
        search=search,
        page=page,
        page_size=page_size,
        source=source,
        cursor=cursor,
        excluded_tiers=excluded_tiers,
    )

    # ``source`` and ``excluded_tiers`` are applied inside
    # ``list_library_primitives`` (they share the COUNT and row-page
    # ``conditions``), so ``org_page.total`` is already the exact filtered
    # universe count and is passed through unchanged.
    org_items = list(org_page.items)
    modulo: list[LibraryPrimitive] = []
    if source is None or source == "modulo":
        modulo = _filter_modulo(
            primitive_type=primitive_type,
            primitive_types=primitive_types,
            search=search,
        )
    if excluded_tiers:
        org_items = [p for p in org_items if p.tier not in excluded_tiers]
        modulo = [p for p in modulo if p.tier not in excluded_tiers]

    all_items: list[LibraryPrimitive] = org_items + modulo
    total = org_page.total + len(modulo)
    # ``has_more`` is derived here rather than forwarded from ``org_page`` so
    # the envelope can never contradict its own items.
    has_more = (page - 1) * page_size + len(all_items) < total
    return PageResult(
        items=all_items,
        total=total,
        page=page,
        page_size=page_size,
        # ``next_cursor`` continues the org slice only. The in-memory modulo
        # built-ins are bounded and are re-merged in full on every page; they
        # share no ordering key with the org rows, so the asymmetry is
        # deliberate. ``has_more`` is the authoritative "another page" signal.
        next_cursor=org_page.next_cursor,
        has_more=has_more,
    )
