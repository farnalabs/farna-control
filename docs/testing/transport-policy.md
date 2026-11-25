# Strict transport policy and contract round-trip

FAR-2702. Two independent mechanisms, both incremental, that close part of the
gap between "the test is green" and "the wire contract is real".

The gap they address: 124 test modules fake 3,000+ tests through `respx`, and
only three recorded cassettes exist. A stub was never diffed against a real
provider response, so a test can prove an assumption nobody wrote down.

- **Strict transport policy** — makes fake transport *visible*: an outbound
  request nobody stubbed, or a stub that was never exercised, is a finding.
- **Contract round-trip** — makes one narrow slice of the real wire format
  *executable*: a sampled set of GET routes is called for real and checked
  against the OpenAPI schema and the generated frontend type.

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

Implementation notes (why these hooks):

- `Router.record` is the single funnel every request passes through and the
  point where an unmatched request is surfaced with `route=None`.
- `MockRouter.stop` is the single teardown point and still sees the router's
  own routes before `rollback()` resets them. Patching these two class methods
  covers every router, including ones a test builds directly.
- The singleton `respx.mock` is cleared at the start of each test in strict
  mode. `respx.get(...)` inside `with respx.mock(...)` registers on the
  singleton, not the scoped router, so a route a test never matched would leak
  into the next test and be blamed on it. Clearing per test makes findings
  order-independent (a subset run reports the same violations as a full run).

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
  "version": 1,
  "modules": ["tests/unit/cli/test_apply_pipeline.py", "..."],
  "violations": {
    "tests/unit/cli/test_apply_pipeline.py::test_x": ["unused_route"]
  }
}
```

- `modules` is every collectable test module that imports `respx` (AST scan).
  A new module using `respx` must be added, so nothing slips past the policy
  scan.
- `violations` is keyed by test node id, with the sorted kinds observed.
- The baseline is **shrink-only**: a violation that is not in it fails the
  gate. A baseline entry that no longer reproduces is reported as
  "baseline may shrink" and never fails — remove it when convenient.

Enforcement lives in two places with the same rule:

- `backend/tests/architecture/test_transport_policy.py` — the module inventory,
  baseline well-formedness (kinds, module existence, test function existence),
  and the new-violation check against a supplied report.
- `scripts/run_transport_strict.py` — the runner. It selects modules (default:
  test files changed versus `--base-ref`, i.e. the PR base), runs them with the
  flag on, prints the report, compares against the baseline and exits non-zero
  on a new violation.

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
test modules, then re-runs the architecture test with
`FARNA_STRICT_TRANSPORT_REPORT` pointing at the report, so the ratchet is a
gate even if the runner were bypassed. It skips green when no backend test
module changed.

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
`/api/v1/me/settings`). It is deliberately not comprehensive — a broad sweep is
schema freshness. What this adds is an *executed* response, not a mocked one.

A negative test pins the defect class: a response carrying `displayName` where
the contract declares `display_name` must be reported (missing required key +
undeclared key), and the generated type must not contain `displayName`. Without
that test the checker could be vacuous.

CI runs it in `contract-roundtrip` when `backend/src/farna/api/` or
`frontend/src/lib/api/schema.ts` changes (Docker/Testcontainers, like the other
integration jobs).

### Limitations, stated plainly

- **No provider drift detection.** Every check compares our response to our
  schema. If a provider changes a field name or type, both sides can be wrong
  together. FAR-2703's recorded real responses are the only defence.
- **Top-level keys only on the frontend side.** The generated-type check
  compares top-level properties; nested objects are covered by the OpenAPI
  recursion, which is generated from the same models.
- **Strict mode is observational.** Violations are collected and turned into a
  failure by a pytest report hook, which keeps the message (which routes, which
  kind) instead of an opaque `respx` assertion. The instrumentation does not
  change request behaviour.
- **Serial by default.** Per-test state is thread-local, so the runner does not
  pass `-n`. `--jobs` exists for local baseline generation; each worker writes
  its own report shard and the findings are identical because attribution is
  per test.
