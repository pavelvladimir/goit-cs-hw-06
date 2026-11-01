'use client'

import { NavigationMenu } from '@base-ui/react/navigation-menu'
import Link from 'next/link'

export default function Navigation({ active }: Readonly<{ active?: 'home' | 'message' }>) {
  return (
    <header>
      <NavigationMenu.Root className="navbar navbar-expand navbar-dark bg-dark" aria-label="Головна навігація">
        <div className="container-fluid">
          <Link className="navbar-brand" href="/" aria-label="На головну">
            {/* Keep the original 995-byte PNG without inline styles under our CSP. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src="/logo.png" alt="GoIT" width={100} height={30} />
          </Link>
          <NavigationMenu.List className="navbar-nav me-auto">
            <NavigationMenu.Item className="nav-item">
              <NavigationMenu.Link
                className={`nav-link${active === 'home' ? ' active' : ''}`}
                render={<Link href="/" />}
                active={active === 'home'}
              >
                Головна
              </NavigationMenu.Link>
            </NavigationMenu.Item>
            <NavigationMenu.Item className="nav-item">
              <NavigationMenu.Link
                className={`nav-link${active === 'message' ? ' active' : ''}`}
                render={<Link href="/message" />}
                active={active === 'message'}
              >
                Надіслати повідомлення
              </NavigationMenu.Link>
            </NavigationMenu.Item>
          </NavigationMenu.List>
        </div>
      </NavigationMenu.Root>
    </header>
  )
}
