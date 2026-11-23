---
id: feat-container-deployment
prd: N/A
adr: [ADR 031 (single-install native distribution)]
code:
  - docker-compose.yml
  - docker-compose.local.yml
  - deploy/compose/docker-compose.prod.yml
  - deploy/compose/docker-compose.test.yml
  - deploy/compose/.env.prod.example
  - deploy/docker/Dockerfile.all-in-one
  - deploy/supervisor/supervisord.conf
  - deploy/supervisor/entrypoint.sh
  - deploy/nginx/all-in-one.conf
  - deploy/caddy/Caddyfile
  - deploy/helm/farna-control/Chart.yaml
  - deploy/helm/farna-control/values.yaml
  - deploy/helm/farna-control/templates/_helpers.tpl
  - deploy/helm/farna-control/templates/backend-deployment.yaml
  - deploy/helm/farna-control/templates/frontend-deployment.yaml
  - deploy/helm/farna-control/templates/saq-runner-deployment.yaml
  - deploy/helm/farna-control/templates/saq-system-deployment.yaml
  - deploy/helm/farna-control/templates/redis-deployment.yaml
  - deploy/helm/farna-control/templates/networkpolicy.yaml
  - deploy/helm/farna-control/templates/hpa.yaml
  - deploy/helm/farna-control/templates/resourcequota.yaml
  - deploy/helm/farna-control/templates/limitrange.yaml
  - deploy/helm/farna-control/templates/ingress.yaml
  - scripts/package_compose_release.py
  - scripts/render_validation_compose.py
  - scripts/smoke-compose.sh
  - scripts/verify_compose_upgrade_rollback.py
  - .github/workflows/publish-images.yml
  - .github/workflows/rc-validate.yml
  - .github/workflows/k8s-conformance.yml
  - docs/deployment.md
  - docs/deployment/k8s-conformance-parity.md
unit-tests:
  - backend/tests/architecture/test_compose_fernet_key_contract.py
  - backend/tests/architecture/test_compose_loopback_ports.py
  - backend/tests/architecture/test_compose_redis_memory_bound.py
  - backend/tests/architecture/test_prod_compose_env_contract.py
  - backend/tests/architecture/test_root_compose_system_url.py
  - backend/tests/architecture/test_dockerfile_docker_extra.py
  - backend/tests/architecture/test_all_in_one_supervisor_workers.py
  - backend/tests/unit/test_docker_compose_wiring.py
  - backend/tests/unit/test_helm_image_tag_defaults.py
  - backend/tests/unit/test_helm_redis_memory_bound.py
  - backend/tests/unit/scripts/test_package_compose_release.py
  - backend/tests/unit/scripts/test_verify_compose_upgrade_rollback.py
  - backend/tests/unit/test_release_workflow_contract.py
  - backend/tests/unit/db/test_alignment_entrypoints.py
  - backend/tests/unit/api/test_robots_txt.py
  - backend/tests/unit/launcher/test_env_contract.py
bdd:
depends-on: []
status: covered
---

# Container Deployment (Docker Compose and Kubernetes)

Farna Control ships two container deployment paths. Docker Compose is the
documented server/production path for a single host: a source-build stack for
local evaluation and a published all-in-one image for production. Kubernetes is
the vendor-neutral, multi-replica path, delivered as a Helm chart. Both paths
share the same application image contracts, the same required secrets, and the
same bounded Redis contract. This is an infra-only surface: it has no manifest
route and no in-product UI, so it is tracked here rather than in the manifest
`features:` registry. The operator-facing contract lives in
`docs/deployment.md`, and the Kubernetes conformance claim discipline lives in
`docs/deployment/k8s-conformance-parity.md`.

## Behaviors

- [x] The source-build Compose stack (`docker-compose.yml`) runs PostgreSQL 16,
      Redis 8, the backend, both SAQ workers (`saq-runner` for the runs queue and
      `saq-system` for the scheduler/cron/polling queue), the frontend, and the
      Gatus watchdog. Every host-published port binds `127.0.0.1`, so a default
      stack is not reachable from a non-loopback interface.
      (`docker-compose.yml`; `test_compose_loopback_ports.py::test_default_compose_ports_are_loopback_only`).
- [x] Every service that builds from `./backend` (and therefore runs the
      credential-widening entrypoint) is given the same `POSTGRES_PASSWORD` as
      the database service, so the `alembic_version` widening step and the
      migrations cannot disagree about the credential.
      (`docker-compose.yml`; `test_docker_compose_wiring.py::test_app_services_declare_postgres_password`).
