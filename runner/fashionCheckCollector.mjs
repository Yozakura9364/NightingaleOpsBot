import { randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  fetchAllGameStaffEnglish,
  fetchAllGameStaffItalian,
  fetchAvantGardeTracker,
  fetchQqCurrentWeek,
  stableHash
} from './fashionCheckSources.mjs'
import { createManualGoldMatch } from './fashionCheckScoring.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(process.env.NS_OPS_PROJECT_ROOT || resolve(__dirname, '..'))
const STORAGE_ROOT = resolve(
  process.env.NS_FASHION_CHECK_ROOT || resolve(PROJECT_ROOT, '.local', 'fashion-check')
)
const TIME_ZONE = 'Asia/Shanghai'
const STATE_PATH = resolve(STORAGE_ROOT, 'state.json')
const CURRENT_PATH = resolve(STORAGE_ROOT, 'current.json')
const QUEUE_PATH = resolve(STORAGE_ROOT, 'notifications.json')
const SUBSCRIBER_UPDATES_PATH = resolve(STORAGE_ROOT, 'subscriber-updates.json')
const SNAPSHOT_ROOT = resolve(STORAGE_ROOT, 'snapshots')
const MAX_RECENT_SIGNATURES = 200
const MAX_SUBSCRIBER_UPDATES = 24
const REFERENCE_GLOBAL_ISSUE = 441
const REFERENCE_WEEK_START_MS = Date.parse('2026-07-07T16:00:00+08:00')
const WEEK_MS = 7 * 86400000

let queueMutation = Promise.resolve()

function defaultState() {
  return {
    schemaVersion: 'fashion-check.collector-state.v1',
    lastBuckets: {},
    windows: {},
    sourceHashes: {},
    sourceFailures: {},
    recentNotificationSignatures: [],
    answerPushLocks: {},
    lastRunAt: null,
    lastRunSummary: null
  }
}

function defaultCurrent() {
  return {
    schemaVersion: 'fashion-check.current-collection.v1',
    updatedAt: null,
    sources: {},
    manualAnswers: {}
  }
}

function defaultQueue() {
  return { schemaVersion: 'fashion-check.notifications.v1', items: [] }
}

function defaultSubscriberUpdates() {
  return { schemaVersion: 'fashion-check.subscriber-updates.v1', items: [] }
}

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

function withQueueMutation(callback) {
  const pending = queueMutation.then(callback, callback)
  queueMutation = pending.catch(() => {})
  return pending
}

function localParts(date) {
  const formatter = new Intl.DateTimeFormat('en-CA', {
    timeZone: TIME_ZONE,
    weekday: 'short',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23'
  })
  const parts = Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== 'literal')
      .map((part) => [part.type, part.value])
  )
  const weekday = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 }[
    parts.weekday
  ]
  return {
    weekday,
    year: Number(parts.year),
    month: Number(parts.month),
    day: Number(parts.day),
    hour: Number(parts.hour),
    minute: Number(parts.minute),
    second: Number(parts.second)
  }
}

function isoDate(parts) {
  return `${String(parts.year).padStart(4, '0')}-${String(parts.month).padStart(2, '0')}-${String(parts.day).padStart(2, '0')}`
}

function shiftIsoDate(value, dayDelta) {
  const date = new Date(`${value}T00:00:00Z`)
  date.setUTCDate(date.getUTCDate() + dayDelta)
  return date.toISOString().slice(0, 10)
}

export function classifyCollectionWindow(date = new Date()) {
  const parts = localParts(date)
  const minuteOfWeek = parts.weekday * 1440 + parts.hour * 60 + parts.minute
  const windows = [
    { kind: 'theme', start: 2 * 1440 + 16 * 60 + 5, end: 3 * 1440 + 16 * 60 + 5 },
    { kind: 'answers', start: 5 * 1440 + 16 * 60 + 5, end: 6 * 1440 + 16 * 60 + 5 }
  ]
  const match = windows.find((window) => minuteOfWeek >= window.start && minuteOfWeek <= window.end)
  if (!match) return null
  const startWeekday = match.kind === 'theme' ? 2 : 5
  const startDate = shiftIsoDate(isoDate(parts), startWeekday - parts.weekday)
  return {
    kind: match.kind,
    id: `${match.kind}:${startDate}`,
    startDate,
    bucket: `${isoDate(parts)}T${String(parts.hour).padStart(2, '0')}`,
    finalTick: minuteOfWeek === match.end && parts.minute === 5,
    localTime: `${isoDate(parts)} ${String(parts.hour).padStart(2, '0')}:${String(parts.minute).padStart(2, '0')}`
  }
}

