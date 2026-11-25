import { computed, ref } from 'vue'
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import {
  arrowFor,
  classFor,
  formatDeltaAbs,
  formatDeltaPct,
  useDeltaAbsText,
  useDeltaArrow,
  useDeltaClass,
  useDeltaPctText,
} from '../composables/useDeltaFormatting'

import EmptyState from '../components/shared/EmptyState.vue'
import ErrorAlert from '../components/shared/ErrorAlert.vue'
import FormDialog from '../components/shared/FormDialog.vue'
import LoadingSpinner from '../components/shared/LoadingSpinner.vue'
import BaseCard from '../components/ui/BaseCard.vue'
import CardContent from '../components/ui/CardContent.vue'
import CardHeader from '../components/ui/CardHeader.vue'
import CardSkeleton from '../components/ui/CardSkeleton.vue'
import CardTable from '../components/ui/CardTable.vue'
import DataCard from '../components/ui/DataCard.vue'
import SummaryCard from '../components/ui/SummaryCard.vue'
import StatCard from '../components/ui/StatCard.vue'
import TableCard from '../components/ui/TableCard.vue'

describe('useDeltaFormatting', () => {
  it.each([
    [2, '▲'],
    [-2, '▼'],
    [0, '→'],
  ] as const)('formats %s as %s', (delta, expected) => {
    expect(arrowFor(delta)).toBe(expected)
  })

  it.each([
    [2, false, 'text-success-text'],
    [-2, false, 'text-destructive'],
    [0, false, 'text-muted-foreground'],
    [2, true, 'text-destructive'],
    [-2, true, 'text-success-text'],
    [0, true, 'text-muted-foreground'],
  ] as const)('maps %s with inverted=%s to %s', (delta, inverted, expected) => {
    expect(classFor(delta, inverted)).toBe(expected)
  })

  it.each([
    [null, ''],
    [0, '0'],
    [2, '+2'],
    [-2, '-2'],
    [2.56, '+2.6'],
    [-2.56, '-2.6'],
  ] as const)('formats absolute delta %s as %s', (delta, expected) => {
    expect(formatDeltaAbs(delta)).toBe(expected)
  })

  it.each([
    [null, false, ''],
    [16.25, false, '16.3%'],
    [-16.25, false, '16.3%'],
    [16.25, true, '+16.3%'],
    [-16.25, true, '-16.3%'],
    [0, true, '0.0%'],
  ] as const)('formats percentage delta %s with includeSign=%s as %s', (delta, includeSign, expected) => {
    expect(formatDeltaPct(delta, includeSign)).toBe(expected)
  })

  it('keeps computed wrappers reactive and handles null, inversion, and neutral trends', () => {
    const delta = ref<number | null>(2)
    const inverted = ref(false)
    const neutral = ref(false)

    const absText = useDeltaAbsText(computed(() => delta.value))
    const pctText = useDeltaPctText(computed(() => delta.value))
    const arrow = useDeltaArrow(computed(() => delta.value))
    const className = useDeltaClass(computed(() => delta.value), computed(() => inverted.value), computed(() => neutral.value))

    expect(absText.value).toBe('+2')
    expect(pctText.value).toBe('2.0%')
    expect(arrow.value).toBe('▲')
    expect(className.value).toBe('text-success-text')

    inverted.value = true
    expect(className.value).toBe('text-destructive')

    neutral.value = true
    expect(className.value).toBe('text-muted-foreground')

    delta.value = null
    expect(absText.value).toBe('')
    expect(pctText.value).toBe('')
    expect(arrow.value).toBe('')
    expect(className.value).toBe('')
  })

  it('supports boolean wrapper options and neutral zero/negative branches', () => {
    const zero = ref<number | null>(0)
    const negative = ref<number | null>(-2)

    expect(useDeltaClass(computed(() => zero.value), true, false).value).toBe('text-muted-foreground')
    expect(useDeltaClass(computed(() => negative.value), false, true).value).toBe('text-muted-foreground')
    expect(useDeltaClass(computed(() => negative.value), true, false).value).toBe('text-success-text')
  })
})

