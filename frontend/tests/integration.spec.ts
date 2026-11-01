import { expect, test } from '@playwright/test'
import { randomUUID } from 'node:crypto'

import { notices } from '../src/api/message'
import { expectAccessible } from './accessibility'

for (const { javaScriptEnabled, mode, lineEnding } of [
  { javaScriptEnabled: true, mode: 'js', lineEnding: '\n' },
  { javaScriptEnabled: false, mode: 'native', lineEnding: '\r\n' },
]) {
  test(`real acknowledged write, JavaScript=${javaScriptEnabled}`, async ({ browser, baseURL }, info) => {
    const context = await browser.newContext({ baseURL, javaScriptEnabled })
    const page = await context.newPage()
    const errors: string[] = []
    page.on('pageerror', (error) => errors.push(error.message))
    const username = `browser-${mode}-${randomUUID()}`
    const message = 'Привіт & + = 🌍\n<script>private</script>'
    await page.goto('/message')
    await page.getByLabel('Ваше ім’я').fill(username)
    await page.getByLabel('Повідомлення', { exact: true }).fill(message)
    // Base UI sets this after hydration; the native form is usable before then.
    await expect(page.locator('#message[data-filled]')).toHaveCount(Number(javaScriptEnabled))
    const submission = page.waitForRequest((request) => request.method() === 'POST')
    await page.getByRole('button', { name: 'Надіслати', exact: true }).click()
    const payload = new URLSearchParams((await submission).postData() ?? '')
    expect(payload.get('message')).toBe(message.replaceAll('\n', lineEnding))
    await expect(page.getByRole('status')).toHaveText(notices.saved)
    await expect(page.getByLabel('Ваше ім’я')).toHaveValue('')
    await expect(page.locator('html')).toHaveAttribute('dir', 'ltr')
    expect(errors).toEqual([])
    // axe injects JavaScript, so scan the hydrated production page only.
    // eslint-disable-next-line playwright/no-conditional-in-test -- fixed project mode, never response-dependent
    if (javaScriptEnabled) await expectAccessible(page, info)
    await context.close()
  })
}
