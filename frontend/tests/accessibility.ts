import { AxeBuilder } from '@axe-core/playwright'
import { expect, type Page, type TestInfo } from '@playwright/test'

export async function expectAccessible(page: Page, info: TestInfo) {
  // Scan the whole document with all default axe rules; no exclusions.
  const results = await new AxeBuilder({ page }).analyze()
  await info.attach('axe-results', {
    body: JSON.stringify(results, null, 2),
    contentType: 'application/json',
  })
  expect(results.violations).toEqual([])
}
