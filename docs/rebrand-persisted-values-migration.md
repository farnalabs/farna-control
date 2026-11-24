# Rebrand: migrating the remaining persisted and wire values

Status: plan. This document changes no code and no data. It records what exists
today, what each option would break, and the order in which a safe change can
land.

Verified on 2026-11-24 against `origin/main` at commit `15e4c08fe` (backend
version `0.3.329`). Line numbers drift; prefer the file path and symbol when a
line has moved.

Abbreviations used below: API (application programming interface), CI
(continuous integration), HITL (human in the loop), HMAC (Hash-based Message
Authentication Code), HTTP (Hypertext Transfer Protocol), JSON (JavaScript
Object Notation), MCP (Model Context Protocol), OTel (OpenTelemetry), REST
(Representational State Transfer), SQL (Structured Query Language), UI (user
interface).

## 1. Purpose and scope

The rebrand from Modulo to Farna Control is complete for the environment
contract (the uppercase `MODULO_*` variables and secrets), the command name,
the container images, the database object names, and most persisted row values.
Two data migrations already rewrote the bulk of the stored text:
`0357_rebrand_library_seed_copy.py` and `0364_rewrite_legacy_persisted_values.py`.

Four lowercase values were deliberately left in place. They are listed below
with the storage each one reaches. This document is the migration plan for
those four values.

| # | Value | Where it lives | Recommendation |
|---|-------|----------------|----------------|
| 1 | `"source": "modulo"` inside a library built-in's `trust_header` | JSON in `library_primitives.content_json` | Keep permanently |
| 2 | `modulo_minted` (`ISSUANCE_PROVENANCE_SELF_MINTED`) | Sandbox environment variable, OpenTelemetry (OTel) span event, in-memory credential handle | Add `self_minted`, keep the old value accepted |
| 3 | `modulo_synthetic_failure` (`SYNTHETIC_FAILURE_MARKER`) | Persisted node telemetry in `run_node_outputs` | Add `synthetic_failure`, dual-write, dual-read, then drop the old key |
| 4 | `Modulo-Notifier/1.0` (`NOTIFIER_USER_AGENT`) | Outbound Hypertext Transfer Protocol (HTTP) `User-Agent` header | Change to `Farna-Control-Notifier/1.0` before the first external install |

Terms used in this document:

- A **producer** writes the value.
- A **reader** consumes it.
- A **persisted** value survives the process that wrote it, usually in a
  database column.
- A **wire** value leaves the product, for example an HTTP header or a value
  passed into a sandbox.

The plan deliberately does not propose renaming any present value in place.
Section 2 explains why that is unsafe for these particular values.

## 2. Why an in-place rename is not safe here

Three properties of this system make a direct rewrite unsafe.

1. **Two releases can run at once.** A rolling deploy leaves an older and a
   newer instance alive together. A value written by one release can be read
   by the other.
2. **A run outlives a deploy.** The finalize path reads stored node telemetry
   that an older release wrote. `SAQ_RUN_TIMEOUT` defaults to 7200 seconds and
   is capped at 86400 seconds, so a run can live for up to 24 hours across a
   deploy.
3. **Some values are contracts.** A webhook receiver, a sandbox script, or an
   external telemetry backend can depend on a value that the product never
   reads back.

The safe shape is therefore the same for every changed value:

- **R1**: write the new value (and, where a second release must read it, the
  old value as well) and make every reader accept both.
- **R2**: stop writing the old value, and keep reading it.
- **R3**: drop the old read path, after no live run can still carry it.

R1 is the first release cut after the implementing change merges. R2 is the
next release, and it must land more than 24 hours after the last R1-only
instance stopped writing. R3 is any later release. If a release is cut less
than 24 hours after R1, R2 waits.

The pre-alpha rule removes the need to protect external consumers, because no
external install exists. It does not remove the need for this sequence, because
our own staging and production databases still hold data written by older
releases, and the finalize path still reads it.

## 3. Value 1: `"source": "modulo"` in the built-in `trust_header`

### What it is

Seven built-in library collections carry a provenance header inside their
`content_json`:

```json
"trust_header": {"source": "modulo", "verified": true}
```

The header is not the `library_primitives.source` column. That column already
holds `farna_control` for built-ins (`_make_native` in
`backend/src/farna/core/library_service/_seed_data.py:103-124`), and migration
`0364` rewrote every stored `modulo` value in it. The header is a separate
nested key that no migration touched. Within the library data path it is the
only remaining `"source": "modulo"` shape; the two stale developer scripts in
section 10 are the other producers.

### Producers

The only producer is the seed data. Each of these lines is one built-in
`library_collection` primitive:

