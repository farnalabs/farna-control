# GitHub required status check (Farna Control verdict)

Farna Control does **not** merge pull requests. It produces a delivery verdict and
publishes it to GitHub as a commit status, so the repository's own branch
protection enforces it. This document is the contract for that publication
(FAR-2862, GitHub only).

The reason this is more than "post a status": a published check that is **not**
marked required in branch protection blocks nothing. The value of the feature is
the pair of operations —

1. publish the verdict as a status, and
2. read branch protection back to prove the status is a **required** check.

A caller that publishes without the read cannot tell the operator whether the
verdict actually gates merging.

## The check context (stable, documented)

Farna Control publishes under the commit-status context:

```
farna-control/verdict
```

It is exposed as `farna.connectors.github.REQUIRED_CHECK_CONTEXT` and is pinned
by a unit test. Treat it as a contract: a repository's branch-protection rule
stores this exact string, so renaming it silently un-blocks every merge until an
operator updates the rule. Do not change it without a documented migration.

The context is namespaced under `farna-control/` so it cannot collide with a CI
job's check name, and `verdict` names exactly what it publishes — Farna
Control's pass/fail decision, not a build.

## The verdict states

A Farna Control verdict is binary. The `state` field accepts exactly:

| State | Meaning |
|---|---|
| `success` | The delivery passed; the check is satisfied. |
| `failure` | The delivery failed; a required check blocks the merge. |

GitHub's commit-status API also accepts `pending` and `error`, but neither is a
verdict (`pending` is not a decision; `error` is a plumbing/transport failure),
so the connector rejects them before publishing anything.

## Publishing the verdict — `write("commit_status")`

```python
ConnectorPayload(
    resource="commit_status",
    data={
        "repo": "owner/repo",
        "sha": "<commit sha>",
        "state": "success" | "failure",
        "description": "human-readable reason",   # optional
        "context": "farna-control/verdict",        # optional; defaults to the constant
        "target_url": "https://…/runs/<id>",       # optional
        "branch": "main",                          # optional; see below
    },
)
```

It calls `POST /repos/{owner}/{repo}/statuses/{sha}` and returns:

```json
{
  "check": {
    "context": "farna-control/verdict",
    "state": "success",
    "description": "…",
    "sha": "…",
    "target_url": "…",
    "status_id": 12345,
    "url": "https://api.github.com/repos/owner/repo/statuses/12345"
  },
  "enforcement": { "…": "present only when `branch` is supplied" }
}
```

When `branch` is supplied, the write **reads that branch's protection first**,
then publishes the status, and includes the `enforcement` object (below) in the
result. The order matters: a token that may write statuses but not read branch
protection fails on the read *before* any status is posted, so a retry can never
duplicate a real side effect. The branch to evaluate is the branch the merge
would target (usually the PR base branch), not necessarily the branch the commit
was authored on.

Every input is validated before the network call, so a malformed payload never
publishes a status. Two inputs are also shape-checked because they are
interpolated into the request path:

- `repo` must be exactly `owner/name` — two non-empty segments, no leading,
  trailing or doubled slash, no `.`/`..` segment and no whitespace. Anything
  else is rejected with a `ValueError` rather than risking a path that reaches a
  different endpoint.
- `sha` must be a **full** Git object id: 40 hex characters (SHA-1) or 64
  (SHA-256). A short, relative or slash-bearing value is rejected.

## Reading enforcement — `query("branch_protection")`

```python
ConnectorQuery(
    resource="branch_protection",
    filters={"repo": "owner/repo", "branch": "main", "context": "farna-control/verdict"},
)
```

`branch` is required; `context` defaults to `REQUIRED_CHECK_CONTEXT`. It calls
`GET /repos/{owner}/{repo}/branches/{branch}/protection` (the branch name is
URL-encoded, so `release/1.0` becomes `release%2F1.0`) and returns one record:

```json
{
  "context": "farna-control/verdict",
  "branch": "main",
  "protected": true,
  "status_checks_required": true,
  "enforced": true,
  "state": "enforced",
  "required_contexts": ["farna-control/verdict", "ci/test"],
  "required_checks": [{"context": "farna-control/verdict", "app_id": null}],
  "reason": "required"
}
```

GitHub reports required status checks in two shapes: the legacy `contexts`
list and the newer `checks` list of `{context, app_id}` entries. A context is
*required* when it appears in either; `required_contexts` is the full required
set and `required_checks` is returned so an operator can see each entry's
`app_id`.

A context counts as **enforced** only when a plain commit status can satisfy it:
it appears in the legacy `contexts` list, **or** in a `checks` entry whose
`app_id` is null. Farna Control publishes a plain commit status. A check pinned
to a specific GitHub App (`app_id` set) is still required by the branch, but a
plain status can never satisfy it — reporting it enforced would be a fail-open
false positive, exactly the failure this feature exists to prevent. Such a
context is reported with `enforced: false` and the distinct reason
`required_by_app_only`.

