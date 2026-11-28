---
id: feat-primitive-registry
prd: N/A
adr: []
code:
  - backend/src/farna/api/routes/registry.py
  - backend/src/farna/api/routes/admin.py
  - backend/src/farna/core/registry/__init__.py
  - backend/src/farna/core/registry/crypto.py
  - backend/src/farna/core/crypto/__init__.py
  - backend/src/farna/db/crud/publisher.py
  - backend/src/farna/db/models/publisher.py
  - backend/src/farna/auth/permissions.py
unit-tests:
  - backend/tests/unit/api/test_registry_protocol_endpoints.py
  - backend/tests/unit/api/test_registry_publishers.py
  - backend/tests/unit/api/test_public_read_endpoints.py
  - backend/tests/unit/api/test_route_introspection.py
  - backend/tests/unit/api/test_admin_coverage_gaps.py
  - backend/tests/unit/registry/test_registry.py
  - backend/tests/unit/registry/test_publisher_trust.py
  - backend/tests/unit/registry/test_crypto.py
  - backend/tests/unit/db/crud/test_publisher.py
  - backend/tests/unit/db/crud/test_org_scoping.py
  - backend/tests/unit/core/library_service/test_registry_lookup_and_listing.py
bdd: []
depends-on: [feat-library, feat-auth]
status: partial
---

# Community Primitive Registry

The community registry (`/api/v1/registry/*`) is the backend catalogue from which
pipeline primitives are published to and pulled from outside the local library:
schemas, workflows, agents and integrations are listed with publisher trust badges,
Ed25519-signed and checksum-verified, and pulled either for inspection or into the
org's own library. It also owns the publisher-trust vocabulary behind those badges,
and the DB-backed org-scoped publisher CRUD under `/api/v1/admin/publishers`.

The surface is REST-only: it has no manifest route, no UI and no MCP tool, and its
catalogue and in-memory publisher registry are process-local (see Known Gaps). The
same in-code registry is the fallback the library service reads for built-in
primitives, so the catalogue is reachable through `/api/v1/libraries` as well.

## Behaviors

- [x] The catalogue read surface is deliberately unauthenticated and tenant-free:
      `GET /api/v1/registry/primitives`, `GET /api/v1/registry/primitives/{slug}`
      and `GET /api/v1/registry/pull/{slug}` answer with NO credentials, a
      path-shaped slug is a catalogue key (never a route/file expansion), and an
      unknown slug is 404
      (`backend/tests/unit/api/test_public_read_endpoints.py`)
      <!-- behaviour: primitive-registry-01 | tests: backend/tests/unit/api/test_public_read_endpoints.py::test_registry_list_is_anonymous, backend/tests/unit/api/test_public_read_endpoints.py::test_registry_detail_is_anonymous, backend/tests/unit/api/test_public_read_endpoints.py::test_registry_pull_is_anonymous, backend/tests/unit/api/test_public_read_endpoints.py::test_registry_slug_lookup_is_a_catalogue_key_only -->
- [x] The catalogue list filters by `author`, `primitive_type` and `search`, and
      those filters compose with `author` + `primitive_type` + `search`
      (`core/registry/__init__.py::list_registry_primitives`)
      <!-- behaviour: primitive-registry-02 | tests: backend/tests/unit/registry/test_registry.py::test_list_all_primitives, backend/tests/unit/registry/test_registry.py::test_list_filter_by_type, backend/tests/unit/registry/test_registry.py::test_list_filter_by_author, backend/tests/unit/registry/test_registry.py::test_list_search, backend/tests/unit/registry/test_registry.py::test_list_with_combined_filters -->
- [x] The ranked listing sorts by popularity, recency and download count, carries
      each entry's publisher status, and an unknown `sort_by` falls back to
      popularity with a warning
      (`core/registry/__init__.py::list_registry_primitives_ranked`)
      <!-- behaviour: primitive-registry-03 | tests: backend/tests/unit/registry/test_publisher_trust.py::test_ranked_list_sorts_by_popularity, backend/tests/unit/registry/test_publisher_trust.py::test_ranked_list_sorts_by_recent, backend/tests/unit/registry/test_publisher_trust.py::test_ranked_list_sorts_by_downloads, backend/tests/unit/registry/test_publisher_trust.py::test_ranked_list_includes_publisher_status, backend/tests/unit/registry/test_publisher_trust.py::test_ranked_list_unknown_sort_falls_back_to_popularity -->