- `backend/src/farna/core/library_service/_seed_data.py:1129`
  (`github-pr-reviewer`)
- `...:1263` (`prompt-to-pr`)
- `...:1380` (`changelog-generator`)
- `...:1494` (`release-notes-generator`)
- `...:1583` (`pr-description-writer`)
- `...:1697` (`issue-triage`)
- `...:1824` (`license-checker`)

Two paths copy that blob into a database row:

- `backend/src/farna/core/library_service/install.py:242-278`
  (`_clone_builtin`) copies `content_json` when a collection is installed, so
  the sentinel organisation gets a persisted row with the header.
- `backend/src/farna/core/library_service/__init__.py:305-339`
  (`_build_copy_args`) copies `content_json` into a tenant-owned `source="local"`
  row when an operator adapts a built-in. The legacy twin is
  `backend/src/farna/db/crud/library_primitive.py:52-109` (`copy_to_adapt`),
  which has no production caller.

A later reinstall of the same collection re-syncs the sentinel row because
`_collection_content_differs` compares `content_json` and `_resync_collection_row`
overwrites it (`install.py:281-292`). A tenant copy is never re-synced.

### Storage

- `library_primitives.content_json` in the sentinel organisation
  `00000000-0000-0000-0000-000000000001`.
- `library_primitives.content_json` in any tenant that adapted a built-in
  collection.
- There is no CHECK constraint or vocabulary for the nested header.
- The separate `library_primitives.trust_header` column exists
  (`backend/src/farna/db/models/library_primitive.py:149`) but nothing in
  production writes it. It is `NULL` on these rows.

### Readers

- REST (Representational State Transfer): `LibraryPrimitiveResponse.content_json`
  (`backend/src/farna/api/routes/library.py:377-414`) returns the blob verbatim.
  `CollectionResponse.trust_header` (`...:698-712`, built at `...:781-793`)
  exposes the empty column, not the nested header.
- MCP (Model Context Protocol): the resource
  `modulo://library/{primitive_type}/{slug}`
  (`backend/src/farna/api/mcp_server.py:14512-14555`) prints `content_json`
  with credentials masked for composite primitives only. The header is
  therefore visible to MCP clients.
- Frontend: the generated OpenAPI type in `frontend/src/lib/api/schema.ts`
  (`:13397` and `:16767`) types `trust_header` as a free-form object. No view
  or component reads it. A repository search for `trust_header` in
  `frontend/src` matches only `schema.ts`.
- Tests that pin the current literal:
  `backend/tests/unit/api/test_library_collection.py:2059-2063` and
  `:2190-2196`.

### What breaks if it changes

Nothing in the product reads the header, so no in-product behaviour changes.
The risks are:

- any external or MCP consumer that filters on the header (we do not document
  it, but it is visible);
- a data rewrite that touches an operator-edited copy. A tenant copy is
  editable, so a rewrite must be equality-guarded;
- the two tests above, which would need to change in the same commit.

### Recommendation: keep permanently

Keep the value. Reasons:

- It is not a brand surface. No view renders it, and no product code reads it.
- It is a provenance header inside an operator-editable content blob. Rewriting
  it changes stored customer data for no user-visible gain.
- The correct product identity is already available on the `source` column, so
  a future consumer has no reason to read the nested header.
- The rebuild is not free: the seed, the sentinel row, every tenant copy, the
  two tests, and the brand baseline entry all move together.

The keep decision is mechanical once approved. Add a pin test (section 8) and a
short comment on the seed constant explaining that the value is frozen.

### If you choose to migrate instead

Use the additive shape, never an in-place rewrite of a modified row:

1. **R1**: add the new value (`"farna_control"`, or a brand-free token such as
   `"builtin"`) in the seed, and make every reader tolerate both values. There
   are no in-product readers, so this is a documentation and test change.
2. **R1 backfill**: add one migration after `0364` (next number `0365`;
   renumber it if another branch claims that number first). Rewrite only rows
   whose `trust_header` is exactly the old literal, using `jsonb_set` on
   `content_json`. The equality guard leaves an operator-edited header
   untouched. Bound the statement with the containment predicate
   `content_json -> 'trust_header' @> '{"source":"modulo"}'::jsonb`.
3. **R2**: stop writing the old value. Keep accepting it in any reader.
4. **R3**: no drop is possible for external consumers, so keep both values
   accepted indefinitely. If no external consumer exists by then, the old value
   can be dropped from documentation and tests.

Verification:

```sql
-- Every row that still carries the old header, split by organisation.
SELECT organisation_id, count(*)
FROM library_primitives
WHERE content_json -> 'trust_header' ->> 'source' = 'modulo'
GROUP BY organisation_id;
```

