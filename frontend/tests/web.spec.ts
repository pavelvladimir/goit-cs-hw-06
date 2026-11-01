import { expect, test, type Page } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { PNG } from 'pngjs'

import { notices } from '../src/api/message'
import { expectAccessible } from './accessibility'

const fixtures = fileURLToPath(new URL('./fixtures/', import.meta.url))
const username = 'Ірина + Pavel'
const message = 'Привіт & světe = 🌍\n<script>alert(1)</script>'

async function stableScreenshot(page: Page) {
  // Chromium paints native controls differently in an inactive tab.
  await page.bringToFront()
  await page.evaluate(
    () =>
      new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
      }),
  )
  return page.screenshot({ fullPage: true, animations: 'disabled' })
}

function expectSamePixels(actual: Buffer, expected: Buffer, allowRounding = false) {
  const current = PNG.sync.read(actual)
  const baseline = PNG.sync.read(expected)
  expect([current.width, current.height]).toEqual([baseline.width, baseline.height])
  let differentPixels = 0
  let largestChannelDifference = 0
  for (let offset = 0; offset < current.data.length; offset += 4) {
    let pixelDifference = 0
    for (let channel = 0; channel < 4; channel++) {
      pixelDifference = Math.max(
        pixelDifference,
        Math.abs(current.data[offset + channel] - baseline.data[offset + channel]),
      )
    }
    differentPixels += Number(pixelDifference > 0)
    largestChannelDifference = Math.max(largestChannelDifference, pixelDifference)
  }
  // Chromium rounds a handful of border antialiasing pixels after dynamic form updates.
  // Mobile fields add more rounded corners: at most 32 pixels may differ by 1/255.
  // Geometry and all larger color differences remain strict.
  expect(largestChannelDifference).toBeLessThanOrEqual(allowRounding ? 1 : 0)
  expect(differentPixels).toBeLessThanOrEqual(allowRounding ? 32 : 0)
}

async function fillForm(page: Page) {
  await page.goto('/message')
  await page.getByLabel('Ваше ім’я').fill(username)
  await page.getByLabel('Повідомлення', { exact: true }).fill(message)
}

for (const [path, heading] of [
  ['/', 'Python'],
  ['/message', 'Надіслати повідомлення'],
  ['/missing', '404'],
]) {
  for (const width of [1280, 320]) {
    test(`accessible ${path} at ${width}px and 200% text`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto(path)
      await expect(page.locator('html')).toHaveAttribute('lang', 'uk')
      await expect(page.locator('html')).toHaveAttribute('dir', 'ltr')
      await expect(page.locator('meta[name="viewport"]')).toHaveAttribute(
        'content',
        'width=device-width, initial-scale=1, viewport-fit=cover',
      )
      await expect(page.locator('meta[name="format-detection"]')).toHaveAttribute(
        'content',
        'telephone=no,email=no,address=no,date=no',
      )
      await expect(page.getByRole('main')).toHaveCount(1)
      await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1)
      await expect(page.getByRole('main').getByRole('heading', { level: 1 })).toHaveText(heading)
      await expectAccessible(page, info)
      // Browser text zoom without changing viewport width.
      await page.evaluate(() => {
        document.documentElement.style.fontSize = '200%'
      })
      const dimensions = await page.evaluate(() => ({
        content: document.documentElement.scrollWidth,
        viewport: document.documentElement.clientWidth,
      }))
      expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport)
      await expectAccessible(page, info)
    })
  }
}

test('keyboard navigation follows LTR and native constraints prevent empty submission', async ({ page }) => {
  let requests = 0
  await page.route('**/api/message', async (route) => {
    requests++
    await route.fulfill({ status: 201, json: { status: 'saved' } })
  })
  await page.goto('/')
  const home = page.getByRole('link', { name: 'Головна', exact: true })
  await home.focus()
  await home.press('ArrowRight')
  const formLink = page.getByRole('link', { name: 'Надіслати повідомлення', exact: true })
  await expect(formLink).toBeFocused()
  await formLink.press('Enter')
  await expect(page).toHaveURL('/message')
  await page.getByRole('button', { name: 'Надіслати', exact: true }).click()
  await expect(page.getByLabel('Ваше ім’я')).toBeFocused()
  expect(requests).toBe(0)
})

