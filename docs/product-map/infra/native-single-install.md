---
id: feat-native-single-install
prd: N/A
adr: [ADR 031 (single-install native distribution)]
code:
  - scripts/install.sh
  - backend/src/farna/launcher/upgrade.py
  - backend/src/farna/launcher/manifest.py
  - backend/src/farna/launcher/entry.py
  - backend/src/farna/launcher/service.py
  - backend/src/farna/launcher/supervisor.py
  - backend/src/farna/launcher/initdb.py
  - scripts/bundle-versions.env
  - scripts/bundle-versions-windows.env
  - scripts/macos/build-native-bundle.sh
  - scripts/macos/install.sh
  - scripts/macos/notarize-and-verify.sh
  - scripts/macos/staple-and-verify.sh
  - scripts/macos/artefact-identity.sh
  - scripts/macos/macho-inventory.sh
  - scripts/macos/signature-diagnostics.sh
  - scripts/macos/install-pinned-shellcheck.sh
  - scripts/create-macos-info-plist.sh
  - scripts/windows/build-launcher.ps1
  - scripts/windows/modulo-launcher.c
  - scripts/linux/native-firstrun-smoke.sh
  - scripts/test-windows-installer.ps1
  - deploy/windows/modulo.iss
  - deploy/windows/INSTALLER-INFO.txt
  - deploy/windows/BUNDLE-BUILD.md
  - .github/workflows/bundle-release.yml
  - .github/workflows/macos-installer.yml
  - .github/workflows/macos-staple-retry.yml
  - .github/workflows/windows-artifacts.yml
  - .github/workflows/windows-installer.yml
  - .github/workflows/native-gate0.yml
  - .github/workflows/native-release-smoke.yml
  - .github/workflows/release-unified.yml
  - docs/system-requirements.md
  - docs/upgrade-process.md
unit-tests:
  - backend/tests/unit/scripts/test_install_manifest_verification.py
  - backend/tests/unit/scripts/test_install_activation_file.py
  - backend/tests/unit/scripts/test_install_support_block.py
  - backend/tests/unit/scripts/test_install_unreadable_cwd.py
  - backend/tests/unit/scripts/test_bundle_release_security.py
  - backend/tests/unit/scripts/test_windows_artifact_provenance.py
  - backend/tests/unit/scripts/test_windows_installer_service.py
  - backend/tests/unit/test_windows_installer_inno_setup_step.py
  - backend/tests/unit/test_macos_installer_normalize.py
  - backend/tests/unit/test_release_workflow_contract.py
  - backend/tests/unit/tools/test_gate0_paths.py
  - backend/tests/unit/launcher/test_manifest.py
  - backend/tests/unit/launcher/test_upgrade.py
  - backend/tests/unit/launcher/test_upgrade_matrix.py
  - backend/tests/unit/launcher/test_service.py
  - backend/tests/unit/launcher/test_supervisor.py
  - backend/tests/unit/launcher/test_native_shim_env_contract.py
  - backend/tests/unit/launcher/test_entry.py
bdd:
depends-on: []
status: covered
---

# Native Single-Install Distribution

Farna Control ships a native single-install path alongside Docker Compose and the
Debian package: a self-contained Linux tarball bundle installed by
`scripts/install.sh`, a signed and notarized macOS `.app` delivered in a `.dmg`,
and a Beta-only Windows payload plus Inno Setup installer. Each bundle carries
its own CPython runtime, PostgreSQL server and Redis/Valkey binaries, so no
host-level language or database runtime is required. The operator contract and
platform matrix live in `docs/system-requirements.md` and
`docs/upgrade-process.md`. This is an infra-only surface: the native install has
no manifest route and no in-product UI, so it is tracked here rather than in the
manifest `features:` registry.

## Behaviors

- [x] The Linux bundle is assembled from pinned upstream inputs, not host
      packages: python-build-standalone CPython 3.12.14, PostgreSQL 16.15 built
      from source, and Redis 8.0.2 built from source, with every download
      verified against its pinned SHA-256 before use. The resulting artifact has
      one top-level directory and ships `VERSION`, an internal `SHA256SUMS`, the
      exact executable `install.sh`, the launcher, and the `runtime/`, `pg/`,
      `redis/`, `backend/` and `frontend-dist/` trees
      (`scripts/bundle-versions.env`, `.github/workflows/bundle-release.yml`;
      `test_bundle_release_security.py::test_bundle_archive_urls_and_hashes_match_lockfile_exactly`,
      `::test_download_verified_checksum_mismatch_fails_without_consumable_destination`,
      `::test_bundle_assembles_the_exact_executable_installer_and_internal_checksum`).
