"""Database/app release alignment guard (FAR-2659).

Migrations are forward-only and the database and the application are versioned
together, so a database migrated by a newer release must never be served by an
older one.  Alembic already records the database's applied revision(s) in the
``alembic_version`` table; this module compares them with the revision ids the
running release ships and refuses to start when the database is AHEAD (records
a revision this release does not know).

Deliberately tiny, and deliberately shared: no new table, no new migration, no
compatibility matrix, no phase machine.  Every startup path (the container
entrypoints and the native launcher) calls this one function, and HA will reuse
it to tell a stale instance from a valid one.

Contract:

* database behind the code  -> :attr:`AlignmentResult.aligned` is True; the
  caller proceeds and migrates exactly as before;
* database exactly at head -> aligned; the caller proceeds;
* database ahead (or at a revision this release cannot name) -> refuses with
  one line naming the database revision, the app release version and the
  recovery command, and :data:`EXIT_DATABASE_AHEAD`.

The check is read-only: it never creates, alters or migrates anything.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "EXIT_DATABASE_AHEAD",
    "RECOVERY_COMMAND",
    "AlignmentCheckError",
    "AlignmentResult",
    "DatabaseAheadError",
    "app_version",
    "check_alignment",
    "database_url_from_env",
    "ensure_release_aligned",
    "format_refusal",
    "main",
    "read_database_revisions",
    "release_revision_ids",
]

#: Distinct refusal status (78 = EX_CONFIG, "configuration error").  It exists
#: so a supervisor/orchestrator can tell "this pairing is invalid" apart from a
#: crash.  Every other outcome is 0 (proceed).
EXIT_DATABASE_AHEAD = 78

#: The recovery command named in the refusal.  Rolling a database backwards is
#: not supported, so the way out is restoring the last pre-upgrade snapshot
#: (FAR-2667 ships the snapshot taker) and starting the release that owns the
#: database, or rolling this deployment forward to that release.
RECOVERY_COMMAND = "farna-control restore <backup-dir> --yes"

#: Driver names the shared migration translator rewrites to a sync driver.
#: PostgreSQL is the only runtime database (ADR 068); anything else is left
#: untouched so a caller-supplied already-sync URL is never rebuilt lossily.
_SYNC_TRANSLATED_DRIVERS = frozenset({"postgres", "postgresql", "postgresql+asyncpg"})


class AlignmentCheckError(RuntimeError):
    """The alignment check could not be performed.

    Callers fail OPEN on this: a transient database blip must not block a
    legitimate forward upgrade, and the migration runner reports the real
    fault with its own error.
    """


class DatabaseAheadError(RuntimeError):
    """The database records a revision this release does not ship."""


@dataclass(frozen=True)
class AlignmentResult:
    """The database's recorded revisions versus this release's revisions."""

    db_revisions: frozenset[str]
    release_revisions: frozenset[str]

    @property
    def unknown_revisions(self) -> frozenset[str]:
        """Revisions the database records that this release does not know."""
        return self.db_revisions - self.release_revisions

    @property
    def aligned(self) -> bool:
        """True when every recorded database revision is known to this release."""
        return not self.unknown_revisions


def app_version() -> str:
    """The running build's version (bundle stamp > package metadata)."""
    from farna.version import get_version

    return get_version()


