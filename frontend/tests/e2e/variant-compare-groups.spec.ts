import type { Page } from '@playwright/test'
import { test, expect, loginAsAdmin } from './setup/fixtures'

// FAR-2598: /variants/compare lists comparison groups in a DataTable (it used
// to be an unlabelled dropdown). These checks pin the two things the redesign
// exists for: the page is legible at phone width, and a row selects the
// comparison in place instead of navigating to the batch-detail route.
const GROUPS = [
  {
    id: 'g1',
    name: 'Sonnet vs Opus',
    pipeline_id: 'p1',
    description: null,
    variants: [
      { name: 'sonnet', weight: 0.5, eval_definition_ids: ['e1', 'e2'] },
      { name: 'opus', weight: 0.5, eval_definition_ids: ['e1'] },
    ],
    selection_strategy: 'weighted',
    run_count: 3,
    max_concurrent_runs: 10,
    degraded_evals: false,
    created_at: '2025-06-01T10:00:00Z',
    updated_at: '2025-06-01T10:00:00Z',
  },
  {
    id: 'g2',
    name: 'Snapshot A vs B',
    pipeline_id: 'p2',
    description: null,
    variants: [
      { name: 'a', weight: 0.5, eval_definition_ids: ['e1'] },
      { name: 'b', weight: 0.5, eval_definition_ids: ['e1'] },
    ],
    selection_strategy: 'weighted',
    run_count: 0,
    max_concurrent_runs: 10,
    degraded_evals: true,
    created_at: '2025-06-02T10:00:00Z',
    updated_at: '2025-06-02T10:00:00Z',
  },
]

const PIPELINES = {
  items: [
    { id: 'p1', name: 'Support triage' },
    { id: 'p2', name: 'Invoice parsing' },
  ],
  total: 2,
}

async function mockVariantGroups(page: Page) {
  await page.route('**/api/v1/variant-groups**', (route) => {
    const path = new URL(route.request().url()).pathname.replace(/\/$/, '')
    // The detail fetch backs the comparison table + diff viewer below the list.
    let body: unknown = GROUPS
    if (path !== '/api/v1/variant-groups') {
      const id = path.split('/').pop()
      body = GROUPS.find((group) => group.id === id) ?? null
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  })
  await page.route('**/api/v1/pipelines**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINES) }),
  )
}

test.describe('Variant compare — comparison groups table (FAR-2598)', { tag: '@regression' }, () => {
  test('the table is legible at 390px: low-priority columns hide and the page does not overflow', async ({ page, env }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    await loginAsAdmin(page, env)
    // Register after loginAsAdmin so these specific routes win over its
    // generic API catch-all.
    await mockVariantGroups(page)
    await page.goto('/variants/compare')

    const table = page.getByTestId('variant-compare-groups-table')
    await expect(table).toBeVisible()
    await expect(table.getByText('Sonnet vs Opus')).toBeVisible()
    await expect(table.getByText('Snapshot A vs B')).toBeVisible()

    // Pipeline and degraded-eval columns only exist from md up.
    await expect(table.getByRole('columnheader', { name: 'Pipeline' })).toBeHidden()
    await expect(table.getByRole('columnheader', { name: 'Degraded evals' })).toBeHidden()
    await expect(table.getByRole('columnheader', { name: 'Name' })).toBeVisible()

    const overflow = await page.evaluate(() => ({
      docScrollWidth: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }))
    expect(overflow.docScrollWidth).toBeLessThanOrEqual(overflow.viewport)
  })

  test('a search filters the rows and a row selects the comparison in place', async ({ page, env }) => {
    await loginAsAdmin(page, env)
    await mockVariantGroups(page)
    await page.goto('/variants/compare')

    const table = page.getByTestId('variant-compare-groups-table')
    await expect(table.getByText('Sonnet vs Opus')).toBeVisible()

    await page.getByTestId('variant-compare-select-g2').click()
    await expect(page.getByRole('heading', { name: 'Snapshot A vs B' })).toBeVisible()
    await expect(page.getByTestId('variant-compare-select-g2')).toHaveText('Viewing')
    // Selection is in place: still on the list route, not the batch detail.
    await expect(page).toHaveURL(/\/variants\/compare$/)

    await page.getByTestId('filter-bar-search').fill('Snapshot')
    await expect(table.getByText('Snapshot A vs B')).toBeVisible()
    await expect(table.getByText('Sonnet vs Opus')).toBeHidden()
  })
})