- [x] The Linux installer verifies the release's ed25519-signed manifest over
      its exact bytes against an embedded current+next trust store, then verifies
      the tarball SHA-256 against that manifest before extracting anything. An
      unknown key id, malformed envelope, tampered manifest or unlisted artifact
      is refused. `--from-file` requires the operator to supply the adjacent
      manifest and `.sig`, and cannot add trust keys
      (`scripts/install.sh`, `backend/src/farna/launcher/manifest.py`;
      `test_install_manifest_verification.py::test_extracted_argv_verifies_a_signed_manifest`,
      `::test_extracted_argv_rejects_a_tampered_manifest`,
      `::test_extracted_argv_rejects_a_wrong_trust_key`,
      `::test_from_file_bare_filename_resolves_the_manifest_beside_the_tarball`,
      `test_manifest.py::test_shipped_trust_store_has_only_the_provisioned_current_key`,
      `::test_installer_embedded_store_matches_trust_rows`).
- [x] A verified bundle installs under a user-writable versioned root
      (`~/.local/opt/farna-control/versions/<version>`), with `current` as the
      only version selector and a `~/.local/bin/farna-control` shim that points
      at it. The installer writes a path-only `env.sh` activation helper, prints
      the `source` command for the caller, and never edits the parent shell
      (`scripts/install.sh`; `test_install_activation_file.py::test_finalise_writes_a_usable_env_sh_under_set_u`).
- [x] A pre-rename install (`~/.local/opt/modulo` plus the old shim) is migrated
      in place to the Farna Control root and marker, while an unrelated
      same-named shim is left alone and a host carrying both install roots is
      refused rather than guessed
      (`scripts/install.sh`; `test_install_activation_file.py::test_a_pre_rename_install_root_and_shim_are_migrated`,
      `::test_an_unrelated_modulo_shim_is_left_untouched`,
      `::test_two_install_roots_is_refused_rather_than_guessed`).
- [x] `scripts/install.sh` refuses macOS with a pointer to the macOS installer
      and any other non-Linux host with an explicit unsupported-platform error.
      It anchors every caller-supplied relative path to the invocation directory
      and leaves that directory before running, so an unreadable working
      directory cannot break the install
      (`scripts/install.sh`; `test_install_unreadable_cwd.py::test_install_from_a_cwd_without_search_permission`,
      `::test_relative_from_file_resolves_against_the_invocation_directory`,
      `::test_install_sh_anchors_every_path_override`).
- [x] Every installer failure prints one delimited support block last, naming the
      stage, the absolute log path and a stable support reference. The transcript
      is written with private permissions, capped while keeping its tail, and only
      the newest five logs are retained
      (`scripts/install.sh`; `test_install_support_block.py::test_support_block_names_target_stage_log_and_reference`,
      `::test_transcript_is_persisted_with_private_permissions`,
      `::test_only_the_newest_five_logs_are_kept`,
      `::test_oversized_transcript_is_capped_and_keeps_the_tail`).
- [x] An upgrade enforces a pre-upgrade `pg_dump` through the old bundle's
      runtime: the dump goes to a versioned snapshot inside the data dir, must be
      non-empty, and any failure aborts before the binary swap. The database
      password never appears in argv, a live holder on the data-dir lock is
      refused, and a stop-then-rerun flow requires `--skip-backup` as an explicit
      acknowledgement that a verified snapshot already exists. A failed boot
      reports the snapshot path and the exact manual restore command
      (`backend/src/farna/launcher/upgrade.py`, `scripts/install.sh`;
      `test_upgrade.py::test_pre_upgrade_dump_reports_snapshot`,
      `::test_dump_password_is_never_in_argv`,
      `::test_perform_upgrade_live_process_aborts`,
      `::test_perform_upgrade_skip_backup_runs_no_dump`,
      `::test_perform_upgrade_boot_failure_prints_snapshot_and_restore`).
