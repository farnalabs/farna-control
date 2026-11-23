import { formatApiError } from '../lib/api/formatError'
import { decodeJwtPayload } from '../lib/jwt'
import { createRouter, createWebHistory, type RouteMeta } from 'vue-router'
import { getActivePinia } from 'pinia'
import { getAccessToken } from '../lib/api/client'
import { usePlanStore } from '../stores/planStore'
import i18n from '../i18n'
import manifest from '@/manifest.yaml'
import LoginView from '../views/LoginView.vue'
import OrgLoginView from '../views/OrgLoginView.vue'
import AuthCallbackView from '../views/AuthCallbackView.vue'
import { resolveDemoEntry } from '../lib/api/demo'

declare module 'vue-router' {
  interface RouteMeta {
    requiresSystemAdmin?: boolean
    breadcrumb?: string
    parent?: string
    testid?: string
    requiredRoles?: string[]
    requiredEntitlement?: string
    requiredPermissions?: string[]
    featureFlag?: string
    visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
    public?: boolean
    bare?: boolean
    title?: string
  }
}

interface ManifestEntry {
  name: string
  breadcrumb: string
  parent: string | null
  testid: string
  required_roles: string[] | null
  required_entitlement: string | null
  required_permissions: string[] | null
  feature_flag: string | null
  visibility?: 'public' | 'public_preview' | 'private_preview' | 'in_dev'
  system_admin_only?: boolean
  sidebar_group?: string | null
}

const manifestRoutes = (manifest as { routes?: Record<string, ManifestEntry> })?.routes ?? {}
const manifestSidebarGroups = (manifest as { sidebar_groups?: Record<string, { system_admin_only?: boolean }> })?.sidebar_groups ?? {}
const manifestByName = new Map<string, ManifestEntry & { path: string }>()
// FAR-1950: the same entries indexed by their manifest path. A navigation can
// resolve to a route record whose `name` is undefined (a named parent with an
// unnamed, empty-path default child — Vue Router R0103), so the path index is
// the fallback that keeps the gate bound to the record the manifest describes.
const manifestByPath = new Map<string, ManifestEntry & { path: string }>()
const manifestPathToName = new Map<string, string>()
for (const [path, entry] of Object.entries(manifestRoutes)) {
  if (!entry) continue
  const withPath = { ...entry, path }
  manifestByPath.set(path, withPath)
  if (entry.name) {
    manifestByName.set(entry.name, withPath)
    manifestPathToName.set(path, entry.name)
  }
}

const AnalyticsView = () => import('../views/AnalyticsView.vue')
const DashboardView = () => import('../views/DashboardView.vue')
const GovernanceStackView = () => import('../views/GovernanceStackView.vue')
const LibraryView = () => import('../views/LibraryView.vue')
const LibraryPipelineWizard = () => import('../views/LibraryPipelineWizard.vue')
const CollectionCreateView = () => import('../views/CollectionCreateView.vue')
const CollectionDetailView = () => import('../views/CollectionDetailView.vue')
const SettingsObservabilityView = () => import('../views/SettingsObservabilityView.vue')
const SettingsRateLimitsView = () => import('../views/SettingsRateLimitsView.vue')
const SettingsRuntimeConfigView = () => import('../views/SettingsRuntimeConfigView.vue')
const SettingsSsoView = () => import('../views/SettingsSsoView.vue')
const SettingsTeamsView = () => import('../views/SettingsTeamsView.vue')
const SchemaInferenceView = () => import('../views/SchemaInferenceView.vue')
const SchemaListView = () => import('../views/SchemaListView.vue')
const SchemaEditorView = () => import('../views/SchemaEditorView.vue')
const OnboardingWizard = () => import('../views/OnboardingWizard.vue')
const FeedbackInboxView = () => import('../views/FeedbackInboxView.vue')
const EvalsListView = () => import('../views/EvalsListView.vue')
const EvalEditorView = () => import('../views/EvalEditorView.vue')
const EvalProposalsQueueView = () => import('../views/EvalProposalsQueueView.vue')
const VariantCompareView = () => import('../views/VariantCompareView.vue')
const VariantBatchCompareView = () => import('../views/VariantBatchCompareView.vue')
const RunsListView = () => import('../views/RunsListView.vue')
const RunDetailView = () => import('../views/RunDetailView.vue')
const AgentOutputDiffView = () => import('../views/AgentOutputDiffView.vue')
const EvidenceView = () => import('../views/EvidenceView.vue')
const RiskAssessmentView = () => import('../views/RiskAssessmentView.vue')
const AdminAuditView = () => import('../views/AdminAuditView.vue')
const AdminFeatureFlagsView = () => import('../views/AdminFeatureFlagsView.vue')
const AdminPluginsView = () => import('../views/AdminPluginsView.vue')
const PipelineEditorView = () => import('../views/PipelineEditorView.vue')
const CompositeEditorView = () => import('../views/pipeline/CompositeEditorView.vue')
const CopyPipelineWizard = () => import('../views/CopyPipelineWizard.vue')
// removed: PipelineTemplateGallery — merged into /library
const AdminUsersView = () => import('../views/AdminUsersView.vue')
const AdminSpendLimitsView = () => import('../views/AdminSpendLimitsView.vue')
const AdminCostBreakdownView = () => import('../views/AdminCostBreakdownView.vue')
const AdminCostControlsView = () => import('../views/AdminCostControlsView.vue')
const CostComponentsView = () => import('../views/CostComponentsView.vue')
const AdminConnectorsView = () => import('../views/AdminConnectorsView.vue')
const AdminNodeCategoriesView = () => import('../views/AdminNodeCategoriesView.vue')
const AdminEvidenceRedactionView = () => import('../views/AdminEvidenceRedactionView.vue')
const ComplianceEvidenceIndexView = () => import('../views/ComplianceEvidenceIndexView.vue')
const AdminModelBackendsView = () => import('../views/AdminModelBackendsView.vue')
const AdminOrgSettingsView = () => import('../views/AdminOrgSettingsView.vue')
const AdminRunRetentionView = () => import('../views/AdminRunRetentionView.vue')
const NotificationsPage = () => import('../views/NotificationsPage.vue')
const MyProfileView = () => import('../views/MyProfileView.vue')
const SettingsLicenseView = () => import('../views/SettingsLicenseView.vue')
const SettingsMcpView = () => import('../views/SettingsMcpView.vue')
const SettingsTriggersView = () => import('../views/SettingsTriggersView.vue')
const SettingsTriggerEventLogView = () => import('../views/SettingsTriggerEventLogView.vue')
const SettingsGuardrailsView = () => import('../views/SettingsGuardrailsView.vue')
const SettingsHitlReviewView = () => import('../views/SettingsHitlReviewView.vue')
const AdminNotificationDeliveryLogView = () => import('../views/AdminNotificationDeliveryLogView.vue')
const AdminHousekeepingView = () => import('../views/AdminHousekeepingView.vue')
const AdminSystemOrgsView = () => import('../views/AdminSystemOrgsView.vue')
const AdminSystemConfigView = () => import('../views/AdminSystemConfigView.vue')
const AdminProductAnalyticsView = () => import('../views/AdminProductAnalyticsView.vue')
const AdminAssistantView = () => import('../views/AdminAssistantView.vue')
const AdminErrorsView = () => import('../views/AdminErrorsView.vue')
const AdminErrorDetailView = () => import('../views/AdminErrorDetailView.vue')
const UserAssistantSkillsView = () => import('../views/UserAssistantSkillsView.vue')
const SettingsEmailView = () => import('../views/SettingsEmailView.vue')
const SettingsNotificationsView = () => import('../views/SettingsNotificationsView.vue')
const SettingsErrorForwardersView = () => import('../views/SettingsErrorForwardersView.vue')
const SettingsMonitorConfigView = () => import('../views/SettingsMonitorConfigView.vue')
const PipelineListView = () => import('../views/PipelineListView.vue')
const ModelBackendSetupView = () => import('../views/setup/ModelBackendSetupView.vue')
const DevMetricsView = () => import('../views/DevMetricsView.vue')
const AdminRunnersView = () => import('../views/AdminRunnersView.vue')
const RunnersProfilesTab = () => import('../views/runners/RunnersProfilesTab.vue')
const RunnersConcurrencyTab = () => import('../views/runners/RunnersConcurrencyTab.vue')
const EnvironmentProfileForm = () => import('../views/environment-profiles/EnvironmentProfileForm.vue')
const ParameterSchemasView = () => import('../views/ParameterSchemasView.vue')
const OAuthConsentView = () => import('../views/OAuthConsentView.vue')
const DemoView = () => import('../views/DemoView.vue')
const AcceptInviteView = () => import('../views/AcceptInviteView.vue')
const AssistantOnlyView = () => import('../views/AssistantOnlyView.vue')
const SystemMapsView = () => import('../views/SystemMapsView.vue')
const SystemMapDetailView = () => import('../views/SystemMapDetailView.vue')
const DeterminationView = () => import('../views/DeterminationView.vue')
const LockedRouteView = () => import('../views/LockedRouteView.vue')

