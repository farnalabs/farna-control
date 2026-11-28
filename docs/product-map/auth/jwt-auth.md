---
id: feat-auth-jwt-auth
prd: N/A
adr: []
code:
  - backend/src/farna/auth/jwt.py
  - backend/src/farna/auth/api_key.py
  - backend/src/farna/auth/dependencies.py
  - backend/src/farna/auth/ws_token.py
unit-tests:
  - backend/tests/unit/auth/test_jwt.py
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/jwt_auth_crypto.feature
  - backend/tests/bdd/features/auth/jwt_security.feature
depends-on: []
status: covered
---

# JWT Auth

Stateless JWT authentication for the Farna Control API: access / refresh token issuance,
principal decoding with tenant context, and purpose-scoped compact tokens
(WebSocket, claim, refresh). Consumed by `feat-teams-org-entity` and every
authenticated API route through `auth/dependencies.py`.

## Behaviors

- [x] `create_access_token` issues a signed JWT carrying user identity (`sub`),
      org context, role, expiry, and the credential class (`client_kind`); unlike
      the refresh/ws/claim helpers it stamps no `purpose` claim
      <!-- behaviour: jwt-auth-01 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_create_access_token_and_decode_principal_roundtrip, backend/tests/unit/auth/test_jwt.py::test_token_carries_org_context -->
- [x] `decode_principal` validates signature and expiry, and validates a token's
      `purpose` claim only when `allowed_purposes` is supplied; returns an
      `AuthenticatedPrincipal` (carrying the `is_system_admin` flag) and raises on
      wrong key, expired token, malformed/empty `sub`, missing or non-string
      account id, or a token used outside its allowed purpose. Farna Control's own
      tokens carry no `iss` claim, so no issuer check is performed
      <!-- behaviour: jwt-auth-02 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_decode_principal_rejects_wrong_key, backend/tests/unit/auth/test_jwt.py::test_expired_token_raises, backend/tests/unit/auth/test_jwt.py::test_missing_sub_raises, backend/tests/unit/auth/test_jwt.py::test_decode_principal_rejects_token_without_account_id -->
- [x] `none` algorithm is rejected and only allowlisted signing algorithms pass
      <!-- behaviour: jwt-auth-03 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_none_algorithm_rejected -->
- [x] Refresh tokens carry a `refresh` purpose and issue a new access token via
      `refresh_access_token`
      <!-- behaviour: jwt-auth-04 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_refresh_token_has_refresh_purpose, backend/tests/unit/auth/test_jwt.py::test_refresh_access_token_returns_valid_access_token -->
- [x] The JWT WebSocket-token helper (`auth/jwt.py` `create_ws_token`) stamps
      `purpose: "ws"` and is accepted only under that purpose; the production
      run-WebSocket credential is instead the opaque single-use Redis token
      minted by `auth/ws_token.py` (60s TTL, `GETDEL` consumption), not a JWT
      <!-- behaviour: jwt-auth-05 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_ws_token.py::test_default_ttl_is_60, backend/tests/unit/auth/test_ws_token.py::test_single_use -->
- [x] Claim tokens (`create_claim_token` / `decode_claim_token`) are purpose-scoped
      short-lived tokens
      <!-- behaviour: jwt-auth-06 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_create_claim_token_roundtrip, backend/tests/unit/auth/test_jwt.py::test_decode_claim_token_expired_raises, backend/tests/unit/auth/test_jwt.py::test_decode_claim_token_wrong_purpose_raises -->
- [x] Tenant identity is embedded in the token and validated on decode
      <!-- behaviour: jwt-auth-07 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_token_carries_org_context, backend/tests/unit/auth/test_jwt.py::test_decode_principal_validates_tenant_identity -->
- [x] Purpose isolation holds in the `decode_principal` contract: an access token
      (which carries no `purpose`) or a refresh token is rejected when the caller
      requires the `ws` purpose. On the normal API path access tokens are decoded
      without a purpose restriction (`allowed_purposes=None`), so the restriction
      guards the purpose-scoped token families rather than every request
      <!-- behaviour: jwt-auth-08 | tests: backend/tests/bdd/features/auth/jwt_auth_crypto.feature, backend/tests/unit/auth/test_jwt.py::test_decode_principal_rejects_access_token_for_ws_purpose, backend/tests/unit/auth/test_jwt.py::test_decode_principal_rejects_refresh_token_for_ws_purpose, backend/tests/unit/auth/test_jwt.py::test_decode_principal_accepts_ws_token_with_allowed_purpose -->

## Known Gaps

None acknowledged: the token-utility contract (mint/decode, purpose isolation,
rotation propagation, claim-token scoping) is now locked by the executing
`jwt_auth_crypto.feature` BDD surface (added 2026-09-18) driving the real
`farna.auth.jwt` seams, and the API-level login/refresh/logout/tamper flows are
covered by `jwt_security.feature`.

## QA History

- 2026-11-28: **Improve Architecture product-map walk** - bound all eight
  `feat-auth-jwt-auth` behaviours (`jwt-auth-01` to `jwt-auth-08`) to their
  proving tests, which had shipped but carried `tests: none` since bindings were
  introduced. The `jwt_auth_crypto.feature` surface proves the token-utility
  contract (mint/decode, purpose isolation, rotation propagation, claim-token
  scoping); the remaining clauses bind to named nodes in
  `tests/unit/auth/test_jwt.py` (wrong key / expiry / missing subject / missing
  account id, `alg=none`, refresh purpose + rotation, tenant validation, purpose
  isolation) and `tests/unit/auth/test_ws_token.py` (the opaque 60s single-use
  Redis run-WebSocket token). The frontmatter `status` is now derived `covered`
  (was `partial`); the unbound-behaviours baseline shrank by eight. No behavior
  change - graph accuracy only.
- 2026-09-18: **product-map review pass** - closed the
  "No standalone BDD feature file" gap. Registered
  `auth/jwt_auth_crypto.feature` into the executing BDD suite from the new
  `steps/test_jwt_auth_crypto.py`, driving the real `farna.auth.jwt` seams
  network-free and DB-free: access-token mint/decode round-trip (identity, role,
  tenant org, `client_kind`), wrong-secret / tampered-signature / expired /
  `alg=none` / missing-subject rejections, the purpose-isolation matrix (access /
  `ws` / `refresh` accepted only under their own purpose, the `refresh_access_token`
  rotation seam refusing a `ws` token), refresh-rotation propagation of identity +
  credential class into the new access token, `decode_claim_token` HITL-gate
  run/gate scoping with a wrong-gate refusal, and the legacy no-`client_kind`
  token decoding as `browser`. `_ORPHANED_BDD_FEATURES` stays empty.
- 2026-08-25: **product-map review pass** - entry added to close the
  dangling `depends-on: feat-auth-jwt-auth` edge in `teams/org-entity.md`. Behaviors
  re-verified against `auth/jwt.py` and `backend/tests/unit/auth/test_jwt.py`. Status:
  covered.