- [x] The in-product `farna-control upgrade <target-version>` performs the same
      verified flow: it stages and re-verifies the signed release, rejects a
      mismatched top-level identity, a tampered tarball, a PostgreSQL major
      mismatch, a schema downgrade and a full disk before swapping, atomically
      re-points `current`, writes an upgrade marker before boot, and prunes old
      versions without removing the referenced one
      (`backend/src/farna/launcher/upgrade.py`;
      `test_upgrade.py::test_perform_upgrade_full_flow`,
      `::test_extract_rejects_mismatched_top_level_identity`,
      `::test_verify_release_stage_refuses_a_tampered_tarball`,
      `::test_perform_upgrade_pg_major_mismatch_refuses`,
      `::test_perform_upgrade_downgrade_refuses`,
      `::test_perform_upgrade_disk_preflight_refuses`,
      `::test_prune_versions_keeps_last_two_and_never_the_referenced`).
- [x] The macOS builder produces a relocatable `.app` from verified inputs: it
      checks the downloaded Python is the expected Mach-O architecture and
      version, builds PostgreSQL and Redis into the bundle, ships the built SPA,
      creates the backend virtualenv with `--copies` and a relative `.pth`, and
      fails on any symlink that escapes the bundle. The build gate resolves the
      directory the compiled launcher actually exports and requires every bundled
      binary there (`scripts/macos/build-native-bundle.sh`;
      `test_macos_installer_normalize.py::test_macos_bundle_build_gate_passes_for_the_shipped_producer_contract`,
      `::test_macos_bundle_build_gate_fails_on_an_env_name_drift`,
      `::test_macos_bundle_build_gate_fails_when_a_required_binary_is_missing`,
      `::test_macos_bundle_layout_binaries_resolve_at_the_launcher_exported_directory`).
- [x] The macOS workflow signs the `.app` inside-out, notarizes the DMG and
      proves the verdict: the artifact identity is recorded before and after the
      submit and must not change, a non-`Accepted` verdict fails loudly with the
      notary log, and a `--wait` timeout is treated as `PENDING` (polling by
      submission id), never as success. Nothing re-signs or rewrites the DMG after
      notarization, and the published DMG is stapled and validated
      (`scripts/macos/notarize-and-verify.sh`, `scripts/macos/staple-and-verify.sh`,
      `scripts/macos/artefact-identity.sh`, `scripts/macos/macho-inventory.sh`;
      `test_macos_installer_normalize.py::test_macos_dmg_contract_structurally_preserves_signing_notarization_and_gatekeeper_order`,
      `::test_macos_notarization_fails_loudly_on_a_non_accepted_status`,
      `::test_macos_nothing_resigns_or_rewrites_the_dmg_after_notarization`,
      `test_release_workflow_contract.py::test_macos_staple_retry_invokes_the_idempotent_helper`).
- [x] `scripts/macos/install.sh` verifies the DMG with `hdiutil verify` and
      `codesign`, requires exactly one non-symlink `.app`, checks the mounted
      launcher's Mach-O architecture against both the host and the artifact name,
      assesses it with Gatekeeper, then copies it and proves the copied bundle can
      import the supervisor in a clean environment. It writes and loads the
      per-user LaunchAgent plist, and normalizes a duplicated `.dmg` extension
      (`scripts/macos/install.sh`; `test_macos_installer_normalize.py::test_install_sh_normalizes_duplicate_dmg_extension`,
      `::test_macos_dmg_contract_structurally_verifies_staging_and_post_mount_gates`,
      `::test_macos_mounted_app_codesign_agrees_with_gatekeeper_strictness`).
- [x] The Windows payload is staged from pinned Windows inputs (CPython, the
      PostgreSQL Windows build, and a pinned Valkey release) and is
      byte-identical when staged twice without nesting, with the runtime marker
      the installer requires. The launcher runs with a scrubbed environment and
      its command-line size accounting includes the fixed invocation
      (`scripts/bundle-versions-windows.env`, `scripts/windows/build-launcher.ps1`,
      `scripts/windows/modulo-launcher.c`, `.github/workflows/windows-artifacts.yml`;
      `test_windows_artifact_provenance.py::test_pins_are_strict_and_consistent`,
      `::test_staged_bundle_writes_the_runtime_marker_the_installer_requires`,
      `::test_staging_twice_is_byte_identical_and_never_nests`,
      `::test_launcher_clean_environment_contract`).
