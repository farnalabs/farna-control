"""ADR 070 §1 — the declared tenant scope of every table that carries ``organisation_id``.

Why this module exists
----------------------
``0351_baseline_rls.py`` shipped two policy shapes and no declaration of *which*
table is meant to have which: 93 tables got ``rls_org_isolation`` and 11 got
``rls_team_isolation``, so a new model with an ``organisation_id`` column
silently inherited org-wide visibility and nothing could tell "deliberately
org-wide" from "nobody thought about it". That is how the 93/11 split happened.

ADR 070 §1 closes it: **every tenant table declares exactly one scope** — team,
account or org_global — and org-wide visibility becomes an explicit, reviewed
fact instead of a default. ``tests/integration/test_rls_policy_gate.py`` gates
the SHAPE of a policy; this module supplies the missing half, the DECLARED
scope, and ``tests/integration/test_tenant_scope_registry.py`` fails when the
declaration and the schema disagree (a new table, a stale entry, a ``team``
entry without a team policy, an unjustified org-wide entry).

The three classes (ADR 070 §1)
------------------------------
``team``
    The resource belongs to a team. This is the default for a new tenant table
    and the only class enforced by ``rls_team_isolation`` today. The entry names
    the ownership column (``owner_team_id``) so a gate can assert it exists
    rather than trusting the declaration.
``account``
    The row is private to one account (a person), directly or through its owning
    parent. The entry names the column that carries that identity —
    ``account_id`` where the schema has one, otherwise the principal column
    (``target_user_id``, ``dismissed_by_user_id``) or the owning parent FK
    (``chat_messages.session_id``).
``org_global``
    Every member of the organisation may read the row. A deliberate, reviewed
    act: each entry carries a one-line justification, never a placeholder.
    Ledger/audit/auth/system tables have real reasons; they are written out so
    the next reviewer can judge them.

Declaring is not enforcing
--------------------------
Nothing reads this registry at runtime: ADR 070 phases 1B/2 do the flipping, and
a declaration that disagrees with the database must fail a gate rather than
change behaviour silently. The registry therefore records reality *and* intent —
an entry may name the scope the ADR has decided for a table whose policy has not
flipped yet (``runs`` is the canonical example: org-scoped today, team per ADR
070 §4), and the justification says so.

Two tables a reader might expect to find under ``account`` are declared
``org_global`` instead: ``deleted_defaults`` and ``onboarding_progress`` carry no
account or team column at all — they hold exactly one row per organisation
(``uq_deleted_defaults_org_signal``, ``uq_onboarding_progress_org``) — so
declaring them ``account`` would instruct a later phase to filter by a dimension
the row does not have.

Adding a table
--------------
Add exactly one :class:`TableScope` to :data:`_ENTRIES`. Do not default to
``org_global``: the default is ``team``, and org-wide needs a written reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

__all__ = [
    "ADR_070_TEAM_TABLE_COUNT",
    "TableScope",
    "TenantScope",
    "UnknownTenantTableError",
    "account_tables",
    "all_tables",
    "justification_of",
    "org_global_tables",
    "ownership_column",
    "registry",
    "scope_of",
    "tables_in_scope",
    "team_tables",
]

# ADR 070 §1 names the split explicitly: 11 tables carry the team predicate. A
# change to that number is a reviewed act (Phase 1B flips the runs family), so
# the number lives next to the declaration it constrains.
ADR_070_TEAM_TABLE_COUNT = 11

# An org-wide declaration must say what the data is and why it cannot be
# narrowed. The banned list is two-tier on purpose: exact matches catch a
# placeholder answer ("none", "nil"), substring markers catch the abbreviated
# forms that never occur in real prose ("n/a", "tbd"). A normal sentence may
# legitimately contain the word "none", so it is banned only as a whole answer.
_MIN_JUSTIFICATION_CHARS = 40
_BANNED_JUSTIFICATIONS = frozenset(
    {
        "",
        "-",
        "na",
        "nil",
        "none",
        "tba",
        "tbd",
        "todo",
        "not applicable",
        "to be decided",
    }
)
_BANNED_JUSTIFICATION_MARKERS = ("n/a", "tbd", "tba", "todo", "to be decided", "not applicable", "fixme", "xxx", "???")


class TenantScope(StrEnum):
    """The three scope classes of ADR 070 §1."""

    TEAM = "team"
    ACCOUNT = "account"
    ORG_GLOBAL = "org_global"


class UnknownTenantTableError(KeyError):
    """Raised when a table is not declared in the registry.

    Subclasses ``KeyError`` because the registry is a lookup: a caller asking for
    an undeclared table wants a scope that does not exist. The message names the
    table and the remedy so the failure is actionable.
    """

    def __init__(self, table: str) -> None:
        super().__init__(f"{table!r} has no declared tenant scope - add a TableScope entry to farna.db.tenant_scope._ENTRIES")


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip surrounding punctuation."""
    return " ".join(text.split()).lower().strip(" .,:;!-_")


