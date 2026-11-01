import type { Metadata } from 'next'

import MessagePage from '../../components/message-page'

export const metadata: Metadata = { title: 'Надіслати повідомлення' }

export default function Page() {
  return <MessagePage />
}
