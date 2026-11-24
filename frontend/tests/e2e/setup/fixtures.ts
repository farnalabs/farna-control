import { spawn, type ChildProcess } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { test as base, expect, type Page } from '@playwright/test'
import yaml from 'js-yaml'
import { startCoverage, stopCoverage } from './coverage'
import { getLocalE2eBaseUrl, getTestEnv, type TestEnv } from './env'
import { loginThroughUi } from './login'
import { installEntitlementNavigationGuard } from './target-tier'

type SsoBackendProcess = { stop: () => Promise<void> }

async function waitForPort(port: number, child: ChildProcess): Promise<void> {
  const deadline = Date.now() + 15_000
  let lastProbeError: unknown
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`[sso-fixture] backend exited before readiness (code ${child.exitCode})`)
    try {
      const response = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(500) })
      if (response.status < 500) return
    } catch (err) {
      // The backend is still booting; the deadline bounds this polling loop.
      // Retain the probe error so a readiness timeout reports the real cause.
      lastProbeError = err
    }
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(
    `[sso-fixture] backend did not become ready on port ${port} within 15000ms (last probe error: ${String(lastProbeError)})`,
    { cause: lastProbeError },
  )
}

async function startLocalSsoBackend(): Promise<SsoBackendProcess> {
  const port = Number.parseInt(process.env.E2E_SSO_BACKEND_PORT ?? '8000', 10)
  const child = spawn('uv', [
    'run', '--project', '.', '--no-sync', 'python', 'tests/e2e/sso_fixture.py',
    '--port', String(port), '--frontend-url', getLocalE2eBaseUrl(),
  ], {
    cwd: resolve(process.cwd(), '..', 'backend'),
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      ...process.env,
      DATABASE_URL: 'sqlite+aiosqlite:///./sso-e2e-fixture.db',
      SECRET_KEY: 'sso-e2e-secret-key-012345678901234567890123',
      FERNET_KEY: 'sso-e2e-fernet-key-012345678901234567890123',
      FARNA_CONTROL_PUBLIC_URL: `http://127.0.0.1:${port}`,
      FARNA_CONTROL_FRONTEND_URL: getLocalE2eBaseUrl(),
      FARNA_CONTROL_MULTI_ORG_ENABLED: 'false',
      FARNA_CONTROL_AUTH_RATE_LIMIT_ENABLED: 'false',
      REDIS_URL: 'redis://127.0.0.1:0/0',
      FARNA_CONTROL_DB: 'sqlite',
    },
  })
  child.stdout?.on('data', (chunk) => process.stderr.write(`[sso-fixture] ${chunk}`))
  child.stderr?.on('data', (chunk) => process.stderr.write(`[sso-fixture] ${chunk}`))
  await waitForPort(port, child)
  return {
    stop: async () => {
      if (child.exitCode !== null) return
      child.kill('SIGTERM')
      await new Promise<void>((resolve) => {
        const timer = setTimeout(() => { child.kill('SIGKILL'); resolve() }, 3000)
        child.once('exit', () => { clearTimeout(timer); resolve() })
      })
    },
  }
}

export const test = base.extend<{ env: TestEnv; localSsoBackend: SsoBackendProcess }>({
  env: async ({}, use) => {
    await use(getTestEnv())
  },
  localSsoBackend: async ({ env }, use) => {
    if (env.name !== 'local') return await use({ stop: async () => {} })
    const backend = await startLocalSsoBackend()
    try { await use(backend) } finally { await backend.stop() }
  },
  page: async ({ page, env }, use, testInfo) => {
    // Staging is the CI-only Community-tier E2E environment; a team-tier page
    // can only redirect to the dashboard there. Skip such a navigation with a
    // clear reason instead of failing the heading assertion (see
    // setup/target-tier.ts). Inert on local / a Team-licensed target.
    installEntitlementNavigationGuard(page, env, (reason) => testInfo.skip(true, reason))
    const enabled = process.env.VITE_COVERAGE === 'true'
    if (enabled) await startCoverage(page)
    await use(page)
    if (enabled) await stopCoverage(page)
  },
})

