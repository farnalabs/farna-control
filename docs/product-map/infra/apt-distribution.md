---
id: feat-apt-distribution
prd: N/A
adr: []
code:
  - scripts/linux/build-deb.sh
  - scripts/linux/validate-deb.sh
  - scripts/linux/build-apt-repo.sh
  - scripts/linux/plan-apt-repo.sh
  - scripts/linux/apt-repo-lib.sh
  - deploy/apt/farna-control-archive-keyring.asc
  - .github/workflows/linux-deb.yml
  - .github/workflows/apt-repo.yml
  - .github/workflows/release-unified.yml
  - docs/system-requirements.md
unit-tests:
  - backend/tests/unit/scripts/test_linux_deb_contract.py
  - backend/tests/unit/scripts/test_apt_repo_contract.py
  - backend/tests/unit/test_release_workflow_contract.py
bdd:
depends-on: []
status: covered
---

# Linux apt Distribution

FAR-2579 ships the Linux install path as a Debian package plus a signed apt
repository at `https://apt.farna.dev`. Operators on Ubuntu 22.04 / 24.04 and
Debian 12 or newer can install with `apt`, and `apt upgrade` follows releases
without the in-app upgrader rewriting files the package manager owns. This is an
infra-only surface: it has no manifest route and no in-product UI, so it is
tracked here rather than in the manifest `features:` registry.

## Behaviors

- [x] The release bundle is packaged as the Debian-canonical
      `farna-control_<version>_amd64.deb` (plus a `.sha256`) by
      `scripts/linux/build-deb.sh`, which refuses a bundle whose name, top-level
      directory, `VERSION` file or internal `SHA256SUMS` do not verify before
      anything is staged (`test_linux_deb_contract.py::test_builder_output_name_is_debian_canonical`,
      `::test_builder_verifies_bundle_checksums_before_packaging`).
      <!-- behaviour: apt-distribution-01 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_builder_output_name_is_debian_canonical -->
- [x] The package layout mirrors the tarball install: the bundle is unpacked
      under `/opt/farna-control/versions/<version>` with a
      `/opt/farna-control/current` symlink and a `/usr/bin/farna-control`
      wrapper that execs the current launcher, so the launcher's derived install
      root is `/opt/farna-control` rather than `/opt`
      (`build-deb.sh`; `test_linux_deb_contract.py::test_builder_lays_bundle_under_opt_versions_with_current_symlink`,
      `::test_builder_installs_wrapper_at_usr_bin`, `::test_wrapper_execs_current_launcher`).
      <!-- behaviour: apt-distribution-02 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_builder_lays_bundle_under_opt_versions_with_current_symlink -->
- [x] Package dependencies are computed at build time from the system libraries
      the shipped ELF files actually link, including a versioned `libc6` floor
      taken from the scanned `GLIBC_` symbols, plus `ca-certificates` and
      `openssl`; any soname that cannot be resolved to a package fails the
      build instead of shipping a package that cannot start
      (`build-deb.sh`; `test_linux_deb_contract.py::test_base_depends_are_declared`,
      `::test_libc6_is_versioned_from_the_scanned_glibc_floor`,
      `::test_unresolvable_system_libraries_fail_the_build`).
      <!-- behaviour: apt-distribution-03 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_base_depends_are_declared -->
- [x] The package installs no daemon and runs nothing as root. Maintainer
      scripts never read or delete a user data directory (remove and purge leave
      `~/.local/share/modulo/data` intact), all deletions stay inside
      `/opt/farna-control`, and an unknown maintainer action is refused
      (`build-deb.sh`; `test_linux_deb_contract.py::test_maintainer_script_never_references_user_data`,
      `::test_maintainer_script_deletions_stay_inside_opt`,
      `::test_postrm_purge_leaves_a_data_dir_untouched`,
      `::test_maintainer_script_rejects_unknown_action`).
      <!-- behaviour: apt-distribution-04 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_maintainer_script_never_references_user_data -->
- [x] The wrapper pins `FARNA_CONTROL_INSTALL_ROOT` so a sourced tarball
      `env.sh` cannot redirect it, refuses the in-app `farna-control upgrade`
      (apt owns those files) and points at apt, and notes a coexisting tarball
      install on `start` (`build-deb.sh`; `test_linux_deb_contract.py::test_wrapper_overrides_an_inherited_tarball_install_root`,
      `::test_wrapper_refuses_the_in_app_upgrader`,
      `::test_wrapper_notes_a_coexisting_tarball_install_on_start`).
      <!-- behaviour: apt-distribution-05 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_wrapper_overrides_an_inherited_tarball_install_root -->
