import type { Metadata } from 'next'

import { notices } from '../../../api/message'
import MessagePage from '../../../components/message-page'

export const metadata: Metadata = { title: 'Повідомлення збережено' }

export default function Page() {
  return <MessagePage notice={notices.saved} />
}
