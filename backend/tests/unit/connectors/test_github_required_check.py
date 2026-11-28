"""Unit tests for the GitHub publish-and-verify required status check (FAR-2862).

Covers the two connector resources this slice adds:

- ``write("commit_status")`` — publish a Farna Control verdict as a commit
  status under the stable required-check context.
- ``query("branch_protection")`` — read a branch's protection and report
  whether that context is in the required set (enforced) or not (advisory).

HTTP is mocked with respx; every assertion is about the request we send or the
structured dict we return — no live GitHub call.
"""

import json

import httpx
import pytest
import respx

from farna.connectors.base import ConnectorPayload, ConnectorQuery
from farna.connectors.github import (
    REQUIRED_CHECK_CONTEXT,
    GitHubAuthError,
    GitHubConnector,
    GitHubNotFoundError,
    _required_status_check_contexts,
)

TOKEN = "ghp_test_token"
REPO = "owner/repo"
# A full 40-char (SHA-1) object id and a full 64-char (SHA-256) object id.
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PROTECTION_URL = f"https://api.github.com/repos/{REPO}/branches/main/protection"
STATUS_URL = f"https://api.github.com/repos/{REPO}/statuses/{SHA}"


@pytest.fixture
def connector() -> GitHubConnector:
    return GitHubConnector(token=TOKEN)


# ---------------------------------------------------------------------------
# The published context is a documented, stable contract.
# ---------------------------------------------------------------------------


def test_required_check_context_is_the_documented_constant() -> None:
    # A change here must be deliberate: customers pin this exact string in a
    # branch-protection rule, and a silent rename un-blocks every merge.
    assert REQUIRED_CHECK_CONTEXT == "farna-control/verdict"


# ---------------------------------------------------------------------------
# Query: read branch protection and decide enforced versus advisory.
# ---------------------------------------------------------------------------


@respx.mock
async def test_branch_protection_reports_enforced_when_context_required(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"required_status_checks": {"strict": True, "contexts": [REQUIRED_CHECK_CONTEXT]}},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is True
    assert enforcement["status_checks_required"] is True
    assert enforcement["enforced"] is True
    assert enforcement["state"] == "enforced"
    assert enforcement["reason"] == "required"
    assert REQUIRED_CHECK_CONTEXT in enforcement["required_contexts"]


@respx.mock
async def test_branch_protection_reports_advisory_when_context_not_listed(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"required_status_checks": {"strict": False, "contexts": ["ci/test"]}},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is True
    assert enforcement["status_checks_required"] is True
    assert enforcement["enforced"] is False
    assert enforcement["state"] == "advisory"
    assert enforcement["reason"] == "not_required"