describe('BaseCard', () => {
  it.each([
    ['default', undefined, 'bg-card'],
    ['explicit default', 'default', 'bg-card'],
    ['bordered (deprecated alias)', 'bordered', 'bg-card'],
    ['muted', 'muted', 'bg-muted/50'],
    ['warning', 'warning', 'border-warning/30'],
    ['context', 'context', 'bg-primary/5'],
  ] as const)('maps the %s variant', (_name, variant, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { variant } })

    expect(wrapper.classes()).toContain(expectedClass)
  })

  it.each([
    ['none', 'none', 'p-0'],
    ['sm', 'sm', 'p-3'],
    ['md', 'md', 'p-4'],
    ['lg', 'lg', 'p-6'],
    ['xl', 'xl', 'p-8'],
    ['default', undefined, 'p-4'],
  ] as const)('maps %s padding', (_name, padding, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { padding } })

    expect(wrapper.classes()).toContain(expectedClass)
  })

  it.each([
    ['none', 'none', ''],
    ['sm', 'sm', 'shadow-sm'],
    ['default', undefined, ''],
  ] as const)('maps %s elevation', (_name, elevation, expectedClass) => {
    const wrapper = mount(BaseCard, { props: { elevation } })

    if (expectedClass) {
      expect(wrapper.classes()).toContain(expectedClass)
    } else {
      expect(wrapper.classes()).not.toContain('shadow-sm')
    }
  })

  it('adds the pressable interaction classes only when pressable', () => {
    const plain = mount(BaseCard)
    const pressable = mount(BaseCard, { props: { pressable: true } })

    expect(plain.classes()).not.toContain('card-hover')
    expect(plain.classes()).not.toContain('cursor-pointer')
    expect(pressable.classes()).toEqual(
      expect.arrayContaining(['card-hover', 'cursor-pointer', 'transition-colors', 'hover:bg-accent/30']),
    )
  })

  it('adds the dashed border only when unavailable', () => {
    expect(mount(BaseCard).classes()).not.toContain('border-dashed')
    expect(mount(BaseCard, { props: { unavailable: true } }).classes()).toContain('border-dashed')
  })

  it('maps visual props, custom classes, test ids, and named slots', () => {
    const wrapper = mount(BaseCard, {
      props: {
        variant: 'bordered',
        padding: 'lg',
        elevation: 'sm',
        class: 'custom-card',
        dataTestId: 'base-card',
      },
      slots: {
        header: '<header data-testid="base-header">Header</header>',
        default: '<main data-testid="base-content">Content</main>',
        footer: '<footer data-testid="base-footer">Footer</footer>',
      },
    })

    expect(wrapper.attributes('data-testid')).toBe('base-card')
    expect(wrapper.classes()).toEqual(expect.arrayContaining(['card', 'card-base', 'bg-card', 'p-6', 'shadow-sm', 'custom-card']))
    expect(wrapper.find('[data-testid="base-header"]').text()).toBe('Header')
    expect(wrapper.find('[data-testid="base-content"]').text()).toBe('Content')
    expect(wrapper.find('[data-testid="base-footer"]').text()).toBe('Footer')
  })
})

describe('CardHeader and DataCard', () => {
  it('renders title, description, actions, and content/footer slot wrappers', () => {
    const wrapper = mount(DataCard, {
      props: {
        title: 'Details',
        description: 'Useful context',
        dataTestId: 'data-card',
      },
      slots: {
        headerActions: '<button data-testid="header-action">Edit</button>',
        default: '<p data-testid="card-content">Body</p>',
        footer: '<p data-testid="card-footer">Footer</p>',
      },
    })

    expect(wrapper.find('[data-testid="data-card-header"] h3').text()).toBe('Details')
    expect(wrapper.find('[data-testid="data-card-header"] p').text()).toBe('Useful context')
    expect(wrapper.find('[data-testid="header-action"]').text()).toBe('Edit')
    expect(wrapper.find('[data-testid="data-card-content"] [data-testid="card-content"]').text()).toBe('Body')
    expect(wrapper.find('[data-testid="data-card-footer"] [data-testid="card-footer"]').text()).toBe('Footer')
  })

  it('renders a footer slot without an explicit data-testid', () => {
    const wrapper = mount(DataCard, {
      slots: {
        footer: '<p data-testid="bare-footer">Bare Footer</p>',
      },
    })

    expect(wrapper.find('[data-testid="bare-footer"]').text()).toBe('Bare Footer')
    expect(wrapper.find('.card-footer').attributes('data-testid')).toBeUndefined()
  })

  it('supports an explicit heading level while retaining the reusable default', () => {
    const defaultHeader = mount(CardHeader, { props: { title: 'Default heading' } })
    const customHeader = mount(CardHeader, { props: { title: 'Custom heading', headingLevel: 'h4' } })

    expect(defaultHeader.find('h3').text()).toBe('Default heading')
    expect(customHeader.find('h4').text()).toBe('Custom heading')
  })

  it('renders description-only and action-only headers', () => {
    const descriptionOnly = mount(CardHeader, { props: { description: 'Context' } })
    const actionOnly = mount(CardHeader, { slots: { actions: '<button>Action</button>' } })

    expect(descriptionOnly.find('p').text()).toBe('Context')
    expect(descriptionOnly.find('h2, h3, h4').exists()).toBe(false)
    expect(actionOnly.find('button').text()).toBe('Action')
    expect(actionOnly.find('.card-header').exists()).toBe(true)
  })

  it('supports a leading slot and optional divider without changing the default header', () => {
    const composed = mount(CardHeader, {
      props: { title: 'Housekeeping', divider: true },
      slots: { leading: '<input type="checkbox" aria-label="Select" />' },
    })
    const plain = mount(CardHeader, { props: { title: 'Plain' } })

    expect(composed.find('input[aria-label="Select"]').exists()).toBe(true)
    expect(composed.find('.card-header').classes()).toEqual(
      expect.arrayContaining(['border-b', 'border-border', 'pb-3']),
    )
    expect(plain.find('.card-header').classes()).not.toContain('border-b')
  })

  it('maps the md header size to the default title and description styles', () => {
    const header = mount(CardHeader, { props: { title: 'Default', description: 'Context' } })

    expect(header.find('h3').classes()).toEqual(expect.arrayContaining(['text-base', 'font-semibold']))
    expect(header.find('p').classes()).toEqual(expect.arrayContaining(['mt-1', 'text-sm', 'text-muted-foreground']))
  })

  it('maps the sm header size to the compact title and description styles', () => {
    const header = mount(CardHeader, {
      props: { title: 'Compact', description: 'Context', size: 'sm' },
    })

    expect(header.find('h3').classes()).toEqual(expect.arrayContaining(['text-sm', 'font-medium']))
    expect(header.find('p').classes()).toEqual(expect.arrayContaining(['mt-1', 'text-xs', 'text-muted-foreground']))
  })

  it('does not render an empty header for a title-less, action-less DataCard', () => {
    const wrapper = mount(DataCard, { slots: { default: '<p>Body</p>' } })

    expect(wrapper.find('.card-header').exists()).toBe(false)
    expect(wrapper.find('.card-content').text()).toBe('Body')
  })

  it('does not render an empty CardHeader without title, description, or actions', () => {
    const wrapper = mount(CardHeader)

    expect(wrapper.find('.card-header').exists()).toBe(false)
  })

  it('keeps DataCard headings at the requested level', () => {
    const wrapper = mount(DataCard, { props: { title: 'Settings', headingLevel: 'h2' } })

    expect(wrapper.find('h2').text()).toBe('Settings')
    expect(wrapper.find('h3').exists()).toBe(false)
  })

  it('derives header, content and footer test ids from the card test id', () => {
    const full = mount(DataCard, {
      props: { title: 'Details', dataTestId: 'derived' },
      slots: { default: '<p>Body</p>', footer: '<p>Footer</p>' },
    })
    const contentOnly = mount(DataCard, {
      props: { dataTestId: 'derived' },
      slots: { default: '<p>Body</p>' },
    })
    const empty = mount(DataCard, { props: { dataTestId: 'derived' } })

    expect(full.find('[data-testid="derived-header"]').exists()).toBe(true)
    expect(full.find('[data-testid="derived-content"]').exists()).toBe(true)
    expect(full.find('[data-testid="derived-footer"]').exists()).toBe(true)
    expect(contentOnly.find('[data-testid="derived-header"]').exists()).toBe(false)
    expect(contentOnly.find('[data-testid="derived-content"]').exists()).toBe(true)
    expect(contentOnly.find('[data-testid="derived-footer"]').exists()).toBe(false)
    expect(empty.find('[data-testid="derived-header"]').exists()).toBe(false)
    expect(empty.find('[data-testid="derived-content"]').exists()).toBe(false)
    expect(empty.find('[data-testid="derived-footer"]').exists()).toBe(false)
  })
})

