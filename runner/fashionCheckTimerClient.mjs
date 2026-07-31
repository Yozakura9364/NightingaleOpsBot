const host = String(process.env.NS_OPS_HOST || '127.0.0.1').trim()
const port = Number(process.env.NS_OPS_PORT || 18766)
const token = String(process.env.NS_OPS_TOKEN || '').trim()

if (!token) {
  console.error('NS_OPS_TOKEN is not configured')
  process.exit(1)
}

const controller = new AbortController()
const timer = setTimeout(() => controller.abort(), 180000)
try {
  const response = await fetch(`http://${host}:${port}/jobs/fashion-check/tick`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: '{}',
    signal: controller.signal
  })
  const payload = await response.json()
  if (!response.ok || !payload.ok || !payload.result?.ok) {
    throw new Error(
      payload.error || payload.result?.summary || `runner returned HTTP ${response.status}`
    )
  }
  console.log(payload.result.summary || 'fashion check tick completed')
} catch (error) {
  console.error(`fashion check timer failed: ${error instanceof Error ? error.message : error}`)
  process.exitCode = 1
} finally {
  clearTimeout(timer)
}
