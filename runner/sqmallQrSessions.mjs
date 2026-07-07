import { execFileSync } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { createRequire } from 'node:module'
import { mkdir, readFile, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(process.env.NS_OPS_PROJECT_ROOT || resolve(__dirname, '..'))
const QR_DIR = resolve(process.env.NS_OPS_SQMALL_QR_DIR || resolve(PROJECT_ROOT, '.local', 'sqmall-qr'))

const DEFAULT_LOGIN_URL =
  'https://login.sdo.com/sdo/Login/LoginFrameFC.php?pm=2&appId=6666&areaId=-1' +
  '&returnURL=https%3A%2F%2Fm.qu.sdo.com%2Fpersonal-center%3FmerchantId%3D1' +
  '&serviceUrl=https%3A%2F%2Fm.qu.sdo.com%2Fpersonal-center%3FmerchantId%3D1'
const SQMALL_SERVICE_URL = 'https://m.qu.sdo.com/personal-center?merchantId=1'
const SESSION_STATUS_URL = 'https://sqmallservice.u.sdo.com/api/us/getSessionStatus'
const DEFAULT_TTL_MS = 180000
const DEFAULT_NAVIGATION_TIMEOUT_MS = 45000
const MAX_SESSIONS = Number.parseInt(process.env.NS_OPS_SQMALL_QR_MAX_SESSIONS || '5', 10)
const USER_AGENT =
  process.env.NS_OPS_SQMALL_USER_AGENT ||
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

function safeUrl(value) {
  try {
    const url = new URL(value)
    return `${url.origin}${url.pathname}`
  } catch {
    return String(value).split('?')[0].split('#')[0]
  }
}

function parseJsonp(text) {
  const raw = String(text || '').trim()
  const start = raw.indexOf('(')
  const end = raw.lastIndexOf(')')
  if (start < 0 || end <= start) {
    return null
  }
  try {
    return JSON.parse(raw.slice(start + 1, end))
  } catch {
    return null
  }
}

function ticketFromCodeKeyPayload(payload) {
  if (!payload || typeof payload !== 'object') {
    return ''
  }
  if (payload.return_code !== 0) {
    return ''
  }
  return String(payload.data?.ticket || '').trim()
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
        // Continue with known candidates.
      }
      for (const root of [...new Set(candidates.filter(Boolean))]) {
        try {
          return require(join(root, 'playwright'))
        } catch {
          // Try the next known module path.
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

function mallHeaders() {
  return {
    accept: 'application/json, text/javascript, */*; q=0.01',
    'cache-control': 'no-cache',
    pragma: 'no-cache',
    origin: 'https://m.qu.sdo.com',
    referer: 'https://m.qu.sdo.com/',
    'user-agent': USER_AGENT,
    'x-requested-with': 'XMLHttpRequest',
    'qu-deploy-platform': '2',
    'qu-hardware-platform': '3',
    'qu-merchant-id': '',
    'qu-software-platform': '1',
    'qu-web-host': 'https://m.qu.sdo.com'
  }
}

async function closeSession(session, { removeImage = true } = {}) {
  sessions.delete(session.id)
  try {
    await session.browser?.close()
  } catch {
    // Discarding session; close failures do not change user-visible status.
  }
  if (removeImage && session.screenshotPath) {
    await rm(session.screenshotPath, { force: true }).catch(() => {})
  }
}

export async function cleanupExpiredSqmallQrSessions() {
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
    userError(400, 'missing sqmall qr session id')
  }
  const session = sessions.get(id)
  if (!session) {
    userError(404, 'sqmall qr session not found or expired')
  }
  if (session.expiresAtMs <= Date.now()) {
    void closeSession(session)
    userError(404, 'sqmall qr session not found or expired')
  }
  return session
}

async function getSanitizedSqmallLoginState(context) {
  try {
    const response = await context.request.get(SESSION_STATUS_URL, {
      headers: mallHeaders(),
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

    const resultCode = payload && typeof payload === 'object' ? payload.resultCode : undefined
    const resultMsg = payload && typeof payload === 'object' ? payload.resultMsg || payload.message || '' : ''
    const data = payload?.data && typeof payload.data === 'object' ? payload.data : {}
    return {
      httpStatus: status,
      resultCode,
      resultMsg: compactText(resultMsg, 120),
      loggedIn: resultCode === 0,
      hasData: Boolean(payload?.data),
      displayName: compactText(data.emankcin || data.nickName || data.showUsername || '', 80),
      memberIdPresent: Boolean(data.direbmemllam)
    }
  } catch (error) {
    return {
      httpStatus: null,
      resultCode: null,
      resultMsg: compactText(error instanceof Error ? error.message : String(error), 160),
      loggedIn: false,
      hasData: false,
      displayName: '',
      memberIdPresent: false
    }
  }
}

async function getRawSqmallLoginData(context) {
  const response = await context.request.get(SESSION_STATUS_URL, {
    headers: mallHeaders(),
    timeout: 15000
  })
  const payload = await response.json().catch(() => null)
  return payload?.data && typeof payload.data === 'object' ? payload.data : {}
}

async function establishMallSession(session) {
  if (!session?.loginTicket || session.mallSessionAttempted) {
    return false
  }
  session.mallSessionAttempted = true
  try {
    const url = new URL(SQMALL_SERVICE_URL)
    url.searchParams.set('ticket', session.loginTicket)
    await session.page.goto(url.toString(), {
      waitUntil: 'domcontentloaded',
      timeout: 30000
    })
    await session.page.waitForTimeout(1500)
    return true
  } catch (error) {
    session.mallSessionError = compactText(error instanceof Error ? error.message : String(error), 160)
    return false
  }
}

async function clickIfPresent(pageOrFrame, locator, actionName) {
  if ((await locator.count().catch(() => 0)) <= 0) {
    return false
  }
  try {
    await locator.click({ timeout: 5000, force: ['outerLogin', 'qrTab'].includes(actionName) })
    return true
  } catch {
    return false
  }
}

async function showLoginFrame(page) {
  const clickedOuter =
    (await clickIfPresent(page, page.locator('.login-content').first(), 'outerLogin')) ||
    (await clickIfPresent(page, page.locator("img[src*='icon-login']").first(), 'outerLogin')) ||
    (await clickIfPresent(page, page.getByText('其他平台登录').first(), 'outerLogin'))
  await page.waitForTimeout(clickedOuter ? 2000 : 1000)

  const loginFrame = page.frames().find((frame) => frame.url().includes('LoginFrameFC.php'))
  if (!loginFrame) {
    return { loginFrame: null, clickedOuter, clickedQrTab: false, forcedQrPanel: false }
  }
  const clickedQrTab =
    (await clickIfPresent(loginFrame, loginFrame.locator('#nav_btn_code2d').first(), 'qrTab')) ||
    (await clickIfPresent(loginFrame, loginFrame.locator('.btn_code2d a').first(), 'qrTab')) ||
    (await clickIfPresent(loginFrame, loginFrame.getByText('二维码', { exact: true }).first(), 'qrTab'))
  if (clickedQrTab) {
    await page.waitForTimeout(2000)
  }
  const forcedQrPanel = await forceShowQrPanel(loginFrame)
  let dynamicQrImage = false
  if (forcedQrPanel) {
    dynamicQrImage = await waitForDynamicQrImage(loginFrame)
    await page.waitForTimeout(500)
  }
  return { loginFrame, clickedOuter, clickedQrTab, forcedQrPanel, dynamicQrImage }
}

async function forceShowQrPanel(frame) {
  try {
    const hasPanel = await frame.locator('#tbody_code2').count()
    if (!hasPanel) {
      return false
    }
    await frame.evaluate(() => {
      const hideSelectors = ['#tbody_login', '#tbody_btn', '#tbody_checkcode']
      const showSelectors = ['#tbody_code2', '#code2']
      for (const selector of hideSelectors) {
        const element = document.querySelector(selector)
        if (element) {
          element.style.display = 'none'
        }
      }
      for (const selector of showSelectors) {
        const element = document.querySelector(selector)
        if (element) {
          element.style.display = 'block'
          element.style.visibility = 'visible'
          element.style.opacity = '1'
        }
      }
      const panel = document.querySelector('#code2')
      if (panel) {
        panel.style.width = '360px'
        panel.style.minHeight = '220px'
        panel.style.padding = '12px 14px 16px'
        panel.style.background = '#fff'
        panel.style.boxSizing = 'border-box'
        panel.style.textAlign = 'center'
      }
      const notice = document.querySelector('#code2 .code2d_notice')
      if (notice) {
        notice.innerHTML = '<span style="color:#e60039;font-weight:bold;">Daoyu / WeChat</span> scan only<br><span style="color:#666;font-size:12px;">Do NOT use QQ scanner</span>'
        notice.style.display = 'block'
        notice.style.margin = '0 0 8px'
        notice.style.lineHeight = '1.5'
        notice.style.fontSize = '14px'
      }
      const codeBg = document.querySelector('#code2 .code_bg')
      if (codeBg) {
        codeBg.style.display = 'block'
        codeBg.style.margin = '0 auto'
      }
      const refresh = document.querySelector('#code2 .code_error_tip')
      if (refresh) {
        refresh.style.display = 'block'
        refresh.style.marginTop = '6px'
      }
      if (window.QRCodeBiz && typeof window.QRCodeBiz.Start === 'function') {
        window.QRCodeBiz.Start(true)
      }
    })
    return true
  } catch {
    return false
  }
}

async function waitForDynamicQrImage(frame) {
  if (!frame) {
    return false
  }
  try {
    await frame.waitForFunction(
      () => {
        const src = document.querySelector('#code2 img')?.getAttribute('src') || ''
        return src.includes('/authen/getcodekey.jsonp') || src.includes('getcodekey.jsonp')
      },
      null,
      { timeout: 10000 }
    )
    return true
  } catch {
    return false
  }
}

async function screenshotQr(page, frame, screenshotPath) {
  if (frame) {
    const panel = frame.locator('#code2').first()
    const panelCount = await panel.count().catch(() => 0)
    if (panelCount > 0) {
      await panel.screenshot({ path: screenshotPath })
      return 'frame_qr_code2_panel'
    }
  }
  await page.screenshot({ path: screenshotPath, fullPage: true })
  return 'page'
}

export async function startSqmallQrLoginSession(payload = {}) {
  await cleanupExpiredSqmallQrSessions()
  if (sessions.size >= MAX_SESSIONS) {
    userError(429, 'too many active sqmall qr sessions')
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
      viewport: { width: 430, height: 900 },
      userAgent: USER_AGENT
    })
    const page = await context.newPage()
    const responses = []
    const pendingResponseReads = new Set()
    page.on('response', (response) => {
      const url = response.url()
      if (url.includes('/authen/codeKeyLogin.jsonp')) {
        const readPromise = response
          .text()
          .then((text) => {
            const ticket = ticketFromCodeKeyPayload(parseJsonp(text))
            if (ticket) {
              const session = sessions.get(sessionId)
              if (session) {
                session.loginTicket = ticket
                session.ticketCaptured = true
              }
            }
          })
          .catch(() => {})
          .finally(() => pendingResponseReads.delete(readPromise))
        pendingResponseReads.add(readPromise)
      }
      const isRelevant =
        url.includes('m.qu.sdo.com') ||
        url.includes('sqmallservice.u.sdo.com') ||
        url.includes('login.sdo.com') ||
        url.includes('login.u.sdo.com') ||
        response.status() >= 400
      if (!isRelevant || responses.length >= 40) {
        return
      }
      responses.push({
        status: response.status(),
        url: safeUrl(url)
      })
    })

    await page.goto(payload?.loginUrl || DEFAULT_LOGIN_URL, {
      waitUntil: 'domcontentloaded',
      timeout: payloadNumber(payload, 'timeoutMs', DEFAULT_NAVIGATION_TIMEOUT_MS, {
        min: 10000,
        max: 120000
      })
    })
    await page.waitForTimeout(1000)
    const { loginFrame, clickedOuter, clickedQrTab, forcedQrPanel, dynamicQrImage } = await showLoginFrame(page)

    const screenshotTarget = await screenshotQr(page, loginFrame, screenshotPath)
    const bodyText = await page.locator('body').innerText({ timeout: 3000 }).catch(() => '')
    const loginState = await getSanitizedSqmallLoginState(context)
    const session = {
      id: sessionId,
      browser,
      context,
      page,
      userAgent: USER_AGENT,
      screenshotPath,
      createdAtMs: now,
      expiresAtMs: now + ttlMs,
      clickedOuter,
      clickedQrTab,
      forcedQrPanel,
      dynamicQrImage,
      screenshotTarget,
      pendingResponseReads,
      loginTicket: '',
      ticketCaptured: false,
      mallSessionAttempted: false,
      mallSessionError: '',
      responses
    }
    sessions.set(sessionId, session)

    return {
      sessionId,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      ttlSeconds: Math.round(ttlMs / 1000),
      imagePath: `/sqmall/qr/${sessionId}/image`,
      clickedOuter,
      clickedQrTab,
      forcedQrPanel,
      dynamicQrImage,
      screenshotTarget,
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

export async function getSqmallQrLoginImage(sessionId) {
  const session = getSession(sessionId)
  return {
    contentType: 'image/png',
    body: await readFile(session.screenshotPath)
  }
}

export async function getSqmallQrLoginStatus(sessionId) {
  const session = getSession(sessionId)
  if (session.pendingResponseReads?.size) {
    await Promise.race([
      Promise.allSettled([...session.pendingResponseReads]),
      new Promise((resolve) => setTimeout(resolve, 1000))
    ])
  }
  await establishMallSession(session)
  const loginState = await getSanitizedSqmallLoginState(session.context)
  if (!loginState.loggedIn) {
    return {
      status: 'pending',
      loggedIn: false,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      ticketCaptured: Boolean(session.ticketCaptured),
      mallSessionAttempted: Boolean(session.mallSessionAttempted),
      loginState
    }
  }

  const cookies = await session.context.cookies('https://sqmallservice.u.sdo.com', 'https://m.qu.sdo.com')
  const sessionCookie = cookies.find((cookie) => cookie.name === 'sessionId')
  const data = await getRawSqmallLoginData(session.context)
  const memberId = String(data.direbmemllam || '').trim()
  if (!sessionCookie?.value || !memberId) {
    return {
      status: 'pending',
      loggedIn: false,
      expiresAt: new Date(session.expiresAtMs).toISOString(),
      loginState: {
        ...loginState,
        resultMsg: '已登录，但尚未取得商城 sessionId 或会员标识'
      }
    }
  }

  await closeSession(session)
  return {
    status: 'success',
    loggedIn: true,
    loginState,
    credential: {
      kind: 'sqmall-session',
      sessionId: sessionCookie.value,
      memberId,
      displayName: String(data.emankcin || data.nickName || data.showUsername || memberId)
    }
  }
}

export async function cancelSqmallQrLoginSession(sessionId) {
  const session = getSession(sessionId)
  await closeSession(session)
  return {
    status: 'cancelled',
    sessionId: session.id
  }
}