/**
 * Move focus to the newly entered route's content.
 *
 * The page heading is the preferred target. A route that renders no
 * `[data-page-heading]` (a bare `/assistant`, for example) otherwise leaves
 * focus on the outgoing view's detached element, so the shell `<main>` carries
 * `tabindex="-1"` as the deterministic fallback: programmatically focusable,
 * never tab-reachable. Focusing a landmark rather than the heading is also
 * what keeps a skip-link target honest.
 */
export function focusRouteContent() {
  const target =
    document.querySelector<HTMLElement>('[data-page-heading]') ??
    document.querySelector<HTMLElement>('[data-main-landmark]')
  if (target && document.activeElement !== target && !target.contains(document.activeElement)) {
    target.focus({ preventScroll: true })
  }
}

/**
 * The `document.title` for a navigated route.
 *
 * A route with neither a manifest `title` nor a breadcrumb label falls back to
 * the bare product name rather than repeating it as "<product> | <product>".
 */
export function documentTitleFor(meta: RouteMeta): string {
  const pageTitle = meta.title ?? meta.breadcrumb
  return pageTitle ? `${pageTitle} | Farna Control` : 'Farna Control'
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
    },
    {
      // Per-org login (FAR-863): bookmarkable, deep-linkable, refresh-safe.
      // The view resolves the org on mount via GET /api/v1/auth/org-login/{slug}.
      // A not-found response shows a generic error — never reveals whether the
      // slug exists (tenancy boundary).
      path: '/login/:slug',
      name: 'org-login',
      component: OrgLoginView,
      props: true,
    },
    {
      // SSO (OIDC/SAML) success handoff: the backend redirects the browser to
      // /auth/callback#access_token=... after a successful (FAR-1197 dropped the refresh-token fragment: it rides the httpOnly cookie)
      // provider callback. This public route consumes the fragment tokens,
      // stores them, strips them from the URL, and redirects to the dashboard.
      // Public so the auth guard does not bounce an unauthenticated browser
      // back to /login before the tokens are persisted.
      path: '/auth/callback',
      name: 'auth-callback',
      component: AuthCallbackView,
      meta: { public: true, breadcrumb: 'Signing in' },
    },
    {
      // OAuth browser consent route (ADR 047 A1b): the 302 target of
      // /mcp/oauth/authorize. Public — anonymous users must be able to land
      // here and sign in before approving (the authenticated approve POST IS
      // the consent).
      path: '/oauth/authorize',
      name: 'oauth-authorize',
      component: OAuthConsentView,
      meta: { public: true, breadcrumb: 'Authorize' },
    },
    {
      // Demo auto-login (FAR-535): public one-shot route. The beforeEach guard
      // intercepts /demo and performs the hand-off pre-mount (clear stored
      // auth → POST /api/v1/auth/demo → store the short-lived read-only demo
      // token), redirecting to the dashboard — or to /login on failure, never
      // surfacing an error that reveals demo internals. The component is a
      // defensive splash only; the guard always redirects before it mounts.
      path: '/demo',
      name: 'demo',
      component: DemoView,
      meta: { public: true, breadcrumb: 'Demo' },
    },
    {
      // One-time invite enrollment (FAR-461): <origin>/accept-invite#token=...
      // Public by design — the token IS the credential; no session exists yet.
      // The view sets a password and then hands off to /login.
      path: '/accept-invite',
      name: 'accept-invite',
      component: AcceptInviteView,
      meta: { public: true, breadcrumb: 'Accept Invitation' },
    },
    {
      path: '/',
      name: 'dashboard',
      component: DashboardView,
    },
    {
      path: '/governance',
      name: 'governance-stack',
      component: GovernanceStackView,
    },
    {
      path: '/analytics',
      name: 'analytics',
      component: AnalyticsView,
    },
    {
      path: '/dashboard',
      redirect: '/',
    },
    {
      path: '/library',
      name: 'library',
      component: LibraryView,
    },
    {
      path: '/library/:id/create-pipeline',
      name: 'library-pipeline-wizard',
      component: LibraryPipelineWizard,
      props: true,
      meta: { breadcrumb: 'Create Pipeline', parent: 'library' },
    },
    {
      path: '/library/collections/new',
      name: 'library-collection-create',
      component: CollectionCreateView,
      meta: { breadcrumb: 'New Collection', parent: 'library' },
    },
    {
      path: '/library/collections/:id',
      name: 'library-collection-detail',
      component: CollectionDetailView,
      props: true,
      meta: { breadcrumb: 'Collection', parent: 'library' },
    },
    {
      path: '/settings/notifications',
      name: 'settings-notifications',
      component: SettingsNotificationsView,
    },
    {
      path: '/settings/email',
      name: 'settings-email',
      component: SettingsEmailView,
    },
    {
      path: '/settings/error-forwarders',
      name: 'settings-error-forwarders',
      component: SettingsErrorForwardersView,
    },
    {
      path: '/settings/monitoring',
      name: 'settings-monitoring',
      component: SettingsMonitorConfigView,
    },
    {
      path: '/settings/observability',
      name: 'settings-observability',
      component: SettingsObservabilityView,
    },
    {
      path: '/notifications',
      name: 'notifications',
      component: NotificationsPage,
    },
    {
      path: '/settings/teams',
      name: 'settings-teams',
      component: SettingsTeamsView,
    },
    {
      path: '/settings/sso',
      name: 'settings-sso',
      component: SettingsSsoView,
    },
    {
      path: '/settings/rate-limits',
      name: 'settings-rate-limits',
      component: SettingsRateLimitsView,
    },
    {
      path: '/settings/runtime-config',
      name: 'settings-runtime-config',
      component: SettingsRuntimeConfigView,
    },
    {
      path: '/settings/license',
      name: 'settings-license',
      component: SettingsLicenseView,
    },
    {
      path: '/settings/mcp',
      name: 'settings-mcp',
      component: SettingsMcpView,
    },
    {
      path: '/settings/triggers',
      name: 'settings-triggers',
      component: SettingsTriggersView,
    },
    {
      path: '/settings/guardrails',
      name: 'settings-guardrails',
      component: SettingsGuardrailsView,
    },
    {
      path: '/settings/hitl-review',
      name: 'settings-hitl-review',
      component: SettingsHitlReviewView,
    },
    {
      path: '/settings/assistant',
      name: 'settings-assistant',
      component: UserAssistantSkillsView,
    },
    {
      path: '/schemas',
      name: 'schemas',
      component: SchemaListView,
    },
    {
      path: '/schemas/editor/:id?',
      name: 'schema-editor',
      component: SchemaEditorView,
    },
    {
      path: '/schemas/infer',
      name: 'schema-infer',
      component: SchemaInferenceView,
    },
    {
      path: '/onboarding',
      name: 'onboarding',
      component: OnboardingWizard,
      meta: { breadcrumb: 'Onboarding', parent: 'dashboard' },
    },
    {
      path: '/feedback/inbox',
      name: 'feedback-inbox',
      component: FeedbackInboxView,
    },
    {
      path: '/evals',
      name: 'evals-list',
      component: EvalsListView,
    },
    {
      path: '/evals/editor',
      name: 'eval-editor',
      component: EvalEditorView,
    },
    {
      path: '/evals/proposals',
      name: 'eval-proposals-queue',
      component: EvalProposalsQueueView,
    },
    {
      path: '/variants/compare',
      name: 'variant-compare',
      component: VariantCompareView,
    },
    {
      path: '/variants/compare/:batchId',
      name: 'variant-compare-detail',
      component: VariantBatchCompareView,
      props: true,
      meta: { breadcrumb: 'Variant Batch Compare', parent: 'variant-compare', testid: 'variant-batch-compare' },
    },
    {
      path: '/runs',
      name: 'runs-list',
      component: RunsListView,
    },
    {
      path: '/runs/diff',
      name: 'runs-diff',
      component: AgentOutputDiffView,
    },
    {
      path: '/evidence',
      name: 'evidence',
      component: EvidenceView,
    },
    {
      path: '/risk-assessment',
      name: 'risk-assessment',
      component: RiskAssessmentView,
    },
    {
      path: '/determination',
      name: 'determination',
      component: DeterminationView,
    },
    {
      path: '/runs/:id',
      name: 'run-detail',
      component: RunDetailView,
    },
    {
      path: '/admin',
      redirect: '/admin/assistant',
    },
    {
      path: '/admin/my-profile',
      name: 'my-profile',
      component: MyProfileView,
    },
    {
      path: '/admin/users',
      name: 'admin-users',
      component: AdminUsersView,
    },
    {
      path: '/admin/costs/limits',
      name: 'admin-costs-limits',
      component: AdminSpendLimitsView,
    },
    {
      path: '/admin/costs',
      name: 'admin-costs',
      component: AdminCostBreakdownView,
    },
    {
      path: '/admin/costs/controls',
      name: 'admin-costs-controls',
      component: AdminCostControlsView,
    },
    {
      path: '/admin/costs/components',
      name: 'admin-costs-components',
      component: CostComponentsView,
    },
    {
      path: '/admin/audit',
      name: 'admin-audit',
      component: AdminAuditView,
    },
    {
      path: '/admin/connectors',
      name: 'admin-connectors',
      component: AdminConnectorsView,
    },
    {
      path: '/admin/node-categories',
      name: 'admin-node-categories',
      component: AdminNodeCategoriesView,
    },
    {
      path: '/admin/evidence-redaction',
      name: 'admin-evidence-redaction',
      component: AdminEvidenceRedactionView,
    },
    {
      path: '/admin/compliance/evidence-index',
      name: 'admin-compliance-evidence-index',
      component: ComplianceEvidenceIndexView,
    },
    {
      path: '/admin/evidence',
      name: 'admin-evidence-index',
      component: ComplianceEvidenceIndexView,
    },
    {
      path: '/admin/model-backends',
      name: 'admin-model-backends',
      component: AdminModelBackendsView,
    },
    {
      path: '/admin/feature-flags',
      name: 'admin-feature-flags',
      component: AdminFeatureFlagsView,
    },
    {
      path: '/admin/org',
      name: 'admin-org',
      component: AdminOrgSettingsView,
    },
    {
      path: '/admin/run-retention',
      name: 'admin-run-retention',
      component: AdminRunRetentionView,
    },
    {
      // FAR-591 D5: the Runners page replaced the standalone Sandbox
      // Concurrency surface (now the Concurrency tab).
      path: '/admin/sandbox-concurrency',
      redirect: '/admin/runners/concurrency',
    },
    {
      path: '/admin/parameter-schemas',
      name: 'admin-parameter-schemas',
      component: ParameterSchemasView,
    },
    {
      path: '/admin/plugins',
      name: 'admin-plugins',
      component: AdminPluginsView,
    },
    {
      path: '/admin/notification-delivery',
      name: 'admin-notification-delivery',
      component: AdminNotificationDeliveryLogView,
    },
    {
      // FAR-1255: the org-wide trigger event log (admin-gated
      // GET /api/v1/admin/trigger-events). Meta (breadcrumb, roles, tier)
      // hydrates from the manifest entry like the sibling admin log routes
      // (/admin/audit, /admin/notification-delivery) — no inline meta here.
      path: '/admin/trigger-events',
      name: 'admin-trigger-events',
      component: SettingsTriggerEventLogView,
    },
    {
      path: '/admin/housekeeping',
      name: 'admin-housekeeping',
      component: AdminHousekeepingView,
      meta: { breadcrumb: 'Housekeeping', testid: 'admin-housekeeping' },
    },
    {
      path: '/admin/environments',
      redirect: '/environment-profiles',
    },
    {
      path: '/admin/system/orgs',
      name: 'admin-system-orgs',
      component: AdminSystemOrgsView,
      meta: { breadcrumb: 'Organisations', parent: 'dashboard' },
    },
    {
      path: '/admin/system/config',
      name: 'admin-system-config',
      component: AdminSystemConfigView,
      meta: { breadcrumb: 'System Config', parent: 'dashboard' },
    },
    {
      path: '/admin/product-analytics',
      name: 'admin-product-analytics',
      component: AdminProductAnalyticsView,
      meta: { breadcrumb: 'Product Analytics', parent: 'dashboard' },
    },
    {
      path: '/admin/errors',
      name: 'admin-errors',
      component: AdminErrorsView,
    },
    {
      path: '/admin/errors/:id',
      name: 'admin-error-detail',
      component: AdminErrorDetailView,
    },
    {
      path: '/admin/assistant',
      name: 'admin-assistant',
      component: AdminAssistantView,
    },
    {
      path: '/pipelines/copy',
      name: 'pipeline-copy',
      component: CopyPipelineWizard,
    },
    {
      path: '/pipelines',
      name: 'pipeline-list',
      component: PipelineListView,
    },
    {
      path: '/templates',
      redirect: '/library',
    },
    {
      path: '/pipelines/:id/editor',
      name: 'pipeline-editor',
      component: PipelineEditorView,
      meta: { breadcrumb: 'Pipeline Editor', parent: 'library' },
    },
    {
      path: '/composites/:id/editor',
      name: 'composite-editor',
      component: CompositeEditorView,
      meta: { breadcrumb: 'Composite Editor', parent: 'library' },
    },
    {
      path: '/setup/model-backend/:id',
      name: 'ModelBackendSetup',
      component: ModelBackendSetupView,
      meta: { breadcrumb: 'Complete Setup' },
    },
     {
       path: '/system-maps',
       name: 'system-maps',
       component: SystemMapsView,
        meta: { visibility: 'private_preview' },
     },
     {
       path: '/system-maps/:id',
       name: 'system-map-detail',
       component: SystemMapDetailView,
       props: true,
        meta: { visibility: 'private_preview' },
     },
     {
       path: '/lifecycle-maps',
       name: 'lifecycle-maps',
       component: () => import('../views/lifecycle-map/LifecycleMapList.vue'),
       meta: { visibility: 'in_dev' },
     },
    {
      path: '/lifecycle-maps/:id',
      name: 'lifecycle-map-detail',
      component: () => import('../views/lifecycle-map/LifecycleMapView.vue'),
      meta: { visibility: 'in_dev' },
    },
    {
      path: '/lifecycle-maps/:id/editor',
      name: 'lifecycle-map-editor',
      component: () => import('../views/lifecycle-map/LifecycleMapEditorView.vue'),
      meta: { breadcrumb: 'Lifecycle Map Editor', parent: 'lifecycle-maps', visibility: 'in_dev' },
    },
    {
      path: '/lifecycle-maps/new',
      name: 'lifecycle-map-new',
      redirect: '/lifecycle-maps',
      meta: { visibility: 'in_dev' },
    },
    {
      path: '/dev/metrics',
      name: 'dev-metrics',
      component: DevMetricsView,
      meta: {
        title: 'Web Vitals',
        testid: 'dev-metrics',
        // Deliberately inline: /dev/metrics has no manifest entry, so the
        // manifest-derived system-admin gate (hydrateManifestMeta) cannot
        // cover it — this is the one intentional exception.
        requiresSystemAdmin: true,
      },
    },
    {
      // FAR-591 D5: the Runners page (CONFIGURE). Route-per-tab:
      // /admin/runners/profiles + /admin/runners/concurrency share the
      // AdminRunnersView layout (PageTabs + persistent status strip).
      // Profile create/edit keep real nested editor routes under the
      // profiles tab so legacy deep links map 1:1.
      //
      // FAR-1950: the `name` sits on the empty-path default child, NOT on the
      // parent. A named parent with an unnamed, empty-path child is Vue
      // Router's R0103 trap: `/admin/runners/profiles` resolves to the child,
      // so `to.name` was `undefined`, hydrateManifestMeta early-returned, and
      // the role/tier gate silently never ran. It also means resolving the
      // parent by name would fail to render this child.
      path: '/admin/runners/profiles',
      component: AdminRunnersView,
      children: [
        {
          path: '',
          name: 'admin-runners-profiles',
          component: RunnersProfilesTab,
        },
        {
          path: 'new',
          name: 'admin-runners-profile-new',
          component: EnvironmentProfileForm,
          meta: { breadcrumb: 'New Profile', parent: 'admin-runners-profiles' },
        },
        {
          path: ':id',
          redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}/edit` }),
        },
        {
          path: ':id/edit',
          name: 'admin-runners-profile-edit',
          component: EnvironmentProfileForm,
          meta: { breadcrumb: 'Edit Profile', parent: 'admin-runners-profiles' },
          props: true,
        },
      ],
    },
    {
      // FAR-1950: same R0103 fix as the profiles tab above — the name lives on
      // the empty-path default child.
      path: '/admin/runners/concurrency',
      component: AdminRunnersView,
      children: [
        {
          path: '',
          name: 'admin-runners-concurrency',
          component: RunnersConcurrencyTab,
        },
      ],
    },
    {
      // FAR-591 D5: the Environment Profiles surface folded into the
      // Runners page — every legacy route keeps working via redirect.
      path: '/environment-profiles',
      redirect: '/admin/runners/profiles',
    },
    {
      path: '/environment-profiles/new',
      redirect: '/admin/runners/profiles/new',
    },
    {
      path: '/environment-profiles/:id',
      redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}` }),
    },
    {
      path: '/environment-profiles/:id/edit',
      redirect: (to) => ({ path: `/admin/runners/profiles/${String(to.params.id)}/edit` }),
    },
    {
      path: '/assistant',
      name: 'assistant-only',
      component: AssistantOnlyView,
      meta: { bare: true },
    },
    {
      // FAR-1196 one-release redirects: legacy Remy paths must not 404.
      path: '/remy',
      redirect: '/assistant',
    },
    {
      path: '/settings/remy',
      redirect: '/settings/assistant',
    },
    {
      path: '/admin/remy',
      redirect: '/admin/assistant',
    },
    {
      // FAR-2664: the locked page a below-tier deep link lands on. It is the
      // refusal surface for a capability the caller cannot open, not a
      // navigable product page, so it renders no sidebar entry and needs no
      // element inventory. It IS named so the product-map guard enumerates it,
      // and the name is recorded in the backend suite's NON_PRODUCT_ROUTES —
      // the exclusion is codified rather than implicit in an unnamed route.
      path: '/locked',
      name: 'locked-route',
      component: LockedRouteView,
      meta: { title: 'Locked' },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      redirect: '/',
    },
  ],
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) {
      return { el: to.hash, behavior: 'smooth' }
    }
    return { top: 0 }
  },
})