describe('CardContent, CardTable and CardSkeleton', () => {
  it.each([
    ['none', 'none', 'p-0'],
    ['sm', 'sm', 'p-3'],
    ['md', 'md', 'p-4'],
    ['default', undefined, 'p-0'],
  ] as const)('maps %s CardContent padding', (_name, padding, expectedClass) => {
    const wrapper = mount(CardContent, { props: { padding } })

    expect(wrapper.classes()).toContain('card-content')
    expect(wrapper.classes()).toContain(expectedClass)
  })

  it('renders a CardTable heading strip and a focusable, labelled scroll region', () => {
    const wrapper = mount(CardTable, {
      props: { heading: 'Rows', label: 'Attempt history table' },
      slots: { default: '<table><tbody><tr><td>row</td></tr></tbody></table>' },
    })

    expect(wrapper.text()).toContain('Rows')
    const scroll = wrapper.find('.overflow-x-auto')
    expect(scroll.attributes('tabindex')).toBe('0')
    expect(scroll.attributes('aria-label')).toBe('Attempt history table')
    expect(scroll.find('table').exists()).toBe(true)
  })

  it('omits the CardTable heading strip when no heading is supplied', () => {
    const wrapper = mount(CardTable, { slots: { default: '<p>body</p>' } })

    expect(wrapper.find('.border-b').exists()).toBe(false)
    expect(wrapper.find('.overflow-x-auto').attributes('aria-label')).toBeUndefined()
  })

  it('renders CardSkeleton as a hidden animated BaseCard with the requested lines', () => {
    const wrapper = mount(CardSkeleton, { props: { lines: 3, dataTestId: 'skeleton-card' } })

    expect(wrapper.classes()).toContain('card')
    expect(wrapper.classes()).toContain('card-base')
    expect(wrapper.classes()).toContain('animate-pulse')
    expect(wrapper.attributes('aria-hidden')).toBe('true')
    expect(wrapper.attributes('data-testid')).toBe('skeleton-card')
    // one heading block + three line blocks
    expect(wrapper.findAll('.bg-muted').length).toBe(4)
  })

  it('omits the CardSkeleton heading block when heading is false', () => {
    const wrapper = mount(CardSkeleton, { props: { heading: false, lines: 1 } })

    expect(wrapper.findAll('.bg-muted').length).toBe(1)
  })
})

