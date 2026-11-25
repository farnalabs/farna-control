"""Unit tests for ``farna.db.tenant_scope`` (ADR 070 §1).

The integration module ``tests/integration/test_tenant_scope_registry.py`` binds
the registry to the live catalog. These unit tests cover the registry's own
pure logic - the dataclass validators, the justification gate, the lookups and
the helpers - so the module is exercised by the unit suite (the changed-lines
coverage gate measures the unit shards, not the integration run).
"""

from __future__ import annotations

import pytest

from farna.db import tenant_scope
from farna.db.tenant_scope import (
    ADR_070_TEAM_TABLE_COUNT,
    TableScope,
    TenantScope,
    UnknownTenantTableError,
)

# A real one-line reason: >= the length floor and free of placeholder markers.
_VALID_JUSTIFICATION = "Every team in the organisation reads this catalogue, so a narrower scope would hide rows."


# ---------------------------------------------------------------------------
# The scope enum
# ---------------------------------------------------------------------------


def test_tenant_scope_has_the_three_adr_classes() -> None:
    assert {scope.value for scope in TenantScope} == {"team", "account", "org_global"}


# ---------------------------------------------------------------------------
# The lookup contract
# ---------------------------------------------------------------------------


def test_unknown_table_raises_the_typed_error() -> None:
    with pytest.raises(UnknownTenantTableError, match="no declared tenant scope"):
        tenant_scope.scope_of("definitely_not_a_table")


def test_unknown_table_error_is_a_key_error() -> None:
    assert issubclass(UnknownTenantTableError, KeyError)
    with pytest.raises(KeyError):
        tenant_scope.scope_of("definitely_not_a_table")


def test_unknown_table_error_names_the_table_and_remedy() -> None:
    with pytest.raises(UnknownTenantTableError) as excinfo:
        tenant_scope.scope_of("definitely_not_a_table")
    message = str(excinfo.value)
    assert "definitely_not_a_table" in message
    assert "_ENTRIES" in message


def test_scope_of_returns_the_declared_scope() -> None:
    assert tenant_scope.scope_of("pipelines") is TenantScope.TEAM
    assert tenant_scope.scope_of("runs") is TenantScope.ORG_GLOBAL


def test_registry_is_a_read_only_mapping() -> None:
    reg = tenant_scope.registry()
    assert reg["pipelines"].scope is TenantScope.TEAM
    with pytest.raises(TypeError):
        reg["pipelines"] = None  # type: ignore[index]


def test_all_tables_matches_the_registry_keys() -> None:
    assert tenant_scope.all_tables() == frozenset(tenant_scope.registry())


# ---------------------------------------------------------------------------
# The three scope collections
# ---------------------------------------------------------------------------


def test_scope_collections_partition_the_registry() -> None:
    team = tenant_scope.team_tables()
    account = tenant_scope.account_tables()
    org_global = tenant_scope.org_global_tables()
    assert not (team & account)
    assert not (team & org_global)
    assert not (account & org_global)
    assert team | account | org_global == tenant_scope.all_tables()


def test_team_table_count_matches_the_adr() -> None:
    assert len(tenant_scope.team_tables()) == ADR_070_TEAM_TABLE_COUNT


def test_tables_in_scope_returns_a_frozenset() -> None:
    assert tenant_scope.tables_in_scope(TenantScope.TEAM) == tenant_scope.team_tables()


def test_tables_in_scope_rejects_a_bare_string() -> None:
    with pytest.raises(TypeError, match="is not a TenantScope"):
        tenant_scope.tables_in_scope("team")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ownership_column / justification_of helpers
# ---------------------------------------------------------------------------


def test_ownership_column_returns_the_declared_column() -> None:
    assert tenant_scope.ownership_column("pipelines") == "owner_team_id"
    assert tenant_scope.ownership_column("chat_sessions") == "account_id"


def test_ownership_column_refuses_a_columnless_class() -> None:
    with pytest.raises(ValueError, match="no ownership column"):
        tenant_scope.ownership_column("runs")


def test_justification_of_returns_the_written_reason() -> None:
    assert tenant_scope.justification_of("runs") == tenant_scope.registry()["runs"].justification


def test_justification_of_refuses_a_narrowed_class() -> None:
    with pytest.raises(ValueError, match="carries no justification"):
        tenant_scope.justification_of("pipelines")


# ---------------------------------------------------------------------------
# TableScope.__post_init__ validation
# ---------------------------------------------------------------------------