- [x] `.github/workflows/linux-deb.yml` is both reusable (release-unified passes
      a prebuilt bundle) and dispatchable (build from a ref, or package a
      published `bundle-vX.Y.Z` after verifying it against the release
      `SHA256SUMS`). Packaging runs in an `ubuntu:22.04` container so the
      computed `Depends` resolve against the oldest supported release, and the
      result is validated in fresh `ubuntu:22.04` and `ubuntu:24.04` containers
      (`test_linux_deb_contract.py::test_workflow_is_reusable_and_dispatchable`,
      `::test_workflow_packages_on_the_glibc_baseline`,
      `::test_workflow_validates_on_both_supported_ubuntu_releases`).
      <!-- behaviour: apt-distribution-06 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_workflow_is_reusable_and_dispatchable -->
- [x] Validation installs the package, starts the stack as a NON-root user,
      checks `/healthz`, the SPA index and both SAQ workers, stops the stack,
      then removes and purges while asserting the user data directory survives
      each step (`scripts/linux/validate-deb.sh`;
      `test_linux_deb_contract.py::test_validation_boots_as_non_root_and_keeps_data_through_purge`).
      <!-- behaviour: apt-distribution-07 | tests: backend/tests/unit/scripts/test_linux_deb_contract.py::test_validation_boots_as_non_root_and_keeps_data_through_purge -->
- [x] The .deb is a mandatory release artefact, not an optional side build: a
      dry-run rehearses packaging without publishing, the final manifest
      requires and hashes it, publication refuses when the deb job did not
      succeed, the final release set attaches it, and the build-provenance
      attestation covers that same set
      (`.github/workflows/release-unified.yml`;
      `test_release_workflow_contract.py::test_release_builds_the_deb_from_the_release_bundle`,
      `::test_dry_run_rehearses_the_deb_without_publishing`,
      `::test_final_manifest_requires_and_covers_the_deb`,
      `::test_publish_release_requires_and_attaches_the_deb`,
      `::test_attestation_covers_the_final_release_set_that_carries_the_deb`).
      <!-- behaviour: apt-distribution-08 | tests: backend/tests/unit/test_release_workflow_contract.py::test_release_builds_the_deb_from_the_release_bundle -->
- [x] `https://apt.farna.dev` is a static Cloudflare Pages site (project
      `farna-control-apt`) built by `scripts/linux/build-apt-repo.sh`: a
      clearsigned `InRelease`, detached `Release.gpg`, `Packages`/`Packages.gz`,
      the binary and armored archive keyring, a short-cache `_headers`, an
      install page and `_redirects`. The repository is signed with the archive
      key whose public half is committed at
      `deploy/apt/farna-control-archive-keyring.asc`, and the builder refuses to
      sign with any key whose fingerprint does not match that reviewed file
      (`test_apt_repo_contract.py::test_release_is_clearsigned_and_detached_signed`,
      `::test_builder_proves_both_signatures_against_the_published_keyring`,
      `::test_builder_enforces_the_reviewed_key_fingerprint`,
      `::test_committed_key_is_public_only`).
      <!-- behaviour: apt-distribution-09 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_release_is_clearsigned_and_detached_signed -->
- [x] `.deb` files are not hosted on the Pages site (Pages caps a file at
      25 MiB): `Packages` records the release asset's size and hashes on a pool
      path, and `_redirects` sends apt to the GitHub release download URL, so
      apt fetches the exact bytes the index describes
      (`build-apt-repo.sh`; `test_apt_repo_contract.py::test_redirects_target_the_release_download_url`,
      `::test_builder_never_publishes_deb_files`).
      <!-- behaviour: apt-distribution-10 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_redirects_target_the_release_download_url -->