@respx.mock
async def test_branch_protection_advisory_when_status_checks_disabled(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(200, json={"required_status_checks": None}),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is True
    assert enforcement["status_checks_required"] is False
    assert enforcement["enforced"] is False
    assert enforcement["reason"] == "status_checks_not_required"


@respx.mock
async def test_branch_protection_unprotected_branch_is_advisory_not_error(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(404, json={"message": "Branch not protected"}),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is False
    assert enforcement["enforced"] is False
    assert enforcement["state"] == "advisory"
    assert enforcement["reason"] == "branch_not_protected"
    assert not enforcement["required_contexts"]


@respx.mock
async def test_branch_protection_missing_repo_404_propagates(connector: GitHubConnector) -> None:
    # A 404 that is NOT "Branch not protected" is a genuinely missing repo or
    # branch — it must raise, never be reported as advisory.
    respx.get(PROTECTION_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
    query = ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"})
    with pytest.raises(GitHubNotFoundError):
        await connector.query(query)


@respx.mock
async def test_branch_protection_a_checks_entry_counts_as_required(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "required_status_checks": {
                    "strict": False,
                    "contexts": [],
                    "checks": [{"context": REQUIRED_CHECK_CONTEXT, "app_id": None}],
                },
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["enforced"] is True
    assert enforcement["required_checks"][0]["context"] == REQUIRED_CHECK_CONTEXT
    assert enforcement["required_checks"][0]["app_id"] is None


@respx.mock
async def test_branch_protection_app_pinned_check_is_not_enforced(connector: GitHubConnector) -> None:
    # A context pinned to a GitHub App is required, but a plain commit status
    # cannot satisfy it — reporting it enforced would be a fail-open false
    # positive, so it must be a distinct, non-enforced reason.
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "required_status_checks": {
                    "strict": False,
                    "contexts": [],
                    "checks": [{"context": REQUIRED_CHECK_CONTEXT, "app_id": 15368}],
                },
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is True
    assert enforcement["status_checks_required"] is True
    assert enforcement["enforced"] is False
    assert enforcement["state"] == "advisory"
    assert enforcement["reason"] == "required_by_app_only"
    assert REQUIRED_CHECK_CONTEXT in enforcement["required_contexts"]
    assert enforcement["required_checks"][0]["app_id"] == 15368


@respx.mock
async def test_branch_protection_legacy_context_wins_over_an_app_pin(connector: GitHubConnector) -> None:
    # The same context in the legacy list is satisfiable by a plain status,
    # even if a checks entry also pins it to an App.
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "required_status_checks": {
                    "contexts": [REQUIRED_CHECK_CONTEXT],
                    "checks": [{"context": REQUIRED_CHECK_CONTEXT, "app_id": 15368}],
                },
            },
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["enforced"] is True
    assert enforcement["state"] == "enforced"
    assert enforcement["reason"] == "required"


@respx.mock
async def test_branch_protection_unreadable_required_status_checks_is_unknown(connector: GitHubConnector) -> None:
    # A 200 whose config we cannot interpret is unknown, never advisory: an
    # unreadable config must not claim "not enforced".
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(200, json={"required_status_checks": "not-an-object"}),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["protected"] is True
    assert enforcement["status_checks_required"] is None
    assert enforcement["enforced"] is False
    assert enforcement["state"] == "unknown"
    assert enforcement["reason"] == "protection_unreadable"


@respx.mock
async def test_branch_protection_non_object_body_is_unknown(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(return_value=httpx.Response(200, json=[]))
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    enforcement = result.records[0]
    assert enforcement["state"] == "unknown"
    assert enforcement["reason"] == "protection_unreadable"
    assert enforcement["enforced"] is False


async def test_branch_protection_requires_a_branch(connector: GitHubConnector) -> None:
    query = ConnectorQuery(resource="branch_protection", filters={"repo": REPO})
    with pytest.raises(ValueError, match="requires 'branch' filter"):
        await connector.query(query)


async def test_branch_protection_rejects_a_blank_branch_filter(connector: GitHubConnector) -> None:
    # A present-but-empty branch is distinct from a missing key: both must be
    # rejected before any network call.
    query = ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": ""})
    with pytest.raises(ValueError, match="non-empty 'branch' filter"):
        await connector.query(query)


@respx.mock
async def test_branch_protection_rejects_a_blank_context(connector: GitHubConnector) -> None:
    query = ConnectorQuery(
        resource="branch_protection",
        filters={"repo": REPO, "branch": "main", "context": ""},
    )
    with pytest.raises(ValueError, match="'context' must be a non-empty string"):
        await connector.query(query)


@respx.mock
async def test_branch_protection_rejects_a_non_string_context(connector: GitHubConnector) -> None:
    query = ConnectorQuery(
        resource="branch_protection",
        filters={"repo": REPO, "branch": "main", "context": 123},
    )
    with pytest.raises(ValueError, match="'context' must be a non-empty string"):
        await connector.query(query)


@respx.mock
async def test_branch_protection_rejects_a_traversal_repo(connector: GitHubConnector) -> None:
    query = ConnectorQuery(resource="branch_protection", filters={"repo": "../repo", "branch": "main"})
    with pytest.raises(ValueError, match="'repo' must be exactly 'owner/name'"):
        await connector.query(query)


@respx.mock
async def test_branch_protection_rejects_an_owner_only_repo(connector: GitHubConnector) -> None:
    query = ConnectorQuery(resource="branch_protection", filters={"repo": "owner", "branch": "main"})
    with pytest.raises(ValueError, match="'repo' must be exactly 'owner/name'"):
        await connector.query(query)


@respx.mock
async def test_branch_protection_url_encodes_a_slash_in_the_branch(connector: GitHubConnector) -> None:
    encoded_url = f"https://api.github.com/repos/{REPO}/branches/release%2F1.0/protection"
    respx.get(encoded_url).mock(
        return_value=httpx.Response(
            200,
            json={"required_status_checks": {"contexts": [REQUIRED_CHECK_CONTEXT]}},
        ),
    )
    result = await connector.query(
        ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "release/1.0"}),
    )

    assert result.records[0]["enforced"] is True
    assert result.records[0]["branch"] == "release/1.0"


@respx.mock
async def test_branch_protection_exposes_rate_limit_metadata(connector: GitHubConnector) -> None:
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"required_status_checks": {"contexts": [REQUIRED_CHECK_CONTEXT]}},
            headers={"X-RateLimit-Remaining": "4999"},
        ),
    )
    result = await connector.query(ConnectorQuery(resource="branch_protection", filters={"repo": REPO, "branch": "main"}))

    assert result.metadata["rate_limit"]["X-RateLimit-Remaining"] == "4999"


