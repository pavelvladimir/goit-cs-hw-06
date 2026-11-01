import type { Metadata } from 'next'

import { notices } from '../../../api/message'
import MessagePage from '../../../components/message-page'

export const metadata: Metadata = { title: 'Не вдалося зберегти повідомлення' }

export default function Page() {
  return <MessagePage notice={notices.storage_unavailable} />
}
