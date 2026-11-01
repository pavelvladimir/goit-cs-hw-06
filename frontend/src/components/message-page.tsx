import MessageForm from './message-form'
import Navigation from './navigation'

export default function MessagePage({ notice = '' }: Readonly<{ notice?: string }>) {
  return (
    <>
      <Navigation active="message" />
      <div className="container mt-3">
        <MessageForm initialNotice={notice} />
      </div>
    </>
  )
}
