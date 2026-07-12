import { spawn } from 'node:child_process'
import { cpus, loadavg } from 'node:os'
import tls from 'node:tls'
import { getTencentCloudTrafficSnapshot } from './tencentCloudTraffic.mjs'

const DEFAULT_CONTAINERS = ['astrbot', 'napcat', 'rsshub']
const DEFAULT_SERVICES = [
  'nightingale-ops-runner',
  'nsglamour',
  'nightingale-xproxy-relay',
  'docker'
]
const DEFAULT_HTTP_TARGETS = [
  ['AstrBot', 'http://127.0.0.1:6185/'],
  ['NapCat', 'http://127.0.0.1:6099/'],
  ['NSGlamour', 'http://127.0.0.1:8765/']
]

function envNumber(name, fallback) {
  const value = Number.parseFloat(String(process.env[name] || '').trim())
  return Number.isFinite(value) ? value : fallback
}

function envBoolean(name, fallback = false) {
  const raw = String(process.env[name] || '').trim().toLowerCase()
  if (!raw) {
    return fallback
  }
  return ['1', 'true', 'yes', 'on'].includes(raw)
}

function parseList(value, fallback) {
  const raw = String(value || '').trim()
  if (!raw) {
    return fallback
  }
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function parseNamedTargets(value, fallback) {
  const raw = String(value || '').trim()
  if (!raw) {
    return fallback
  }
  return raw
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => {
      const separator = item.indexOf('=')
      if (separator === -1) {
        return [item, item]
      }
      return [item.slice(0, separator).trim(), item.slice(separator + 1).trim()]
    })
    .filter(([, url]) => url)
}

function stripAnsi(text) {
  return String(text ?? '').replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '')
}

function redact(text) {
  return stripAnsi(text)
    .replace(/(Authorization:\s*Bearer\s+)[^\s]+/gi, '$1[redacted]')
    .replace(/(access[_-]?token["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/(api[_-]?key["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/(secret[_-]?(?:id|key)?["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/(cookie["']?\s*[:=]\s*["']?)[^"'\n]+/gi, '$1[redacted]')
    .replace(/sk-[A-Za-z0-9_-]{20,}/g, 'sk-[redacted]')
}

function run(command, args = [], options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      shell: false,
      windowsHide: true,
      env: { ...process.env, LC_ALL: 'C', LANG: 'C' }
    })
    let stdout = ''
    let stderr = ''
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      child.kill('SIGTERM')
    }, options.timeoutMs || 10000)

    child.stdout?.on('data', (chunk) => {
      stdout += chunk.toString('utf8')
    })
    child.stderr?.on('data', (chunk) => {
      stderr += chunk.toString('utf8')
    })
    child.on('error', (error) => {
      clearTimeout(timer)
      resolve({ ok: false, stdout: '', stderr: redact(error.message), exitCode: null, timedOut })
    })
    child.on('close', (exitCode) => {
      clearTimeout(timer)
      resolve({
        ok: exitCode === 0 && !timedOut,
        stdout: redact(stdout),
        stderr: redact(stderr),
        exitCode,
        timedOut
      })
    })
  })
}

function fmt(value, digits = 1) {
  return Number.isFinite(value) ? value.toFixed(digits) : '-'
}

function bytesToGb(value) {
  return value / 1024 / 1024 / 1024
}

function fmtBytes(value) {
  return Number.isFinite(value) ? `${fmt(bytesToGb(value), 2)} GB` : '-'
}

function parseDf(stdout) {
  const lines = stdout.trim().split('\n').filter(Boolean)
  return lines.slice(1).map((line) => {
    const parts = line.trim().split(/\s+/)
    const usedPercent = Number.parseFloat((parts[4] || '').replace('%', ''))
    return {
      filesystem: parts[0] || '-',
      sizeBytes: Number.parseInt(parts[1] || '0', 10),
      usedBytes: Number.parseInt(parts[2] || '0', 10),
      availableBytes: Number.parseInt(parts[3] || '0', 10),
      usedPercent,
      mount: parts[5] || '-'
    }
  })
}

async function diskSnapshot() {
  const paths = parseList(process.env.NS_HEALTH_DISK_PATHS, ['/'])
  const result = await run('df', ['-P', '-B1', ...paths])
  if (!result.ok) {
    return { ok: false, error: result.stderr || result.stdout || 'df failed', disks: [] }
  }
  return { ok: true, disks: parseDf(result.stdout) }
}

