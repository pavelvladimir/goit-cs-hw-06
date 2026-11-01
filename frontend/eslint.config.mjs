import js from '@eslint/js'
import nextVitals from 'eslint-config-next/core-web-vitals'
import nextTs from 'eslint-config-next/typescript'
import prettierConfig from 'eslint-config-prettier/flat'
import imports from 'eslint-plugin-import'
import importHelpers from 'eslint-plugin-import-helpers'
import a11y from 'eslint-plugin-jsx-a11y'
import playwright from 'eslint-plugin-playwright'
import prettier from 'eslint-plugin-prettier'
import sonarjs from 'eslint-plugin-sonarjs'
import { defineConfig, globalIgnores } from 'eslint/config'
import globals from 'globals'

const SOURCE_FILES = ['**/*.{js,jsx,cjs,mjs,ts,tsx,cts,mts}']

export default defineConfig([
  globalIgnores([
    'node_modules/**',
    'out/**',
    'dist/**',
    '.next/**',
    'build/**',
    'coverage/**',
    'next-env.d.ts',
    'playwright-report/**',
    'test-results/**',
    'src/api/generated/**',
    'tests/fixtures/**',
    'public/vendor/**',
  ]),
  js.configs.recommended,
  // Include ESM/CommonJS configuration files in the Next.js and React checks.
  ...nextVitals.map((config) => (config.name === 'next' ? { ...config, files: SOURCE_FILES } : config)),
  ...nextTs,
  sonarjs.configs.recommended,
  prettierConfig,
  {
    files: SOURCE_FILES,
    plugins: { 'import-helpers': importHelpers, prettier },
    rules: {
      ...a11y.configs.recommended.rules,
      ...imports.configs.warnings.rules,
      'jsx-a11y/alt-text': ['error', { img: ['Image'] }],
      'react/no-unknown-property': ['error', { ignore: ['jsx', 'global'] }],
      'react/jsx-no-target-blank': 'error',
      '@typescript-eslint/no-unused-expressions': 'error',
      'no-var': 'error',
      'prefer-const': 'error',
      'prefer-rest-params': 'error',
      'prefer-spread': 'error',
      'import-helpers/order-imports': [
        'warn',
        {
          newlinesBetween: 'always',
          groups: ['/^react/', 'module', ['parent', 'sibling', 'index']],
          alphabetize: { order: 'asc', ignoreCase: true },
        },
      ],
      // Options live in .prettierrc.json, shared with the standalone formatter.
      'prettier/prettier': 'warn',
      'react/react-in-jsx-scope': 'off',
      'react/display-name': 'off',
      'react/prop-types': 'off',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          args: 'all',
          argsIgnorePattern: '^_',
          caughtErrors: 'all',
          caughtErrorsIgnorePattern: '^_',
          destructuredArrayIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          ignoreRestSiblings: true,
        },
      ],
      'no-console': ['warn', { allow: ['warn', 'error', 'info'] }],
      'no-magic-numbers': 'error',
      '@typescript-eslint/ban-ts-comment': [
        'error',
        { 'ts-ignore': 'allow-with-description', minimumDescriptionLength: 10 },
      ],
      'sonarjs/cognitive-complexity': ['warn', 20],
      'sonarjs/no-ignored-exceptions': 'warn',
      'sonarjs/no-nested-conditional': 'error',
      'sonarjs/no-nested-functions': 'warn',
      // TypeScript's rule above owns unused-variable diagnostics.
      'sonarjs/no-unused-vars': 'off',
    },
  },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: { globals: globals.browser },
  },
  {
    files: ['*.config.{js,cjs,mjs,ts}', 'tests/**/*.ts'],
    languageOptions: { globals: globals.node },
    // Numeric configuration values and expected test data are intentional literals.
    rules: { 'no-magic-numbers': 'off' },
  },
  {
    files: ['**/*.{ts,tsx,cts,mts}'],
    languageOptions: { parserOptions: { projectService: true } },
    rules: { '@typescript-eslint/no-deprecated': 'error' },
  },
  {
    files: ['tests/**/*.spec.ts'],
    extends: [playwright.configs['flat/recommended']],
    rules: {
      'playwright/expect-expect': [
        'error',
        { assertFunctionNames: ['expect', 'expectSamePixels', 'expectAccessible'] },
      ],
    },
  },
  {
    files: ['scripts/**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
])
