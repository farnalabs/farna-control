# Provider contract probe

A nightly, read-only check that the third-party APIs Modulo depends on still
return the response **shape** our code expects. Mocks and cassettes prove our
code against a frozen assumption; nothing in the test suite notices when a
provider changes. This probe does.

- Script: `scripts/provider_contract_probe.py`
- Config: `configs/provider-contract-probes.yaml`
- Goldens: `contract-goldens/provider-shapes.json`
- Workflow: `.github/workflows/provider-contract-probe.yml`
- Drift report: `contract-goldens/report.json` (generated, gitignored; uploaded
  as the `provider-contract-report` CI artifact on every run)

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

| Drift kind     | Meaning                                                     | Fails the job? |
| -------------- | ----------------------------------------------------------- | -------------- |
| `missing`      | A pinned key is absent                                      | Yes            |
| `type_changed` | A pinned key changed value type                             | Yes            |
| `emptied`      | A pinned array is now empty                                 | Yes            |
| `populated`    | A previously empty array now has items                      | Yes            |
| `added`        | The provider added an unpinned object key                   | Yes by default |

`added` is breaking because `strict_shape` defaults to `true`: a provider
adding an unexpected top-level field can change how our parser behaves. Set
`strict_shape: false` on a probe (or in `defaults`) to report additions in the
JSON report's `additions` list without failing. This is the documented escape
hatch when a provider routinely adds fields.

Arrays are compared as a **union of every element** (not just `value[0]`), so a
polymorphic listing is described in full. A key present in only some elements is
marked optional and its later absence is not drift. An array that goes from
having items to `[]` (or the reverse) is always breaking drift.

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