def database_url_from_env() -> str | None:
    """The migration/admin database URL from the environment, or ``None``.

    Same precedence as ``farna.db.migrations.env``: ``DATABASE_ADMIN_URL`` (the
    owner role migrations run as) wins over ``DATABASE_URL``.
    """
    for name in ("DATABASE_ADMIN_URL", "DATABASE_URL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def _sync_url(url: str) -> str:
    """Translate a driver URL to a sync SQLAlchemy URL (single translator).

    Only the driver families the migration env knows are rewritten; an
    already-sync URL is returned untouched.  Rebuilding every URL through the
    translator is lossy for schemes it does not map (``urlunparse`` collapses
    ``sqlite:///`` to ``sqlite:/``), which is exactly the kind of silent
    mistranslation this guard must not introduce.
    """
    from sqlalchemy.engine import make_url

    if make_url(url).drivername in _SYNC_TRANSLATED_DRIVERS:
        from farna.db.migrations.env import _to_sync_url

        return _to_sync_url(url)
    return url


def release_revision_ids() -> frozenset[str]:
    """Every revision id the release's migration tree ships.

    Raises :class:`AlignmentCheckError` when the tree cannot be read: an image
    whose own migrations cannot be enumerated cannot claim alignment.
    """
    from farna.db.migration_guard import release_revision_ids as _release_revision_ids

    try:
        return frozenset(_release_revision_ids())
    except Exception as exc:  # any unreadable tree is "cannot check", not "ahead"
        raise AlignmentCheckError(f"could not read the release migration tree: {exc}") from exc


def read_database_revisions(database_url: str) -> frozenset[str]:
    """The revision ids recorded in the database's ``alembic_version`` table.

    An absent table (or an empty one) means a fresh/uninitialised database:
    that is ``frozenset()``, not an error.  Any actual read failure raises
    :class:`AlignmentCheckError` so the caller can fail open.
    """
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.engine import Engine
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.pool import NullPool

    engine: Engine | None = None
    try:
        engine = create_engine(_sync_url(database_url), poolclass=NullPool)
        with engine.connect() as connection:
            if not inspect(connection).has_table("alembic_version"):
                return frozenset()
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        return frozenset(str(row[0]) for row in rows)
    except SQLAlchemyError as exc:
        raise AlignmentCheckError(f"could not read alembic_version: {exc}") from exc
    finally:
        if engine is not None:
            engine.dispose()


def check_alignment(
    db_revisions: Iterable[str],
    release_revisions: Iterable[str],
) -> AlignmentResult:
    """Compare the database's revisions with the release's (pure)."""
    return AlignmentResult(
        db_revisions=frozenset(db_revisions),
        release_revisions=frozenset(release_revisions),
    )


def format_refusal(result: AlignmentResult) -> str:
    """The single-line refusal: database revision, app version, recovery."""
    database = ", ".join(sorted(result.unknown_revisions))
    return (
        f"REFUSED: database revision {database} is not known to this release (farna-control {app_version()}); "
        "migrations are forward-only, so this release will not run against a database from a newer release. "
        f"Recover with: {RECOVERY_COMMAND} and then start the release that owns the database, or deploy that release."
    )


def ensure_release_aligned(database_url: str) -> AlignmentResult:
    """Raise :class:`DatabaseAheadError` when the database is ahead of this release.

    Also raises :class:`AlignmentCheckError` when the check cannot run at all;
    callers treat that as "proceed" (fail open).
    """
    result = check_alignment(read_database_revisions(database_url), release_revision_ids())
    if not result.aligned:
        raise DatabaseAheadError(format_refusal(result))
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint for ``python -m farna.db.alignment``.

    Returns :data:`EXIT_DATABASE_AHEAD` when the database is ahead, else 0.
    A check that cannot run warns and returns 0 so a normal forward upgrade is
    never blocked by a transient database problem.
    """
    del argv  # no arguments: the environment carries the target
    database_url = database_url_from_env()
    if database_url is None:
        sys.stderr.write("WARNING: no DATABASE_ADMIN_URL/DATABASE_URL set; skipping the database/app release alignment check\n")
        return 0
    try:
        ensure_release_aligned(database_url)
    except DatabaseAheadError as exc:
        sys.stdout.write(f"{exc}\n")
        sys.stdout.flush()
        return EXIT_DATABASE_AHEAD
    except AlignmentCheckError as exc:
        sys.stderr.write(f"WARNING: database/app release alignment check skipped ({exc})\n")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