describe('TableCard', () => {
  const tableSlot = { default: '<table><tbody><tr><td>row</td></tr></tbody></table>' }

  it('renders a bare table untouched when neither heading nor table label is supplied', () => {
    const wrapper = mount(TableCard, { slots: tableSlot })

    expect(wrapper.find('.overflow-x-auto').exists()).toBe(false)
    expect(wrapper.find('table').exists()).toBe(true)
  })

  it('opts into the CardTable region from a table label alone', () => {
    const wrapper = mount(TableCard, { props: { tableLabel: 'Rows table' }, slots: tableSlot })

    const scroll = wrapper.find('.overflow-x-auto')
    expect(scroll.exists()).toBe(true)
    expect(scroll.attributes('aria-label')).toBe('Rows table')
  })

  it('opts into the CardTable region from a heading alone', () => {
    const wrapper = mount(TableCard, { props: { heading: 'Rows' }, slots: tableSlot })

    expect(wrapper.text()).toContain('Rows')
    expect(wrapper.find('.overflow-x-auto').exists()).toBe(true)
  })

  it('honours an explicit padding over the header-derived default', () => {
    const wrapper = mount(TableCard, { props: { title: 'Rows', padding: 'lg' }, slots: tableSlot })

    expect(wrapper.find('.card').classes()).toContain('p-6')
  })
})

describe('StatCard metric tile', () => {
  it('renders delta values, direction, percentage, and custom test id', () => {
    const wrapper = mount(StatCard, {
      props: { label: 'Runs', value: 12, delta: 2, deltaPct: 16.25, dataTestId: 'metric-card' },
    })

    expect(wrapper.attributes('data-testid')).toBe('metric-card')
    expect(wrapper.text()).toContain('+2 (+16.3%)')
    expect(wrapper.text()).toContain('▲')
   expect(wrapper.find('.text-success-text').exists()).toBe(true)
   })

    it.each([
      [{ delta: -2 }, '-2'],
      [{ delta: 2.5 }, '+2.5'],
      [{ delta: 2 }, '+2'],
      [{ deltaPct: 16.25 }, '+16.3%'],
      [{ delta: Number.NaN, deltaPct: Number.POSITIVE_INFINITY }, ''],
      [{ delta: Number.NaN }, ''],
      [{ delta: 0, deltaPct: 0 }, '0 (0.0%)'],
      [{ delta: 2, deltaPct: -16.25 }, '+2 (-16.3%)'],
    ])('renders valid delta combinations safely: %o', (deltaProps, expected) => {
      const wrapper = mount(StatCard, {
        props: { label: 'Runs', value: 12, ...deltaProps },
      })

      if (expected) {
        expect(wrapper.text()).toContain(expected)
      } else {
        expect(wrapper.text()).not.toContain('NaN')
        expect(wrapper.text()).not.toContain('Infinity')
        expect(wrapper.find('.text-xs.font-medium').exists()).toBe(false)
      }
    })

    it.each([
      ['success', 'text-success-text'],
      ['warning', 'text-warning-text'],
      ['destructive', 'text-destructive'],
      ['muted', 'text-muted-foreground'],
      ['primary', ''],
    ] as const)('maps %s value color', (color, expectedClass) => {
      const wrapper = mount(StatCard, { props: { label: 'Runs', value: 1, color } })

      if (expectedClass) expect(wrapper.find('.text-2xl').classes()).toContain(expectedClass)
      else expect(wrapper.find('.text-2xl').classes()).not.toContain('text-success-text')
    })

    it('preserves the absent test id when no custom id is provided', () => {
       expect(mount(StatCard, { props: { label: 'Runs', value: 1 } }).attributes('data-testid')).toBeUndefined()
     })


   it.each([
     [{ value: 120, max: 100 }, '100%'],
     [{ value: -10, max: 100 }, '0%'],
     [{ value: 50, max: 0 }, '0%'],
     [{ value: 50, max: -10 }, '0%'],
   ])('clamps progress safely for %o', (progress, width) => {
     const wrapper = mount(StatCard, {
       props: { label: 'Capacity', value: 'Full', progress },
     })

     expect(wrapper.find('[style]').attributes('style')).toContain(`width: ${width}`)
   })

    it('uses the destructive style for over-max progress', () => {
      const wrapper = mount(StatCard, {
        props: { label: 'Capacity', value: 'Full', progress: { value: 120, max: 100 } },
      })

      expect(wrapper.find('.bg-destructive').exists()).toBe(true)
    })

     it('falls back to zero progress for non-finite values', () => {
       const invalidMax = mount(StatCard, {
         props: { label: 'Capacity', value: 'Unknown', progress: { value: 50, max: Number.NaN } },
       })
       const invalidValue = mount(StatCard, {
         props: { label: 'Capacity', value: 'Unknown', progress: { value: Number.NaN, max: 100 } },
       })

       expect(invalidMax.find('[style]').attributes('style')).toContain('width: 0%')
       expect(invalidValue.find('[style]').attributes('style')).toContain('width: 0%')
     })

     it('covers absent progress and invalid delta computed fallbacks', () => {
       const wrapper = mount(StatCard, { props: { label: 'Capacity', value: 'Unknown' } })
       const vm = wrapper.vm as unknown as { progressBarClass: string; deltaArrow: string; deltaClass: string }

       expect(vm.progressBarClass).toBe('bg-primary')
       expect(vm.deltaArrow).toBe('')
       expect(vm.deltaClass).toBe('')
     })


     it.each([
       [{ value: 50, max: 100, color: 'success' }, 'bg-success'],
       [{ value: 95, max: 100, color: 'success' }, 'bg-success'],
       [{ value: 100, max: 100, color: 'success' }, 'bg-success'],
       [{ value: 50, max: 100, color: 'warning' }, 'bg-warning'],
       [{ value: 95, max: 100, color: 'warning' }, 'bg-warning'],
       [{ value: 100, max: 100, color: 'warning' }, 'bg-warning'],
       [{ value: 50, max: 100, color: 'destructive' }, 'bg-destructive'],
       [{ value: 95, max: 100, color: 'destructive' }, 'bg-destructive'],
       [{ value: 100, max: 100, color: 'destructive' }, 'bg-destructive'],
       [{ value: 50, max: 100, color: 'primary' }, 'bg-primary'],
       [{ value: 95, max: 100, color: 'primary' }, 'bg-primary'],
       [{ value: 100, max: 100, color: 'primary' }, 'bg-primary'],
       [{ value: 50, max: 100 }, 'bg-primary'],
       [{ value: 74, max: 100 }, 'bg-primary'],
       [{ value: 75, max: 100 }, 'bg-warning'],
       [{ value: 99, max: 100 }, 'bg-warning'],
       [{ value: 100, max: 100 }, 'bg-destructive'],
     ] as const)('maps progress %o to %s', (progress, expectedClass) => {
        const wrapper = mount(StatCard, { props: { label: 'Capacity', value: 'Half', progress } })

        expect(wrapper.find('[style]').classes()).toContain(expectedClass)
     })
 })