def test_table_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        TableScope("", TenantScope.TEAM, owner_column="owner_team_id")


def test_table_name_must_not_be_padded() -> None:
    with pytest.raises(ValueError, match="must not be padded"):
        TableScope(" pipelines ", TenantScope.TEAM, owner_column="owner_team_id")


def test_a_bare_string_scope_is_rejected() -> None:
    with pytest.raises(TypeError, match="is not a TenantScope"):
        TableScope("pipelines", "team")  # type: ignore[arg-type]


def test_a_team_entry_needs_an_ownership_column() -> None:
    with pytest.raises(ValueError, match="must name the column"):
        TableScope("pipelines", TenantScope.TEAM)


def test_an_ownership_column_must_not_be_padded() -> None:
    with pytest.raises(ValueError, match="must be a bare column name"):
        TableScope("pipelines", TenantScope.TEAM, owner_column=" owner_team_id ")


def test_a_valid_team_entry_is_accepted() -> None:
    entry = TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id")
    assert entry.scope is TenantScope.TEAM
    assert entry.owner_column == "owner_team_id"


def test_a_valid_account_entry_is_accepted() -> None:
    entry = TableScope("chat_sessions", TenantScope.ACCOUNT, owner_column="account_id")
    assert entry.scope is TenantScope.ACCOUNT


def test_a_narrowed_entry_rejects_a_justification() -> None:
    with pytest.raises(ValueError, match="only an org_global entry"):
        TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id", justification=_VALID_JUSTIFICATION)


def test_an_org_global_entry_names_no_ownership_column() -> None:
    with pytest.raises(ValueError, match="names no ownership column"):
        TableScope("runs", TenantScope.ORG_GLOBAL, owner_column="owner_team_id", justification=_VALID_JUSTIFICATION)


def test_a_valid_org_global_entry_is_accepted() -> None:
    entry = TableScope("runs", TenantScope.ORG_GLOBAL, justification=_VALID_JUSTIFICATION)
    assert entry.scope is TenantScope.ORG_GLOBAL
    assert entry.reason == _VALID_JUSTIFICATION


# ---------------------------------------------------------------------------
# The justification gate
# ---------------------------------------------------------------------------


def test_an_org_global_entry_needs_a_justification() -> None:
    with pytest.raises(ValueError, match="must carry a written justification"):
        TableScope("runs", TenantScope.ORG_GLOBAL)


def test_a_placeholder_justification_is_rejected() -> None:
    with pytest.raises(ValueError, match="placeholder"):
        TableScope("runs", TenantScope.ORG_GLOBAL, justification="none")


def test_a_placeholder_marker_is_rejected() -> None:
    with pytest.raises(ValueError, match="placeholder marker"):
        TableScope("runs", TenantScope.ORG_GLOBAL, justification="n/a")


def test_a_too_short_justification_is_rejected() -> None:
    with pytest.raises(ValueError, match="characters"):
        TableScope("runs", TenantScope.ORG_GLOBAL, justification="it is shared")


# ---------------------------------------------------------------------------
# TableScope.reason
# ---------------------------------------------------------------------------


def test_reason_returns_the_justification_for_org_global() -> None:
    entry = TableScope("runs", TenantScope.ORG_GLOBAL, justification=_VALID_JUSTIFICATION)
    assert entry.reason == _VALID_JUSTIFICATION


def test_reason_refuses_a_narrowed_entry() -> None:
    entry = TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id")
    with pytest.raises(ValueError, match="carries no justification"):
        _ = entry.reason


# ---------------------------------------------------------------------------
# _validate_registry
# ---------------------------------------------------------------------------


def test_a_duplicate_declaration_is_rejected() -> None:
    duplicate = (
        TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id"),
        TableScope("pipelines", TenantScope.ORG_GLOBAL, justification=_VALID_JUSTIFICATION),
    )
    with pytest.raises(ValueError, match="declared more than once"):
        tenant_scope._validate_registry(duplicate)


def test_validate_registry_builds_a_read_only_mapping() -> None:
    entries = (
        TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id"),
        TableScope("runs", TenantScope.ORG_GLOBAL, justification=_VALID_JUSTIFICATION),
    )
    mapping = tenant_scope._validate_registry(entries)
    assert set(mapping) == {"pipelines", "runs"}
    with pytest.raises(TypeError):
        mapping["pipelines"] = None  # type: ignore[index]