- [x] The root and local Compose bring-up files refuse to start without a
      required `FERNET_KEY` interpolation whose message tells the operator how to
      generate one. The tests prove a generated value satisfies `Settings` and
      is a real Fernet key, and that the two historical placeholder literals do
      not.
      (`docker-compose.yml`, `docker-compose.local.yml`;
      `test_compose_fernet_key_contract.py::test_every_service_passes_a_required_fernet_key`,
      `::test_the_failure_message_says_how_to_generate_the_key`,
      `::test_the_broken_placeholders_are_not_used_as_a_fernet_key`,
      `::test_a_generated_key_boots_settings_and_is_a_real_fernet_key`).
- [x] Every shipped Compose Redis sets an operator-overridable `--maxmemory`
      bound and a `volatile-*` eviction policy, so a worker wedge cannot grow
      the queue until enqueue writes fail, and eviction only touches TTL-bearing
      job hashes that `dispatcher_reconcile` can re-dispatch.
      (`docker-compose.yml`, `docker-compose.local.yml`,
      `deploy/compose/docker-compose.prod.yml`;
      `test_compose_redis_memory_bound.py::test_shipped_redis_has_a_maxmemory_bound`,
      `::test_shipped_redis_policy_only_evicts_repairable_keys`).
- [x] The source-build stack wires `FARNA_CONTROL_SYSTEM_DATABASE_URL` into the
      backend and the system worker, authenticating as the `farna_control_system`
      (BYPASSRLS) role with the same password as the app role. Without it the
      `dispatcher_reconcile` cron fails closed and `/healthz/ready` never
      reports ready.
      (`docker-compose.yml`; `test_root_compose_system_url.py::test_service_wires_the_system_database_url`).
- [x] The production Compose file pulls the published all-in-one image pinned by
      a required `TAG`, publishes only the application on `${PORT:-80}:80`, and
      keeps every other service loopback-only. It refuses to run until
      `FARNA_CONTROL_DB_PASSWORD`, `SECRET_KEY`, `FERNET_KEY`,
      `SAQ_AUTH_USERNAME` and `SAQ_AUTH_PASSWORD` are set, and it passes the
      project-directory `.env` to the container so optional settings reach both
      the app and the workers.
      (`deploy/compose/docker-compose.prod.yml`;
      `test_compose_loopback_ports.py::test_service_scoped_compose_ports_are_loopback_only`,
      `test_all_in_one_supervisor_workers.py::test_the_compose_wires_the_system_database_url_the_system_worker_requires`).
- [x] Every environment variable the production Compose passes to the
      application is one the application actually reads, every required
      `Settings` field is satisfied, the resolved environment constructs
      `Settings`, and `.env.prod.example` documents the required secrets with no
      silent-default fallbacks.
      (`deploy/compose/docker-compose.prod.yml`, `deploy/compose/.env.prod.example`;
      `test_prod_compose_env_contract.py::test_every_env_var_passed_to_the_app_is_read_by_the_app`,
      `::test_required_settings_fields_are_satisfied_by_the_compose`,
      `::test_compose_environment_constructs_settings`,
      `::test_env_example_documents_the_required_secrets`,
      `::test_no_compose_variable_has_a_silent_default`).
- [x] The all-in-one image is a multi-stage build (Node/pnpm frontend, Python
      runtime) that runs as the unprivileged `www-data` user, grants only
      `cap_net_bind_service` to bind port 80, removes the distribution nginx
      default site and fails the build if any stock site remains, and installs
      the `docker` extra so the worker's `aiodocker` crons import cleanly.
      Its entrypoint runs the shared newer-database guard before `alembic
      upgrade heads`, then starts supervisord.
      (`deploy/docker/Dockerfile.all-in-one`, `deploy/supervisor/entrypoint.sh`;
      `test_dockerfile_docker_extra.py::test_saq_worker_images_install_docker_extra`,
      `test_alignment_entrypoints.py::test_entrypoint_guards_before_running_migrations`).
