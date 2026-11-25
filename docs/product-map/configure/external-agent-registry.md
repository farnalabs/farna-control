---
id: feat-external-agent-registry
prd: N/A
adr: [ADR 055]
code:
  - backend/src/farna/api/routes/external_agents.py
  - backend/src/farna/db/crud/external_agent.py
  - backend/src/farna/db/models/external_agent.py
  - backend/src/farna/db/migrations/versions/0350_baseline_schema.py
  - backend/src/farna/db/migrations/versions/0351_baseline_rls.py
  - backend/src/farna/auth/permissions.py
  - backend/src/farna/api/team_scope.py
unit-tests:
  - backend/tests/unit/api/routes/test_far1692_registry_routes.py
  - backend/tests/unit/db/test_external_agent_mcp_server_crud.py
  - backend/tests/integration/test_rls_isolation.py
bdd:
depends-on: [feat-teams]
status: partial
---

# External Agent Registry

An org-scoped inventory of agents that Farna Control neither dispatched nor
executed: Claude Code, Cursor, a local opencode session, and anything else that
reports as `other`. The registry is policy substrate and a record, not
enforcement (ADR 055, detect and surface): a row says what exists and how it was
discovered, and never itself polices the agent. The surface is REST only
(`/api/v1/external-agents`), with no manifest route, no in-product admin page
and no MCP tools, so it is tracked here rather than in the manifest `features:`
registry. It is the inventory half (S1) of FAR-1692; ingestion slices S3/S4
(the legs that would populate `first_seen`/`last_seen`) are not shipped.

## Behaviors

- [x] CRUD over `/api/v1/external-agents`: `GET ""` lists paginated rows
      (`page` >= 1, `page_size` 1..100, ordered `created_at` DESC then `id`
      DESC, envelope `items`/`total`/`page`/`page_size`), `POST ""` creates and
      returns 201, `GET /{agent_id}` reads one, `PATCH /{agent_id}` updates,
      and `DELETE /{agent_id}` soft-deletes and returns 204
      (`api/routes/external_agents.py`, `db/crud/external_agent.py`;
      `test_far1692_registry_routes.py`).
      <!-- behaviour: external-agent-registry-01 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py -->
- [x] The create shape is constrained: `name` 1..255, `runtime_kind` one of
      `claude-code` / `cursor` / `opencode` / `other` (default `other`),
      `host_descriptor` <= 500, `status` one of `active` / `inactive` /
      `unknown` (default `active`), `discovery_provenance` one of `manual` /
      `mcp` / `webhook` / `forge` (default `manual`), and `visibility` one of
      `org` / `team` (default `org`). The model adds the same CHECK constraints
      as a structural backstop, plus `owner_team_id NOT NULL` whenever
      `visibility = 'team'` (`db/models/external_agent.py`;
      `0350_baseline_schema.py`).
      <!-- behaviour: external-agent-registry-02 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py::test_create_external_agent_rejects_out_of_shape_requests, backend/tests/unit/api/routes/test_far1692_registry_routes.py::test_create_external_agent_applies_documented_defaults -->
- [x] Access is permission-floored per verb: `external_agent.list` (viewer
      floor) for list and read, `external_agent.create` / `.update` / `.delete`
      (operator floor) for mutations (`auth/permissions.py`;
      `test_far1692_registry_routes.py`).
      <!-- behaviour: external-agent-registry-03 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py -->
- [x] Reads and writes are org-scoped twice: the route sets the RLS org and
      user context, and every CRUD query carries an explicit
      `organisation_id` predicate, so a cross-org read is refused by the query
      layer itself and answers 404 exactly like an unknown id rather than
      disclosing the row (`db/crud/external_agent.py`,
      `test_external_agent_mcp_server_crud.py`).
      <!-- behaviour: external-agent-registry-04 | tests: backend/tests/unit/db/test_external_agent_mcp_server_crud.py -->