export function expectedGlobalIssue(date = new Date()) {
  const elapsedWeeks = Math.floor((date.getTime() - REFERENCE_WEEK_START_MS) / WEEK_MS)
  return REFERENCE_GLOBAL_ISSUE + elapsedWeeks
}

async function enqueueNotification(state, kind, signature, text) {
  if (state.recentNotificationSignatures.includes(signature)) return false
  await withQueueMutation(async () => {
    const queue = await readJson(QUEUE_PATH, defaultQueue())
    queue.items.push({
      id: randomUUID(),
      kind,
      signature,
      text,
      createdAt: new Date().toISOString()
    })
    await writeJsonAtomic(QUEUE_PATH, queue)
  })
  state.recentNotificationSignatures.push(signature)
  state.recentNotificationSignatures = state.recentNotificationSignatures.slice(
    -MAX_RECENT_SIGNATURES
  )
  return true
}

export async function recordFashionCheckSubscriberUpdate(kind, signature, text) {
  return withQueueMutation(async () => {
    const updates = await readJson(SUBSCRIBER_UPDATES_PATH, defaultSubscriberUpdates())
    if (updates.items.some((item) => item.signature === signature)) return false

    updates.items.push({
      id: randomUUID(),
      kind,
      signature,
      text,
      createdAt: new Date().toISOString()
    })
    updates.items = updates.items.slice(-MAX_SUBSCRIBER_UPDATES)
    await writeJsonAtomic(SUBSCRIBER_UPDATES_PATH, updates)
    return true
  })
}

function formatThemeNotification(current, initial) {
  const week = current.current
  const slotLines = week.slots.map((slot) => `${slot.slotLabel}：${slot.tag}`)
  return [
    '时尚品鉴自动采集',
    `阶段：${initial ? '首次建立主题快照' : '主题与标签已更新'}`,
    `周次：国际服 ${week.globalIssue} / 国服 ${week.cnIssue}`,
    `主题：${week.theme}`,
    ...slotLines,
    '',
    `装备答案：${week.answerCount}/${week.tagCount}`,
    `采集时间：${current.retrievedAt}`
  ].join('\n')
}

function formatAnswerNotification(source, qq, date = new Date()) {
  const expectedIssue = expectedGlobalIssue(date)
  const expectedCnIssue = expectedIssue - 15
  const qqAnswers = qq?.current
    ? `${qq.current.answerCount}/${qq.current.tagCount}`
    : '未取得'
  return [
    '时尚品鉴自动采集',
    '阶段：装备、染色与方案已更新',
    `当前周次：国际服 ${expectedIssue} / 国服 ${expectedCnIssue}`,
    `AllGameStaff 周次：${source.globalIssue ?? '-'}`,
    `AllGameStaff 表格：${source.tableCount}`,
    `QQ 周次：${qq?.current?.globalIssue ?? '-'}`,
    `QQ 装备答案：${qqAnswers}`,
    '',
    '状态：已进入服务器私有待发布数据，尚未绕过审核直接公开。',
    `采集时间：${source.retrievedAt}`
  ].join('\n')
}

function formatFailureNotification(sourceId, error) {
  return [
    '时尚品鉴采集异常',
    `来源：${sourceId}`,
    `错误：${String(error).slice(0, 500)}`,
    '',
    '已保留上一版数据，后续窗口检查会自动重试。'
  ].join('\n')
}

function formatRecoveryNotification(sourceId) {
  return ['时尚品鉴采集恢复', `来源：${sourceId}`, '公开数据读取已恢复。'].join('\n')
}

function formatIncompleteNotification(window, current, failures) {
  const qq = current.sources['qq-cn-history']
  const issue = qq?.current?.globalIssue ?? '-'
  return [
    '时尚品鉴采集窗口结束',
    `阶段：${window.kind === 'theme' ? '主题与标签' : '装备、染色与方案'}`,
    `当前国际服周次：${issue}`,
    '状态：本轮没有确认到完整的新数据，需要人工查看。',
    failures.length ? `失败来源：${failures.join('、')}` : '失败来源：无，来源可能尚未更新。'
  ].join('\n')
}

