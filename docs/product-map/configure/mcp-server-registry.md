---
id: feat-mcp-server-registry
prd: N/A
adr: [ADR 055]
code:
  - backend/src/farna/api/routes/mcp_servers.py
  - backend/src/farna/core/mcp/tool_pinning.py
  - backend/src/farna/db/crud/mcp_server.py
  - backend/src/farna/db/models/mcp_server.py
  - backend/src/farna/db/migrations/versions/0350_baseline_schema.py
  - backend/src/farna/db/migrations/versions/0351_baseline_rls.py
  - backend/src/farna/auth/permissions.py
  - backend/src/farna/auth/secret_storage.py
  - backend/src/farna/api/team_scope.py
unit-tests:
  - backend/tests/unit/api/routes/test_far1692_registry_routes.py
  - backend/tests/unit/core/test_mcp_tool_pinning.py
  - backend/tests/unit/db/test_external_agent_mcp_server_crud.py
  - backend/tests/integration/test_rls_isolation.py
bdd:
depends-on: [feat-teams]
status: partial
---

# MCP Server Registry

An org-scoped inventory of the customer-side MCP servers their agents consume,
plus the tool-definition pin that records whether a server's tools still match
what an operator approved. This is the reverse direction from the existing
"Farna Control as an MCP server" surface: these rows describe servers the
customer runs. It is policy substrate and a record, not enforcement (ADR 055):
Farna Control cannot police a client it does not control, so the honest posture
is detect and surface. The surface is REST only (`/api/v1/mcp-servers`), with no
manifest route, no admin page and no MCP tools, so it is tracked here rather
than in the manifest `features:` registry. It ships FAR-1692 S1 (inventory) and
S2 (tool-definition pinning).

## Behaviors

- [x] CRUD over `/api/v1/mcp-servers`: `GET ""` lists paginated rows
      (`page` >= 1, `page_size` 1..100, ordered `created_at` DESC then `id`
      DESC, envelope `items`/`total`/`page`/`page_size`), `POST ""` creates and
      returns 201, `GET /{server_id}` reads one, `PATCH /{server_id}` updates,
      and `DELETE /{server_id}` soft-deletes and returns 204
      (`api/routes/mcp_servers.py`, `db/crud/mcp_server.py`;
      `test_far1692_registry_routes.py`).
- [x] The create shape is constrained: `name` 1..255, `transport` one of
      `stdio` / `sse` / `http` / `websocket`, `endpoint` and `command` <= 2000,
      `declared_scopes` a list of strings, optional `credentials`, and
      `visibility` one of `org` / `team` (default `org`). The creator's account
      is stamped as `owner_account_id`, and the model enforces the transport /
      approval-status / visibility vocabulary plus `owner_team_id NOT NULL`
      whenever `visibility = 'team'` (`db/models/mcp_server.py`,
      `0350_baseline_schema.py`).
- [x] A supplied credential is encrypted at rest with Fernet through the shared
      `encrypt_stored_secret` helper and is never returned: the response carries
      only `has_credentials`. A partial update that omits `credentials` leaves
      the stored ciphertext untouched, while a supplied value is re-encrypted
      (`api/routes/mcp_servers.py`, `auth/secret_storage.py`;
      `test_far1692_registry_routes.py::test_create_mcp_server_success_with_credentials`,
      `::test_update_mcp_server_with_credentials`).
- [x] Access is permission-floored per verb: `mcp_server.list` (viewer floor)
      for list and read, `mcp_server.create` / `.update` / `.delete` (operator
      floor) for inventory mutations, and `mcp_server.approve` (operator floor)
      for approving a tool set (`auth/permissions.py`;
      `test_far1692_registry_routes.py`).
- [x] Reads and writes are org-scoped twice: the route sets the RLS org and
      user context, and every CRUD query carries an explicit
      `organisation_id` predicate, so a cross-org read answers 404 instead of
      disclosing the row. Team visibility is a DB-RLS boundary through
      `rls_team_isolation`, matching the core team-scoped tables' DB-RLS-only
      posture (FAR-1514); the `team_scope` resolver is registered for registry
      completeness only and is not wired into these routes
      (`db/crud/mcp_server.py`, `0351_baseline_rls.py`, `api/team_scope.py`;
      `test_external_agent_mcp_server_crud.py`, `test_rls_isolation.py`).
- [x] Team assignment is validated at the write: create refuses an
      `owner_team_id` the caller cannot reach, and a visibility or owner-team
      change goes through the shared update-transition guard
      (`api/team_scope.py`; `test_far1692_registry_routes.py`).
- [x] Create, update and delete emit `mcp_server_created` /
      `mcp_server_updated` / `mcp_server_deleted` audit events through the
      shared `audited()` dependency on the `mcp_server` resource
      (`api/routes/mcp_servers.py`; `test_far1692_registry_routes.py`).
- [x] The tool-definition hash is canonical and idempotent: it folds each tool
      to its identity projection (`name`, `title`, `description`,
      `inputSchema`, accepting the `input_schema` alias), sorts the projection
      so list order and volatile server metadata cannot change the digest, and
      SHA-256s the canonical JSON. A tool without a non-empty string `name`, or
      a non-mapping entry, raises `ToolPinningError` and is never persisted
      (`core/mcp/tool_pinning.py`; `test_mcp_tool_pinning.py`).
