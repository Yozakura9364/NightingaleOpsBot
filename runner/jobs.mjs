import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, extname, isAbsolute, normalize, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  getTencentCloudTrafficDebugReport,
  getTencentCloudTrafficReport
} from './tencentCloudTraffic.mjs'
import {
  getSystemAlertsReport,
  getSystemDailyReport,
  getSystemHealthReport
} from './systemHealth.mjs'
import {
  auditLatestStoreForArmoire,
  checkStoreCatalogMirror,
  prepareSyncStoreCatalogMirror,
  syncStoreCatalogMirror
} from './armoireStoreLatest.mjs'
import {
  ackFashionCheckNotification,
  getFashionCheckSubscriberUpdates,
  getFashionCheckStatus,
  peekFashionCheckNotification,
  runFashionCheckTick
} from './fashionCheckCollector.mjs'
import { getPublicFashionCheckAnswer } from './fashionCheckPublicSnapshot.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = resolve(process.env.NS_OPS_PROJECT_ROOT || resolve(__dirname, '..'))
const V2_ROOT = resolve(process.env.NS_OPS_V2_ROOT || resolve(PROJECT_ROOT, '..', 'NightingaleSilenceWebV2'))
const ASTRBOT_ROOT = resolve(
  process.env.NS_OPS_ASTRBOT_ROOT || resolve(PROJECT_ROOT, '..', 'astrbot')
)
const LOG_DIR = resolve(process.env.NS_OPS_LOG_DIR || resolve(PROJECT_ROOT, '.local', 'logs'))
const INBOX_ROOT = resolve(process.env.NS_OPS_FILE_WRITE_ROOT || resolve(PROJECT_ROOT, '.local', 'inbox'))
const DEPLOY_NPM_SCRIPT = String(process.env.NS_OPS_DEPLOY_NPM_SCRIPT || '').trim()

const isWindows = process.platform === 'win32'
const commandShell = isWindows ? process.env.ComSpec || 'cmd.exe' : ''

const DEFAULT_TIMEOUT_MS = 120000
const LONG_TIMEOUT_MS = 600000
const MAX_CAPTURE_CHARS = 120000
const MAX_FILE_WRITE_CHARS = 20000
const FILE_WRITE_EXTENSIONS = new Set(['.md', '.txt', '.json', '.log'])

function stripAnsi(text) {
  return String(text ?? '').replace(/\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, '')
}

