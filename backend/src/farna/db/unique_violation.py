"""Low-level unique-constraint-violation detection.

Neutral home shared by the two rate-limit conflict paths so they use identical
detection semantics: ``TriggerEngine``'s dedup path (core/trigger_engine) and
``create_run``'s admission path (db/crud/run). Kept out of either consumer so
there is a single source of truth and no circular-import pressure.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def is_unique_violation(exc: IntegrityError) -> bool:
    """Return True if *exc* is a unique-constraint violation (not FK, NOT NULL, etc.).

    Handles PostgreSQL (pgcode 23505) and SQLite (UNIQUE constraint failed).
    PostgreSQL is the only supported runtime database (ADR 068); SQLite exists
    for tests only. No MySQL-family detection remains.
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    pgcode = getattr(orig, "pgcode", None)
    if pgcode is not None:
        return str(pgcode) == "23505"
    msg = str(orig)
    return "UNIQUE constraint failed" in msg
