"""Redact raw Python tracebacks from already-persisted ``runs.error_detail`` (FAR-2823).

A terminal pipeline run whose unexpected failure is not converted into a typed
failure stores the *raw* traceback in ``runs.error_detail``: the executor's
failure path (``core.pipeline_engine.executor`` -> ``failure_context.enrich_error_detail``)
appends attribution to a ``traceback.format_exc()`` string, so a row can carry a
multi-line traceback with absolute source paths, e.g. on staging::

    Traceback (most recent call last):
      File "/app/src/farna/core/pipeline_engine/executor.py", line 3970, in ...

That field is exposed on REST/MCP run responses, so the internal server paths
and code layout leak to every reader of the run. This migration is a one-time,
data-only cleanup of the rows that already hold such a traceback. It does not
change how the field is written going forward (that is the executor's job); it
repairs the rows already on disk.

What is rewritten
-----------------
Only ``runs.error_detail``. For every matching row the column is set to a short,
path-free reason derived from the row's own ``error_code``::

    <error_code>: server-side failure; see server logs

and, when ``error_code`` is NULL or empty, the bare generic reason::

    server-side failure; see server logs

The value is capped at ``_MAX_DETAIL_LEN`` (500) characters with ``left(...)``.
``error_code`` is ``varchar(255)`` so the composed value is at most ~293
characters; the ``left(...)`` cap is a defensive guard, not a truncation that
fires in practice. ``error_code``, ``status`` and every other column are left
untouched - only ``error_detail`` appears in the ``SET`` clause, so the
tenant-consistency triggers on ``runs`` (they fire on
``UPDATE OF organisation_id | owner_team_id | ...``) are **not** triggered by
this statement.

Exact match predicate
---------------------
A row matches when its ``error_detail`` is non-NULL and CONTAINS any of these
three markers (substring containment, via ``position(marker IN error_detail) > 0``
- not ``LIKE``, so there is no ``%``/``_`` wildcard ambiguity)::

    "Traceback (most recent call last)"   _MARKER_TRACEBACK
    "/app/src/"                          _MARKER_APP_PATH
    '.py", line '                        _MARKER_FRAME

These are the three shapes the ticket names. They are deliberately NARROW: a
tidy single-line summary (e.g. "Upstream connector returned HTTP 503") matches
none of them and is left byte-identical. The predicate is not broadened here.

Idempotency and termination
---------------------------
The rewrite is idempotent: the replacement text contains none of the three
markers, so a redacted row no longer matches and a second run selects nothing
(it is a genuine no-op, not an "update to the same value"). Improvement is
monotonic - each batch removes rows from the matching set - so the loop
terminates. Pagination is keyset on the primary key (``id > :after_id``), so
even a pathological row whose ``error_code`` itself contained a marker could be
visited at most once and could not loop forever; in practice ``error_code`` is a
short taxonomy code (``harness.state_serialization``, ``provider.timeout`` ...)
that cannot contain any marker.

Bounded batches, no long lock, no destructive DDL
-------------------------------------------------
``runs`` is a large, hot, append-only table. The rewrite therefore runs as a
sequence of bounded ``UPDATE ... LIMIT`` batches (``_BATCH_SIZE`` rows each) on
a DEDICATED AUTOCOMMIT connection opened from the migration bind's engine - NOT
the alembic transaction connection. Each batch commits and releases its row
locks before the next begins, so no single long-lived transaction accumulates
locks or WAL on the table. There is no ``ALTER COLUMN`` (no type change, no
ACCESS EXCLUSIVE rewrite) and no row is ever deleted.

RLS / organisation scope
------------------------
``runs`` enables row-level security with the ``rls_org_isolation`` policy but is
NOT ``FORCE``-RLS'd (0351), so the schema owner - the role the deploy's release
command runs ``alembic upgrade heads`` as - bypasses it. This is intentionally a
cross-organisation data fix and it never sets ``app.organisation_id``. Run as a
non-owner role, RLS would filter the rows and the fix would silently under-apply;
the shipped migration posture is the owner.

``node_telemetry_json`` - checked, deliberately NOT touched
-----------------------------------------------------------
There is no ``runs.node_telemetry_json`` column: the legacy ``runs`` blob column
was dropped (FAR-583 B1) and per-node telemetry now lives in
``run_node_outputs.node_telemetry_json``. That column is free-form per-node
telemetry (``dict[str, Any]`` - ``{value, reason, delivered_pr_urls, computed_at,
work_intact, ...}``), read back by retention/purge and the per-node store, and it
is not the run's error reason. A bounded, structure-preserving text rewrite over
an arbitrary-shaped JSON blob cannot be proven safe (the traceback could sit at
any nesting depth, and replacing a subtree would corrupt the node telemetry a
reader relies on), so it is intentionally left untouched. Only the well-typed
``runs.error_detail`` text column is redacted.

Downgrade
---------
A documented no-op. The redaction deletes internal source paths from a
security-sensitive field and the original traceback text is deliberately NOT
retained, so there is nothing to restore; re-introducing the paths would be a
regression. The reverse direction changes no rows and no schema.

SQLite (test-only, ADR 068) never runs migrations, so ``upgrade`` no-ops on a
non-PostgreSQL dialect.

Proven against real PostgreSQL by
``tests/unit/db/test_migration_0369_redact_error_detail_tracebacks.py`` (seeds a
matching row, a NULL-code matching row and a clean row; runs the cleanup twice;
asserts the matching rows are redacted with no paths / "Traceback", the clean row
is byte-identical, and the second run changes nothing). The predicate,
replacement text, cap and marker set are additionally unit-tested without a
database.

Revision ID: 0369_redact_error_detail_tracebacks
Revises: 0368_invitations_quarantine_rls
Create Date: 2026-11-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0369_redact_error_detail_tracebacks"
down_revision: str | None = "0368_invitations_quarantine_rls"
branch_labels = None
depends_on = None

#: The three exact traceback / internal-path markers the ticket names. A row is
#: redacted when its ``error_detail`` contains any one of them (containment).
_MARKER_TRACEBACK = "Traceback (most recent call last)"
_MARKER_APP_PATH = "/app/src/"
_MARKER_FRAME = '.py", line '

#: The path-free generic reason, and the ``"<code>" + suffix`` shape used when a
#: row carries a usable ``error_code``. ``_REASON_SUFFIX`` is derived from
#: ``_GENERIC_REASON`` so the two can never drift apart.
_GENERIC_REASON = "server-side failure; see server logs"
_REASON_SUFFIX = ": " + _GENERIC_REASON

#: Defensive cap on the rewritten detail (well above the ~293-char practical
#: maximum for a 255-char ``error_code`` plus the suffix).
_MAX_DETAIL_LEN = 500

#: Rows per bounded ``UPDATE`` batch. Small enough that each batch's row locks
#: and WAL are released quickly; large enough to keep the loop's round-trips low.
_BATCH_SIZE = 500

#: The lowest possible UUID, used as the initial keyset cursor so the first batch
#: starts at the beginning of ``runs``.
_MIN_UUID = "00000000-0000-0000-0000-000000000000"

#: One bounded, keyset-paginated redaction batch. Plain string literal with bind
#: parameters only - no interpolation - so it is not SQL-injection surface (and
#: is not flagged by the migration f-string SQL rules). The CTE selects the next
#: ``LIMIT :batch`` matching ids ordered by primary key (forward, resumable); the
#: outer ``UPDATE`` rewrites exactly those ids and returns them for counting.
#:
#: ``error_detail`` is the sole column in ``SET`` - ``error_code``, ``status`` and
#: every other column are untouched, and the ``UPDATE OF <tenant col>`` triggers
#: on ``runs`` do not fire.
_BATCH_SQL = """
WITH batch AS (
    SELECT id
    FROM public.runs
    WHERE id > CAST(:after_id AS uuid)
      AND error_detail IS NOT NULL
      AND (
            position(:m1 IN error_detail) > 0
         OR position(:m2 IN error_detail) > 0
         OR position(:m3 IN error_detail) > 0
      )
    ORDER BY id
    LIMIT CAST(:batch AS integer)
)
UPDATE public.runs AS r
SET error_detail = left(
        CASE
            WHEN r.error_code IS NULL OR r.error_code = ''
                THEN :generic
            ELSE r.error_code || :suffix
        END,
        CAST(:max_len AS integer)
    )
