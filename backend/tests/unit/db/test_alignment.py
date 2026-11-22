"""FAR-2659: the shared database/app release alignment guard.

The guard reads the revision(s) alembic already records in
``alembic_version`` and compares them with the revisions this release ships.
A database AHEAD of the release must refuse with a legible one-liner and a
distinct status; a database behind or exactly at the release's head must
proceed so a normal forward upgrade is unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from farna.db import alignment

# A synthetic revision no release can know: this is the rollback shape (the
# newer release stamped it into alembic_version, then the old image booted).
# Deliberately NOT a real in-tree revision id, so the ahead case is genuine even
# if ``release_revision_ids`` is not monkeypatched.
_AHEAD_REVISION = "9999_far_future_release"


def _sqlite_url_with_versions(tmp_path: Path, versions: tuple[str, ...], *, table: bool = True) -> str:
    """Create a SQLite DB with an ``alembic_version`` table holding *versions*."""
    url = f"sqlite:///{tmp_path.as_posix()}/alignment.db"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            if table:
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"))
                for version in versions:
                    connection.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": version})
    finally:
        engine.dispose()
    return url


# ---------------------------------------------------------------------------
# Pure comparison
# ---------------------------------------------------------------------------


def test_ahead_database_is_not_aligned() -> None:
    """A DB revision absent from the release is the "ahead" verdict."""
    result = alignment.check_alignment({_AHEAD_REVISION}, {"0001_baseline"})

    assert result.aligned is False
    assert result.unknown_revisions == {_AHEAD_REVISION}


def test_at_head_database_is_aligned() -> None:
    result = alignment.check_alignment({"0002_head"}, {"0001_baseline", "0002_head"})

    assert result.aligned is True
    assert not result.unknown_revisions


def test_behind_database_is_aligned() -> None:
    """Behind the code is a valid state: it proceeds and migrates forward."""
    result = alignment.check_alignment({"0001_baseline"}, {"0001_baseline", "0002_head"})

    assert result.aligned is True


def test_empty_database_is_aligned() -> None:
    """A fresh database records no revision and must never be refused."""
    assert alignment.check_alignment(set(), {"0001_baseline"}).aligned is True


# ---------------------------------------------------------------------------
# Reading the database
# ---------------------------------------------------------------------------


def test_read_database_revisions_reads_recorded_versions(tmp_path: Path) -> None:
    url = _sqlite_url_with_versions(tmp_path, ("0001_baseline", "0002_head"))

    assert alignment.read_database_revisions(url) == frozenset({"0001_baseline", "0002_head"})


def test_read_database_revisions_missing_table_is_empty(tmp_path: Path) -> None:
    """No table yet = an uninitialised database, not a failure."""
    url = _sqlite_url_with_versions(tmp_path, (), table=False)

    assert not alignment.read_database_revisions(url)


def test_read_database_revisions_unreadable_raises_check_error(tmp_path: Path) -> None:
    """A real read failure is surfaced so the caller can fail open."""
    with pytest.raises(alignment.AlignmentCheckError, match="could not read alembic_version"):
        alignment.read_database_revisions(f"sqlite:///{tmp_path.as_posix()}/missing-dir/nope.db")


def test_read_database_revisions_malformed_url_raises_check_error() -> None:
    """A malformed URL is "cannot check", not an escaping ``ValueError``.

    ``make_url`` rejects a non-numeric port with a bare ``ValueError``; it must
    not escape the documented fail-open contract.
    """
    with pytest.raises(alignment.AlignmentCheckError, match="could not read alembic_version"):
        alignment.read_database_revisions("postgresql+psycopg://u:p@db:not-a-port/farna_control")


# ---------------------------------------------------------------------------
# URL translation (Postgres is the only runtime database)
# ---------------------------------------------------------------------------


def test_sync_url_translates_the_async_driver() -> None:
    assert alignment._sync_url("postgresql+asyncpg://u:p@db:5432/farna_control") == "postgresql+psycopg://u:p@db:5432/farna_control"


def test_sync_url_translates_the_legacy_postgres_alias() -> None:
    assert alignment._sync_url("postgres://u:p@db:5432/farna_control") == "postgresql+psycopg://u:p@db:5432/farna_control"


def test_sync_url_leaves_an_already_sync_url_untouched() -> None:
    url = "postgresql+psycopg://u:p@db:5432/farna_control"

    assert alignment._sync_url(url) == url


# ---------------------------------------------------------------------------
# The release's own revisions
# ---------------------------------------------------------------------------


def test_release_revisions_contains_the_current_head() -> None:
    """The release revision set must include alembic's current head."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from farna.db.health_checks import resolve_alembic_ini, resolve_migration_script_location

    ini = resolve_alembic_ini()
    config = Config(str(ini))
    config.set_main_option("script_location", str(resolve_migration_script_location(ini)))
    head = ScriptDirectory.from_config(config).get_current_head()

    assert head
    assert head in alignment.release_revision_ids()


