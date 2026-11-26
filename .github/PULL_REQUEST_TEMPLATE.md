## Description

<!-- Briefly describe the change and why it's needed. -->

## Changelog classification

<!-- Apply EXACTLY ONE `changelog:*` label to this PR (a CI check fails
     user-facing PRs that carry none):
  - `changelog: breaking` — breaking change
  - `changelog: feature`  — new feature
  - `changelog: fix`      — bug fix
  - `changelog: none`     — internal / refactor / test / docs / infra work;
                            a valid and honest answer, not a cop-out
Only `feature`, `fix`, and `breaking` produce a changelog entry. -->

## High-level summary

<!-- One or two sentences a product user would recognise — this is what ends
     up in the release notes. -->

## Proving test

<!-- A PR that changes backend/src/** or frontend/src/** must name the test
     that would FAIL without the change, one `Proves:` line per test (the
     `Prove the fix marker` workflow checks it). A change that touches no
     product source can leave this empty. Convention:
     docs/product-map/README.md

       Proves: backend/tests/unit/api/test_health.py::test_liveness_ok
       Proves: frontend/src/__tests__/Badge.spec.ts::defaults to the neutral variant
-->

Proves: <!-- fill in above, one test per line; an empty line is ignored, so
     leave it empty when the change touches no product source -->

## Checklist

- [ ] ruff, mypy, bandit, semgrep pass
- [ ] All tests pass (pytest + vitest)
- [ ] vue-tsc --noEmit passes (for frontend changes)
- [ ] No .env files or secrets committed
- [ ] Changelog updated (if user-facing)