function currentMemorySnapshot() {
  const memory = process.memoryUsage()
  return {
    processRssBytes: memory.rss
  }
}

async function memorySnapshot() {
  const result = await run('free', ['-b'])
  if (!result.ok) {
    return { ok: false, error: result.stderr || result.stdout || 'free failed' }
  }
  const line = result.stdout
    .split('\n')
    .find((item) => item.toLowerCase().startsWith('mem:'))
  const parts = line?.trim().split(/\s+/) || []
  const totalBytes = Number.parseInt(parts[1] || '0', 10)
  const usedBytes = Number.parseInt(parts[2] || '0', 10)
  const availableBytes = Number.parseInt(parts[6] || '0', 10)
  const usedPercent = totalBytes ? (usedBytes / totalBytes) * 100 : Number.NaN
  const sarPeakPercent = await memoryPeakPercent()

  return {
    ok: true,
    totalBytes,
    usedBytes,
    availableBytes,
    usedPercent,
    process: currentMemorySnapshot(),
    peakPercent: Number.isFinite(sarPeakPercent)
      ? Math.max(usedPercent, sarPeakPercent)
      : usedPercent
  }
}

async function cpuPeakPercent() {
  const result = await run('sar', ['-u', '-s', '00:00:00'], { timeoutMs: 15000 })
  if (!result.ok) {
    return Number.NaN
  }
  let idleIndex = -1
  let maxUsage = Number.NaN
  for (const line of result.stdout.split('\n')) {
    const parts = line.trim().split(/\s+/).filter(Boolean)
    if (parts.includes('%idle')) {
      idleIndex = parts.indexOf('%idle')
      continue
    }
    if (idleIndex >= 0 && parts.includes('all')) {
      const idle = Number.parseFloat(parts[idleIndex])
      if (Number.isFinite(idle)) {
        const usage = Math.max(0, 100 - idle)
        maxUsage = Number.isFinite(maxUsage) ? Math.max(maxUsage, usage) : usage
      }
    }
  }
  return maxUsage
}

async function memoryPeakPercent() {
  const result = await run('sar', ['-r', '-s', '00:00:00'], { timeoutMs: 15000 })
  if (!result.ok) {
    return Number.NaN
  }
  let percentIndex = -1
  let maxPercent = Number.NaN
  for (const line of result.stdout.split('\n')) {
    const parts = line.trim().split(/\s+/).filter(Boolean)
    if (parts.includes('%memused')) {
      percentIndex = parts.indexOf('%memused')
      continue
    }
    if (percentIndex >= 0 && parts.length > percentIndex && !line.startsWith('Average:')) {
      const percent = Number.parseFloat(parts[percentIndex])
      if (Number.isFinite(percent)) {
        maxPercent = Number.isFinite(maxPercent) ? Math.max(maxPercent, percent) : percent
      }
    }
  }
  return maxPercent
}

async function cpuSnapshot() {
  const loads = loadavg()
  return {
    ok: true,
    cores: cpus().length || 1,
    load1: loads[0],
    load5: loads[1],
    load15: loads[2],
    peakPercent: await cpuPeakPercent()
  }
}

