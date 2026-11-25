---
id: feat-backup-restore
prd: N/A
adr: []
code:
  - backend/src/farna/cli/backup.py
  - backend/src/farna/cli/main.py
  - backend/src/farna/db/plain_dump.py
  - backend/src/farna/launcher/upgrade.py
  - deploy/supervisor/entrypoint.sh
  - scripts/backup.py
  - scripts/restore.py
  - scripts/compose-restore.sh
  - scripts/compose-rollback.sh
  - scripts/backup-prune.py
  - scripts/verify_backup_restore.py
  - .github/workflows/backup-restore-nightly.yml
  - docs/operations/backup.md
unit-tests:
  - backend/tests/unit/cli/test_backup.py
  - backend/tests/unit/cli/test_backup_paths.py
  - backend/tests/unit/cli/test_backup_refactor_branches.py
  - backend/tests/unit/cli/test_backup_restore_semantics.py
  - backend/tests/unit/launcher/test_upgrade.py
  - backend/tests/unit/launcher/test_upgrade_snapshot.py
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
- [x] The backup manifest is versioned (`MANIFEST_VERSION = 2`) and carries the
      timestamp, `backup_type`, database version, Alembic schema heads, a hash
      of `FERNET_KEY`, per-file SHA-256 checksums and the launcher snapshot
      section. A restore refuses an archive written by a newer manifest version
      and accepts the older supported version, so a legacy archive still
      restores (`backup.py`; `test_backup_restore_semantics.py::test_restore_refuses_newer_manifest_version`,
      `::test_restore_accepts_supported_manifest_version`).
- [x] The default archive contains no cleartext secret material: the launcher
      `state.json` rides along credential-free while the 0600 secrets file and
      the credential-bearing conf profile are copied only with the explicit
      `--include-secrets` opt-in, which prints a loud warning. Redis/SAQ queue
      data is excluded by design because it is ephemeral
      (`backup.py`; `test_backup_restore_semantics.py::test_backup_default_archive_carries_no_credentials`,
      `::test_backup_include_secrets_copies_and_warns_loudly`,
      `::test_backup_manifest_documents_ephemeral_by_design`).
- [x] A backup against a launcher data directory takes its exclusive lock and
      records `last_backup_at` in the HMAC'd `state.json`; a running launcher
      refuses the backup with the holder PID, and the lock is released in a
      `finally`. Backup artifacts are written owner-only, the directory gets the
      search bit, and an output directory under a cloud-synced root warns
      (`backup.py`; `test_backup_restore_semantics.py::test_backup_refuses_while_a_launcher_is_running`,
      `::test_backup_persists_last_backup_timestamp`,
      `::test_backup_artifacts_are_owner_only`, `::test_backup_inside_cloud_synced_root_warns`,
      `test_backup_paths.py`).
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
- [x] Restore takes an automatic pre-restore safety dump of the target before
      the `psql` import (skippable only with the loud `--no-safety-dump`); a
      safety-dump failure refuses before the wipe, and the import runs as a
      single transaction without putting a password in argv
      (`backup.py`; `test_backup_restore_semantics.py::test_restore_writes_pre_restore_safety_dump_before_psql`,
      `::test_restore_safety_dump_failure_refuses_before_the_wipe`,
      `::test_restore_psql_single_transaction_and_no_password_argv`).
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
- [x] A collation-version family change (for example alpine/musl to glibc) is a
      hard refusal, while a compatible-but-older same-family drift warns and
      proceeds with the `reindexdb` remediation. `--dry-run` validates integrity
      and previews every restore step without changing the database
      (`backup.py`; `test_backup_restore_semantics.py::test_restore_collation_drift_prints_hard_warning`,
      `test_backup_paths.py::TestPreviewRestore`).
- [x] Machine migration works: a backup from one machine restores onto a fresh
      target on another, and a populated target with a different instance
      identity refuses without `--replace-cluster`
      (`test_cross_machine_restore.py::test_machine_b_restores_machine_a_backup_onto_a_fresh_target`,
      `::test_cross_machine_instance_identity_mismatch_refuses_populated_target`).
- [x] The legacy `scripts/backup.py` / `scripts/restore.py` pair produces and
      reads a single AES-256-CBC encrypted archive (PBKDF2, 600K iterations)
      containing the dump, collected secrets, manifest and SHA-256 checksums.
      `scripts/restore.py` decrypts, extracts, verifies every checksum and then
      runs in `--dry-run`, `--data-only`, `--config-only` or `--full` mode; the
      two tools are directory-vs-archive incompatible with the in-product pair
      (`scripts/backup.py`, `scripts/restore.py`;
      `tests/unit/scripts/test_backup.py`, `tests/unit/scripts/test_restore.py`).