`state` is the display form: `enforced` when the context is satisfied, otherwise
`advisory`; an unreadable configuration is `unknown` (see below). `reason` names
why it is not enforced:

| `reason` | Meaning |
|---|---|
| `required` | The check is in the required set and gates merging. |
| `required_by_app_only` | The context is required only through a GitHub-App-pinned `checks` entry, which a plain commit status cannot satisfy — not enforced. |
| `not_required` | Branch protection exists and requires status checks, but not this context — the verdict is advisory. |
| `status_checks_not_required` | The branch is protected but "Require status checks to pass" is off — advisory. |
| `branch_not_protected` | The branch has no protection rule at all — advisory. |
| `protection_unreadable` | A `200` response whose configuration could not be interpreted — `state` is `unknown`, not advisory (see below). |

## What happens when the check is not required

The status still appears on the commit and in the pull request's checks list,
but branch protection does not wait for it and does not block the merge. It is
**advisory**: a human sees a red `farna-control/verdict` check but GitHub will
let the merge through. This is exactly the state the enforcement read exists to
surface.

## How a customer marks the check required

GitHub enforces required status checks from the branch protection rule for the
target branch.

- **UI:** repository **Settings → Branches →** the rule for the branch (or
  **Add branch protection rule**) → enable **Require status checks to pass
  before merging** → search for `farna-control/verdict` → select it → save.
- **API:** `PATCH /repos/{owner}/{repo}/branches/{branch}/protection/required_status_checks`
  with `{"strict": <bool>, "contexts": [..., "farna-control/verdict"]}`.

The context only appears in the picker once Farna Control has published at least
one status for a commit on that repository. After the rule is saved, the
enforcement read returns `"state": "enforced"`.

## Required token permissions

The connector authenticates with a single token (the GitHub connector's `token`
credential). Two distinct permissions are involved:

| Operation | Classic PAT scope | Fine-grained PAT permission |
|---|---|---|
| Publish a commit status (`commit_status`) | `repo` (or `public_repo` for a public repo) | **Commit statuses:** read and write |
| Read branch protection (`branch_protection`, and the `enforcement` block) | `repo`, and the token's user must have **admin** access to the repository | **Administration:** read, and admin access to the repository |

Notes:

- GitHub's branch-protection read endpoint requires admin rights on the
  repository. A token with only code/status permissions will get HTTP 403.
- On GitHub's free plan, branch protection on a **private** repository is not
  available, so the read may return 403 regardless of token scope.
- The `commit_status` write only publishes a status. It reads branch protection
  only when you pass `branch`, and then it reads **before** publishing (see
  above), so a token missing the read permission fails before any status is
  posted. The combined one-call result requires **both** permissions.

## When the configuration cannot be read

A successful (`200`) protection read whose shape cannot be interpreted is
**unknown**, never advisory:

- the response body is not a JSON object, or
- `required_status_checks` is present but is neither an object nor `null`.

In that case the record reports `state: "unknown"`, `reason:
"protection_unreadable"`, `enforced: false`, `protected: true`, and
`status_checks_required: null` (we cannot claim it is either on or off). Advisory
would assert the check is genuinely not enforced; an unreadable configuration is
unknown, so a caller must be able to fail closed on this state.

## Error semantics

The enforcement read must never be mistaken for a positive result when it could
not actually read the configuration:

| Situation | Behaviour |
|---|---|
| Branch exists but is not protected | GitHub returns `404 {"message": "Branch not protected"}`; the connector reports `protected: false`, `enforced: false` — a legitimate advisory answer, not an error. |
| Repository or branch genuinely missing | Any other `404` propagates as `GitHubNotFoundError` — never silently reported as advisory. |
| Protection response is unreadable | A `200` whose body or `required_status_checks` shape cannot be interpreted reports `state: "unknown"`, `reason: "protection_unreadable"` (never advisory). |
| Token lacks permissions (status write or branch-protection read) | `403` propagates as `GitHubAuthError` (`error_code="insufficient_scope"`). When `branch` is supplied the read runs first, so this fails before any status is published. |
| Rate limited / transient upstream failure | The connector's existing retry + circuit-breaker machinery applies; a terminal failure surfaces a structured `GitHubError`. |

Failing loudly on an unreadable configuration is deliberate: reporting
"advisory" when the token simply could not read the rule would hide exactly the
misconfiguration this feature exists to expose.

## Scope

- GitHub only (Gitea/Forgejo and GitLab are out of scope for this slice).
- No merge is performed or invoked. The connector's merge resource is untouched.
- This slice is the connector capability only; wiring it into the run surface and
  UI is a later slice.