- [x] Every built-in primitive carries a SHA-256 checksum and an Ed25519 signature
      that verifies against the built-in registry key; bundle integrity is
      recomputed from the canonical payload, a tampered bundle reports
      `integrity_ok=false`, and the download response carries both `verified`
      and `integrity_ok`
      (`core/registry/__init__.py::verify_primitive_signature`,
      `verify_bundle_integrity`;
      `api/routes/registry.py::get_registry_primitive_endpoint`)
      <!-- behaviour: primitive-registry-04 | tests: backend/tests/unit/registry/test_registry.py::test_builtin_primitives_have_valid_signatures, backend/tests/unit/registry/test_registry.py::test_builtin_primitives_have_checksums, backend/tests/unit/registry/test_registry.py::test_bundle_integrity, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_bundle_integrity_mismatch_returns_integrity_ok_false -->
- [x] Publishing is slug-keyed and last-write-wins, and input validation rejects an
      invalid primitive type or missing content fields before anything is stored
      (`core/registry/__init__.py::publish_primitive`)
      <!-- behaviour: primitive-registry-05 | tests: backend/tests/unit/registry/test_registry.py::test_publish_new_primitive, backend/tests/unit/registry/test_registry.py::test_publish_overwrites_existing_slug, backend/tests/unit/registry/test_registry.py::test_publish_rejects_invalid_input -->
- [x] The v2 publish endpoint verifies the submitted Ed25519 signature over the
      canonical payload before accepting a primitive (403 on a mismatch), and a
      repeat publish of the same slug overwrites the prior entry rather than 409ing
      (`api/routes/registry.py::publish_primitive_v2`)
      <!-- behaviour: primitive-registry-06 | tests: backend/tests/unit/api/test_registry_protocol_endpoints.py::test_publish_signature_failure_returns_403, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_publish_duplicate_slug_overwrites -->
- [x] `GET /api/v1/registry/verify/{slug}` reports `trust_anchor_verified` by
      checking the submitted PEM public key against the trust anchor and reports
      the signature result separately, so a failed signature and a failed anchor
      check are both visible rather than collapsed into one boolean
      (`api/routes/registry.py::verify_registry_primitive_v2`)
      <!-- behaviour: primitive-registry-07 | tests: backend/tests/unit/api/test_registry_protocol_endpoints.py::test_verify_with_pem_returns_trust_anchor, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_verify_with_pem_and_failed_verify -->
- [x] `GET /api/v1/registry/verify/{slug}?public_key_hex=...` resolves the
      caller-org DB publisher row and returns its `trust_tier` and `publisher_name`;
      an unknown slug is 404, a verified signature with no matching DB publisher
      returns a null tier/name, and verify-by-hex maps a missing table to 501 and a
      DB failure to 503
      (`api/routes/registry.py::verify_registry_primitive_v2`)
      <!-- behaviour: primitive-registry-08 | tests: backend/tests/unit/api/test_registry_publishers.py::test_verify_with_pubkey_returns_trust_tier, backend/tests/unit/api/test_registry_publishers.py::test_verify_without_pubkey_returns_no_trust_tier, backend/tests/unit/api/test_registry_publishers.py::test_verify_unknown_primitive_returns_404, backend/tests/unit/api/test_registry_publishers.py::test_verify_with_pubkey_no_db_match, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_verify_hex_programming_error_returns_501, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_verify_hex_sqlalchemy_error_returns_503 -->
- [x] The download route verifies the primitive's signature and bundle integrity,
      returns both results, and maps a missing table to 501 "run migrations" and a
      database failure to 503 rather than leaking the driver error
      (`api/routes/registry.py::download_registry_primitive_endpoint`)
      <!-- behaviour: primitive-registry-09 | tests: backend/tests/unit/api/test_registry_protocol_endpoints.py::test_download_programming_error_returns_501, backend/tests/unit/api/test_registry_protocol_endpoints.py::test_download_sqlalchemy_error_returns_503 -->
