import type { Metadata } from 'next'

import { notices } from '../../../api/message'
import MessagePage from '../../../components/message-page'

export const metadata: Metadata = { title: 'Перевірте повідомлення' }

export default function Page() {
  return <MessagePage notice={notices.invalid_message} />
}