type RouteLocationTarget = Parameters<Parameters<typeof router.beforeEach>[0]>[0]

/**
 * Resolve the manifest entry for a navigation target.
 *
 * The primary key is the route *name* (the manifest registry is name-keyed).
 * FAR-1950: a route can be a named parent with an unnamed, empty-path default
 * child (Vue Router R0103) — Vue Router then resolves the navigation to the
 * child, so `to.name` is `undefined` and a name-only lookup silently dropped
 * the gate (it never ran for /admin/runners/{profiles,concurrency}). Fall back
 * to the deepest `to.matched` record that is either named in the manifest or
 * whose full path is a manifest key, so the gate hydrates from whichever
 * record the manifest actually describes.
 */
function resolveManifestEntry(to: RouteLocationTarget): (ManifestEntry & { path: string }) | undefined {
  if (typeof to.name === 'string') {
    const byName = manifestByName.get(to.name)
    if (byName) return byName
  }
  // `to.matched` is always present on a real navigation, but the exported
  // helpers are also driven with partial route objects in unit tests — stay
  // tolerant rather than throwing (a throw is caught by the guard and would
  // redirect to the dashboard).
  const matched = to.matched ?? []
  for (let i = matched.length - 1; i >= 0; i -= 1) {
    const record = matched[i]
    if (typeof record.name === 'string') {
      const byName = manifestByName.get(record.name)
      if (byName) return byName
    }
    const byPath = manifestByPath.get(record.path)
    if (byPath) return byPath
  }
  return undefined
}

