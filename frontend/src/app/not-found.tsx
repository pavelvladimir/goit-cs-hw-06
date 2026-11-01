import Link from 'next/link'

import Navigation from '../components/navigation'

export default function NotFound() {
  return (
    <>
      <Navigation />
      <main className="container text-center p-3 pb-md-4">
        <h1 className="display-4 fw-normal">404</h1>
        <p className="fs-5 text-muted">Сторінку не знайдено</p>
        <Link href="/">Повернутися на головну</Link>
      </main>
    </>
  )
}