export { expect }

export function isDevModeTarget(env: TestEnv): boolean {
  return env.name === 'local'
}

function b64url(input: unknown): string {
  return Buffer.from(JSON.stringify(input)).toString('base64url')
}

/**
 * The local target's mock login must issue a token with the SAME claim
 * contract as the real backend login (`POST /api/v1/auth/login` →
 * `create_access_token`): a JWT whose payload carries `sub` (account email),
 * `org_id`, `account_id`, `org_role` and `is_system_admin`.
 *
 * The SPA reads those claims straight off the token — `lib/jwt.ts` decodes it
 * for the router's role gate (`enforceRoleTierVisibility`), `useCurrentUser`
 * and `planStore.currentOrgId()` — and a non-JWT opaque string decodes to
 * `null`. With no readable `org_role`, EVERY route declaring
 * `required_roles: [admin]` (the ADMIN / CONFIGURE groups and the whole
 * `x-team` surface) was silently redirected to the dashboard, so its specs
 * failed asserting the wrong `<h1>`. The SPA never verifies the signature, so a
 * fixed placeholder third segment is sufficient — only the payload shape
 * matters.
 */
export function mintMockAccessToken(overrides: Record<string, unknown> = {}): string {
  const header = b64url({ alg: 'HS256', typ: 'JWT' })
  const now = Math.floor(Date.now() / 1000)
  const payload = b64url({
    sub: 'admin@example.com',
    org_id: '1',
    account_id: '1',
    org_role: 'admin',
    is_system_admin: false,
    ...overrides,
    iat: now,
    exp: now + 3600,
  })
  return `${header}.${payload}.mocked-signature`
}

// A regular org-admin session — the local analogue of the staging
// E2E_ADMIN_* identity, which is also a non-system org admin.
const MOCK_ACCESS_TOKEN = mintMockAccessToken()
const MOCK_REFRESH_TOKEN = 'mock-refresh-token-for-e2e-tests'

// FAR-2665: the plan payload now carries the commercial ENTITLEMENT grant, and
// a behaviour flag is no longer opened by a tier. Before FAR-2665 the local
// mock answered with `flags: []` plus `license.tier: 'enterprise'`, and the
// FeatureGate's `required-tier` fallback (enterprise rank 2 >= community/team)
// opened every gated page. That fallback is gone, so the mock must model the
// fully-licensed, everything-enabled target explicitly: every gated behaviour
// flag active and every entitlement granted. Without it `flags: []` fails every
// FeatureGate closed (#settingsmcpview-client never renders) and hides the
// entitlement-gated routes (/settings/sso, /admin/audit, /admin/costs, ...).
const MOCK_ENTITLEMENTS = [
  { key: 'identity', name: 'Identity', description: 'SSO, SCIM and team RBAC', granted: true, flags: ['sso', 'scim', 'team_rbac'] },
  { key: 'audit_evidence', name: 'Audit and evidence', description: 'Audit viewer and run retention', granted: true, flags: ['audit_viewer', 'admin_run_retention'] },
  { key: 'cost_governance', name: 'Cost governance', description: 'Spend limits, cost controls and breakdown', granted: true, flags: ['admin_spend_limits', 'admin_cost_controls', 'admin_cost_breakdown'] },
]

// Every behaviour flag a view gates on with a `feature-name` FeatureGate. On
// main each was opened by the tier fallback above; keep them all active so the
// mock target stays the fully-enabled instance those specs were written for.
const MOCK_ACTIVE_FLAGS = [
  'assistant',
  'audit_viewer',
  'plugin_management',
  'admin_cost_breakdown',
  'admin_cost_controls',
  'error_tracking',
  'model_backend_management',
  'webhook_notification_log',
  'team_rbac',
  'admin_run_retention',
  'environment_profiles',
  'admin_spend_limits',
  'runtime_config',
  'user_management',
  'eval_system',
  'schema_version_history',
  'email_config',
  'error_forwarders',
  'mcp_server',
  'observability',
  'rate_limits',
  'sso',
  'webhook_trigger',
]