/**
 * The hydrated route meta carries a manifest-declared gate signal (role /
 * tier / system admin / permissions).
 *
 * This is the hydration-success half of the FAR-1950 fail-closed check: a
 * signal present on `to.meta` proves `hydrateManifestMeta` reached this route,
 * so the backstop must NOT fire. It deliberately mirrors
 * `entryCarriesGate`'s declared signals — including `required_permissions`,
 * which `enforceRoleTierVisibility` does not itself enforce (permissions are a
 * sidebar concern) but which `hydrateManifestMeta` copies onto `to.meta`.
 * Without it the two halves disagree: a permissions-only gate would look
 * "declared but unhydrated" and fail-closed redirect even a user holding the
 * permission.
 */
function metaCarriesGate(meta: RouteLocationTarget['meta'] | undefined): boolean {
  return Boolean(
    meta?.requiresSystemAdmin ||
      meta?.requiredRoles?.length ||
      meta?.requiredEntitlement ||
      meta?.requiredPermissions?.length ||
      meta?.visibility === 'private_preview' ||
      meta?.visibility === 'in_dev',
  )
}

/**
 * The manifest entry *declares* a gate. Mirrors the signals
 * `hydrateManifestMeta` reads: role, tier, permissions, route-level
 * `system_admin_only`, or a system-admin sidebar group. Deliberately NOT the
 * hydrated `to.meta` — this is the manifest-side half of the FAR-1950
 * fail-closed check.
 */