- [x] The registry publisher trust model has three states: the built-in
      "Farna Control Team" publisher is `verified`, an unknown signing fingerprint
      is `community`, registration adds a `verified` publisher, revocation flips a
      publisher to `revoked`, and the public publishers list contains only verified
      publishers
      (`core/registry/__init__.py`; `api/routes/registry.py::list_publishers_endpoint`)
      <!-- behaviour: primitive-registry-10 | tests: backend/tests/unit/registry/test_publisher_trust.py::test_builtin_modulo_publisher_is_verified, backend/tests/unit/registry/test_publisher_trust.py::test_unknown_fingerprint_is_community, backend/tests/unit/registry/test_publisher_trust.py::test_register_publisher, backend/tests/unit/registry/test_publisher_trust.py::test_revoke_publisher, backend/tests/unit/registry/test_publisher_trust.py::test_list_verified_publishers, backend/tests/unit/api/test_registry_publishers.py::test_list_publishers_endpoint_returns_rebranded_name -->
- [x] The org-scoped admin publisher CRUD is durable in the `publishers` table and
      manages the `green`/`amber` trust tiers: creating a green publisher stamps
      `verified_since`, moving amber-to-green stamps it, green-to-amber clears it,
      a green-to-green update preserves it, and every read/update/delete is filtered
      by the caller's organisation
      (`db/crud/publisher.py`; `api/routes/admin.py::admin_create_publisher`)
      <!-- behaviour: primitive-registry-11 | tests: backend/tests/unit/db/crud/test_publisher.py::test_creates_amber_publisher, backend/tests/unit/db/crud/test_publisher.py::test_creates_green_publisher_with_verified_since, backend/tests/unit/db/crud/test_publisher.py::test_tier_change_to_green_sets_verified_since, backend/tests/unit/db/crud/test_publisher.py::test_tier_change_from_green_to_amber_clears_verified_since, backend/tests/unit/db/crud/test_publisher.py::test_green_to_green_preserves_verified_since, backend/tests/unit/db/crud/test_org_scoping.py::test_get_other_org_returns_none, backend/tests/unit/db/crud/test_org_scoping.py::test_update_other_org_returns_none, backend/tests/unit/db/crud/test_org_scoping.py::test_delete_other_org_returns_false -->
- [x] The admin publisher routes map duplicates to 409, an invalid trust tier to
      422, a missing publisher to 404, and database failures to their typed status
      (`api/routes/admin.py`; `api/routes/admin.py::_enforce_publisher_name_uniqueness`)
      <!-- behaviour: primitive-registry-12 | tests: backend/tests/unit/api/test_admin_coverage_gaps.py::test_create_publisher_name_conflict_409, backend/tests/unit/api/test_admin_coverage_gaps.py::test_create_publisher_key_conflict_409, backend/tests/unit/api/test_admin_coverage_gaps.py::test_create_publisher_value_error_422, backend/tests/unit/api/test_admin_coverage_gaps.py::test_update_publisher_key_conflict_409, backend/tests/unit/api/test_admin_coverage_gaps.py::test_delete_publisher_not_found_404, backend/tests/unit/api/test_admin_coverage_gaps.py::test_list_publishers_error_mapping -->
- [x] The library service reads a persisted primitive first and falls back to the
      in-code registry for a built-in primitive that has no DB row, so built-ins are
      installable without being copied into every organisation
      (`core/library_service/install.py`; `api/routes/library.py`)
      <!-- behaviour: primitive-registry-13 | tests: backend/tests/unit/core/library_service/test_registry_lookup_and_listing.py::test_db_row_wins, backend/tests/unit/core/library_service/test_registry_lookup_and_listing.py::test_db_miss_falls_back_to_builtin_registry -->
- [ ] The registry is durable and shared across instances - today the catalogue,
      download counts and the `/api/v1/registry/publishers` registration list are
      process-local in-memory state
      <!-- behaviour: primitive-registry-14 | tests: none -->

## Known Gaps

- **The catalogue is process-local in-memory state.** Published primitives,
  download counts and the in-memory publisher registry are held in module globals
  (`core/registry/__init__.py`) and are lost on restart; there is no hosted
  registry, no persistence layer and no cross-instance consistency. The docstring
  says "in-memory for alpha".