function redact(text) {
  return stripAnsi(text)
    .replace(/(Authorization:\s*Bearer\s+)[^\s]+/gi, '$1[redacted]')
    .replace(/(access[_-]?token["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/(api[_-]?key["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/(secret["']?\s*[:=]\s*["']?)[^"',\s]+/gi, '$1[redacted]')
    .replace(/sk-[A-Za-z0-9_-]{20,}/g, 'sk-[redacted]')
}

function trimCapture(value) {
  const text = redact(value)
  if (text.length <= MAX_CAPTURE_CHARS) {
    return text
  }
  return `${text.slice(0, MAX_CAPTURE_CHARS)}\n...[output truncated by ns-ops-runner]`
}

function normalizeNewlines(text) {
  return String(text ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n')
}

function compactSummary(output) {
  const lines = normalizeNewlines(output)
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (lines.length <= 12) {
    return lines.join('\n')
  }
  return lines.slice(-12).join('\n')
}

function createStep(command, args, options = {}) {
  return {
    command,
    args,
    cwd: options.cwd,
    timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
    label: options.label ?? [command, ...args].join(' ')
  }
}

function createNpmStep(args, options = {}) {
  if (isWindows) {
    return createStep(commandShell, ['/d', '/s', '/c', 'npm.cmd', ...args], {
      ...options,
      label: options.label ?? ['npm', ...args].join(' ')
    })
  }
  return createStep('npm', args, options)
}

function createGitStep(args, options = {}) {
  return createStep('git', args, {
    cwd: V2_ROOT,
    ...options,
    label: options.label ?? ['git', ...args].join(' ')
  })
}

function payloadObject(payload) {
  return payload && typeof payload === 'object' && !Array.isArray(payload) ? payload : {}
}

function userError(message) {
  const error = new Error(message)
  error.statusCode = 400
  throw error
}

function outputLines(text) {
  return normalizeNewlines(text)
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

function normalizeCommitMessage(payload) {
  const message = String(payloadObject(payload).message || '').trim()
  if (!message) {
    userError('缺少 commit message。用法：/ns git commit <中文提交说明>')
  }
  if (message.length > 200) {
    userError('commit message 过长，请控制在 200 字以内。')
  }
  return message
}

async function runCheckedStep(step, failureMessage) {
  const result = await runProcess(step)
  if (!result.ok) {
    userError(
      [failureMessage, result.stdout, result.stderr]
        .filter(Boolean)
        .join('\n')
        .trim()
    )
  }
  return result
}

async function prepareGitCommit(payload) {
  const message = normalizeCommitMessage(payload)
  const staged = await runCheckedStep(
    createGitStep(['diff', '--cached', '--name-only'], { label: 'git diff --cached --name-only' }),
    '读取暂存文件失败。'
  )
  const files = outputLines(staged.stdout)
  if (files.length === 0) {
    userError('没有 staged 文件，QQ 端不会自动 git add。请先在本机确认并暂存本次要提交的文件。')
  }

  const stat = await runCheckedStep(
    createGitStep(['diff', '--cached', '--stat'], { label: 'git diff --cached --stat' }),
    '读取 staged diff 失败。'
  )

  return {
    ok: true,
    payload: { message },
    preview: [
      `commit message: ${message}`,
      '',
      `staged files (${files.length}):`,
      ...files.slice(0, 30),
      files.length > 30 ? `...还有 ${files.length - 30} 个文件` : '',
      stat.stdout ? ['', 'stat:', stat.stdout].join('\n') : ''
    ]
      .filter(Boolean)
      .join('\n')
  }
}

async function prepareGitPush() {
  const status = await runCheckedStep(
    createGitStep(['status', '--short'], { label: 'git status --short' }),
    '读取工作区状态失败。'
  )
  if (status.stdout.trim()) {
    userError(`工作区不干净，暂不允许 QQ 直接 push。\n${status.stdout.trim()}`)
  }

  const branch = await runCheckedStep(
    createGitStep(['branch', '--show-current'], { label: 'git branch --show-current' }),
    '读取当前分支失败。'
  )
  const branchName = branch.stdout.trim()
  if (!branchName) {
    userError('当前不在普通分支上，暂不允许 QQ push。')
  }

  const upstream = await runProcess(
    createGitStep(['rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}'], {
      label: 'git upstream'
    })
  )
  if (!upstream.ok || !upstream.stdout.trim()) {
    userError('当前分支没有 upstream，先在本机配置后再允许 QQ push。')
  }

  const lastCommit = await runCheckedStep(
    createGitStep(['log', '-1', '--pretty=format:%h %s'], { label: 'git log -1' }),
    '读取最近提交失败。'
  )
  const statusBranch = await runCheckedStep(
    createGitStep(['status', '-sb'], { label: 'git status -sb' }),
    '读取分支同步状态失败。'
  )

  return {
    ok: true,
    payload: {},
    preview: [
      `branch: ${branchName}`,
      `upstream: ${upstream.stdout.trim()}`,
      `latest: ${lastCommit.stdout.trim()}`,
      '',
      statusBranch.stdout.trim()
    ].join('\n')
  }
}

async function packageScripts() {
  const packageJson = JSON.parse(await readFile(resolve(V2_ROOT, 'package.json'), 'utf8'))
  return packageJson.scripts || {}
}

async function hasPackageScript(scriptName) {
  try {
    const scripts = await packageScripts()
    return Boolean(scripts[scriptName])
  } catch {
    return false
  }
}

function isNpmUnavailable(result) {
  const text = `${result.stderr || ''}\n${result.stdout || ''}`
  return /spawn npm ENOENT|npm(?:\.cmd)?: command not found|ENOENT/i.test(text)
}

async function runV2NpmOrFallback(scriptName, fallback, fallbackReason) {
  if (await hasPackageScript(scriptName)) {
    const result = await runProcess(
      createNpmStep(['run', scriptName], {
        cwd: V2_ROOT,
        timeoutMs: LONG_TIMEOUT_MS
      })
    )

    if (result.ok) {
      return {
        ok: true,
        output: [result.stdout, result.stderr ? `[stderr]\n${result.stderr}` : '']
          .filter(Boolean)
          .join('\n')
          .trim()
      }
    }

    if (!isNpmUnavailable(result)) {
      return {
        ok: false,
        output: [result.stdout, result.stderr ? `[stderr]\n${result.stderr}` : '']
          .filter(Boolean)
          .join('\n')
          .trim()
      }
    }
  }

  const fallbackResult = await fallback()
  const prefix = fallbackReason ? `${fallbackReason}\n\n` : ''
  return {
    ok: Boolean(fallbackResult.ok),
    output: `${prefix}${fallbackResult.output || ''}`.trim()
  }
}

async function prepareDeploy() {
  if (!DEPLOY_NPM_SCRIPT) {
    userError('尚未配置真实部署脚本。需要给 runner 设置 NS_OPS_DEPLOY_NPM_SCRIPT，例如 deploy。')
  }
  if (!/^[A-Za-z0-9:_-]+$/.test(DEPLOY_NPM_SCRIPT)) {
    userError('NS_OPS_DEPLOY_NPM_SCRIPT 只能包含字母、数字、冒号、下划线和短横线。')
  }

  const scripts = await packageScripts()
  if (!scripts[DEPLOY_NPM_SCRIPT]) {
    userError(`package.json 里没有 npm script：${DEPLOY_NPM_SCRIPT}`)
  }

  return {
    ok: true,
    payload: { script: DEPLOY_NPM_SCRIPT },
    preview: [`npm script: ${DEPLOY_NPM_SCRIPT}`, `command: npm run ${DEPLOY_NPM_SCRIPT}`].join('\n')
  }
}

function resolveInboxFile(relativePath) {
  const raw = String(relativePath || '').trim().replace(/\\/g, '/')
  if (!raw) {
    userError('缺少写入路径。用法：/ns file write <文件名.md> <内容>')
  }
  if (raw.includes('\0') || isAbsolute(raw) || /^[A-Za-z]:/.test(raw)) {
    userError('写入路径必须是相对路径。')
  }

  const normalized = normalize(raw).replace(/\\/g, '/')
  if (normalized === '.' || normalized.startsWith('../') || normalized.includes('/../')) {
    userError('写入路径不能跳出安全目录。')
  }

  const parts = normalized.split('/').filter(Boolean)
  if (parts.some((part) => part === '.' || part === '..')) {
    userError('写入路径不能包含 . 或 ..。')
  }
  if (parts.some((part) => ['.git', 'node_modules', 'dist'].includes(part))) {
    userError('写入路径不能指向 .git、node_modules 或 dist。')
  }

  const extension = extname(normalized).toLowerCase()
  if (!FILE_WRITE_EXTENSIONS.has(extension)) {
    userError('当前只允许写入 .md、.txt、.json、.log 文件。')
  }

  const absolutePath = resolve(INBOX_ROOT, normalized)
  const relativeToRoot = relative(INBOX_ROOT, absolutePath)
  if (relativeToRoot.startsWith('..') || isAbsolute(relativeToRoot)) {
    userError('写入路径不能跳出安全目录。')
  }

  return {
    relativePath: normalized,
    absolutePath
  }
}

function normalizeFileWritePayload(payload) {
  const data = payloadObject(payload)
  const file = resolveInboxFile(data.relativePath)
  const content = String(data.content ?? '')
  if (content.length > MAX_FILE_WRITE_CHARS) {
    userError(`写入内容过长，请控制在 ${MAX_FILE_WRITE_CHARS} 字以内。`)
  }
  const sha256 = createHash('sha256').update(content, 'utf8').digest('hex')
  return {
    relativePath: file.relativePath,
    absolutePath: file.absolutePath,
    content,
    sha256
  }
}

async function prepareFileWrite(payload) {
  const data = normalizeFileWritePayload(payload)
  return {
    ok: true,
    payload: {
      relativePath: data.relativePath,
      content: data.content
    },
    preview: [
      `safeRoot: ${INBOX_ROOT}`,
      `file: ${data.relativePath}`,
      `chars: ${data.content.length}`,
      `sha256: ${data.sha256.slice(0, 16)}`
    ].join('\n')
  }
}

export const jobs = new Map(
  Object.entries({
    'system.status': {
      title: 'NS Ops runner 状态',
      description: '查看 runner、V2 和 AstrBot 路径。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: [
            `runner: ok`,
            `node: ${process.version}`,
            `platform: ${process.platform}`,
            `v2Root: ${V2_ROOT}`,
            `astrbotRoot: ${ASTRBOT_ROOT}`,
            `pid: ${process.pid}`
          ].join('\n')
        }
      }
    },
    'astrbot.logs': {
      title: 'AstrBot 最近日志',
      description: '读取 astrbot 容器最近日志。',
      readOnly: true,
      steps: [createStep('docker', ['logs', '--tail', '120', 'astrbot'], { timeoutMs: 30000 })]
    },
    'system.health': {
      title: 'NS 综合健康检查',
      description: '查看流量、磁盘、CPU/内存、容器、服务、站点、证书和错误日志。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await getSystemHealthReport()
        }
      }
    },
    'system.daily': {
      title: 'NS 每日状态',
      description: '生成每日服务器状态报告。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await getSystemDailyReport()
        }
      }
    },
    'system.alerts': {
      title: 'NS 异常告警检查',
      description: '检查是否存在需要主动推送的服务器异常。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await getSystemAlertsReport()
        }
      }
    },
    'fashion-check.tick': {
      title: '时尚品鉴自动采集',
      description: '按北京时间窗口采集时尚品鉴公开来源。',
      readOnly: false,
      run: async (payload) => runFashionCheckTick(payload)
    },
    'fashion-check.answer': {
      title: '本周时尚品鉴答案',
      description: '返回可公开发送到群聊的本周答案（优先 Hermes 发布的公开快照，失败回退私有 staging）。',
      readOnly: true,
      run: async (payload) => ({ ok: true, output: await getPublicFashionCheckAnswer(payload) })
    },
    'fashion-check.status': {
      title: '时尚品鉴采集状态',
      description: '查看当前周次、来源和通知队列。',
      readOnly: true,
      run: async () => ({ ok: true, output: await getFashionCheckStatus() })
    },
    'fashion-check.notifications.peek': {
      title: '读取时尚品鉴通知',
      description: '读取一条尚未发送的 QQ 通知。',
      readOnly: true,
      run: async () => ({
        ok: true,
        output: JSON.stringify(await peekFashionCheckNotification())
      })
    },
    'fashion-check.notifications.ack': {
      title: '确认时尚品鉴通知',
      description: '在 QQ 成功发送后确认消费通知。',
      readOnly: false,
      run: async (payload) => ({
        ok: true,
        output: JSON.stringify({
          acknowledged: await ackFashionCheckNotification(payload.id)
        })
      })
    },
    'fashion-check.subscriber-updates': {
      title: '时尚品鉴订阅更新',
      description: '读取最近有效的时尚品鉴订阅更新，不包含订阅者信息。',
      readOnly: true,
      run: async () => ({
        ok: true,
        output: JSON.stringify(await getFashionCheckSubscriberUpdates())
      })
    },
    'cloud.tencent.traffic.today': {
      title: '腾讯云今日流量',
      description: '读取腾讯云轻量应用服务器流量包、今日公网流量估算和峰值。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await getTencentCloudTrafficReport()
        }
      }
    },
    'cloud.tencent.traffic.debug': {
      title: '腾讯云监控调试',
      description: '测试腾讯云轻量应用服务器 API、云监控指标和维度。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await getTencentCloudTrafficDebugReport()
        }
      }
    },
    'v2.status': {
      title: 'V2 工作区状态',
      description: '查看当前分支、最近提交和 git status。',
      readOnly: true,
      steps: [
        createStep('git', ['branch', '--show-current'], { cwd: V2_ROOT, label: 'git branch' }),
        createStep('git', ['log', '-1', '--pretty=format:%h %s'], {
          cwd: V2_ROOT,
          label: 'git log -1'
        }),
        createStep('git', ['status', '--short'], { cwd: V2_ROOT, label: 'git status --short' })
      ]
    },
    'v2.check': {
      title: 'V2 项目检查',
      description: '运行 npm run check。',
      readOnly: true,
      steps: [createNpmStep(['run', 'check'], { cwd: V2_ROOT, timeoutMs: LONG_TIMEOUT_MS })]
    },
    'v2.build': {
      title: 'V2 项目构建',
      description: '运行 npm run build。',
      readOnly: true,
      steps: [createNpmStep(['run', 'build'], { cwd: V2_ROOT, timeoutMs: LONG_TIMEOUT_MS })]
    },
    'v2.deploy': {
      title: 'V2 项目部署',
      description: '运行已配置的部署 npm script。',
      readOnly: false,
      requiresConfirmation: true,
      prepare: prepareDeploy,
      steps: (payload) => [
        createNpmStep(['run', payloadObject(payload).script || DEPLOY_NPM_SCRIPT], {
          cwd: V2_ROOT,
          timeoutMs: LONG_TIMEOUT_MS
        })
      ]
    },
    'git.status': {
      title: 'Git 状态',
      description: '查看当前分支、最近提交和工作区状态。',
      readOnly: true,
      steps: [
        createGitStep(['branch', '--show-current'], { label: 'git branch' }),
        createGitStep(['log', '-1', '--pretty=format:%h %s'], { label: 'git log -1' }),
        createGitStep(['status', '-sb'], { label: 'git status -sb' }),
        createGitStep(['status', '--short'], { label: 'git status --short' })
      ]
    },
    'git.diff': {
      title: 'Git 差异摘要',
      description: '查看 staged 和未 staged diff 统计。',
      readOnly: true,
      steps: [
        createGitStep(['diff', '--stat'], { label: 'git diff --stat' }),
        createGitStep(['diff', '--cached', '--stat'], { label: 'git diff --cached --stat' }),
        createGitStep(['diff', '--name-only'], { label: 'git diff --name-only' }),
        createGitStep(['diff', '--cached', '--name-only'], { label: 'git diff --cached --name-only' })
      ]
    },
    'git.commit': {
      title: 'Git 提交 staged 文件',
      description: '提交已经 staged 的文件；QQ 端不会自动 git add。',
      readOnly: false,
      requiresConfirmation: true,
      prepare: prepareGitCommit,
      steps: (payload) => [
        createGitStep(['commit', '-m', normalizeCommitMessage(payload)], { label: 'git commit' }),
        createGitStep(['status', '--short'], { label: 'git status --short' })
      ]
    },
    'git.push': {
      title: 'Git 推送当前分支',
      description: '推送当前分支到已配置 upstream。',
      readOnly: false,
      requiresConfirmation: true,
      prepare: prepareGitPush,
      steps: [
        createGitStep(['push'], { label: 'git push', timeoutMs: LONG_TIMEOUT_MS }),
        createGitStep(['status', '-sb'], { label: 'git status -sb' })
      ]
    },
    'file.write': {
      title: '写入 Ops inbox 文件',
      description: '写入 tools/ns-ops-runner/inbox 下的安全文本文件。',
      readOnly: false,
      requiresConfirmation: true,
      prepare: prepareFileWrite,
      run: async (payload) => {
        const data = normalizeFileWritePayload(payload)
        await mkdir(dirname(data.absolutePath), { recursive: true })
        await writeFile(data.absolutePath, data.content, 'utf8')
        return {
          ok: true,
          output: [
            '写入完成',
            `safeRoot: ${INBOX_ROOT}`,
            `file: ${data.relativePath}`,
            `chars: ${data.content.length}`,
            `sha256: ${data.sha256.slice(0, 16)}`
          ].join('\n')
        }
      }
    },
    'armoire.check-store': {
      title: 'NSArmoire 商城目录校验',
      description: '运行商城目录校验；服务器轻量模式下校验 catalog 镜像。',
      readOnly: true,
      run: async () =>
        runV2NpmOrFallback(
          'check:armoire-store-catalog:quiet',
          () => checkStoreCatalogMirror({ v2Root: V2_ROOT }),
          '未检测到可用的完整 V2 npm 校验环境，已改用服务器 catalog 镜像轻量校验。'
        )
    },
    'armoire.audit-store': {
      title: 'NSArmoire 商城覆盖审计',
      description: '审计商城覆盖情况；服务器轻量模式下降级为最新商城审核。',
      readOnly: true,
      run: async () =>
        runV2NpmOrFallback(
          'audit:armoire-store-coverage',
          async () => ({
            ok: true,
            output: await auditLatestStoreForArmoire({ v2Root: V2_ROOT })
          }),
          '未检测到可用的完整 V2 npm 审计环境，已改用最新商城补全审核。'
        )
    },
    'armoire.audit-store-latest': {
      title: 'NSArmoire 最新商城补全审核',
      description: '抓取各服最新商城/商城新闻，审核是否已进入商城目录；只读，不写 JSON。',
      readOnly: true,
      run: async () => {
        return {
          ok: true,
          output: await auditLatestStoreForArmoire({ v2Root: V2_ROOT })
        }
      }
    },
    'armoire.sync-catalog': {
      title: 'NSArmoire 同步商城目录镜像',
      description: '把 V2/source catalog 同步到 runner 镜像；需要确认。',
      readOnly: false,
      requiresConfirmation: true,
      prepare: async () => prepareSyncStoreCatalogMirror({ v2Root: V2_ROOT }),
      run: async (payload) => syncStoreCatalogMirror({ v2Root: V2_ROOT, ...payloadObject(payload) })
    },
    'restart.astrbot': {
      title: '重启 AstrBot 容器',
      description: '通过 docker compose restart astrbot 重启 AstrBot。',
      readOnly: false,
      requiresConfirmation: true,
      steps: [
        createStep('docker', ['compose', 'restart', 'astrbot'], {
          cwd: ASTRBOT_ROOT,
          timeoutMs: 120000
        })
      ]
    }
  })
)