async function dockerSnapshot() {
  const names = parseList(process.env.NS_HEALTH_DOCKER_CONTAINERS, DEFAULT_CONTAINERS)
  const result = await run('docker', ['inspect', ...names], { timeoutMs: 15000 })
  if (!result.ok) {
    return {
      ok: false,
      error: result.stderr || result.stdout || 'docker inspect failed',
      containers: names.map((name) => ({ name, ok: false, status: 'missing' }))
    }
  }
  const inspected = JSON.parse(result.stdout)
  const containers = inspected.map((item) => {
    const state = item.State || {}
    const name = String(item.Name || '').replace(/^\//, '') || item.Config?.Hostname || '-'
    const health = state.Health?.Status || ''
    const ok = state.Status === 'running' && (!health || health === 'healthy')
    return {
      name,
      ok,
      status: state.Status || '-',
      health,
      restartCount: item.RestartCount || 0
    }
  })
  return { ok: containers.every((container) => container.ok), containers }
}

async function servicesSnapshot() {
  const services = parseList(process.env.NS_HEALTH_SYSTEMD_SERVICES, DEFAULT_SERVICES)
  const results = []
  for (const service of services) {
    const result = await run('systemctl', ['is-active', service], { timeoutMs: 5000 })
    const status = result.stdout.trim() || result.stderr.trim() || 'unknown'
    results.push({ name: service, ok: status === 'active', status })
  }
  return { ok: results.every((service) => service.ok), services: results }
}

async function httpSnapshot() {
  const targets = parseNamedTargets(process.env.NS_HEALTH_HTTP_URLS, DEFAULT_HTTP_TARGETS)
  const checks = []
  for (const [name, url] of targets) {
    const started = Date.now()
    try {
      const response = await fetch(url, {
        method: 'GET',
        redirect: 'manual',
        signal: AbortSignal.timeout(8000)
      })
      const latencyMs = Date.now() - started
      checks.push({
        name,
        url,
        ok: response.status < 500,
        status: response.status,
        latencyMs
      })
    } catch (error) {
      checks.push({
        name,
        url,
        ok: false,
        status: '-',
        latencyMs: Date.now() - started,
        error: error.message
      })
    }
  }
  return { ok: checks.every((check) => check.ok), checks }
}

function tlsExpiry(host, port = 443) {
  return new Promise((resolve) => {
    const socket = tls.connect(
      {
        host,
        port,
        servername: host,
        rejectUnauthorized: false,
        timeout: 8000
      },
      () => {
        const cert = socket.getPeerCertificate()
        socket.end()
        const validTo = cert?.valid_to ? new Date(cert.valid_to) : null
        const daysRemaining = validTo
          ? (validTo.getTime() - Date.now()) / 1000 / 60 / 60 / 24
          : Number.NaN
        resolve({ host, ok: Number.isFinite(daysRemaining) && daysRemaining > 0, validTo, daysRemaining })
      }
    )
    socket.on('timeout', () => {
      socket.destroy()
      resolve({ host, ok: false, error: 'timeout' })
    })
    socket.on('error', (error) => {
      resolve({ host, ok: false, error: error.message })
    })
  })
}

async function certificateSnapshot() {
  const configured = parseList(process.env.NS_HEALTH_TLS_HOSTS, [])
  const httpsTargets = parseNamedTargets(process.env.NS_HEALTH_HTTP_URLS, DEFAULT_HTTP_TARGETS)
    .map(([, url]) => {
      try {
        const parsed = new URL(url)
        return parsed.protocol === 'https:' ? parsed.hostname : ''
      } catch {
        return ''
      }
    })
    .filter(Boolean)
  const hosts = Array.from(new Set([...configured, ...httpsTargets]))
  if (hosts.length === 0) {
    return { ok: true, checks: [], skipped: true }
  }
  const checks = await Promise.all(hosts.map((host) => tlsExpiry(host)))
  const warnDays = envNumber('NS_HEALTH_CERT_WARN_DAYS', 14)
  return {
    ok: checks.every((check) => check.ok && check.daysRemaining >= warnDays),
    checks
  }
}

async function recentErrorsSnapshot() {
  const journal = await run(
    'journalctl',
    ['-p', 'err..alert', '--since', '24 hours ago', '--no-pager', '-n', '20'],
    { timeoutMs: 15000 }
  )
  const journalLines = journal.ok
    ? journal.stdout
        .split('\n')
        .map((line) => line.trim())
        .filter(Boolean)
        .filter((line) => !line.includes('-- No entries --'))
    : []

  const containers = parseList(process.env.NS_HEALTH_DOCKER_CONTAINERS, DEFAULT_CONTAINERS)
  const dockerLines = []
  const pattern = /traceback|exception|fatal|unhandled|critical|error|502|panic/i
  for (const container of containers) {
    const result = await run('docker', ['logs', '--since', '24h', '--tail', '200', container], {
      timeoutMs: 15000
    })
    const lines = `${result.stdout}\n${result.stderr}`
      .split('\n')
      .map((line) => line.trim())
      .filter((line) => pattern.test(line))
      .slice(-5)
      .map((line) => `${container}: ${line}`)
    dockerLines.push(...lines)
  }

  const lines = [...journalLines, ...dockerLines].slice(-12)
  return {
    ok: lines.length === 0,
    count: lines.length,
    lines: lines.slice(-5)
  }
}

async function trafficSnapshot() {
  try {
    const traffic = await getTencentCloudTrafficSnapshot()
    return { ok: true, traffic }
  } catch (error) {
    return { ok: false, error: error.message }
  }
}

function addIssue(issues, level, message) {
  issues.push({ level, message })
}

function buildIssues(snapshot) {
  const issues = []
  const diskWarn = envNumber('NS_HEALTH_DISK_WARN_PERCENT', 85)
  const memoryWarn = envNumber('NS_HEALTH_MEMORY_WARN_PERCENT', 90)
  const cpuWarn = envNumber('NS_HEALTH_CPU_WARN_PERCENT', 95)
  const trafficWarn = envNumber('NS_HEALTH_TRAFFIC_REMAINING_WARN_PERCENT', 20)
  const certWarn = envNumber('NS_HEALTH_CERT_WARN_DAYS', 14)

  for (const disk of snapshot.disk.disks || []) {
    if (disk.usedPercent >= diskWarn) {
      addIssue(issues, 'critical', `磁盘 ${disk.mount} 使用率 ${fmt(disk.usedPercent)}%`)
    }
  }
  if (snapshot.memory.ok && snapshot.memory.usedPercent >= memoryWarn) {
    addIssue(issues, 'warning', `内存使用率 ${fmt(snapshot.memory.usedPercent)}%`)
  }
  if (snapshot.cpu.ok && snapshot.cpu.peakPercent >= cpuWarn) {
    addIssue(issues, 'warning', `今日 CPU 峰值 ${fmt(snapshot.cpu.peakPercent)}%`)
  }
  for (const container of snapshot.docker.containers || []) {
    if (!container.ok) {
      addIssue(issues, 'critical', `容器 ${container.name} 状态 ${container.status}`)
    }
  }
  for (const service of snapshot.services.services || []) {
    if (!service.ok) {
      addIssue(issues, 'critical', `服务 ${service.name} 状态 ${service.status}`)
    }
  }
  for (const check of snapshot.http.checks || []) {
    if (!check.ok) {
      addIssue(issues, 'critical', `站点 ${check.name} HTTP ${check.status}${check.error ? ` ${check.error}` : ''}`)
    }
  }
  for (const check of snapshot.certificates.checks || []) {
    if (!check.ok || check.daysRemaining < certWarn) {
      addIssue(issues, 'warning', `证书 ${check.host} 剩余 ${fmt(check.daysRemaining)} 天`)
    }
  }
  if (!snapshot.traffic.ok) {
    addIssue(issues, 'warning', `腾讯云流量读取失败：${snapshot.traffic.error}`)
  } else if (snapshot.traffic.traffic.remainingPercent < trafficWarn) {
    addIssue(
      issues,
      'critical',
      `流量包剩余 ${fmt(snapshot.traffic.traffic.remainingPercent, 2)}%`
    )
  }
  if (!snapshot.errors.ok && envBoolean('NS_HEALTH_ERROR_LOG_ALERT', false)) {
    addIssue(issues, 'warning', `最近 24h 发现 ${snapshot.errors.count} 条错误日志`)
  }
  return issues
}

export async function getSystemHealthSnapshot() {
  const [disk, memory, cpu, docker, services, http, certificates, errors, traffic] =
    await Promise.all([
      diskSnapshot(),
      memorySnapshot(),
      cpuSnapshot(),
      dockerSnapshot(),
      servicesSnapshot(),
      httpSnapshot(),
      certificateSnapshot(),
      recentErrorsSnapshot(),
      trafficSnapshot()
    ])
  const snapshot = {
    checkedAt: new Date().toISOString(),
    disk,
    memory,
    cpu,
    docker,
    services,
    http,
    certificates,
    errors,
    traffic
  }
  snapshot.issues = buildIssues(snapshot)
  snapshot.ok = snapshot.issues.length === 0
  snapshot.signature = snapshot.issues.map((issue) => `${issue.level}:${issue.message}`).join('|')
  return snapshot
}

function statusLabel(ok) {
  return ok ? '正常' : '异常'
}

function formatTraffic(snapshot) {
  if (!snapshot.traffic.ok) {
    return `流量：读取失败（${snapshot.traffic.error}）`
  }
  const { packages, packagePercent, remainingPercent } = snapshot.traffic.traffic
  return `流量：${fmtBytes(packages.usedBytes)} / ${fmtBytes(packages.totalBytes)}，剩余 ${fmtBytes(packages.remainingBytes)}（${fmt(remainingPercent, 2)}%）`
}

function formatDisk(snapshot) {
  if (!snapshot.disk.ok) {
    return `磁盘：读取失败（${snapshot.disk.error}）`
  }
  return `磁盘：${snapshot.disk.disks
    .map((disk) => `${disk.mount} ${fmt(disk.usedPercent)}%（剩余 ${fmtBytes(disk.availableBytes)}）`)
    .join('；')}`
}

function formatMemory(snapshot) {
  if (!snapshot.memory.ok) {
    return `内存：读取失败（${snapshot.memory.error}）`
  }
  return `内存：当前 ${fmt(snapshot.memory.usedPercent)}%，今日峰值 ${fmt(snapshot.memory.peakPercent)}%`
}

function formatCpu(snapshot) {
  if (!snapshot.cpu.ok) {
    return 'CPU：读取失败'
  }
  return `CPU：负载 ${fmt(snapshot.cpu.load1, 2)} / ${snapshot.cpu.cores} 核，今日峰值 ${fmt(snapshot.cpu.peakPercent)}%`
}

function formatContainers(snapshot) {
  const bad = (snapshot.docker.containers || []).filter((container) => !container.ok)
  if (!snapshot.docker.ok) {
    return `容器：异常（${bad.map((container) => `${container.name}:${container.status}`).join('，')}）`
  }
  return `容器：${(snapshot.docker.containers || []).map((container) => container.name).join(' / ')} 正常`
}

function formatServices(snapshot) {
  const bad = (snapshot.services.services || []).filter((service) => !service.ok)
  if (!snapshot.services.ok) {
    return `服务：异常（${bad.map((service) => `${service.name}:${service.status}`).join('，')}）`
  }
  return `服务：${(snapshot.services.services || []).map((service) => service.name).join(' / ')} 正常`
}

function formatHttp(snapshot) {
  const checks = snapshot.http.checks || []
  if (checks.length === 0) {
    return '站点：未配置'
  }
  return `站点：${checks
    .map((check) => `${check.name} ${check.status}${check.ok ? '' : ' 异常'}`)
    .join('，')}`
}

function formatCertificates(snapshot) {
  if (snapshot.certificates.skipped) {
    return '证书：未配置 HTTPS 检查'
  }
  return `证书：${(snapshot.certificates.checks || [])
    .map((check) => `${check.host} 剩余 ${fmt(check.daysRemaining)} 天`)
    .join('，')}`
}

function formatErrors(snapshot) {
  if (snapshot.errors.ok) {
    return '异常日志：无'
  }
  return ['异常日志：', ...snapshot.errors.lines.map((line) => `- ${line}`)].join('\n')
}

export function formatSystemHealth(snapshot, { daily = false } = {}) {
  const lines = [
    daily ? 'NS 每日状态' : 'NS 综合健康检查',
    `状态：${statusLabel(snapshot.ok)}`,
    `时间：${new Date(snapshot.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}`,
    '',
    formatTraffic(snapshot),
    formatDisk(snapshot),
    formatMemory(snapshot),
    formatCpu(snapshot),
    formatContainers(snapshot),
    formatServices(snapshot),
    formatHttp(snapshot),
    formatCertificates(snapshot),
    formatErrors(snapshot)
  ]
  if (snapshot.issues.length) {
    lines.push('', '需要关注：', ...snapshot.issues.map((issue) => `- ${issue.message}`))
  }
  return lines.join('\n')
}

export async function getSystemHealthReport() {
  return formatSystemHealth(await getSystemHealthSnapshot())
}

export async function getSystemDailyReport() {
  return formatSystemHealth(await getSystemHealthSnapshot(), { daily: true })
}

export async function getSystemAlertsReport() {
  const snapshot = await getSystemHealthSnapshot()
  if (snapshot.ok) {
    return 'OK'
  }
  return [
    'NS 异常告警',
    `签名：${snapshot.signature || '-'}`,
    `时间：${new Date(snapshot.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })}`,
    '',
    ...snapshot.issues.map((issue) => `- ${issue.message}`),
    '',
    '发送 /ns health 查看完整状态。'
  ].join('\n')
}
