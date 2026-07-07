#!/usr/bin/env node

const DEFAULT_TIMEOUT_MS = 20000;

const SOURCES = [
  {
    id: "cn-news",
    kind: "news",
    region: "cn",
    label: "国服官网新闻",
    url: "https://ff.web.sdo.com/web8/index.html#/newstab/newslist",
    probe: probeCnNews,
  },
  {
    id: "jp-news",
    kind: "news",
    region: "jp",
    label: "国际服 Lodestone",
    url: "https://jp.finalfantasyxiv.com/lodestone",
    probe: probeJpNews,
  },
  {
    id: "cn-store",
    kind: "store",
    region: "cn",
    label: "国服盛趣商城",
    url: "https://qu.sdo.com/product-detail/0d527e640bd3ada51565",
    probe: probeCnStore,
  },
  {
    id: "tw-store",
    kind: "store",
    region: "tw",
    label: "台服水晶商城",
    url: "https://www.ffxiv.com.tw/web/store/product_detail.aspx?id=F0068_251120152555",
    probe: probeGenericProductPage,
  },
  {
    id: "jp-store",
    kind: "store",
    region: "jp",
    label: "日服 Online Store",
    url: "https://store.finalfantasyxiv.com/ffxivstore/ja-jp/product/392",
    probe: probeGenericProductPage,
  },
  {
    id: "kr-store",
    kind: "store",
    region: "kr",
    label: "韩服商城",
    url: "https://www.ff14.co.kr/shop/home/detail/1687",
    probe: probeGenericProductPage,
  },
];

function parseArgs(argv) {
  const args = {
    source: "",
    pretty: true,
    timeoutMs: DEFAULT_TIMEOUT_MS,
  };
  for (const arg of argv) {
    if (arg.startsWith("--source=")) args.source = arg.slice("--source=".length).trim();
    if (arg === "--compact") args.pretty = false;
    if (arg.startsWith("--timeout-ms=")) {
      const value = Number(arg.slice("--timeout-ms=".length));
      if (Number.isFinite(value) && value > 0) args.timeoutMs = value;
    }
  }
  return args;
}

function htmlDecode(value = "") {
  return String(value)
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x([0-9a-f]+);/gi, (_, hex) => String.fromCodePoint(Number.parseInt(hex, 16)))
    .replace(/&#(\d+);/g, (_, dec) => String.fromCodePoint(Number.parseInt(dec, 10)));
}

function stripTags(value = "") {
  return htmlDecode(
    String(value)
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/\s+/g, " ")
      .trim(),
  );
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function absoluteUrl(baseUrl, value = "") {
  try {
    return new URL(value, baseUrl).toString();
  } catch {
    return value;
  }
}

function hashText(text = "") {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) {
    hash ^= text.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

async function fetchText(url, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetch(url, {
      redirect: "follow",
      signal: controller.signal,
      headers: {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7,ko;q=0.6",
        "cache-control": "no-cache",
        "user-agent": "Mozilla/5.0 NightingaleOpsBot-FFXIVWatchProbe/0.1",
      },
    });
    const text = await response.text();
    return {
      ok: response.ok,
      status: response.status,
      url: response.url,
      contentType: response.headers.get("content-type") || "",
      elapsedMs: Date.now() - startedAt,
      text,
    };
  } finally {
    clearTimeout(timeout);
  }
}

function extractTitle(html) {
  return htmlDecode(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || "").trim();
}

function extractMeta(html, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const patterns = [
    new RegExp(`<meta[^>]+property=["']${escaped}["'][^>]+content=["']([^"']*)["']`, "i"),
    new RegExp(`<meta[^>]+name=["']${escaped}["'][^>]+content=["']([^"']*)["']`, "i"),
    new RegExp(`<meta[^>]+content=["']([^"']*)["'][^>]+(?:property|name)=["']${escaped}["']`, "i"),
  ];
  for (const pattern of patterns) {
    const value = html.match(pattern)?.[1];
    if (value) return htmlDecode(value).trim();
  }
  return "";
}

