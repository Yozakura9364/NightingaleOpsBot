import { createServer } from 'node:http'
import { randomInt } from 'node:crypto'
import { listJobs, getJob, paths, prepareJob, runJob } from './jobs.mjs'
import {
  cancelQrLoginSession,
  cleanupExpiredQrSessions,
  getQrLoginImage,
  getQrLoginStatus,
  startQrLoginSession
} from './risingstoneQrSessions.mjs'
import {
  cancelSqmallQrLoginSession,
  cleanupExpiredSqmallQrSessions,
  getSqmallQrLoginImage,
  getSqmallQrLoginStatus,
  startSqmallQrLoginSession
} from './sqmallQrSessions.mjs'

const HOST = process.env.NS_OPS_HOST || '127.0.0.1'
const PORT = Number.parseInt(process.env.NS_OPS_PORT || '18766', 10)
const TOKEN = process.env.NS_OPS_TOKEN || ''
const CONFIRM_TTL_MS = Number.parseInt(process.env.NS_OPS_CONFIRM_TTL_MS || '120000', 10)
const MAX_BODY_BYTES = 1024 * 1024

const pendingConfirmations = new Map()
let latestResult = null

function sendJson(response, statusCode, payload) {
  const body = `${JSON.stringify(payload, null, 2)}\n`
  response.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store'
  })
  response.end(body)
}

function sendBinary(response, statusCode, contentType, body) {
  response.writeHead(statusCode, {
    'content-type': contentType,
    'cache-control': 'no-store'
  })
  response.end(body)
}

function isAuthorized(request) {
  if (!TOKEN) {
    return false
  }
  const auth = request.headers.authorization || ''
  const bearer = auth.toLowerCase().startsWith('bearer ') ? auth.slice(7).trim() : ''
  const headerToken = request.headers['x-ns-ops-token'] || ''
  return bearer === TOKEN || headerToken === TOKEN
}

function requireAuth(request, response) {
  if (!TOKEN) {
    sendJson(response, 503, { ok: false, error: 'NS_OPS_TOKEN is not configured' })
    return false
  }
  if (isAuthorized(request)) {
    return true
  }
  sendJson(response, 401, { ok: false, error: 'unauthorized' })
  return false
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let body = ''
    request.on('data', (chunk) => {
      body += chunk.toString('utf8')
      if (body.length > MAX_BODY_BYTES) {
        reject(new Error('request body too large'))
        request.destroy()
      }
    })
    request.on('end', () => {
      if (!body.trim()) {
        resolve({})
        return
      }
      try {
        resolve(JSON.parse(body))
      } catch (error) {
        reject(error)
      }
    })
    request.on('error', reject)
  })
}

