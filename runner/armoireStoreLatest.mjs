import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const STORE_CATALOG_RELATIVE_PATH = 'public/data/armoire-store-catalog.json'
const STORE_SCHEMA_VERSION = 'nsarmoire.storeCatalog.v1'
const REQUEST_TIMEOUT_MS = 15000
const CN_PRODUCT_API = 'https://sqmallservice.u.sdo.com/api/ps/product/list'
const CN_STORE_URL = 'https://qu.sdo.com/tools-shop?merchantId=1'
const GLOBAL_PRODUCT_API = 'https://api.store.finalfantasyxiv.com/ffxivcatalog/api/products/'
const GLOBAL_STORE_URL = 'https://store.finalfantasyxiv.com/ffxivstore/ja-jp'
const TW_NEWS_URL = 'https://www.ffxiv.com.tw/web/news/news_list.aspx'

const CN_HEADERS = {
  'qu-merchant-id': '1',
  'qu-hardware-platform': '3',
  'qu-software-platform': '1',
  'qu-deploy-platform': '1',
  'qu-web-host': 'qu.sdo.com',
  accept: 'application/json, text/plain, */*',
  'User-Agent': 'NightingaleOpsBot ArmoireLatestAudit/0.1'
}

const GENERIC_HEADERS = {
  accept: 'text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8',
  'Accept-Language': 'zh-CN,zh;q=0.9,zh-TW;q=0.8,ja;q=0.7,en;q=0.6',
  'User-Agent': 'NightingaleOpsBot ArmoireLatestAudit/0.1'
}

const GLOBAL_APPEARANCE_PATTERNS = [
  /コスチューム/,
  /衣装/,
  /武器/,
  /ウェポン/i,
  /ソード/,
  /ブレード/,
  /アクス/,
  /アックス/,
  /ランス/,
  /スピア/,
  /ボウ/,
  /ダガー/,
  /ナイフ/,
  /スタッフ/,
  /ロッド/,
  /ワンド/,
  /サイズ/,
  /ガン/,
  /シールド/,
  /盾/,
  /剣/,
  /刀/,
  /斧/,
  /槍/,
  /弓/,
  /銃/,
  /杖/,
  /鎌/
]

const NON_APPEARANCE_PATTERNS = [
  /冒险录|冒険録/,
  /坐骑|マウント/,
  /宠物|ミニオン/,
  /情感动作|演技教材|エモート/,
  /乐谱|オーケストリオン/,
  /幻想药|幻想薬/,
  /住宅|家具|庭具|ポスター|海报|海報|壁纸|壁紙/,
  /染剂|染料|カララント/,
  /烟花|花火/,
  /转服|リテイナー|チョコボかばん/
]

const TW_STOP_WORDS = new Set([
  '本次新增販售品項',
  '本次新增贩售品项',
  '時尚美學',
  '时尚美学',
  '情感動作',
  '情感动作',
  '住宅與裝飾',
  '住宅与装饰',
  '實際品項請依商城顯示內容為準',
  '实际品项请依商城显示内容为准'
])
const TW_CATEGORY_NAMES = new Map([
  ['時尚美學', '时尚美学'],
  ['时尚美学', '时尚美学'],
  ['情感動作', '情感动作'],
  ['情感动作', '情感动作'],
  ['住宅與裝飾', '住宅与装饰'],
  ['住宅与装饰', '住宅与装饰']
])

