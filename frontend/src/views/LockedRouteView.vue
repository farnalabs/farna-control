<template>
  <div class="page-narrow" data-testid="locked-route">
    <PageHeader :title="heading" :subtitle="subtitle" data-testid="locked-route-header" />

    <!-- A commercial capability: an upgrade fixes it, so reuse the
         entitlement gate (the same lock Org Settings / My Profile / schema
         history show) rather than inventing a second lock pattern. FAR-2665:
         the gate is driven by the entitlement the router named, read from the
         plan payload's registry. -->
    <FeatureGate
      v-if="isEntitlementLock"
      class="mt-6"
      :required-entitlement="requiredEntitlement || undefined"
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
        {{ refusalBody }}
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
 *   from         the page the caller was trying to open
 *   capability   the manifest breadcrumb for it (display fallback)
 *   entitlement  the capability it needs (commercial locks only)
 *   role         the role key(s) that would open it, comma-separated (FAR-2687,
 *                role locks only)
 *   reason       'entitlement' (default) | 'role' | 'system_admin'
 *
 * Naming the capability is the point: "Costs requires the Cost governance
 * capability" tells the caller they hit a commercial gate, where the old
 * silent bounce to the Dashboard read as a broken application. A role refusal
 * names the role for the same reason (FAR-2687): "Your role cannot open Users"
 * alone does not tell them what to ask for.
 */
const route = useRoute()
const { t } = useI18n()
const planStore = usePlanStore()

const reason = computed<'entitlement' | 'role' | 'system_admin'>(() => {
  const raw = route.query.reason
  if (raw === 'role' || raw === 'system_admin') return raw
  return 'entitlement'
})

const isEntitlementLock = computed(() => reason.value === 'entitlement')

// A role refusal is fixable by the organisation; an instance-admin restriction
// is not, so the body must name the right person (FAR-2664 review).
//
// FAR-2687: a role refusal also names WHICH role would open the page, because
// "ask for the role that includes it" leaves the caller to guess. The role keys
// arrive comma-separated from the guard; an unknown key is shown verbatim
// rather than dropped, so a new manifest role still reads sensibly.
const ROLE_LABEL_KEYS: Record<string, string> = {
  admin: 'views.LockedRoute.role_admin',
  operator: 'views.LockedRoute.role_operator',
  runner: 'views.LockedRoute.role_runner',
  viewer: 'views.LockedRoute.role_viewer',
}

const requiredRoleLabels = computed(() => {
  const raw = route.query.role
  const value = typeof raw === 'string' ? raw : ''
  const keys = value
    .split(',')
    .map((role) => role.trim())
    .filter(Boolean)
  return keys.map((role) => {
    const labelKey = ROLE_LABEL_KEYS[role]
    return labelKey ? t(labelKey) : role
  })
})

const refusalBody = computed(() => {
  if (reason.value === 'system_admin') return t('views.LockedRoute.system_admin_body')
  const roles = requiredRoleLabels.value
  if (reason.value === 'role' && roles.length === 1) {
    return t('views.LockedRoute.role_body_required', { roles: roles[0] })
  }
  if (reason.value === 'role' && roles.length > 1) {
    return t('views.LockedRoute.role_body_required_any', { roles: roles.join(', ') })
  }
  return t('views.LockedRoute.role_body')
})

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

const requiredEntitlement = computed(() => {
  const raw = route.query.entitlement
  return typeof raw === 'string' ? raw : ''
})

const entitlementLabel = computed(() =>
  requiredEntitlement.value
    ? planStore.getEntitlementLabel(requiredEntitlement.value)
    : '',
)

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
  if (!entitlementLabel.value) {
    return t('views.LockedRoute.entitlement_heading_unknown', { capability: capabilityLabel.value })
  }
  return t('views.LockedRoute.entitlement_heading', {
    capability: capabilityLabel.value,
    entitlement: entitlementLabel.value,
  })
})

const subtitle = computed(() => {
  if (reason.value === 'role') return t('views.LockedRoute.role_subtitle')
  if (reason.value === 'system_admin') return t('views.LockedRoute.system_admin_subtitle')
  if (!entitlementLabel.value) return t('views.LockedRoute.entitlement_subtitle_unknown')
  return t('views.LockedRoute.entitlement_subtitle', { entitlement: entitlementLabel.value })
})
</script>
