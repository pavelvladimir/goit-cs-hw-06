import assert from 'node:assert/strict'
import { test } from 'node:test'

import { MessageError, sendMessage } from '../src/api/message.ts'

function form() {
  const data = new FormData()
  data.set('username', 'Ірина + Pavel')
  data.set('message', 'Привіт & světe = 🌍\nДругий рядок')
  return data
}

function jsonRequest(status: number, data: unknown): typeof fetch {
  return async () => Response.json(data, { status })
}

test('sends one URL-encoded POST and accepts only the acknowledged write', async () => {
  let calls = 0
  await sendMessage(form(), async (input) => {
    calls++
    assert.ok(input instanceof Request)
    assert.equal(new URL(input.url).pathname, '/api/message')
    assert.equal(input.method, 'POST')
    assert.equal(input.headers.get('Accept'), 'application/json')
    assert.equal(input.redirect, 'error')
    assert.ok(input.signal)
    const body = new URLSearchParams(await input.text())
    assert.equal(body.get('username'), 'Ірина + Pavel')
    assert.equal(body.get('message'), 'Привіт & světe = 🌍\nДругий рядок')
    return Response.json({ status: 'saved' }, { status: 201 })
  })
  assert.equal(calls, 1)
})

for (const [status, code] of [
  [400, 'invalid_message'],
  [503, 'storage_unavailable'],
] as const) {
  test(`recognizes the documented ${status} response`, async () => {
    await assert.rejects(sendMessage(form(), jsonRequest(status, { error: code })), (error: unknown) => {
      assert.ok(error instanceof MessageError)
      assert.equal(error.code, code)
      return true
    })
  })
}

for (const [status, data] of [
  [200, { status: 'saved' }],
  [201, { status: 'pending' }],
  [201, null],
  [201, 'saved'],
  [201, {}],
  [400, { error: 'storage_unavailable' }],
  [500, { error: 'database secret' }],
] as const) {
  test(`rejects unexpected status/payload: ${status} ${JSON.stringify(data)}`, async () => {
    await assert.rejects(sendMessage(form(), jsonRequest(status, data)), (error: unknown) => {
      assert.ok(error instanceof Error)
      assert.ok(!(error instanceof MessageError))
      assert.ok(!error.message.includes('database secret'))
      return true
    })
  })
}

test('rejects an HTML response from a proxy', async () => {
  await assert.rejects(
    sendMessage(
      form(),
      async () =>
        new Response('<html>Error</html>', {
          status: 503,
          headers: { 'Content-Type': 'text/html' },
        }),
    ),
  )
})

test('rejects malformed JSON', async () => {
  await assert.rejects(
    sendMessage(
      form(),
      async () =>
        new Response('{', {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        }),
    ),
  )
})

test('does not retry a network failure or unknown write result', async () => {
  let calls = 0
  await assert.rejects(
    sendMessage(form(), async () => {
      calls++
      throw new TypeError('Network error')
    }),
  )
  assert.equal(calls, 1)
})
