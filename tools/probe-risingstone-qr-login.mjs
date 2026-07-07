#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import fs from "node:fs/promises";

const DEFAULT_REDIRECT_URL = "https://ff14risingstones.web.sdo.com/pc/index.html#/me/info";
const LOGIN_BASE_URL = "https://apiff14risingstones.web.sdo.com/api/home/GHome/login";
const IS_LOGIN_URL = "https://apiff14risingstones.web.sdo.com/api/home/GHome/isLogin";
const DEFAULT_WAIT_MS = 8000;
const DEFAULT_TIMEOUT_MS = 45000;

const scriptDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = dirname(scriptDir);

function parseArgs(argv) {
  const options = {
    headed: false,
    json: false,
    clickQrTab: true,
    acceptLoginTerms: false,
    waitForLoginMs: 0,
    waitMs: DEFAULT_WAIT_MS,
    timeoutMs: DEFAULT_TIMEOUT_MS,
    redirectUrl: DEFAULT_REDIRECT_URL,
  };

  for (const arg of argv) {
    if (arg === "--headed") {
      options.headed = true;
    } else if (arg === "--json") {
      options.json = true;
    } else if (arg === "--no-click-qr-tab") {
      options.clickQrTab = false;
    } else if (arg === "--accept-login-terms") {
      options.acceptLoginTerms = true;
    } else if (arg.startsWith("--wait-ms=")) {
      options.waitMs = Number(arg.slice("--wait-ms=".length));
    } else if (arg.startsWith("--wait-for-login-ms=")) {
      options.waitForLoginMs = Number(arg.slice("--wait-for-login-ms=".length));
    } else if (arg.startsWith("--timeout-ms=")) {
      options.timeoutMs = Number(arg.slice("--timeout-ms=".length));
    } else if (arg.startsWith("--redirect-url=")) {
      options.redirectUrl = arg.slice("--redirect-url=".length);
    } else if (arg === "-h" || arg === "--help") {
      printHelp();
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!Number.isFinite(options.waitMs) || options.waitMs < 0) {
    throw new Error("--wait-ms must be a non-negative number.");
  }
  if (!Number.isFinite(options.timeoutMs) || options.timeoutMs <= 0) {
    throw new Error("--timeout-ms must be a positive number.");
  }
  if (!Number.isFinite(options.waitForLoginMs) || options.waitForLoginMs < 0) {
    throw new Error("--wait-for-login-ms must be a non-negative number.");
  }

  return options;
}

function printHelp() {
  console.log(`Usage: npm run probe:risingstone-login -- [options]

Options:
  --headed                  Launch a visible Chrome window.
  --accept-login-terms      Click the login page privacy/service agreement checkbox before QR detection.
  --no-click-qr-tab         Do not try to switch to the QR-code tab.
  --wait-ms=<ms>            Wait after page load before detection. Default: ${DEFAULT_WAIT_MS}
  --wait-for-login-ms=<ms>  Keep polling sanitized Rising Stones login status after QR detection.
  --timeout-ms=<ms>         Navigation timeout. Default: ${DEFAULT_TIMEOUT_MS}
  --redirect-url=<url>      Redirect URL passed to GHome/login.
  --json                    Print JSON only.

The probe never prints cookies. Screenshots are written under .local/probes,
which is ignored by git. Treat a screenshot as sensitive if it contains a QR code.
`);
}

async function loadPlaywright() {
  try {
    return await import("playwright");
  } catch {
    const require = createRequire(import.meta.url);
    const candidates = [];
    if (process.env.NODE_PATH) {
      candidates.push(...process.env.NODE_PATH.split(process.platform === "win32" ? ";" : ":"));
    }
    if (process.platform === "win32" && process.env.APPDATA) {
      candidates.push(join(process.env.APPDATA, "npm", "node_modules"));
    }

    let npmRootError = "";
    try {
      const command = process.platform === "win32" ? "cmd.exe" : "npm";
      const args = process.platform === "win32" ? ["/d", "/s", "/c", "npm root -g"] : ["root", "-g"];
      candidates.push(execFileSync(command, args, { encoding: "utf8" }).trim());
    } catch (error) {
      npmRootError = error.message;
    }

    const tried = [];
    for (const root of [...new Set(candidates.filter(Boolean))]) {
      const candidate = join(root, "playwright");
      tried.push(candidate);
      try {
        return require(candidate);
      } catch {
        // Try the next known global module path.
      }
    }

    throw new Error(
      "Cannot load Playwright. Install it globally or run from an environment that provides Playwright. " +
        `Tried: ${tried.join(", ") || "(none)"}. npm root -g error: ${npmRootError || "(none)"}`,
    );
  }
}

function buildLoginUrl(redirectUrl) {
  const url = new URL(LOGIN_BASE_URL);
  url.searchParams.set("redirectUrl", redirectUrl);
  return url.toString();
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return `${url.origin}${url.pathname}`;
  } catch {
    return String(value).split("?")[0].split("#")[0];
  }
}

function compactText(value, maxChars = 500) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maxChars);
}

async function getSanitizedLoginState(context) {
  try {
    const response = await context.request.get(IS_LOGIN_URL, {
      headers: {
        accept: "application/json, text/plain, */*",
        referer: "https://ff14risingstones.web.sdo.com/",
      },
      timeout: 15000,
    });
    const status = response.status();
    const contentType = response.headers()["content-type"] || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      const text = await response.text().catch(() => "");
      payload = { text: compactText(text, 200) };
    }
    const code = payload && typeof payload === "object" ? payload.code : undefined;
    const message = payload && typeof payload === "object" ? payload.msg || payload.message || "" : "";
    return {
      httpStatus: status,
      code,
      message: compactText(message, 120),
      loggedIn: [10000, 10002, 10103, 10104].includes(code),
      hasData: Boolean(payload?.data),
    };
  } catch (error) {
    return {
      httpStatus: null,
      code: null,
      message: compactText(error.message, 160),
      loggedIn: false,
      hasData: false,
    };
  }
}

