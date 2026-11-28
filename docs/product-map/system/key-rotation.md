---
id: feat-key-rotation
prd: N/A
adr: []
code:
  - backend/src/farna/api/routes/admin_rotation.py
  - backend/src/farna/core/fernet_rotation.py
  - backend/src/farna/core/audit_coverage.py
  - backend/src/farna/api/dependencies.py
  - backend/src/farna/api/main.py
unit-tests:
  - backend/tests/unit/api/test_toctou_fixes.py
  - backend/tests/unit/api/test_break_glass_rotation_mint_deny.py
  - backend/tests/unit/api/test_break_glass_mint_deny.py
  - backend/tests/unit/core/test_fernet_rotation.py
  - backend/tests/integration/test_fernet_rotation_triggers_jsonb_set.py
bdd: []
depends-on: [feat-core-secrets-backend]
status: partial
---

# Fernet Key Rotation

The system-administrator control plane for re-encrypting every Fernet-encrypted
store under a new `FERNET_KEY` without downtime. It is the operational half of
`feat-core-secrets-backend`: the core module re-wraps ciphertext, and this
surface starts, locks and reports a whole-instance rotation over
`/api/v1/admin/rotation`. It ships REST only, with no manifest route, no admin
screen and no MCP tool, so it is tracked here rather than in the manifest
`features:` registry. An operator follows `docs/security/secret-management.md`
(POST to start, poll the status endpoint until the result lands), which is the
documented key-rotation procedure.

## Behaviors

- [x] `POST /api/v1/admin/rotation/rotate-key` accepts
      `{new_fernet_key, old_fernet_key?}` and returns `202 Accepted` with
      `status: accepted`, a `task_id` and a message; re-encryption runs as a
      background task so the old key stays valid for reads while it runs
      (`api/routes/admin_rotation.py`; `docs/security/secret-management.md`).
      <!-- behaviour: key-rotation-01 | tests: none -->
- [x] The old key is `old_fernet_key` when supplied and otherwise the running
      `settings.fernet_key`, so the previous key remains the read fallback until
      the rotation completes.
      <!-- behaviour: key-rotation-02 | tests: none -->
- [x] A new key shorter than 32 bytes is rejected with `422` before any write
      (`_validate_fernet_key`).
      <!-- behaviour: key-rotation-03 | tests: none -->
- [x] Access requires the system permission `system.config.manage`, and the
      route is additionally guarded by `deny_break_glass_mint`: a break-glass
      administrator may not mint a new instance-wide encryption key (403)
      (`api/routes/admin_rotation.py`, `api/dependencies.py`;
      `test_break_glass_rotation_mint_deny.py`, `test_break_glass_mint_deny.py`).
      <!-- behaviour: key-rotation-04 | tests: backend/tests/unit/api/test_break_glass_rotation_mint_deny.py, backend/tests/unit/api/test_break_glass_mint_deny.py -->
- [x] The start route is audited twice: the shared `audited()` dependency
      records the coarse `api_access_post` success/attempt event, and the
      handler appends `fernet_key_rotation_started` only AFTER the lock is held,
      so a request that loses the race cannot record a start for a rotation that
      never runs. A failed start-audit append releases the lock and fails the
      request (`core/audit_coverage.py`, `admin_rotation.py`).
      <!-- behaviour: key-rotation-05 | tests: none -->
- [x] Only one rotation may run at a time. With Redis configured the guard is a
      cross-process `SET NX` key carrying a unique owner token and a 30-minute
      TTL; without Redis an in-process owner token guarded by an `asyncio.Lock`
      spans the background task. An active rotation answers `409 Conflict`
      (`admin_rotation.py`; `TestRotationGuardRedisLock` in
      `test_toctou_fixes.py`).
      <!-- behaviour: key-rotation-06 | tests: backend/tests/unit/api/test_toctou_fixes.py -->