export function entryCarriesGate(entry: ManifestEntry | undefined): boolean {
  if (!entry) return false
  const groupId = entry.sidebar_group
  return Boolean(
    entry.required_entitlement ||
      entry.required_roles?.length ||
      entry.required_permissions?.length ||
      entry.visibility === 'private_preview' ||
      entry.visibility === 'in_dev' ||
      entry.system_admin_only ||
      (groupId && manifestSidebarGroups[groupId]?.system_admin_only),
  )
}

/**
 * The manifest path a navigation corresponds to, derived independently of
 * `resolveManifestEntry`: by `to.path` / the matched route record paths — the
 * raw path index, never a name lookup. Used by the fail-closed backstop.
 */
function manifestPathForRoute(to: RouteLocationTarget): string | undefined {
  const matched = to.matched ?? []
  for (let i = matched.length - 1; i >= 0; i -= 1) {
    const recordPath = matched[i].path
    if (manifestByPath.has(recordPath)) return recordPath
  }
  if (manifestByPath.has(to.path)) return to.path
  return undefined
}

/**
 * FAR-1950: fail closed when the manifest declares a gate for this route but
 * hydration left `to.meta` ungated — the silent-fail-open shape that let
 * /admin/runners/{profiles,concurrency} render without enforcing anything.
 *
 * Returns the manifest path that should have been gated, or `undefined` when
 * the navigation is consistent (either the meta is already gated, or the
 * manifest declares no gate for this path). The caller warns and redirects;
 * this is the structural form of "fail closed, log it".
 */
export function findUnhydratedGatedManifestPath(to: RouteLocationTarget): string | undefined {
  if (metaCarriesGate(to.meta)) return undefined
  const manifestPath = manifestPathForRoute(to)
  if (!manifestPath) return undefined
  return entryCarriesGate(manifestRoutes[manifestPath]) ? manifestPath : undefined
}

/**
 * Hydrate to.meta fields from the manifest registry so downstream guards
 * and breadcrumb logic see a single canonical source. Manifest entries are
 * keyed by route *name*; the parent path is resolved to a route name via
 * manifestPathToName. Routes that resolve to an unnamed child are resolved
 * from `to.matched` (FAR-1950 — see `resolveManifestEntry`).
 */
export function hydrateManifestMeta(to: RouteLocationTarget): void {
  const entry = resolveManifestEntry(to)
  if (!entry) return

  to.meta.breadcrumb = entry.breadcrumb
  to.meta.testid = entry.testid
  to.meta.requiredRoles = entry.required_roles ?? undefined
  to.meta.requiredEntitlement = entry.required_entitlement ?? undefined
  to.meta.requiredPermissions = entry.required_permissions ?? undefined
  to.meta.featureFlag = entry.feature_flag ?? undefined
  to.meta.visibility = entry.visibility ?? undefined
  to.meta.parent = entry.parent
    ? (manifestPathToName.get(entry.parent) ?? entry.parent)
    : undefined

  // FAR-938: derive requiresSystemAdmin from the manifest so the router gate
  // can never drift from it. A route is gated when EITHER its own route entry
  // carries `system_admin_only: true` OR its sidebar group has
  // `system_admin_only: true` — the two signals OR together.
  // NOTE: the SIDEBAR filter (config/navigation.ts) honours BOTH signals too
  // (FAR-1796), so the router gate and the sidebar cannot disagree.
  if (entry.path) {
    const routeEntry = manifestRoutes[entry.path]
    const groupId = routeEntry?.sidebar_group
    if (routeEntry?.system_admin_only || (groupId && manifestSidebarGroups[groupId]?.system_admin_only)) {
      to.meta.requiresSystemAdmin = true
    }
  }
}

/**
 * Resolve whether the current user holds at least one of the route's
 * manifest-declared permissions.  Extracted from the navigation guard to keep
 * its cognitive complexity within the SonarCloud budget.  Routes with no
 * declared permissions pass without fetching anything.
 */
async function hasRequiredPermissions(
  to: Parameters<Parameters<typeof router.beforeEach>[0]>[0],
): Promise<boolean> {
  const requiredPermissions = to.meta?.requiredPermissions
  if (!requiredPermissions?.length) return true
  const planStore = usePlanStore()
  await planStore.fetchPermissions()
  return requiredPermissions.some((permission) => planStore.userPermissions.includes(permission))
}

/** The path of the locked page (FAR-2664) — see the route registration. */
export const LOCKED_ROUTE_PATH = '/locked'