function runProcess(step) {
  return new Promise((resolveStep) => {
    const startedAt = new Date()
    const child = spawn(step.command, step.args, {
      cwd: step.cwd,
      windowsHide: true,
      shell: false,
      env: {
        ...process.env,
        CI: process.env.CI || '1',
        FORCE_COLOR: '0',
        NO_COLOR: '1'
      }
    })

    let stdout = ''
    let stderr = ''
    let timedOut = false

    const timer = setTimeout(() => {
      timedOut = true
      child.kill('SIGTERM')
      setTimeout(() => {
        if (!child.killed) {
          child.kill('SIGKILL')
        }
      }, 2500).unref()
    }, step.timeoutMs)

    child.stdout?.on('data', (chunk) => {
      stdout = trimCapture(stdout + chunk.toString('utf8'))
    })
    child.stderr?.on('data', (chunk) => {
      stderr = trimCapture(stderr + chunk.toString('utf8'))
    })

    child.on('error', (error) => {
      clearTimeout(timer)
      resolveStep({
        label: step.label,
        command: step.command,
        args: step.args,
        cwd: step.cwd,
        startedAt: startedAt.toISOString(),
        finishedAt: new Date().toISOString(),
        ok: false,
        exitCode: null,
        timedOut,
        stdout,
        stderr: trimCapture(`${stderr}\n${error.message}`)
      })
    })

    child.on('close', (exitCode) => {
      clearTimeout(timer)
      resolveStep({
        label: step.label,
        command: step.command,
        args: step.args,
        cwd: step.cwd,
        startedAt: startedAt.toISOString(),
        finishedAt: new Date().toISOString(),
        ok: exitCode === 0 && !timedOut,
        exitCode,
        timedOut,
        stdout,
        stderr
      })
    })
  })
}