describe('StatCard', () => {
  it('binds a custom test id and emits none by default', () => {
    const custom = mount(StatCard, { props: { label: 'Runs', value: 12, dataTestId: 'custom-stat' } })
    const fallback = mount(StatCard, { props: { label: 'Runs', value: 12 } })

    expect(custom.find('[data-testid="custom-stat"]').exists()).toBe(true)
    expect(fallback.find('[data-testid="dashboard-stats-card-pipelines"]').exists()).toBe(false)
    expect(fallback.attributes('data-testid')).toBeUndefined()
  })

  it.each([
    ['primary', 'bg-primary/10', ''],
    ['success', 'bg-success/10', 'text-success-text'],
    ['warning', 'bg-warning/10', 'text-warning-text'],
    ['destructive', 'bg-destructive/10', 'text-destructive'],
    ['muted', 'bg-muted', 'text-muted-foreground'],
  ] as const)('maps %s color tokens', (color, iconClass, valueClass) => {
    const wrapper = mount(StatCard, {
      props: { label: 'Runs', value: 12, color },
      slots: { icon: '<circle cx="12" cy="12" r="4" />' },
    })

    expect(wrapper.find('svg').element.parentElement?.className).toContain(iconClass)
    if (valueClass) expect(wrapper.find('.text-2xl').classes()).toContain(valueClass)
  })

  it.each([
    [{ current: 12, previous: 10, delta_pct: 20 }, '▲', '+2 (20.0%)', 'text-success-text'],
    [{ current: 8, previous: 10, delta_pct: -20 }, '▼', '-2 (20.0%)', 'text-destructive'],
    [{ current: 10, previous: 10, delta_pct: 0 }, '→', '0 (0.0%)', 'text-muted-foreground'],
  ] as const)('renders percentage and absolute delta state %o', (delta, arrow, text, className) => {
    const wrapper = mount(StatCard, { props: { label: 'Runs', value: 10, delta } })

    expect(wrapper.text()).toContain(arrow)
    expect(wrapper.text()).toContain(text)
    expect(wrapper.find('.text-xs').classes()).toContain(className)
  })

  it('covers incomplete and invalid delta inputs', () => {
    const incomplete = mount(StatCard, { props: { label: 'Runs', value: 12, delta: { current: 12 } } })
    const invalid = mount(StatCard, {
      props: { label: 'Runs', value: 12, delta: { current: Number.NaN, previous: 10, delta_pct: Number.POSITIVE_INFINITY } },
    })

    expect(incomplete.find('[data-testid="stat-no-baseline"]').exists()).toBe(false)
    expect(invalid.find('.text-xs').exists()).toBe(false)
  })

  it('falls back to the primary icon background when no colour is supplied', () => {
    const wrapper = mount(StatCard, {
      props: { label: 'Runs', value: 12 },
      slots: { icon: '<circle cx="12" cy="12" r="4" />' },
    })

    expect(wrapper.find('svg').element.parentElement?.className).toContain('bg-primary/10')
  })

  it('omits the delta row when only an absolute delta is available without a baseline label', () => {
    const wrapper = mount(StatCard, {
      props: {
        label: 'Runs',
        value: 12,
        progress: { value: 50, max: 100 },
        delta: { current: 12, previous: 10 },
      },
    })

    expect(wrapper.find('.text-xs').exists()).toBe(false)
    expect(wrapper.find('[data-testid="stat-no-baseline"]').exists()).toBe(false)
    expect(wrapper.find('[style]').attributes('style')).toContain('width: 50%')
  })

     it('renders percentage-only deltas and omits a delta when no valid values remain', () => {
     const percentageOnly = mount(StatCard, { props: { label: 'Runs', value: 12, delta: { delta_pct: -12.5 } } })
     const absent = mount(StatCard, { props: { label: 'Runs', value: 12, delta: null } })

     expect(percentageOnly.find('.text-xs').text()).toContain('▼ (12.5%)')
     expect(absent.find('.text-xs').exists()).toBe(false)
   })

   it('covers neutral styling for an absolute delta without a percentage', () => {
     const wrapper = mount(StatCard, {
       props: {
         label: 'Runs',
         value: 12,
         neutralTrend: true,
         noBaselineLabel: 'No baseline',
         delta: { current: 12.5, previous: 10 },
       },
     })

     expect(wrapper.find('[data-testid="stat-no-baseline"]').text()).toContain('+2.5')
     expect(wrapper.find('[data-testid="stat-no-baseline"]').classes()).toContain('text-muted-foreground')
     expect((wrapper.vm as unknown as { deltaArrow: string }).deltaArrow).toBe('')
     expect((wrapper.vm as unknown as { deltaClass: string }).deltaClass).toBe('')
     expect((wrapper.vm as unknown as { deltaAbsArrow: string }).deltaAbsArrow).toBe('▲')
     expect((wrapper.vm as unknown as { deltaAbsClass: string }).deltaAbsClass).toBe('text-muted-foreground')
   })


  it('formats fractional absolute deltas and inverted positive trends', () => {
    const fractional = mount(StatCard, {
      props: { label: 'Runs', value: 12, noBaselineLabel: 'No baseline', delta: { current: 12.5, previous: 10 } },
    })
    const inverted = mount(StatCard, {
      props: { label: 'Failures', value: 12, inverted: true, delta: { current: 12, previous: 10, delta_pct: 20 } },
    })

    expect(fractional.find('[data-testid="stat-no-baseline"]').text()).toContain('+2.5')
    expect(inverted.find('.text-xs').classes()).toContain('text-destructive')
  })

  it('supports inverted and neutral delta styling plus no-baseline absolute deltas', () => {
    const inverted = mount(StatCard, {
      props: { label: 'Failures', value: 8, inverted: true, delta: { current: 8, previous: 10, delta_pct: -20 } },
    })
    const neutral = mount(StatCard, {
      props: { label: 'Runs', value: 12, neutralTrend: true, delta: { current: 12, previous: 10, delta_pct: 20 } },
    })
    const noBaseline = mount(StatCard, {
      props: { label: 'Runs', value: 12, delta: { current: 12, previous: 10 }, noBaselineLabel: 'No baseline' },
    })

    expect(inverted.find('.text-xs').classes()).toContain('text-success-text')
    expect(neutral.find('.text-xs').classes()).toContain('text-muted-foreground')
    expect(noBaseline.find('[data-testid="stat-no-baseline"]').text()).toContain('+2')
     expect(noBaseline.find('[data-testid="stat-no-baseline"]').attributes('title')).toBe('No baseline')
   })

    it('covers zero and negative absolute-only delta branches', () => {
      const zero = mount(StatCard, {
        props: { label: 'Runs', value: 10, noBaselineLabel: 'No baseline', delta: { current: 10, previous: 10 } },
      })
      const negative = mount(StatCard, {
        props: { label: 'Runs', value: 8, noBaselineLabel: 'No baseline', delta: { current: 8, previous: 10 } },
      })

      expect(zero.find('[data-testid="stat-no-baseline"]').text()).toContain('→0')
      expect(zero.find('[data-testid="stat-no-baseline"]').classes()).toContain('text-muted-foreground')
      expect(negative.find('[data-testid="stat-no-baseline"]').text()).toContain('▼-2')
      expect(negative.find('[data-testid="stat-no-baseline"]').classes()).toContain('text-destructive')
    })

    it('evaluates absent absolute deltas and neutral absolute fallbacks', () => {
      const absent = mount(StatCard, { props: { label: 'Runs', value: 1 } })
      const neutral = mount(StatCard, {
        props: {
          label: 'Runs',
          value: 1,
          neutralTrend: true,
          noBaselineLabel: 'No baseline',
          delta: { current: 3, previous: 1 },
        },
      })
      const nonNeutral = mount(StatCard, {
        props: {
          label: 'Runs',
          value: 1,
          noBaselineLabel: 'No baseline',
          delta: { current: 3, previous: 1 },
        },
      })
      const absentVm = absent.vm as unknown as { deltaAbsArrow: string; deltaAbsClass: string; deltaPctText: string }
      const neutralVm = neutral.vm as unknown as { deltaAbsClass: string }
      const nonNeutralVm = nonNeutral.vm as unknown as { deltaAbsClass: string }

      expect(absentVm.deltaAbsArrow).toBe('')
      expect(absentVm.deltaAbsClass).toBe('')
      expect(absentVm.deltaPctText).toBe('')
      expect(neutralVm.deltaAbsClass).toBe('text-muted-foreground')
      expect(nonNeutralVm.deltaAbsClass).toBe('text-success-text')
    })

    it('mounts with noBaselineLabel, inverted: true, and delta: { current: 8, previous: 10 }', () => {
      const wrapper = mount(StatCard, {
        props: {
          label: 'Failures',
          value: 8,
          noBaselineLabel: 'No baseline',
          inverted: true,
          delta: { current: 8, previous: 10 },
        },
      })

      const noBaseline = wrapper.find('[data-testid="stat-no-baseline"]')
      expect(noBaseline.exists()).toBe(true)
      expect(noBaseline.classes()).toContain('text-success-text')
      expect((wrapper.vm as unknown as { deltaAbsClass: string }).deltaAbsClass).toBe('text-success-text')
    })

   it('renders as a router link when a destination is provided', () => {
    const wrapper = mount(StatCard, { props: { label: 'Runs', value: 12, to: '/runs' } })

    expect(wrapper.find('a').attributes('href')).toBe('/runs')
  })
})