def _validate_justification(table: str, justification: str | None) -> str:
    """Return *justification* when it is a real reason, else raise ``ValueError``."""
    if justification is None:
        raise ValueError(f"{table}: an org_global entry must carry a written justification (ADR 070 §1)")
    text = justification.strip()
    normalised = _normalise(text)
    if normalised in _BANNED_JUSTIFICATIONS:
        raise ValueError(f"{table}: justification {text!r} is a placeholder - write the actual reason this table is org-wide")
    # Markers are checked before the length floor so an abbreviated placeholder
    # ("n/a", "tbd") is reported as the placeholder it is, not as "too short".
    for marker in _BANNED_JUSTIFICATION_MARKERS:
        if marker in normalised:
            raise ValueError(f"{table}: justification contains the placeholder marker {marker!r} - write the actual reason this table is org-wide")
    if len(text) < _MIN_JUSTIFICATION_CHARS:
        raise ValueError(f"{table}: justification is {len(text)} characters - ADR 070 §1 requires a real one-line reason (>= {_MIN_JUSTIFICATION_CHARS})")
    return text


@dataclass(frozen=True, slots=True)
class TableScope:
    """The declared scope of one tenant table.

    ``scope`` is typed as :class:`TenantScope`, not ``str``, so a misspelt scope
    (``"teams"``, ``"org"``, ``"TEAM"``) raises at import instead of becoming a
    silent fourth class. ``owner_column`` is required for ``team`` (the
    ownership column) and ``account`` (the column carrying the account
    identity); ``justification`` is required for ``org_global`` and rejected
    elsewhere, so an entry can never be half-declared.
    """

    table: str
    scope: TenantScope
    owner_column: str | None = None
    justification: str | None = None

    def __post_init__(self) -> None:
        if not self.table:
            raise ValueError("TableScope.table must not be empty")
        if self.table.strip() != self.table:
            raise ValueError(f"{self.table!r}: table name must not be padded with whitespace")
        # ``TenantScope`` is a StrEnum: a bare string compares equal to a member
        # but is not an instance. This is the typo guard.
        if not isinstance(self.scope, TenantScope):
            raise TypeError(f"{self.table}: scope {self.scope!r} is not a TenantScope - use TenantScope.TEAM/ACCOUNT/ORG_GLOBAL")
        if self.scope is TenantScope.ORG_GLOBAL:
            if self.owner_column is not None:
                raise ValueError(f"{self.table}: an org_global entry names no ownership column (got {self.owner_column!r})")
            _validate_justification(self.table, self.justification)
            return
        if self.justification is not None:
            raise ValueError(f"{self.table}: only an org_global entry carries a justification - scope {self.scope.value!r} is narrowed by a column")
        if not self.owner_column:
            raise ValueError(f"{self.table}: a {self.scope.value!r} entry must name the column that carries its owner (owner_column)")
        if self.owner_column.strip() != self.owner_column:
            raise ValueError(f"{self.table}: owner_column must be a bare column name, got {self.owner_column!r}")

    @property
    def reason(self) -> str:
        """The written justification of an org_global entry; raises for other scopes."""
        if self.scope is not TenantScope.ORG_GLOBAL:
            raise ValueError(f"{self.table}: scope {self.scope.value!r} carries no justification - it is narrowed by {self.owner_column!r}")
        if self.justification is None:  # pragma: no cover - __post_init__ guarantees it
            raise ValueError(f"{self.table}: org_global entry lost its justification")
        return self.justification