- [x] `scripts/backup-prune.py` applies the documented retention policy (7 daily,
      4 weekly Sunday, 12 monthly first-of-month), scopes retention per org,
      refuses paths outside the backup directory, and offers a `--dry-run`
      (`scripts/backup-prune.py`; `test_backup_prune.py`,
      `test_backup_prune_classify_one.py`).
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

- [x] Both upgrade paths take a pre-upgrade snapshot immediately before the
      migrations run, through one shared callable
      (`launcher/upgrade.py::take_upgrade_snapshot`): the container entrypoint
      (`deploy/supervisor/entrypoint.sh`) writes to the `/backups` bind mount
      and aborts the boot when no verified snapshot can be produced, and the
      native launcher writes beside the cluster (a sibling of `pgdata/`, never
      inside it). A pristine database (first install) is a no-op
      (`deploy/supervisor/entrypoint.sh`, `launcher/upgrade.py`;
      `test_upgrade_snapshot.py::TestEntrypointMode`).
- [x] The snapshot refuses the upgrade before writing anything when the output
      filesystem has less than `max(2 GB, 1.25 x pg_database_size)` free,
      naming the required space, the available space and that nothing has been
      changed; an unmeasurable database fails closed
      (`launcher/upgrade.py::enforce_snapshot_disk_headroom`;
      `test_upgrade_snapshot.py::TestDiskGuard`).
- [x] A dump is accepted only when it is PROVEN complete at dump time:
      pg_dump's completion trailer must be present (a truncated plain dump
      would otherwise replay as a shorter valid script), a `-Fc` archive in the
      plain-SQL path is refused by name, and the dump is checksummed and
      recorded in both manifests. The same gate exists at restore time when the
      manifest attested completeness, and an archive that records no checksum
      for `database.sql` is refused as foreign
      (`backend/src/farna/db/plain_dump.py`, `cli/backup.py`;
      `test_upgrade_snapshot.py::TestVerifyDumpComplete`,
      `test_backup.py::TestRestoreDumpIntegrity`).
- [x] Retention is BOUNDED: it keeps the two newest VERIFIED snapshots plus
      the newest snapshot recording EXACTLY the database's current schema — the
      recovery point a database that did not move since (a failed migration, a
      rollback) actually depends on — and never prunes before the new snapshot
      verifies: dump, verify, then prune. Older generations are superseded and
      deleted, so snapshots cannot accumulate until the disk guard refuses every
      upgrade; at most `retain + 1` directories survive. A directory another live
      process is writing (its in-progress marker names a live pid) is never
      touched; pruning and snapshotting serialise on a per-directory lock. A
      snapshot that is not intact — including one whose dump checksum no longer
      matches — is swept, so the corrupt one is pruned rather than the good one
      beside it (`launcher/upgrade.py::prune_snapshots`,
      `_retention_keep_set`, `snapshot_is_intact`;
      `test_upgrade_snapshot.py::TestPruneSnapshots`,
      `::TestRetentionIsBounded`, `::TestRetentionVerifiesTheChecksum`,
      `::TestInFlightSnapshots`).