async function collectSource(sourceId, callback) {
  try {
    return { sourceId, ok: true, data: await callback() }
  } catch (error) {
    return { sourceId, ok: false, error: error instanceof Error ? error.message : String(error) }
  }
}

async function collectSources(kind) {
  const requests = [collectSource('qq-cn-history', fetchQqCurrentWeek)]
  if (kind === 'answers' || kind === 'full') {
    requests.push(
      collectSource('avantgarde-tracker', fetchAvantGardeTracker),
      collectSource('allgamestaff-en', fetchAllGameStaffEnglish),
      collectSource('allgamestaff-it', fetchAllGameStaffItalian)
    )
  }
  return Promise.all(requests)
}

async function archiveChangedSource(sourceId, data, previousHash) {
  const hash = data.contentHash || stableHash(data)
  if (hash === previousHash) return hash
  const path = resolve(SNAPSHOT_ROOT, sourceId, `${hash}.json`)
  await writeJsonAtomic(path, data)
  return hash
}

export function applyManualQqAnswers(
  source,
  manualAnswers = {},
  expectedIssue = source?.current?.globalIssue
) {
  const sourceWeek = source?.current
  const issue = Number(expectedIssue)
  const manual = Number.isInteger(issue) ? manualAnswers?.[String(issue)] : null
  const preview = manual?.preview
  let week = sourceWeek

  if (preview && typeof preview === 'object') {
    const slots = Array.isArray(preview.slots)
      ? preview.slots
          .map((slot) => ({
            slotId: String(slot?.slotId || '').trim(),
            slotLabel: String(slot?.slotLabel || '').trim(),
            tag: String(slot?.tag || '').trim(),
            answerText: null,
            matches: []
          }))
          .filter((slot) => slot.slotId && slot.slotLabel && slot.tag)
      : []
    const themeId = Number(preview.themeId)

    week = {
      ...(sourceWeek || {}),
      globalIssue: issue,
      cnIssue: Number(preview.cnIssue ?? issue - 15),
      startDate: String(preview.startDate || '').trim(),
      theme: String(preview.theme || '').trim(),
      ...(Number.isInteger(themeId) && themeId > 0 ? { themeId } : {}),
      themeRowIndex: null,
      answerRowIndex: null,
      slots,
      dyes: [],
      answerCount: 0,
      tagCount: slots.length
    }
  }

  if (!week) return source
  const overrides = manual?.answers
  if (!overrides || typeof overrides !== 'object') {
    return week === sourceWeek ? source : { ...source, current: week }
  }

  const slots = (week.slots || []).map((slot) => {
    const match = createManualGoldMatch(slot.slotId, overrides[slot.slotId])
    if (!match) return slot
    return { ...slot, answerText: match.equipmentText, matches: [match] }
  })
  return {
    ...source,
    current: {
      ...week,
      slots,
      answerCount: slots.filter((slot) => String(slot.answerText || '').trim()).length
    }
  }
}

function manualScorePlanExists(manual, targetScore) {
  return (manual?.scorePlans || []).some((plan) => {
    const title = String(plan?.title || '').trim()
    const entries = Array.isArray(plan?.entries) ? plan.entries.filter((entry) => String(entry || '').trim()) : []
    return new RegExp(`^${targetScore}\\s*分`).test(title) && entries.length > 0
  })
}

function sourceScorePlanExists(source, targetScore) {
  const headingFragment = targetScore === 80 ? '80 points' : '100 points'
  return formatPlanTable(source, headingFragment, `${targetScore} 分方案`).length > 1
}

export function getScorePlanStatus(current, issue = current?.sources?.['qq-cn-history']?.current?.globalIssue) {
  const manual = current?.manualAnswers?.[String(issue)] ?? {}
  const source = current?.sources?.['allgamestaff-en']
  return {
    has80: manualScorePlanExists(manual, 80) || sourceScorePlanExists(source, 80),
    has100: manualScorePlanExists(manual, 100) || sourceScorePlanExists(source, 100)
  }
}

