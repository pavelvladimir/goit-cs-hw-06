import { expect, test } from '@playwright/test'

if (!process.env.FRONTEND_BASE_URL) {
  test.beforeEach(async ({ page }) => {
    await page.route('**/health', (route) => route.fulfill({ status: 200, contentType: 'text/plain', body: 'ok\n' }))
  })
}

test('local Swagger UI loads the contract and executes a health request', async ({ page, baseURL }) => {
  const errors: string[] = []
  const external: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  page.on('request', (request) => {
    if (!request.url().startsWith(baseURL ?? 'http://127.0.0.1:4176')) external.push(request.url())
  })
  await page.goto('/docs')
  await expect(page.getByRole('heading', { name: 'GoIT Messages API', exact: false })).toBeVisible()
  const operation = page.locator('#operations-default-checkHealth')
  await operation.getByRole('button', { name: /GET.*health/ }).click()
  await operation.getByRole('button', { name: 'Try it out' }).click()
  const response = page.waitForResponse((result) => result.url().endsWith('/health'))
  await operation.getByRole('button', { name: 'Execute', exact: true }).click()
  expect((await response).status()).toBe(200)
  await expect(operation.locator('.live-responses-table')).toContainText('ok')
  expect(errors).toEqual([])
  expect(external).toEqual([])
})