# ---------------------------------------------------------------------------
# Required-context extraction: tolerant parse + App-pin split.
# ---------------------------------------------------------------------------


def test_required_contexts_ignores_a_non_dict_payload() -> None:
    assert _required_status_check_contexts(None) == ([], [], [])
    assert _required_status_check_contexts("nope") == ([], [], [])


def test_required_contexts_tolerates_bad_container_shapes() -> None:
    # Neither key is a list -> nothing is collected.
    assert _required_status_check_contexts({"contexts": "nope", "checks": "nope"}) == ([], [], [])


def test_required_contexts_skips_non_string_and_non_dict_entries() -> None:
    contexts, checks, satisfiable = _required_status_check_contexts(
        {
            "contexts": [REQUIRED_CHECK_CONTEXT, 7],
            "checks": [
                "not-a-dict",
                7,
                {"context": 5},
                {"context": "ci/test", "app_id": None},
            ],
        },
    )
    assert contexts == [REQUIRED_CHECK_CONTEXT, "ci/test"]
    assert satisfiable == [REQUIRED_CHECK_CONTEXT, "ci/test"]
    assert [entry["context"] for entry in checks] == ["ci/test"]


def test_required_contexts_deduplicates_across_contexts_and_checks() -> None:
    contexts, checks, satisfiable = _required_status_check_contexts(
        {
            "contexts": [REQUIRED_CHECK_CONTEXT, REQUIRED_CHECK_CONTEXT],
            "checks": [
                {"context": REQUIRED_CHECK_CONTEXT, "app_id": None},
                {"context": REQUIRED_CHECK_CONTEXT, "app_id": None},
            ],
        },
    )
    assert contexts == [REQUIRED_CHECK_CONTEXT]
    assert satisfiable == [REQUIRED_CHECK_CONTEXT]
    assert len(checks) == 2


# ---------------------------------------------------------------------------
# Write: publish the verdict as a commit status.
# ---------------------------------------------------------------------------


@respx.mock
async def test_commit_status_publishes_context_state_and_description(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(
        return_value=httpx.Response(201, json={"id": 7, "url": "https://api.github.com/statuses/7"}),
    )
    payload = ConnectorPayload(
        resource="commit_status",
        data={
            "repo": REPO,
            "sha": SHA,
            "state": "failure",
            "description": "Farna Control verdict: 2 evals failed",
        },
    )
    result = await connector.write(payload)

    sent = json.loads(route.calls.last.request.content)
    assert sent["state"] == "failure"
    assert sent["context"] == REQUIRED_CHECK_CONTEXT
    assert sent["description"] == "Farna Control verdict: 2 evals failed"

    assert result["check"]["state"] == "failure"
    assert result["check"]["context"] == REQUIRED_CHECK_CONTEXT
    assert result["check"]["status_id"] == 7
    assert "enforcement" not in result


@respx.mock
async def test_commit_status_publishes_an_optional_target_url(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 8}))
    payload = ConnectorPayload(
        resource="commit_status",
        data={
            "repo": REPO,
            "sha": SHA,
            "state": "success",
            "target_url": "https://control.example/runs/1",
        },
    )
    result = await connector.write(payload)

    sent = json.loads(route.calls.last.request.content)
    assert sent["target_url"] == "https://control.example/runs/1"
    assert result["check"]["target_url"] == "https://control.example/runs/1"


@respx.mock
async def test_commit_status_accepts_a_64_char_sha(connector: GitHubConnector) -> None:
    sha256_status_url = f"https://api.github.com/repos/{REPO}/statuses/{SHA256}"
    route = respx.post(sha256_status_url).mock(return_value=httpx.Response(201, json={"id": 9}))
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": SHA256, "state": "success"},
    )
    result = await connector.write(payload)

    assert route.call_count == 1
    assert result["check"]["sha"] == SHA256