A non-zero count after R2 is acceptable only for rows whose header was edited
by an operator, which the equality guard deliberately preserves. Report them;
do not rewrite them.

## 4. Value 2: `modulo_minted`

### What it is

`ISSUANCE_PROVENANCE_SELF_MINTED` names how a run-scoped credential was issued.
The vocabulary has two values: `modulo_minted` means this product minted the
credential itself, and `broker_issued` means an external broker did. The value
is validated against a closed set at construction.

The constant is at `backend/src/farna/core/credential_issuer.py:55`. The set is
`ISSUANCE_PROVENANCES` at `:58`. `IssuedRunCredential.__post_init__` rejects any
value outside the set (`:97-100`), so the value and the set must change in the
same commit.

### Producers

- `backend/src/farna/core/credential_issuer.py:91` and `:201` construct the
  handle with the value. `:147` sets `ModuloCredentialIssuer.name`, and `:93`
  sets `IssuedRunCredential.issuer`, both to `"modulo"`. The same span event
  carries those fields (see below).
- `backend/src/farna/core/pipeline_engine/node_runner.py:12309` writes the value
  into the sandbox environment variable `FARNA_CONTROL_CREDENTIAL_PROVENANCE`.
- `backend/src/farna/core/bundled_runner/runner_dispatch.py:1266` writes the same
  variable on the bundled-runner dispatch path.
- `backend/src/farna/core/pipeline_engine/node_runner.py:8885-8890` emits an
  OpenTelemetry span event named `credential.issued` with
  `issuer.describe()` (`credential_issuer.py:254-260`), which carries
  `provenance`, `provenance_kind` and `issuer`.
- `backend/src/farna/db/runner_binding_constraints.py:32-36` reserves the
  environment variable name so a node's `env_vars` cannot spoof it.

### Storage and readers

Be precise here: the constant's docstring says the value is written into
"credential records and run telemetry". That is stronger than the code.

- **Database**: no column stores it. `org_api_keys` has no provenance column
  (`backend/src/farna/db/models/api_key.py`). No migration adds one. The run
  tables store no credential provenance.
- **API**: no endpoint returns it. A repository search for `modulo_minted` and
  `provenance_kind` in `backend/src` finds the issuer module, the dispatch call
  sites, and comments that explain the value. No route, response model or
  database model references it.
- **Sandbox environment**: the value is written into
  `FARNA_CONTROL_CREDENTIAL_PROVENANCE`, a reserved name. Any script in the
  sandbox can read it. Nothing in this repository reads it.
- **Telemetry**: the value rides an OpenTelemetry span event. The exporter is
  opt-in: it is absent unless `FARNA_CONTROL_TELEMETRY_ENABLED=true`, and the
  OpenTelemetry Protocol (OTLP) exporter also needs
  `OTEL_EXPORTER_OTLP_ENDPOINT` (`backend/src/farna/otel_bridge/export.py:24-60`).
  Where an operator enabled it, the value is persisted in that operator's
  tracing backend.
- **Memory**: the value is on the `IssuedRunCredential` handle for the life of
  the dispatch.

The docstring drift should be corrected when this value changes. A future
engineer who trusts the docstring will look for a database column that does not
exist.

### What breaks if it changes

- `IssuedRunCredential.__post_init__` raises `ValueError` for an unlisted
  value, so a partial change fails fast at dispatch. That is good, but it means
  the value and the set must be deployed together.
- A customer sandbox script that branches on
  `FARNA_CONTROL_CREDENTIAL_PROVENANCE` would see a new value.
- Telemetry queries and dashboards that filter on the span attribute would stop
  matching.
- These tests pin the literal:
  `backend/tests/unit/pipeline_engine/test_sandbox_agent_script_mode.py:841`,
  `:932` and `:1004`; `backend/tests/unit/core/test_credential_issuer.py:61`,
  `:135` and `:210`; `backend/tests/unit/core/test_node_runner_residual.py:2404`.

### Recommendation: add `self_minted`, keep the old value accepted

The constant is already named `ISSUANCE_PROVENANCE_SELF_MINTED`, but the value
says `modulo_minted`. Aligning the value with the constant is a real
improvement, and choosing a brand-free token prevents the next rebrand from
touching it at all.

Proposed pattern:

1. **R1**: add `ISSUANCE_PROVENANCE_SELF_MINTED = "self_minted"` and keep
   `"modulo_minted"` in the accepted set as a legacy value. Write the new value
   only. There is no Modulo reader to dual-read, and no stored value to
   backfill.