const MOCK_PLAN = {
  license: { tier: 'enterprise', org_id: '1', expires_at: null },
  dev_mode: true,
  flags: MOCK_ACTIVE_FLAGS.map((name) => ({ name, currently_active: true })),
  entitlements: MOCK_ENTITLEMENTS,
}

/**
 * FAR-2713: the caller-scoped permission list the router gate and the sidebar
 * read (`GET /api/v1/auth/permissions` → `{ permissions: string[] }`, resolved
 * server-side from the backend permission registry). It is NOT the generic
 * `{items,total}` envelope: answering that shape leaves
 * `planStore.userPermissions` empty, the gate fails closed, and every route
 * declaring `required_permissions` (the compliance evidence index, analytics,
 * determination, evals, library search, system maps, ...) is refused before its
 * view ever mounts.
 *
 * The mock models a fully-enabled org admin, so it grants exactly the
 * permissions the product's own route manifest declares. Deriving them from
 * `src/manifest.yaml` (rather than hard-coding a list) keeps the mock in step
 * when a route adds a gate — a static list would silently refuse that route
 * again.
 */
function manifestRoutePermissions(): string[] {
  const manifestPath = resolve(__dirname, '../../../src/manifest.yaml')
  const manifest = yaml.load(readFileSync(manifestPath, 'utf-8')) as {
    routes?: Record<string, { required_permissions?: string[] | null }>
  }
  const names = new Set<string>()
  for (const entry of Object.values(manifest.routes ?? {})) {
    for (const name of entry.required_permissions ?? []) names.add(name)
  }
  return [...names].sort()
}

const MOCK_PERMISSIONS = manifestRoutePermissions()

/**
 * FAR-2713: fixtures for the four admin surfaces the general mock answered with
 * the generic envelope. Each payload mirrors the generated OpenAPI type the
 * view actually consumes (frontend/src/lib/api/schema.ts), with at least one
 * row so cards, tables and definition tiles render content rather than an
 * empty or error state.
 */
const MOCK_NOW = '2026-07-07T09:00:00Z'

// GET /api/v1/evidence/redaction-rules → RedactionRuleResponse[] (bare array).
// The seeded catch-all (`*` / viewer) mirrors migration 0320 / org creation.
const MOCK_REDACTION_RULES = [
  {
    id: '00000000-0000-4000-8000-00000000e001',
    key_pattern: '*',
    role: 'viewer',
    redacted_fields: [],
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  },
  {
    id: '00000000-0000-4000-8000-00000000e002',
    key_pattern: 'api_key',
    role: 'operator',
    redacted_fields: ['sensitive_output'],
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  },
  {
    id: '00000000-0000-4000-8000-00000000e003',
    key_pattern: 'internal_metadata',
    role: 'admin',
    redacted_fields: ['internal_metadata'],
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  },
]

// GET /api/v1/compliance/evidence-index → EvidenceIndex (a structured object,
// never the envelope). `detail.human_only_counts_are_lower_bounds` and a
// `pipeline-snapshot:` identifier are included so the view's lower-bound banner
// and its concise snapshot label render too.
const MOCK_EVIDENCE_INDEX = {
  pack_version: '2026.07.1',
  pack_content_hash: 'sha256:4f1c0f4b9a2e7d3c8b5a6f0e1d2c3b4a5f6e7d8c9b0a1f2e3d4c5b6a7f8e9d0c',
  pack_published_at: '2026-07-01T00:00:00Z',
  generated_at: MOCK_NOW,
  period_from: null,
  period_to: null,
  product_version: '0.9.0',
  organisation_id: '1',
  framework_ids: ['soc2', 'iso27001'],
  coverage: {
    soc2: { access_control: 4, audit_logging: 3 },
    iso27001: { access_control: 2 },
  },
  complete: true,
  missing_artifact_types: [],
  artifacts: [
    {
      artifact_type: 'audit_log',
      status: 'present',
      provenance: 'observed',
      description: 'Organisation audit trail export',
      location: '/api/v1/admin/audit',
      identifiers: ['audit-log:2026-07'],
      record_count: 42,
      detail: { human_only_counts_are_lower_bounds: false },
      mapping_row_ids: ['soc2-cc6.1', 'iso27001-a.12.4'],
      absence_reason: null,
    },
    {
      artifact_type: 'run_snapshot',
      status: 'present',
      provenance: 'observed',
      description: 'Frozen pipeline snapshot for a completed run',
      location: '/runs/101',
      identifiers: ['pipeline-snapshot:00000000-0000-4000-8000-00000000e101@v2'],
      record_count: 1,
      detail: { human_only_counts_are_lower_bounds: true },
      mapping_row_ids: ['soc2-cc7.2'],
    },
    {
      artifact_type: 'customer_policy',
      status: 'customer_supplied',
      provenance: 'customer-supplied',
      description: 'Customer-managed security policy document',
      location: 'customer-managed',
      identifiers: ['policy:acme-sec-1'],
      record_count: null,
      detail: {},
      mapping_row_ids: ['iso27001-a.5.1'],
    },
  ],
}