function jobIdFromPath(pathname) {
  return pathname.replace(/^\/jobs\/?/, '').replace(/\//g, '.').replace(/^\.+|\.+$/g, '')
}

function cleanupConfirmations() {
  const now = Date.now()
  for (const [token, pending] of pendingConfirmations.entries()) {
    if (pending.expiresAt <= now) {
      pendingConfirmations.delete(token)
    }
  }
}

function createConfirmation(jobId, payload = {}) {
  cleanupConfirmations()
  const token = String(randomInt(100000, 999999))
  const now = Date.now()
  pendingConfirmations.set(token, {
    jobId,
    payload,
    createdAt: now,
    expiresAt: now + CONFIRM_TTL_MS
  })
  return {
    token,
    expiresAt: new Date(now + CONFIRM_TTL_MS).toISOString(),
    ttlSeconds: Math.round(CONFIRM_TTL_MS / 1000)
  }
}

async function executeJob(jobId, payload = {}) {
  const result = await runJob(jobId, payload)
  latestResult = result
  return result
}

async function handleRequest(request, response) {
  const url = new URL(request.url || '/', `http://${request.headers.host || `${HOST}:${PORT}`}`)
  const pathname = url.pathname

  if (request.method === 'GET' && pathname === '/health') {
    sendJson(response, 200, {
      ok: true,
      service: 'ns-ops-runner',
      version: '0.1.0',
      pid: process.pid,
      uptimeSeconds: Math.round(process.uptime()),
      paths,
      authRequiredForJobs: true,
      tokenConfigured: Boolean(TOKEN)
    })
    return
  }

  if (!requireAuth(request, response)) {
    return
  }

  try {
    if (request.method === 'POST' && pathname === '/risingstone/qr/start') {
      const payload = await readBody(request)
      sendJson(response, 200, { ok: true, session: await startQrLoginSession(payload) })
      return
    }

    const qrMatch = pathname.match(/^\/risingstone\/qr\/([^/]+)(?:\/(image|status))?$/)
    if (qrMatch) {
      const [, sessionId, action] = qrMatch
      if (request.method === 'GET' && action === 'image') {
        const image = await getQrLoginImage(sessionId)
        sendBinary(response, 200, image.contentType, image.body)
        return
      }
      if (request.method === 'GET' && action === 'status') {
        sendJson(response, 200, { ok: true, session: await getQrLoginStatus(sessionId) })
        return
      }
      if (request.method === 'DELETE' && !action) {
        sendJson(response, 200, { ok: true, session: await cancelQrLoginSession(sessionId) })
        return
      }
    }

    if (request.method === 'POST' && pathname === '/sqmall/qr/start') {
      const payload = await readBody(request)
      sendJson(response, 200, { ok: true, session: await startSqmallQrLoginSession(payload) })
      return
    }

    const sqmallQrMatch = pathname.match(/^\/sqmall\/qr\/([^/]+)(?:\/(image|status))?$/)
    if (sqmallQrMatch) {
      const [, sessionId, action] = sqmallQrMatch
      if (request.method === 'GET' && action === 'image') {
        const image = await getSqmallQrLoginImage(sessionId)
        sendBinary(response, 200, image.contentType, image.body)
        return
      }
      if (request.method === 'GET' && action === 'status') {
        sendJson(response, 200, { ok: true, session: await getSqmallQrLoginStatus(sessionId) })
        return
      }
      if (request.method === 'DELETE' && !action) {
        sendJson(response, 200, { ok: true, session: await cancelSqmallQrLoginSession(sessionId) })
        return
      }
    }

    if (request.method === 'GET' && pathname === '/jobs') {
      sendJson(response, 200, { ok: true, jobs: listJobs() })
      return
    }

    if (request.method === 'GET' && pathname === '/jobs/latest') {
      sendJson(response, 200, { ok: true, latest: latestResult })
      return
    }

    if (request.method === 'POST' && pathname.startsWith('/jobs/')) {
      const payload = await readBody(request)
      const jobId = jobIdFromPath(pathname)
      const job = getJob(jobId)
      if (!job) {
        sendJson(response, 404, { ok: false, error: `unknown job: ${jobId}` })
        return
      }

      if (job.requiresConfirmation) {
        const prepared = await prepareJob(jobId, payload)
        const confirmation = createConfirmation(jobId, prepared.payload || {})
        sendJson(response, 202, {
          ok: false,
          confirmationRequired: true,
          jobId,
          title: job.title,
          message: `Reply with /ns confirm ${confirmation.token} within ${confirmation.ttlSeconds}s.`,
          preview: prepared.preview || '',
          confirmation
        })
        return
      }

      sendJson(response, 200, { ok: true, result: await executeJob(jobId, payload) })
      return
    }

    if (request.method === 'POST' && pathname === '/confirm') {
      const payload = await readBody(request)
      const token = String(payload.token || '').trim()
      cleanupConfirmations()
      const pending = pendingConfirmations.get(token)
      if (!pending) {
        sendJson(response, 404, { ok: false, error: 'confirmation not found or expired' })
        return
      }

      pendingConfirmations.delete(token)
      sendJson(response, 200, { ok: true, result: await executeJob(pending.jobId, pending.payload) })
      return
    }

    sendJson(response, 404, { ok: false, error: 'not found' })
  } catch (error) {
    sendJson(response, error.statusCode || 500, {
      ok: false,
      error: error instanceof Error ? error.message : String(error)
    })
  }
}

const server = createServer((request, response) => {
  void handleRequest(request, response)
})

server.listen(PORT, HOST, () => {
  console.log(`[ns-ops-runner] listening on http://${HOST}:${PORT}`)
  if (!process.env.NS_OPS_TOKEN) {
    console.warn('[ns-ops-runner] NS_OPS_TOKEN is not set; job endpoints are disabled.')
  }
})

setInterval(() => {
  void cleanupExpiredQrSessions()
  void cleanupExpiredSqmallQrSessions()
}, 30000).unref()