- [x] The all-in-one image's supervisord runs the API (`uvicorn` on loopback),
      nginx, and BOTH SAQ workers in the one container. The worker commands
      match the root Compose services verbatim, autostart, run in `/app`, and
      log under `/var/log/supervisor`. The production Compose deliberately
      defines no worker service, so nothing is double-run.
      (`deploy/supervisor/supervisord.conf`;
      `test_all_in_one_supervisor_workers.py::test_each_worker_program_matches_the_root_compose_command`,
      `::test_each_worker_program_autostarts_and_logs_where_an_operator_can_read_them`,
      `::test_supervisord_conf_is_actually_shipped_by_the_all_in_one_dockerfile`,
      `::test_the_workers_are_not_a_compose_side_service`).
- [x] The all-in-one nginx serves the SPA with history fallback and a
      no-cache index, proxies `/api/`, `/.well-known/` (OAuth discovery JSON),
      `/mcp/` (buffering off for SSE), `/ws` (upgrade headers), and `/health`
      to the backend, and caches hashed static assets immutably. The
      host-keyed `robots.txt` block disallows crawling for the staging host and
      serves the allow-all file for app/demo hosts.
      (`deploy/nginx/all-in-one.conf`;
      `test_robots_txt.py::test_staging_robots_disallows_crawling`,
      `::test_app_demo_robots_allows_crawling`;
      `.github/workflows/publish-images.yml` runs `nginx -t` against the built
      image).
- [x] `deploy/caddy/Caddyfile` is an optional reference TLS reverse proxy:
      environment-substituted domain and upstreams, automatic HTTPS, security
      headers, JSON access logging, a per-IP rate limit on `/api/*`, and the
      same route split (health/metrics, API, `/.well-known/`, MCP SSE with no
      buffering, WebSocket, SPA fallback). It is a sample config, not part of
      the default stack.
      (`deploy/caddy/Caddyfile`).
- [x] `scripts/package_compose_release.py` builds the downloadable
      `farna-control-<version>-compose.tar.gz` bundle with the production
      Compose hard-pinned to an immutable release tag, a copy named
      `docker-compose.yml`, a working `.env.example`, `init-env.sh`,
      `quickstart.md`, and `SHA256SUMS`. It rejects `latest` and git-SHA tags,
      strips the repository-internal watchdog so the bundle stands alone, ships
      no secret values, and its generated env satisfies the `Settings`
      validators.
      (`scripts/package_compose_release.py`;
      `test_package_compose_release.py::test_validate_release_tag_rejects_latest`,
      `::test_validate_release_tag_rejects_git_sha`,
      `::test_transform_compose_content_hardpins_tag_and_removes_watchdog`,
      `::test_bundled_env_example_declares_every_required_compose_variable`,
      `::test_bundled_env_example_ships_no_secret_values`,
      `::test_bundled_init_env_generates_every_required_variable`,
      `::test_bundled_compose_is_standalone_and_dropped_the_source_build`,
      `::test_bundled_env_values_satisfy_the_settings_validators`).
- [x] The release acceptance matrix proves the operator-visible Compose
      lifecycle: clean install of the old tag, upgrade to the new tag, an
      intentionally broken upgrade (invalid `SECRET_KEY`), rollback to the old
      tag, and rollback readiness. It runs in an isolated Compose project,
      records host/image/command/outcome/container-log evidence, redacts secret
      literals, and tears the project down in a `finally` block.
      (`scripts/verify_compose_upgrade_rollback.py`;
      `test_verify_compose_upgrade_rollback.py::test_build_compose_command_is_project_isolated`,
      `::test_matrix_records_every_required_transition`,
      `::test_evidence_redacts_secret_literals`,
      `::test_failed_upgrade_reads_the_backend_child_log_not_the_container_log`).
- [x] `scripts/smoke-compose.sh` proves a running stack can actually store a
      credential rather than merely answer a health poll: it logs in, asserts
      the readiness `checkpointer` check is `ok`, confirms the checkpoint tables
      exist in Postgres, then creates, reads back, and deletes a model backend.
      (`scripts/smoke-compose.sh`).
- [x] The build/release pipeline publishes the all-in-one image and the split
      backend/frontend images to GHCR, with built-in image smoke checks
      (`import farna`, `nginx -t`); the release workflow supplies the versioned
      tag the Compose bundle pins. The RC gate installs the exact packaged
      bundle anonymously on a clean host, verifies `SHA256SUMS`, rejects a
      bundle that floats on `latest`/`stable`, renders the phase image tag, and
      tears the stack down afterwards.
      (`.github/workflows/publish-images.yml`, `.github/workflows/rc-validate.yml`,
      `scripts/render_validation_compose.py`;
      `test_release_workflow_contract.py::test_rc_validation_runs_the_docker_acceptance_matrix_before_pen_test`,
      `::test_rc_validation_renders_matrix_compose_without_a_concrete_tag`).