describe('SummaryCard', () => {
  it('binds the configured grid test id and renders summary items', () => {
    const wrapper = mount(SummaryCard, {
      props: {
        title: 'Summary',
        items: [{ label: 'Passed', value: 4, testId: 'passed-item' }],
        gridTestId: 'custom-summary-grid',
      },
    })

    expect(wrapper.find('[data-testid="custom-summary-grid"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="risk-assessment-summary-grid"]').exists()).toBe(false)
     expect(wrapper.find('[data-testid="passed-item"]').text()).toContain('4')
   })

    it('passes an explicit heading level through to CardHeader', () => {
      const wrapper = mount(SummaryCard, {
        props: {
          title: 'Summary',
          headingLevel: 'h2',
          items: [],
        },
      })

      expect(wrapper.find('h2').text()).toBe('Summary')
      expect(wrapper.find('h3').exists()).toBe(false)
    })

    it('renders a header without a custom dataTestId prefix', () => {
      const wrapper = mount(SummaryCard, {
        props: {
          title: 'Unprefixed Summary',
          items: [],
        },
      })

      expect(wrapper.find('.card-header').exists()).toBe(true)
      expect(wrapper.find('.card-header').attributes('data-testid')).toBeUndefined()
    })

    it('adds the card id suffix to its header when provided', () => {
      const wrapper = mount(SummaryCard, {
        props: { title: 'Prefixed Summary', items: [], dataTestId: 'summary-card' },
      })

      expect(wrapper.find('[data-testid="summary-card-header"]').text()).toContain('Prefixed Summary')
    })

     it('omits the header test id when no card test id is provided', () => {
       const wrapper = mount(SummaryCard, {
         props: { title: 'Unprefixed Summary', items: [] },
       })

       expect(wrapper.find('.card-header').attributes('data-testid')).toBeUndefined()
     })

     it('renders all supported item colors, fallback color, footnote, header action, and footer', () => {
      const wrapper = mount(SummaryCard, {
        props: {
          items: [
            { label: 'Primary', value: 1, color: 'primary' },
            { label: 'Success', value: 2, color: 'success' },
            { label: 'Warning', value: 3, color: 'warning' },
            { label: 'Destructive', value: 4, color: 'destructive' },
            { label: 'Muted', value: 5, color: 'muted' },
            { label: 'Foreground', value: 6, color: 'foreground' },
            { label: 'Unknown', value: 7, color: 'unknown' as never },
          ],
          footnote: 'Updated recently',
          footnoteTestId: 'summary-footnote',
        },
        slots: {
          headerActions: '<button data-testid="summary-action">Action</button>',
          footer: '<span data-testid="summary-footer">Footer</span>',
        },
      })

      expect(wrapper.find('[data-testid="summary-footnote"]').text()).toBe('Updated recently')
      expect(wrapper.find('[data-testid="summary-action"]').text()).toBe('Action')
      expect(wrapper.find('[data-testid="summary-footer"]').text()).toBe('Footer')
      expect(wrapper.find('.text-primary-text').text()).toContain('1')
      expect(wrapper.find('.text-success-text').text()).toContain('2')
      expect(wrapper.find('.text-warning-text').text()).toContain('3')
      expect(wrapper.find('.text-destructive-text').text()).toContain('4')
      expect(wrapper.findAll('.text-muted-foreground').some((item) => item.text().includes('5'))).toBe(true)
      expect(wrapper.findAll('.text-foreground').some((item) => item.text().includes('6'))).toBe(true)
      expect(wrapper.findAll('.text-foreground').some((item) => item.text().includes('7'))).toBe(true)
    })
  })