- [x] The snapshot only happens when a migration is PENDING, and the decision
      is made INSIDE the shared helper so no caller can bypass it: a database
      already at the target's migration head takes no snapshot, so a release
      that migrates and then crash-loops — or a plain installer re-run — cannot
      rotate its own pre-upgrade recovery point away. The comparison is against
      the TARGET release (`--target-bundle` for the installer, which runs the
      OLD installation's helper), never the helper's own code
      (`launcher/upgrade.py::migration_pending`, `pre_upgrade_dump`,
      `_pending_for_target`; `test_upgrade_snapshot.py::
      TestMigrationPendingDecision`, `::test_the_pre_upgrade_snapshot_survives_two_failed_restarts`,
      `test_upgrade_skip_backup.py::TestThePendingCheckCannotBeBypassed`).
- [x] The disk guard accounts for the snapshots retention KEEPS, not just the
      new dump, so it cannot pass and then hit ENOSPC
      (`launcher/upgrade.py::enforce_snapshot_disk_headroom`;
      `test_upgrade_snapshot.py::TestDiskHeadroomCountsRetainedSnapshots`).
- [x] Both dump producers run ONE shared pipeline
      (`farna.db.plain_dump.dump_plain_sql`): pinned argv, 0600 creation, the
      replay rewrite, then the completeness proof. The URL is normalised to a
      libpq scheme (``postgresql+asyncpg://`` is what the container is
      configured with, and libpq rejects it) in one shared place
      (`farna.db.url_utils.normalise_libpq_url`), and the password is lifted out
      of the DSN so a connection error can never quote it
      (`test_plain_dump.py::TestSharedPipeline`,
      `test_url_utils.py::test_both_url_forms_reach_a_working_psycopg_connection_string`).
- [x] A dump is replayable by the release that took it: pg_dump emits the
      client's settings as a preamble, so a client newer than the server writes
      GUCs the server rejects (pg_dump 17 writes `SET transaction_timeout` /
      `SELECT pg_catalog.set_config('transaction_timeout', ...)`, PostgreSQL 16
      refuses it) and wraps the dump in `\restrict` / `\unrestrict` meta-commands
      an older psql rejects. The preamble is rewritten to drop unknown settings
      and those meta-commands; the rewrite STREAMS (a multi-GB dump is never
      read into memory) and stops at the first real statement so function bodies
      are never touched (`backend/src/farna/db/plain_dump.py`;
      `test_plain_dump.py::TestPreambleRewrite`).
- [x] Restore stops the writers itself and leaves them stopped: with
      `--data-dir` it stops a live launcher (refusing when it survives the stop
      request), takes the exclusive lock, and prints the exact
      `farna-control start --data-dir <dir>` command; without `--data-dir` it
      prints the compose start command. The command is printed on EVERY exit —
      success, a handled failure, and an interrupt: `KeyboardInterrupt` (Ctrl-C,
      and the window's signal handlers) is a `BaseException`, so guidance that
      lived only in `except` clauses was never printed on the one path where the
      writers are already stopped (`cli/backup.py::_run_restore`;
      `test_backup.py::TestRestoreStartGuidance::test_an_interrupt_mid_restore_still_prints_the_start_command`,
      prove-the-fix: with the guidance back in the `except` clauses that test
      prints nothing).
      Nothing is auto-restarted, and `--safety-dump-dir` keeps the pre-restore
      safety dump on a persistent path when restore runs in a disposable
      container (`cli/backup.py`;
      `test_backup.py::TestRestoreStartGuidance`).
- [x] The stop does not leave the restore without a database: the launcher's
      ordered teardown stops the bundled PostgreSQL it supervises, so when the
      resolved target IS that bundled cluster the restore starts ONLY the
      cluster (the supervisor's own argv and readiness probe) for the duration
      of the safety dump, the replay, the credential re-encryption, the
      collation check and the posture re-run, then stops it again and leaves the
      writers stopped. A target the stop did not take down (an explicitly named
      external database) is never shadowed by a cluster the restore started, and
      a cluster already accepting connections is reused and left running. The
      target and application URLs come from `--db-url`/`--app-db-url`, an
      exported `DATABASE_ADMIN_URL`/`DATABASE_URL`, or the data dir's own
      `config.env`, in that order — a native operator's shell exports none of
      them (`cli/backup.py::_bundled_cluster_for_restore`,
      `::_target_is_bundled_cluster`, `::_launcher_config_values`;
      `test_restore_bundled_cluster_window.py` — a REAL PostgreSQL 16 stopped
      and restarted under the real `_run_restore`; prove-the-fix: with the
      window removed the replay fails with a connection timeout;
      `test_backup.py::TestNativeRestoreServesItsOwnBundledTarget`,
      `::TestBundledClusterWindowInternals`).
- [x] An interrupted restore does not leave an unsupervised PostgreSQL behind:
      the window's postmaster runs in its own session, so nothing supervises it.
      SIGTERM, SIGINT and SIGHUP are handled for the whole window — each stops
      that cluster before the command unwinds, and a second signal during that
      stop is ignored so the shutdown can finish — and the previous handlers are
      restored afterwards. The window records the child's pid in
      `<data-dir>/restore-window-postmaster.pid` while it is open. SIGKILL can
      be handled by no process: the record is then the recovery, so the next
      `farna-control restore --data-dir` stops that pid itself and says so, and
      `farna-control start --data-dir` refuses naming the pid, the recorded file
      and the `kill -INT <pid>` to run — instead of only "belongs to a live
      postgres process" or a port-in-use failure (`cli/backup.py::_bundled_cluster_for_restore`,
      `::_restore_window_signal_handlers`, `::_reap_leftover_window_postmaster`;
      `launcher/supervisor.py::reconcile_orphans`;
      `test_backup.py::TestBundledClusterWindowInternals::test_a_real_sigterm_stops_the_cluster_before_the_window_unwinds`,
      `::test_a_second_signal_does_not_interrupt_the_stop`,
      `::test_a_leftover_window_postmaster_is_stopped_by_pid_before_the_window`;
      `test_supervisor.py::test_reconcile_names_a_live_restore_window_postmaster_by_pid`).
- [x] Restore refuses a MISSING dump when the archive describes one (a silent
      no-op restore is worse than a refusal), refuses a manifest filename that
      escapes the archive directory (absolute, `..`, or a symlink — the
      checksum loop would otherwise hash an arbitrary file), runs psql with
      `-X` so a `~/.psqlrc` cannot disable `ON_ERROR_STOP`, and its disk
      pre-flight measures the DATABASE's filesystem (`--target-fs` /
      `DB_CAPACITY_PATH`) rather than the archive's. `--dry-run` has no side
      effects: it validates before the writers are stopped or the data-dir lock
      is taken (`cli/backup.py`; `test_backup_restore_semantics.py::
      TestMissingDumpFailsClosed`, `::TestManifestContainment`,
      `::TestDryRunHasNoSideEffects`, `::TestDiskPreflightTarget`,
      `test_backup.py::TestRunPsql`).
- [x] The compose path's recovery VERIFIES before it destroys: the helper
      requires an explicit `--confirm` argument, verifies the archive with a
      side-effect-free `restore --dry-run`, and refuses unless the TARGET
      database records exactly the schema revisions the snapshot was taken from
      (`--verify-target-schema`) — the stand-in for the data-dir instance
      identity guard, which the old no-`--data-dir` invocation skipped
      entirely. Every one-off container overrides the image ENTRYPOINT with
      `--entrypoint farna-control`, because the all-in-one image's
      `/entrypoint.sh` ignores its arguments, migrates and execs supervisord —
      without the override the "dry run" MIGRATED the live database and
      restored nothing. The helper never reads or passes a database URL: the
      one-off container inherits `DATABASE_ADMIN_URL` from the service
      environment and the CLI resolves it there, so no superuser password
      reaches a host argv. The exact start command is printed on failure as
      well as success (`scripts/compose-restore.sh`,
      `test_backup_restore_semantics.py::TestVerifyTargetSchema`,
      `test_compose_restore_wiring.py`).
- [x] `--skip-backup` skips the DUMP, not the recovery point: it refuses unless
      an INTACT snapshot of the CURRENT schema state already exists under the
      data dir — the snapshot's recorded revisions must EQUAL the live
      database's, so a snapshot several migrations stale cannot satisfy the
      requirement — and it reads the LIVE database's revisions (not the old
      bundle's script heads) so an already-migrated-ahead database is still
      detected (`launcher/upgrade.py::_require_existing_snapshot`,
      `live_database_revisions`; `test_upgrade_skip_backup.py`).
- [x] Every upgrade path whose NORMAL migration runs where a snapshot can be
      taken does, through the same helper: the compose entrypoint and the
      supervisor entrypoint call `farna.launcher.upgrade` between the alignment
      guard and the migrations. Fly cannot — its normal migration is the
      `[release]` stage, which runs before any machine boots and takes no
      snapshot. Its boot entrypoint still carries an `alembic upgrade heads`
      fallback (also entered on any probe error), and THAT migration path is
      unguarded, so it is recorded as a known gap below rather than described
      as impossible (`backend/entrypoint.sh`,
      `deploy/supervisor/entrypoint.sh`,
      `test_alignment_entrypoints.py::test_entrypoint_orders_guard_snapshot_migrate`,
      `test_compose_restore_wiring.py::TestTheFlyBootCannotTakeTheSnapshot`).
- [x] The compose release bundle ships `compose-restore.sh` — the one documented
      recovery command for that path — which stops the application, restores
      through a one-off container, and prints the exact start command without
      starting anything (`scripts/compose-restore.sh`,
      `scripts/package_compose_release.py`).

## Accepted Residuals

- The pre-upgrade dump is written by the tenant-serving application container,
  so that process can read and write its own disaster-recovery point. Accepted
  deliberately: the same container already holds the owner/superuser DB URL and
  can read every row, and an unskippable snapshot is worth more than the
  marginal confidentiality gain. Recorded in
  `docs/deployment-security.md` §3.6; the follow-up (a separate one-off
  snapshot container) is out of scope here.

## Known Gaps
- **Fly.io takes no pre-upgrade snapshot, and there is no opt-in.** The Fly
  `[release]` command (`deploy/fly/release.sh`) is the NORMAL migrator and runs
  before any machine boots, so in the normal path the boot entrypoint finds the
  schema at head. A boot-time migration path still exists and is NOT
  snapshot-protected: `deploy/fly/entrypoint.sh` keeps a 10-attempt
  `alembic upgrade heads` fallback that runs when its at-head probe reports the
  database behind — and, because that probe is fail-safe, on any probe error. A
  schema change applied there has no recovery point. Fly is therefore the
  documented exception to "every upgrade path keeps a recovery point", and a Fly
  operator must take an out-of-band backup before a schema-changing deploy.
  Closing the gap means moving the snapshot into the release stage, where the
  normal migration happens. The unsupported `FARNA_CONTROL_SNAPSHOT_DIR` opt-in
  was removed rather than left documented-but-dead.
- The RC matrix (`scripts/verify_compose_upgrade_rollback.py`) rehearses the
  snapshot-restore leg (FAR-2674): it verifies the pre-upgrade snapshot, asserts
  the previous image is refused by the startup guard, restores the snapshot with
  the shipped `compose-restore.sh`, then starts the previous release with
  `compose-rollback.sh` and asserts the data written before the upgrade is still
  present. The green path through the restore is still gated on FAR-2763.
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

- 2026-11-24 (FAR-2667 seventh review round): three findings on the restore
  window. (1) The POSIX-only E2E in `test_upgrade.py` still asserted a restore
  line ending in `--db-url` after `restore_guidance` stopped emitting one, so
  backend-test on Linux (the only platform the native launcher ships on) failed
  while Windows skipped the test. (2) The window's postmaster was spawned with
  `start_new_session=True` and stopped only by the Python `finally`: SIGTERM,
  SIGKILL or a second Ctrl-C during a window that can run ~18 minutes left an
  unsupervised postmaster holding `pgdata` and the port, and the next start
  failed on a stale `postmaster.pid` or a bound port with nothing naming the
  process. SIGTERM/SIGINT/SIGHUP now stop the window before exiting (handlers
  restored, a second signal during the stop ignored), the child's pid is
  recorded in `restore-window-postmaster.pid`, and a later restore stops that
  pid while a later start refuses naming it; SIGKILL has no handler, so the pid
  record is the documented recovery. (3) `farna-control start` guidance lived in
  `except click.ClickException` / `except Exception`, so a Ctrl-C
  (`BaseException`) during the dump or replay exited silently with the writers
  stopped and the database possibly part-restored; the guidance now prints from
  a `finally` on every exit, and a test interrupts the replay and asserts the
  command appears.
- 2026-11-24 (FAR-2667 sixth review round): the native `--data-dir` restore
  stopped the database it then needed. Its own launcher stop is the ordered
  teardown that stops the bundled PostgreSQL, so the safety dump, the replay,
  the credential re-encryption, the collation check and the posture re-run all
  connected to a server the same command had just stopped — and nothing
  restarted it. The earlier "mitigation" (refuse unless an explicit,
  independently reachable `--db-url` is given) only dodged it: a native
  operator's shell has no `DATABASE_ADMIN_URL`, which is also what the printed
  recovery command named, so the documented native recovery could not run.
  Now: the target and application URLs resolve from the data dir's own
  `config.env` when nothing is exported, and when the target IS the bundled
  cluster the restore starts ONLY that cluster for the dump-and-replay window
  and stops it again, leaving the writers stopped. Proven against a REAL
  PostgreSQL 16 with the window removed as the negative control (the replay
  fails with a connection timeout); `restore_guidance` no longer prints a
  `--db-url` the operator does not have.
- 2026-11-23 (FAR-2667 fifth review round): the printed compose rollback command
  did not roll back — the packaged bundle rewrites the image line to a literal
  release tag, so `TAG=<previous-version> docker compose up -d` only set
  `FARNA_CONTROL_VERSION` and restarted the failed release. The instruction is
  now `compose-rollback.sh <previous-version>`, which ships in the bundle and
  merges a one-line image override (pinned by a test that runs the real
  packaging transform). Also: `check_no_downgrade` treated an unreadable target
  migrations tree as "every live revision is ahead" and refused a legitimate
  install with a misleading DOWNGRADE; it is now undecidable-and-proceeds, in
  line with `_pending_for_target`. And the Fly docs overstated the fix: the
  release stage is the normal migrator, but the boot entrypoint's 10-attempt
  `alembic upgrade heads` fallback still exists and is not snapshot-protected,
  so that stays a known gap.
- 2026-11-23 (FAR-2667 fourth review round): the POSIX-only upgrade tests were
  faking `pre_upgrade_dump` without the `target_heads` the production flow now
  passes, so the full-flow, poisoned-artifact, matrix and disk-preflight tests
  raised `TypeError` on Linux CI while the Windows run (which skips them) stayed
  green. Also: the container restore's printed start command named a path that
  does not exist in the shipped bundle and carried no `TAG`; the compose
  quickstart mis-stated the retention rule; the Fly boot carried a
  `FARNA_CONTROL_SNAPSHOT_DIR` opt-in that could never fire (the release stage
  migrates first) — removed, and Fly is documented as unsupported; the native
  pre-flight now runs the FAR-2659 downgrade refusal BEFORE the dump; and the
  dead `_run_dump` producer was deleted.
- 2026-11-22 (FAR-2667 third review round): fixed the nine findings from the
  independent verification pass. The documented compose recovery command did
  not work at all: `docker compose run` never overrode the image ENTRYPOINT, so
  the "read-only verification" ran `/entrypoint.sh`, migrated the live database
  and restored nothing, and the bare token `restore` is not installed (now
  `--entrypoint farna-control --no-deps`). It also required
  `$DATABASE_ADMIN_URL` in the operator's shell — unbound, so `set -u` aborted,
  and exporting it put the superuser password on a host argv (now the script
  references no URL at all; the container resolves it). Seven probes still
  handed the password-bearing DSN to psycopg, so a password with a space or a
  bad percent escape reached stderr (now every connection goes through
  `_connect` and driver error text is scrubbed). Also: the native restore
  prints its start command on the failure path; `--skip-backup` requires a
  snapshot of the CURRENT schema; retention is bounded at two snapshots plus
  the recovery point; the pending check moved inside the shared helper with
  `--target-bundle` so the installer cannot bypass it; and the docs no longer
  claim the RC matrix rehearses the restore (at the time it did not; FAR-2674
  has since taught the harness that leg).
- 2026-11-22 (FAR-2667 review round): fixed the findings from the five-lens
  review. The compose snapshot could not run at all (libpq rejects the
  `postgresql+asyncpg://` scheme the compose file exports; the `/backups` bind
  directory was root-owned while the image runs as `www-data`), the
  snapshot-on-every-boot plus two-snapshot retention could delete the only real
  recovery point (now: only when a migration is pending; retention keeps the two
  newest verified snapshots plus the newest one at the database's current
  schema, and prunes an older-schema snapshot only once it is neither), the
  FAR-2659 alignment guard was dropped from both
  entrypoints (restored, guard -> snapshot -> migrate), and restore reported
  success when `database.sql` was absent (now fails closed). Also hardened:
  the shared dump pipeline's completeness rule (final non-empty line, streamed
  rewrite, set_config / `\restrict` handling), the workspace-vs-retained
  disk guard, snapshot locking, release-segment validation, manifest path
  containment, `psql -X`, the dry-run's side effects, the skip-backup live DB
  probe, and the compose recovery's confirmation/verification contract.

- 2026-11-22 (FAR-2667): made the pre-upgrade snapshot real on both deployment
  paths and made the documented recovery work end to end. Added the disk guard,
  the dump-time completeness proof, two-snapshot retention, the shared
  `take_upgrade_snapshot` callable, the container entrypoint snapshot, the
  restore-side corrupt/foreign refusal, stop-writers-and-leave-stopped
  semantics, and the client-newer-than-server replay rewrite. Verified with a
  real Docker end-to-end: snapshot from PostgreSQL 16, damage the database,
  restore through the documented command and get the rows back; a truncated
  dump and a foreign dump are both refused; the disk guard refuses on a 1 MB
  filesystem; retention keeps the newest two.
- 2026-11-22: Added this tracker after walking the product map and finding the
  shipped backup/restore surface had no entry in either product-map layer.
  Verified each behaviour against `backend/src/farna/cli/backup.py`, the
  `scripts/` pair, the prune and nightly-gate scripts, the nightly workflow and
  the unit suites cited above, then reconciled the operator contract in
  `docs/operations/backup.md`. No product code changed in this pass.