export function scorePlansComplete(current, issue = current?.sources?.['qq-cn-history']?.current?.globalIssue) {
  const status = getScorePlanStatus(current, issue)
  return status.has80 && status.has100
}

export function manualScorePlansComplete(
  current,
  issue = current?.sources?.['qq-cn-history']?.current?.globalIssue
) {
  const manual = current?.manualAnswers?.[String(issue)] ?? {}
  return manualScorePlanExists(manual, 80) && manualScorePlanExists(manual, 100)
}

export function shouldLockAnswerPush(state, current, date = new Date()) {
  const issue = expectedGlobalIssue(date)
  const currentIssue = current?.sources?.['qq-cn-history']?.current?.globalIssue
  return (
    currentIssue === issue &&
    manualScorePlansComplete(current, issue) &&
    !state?.answerPushLocks?.[String(issue)]
  )
}

export function answerSourceMatchesExpectedIssue(source, qq, date = new Date()) {
  const expectedIssue = expectedGlobalIssue(date)
  const sourceIssue = source?.globalIssue
  const qqIssue = qq?.current?.globalIssue
  return sourceIssue === expectedIssue && (!qqIssue || qqIssue === expectedIssue)
}

function answersComplete(current) {
  const qq = current.sources['qq-cn-history']
  const english = current.sources['allgamestaff-en']
  return Boolean(
    qq?.current?.globalIssue &&
      english?.globalIssue === qq.current.globalIssue &&
      english.tableCount >= 3 &&
      qq.current.answerCount >= Math.min(4, qq.current.tagCount) &&
      scorePlansComplete(current, qq.current.globalIssue)
  )
}

