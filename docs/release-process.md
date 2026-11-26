# Release Process

How we cut, validate and promote a release of Farna Control - and how to rehearse the whole thing without publishing anything.

---

## The single entry point

Every release action runs through `.github/workflows/release-unified.yml` ("Release: Unified Pipeline"). This is the only executable release entry point. The legacy `.github/workflows/release.yml` is a read-only, fail-closed migration wrapper that exists only to direct old callers here; it has no write permissions or release-mutating steps, so it cannot create a tag, release, freeze marker, or package, and operators using an old bookmark must re-run the command against the unified workflow. It is `workflow_dispatch` only - it never runs on a push - and takes the inputs below:

| Input | Meaning |
|-------|---------|
| `mode` | `cut`, `recut`, `promote`, or `dry-run` |
| `version` | The plain numeric version being released, e.g. `0.4.0` |
| `freeze` | When `true`, `cut`/`recut` establish the authoritative `release-freeze` branch. Defaults to `false` while the pipeline is being tested, so a default dispatch tags and publishes the RC without pausing the merge queue; `promote` still refuses to run without an active freeze. |
| `include_windows` | Beta-only opt-in for the signed Windows installer; defaults to `false`. |

**The unified pipeline owns every publication.** `release-unified.yml` pushes the four container images (and their versioned tags) to GHCR, pushes the Helm chart to the OCI registry, builds and signs the Linux release bundle through `bundle-release.yml`, builds and notarizes separate native macOS arm64 and x86_64 `.dmg` artifacts on `macos-15` and `macos-15-intel`, builds the native Windows payload (`windows-artifacts.yml`), then compiles, signs (Azure Trusted Signing) and attests the Windows installer (`windows-installer.yml`) before generating the final `SHA256SUMS` and signed `RELEASE_MANIFEST.json`. Windows native installer support is Beta scope (delivered by FAR-2337 / FAR-2338) and is not built on the Alpha release path: it is included in the release manifest and `SHA256SUMS` only when the Beta-only `include_windows` input is explicitly set to true (it defaults to false); its publication depends on a release running (no signed Windows installer is published yet), and real-host validation under FAR-2250 is still outstanding. It creates the GitHub Release from that final manifest/checksum chain, pushes the `bundle-v*` tag, publishes `farna-control` to PyPI on `promote`, and dispatches Deploy plus `rc-validate`. macOS arm64 and Intel x86_64 are supported as separate native artifacts; Rosetta is not required.

**The signed manifest is the provenance record.** The signed `RELEASE_MANIFEST.json` records the exact source commit the publishing job checked out and built from: schema v1's `generated_at` field carries that commit SHA, not a timestamp and not the workflow's dispatch ref. The immutable `bundle-v<VERSION>` tag is a version anchor, not a per-recut commit pointer - a recut republishes the signed manifest, so what was actually built is read from the signed manifest, never inferred from the tag's commit.

**Slow Apple notarization holds the release; it does not fail it.** A macOS DMG is published only once a notarization ticket is stapled to it and verified (FAR-2573). If Apple is still processing the submission when the macOS job's notary budget (a 90-minute `notarytool --wait` plus a 45-minute `notarytool info` poll) runs out, the job ends with a PENDING outcome instead of failing: the unstapled DMG still passes every non-ticket gate, publication is HELD, and `macos-staple-retry.yml` staples and publishes the release once Apple accepts it. The Apple submission id is kept in the run's `macos-notary-submission-<version>-<arch>` artifact. An `Invalid` or `Rejected` verdict is never retried: it fails the macOS job at build time, or fails the staple retry loudly (and stops its re-dispatch) if it arrives later (FAR-2577).

The workflow defaults to workflow-level `permissions: contents: read`, with write scopes granted only on the job that needs them: the `release` job carries `contents: write` (push tags and the CHANGELOG commit, create the GitHub Release) and `actions: write` (`gh workflow run rc-validate.yml`). The `dry-run` and build jobs are read-only. A `release-unified-${{ github.ref }}` concurrency group with `cancel-in-progress: false` ensures two release actions can never interleave.

## The four modes