for (const width of [1280, 320]) {
  for (const activation of ['Enter', 'Space']) {
    test(`pending submit with ${activation}, ${width}px`, async ({ page }, info) => {
      let release: () => void = () => {
        throw new Error('Route not started')
      }
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      let requests = 0
      await page.route('**/api/message', async (route) => {
        requests++
        const body = new URLSearchParams(route.request().postData() ?? '')
        expect(body.get('username')).toBe(username)
        expect(body.get('message')).toBe(message)
        await gate
        await route.fulfill({ status: 201, json: { status: 'saved' } })
      })
      await page.setViewportSize({ width, height: 900 })
      await fillForm(page)
      const button = page.getByRole('button')
      await button.focus()
      await button.press(activation)
      await expect(page.getByRole('status')).toHaveAttribute('aria-live', 'polite')
      await expect(page.getByRole('status')).toHaveAttribute('aria-atomic', 'true')
      await expect(button).toHaveAttribute('aria-busy', 'true')
      await expect(button).toHaveAttribute('aria-disabled', 'true')
      await expect(button).toBeFocused()
      await expect(page.getByLabel('Ваше ім’я')).toHaveAttribute('readonly', '')
      await button.press('Enter')
      await button.press('Space')
      await expectAccessible(page, info)
      expect(requests).toBe(1)
      release()
      await expect(page.getByRole('status')).toHaveText(notices.saved)
      await expect(page.getByLabel('Ваше ім’я')).toHaveValue('')
      await expect(page.getByLabel('Повідомлення', { exact: true })).toHaveValue('')
      await expect(button).toBeFocused()
      await expect(button).toHaveAttribute('aria-busy', 'false')
      await expectAccessible(page, info)
    })
  }

  for (const failure of ['invalid', 'unavailable', 'network', 'malformed', 'wrong-status', 'html']) {
    test(`preserves input on ${failure}, ${width}px and permits retry`, async ({ page }, info) => {
      await page.route('**/api/message', async (route) => {
        if (failure === 'network') return route.abort()
        if (failure === 'malformed') return route.fulfill({ status: 201, contentType: 'application/json', body: '{' })
        if (failure === 'html')
          return route.fulfill({ status: 503, contentType: 'text/html', body: '<h1>private</h1>' })
        if (failure === 'wrong-status') return route.fulfill({ status: 200, json: { status: 'saved' } })
        await route.fulfill({
          status: failure === 'invalid' ? 400 : 503,
          json: { error: failure === 'invalid' ? 'invalid_message' : 'storage_unavailable' },
        })
      })
      await page.setViewportSize({ width, height: 900 })
      await fillForm(page)
      await page.getByRole('button').click()
      await expect(page.getByRole('status')).toHaveText(
        failure === 'invalid' ? notices.invalid_message : notices.storage_unavailable,
      )
      await expect(page.getByLabel('Ваше ім’я')).toHaveValue(username)
      await expect(page.getByLabel('Повідомлення', { exact: true })).toHaveValue(message)
      await expectAccessible(page, info)
      await page.unroute('**/api/message')
      await page.route('**/api/message', (route) => route.fulfill({ status: 201, json: { status: 'saved' } }))
      await page.getByRole('button').click()
      await expect(page.getByRole('status')).toHaveText(notices.saved)
    })
  }
}

for (const [path, fixture] of [
  ['/', 'index'],
  ['/message', 'message'],
  ['/missing', '404'],
]) {
  for (const width of [1280, 390]) {
    test(`original appearance ${fixture} at ${width}px`, async ({ page, context }, info) => {
      await page.setViewportSize({ width, height: 900 })
      const reference = await context.newPage()
      await reference.setViewportSize({ width, height: 900 })
      await reference.route('**/__fixtures__/**', async (route) => {
        const file = new URL(route.request().url()).pathname.split('/').at(-1) ?? ''
        await route.fulfill({
          path: `${fixtures}/${file}`,
          contentType: file.endsWith('.css') ? 'text/css' : 'text/html',
        })
      })
      await reference.goto(`/__fixtures__/${fixture}.html`)
      await page.goto(path)
      await Promise.all([reference.evaluate(() => document.fonts.ready), page.evaluate(() => document.fonts.ready)])
      const baseline = await stableScreenshot(reference)
      const current = await stableScreenshot(page)
      await info.attach('original', { body: baseline, contentType: 'image/png' })
      await info.attach('current', { body: current, contentType: 'image/png' })
      expectSamePixels(current, baseline)
      await reference.close()
    })
  }
}