// GET /api/v1/plugins → PluginResponse[] (bare array). `capabilities` is a
// JSON array (the backend field is a set), which the view's pluginTypeLabel
// calls `.includes` on.
const MOCK_PLUGINS = [
  {
    PLUGIN_ID: 'farnalabs.github-connector',
    display_name: 'GitHub Connector',
    description: 'Bidirectional GitHub integration for pipelines and pull requests',
    version: '1.4.0',
    capabilities: ['connector_type'],
    health_ok: true,
    health_detail: 'Registry reachable',
    health_checked_at: MOCK_NOW,
  },
  {
    PLUGIN_ID: 'farnalabs.anthropic-backend',
    display_name: 'Anthropic Model Backend',
    description: 'Anthropic Claude model backend',
    version: '2.0.1',
    capabilities: ['model_backend'],
    health_ok: false,
    health_detail: 'API key not configured',
    health_checked_at: MOCK_NOW,
  },
  {
    PLUGIN_ID: 'farnalabs.core-tools',
    display_name: 'Core Tools',
    description: 'Built-in pipeline node types',
    version: '0.9.0',
    capabilities: [],
    health_ok: true,
    health_detail: 'OK',
    health_checked_at: null,
  },
]

// Shared date spine for the dashboard summary/trends fixtures. Kept as data
// (rather than inlined per series) so the seven trends arrays stay the same
// length and every series lines up on the same dates for the charts.
const DASHBOARD_TREND_DATES = [
  '2026-07-01', '2026-07-02', '2026-07-03', '2026-07-04',
  '2026-07-05', '2026-07-06', '2026-07-07',
]

