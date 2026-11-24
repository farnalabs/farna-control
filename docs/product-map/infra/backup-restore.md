---
id: feat-backup-restore
prd: N/A
adr: []
code:
  - backend/src/farna/cli/backup.py
  - backend/src/farna/cli/main.py
  - scripts/backup.py
  - scripts/restore.py
  - scripts/backup-prune.py
  - scripts/verify_backup_restore.py
  - .github/workflows/backup-restore-nightly.yml
  - docs/operations/backup.md
unit-tests:
  - backend/tests/unit/cli/test_backup.py
  - backend/tests/unit/cli/test_backup_paths.py
  - backend/tests/unit/cli/test_backup_refactor_branches.py
  - backend/tests/unit/cli/test_backup_restore_semantics.py
  - backend/tests/unit/launcher/test_cross_machine_restore.py
  - backend/tests/unit/scripts/test_backup.py
  - backend/tests/unit/scripts/test_restore.py
  - backend/tests/unit/scripts/test_restore_helpers.py
  - backend/tests/unit/scripts/test_backup_prune.py
  - backend/tests/unit/scripts/test_backup_prune_classify_one.py
bdd:
depends-on: []
status: covered
---

# Backup and Restore

Farna Control ships two independent, non-interchangeable recovery tools for
self-hosted operators: the in-product `farna-control backup` / `farna-control
restore` pair (a database-level dump directory) and the legacy
`scripts/backup.py` / `scripts/restore.py` pair (a single encrypted archive).
This is an infra-only surface: it has no manifest route and no in-product UI, so
it is tracked here rather than in the manifest `features:` registry. The
operator-facing contract lives in `docs/operations/backup.md`.

## Behaviors

- [x] `farna-control backup` writes a fresh directory containing six
      checksummed artifacts: `database.sql` (a `pg_dump` of schema and data),
      the three LangGraph checkpoint exports (`checkpoint_blobs.json`,
      `checkpoints.json`, `checkpoint_writes.json`), `credentials_references.json`
      and a versioned `backup-info.json` manifest. Without `--output-dir` it
      creates a collision-free `./modulo-backup-<timestamp>-<rand>` directory;
      with `--output-dir` it reuses and overwrites that directory
      (`backend/src/farna/cli/backup.py`;
      `test_backup_restore_semantics.py::test_backup_manifest_is_versioned`,
      `::test_backup_manifest_documents_ephemeral_by_design`).
      <!-- behaviour: backup-restore-01 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_backup_manifest_is_versioned -->
- [x] The backup manifest is versioned (`MANIFEST_VERSION = 2`) and carries the
      timestamp, `backup_type`, database version, Alembic schema heads, a hash
      of `FERNET_KEY`, per-file SHA-256 checksums and the launcher snapshot
      section. A restore refuses an archive written by a newer manifest version
      and accepts the older supported version, so a legacy archive still
      restores (`backup.py`; `test_backup_restore_semantics.py::test_restore_refuses_newer_manifest_version`,
      `::test_restore_accepts_supported_manifest_version`).
      <!-- behaviour: backup-restore-02 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_restore_refuses_newer_manifest_version -->
- [x] The default archive contains no cleartext secret material: the launcher
      `state.json` rides along credential-free while the 0600 secrets file and
      the credential-bearing conf profile are copied only with the explicit
      `--include-secrets` opt-in, which prints a loud warning. Redis/SAQ queue
      data is excluded by design because it is ephemeral
      (`backup.py`; `test_backup_restore_semantics.py::test_backup_default_archive_carries_no_credentials`,
      `::test_backup_include_secrets_copies_and_warns_loudly`,
      `::test_backup_manifest_documents_ephemeral_by_design`).
      <!-- behaviour: backup-restore-03 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_backup_default_archive_carries_no_credentials -->
