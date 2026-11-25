# Strict transport policy and contract round-trip

FAR-2702. Two independent mechanisms, both incremental, that close part of the
gap between "the test is green" and "the wire contract is real".

The gap they address: 124 test modules fake 3,000+ tests through `respx`, and
only three recorded cassettes exist. A stub was never diffed against a real
provider response, so a test can prove an assumption nobody wrote down.

- **Strict transport policy** — makes fake transport *visible*: an outbound
  request nobody stubbed, a stub that was never exercised, or a write to our
  own API without the required headers is a finding.
- **Contract round-trip** — makes one narrow slice of the real wire format
  *executable*: a sampled set of GET routes is called for real and checked
  against the OpenAPI schema and the generated frontend type, and a ratchet
  requires every route in a changed API module to have a case.

Neither detects provider drift. A stub can only disagree with another
assumption of ours; proving a connector still matches GitHub's real payload is
FAR-2703 (record-and-replay against real responses). That limitation is the
whole reason these two mechanisms are additive, not a substitute.

---

## 1. Strict transport policy

### What it checks

When enabled, every `respx` router is instrumented (`backend/tests/_transport_strict.py`),
however the test builds it — the bare `respx.mock` context manager/decorator,
`respx.mock(...)` scoped routers, or a router the test module constructs itself.
A test records a finding when:

| kind | meaning |
| --- | --- |
| `unmatched_request` | a request reached the router but matched no registered route. `respx` auto-mocks a 200 for those when `assert_all_mocked=False`, so the test would otherwise pass against an assumption that appears nowhere in the test. |
| `unused_route` | a registered route was never called before the router stopped. The stub is dead weight and may be pinned to a wire shape the provider no longer returns. |
| `missing_write_headers` | a non-safe request to our own API (`/api/...`) lacks `Authorization` or `Idempotency-Key`. Third-party provider calls are out of scope: their auth/idempotency conventions belong to FAR-2703. |

Two further properties make the policy an actual control:

**A finding is keyed by `(nodeid, kind, detail fingerprint)`.** The fingerprint
is a stable hash of the finding's detail with volatile pieces (UUIDs, numbers,
object addresses) normalised. A test that already has one baselined
`unused_route` and grows a SECOND dead stub therefore produces a new identity
and fails. Keying on `(nodeid, kind)` let any number of stubs hide behind one
baselined entry.

**Baselined findings do not fail their test.** The pytest hooks compare each
finding against the baseline and fail the test only for one the baseline does
not record. Baselined findings are still drained and written to the report. This
is what makes pytest's exit code trustworthy: any exit code outside `{0, 5}` is
a genuine failure (a new violation or a real assertion failure), never the
ordinary consequence of running a module whose known findings are baselined.

### Implementation notes

- `Router.record` is the single funnel every matched, unmatched and pass-through
  request passes through and the point where an unmatched request is surfaced
  with `route=None`. It is also where the write-header check runs.
- `MockRouter.stop` is the single teardown point and still sees the router's
  own routes before `rollback()` resets them. Patching these two class methods
  covers every router, including ones a test builds directly.
- The singleton `respx.mock` is **snapshotted after fixture setup and restored
  at teardown** (not cleared). A module- or session-scoped fixture registers its
  routes during setup; clearing at every test start destroyed those and made
  findings order-dependent. Restoring removes only the routes the test itself
  added, and reports any it left unused.
- Findings that belong to **no test item** (a session-scoped router stopped
  after the last test, a request a session fixture made) are kept in a session
  bucket and fail the session at `pytest_sessionfinish` unless baselined. They
  are recorded under the reserved `<session>` node id.
- In strict mode an autouse **sentinel router** with `assert_all_mocked=True` is
  started for every test. It registers no routes, so a raw `httpx` call that no
  test router matches raises instead of reaching the network. A test that
  deliberately makes a live call opts out with
  `@pytest.mark.transport_allow_live_http`.
- A test that deliberately exercises a header-free write opts out of the header
  rule with `@pytest.mark.transport_exempt_write_headers`. Both markers are
  registered in `tests/conftest.py`.

`quiet=True` teardown (the test already raised) suppresses `unused_route`
reporting: a test that failed for another reason is noisy enough.

### Enabling it

```bash
# one module
FARNA_STRICT_TRANSPORT=1 uv run pytest tests/bdd/steps/test_n8n.py -q

# machine-readable report (JSON lines, one object per finding)
FARNA_STRICT_TRANSPORT=1 \
FARNA_STRICT_TRANSPORT_REPORT=/tmp/strict.jsonl \
uv run pytest tests/unit/cli/test_apply_pipeline.py -q
```

Default behaviour is unchanged: with the env var unset nothing is patched and no
hook does work.

### The baseline and the ratchet

Findings are recorded in
`backend/tests/architecture/transport_policy_baseline.json`:

```json
{
  "version": 2,
  "modules": ["tests/unit/cli/test_apply_pipeline.py", "..."],
  "violations": {
    "tests/unit/cli/test_apply_pipeline.py::test_x": [
      {"kind": "unused_route", "fingerprint": "1d7a4547dbf247cb"}
    ]
  }
}
```

- `modules` is every collectable test module that imports `respx` (AST scan).
  A new module using `respx` must be added, so nothing slips past the policy
  scan.