2. **R1 companion**: decide the two other strings in the same span event:
   `IssuedRunCredential.issuer` and `ModuloCredentialIssuer.name` are both
   `"modulo"`. If the goal is telemetry that never names the retired product,
   change them to `"farna_control"` in the same release. They are
   telemetry-only, so no compatibility window is needed. If the goal is only
   the provenance token, leave them and record that choice.
3. **R2**: no further write change is needed. Keep the legacy value accepted.
4. **R3**: optionally remove `"modulo_minted"` from the accepted set. Keeping it
   costs one tuple member and tolerates any replayed or imported telemetry, so
   the low-cost default is to keep it permanently as a documented legacy alias.

Rollback is a one-line constant revert plus the set. No data moves.

Verification:

- A live sandbox dispatch prints
  `FARNA_CONTROL_CREDENTIAL_PROVENANCE=self_minted`.
- A unit test asserts `IssuedRunCredential(provenance="modulo_minted")` is
  accepted during the window, and that the issuer writes `self_minted`.
- If telemetry is enabled, query the span backend for the attribute value in
  the 24 hours after R2. No new `modulo_minted` value should appear.

## 5. Value 3: `modulo_synthetic_failure`

### What it is

`SYNTHETIC_FAILURE_MARKER` is a machine key in a sandbox node's telemetry. It
marks a failure envelope that the runner generated itself, rather than a
failure the agent reported. The executor's finalize predicate uses it to stop a
failed sandbox node being counted as a completed node, which would otherwise
finalize the run as `complete`. The key is the safety net for a masked failure.

- Constant: `backend/src/farna/core/pipeline_engine/node_runner.py:758`.
- Dataclass field with the same name: `...:9760`.
- Writer: `...:10042-10043` puts the key into the telemetry envelope when the
  field is true.
- The live producer today is the generic-exception path at `...:14915`. The
  schema-validation path no longer returns a synthetic envelope; it raises a
  retryable error (`...:13600-13620`). The finalize docstring still says "the
  generic-exception path and the schema-validation path"
  (`backend/src/farna/core/pipeline_engine/executor_parts/finalize.py:251-253`),
  which is stale and should be corrected with this work.
- Re-export: `backend/src/farna/core/pipeline_engine/executor.py:141`.

### Storage

The marker is persisted, not just passed in memory.

- `run_node_outputs.node_telemetry_json` holds the telemetry envelope for the
  node. This is the current single storage surface. The old whole-run columns
  `runs.outputs_json`, `runs.node_telemetry_json` and `runs.raw_output_markers`
  were dropped by migration `0215` (see
  `docs/operations/drop-runs-blob-columns.md`).
- `run_node_outputs.outputs_json` can also carry a legacy mixed shape for rows
  written before the split. The readers scan that shape too.

### Readers

- `backend/src/farna/core/pipeline_engine/executor_parts/finalize.py:193-201`
  (`_is_sandbox_synthetic_failure_view`) is the predicate. It requires
  `status == "failed"` and the marker `True`.
- `...:237-300` (`_downgrade_masked_sandbox_failures`) scans three views per
  node: the live completed output, the stored outputs, and the stored telemetry
  (via `node_telemetry()`). A match in any view downgrades the run.
- `...:303-340` loads the stored columns through `read_run_blobs`, which reads
  `run_node_outputs` only.
- Wire readers: `GET /api/v1/runs/{run_id}/io` returns `node_telemetry`
  (`backend/src/farna/api/routes/runs.py:2382-2404`, route at `:2583`), and
  `GET /api/v1/runs/{run_id}/nodes/{node_id}/output` returns a derived status
  for a node that has no pure return (`...:3064-3110`). Both can expose the key.
- Tests: `backend/tests/unit/core/test_node_runner_residual.py:3290`;
  `backend/tests/unit/pipeline_engine/test_executor.py:4585`, `:4607` and
  `:4768`; `backend/tests/unit/pipeline_engine/test_executor_coverage.py:1188`.

### What breaks if it changes

This is the highest-risk value of the four. If a release writes the new key
but an older release finalizes the run, the older release reads only the old
key and misses the marker. The failed sandbox node then counts as completed and
the run can finalize `complete` (a masked failure). The same happens in reverse
if a release reads only the new key against rows written by an older release.

That cross-release read is not theoretical. A run can live across a rolling
deploy, and a HITL resume only re-emits the resumed segment's events, so the
stored row is the only place a prior segment's masked failure is visible
(`finalize.py:258-273`).

### Recommendation: add `farna_control_synthetic_failure`, dual-write, dual-read, then drop

