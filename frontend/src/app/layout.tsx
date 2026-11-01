import type { ReactNode } from 'react'

import { DirectionProvider } from '@base-ui/react/direction-provider'
import type { Metadata, Viewport } from 'next'

import '../style.css'

const direction = 'ltr'

export const metadata: Metadata = {
  title: { default: 'Python — GoIT', template: '%s — GoIT' },
  description: 'Фінальне завдання GoIT: форма повідомлень, TCP та MongoDB.',
  other: { 'format-detection': 'telephone=no,email=no,address=no,date=no' },
}

export const viewport: Viewport = { width: 'device-width', initialScale: 1, viewportFit: 'cover' }

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="uk" dir={direction}>
      <body>
        <DirectionProvider direction={direction}>{children}</DirectionProvider>
      </body>
    </html>
  )
}
