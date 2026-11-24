# Provider contract probe

A nightly, read-only check that the third-party APIs Modulo depends on still
return the response **shape** our code expects. Mocks and cassettes prove our
code against a frozen assumption; nothing in the test suite notices when a
provider changes. This probe does.

- Script: `scripts/provider_contract_probe.py`
- Config: `configs/provider-contract-probes.yaml`
- Goldens: `contract-goldens/provider-shapes.json`
- Workflow: `.github/workflows/provider-contract-probe.yml`
- Drift report: `contract-goldens/report.json` (generated; uploaded as the
  `provider-contract-report` CI artifact on every run)

The probe calls the same cheap read-only endpoint each connector's own health
check (or cheapest read) uses. It deliberately does **not** instantiate the
connector: connectors have heterogeneous, per-tenant constructors, and
instantiating them would hide the raw wire shape we want to pin. The contract
boundary is the provider's HTTP response.

## What "shape" means

A golden stores a **shape tree**, never response values:

```json
{"type": "object", "keys": {"ok": {"type": "boolean"}, "team": {"type": "string"}}}
```

A check extracts the same tree from the live response and diffs it:

| Drift kind     | Meaning                                   | Fails the job? |
| -------------- | ----------------------------------------- | -------------- |
| `missing`      | A pinned key is absent                    | Yes            |
| `type_changed` | A pinned key changed value type           | Yes            |
| `added`        | The provider added an unpinned key        | No (reported)  |

Additive changes are reported in the JSON report (`additions`) but do not fail,
so a provider adding a field never wakes anyone up. Set `strict_shape: true` on
a probe to make additions fail too.

Because goldens hold key names and type names only, **a secret or customer value
can never reach a golden**. Request and response headers are never persisted,
and any credential value (plus URL userinfo) is scrubbed from every error string
before it is written or printed.

## Modes

```bash
# default: call every probe, diff vs golden, exit non-zero on drift
python scripts/provider_contract_probe.py --mode check

# print planned calls; no network, no writes
python scripts/provider_contract_probe.py --dry-run

# write the report but never exit non-zero on drift
python scripts/provider_contract_probe.py --report-only

# explicitly (re)record ONE golden — the ONLY path that writes a golden
python scripts/provider_contract_probe.py --mode record --provider github --operation rate_limit
```

Exit codes: `0` clean (or report-only / dry-run), `1` drift or probe error,
`2` config error / usage error.

Useful flags: `--config PATH`, `--goldens PATH`, `--report PATH`,
`--provider ID` (restrict a check to one provider).

## Adding a provider

1. Decide whether it can be probed safely and cheaply. If not, add a **skip**
   entry with a reason (see below) — every connector type and model backend must
   appear in the config as either a probe or a skip.
2. Add a probe under `providers:`:

   ```yaml
   - id: example
     kind: connector            # or model_backend
     operation: users_me        # unique within the provider
     description: Current user identity (Example GET /users/me).
     request:
       method: GET
       url: https://api.example.com/users/me
     auth:
       kind: bearer             # none | bearer | header | query
       env: MODULO_PROBE_EXAMPLE_TOKEN
     max_depth: 1               # how many shape levels to pin
     normalise:                 # volatile handling lives HERE, not in code
       - path: data.id
         rule: volatile
   ```

3. Add the credential as a repo secret named exactly as the config's `env`
   (or `params` / `extra_headers` env names), and pass it through in the
   workflow's `Probe live provider contracts` step.
4. Record the golden behind review (below), then commit it.

### Credential kinds

| `auth.kind` | Wire form                                                       |
| ----------- | --------------------------------------------------------------- |
| `none`      | No credential (public endpoint)                                  |
| `bearer`    | `Authorization: Bearer <value>`                                  |
| `header`    | `<header>: <template>` where `{token}` is replaced (`template` optional; default header `Authorization`) |
| `query`     | Each `params` entry appended as a query parameter                |

Multi-header auth (e.g. Datadog) uses `extra_headers:` (header name → **env var
name**). Literal, non-secret headers go in `static_headers:`.

### Normalisation rules

Declared per probe in the config, never in code:

| Rule       | Effect                                                              |
| ---------- | ------------------------------------------------------------------- |
| `volatile` | Replace the value with `<volatile>` (stabilises ids, timestamps, etags — even across scalar types) |
| `drop`     | Remove the path before shaping (optional or unbounded subtrees)     |
| `nullable` | Treat `null` as the string `<null>` (a legitimately null field is not a type change) |

Paths are dot-separated. `*` matches every key of a dict at that level and `[]`
matches every element of a list, e.g. `data.[].id` or `resources.*.reset`.

## Re-recording a golden (explicit review step)

Recording is **never** automatic and is the only code path that writes a
golden. When a provider legitimately changes shape:

1. Reproduce the change and decide it is acceptable (read the live payload; do
   not blindly accept).
2. Record with a credential for that provider only:

   ```bash
   MODULO_PROBE_GITHUB_TOKEN=... \
     python scripts/provider_contract_probe.py --mode record --provider github --operation rate_limit
   ```

3. Review the `contract-goldens/provider-shapes.json` diff: it must contain
   only the intended shape change. A new or removed key is the whole point of
   the review.
4. Commit the golden with the change that depends on it.

If a probe has no golden, check mode **skips it loudly** (a warning on stderr
and a `skipped` row in the report). It never creates a golden.

## Skip list and why

Every skip is explicit in `configs/provider-contract-probes.yaml` with a reason.
There are three classes: no safe default host (self-hosted / per-tenant), no
safe read-only endpoint, and auth that needs a token exchange outside plain
HTTP.

