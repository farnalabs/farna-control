import pluginVue from 'eslint-plugin-vue'
import vuejsAccessibility from 'eslint-plugin-vuejs-accessibility'
import { withVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import globals from 'globals'

export default withVueTs(
  {
    ignores: ['dist/**', 'coverage/**', 'node_modules/**'],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  vuejsAccessibility.configs['flat/recommended'],
  {
    rules: {
      'no-console': ['error', { allow: ['warn', 'error'] }],
      // vue-tsc owns unused-symbol enforcement to avoid duplicate diagnostics.
      '@typescript-eslint/no-unused-vars': 'off',
      // Preserve the legacy config contract; these are tracked by vue-tsc or are
      // intentionally used at API and test boundaries throughout the codebase.
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/ban-ts-comment': 'warn',
      '@typescript-eslint/no-this-alias': 'warn',
      'vue/component-api-style': ['error', ['script-setup']],
      'vue/multi-word-component-names': ['error', {
        ignores: ['Badge', 'Button', 'Card', 'Dialog', 'Input', 'Select', 'Tabs', 'Tooltip', 'LogoMark', 'SidebarLink', 'OwnershipPicker', 'SsoProviderForm'],
      }],
      'vuejs-accessibility/aria-props': 'error',
      'vuejs-accessibility/alt-text': 'error',
      'vuejs-accessibility/no-autofocus': 'warn',
      'vuejs-accessibility/tabindex-no-positive': 'error',
      'vuejs-accessibility/aria-unsupported-elements': 'error',
      'vuejs-accessibility/click-events-have-key-events': 'warn',
      'vuejs-accessibility/form-control-has-label': 'warn',
      'vuejs-accessibility/heading-has-content': 'warn',
      'vuejs-accessibility/label-has-for': ['error', { required: { some: ['id', 'nesting'] } }],
      'vuejs-accessibility/mouse-events-have-key-events': 'warn',
      'vuejs-accessibility/no-distracting-elements': 'error',
      'vuejs-accessibility/no-static-element-interactions': 'warn',
      'vuejs-accessibility/role-has-required-aria-props': 'error',
      'vuejs-accessibility/iframe-has-title': 'error',
      'vuejs-accessibility/interactive-supports-focus': 'warn',
      'vuejs-accessibility/anchor-has-content': 'warn',
    },
  },
  {
    // Node CLI tooling under tests/visual-baseline, which the `lint` script now
    // covers alongside `src`. Additive only: nothing here changes the rules
    // applied to `src`.
    files: ['tests/visual-baseline/**/*.mjs'],
    languageOptions: {
      sourceType: 'module',
      globals: { ...globals.node },
    },
    rules: {
      // These are CLIs whose stdout report is the deliverable, so `console.log`
      // is the intended output channel; `warn`/`error` stay allowed as in src.
      'no-console': ['error', { allow: ['warn', 'error', 'log'] }],
      // The repo defers unused-symbol enforcement to vue-tsc, but tsconfig
      // covers only `src/**` and `tests/e2e/**/*.ts`, so these .mjs files have
      // no other checker. Keep the same guarantee for this scope.
      'no-unused-vars': 'error',
    },
  },
  {
    // E2E specs and their harness are CLIs whose stdout IS the deliverable, the
    // same rationale as tests/visual-baseline above: the axe-violation dumps
    // (failing node selector, measured foreground/background and the actual
    // ratio) printed by wcag-contrast.spec.ts, staging-wcag.spec.ts,
    // json-viewer.spec.ts and dialog-select-overlay.spec.ts are the diagnostic
    // a CI failure is read from, and setup/seeder.ts + setup/coverage-teardown.ts
    // print their progress as the run log. `console.log` is therefore the
    // intended output channel here. Scoped to tests/e2e only — `src/**` keeps
    // the warn/error-only policy above unchanged.
    files: ['tests/e2e/**/*.ts'],
    rules: {
      'no-console': ['error', { allow: ['warn', 'error', 'log'] }],
    },
  },
  {
    // tests/e2e/setup/** is Node test scaffolding (the Playwright global setup /
    // teardown and the data seeder), not bundled app code. The coverage teardown
    // lazily `require()`s istanbul's CJS-only packages right beside an
    // `await import` for the ESM-exported one — a legitimate Node CJS interop, so
    // the browser-oriented no-require-imports rule does not apply to this scope.
    files: ['tests/e2e/setup/**/*.ts'],
    rules: {
      '@typescript-eslint/no-require-imports': 'off',
    },
  },
)
