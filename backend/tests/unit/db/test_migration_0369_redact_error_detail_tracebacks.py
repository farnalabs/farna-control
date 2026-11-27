"""0369 redacts raw Python tracebacks from persisted ``runs.error_detail`` (FAR-2823).

Two layers of proof:

* DB-free unit tests pin the *pure* parts deterministically on every machine - the
  exact match markers, the replacement text derived from ``error_code``, the
  500-character cap, and that the replacement is path-free / ``Traceback``-free
  (which is what makes the rewrite idempotent). These run without PostgreSQL.
* A PostgreSQL-execution test seeds one matching row (with a real traceback and a
  code), one matching row with a NULL code (the generic branch) and three rows
  that must NOT match (a clean summary, a ``.py at line`` near-miss that is not a
  frame marker, and a bare "Traceback" without the full phrase). It then runs the
  cleanup TWICE and asserts: the matching rows are redacted with no paths and no
  "Traceback", ``error_code``/``status`` are untouched, the non-matching rows are
  byte-identical, and the second run changes nothing.

The PostgreSQL test is capability-gated exactly like
``test_migration_0364_rewrite_legacy_persisted_values.py``: an env-provisioned URL
is used when present, otherwise testcontainers, otherwise the real-DB test skips
(never silently passing). On a workstation with no Docker it skips and the
Postgres execution is named as deferred to CI.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

_MIGRATION_PATH = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0369_redact_error_detail_tracebacks.py"
_PG_URL_ENV = "FARNA_2823_TEST_PG_URL"

#: A real Python traceback: carries all three markers.
_TRACEBACK_WITH_CODE = 'Traceback (most recent call last):\n  File "/app/src/farna/core/pipeline_engine/executor.py", line 3970, in _stream_graph\n    raise RuntimeError("state not serializable")\nRuntimeError: state not serializable'
#: A partial traceback body: carries the frame + app-path markers, with no code.
_TRACEBACK_NULL_CODE = '  File "/app/src/farna/core/pipeline_engine/executor.py", line 42, in run\nValueError: boom'
#: A tidy summary that matches none of the markers - must stay byte-identical.
_CLEAN_DETAIL = "Upstream connector returned HTTP 503; retry budget exhausted."
#: A near-miss: mentions ".py" and "line" but NOT the '.py", line ' frame marker.
_PY_NEAR_MISS = "Config error in settings.py at line 12"
#: A bare "Traceback" word WITHOUT the full marker phrase - must not match.
_BARE_TRACEBACK_WORD = "See Traceback above for context"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0369_redact_error_detail_tracebacks", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# DB-free unit tests - run on every machine, no PostgreSQL.
# ---------------------------------------------------------------------------


def test_revision_continues_the_current_single_head() -> None:
    module = _load_migration()

    assert module.revision == "0369_redact_error_detail_tracebacks"
    assert module.down_revision == "0368_invitations_quarantine_rls"


def test_the_three_exact_match_markers_are_the_ticket_spec() -> None:
    module = _load_migration()

    assert module._MARKER_TRACEBACK == "Traceback (most recent call last)"
    assert module._MARKER_APP_PATH == "/app/src/"
    assert module._MARKER_FRAME == '.py", line '
    # The SQL predicate is strict containment of all three markers (bound to :m1/:m2/:m3), keyed and batched.
    assert "position(:m1 IN error_detail) > 0" in module._BATCH_SQL
    assert "position(:m2 IN error_detail) > 0" in module._BATCH_SQL
    assert "position(:m3 IN error_detail) > 0" in module._BATCH_SQL
    assert "error_detail IS NOT NULL" in module._BATCH_SQL
    assert "LIMIT CAST(:batch AS integer)" in module._BATCH_SQL
    # Keyset cursor on the primary key so a re-run resumes forward and terminates.
    assert "id > CAST(:after_id AS uuid)" in module._BATCH_SQL


def test_replacement_is_derived_from_error_code_and_capped() -> None:
    module = _load_migration()

    # The exact generic reason, and the code-prefixed shape the SQL builds.
    assert module._GENERIC_REASON == "server-side failure; see server logs"
    assert "harness.state_serialization" + module._REASON_SUFFIX == "harness.state_serialization: server-side failure; see server logs"
    # The SQL derives the value from error_code (concatenation), falls back to the generic, and caps the length.
    assert "r.error_code IS NULL OR r.error_code = ''" in module._BATCH_SQL
    assert "r.error_code || :suffix" in module._BATCH_SQL
    assert ":generic" in module._BATCH_SQL
    assert "left(" in module._BATCH_SQL
    assert ":max_len" in module._BATCH_SQL
    assert module._MAX_DETAIL_LEN == 500
    # A maximal 255-char code (the column width) plus the suffix still fits under the cap,
    # so the left(...) guard never truncates a real value.
    assert 255 + len(module._REASON_SUFFIX) <= module._MAX_DETAIL_LEN


def test_replacement_text_is_path_free_and_traceback_free() -> None:
    module = _load_migration()

    markers = (module._MARKER_TRACEBACK, module._MARKER_APP_PATH, module._MARKER_FRAME)
    reasons = (
        module._GENERIC_REASON,
        module._REASON_SUFFIX,
        "harness.state_serialization" + module._REASON_SUFFIX,
    )
    for reason in reasons:
        assert "Traceback" not in reason
        for marker in markers:
            assert marker not in reason


# ---------------------------------------------------------------------------
# PostgreSQL execution - capability-gated (env URL -> testcontainers -> skip).
# ---------------------------------------------------------------------------


def _sync_url(raw: str) -> str:
    """Force the psycopg (v3) sync driver - psycopg2 is not in the tree."""
    return re.sub(r"^postgresql(\+[a-z0-9]+)?://", "postgresql+psycopg://", raw, flags=re.IGNORECASE)


def _docker_daemon_socket_present() -> bool:
    """Leak-free check for a local Docker daemon before touching testcontainers.

    ``docker.from_env()`` leaves its probe socket open when the local unix socket
    is absent, and this module runs under the gated-suite warning gate, which
    escalates that leak to a session error. Checking the well-known socket paths
    first keeps a no-Docker run a clean skip.
    """
    if os.environ.get("DOCKER_HOST"):
        return True
    candidates = [Path("/var/run/docker.sock"), Path("~/.docker/run/docker.sock").expanduser()]
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        candidates.append(Path(xdg_runtime) / "docker.sock")
    return any(path.exists() for path in candidates)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """Throwaway PostgreSQL URL: env-provisioned, else testcontainers, else skip."""
    env_url = os.environ.get(_PG_URL_ENV)
    if env_url:
        yield _sync_url(env_url)
        return

    if not _docker_daemon_socket_present():
        pytest.skip("testcontainers Postgres unavailable (no Docker daemon socket reachable)")

    try:
        from testcontainers.community.postgres import PostgresContainer
    except Exception as exc:  # capability-gated skip, never a silent pass
        pytest.skip(f"testcontainers Postgres unavailable (driver missing): {exc}")

    container = None
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # capability-gated skip, never a silent pass
        if container is not None:
            with contextlib.suppress(Exception):
                container.stop()
        pytest.skip(f"testcontainers Postgres unavailable (no Docker): {exc}")
    try:
        yield _sync_url(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture
def engine(pg_url: str) -> Iterator[sa.Engine]:
    """A fresh database per test - the migration hard-codes the ``public`` schema."""
    base_url = make_url(pg_url)
    admin = sa.create_engine(base_url, isolation_level="AUTOCOMMIT")
    database = f"m0369_{uuid.uuid4().hex[:12]}"
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    test_engine = sa.create_engine(base_url.set(database=database))
    try:
        yield test_engine
    finally:
        test_engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.dispose()


#: The minimal ``runs`` shape the migration touches. Only ``error_detail`` is
#: rewritten; ``error_code`` and ``status`` must survive unchanged.
_RUNS_DDL = """
CREATE TABLE public.runs (
    id uuid PRIMARY KEY,
    error_detail text,
    error_code varchar(255),
    status varchar(30) NOT NULL DEFAULT 'failed'
)
"""

#: Explicit ids so the keyset pagination order is deterministic.
_ROW_TRACEBACK_WITH_CODE = "00000000-0000-0000-0000-0000000000a1"
_ROW_TRACEBACK_NULL_CODE = "00000000-0000-0000-0000-0000000000b2"
_ROW_CLEAN = "00000000-0000-0000-0000-0000000000c3"
_ROW_PY_NEAR_MISS = "00000000-0000-0000-0000-0000000000d4"
_ROW_BARE_TRACEBACK = "00000000-0000-0000-0000-0000000000e5"


def _exec(conn: sa.Connection, sql: str, params: dict[str, object] | None = None) -> sa.CursorResult[Any]:
    """Run ``sql`` with ``:name`` parameters translated by SQLAlchemy."""
    return conn.execute(sa.text(sql), params or {})


def _seed(engine: sa.Engine) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(_RUNS_DDL)
        rows: tuple[tuple[str, str | None, str | None, str], ...] = (
            (_ROW_TRACEBACK_WITH_CODE, _TRACEBACK_WITH_CODE, "harness.state_serialization", "failed"),
            (_ROW_TRACEBACK_NULL_CODE, _TRACEBACK_NULL_CODE, None, "failed"),
            (_ROW_CLEAN, _CLEAN_DETAIL, "provider.unavailable", "failed"),
            (_ROW_PY_NEAR_MISS, _PY_NEAR_MISS, "config.invalid", "failed"),
            (_ROW_BARE_TRACEBACK, _BARE_TRACEBACK_WORD, "agent.failed", "failed"),
        )
        for row_id, detail, code, status in rows:
            _exec(
                conn,
                "INSERT INTO public.runs (id, error_detail, error_code, status) VALUES (CAST(:id AS uuid), :detail, :code, :status)",
                {"id": row_id, "detail": detail, "code": code, "status": status},
            )


def _run_upgrade(engine: sa.Engine, module: ModuleType) -> None:
    """Execute ``module.upgrade`` against the real database.

    The migration reads ``op.get_bind()`` and opens its own AUTOCOMMIT connection
    from ``bind.engine``; patching ``get_bind`` to return a live connection on the
    test engine makes that path run for real.
    """
    with engine.connect() as bind, patch.object(op, "get_bind", return_value=bind):
        module.upgrade()


def _snapshot(conn: sa.Connection) -> dict[str, tuple[str | None, str | None, str]]:
    """Map every row id to ``(error_detail, error_code, status)`` for exact diffs."""
    rows = conn.exec_driver_sql("SELECT id, error_detail, error_code, status FROM public.runs").all()
    return {str(row[0]): (row[1], row[2], row[3]) for row in rows}


def _matching_count(conn: sa.Connection, module: ModuleType) -> int:
    """Count rows whose ``error_detail`` still contains any of the markers."""
    markers = (module._MARKER_TRACEBACK, module._MARKER_APP_PATH, module._MARKER_FRAME)
    rows = conn.exec_driver_sql("SELECT error_detail FROM public.runs WHERE error_detail IS NOT NULL").all()
    return sum(1 for (detail,) in rows if any(marker in detail for marker in markers))


def _detail(conn: sa.Connection, row_id: str) -> str | None:
    return cast("str | None", _exec(conn, "SELECT error_detail FROM public.runs WHERE id = CAST(:id AS uuid)", {"id": row_id}).scalar_one())


def test_upgrade_redacts_matching_rows_and_leaves_everything_else_byte_identical(engine: sa.Engine, capsys: pytest.CaptureFixture[str]) -> None:
    module = _load_migration()
    _seed(engine)

    _run_upgrade(engine, module)

    with engine.connect() as conn:
        with_code = _detail(conn, _ROW_TRACEBACK_WITH_CODE)
        null_code = _detail(conn, _ROW_TRACEBACK_NULL_CODE)
        clean = _detail(conn, _ROW_CLEAN)
        near_miss = _detail(conn, _ROW_PY_NEAR_MISS)
        bare = _detail(conn, _ROW_BARE_TRACEBACK)
        before_second = _snapshot(conn)
        still_matching = _matching_count(conn, module)

    # The two matching rows are redacted to a path-free, code-derived reason.
    assert with_code == "harness.state_serialization: server-side failure; see server logs"
    assert null_code == "server-side failure; see server logs"
    # No path, no frame, no "Traceback" survives in a redacted value.
    assert "Traceback" not in with_code
    assert "/app/src/" not in with_code
    assert '.py", line ' not in with_code
    assert "Traceback" not in null_code
    # The non-matching rows are byte-identical (predicate is not broadened).
    assert clean == _CLEAN_DETAIL
    assert near_miss == _PY_NEAR_MISS
    assert bare == _BARE_TRACEBACK_WORD
    # Nothing left matches after the pass.
    assert still_matching == 0

    # error_code and status are untouched for the redacted rows.
    assert before_second[_ROW_TRACEBACK_WITH_CODE][1] == "harness.state_serialization"
    assert before_second[_ROW_TRACEBACK_WITH_CODE][2] == "failed"
    assert before_second[_ROW_TRACEBACK_NULL_CODE][1] is None
    assert before_second[_ROW_TRACEBACK_NULL_CODE][2] == "failed"

    # The number of changed rows is logged.
    captured = capsys.readouterr()
    assert "redacted error_detail on 2 run row(s)" in captured.out


def test_upgrade_is_idempotent(engine: sa.Engine) -> None:
    module = _load_migration()
    _seed(engine)

    _run_upgrade(engine, module)
    with engine.connect() as conn:
        first = _snapshot(conn)

    # A second run selects nothing and changes no row.
    _run_upgrade(engine, module)
    with engine.connect() as conn:
        second = _snapshot(conn)
        still_matching = _matching_count(conn, module)

    assert second == first
    assert still_matching == 0


def test_downgrade_is_a_safe_no_op(engine: sa.Engine) -> None:
    """The redaction is irreversible by design; downgrade must not error or mutate."""
    module = _load_migration()
    _seed(engine)
    _run_upgrade(engine, module)

    with engine.connect() as conn:
        after_upgrade = _snapshot(conn)

    module.downgrade()

    with engine.connect() as conn:
        after_downgrade = _snapshot(conn)

    assert after_downgrade == after_upgrade