/**
 * A navigation target for a request the caller may not open *yet* (FAR-2664).
 * Carries everything the locked page needs to name the capability in the
 * caller's terms: where they were going, what it is called, and which licence
 * it needs. `reason` distinguishes a paid capability (an upgrade fixes it)
 * from a role / instance-admin refusal (it does not).
 */
export interface LockedRouteTarget {
  path: typeof LOCKED_ROUTE_PATH
  query: Record<string, string>
}

/** What a guard hands back when it refuses a navigation. */
export type GuardDenial = { name: string } | LockedRouteTarget

/**
 * FAR-2664 / FAR-2665: a route outside the licence is a commercial capability
 * the caller cannot use — not a broken application. Land on the locked page,
 * which names the capability and offers the upgrade path, instead of bouncing
 * silently to the dashboard with no explanation. The query carries the
 * ENTITLEMENT the route needs (never a tier string).
 *
 * FAR-2687: the same applies to a role refusal — the page names the role(s)
 * that would open the capability. The reason is what selects which explanation
 * the locked page shows; the `role` query is only meaningful for `'role'`, so a
 * below-tier lock keeps exactly the URL it had before.
 *
 * The capability label comes from the manifest breadcrumb (the same string the
 * breadcrumb bar and document title use); the view upgrades it to the
 * translated sidebar label when it can resolve one.
 */
export function lockedRouteFor(
  to: Parameters<Parameters<typeof router.beforeEach>[0]>[0],
  reason: 'entitlement' | 'role' | 'system_admin' = 'entitlement',
): LockedRouteTarget {
  const entry = resolveManifestEntry(to)
  const query: Record<string, string> = { from: to.fullPath || to.path || '/' }
  const capability = entry?.breadcrumb
  if (capability) query.capability = capability
  const entitlement = to.meta?.requiredEntitlement ?? entry?.required_entitlement
  if (entitlement) query.entitlement = entitlement
  if (reason === 'role') {
    // The roles that would grant access (the manifest list is any-of). Carried
    // as a comma-separated key list so the view can translate each one; omit it
    // entirely when the entry declares no roles, and the view falls back to the
    // generic wording.
    const requiredRoles = to.meta?.requiredRoles ?? entry?.required_roles
    if (requiredRoles?.length) query.role = requiredRoles.join(',')
  }
  query.reason = reason
  return { path: LOCKED_ROUTE_PATH, query }
}

/**
 * The plan store when a Pinia instance is active, else null.
 *
 * Production installs Pinia (`app.use(pinia)`) before `app.use(router)`, so the
 * guard always has a store. A handful of spec files drive this same real guard
 * callback WITHOUT installing Pinia (they exercise the manifest-binding
 * backstop, not navigation state); returning null lets the pure
 * manifest-backstop assertions run instead of throwing a "no active Pinia"
 * error into the guard's catch-all, which would bounce every navigation to the
 * dashboard.
 */
function activePlanStore(): ReturnType<typeof usePlanStore> | null {
  const pinia = getActivePinia()
  return pinia ? usePlanStore(pinia) : null
}

/**
 * Enforce role, ENTITLEMENT and devMode/visibility access rules.  Returns a
 * redirect/locked route-object when access is denied, or `true` when the
 * caller may continue.  The plan store is lazily fetched so the guard sees the
 * real licence grant — the first navigation resolves *before*
 * AppLayout.onMounted kicks off `fetchPlan()`.
 */
export async function enforceRoleTierVisibility(
  to: Parameters<Parameters<typeof router.beforeEach>[0]>[0],
  token: string,
): Promise<GuardDenial | true> {
  const payload = decodeJwtPayload(token)

  if (to.meta?.requiresSystemAdmin && !payload?.is_system_admin) {
    // FAR-2688: an instance-admin refusal is not a broken application — land
    // on the locked page, which names the instance-level restriction and the
    // one person who can lift it, instead of a silent bounce to the Dashboard.
    // The locked page carries no gate of its own (it is not in the manifest),
    // so this cannot loop. The copy and the view branch already existed
    // (FAR-2664); only this denial was still returning the Dashboard.
    return lockedRouteFor(to, 'system_admin')
  }

  if (!(await hasRequiredPermissions(to))) {
    // FAR-2688 (deliberate): a permission refusal keeps the Dashboard bounce.
    // Unlike role / instance-admin / tier — all decided from OBSERVED token
    // claims or a resolved tier — the permission list is resolved
    // asynchronously and FAILS CLOSED: `fetchPermissions` sets
    // `permissionsSource='unavailable'` with an empty list on a 4xx/5xx/
    // timeout, which is indistinguishable here from a caller who genuinely
    // holds none (stores/planStore.ts, FAR-2020). Explaining "your role cannot
    // open this" would therefore assert a role change is needed during a
    // transient fetch failure, when a retry is the actual fix. An explanation
    // needs the unknown state separated from the observed one first; until
    // then a silent bounce states nothing false.
    return { name: 'dashboard' }
  }

  if (to.meta?.requiredRoles?.length) {
    const orgRole = payload?.org_role
    if (typeof orgRole !== 'string' || !to.meta.requiredRoles.includes(orgRole)) {
      // FAR-2687: a role denial is not a broken application, exactly like a
      // below-tier one — land on the locked page, which names the role the
      // caller needs and the administrator who can grant it, instead of a
      // silent bounce to the Dashboard. The locked page carries no role gate
      // of its own (it is not in the manifest), so this cannot loop.
      return lockedRouteFor(to, 'role')
    }
  }

  const needsEntitlementOrVisibility =
    // Boolean OR is deliberate: a falsy-but-present requiredEntitlement must
    // still trigger the guard rather than fail open via `??`.
    Boolean(to.meta?.requiredEntitlement) || // NOSONAR: fail closed on any falsy non-null requiredEntitlement
    (to.meta?.visibility === 'private_preview' || to.meta?.visibility === 'in_dev')

  if (needsEntitlementOrVisibility) {
    const planStore = usePlanStore()
    if (!planStore.loaded) {
      await planStore.fetchPlan()
    }
    if (
      to.meta?.requiredEntitlement &&
      // Entitlement check keyed on PLAN KNOWLEDGE, not flag-map emptiness
      // (FAR-1809 / FAR-2665). The two distinct states:
      //   - KNOWN: a payload applied, so the licence answer is observed truth.
      //     `loaded` is set together with the plan context, so a resolved
      //     UNENTITLED org must still be refused — an empty flag map says
      //     nothing about the grant.
      //   - UNKNOWN: no server payload has ever applied (fetch failed ->
      //     flagsSource 'unavailable', `loaded` stays false). Refusing on the
      //     bootstrap default would bounce licensed users to the locked page on
      //     a transient /plan failure — the exact wrong-tier defect FAR-1809
      //     removed. Fail open here; the backend 402 still enforces the real
      //     licence on the gated API routes.
      // The devMode/visibility branch below is deliberately NOT flipped: a
      // private_preview surface stays hidden when dev mode is unknown, which
      // is the safe default for a hidden surface.
      planStore.loaded &&
      !planStore.hasEntitlement(to.meta.requiredEntitlement)
    ) {
      // FAR-2664: the caller is otherwise permitted — this route is simply
      // outside their licence. Show the capability and the upgrade path instead
      // of a silent dashboard bounce. The redirect stays for the genuinely
      // unknown route, and for the role / instance-admin refusals above (a
      // licence upgrade cannot fix those).
      return lockedRouteFor(to)
    }
    const isPrivateOrDev =
      to.meta?.visibility === 'private_preview' || to.meta?.visibility === 'in_dev'
    if (isPrivateOrDev && !planStore.devMode) {
      // FAR-2248: a hidden preview surface bounces to the dashboard by design
      // (FAR-542 MVP cut), but the bounce used to be fully silent and so
      // indistinguishable from a bug. Name the cause in the console.
      console.warn(
        `[router] ${String(to.name)} is a ${String(to.meta?.visibility)} surface and dev mode is off; redirecting to the dashboard`,
      )
      return { name: 'dashboard' }
    }
  }

  return true
}

