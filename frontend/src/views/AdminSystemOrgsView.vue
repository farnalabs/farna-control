<template>
  <div class="page-wide">
    <PageHeader :title="$t('views.AdminSystemOrgsView.system_admin_organisations')" :subtitle="$t('views.AdminSystemOrgsView.crosstenant_organisation_management_system_admin_only')" />

    <div class="grid gap-6 md:grid-cols-2">
      <DataCard
        heading-level="h2"
        padding="lg"
        :title="$t('views.AdminSystemOrgsView.create_organisation')"
        description="Create a new tenant organization with a unique slug. The creator is automatically added as an admin."
      >
        <div class="mt-3">
          <code class="rounded bg-muted px-2 py-1 text-xs font-mono">{{ $t('views.AdminSystemOrgsView.post_apiv1adminorgs') }}</code>
        </div>
        <p class="mt-3 text-xs text-muted-foreground">
          Payload: <code class="font-mono">{ "name": "...", "slug": "..." }</code>
        </p>
      </DataCard>

      <DataCard
        heading-level="h2"
        padding="lg"
        :title="$t('views.AdminSystemOrgsView.create_user_in_organisation')"
        description="Provision a new user account within a specific organization and assign a role."
      >
        <div class="mt-3">
          <code class="rounded bg-muted px-2 py-1 text-xs font-mono">{{ $t('views.AdminSystemOrgsView.post_apiv1adminorgsorgidusers') }}</code>
        </div>
        <p class="mt-3 text-xs text-muted-foreground">
          Roles: <code class="font-mono">admin</code>, <code class="font-mono">operator</code>, <code class="font-mono">runner</code>, <code class="font-mono">viewer</code>
        </p>
      </DataCard>
    </div>
  </div>
</template>

<script setup lang="ts">
import PageHeader from '../components/shared/PageHeader.vue'
import DataCard from '../components/ui/DataCard.vue'
</script>
