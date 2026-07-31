import { createHash } from 'node:crypto'

import { load } from 'cheerio'
import { fetch as undiciFetch, ProxyAgent } from 'undici'

import { createUnverifiedMatch, extractScoreEvidence, extractScorePlans } from './fashionCheckScoring.mjs'

const QQ_DOCUMENT_URL = 'https://docs.qq.com/sheet/DY2lCeEpwemZESm5q?tab=BB08J2'
const QQ_SHEET_ID = 'BB08J2'
const TRACKER_URL =
  'https://docs.google.com/spreadsheets/d/1b9NwL-Ba4tS0ROSy1_4HPfi7QSMQWuhXKqFSSY9Ovp4/export?format=csv'
const ALL_GAME_STAFF_EN_URL =
  'https://www.allgamestaff.it/wp-json/wp/v2/pages?slug=fashion-report-guide-ffxiv-eng&_fields=id,modified,slug,link,title,content'
const ALL_GAME_STAFF_IT_URL =
  'https://www.allgamestaff.it/wp-json/wp/v2/posts?slug=guida-fashion-report-ffxiv&_fields=id,modified,slug,link,title,content'

const USER_AGENT =
  'NightingaleFashionCheckCollector/1.0 (+https://nightingalesilence.com; weekly public-data monitor)'
const SLOT_COLUMNS = [
  ['head', 3, '头部'],
  ['body', 4, '身体'],
  ['hands', 5, '手部'],
  ['legs', 6, '腿部'],
  ['feet', 7, '脚部'],
  ['ears', 8, '耳坠'],
  ['neck', 9, '项环'],
  ['wrists', 10, '手饰'],
  ['rightRing', 11, '右戒'],
  ['leftRing', 12, '左戒']
]

function cleanText(value) {
  return String(value ?? '')
    .replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const proxyAgents = new Map()

export function stableHash(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return createHash('sha256').update(text).digest('hex')
}

function splitSetCookieHeader(value) {
  return String(value || '')
    .split(/,(?=\s*[^;,=\s]+=[^;,]*)/)
    .map((entry) => entry.split(';', 1)[0].trim())
    .filter(Boolean)
}

function responseCookies(response) {
  if (typeof response.headers.getSetCookie === 'function') {
    return response.headers
      .getSetCookie()
      .map((entry) => entry.split(';', 1)[0].trim())
      .filter(Boolean)
      .join('; ')
  }
  return splitSetCookieHeader(response.headers.get('set-cookie')).join('; ')
}

function dispatcherFor(proxyUrl) {
  const normalized = String(proxyUrl || '').trim()
  if (!normalized) return undefined
  if (!proxyAgents.has(normalized)) proxyAgents.set(normalized, new ProxyAgent(normalized))
  return proxyAgents.get(normalized)
}

async function fetchText(url, options = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 25000)
  try {
    const response = await undiciFetch(url, {
      dispatcher: dispatcherFor(options.proxyUrl),
      headers: {
        Accept: options.accept ?? '*/*',
        'User-Agent': USER_AGENT,
        ...options.headers
      },
      redirect: 'follow',
      signal: controller.signal
    })
    if (!response.ok) throw new Error(`HTTP ${response.status} ${response.statusText}`)
    const text = await response.text()
    const maxChars = options.maxChars ?? 4_000_000
    if (text.length > maxChars) throw new Error(`response exceeds ${maxChars} characters`)
    return { response, text }
  } finally {
    clearTimeout(timer)
  }
}

function parseJsonp(text) {
  const match = String(text).match(/^clientVarsCallback\(([\s\S]*)\);?\s*$/)
  if (!match) throw new Error('QQ opendoc response is not valid clientVarsCallback JSONP')
  return JSON.parse(match[1])
}

function collectOperations(value, target) {
  if (Array.isArray(value)) {
    value.forEach((entry) => collectOperations(entry, target))
    return
  }
  if (value && typeof value === 'object' && Number.isInteger(value.t)) target.push(value)
}

function qqCellValue(cell) {
  if (!cell || !Array.isArray(cell['2'])) return ''
  const value = cell['2'][1]
  return typeof value === 'string' ? value : value ?? ''
}