# explicitly (re)record ONE golden - the ONLY path that writes a golden
python scripts/provider_contract_probe.py --mode record --provider github --operation rate_limit
```

Exit codes: `0` clean (or report-only / dry-run), `1` drift, a probe error, an
unavailable provider, an unexcused skip, or **zero attempted probes**, `2` config
error / usage error.

Useful flags: `--config PATH`, `--goldens PATH`, `--report PATH`,
`--provider ID` (restrict a check to one provider).

## A red nightly is the honest state

The committed goldens are hand-authored from each provider's documented
response contract and **none has been recorded against the live provider yet**
(`recorded_at` is `null`). No provider is excused, so `--mode check` fails: every
probe is an unexcused "not yet recorded" skip and the run exits `1`.

That is deliberate. The nightly is **expected to be red** until each golden is
recorded live behind review (see below). A green nightly in this state would
mean the job was proving nothing.

The run counts **attempts**, not passes: if it issues no request at all it exits
non-zero, even when every skip is explicitly excused. This is what stops the
bootstrap shape (all skips allow-listed, `passed: 0`) from reading as clean.

## Fail-closed skips

A probe is never silently skipped. Each of these is a **failure** unless the
provider id appears, with a reason, in the matching top-level allow list in the
config; an allow-listed skip is reported as `NOTE` in the log and `allowed:
true` in the report, so it is always visible:

| Condition                                | Allow list                  |
| ---------------------------------------- | --------------------------- |
| No credential in the environment         | `allow_missing_credential`  |
| No golden entry for the probe            | `allow_missing_golden`      |
| Golden exists but `recorded_at` is null  | `allow_unrecorded_golden`   |

All three lists are **empty** in the committed config. This catches a rotated
secret (a recorded provider whose token disappeared) and a newly added provider
nobody recorded. Recording a golden (below) sets its `recorded_at`, after which
it is genuinely probed against the live provider.

Add an allow-list entry **only** as a deliberate, reviewed, time-boxed
exception, with a reason. Do not re-add every provider to
`allow_unrecorded_golden`: that is the state that made the nightly green while
probing nothing.

Even with excused skips, a run that attempts **zero** probes fails.

## Read-only and credential confinement

The config names the URL, method and env var names, so the loader enforces the
read-only guarantee before any request. Two things are pinned **in code**, so a
config edit alone cannot widen them:

- **Host pin.** `PROVIDER_HOST_PINS` in the script maps every provider id to its
  one permitted host. A probe whose id is not in that mapping is rejected, and a
  URL whose host differs from the pin is rejected. `allowed_host` in the config
  must agree with the pin, but the pin is the authority: changing the URL and
  `allowed_host` together is still rejected, so a config edit cannot redirect a
  credential to a new host.
- **POST endpoint pin.** Only a handful of providers use a POST, and each is a
  read query. `READ_ONLY_POST_ENDPOINTS` in the script pins every one of them to
  its exact URL and exact body (`dropbox_paper`, `linear`, `monday`, `slack`). A
  POST whose id, URL or body differs is rejected - renaming a probe onto a write
  endpoint such as Slack `chat.delete` fails at load.

Also enforced:

- The URL must be `https`, must not embed userinfo, and must not carry a query
  string (use `auth.params`).
- Only `GET` and `HEAD` are accepted, except the pinned POSTs above.
- `auth.env`, every `auth.params` value and every `extra_headers` value must
  appear in the probe's `env_vars`. A config edit cannot pull in an undeclared
  secret.
- `auth.template` may reference only `{token}`; header names are validated.

## Transient failures, timeouts and deadlines

A network error, `429` or any `5xx` is retried (default: 3 attempts) with
exponential backoff and jitter, honouring `Retry-After` on `429`. After the
attempts are exhausted the probe is reported as **`provider_unavailable`**, not
contract drift - one transient failure can never be recorded as a shape change.
Tune `defaults.retry` (`attempts`, `base_delay_seconds`, `max_delay_seconds`).

Each request uses the configured timeout (`defaults.timeout_seconds`, or a
per-probe `timeout_seconds`). The whole run also has an overall wall-clock
deadline (`defaults.deadline_seconds`, default 1500s / 25 min); probes past it
are reported and the run ends. The JSON report is written **after every probe**,
so a killed run still leaves a usable report. The workflow's step budget exceeds
the in-script deadline.

## Adding a provider

1. Decide whether it can be probed safely and cheaply. If not, add a **skip**
   entry with a reason (see below) - every connector type and model backend must
   appear in the config as either a probe or a skip.
2. Add the provider id to `PROVIDER_HOST_PINS` in
   `scripts/provider_contract_probe.py`, mapped to its one permitted host. The
   loader rejects a probe whose id is unpinned, so this code change is the only
   way to introduce a host.
3. Add a probe under `providers:`:

   ```yaml
   - id: example
     kind: connector            # or model_backend
     operation: users_me        # unique within the provider
     description: Current user identity (Example GET /users/me).
     request:
       method: GET              # GET/HEAD, or a pinned read-only POST
       url: https://api.example.com/users/me
     allowed_host: api.example.com   # must equal the code pin
     auth:
       kind: bearer             # none | bearer | header | query
       env: MODULO_PROBE_EXAMPLE_TOKEN
     env_vars:                  # the ONLY env vars this probe may read
       - MODULO_PROBE_EXAMPLE_TOKEN
     max_depth: 1               # how many shape levels to pin
     normalise:                 # volatile handling lives HERE, not in code
       - path: data.id
         rule: volatile
   ```

   If the cheapest read is an HTTP POST, add its id, exact URL and exact body to
   `READ_ONLY_POST_ENDPOINTS` in the same script first; a POST that does not
   match a pin is rejected.

4. Add the credential as a repo secret named exactly as the config's `env`
   (or `params` / `extra_headers` env names), and pass it through in the
   workflow's `Probe live provider contracts` step.
5. Record the golden behind review (below) and commit it. The nightly stays red
   until it is recorded - that red is the correct signal.

### Request bodies

`request` may carry a body:

- `json: <value>` - JSON body. `json: null` sends a literal `null` body (needed
  by Dropbox no-argument RPCs).
- `form: {key: value}` - `application/x-www-form-urlencoded` body.

At most one of `json` / `form` may be set. For a POST the body must match the
pinned `READ_ONLY_POST_ENDPOINTS` entry exactly (kind and value).

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
| `volatile` | Replace the value with `<volatile>` (stabilises ids, timestamps, etags - even across scalar types) |
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
   only the intended shape change. Record mode rewrites the file in the same
   canonical, compact inline-leaf format the repo commits, so re-recording one
   provider changes one block - nothing else reflows.
4. Commit the golden with the change that depends on it. Its `recorded_at` is
   now set, so it is genuinely probed and is no longer an "unrecorded" skip.

If a probe has no golden, check mode **fails** it by default (allow-list it
explicitly if that is intended, with a reason). Check mode never creates a
golden. Recording the first golden is what turns the nightly from honestly red
into a real check for that provider.

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

## Credential inventory (names only - values live in repo secrets)

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

`pypi` is public and needs no credential. A missing credential on a probe that
**should** run fails the run unless the provider is explicitly allow-listed.

## Drift report

`contract-goldens/report.json` is written after every probe (override with
`--report`). It contains, per probe: `status` (`pass` / `drift` / `skipped` /
`unavailable` / `error`), `status_code`, `attempts`, the breaking `drift` list,
and the `additions` list, plus a summary (`attempted`, `expected`, `passed`,
`drifted`, `unavailable`, `skipped`, `skipped_unexcused`, `errors`, `additions`)
and the config's skip list. `attempted` counts probes that actually issued a
request; zero in check mode is always a non-zero exit. It never contains a
credential, request header, or response
value. In CI it is uploaded as the `provider-contract-report` artifact on every
run, including failures.

## Workflow and credentials in CI

The nightly job checks out `main` and installs the backend with `uv sync
--frozen`. `GITHUB_TOKEN` is scoped to **that install step only** (it
authenticates the git fetches `uv sync` makes for git-pinned dependencies), so
no later step or third-party action can read it; it is not in workflow-level
`env`. Provider credentials are passed only to the probe step.

## Tests

`backend/tests/unit/tools/test_provider_contract_probe.py` uses respx and a temp
directory. It proves, among other things: a mutated golden fails; a changed
shape is reported and not auto-accepted; a secret never appears in output;
missing credential / missing golden / unrecorded golden each fail by default and
pass only via an allow list; a run that attempts **zero** probes fails even when
every skip is excused; `429` is retried honouring `Retry-After` and a
persistent `5xx` ends as `provider_unavailable` (not drift); an emptied or
populated array is breaking and array shapes are unioned; `added` is breaking by
default; the loader rejects a POST that is not pinned in code (including a
probe renamed onto Slack `chat.delete`), a pinned POST whose body differs, a URL
host that differs from the code pin, a config that changes the URL and
`allowed_host` together to a new host, an unpinned provider id, embedded URL
credentials and an undeclared header env var; one probe's exception does not
abort the run and still writes the report; record mode rewrites only the
selected provider in the committed format.

Two further tests load the real artefacts: `configs/provider-contract-probes.yaml`
and `contract-goldens/provider-shapes.json` (unique ids, valid status classes,
normalisation paths resolve against the recorded shape, every provider has a
golden), and a registry-coverage test enumerates
`backend/src/farna/connectors/*` and `backend/src/farna/model_backends/*` and
fails if a package is in neither `providers` nor `skips`.
