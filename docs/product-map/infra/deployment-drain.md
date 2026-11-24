---
id: feat-deployment-drain
prd: N/A
adr: []
code:
  - backend/src/farna/api/routes/deployment.py
  - backend/src/farna/db/crud/run.py
  - backend/src/farna/db/crud/_gate_advisory_lock.py
  - backend/src/farna/db/models/system_config.py
  - backend/src/farna/core/system_audit_logger.py
  - backend/src/farna/core/exceptions.py
  - backend/src/farna/api/db_error_handling.py
  - backend/src/farna/api/main.py
  - .github/workflows/deploy.yml
unit-tests:
  - backend/tests/unit/api/test_deployment_endpoint.py
  - backend/tests/unit/db/test_journey_create_run.py
  - backend/tests/integration/test_runner_capacity_gate.py
bdd: []
depends-on: [feat-container-deployment, feat-runs, feat-core-runtime-provider-core]
status: covered
---

# Deployment Drain

The operational control plane a deploy pipeline uses to freeze new sandbox work
before replacing a running instance. Two REST endpoints ship under
`/api/v1/deployment`: an unauthenticated, public-safe build/runtime readout
that the CI/CD pipelines poll to confirm which build is live, and a
token-guarded drain toggle that writes an instance-wide admission flag read by
every run-creation path. A composed deploy enters the drain, waits for the live
sandbox population to converge, replaces the instance, then clears the drain
(`.github/workflows/deploy.yml`). The surface ships REST only, with no manifest
route, no admin screen and no MCP tool, so it is tracked here rather than in the
manifest `features:` registry.

## Behaviors

- [x] `GET /api/v1/deployment` is deliberately UNAUTHENTICATED so the deploy
      pipelines (`deploy.yml`, `rc-validate.yml`, `deploy-watchdog.yml`,
      `deploy-staleness-check.yml`) can verify the live build with no
      principal, and a request carrying no `Authorization` header answers
      `200` (`api/routes/deployment.py`;
      `test_deployment_endpoint.py::TestDeploymentInfo::test_unauthenticated_reachable_returns_200`).
- [x] The readout returns `version`, `uptime_seconds`, `started_at` (tz-aware
      ISO-8601 UTC), `python_version`, `environment` (defaulting to
      `development`), `git_sha`, `git_commit_timestamp`, `build_timestamp`,
      `active_sandbox_runs` and `deployment_drain`. Absent build identifiers
      fall back to empty strings so the endpoint is always safe to call
      (`test_response_has_required_fields`,
      `test_environment_defaults_to_development`,
      `test_build_metadata_falls_back_to_empty`,
      `test_started_at_is_valid_iso_datetime`).
- [x] The response is public-safe by construction: it omits `hostname` and
      `ci_job_url` and the deployment source repository's free text
      (`git_branch`, `git_commit_message`), because a self-hosted operator may
      build from a private fork and no consumer reads them (FAR-1975).
      `test_sensitive_fields_absent`, `test_source_repo_free_text_absent`,
      `test_no_tenant_or_credential_fields`.
- [x] `active_sandbox_runs` is the instance-wide deploy-drain population: every
      `running` run with a live `sandbox_dispatch_state` marker, excluding
      `cleared_at_hitl` tombstones and carrying no tenant filter. It is the same
      live-marker population the per-org capacity gate uses, so capacity
      accounting and the drain wait cannot disagree
      (`db/crud/run.py::count_active_runner_dispatches_for_deployment`;
      `test_active_sandbox_runs_is_reported`,
      `test_active_sandbox_query_excludes_non_running_runs`;
      `test_runner_capacity_gate.py` F6).
- [x] `POST /api/v1/deployment/drain?active=<bool>` is guarded by a bearer
      credential compared in constant time (`hmac.compare_digest`) against
      `FARNA_CONTROL_DEPLOY_DRAIN_TOKEN`. The route answers `503` when the
      environment variable is unset or the header is not a bearer token, `403`
      when the supplied token is wrong, and `200 {"active": <bool>}` when it is
      accepted (`_require_drain_token`;
      `test_missing_or_invalid_token_is_rejected`,
      `test_unconfigured_token_is_rejected`,
      `test_valid_token_persists_value_and_audits`).
- [x] A toggle takes the transaction-scoped Postgres advisory lock
      `deployment_drain`, writes the `system_config` key `deployment_drain` to
      `{active, updated_at, source: "deploy-workflow"}` and appends the system
      audit event `deployment_drain_changed` (resource `deployment`) in the same
      transaction (`_set_drain`, `db/crud/_gate_advisory_lock.py`;
      `test_set_drain_writes_config_and_audit`). The lock is a no-op off
      Postgres, which is why the sqlite-backed tests exercise the write without
      it.
- [x] Run admission enforces the flag. `create_run` calls
      `_enforce_deployment_drain_gate` after the org-deleted, capacity and
      pipeline-state gates; the gate acquires the SAME advisory lock BEFORE
      reading the flag, so a drain activation cannot commit between the
      admission decision and the run insert. Only an exact `active is True`
      refuses anything (`db/crud/run.py`;
      `test_journey_create_run.py::TestDeploymentDrainAdmission`).