export async function setupLocalMockApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const url = route.request().url()
    const method = route.request().method()
    if ((url.includes('/api/v1/auth/login') || url.includes('/api/v1/auth/refresh')) && method === 'POST') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ access_token: MOCK_ACCESS_TOKEN, refresh_token: MOCK_REFRESH_TOKEN, token_type: 'bearer' }),
      })
    }
    // Leave the real SSO login/callback legs to the test's deterministic IdP
    // seam (backend/tests/e2e/sso_fixture.py) instead of letting this general
    // local mock swallow those requests.
    if (
      url.includes('/api/v1/auth/oidc/') ||
      url.includes('/api/v1/auth/login-context') ||
      url.includes('/api/v1/auth/sso/providers')
    ) return route.fallback()
    if (url.includes('/api/v1/me/settings')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ locale: 'en-US' }) })
    }
    if (url.includes('/api/v1/auth/permissions')) {
      // FAR-2713: `{ permissions: string[] }` — never the generic
      // {items,total} envelope (see MOCK_PERMISSIONS). Registered before the
      // generic GET catch-all so `required_permissions` routes render.
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ permissions: MOCK_PERMISSIONS }),
      })
    }
    if (url.includes('/api/v1/me')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: '1', email: 'admin@example.com', display_name: 'Admin' }) })
    }
    if (url.includes('/api/v1/pipelines') && method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [{ id: '1', name: 'Test Pipeline', organisation_id: '1', description: 'A test pipeline', visibility: 'org', status: 'idle', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), archived_at: null }], total: 1 }) })
    }
    if (url.includes('/api/v1/plan') || url.includes('/api/v1/admin/feature-flags')) {
      // planStore bootstraps from the caller-scoped GET /api/v1/plan (FAR-1809
      // collapsed the old instance-admin /api/v1/admin/feature-flags reads into
      // it); the instance-admin endpoint still backs the admin feature-flags
      // page, so answer both with the plan payload shape. FAR-2665: the payload
      // also carries the entitlement grant — see MOCK_PLAN.
      // dev_mode: true so private_preview routes (evals, runs-diff, feedback
      // inbox, saved views, node categories, feature flags, monitoring,
      // error forwarders, runtime config, rate limits, ...) resolve on the
      // local mock-API target instead of redirecting to the dashboard. Local
      // e2e mirrors staging, which also runs with dev mode on.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PLAN) })
    }
    if (url.includes('/api/v1/admin/license')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ expires_at: null, org_id: '1', tier: 'enterprise' }) })
    }
    if (url.includes('/api/v1/admin/tiers')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ tiers: [{ tier_id: 'community', label: 'Community', rank: 0 }, { tier_id: 'team', label: 'Team', rank: 1 }, { tier_id: 'enterprise', label: 'Enterprise', rank: 2 }] }) })
    }
    if (url.includes('/api/v1/dashboard/summary') && method === 'GET') {
      // DashboardView calls this on mount (with `days=<n>` for a rolling
      // window). serve the same populated body for both shapes — the summary
      // validators require total_runs/active_pipelines/run_counts_by_status/
      // teams/recent_runs, and the arrays must not be empty or the view falls
      // through to its error/empty branches. Registered BEFORE the generic GET
      // catch-all below so it is actually reachable.
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          total_runs: 128,
          active_pipelines: 4,
          run_counts_by_status: { running: 1, awaiting_human: 0, failed: 3, idle: 124 },
          eval_pass_rate: {
            overall_pass_rate: 92,
            total_evals: 50,
            passed_evals: 46,
            per_pipeline: {},
            per_team_pipeline: {},
          },
          trend: DASHBOARD_TREND_DATES.map((date, i) => ({
            date,
            run_count: 12 + i * 3,
            eval_pass_rate: 88 + i,
            token_spend_usd: 3.5 + i * 0.75,
            avg_time_to_approve_ms: 120000 + i * 5000,
            rejection_rate: 5 + i,
          })),
          teams: [
            {
              id: '1',
              name: 'Platform',
              total_runs: 128,
              active_pipelines: 4,
              run_counts_by_status: { running: 1, awaiting_human: 0, failed: 3, idle: 124 },
              eval_pass_rate: { total_evals: 50, passed_evals: 46, pass_rate: 92 },
            },
          ],
          recent_runs: [
            { id: '101', pipeline_name: 'Release Pipeline', status: 'success', created_at: '2026-07-07T09:15:00Z', trigger_type: 'webhook', execution_origin: null },
            { id: '102', pipeline_name: 'Nightly Regression', status: 'failed', created_at: '2026-07-07T06:00:00Z', trigger_type: 'cron', execution_origin: 'dispatched' },
            { id: '103', pipeline_name: 'Docs Build', status: 'running', created_at: '2026-07-07T11:30:00Z', trigger_type: 'manual', execution_origin: null },
          ],
          config_warnings: [],
        }),
      })
    }
    if (url.includes('/api/v1/dashboard/trends') && method === 'GET') {
      // Seven required series, each an ARRAY (validateTrendsResponse rejects
      // any missing/non-array key). Populated wherever the chart reads a value;
      // every series shares DASHBOARD_TREND_DATES so the combined trend in
      // DashboardView renders a full curve. Registered BEFORE the generic GET
      // catch-all so it is actually reachable.
      // Every rejection-rate field is a PERCENTAGE (0-100), matching the
      // backend's `round(rejected / total * 100, 1)` — never a 0-1 fraction.
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          days: DASHBOARD_TREND_DATES.length,
          run_counts: DASHBOARD_TREND_DATES.map((date, i) => ({ date, run_count: 12 + i * 3 })),
          eval_pass_rates: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_evals: 8, passed_evals: 7, pass_rate: 88 + i })),
          token_spend: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_spend_usd: 3.5 + i * 0.75 })),
          hitl_volume: DASHBOARD_TREND_DATES.map((date, i) => ({ date, total_decisions: 4, approved_count: 3, rejected_count: 1, rejection_rate: 25, avg_time_to_approve_ms: 120000 + i * 5000 })),
          rejection_trend: DASHBOARD_TREND_DATES.map((date, i) => ({ date, rolling_rejection_rate: 5 + i, raw_rejection_rate: 6 + i })),
          correlation: DASHBOARD_TREND_DATES.map((date, i) => ({ date, rejection_rate: 5 + i, eval_pass_rate: 88 + i })),
          feedback_volume: DASHBOARD_TREND_DATES.map((date) => ({ date, feedback_count: 2, resolved_count: 1, correcting_count: 1 })),
        }),
      })
    }
    if (
      (url.includes('/api/v1/pipeline-folders') || url.includes('/api/v1/schema-folders')) &&
      method === 'GET'
    ) {
      // Folder-list endpoints return a BARE ARRAY, not the generic
      // {items,total} envelope. The consumers type the response as
      // FolderItem[] and iterate it in a computed — FolderTree.vue
      // (`allFolders.value.filter`) and PipelineListView.vue /
      // SchemaListView.vue (`for (const f of foldersList.value)`) — so the
      // envelope makes the computed throw `TypeError: ... is not a function`
      // / `... is not iterable` and silently blanks the folder sidebar
      // (FAR-1876). Mirrors `list_folders` in
      // backend/src/modulo/api/routes/{pipeline_folders,schema_folders}.py,
      // which returns `list[FolderResponse]`. Registered BEFORE the generic
      // GET catch-all below so it is actually reachable.
      return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    }
    if (url.includes('/api/v1/runners/status') && method === 'GET') {
      // `RunnersStatusResponse` is a STRUCTURED contract, not the generic
      // {items,total} envelope: `RunnersConcurrencyTab` reads
      // `status.concurrency.preflight` (FAR-591 D5), so the envelope makes
      // `.preflight` throw `TypeError: Cannot read properties of undefined` and
      // blanks the whole Concurrency tab. Mirrors
      // `components["schemas"]["RunnersStatusResponse"]` in the generated
      // OpenAPI types (frontend/src/lib/runnersStatus.ts derives from it).
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          aggregate_state: 'healthy',
          probe_interval_seconds: 60,
          staleness_threshold_seconds: 300,
          machines: [],
          profiles: [],
          concurrency: {
            sandbox_concurrency_limit: 4,
            is_default: true,
            preflight: {
              state: 'ok',
              detail: 'Within engine capacity',
              engine_cpu_count: 8,
              engine_mem_total_mb: 16384,
              needed_cpu: 4,
              needed_mem_mb: 4096,
            },
          },
        }),
      })
    }
    if (url.includes('/api/v1/admin/org/sandbox-concurrency') && method === 'GET') {
      // `SandboxConcurrencyResponse` (FAR-589 D3b) — the Concurrency tab's
      // limit read. `is_default` marks the ABSENT-key case so the UI can
      // render "4 (default)" rather than "no gate".
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ sandbox_concurrency_limit: 4, is_default: true }),
      })
    }
    if (url.includes('/api/v1/admin/costs/anomalies') && method === 'GET') {
      // `get_anomalies` returns a BARE ARRAY (`list[AnomalyResponse]` — the
      // generated 200 type is `components["schemas"]["AnomalyResponse"][]`),
      // not the generic {items,total} envelope. AdminCostBreakdownView calls
      // `.filter()` on it, so serving the envelope throws
      // `TypeError: anomalies.value.filter is not a function` (FAR-2614).
      // Registered BEFORE the generic GET catch-all below so it is reachable.
      return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    }
    if (url.includes('/api/v1/evidence/redaction-rules') && method === 'GET') {
      // `list_redaction_rules_endpoint` returns a BARE ARRAY (`list[RedactionRuleResponse]`).
      // AdminEvidenceRedactionView reads `rules.value.some(...)` / `.length` /
      // iteration over it, so the {items,total} envelope throws
      // `TypeError: rules.value.some is not a function` and the whole view's
      // render fails (no h1, no table). Registered before the generic GET
      // catch-all below.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_REDACTION_RULES) })
    }
    if (url.includes('/api/v1/compliance/evidence-index') && method === 'GET') {
      // Structured `EvidenceIndex` object (coverage map + artifacts array),
      // never the envelope: ComplianceEvidenceIndexView reads
      // `indexData.coverage` and `.artifacts.filter(...)`, so the envelope
      // throws `Cannot read properties of undefined (reading 'filter')` and
      // the definition tiles never render.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_EVIDENCE_INDEX) })
    }
    if (url.includes('/api/v1/plugins') && method === 'GET') {
      // `list_plugins_endpoint` returns a BARE ARRAY (`list[PluginResponse]`).
      // AdminPluginsView iterates it and calls `capabilities.includes(...)` in
      // pluginTypeLabel; the envelope makes each row an object whose
      // `capabilities` is undefined, so the render throws and the page has no
      // h1 or table.
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PLUGINS) })
    }
    if (method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [], total: 0 }) })
    }
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
  })
}

