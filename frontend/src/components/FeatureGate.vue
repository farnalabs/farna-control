<template>
  <div class="relative" data-testid="feature-gate">
    <slot v-if="enabled" />

    <div
      v-else-if="showDisabled"
      class="relative min-h-[300px]"
      data-testid="feature-gate-disabled"
    >
      <div class="pointer-events-none select-none opacity-40">
        <slot />
      </div>
      <div class="absolute inset-0 flex items-start justify-center pt-8">
        <div
          class="mx-4 rounded-lg border border-warning/30 bg-background/95 p-4 text-center shadow-lg backdrop-blur-sm"
        >
          <p class="text-sm font-medium text-warning-text" data-testid="feature-gate-title">
            {{ gateTitle }}
          </p>
          <p class="mt-1 text-xs text-muted-foreground">{{ tooltipText }}</p>
          <!-- No pricing link for a pure behaviour flag (FAR-1283): no
               entitlement covers it, so no plan upgrade can enable it and a
               "View Plans" call to action would send the operator somewhere
               that cannot fix the flag. FAR-2665: this is now derived from the
               plan payload's entitlement registry, not a per-view tier prop. -->
          <a
            v-if="isCommercialGate"
            :href="pricingUrl"
            target="_blank"
            rel="noopener noreferrer"
            class="mt-2 inline-block text-xs font-semibold text-primary-text hover:underline"
          >
            {{ $t('components.FeatureGate.view_plans') }}
          </a>
          <slot name="locked" :tooltip="tooltipText" />
        </div>
      </div>
    </div>

    <div
      v-else
      class="flex items-center justify-center py-16"
      data-testid="feature-gate-lock"
    >
      <div class="text-center space-y-4">
        <LockIcon :locked="true" :tooltip="tooltipText" />
        <div>
          <h3 class="text-lg font-semibold" data-testid="feature-gate-title">{{ gateTitle }}</h3>
          <p class="text-sm text-muted-foreground">{{ tooltipText }}</p>
        </div>
        <Button
          v-if="isCommercialGate"
          as="a"
          :href="pricingUrl"
          target="_blank"
          rel="noopener noreferrer"
          class="border-primary/30 hover:border-primary/60"
        >
          {{ $t('components.FeatureGate.view_plans') }}
        </Button>
        <slot name="locked" :tooltip="tooltipText" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import Button from 'primevue/button'
import { useI18n } from "vue-i18n";
import { usePlanStore } from "../stores/planStore";
import LockIcon from "./LockIcon.vue";

const { t } = useI18n();

const props = withDefaults(defineProps<{
  /**
   * The behaviour flag to gate. FAR-2665: a flag is behaviour only — when the
   * plan payload maps it to an entitlement, the lock copy becomes the
   * commercial one; otherwise it is an organisation-disabled behaviour flag.
   */
  featureName?: string;
  /**
   * Gate on a commercial entitlement directly. Used where there is no single
   * flag for the capability (the locked page naming a route's entitlement).
   */
  requiredEntitlement?: string;
  showDisabled?: boolean;
  pricingUrl?: string;
}>(), {
  pricingUrl: "/settings/license",
});

const planStore = usePlanStore();

const enabled = computed(() => {
  if (props.featureName) return planStore.featureEnabled(props.featureName);
  if (props.requiredEntitlement) return planStore.hasEntitlement(props.requiredEntitlement);
  return true;
});

// FAR-1283 + FAR-2665: the lock copy is decided by the ENTITLEMENT registry in
// the plan payload, not by a static per-view tier prop. A capability the
// licence does not grant gets the upgrade wording and the View Plans link; a
// behaviour flag the org switched off is available on this plan, so saying
// "Team Feature" would be false and a purchase could not change the answer.
const entitlementKey = computed(() => {
  if (props.requiredEntitlement) return props.requiredEntitlement;
  if (props.featureName) return planStore.entitlementForFlag(props.featureName);
  return null;
});

// A commercial lock is only truthful when the licence does NOT grant the
// capability. When it IS granted but the operator kill switch has switched the
// implementing flag off, no purchase can fix it, so the behaviour copy is the
// correct one (FAR-1283 + FAR-2665). Keying on the entitlement mapping alone
// would offer "View Plans" for a capability the org already owns.
const isCommercialGate = computed(
  () => entitlementKey.value !== null && !planStore.hasEntitlement(entitlementKey.value),
);

const gateTitle = computed(() =>
  isCommercialGate.value
    ? t("components.FeatureGate.team_feature")
    : t("components.FeatureGate.feature_disabled"),
);

const tooltipText = computed(() => {
  if (!isCommercialGate.value) {
    return t("components.FeatureGate.disabled_for_your_organisation");
  }
  const label = planStore.getEntitlementLabel(entitlementKey.value as string);
  return `${t("components.FeatureGate.available_on_higher_plan_tier")} — ${label} — /settings/license`;
});
</script>
