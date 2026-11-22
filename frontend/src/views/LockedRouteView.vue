<template>
  <div class="page-narrow" data-testid="locked-route">
    <PageHeader :title="heading" :subtitle="subtitle" data-testid="locked-route-header" />

    <!-- A paid capability: an upgrade fixes it, so reuse the entitlement gate
         (the same lock Org Settings / My Profile / schema history show) rather
         than inventing a second lock pattern. The gate renders the "Team
         Feature" lock and its View Plans call to action. -->
    <FeatureGate
      v-if="isTierLock"
      class="mt-6"
      :feature-name="FEATURE_NAME"
      :required-tier="requiredTier || undefined"
    >
      <!-- Reached when the licence now covers the capability (an upgrade
           landed and this page was reloaded, which refetches the plan). -->
      <div class="card p-6 text-center" data-testid="locked-route-available">
        <p class="text-sm text-muted-foreground">
          {{ $t('views.LockedRoute.available_body', { capability: capabilityLabel }) }}
        </p>
        <router-link
          v-if="fromPath"
          :to="fromPath"
          class="mt-3 inline-block text-sm font-semibold text-primary-text hover:underline"
          data-testid="locked-route-open"
        >
          {{ $t('views.LockedRoute.open_capability', { capability: capabilityLabel }) }}
        </router-link>
      </div>
    </FeatureGate>

    <!-- A role / instance-admin refusal: no purchase can fix it, so say who
         can and offer the way back rather than a plan upgrade. -->
    <div v-else class="card mt-6 p-6" data-testid="locked-route-closed">
      <p class="text-sm text-muted-foreground">
        {{ $t(refusalBodyKey) }}
      </p>
    </div>

    <!-- The way back belongs on every branch: a tier lock offers the upgrade
         CTA but still leaves the caller somewhere to go. -->
    <router-link
      to="/"
      class="mt-4 inline-block text-sm font-semibold text-primary-text hover:underline"
      data-testid="locked-route-dashboard"
    >
      {{ $t('views.LockedRoute.back_to_dashboard') }}
    </router-link>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import PageHeader from '../components/shared/PageHeader.vue'
import FeatureGate from '../components/FeatureGate.vue'
import { getNavGroups } from '../config/navigation'
import { usePlanStore } from '../stores/planStore'

/**
 * FAR-2664: the page a below-tier deep link lands on.
 *
 * The router guard (`lockedRouteFor`) and a view that knowingly links to a
 * capability the caller cannot reach both target this page with a query:
 *
 *   from        the page the caller was trying to open
 *   capability  the manifest breadcrumb for it (display fallback)
 *   tier        the licence it needs (tier locks only)
 *   reason      'tier' (default) | 'role' | 'system_admin'
 *
 * Naming the capability is the point: "Costs requires the Team licence"
 * tells the caller they hit a paid capability, where the old silent bounce to
 * the Dashboard read as a broken application.
 */
const route = useRoute()
const { t } = useI18n()
const planStore = usePlanStore()

// FeatureGate requires a flag name; there is no per-capability flag here, so
// the gate is driven entirely by `required-tier`.
const FEATURE_NAME = 'locked_route'

const reason = computed<'tier' | 'role' | 'system_admin'>(() => {
  const raw = route.query.reason
  if (raw === 'role' || raw === 'system_admin') return raw
  return 'tier'
})

const isTierLock = computed(() => reason.value === 'tier')

// A role refusal is fixable by the organisation; an instance-admin restriction
// is not, so the body must name the right person (FAR-2664 review).
const refusalBodyKey = computed(() =>
  reason.value === 'system_admin'
    ? 'views.LockedRoute.system_admin_body'
    : 'views.LockedRoute.role_body',
)

/**
 * Only an in-app absolute path is a safe navigation target. `//host` is a
 * protocol-relative URL, a leading backslash is normalised to one by browsers,
 * and a tab/newline inside a URL is stripped before parsing (`/\t/evil` would
 * become `//evil`), so all of those are refused. An empty value simply hides
 * the "open" affordance.
 */
const SAFE_IN_APP_PATH = /^\/(?![/\\])[^\s\\]*$/

const fromPath = computed(() => {
  const raw = route.query.from
  const value = typeof raw === 'string' ? raw : ''
  return SAFE_IN_APP_PATH.test(value) ? value : ''
})

const requiredTier = computed(() => {
  const raw = route.query.tier
  return typeof raw === 'string' ? raw : ''
})

const tierLabel = computed(() =>
  requiredTier.value ? planStore.getTierLabel(requiredTier.value) : '',
)

const currentTierLabel = computed(() => planStore.getTierLabel(planStore.currentTier))

/**
 * The capability in the caller's terms: the translated sidebar label for the
 * attempted page (the name they clicked in the nav), else the manifest
 * breadcrumb the guard passed, else a generic noun.
 */
const capabilityLabel = computed(() => {
  const sourcePath = fromPath.value.split(/[?#]/, 1)[0]
  if (sourcePath) {
    const item = getNavGroups()
      .flatMap((group) => group.items)
      .find((navItem) => navItem.to === sourcePath)
    if (item) return t(item.labelKey)
  }
  const raw = route.query.capability
  if (typeof raw === 'string' && raw) return raw
  return t('views.LockedRoute.this_page')
})

const heading = computed(() => {
  if (reason.value === 'role') {
    return t('views.LockedRoute.role_heading', { capability: capabilityLabel.value })
  }
  if (reason.value === 'system_admin') {
    return t('views.LockedRoute.system_admin_heading', { capability: capabilityLabel.value })
  }
  if (!tierLabel.value) {
    return t('views.LockedRoute.tier_heading_unknown', { capability: capabilityLabel.value })
  }
  return t('views.LockedRoute.tier_heading', {
    capability: capabilityLabel.value,
    tier: tierLabel.value,
  })
})

const subtitle = computed(() => {
  if (reason.value === 'role') return t('views.LockedRoute.role_subtitle')
  if (reason.value === 'system_admin') return t('views.LockedRoute.system_admin_subtitle')
  if (!tierLabel.value) return t('views.LockedRoute.tier_subtitle_unknown')
  return t('views.LockedRoute.tier_subtitle', { currentTier: currentTierLabel.value })
})
</script>
