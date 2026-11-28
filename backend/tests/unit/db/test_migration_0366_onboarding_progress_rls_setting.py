"""0366 - ``onboarding_progress`` fails CLOSED on an unset org context.

The baseline policy used the ONE-argument ``current_setting(name)``, which RAISES
``unrecognized configuration parameter`` when the GUC was never set, instead of
matching nothing. These tests pin:

* the replacement policy uses the two-argument form plus the ``NULLIF(..., '')``
  empty-string normalisation, exactly like every sibling org-isolation policy;
* the downgrade restores the baseline text verbatim (so the migration round-trips);
* ``onboarding_progress`` was the ONLY table in the baseline with the raising
  shape - the guard that stops the divergence reappearing elsewhere.

The behavioural proof (zero rows instead of an error, on real Postgres, before and
after) is ``tests/integration/db/test_runs_family_owner_team.py``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic import op

_VERSIONS = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions"
_MIGRATION_PATH = _VERSIONS / "0366_onboarding_progress_rls_setting.py"


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migration_0366_onboarding_progress_rls_setting", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _capture(monkeypatch: pytest.MonkeyPatch, run_name: str) -> list[str]:
    module = _load_migration()
    executed: list[str] = []
    monkeypatch.setattr(op, "execute", executed.append)
    getattr(module, run_name)()
    return executed


def test_upgrade_recreates_the_policy_with_a_missing_ok_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _capture(monkeypatch, "upgrade")
    assert executed[0].startswith("DROP POLICY IF EXISTS rls_org_isolation")
    policy = executed[1]
    assert "current_setting('app.organisation_id'::text, true)" in policy
    # Zero rows, not an error: the setting is NULLIF-normalised to NULL, never
    # raised on. The empty-string arm matters too - the pool-checkout reset writes
    # '' rather than unsetting the GUC.
    assert "NULLIF(current_setting('app.organisation_id'::text, true), ''::text)" in policy


def test_downgrade_restores_the_baseline_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The migration is reversible: the exact baseline text comes back."""
    module = _load_migration()
    baseline = (_VERSIONS / "0351_baseline_rls.py").read_text(encoding="utf-8")
    legacy_line = "CREATE POLICY rls_org_isolation ON public.onboarding_progress USING ((organisation_id = (current_setting('app.organisation_id'::text))::uuid));"
    assert legacy_line in baseline, "the downgrade must restore what 0351 actually shipped"

    executed = _capture(monkeypatch, "downgrade")
    assert executed[1] == legacy_line
    assert legacy_line == module.LEGACY_POLICY_SQL


def test_onboarding_progress_was_the_only_raising_org_policy_in_the_baseline() -> None:
    """No OTHER sibling policy carries the one-argument shape.

    A single-argument ``current_setting`` inside an org-isolation policy is the
    fail-loud shape this migration fixes; the guard fails if it ever appears on a
    second table without its own fix.
    """
    baseline = (_VERSIONS / "0351_baseline_rls.py").read_text(encoding="utf-8")
    raising = [line for line in baseline.splitlines() if "CREATE POLICY rls_org_isolation ON public." in line and "current_setting('app.organisation_id'::text, true)" not in line and "current_setting('app.organisation_id'::text))" in line]
    assert len(raising) == 1
    assert "public.onboarding_progress" in raising[0]
