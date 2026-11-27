# Database migrations

Alembic migrations live in `versions/`. This file records the conventions a new
migration must follow.

## A merged migration is immutable

Never edit a revision that has shipped. Anything already applied on a real
database cannot be changed by editing its file: the revision id is recorded, so
the edit is simply never run. Add a new revision on top instead.

## One linear head

Number the new file above the current head and set its `down_revision` to that
head. Do not branch. The `check-migration-heads` hook rejects a duplicate number
or a revision that does not extend the single head, and CI runs the same check.

## Batched data migrations

A migration that rewrites data must stay bounded and repeatable:

- Process rows in bounded batches with a keyset cursor (for example `id > :after_id`
  ordered by the primary key), never one big `UPDATE`.
- Make each run idempotent: the rewrite must stop matching its own output, so a
  second run is a genuine no-op and the batch loop terminates.
- No-op cleanly when nothing matches.
- Never use destructive DDL — no dropped columns, no `ALTER COLUMN TYPE`, no
  deletes.
- Log the number of rows changed so an operator can see what the run did.

## Which connection the batches run on

This choice is the one that bites. Get it wrong and the migration either cannot
run on a fresh install or holds locks for the whole upgrade.

**Default — a dedicated autocommit connection.** When the target table already
exists and is committed before your migration runs, open a side connection from
the migration engine with `AUTOCOMMIT` (`bind.engine.connect()` with
`execution_options(isolation_level="AUTOCOMMIT")`) and run the batches on it.
Do not call `commit()` on alembic's own connection to get autocommit: that closes
the migration transaction. A side connection gives per-batch commits — resumable,
with short row locks.

**Exception — run on the migration connection instead.** When the target table
may be created earlier in the *same* upgrade chain — the fresh-install case — a
side connection cannot see it and fails with
`relation "..." does not exist`. Rehearsal mode also refuses a side connection,
because the run must stay inside one rolled-back transaction. In that case run the
batches on `op.get_bind()`. The whole migration is then one transaction: row locks
are held until it commits and there is no mid-way resume. If the matching set can
become large, move the cleanup into an explicit one-off script.

The trade-off, stated once: a side connection buys resumability and short locks
but cannot see uncommitted tables or be rehearsed; the migration connection always
works but holds its locks to the end. Check which case yours is before choosing —
do not "simplify" this to one connection for both.

## Keep internal detail out of stored text

Do not persist raw tracebacks or internal source paths in a user-visible text
column. Store a short, readable reason and keep the full diagnostic in the log.
The `runs.error_detail` redaction is the worked example: matching rows are
rewritten to a short reason derived from `error_code`, and clean one-line
summaries are left byte-identical.

## Tests

A migration that changes data needs a test in `tests/unit/db/` proving:

- only matching rows are rewritten;
- clean rows are left byte-identical;
- a second run changes nothing (idempotent).

Postgres-backed cases run in CI. Where Docker is unavailable locally, the real
database test skips rather than passing silently, and the Postgres execution is
reported as deferred to CI.