// Note: the phantom `variant_batch_compare` feature flag was removed in FAR-926.

/**
 * Enforce the manifest-declared role / tier / visibility gate, or fail closed
 * when the manifest declares a gate for the resolved path but hydration left
 * `to.meta` ungated (FAR-1950). Extracted from the navigation guard to keep its
 * cognitive complexity within the SonarCloud budget.
 */
async function enforceManifestGate(
  to: RouteLocationTarget,
  token: string,
): Promise<GuardDenial | true> {
  if (
    to.meta?.requiresSystemAdmin ||
    to.meta?.requiredRoles?.length ||
    to.meta?.requiredPermissions?.length ||
    to.meta?.requiredEntitlement ||
    to.meta?.visibility === 'private_preview' ||
    to.meta?.visibility === 'in_dev'
  ) {
    return enforceRoleTierVisibility(to, token)
  }
  // FAR-1950: the manifest declares a gate for this path but hydration left
  // to.meta ungated — the gate would silently never run, which is how
  // /admin/runners/{profiles,concurrency} rendered ungated for any caller.
  // Fail closed (dashboard) and say so, rather than rendering the surface.
  const unhydratedGatedPath = findUnhydratedGatedManifestPath(to)
  if (unhydratedGatedPath) {
    console.warn(
      `[router] manifest gate for ${unhydratedGatedPath} was not hydrated onto the resolved route (name=${String(to.name)}); failing closed to the dashboard`,
    )
    return { name: 'dashboard' }
  }
  return true
}

/**
 * Enforce a manifest-declared feature flag (FAR-656): a route whose manifest
 * entry carries feature_flag is reachable only while that flag resolves true —
 * a disabled flag redirects to the dashboard. navigation.ts filters the sidebar
 * item on the same flag so nav and route stay consistent.
 */
async function enforceRouteFeatureFlag(
  to: RouteLocationTarget,
): Promise<GuardDenial | true> {
  if (!to.meta?.featureFlag) return true
  const planStore = usePlanStore()
  if (!planStore.loaded) {
    await planStore.fetchPlan()
  }
  if (!planStore.featureEnabled(to.meta.featureFlag)) {
    // FAR-2688 (deliberate): a disabled feature flag keeps the Dashboard
    // bounce, and says so here rather than inventing copy. `featureEnabled` is
    // false BOTH when the flag is genuinely off and when the plan payload
    // never resolved (it reads `orgOverrides[flag] ?? features[flag] ??
    // false`), so the two states are indistinguishable at this point and any
    // user-facing explanation could be false during a transient plan failure.
    // The only flag-gated route (`/admin/notification-delivery`) is filtered
    // out of the nav while its flag is off, so this is reachable only by a
    // stale deep link; and no role, purchase or request can grant a flag, so
    // none of the locked page's three reasons (tier / role / system_admin)
    // would be true. Explaining it needs an "unknown plan" state separated
    // from "flag off" first — a separate change, not a generic mechanism here.
    return { name: 'dashboard' }
  }
  return true
}

/**
 * FAR-2594: the fullPath of the navigation currently being resolved.
 *
 * A failed lazy route-chunk fetch ABORTS the navigation, so `currentRoute` is
 * still the page the user is looking at. Recovery has to retry the
 * DESTINATION, which is only known from the attempted navigation.
 */
let _chunkRetryTarget: string | null = null

/**
 * Record the navigation being resolved so a chunk error can retry ITS
 * destination. Exported (with the two helpers below) so the recovery contract
 * is pinned by `__tests__/routerChunkRecovery.spec.ts`: the retry budget is
 * module state, and these are its only mutators.
 */
export function noteNavigationStarted(to: { fullPath: string }): void {
  _chunkRetryTarget = to.fullPath
}

router.beforeEach(async (to) => {
  noteNavigationStarted(to)
  try {
    // FAR-535 demo auto-login: /demo never renders as a page. The guard owns
    // the hand-off so it runs pre-mount — main.ts awaits router.isReady()
    // BEFORE App mounts, so an anonymous visitor hitting /demo lands straight
    // on the dashboard with the demo session already stored (App.vue's
    // unauthenticated LoginView branch never has a chance to flash).
    // qa iter 1: a LIVE session must never be torn down by merely visiting
    // /demo — previously every navigation re-ran the hand-off, so
    // Back/Forward to /demo logged a real user out, raced the auto-login
    // recovery listener, and re-minted a token (burning the 10/hour budget).
    // With a token present (demo flag set or a real session) the visitor goes
    // straight to the dashboard; only a tokenless browser runs the hand-off.
    // qa iter 2: the rule lives in resolveDemoEntry (lib/api/demo.ts) so the
    // guard and DemoView's fallback cannot drift.
    if (to.name === 'demo') {
      return { name: await resolveDemoEntry() }
    }

    hydrateManifestMeta(to)

    const token = getAccessToken()
    if (to.meta?.public) return true
    if ((to.name === 'login' || to.name === 'org-login') && token) return { name: 'dashboard' }
    if (to.name === 'login' || to.name === 'org-login') return true // login page without token — allowed
    if (!token) return { name: 'login' }

    // FAR-2665: the plan/flags payload must resolve before the FIRST
    // authenticated navigation completes when there is no persisted layout to
    // trust. Dropping the universal community tier gate from ordinary routes
    // means the per-route gate no longer fetches the payload for them, so
    // without this `main.ts` would mount AppLayout before `/api/v1/plan` landed
    // and paint nav chrome against an unresolved flag map (the FAR-1237
    // mobile-nav flash: `mobile-nav-flash.spec.ts` asserts NOTHING paints while
    // the flags are unknown). The gate is deliberately keyed on
    // `flagsSource === 'none'` — no cache, no server payload — rather than
    // `!loaded`: a CACHE hit already knows the layout from the persisted map,
    // and blocking it on a slow server read would regress the cached
    // first-paint the same suite pins. The fetch never rejects
    // (`settlePlanSource` swallows), so a failed read settles to the
    // `unavailable` drawer fallback instead of hanging the navigation.
    // Entitlement-gated routes still fetch in `enforceRoleTierVisibility` when
    // the grant is needed, so this is additive, never a replacement.
    const planStore = activePlanStore()
    if (planStore && planStore.flagsSource === 'none') {
      await planStore.fetchPlan()
    }

    // Role / tier / visibility enforcement, plus the FAR-1950 fail-closed
    // backstop for a manifest-gated path that hydration did not reach.
    const denied = await enforceManifestGate(to, token)
    if (denied !== true) return denied

    const flagDenied = await enforceRouteFeatureFlag(to)
    if (flagDenied !== true) return flagDenied

  } catch (err) {
    console.error('[router] navigation guard error:', err)
    return { name: 'dashboard' }
  }
})