- [x] The lock acquisition fails CLOSED: a Redis error while acquiring answers
      `503` rather than letting two rotations run, and release deletes the key
      only when it still holds this rotation's owner token (atomic Lua compare
      and delete), so a rotation that outlived the TTL cannot delete a
      successor's lock.
      <!-- behaviour: key-rotation-07 | tests: none -->
- [x] The status read fails OPEN: `GET /api/v1/admin/rotation/status` answers
      `{rotation_in_progress, last_rotation_result}`, reads the Redis lock when
      configured and otherwise the in-process owner guard, and reports
      `rotation_in_progress: false` when Redis is unreachable (the acquire path,
      not the status read, is the authoritative gate). It needs the same
      `system.config.manage` permission.
      <!-- behaviour: key-rotation-08 | tests: none -->
- [x] Rotation is refused with `503` when
      `FARNA_CONTROL_SYSTEM_DATABASE_URL` is unset: without the cross-org
      `farna_control_system` (BYPASSRLS) role the RLS-scoped UPDATEs would
      silently match zero rows, so the route refuses loudly instead of reporting
      a hollow success. The background task repeats the guard and records a
      `failed` result if the role disappears before it runs.
      <!-- behaviour: key-rotation-09 | tests: none -->
- [x] The background task runs on the cross-org system session factory and
      re-encrypts every encrypted store through
      `rotate_all_encrypted_data` (secrets, connector instances, model backends,
      notification endpoints, OTel config, checkpoints / checkpoint blobs /
      writes), appending `fernet_key_rotation_completed` with the
      `tables_processed` / `total_rows_reencrypted` counts in the SAME
      transaction as the re-encryption, then stores the completed result. The
      lock is released in `finally` on success, failure and crash recovery
      (`core/fernet_rotation.py`, `admin_rotation.py`;
      `test_fernet_rotation.py`,
      `test_fernet_rotation_triggers_jsonb_set.py`).
      <!-- behaviour: key-rotation-10 | tests: backend/tests/unit/core/test_fernet_rotation.py, backend/tests/integration/test_fernet_rotation_triggers_jsonb_set.py -->

## Known Gaps

- **The last rotation result is process-local and in memory.** `_last_rotation_result`
  lives on the API process that started the task, so in a multi-worker
  deployment a status poll served by another worker can report
  `last_rotation_result: null` even after a completed rotation, and a restart
  clears the result (the Redis lock still reports in-progress correctly).
  Persisting the result (or re-deriving it from the audit chain) is not shipped.
- **A worker restart abandons a running rotation.** Rotation is an in-process
  `asyncio` task; the re-encryption transaction rolls back and the Redis lock
  expires after its TTL, so the operator must start again. There is no resume,
  no cancellation endpoint, and no progress percentage.
- **Failure is not recorded in the audit chain.** A failed rotation logs and
  stores a `failed` result in memory but appends no event; only
  `fernet_key_rotation_started` and `fernet_key_rotation_completed` exist.
- **No UI.** Rotation is a documented `curl` procedure only (deliberate for a
  `system.config.manage` operational control), so there is no admin page to
  start it, show progress, or display the last result.
- **Rotation is instance-wide, not per organisation.** Every org's ciphertext is
  re-encrypted under the one instance `FERNET_KEY`; there is no per-org key or
  per-org rotation scope, matching the single-key `fernet` backend.

## QA History

- 2026-11-24: **Improve Architecture product-map walk** - entry added for a
  shipped but untracked system-administrator surface. The route, permission
  gate, break-glass mint-deny, audit events, Redis/in-process lock, `503`
  system-role guard and status semantics were verified against
  `api/routes/admin_rotation.py`, `core/fernet_rotation.py`,
  `docs/security/secret-management.md` and the cited unit/integration suites.
  Status: covered, with the process-local result, no-resume, audit-on-failure,
  no-UI and instance-wide-scope limitations recorded as honest gaps.
