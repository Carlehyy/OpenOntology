import { readFileSync } from 'node:fs'
import { chromium } from '@playwright/test'

const session = JSON.parse(readFileSync('/tmp/msgrail-session.json', 'utf8'))

const browser = await chromium.launch()
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  viewport: { width: 1600, height: 900 },
})
await context.addInitScript(session => {
  localStorage.setItem('token', session.token)
  localStorage.setItem('auth-store', session.authStore)
}, session)
const page = await context.newPage()
page.on('console', msg => {
  if (msg.type() === 'error' || msg.type() === 'warning') {
    console.log(`[console.${msg.type()}]`, msg.text().slice(0, 400))
  }
})
page.on('pageerror', err => console.log('[pageerror]', String(err).slice(0, 600)))

await page.goto('https://64.83.14.139:8443/#/super-assistant?conversation=25a458f5-7235-464a-bc2e-7100c4321b3d')
await page.waitForLoadState('domcontentloaded')
await page.waitForTimeout(4000)

const diag = await page.evaluate(() => {
  const host = document.querySelector('main div.h-full.overflow-y-auto')
  const rail = document.querySelector('[data-testid="super-assistant-turn-rail"]')
  return {
    hostWidth: host ? Math.round(host.getBoundingClientRect().width) : null,
    hostExists: Boolean(host),
    railExists: Boolean(rail),
    mainChildren: document.querySelector('main') ? [...document.querySelector('main').children].map(el => el.tagName.toLowerCase()) : [],
    resizeObserverSupported: typeof ResizeObserver !== 'undefined',
  }
})
console.log(JSON.stringify(diag, null, 2))
await page.screenshot({ path: '/tmp/msgrail-diag.png' })
await browser.close()