- `violations` is keyed by test node id, each entry a `(kind, fingerprint)`
  identity. Two dead stubs in one test are two entries.
- The baseline is **shrink-only**: an identity that is not in it fails the gate.
  An entry that no longer reproduces is reported as "baseline may shrink" and
  never fails — remove it when convenient.

Enforcement lives in three places:

- `backend/tests/architecture/test_transport_policy.py` — module inventory,
  baseline well-formedness (version, kinds, module/function existence,
  fingerprints), the new-violation check against a supplied report, a
  fingerprint negative test, and a meta-test that drives the instrumentation and
  asserts every kind is recorded.
- `scripts/run_transport_strict.py` — the runner. It selects modules (default:
  test files changed versus `--base-ref`, i.e. the PR base), runs them with the
  flag on, prints the report, compares identities, and exits non-zero on a new
  violation **or any pytest exit code outside `{0, 5}`**.
- `scripts/run_check_ratchet_baseline.py` — the FAR-2699 guard compares this
  file against the base revision. An **added violation identity** is a loosening
  and fails, so the same PR cannot both break a test and widen the baseline that
  excuses it.

### Runner exit codes

| code | meaning |
| --- | --- |
| 0 | no new violation, and pytest exited 0 or 5 (nothing collected) |
| 1 | a new violation, or pytest exited with any other non-zero code (a real test failure) |
| 2 | the invocation was unusable: an unresolvable base ref, a failed `git diff`, or modules selected but no report produced |

Exit 2 exists because the old runner returned `[]` for a failed `git diff` and
exited 0: a policy that silently checks nothing is worse than no policy. CI
never wraps that diff in `|| true`.

### Shrinking the baseline

```bash
# remove entries for modules whose violations no longer reproduce
python scripts/run_transport_strict.py --all --shrink-baseline

# record the current findings for the scanned modules (use ONLY for a
# pre-existing defect deliberately out of scope — say why in the PR)
python scripts/run_transport_strict.py --all --write-baseline

# regenerate the module inventory too (adds a newly respx-using module)
python scripts/run_transport_strict.py --all --write-baseline
```

`--all` runs every module listed in `modules`, which is slow (the whole respx
surface). CI never does that: it runs only the modules a PR changed.

### CI

`transport-policy` in `.github/workflows/ci.yml` runs the runner over changed
test modules, then **unconditionally** re-runs the architecture test with
`FARNA_STRICT_TRANSPORT_REPORT` pointing at the report. The runner always
creates that file (an empty one when no test module changed), so a missing file
means the runner did not run and the architecture test FAILS rather than
skipping.

---

## 2. Contract round-trip

`backend/tests/integration/test_contract_roundtrip.py` calls a small, explicit
sample of GET routes through the ASGI app against Postgres and checks each live
response against:

1. the app's OpenAPI schema (types, required keys, no undeclared keys,
   recursively), and
2. the generated frontend type in `frontend/src/lib/api/schema.ts` (live keys
   are declared; required generated props are present).

The sample is three routes (`/api/v1/me`, `/api/v1/auth/me`,
`/api/v1/me/settings`). What this adds is an *executed* response, not a mocked
one.

A negative test pins the defect class: a response carrying `displayName` where
the contract declares `display_name` must be reported (missing required key +
undeclared key), and the generated type must not contain `displayName`. Without
that test the checker could be vacuous.

### The ratchet: a changed route cannot dodge the sample

A fixed sample never touches the route a PR actually changed. The inventory is
every operation in the app's OpenAPI document, each mapped to the module that
defines its handler (via each `APIRoute.endpoint.__module__`). CI passes the
`backend/src/farna/api/...` files this PR changed through
`FARNA_CONTRACT_CHANGED_API_FILES`; the test converts them to modules and fails
when a route owned by a changed module has neither a round-trip case nor an
entry in `backend/tests/architecture/contract_roundtrip_baseline.json`.

The baseline lists routes with no case yet, `"<method> <path>"` each. It is
validated every run: every entry must still exist in the OpenAPI document and
must not be covered by a case (remove it when it gains one). A pure predicate
test drives the rule with synthetic data, so the behaviour is proven on a local
run where no API file changed.

CI runs it in `contract-roundtrip`, named **"Contract round-trip ratchet
(sampled live routes)"** because it is narrower than "every changed route": the
live call is still the three-route sample. The ratchet is what guarantees a
changed route is either covered or explicitly declared uncovered.

### Limitations, stated plainly

- **No provider drift detection.** Every check compares our response to our
  schema. If a provider changes a field name or type, both sides can be wrong
  together. FAR-2703's recorded real responses are the only defence.
- **Top-level keys only on the frontend side.** The generated-type check
  compares top-level properties; nested objects are covered by the OpenAPI
  recursion, which is generated from the same models.
- **Strict mode is observational.** Violations are collected and turned into a
  failure by a pytest report hook for the UNBASELINED ones, which keeps the
  message (which routes, which kind) instead of an opaque `respx` assertion.
- **Serial by default.** Per-test state is thread-local, so the runner does not
  pass `-n`. `--jobs` exists for local baseline generation; each worker writes
  its own report shard and the findings are identical because attribution is
  per test.
