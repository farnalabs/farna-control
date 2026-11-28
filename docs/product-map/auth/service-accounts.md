---
id: feat-service-accounts
prd: N/A
adr: [ADR 056, ADR 058]
code:
  - backend/src/farna/api/routes/service_accounts.py
  - backend/src/farna/db/crud/account.py
  - backend/src/farna/core/principals.py
  - backend/src/farna/auth/api_key.py
  - backend/src/farna/db/models/account.py
  - backend/src/farna/db/migrations/versions/0351_baseline_rls.py
  - docs/architecture.md
unit-tests:
  - backend/tests/unit/api/test_service_accounts_endpoint.py
  - backend/tests/unit/api/test_service_principal_exclusion.py
  - backend/tests/unit/auth/test_login_endpoint.py
  - backend/tests/unit/core/test_principals.py
  - backend/tests/unit/db/crud/test_account.py
  - backend/tests/unit/db/test_account_model.py
  - backend/tests/integration/api/test_service_accounts_lifecycle.py
bdd:
depends-on: [feat-teams, feat-mcp]
status: covered
---

# Service Accounts

Machine identities owned by an organisation. A service account is a first-class
`accounts` row with `kind='service'` and no human-shaped identity, so it can
hold an org membership and team memberships and own API keys, but can never log
in. Org admins provision, list, read, rename and decommission service accounts
through `/api/v1/service-accounts`, and mint `mk_` keys that the service
principal owns. The surface is REST only: it has no manifest route and no
in-product admin page, so it is tracked here rather than in the manifest
`features:` registry. The governing design is recorded in `docs/architecture.md`
("Service accounts", FAR-1478, ADR 056 / ADR 058).

## Behaviors

- [x] Provisioning is admin-only and creates a service principal, never a
      human: `POST /api/v1/service-accounts` returns 201 with `kind="service"`,
      a trimmed non-blank display name and an org role (default `operator`);
      a blank name is 422 and a non-admin caller is 403. The row carries NULL
      `email`, `password_hash`, `auth_provider` and `sso_subject`, so every
      email / invite / SSO lookup structurally misses it
      (`service_accounts.py`, `db/crud/account.py`;
      `test_service_accounts_endpoint.py::test_create_returns_201_and_stamps_kind`,
      `::test_create_with_blank_name_is_rejected`,
      `::test_create_non_admin_is_403`,
      `test_account.py::test_create_service_account_shape`).
      <!-- behaviour: service-accounts-01 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_create_returns_201_and_stamps_kind, backend/tests/unit/db/crud/test_account.py::test_create_service_account_shape -->
- [x] Creation emits a `service_account_created` audit event fail-open (a
      failed audit write never rolls back the account), and the audit payload
      names the display name and granted org role
      (`test_service_accounts_endpoint.py::test_create_emits_service_account_created_audit`).
      <!-- behaviour: service-accounts-02 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_create_emits_service_account_created_audit -->
- [x] The role ceiling is `operator` and `admin` is refused rather than
      silently clamped: the request vocabulary is the org-role set, an `admin`
      grant is 422, an unknown role is 422, and `core/principals.py` is the
      ceiling authority (`clamp_service_role`). The baseline migration adds two
      structural backstops that reject a raw-SQL bypass: the
      `trg_org_memberships_service_role_ceiling` trigger refuses an `admin`
      service membership with SQLSTATE 23514, and
      `trg_accounts_kind_immutable` keeps `accounts.kind` immutable so a human
      row can never be flipped into a service row (or the reverse)
      (`core/principals.py`,
      `db/migrations/versions/0351_baseline_rls.py`;
      `test_service_accounts_endpoint.py::test_create_with_admin_role_is_rejected_structurally`,
      `test_principals.py::test_ceiling_is_operator`,
      `test_service_accounts_lifecycle.py::test_admin_role_grant_is_structurally_refused`).
      <!-- behaviour: service-accounts-03 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_create_with_admin_role_is_rejected_structurally, backend/tests/unit/core/test_principals.py::test_ceiling_is_operator, backend/tests/integration/api/test_service_accounts_lifecycle.py::test_admin_role_grant_is_structurally_refused -->