export async function runFashionCheckTick(payload = {}) {
  const now = payload.now ? new Date(payload.now) : new Date()
  const scheduledWindow = classifyCollectionWindow(now)
  const force = Boolean(payload.force)
  const window = force
    ? {
        kind: payload.kind === 'theme' ? 'theme' : 'full',
        id: `manual:${now.toISOString()}`,
        startDate: now.toISOString().slice(0, 10),
        bucket: `manual:${now.toISOString()}`,
        finalTick: false,
        localTime: now.toISOString()
      }
    : scheduledWindow
  if (!window) {
    return { ok: true, output: 'SKIP：当前不在时尚品鉴采集窗口。' }
  }

  const state = await readJson(STATE_PATH, defaultState())
  if (!state.answerPushLocks || typeof state.answerPushLocks !== 'object' || Array.isArray(state.answerPushLocks)) {
    state.answerPushLocks = {}
  }
  if (!force && state.lastBuckets[window.kind] === window.bucket) {
    return { ok: true, output: `SKIP：${window.bucket} 已执行。` }
  }
  state.lastBuckets[window.kind] = window.bucket
  const current = await readJson(CURRENT_PATH, defaultCurrent())
  const previousQqSignature = current.sources['qq-cn-history']
    ? stableHash(current.sources['qq-cn-history'].current)
    : null
  const previousAnswerSignature = current.sources['allgamestaff-en']?.contentHash ?? null
  const results = await collectSources(window.kind)
  const successes = results.filter((result) => result.ok)
  const failures = results.filter((result) => !result.ok)

  for (const result of successes) {
    const previousFailures = Number(state.sourceFailures[result.sourceId] ?? 0)
    state.sourceHashes[result.sourceId] = await archiveChangedSource(
      result.sourceId,
      result.data,
      state.sourceHashes[result.sourceId]
    )
    state.sourceFailures[result.sourceId] = 0
    current.sources[result.sourceId] =
      result.sourceId === 'qq-cn-history'
        ? applyManualQqAnswers(result.data, current.manualAnswers, expectedGlobalIssue(now))
        : result.data
    if (previousFailures >= 3) {
      await enqueueNotification(
        state,
        'source-recovered',
        `source-recovered:${result.sourceId}:${result.data.contentHash}`,
        formatRecoveryNotification(result.sourceId)
      )
    }
  }

  for (const result of failures) {
    const count = Number(state.sourceFailures[result.sourceId] ?? 0) + 1
    state.sourceFailures[result.sourceId] = count
    if (count === 3) {
      await enqueueNotification(
        state,
        'source-failed',
        `source-failed:${result.sourceId}:${stableHash(result.error)}`,
        formatFailureNotification(result.sourceId, result.error)
      )
    }
  }

  current.updatedAt = now.toISOString()
  current.collection = {
    windowId: window.id,
    windowKind: window.kind,
    checkedAt: now.toISOString(),
    successfulSources: successes.map((result) => result.sourceId),
    failedSources: failures.map((result) => ({ sourceId: result.sourceId, error: result.error }))
  }

  const qq = current.sources['qq-cn-history']
  const qqSignature = qq ? stableHash(qq.current) : null
  if (qqSignature && qqSignature !== previousQqSignature) {
    await enqueueNotification(
      state,
      'theme-updated',
      `theme:${qqSignature}`,
      formatThemeNotification(qq, !previousQqSignature)
    )
    if (window.kind === 'theme') {
      await recordFashionCheckSubscriberUpdate(
        'theme-updated',
        `theme:${qqSignature}`,
        formatFashionCheckAnswer(current, now)
      )
    }
  }

  const english = current.sources['allgamestaff-en']
  const answerSignature = english?.contentHash ?? null
  const answerPushLocked = Boolean(state.answerPushLocks[String(expectedGlobalIssue(now))])
  if (
    answerSignature &&
    answerSignature !== previousAnswerSignature &&
    answerSourceMatchesExpectedIssue(english, qq, now) &&
    !answerPushLocked
  ) {
    await enqueueNotification(
      state,
      'answers-updated',
      `answers:${answerSignature}`,
      formatAnswerNotification(english, qq, now)
    )
  }

  if (window.kind === 'answers' && answersComplete(current) && !answerPushLocked) {
    const subscriberAnswerSignature = stableHash({
      qq: qq?.current,
      answers: english?.contentHash ?? null
    })
    await recordFashionCheckSubscriberUpdate(
      'answers-updated',
      `answers:${subscriberAnswerSignature}`,
      formatFashionCheckAnswer(current, now)
    )
  }

  if (shouldLockAnswerPush(state, current, now)) {
    const issue = String(expectedGlobalIssue(now))
    state.answerPushLocks[issue] = {
      lockedAt: now.toISOString(),
      reason: 'score-plans-complete'
    }
  }

  const windowState = state.windows[window.id] ?? {
    firstCheckedAt: now.toISOString(),
    baselineQqSignature: previousQqSignature,
    baselineAnswerSignature: previousAnswerSignature,
    completed: false,
    finalNotified: false
  }
  windowState.lastCheckedAt = now.toISOString()
  windowState.completed =
    window.kind === 'theme'
      ? qq?.current?.startDate === window.startDate
      : window.kind === 'answers'
        ? answersComplete(current)
        : Boolean(qq)
  if (window.finalTick && !windowState.completed && !windowState.finalNotified) {
    await enqueueNotification(
      state,
      'window-incomplete',
      `window-incomplete:${window.id}`,
      formatIncompleteNotification(
        window,
        current,
        failures.map((result) => result.sourceId)
      )
    )
    windowState.finalNotified = true
  }
  state.windows[window.id] = windowState
  state.lastRunAt = now.toISOString()
  state.lastRunSummary = {
    windowId: window.id,
    kind: window.kind,
    successfulSources: successes.map((result) => result.sourceId),
    failedSources: failures.map((result) => result.sourceId),
    completed: windowState.completed
  }

  await Promise.all([writeJsonAtomic(CURRENT_PATH, current), writeJsonAtomic(STATE_PATH, state)])
  const output = [
    '时尚品鉴采集完成',
    `窗口：${window.id}`,
    `成功来源：${successes.map((result) => result.sourceId).join('、') || '无'}`,
    `失败来源：${failures.map((result) => result.sourceId).join('、') || '无'}`,
    `当前周次：${qq?.current?.globalIssue ?? '-'}`,
    `主题：${qq?.current?.theme ?? '-'}`,
    `窗口完成：${windowState.completed ? '是' : '否'}`
  ].join('\n')
  return { ok: successes.length > 0, output, error: successes.length ? '' : output }
}