The key must stay collision-resistant. The finalize scan also inspects the pure
agent return (`outputs_json`), so a bare `synthetic_failure` key would let an
agent-authored `{"status": "failed", "synthetic_failure": true}` masquerade as
the runner's marker and downgrade a run that should finalize `complete`. The
namespaced alternative from decision 2 (`farna_control_synthetic_failure`)
restores the collision resistance the legacy key had. It repeats the rebrand
problem at the next rename, but the masked-failure safety path outweighs that
cost.

1. **R1**: define `SYNTHETIC_FAILURE_MARKER = "farna_control_synthetic_failure"`
   and `LEGACY_SYNTHETIC_FAILURE_MARKERS = ("modulo_synthetic_failure",)`. When
   the field is true, write BOTH keys into the envelope. Change the predicate
   to match either key. Dual-write is mandatory here because an older release
   may finalize a run written by a newer one.
2. **R2** (at least 24 hours after R1): stop writing the old key. Keep reading
   it. The old key remains in every historical row, which is correct: those
   rows are evidence, and the finalize predicate is the only reader that acts
   on them.
3. **R3** (at least 24 hours after R2): drop the old key from the predicate.
   No live run can still be carrying a pre-R1 row at that point, because a run
   is bounded by `SAQ_RUN_TIMEOUT` (default 7200 seconds, maximum 86400
   seconds) and the stale-run sweeps cap the practical window at 1440 minutes.

Rollback: if R1 or R2 shows a problem, revert the writer to the old key (or to
dual-write) and keep the dual-read. No stored data needs to move. A rollback
after R3 requires restoring the old read arm from source control, because the
old key is still present in historical rows.

Verification:

```sql
-- New writes of the old key, after R2. Must be zero.
SELECT count(*)
FROM run_node_outputs
WHERE updated_at >= :r2_deploy_time
  AND node_telemetry_json ? 'modulo_synthetic_failure';
```

Also run a deliberate synthetic-failure case and assert the finalize log line
`pipeline.masked_sandbox_failure_downgraded` fires. The log line is the
observable proof that the predicate still works.

## 6. Value 4: `Modulo-Notifier/1.0`

### What it is and where it goes

`NOTIFIER_USER_AGENT` is the `User-Agent` header on every outbound notification
POST. It is a wire identifier: a customer's webhook endpoint or an edge filter
can match on it.

- Constant and docstring: `backend/src/farna/core/notifier/__init__.py:100-104`.
- Real delivery path: `...:805` in `_deliver_with_retries`.
- Admin retry and test delivery: `backend/src/farna/api/routes/admin_notifications.py:640`
  and `:1086`.
- Product map: `frontend/src/manifest.yaml:8183` states the header value
  verbatim.
- Public docs: no page under `Repos/modulo-website/src/docs` mentions the header
  or the user agent.

### Storage and readers

Nothing stores it. It is an outbound header only. `NotificationDeliveryLog`
records the response code, the error, the response body and an optional
encrypted request payload. It does not record request headers
(`backend/src/farna/db/models/notification_delivery.py:19-32`).

The readers are external: a receiver that allow-lists or routes on the user
agent, an edge firewall, or a logging pipeline.

### Test coverage

No test pins the notifier user agent. A repository search for
`NOTIFIER_USER_AGENT` in `backend/tests` returns nothing. The sibling
`Modulo-Watchdog/1.0` value is pinned at
`backend/tests/unit/core/watchdog/test_worker_liveness.py:1160`, which shows the
pattern the notifier is missing.

### Recommendation: change it before the first external alpha install

Change the product token to `Farna-Control-Notifier/1.0`. Keep the `1.0`
version.

Reasoning:

- The user agent is the only one of the four values that is explicitly branded
  on the wire. A customer's logs will show it.
- There is no external install yet, so the compatibility cost is zero today.
  After the first external install it becomes a deprecation-window problem.
- The version part of a user agent should name the notifier protocol, not the
  product release. The payload shape and the signature scheme are unchanged, so
  `1.0` stays honest. Bump it only on a breaking change to the webhook
  contract.
- A user agent is a single string, so we cannot send two product tokens at
  once. If a future receiver needs a grace period, the standards-compliant
  option is a comment:
  `Farna-Control-Notifier/1.0 (Modulo-Notifier/1.0)`. A substring filter on the
  old token still matches, and an exact-match filter fails. Do not build a
  configuration knob for this; announce the change in the release notes
  instead.

Update `frontend/src/manifest.yaml:8183` in the same change. That line is the
product map's record of the wire contract, and it must not describe the old
value after the code changes.

### External compatibility cost