export function listJobs() {
  return Array.from(jobs.entries()).map(([id, job]) => ({
    id,
    title: job.title,
    description: job.description,
    readOnly: job.readOnly,
    requiresConfirmation: Boolean(job.requiresConfirmation)
  }))
}

export function getJob(jobId) {
  return jobs.get(jobId)
}

export async function prepareJob(jobId, payload = {}) {
  const job = jobs.get(jobId)
  if (!job) {
    const error = new Error(`Unknown job: ${jobId}`)
    error.statusCode = 404
    throw error
  }

  if (typeof job.prepare === 'function') {
    return job.prepare(payload)
  }

  return {
    ok: true,
    payload: payloadObject(payload),
    preview: job.description || ''
  }
}

export async function runJob(jobId, payload = {}) {
  const job = jobs.get(jobId)
  if (!job) {
    const error = new Error(`Unknown job: ${jobId}`)
    error.statusCode = 404
    throw error
  }

  const startedAt = new Date()
  let steps = []
  let ok = true

  if (typeof job.run === 'function') {
    const result = await job.run(payloadObject(payload))
    steps = [
      {
        label: job.title,
        ok: Boolean(result.ok),
        stdout: result.output ?? '',
        stderr: result.error ?? '',
        exitCode: result.ok ? 0 : 1
      }
    ]
    ok = Boolean(result.ok)
  } else {
    const stepList =
      typeof job.steps === 'function' ? await job.steps(payloadObject(payload)) : job.steps ?? []
    for (const step of stepList) {
      const result = await runProcess(step)
      steps.push(result)
      if (!result.ok) {
        ok = false
        break
      }
    }
  }

  const finishedAt = new Date()
  const output = steps
    .map((step) => {
      const header = `# ${step.label}`
      const body = [step.stdout, step.stderr ? `[stderr]\n${step.stderr}` : '']
        .filter(Boolean)
        .join('\n')
      return `${header}\n${body}`.trim()
    })
    .join('\n\n')

  const result = {
    jobId,
    title: job.title,
    ok,
    status: ok ? 'success' : 'failed',
    readOnly: job.readOnly,
    startedAt: startedAt.toISOString(),
    finishedAt: finishedAt.toISOString(),
    durationMs: finishedAt.getTime() - startedAt.getTime(),
    summary: compactSummary(output),
    output: trimCapture(output),
    steps
  }

  await writeJobLog(result)
  return result
}

async function writeJobLog(result) {
  await mkdir(LOG_DIR, { recursive: true })
  const safeId = result.jobId.replace(/[^a-z0-9_.-]/gi, '_')
  const timestamp = result.startedAt.replace(/[:.]/g, '-')
  const path = resolve(LOG_DIR, `${timestamp}_${safeId}.json`)
  await writeFile(path, `${JSON.stringify(result, null, 2)}\n`, 'utf8')
}

export const paths = {
  PROJECT_ROOT,
  V2_ROOT,
  ASTRBOT_ROOT,
  LOG_DIR,
  INBOX_ROOT
}