const PUBLIC_SLOT_NAMES = new Map([
  ['Head', '头部'],
  ['Body', '身体'],
  ['Hands', '手部'],
  ['Legs', '腿部'],
  ['Feet', '脚部'],
  ['Ears', '耳部'],
  ['Ear', '耳部'],
  ['Neck', '颈部'],
  ['Wrists', '腕部'],
  ['Ring', '戒指']
])

function publicSlotName(value) {
  return PUBLIC_SLOT_NAMES.get(String(value || '').trim()) || String(value || '').trim()
}

function formatPlanTable(source, headingFragment, title) {
  const table = source?.tables?.find((entry) =>
    String(entry.heading || '').toLowerCase().includes(headingFragment)
  )
  if (!table) return []
  const lines = [title]
  for (const row of table.rows.slice(1)) {
    const [slot, item, dye] = row
    if (!slot || !item) continue
    const dyeText = dye && dye !== '–' && dye !== '-' ? ` / ${dye}` : ''
    lines.push(`${publicSlotName(slot)}：${item}${dyeText}`)
  }
  return lines.length > 1 ? lines : []
}

function formatManualDyeGuide(manual) {
  const guide = manual?.dyeGuide
  const entries = Array.isArray(guide?.entries) ? guide.entries : []
  if (!guide || entries.length === 0) return []

  const title = String(guide.title || '各部位染色攻略').trim()
  const provider = String(guide.provider || '').trim()
  const lines = [`${title}${provider ? `（提供者：${provider}）` : ''}`]
  for (const entry of entries) {
    const slot = String(entry?.slotLabel || publicSlotName(entry?.slot) || '').trim()
    const family = String(entry?.family || '').trim()
    const dye = String(entry?.dye || '').trim()
    const source = String(entry?.source || '').trim()
    if (!slot || !dye) continue
    const dyeText = family ? `${family}-${dye}` : dye
    lines.push(`${slot}：${dyeText}${source ? `（${source}）` : ''}`)
  }
  return lines.length > 1 ? lines : []
}

function formatManualScorePlans(manual) {
  const plans = Array.isArray(manual?.scorePlans) ? manual.scorePlans : []
  const output = []
  for (const plan of plans) {
    const title = String(plan?.title || '').trim()
    const entries = Array.isArray(plan?.entries) ? plan.entries : []
    if (!title || entries.length === 0) continue
    output.push(title)
    for (const entry of entries) {
      const text = String(entry || '').trim()
      if (text) output.push(text)
    }
  }
  return output
}

function missingPlanText({ hasManualDyeGuide, hasManualScorePlans, hasManual80, hasManual100 }) {
  if (hasManual80 && !hasManual100) return '100 分方案未更新'
  if (hasManual100 && !hasManual80) return '80 分方案未更新'
  if (hasManualScorePlans) return ''
  return hasManualDyeGuide ? '80/100 方案未更新' : '作业未更新'
}