- [x] `POST /{server_id}/observe-tools` records the observed set (capped at
      1000 tool definitions, oversized list 422), stores
      `tools_json`/`tools_hash`/`last_observed_at`, recomputes the hash and
      applies the drift decision. A malformed set is 422 and the request is
      permission-checked at `mcp_server.update`
      (`core/mcp/tool_pinning.py`, `api/routes/mcp_servers.py`;
      `test_far1692_registry_routes.py::test_observe_tools_success`,
      `::test_observe_tools_malformed_tools`,
      `::test_observe_tools_rejects_oversized_tool_list`).
- [x] The drift decision is fail-safe and honest: with no recorded
      `approved_hash` the status stays `unapproved` (a defensive
      `approved`/`approved-stale` without a hash resets to `unapproved`); a
      hash equal to `approved_hash` is `approved` and clears a previous
      `approved-stale`; any other hash flips to `approved-stale` with
      `drifted=true`; and a `blocked` server is never downgraded by drift and
      raises no drift alert (`core/mcp/tool_pinning.py`;
      `test_mcp_tool_pinning.py::test_unapproved_observation_stays_unapproved`,
      `::test_approval_without_hash_is_reset_to_unapproved`,
      `::test_matching_approved_hash_is_approved`,
      `::test_changed_hash_is_drift`,
      `::test_return_to_approved_hash_clears_stale`,
      `::test_blocked_server_is_never_downgraded`).
- [x] A NEW drift (a previously-approved set changed, and the status was not
      already `approved-stale`) appends an `mcp_server_tools_drifted` audit
      event carrying the previous/observed/approved hashes and raises one
      operator warning notification (category `mcp_server_tools_drift`). Both
      side effects are best-effort in their own savepoints, so an alerting
      failure can never roll back the observation, and a second observation
      while already stale does not re-alert
      (`core/mcp/tool_pinning.py`;
      `test_mcp_tool_pinning.py::test_observation_of_changed_tools_marks_stale_and_alerts`,
      `::test_second_observation_while_stale_does_not_re_alert`,
      `::test_drift_audit_failure_is_swallowed`,
      `::test_drift_notification_failure_is_swallowed`).
- [x] `POST /{server_id}/approve-tools` pins the CURRENT observed set: it is
      422 when nothing has been observed yet, otherwise it computes the hash
      over the stored `tools_json` and stamps `approval_status=approved`,
      `approved_by`, `approved_at` and `approved_hash`. It is gated at
      `mcp_server.approve` and audited as `mcp_server_tools_approved`
      (`api/routes/mcp_servers.py`; `test_far1692_registry_routes.py::test_approve_tools_success`,
      `::test_approve_tools_no_observed_tools`,
      `::test_approve_tools_malformed_tools`).
- [x] The read model exposes the full pin state: `tools_json`, `tools_hash`,
      `approval_status`, `approved_by` / `approved_at` / `approved_hash`,
      `last_observed_at`, `owner_account_id` and `has_credentials` (never the
      credential value) (`api/routes/mcp_servers.py`).
- [x] Error handling is typed and never leaks internals: `ToolPinningError` is
      422, a missing table (`ProgrammingError`) is 501, a constraint violation
      (`IntegrityError`) is 409, any other database failure is 503, a missing
      row is 404, and an unexpected error is 500 with a generic message
      (`api/routes/mcp_servers.py`; `test_far1692_registry_routes.py`).
- [ ] The `blocked` approval status is part of the vocabulary and is honoured
      by the decision logic (a blocked server is never downgraded), but no route
      sets it, so there is no shipped block/unblock action
      (`db/models/mcp_server.py`, `api/routes/mcp_servers.py`).
- [ ] Tool sets are recorded only when a caller POSTs `observe-tools`. There is
      no poller or ingestion leg that discovers a server's live tool set on its
      own, so a server that changes without a caller observing it produces no
      drift signal.

## Known Gaps

- There is no in-product surface for the registry: no manifest route, no admin
  page, and no MCP tools. The drift notification's `action_url` is
  `/settings/mcp`, the MCP setup page, not a registry view, because no registry
  view exists.
- No BDD feature covers the registry or the pinning decision; the contract is
  pinned by route-level unit tests, the tool-pinning unit and DB-free decision
  matrix, the CRUD/org-scoping unit test and the RLS integration test only.
- `declared_scopes` is recorded but nothing enforces or compares it, so it is
  inventory data, not a control.
- `blocked` is unreachable through the shipped API (see the unchecked behaviour
  above): the pin can be approved or drift to `approved-stale`, but there is no
  route that blocks a server.
- Team visibility is enforced by database RLS alone. There is no request-time
  team gate on these routes (deliberately matching FAR-1514).

## QA History

- 2026-11-23: Added this tracker after walking the product map and finding the
  shipped FAR-1692 S1/S2 MCP server registry and tool-definition pinning had no
  entry in either product-map layer, so it was invisible to the feature graph
  and to Assistant's documentation indexer. Verified each behaviour against
  `api/routes/mcp_servers.py`, `core/mcp/tool_pinning.py`,
  `db/crud/mcp_server.py`, `db/models/mcp_server.py`, `auth/permissions.py`,
  `auth/secret_storage.py` and `api/team_scope.py`, then cross-checked
  `test_far1692_registry_routes.py`, `test_mcp_tool_pinning.py`,
  `test_external_agent_mcp_server_crud.py` and `test_rls_isolation.py`. No
  product code changed in this pass.