// Budgets for the login handshake.  The step race is generous enough for a
// cold SPA boot; the credential-form guard is tighter so a broken login page
// aborts in seconds instead of letting every test retry for 30 s.
const LOGIN_STEP_TIMEOUT = 15_000
const CREDENTIAL_FORM_TIMEOUT = 10_000

/**
 * The credential-form selectors actually rendered by the target. Two layouts
 * provide a credential form:
 *  - LoginView's direct branch (`login-email`/`login-password`) on single-org
 *    instances; and
 *  - OrgLoginView (`org-login-email`/`org-login-password`) on multi-org
 *    instances, reached after the slug step navigates to /login/<slug>.
 * Callers must fill/submit whichever one rendered, not assume LoginView's.
 */
export interface CredentialFormSelectors {
  email: string
  password: string
  submit: string
}

function singleOrgCredentialForm(env: TestEnv): CredentialFormSelectors {
  return {
    email: env.credentials.loginFormEmailSelector,
    password: env.credentials.loginFormPasswordSelector,
    submit: env.credentials.loginFormSubmitSelector,
  }
}

function orgCredentialForm(env: TestEnv): CredentialFormSelectors {
  return {
    email: env.credentials.orgLoginFormEmailSelector,
    password: env.credentials.orgLoginFormPasswordSelector,
    submit: env.credentials.orgLoginFormSubmitSelector,
  }
}