function formatMatch(match) {
  const text = String(match?.equipmentText || '').trim()
  if (!text) return ''
  const hasPoints = match.points !== null && match.points !== '' && match.points !== undefined && Number.isFinite(Number(match.points))
  if (match.grade === 'gold' && hasPoints) {
    return `金牌（${Number(match.points)} 分）：${text}`
  }
  if (match.grade === 'silver' && hasPoints) {
    return `银牌（${Number(match.points)} 分）：${text}`
  }
  return `待核验档位：${text}`
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

export function formatFashionCheckAnswer(current, date = new Date()) {
  const expectedIssue = expectedGlobalIssue(date)
  const expectedCnIssue = expectedIssue - 15
  const qq = current?.sources?.['qq-cn-history']
  const week = qq?.current
  const manual = current?.manualAnswers?.[String(expectedIssue)] ?? {}
  const heading = `时尚品鉴｜国际服第 ${expectedIssue} 期 / 国服第 ${expectedCnIssue} 期`

  if (!week || week.globalIssue !== expectedIssue) {
    return [heading, '', '本周主题与答案尚未更新。', '更新后会由机器人自动推送。'].join('\n')
  }

  const knownSlots = (week.slots || []).filter((slot) => {
    const hasTag = String(slot?.slotLabel || '').trim() && String(slot?.tag || '').trim()
    return hasTag || String(slot?.answerText || '').trim()
  })
  const lines = [heading, `主题：${week.theme}`]
  const manualDyeGuide = formatManualDyeGuide(manual)
  if (manualDyeGuide.length) {
    lines.push('', ...manualDyeGuide)
  }
  if (knownSlots.length) {
    lines.push('', '已知部位与装备')
  }
  for (const slot of knownSlots) {
    const label = [slot.slotLabel, slot.tag].filter(Boolean).join('｜')
    const matches = Array.isArray(slot.matches) ? slot.matches.map(formatMatch).filter(Boolean) : []
    const answerText = String(slot.answerText || '').trim()
    if (label) lines.push(label)
    if (matches.length) lines.push(...matches)
    else if (answerText) lines.push(`待核验档位：${answerText}`)
  }

  const manualScorePlans = formatManualScorePlans(manual)
  const scorePlanStatus = getScorePlanStatus(current, expectedIssue)
  if (manualScorePlans.length) {
    lines.push('', ...manualScorePlans)
  }

  const english = current.sources['allgamestaff-en']
  if (english?.globalIssue === expectedIssue) {
    if (!manualScorePlanExists(manual, 80)) {
      const plan80 = formatPlanTable(english, '80 points', '80 分方案')
      if (plan80.length) lines.push('', ...plan80)
    }
    if (!manualScorePlanExists(manual, 100)) {
      const plan100 = formatPlanTable(english, '100 points', '100 分方案')
      if (plan100.length) lines.push('', ...plan100)
    }
  } else {
    const missingText = missingPlanText({
      hasManualDyeGuide: manualDyeGuide.length > 0,
      hasManualScorePlans: manualScorePlans.length > 0,
      hasManual80: scorePlanStatus.has80,
      hasManual100: scorePlanStatus.has100
    })
    if (missingText) lines.push('', missingText)
  }

  return clampPublicAnswer(lines.join('\n'))
}

export async function getFashionCheckAnswer(payload = {}) {
  const current = await readJson(CURRENT_PATH, defaultCurrent())
  const date = payload.now ? new Date(payload.now) : new Date()
  return formatFashionCheckAnswer(current, date)
}

export async function getFashionCheckStatus() {
  const [state, current, queue] = await Promise.all([
    readJson(STATE_PATH, defaultState()),
    readJson(CURRENT_PATH, defaultCurrent()),
    readJson(QUEUE_PATH, defaultQueue())
  ])
  const qq = current.sources['qq-cn-history']
  const english = current.sources['allgamestaff-en']
  return [
    '时尚品鉴自动采集',
    `上次运行：${state.lastRunAt ?? '-'}`,
    `当前周次：${qq?.current?.globalIssue ?? '-'}`,
    `当前主题：${qq?.current?.theme ?? '-'}`,
    `QQ 答案：${qq?.current ? `${qq.current.answerCount}/${qq.current.tagCount}` : '-'}`,
    `AllGameStaff 周次：${english?.globalIssue ?? '-'}`,
    `待发送通知：${queue.items.length}`,
    `数据目录：${STORAGE_ROOT}`
  ].join('\n')
}

export async function peekFashionCheckNotification() {
  const queue = await readJson(QUEUE_PATH, defaultQueue())
  return queue.items[0] ?? null
}

export async function ackFashionCheckNotification(id) {
  const normalizedId = String(id || '').trim()
  if (!normalizedId) throw new Error('notification id is required')
  return withQueueMutation(async () => {
    const queue = await readJson(QUEUE_PATH, defaultQueue())
    const before = queue.items.length
    queue.items = queue.items.filter((item) => item.id !== normalizedId)
    await writeJsonAtomic(QUEUE_PATH, queue)
    return before !== queue.items.length
  })
}

export async function getFashionCheckSubscriberUpdates() {
  const updates = await readJson(SUBSCRIBER_UPDATES_PATH, defaultSubscriberUpdates())
  const items = Array.isArray(updates.items) ? updates.items : []
  return {
    items,
    latestId: items.at(-1)?.id ?? ''
  }
}

export const fashionCheckPaths = {
  STORAGE_ROOT,
  STATE_PATH,
  CURRENT_PATH,
  QUEUE_PATH,
  SUBSCRIBER_UPDATES_PATH,
  SNAPSHOT_ROOT
}