FROM batch
WHERE r.id = batch.id
RETURNING r.id
"""

_BATCH_STATEMENT = sa.text(_BATCH_SQL)


def _bind_params(after_id: str) -> dict[str, object]:
    """Bind values for one batch: the keyset cursor, markers, replacement and cap."""
    return {
        "after_id": after_id,
        "m1": _MARKER_TRACEBACK,
        "m2": _MARKER_APP_PATH,
        "m3": _MARKER_FRAME,
        "generic": _GENERIC_REASON,
        "suffix": _REASON_SUFFIX,
        "max_len": _MAX_DETAIL_LEN,
        "batch": _BATCH_SIZE,
    }


def _redact_runs(bind: sa.Connection) -> int:
    """Redact every matching ``runs`` row in bounded batches; return rows changed.

    A dedicated AUTOCOMMIT connection (NOT the alembic transaction connection) is
    used so each batch commits and releases its locks before the next. The keyset
    cursor advances on the returned ids so the loop is forward-only and
    terminates; a batch that returns no rows ends the loop.
    """
    engine = bind.engine
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    total = 0
    after_id = _MIN_UUID
    try:
        while True:
            rows = connection.execute(_BATCH_STATEMENT, _bind_params(after_id)).all()
            if not rows:
                break
            total += len(rows)
            after_id = str(max(row[0] for row in rows))
    finally:
        connection.close()
    return total


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Migrations never run on the SQLite test backend (ADR 068); a no-op keeps
        # the module importable wherever the alembic environment is walked.
        return
    changed = _redact_runs(bind)
    print(  # noqa: T201 - migrations signal progress via stdout; the structured logger does not render in fly logs
        f"0369_redact_error_detail_tracebacks: redacted error_detail on {changed} run row(s)"
    )


def downgrade() -> None:
    # Deliberately irreversible: the redaction removes internal source paths from a
    # security-sensitive field and the original traceback text is not retained, so
    # there is nothing to restore. Re-introducing the paths would be a regression.
    return
