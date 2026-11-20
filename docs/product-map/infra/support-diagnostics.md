---
id: feat-support
prd: N/A
adr: []
code:
  - backend/src/farna/support/__init__.py
  - backend/src/farna/support/bundle.py
  - backend/src/farna/support/redaction.py
  - backend/src/farna/launcher/doctor_report.py
  - backend/src/farna/cli/main.py
  - scripts/install.sh
  - scripts/support-bundle.sh
  - frontend/src/lib/supportReference.ts
  - frontend/src/views/RunDetailView.vue
  - frontend/src/views/AdminErrorsView.vue
  - frontend/src/views/AdminErrorDetailView.vue
unit-tests:
  - backend/tests/unit/support/test_bundle.py
  - backend/tests/unit/support/test_bundle_withholding.py
  - backend/tests/unit/support/test_support_coverage_gaps.py
  - backend/tests/unit/cli/test_support_command.py
  - backend/tests/unit/launcher/test_doctor_report.py
  - backend/tests/unit/scripts/test_support_bundle_sh.py
  - backend/tests/unit/scripts/test_install_support_block.py
  - frontend/src/__tests__/supportReference.spec.ts
  - frontend/src/__tests__/RunDetailShare.spec.ts
  - frontend/src/__tests__/AdminErrorsView.spec.ts
  - frontend/src/__tests__/AdminErrorDetailView.spec.ts
bdd:
depends-on: []
status: covered
---

# Support Diagnostics

Self-hosted operators cannot hand Farnalabs an instance, so the product ships
two deliberate support surfaces: a canonical one-line support reference printed
or copied from the failing surface, and a redacted, bounded support bundle the
operator can read before sending. Nothing is uploaded automatically and no
Farnalabs service is contacted.

## Behaviors

- [x] A failed run exposes one canonical support reference built by the shared
      `buildRunSupportReference` helper: the FULL run id, pipeline id (plus the
      name when known), status, error code (`none` when the run has no error),
      and an `url <origin>/runs/<run_id>` line derived from the serving origin
      (trailing slashes removed; the url line is omitted when no origin exists
      rather than emitting a broken one). The run detail page copies it in one
      action and logs a console warning on clipboard failure instead of silently
      doing nothing
      (`frontend/src/lib/supportReference.ts`,
      `frontend/src/views/RunDetailView.vue`; `supportReference.spec.ts`,
      `RunDetailShare.spec.ts`, `RunDetailView.spec.ts`)
- [x] An error group exposes one canonical support reference built by
      `buildErrorGroupSupportReference`: group id, fingerprint, level and count,
      plus the source segment only when the caller actually has one (the list
      payload does not), and both the Error Dashboard rows and the error detail
      page copy it through the same helper so the two surfaces cannot drift
      (`AdminErrorsView.vue`, `AdminErrorDetailView.vue`; `AdminErrorsView.spec.ts`,
      `AdminErrorDetailView.spec.ts`)
- [x] `farna-control support` builds the redacted bundle and prints the
      pasteable reference line, the absolute bundle path and byte size, the
      instruction to open the bundle before sending it, and one `missing
      <member>: <reason>` / `withheld: <member> - ...` line per affected member.
      `--out` chooses the destination, `--json` emits one machine-readable
      object (ref, ref line, format and path), `--data-dir` selects the launcher
      data directory, and the default destination is
      `./modulo-support-<ref>.zip` carrying the same per-run ref as the archive
      (`backend/src/farna/cli/main.py`;
      `backend/tests/unit/cli/test_support_command.py`)
- [x] `farna-control doctor --report PATH` writes the same redacted bundle
      format (the historical narrow report is only an import shim now), keeps
      the doctor exit status rather than the report status, and prints
      `diagnostic report written: <path>` so a non-zero exit is not mistaken for
      a missing report (`backend/src/farna/launcher/doctor_report.py`,
      `backend/src/farna/cli/main.py`; `test_doctor_report.py`,
      `test_bundle.py::test_doctor_report_delegates_to_v1_bundle`)
- [x] The bundle is a versioned zip with a FIXED member allowlist
      (`MANIFEST.txt`, `report.json`, `version.txt`, `doctor.txt`, `health.json`,
      `settings.txt`, `deployment.txt`, `db.txt`, `errors.json`, `runs.json`,
      `licence.txt`, `degraded.json`, plus well-formed
      `logs/<component>.log.tail` members). Any other member shape is refused at
      assembly time, and a new member requires a format-version change
      (`backend/src/farna/support/bundle.py`; `test_bundle.py::test_golden_member_set_is_exact`,
      `test_support_coverage_gaps.py::test_assert_allowlist_rejects_a_member_outside_the_fixed_set`)