const responseStatuses = { saved: 201, invalid_message: 400, storage_unavailable: 503 } as const
for (const width of [1280, 390]) {
  for (const state of ['saved', 'invalid_message', 'storage_unavailable'] as const) {
    test(`original form appearance after ${state}, ${width}px`, async ({ page, context }, info) => {
      await page.setViewportSize({ width, height: 900 })
      const reference = await context.newPage()
      await reference.setViewportSize({ width, height: 900 })
      await reference.route('**/__fixtures__/**', async (route) => {
        const file = new URL(route.request().url()).pathname.split('/').at(-1) ?? ''
        await route.fulfill({
          path: `${fixtures}/${file}`,
          contentType: file.endsWith('.css') ? 'text/css' : 'text/html',
        })
      })
      await reference.goto('/__fixtures__/message.html')
      await reference.getByRole('status', { includeHidden: true }).evaluate((element, text) => {
        element.textContent = text
      }, notices[state])
      await page.route('**/api/message', (route) =>
        route.fulfill({
          status: responseStatuses[state],
          json: state === 'saved' ? { status: 'saved' } : { error: state },
        }),
      )
      await fillForm(page)
      await page.getByRole('button').click()
      await expect(page.getByRole('status')).toHaveText(notices[state])
      await reference.locator('input').fill(state === 'saved' ? '' : username)
      await reference.locator('textarea').fill(state === 'saved' ? '' : message)
      // Compare resting appearance; keyboard focus is verified separately.
      await page.getByRole('heading', { level: 1 }).click()
      await reference.getByRole('heading', { level: 1 }).click()
      const baseline = await stableScreenshot(reference)
      const current = await stableScreenshot(page)
      await info.attach('original', { body: baseline, contentType: 'image/png' })
      await info.attach('current', { body: current, contentType: 'image/png' })
      expectSamePixels(current, baseline, true)
      await reference.close()
    })
  }
}

test('server-rendered controls remain labelled without JavaScript', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, javaScriptEnabled: false })
  const page = await context.newPage()
  await page.goto('/message')
  await page.getByLabel('Ваше ім’я').fill(username)
  await page.getByLabel('Повідомлення', { exact: true }).fill(message)
  await expect(page.getByRole('textbox', { name: 'Ваше ім’я' })).toHaveValue(username)
  // Axe needs JavaScript; native label association is exercised with JS disabled.
  await page.getByText('Ваше ім’я', { exact: true }).click()
  await expect(page.getByLabel('Ваше ім’я')).toBeFocused()
  await context.close()
})

for (const width of [1280, 390]) {
  test(`original button hover at ${width}px`, async ({ page, context }, info) => {
    await page.setViewportSize({ width, height: 900 })
    const reference = await context.newPage()
    await reference.setViewportSize({ width, height: 900 })
    await reference.route('**/__fixtures__/**', async (route) => {
      const file = new URL(route.request().url()).pathname.split('/').at(-1) ?? ''
      await route.fulfill({
        path: `${fixtures}/${file}`,
        contentType: file.endsWith('.css') ? 'text/css' : 'text/html',
      })
    })
    await reference.goto('/__fixtures__/message.html')
    await page.goto('/message')
    await reference.getByRole('button').hover()
    const baseline = await stableScreenshot(reference)
    await page.getByRole('button').hover()
    const current = await stableScreenshot(page)
    expectSamePixels(current, baseline, true)
    await expectAccessible(page, info)
    await reference.close()
  })
}

test('320px error at 200% text remains visible and keyboard retry retains focus', async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 900 })
  await page.route('**/api/message', (route) => route.fulfill({ status: 503, json: { error: 'storage_unavailable' } }))
  await fillForm(page)
  const before = await page
    .getByLabel('Повідомлення', { exact: true })
    .evaluate((element) => parseFloat(getComputedStyle(element).fontSize))
  await page.evaluate(() => {
    document.documentElement.style.fontSize = '200%'
  })
  const after = await page
    .getByLabel('Повідомлення', { exact: true })
    .evaluate((element) => parseFloat(getComputedStyle(element).fontSize))
  expect(after).toBeGreaterThanOrEqual(before * 2)
  const button = page.getByRole('button')
  await page.getByLabel('Повідомлення', { exact: true }).focus()
  await page.keyboard.press('Tab')
  await expect(button).toBeFocused()
  await expect(button).toHaveCSS('outline-style', 'solid')
  await expect(button).toHaveCSS('outline-width', '3px')
  await button.press('Space')
  await expect(page.getByRole('status')).toHaveText(notices.storage_unavailable)
  await expect(button).toBeFocused()
  const box = await page.getByRole('status').boundingBox()
  expect(box).not.toBeNull()
  expect(box!.x).toBeGreaterThanOrEqual(0)
  expect(box!.x + box!.width).toBeLessThanOrEqual(320)
  const clippedText = await page.getByRole('status').evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    const text = range.getBoundingClientRect()
    const bounds = element.getBoundingClientRect()
    return {
      top: text.top < bounds.top,
      bottom: text.bottom > bounds.bottom,
      left: text.left < bounds.left,
      right: text.right > bounds.right,
    }
  })
  expect(clippedText).toEqual({ top: false, bottom: false, left: false, right: false })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(320)
  await expectAccessible(page, info)
  await page.unroute('**/api/message')
  await page.route('**/api/message', (route) => route.fulfill({ status: 201, json: { status: 'saved' } }))
  await button.press('Enter')
  await expect(page.getByRole('status')).toHaveText(notices.saved)
  await expect(button).toBeFocused()
})