- [x] Listing and reads are admin-only and org-scoped: the list is paginated
      (`page` >= 1, `page_size` 1..100, optional display-name search) and
      returns the org's `kind='service'` memberships; a missing id, a human
      account, or a service account with no membership in the caller's org is
      the same 404, so the route never discloses a cross-org service principal
      (`service_accounts.py`;
      `test_service_accounts_endpoint.py::test_list_returns_service_accounts`,
      `::test_get_human_account_is_404`,
      `::test_get_service_account_without_org_membership_is_404`).
      <!-- behaviour: service-accounts-04 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_list_returns_service_accounts -->
- [x] Rename and role reassignment go through one PATCH: at least one of
      `name` / `role` is required (neither is 422), the same `operator` ceiling
      applies to a role change, and a decommissioned principal is 409. The
      change emits a `service_account_updated` audit event fail-open
      (`test_service_accounts_endpoint.py::test_patch_rename`,
      `::test_patch_without_fields_is_422`,
      `::test_patch_decommissioned_is_409`).
      <!-- behaviour: service-accounts-05 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_patch_rename -->
- [x] Decommission is explicit and one-way: `POST
      /api/v1/service-accounts/{id}/decommission` stamps
      `accounts.decommissioned_at`, flips `active` off, tombstones the org
      membership, bulk-revokes every live key owned by the principal and
      removes its team memberships in the same transaction, then returns the
      revoked-key and removed-team-membership counts and emits a
      `service_account_decommissioned` audit event. A second decommission is
      409, and the human user-admin surface refuses a service target with 422
      (while a cross-org target stays 404 first, so the refusal never discloses
      it) (`service_accounts.py`;
      `test_service_accounts_endpoint.py::test_decommission_revokes_keys_and_tombstones`,
      `::test_decommission_twice_is_409`,
      `test_service_accounts_lifecycle.py::test_provision_mint_decommission_end_to_end`,
      `::test_human_user_admin_surface_rejects_service_target`,
      `::test_cross_org_service_target_is_404_not_422`).
      <!-- behaviour: service-accounts-06 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_decommission_revokes_keys_and_tombstones, backend/tests/integration/api/test_service_accounts_lifecycle.py::test_provision_mint_decommission_end_to_end -->
- [x] Keys minted for a service principal are OWNED by it: `POST
      /api/v1/service-accounts/{id}/keys` returns 201 and stamps
      `org_api_keys.account_id` with the service account, never the human who
      bootstrapped it. A human admin (JWT) may bootstrap a key, and a service
      principal may mint one with its own `mk_` credential, but a key may only
      mint for its own principal (another principal is 403) and a non-admin JWT
      may not bootstrap for a service account (403) (`service_accounts.py`;
      `test_service_accounts_endpoint.py::test_admin_bootstrap_mint_owns_the_service_principal`,
      `::test_service_principal_mints_its_own_key`,
      `::test_api_key_cannot_mint_for_another_principal`,
      `::test_non_admin_jwt_cannot_mint_for_a_service_account`).
      <!-- behaviour: service-accounts-07 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_admin_bootstrap_mint_owns_the_service_principal -->
- [x] Key shape is constrained at the surface: the role is `operator` or
      `runner` only (`admin` is 422), the scope is immutable org scope (any
      other scope is 422), and the minted role may never exceed the target's
      LIVE org role (a higher role or a principal with no live membership is
      403). An optional grant-set must pass the org's `api_key_grants` flag: a
      flag that is off is 422, and a strict flag-read failure is 503 rather
      than a widened legacy full-role key
      (`service_accounts.py`, `auth/api_key.py`;
      `test_service_accounts_endpoint.py::test_admin_key_role_is_422`,
      `::test_user_scope_is_rejected`,
      `::test_role_above_target_live_role_is_403`,
      `::test_mint_with_no_live_role_is_403`,
      `::test_grants_with_flag_off_are_rejected`,
      `::test_grants_flag_read_failure_is_503`).
      <!-- behaviour: service-accounts-08 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_admin_key_role_is_422 -->