- [x] Bundle contents are bounded and honest: settings carry a full key
      inventory with values only for a reviewed safe-list and `set`/`unset` for
      everything else, the licence member never carries the key, database and
      run members carry counts/identity rather than rows or payloads, and log
      tails collapse consecutive repeats and cap both bytes and lines. Over the
      compressed size cap the logs are truncated and the truncation is recorded;
      if the bundle still exceeds the cap the build refuses rather than writing
      an oversized archive (`bundle.py`; `test_bundle.py`,
      `test_support_coverage_gaps.py`)
- [x] Redaction is five ordered layers and fails closed: (1) the fixed member
      allowlist; (2) exact-value scrubbing of generated secrets from Settings
      and the launcher `secrets.json`, including percent-encoded forms; (3)
      structural sensitive `KEY=value` scrubbing through the canonical
      classifier; (4) shared secret-shape patterns (tokens, URL userinfo,
      private keys, JWTs); and (5) a post-assembly gate that re-scans every
      member and withholds the offending item (or a small invariant member
      whole), records the withholding in `MANIFEST.txt`/`report.json`, and keeps
      the rest of the bundle. An unattributable trip, or a member that still
      looks secret-bearing after every layer, aborts the build and writes NO
      zip. The one entropy exemption is a content-hashed build asset filename,
      scoped narrowly so a credential in a URL path is still caught
      (`backend/src/farna/support/redaction.py`; `test_bundle.py`,
      `test_bundle_withholding.py`)
- [x] Docker Compose has its own collector, `scripts/support-bundle.sh`: it
      runs from the repository root, writes one zip (default
      `modulo-support-bundle-<UTC>.zip`), works with the stack down by recording
      the app-level half as missing rather than failing, merges the in-container
      module bundle when the app image is reachable, never reads `.env` or
      `secrets.json`, never dumps interpolated `docker compose config`, and
      pipes Docker log output through the module's own redaction (withholding
      the member when that filter cannot run)
      (`scripts/support-bundle.sh`;
      `backend/tests/unit/scripts/test_support_bundle_sh.py`)
- [x] A failed native install still leaves a diagnosable artefact before any
      `farna-control` binary exists: the installer tees its transcript to
      `~/.local/state/farna-control/install-<UTC>-<ref>.log` (state dir 0700,
      log 0600, newest five retained, size-capped), redacts credential-shaped
      values in the support block's target, stage and log-path fields (the
      transcript itself is verbatim), and prints one delimited support block
      LAST on every failure path naming the target, failing stage, an 8-hex
      support reference and, when logging is available, the absolute log path
      that also embeds the reference. A successful run prints no block and exit
      codes are unchanged (`scripts/install.sh`;
      `backend/tests/unit/scripts/test_install_support_block.py`)
- [x] Kubernetes / Helm needs no extra install: the same module runs inside the
      backend pod (`python -m farna.support.bundle --out ...`) and the bundle is
      copied out with `kubectl cp`, with cluster facts and raw logs captured via
      `kubectl` alongside it (`docs/support.md`)

## Known Gaps

- The bundle is generated from the CLI, the Compose collector script or an
  in-pod module invocation. There is no HTTP/API or in-product download route,
  and nothing is uploaded automatically.
- The format is v1 with a fixed member list; adding a member is a deliberate
  format-version change rather than an additive field.
- Kubernetes has no bundled Helm job/chart for collection; the operator runs
  the documented `kubectl exec`/`kubectl cp` steps.
- Redaction is a guard against known credential shapes seeded by this build, not
  a proof about arbitrary free text, so the operator is always told to read the
  bundle first.

## QA History

- 2026-11-19: **Improve Architecture product-map walk** - added this tracker for
  the shipped support-diagnostics surface, which neither product-map layer
  described even though `docs/support.md`, the native CLI, the Compose collector,
  the installer and the run/error reference actions all ship. Registered
  `feat-support` in the manifest `features:` registry and tagged the three routes
  that expose a support-reference action (`/runs/:id`, `/admin/errors`,
  `/admin/errors/:id`). Also corrected `docs/support.md`, which still told
  readers the installer support block and the run/error reference lines were
  "landing in this milestone"; they ship and are pinned by
  `test_install_support_block.py`, `RunDetailView.spec.ts` and
  `AdminErrorsView.spec.ts`.