export function decodeQqSheetJsonp(text, sheetId = QQ_SHEET_ID) {
  const payload = parseJsonp(text)
  const attributedText = payload.clientVars?.collab_client_vars?.initialAttributedText?.text
  if (!Array.isArray(attributedText)) throw new Error('QQ opendoc cell operations are missing')

  const operations = []
  collectOperations(attributedText, operations)
  const operation = operations.find(
    (entry) => entry.t === 3 && Array.isArray(entry.c?.[0]) && entry.c[0][0] === sheetId
  )
  if (!operation) throw new Error(`QQ sheet ${sheetId} cell operation is missing`)

  const [range, cells] = operation.c
  const [, startRow, endRow, startColumn, endColumn] = range
  const width = endColumn - startColumn + 1
  const height = endRow - startRow + 1
  if (width <= 0 || height <= 0 || width > 200 || height > 5000) {
    throw new Error(`QQ sheet range is invalid: ${JSON.stringify(range)}`)
  }

  return Array.from({ length: height }, (_, rowOffset) =>
    Array.from({ length: width }, (_, columnOffset) => {
      const key = String(rowOffset * width + columnOffset)
      return qqCellValue(cells?.[key])
    })
  )
}

function issueStartDate(globalIssue) {
  const referenceIssue = 441
  const referenceStart = Date.UTC(2026, 6, 7)
  const date = new Date(referenceStart + (globalIssue - referenceIssue) * 7 * 86400000)
  return date.toISOString().slice(0, 10)
}

export function extractQqCurrentWeek(rows) {
  if (!Array.isArray(rows) || rows.length < 20) throw new Error('QQ sheet has too few rows')
  const globalIssue = Number(rows[9]?.[0])
  if (!Number.isInteger(globalIssue) || globalIssue < 1 || globalIssue > 2000) {
    throw new Error(`QQ current global issue is invalid: ${rows[9]?.[0] ?? ''}`)
  }

  let themeRowIndex = -1
  for (let rowIndex = 11; rowIndex < rows.length - 1; rowIndex += 1) {
    const row = rows[rowIndex] ?? []
    const theme = cleanText(row[2])
    const tagCount = SLOT_COLUMNS.filter(([, column]) => cleanText(row[column])).length
    if (theme && tagCount >= 3) {
      themeRowIndex = rowIndex
      break
    }
  }
  if (themeRowIndex < 0) throw new Error('QQ current theme row could not be located')

  const themeRow = rows[themeRowIndex]
  const answerRow = rows[themeRowIndex + 1] ?? []
  const slots = SLOT_COLUMNS.flatMap(([slotId, column, label]) => {
    const tag = cleanText(themeRow[column])
    if (!tag) return []
    const answerText = cleanText(answerRow[column]) || null
    return [
      {
        slotId,
        slotLabel: label,
        tag,
        answerText,
        matches: answerText ? [createUnverifiedMatch(slotId, answerText, 'qq-cn-history')] : []
      }
    ]
  })

  return {
    sourceId: 'qq-cn-history',
    globalIssue,
    cnIssue: globalIssue - 15,
    startDate: issueStartDate(globalIssue),
    theme: cleanText(themeRow[2]),
    themeRowIndex,
    answerRowIndex: themeRowIndex + 1,
    slots,
    dyes: [],
    answerCount: slots.filter((slot) => slot.answerText).length,
    tagCount: slots.length
  }
}

export async function fetchQqCurrentWeek() {
  const page = await fetchText(QQ_DOCUMENT_URL, {
    accept: 'text/html,application/xhtml+xml',
    maxChars: 800_000
  })
  const $ = load(page.text)
  const source = $('#opendoc-jsonp').attr('src') || $('link[href*="/dop-api/opendoc"]').attr('href')
  if (!source) throw new Error('QQ page did not provide an opendoc source URL')

  const dataUrl = new URL(source, QQ_DOCUMENT_URL)
  dataUrl.searchParams.set('startrow', '0')
  dataUrl.searchParams.set('endrow', '1200')
  const cookie = responseCookies(page.response)
  const data = await fetchText(dataUrl, {
    accept: 'application/javascript,*/*;q=0.8',
    maxChars: 3_000_000,
    headers: {
      Referer: QQ_DOCUMENT_URL,
      ...(cookie ? { Cookie: cookie } : {})
    }
  })
  const rows = decodeQqSheetJsonp(data.text)
  const current = extractQqCurrentWeek(rows)
  return {
    retrievedAt: new Date().toISOString(),
    url: QQ_DOCUMENT_URL,
    rowCount: rows.length,
    columnCount: rows[0]?.length ?? 0,
    contentHash: stableHash(rows),
    current
  }
}