- [x] A backup against a launcher data directory takes its exclusive lock and
      records `last_backup_at` in the HMAC'd `state.json`; a running launcher
      refuses the backup with the holder PID, and the lock is released in a
      `finally`. Backup artifacts are written owner-only, the directory gets the
      search bit, and an output directory under a cloud-synced root warns
      (`backup.py`; `test_backup_restore_semantics.py::test_backup_refuses_while_a_launcher_is_running`,
      `::test_backup_persists_last_backup_timestamp`,
      `::test_backup_artifacts_are_owner_only`, `::test_backup_inside_cloud_synced_root_warns`,
      `test_backup_paths.py`).
      <!-- behaviour: backup-restore-04 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_backup_refuses_while_a_launcher_is_running, backend/tests/unit/cli/test_backup_paths.py -->
- [x] `farna-control restore` verifies everything before it touches the target:
      manifest shape and scalar types, manifest version, per-file checksums,
      instance identity from the HMAC'd `state.json`, a populated-target
      emptiness probe, a schema-downgrade guard (archive versions must be a
      subset of the running code's heads) and a disk pre-flight. A populated
      instance mismatch refuses unless `--replace-cluster` is given with an
      interactive confirmation; a fresh target is exempt, and a legacy archive
      with no identity warns instead of silently trusting it
      (`backup.py`; `test_backup_restore_semantics.py::test_restore_refuses_populated_instance_mismatch`,
      `::test_restore_fresh_target_is_exempt`,
      `::test_restore_refuses_schema_downgrade`,
      `::test_restore_checks_archived_state_mac_before_adoption`,
      `::test_restore_verifies_ride_along_files_via_manifest_checksums`,
      `::test_restore_typed_field_validation_refuses_wrong_types`).
      <!-- behaviour: backup-restore-05 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_restore_refuses_populated_instance_mismatch -->
- [x] Restore takes an automatic pre-restore safety dump of the target before
      the `psql` import (skippable only with the loud `--no-safety-dump`); a
      safety-dump failure refuses before the wipe, and the import runs as a
      single transaction without putting a password in argv
      (`backup.py`; `test_backup_restore_semantics.py::test_restore_writes_pre_restore_safety_dump_before_psql`,
      `::test_restore_safety_dump_failure_refuses_before_the_wipe`,
      `::test_restore_psql_single_transaction_and_no_password_argv`).
      <!-- behaviour: backup-restore-06 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_restore_writes_pre_restore_safety_dump_before_psql -->
- [x] After the database restore, restore re-inserts the checkpoint JSON
      exports, re-encrypts credentials when `FERNET_KEY` changed (using
      `--previous-fernet-key`), and re-adopts launcher artefacts only after the
      database restore succeeds. It then re-runs the first-boot role bootstrap
      for the APPLICATION role resolved from `--app-db-url` or `DATABASE_URL`,
      never from `--db-url`, and a posture failure exits non-zero naming the
      check instead of reporting a cluster that can still bypass RLS as
      complete (`backup.py`; `test_backup_restore_semantics.py::test_restore_reruns_promoted_posture_bootstrap`,
      `::test_restore_resolves_app_role_from_configuration_not_db_url`,
      `::test_restore_fails_when_posture_verification_fails`,
      `::test_restore_adopts_launcher_files_only_after_db_restore_succeeds`).
      <!-- behaviour: backup-restore-07 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_restore_reruns_promoted_posture_bootstrap -->
- [x] A collation-version family change (for example alpine/musl to glibc) is a
      hard refusal, while a compatible-but-older same-family drift warns and
      proceeds with the `reindexdb` remediation. `--dry-run` validates integrity
      and previews every restore step without changing the database
      (`backup.py`; `test_backup_restore_semantics.py::test_restore_collation_drift_prints_hard_warning`,
      `test_backup_paths.py::TestPreviewRestore`).
      <!-- behaviour: backup-restore-08 | tests: backend/tests/unit/cli/test_backup_restore_semantics.py::test_restore_collation_drift_prints_hard_warning, backend/tests/unit/cli/test_backup_paths.py -->
- [x] Machine migration works: a backup from one machine restores onto a fresh
      target on another, and a populated target with a different instance
      identity refuses without `--replace-cluster`
      (`test_cross_machine_restore.py::test_machine_b_restores_machine_a_backup_onto_a_fresh_target`,
      `::test_cross_machine_instance_identity_mismatch_refuses_populated_target`).
      <!-- behaviour: backup-restore-09 | tests: backend/tests/unit/launcher/test_cross_machine_restore.py::test_machine_b_restores_machine_a_backup_onto_a_fresh_target -->
- [x] The legacy `scripts/backup.py` / `scripts/restore.py` pair produces and
      reads a single AES-256-CBC encrypted archive (PBKDF2, 600K iterations)
      containing the dump, collected secrets, manifest and SHA-256 checksums.
      `scripts/restore.py` decrypts, extracts, verifies every checksum and then
      runs in `--dry-run`, `--data-only`, `--config-only` or `--full` mode; the
      two tools are directory-vs-archive incompatible with the in-product pair
      (`scripts/backup.py`, `scripts/restore.py`;
      `tests/unit/scripts/test_backup.py`, `tests/unit/scripts/test_restore.py`).
      <!-- behaviour: backup-restore-10 | tests: backend/tests/unit/scripts/test_restore.py -->
- [x] `scripts/backup-prune.py` applies the documented retention policy (7 daily,
      4 weekly Sunday, 12 monthly first-of-month), scopes retention per org,
      refuses paths outside the backup directory, and offers a `--dry-run`
      (`scripts/backup-prune.py`; `test_backup_prune.py`,
      `test_backup_prune_classify_one.py`).
      <!-- behaviour: backup-restore-11 | tests: backend/tests/unit/scripts/test_backup_prune.py, backend/tests/unit/scripts/test_backup_prune_classify_one.py -->
- [x] `.github/workflows/backup-restore-nightly.yml` runs
      `scripts/verify_backup_restore.py` daily at 04:30 UTC and on manual
      dispatch. Against a throwaway Postgres 16 it seeds real rows, creates and
      seeds the three checkpoint tables, runs `farna-control backup` and
      `farna-control restore` into a second empty database, asserts the seeded
      rows and checkpoint rows survived, re-asserts the app role is
      `NOBYPASSRLS` with its grants, then proves `alembic downgrade -1` moves off
      head and `alembic upgrade heads` returns to head with the data intact.
      The script refuses a cluster-wide role reset unless its target is provably
      scratch or `--allow-role-reset` is passed
      (`scripts/verify_backup_restore.py`, `.github/workflows/backup-restore-nightly.yml`).
      <!-- behaviour: backup-restore-12 | tests: none -->

## Known Gaps

- The in-product `farna-control backup` directory is written unencrypted. An
  operator who needs encryption at rest must use the `scripts/backup.py`
  archive or store the backup directory on encrypted media.
- There is no built-in backup scheduler or in-product backup/restore UI. The
  documented pattern is an external cron job, and the in-product tools are CLI
  only.
- `scripts/backup-prune.py` prunes only the encrypted
  `modulo-backup-*.tar.gz.enc` archives. It does not prune `farna-control
  backup` directories, which the operator must delete.
- The nightly gate proves the backup/restore/downgrade path on Postgres 16
  only, and it deliberately resets cluster-scoped runtime roles, so it must
  never be pointed at a shared database server.
- Restore requires the running release's migration heads to be a superset of
  the archive's recorded heads. A dump from a newer release than the binary
  doing the restore is refused, so a rollback-and-restore must use the matching
  release.

## QA History

- 2026-11-22: Added this tracker after walking the product map and finding the
  shipped backup/restore surface had no entry in either product-map layer.
  Verified each behaviour against `backend/src/farna/cli/backup.py`, the
  `scripts/` pair, the prune and nightly-gate scripts, the nightly workflow and
  the unit suites cited above, then reconciled the operator contract in
  `docs/operations/backup.md`. No product code changed in this pass.