- An exact-match allow-list on `Modulo-Notifier/1.0` breaks.
- A prefix or substring filter on `Modulo` breaks.
- A receiver that ignores the header is unaffected.
- There is no signing or authentication dependency on the user agent. The
  HMAC signature header is separate and unchanged.

### The sibling user-agent family

The notifier is not the only outbound user agent that still carries the retired
name. A coherent decision should cover the family, even though only the
notifier is in this plan's scope:

| Value | Location |
|-------|----------|
| `Modulo-Watchdog/1.0` | `backend/src/farna/core/watchdog/worker_liveness.py:353` |
| `Modulo-MetricsDump/1.0` | `backend/src/farna/core/product_analytics/vendor_client.py:126` |
| `Modulo-Error-Forwarder/1.0` | `backend/src/farna/core/error_tracking/forwarders/rollbar.py:79` and `.../sentry.py:157` |
| `Modulo-Error-Alert/1.0` | `backend/src/farna/core/error_tracking/alert_dispatcher.py:319` and `:385` |
| `modulo/{version}` | `backend/src/farna/model_backends/opencode/__init__.py:44` |

Two of these are pinned by tests: the watchdog value at
`backend/tests/unit/core/watchdog/test_worker_liveness.py:1160`, and the
opencode value at `backend/tests/unit/model_backends/test_opencode.py:191`,
`:226`, `:293` and `:339`. Changing them is mechanical, but each is a separate
external identifier with its own consumer audience.

## 7. Rollout and rollback

### Release plan

| Value | R1 | R2 | R3 |
|-------|----|----|----|
| `trust_header` | Keep (no change) | n/a | n/a |
| `modulo_minted` | Write `self_minted`; accept both | No write change | Optionally drop the legacy value from the accepted set |
| `modulo_synthetic_failure` | Dual-write both keys; read both | Stop writing the old key; keep reading | Drop the old read, 24 hours or more after R2 |
| `Modulo-Notifier/1.0` | Change the token; update the product map | n/a | n/a |

The two machine values may share one release if the target tokens are approved
together. They must not share a release with a change that alters the finalize
predicate's other inputs.

### Drop criteria

The old value may be dropped in R3 only when all of these hold:

1. R1 has been deployed to every instance, including workers, for more than 24
   hours (`SAQ_RUN_TIMEOUT` maximum is 86400 seconds).
2. R2 has been deployed to every instance, and the verification query in
   section 5 returns zero new writes of the old key.
3. A synthetic-failure drill still produces the downgrade log line.

If any condition fails, wait one more release. There is no cost to keeping the
old read arm for an extra release.

### Rollback

Every change is additive, so rollback is a writer revert, not a data restore.

- `modulo_minted`: revert the constant and the set. No data moves.
- `modulo_synthetic_failure`: revert to dual-write or to the old key. The
  dual-read stays in place, so an older writer and a newer reader still agree.
- `trust_header`, if migrated: reverse the backfill with the equality-guarded
  reverse mapping, exactly like the downgrade in migration `0364`.
- User agent: revert the constant and the product-map line.

The only irreversible step is R3 for the synthetic marker. Do it last, and only
after the verification in section 5.

## 8. Tests and gates

The brand-consistency gate is not enough on its own. It counts occurrences of
the retired name per file and fails on a new file or a grown count
(`backend/tests/architecture/test_brand_consistency.py`). The companion
`scripts/run_check_ratchet_baseline.py` fails CI when a shrink-only baseline is
raised. Together they stop new drift, but they do not stop an in-place swap
(`modulo_minted` to `farna_control_minted`) because the count stays the same.

Add these gates.

1. **A persisted-identifier contract test.** Add a module named
   `test_persisted_identifier_contract.py` under `backend/tests/architecture/`
   (it does not exist yet). It asserts the exact literal for each value, the
   legacy alias while the window is open, and a comment naming this plan. At
   minimum:
   - `ISSUANCE_PROVENANCE_SELF_MINTED == "self_minted"` (or the approved value)
     and `"modulo_minted" in ISSUANCE_PROVENANCES` until R3.
   - `SYNTHETIC_FAILURE_MARKER == "farna_control_synthetic_failure"` and
     `LEGACY_SYNTHETIC_FAILURE_MARKERS == ("modulo_synthetic_failure",)` until
     R3.
   - `NOTIFIER_USER_AGENT == "Farna-Control-Notifier/1.0"`.
   - The seven built-in collections' `content_json["trust_header"]["source"]`
     equals the approved value.
   A constant test alone would not catch a change to the read path, so pair it
   with the next item.