### Connectors (19 skipped)

| Provider | Reason |
| --- | --- |
| `azure_key_vault` | Tenant-specific vault host required; no safe default endpoint. |
| `azure_pipelines` | Organisation-specific `dev.azure.com/<org>` host required. |
| `azure_repos` | Organisation-specific `dev.azure.com/<org>` host required. |
| `ci_runner` | Internal abstraction over multiple CI providers; no single upstream contract. |
| `confluence` | Site-specific Atlassian host required. |
| `filesystem` | Local filesystem connector; no network provider. |
| `gitea` | Self-hosted host required. |
| `grafana` | Self-hosted host required. |
| `jenkins` | Self-hosted host required. |
| `jira` | Site-specific Atlassian host required. |
| `n8n` | Self-hosted host required. |
| `onepassword` | 1Password Connect server host and token required. |
| `plane` | Instance-specific host required. |
| `rest` | Generic HTTP connector; target chosen per use, no fixed provider. |
| `shell` | Local command connector; no network provider. |
| `sharepoint` | Site-scoped Graph; identity already covered by `microsoft_teams`. |
| `teamcity` | Self-hosted host required. |
| `ticket_tracker` | Internal abstraction; Linear probed directly, Jira skipped. |
| `youtrack` | Instance-specific host required. |

### Model backends (15 skipped)

| Provider | Reason |
| --- | --- |
| `ai21` | No documented read-only identity/model endpoint. |
| `azure_openai` | Resource host + deployment name required. |
| `bedrock` | AWS SigV4 signing required (outside plain-HTTP scope). |
| `jan` | Local runtime; no hosted contract. |
| `llamacpp` | Local runtime; no hosted contract. |
| `lm_studio` | Local runtime; no hosted contract. |
| `localai` | Local runtime; no hosted contract. |
| `ollama` | Local runtime; no hosted contract. |
| `opencode` | Local CLI agent; no HTTP contract. |
| `perplexity` | No documented read-only endpoint. |
| `stub` | Test double; no upstream provider. |
| `tgi` | Self-hosted host required. |
| `vertexai` | GCP OAuth token minting required (outside plain-HTTP scope). |
| `vllm` | Self-hosted host required. |
| `watsonx` | IBM IAM token exchange required (outside plain-HTTP scope). |

## Credential inventory (names only — values live in repo secrets)

Connectors:

`MODULO_PROBE_ASANA_TOKEN`, `MODULO_PROBE_BITBUCKET_TOKEN`,
`MODULO_PROBE_BUILDKITE_TOKEN`, `MODULO_PROBE_CIRCLECI_TOKEN`,
`MODULO_PROBE_CODECLIMATE_TOKEN`, `MODULO_PROBE_DATADOG_API_KEY`,
`MODULO_PROBE_DATADOG_APP_KEY`, `MODULO_PROBE_DISCORD_TOKEN`,
`MODULO_PROBE_DROPBOX_TOKEN`, `MODULO_PROBE_GITHUB_TOKEN`,
`MODULO_PROBE_GITLAB_TOKEN`, `MODULO_PROBE_LINEAR_TOKEN`,
`MODULO_PROBE_MICROSOFT_TEAMS_TOKEN`, `MODULO_PROBE_MONDAY_TOKEN`,
`MODULO_PROBE_NOTION_TOKEN`, `MODULO_PROBE_NPM_TOKEN`,
`MODULO_PROBE_OPSGENIE_TOKEN`, `MODULO_PROBE_PAGERDUTY_TOKEN`,
`MODULO_PROBE_SENTRY_TOKEN`, `MODULO_PROBE_SHORTCUT_TOKEN`,
`MODULO_PROBE_SLACK_TOKEN`, `MODULO_PROBE_SNYK_TOKEN`,
`MODULO_PROBE_SONARQUBE_TOKEN`, `MODULO_PROBE_TRELLO_KEY`,
`MODULO_PROBE_TRELLO_TOKEN`.

Model backends:

`MODULO_PROBE_ANTHROPIC_TOKEN`, `MODULO_PROBE_COHERE_TOKEN`,
`MODULO_PROBE_DEEPSEEK_TOKEN`, `MODULO_PROBE_FIREWORKS_TOKEN`,
`MODULO_PROBE_GEMINI_TOKEN`, `MODULO_PROBE_GROK_TOKEN`,
`MODULO_PROBE_GROQ_TOKEN`, `MODULO_PROBE_MISTRAL_TOKEN`,
`MODULO_PROBE_OPENAI_TOKEN`, `MODULO_PROBE_OPENROUTER_TOKEN`,
`MODULO_PROBE_QWEN_TOKEN`, `MODULO_PROBE_TOGETHERAI_TOKEN`.

`pypi` is public and needs no credential. A missing credential skips that probe
with a warning; it does not fail the run and it does not count as a pass.

## Drift report

`contract-goldens/report.json` is written on every check (override with
`--report`). It contains, per probe: `status` (`pass` / `drift` / `skipped` /
`error`), `status_code`, the breaking `drift` list, and the `additions` list,
plus a summary and the config's skip list. It never contains a credential,
request header, or response value. In CI it is uploaded as the
`provider-contract-report` artifact on every run, including failures.

## Tests

`backend/tests/unit/tools/test_provider_contract_probe.py` uses respx and a
temp directory. It proves: a mutated golden fails; a changed shape is reported
and not auto-accepted; a secret never appears in output; a missing credential is
skipped loudly with no request; record mode writes a golden while check mode is
read-only; and a normalisation rule absorbs a volatile field.