# ---------------------------------------------------------------------------
# The registry — derived from the live schema, then reviewed table by table.
# ---------------------------------------------------------------------------
# Alphabetical by table so a change reads as a clean insertion. Every entry is
# one reviewed decision: never add a table without choosing a scope.
_ENTRIES: tuple[TableScope, ...] = (
    # ── team: the 11 tables that already carry rls_team_isolation ───────────
    TableScope("connector_instances", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("environment_profiles", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("eval_datasets", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("eval_suites", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("external_agents", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("library_primitives", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("lifecycle_maps", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("mcp_servers", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("model_backends", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("pipelines", TenantScope.TEAM, owner_column="owner_team_id"),
    TableScope("system_maps", TenantScope.TEAM, owner_column="owner_team_id"),
    # ── account: the row is private to one account ──────────────────────────
    # Three of these carry the identity indirectly: a message inherits its
    # session's account, a notification is addressed to target_user_id (the
    # org- and admin-scoped notifications leave it NULL and are intentionally
    # org-readable), and a dismissal names the dismissing account.
    TableScope("chat_messages", TenantScope.ACCOUNT, owner_column="session_id"),
    TableScope("chat_sessions", TenantScope.ACCOUNT, owner_column="account_id"),
    TableScope("dismissals", TenantScope.ACCOUNT, owner_column="dismissed_by_user_id"),
    TableScope("feedback_records", TenantScope.ACCOUNT, owner_column="account_id"),
    TableScope("notification_preferences", TenantScope.ACCOUNT, owner_column="account_id"),
    TableScope("notifications", TenantScope.ACCOUNT, owner_column="target_user_id"),
    TableScope("saved_views", TenantScope.ACCOUNT, owner_column="account_id"),
    # ── org_global: governance and audit ledgers ────────────────────────────
    TableScope("audit_chain_heads", TenantScope.ORG_GLOBAL, justification="Hash-chain head per organisation; per-team filtering would leak the chain through the gaps and make verification impossible (ADR 070 §6)."),
    TableScope("audit_events", TenantScope.ORG_GLOBAL, justification="Hash-chained audit ledger; compliance exports filter on actor and work-item at export time, never on the ledger's row policy (ADR 070 §6)."),
    TableScope("evidence", TenantScope.ORG_GLOBAL, justification="Append-only compliance evidence facts; a compliance export reads the organisation's evidence set whole, so a per-team predicate would produce an incomplete pack."),
    TableScope("evidence_redaction_rules", TenantScope.ORG_GLOBAL, justification="Org-level redaction policy applied to the evidence store before a fact is written; one rule set per organisation, maintained by an org admin."),
    TableScope("policy_gate_decisions", TenantScope.ORG_GLOBAL, justification="Records each evaluation of a policy gate; the assurance trail is read org-wide by admins and exporters, matching the audit ledger's scope."),
    TableScope("policy_gates", TenantScope.ORG_GLOBAL, justification="Policy-gate definitions are org-level governance configuration administered by an org admin and applied to every team's pipelines."),
    TableScope("run_evidence", TenantScope.ORG_GLOBAL, justification="Per-run evidence keyed by (run, node); it follows the run's own scope, and the runs family stays org-scoped until ADR 070 Phase 1B."),
    TableScope("spend_anomalies", TenantScope.ORG_GLOBAL, justification="Detected cost anomalies are org-level cost-control findings reviewed by an org admin across all teams, so a team predicate would hide part of the anomaly."),
    TableScope("org_daily_run_counts", TenantScope.ORG_GLOBAL, justification="Holds one org-level row plus per-team rows per day; a team predicate would hide the only correct org total or double-count it (ADR 070 §6 non-goal)."),
    TableScope("run_daily_facts", TenantScope.ORG_GLOBAL, justification="Daily run analytics facts where org-level and per-team rows coexist by design; team-scoping the rows either hides the total or counts it twice (ADR 070 §6)."),
    TableScope("cost_components", TenantScope.ORG_GLOBAL, justification="Cost-attribution ledger rows recorded per run; the ledger is read org-wide for totals, and per-team filtering would contradict the rollups it feeds (ADR 070 §6)."),
    TableScope("org_mint_budget_usage", TenantScope.ORG_GLOBAL, justification="Org mint-budget counter keyed (organisation_id, window_start) with no account or team column; it is a per-organisation quota ledger."),
    TableScope("farna_journey_facts", TenantScope.ORG_GLOBAL, justification="Per-writer journey denominator rows aggregated per organisation for the journey metrics; they are analytics facts, not records any single person owns."),
    TableScope("metrics_staging", TenantScope.ORG_GLOBAL, justification="Staging buffer for product-analytics events between ingest and the daily dump cron; operational plumbing with no user-facing reader to scope to."),
    TableScope("web_vital_events", TenantScope.ORG_GLOBAL, justification="Frontend web-vital telemetry ingested for the whole organisation and read only by operational aggregation, so no narrower scope is meaningful."),
    # ── org_global: auth and pre-principal ──────────────────────────────────
    TableScope("invitations", TenantScope.ORG_GLOBAL, justification="Invitation rows are consumed before the invitee holds any principal or organisation context, so they are deliberately outside the RLS tenant set."),
    TableScope("mcp_setup_tokens", TenantScope.ORG_GLOBAL, justification="Short-lived one-time setup tokens for the MCP credential flow, bound to an organisation and redeemed before any team identity exists."),
    TableScope("oauth_authorization_codes", TenantScope.ORG_GLOBAL, justification="OAuth authorization codes are short-lived pre-auth artefacts read during the token exchange, before an organisation context is bound to the session."),
    TableScope("oauth_clients", TenantScope.ORG_GLOBAL, justification="OAuth client registrations are read during the pre-auth flow before an organisation context exists; migration 0367 replaced the fail-open arm with the transaction-scoped app.bootstrap_context grant."),
    TableScope("oauth_consent_states", TenantScope.ORG_GLOBAL, justification="OAuth consent state is read during the pre-auth consent round-trip, before any organisation context exists; the read uses the transaction-scoped bootstrap grant added by migration 0367."),
    TableScope("oauth_token_families", TenantScope.ORG_GLOBAL, justification="OAuth token-rotation families keyed by client_id; they are consulted during token refresh, which runs before a team identity is available."),
    TableScope("token_families", TenantScope.ORG_GLOBAL, justification="Refresh-token rotation families are consulted during token refresh, before a team identity exists; migration 0367 replaced the fail-open arm with the transaction-scoped bootstrap grant."),
    TableScope("org_api_keys", TenantScope.ORG_GLOBAL, justification="Organisation-scoped API keys are deliberately team-blind within their own organisation (ADR 070 §5); account_id records the minting principal, not the reader."),
    TableScope("publishers", TenantScope.ORG_GLOBAL, justification="Publisher identities for library primitives are org-level catalogue metadata named in ADR 070 §6; a team predicate would make a published primitive unresolvable."),
    # ── org_global: organisation identity and membership ────────────────────
    TableScope("teams", TenantScope.ORG_GLOBAL, justification="Teams are the separation boundary itself (ADR 070 §6): the roster must be readable within the org for the team predicate and every membership lookup to resolve."),
    TableScope("team_memberships", TenantScope.ORG_GLOBAL, justification="Team membership is the table the team predicate itself joins (ADR 070 §6); scoping it per team would make the policy evaluate against a filtered set."),
    TableScope("org_memberships", TenantScope.ORG_GLOBAL, justification="Organisation membership is read to resolve a principal's role before any team identity exists, and it is the table that grants team membership (ADR 070 §6)."),
    TableScope("sso_providers", TenantScope.ORG_GLOBAL, justification="SSO provider configuration is org-level identity config: one identity provider per organisation, read during login before any team is known (ADR 070 §6)."),
    # ── org_global: organisation configuration and shared catalogues ────────
    TableScope("agents", TenantScope.ORG_GLOBAL, justification="Agent definitions are org-wide reusable resources; ADR 070 defers them to Phase 2 because a team-owned pipeline may bind an org-visible agent, so the scope cannot flip first."),
    TableScope("secrets", TenantScope.ORG_GLOBAL, justification="Secrets are org-level credentials; ADR 070 Phase 2 requires the binding-visibility pattern before any flip, since a team pipeline binding an invisible secret is a correctness problem."),
    TableScope("schemas", TenantScope.ORG_GLOBAL, justification="Schemas are org-wide reusable definitions; ADR 070 defers schemas and parameters to Phase 2 alongside the secrets and agents product calls."),
    TableScope("schema_versions", TenantScope.ORG_GLOBAL, justification="Schema versions are children of an org-wide schema definition, so a team predicate on the child would contradict its parent's declared scope (Phase 2)."),
    TableScope("schema_folders", TenantScope.ORG_GLOBAL, justification="Schema folders are org-level navigation for the schema list; the tree is browsed by every team and account_id records the creator only (Phase 2 with schemas)."),
    TableScope("parameter_schemas", TenantScope.ORG_GLOBAL, justification="Parameter schemas are org-wide reusable definitions, deferred with schemas to ADR 070 Phase 2; every pipeline in the org may reference one."),
    TableScope("parameter_sets", TenantScope.ORG_GLOBAL, justification="Parameter sets belong to an org-wide parameter schema, so narrowing the child would contradict the parent's declared scope (Phase 2)."),
    TableScope("composite_templates", TenantScope.ORG_GLOBAL, justification="Org-wide reusable template catalogue: a template is composed into pipelines of any team, so a team predicate would hide it from the pipelines that use it."),
    TableScope("connector_profiles", TenantScope.ORG_GLOBAL, justification="Runtime profile (base URL, method, auth shape) of a connector instance: a 1:1 configuration child whose visibility follows its connector, with no team column of its own."),
    TableScope("node_categories", TenantScope.ORG_GLOBAL, justification="Node catalogue taxonomy named in ADR 070 §6; a built-in org-wide vocabulary that every pipeline editor reads."),
    TableScope("node_observations", TenantScope.ORG_GLOBAL, justification="Per-node observation counters feeding the org-wide node-health aggregation, which would be incomplete if the underlying rows were team-partitioned."),
    TableScope("collection_install", TenantScope.ORG_GLOBAL, justification="Install receipts for org library collections: installation is org-wide state (one install serves every team), so a team predicate would hide an installed collection."),
    TableScope("primitive_ratings", TenantScope.ORG_GLOBAL, justification="Ratings on org library primitives; account_id records the rater while the aggregate is read org-wide, so a narrower scope would break the rating it feeds."),
    TableScope("primitive_abuse_reports", TenantScope.ORG_GLOBAL, justification="Library moderation reports are reviewed by admins across the whole catalogue, so the queue must not be partitioned by the reporter's or the primitive's team."),
    TableScope("variant_groups", TenantScope.ORG_GLOBAL, justification="Variant group definitions are org-level experiment configuration attached to a pipeline, read by the experiment surfaces for the whole organisation."),
    TableScope("variant_batch_state", TenantScope.ORG_GLOBAL, justification="Persistence row for an A/B variant batch: batches are org-level experiment bookkeeping, and batches predating FAR-775 have no stored row at all."),
    # ── org_global: pipeline graph children of a team-owned parent ──────────
    # These carry no team column, so they stay on the org gate until ADR 070
    # Phase 1B denormalises (or joins) the parent's owner.
    TableScope("pipeline_edges", TenantScope.ORG_GLOBAL, justification="Pipeline graph edges are children of a team-owned pipeline; the child has no team column, so it stays org-gated until Phase 1B denormalises the parent's owner."),
    TableScope("pipeline_snapshots", TenantScope.ORG_GLOBAL, justification="Immutable graphs captured at trigger time and read by the executor team-blind; the row has no team column, so it follows the org gate until Phase 1B."),
    TableScope("snapshot_schema_pins", TenantScope.ORG_GLOBAL, justification="Schema pins recorded on a pipeline snapshot; they follow the snapshot's org gate until ADR 070 Phase 1B gives the runs family a team predicate."),
    TableScope("pipeline_folders", TenantScope.ORG_GLOBAL, justification="Pipeline folders are org-level navigation for the pipeline list; account_id records the creator, and every team browses the same folder tree."),
    TableScope("nodes", TenantScope.ORG_GLOBAL, justification="Legacy pipeline-node table, deprecated outside composite templates; it has no team column and is org-gated until the composite-template migration removes it."),
    TableScope("eval_cases", TenantScope.ORG_GLOBAL, justification="Eval dataset cases: the parent eval_datasets row is team-scoped, and the child carries no team column, so it stays org-gated until the parent's scope is denormalised."),
    TableScope("eval_results", TenantScope.ORG_GLOBAL, justification="Per-case eval results attached to a run; they follow the run's scope, and the runs family stays org-scoped until ADR 070 Phase 1B."),
    TableScope("evals", TenantScope.ORG_GLOBAL, justification="Eval definitions are org-wide library resources: they carry no visibility or owner column yet, so ADR 070 defers their narrowing to Phase 2."),
    TableScope("suite_runs", TenantScope.ORG_GLOBAL, justification="One execution of an eval suite; it carries owner_team_id for attribution but is recorded evidence read org-wide by the suite surfaces and reports."),
    TableScope("lifecycle_map_stages", TenantScope.ORG_GLOBAL, justification="Stage rows of a lifecycle map: the parent lifecycle_maps row is team-scoped and the child has no team column, so it stays org-gated until Phase 1B denormalises the parent."),
    TableScope("system_map_attribution_bindings", TenantScope.ORG_GLOBAL, justification="Attribution bindings of a system map: the parent system_maps row is team-scoped and the child has no team column, so it stays org-gated until the parent scope is denormalised."),
    TableScope("system_map_loop_admission_attempts", TenantScope.ORG_GLOBAL, justification="Loop admission attempts for a system map: child of a team-scoped parent with no team column, so it stays org-gated until Phase 1B denormalises the parent."),
    TableScope("system_map_loop_configs", TenantScope.ORG_GLOBAL, justification="Loop configuration for a system map: child of a team-scoped parent with no team column, so it stays org-gated until the parent scope is denormalised."),
    TableScope("system_map_loop_reservations", TenantScope.ORG_GLOBAL, justification="Loop budget reservations for a system map: child of a team-scoped parent with no team column, so it stays org-gated until the parent scope is denormalised."),
    TableScope("system_map_nodes", TenantScope.ORG_GLOBAL, justification="Node rows of a system map: they carry owner_team_id for attribution but the map's nodes are read whole by the map surfaces, whose parent is team-scoped."),
    TableScope("system_map_relationships", TenantScope.ORG_GLOBAL, justification="Relationship edges between system-map nodes: child of a team-scoped parent with no team column, so it stays org-gated until the parent scope is denormalised."),
    TableScope("system_map_revisions", TenantScope.ORG_GLOBAL, justification="Immutable revisions of a system map: child of a team-scoped parent with no team column, so it stays org-gated until the parent scope is denormalised."),
    # ── org_global: the runs family (org-scoped until ADR 070 Phase 1B) ─────
    TableScope("runs", TenantScope.ORG_GLOBAL, justification="The runs family is org-scoped until ADR 070 §4 flips it to the team predicate in Phase 1B; the current scope is pinned by tests/integration/test_rls_policy_gate.py."),
    TableScope("run_attempts", TenantScope.ORG_GLOBAL, justification="Run attempt rows follow the run's scope; the runs family is org-scoped until Phase 1B and the child carries no team column."),
    TableScope("run_node_outputs", TenantScope.ORG_GLOBAL, justification="Node outputs of a run; read team-blind by the executor and by the run surfaces, which are org-scoped until ADR 070 Phase 1B."),
    TableScope("run_node_outputs_quarantine", TenantScope.ORG_GLOBAL, justification="Ops-only quarantine of oversized or unparseable node outputs, copied aside for remediation; deliberately outside the RLS tenant set."),
    TableScope("run_number_counters", TenantScope.ORG_GLOBAL, justification="Per-organisation run-number allocator (ADR 070 §6): a team predicate would break the org-wide monotonic counter that every run insert consumes."),
    TableScope("journeys", TenantScope.ORG_GLOBAL, justification="One journey row per (organisation, kind, ref) work item, minted by a unique constraint on the org key; it carries owner_team_id for attribution but stays org-level so history survives a team change."),
    TableScope("triggers", TenantScope.ORG_GLOBAL, justification="Triggers are org-gated children of pipelines today; a team predicate needs the pipeline's owner denormalised, which has not landed (Phase 2)."),
    TableScope("trigger_events", TenantScope.ORG_GLOBAL, justification="Trigger ingress ledger: every delivery is recorded for the organisation so operators can audit webhook and cron traffic; account_id records the actor."),
    TableScope("trigger_event_findings", TenantScope.ORG_GLOBAL, justification="Guardrail findings attached to a trigger event: child of the org-level trigger-event ledger with no team column."),
    TableScope("webhook_dedup_hashes", TenantScope.ORG_GLOBAL, justification="Webhook dedupe hashes named in ADR 070 §6; the dedupe window is per trigger across the whole organisation, so a team predicate would re-admit a duplicate."),
    TableScope("webhook_payloads", TenantScope.ORG_GLOBAL, justification="Raw webhook payload retention for replay and debugging, read org-wide by the trigger inspector; the row carries no team column."),
    # ── org_global: notifications, errors and operational telemetry ─────────
    TableScope("notification_endpoints", TenantScope.ORG_GLOBAL, justification="Webhook endpoints are org-level integration config, optionally tagged with team_id and account_id for routing; a team predicate would hide endpoints the org shares."),
    TableScope("notification_delivery_log", TenantScope.ORG_GLOBAL, justification="Delivery-attempt audit log for notification endpoints: operators inspect one org-wide delivery history, and the row carries no account or team column."),
    TableScope("error_events", TenantScope.ORG_GLOBAL, justification="Error telemetry stream; ADR 070 §6 names the error_* tables as org_global because operators triage one org-wide feed and group signatures globally."),
    TableScope("error_groups", TenantScope.ORG_GLOBAL, justification="Aggregated error groups; the group is a de-duplicated org-wide signature, so a per-team filter would fragment one incident into several."),
    TableScope("error_forwarder_configs", TenantScope.ORG_GLOBAL, justification="Org-level error-forwarder configuration: one forwarder set per organisation, administered by an org admin, with no team or account dimension."),
    TableScope("error_notification_rules", TenantScope.ORG_GLOBAL, justification="Org-level alert rules over the error feed (ADR 070 §6 names the error_* tables), administered per organisation rather than per team."),
    TableScope("runner_probe_cache", TenantScope.ORG_GLOBAL, justification="Cached runner-availability probes refreshed by the system worker for the whole organisation; there is no per-user reader to scope to."),
    TableScope("agent_runner_bindings", TenantScope.ORG_GLOBAL, justification="Runner environment bindings are org-level provisioning config attached to an agent; account_id records the creator, and every run of the org must resolve the same bindings."),
    TableScope("assistant_context_sources", TenantScope.ORG_GLOBAL, justification="Global assistant asset catalogue: built-in NULL-organisation rows are readable by every organisation, recorded as org_global in ADR 070 §6."),
    TableScope("assistant_skills", TenantScope.ORG_GLOBAL, justification="Global assistant skill catalogue: built-in NULL-organisation rows are readable by every organisation, recorded as org_global in ADR 070 §6."),
    TableScope("hitl_claims", TenantScope.ORG_GLOBAL, justification="HITL claim records attached to a run's gate: the gate is org-visible so any eligible reviewer can claim it, and the row follows the run's scope until Phase 1B."),
    TableScope("scheduled_reports", TenantScope.ORG_GLOBAL, justification="Scheduled report definitions are org-level delivery config deferred to ADR 070 Phase 2; account_id records the creator, and the schedule is served by the system worker."),
    TableScope("deleted_defaults", TenantScope.ORG_GLOBAL, justification="Tombstone for a deleted default alert rule, keyed UNIQUE(organisation_id, signal) with no account column: the default rule set is org configuration, so the tombstone cannot be narrower."),
    TableScope("onboarding_progress", TenantScope.ORG_GLOBAL, justification="Onboarding checklist state keyed UNIQUE(organisation_id) alone with no account column: there is exactly one row per organisation, so no account dimension exists to declare."),
    # ── org_global: runtime-managed execution state ─────────────────────────
    TableScope("checkpoints", TenantScope.ORG_GLOBAL, justification="LangGraph checkpoint header rows written by the graph saver, not the application; they carry no account or team column and are keyed by thread and checkpoint id."),
    TableScope("checkpoint_blobs", TenantScope.ORG_GLOBAL, justification="LangGraph checkpoint payload blobs written by the graph saver for a checkpoint; runtime-managed state with no account or team column to scope on."),
    TableScope("checkpoint_writes", TenantScope.ORG_GLOBAL, justification="LangGraph pending-write rows written by the graph saver during checkpointing; runtime-managed state with no account or team column to scope on."),
)


def _validate_registry(entries: tuple[TableScope, ...]) -> Mapping[str, TableScope]:
    """Build the lookup and refuse a duplicate table declaration.

    A duplicate would otherwise be resolved silently by dict construction, which
    is exactly the "two declarations, one wins" failure this registry exists to
    prevent.
    """
    by_table: dict[str, TableScope] = {}
    for entry in entries:
        if entry.table in by_table:
            raise ValueError(f"{entry.table}: declared more than once in the tenant-scope registry - each table has exactly one scope (ADR 070 §1)")
        by_table[entry.table] = entry
    return MappingProxyType(by_table)


_REGISTRY: Mapping[str, TableScope] = _validate_registry(_ENTRIES)


def registry() -> Mapping[str, TableScope]:
    """The whole registry, read-only, keyed by table name."""
    return _REGISTRY


def all_tables() -> frozenset[str]:
    """Every table the registry declares a scope for."""
    return frozenset(_REGISTRY)


def _entry(table: str) -> TableScope:
    try:
        return _REGISTRY[table]
    except KeyError:
        raise UnknownTenantTableError(table) from None


def scope_of(table: str) -> TenantScope:
    """The declared scope of *table*; raises :class:`UnknownTenantTableError` if undeclared."""
    return _entry(table).scope


def tables_in_scope(scope: TenantScope) -> frozenset[str]:
    """Every table declared with *scope*."""
    if not isinstance(scope, TenantScope):
        raise TypeError(f"{scope!r} is not a TenantScope")
    return frozenset(table for table, entry in _REGISTRY.items() if entry.scope is scope)


def team_tables() -> frozenset[str]:
    """Every table declared ``team``."""
    return tables_in_scope(TenantScope.TEAM)


def account_tables() -> frozenset[str]:
    """Every table declared ``account``."""
    return tables_in_scope(TenantScope.ACCOUNT)


def org_global_tables() -> frozenset[str]:
    """Every table declared ``org_global``."""
    return tables_in_scope(TenantScope.ORG_GLOBAL)


def ownership_column(table: str) -> str:
    """The column carrying *table*'s owner (the team, or the account identity)."""
    entry = _entry(table)
    if entry.owner_column is None:
        raise ValueError(f"{table}: declared {entry.scope.value!r} and therefore has no ownership column")
    return entry.owner_column


def justification_of(table: str) -> str:
    """The written justification of an ``org_global`` table."""
    return _entry(table).reason