/**
 * Fail fast unless a credential (email/password) form renders within the
 * budget, and return the selectors for whichever layout appeared. Shared by
 * every login entry point so a broken login page produces one actionable
 * diagnostic instead of a 30 s timeout per test.
 */
async function requireCredentialForm(
  page: Page,
  env: TestEnv,
  slugStepTaken: boolean,
): Promise<CredentialFormSelectors> {
  const candidates = [singleOrgCredentialForm(env), orgCredentialForm(env)]
  // Wait once for whichever layout's email field appears first. A single
  // union selector avoids racing two waitFor()s and leaving the loser's
  // timer dangling for the rest of the budget after one resolves.
  const emailSelectors = candidates.map((form) => form.email).join(', ')
  const appeared = await page
    .locator(emailSelectors)
    .first()
    .waitFor({ state: 'visible', timeout: CREDENTIAL_FORM_TIMEOUT })
    .then(() => true)
    .catch(() => false)

  if (appeared) {
    // Disambiguate which layout rendered so callers fill its own fields.
    for (const form of candidates) {
      if (await page.locator(form.email).isVisible()) return form
    }
  }

  const hint = slugStepTaken
    ? `the org slug "${env.orgSlug}" may be wrong, or /login/${env.orgSlug} did not render a credential form`
    : 'the login page layout or login-context API may have changed'
  throw new Error(
    `[login] Credential form did not appear within ${CREDENTIAL_FORM_TIMEOUT}ms ` +
    `(slugStepTaken=${slugStepTaken}). Neither ${env.credentials.loginFormEmailSelector} ` +
    `nor ${env.credentials.orgLoginFormEmailSelector} became visible. ` +
    `Current URL: ${page.url()}. ${hint}. ` +
    'Every test that signs in will fail the same way.',
  )
}