const LINK_URL_PATTERNS = new Map([
  [
    'cn',
    /^https:\/\/(?:qu\.sdo\.com\/product-detail\/[A-Za-z0-9]+|ffpay\.sdo\.com\/pc\/giftsStation\/index\.html#\/index|actff1\.web\.sdo\.com\/[A-Za-z0-9_-]+\/index\.html#\/index)(?:[/?#].*)?$/
  ],
  [
    'global',
    /^https:\/\/(?:store\.finalfantasyxiv\.com\/ffxivstore\/[a-z]{2}-[a-z]{2}\/product\/\d+|jp\.finalfantasyxiv\.com\/product\/)(?:[/?#].*)?$/
  ],
  [
    'tw',
    /^https:\/\/www\.ffxiv\.com\.tw\/web\/store\/product_detail\.aspx\?id=[A-Za-z0-9_]+(?:[&#].*)?$/
  ],
  [
    'kr',
    /^https:\/\/www\.ff14\.co\.kr\/(?:shop\/home\/detail\/\d+|member\/event\/news\/index\.aspx|main)(?:[/?#].*)?$/i
  ]
])

function cleanText(value) {
  return String(value ?? '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, ' ')
    .trim()
}

function hashText(value) {
  return createHash('sha256').update(String(value ?? ''), 'utf8').digest('hex')
}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function resolveCatalogPath(options = {}) {
  const v2Root = resolve(options.v2Root || process.env.NS_OPS_V2_ROOT || process.cwd())
  return resolve(
    options.storeCatalogPath ||
      process.env.NS_OPS_ARMOIRE_STORE_CATALOG ||
      resolve(v2Root, STORE_CATALOG_RELATIVE_PATH)
  )
}

function resolveSourceCatalogPath(options = {}) {
  const v2Root = resolve(options.v2Root || process.env.NS_OPS_V2_ROOT || process.cwd())
  return resolve(
    options.sourcePath ||
      process.env.NS_OPS_ARMOIRE_STORE_CATALOG_SOURCE ||
      resolve(v2Root, STORE_CATALOG_RELATIVE_PATH)
  )
}

async function readJsonFile(filePath) {
  return JSON.parse(await readFile(filePath, 'utf8'))
}

function buildCatalogSummary(storeCatalog) {
  const outfits = Array.isArray(storeCatalog.outfits) ? storeCatalog.outfits : []
  const linkRegions = new Map()
  for (const outfit of outfits) {
    for (const region of Object.keys(outfit.regionalStoreUrls ?? {})) {
      linkRegions.set(region, (linkRegions.get(region) ?? 0) + 1)
    }
  }

  return {
    generatedAt: storeCatalog.generatedAt || '-',
    outfitCount: outfits.length,
    mappedCount: outfits.filter((outfit) => Array.isArray(outfit.itemIds) && outfit.itemIds.length > 0)
      .length,
    needsMappingCount: outfits.filter((outfit) => outfit.needsMapping).length,
    correctedCount: outfits.filter((outfit) => outfit.corrected === true).length,
    linkRegions
  }
}

function validateStoreCatalog(storeCatalog) {
  const issues = []
  const warnings = []

  if (!isRecord(storeCatalog)) {
    return { issues: ['catalog root must be an object'], warnings }
  }
  if (storeCatalog.schemaVersion !== STORE_SCHEMA_VERSION) {
    issues.push(`schemaVersion 不匹配：${storeCatalog.schemaVersion || '-'}`)
  }
  if (!Array.isArray(storeCatalog.outfits)) {
    issues.push('outfits 必须是数组')
    return { issues, warnings }
  }

  const ids = new Set()
  const duplicateIds = new Set()
  const productKeys = new Set()
  const duplicateProductKeys = new Set()

  for (const [index, outfit] of storeCatalog.outfits.entries()) {
    if (!isRecord(outfit)) {
      issues.push(`outfits[${index}] 不是对象`)
      continue
    }

    for (const key of ['id', 'region', 'name', 'storeUrl', 'sourceUrl']) {
      if (typeof outfit[key] !== 'string' || !outfit[key].trim()) {
        issues.push(`${outfit.id || `outfits[${index}]`}.${key} 缺失`)
      }
    }
    if (!Array.isArray(outfit.itemNames)) {
      issues.push(`${outfit.id || `outfits[${index}]`}.itemNames 不是数组`)
    }
    if (!Array.isArray(outfit.itemIds) || outfit.itemIds.some((itemId) => !Number.isInteger(itemId) || itemId <= 0)) {
      issues.push(`${outfit.id || `outfits[${index}]`}.itemIds 包含非法值`)
    }
    if (outfit.needsMapping === false && Array.isArray(outfit.itemIds) && outfit.itemIds.length === 0) {
      warnings.push(`${outfit.name || outfit.id}: needsMapping=false 但 itemIds 为空`)
    }

    if (outfit.id) {
      if (ids.has(outfit.id)) {
        duplicateIds.add(outfit.id)
      }
      ids.add(outfit.id)
    }

    const productKey = outfit.region && outfit.productId ? `${outfit.region}:${outfit.productId}` : ''
    if (productKey) {
      if (productKeys.has(productKey)) {
        duplicateProductKeys.add(productKey)
      }
      productKeys.add(productKey)
    }

    for (const [region, url] of Object.entries(outfit.regionalStoreUrls ?? {})) {
      const pattern = LINK_URL_PATTERNS.get(region)
      if (!pattern) {
        issues.push(`${outfit.id || outfit.name}.regionalStoreUrls.${region} 不支持`)
        continue
      }
      if (typeof url !== 'string' || !pattern.test(url)) {
        issues.push(`${outfit.id || outfit.name}.regionalStoreUrls.${region} URL 格式异常`)
      }
    }
  }

  for (const id of duplicateIds) {
    issues.push(`重复 outfit id：${id}`)
  }
  for (const key of duplicateProductKeys) {
    issues.push(`重复商品键：${key}`)
  }

  return { issues, warnings }
}

function formatCatalogCheck(storeCatalog, catalogPath, issues, warnings) {
  const summary = buildCatalogSummary(storeCatalog)
  const linkSummary = Array.from(summary.linkRegions.entries())
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([region, count]) => `${region}:${count}`)
    .join(' / ')

  const lines = [
    'NSArmoire 商城目录镜像校验',
    `catalogPath：${catalogPath}`,
    `generatedAt：${summary.generatedAt}`,
    `outfits：${summary.outfitCount}`,
    `mapped：${summary.mappedCount}`,
    `needsMapping：${summary.needsMappingCount}`,
    `corrected：${summary.correctedCount}`,
    `links：${linkSummary || '-'}`,
    '',
    issues.length === 0 ? '结果：通过' : `结果：失败（${issues.length} 项）`
  ]

  if (issues.length > 0) {
    lines.push('', 'Errors:')
    lines.push(...issues.slice(0, 20).map((issue) => `- ${issue}`))
    if (issues.length > 20) {
      lines.push(`- 还有 ${issues.length - 20} 项未显示`)
    }
  }
  if (warnings.length > 0) {
    lines.push('', 'Warnings:')
    lines.push(...warnings.slice(0, 12).map((warning) => `- ${warning}`))
    if (warnings.length > 12) {
      lines.push(`- 还有 ${warnings.length - 12} 项未显示`)
    }
  }

  return lines.join('\n')
}

export async function checkStoreCatalogMirror(options = {}) {
  const catalogPath = resolveCatalogPath(options)
  const storeCatalog = await readJsonFile(catalogPath)
  const { issues, warnings } = validateStoreCatalog(storeCatalog)
  return {
    ok: issues.length === 0,
    output: formatCatalogCheck(storeCatalog, catalogPath, issues, warnings)
  }
}

async function loadSourceCatalogText(options = {}) {
  const sourceUrl = String(options.sourceUrl || process.env.NS_OPS_ARMOIRE_STORE_CATALOG_URL || '').trim()
  if (sourceUrl) {
    const text = await fetchText(sourceUrl, { headers: GENERIC_HEADERS })
    return { text, source: sourceUrl }
  }

  const sourcePath = resolveSourceCatalogPath(options)
  return {
    text: await readFile(sourcePath, 'utf8'),
    source: sourcePath
  }
}

export async function prepareSyncStoreCatalogMirror(options = {}) {
  const mirrorPath = resolve(
    options.mirrorPath ||
      process.env.NS_OPS_ARMOIRE_STORE_CATALOG ||
      resolve(process.cwd(), '.local', 'armoire-store-catalog.json')
  )
  const { text, source } = await loadSourceCatalogText(options)
  const incomingCatalog = JSON.parse(text)
  const { issues, warnings } = validateStoreCatalog(incomingCatalog)
  if (issues.length > 0) {
    const error = new Error(formatCatalogCheck(incomingCatalog, source, issues, warnings))
    error.statusCode = 400
    throw error
  }

  let currentHash = ''
  try {
    currentHash = hashText(await readFile(mirrorPath, 'utf8'))
  } catch {
    currentHash = ''
  }

  const incomingHash = hashText(text)
  const summary = buildCatalogSummary(incomingCatalog)
  return {
    ok: true,
    payload: {
      source,
      mirrorPath
    },
    preview: [
      '同步 NSArmoire 商城目录镜像',
      `source：${source}`,
      `mirror：${mirrorPath}`,
      `outfits：${summary.outfitCount}`,
      `generatedAt：${summary.generatedAt}`,
      `sha256：${incomingHash.slice(0, 16)}`,
      currentHash ? `current：${currentHash.slice(0, 16)}` : 'current：无',
      incomingHash === currentHash ? '状态：内容相同，确认后仍会覆盖写入。' : '状态：内容不同，确认后覆盖镜像。'
    ].join('\n')
  }
}

export async function syncStoreCatalogMirror(options = {}) {
  const mirrorPath = resolve(
    options.mirrorPath ||
      process.env.NS_OPS_ARMOIRE_STORE_CATALOG ||
      resolve(process.cwd(), '.local', 'armoire-store-catalog.json')
  )
  const { text, source } = await loadSourceCatalogText(options)
  const incomingCatalog = JSON.parse(text)
  const { issues, warnings } = validateStoreCatalog(incomingCatalog)
  if (issues.length > 0) {
    return {
      ok: false,
      output: formatCatalogCheck(incomingCatalog, source, issues, warnings)
    }
  }

  await mkdir(dirname(mirrorPath), { recursive: true })
  await writeFile(mirrorPath, text.endsWith('\n') ? text : `${text}\n`, 'utf8')

  const summary = buildCatalogSummary(incomingCatalog)
  return {
    ok: true,
    output: [
      'NSArmoire 商城目录镜像同步完成',
      `source：${source}`,
      `mirror：${mirrorPath}`,
      `outfits：${summary.outfitCount}`,
      `generatedAt：${summary.generatedAt}`,
      `sha256：${hashText(text).slice(0, 16)}`
    ].join('\n')
  }
}

function stripTags(value) {
  return cleanText(
    String(value ?? '')
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<br\s*\/?>/gi, ' ')
      .replace(/<[^>]+>/g, ' ')
  )
}

function normalizeName(value) {
  return cleanText(value)
    .toLowerCase()
    .replace(/[　\s·・\-—_（）()【】\[\]「」『』“”"'：:]/g, '')
    .replace(/套裝/g, '套装')
    .replace(/服裝/g, '服装')
    .replace(/時尚/g, '时尚')
    .replace(/學/g, '学')
}

function canonicalUrl(value) {
  const raw = String(value ?? '').trim()
  if (!raw) {
    return ''
  }

  try {
    const url = new URL(raw)
    if (url.hostname === 'qu.sdo.com') {
      const skuId = url.pathname.match(/\/product-detail\/([A-Za-z0-9]+)/)?.[1]
      return skuId ? `cn:${skuId}` : raw
    }
    if (url.hostname === 'store.finalfantasyxiv.com') {
      const productId = url.pathname.match(/\/product\/(\d+)/)?.[1]
      return productId ? `global:${productId}` : raw
    }
    if (url.hostname === 'www.ffxiv.com.tw') {
      const productId = url.searchParams.get('id')
      return productId ? `tw:${productId}` : raw
    }
    if (url.hostname === 'www.ff14.co.kr') {
      const productId = url.pathname.match(/\/detail\/(\d+)/)?.[1]
      return productId ? `kr:${productId}` : raw
    }
  } catch {
    return raw
  }

  return raw
}

function getGlobalProductIdFromUrl(value) {
  const match = String(value ?? '').match(/\/product\/(\d+)(?:$|[/?#])/)
  return match ? Number(match[1]) : 0
}

function appearanceScore(candidate) {
  const text = `${candidate.name} ${candidate.summary ?? ''}`
  if (candidate.category && /住宅|装饰|裝飾|情感|动作|動作/.test(candidate.category)) {
    return '非外观/暂忽略'
  }
  if (candidate.category && /时尚|時尚/.test(candidate.category)) {
    return '疑似外观'
  }
  if (NON_APPEARANCE_PATTERNS.some((pattern) => pattern.test(text))) {
    return '非外观/暂忽略'
  }
  if (/套装|套裝|服装|服裝|衣装|外观|外觀|武器|装备|裝備|コスチューム/.test(text)) {
    return '疑似外观'
  }
  if (GLOBAL_APPEARANCE_PATTERNS.some((pattern) => pattern.test(text))) {
    return '疑似外观'
  }
  return '需人工判断'
}

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(url, { ...options, signal: controller.signal })
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`)
    }
    return response
  } finally {
    clearTimeout(timeout)
  }
}

async function fetchJson(url, options = {}) {
  const response = await fetchWithTimeout(url, options)
  return response.json()
}

async function fetchText(url, options = {}) {
  const response = await fetchWithTimeout(url, options)
  return response.text()
}

function formatCnPrice(row) {
  const sku = row?.sku ?? {}
  const rawPrice = row?.price ?? sku.netPrice ?? sku.originalPrice
  if (rawPrice === undefined || rawPrice === null || rawPrice === '') {
    return ''
  }
  const numeric = Number(rawPrice)
  const price = Number.isFinite(numeric) ? String(Math.trunc(numeric)) : String(rawPrice)
  const unit = row?.currency?.shortName ?? sku?.currency?.shortName ?? '点券'
  return `${price}${unit}`
}

async function fetchCnLatest(limit) {
  const url = new URL(CN_PRODUCT_API)
  url.searchParams.set('merchantId', '1')
  url.searchParams.set('page', '1')
  url.searchParams.set('pageSize', String(limit))
  url.searchParams.set('categoryType', '0')
  url.searchParams.set('order', '0')
  url.searchParams.set('keyword', '')

  const payload = await fetchJson(url, { headers: CN_HEADERS })
  const rows = Array.isArray(payload?.data?.productList) ? payload.data.productList : []

  return rows.map((row) => {
    const product = row.product ?? {}
    const sku = row.sku ?? {}
    const skuId = String(row.defaultSKUId ?? sku.skuId ?? sku.id ?? '').trim()
    const productId = String(row.productId ?? product.productId ?? product.id ?? '').trim()
    const name = cleanText(product.productName)
    const url = skuId ? `https://qu.sdo.com/product-detail/${skuId}` : CN_STORE_URL

    return {
      region: 'cn',
      source: '国服商城',
      productId,
      skuId,
      name,
      url,
      price: formatCnPrice(row),
      summary: stripTags(product.productContent ?? product.description ?? '').slice(0, 180)
    }
  })
}

async function fetchGlobalLatest(limit) {
  const url = new URL(GLOBAL_PRODUCT_API)
  url.searchParams.set('lang', 'ja-jp')
  url.searchParams.set('currency', 'JPY')
  url.searchParams.set('offset', '0')

  const payload = await fetchJson(url, {
    headers: {
      ...GENERIC_HEADERS,
      accept: 'application/json, text/plain, */*'
    }
  })
  const products = Array.isArray(payload.products) ? payload.products : []

  return products.slice(0, limit).map((product) => ({
    region: 'global',
    source: '国际服商城',
    productId: String(product.id ?? '').trim(),
    skuId: cleanText(product.skuId),
    name: cleanText(product.name),
    url: `${GLOBAL_STORE_URL}/product/${Number(product.id)}`,
    price: cleanText(product.salePriceText || product.priceText),
    summary: cleanText(product.topLeftIcon === 'new' ? 'new' : '')
  }))
}

function extractTwNewsItems(page) {
  const items = []
  for (const match of page.matchAll(
    /<div class="item">([\s\S]*?)(?=<div class="item">|<div class="page|<\/section|$)/gi
  )) {
    const block = match[1]
    const titleMatch = block.match(/<div class="title[^"]*">[\s\S]*?<a href="([^"]+)">([\s\S]*?)<\/a>/i)
    if (!titleMatch) {
      continue
    }
    const title = stripTags(titleMatch[2])
    if (!/商城|上架|新品/.test(title)) {
      continue
    }
    const date = stripTags(block.match(/<div class="publish_date">([^<]+)<\/div>/i)?.[1] ?? '')
    items.push({
      title,
      url: new URL(titleMatch[1], TW_NEWS_URL).href,
      publishedAt: date
    })
  }
  return items.slice(0, 3)
}

function extractTwProductNames(content) {
  const text = stripTags(content)
  const section = text
    .split(/本次新增販售品項|本次新增贩售品项/)[1]
    ?.split(/實際品項|实际品项|歡迎各位|欢迎各位/)[0]

  if (!section) {
    return []
  }

  const tokens = section
    .split(/\s+/)
    .map(cleanText)
    .filter((token) => token.length >= 2)

  const products = []
  let category = ''
  for (const token of tokens) {
    if (TW_CATEGORY_NAMES.has(token)) {
      category = TW_CATEGORY_NAMES.get(token)
      continue
    }
    if (TW_STOP_WORDS.has(token)) {
      continue
    }
    products.push({ name: token, category })
  }

  const seen = new Set()
  return products
    .filter((product) => {
      const key = `${product.category}:${product.name}`
      if (seen.has(key)) {
        return false
      }
      seen.add(key)
      return true
    })
    .slice(0, 30)
}

async function fetchTwLatest() {
  const page = await fetchText(TW_NEWS_URL, { headers: GENERIC_HEADERS })
  const newsItems = extractTwNewsItems(page)
  const candidates = []

  for (const news of newsItems) {
    try {
      const content = await fetchText(news.url, { headers: GENERIC_HEADERS })
      for (const product of extractTwProductNames(content)) {
        candidates.push({
          region: 'tw',
          source: '台服新闻',
          productId: '',
          skuId: '',
          name: product.name,
          url: news.url,
          price: '',
          publishedAt: news.publishedAt,
          category: product.category,
          summary: news.title
        })
      }
    } catch (error) {
      candidates.push({
        region: 'tw',
        source: '台服新闻',
        productId: '',
        skuId: '',
        name: news.title,
        url: news.url,
        price: '',
        publishedAt: news.publishedAt,
        summary: `新闻正文读取失败：${error.message}`
      })
    }
  }

  return candidates
}

function buildCatalogIndex(storeCatalog) {
  const byUrl = new Map()
  const byRegionProduct = new Map()
  const byName = new Map()

  for (const outfit of storeCatalog.outfits ?? []) {
    const urls = [
      outfit.storeUrl,
      ...Object.values(outfit.regionalStoreUrls ?? {}).filter((url) => typeof url === 'string')
    ]
    for (const url of urls) {
      const key = canonicalUrl(url)
      if (key) {
        byUrl.set(key, outfit)
      }
    }

    if (outfit.region && outfit.productId) {
      byRegionProduct.set(`${outfit.region}:${outfit.productId}`, outfit)
    }
    if (outfit.skuId) {
      byRegionProduct.set(`cn:${outfit.skuId}`, outfit)
    }
    if (outfit.globalProductId) {
      byRegionProduct.set(`global:${outfit.globalProductId}`, outfit)
    }
    const globalUrlProductId = getGlobalProductIdFromUrl(outfit.regionalStoreUrls?.global)
    if (globalUrlProductId) {
      byRegionProduct.set(`global:${globalUrlProductId}`, outfit)
    }

    const names = [
      outfit.name,
      outfit.globalProductName,
      ...(Object.values(outfit.localizedNames ?? {}) ?? []),
      ...(outfit.itemNames ?? [])
    ]
    for (const name of names) {
      const key = normalizeName(name)
      if (!key) {
        continue
      }
      const existing = byName.get(key) ?? []
      if (!existing.some((item) => item.id === outfit.id)) {
        existing.push(outfit)
      }
      byName.set(key, existing)
    }
  }

  return { byUrl, byRegionProduct, byName }
}

function findCatalogMatch(candidate, index) {
  const urlKey = canonicalUrl(candidate.url)
  const byUrl = index.byUrl.get(urlKey)
  if (byUrl) {
    return { kind: 'url', outfit: byUrl }
  }

  const productKeys = [
    candidate.productId ? `${candidate.region}:${candidate.productId}` : '',
    candidate.region === 'cn' && candidate.skuId ? `cn:${candidate.skuId}` : '',
    candidate.region === 'global' && candidate.productId ? `global:${candidate.productId}` : ''
  ].filter(Boolean)
  for (const key of productKeys) {
    const outfit = index.byRegionProduct.get(key)
    if (outfit) {
      return { kind: 'product', outfit }
    }
  }

  const nameKey = normalizeName(candidate.name)
  const exactNameMatches = index.byName.get(nameKey) ?? []
  if (exactNameMatches.length > 0) {
    return { kind: 'name', outfit: exactNameMatches[0], count: exactNameMatches.length }
  }

  return null
}

function classifyCandidate(candidate, match) {
  if (!match) {
    return appearanceScore(candidate) === '非外观/暂忽略' ? '忽略' : '需人工确认'
  }
  if (match.kind === 'url' || match.kind === 'product') {
    return '已存在'
  }
  if (match.kind === 'name') {
    const regionUrl = match.outfit.regionalStoreUrls?.[candidate.region]
    if (regionUrl) {
      return '已存在'
    }
    return candidate.region === 'tw' ? '已匹配，缺台服链接' : '可补链接'
  }
  return '需人工确认'
}

function formatCandidate(candidate, match) {
  const status = classifyCandidate(candidate, match)
  const appearance = appearanceScore(candidate)
  const matched = match?.outfit
    ? ` -> ${match.outfit.name} (${match.outfit.id}${match.count > 1 ? `, 同名${match.count}条` : ''})`
    : ''
  const meta = [
    candidate.publishedAt,
    candidate.category,
    candidate.price,
    candidate.productId ? `id=${candidate.productId}` : '',
    candidate.skuId && candidate.skuId !== candidate.productId ? `sku=${candidate.skuId}` : '',
    appearance
  ].filter(Boolean)

  return `- [${status}] ${candidate.source}：${candidate.name}${matched}${meta.length ? ` | ${meta.join(' / ')}` : ''}\n  ${candidate.url}`
}

function summarize(results, failures, storeCatalog, catalogPath) {
  const counts = new Map()
  for (const result of results) {
    counts.set(result.status, (counts.get(result.status) ?? 0) + 1)
  }

  const lines = [
    'NSArmoire 最新商城补全审核',
    `catalog：${storeCatalog.outfits?.length ?? 0} 条`,
    `catalogPath：${catalogPath}`,
    `抓取：${results.length} 条，失败源：${failures.length}`,
    [
      `已存在 ${counts.get('已存在') ?? 0}`,
      `可补链接 ${counts.get('可补链接') ?? 0}`,
      `缺台服链接 ${counts.get('已匹配，缺台服链接') ?? 0}`,
      `需人工确认 ${counts.get('需人工确认') ?? 0}`,
      `忽略 ${counts.get('忽略') ?? 0}`
    ].join(' / '),
    '',
    '重点项：'
  ]

  const priority = results.filter((item) => item.status !== '忽略').slice(0, 24)
  if (priority.length === 0) {
    lines.push('- 暂无需要处理的候选项。')
  } else {
    lines.push(...priority.map((item) => item.line))
  }

  if (failures.length > 0) {
    lines.push('', '抓取失败：')
    for (const failure of failures) {
      lines.push(`- ${failure.source}：${failure.error}`)
    }
  }

  lines.push('', '说明：本命令只读审核，不会修改 armoire-store-catalog.json。')
  return lines.join('\n')
}

export async function auditLatestStoreForArmoire(options = {}) {
  const latestLimit = Number(options.latestLimit || process.env.NS_OPS_ARMOIRE_LATEST_LIMIT || 12)
  const catalogPath = resolveCatalogPath(options)
  const storeCatalog = JSON.parse(await readFile(catalogPath, 'utf8'))
  const index = buildCatalogIndex(storeCatalog)
  const failures = []

  async function collect(source, fetcher) {
    try {
      return await fetcher()
    } catch (error) {
      failures.push({ source, error: error.message || String(error) })
      return []
    }
  }

  const candidateGroups = await Promise.all([
    collect('国服商城', () => fetchCnLatest(latestLimit)),
    collect('国际服商城', () => fetchGlobalLatest(latestLimit)),
    collect('台服新闻', () => fetchTwLatest())
  ])

  const candidates = candidateGroups.flat()
  const seen = new Set()
  const results = []

  for (const candidate of candidates) {
    const dedupeKey = `${candidate.region}:${candidate.productId || normalizeName(candidate.name)}:${candidate.url}`
    if (seen.has(dedupeKey)) {
      continue
    }
    seen.add(dedupeKey)
    const match = findCatalogMatch(candidate, index)
    const status = classifyCandidate(candidate, match)
    results.push({
      status,
      candidate,
      match,
      line: formatCandidate(candidate, match)
    })
  }

  return summarize(results, failures, storeCatalog, catalogPath)
}