- [x] The drain blocks new sandbox pipelines, not every run: it raises
      `TriggersPausedError` only when the run's frozen snapshot graph contains a
      top-level `sandbox_agent` node, and admits non-sandbox graphs during an
      active drain. Composite snapshots are expanded at creation time, so a
      sandbox sub-node of a composite appears at the top level and is found
      (`_graph_contains_sandbox_agent`; `test_active_drain_refuses_sandbox_graph`,
      `test_active_drain_admits_non_sandbox_graph`).
- [x] The admission read fails CLOSED: a `SQLAlchemyError` reading the drain
      flag propagates rather than silently admitting a sandbox run while a
      drain may be active (`test_drain_flag_read_failure_propagates`).
- [x] The refusal is mapped to a typed outcome by the origins that catch it:
      the cron, polling and agent-signal fire jobs return a typed
      `{status: skipped}` result (`core/cron_helpers.py`,
      `core/trigger_engine/agent_signal.py`), and the webhook and Slack routes
      return their own paused payload (`api/routes/webhooks.py`,
      `api/routes/slack.py`). The test-trigger route, the manual run and rerun
      REST routes, and the MCP `trigger_pipeline` tool do NOT map it: each has
      a generic `except Exception` arm (or generic tool error arm) that converts
      the refusal to `500`/`server_error` before `handle_db_errors`'
      `TriggersPausedError` arm can see it (see Known Gaps).
- [x] The production deploy composes the control: it enters the drain before
      pre-deploy migrations, waits for `active_sandbox_runs` to converge, and
      clears the drain after the deploy; a genuine control failure refuses the
      deploy, while the first bootstrap deploy (no token or no live route)
      degrades to the legacy active-run wait gate with a loud warning
      (`.github/workflows/deploy.yml`).

## Known Gaps

- **No manifest route, admin UI or MCP tool.** The drain is a `curl`-only
  operational control consumed by the deploy workflow, so an operator can only
  inspect or toggle it with the bearer token and the endpoint.
- **The drain does not stop running work.** It only refuses new sandbox
  pipeline runs. Already-running sandbox runs keep going, and the workflow
  separately waits for the live population to drain (bounded by the deploy
  step's own timeout). There is no cancel-on-drain.
- **The refusal reuses `TriggersPausedError`.** The drain raises the same
  exception as the org-wide pause kill-switch (with no context arguments), so
  the mapped `409` reads "triggers are paused for organisation None" and does
  not name the deployment drain. A caller cannot tell the two refusals apart
  from the mapped response alone.
- **Several origins answer `500`/`server_error` rather than a typed drain
  refusal.** Three origins do not catch `TriggersPausedError`, so a drain
  refusal falls into a generic arm instead of a typed response:
  - the manual `trigger_run` and `trigger_rerun` REST routes
    (`api/routes/runs.py`) have bespoke `except` chains that map
    `OrgDeletedError`, `PipelineNotRunnableError` and the rate-limit/lock errors
    but not the pause refusal, so it reaches the generic `except Exception` and
    returns `500`;
  - the test-trigger route (`test_trigger`, `api/routes/triggers.py`) has the
    same shape: its in-body generic `except Exception` converts the refusal to
    `500` before the `@handle_db_errors` `TriggersPausedError` arm (which would
    return `409`) ever sees it;
  - the MCP `trigger_pipeline` tool (`api/mcp_server.py`) falls into its generic
    `except Exception` and returns a `server_error`, not a typed drain refusal.

  The cron, polling and agent-signal fire jobs (typed skip) and the webhook and
  Slack routes (paused payload) do map it correctly.
- **The sandbox-node scan is top-level and fail-open.** `_graph_contains_sandbox_agent`
  returns `False` for a non-dict graph or a missing `nodes` list, so a malformed
  snapshot would be admitted during a drain. Normal creation always expands a
  well-formed graph, so this is a defensive-only hole.
- **Instance-wide, not per-organisation.** The flag lives in `system_config`
  and freezes every org's sandbox admission at once; there is no per-org or
  per-team drain scope and no scheduled/automatic drain window.

## QA History

- 2026-11-24: **Improve Architecture product-map walk** - entry added for a
  shipped but untracked operational surface. The public readout, the
  bearer-token drain toggle, the advisory-lock plus audit write, the
  `sandbox_agent`-scoped admission gate (and its fail-closed flag read), and the
  deploy-workflow composition were verified against `api/routes/deployment.py`,
  `db/crud/run.py`, `db/crud/_gate_advisory_lock.py`,
  `.github/workflows/deploy.yml` and the cited unit/integration suites. The
  per-origin refusal mapping was checked across `api/routes/runs.py`,
  `api/routes/triggers.py`, `api/routes/webhooks.py`, `api/routes/slack.py`,
  `core/cron_helpers.py` and `core/trigger_engine/agent_signal.py`; the
  test-trigger, manual run/rerun and MCP `trigger_pipeline` `500`/`server_error`
  refusals are recorded as a gap rather than fixed in this docs-only walk.
  Status: covered, with the CI-only control, no cancel-on-drain,
  `TriggersPausedError` reuse, test-trigger/manual-run/MCP `500`/`server_error`,
  fail-open malformed-graph scan and instance-wide scope recorded as honest
  gaps.
