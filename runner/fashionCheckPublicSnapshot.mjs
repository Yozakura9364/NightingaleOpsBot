import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { getFashionCheckAnswer as getStagingAnswer } from './fashionCheckCollector.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(process.env.NS_OPS_PROJECT_ROOT || resolve(__dirname, '..'))
const STORAGE_ROOT = resolve(
  process.env.NS_FASHION_CHECK_ROOT || resolve(PROJECT_ROOT, '.local', 'fashion-check')
)
const PUBLIC_BASE = (process.env.NS_FASHION_CHECK_PUBLIC_BASE || 'https://www.nightingalesilence.com/data/fashion-check').replace(/\/$/, '')
const CACHE_PATH = resolve(STORAGE_ROOT, 'public-snapshot.json')
const CURRENT_SCHEMA = 'fashion-check.public-current.v5'
const LOCALES_SCHEMA = 'fashion-check.current-locales.v3'
const SLOT_ORDER = ['weapon', 'head', 'body', 'hands', 'legs', 'feet']

async function readJson(path, fallback) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (error) {
    if (error?.code === 'ENOENT') return fallback
    throw error
  }
}

async function writeJsonAtomic(path, value) {
  await mkdir(dirname(path), { recursive: true })
  const tempPath = `${path}.${process.pid}.${randomUUID()}.tmp`
  await writeFile(tempPath, `${JSON.stringify(value, null, 2)}\n`, 'utf8')
  await rename(tempPath, path)
}

async function fetchJson(path) {
  const response = await fetch(`${PUBLIC_BASE}/${path}?t=${Date.now()}`, {
    headers: { 'cache-control': 'no-store' }
  })
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`)
  return response.json()
}

export function validatePublicSnapshot(current, locales) {
  const errors = []
  if (current?.schemaVersion !== CURRENT_SCHEMA) errors.push(`current schemaVersion 应为 ${CURRENT_SCHEMA}`)
  if (locales?.schemaVersion !== LOCALES_SCHEMA) errors.push(`locales schemaVersion 应为 ${LOCALES_SCHEMA}`)
  if (!Number.isInteger(current?.globalIssue) || current.globalIssue <= 0) errors.push('globalIssue 必须是正整数')
  if (!Number.isInteger(current?.cnIssue) || current.cnIssue <= 0) errors.push('cnIssue 必须是正整数')
  const start = Date.parse(current?.challengeWindow?.startsAt ?? '')
  const end = Date.parse(current?.challengeWindow?.endsAt ?? '')
  if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end) errors.push('challengeWindow 起止时间无效')
  if (!Array.isArray(current?.slots) || current.slots.length === 0) errors.push('slots 必须是非空数组')
  return errors
}

async function fetchPublicSnapshot() {
  const [current, locales] = await Promise.all([fetchJson('current.json'), fetchJson('current-locales.json')])
  const errors = validatePublicSnapshot(current, locales)
  if (errors.length) throw new Error(`公开快照校验失败：${errors.join('；')}`)
  const snapshot = { fetchedAt: new Date().toISOString(), current, locales }
  await writeJsonAtomic(CACHE_PATH, snapshot)
  return snapshot
}

export async function getPublicSnapshot() {
  try {
    return { snapshot: await fetchPublicSnapshot(), origin: 'live' }
  } catch (error) {
    const cache = await readJson(CACHE_PATH, null)
    if (!cache?.current || !cache?.locales) throw error
    return { snapshot: cache, origin: 'cache' }
  }
}

function zhName(table, id, fallback = '') {
  const entry = table?.[String(id)]
  return String(entry?.['zh-CN'] || fallback || '').trim()
}

function formatWindow(current) {
  const start = new Date(current.challengeWindow.startsAt)
  const end = new Date(current.challengeWindow.endsAt)
  const fmt = (d) => `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return `${fmt(start)} ~ ${fmt(end)}`
}