export function parseCsv(text) {
  const rows = []
  let row = []
  let field = ''
  let quoted = false
  const source = String(text).replace(/^\uFEFF/, '')
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (quoted) {
      if (char === '"' && source[index + 1] === '"') {
        field += '"'
        index += 1
      } else if (char === '"') {
        quoted = false
      } else {
        field += char
      }
    } else if (char === '"' && !field) {
      quoted = true
    } else if (char === ',') {
      row.push(field)
      field = ''
    } else if (char === '\n') {
      row.push(field.replace(/\r$/, ''))
      rows.push(row)
      row = []
      field = ''
    } else {
      field += char
    }
  }
  if (quoted) throw new Error('CSV contains an unterminated quoted field')
  if (field || row.length) {
    row.push(field.replace(/\r$/, ''))
    rows.push(row)
  }
  return rows
}

export async function fetchAvantGardeTracker() {
  const result = await fetchText(TRACKER_URL, {
    accept: 'text/csv',
    maxChars: 1_000_000,
    timeoutMs: 30000,
    proxyUrl: process.env.NS_FASHION_CHECK_GOOGLE_PROXY
  })
  const rows = parseCsv(result.text)
  const dataRows = rows.slice(1).filter((row) => row.some((value) => cleanText(value)))
  const categories = new Set(dataRows.map((row) => cleanText(row[4])).filter(Boolean))
  return {
    sourceId: 'avantgarde-tracker',
    retrievedAt: new Date().toISOString(),
    url: TRACKER_URL,
    contentHash: stableHash(rows),
    rowCount: dataRows.length,
    categoryCount: categories.size,
    headers: rows[0] ?? []
  }
}

function tableHeading($, table) {
  const direct = $(table).prevAll('h1,h2,h3,h4,h5,h6').first()
  if (direct.length) return cleanText(direct.text())
  const wrapper = $(table).closest('figure,div')
  return cleanText(wrapper.prevAll('h1,h2,h3,h4,h5,h6').first().text())
}

export function parseAllGameStaffEntry(entry, sourceId) {
  if (!entry || typeof entry !== 'object') throw new Error(`${sourceId} response entry is missing`)
  const title = cleanText(load(String(entry.title?.rendered ?? '')).text())
  const html = String(entry.content?.rendered ?? '')
  if (!title || !html) throw new Error(`${sourceId} title or rendered content is missing`)
  const $ = load(html)
  const tables = []
  $('table').each((index, table) => {
    const rows = []
    $(table)
      .find('tr')
      .each((_, tr) => {
        const cells = $(tr)
          .find('th,td')
          .map((__, cell) => cleanText($(cell).text()))
          .get()
        if (cells.some(Boolean)) rows.push(cells)
      })
    if (rows.length) tables.push({ index, heading: tableHeading($, table), rows })
  })
  const weekMatch = title.match(/\bWeek\s+(\d+)\b/i)
  const scoreEvidence = extractScoreEvidence(tables, sourceId)
  return {
    sourceId,
    retrievedAt: new Date().toISOString(),
    modifiedAt: entry.modified ?? null,
    url: entry.link ?? null,
    title,
    globalIssue: weekMatch ? Number(weekMatch[1]) : null,
    tableCount: tables.length,
    tables,
    scoreEvidence,
    scorePlans: extractScorePlans(tables, scoreEvidence),
    contentHash: stableHash({ title, html })
  }
}

async function fetchWordpressEntry(url, sourceId) {
  const result = await fetchText(url, { accept: 'application/json', maxChars: 1_000_000 })
  const entries = JSON.parse(result.text)
  if (!Array.isArray(entries) || !entries[0]) throw new Error(`${sourceId} WordPress entry not found`)
  return parseAllGameStaffEntry(entries[0], sourceId)
}

export function fetchAllGameStaffEnglish() {
  return fetchWordpressEntry(ALL_GAME_STAFF_EN_URL, 'allgamestaff-en')
}

export function fetchAllGameStaffItalian() {
  return fetchWordpressEntry(ALL_GAME_STAFF_IT_URL, 'allgamestaff-it')
}
