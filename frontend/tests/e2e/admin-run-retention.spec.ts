import { test, expect, loginForSystemRoute } from './setup/fixtures'

test.describe('Admin Run Retention', { tag: "@regression" }, () => {
  test('renders the Run Retention page', { tag: "@regression" }, async ({ page, env }) => {
    const systemAdmin = await loginForSystemRoute(page, env)
    await page.goto('/admin/run-retention')
    if (!systemAdmin) {
      // FAR-938: the SYSTEM sidebar group is system-admin-only. The staging/prod
      // e2e identity is a regular org admin, so FAR-2664 refuses the navigation
      // onto the locked page (naming the instance-admin restriction) instead of
      // silently bouncing back to the dashboard.
      await expect(page).toHaveURL(/\/locked\?.*reason=system_admin/)
      await expect(page.getByTestId('locked-route')).toBeVisible()
      await expect(page.locator('h1')).toContainText('restricted to instance administrators')
      return
    }
    await expect(page.locator('h1')).toContainText('Run Retention')
    if (env.name === 'local') {
      await expect(page.getByTestId('admin-run-retention-refresh')).toBeVisible()
    }
  })

  test('auto-applies filters without Apply button (FAR-868)', { tag: "@regression" }, async ({ page, env }) => {
    test.skip(env.name !== 'local', 'SYSTEM route is system-admin-only; no system-admin e2e identity on real targets')
    await loginForSystemRoute(page, env)
    await page.goto('/admin/run-retention')

    // The Apply Filters button must not exist
    await expect(page.getByTestId('admin-run-retention-apply')).toHaveCount(0)

    // Reset button must exist
    await expect(page.getByTestId('admin-run-retention-reset')).toBeVisible()

    // Changing a dropdown filter should auto-apply (no Apply button needed)
    const pipelineSelect = page.getByTestId('admin-run-retention-pipeline')
    if (await pipelineSelect.isVisible()) {
      await pipelineSelect.click()
      // Select an option if available, or just close the dropdown
      const firstOption = page.locator('.p-select-option').first()
      if (await firstOption.isVisible()) {
        await firstOption.click()
      }
    }

    // Reset should clear filters
    await page.getByTestId('admin-run-retention-reset').click()
    await expect(page.getByTestId('admin-run-retention-reset')).toBeVisible()
  })
})
