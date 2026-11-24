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
)