/**
 * FAR-2594: a route's lazy chunk can be missing even though the SPA itself is
 * running. Every deploy replaces the hashed asset filenames, and a tab left
 * open across one keeps the OLD index.html in memory (index.html is served
 * `no-cache`, but the SPA never re-fetches it until a document load), so any
 * route the user had not visited yet requests a chunk that now 404s. The
 * failure surfaces only as a console error and the click looks like it did
 * nothing, which is exactly how "none of the buttons on the Costs page work"
 * was reported (FAR-2594).
 *
 * The previous recovery retried `router.currentRoute` — the page the user is
 * already on — which cannot retry the failed chunk at all, and the retry's
 * own `afterEach` reset the counter, so `window.location.reload()` was
 * unreachable and the app stayed wedged for the life of the tab.
 */
const CHUNK_LOAD_ERROR =
  /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically|ChunkLoadError/i

/** In-app retries before falling back to a hard document load. */
const CHUNK_RETRY_LIMIT = 1

/**
 * FAR-2594: the retry budget above is module state, so it is wiped by every
 * document load. If the destination's chunk is missing in the NEW bundle too
 * (a broken deploy, a CDN outage), each new document would boot at the
 * destination, retry once and assign again — an unbounded reload loop. One
 * marker per destination, in `sessionStorage` (it survives a same-tab document
 * load and dies with the tab), bounds that to a single reload.
 */
const CHUNK_RECOVERY_STORAGE_PREFIX = 'modulo.chunk-recovery:'
const DASHBOARD_ROUTE_NAME = 'dashboard'

let _chunkRetryCount = 0

/** True when this tab already spent its one document reload for *target*. */
export function chunkRecoveryAlreadyUsed(target: string): boolean {
  try {
    return sessionStorage.getItem(CHUNK_RECOVERY_STORAGE_PREFIX + target) === '1'
  } catch (err) {
    // Storage can be unavailable (sandboxed frame, storage disabled). This is
    // a best-effort storm guard, so fail open with a log rather than block the
    // recovery: the alternative is no recovery at all.
    console.warn('[router] chunk-recovery guard unavailable', err)
    return false
  }
}

function markChunkRecoveryUsed(target: string): void {
  try {
    sessionStorage.setItem(CHUNK_RECOVERY_STORAGE_PREFIX + target, '1')
  } catch (err) {
    console.warn('[router] could not record the chunk-recovery guard', err)
  }
}

/**
 * Drop every guard for this tab. A navigation that actually landed proves the
 * chunk map is usable again, so a later, unrelated deploy must be able to
 * recover instead of inheriting a spent marker.
 */
function clearChunkRecoveryGuards(): void {
  try {
    for (let i = sessionStorage.length - 1; i >= 0; i -= 1) {
      const key = sessionStorage.key(i)
      if (key?.startsWith(CHUNK_RECOVERY_STORAGE_PREFIX)) {
        sessionStorage.removeItem(key)
      }
    }
  } catch (err) {
    console.warn('[router] could not clear the chunk-recovery guard', err)
  }
}

/**
 * A navigation that actually LANDED proves the chunks are healthy again, so
 * the retry budget starts fresh. A failed (or duplicated) navigation must NOT
 * refill it: that is what made the hard-load fallback unreachable.
 */
export function noteNavigationSettled(failure?: unknown): void {
  if (!failure) {
    _chunkRetryCount = 0
    clearChunkRecoveryGuards()
  }
}

/** Tell the user, visibly, that the safe dashboard fallback is being taken. */
function reportChunkRecoveryFailure(target: string): void {
  const message = i18n.global.t('components.Router.chunk_recovery_failed')
  console.error(`[router] ${message}`, { target })
  try {
    window.alert(message)
  } catch (err) {
    console.warn('[router] could not show the chunk-recovery message', err)
  }
}

/**
 * The router's `onError` handler: recover a navigation whose route chunk is
 * gone (FAR-2594). Exported so `__tests__/routerChunkRecovery.spec.ts` can
 * drive it directly — `router.onError` keeps its listeners private, so a
 * navigation-based test would have to mount the whole app graph to reach it.
 *
 * `assign` is the handler's single side-effecting boundary (a full document
 * navigation) and is injectable because jsdom's `window.location` is
 * non-configurable, so a test cannot observe it otherwise. Production never
 * passes it: the default IS `window.location.assign`.
 */
export function handleRouteChunkError(
  err: unknown,
  assign: (target: string) => void = (target) => window.location.assign(target),
): void {
  console.error('[router] navigation error:', err)
  if (!CHUNK_LOAD_ERROR.test(formatApiError(err))) return

  // No target means no navigation has started yet (the module is still
  // initialising, or the error came from outside a navigation); there is
  // nothing to retry.
  const target = _chunkRetryTarget
  if (!target) return

  if (_chunkRetryCount < CHUNK_RETRY_LIMIT) {
    _chunkRetryCount += 1
    // Retry the DESTINATION, never `router.currentRoute` (see above).
    router.replace(target).catch(() => {})
    return
  }

  if (chunkRecoveryAlreadyUsed(target)) {
    // This tab already reloaded for this destination and the freshly loaded
    // bundle is missing the same chunk: assigning again would loop forever.
    // Say so, and send the user to a route that does load.
    const dashboardPath = router.resolve({ name: DASHBOARD_ROUTE_NAME }).fullPath
    reportChunkRecoveryFailure(target)

    // Terminal state. If the failing destination IS the dashboard, or the
    // dashboard fallback was already reloaded once and is still missing, there
    // is no route left that loads: stop rather than re-enter the reload loop
    // with a blocking alert on every document load.
    if (dashboardPath === target || chunkRecoveryAlreadyUsed(dashboardPath)) {
      return
    }

    // Bound the fallback too: mark the dashboard as spent before reloading it,
    // so a missing dashboard chunk terminates on the next document instead of
    // reloading it again.
    markChunkRecoveryUsed(dashboardPath)
    assign(dashboardPath)
    return
  }

  // Budget exhausted: this document's chunk map is stale. Only a full load
  // picks up the current index.html and its chunk filenames; `reload()` would
  // drop the user back on the page they are already stuck on.
  markChunkRecoveryUsed(target)
  assign(target)
}

router.afterEach((to, _from, failure) => {
  noteNavigationSettled(failure)
  document.title = documentTitleFor(to.meta)
})
// Vue Router calls a listener as (error, to, from); only the error is ours —
// passing `to` through as the second argument would land in `assign`.
router.onError((err) => handleRouteChunkError(err))

export default router