describe('EmptyState variant', () => {
  it('keeps the card shell by default (byte-identical to the original)', () => {
    const wrapper = mount(EmptyState, {
      props: { title: 'Nothing here', description: 'Add something to see it.' },
    })

    expect(wrapper.classes()).toContain('card')
    expect(wrapper.classes()).toContain('p-8')
    expect(wrapper.classes()).toContain('text-center')
    expect(wrapper.text()).toContain('Nothing here')
    expect(wrapper.text()).toContain('Add something to see it.')
  })

  it('renders the same anatomy WITHOUT the card shell when variant is bare', () => {
    // A bare empty state is placed inside an existing card/panel, so carrying
    // the card shell would nest a card in a card. It must keep the icon, the
    // title/description and the centring.
    const wrapper = mount(EmptyState, {
      props: { title: 'Nothing here', description: 'Add something to see it.', variant: 'bare' },
    })

    expect(wrapper.classes()).not.toContain('card')
    expect(wrapper.classes()).not.toContain('p-8')
    expect(wrapper.classes()).toContain('text-center')
    expect(wrapper.find('svg').exists()).toBe(true)
    expect(wrapper.text()).toContain('Nothing here')
    expect(wrapper.text()).toContain('Add something to see it.')
  })

  it('replaces the default icon through the #icon slot', () => {
    const wrapper = mount(EmptyState, {
      props: { title: 'Nothing here' },
      slots: { icon: '<span data-testid="custom-icon">icon</span>' },
    })

    expect(wrapper.find('[data-testid="custom-icon"]').exists()).toBe(true)
    // The hard-coded two-hexagon svg is only the fallback.
    expect(wrapper.find('svg').exists()).toBe(false)
  })
})

