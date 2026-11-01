'use client'

import { useRef, useState } from 'react'
import type { SubmitEvent } from 'react'

import { Button } from '@base-ui/react/button'
import { Field } from '@base-ui/react/field'
import { Form } from '@base-ui/react/form'

import { MessageError, notices, sendMessage } from '../api/message'

export default function MessageForm({ initialNotice = '' }: Readonly<{ initialNotice?: string }>) {
  const [notice, setNotice] = useState(initialNotice)
  const [pending, setPending] = useState(false)
  const inFlight = useRef(false)

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault()
    if (inFlight.current) return
    const form = event.currentTarget
    const data = new FormData(form)
    inFlight.current = true
    setPending(true)
    setNotice('Зберігаємо повідомлення…')
    try {
      await sendMessage(data)
      form.reset()
      setNotice(notices.saved)
    } catch (error) {
      setNotice(error instanceof MessageError ? error.message : notices.storage_unavailable)
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }

  return (
    <main className="form-signin m-auto">
      <p className="notice" role="status" aria-live="polite" aria-atomic="true">
        {notice}
      </p>
      <Form action="/api/message" method="POST" onSubmit={handleSubmit} noValidate={false} aria-busy={pending}>
        <h1 className="h3 mb-3 fw-normal">Надіслати повідомлення</h1>
        <Field.Root className="row mb-3" name="username">
          <Field.Label>Ваше ім’я</Field.Label>
          <Field.Control
            type="text"
            className="form-control"
            id="username"
            required
            maxLength={80}
            autoComplete="nickname"
            readOnly={pending}
          />
        </Field.Root>
        <Field.Root className="row mb-3 h-100" name="message">
          <Field.Label>Повідомлення</Field.Label>
          <Field.Control
            render={<textarea rows={8} />}
            className="form-control"
            id="message"
            required
            maxLength={4000}
            readOnly={pending}
          />
        </Field.Root>
        <Button
          className="w-100 btn btn-lg btn-primary"
          type="submit"
          disabled={pending}
          focusableWhenDisabled
          aria-busy={pending}
          aria-labelledby="submit-label"
        >
          <span id="submit-label">{pending ? 'Зберігаємо…' : 'Надіслати'}</span>
        </Button>
      </Form>
    </main>
  )
}
