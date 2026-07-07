import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { mkdir, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(process.env.NS_OPS_PROJECT_ROOT || resolve(__dirname, '..'))
const QR_DIR = resolve(process.env.NS_OPS_RISINGSTONE_QR_DIR || resolve(PROJECT_ROOT, '.local', 'risingstone-qr'))

const DEFAULT_REDIRECT_URL = 'https://ff14risingstones.web.sdo.com/pc/index.html#/me/info'
const LOGIN_BASE_URL = 'https://apiff14risingstones.web.sdo.com/api/home/GHome/login'
const IS_LOGIN_URL = 'https://apiff14risingstones.web.sdo.com/api/home/GHome/isLogin'
const DEFAULT_TTL_MS = 180000
const DEFAULT_NAVIGATION_TIMEOUT_MS = 45000
const MAX_SESSIONS = Number.parseInt(process.env.NS_OPS_RISINGSTONE_QR_MAX_SESSIONS || '5', 10)
const USER_AGENT =
  process.env.NS_OPS_RISINGSTONE_USER_AGENT ||
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

const sessions = new Map()
let playwrightPromise = null

function userError(statusCode, message) {
  const error = new Error(message)
  error.statusCode = statusCode
  throw error
}

function compactText(value, maxChars = 500) {
  return String(value || '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxChars)
}

function buildLoginUrl(redirectUrl = DEFAULT_REDIRECT_URL) {
  const url = new URL(LOGIN_BASE_URL)
  url.searchParams.set('redirectUrl', redirectUrl)
  return url.toString()
}

function safeUrl(value) {
  try {
    const url = new URL(value)
    return `${url.origin}${url.pathname}`
  } catch {
    return String(value).split('?')[0].split('#')[0]
  }
}

async function loadPlaywright() {
  if (playwrightPromise) {
    return playwrightPromise
  }

  playwrightPromise = (async () => {
    try {
      return await import('playwright')
    } catch {
      const require = createRequire(import.meta.url)
      const candidates = []
      if (process.env.NODE_PATH) {
        candidates.push(...process.env.NODE_PATH.split(process.platform === 'win32' ? ';' : ':'))
      }
      if (process.platform === 'win32' && process.env.APPDATA) {
        candidates.push(join(process.env.APPDATA, 'npm', 'node_modules'))
      }

      try {
        const command = process.platform === 'win32' ? 'cmd.exe' : 'npm'
        const args = process.platform === 'win32' ? ['/d', '/s', '/c', 'npm root -g'] : ['root', '-g']
        candidates.push(execFileSync(command, args, { encoding: 'utf8' }).trim())
      } catch {
        // The fixed APPDATA path above is enough on the Windows host. Keep going.
      }

      for (const root of [...new Set(candidates.filter(Boolean))]) {
        try {
          return require(join(root, 'playwright'))
        } catch {
          // Try the next known global module path.
        }
      }

      throw new Error('Playwright is not available to the runner process.')
    }
  })()

  return playwrightPromise
}

function payloadNumber(payload, key, fallback, { min, max } = {}) {
  const raw = Number(payload?.[key])
  const value = Number.isFinite(raw) ? raw : fallback
  if (typeof min === 'number' && value < min) {
    return min
  }
  if (typeof max === 'number' && value > max) {
    return max
  }
  return value
}

async function closeSession(session, { removeImage = true } = {}) {
  sessions.delete(session.id)
  try {
    await session.browser?.close()
  } catch {
    // The session is being discarded; closing failures do not change the user-visible state.
  }
  if (removeImage && session.screenshotPath) {
    await rm(session.screenshotPath, { force: true }).catch(() => {})
  }
}

export async function cleanupExpiredQrSessions() {
  const now = Date.now()
  const expired = []
  for (const session of sessions.values()) {
    if (session.expiresAtMs <= now) {
      expired.push(session)
    }
  }
  await Promise.all(expired.map((session) => closeSession(session)))
}

function getSession(sessionId) {
  const id = String(sessionId || '').trim()
  if (!id) {
    userError(400, 'missing qr session id')
  }

  const session = sessions.get(id)
  if (!session) {
    userError(404, 'qr session not found or expired')
  }
  if (session.expiresAtMs <= Date.now()) {
    void closeSession(session)
    userError(404, 'qr session not found or expired')
  }
  return session
}

async function getSanitizedLoginState(context) {
  try {
    const response = await context.request.get(IS_LOGIN_URL, {
      headers: {
        accept: 'application/json, text/plain, */*',
        referer: 'https://ff14risingstones.web.sdo.com/'
      },
      timeout: 15000
    })
    const status = response.status()
    const contentType = response.headers()['content-type'] || ''
    let payload = null
    if (contentType.includes('application/json')) {
      payload = await response.json().catch(() => null)
    } else {
      const text = await response.text().catch(() => '')
      payload = { text: compactText(text, 200) }
    }
    const code = payload && typeof payload === 'object' ? payload.code : undefined
    const message = payload && typeof payload === 'object' ? payload.msg || payload.message || '' : ''
    return {
      httpStatus: status,
      code,
      message: compactText(message, 120),
      loggedIn: [10000, 10002, 10103, 10104].includes(code),
      hasData: Boolean(payload?.data)
    }
  } catch (error) {
    return {
      httpStatus: null,
      code: null,
      message: compactText(error instanceof Error ? error.message : String(error), 160),
      loggedIn: false,
      hasData: false
    }
  }
}

async function clickIfPresent(page, locator, actionName) {
  if ((await locator.count().catch(() => 0)) <= 0) {
    return false
  }
  try {
    await locator.click({ timeout: 5000, force: actionName === 'acceptLoginTerms' })
    return true
  } catch {
    return false
  }
}

export async function startQrLoginSession(payload = {}) {
  await cleanupExpiredQrSessions()
  if (sessions.size >= MAX_SESSIONS) {
    userError(429, 'too many active qr sessions')
  }

  const { chromium } = await loadPlaywright()
  const sessionId = randomUUID()
  const now = Date.now()
  const ttlMs = payloadNumber(payload, 'ttlMs', DEFAULT_TTL_MS, { min: 60000, max: 300000 })
  const screenshotPath = resolve(QR_DIR, `${sessionId}.png`)
  await mkdir(QR_DIR, { recursive: true })

  let browser = null
  try {
    const launchOptions = {
      headless: !payload?.headed,
      args: [
        '--disable-blink-features=AutomationControlled',
        '--disable-crash-reporter',
        '--disable-crashpad'
      ]
    }
    if (process.env.NS_OPS_PLAYWRIGHT_EXECUTABLE_PATH) {
      launchOptions.executablePath = process.env.NS_OPS_PLAYWRIGHT_EXECUTABLE_PATH
    } else if (process.env.NS_OPS_PLAYWRIGHT_CHANNEL) {
      launchOptions.channel = process.env.NS_OPS_PLAYWRIGHT_CHANNEL
    } else if (process.platform === 'win32') {
      launchOptions.channel = 'chrome'
    }

    browser = await chromium.launch(launchOptions)

    const context = await browser.newContext({
      locale: 'zh-CN',
      timezoneId: 'Asia/Shanghai',
      viewport: { width: 1280, height: 900 },
      userAgent: USER_AGENT
    })
    const page = await context.newPage()
    const responses = []
    page.on('response', (response) => {
      const url = response.url()
      const isRelevant =
        url.includes('risingstones.web.sdo.com') ||
        url.includes('apiff14risingstones.web.sdo.com') ||
        response.status() >= 400
      if (!isRelevant || responses.length >= 30) {
        return
      }
      responses.push({
        status: response.status(),
        url: safeUrl(url)
      })
    })

    await page.goto(buildLoginUrl(payload?.redirectUrl || DEFAULT_REDIRECT_URL), {
      waitUntil: 'domcontentloaded',
      timeout: payloadNumber(payload, 'timeoutMs', DEFAULT_NAVIGATION_TIMEOUT_MS, {
        min: 10000,
        max: 120000
      })
    })
    await page.waitForTimeout(1000)

    const acceptedLoginTerms = await clickIfPresent(
      page,
      page.locator("input[type='checkbox']").first(),
      'acceptLoginTerms'
    )
    if (acceptedLoginTerms) {
      await page.waitForTimeout(500)
    }

    const clickedQrTab = await clickIfPresent(page, page.getByText('二维码', { exact: true }).first(), 'qrTab')
    if (clickedQrTab) {
      await page.waitForTimeout(2000)
    }

    await page.screenshot({ path: screenshotPath, fullPage: true })
    const bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(() => '')
    const loginState = await getSanitizedLoginState(context)
    const session = {
      id: sessionId,
      browser,
      context,
      page,
      userAgent: USER_AGENT,
      screenshotPath,
      createdAtMs: now,
      expiresAtMs: now + ttlMs,
      acceptedLoginTerms,
      clickedQrTab,
      responses
    }
    sessions.set(sessionId, session)

    return {
      sessionId,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      ttlSeconds: Math.round(ttlMs / 1000),
      imagePath: `/risingstone/qr/${sessionId}/image`,
      acceptedLoginTerms,
      clickedQrTab,
      loginState,
      visibleText: compactText(bodyText, 240)
    }
  } catch (error) {
    if (browser) {
      await browser.close().catch(() => {})
    }
    await rm(screenshotPath, { force: true }).catch(() => {})
    throw error
  }
}

export async function getQrLoginImage(sessionId) {
  const session = getSession(sessionId)
  return {
    contentType: 'image/png',
    body: await readFile(session.screenshotPath)
  }
}

export async function getQrLoginStatus(sessionId) {
  const session = getSession(sessionId)
  const loginState = await getSanitizedLoginState(session.context)
  if (!loginState.loggedIn) {
    return {
      status: 'pending',
      loggedIn: false,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      loginState
    }
  }

  const cookies = await session.context.cookies()
  const risingstoneCookie = cookies.find((cookie) => cookie.name === 'ff14risingstones')
  if (!risingstoneCookie?.value) {
    return {
      status: 'pending',
      loggedIn: false,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      loginState: {
        ...loginState,
        message: '已登录，但尚未取得石之家 cookie'
      }
    }
  }

  await closeSession(session)
  return {
    status: 'success',
    loggedIn: true,
    loginState,
    credential: {
      cookie: `ff14risingstones=${risingstoneCookie.value}`,
      userAgent: session.userAgent
    }
  }
}

export async function cancelQrLoginSession(sessionId) {
  const session = getSession(sessionId)
  await closeSession(session)
  return {
    status: 'cancelled',
    sessionId: session.id
  }
}
