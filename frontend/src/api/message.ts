import { submitMessage } from './generated/sdk.gen'
import { zInvalidMessage, zMessageSaved, zStorageUnavailable } from './generated/zod.gen'

const REQUEST_TIMEOUT_MS = 10_000
const CREATED = 201
const INVALID = 400
const UNAVAILABLE = 503

export const notices = {
  saved: 'Повідомлення збережено. Дякую!',
  invalid_message: 'Перевірте поля: ім’я — до 80 символів, повідомлення — до 4000.',
  storage_unavailable: 'Не вдалося підтвердити збереження. Спробуйте пізніше.',
} as const

export class MessageError extends Error {
  readonly code: 'invalid_message' | 'storage_unavailable'

  constructor(code: 'invalid_message' | 'storage_unavailable') {
    super(notices[code])
    this.name = 'MessageError'
    this.code = code
  }
}

export async function sendMessage(form: FormData, request: typeof fetch = fetch): Promise<void> {
  const { data, error, response } = await submitMessage({
    baseUrl: typeof window === 'undefined' ? 'http://127.0.0.1' : window.location.origin,
    fetch: request,
    headers: { Accept: 'application/json' },
    body: {
      username: String(form.get('username') ?? ''),
      message: String(form.get('message') ?? ''),
    },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    redirect: 'error',
    parseAs: 'json',
  })
  if (!response || response.headers.get('Content-Type')?.split(';').shift()?.trim() !== 'application/json') {
    throw new Error('Unexpected response content type')
  }
  if (response.status === CREATED && zMessageSaved.strict().safeParse(data).success) return
  if (response.status === INVALID && zInvalidMessage.strict().safeParse(error).success)
    throw new MessageError('invalid_message')
  if (response.status === UNAVAILABLE && zStorageUnavailable.strict().safeParse(error).success)
    throw new MessageError('storage_unavailable')
  throw new Error('Unexpected response status or payload')
}
