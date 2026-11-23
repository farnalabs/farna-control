import { test, expect, loginForSystemRoute } from './setup/fixtures'

test.describe('Admin Housekeeping', { tag: "@regression" }, () => {
  test('renders the Housekeeping page', { tag: "@regression" }, async ({ page, env }) => {
    const systemAdmin = await loginForSystemRoute(page, env)
    await page.goto('/admin/housekeeping')
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
    await expect(page).toHaveURL(/\/admin\/housekeeping$/)
    await expect(page.locator('h1')).toContainText('Housekeeping')
    await expect(page.getByTestId('hk-refresh')).toBeVisible()
    await expect(page.getByTestId('hk-checkpoint-retention')).toBeVisible()
    if (env.name === 'local') {
      await expect(page.getByTestId('hk-empty')).toBeVisible()
      await expect(page.getByText('All Clean!')).toBeVisible()
      await expect(page.getByTestId('hk-ckpt-max-age')).toBeVisible()
      await expect(page.getByTestId('hk-ckpt-purge')).toBeVisible()
    }
  })
})