- [x] The Helm chart deploys the backend API, both SAQ workers, the frontend
      SPA, and (optionally) an in-cluster Redis. PostgreSQL is always external.
      It refuses to render without `SECRET_KEY` and `FERNET_KEY`, and the
      `SAQ_AUTH_USERNAME`/`SAQ_AUTH_PASSWORD` pair is referenced non-optionally
      by the system worker, so omitting it leaves that worker unable to start
      and readiness permanently failing. Every image tag defaults to the
      chart's own `appVersion` rather than `latest`, with every workload
      passing that app version into the image helper.
      (`deploy/helm/farna-control/Chart.yaml`, `deploy/helm/farna-control/values.yaml`,
      `deploy/helm/farna-control/templates/_helpers.tpl`;
      `test_helm_image_tag_defaults.py::test_default_image_tags_resolve_from_the_chart_not_a_literal`,
      `::test_image_helper_defaults_to_chart_app_version`,
      `::test_workload_templates_pass_app_version_into_the_image_helper`).
- [x] The Helm chart's embedded Redis requires a password (or an existing
      Secret), passes it to `redis-server --requirepass` through a
      shell-expanded environment variable rather than rendering it into the
      command, keeps the `256mb` bound and `volatile-lru` policy, and embeds
      the same credential in `REDIS_URL`. It also ships namespace-scoped
      Pod Security `restricted` enforcement, ResourceQuota, LimitRange,
      optional HPA and Ingress, and an optional workspace egress
      NetworkPolicy that injects the declared-enforcement signal the
      Kubernetes runtime provider requires.
      (`deploy/helm/farna-control/templates/redis-deployment.yaml`,
      `deploy/helm/farna-control/templates/networkpolicy.yaml`,
      `deploy/helm/farna-control/templates/hpa.yaml`,
      `deploy/helm/farna-control/templates/resourcequota.yaml`,
      `deploy/helm/farna-control/templates/limitrange.yaml`,
      `deploy/helm/farna-control/templates/ingress.yaml`;
      `test_helm_redis_memory_bound.py::test_helm_redis_has_a_maxmemory_bound`,
      `::test_helm_embedded_redis_auth_runtime_wiring`,
      `::test_helm_redis_policy_only_evicts_ttl_bearing_keys`).
- [x] The Kubernetes conformance gate lints the Helm chart on every chart
      change and runs the runtime-provider conformance suite on a throwaway
      kind cluster plus a rotating managed cloud (EKS/AKS/GKE). A substrate is
      claimed conformant only while its most recent run is green; chart
      install/upgrade lifecycle and real NetworkPolicy enforcement are
      explicitly not claimed.
      (`.github/workflows/k8s-conformance.yml`,
      `docs/deployment/k8s-conformance-parity.md`).

## Known Gaps

- Docker Compose is documented as the single-server deployment path. Horizontal
  scaling beyond what the all-in-one container and its workers provide is the
  Kubernetes path's job, not Compose's.
- The Caddy configuration is a reference sample. There is no supported
  one-command Compose profile that adds Caddy with automatic certificate
  management; operators wire it themselves.
- The release Compose bundle deliberately removes the repository-internal Gatus
  watchdog (it needs a source-tree build), so a bundle-only install has no
  out-of-process health sentinel unless the operator adds one.
- The Helm chart's install/upgrade lifecycle is linted, not exercised by the
  conformance gate, and the workspace egress NetworkPolicy is only a declared
  posture: actual enforcement depends on the customer's CNI.
- The all-in-one image runs the API, nginx, and both workers in one container.
  Independent scaling of the API and the two worker queues is a Kubernetes
  concern; the single-container Compose artifact does not offer it.

## QA History

- 2026-11-22: Added this tracker after walking the product map and finding the
  shipped container deployment surface had no entry in either product-map
  layer, even though Docker Compose is the documented production path, the
  all-in-one image and release bundle ship, and a full Helm chart backs the
  Kubernetes path. Verified each behavior against the Compose files, the
  all-in-one Dockerfile/supervisord/nginx config, the Caddy sample, the release
  packaging and acceptance scripts, the Helm chart, and the architecture/unit
  tests that pin them. No product code changed in this pass.