- [x] Every published release that carries a `.deb` stays in the index (newest
      first), so older versions remain installable, pinnable and downgradable.
      An incremental refresh reuses a published stanza only when the live
      `InRelease` verifies against the reviewed key, the signed `Packages` hash
      matches, and the stanza's `SHA256` equals the release `SHA256SUMS` entry;
      every new or unconfirmed version is downloaded and verified against the
      release `SHA256SUMS` before the builder hashes it. An unreachable or
      unverifiable live index falls back to a full download
      (`scripts/linux/plan-apt-repo.sh`, `apt-repo-lib.sh`;
      `test_apt_repo_contract.py::test_planner_verifies_the_live_index_before_reusing_it`,
      `::test_incremental_refresh_reuses_indexed_versions_and_downloads_only_new_ones`,
      `::test_incremental_refresh_discards_a_stanza_the_release_sha256sums_does_not_confirm`,
      `::test_incremental_refresh_ignores_a_live_index_signed_by_another_key`).
      <!-- behaviour: apt-distribution-11 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_planner_verifies_the_live_index_before_reusing_it -->
- [x] The refresh runs on a published release and on manual dispatch; release
      publishers that create a release with a workflow `GITHUB_TOKEN` (which
      raises no `release` event for other workflows) explicitly dispatch it
      (`apt-repo.yml`; `test_apt_repo_contract.py::test_workflow_triggers_on_release_and_dispatch`,
      `::test_release_publishers_dispatch_the_refresh`).
      <!-- behaviour: apt-distribution-12 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_workflow_triggers_on_release_and_dispatch -->
- [x] A post-deploy check installs the repository exactly as documented on
      fresh `ubuntu:22.04` and `ubuntu:24.04`: it waits for the new index, adds
      the keyring and `signed-by` source, fails on any apt signature warning,
      installs the expected version, asserts `farna-control --version` reports
      it, then boots the stack as a non-root user and exercises remove/purge
      (`apt-repo.yml`; `test_apt_repo_contract.py::test_post_deploy_check_installs_the_documented_way_on_both_releases`).
      <!-- behaviour: apt-distribution-13 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_post_deploy_check_installs_the_documented_way_on_both_releases -->
- [x] The repository workflow keeps the supply-chain guards: actions are pinned
      by commit SHA, permissions are read-only, signing material comes from
      secrets rather than literals, and `wrangler` is pinned to an exact version
      and project (`test_apt_repo_contract.py::test_workflow_actions_are_pinned_by_sha`,
      `::test_workflow_permissions_are_read_only`,
      `::test_workflow_uses_secrets_not_literals`,
      `::test_wrangler_is_pinned_to_an_exact_version_and_project`).
      <!-- behaviour: apt-distribution-14 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_workflow_actions_are_pinned_by_sha -->
- [x] `docs/system-requirements.md` carries the install, upgrade, pinning and
      air-gapped `.deb` instructions, the archive fingerprint, the package
      layout and the remove/purge data-safety contract, and the documented key
      fingerprint and `signed-by` keyring path match the committed key and the
      builder's install page (`test_apt_repo_contract.py::test_docs_install_the_keyring_where_signed_by_points`,
      `::test_docs_cover_upgrade_and_pinning`,
      `::test_docs_fingerprint_matches_the_committed_public_key`,
      `::test_builder_install_page_uses_the_same_sources_line`).
      <!-- behaviour: apt-distribution-15 | tests: backend/tests/unit/scripts/test_apt_repo_contract.py::test_docs_install_the_keyring_where_signed_by_points -->

## Known Gaps

- Only `amd64` is packaged. There is no `arm64` `.deb` or arm64 apt index, so
  arm64 hosts stay on the tarball/DMG paths.
- The default CI validation covers install, boot, health and remove/purge within
  about a minute. The longer assertions (the stack stays healthy for 180
  seconds and an admin login succeeds) are opt-in through the workflow's
  `full_check` input, so a regression that only appears after the first minute
  is not gated by the default run.
- The repository publishes a single `stable` suite/component. There is no
  separate prerelease or LTS channel, and no in-product download or package
  status surface.
- The public `apt.farna.dev` hostname can lag its Pages deployment; the
  post-deploy check falls back to the `*.pages.dev` hostname with a warning
  while DNS or the custom domain catches up.

## QA History

- 2026-11-21: Added this tracker after the FAR-2579 apt/.deb work (PRs #3282,
  #3285, #3290 and the release-wiring commits) shipped with no entry in either
  product-map layer. Verified each behaviour against the builder and validation
  scripts, both workflows, the release-unified wiring, the committed archive key
  and the contract tests cited above. No product code changed in this pass.