async function waitForLoginState(context, waitForLoginMs) {
  const deadline = Date.now() + waitForLoginMs;
  let state = await getSanitizedLoginState(context);
  while (!state.loggedIn && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    state = await getSanitizedLoginState(context);
  }
  return state;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const { chromium } = await loadPlaywright();
  const loginUrl = buildLoginUrl(options.redirectUrl);
  const outputDir = join(repoRoot, ".local", "probes");
  await fs.mkdir(outputDir, { recursive: true });

  const browser = await chromium.launch({
    channel: "chrome",
    headless: !options.headed,
    args: ["--disable-blink-features=AutomationControlled"],
  });

  const responses = [];
  const screenshotPath = join(outputDir, `risingstone-login-${Date.now()}.png`);

  try {
    const context = await browser.newContext({
      locale: "zh-CN",
      timezoneId: "Asia/Shanghai",
      viewport: { width: 1280, height: 900 },
      userAgent:
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    });
    const page = await context.newPage();

    page.on("response", (response) => {
      const url = response.url();
      const isRelevant =
        url.includes("risingstones.web.sdo.com") ||
        url.includes("apiff14risingstones.web.sdo.com") ||
        response.status() >= 400;
      if (!isRelevant || responses.length >= 30) {
        return;
      }
      responses.push({
        status: response.status(),
        url: safeUrl(url),
      });
    });

    await page.goto(loginUrl, {
      waitUntil: "domcontentloaded",
      timeout: options.timeoutMs,
    });
    await page.waitForTimeout(1000);

    let acceptedLoginTerms = false;
    if (options.acceptLoginTerms) {
      const checkbox = page.locator("input[type='checkbox']").first();
      if ((await checkbox.count().catch(() => 0)) > 0) {
        try {
          await checkbox.click({ timeout: 5000, force: true });
          acceptedLoginTerms = true;
          await page.waitForTimeout(500);
        } catch {
          acceptedLoginTerms = false;
        }
      }
    }

    let clickedQrTab = false;
    if (options.clickQrTab) {
      const qrTab = page.getByText("二维码", { exact: true }).first();
      if ((await qrTab.count().catch(() => 0)) > 0) {
        try {
          await qrTab.click({ timeout: 5000 });
          clickedQrTab = true;
          await page.waitForTimeout(2000);
        } catch {
          clickedQrTab = false;
        }
      }
    }

    await page.waitForTimeout(options.waitMs);
    await page.screenshot({ path: screenshotPath, fullPage: true });

    const bodyText = await page.locator("body").innerText({ timeout: 3000 }).catch(() => "");
    const title = await page.title().catch(() => "");
    const visibleText = compactText(bodyText);
    const elementCounts = {
      canvas: await page.locator("canvas").count().catch(() => 0),
      img: await page.locator("img").count().catch(() => 0),
      svg: await page.locator("svg").count().catch(() => 0),
      input: await page.locator("input").count().catch(() => 0),
    };
    const cookies = await context.cookies();
    const risingstoneCookie = cookies.find((cookie) => cookie.name === "ff14risingstones");
    const hasRisingstoneCookie = Boolean(risingstoneCookie?.value);
    const loginState =
      options.waitForLoginMs > 0
        ? await waitForLoginState(context, options.waitForLoginMs)
        : await getSanitizedLoginState(context);

    const edgeOneBlocked =
      /请求已被站点的安全策略拦截|Tencent Cloud EdgeOne|EdgeOne|567/.test(bodyText) ||
      responses.some((response) => response.status === 567);
    const qrCandidate =
      /扫码|二维码|扫一扫|QR/i.test(bodyText) ||
      elementCounts.canvas > 0 ||
      elementCounts.svg > 0 ||
      elementCounts.img > 0;

    const result = {
      ok: !edgeOneBlocked,
      status: edgeOneBlocked ? "edgeone_blocked" : qrCandidate ? "qr_or_login_candidate" : "unknown_page",
      currentUrl: safeUrl(page.url()),
      title,
      visibleText,
      elementCounts,
      hasRisingstoneCookie,
      risingstoneCookieValueLength: risingstoneCookie?.value?.length || 0,
      loginState,
      acceptedLoginTerms,
      clickedQrTab,
      responses,
      screenshotPath,
      headed: options.headed,
      waitMs: options.waitMs,
    };

    if (options.json) {
      console.log(JSON.stringify(result, null, 2));
    } else {
      console.log(`Status: ${result.status}`);
      console.log(`Current URL: ${result.currentUrl}`);
      console.log(`Title: ${result.title || "-"}`);
      console.log(`Visible text: ${result.visibleText || "-"}`);
      console.log(`Elements: ${JSON.stringify(result.elementCounts)}`);
      console.log(`Accepted login terms: ${result.acceptedLoginTerms ? "yes" : "no"}`);
      console.log(`Clicked QR tab: ${result.clickedQrTab ? "yes" : "no"}`);
      console.log(`Has non-empty ff14risingstones cookie: ${result.hasRisingstoneCookie ? "yes" : "no"}`);
      console.log(`Login state: ${JSON.stringify(result.loginState)}`);
      console.log(`Screenshot: ${result.screenshotPath}`);
      if (edgeOneBlocked) {
        console.log(
          "Finding: backend/headless QR login is currently blocked by EdgeOne. " +
            "Use this as a probe result, not as a production login flow.",
        );
      }
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