- [x] Team visibility is a DB-RLS boundary, not a request-time gate: the
      `rls_team_isolation` policy on `external_agents` scopes a team-private
      row to its owner team, matching the core team-scoped tables' DB-RLS-only
      posture (FAR-1514). The `team_scope` resolver for the table is registered
      for registry completeness only and is not wired into these routes
      (`0351_baseline_rls.py`, `api/team_scope.py`;
      `test_rls_isolation.py`).
      <!-- behaviour: external-agent-registry-05 | tests: backend/tests/integration/test_rls_isolation.py -->
- [x] Team assignment is validated at the write: create refuses an
      `owner_team_id` the caller cannot reach, and a visibility or owner-team
      change goes through the shared update-transition guard
      (`api/team_scope.py`; `test_far1692_registry_routes.py`).
      <!-- behaviour: external-agent-registry-06 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py -->
- [x] Create, update and delete emit `external_agent_created` /
      `external_agent_updated` / `external_agent_deleted` audit events through
      the shared `audited()` dependency on the `external_agent` resource
      (`api/routes/external_agents.py`; `test_far1692_registry_routes.py`).
      <!-- behaviour: external-agent-registry-07 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py -->
- [x] Delete is a soft delete: `deleted_at` is stamped, the global soft-delete
      filter hides the row from every later read, and the physical row is kept
      (`db/crud/external_agent.py`; `test_external_agent_mcp_server_crud.py`).
      <!-- behaviour: external-agent-registry-08 | tests: backend/tests/unit/db/test_external_agent_mcp_server_crud.py -->
- [x] Error handling is typed and never leaks internals: a missing table
      (`ProgrammingError`) is 501, a unique/constraint violation
      (`IntegrityError`) is 409, any other database failure is 503, a missing
      row is 404, and an unexpected error is 500 with a generic message
      (`api/routes/external_agents.py`; `test_far1692_registry_routes.py`).
      <!-- behaviour: external-agent-registry-09 | tests: backend/tests/unit/api/routes/test_far1692_registry_routes.py -->
- [ ] `first_seen` / `last_seen` are carried on the model and in the response
      but no create/update request field sets them: they are the S3/S4
      ingestion slice's to populate, not manual CRUD's
      (`db/models/external_agent.py`, `api/routes/external_agents.py`).
      <!-- behaviour: external-agent-registry-10 | tests: none -->
- [ ] The `discovery_provenance` vocabulary accepts `mcp` / `webhook` /
      `forge`, but only `manual` is producible today: no ingestion leg emits
      the other three yet.
      <!-- behaviour: external-agent-registry-11 | tests: none -->

## Known Gaps

- There is no in-product surface for the registry: no manifest route, no admin
  page, and no MCP tools. An operator must call the REST API directly.
- No BDD feature covers the registry; the contract is pinned by route-level unit
  tests, the CRUD/org-scoping unit test and the RLS integration test only.
- Ingestion (FAR-1692 S3/S4) is not shipped, so nothing discovers an external
  agent automatically. Rows are created by hand and `first_seen`/`last_seen`
  stay NULL.
- Team visibility is enforced by database RLS alone. There is no request-time
  team gate on these routes (deliberately matching FAR-1514), so the boundary
  is only as strong as the RLS policy is applied to a given connection.

## QA History

- 2026-11-25: **Improve Architecture product-map walk** - bound
  `external-agent-registry-02` (create-shape constraints and defaults) to new
  route tests in `test_far1692_registry_routes.py` that drive each invalid shape
  to 422 without reaching the CRUD seam and assert the documented defaults on a
  bare-name create. No product code changed.

- 2026-11-23: Added this tracker after walking the product map and finding the
  shipped FAR-1692 S1 external-agent registry had no entry in either
  product-map layer, so it was invisible to the feature graph and to Assistant's
  documentation indexer. Verified each behaviour against
  `api/routes/external_agents.py`, `db/crud/external_agent.py`,
  `db/models/external_agent.py`, `auth/permissions.py` and `api/team_scope.py`,
  then cross-checked `test_far1692_registry_routes.py`,
  `test_external_agent_mcp_server_crud.py` and `test_rls_isolation.py`. No
  product code changed in this pass.