- **The built-in signing key and the trust anchor are generated per process.**
  `_registry_private` and `_get_trust_anchor()` each generate a fresh Ed25519 key
  at import, so a primitive signed by the built-in key in one process cannot be
  verified in another, and `trust_anchor_verified` can never anchor a real
  external publisher. There is no configured registry signing key or trust
  anchor.
- **Two publisher trust models disagree.** The public `GET /api/v1/registry/publishers`
  list and the `verified`/`community`/`revoked` badge come from the in-memory
  publisher store, while `GET /verify?public_key_hex=...` reads the org-scoped DB
  `publishers.trust_tier` (`green`/`amber`). `POST /api/v1/registry/publishers`
  writes only the in-memory store and `POST /api/v1/admin/publishers` writes only
  the DB row, so the two surfaces can report different trust for the same key.
- **The public detail/pull surfaces cannot verify a third-party signature.** Both
  call `verify_primitive_signature` without a public key, which falls back to the
  built-in registry key and returns `false` for every fingerprint that is not the
  built-in one. Only `GET /verify` accepts a caller-supplied key.
- **The v2 publish payload and the pull/verify payload are not the same bytes.**
  `POST /api/v1/registry/publish` signs and checksums a payload that omits
  `version`, while `GET /pull/{slug}` and `GET /verify/{slug}` rebuild the payload
  WITH `version`. A client that signs the documented publish payload therefore
  produces a signature/checksum that the pull/verify path cannot reproduce; the
  route tests mock the verification, so no end-to-end test catches it.
- **The legacy publish route still takes the raw private key.**
  `POST /api/v1/registry/primitives` requires `signing_key_hex` (the caller's
  Ed25519 private key) in the request body, while the v2 route accepts a signature
  plus public key. The legacy route remains mounted with a narrower
  `primitive_type` set (it omits `test_fixture` and `pipeline_template`).
- **`GET /verify` is authenticated while the catalogue reads are public.** It
  requires `registry.pull`, so an anonymous consumer that can list and pull a
  primitive cannot verify it; the public detail/pull surfaces cannot substitute
  because they cannot verify third-party keys.
- **`GET /api/v1/registry/publishers` has no explicit unauthenticated test.** The
  route declares no auth dependency and is allowlisted as a public catalogue read
  in `test_route_introspection.py`, but unlike list/detail/pull it has no
  anonymous-contract test in `test_public_read_endpoints.py`, so a future auth
  dependency could regress it without failing a test.
- **Pagination is not pinned.** `GET /api/v1/registry/primitives` accepts
  `page`/`page_size` (1..100) at the route, but no test exercises the slicing or
  the bounds validation.
- **Download is not pinned end-to-end.** The route's signature/integrity result
  and error mapping are tested with `create_library_primitive` mocked, so the
  actual materialisation of a registry primitive into the org library, the
  download-count increment and the `registry_primitive_downloaded` audit are not
  asserted by a test.
- **No manifest route, UI, MCP tool or BDD coverage.** The registry is
  REST/CLI-only and is not registered in the manifest `features:` registry; there
  is no admin screen for the catalogue or publisher trust, and no `.feature` file
  drives the protocol.

## QA History

- 2026-11-25: **Improve Architecture product-map walk** - entry added for a
  shipped but untracked backend surface. The public catalogue reads, filtering,
  ranking, built-in signing/checksums, legacy and v2 publish, verify by PEM and
  hex, download error mapping, the in-memory publisher trust model, the
  DB-backed admin publisher CRUD and the library in-code-registry fallback were
  verified against `api/routes/registry.py`, `api/routes/admin.py`,
  `core/registry/__init__.py`, `core/registry/crypto.py`, `core/crypto/__init__.py`,
  `db/crud/publisher.py` and the cited unit suites. The in-memory catalogue,
  per-process signing/trust keys, split publisher trust models, third-party public
  verification gap, v2 payload/version mismatch, legacy private-key route,
  authenticated `/verify`, untested anonymous `GET /publishers`, unpinned
  pagination and download materialisation are recorded as honest gaps rather than
  fixed in this docs-only walk. Status: partial (the durability behaviour is
  unchecked).