- [x] The 365-day service TTL is enforced at both key choke points: an explicit
      expiry beyond 365 days is 422 on mint AND on `PUT /api/v1/api-keys/{id}`
      extension (never a silent clamp) and exactly 365 days is accepted, both
      owned by the `auth/api_key.py` TTL ceiling; the service-account mint
      parses the expiry in `service_accounts.py::_parse_future_expires_at`,
      which treats a naive timestamp as UTC and rejects a past timestamp with
      422 (`auth/api_key.py`, `api/routes/service_accounts.py`;
      `test_service_accounts_endpoint.py::test_ttl_ceiling_maps_to_422`,
      `::test_naive_expires_at_is_accepted_as_utc`,
      `::test_past_expires_at_is_422`,
      `test_service_accounts_lifecycle.py::test_provision_mint_decommission_end_to_end`).
      <!-- behaviour: service-accounts-09 | tests: backend/tests/unit/api/test_service_accounts_endpoint.py::test_ttl_ceiling_maps_to_422, backend/tests/integration/api/test_service_accounts_lifecycle.py::test_provision_mint_decommission_end_to_end -->
- [x] A service principal can never authenticate through the human login path:
      the email-based login lookup refuses it with 401 byte-identically to a
      wrong password, so the denial never reveals that the row exists, and the
      stealth demo-login answers its plain 404 (`api/routes/auth.py`;
      `test_login_endpoint.py::test_login_rejects_service_principal`).
      <!-- behaviour: service-accounts-10 | tests: backend/tests/unit/auth/test_login_endpoint.py::test_login_rejects_service_principal -->
- [x] Human-only read surfaces that load an account by primary key call
      `is_service_account` and answer 404: `/me` and SCIM return not-found
      exactly as they would for an unknown id, so a service principal can never
      read a human profile or appear as a SCIM user. The NULL identity columns
      are the structural first line; the shared predicate is the second
      (`api/routes/me.py`, `api/routes/scim.py`;
      `test_service_principal_exclusion.py::TestMeHumanGuard::test_service_account_is_404`,
      `::TestScimHumanGuard::test_service_user_is_404`,
      `test_login_endpoint.py::test_me_returns_404_for_a_service_principal`,
      `test_account_model.py::test_account_service_identity_check_forbids_human_shaped_columns`).
      <!-- behaviour: service-accounts-11 | tests: backend/tests/unit/api/test_service_principal_exclusion.py, backend/tests/unit/auth/test_login_endpoint.py::test_me_returns_404_for_a_service_principal, backend/tests/unit/db/test_account_model.py::test_account_service_identity_check_forbids_human_shaped_columns -->
- [x] A service account holds ordinary team memberships through the existing
      team surface, and the team role CHECK still excludes `admin`. Decommission
      removes those memberships in the same act
      (`test_service_accounts_lifecycle.py::test_service_account_holds_a_team_membership`).
      <!-- behaviour: service-accounts-12 | tests: backend/tests/integration/api/test_service_accounts_lifecycle.py::test_service_account_holds_a_team_membership -->

## Known Gaps

- There is no dedicated admin page or route for service-account lifecycle: an
  org admin must use the REST API. Settings > MCP only labels the account kind
  when listing API keys, and there are no MCP tools to create, list or
  decommission service accounts.
- No BDD feature or step definitions cover service accounts; the contract is
  pinned by unit and integration tests only.
- Decommission is irreversible by design. There is no restore or reactivation
  path, and creating a replacement service account is the supported remedy.
- Service credentials are org-scoped machine identities only. The scope is
  immutable after mint, so a service key can never become a user-scoped key.
- Grant-set support on a minted key depends on the default-off
  `api_key_grants` flag. With the flag off, a mint carrying grants is refused,
  not silently downgraded to the full role bundle.

## QA History

- 2026-11-23: Added this tracker after walking the product map and finding the
  shipped service-account surface (FAR-1478, ADR 056 / ADR 058) had no entry in
  either product-map layer. Verified each behaviour against
  `api/routes/service_accounts.py`, `db/crud/account.py`,
  `core/principals.py`, `auth/api_key.py`, the `accounts` model constraints and
  the baseline RLS triggers, then cross-checked the unit and integration suites
  cited above and the "Service accounts" section of `docs/architecture.md`. No
  product code changed in this pass.