describe('ErrorAlert slots', () => {
  it('renders default-slot body content instead of the message text', () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'plain message' },
      slots: { default: '<ul data-testid="body"><li>first problem</li></ul>' },
    })

    expect(wrapper.find('[data-testid="body"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('first problem')
    expect(wrapper.text()).not.toContain('plain message')
    // Body content lives inside the ONE live region, never a second one.
    expect(wrapper.findAll('[role="alert"]')).toHaveLength(1)
  })

  it('renders the #actions slot controls', () => {
    const wrapper = mount(ErrorAlert, {
      props: { message: 'boom' },
      slots: { actions: '<button type="button" data-testid="body-action">Try again</button>' },
    })

    expect(wrapper.find('[data-testid="body-action"]').exists()).toBe(true)
    expect(wrapper.findAll('[role="alert"]')).toHaveLength(1)
  })
})

describe('FormDialog width', () => {
  function mountDialog(props: Record<string, unknown> = {}) {
    return mount(FormDialog, {
      props: { open: true, title: 'Confirm action', ...props },
      global: {
        stubs: {
          Dialog: {
            props: ['visible'],
            template: '<div v-if="visible" data-testid="dialog"><slot name="header" /><slot /><slot name="footer" /></div>',
          },
          Button: {
            props: ['disabled', 'loading'],
            template: '<button :disabled="disabled"><slot /></button>',
          },
        },
      },
    })
  }

  it('applies the historical 28rem width when width is omitted', () => {
    const dialog = mountDialog().find('[data-testid="dialog"]')

    expect(dialog.attributes('style')).toContain('width: 28rem')
  })

  it('applies a caller-supplied width to the dialog style', () => {
    const dialog = mountDialog({ width: '40rem' }).find('[data-testid="dialog"]')

    expect(dialog.attributes('style')).toContain('width: 40rem')
  })
})

describe('LoadingSpinner', () => {
  it('renders the original page-level markup byte-for-byte with no props', () => {
    const wrapper = mount(LoadingSpinner)

    // No props must equal the pre-prop output exactly: a page-level py-16
    // wrapper around the md ring.
    expect(wrapper.attributes('class')).toBe('flex items-center justify-center py-16')
    expect(wrapper.find('.animate-spin').attributes('class')).toBe(
      'h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent',
    )
  })

  it('renders the sm ring when size="sm"', () => {
    const wrapper = mount(LoadingSpinner, { props: { size: 'sm' } })
    const ring = wrapper.find('.animate-spin')

    expect(ring.classes()).toContain('h-5')
    expect(ring.classes()).toContain('w-5')
    expect(ring.classes()).toContain('border-2')
    // The md size must not leak through when the prop is set.
    expect(ring.classes()).not.toContain('h-8')
    expect(ring.classes()).not.toContain('border-4')
  })

  it('renders the lg ring when size="lg"', () => {
    const wrapper = mount(LoadingSpinner, { props: { size: 'lg' } })
    const ring = wrapper.find('.animate-spin')

    expect(ring.classes()).toContain('h-12')
    expect(ring.classes()).toContain('w-12')
    expect(ring.classes()).toContain('border-4')
    expect(ring.classes()).not.toContain('h-8')
    expect(ring.classes()).not.toContain('border-2')
  })

  it('drops the vertical padding when padded is false, keeping the centring', () => {
    const wrapper = mount(LoadingSpinner, { props: { padded: false } })

    expect(wrapper.classes()).toEqual(['flex', 'items-center', 'justify-center'])
    expect(wrapper.classes()).not.toContain('py-16')
    expect(wrapper.find('.animate-spin').exists()).toBe(true)
  })

  it('combines the inline size and unpadded variant used by inline panels', () => {
    const wrapper = mount(LoadingSpinner, { props: { size: 'sm', padded: false } })

    expect(wrapper.attributes('class')).toBe('flex items-center justify-center')
    expect(wrapper.find('.animate-spin').classes()).toContain('h-5')
  })
})