| Mode | What it does | Publishes? |
|------|--------------|------------|
| `cut` | Establishes a temporary publication hold, bumps the version on `main`, tags `vX.Y.Z-rcN`, establishes the authoritative `release-freeze` when `freeze=true`, publishes four RC-qualified container images (including `runner-opencode`), builds/signs the release bundle, pushes the bundle tag, creates the GitHub Release, and dispatches forced deploy plus `rc-validate.yml` | Yes - RC tag, freeze (when `freeze=true`), four RC images, signed bundle/artifacts, GitHub Release, bundle tag, forced deploy + validation |
| `recut` | After a fix has been manually merged to `main`: tags the *next* RC, re-freezes when `freeze=true`, republishes the four RC-qualified images and signed release artifacts, and dispatches the forced soak and validation again | Yes - next RC tag, freeze (when `freeze=true`), four RC images, signed bundle/artifacts, GitHub Release, bundle tag, forced deploy + validation |
| `promote` | Promotes the validated RC to GA: creates and pushes the `vX.Y.Z` tag, retags all four images as `VERSION_NUM` and `latest`, pushes the stable Helm chart, updates `CHANGELOG.md`, creates or updates the GitHub Release, lifts the freeze, and publishes `farna-control` to PyPI | Yes - stable `VERSION_NUM`/`latest` images and Helm chart only here, plus GA tag, Release and PyPI package |
| `dry-run` | Builds everything and resolves what a `cut` would have done, then stops | **No - no publication, tags, commits, registry pushes, Release, attestation, or deploy** |

### The freeze

`cut` first pushes a temporary `release-publication-hold` marker branch before its version-bump push; `publish-images.yml` observes that marker and refuses stable `latest` while allowing the immutable SHA publication to proceed. It then pushes the authoritative `release-freeze` branch when the `freeze` input is `true` (the default is `false` while the pipeline is being tested), which the merge queue checks and does not merge while it is present, and removes the temporary hold. Because `promote` requires an active freeze, a cut or recut dispatched with `freeze=false` can publish an RC but cannot be promoted; dispatch with `freeze=true` for a release intended to reach GA. A cut chooses the first free `vX.Y.Z-rcN`; a recut always chooses the next free RC number and tags the current `main` commit, so it cannot accidentally republish an older RC after a fix has landed. The freeze stops merges, not deploys: the release workflow force-dispatches `Deploy: Staging + Prod` from `main` with `force_deploy=true` and the RC tag as `release_ref`/`release_sha`; dispatching from the RC tag would give each RC its own `deploy-pipeline-${{ github.ref_name }}` concurrency group and allow two forced deploys to race the same Fly apps. The deploy workflow checks out the RC commit explicitly from `release_ref`. The deploy workflow's normal staging, E2E and production gates remain in force. `promote` deletes the authoritative freeze branch, which lets the merge queue resume on its next tick. Do not add release-freeze awareness to `deploy.yml`.

Both markers are branches under `refs/heads/`, not tags, so the protected-tag ruleset no longer covers them. The repository must carry a branch ruleset protecting `release-freeze` and `release-publication-hold` that restricts force-push and deletion to the release-bot identity (and admins); without it any write collaborator could move or delete the freeze. A repository ruleset is GitHub configuration, not workflow code, so it has to be applied with the repository settings before a real cut - the workflows cannot create it.

If a `cut` or `recut` fails, a dedicated finalizer job clears `release-freeze` automatically so the merge queue does not remain stranded. The finalizer checks live ref state rather than upstream step outcomes: it confirms `bundle-v<VERSION>` is unpublished, proves the freeze points to one of this release's RC commits before touching it, and leaves any freeze belonging to another version alone.

### How long a freeze may last

A freeze is deliberate, but it is **not unbounded**: the intended maximum is **4 hours** from the moment `release-freeze` is pushed until `promote` deletes it. That window covers the RC soak, the forced deploy and `rc-validate`; a `recut` starts it again from the new RC. It exists because the marker is the one piece of release state with no expiry - on 2026-11-02/03 a release failed four times on an unrelated CI flag and the marker survived for about 12 hours, during which 20 approved green PRs sat unmerged while every merge-queue run still concluded success (the queue job is skipped while the marker exists).

**Nothing opens the gate when the maximum is exceeded.** A release that legitimately overruns must not be un-frozen mid-flight: merging into `main` while an RC is being validated would change the commit that gets promoted. An overrun instead becomes visible, and the decision stays with a human:

- `merge-queue.yml`'s freeze gate reports the marker's commit and age on every tick (a `::notice::` plus a run-summary block) and adds a `::warning::` once the marker is older than the maximum. The gate still fails closed and never lifts a freeze by itself.
- `merge-queue-watchdog.yml` (every 10 minutes) raises an operator alert through the existing watchdog escalation path - one deduplicated Linear ticket per condition, `RELEASE FREEZE STALE`, naming the marker commit, its age and the threshold. The alert fires on the **watchdog** run, never on the merge-queue tick: a halt somebody chose is not a queue failure, and failing the tick would make every frozen pass look like a broken queue.
- The threshold defaults to 4 hours and is overridden with the `RELEASE_FREEZE_MAX_HOURS` repository variable; the watchdog also accepts a `max_freeze_hours` `workflow_dispatch` input for a one-off re-check.
- The reported age is measured from the commit the marker points at, because GitHub exposes no ref-creation time. It is therefore a **lower bound**: an alert can arrive late, never early.

**The manual escape is deleting the branch, and it is the only escape:**

```bash
git ls-remote --exit-code origin refs/heads/release-freeze   # confirm it exists
git push origin --delete refs/heads/release-freeze            # lift the freeze
```

Do that only when the freeze is genuinely abandoned - a `cut`/`recut` that failed in a way the finalizer above could not clear, or a `promote` that was abandoned. Deleting a live freeze lets the merge queue land commits on `main` mid-release, and the GA tag then points at a commit nobody validated. Do **not** promote a release just to clear a stale marker, and do not add an automatic lift: a late merge is recoverable, a wrongly-lifted freeze is a wrong release.

### Recut runbook

The unified workflow is the canonical entry point. Its `recut` mode publishes from the new RC tag created at current `main`; it never checks out or reuses the previous RC tag.

A recut is **idempotent for a version that already published a bundle**. The bundle release is keyed on the version alone (`bundle-v<X.Y.Z>`, never on the RC tag), so when a recut finds the release already present it replaces its assets in place instead of refusing. The replacement is ordered: every portable/binary asset first, then `SHA256SUMS`, then `RELEASE_MANIFEST.json`, then `RELEASE_MANIFEST.json.sig` **last**, and a post-upload step then verifies that every expected asset is on the release at the local byte size and that the *published* manifest and signature verify against the checked-in trust store. The release is never deleted or hidden, so the public artefact is never briefly absent, and **no manual deletion of a published release is required** - deleting one is not part of the recovery path. The manifest covers every asset except `openapi.json` (which is generated after the manifest is built, so neither it nor `SHA256SUMS` covers it; the post-upload step verifies `openapi.json` by published byte size only), and the signature covers the manifest, so a verifiable manifest can only exist once the whole replacement has landed: an interrupted recut fails verification instead of certifying a half-replaced release. A recut whose inventory shrinks, specifically one that drops the conditional Windows installer by setting `include_windows` to false, removes that stale asset from the release before replacing the rest, so the post-upload inventory check still passes; any other unexplained asset remains a hard failure.

The bundle tag itself is **immutable**. `bundle-v<X.Y.Z>` identifies the *first* published commit for that version, and the repository ruleset `release-tag-protection` targets `refs/tags/bundle-v*` with `deletion` and `update` rules at `active` enforcement, so the workflow never moves or deletes it. A recut of an already-published version therefore publishes new assets and a new signed manifest against that same release identity, and the manifest plus the release notes name the exact built commit the recut published. A recut is **refused once the GA tag `v<X.Y.Z>` exists**: a promoted version's bundle is immutable and is never republished.