2. **Dual-read regression tests.** These are the tests that prove the old value
   still works. Feed a stored telemetry view carrying the legacy key into
   `_downgrade_masked_sandbox_failures` and assert the run is downgraded to
   `failed`. Feed `IssuedRunCredential(provenance="modulo_minted")` and assert it
   is accepted during the window. A constant test that only checks a string
   does not exercise the code path that protects against a masked failure.
3. **A user-agent wire test.** Use `httpx.MockTransport` or the existing
   notifier test harness to capture the outbound request and assert the
   `User-Agent` header. Add a second assertion that the manifest text contains
   the `NOTIFIER_USER_AGENT` value, so the product map cannot drift from the
   code. The sibling watchdog test shows the shape.
4. **Brand baseline bookkeeping.** A new doc contains the retired name, so the
   brand gate fails on a new file until the file is registered. Stage the file,
   then add `docs/rebrand-persisted-values-migration.md<TAB><count>` to
   `backend/tests/architecture/brand_consistency_baseline.txt`, where the count
   comes from
   `git grep -i -F -o -- modulo -- docs/rebrand-persisted-values-migration.md | wc -l`.
   `git grep` reads tracked files, so the count is taken after `git add`; a
   plain `grep -o -i -F modulo <path> | wc -l` gives the same number before
   staging. The ratchet guard allows an entry for a path that does not exist at
   the base revision, so a new file is legal. Lower the counts of any file
   whose occurrences this work removes; never raise one.
5. **A Semgrep rule (optional, but preferred for the sweep guard).** Add a rule
   that forbids a new `Modulo-` string literal in a `User-Agent` header, and a
   new `modulo_` prefixed key in `backend/src`, with an explicit allowlist for
   the legacy alias lines and the historical migrations. This is the structural
   version of the rule "a future sweep must not change these values by
   accident". If the rule is too broad to keep green, the contract test in item
   1 is the minimum acceptable gate.
6. **Test-style scanner.** Any new test file must pass
   `uv run pytest tests/architecture/test_test_suite_quality.py` from
   `backend/`, because that scanner only runs on changed test files when the
   full suite runs otherwise.

## 9. Decision list for Duncan

Each item says whether it needs a decision or is mechanical once the decision
is made.

1. **`trust_header.source`: keep or migrate.** Your call. The recommendation is
   keep: no product reader, operator-editable data, and the correct identity is
   already on the `source` column. If you choose migrate, the work is
   mechanical: new seed value, equality-guarded migration `0365`, dual-read,
   tests.
2. **Target token shape for the two machine values.** Decided: `self_minted`
   is brand-free, but the synthetic marker must be namespaced
   `farna_control_synthetic_failure`. A bare `synthetic_failure` collides with
   agent-authored JSON scanned by the finalize predicate (see section 5), so
   the product-prefixed alternative wins for that one value.
3. **`modulo_synthetic_failure`: migrate or keep.** Decided: migrate with
   dual-write and dual-read, using the namespaced new key. If you prefer zero
   risk later, keep the legacy key permanently and add only the pin test.
4. **`modulo_minted` companions.** Your call. The recommendation is to change
   `issuer` and `ModuloCredentialIssuer.name` from `"modulo"` to
   `"farna_control"` in the same release as the provenance token, because the
   same span event carries all three. If you only want the provenance token
   changed, record that the other two stay.
5. **User agent: change now, and how far.** Your call. The recommendation is to
   change the notifier token to `Farna-Control-Notifier/1.0` before the first
   external alpha install, keep the `1.0` protocol version, and sweep the
   sibling user-agent family in the same change. The alternative is to keep the
   notifier value and accept the retired brand on the wire.
6. **Release compression.** Your call. Because no external install exists, the
   user-agent change needs no window. The two data values should still follow
   R1, R2, R3, because our own staging and production databases are the
   compatibility surface. Compressing them is possible only if staging and
   production are rebuilt from scratch between releases.

Mechanical once the above are decided: the migrations, the dual-write and
dual-read code, the pin tests, the user-agent wire test, the brand-baseline
entry, and the product-map update.

## 10. Adjacent values found during enumeration (not in this plan's scope)

These are not part of the four targeted values. They are recorded so a future
sweep does not treat them as missed targets or, worse, change them by accident.

1. **Registry entry authors are still `"modulo"`.**
   `backend/src/farna/core/registry/__init__.py:237-384` and `:598` build
   registry entries with `author="modulo"`. The author is part of the signed
   payload: `_build_entry` computes the checksum and the Ed25519 signature over
   a payload that includes the author (`...:193-223`), and the entry slug is
   `"modulo/<name>"` (`:208`). Renaming the author therefore requires
   regenerating every checksum and signature and rewriting every pulled
   `library_primitives.author` row. Migration `0357` documents this and
   deliberately left it. Keep it unless the whole registry is re-signed.
   `resolve_namespaced_slug` also defaults a bare slug to the `"modulo"` author
   (`...:524-534`), so a registry rename would change slug resolution.