function extractJsonLdProducts(html, baseUrl) {
  const products = [];
  for (const match of html.matchAll(/<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
    try {
      const payload = JSON.parse(htmlDecode(match[1].trim()));
      const nodes = Array.isArray(payload) ? payload : [payload];
      for (const node of nodes) {
        if (!node || typeof node !== "object") continue;
        if (node["@type"] !== "Product") continue;
        products.push({
          id: String(node.sku || node.productID || node.name || hashText(JSON.stringify(node))),
          title: String(node.name || "").trim(),
          url: absoluteUrl(baseUrl, String(node.url || baseUrl)),
          price: node.offers?.price ? String(node.offers.price) : "",
          currency: node.offers?.priceCurrency ? String(node.offers.priceCurrency) : "",
        });
      }
    } catch {
      // Ignore malformed JSON-LD.
    }
  }
  return products;
}

function extractScriptAndApiCandidates(html, baseUrl) {
  const scripts = unique(
    [...html.matchAll(/<script[^>]+src=(?:"([^"]+)"|'([^']+)'|([^\s>]+))/gi)].map((match) =>
      absoluteUrl(baseUrl, match[1] || match[2] || match[3]),
    ),
  );
  const links = unique(
    [...html.matchAll(/<(?:a|link)[^>]+href=(?:"([^"]+)"|'([^']+)'|([^\s>]+))/gi)].map((match) =>
      absoluteUrl(baseUrl, match[1] || match[2] || match[3]),
    ),
  );
  const apiCandidates = unique(
    [...html.matchAll(/(?:"|')([^"']*(?:api|news|notice|article|product|goods|mall|shop)[^"']*)(?:"|')/gi)]
      .map((match) => match[1])
      .filter((value) => value && !value.startsWith("data:"))
      .map((value) => absoluteUrl(baseUrl, value)),
  ).slice(0, 80);
  return { scripts, links, apiCandidates };
}

function extractGenericAnchors(html, baseUrl, { includePattern, limit = 20 } = {}) {
  const items = [];
  const seen = new Set();
  for (const match of html.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/gi)) {
    const href = match[1].match(/\bhref=["']([^"']+)["']/i)?.[1] || "";
    if (!href) continue;
    const url = absoluteUrl(baseUrl, href);
    if (includePattern && !includePattern.test(url)) continue;
    if (seen.has(url)) continue;
    seen.add(url);
    const title = stripTags(match[2]);
    if (!title || title.length < 2) continue;
    items.push({
      id: hashText(url),
      title,
      url,
      publishedAt: "",
      category: "",
    });
    if (items.length >= limit) break;
  }
  return items;
}

async function probeCnNews(source, options) {
  const page = await fetchText(source.url, options);
  const assets = extractScriptAndApiCandidates(page.text, page.url);
  const apiItems = [];
  const apiAttempts = [];
  for (const categoryCode of ["7187"]) {
    const apiUrl = `https://cqnews.web.sdo.com/api/news/newsList?gameCode=ff&CategoryCode=${categoryCode}&pageIndex=0&pageSize=10`;
    try {
      const api = await fetchText(apiUrl, options);
      const payload = JSON.parse(api.text);
      const rows = Array.isArray(payload.Data) ? payload.Data : [];
      apiAttempts.push({
        categoryCode,
        url: apiUrl,
        status: api.status,
        code: payload.Code ?? payload.code ?? "",
        count: rows.length,
      });
      for (const item of rows) {
        apiItems.push({
          id: String(item.Id || item.ID || item.id || hashText(JSON.stringify(item))),
          title: String(item.Title || item.title || "").trim(),
          url: absoluteUrl(
            source.url,
            item.OutLink || `https://ff.web.sdo.com/web8/index.html#/newstab/newscont/${item.Id}`,
          ),
          publishedAt: String(item.PublishDate || item.publishDate || ""),
          category: String(item.CategoryCode || categoryCode),
        });
      }
    } catch (error) {
      apiAttempts.push({
        categoryCode,
        url: apiUrl,
        error: String(error?.message || error),
      });
    }
  }
  const scriptCandidates = [];
  for (const scriptUrl of assets.scripts.slice(0, 12)) {
    if (!/\.(?:js)(?:\?|$)/i.test(scriptUrl)) continue;
    try {
      const script = await fetchText(scriptUrl, options);
      const scriptAssets = extractScriptAndApiCandidates(script.text, script.url);
      scriptCandidates.push({
        script: scriptUrl,
        status: script.status,
        length: script.text.length,
        apiCandidates: scriptAssets.apiCandidates.slice(0, 30),
      });
    } catch (error) {
      scriptCandidates.push({
        script: scriptUrl,
        error: String(error?.message || error),
      });
    }
  }
  return {
    ok: page.ok && (apiItems.length > 0 || assets.scripts.length > 0),
    mode: "spa-probe",
    http: summarizeHttp(page),
    pageTitle: extractTitle(page.text),
    items: apiItems,
    apiAttempts,
    scripts: assets.scripts.slice(0, 20),
    apiCandidates: unique([
      ...assets.apiCandidates,
      ...scriptCandidates.flatMap((item) => item.apiCandidates || []),
    ]).slice(0, 80),
    notes: [
      apiItems.length
        ? "已确认 cqnews.web.sdo.com 新闻 API 可访问；当前 categoryCode=7187 更像置顶/头图源，后续仍需补全普通新闻分类码。"
        : "国服新闻页是 SPA。若 items 为空，需要从 apiCandidates 中确认稳定新闻接口，或后续改用浏览器探针。",
    ],
  };
}

async function probeJpNews(source, options) {
  const page = await fetchText(source.url, options);
  const anchors = extractGenericAnchors(page.text, page.url, {
    includePattern: /\/lodestone\/(?:topics|news)\/detail\//i,
    limit: 12,
  });
  const fallbackAnchors = anchors.length
    ? []
    : extractGenericAnchors(page.text, page.url, {
        includePattern: /\/lodestone\//i,
        limit: 12,
      });
  return {
    ok: page.ok && (anchors.length > 0 || fallbackAnchors.length > 0),
    mode: "html",
    http: summarizeHttp(page),
    pageTitle: extractTitle(page.text),
    items: anchors.length ? anchors : fallbackAnchors,
    notes: anchors.length
      ? ["已从 Lodestone HTML 中解析新闻链接。"]
      : ["未命中 topics/news/detail 链接，返回 Lodestone 相关链接作为 fallback。"],
  };
}

async function probeGenericProductPage(source, options) {
  const page = await fetchText(source.url, options);
  const jsonLdProducts = extractJsonLdProducts(page.text, page.url);
  const pageTitle = extractTitle(page.text);
  const title = extractProductTitle(page.text) || normalizeProductPageTitle(extractMeta(page.text, "og:title") || pageTitle);
  const description = extractMeta(page.text, "og:description") || extractMeta(page.text, "description");
  const image = extractMeta(page.text, "og:image");
  const productId =
    new URL(page.url).searchParams.get("id") ||
    page.url.match(/\/product\/(\d+)/)?.[1] ||
    page.url.match(/\/detail\/(\d+)/)?.[1] ||
    page.url.match(/product-detail\/([^/?#]+)/)?.[1] ||
    hashText(page.url);
  const product = jsonLdProducts[0] || {
    id: productId,
    title: stripTags(title),
    url: page.url,
    price: findPrice(page.text),
    currency: "",
  };
  return {
    ok: page.ok && Boolean(product.title),
    mode: "html-product",
    http: summarizeHttp(page),
    pageTitle,
    items: [
      {
        id: String(product.id || productId),
        title: product.title || stripTags(title),
        url: product.url || page.url,
        price: product.price || findPrice(page.text),
        currency: product.currency || "",
        description: stripTags(description).slice(0, 240),
        image: image ? absoluteUrl(page.url, image) : "",
      },
    ],
    apiCandidates: extractScriptAndApiCandidates(page.text, page.url).apiCandidates.slice(0, 40),
    notes: ["当前按商品详情页解析。商城更新 MVP 仍需要后续找到商品列表页或列表 API。"],
  };
}

async function probeCnStore(source, options) {
  const page = await fetchText(source.url, options);
  const skuId = page.url.match(/product-detail\/([^/?#]+)/)?.[1] || "";
  const merchantId = await fetchCnStoreMerchantId(skuId, options);
  const apiUrl = `https://sqmallservice.u.sdo.com/api/ps/product/allInOne?skuId=${encodeURIComponent(skuId)}`;
  const api = await fetchTextWithHeaders(apiUrl, {
    ...options,
    headers: cnStoreHeaders(merchantId),
  });
  const payload = JSON.parse(api.text);
  const data = payload.data || {};
  const sku = data.priceInfo?.sku || {};
  const product = data.productBasicInfo?.product || {};
  const title = String(sku.productName || product.productName || extractProductTitle(page.text) || extractTitle(page.text)).trim();
  const price = String(sku.netPrice || sku.memberPrice || sku.originalPrice || "");
  const currency = String(sku.currency?.shortName || sku.currency?.baseUnit || sku.currency?.fullName || "");
  const image = sku.picUrl || product.picUrl || "";
  const description = stripTags(sku.description || product.description || product.productContent || "");

  return {
    ok: page.ok && api.ok && payload.resultCode === 0 && Boolean(title),
    mode: "api-product",
    http: summarizeHttp(page),
    pageTitle: extractTitle(page.text),
    api: {
      status: api.status,
      finalUrl: api.url,
      merchantId,
      resultCode: payload.resultCode,
      resultMsg: payload.resultMsg,
    },
    items: [
      {
        id: sku.skuId || skuId,
        productId: String(sku.productId || product.id || ""),
        title,
        url: page.url,
        price,
        currency,
        description: description.slice(0, 240),
        image: image ? absoluteUrl(page.url, image) : "",
        saleable: sku.saleable ?? "",
        publishStatus: sku.publishStatus ?? product.publishStatus ?? "",
      },
    ],
    notes: ["国服商城详情 API 可用。商城更新 MVP 仍需要后续找到商品列表页或列表 API。"],
  };
}

async function fetchCnStoreMerchantId(skuId, options) {
  if (!skuId) return "1";
  const apiUrl = `https://sqmallservice.u.sdo.com/api/cs/merchant/getBySkuId?skuId=${encodeURIComponent(skuId)}`;
  try {
    const response = await fetchTextWithHeaders(apiUrl, {
      ...options,
      headers: cnStoreHeaders("1"),
    });
    const payload = JSON.parse(response.text);
    return String(payload.data?.merchantId || "1");
  } catch {
    return "1";
  }
}

function cnStoreHeaders(merchantId) {
  return {
    "accept": "application/json,text/plain,*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "user-agent": "Mozilla/5.0 NightingaleOpsBot-FFXIVWatchProbe/0.1",
    "qu-merchant-id": String(merchantId || "1"),
    "qu-hardware-platform": "3",
    "qu-software-platform": "1",
    "qu-deploy-platform": "1",
    "qu-web-host": "qu.sdo.com",
  };
}

async function fetchTextWithHeaders(url, { timeoutMs = DEFAULT_TIMEOUT_MS, headers = {} } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetch(url, {
      redirect: "follow",
      signal: controller.signal,
      headers,
    });
    const text = await response.text();
    return {
      ok: response.ok,
      status: response.status,
      url: response.url,
      contentType: response.headers.get("content-type") || "",
      elapsedMs: Date.now() - startedAt,
      text,
    };
  } finally {
    clearTimeout(timeout);
  }
}

function extractProductTitle(html) {
  const candidates = [
    ...[...html.matchAll(/<h1\b[^>]*>([\s\S]{0,600}?)<\/h1>/gi)].map((match) => stripTags(match[1])),
    ...[...html.matchAll(/<h2\b[^>]*>([\s\S]{0,600}?)<\/h2>/gi)].map((match) => stripTags(match[1])),
    ...[...html.matchAll(/productName\s*[:=]\s*["']([^"']+)["']/gi)].map((match) => stripTags(match[1])),
  ].filter(Boolean);
  const genericTitles = new Set([
    "商品详情",
    "商品介紹",
    "商品介绍",
    "商品紹介",
    "關於此商品",
    "アイテムについて",
    "NOTICE",
    "{{product.name}}",
    "FINAL FANTASY XIV ONLINE STORE 繁體中文版 水晶商城",
  ]);
  return candidates.find((value) => value.length >= 2 && !genericTitles.has(value) && !value.includes("{{")) || "";
}

function normalizeProductPageTitle(title) {
  const value = stripTags(title);
  if (!value) return "";
  const parts = value.split(/\s+\|\s+|\s+-\s+/).map((part) => part.trim()).filter(Boolean);
  if (parts.length > 1) return parts[0];
  return value;
}

function findPrice(html) {
  const text = stripTags(html);
  const patterns = [
    /(?:¥|￥|NT\$|₩)\s?[\d,]+(?:\.\d+)?/,
    /[\d,]+(?:\.\d+)?\s?(?:JPY|KRW|TWD|CNY|円|크리스탈|水晶)/i,
  ];
  for (const pattern of patterns) {
    const value = text.match(pattern)?.[0];
    if (value) return value.trim();
  }
  return "";
}

function summarizeHttp(result) {
  return {
    status: result.status,
    finalUrl: result.url,
    contentType: result.contentType,
    length: result.text.length,
    elapsedMs: result.elapsedMs,
  };
}

async function probeSource(source, options) {
  const startedAt = new Date().toISOString();
  try {
    const result = await source.probe(source, options);
    return {
      source: source.id,
      label: source.label,
      kind: source.kind,
      region: source.region,
      url: source.url,
      checkedAt: startedAt,
      ...result,
    };
  } catch (error) {
    return {
      source: source.id,
      label: source.label,
      kind: source.kind,
      region: source.region,
      url: source.url,
      checkedAt: startedAt,
      ok: false,
      error: {
        name: error?.name || "Error",
        message: String(error?.message || error),
      },
      items: [],
      notes: ["探针请求失败。后续可在服务器环境或浏览器环境重试。"],
    };
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const selected = args.source
    ? SOURCES.filter((source) => source.id === args.source)
    : SOURCES;

  if (!selected.length) {
    console.error(`Unknown source: ${args.source}`);
    console.error(`Available sources: ${SOURCES.map((source) => source.id).join(", ")}`);
    process.exitCode = 2;
    return;
  }

  const results = [];
  for (const source of selected) {
    results.push(await probeSource(source, args));
  }

  const payload = {
    ok: results.every((result) => result.ok),
    checkedAt: new Date().toISOString(),
    results,
  };
  console.log(JSON.stringify(payload, null, args.pretty ? 2 : 0));
  if (!payload.ok) process.exitCode = 1;
}

await main();