Why there is one stable bundle tag and not one per RC: the marketing website resolves `https://api.github.com/repos/farnalabs/farna-control/releases/latest`, rejects any draft or prerelease, strips the `bundle-v` prefix and then requires the resulting version to match `^\d+\.\d+\.\d+$` (the marketing website repo's `scripts/release-lib.mjs::validateRelease`, used by its `scripts/fetch-release.mjs`). An RC-qualified tag (`bundle-v0.3.330-rc3`) would make an RC the latest non-prerelease release, fail that check, and leave the site silently serving its previous release for the whole RC window; it would also stop the newest-bundle selection in `release-unified.yml` from recognising the release (its filter only matches `bundle-v<X.Y.Z>`) and break every installer URL that resolves `/releases/latest`. Do not re-open "key the bundle tag on the RC" without that constraint.

Use `recut` after a fix has been merged to `main` while the release freeze is active:

1. Confirm the fix is on `main` and the previous RC remains identifiable.
2. Dispatch `recut` for the same `X.Y.Z` version. The workflow selects the next free `vX.Y.Z-rcN`, tags the current `main` commit with it, and force-moves `release-freeze` to that new RC commit.
3. The workflow then dispatches `Deploy: Staging + Prod` with `force_deploy=true`, so the freeze cannot throttle the soak deployment. It dispatches `rc-validate.yml` for the new RC only after the deploy dispatch is accepted.
4. `rc-validate.yml` waits for `/api/v1/deployment` to report the new RC commit, runs the read-only Schemathesis checks, and pushes `validated/vX.Y.Z-rcN` at the RC commit only on success.
5. Promote only after the validation marker exists and points at the latest RC commit. If validation fails, fix the issue on `main` and repeat this recut sequence; do not move the marker by hand.

## What `promote` requires (the four promote invariants)

Promotion is the irreversible step, so the `release` job refuses to run unless all four of these hold. Each is a separate, loud `::error::` and a non-zero exit:

1. **A freeze is active.** The `release-freeze` branch must exist - you cannot promote out of nothing.
2. **The GA tag does not already exist.** `vX.Y.Z` must not be present; a release is never overwritten.
3. **`main` is exactly the validated RC commit.** The newest `vX.Y.Z-rc*` tag's commit must equal `origin/main`'s HEAD (fetched live, so a commit landing between checkout and promote is caught). We promote the commit that was validated, not whatever happens to be on `main` now.
4. **The RC was actually validated.** The `validated/<rc_tag>` marker - pushed by `rc-validate.yml` on success - must exist *and* point at that same RC commit.

Only when all four pass does the job create and push the annotated GA tag.

### After the tag

The GA tag is the release identity, so it is created and pushed before anything else. The following step updates `CHANGELOG.md` (via `scripts/generate_changelog.py`) and creates the GitHub Release. Its failure handling is deliberate:

- A failed CHANGELOG commit/push turns the job **red** - it is a required deliverable.
- Failed release-notes generation or a failed `gh release create` is a **warning only**, because the tag already exists and is intact.

### Resuming a failed promote

`promote` pushes the `vX.Y.Z` GA tag before the rest of the GA publication chain runs (Helm chart, `CHANGELOG.md` and the GitHub Release, PyPI, the GA compose bundle, the freeze lift). If a job after that point fails, a plain re-dispatch of `promote` cannot finish the release: safety 2 refuses because the GA tag already exists, and safety 4 refuses because `origin/main` is no longer the validated RC commit once the promote PR has squash-merged. Every later job is then unreachable, and the release is stranded with the GA tag pushed but GA publication incomplete.

The explicit resume path completes that release instead. Dispatch `promote` again with `resume=true`:

```bash
gh workflow run "Release: Unified Pipeline" -f mode=promote -f version=0.4.0 -f resume=true
```

`resume` is only valid with `mode=promote`; the workflow refuses it for `cut`, `recut` and `dry-run` at the first input-validation step. The release identity is re-derived from the frozen state, never from `main`:

- the GA tag `vX.Y.Z` must exist on origin - it is the release commit, and its commit is what every downstream job checks out and attests;
- `release-freeze` on origin must resolve to that exact GA commit. A missing freeze and a failed query both fail closed: a resume never treats an unknown state as absent;
- the GA commit must have exactly one parent, because the promote PR is squash-merged. A merge commit, a root commit or an unreadable parent listing is a hard failure, so a release promoted by a non-squash merge cannot be resumed;
- that single parent is the RC commit. A `vX.Y.Z-rc*` tag must point at it (the highest rc wins when several share the commit), and the `validated/<rc>` marker must exist on origin and point at the same commit.

A resume creates, moves and deletes nothing. It skips safety 2 and safety 4, the freeze/publication-hold swap, the promote PR creation and merge, and the tag push, and it never creates, moves or deletes the GA tag or `release-freeze`. It checks out the GA commit and re-runs only the publication steps. If the resume fails, the freeze is left where it is - at the GA commit - so another resume can re-verify and try again.

The publication steps are idempotent, so a resume is safe after a partial publish and never overwrites anything already published:

- **Helm chart** - before pushing, the workflow pulls `oci://ghcr.io/farnalabs/farna-control/charts/farna-control` at the target version. If that version is already published, the pulled `Chart.yaml` must report the same `version` and `appVersion`, and the push is skipped with a notice. Only a not-found error means "safe to push"; any other pull error is a hard failure. An existing chart version is never overwritten.
- **PyPI** - before uploading, the workflow queries `https://pypi.org/pypi/farna-control/<version>/json`. A 404 means the version is not published yet and the upload proceeds. An HTTP 200 means it is already published: the published filenames must exactly match the built `backend/dist/*` filenames, and the upload is skipped; a different file set is a hard failure. Any other HTTP status fails closed. Wheels are not byte-reproducible, so the comparison is by filename, never by hash.
- **Container images** - the GA retags are immutable SHA-derived tags, so re-running them is naturally idempotent.
- **Freeze lift** - the final GA step remains the only place that lifts the freeze, and it now tolerates the freeze already being absent (it queries first; a genuinely unknown state still fails closed).

### Website rebuild hook (optional)

The marketing website reads the latest GitHub Release at build time. As the last step of `Create GitHub Release` (modes `cut` and `recut`), the workflow POSTs to a Cloudflare Pages deploy hook so the website rebuilds with the new version and installer links. It is controlled by the repository secret `WEBSITE_DEPLOY_HOOK_URL`:

- The secret is **optional**. If it is unset the step prints a notice and skips.
- The step can never fail a release: it retries three times and then only warns. If it warns, trigger the website deploy by hand.
- To rotate it, create a new deploy hook in Cloudflare Pages for the website project, update the `WEBSITE_DEPLOY_HOOK_URL` secret, then delete the old hook.
- `dry-run` does not call it.

## `dry-run` - rehearse without publishing

`dry-run` is the safe way to answer "what would this release actually do?" before committing to it. It:

- validates the version input (must be semver `X.Y.Z`);
- resolves the version, the next RC tag, and the artifacts a `cut` would produce, and writes them to the job summary;
- builds every container image (see below);
- generates the CycloneDX SBOM and uploads it as the `sbom-cyclonedx` workflow artifact (building a file inside the run is not publishing - it reaches no registry and no release).

It does **not** publish anything. It does not create a tag or marker branch of any kind - not the RC, not `release-freeze`, not `release-publication-hold`, not `validated/*`, not GA - does not push to any registry, and does not create a Release, commit a CHANGELOG, publish a package, create an attestation, or deploy. It is therefore safe to run during an active freeze.

### Why the "no marker at all" guarantee is structural, not a convention

A tag push is not a private act: a separate tag-triggered publication path could publish images if one were configured. The current `publish-images.yml` trigger is limited to `main` pushes and manual dispatch, while Release: Unified Pipeline publishes release images explicitly. So the dry-run path takes no tag action whatsoever rather than "a harmless local tag".

The rule that keeps this true as the workflow grows: **publish-shaped work belongs in `release-unified.yml`, and every job that publishes must be explicitly gated so it cannot run under `dry-run`.** Publish-shaped jobs carry `if: inputs.mode != 'dry-run'` (or a narrower mode gate). This is documented in the workflow header so the next author cannot miss it. The legacy `release.yml` wrapper must remain fail-closed and read-only: do not add tags, releases, freeze changes, registry publishes, package publishes, changelog commits, or release dispatches to it.

## Container images and platform scope

The `dry-run` build job builds the four images the release and deployments consume:

| Image | Context | Dockerfile |
|-------|---------|------------|
| all-in-one | `.` | `deploy/docker/Dockerfile.all-in-one` |
| backend | `backend` | `backend/Dockerfile` |
| frontend | `frontend` | `frontend/Dockerfile.prod` |
| runner-opencode | `.` | `deploy/docker/runner-opencode.Dockerfile` |

The `dry-run` rehearsal builds the same four images with `push: false` and applies no tag at all. Publishing happens only in `cut`/`recut`, which tags each pushed image with the RC tag (`vX.Y.Z-rcN`) alone and runs its own smoke check after the push (`import farna` for the all-in-one and backend, `nginx -t` for the all-in-one and frontend, `opencode --version` for the runner). A failing smoke check fails the job - never `|| true`. `promote` then retags those RC images to the plain `X.Y.Z` and `latest` via `docker buildx imagetools` (falling back to pull/tag/push); it does not rerun the smoke checks, which already passed against the same image digests.

Server images are `linux/amd64` **only**. The native macOS installers are separate `macos-arm64` and `macos-x86_64` artifacts built and validated on `macos-15` and `macos-15-intel`; neither is a universal or Rosetta bundle. Linux arm64 remains planned for the server bundle.  `publish-images.yml` declares no `platforms:` (so it builds amd64), and `docs/system-requirements.md` lists `linux-amd64 (x86_64)` plus the native macOS arm64 and x86_64 `.dmg` artifacts as supported, while `linux-arm64` is Planned (P2) and Windows native is Beta scope, opt-in only (FAR-2337 / FAR-2338; earlier tracking: FAR-1873), with the release pipeline building, signing and attesting the installer when the Beta-only `include_windows` input is enabled. `bundle-release.yml` carries the same deferral for the server bundle ("linux-amd64 only for now; the matrix structure below is where linux-arm64 slots in; `scripts/bundle-versions.env` already carries the arm64 pins"). The native Windows installer is a Beta deliverable, built only on the explicit `include_windows` opt-in; its publication depends on a release running, so no signed Windows installer is published yet, validation on the real Windows installer host is still outstanding under FAR-2250, and how its bundle directory is built and how the installer is compiled is in [`deploy/windows/BUNDLE-BUILD.md`](../deploy/windows/BUNDLE-BUILD.md).

`dry-run` builds each image with `push: false`, so it validates that every Dockerfile still builds on a clean runner without producing a loadable image. The post-publish smoke checks that guard against the 2026-07-06 stale-image regression (a `:latest` that did not even contain the `farna` package) live in `publish-images.yml` and are *not* duplicated here - they belong with the publish path, and re-implementing them in a `push: false` build would be a parallel copy that could drift. Wiring them into this workflow is future work (see below).

### Upgrade and rollback acceptance evidence

A release is not upgrade-proof because its image builds. The Alpha acceptance
matrix must be run for both supported targets: Linux native on a supported
glibc host, and Docker using the shipped `docker-compose.prod.yml` bundle. For
each target, capture one repeatable transcript for:

`clean install -> upgrade (verified pre-upgrade snapshot) -> intentionally failed upgrade -> startup-guard refusal on the forward-migrated database -> restore the pre-upgrade snapshot -> start the previous release with the pre-upgrade data intact`.

The native release smoke workflow already performs the native install, refusal,
N-1 upgrade, doctor, backup and rollback-target checks against the published
Linux bundle. The Docker leg is the companion
`scripts/verify_compose_upgrade_rollback.py` harness, which runs against the
extracted release compose asset and writes `metadata.json`, `commands.jsonl`,
per-command stdout/stderr, and a final `RESULT` marker. Its failed-upgrade step
must leave readiness unavailable; the documented recovery then restores the
pre-upgrade snapshot before the previous release is started on the restored
database.

`rc-validate.yml` runs that Docker leg after the anonymous clean-host compose
check and before the live pen test. It resolves the previous published
`bundle-v*` release as the rollback target, refuses to claim coverage when no
previous release exists, and uploads the evidence directory as the
`alpha-docker-upgrade-rollback-*` workflow artifact. The predecessor is the
greatest already-published version below the RC's version: the RC's own bundle
is published after this validation is dispatched, so it does not need to exist
in the release history for the matrix to run. A green build without that
transcript is not a claim of rollback coverage. The same matrix remains
available locally with the command in `docs/upgrade-process.md`.

The matrix is intentionally limited to Linux native and Docker. Windows is
not covered by this acceptance claim, and macOS native validation is recorded
separately.

### How images are actually published

The unified workflow owns release publication directly. `cut` and `recut` publish four RC-qualified images (`all-in-one`, `backend`, `frontend`, and `runner-opencode`) alongside the signed bundle and release artifacts. `promote` is the only path that publishes stable `VERSION_NUM` and `latest` image tags and the stable Helm chart.

`publish-images.yml` remains the ordinary `main` publication path. It always publishes the immutable short-SHA image; it publishes `latest` only when neither the authoritative `release-freeze` nor the cut-only `release-publication-hold` marker exists. A failed or unknown marker lookup fails closed. The temporary hold closes the race between the cut's version-bump push and creation of `release-freeze`; the freeze remains the authoritative release boundary after that point.

If a cut fails between pushing `release-publication-hold` and removing it, the hold stays behind and every later cut refuses to start while `publish-images.yml` keeps stable publication suppressed. Nothing in the pipeline clears a stale hold automatically (including `promote`), so the recovery is manual once the failed cut is understood:

```bash
git ls-remote --exit-code origin refs/heads/release-publication-hold   # confirm it exists
git push origin --delete refs/heads/release-publication-hold            # release the suppression
```

The dry-run's no-publication guarantee stays structural: it performs no marker action, registry push, Release creation, commit, attestation, or deploy. A human or another credential pushing a tag or branch marker is a separate event and is outside the dry-run path.

## Legacy workflow migration

`release.yml` is retained only so an old bookmarked workflow name produces a loud, actionable failure. It is deliberately read-only and contains no release-mutating job. A failed wrapper run is safe: it creates no tag, moves no freeze marker, creates no GitHub Release, publishes no package, commits no changelog, and dispatches no release workflow. Re-run the intended operation through `Release: Unified Pipeline` using the commands above.

## Not implemented yet

`release-unified.yml` owns the RC/GA lifecycle, RC publication, GA publication, the rehearsal, the deterministic RC soak dispatch (FAR-1870), and PyPI publication. The legacy `release.yml` is not a second release implementation: its release jobs were removed outright in favor of a single fail-closed refusal, and it must stay that way. Only the explicitly listed future publication surfaces remain out of scope; they are not executable release steps and must not be described as active stubs.

| Future surface | Ticket | Scope |
|----------------|--------|-------|
| `pin-release-docs` | FAR-1872 | Pinning the released version in the docs |

SBOM production and file-artifact attestation are **no longer stubs** (FAR-1868). `produce-sbom` runs for every action, dry-run included, and uploads a validated CycloneDX SBOM as the `sbom-cyclonedx` artifact. `attest-artefacts` runs for every action except `dry-run` and creates SLSA build provenance with `actions/attest-build-provenance`; it is the producer for the fail-closed `gh attestation verify` gate in `native-release-smoke.yml`. Today the SBOM is the only attested subject: container-image and release-bundle attestation remain FAR-1869 work, because attesting an image needs a registry digest - nothing fabricates a subject for an artifact that was not produced.

Until `pin-release-docs` lands, a GA release still gets its tag, its GitHub Release, its CHANGELOG section, stable container/chart publication and the release bundle. It also produces an SBOM for the run, plus an attestation for it on non-`dry-run` actions.

## Attestation scope: `gh attestation verify`, not cosign

The repository enforces build provenance with `gh attestation verify`, fail-closed, before any release artifact is installed (`native-release-smoke.yml`). cosign/Sigstore is explicitly **out of scope**: no consumer in the repo verifies a cosign signature or bundle, and `gh attestation verify` needs neither, so a cosign producer would be a second, unreferenced signing path.

## Running a release

```bash
# Rehearse first - builds everything, publishes nothing.
gh workflow run "Release: Unified Pipeline" -f mode=dry-run -f version=0.4.0

# Cut an RC and freeze.
gh workflow run "Release: Unified Pipeline" -f mode=cut -f version=0.4.0

# After a fix has been merged to main, cut the next RC and re-run the soak.
gh workflow run "Release: Unified Pipeline" -f mode=recut -f version=0.4.0

# Once the RC has validated, promote to GA (tag, Release, CHANGELOG, PyPI).
gh workflow run "Release: Unified Pipeline" -f mode=promote -f version=0.4.0
```

The deprecated `Release: Legacy Entry Point (Deprecated)` workflow still accepts the same arguments under its `action` input, but every run now fails closed with an instruction to re-run here; it performs no release action.

## Version numbering

The version is a plain `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the git tag only and is never written into any file. See [upgrade-process.md](upgrade-process.md) for the version source of truth (`backend/pyproject.toml`) and how to bump it.

**Legacy tags already on origin:** 42 `v*` tags predate this process (`v0.1.1`-`v0.1.41` and `v0.2.0`, committed June-August 2026, all predating `publish-images.yml`). The promote invariant that refuses to create a GA tag which already exists means none of those version numbers can be released through this process.