function orderSlots(slots) {
  return [...slots].sort((a, b) => SLOT_ORDER.indexOf(a.slotId) - SLOT_ORDER.indexOf(b.slotId))
}

const SLOT_LABELS = { weapon: '武器', head: '头部', body: '身体', hands: '手部', legs: '腿部', feet: '脚部' }
function slotLabel(slotId) {
  return SLOT_LABELS[slotId] ?? slotId
}

function entryName(entry, locales) {
  if (entry.item) return zhName(locales.items, entry.item.itemId, entry.item.name)
  const label = String(entry.label || '').trim()
  if (label) return label
  if (typeof entry.labelKey === 'string' && entry.labelKey.startsWith('fashionCheck.anyDyeable')) {
    const slot = entry.labelKey.replace('fashionCheck.anyDyeable', '')
    const slotKey = slot.charAt(0).toLowerCase() + slot.slice(1)
    return `任意可染色${slotLabel(slotKey)}装备`
  }
  return ''
}

function clampPublicAnswer(text, maxChars = 3200) {
  if (text.length <= maxChars) return text
  const output = []
  let length = 0
  for (const line of text.split('\n')) {
    if (length + line.length + 1 > maxChars - 20) break
    output.push(line)
    length += line.length + 1
  }
  output.push('...[答案过长，已截断]')
  return output.join('\n')
}

export function formatPublicSnapshotAnswer({ current, locales }, date = new Date(), origin = 'live', fetchedAt = '') {
  const lines = [`时尚品鉴｜国际服第 ${current.globalIssue} 期 / 国服第 ${current.cnIssue} 期`]
  lines.push(`主题：${current.theme}`)
  lines.push(`挑战时间：${formatWindow(current)}（北京时间）`)

  const now = date.getTime()
  const start = Date.parse(current.challengeWindow.startsAt)
  const end = Date.parse(current.challengeWindow.endsAt)
  if (now < start) lines.push('本周挑战尚未开始，以下为预告。')
  if (now > end) lines.push('⚠️ 本期挑战已结束，答案可能即将更新。')
  if (origin === 'cache') lines.push(`（网络失败，展示 ${fetchedAt} 的缓存数据）`)

  const slots = orderSlots(current.slots ?? [])
  if (slots.length) lines.push('', '金牌装备')
  for (const slot of slots) {
    const names = (slot.gold?.items ?? [])
      .map((item) => zhName(locales.items, item.itemId, item.name))
      .filter(Boolean)
    const label = [slot.label, slot.tag].filter(Boolean).join('｜')
    lines.push(`${label}：${names.join('、')}`)
  }

  const dyes = current.referenceShowcase?.dyes ?? []
  if (dyes.length) lines.push('', '染色攻略')
  for (const dye of orderSlots(dyes)) {
    const exactName = zhName(locales.dyes, dye.exact?.dyeId, dye.exact?.name)
    const family = String(dye.family?.name || '').trim()
    lines.push(`${slotLabel(dye.slotId)}：${exactName}${family ? `（${family}）` : ''}`)
  }

  for (const solution of current.referenceShowcase?.solutions ?? []) {
    const entries = solution.entries ?? []
    if (!entries.length) continue
    lines.push('', `${solution.score} 分方案`)
    for (const entry of entries) {
      const name = entryName(entry, locales)
      const dyeName = entry.dye ? zhName(locales.dyes, entry.dye.dyeId, entry.dye.name) : ''
      const content = dyeName ? (name ? `${name}（${dyeName}）` : dyeName) : name
      if (content) lines.push(`${slotLabel(entry.slotId)}：${content}`)
    }
  }

  return clampPublicAnswer(lines.join('\n'))
}

export async function getPublicFashionCheckAnswer(payload = {}) {
  const date = payload.now ? new Date(payload.now) : new Date()
  try {
    const { snapshot, origin } = await getPublicSnapshot()
    return formatPublicSnapshotAnswer(snapshot, date, origin, snapshot.fetchedAt)
  } catch {
    return getStagingAnswer(payload)
  }
}