# ---------------------------------------------------------------------------
# ensure_release_aligned
# ---------------------------------------------------------------------------


def test_ensure_release_aligned_refuses_ahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _sqlite_url_with_versions(tmp_path, (_AHEAD_REVISION,))
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline"}))

    with pytest.raises(alignment.DatabaseAheadError) as excinfo:
        alignment.ensure_release_aligned(url)

    assert _AHEAD_REVISION in str(excinfo.value)


def test_ensure_release_aligned_allows_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _sqlite_url_with_versions(tmp_path, ("0001_baseline",))
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline", "0002_head"}))

    result = alignment.ensure_release_aligned(url)

    assert result.aligned is True


# ---------------------------------------------------------------------------
# The refusal message
# ---------------------------------------------------------------------------


def test_refusal_message_names_database_revision_app_version_and_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alignment, "app_version", lambda: "9.9.9")
    result = alignment.check_alignment({_AHEAD_REVISION}, {"0001_baseline"})

    message = alignment.format_refusal(result)

    assert _AHEAD_REVISION in message
    assert "9.9.9" in message
    assert alignment.RECOVERY_COMMAND in message
    assert "\n" not in message


# ---------------------------------------------------------------------------
# main(): the ``python -m farna.db.alignment`` contract
# ---------------------------------------------------------------------------


def test_main_ahead_refuses_with_distinct_status_and_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The ahead case: distinct non-zero status, one line naming both sides."""
    url = _sqlite_url_with_versions(tmp_path, (_AHEAD_REVISION,))
    monkeypatch.setenv("DATABASE_ADMIN_URL", url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline"}))
    monkeypatch.setattr(alignment, "app_version", lambda: "9.9.9")

    status = alignment.main([])

    assert status == alignment.EXIT_DATABASE_AHEAD
    assert status != 0
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert _AHEAD_REVISION in captured.out
    assert "9.9.9" in captured.out


def test_main_at_head_proceeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _sqlite_url_with_versions(tmp_path, ("0002_head",))
    monkeypatch.setenv("DATABASE_ADMIN_URL", url)
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline", "0002_head"}))

    assert alignment.main([]) == 0


def test_main_behind_proceeds_to_the_migrate_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Behind is allowed: status 0 means ``set -e`` continues to ``alembic upgrade``.

    The entrypoint ordering (guard immediately before the migrate command) is
    pinned in ``test_alignment_entrypoints.py``.
    """
    url = _sqlite_url_with_versions(tmp_path, ("0001_baseline",))
    monkeypatch.setenv("DATABASE_ADMIN_URL", url)
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline", "0002_head"}))

    assert alignment.main([]) == 0


def test_main_prefers_admin_url_to_runtime_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrations run as the owner role; DATABASE_ADMIN_URL wins."""
    admin_url = _sqlite_url_with_versions(tmp_path, (_AHEAD_REVISION,))
    monkeypatch.setenv("DATABASE_ADMIN_URL", admin_url)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + tmp_path.as_posix() + "/unused.db")
    monkeypatch.setattr(alignment, "release_revision_ids", lambda: frozenset({"0001_baseline"}))

    assert alignment.main([]) == alignment.EXIT_DATABASE_AHEAD


def test_main_without_database_url_proceeds_with_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("DATABASE_ADMIN_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert alignment.main([]) == 0
    assert "skipping" in capsys.readouterr().err


def test_main_unreadable_database_proceeds_with_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Fail open: a check that cannot run never blocks a forward upgrade."""
    monkeypatch.setenv("DATABASE_ADMIN_URL", f"sqlite:///{tmp_path.as_posix()}/missing-dir/nope.db")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert alignment.main([]) == 0
    assert "skipped" in capsys.readouterr().err


def test_main_malformed_database_url_proceeds_with_warning(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed URL is also a fail-open skip, not an uncaught ``ValueError``."""
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql+psycopg://u:p@db:not-a-port/farna_control")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert alignment.main([]) == 0
    assert "skipped" in capsys.readouterr().err


def test_unreadable_release_tree_is_a_check_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = _sqlite_url_with_versions(tmp_path, ("0001_baseline",))

    def _boom() -> frozenset[str]:
        raise RuntimeError("tree unavailable")

    monkeypatch.setattr("farna.db.migration_guard.release_revision_ids", _boom)

    with pytest.raises(alignment.AlignmentCheckError, match="could not read the release migration tree"):
        alignment.ensure_release_aligned(url)