- [x] The Windows Inno Setup installer copies the bundle launcher, creates
      Start Menu and optional desktop shortcuts, and deliberately registers no
      Windows service (`sc.exe` is never invoked and there is no uninstall run
      section). Its post-install stage records the Beta limitation without
      offering it as a tickable task or aborting, and architecture identifiers use
      the non-deprecated, 6.3-gated form. The workflow compiles the installer on a
      preinstalled Inno Setup or a Chocolatey fallback, signs it with Azure
      Trusted Signing, and attests the build provenance
      (`deploy/windows/modulo.iss`, `deploy/windows/INSTALLER-INFO.txt`,
      `.github/workflows/windows-installer.yml`;
      `test_windows_installer_service.py::test_the_installer_never_invokes_sc_exe`,
      `::test_the_installer_declares_and_copies_the_bundle_launcher`,
      `::test_the_info_file_states_the_coming_soon_limitation`,
      `::test_architecture_identifiers_are_gated_on_the_6_3_boundary`,
      `test_windows_installer_inno_setup_step.py::test_both_jobs_have_an_inno_setup_install_step`).
- [x] The release pipeline gates the native artifacts end to end: the Linux
      bundle and `.deb` are built, the macOS DMGs are built on both native
      architectures, and the Windows installer is included only when the
      Beta-only `include_windows` input is true. Publication fails when a
      dependency did not succeed, and the final release manifest hashes each
      artifact without loading it into memory
      (`.github/workflows/release-unified.yml`, `.github/workflows/bundle-release.yml`;
      `test_release_workflow_contract.py::test_publish_release_refuses_when_a_dependency_did_not_succeed`,
      `::test_final_release_manifest_hashes_each_artefact_without_loading_it_into_memory`,
      `::test_release_builds_the_deb_from_the_release_bundle`).
- [x] Native-specific verification runs against the real artifacts: Gate 0
      proves an offline `uv` resolution and a no-egress boot import in a
      minimum-glibc container on pull requests, and the release smoke test
      installs the exact artifact through the signed installer, boots it, checks
      health/SPA/API/MCP/WebSocket, runs `doctor`, proves zero egress, round-trips
      a backup and restore, and verifies orphan reaping and recovery
      (`.github/workflows/native-gate0.yml`, `.github/workflows/native-release-smoke.yml`,
      `scripts/linux/native-firstrun-smoke.sh`;
      `test_gate0_paths.py::test_gate0_covers_bundled_boot_critical_modules`,
      `::test_gate0_covers_bundled_boot_critical_directories`).

## Known Gaps

- The Linux installer requires `python3` (to read the signed manifest), OpenSSL
  3.0+ (for ed25519 `pkeyutl -rawin`) and `sha256sum` on the host. A minimal
  host without them is refused rather than falling back to an unverified install.
- The macOS installer verifies the DMG and `.app` through `hdiutil`, `codesign`
  and Gatekeeper, not the ed25519 release manifest. A macOS install therefore
  depends on Apple signing/notarization rather than the cross-platform manifest
  trust chain.
- The Windows path is Beta scope and opt-in only (`include_windows`, default
  false). No signed Windows installer is published yet, the installer registers
  no Windows service, and real-host validation is still outstanding, so a native
  Windows install is not a supported production target.
- The native single-install supports Linux (amd64/arm64), macOS arm64 and macOS
  x86_64. The Linux bundle targets a glibc 2.35 floor and is not a musl
  distribution; Alpine and other musl hosts need the container path.
- The native install has no in-product installer download or update-check UI.
  Upgrades are run through `scripts/install.sh` or `farna-control upgrade`, and
  a failed boot is reported with a manual restore command rather than an
  automatic rollback.
- The Linux arm64 bundle and the macOS x86_64 DMG are built and smoke-tested by
  their workflows, but the default release path still centres on the amd64 Linux
  bundle and both macOS architectures; there is no universal macOS artifact.

## QA History

- 2026-11-22: Added this tracker after walking the product map and finding the
  shipped native single-install distribution (ADR 031) had no entry in either
  product-map layer, even though the Linux bundle installer, macOS DMG pipeline,
  Windows Beta payload and the launcher/upgrade machinery all ship. Verified
  each behaviour against the installer, launcher, macOS and Windows scripts, the
  four build/release workflows, `docs/system-requirements.md` and
  `docs/upgrade-process.md`, then cited the contract tests that pin them. No
  product code changed in this pass.