2. **`backend/scripts/gen_seed_sql.py` emits `source='modulo'`.** Lines 32 and
   35 write `author='modulo'` and `source='modulo'`. The current CHECK
   constraint rejects `source='modulo'` (migration `0364` narrowed it to
   `local`, `registry`, `farna_control`, `community`), so the generated SQL
   would fail if run today. This is a stale developer helper, not a production
   path, but it should be fixed or retired.
3. **`backend/scripts/seed_library_templates.py:49` writes `author="modulo"`**
   with `source="local"` and a non-null `forked_from`. Migration `0357`'s author
   rewrite does not match that shape (it matches `source="modulo"`, or
   `source="local"` with a null `forked_from`), so this manual seed would
   reintroduce the retired author. Fix or retire the script.
4. **The UI badge discriminant is `"modulo"`.**
   `frontend/src/views/LibraryView.vue:116` passes `badge="modulo"` and
   `frontend/src/components/library/LibraryPrimitiveCard.vue:15` compares it.
   The displayed text is already `Farna Control`
   (`frontend/src/locales/en-US.js:465`). This value is not persisted and not a
   wire value. Renaming it is safe but is a component-contract change with its
   own tests; it does not belong in a data migration.
5. **The MCP resource URI scheme is `modulo://`.** Thirteen resources under
   `backend/src/farna/api/mcp_server.py:13869-14528` use it, for example
   `modulo://pipelines`, `modulo://runs/{run_id}` and
   `modulo://library/{primitive_type}/{slug}`. An MCP client addresses a
   resource by URI, so this is a wire contract in the same class as a user
   agent. The product map and `docs/deployment-security.md:168` also quote the
   scheme. Renaming it needs an alias window (serve both schemes) and a client
   migration plan; it is out of scope here but should be decided with the user
   agent rather than left to a later sweep.
6. **Telemetry service names and metrics still say `modulo`.**
   `backend/src/farna/settings.py:1167` defaults the OTel service name to
   `"modulo"`, `backend/src/farna/otel_bridge/export.py:26` does the same, and
   the notifier meter is `modulo.notifier` (`core/notifier/__init__.py:206`).
   These are observability identifiers, not product data. They are valid
   candidates for a separate telemetry rename, with a dashboard migration.

## Appendix A: value inventory

| Value | Producers | Storage | Readers |
|-------|-----------|---------|---------|
| `trust_header.source = "modulo"` | `_seed_data.py:1129,1263,1380,1494,1583,1697,1824` | `library_primitives.content_json`, sentinel and tenant copies | REST `content_json`, MCP library resource, two tests |
| `modulo_minted` | `credential_issuer.py:55,91,147,201`; `node_runner.py:8885-8890,12309`; `runner_dispatch.py:1266` | sandbox env `FARNA_CONTROL_CREDENTIAL_PROVENANCE`; OTel span `credential.issued`; in-memory handle. No database column, no API field | sandbox scripts; OTel backend; tests |
| `modulo_synthetic_failure` | `node_runner.py:758,10042-10043,14915` | `run_node_outputs.node_telemetry_json` (and legacy mixed `outputs_json`) | `finalize.py:193-201,237-340`; `GET /runs/{id}/io`; `GET /runs/{id}/nodes/{node}/output`; tests |
| `Modulo-Notifier/1.0` | `notifier/__init__.py:104,805`; `admin_notifications.py:640,1086` | none (outbound header) | customer webhook receivers; product map `manifest.yaml:8183` |

## Appendix B: verification commands

Run from the repository root unless a command says otherwise.

```bash
# Every remaining producer of each targeted value.
git grep -n -E 'modulo_minted|modulo_synthetic_failure|Modulo-Notifier' -- backend/src frontend/src
git grep -n '"source": "modulo"' -- backend/src

# The brand gate, after adding the new doc to the baseline.
python3 scripts/run_check_ratchet_baseline.py --base origin/main
(cd backend && uv run pytest tests/architecture/test_brand_consistency.py -q)

# The exact count to record for the new doc (after git add; see section 8).
git grep -i -F -o -- modulo -- docs/rebrand-persisted-values-migration.md | wc -l
```

The database queries in sections 3 and 5 are the authoritative checks for
stored values. A grep proves only what the source tree writes; it says nothing
about rows that existing installs already hold.