/**
 * Advance past the multi-org slug-entry step (if present) and guarantee the
 * email/password credential form is on screen. Single-org instances render the
 * credential form directly on /login; multi-org instances render OrgLoginView's
 * form after the slug step. Returns the selectors for whichever form appeared,
 * and throws a diagnostic if neither does, so every caller fails fast instead
 * of timing out per test. Must be called after navigating to /login.
 */
export async function completeLoginForm(page: Page, env: TestEnv): Promise<CredentialFormSelectors> {
  const slugInput = page.locator(env.credentials.orgSlugInputSelector)
  const emailInput = page.locator(env.credentials.loginFormEmailSelector)

  // Race: whichever appears first wins — the slug step on multi-org targets,
  // the credential form on single-org targets.
  await Promise.race([
    slugInput.waitFor({ state: 'visible', timeout: LOGIN_STEP_TIMEOUT }).catch(() => {}),
    emailInput.waitFor({ state: 'visible', timeout: LOGIN_STEP_TIMEOUT }).catch(() => {}),
  ])

  const slugStepTaken = await slugInput.isVisible()
  if (slugStepTaken) {
    await slugInput.fill(env.orgSlug)
    await page.locator(env.credentials.orgSlugSubmitSelector).click()
  }

  return requireCredentialForm(page, env, slugStepTaken)
}

/**
 * Navigate to the login page and ensure the credential form is ready to fill.
 * Use this instead of a bare `page.goto('/login')` whenever the test is about
 * to enter credentials, so the suite works on both single-org and multi-org
 * targets. Returns the selectors for whichever form rendered.
 */
export async function openLoginForm(page: Page, env: TestEnv): Promise<CredentialFormSelectors> {
  await page.goto('/login')
  return completeLoginForm(page, env)
}

export async function loginAsAdmin(page: Page, env: TestEnv) {
  if (env.name !== 'local') {
    await loginThroughUi(page, env)
    return
  }

  await setupLocalMockApi(page)
  await page.goto('/login')
  await page.evaluate(([token, refresh]) => {
    localStorage.setItem('modulo_access_token', token)
    localStorage.setItem('modulo_refresh_token', refresh)
  }, [MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN])
}

/**
 * Mint a real JWT whose payload marks the principal as a system admin
 * (is_system_admin + org_role admin). The local mock login token is a plain org
 * admin, so the router's SYSTEM-group gate (`requiresSystemAdmin`) redirects
 * until this is swapped in.
 *
 * `sub` defaults to the mock admin id; pass a specific value when a test needs
 * a particular principal (e.g. a UUID `sub`, which is what the backend mints
 * and what populates the sidebar's visible identity text).
 */
export function systemAdminJwt(sub = '1'): string {
  return mintMockAccessToken({ sub, is_system_admin: true })
}

/**
 * Log in for a route in the SYSTEM sidebar group. FAR-938 makes the whole
 * group system-admin-only (UI + router).
 *
 * Returns true when the target can actually render such a route. The local
 * target uses the mock API and swaps in a system-admin JWT (same approach as
 * assistant-only.spec.ts). Staging/prod authenticate with the real E2E_ADMIN_EMAIL
 * identity, which is a regular org admin — there is no in-product way to grant
 * is_system_admin — so the router guard redirects SYSTEM routes to the
 * dashboard and this returns false.
 */
export async function loginForSystemRoute(page: Page, env: TestEnv): Promise<boolean> {
  await loginAsAdmin(page, env)
  if (env.name !== 'local') return false
  await page.evaluate((token) => {
    localStorage.setItem('modulo_access_token', token)
  }, systemAdminJwt())
  return true
}