@respx.mock
async def test_commit_status_returns_enforcement_when_branch_supplied(connector: GitHubConnector) -> None:
    respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 11, "url": "https://api.github.com/statuses/11"}))
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"required_status_checks": {"contexts": [REQUIRED_CHECK_CONTEXT]}},
        ),
    )
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": SHA, "state": "success", "branch": "main"},
    )
    result = await connector.write(payload)

    assert result["check"]["status_id"] == 11
    assert result["enforcement"]["enforced"] is True
    assert result["enforcement"]["state"] == "enforced"


@respx.mock
async def test_commit_status_reads_protection_before_publishing(connector: GitHubConnector) -> None:
    # When branch is supplied the read must complete before the publish, so a
    # token that can write but not read fails before any side effect.
    order: list[str] = []

    def _read(_request: httpx.Request) -> httpx.Response:
        order.append("read")
        return httpx.Response(200, json={"required_status_checks": {"contexts": [REQUIRED_CHECK_CONTEXT]}})

    def _publish(_request: httpx.Request) -> httpx.Response:
        order.append("publish")
        return httpx.Response(201, json={"id": 12})

    respx.get(PROTECTION_URL).mock(side_effect=_read)
    respx.post(STATUS_URL).mock(side_effect=_publish)

    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": SHA, "state": "success", "branch": "main"},
    )
    result = await connector.write(payload)

    assert order == ["read", "publish"]
    assert result["check"]["status_id"] == 12


@respx.mock
async def test_commit_status_read_failure_leaves_status_unpublished(connector: GitHubConnector) -> None:
    status_route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 13}))
    respx.get(PROTECTION_URL).mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"}),
    )
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": SHA, "state": "success", "branch": "main"},
    )
    with pytest.raises(GitHubAuthError):
        await connector.write(payload)
    assert status_route.call_count == 0


@respx.mock
async def test_commit_status_accepts_an_explicit_context(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 3}))
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": SHA, "state": "success", "context": "acme/delivery"},
    )
    result = await connector.write(payload)

    sent = json.loads(route.calls.last.request.content)
    assert sent["context"] == "acme/delivery"
    assert result["check"]["context"] == "acme/delivery"


@respx.mock
async def test_commit_status_rejects_a_non_verdict_state_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "pending"})
    with pytest.raises(ValueError, match="'state' must be one of"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_blank_branch_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "branch": ""})
    with pytest.raises(ValueError, match="'branch' must be a non-empty string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_non_string_branch_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "branch": 5})
    with pytest.raises(ValueError, match="'branch' must be a non-empty string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_blank_context_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "context": ""})
    with pytest.raises(ValueError, match="'context' must be a non-empty string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_non_string_context_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "context": 9})
    with pytest.raises(ValueError, match="'context' must be a non-empty string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_non_string_description_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "description": 42})
    with pytest.raises(ValueError, match="'description' must be a string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_non_string_target_url_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": SHA, "state": "success", "target_url": 42})
    with pytest.raises(ValueError, match="'target_url' must be a string"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_traversal_repo_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": "owner/repo/../../other", "sha": SHA, "state": "success"},
    )
    with pytest.raises(ValueError, match="'repo' must be exactly 'owner/name'"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_an_owner_only_repo_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": "owner", "sha": SHA, "state": "success"})
    with pytest.raises(ValueError, match="'repo' must be exactly 'owner/name'"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_repo_with_whitespace_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": "owner /repo", "sha": SHA, "state": "success"})
    with pytest.raises(ValueError, match="'repo' must not contain whitespace"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_traversal_sha_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(
        resource="commit_status",
        data={"repo": REPO, "sha": "../../heads/main", "state": "success"},
    )
    with pytest.raises(ValueError, match="'sha' must be a full 40- or 64-character hex object id"):
        await connector.write(payload)
    assert route.call_count == 0


@respx.mock
async def test_commit_status_rejects_a_short_sha_before_publishing(connector: GitHubConnector) -> None:
    route = respx.post(STATUS_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "sha": "abc123def456", "state": "success"})
    with pytest.raises(ValueError, match="'sha' must be a full 40- or 64-character hex object id"):
        await connector.write(payload)
    assert route.call_count == 0


async def test_commit_status_requires_a_sha(connector: GitHubConnector) -> None:
    payload = ConnectorPayload(resource="commit_status", data={"repo": REPO, "state": "success"})
    with pytest.raises(ValueError, match="requires 'sha' in data"):
        await connector.write(payload)
