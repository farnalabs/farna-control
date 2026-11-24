"""Seed 3 pipeline template definitions into the database.

Usage:
    uv run python -m scripts.seed_library_templates

The seeded rows are built-in ``pipeline_template`` primitives, so they must
carry the same current canonical provenance (``farna_control``) and author
(``Farna Control``) as ``core/library_service/_seed_data.py::_make_native``.

The legacy provenance value is rejected by ``ck_library_primitives_source``
(migration 0364 rewrote the stored rows and narrowed the vocabulary). The
legacy author is rewritten to ``Farna Control`` by migration 0357 — but only
for the persisted built-in collection row or a local copy with a NULL
``forked_from``. The previous shape here (local source, non-NULL ``forked_from``)
escaped that guard, so each manual run reintroduced the retired author.
"""

import asyncio
import sys
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from farna.core.library_service import _BUILTIN_PRIMITIVES
from farna.db.crud.library_primitive import create_library_primitive
from farna.db.models.library_primitive import LibraryPrimitive
from farna.settings import get_settings

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

#: Current canonical provenance and author for a materialised built-in row.
#: Keep these aligned with the 0364 (source) and 0357 (author) rewrite targets.
SOURCE = "farna_control"
AUTHOR = "Farna Control"


def build_primitive_kwargs(template: LibraryPrimitive) -> dict[str, Any]:
    """Build the ``create_library_primitive`` arguments for one built-in template."""
    return {
        "org_id": ORG_ID,
        "source": SOURCE,
        "primitive_type": "pipeline_template",
        "name": template.name,
        "slug": template.slug,
        "description": template.description,
        "author": AUTHOR,
        "version": "1.0",
        "tags": list(template.tags) if template.tags else [],
        "content_json": dict(template.content_json),
        "source_url": None,
        "forked_from": None,
        "checksum": None,
        "ed25519_signature": None,
        "verified": None,
        "download_count": None,
        "average_rating": None,
        "review_count": None,
        "owner_team_id": None,
        "visibility": "org",
        "account_id": ACCOUNT_ID,
    }


async def seed() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with AsyncSession(engine) as session:
        templates = [p for p in _BUILTIN_PRIMITIVES if p.primitive_type == "pipeline_template"]
        for template in templates:
            existing = (
                await session.execute(
                    select(LibraryPrimitive).where(
                        LibraryPrimitive.organisation_id == ORG_ID,
                        LibraryPrimitive.name == template.name,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  SKIP  {template.name} — already exists", file=sys.stderr)
                continue

            await create_library_primitive(session, **build_primitive_kwargs(template))
            print(f"  CREATED  {template.name}", file=sys.stderr)

        await session.commit()
    await engine.dispose()
    print(f"\nSeeded {len(templates)} pipeline template definitions.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(seed())
