import { createHash, createHmac } from 'node:crypto'

const MONITOR_ENDPOINT = 'monitor.tencentcloudapi.com'
const MONITOR_SERVICE = 'monitor'
const MONITOR_ACTION = 'GetMonitorData'
const MONITOR_VERSION = '2018-07-24'
const LIGHTHOUSE_ENDPOINT = 'lighthouse.tencentcloudapi.com'
const LIGHTHOUSE_SERVICE = 'lighthouse'
const LIGHTHOUSE_VERSION = '2020-03-24'
const LIGHTHOUSE_NAMESPACE = 'QCE/LIGHTHOUSE'
const TIME_ZONE_OFFSET = '+08:00'

function userError(message) {
  const error = new Error(message)
  error.statusCode = 400
  throw error
}

function sha256Hex(value) {
  return createHash('sha256').update(value, 'utf8').digest('hex')
}

function hmac(key, value, encoding) {
  return createHmac('sha256', key).update(value, 'utf8').digest(encoding)
}

function credentials() {
  const secretId = String(process.env.TENCENTCLOUD_SECRET_ID || '').trim()
  const secretKey = String(process.env.TENCENTCLOUD_SECRET_KEY || '').trim()
  if (!secretId || !secretKey) {
    userError(
      '腾讯云 API 凭据未配置。请在 runner.env 设置 TENCENTCLOUD_SECRET_ID 和 TENCENTCLOUD_SECRET_KEY。'
    )
  }
  return { secretId, secretKey }
}

async function metadata(path) {
  const response = await fetch(`http://metadata.tencentyun.com/latest/meta-data/${path}`, {
    signal: AbortSignal.timeout(2000)
  })
  if (!response.ok) {
    throw new Error(`metadata ${path} HTTP ${response.status}`)
  }
  return (await response.text()).trim()
}

async function region() {
  const configured = String(process.env.TENCENTCLOUD_REGION || '').trim()
  if (configured) {
    return configured
  }
  return metadata('placement/region')
}

function shanghaiDateParts(date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).formatToParts(date)
  return Object.fromEntries(parts.map((part) => [part.type, part.value]))
}

function todayRange() {
  const parts = shanghaiDateParts()
  const date = `${parts.year}-${parts.month}-${parts.day}`
  return {
    date,
    startTime: `${date}T00:00:00${TIME_ZONE_OFFSET}`,
    endTime: `${date}T${parts.hour}:${parts.minute}:${parts.second}${TIME_ZONE_OFFSET}`
  }
}

function signedHeaders({ payload, endpoint, service, action, version, regionName, secretId, secretKey }) {
  const timestamp = Math.floor(Date.now() / 1000)
  const date = new Date(timestamp * 1000).toISOString().slice(0, 10)
  const canonicalHeaders = [
    'content-type:application/json; charset=utf-8',
    `host:${endpoint}`,
    `x-tc-action:${action.toLowerCase()}`
  ].join('\n') + '\n'
  const signedHeaderNames = 'content-type;host;x-tc-action'
  const canonicalRequest = [
    'POST',
    '/',
    '',
    canonicalHeaders,
    signedHeaderNames,
    sha256Hex(payload)
  ].join('\n')

  const credentialScope = `${date}/${service}/tc3_request`
  const stringToSign = [
    'TC3-HMAC-SHA256',
    String(timestamp),
    credentialScope,
    sha256Hex(canonicalRequest)
  ].join('\n')

  const secretDate = hmac(`TC3${secretKey}`, date)
  const secretService = hmac(secretDate, service)
  const secretSigning = hmac(secretService, 'tc3_request')
  const signature = hmac(secretSigning, stringToSign, 'hex')
  const authorization = [
    `TC3-HMAC-SHA256 Credential=${secretId}/${credentialScope}`,
    `SignedHeaders=${signedHeaderNames}`,
    `Signature=${signature}`
  ].join(', ')

  return {
    Authorization: authorization,
    'Content-Type': 'application/json; charset=utf-8',
    Host: endpoint,
    'X-TC-Action': action,
    'X-TC-Version': version,
    'X-TC-Timestamp': String(timestamp),
    'X-TC-Region': regionName
  }
}

async function requestTencentCloud({ endpoint, service, action, version, regionName, payload }) {
  const { secretId, secretKey } = credentials()
  const body = JSON.stringify(payload)
  const response = await fetch(`https://${endpoint}`, {
    method: 'POST',
    headers: signedHeaders({
      payload: body,
      endpoint,
      service,
      action,
      version,
      regionName,
      secretId,
      secretKey
    }),
    body,
    signal: AbortSignal.timeout(15000)
  })
  const data = await response.json().catch(async () => ({
    Response: { Error: { Message: (await response.text()).slice(0, 500) } }
  }))
  return {
    ok: response.ok && !data?.Response?.Error,
    status: response.status,
    response: data?.Response || {},
    error: data?.Response?.Error || null,
    requestId: data?.Response?.RequestId || ''
  }
}

async function requestLighthouse(action, payload, regionName) {
  const result = await requestTencentCloud({
    endpoint: LIGHTHOUSE_ENDPOINT,
    service: LIGHTHOUSE_SERVICE,
    action,
    version: LIGHTHOUSE_VERSION,
    regionName,
    payload
  })
  if (!result.ok) {
    const err = result.error || {}
    userError(
      `轻量应用服务器 API ${action} 失败：${err.Code ? `${err.Code} : ` : ''}${
        err.Message || `HTTP ${result.status}`
      }`
    )
  }
  return result.response
}

async function requestMonitorData({
  metricName,
  period,
  startTime,
  endTime,
  instanceIdValue,
  regionName,
  dimensionName = 'InstanceId'
}) {
  const payload = {
    Namespace: LIGHTHOUSE_NAMESPACE,
    MetricName: metricName,
    Instances: [
      {
        Dimensions: [
          {
            Name: dimensionName,
            Value: instanceIdValue
          }
        ]
      }
    ],
    Period: period,
    StartTime: startTime,
    EndTime: endTime
  }

  return requestTencentCloud({
    endpoint: MONITOR_ENDPOINT,
    service: MONITOR_SERVICE,
    action: MONITOR_ACTION,
    version: MONITOR_VERSION,
    regionName,
    payload
  })
}

async function describeInstancesByFilter(regionName, name, value) {
  if (!value) {
    return null
  }
  const response = await requestLighthouse(
    'DescribeInstances',
    {
      Filters: [
        {
          Name: name,
          Values: [value]
        }
      ],
      Limit: 5
    },
    regionName
  )
  const instances = response.InstanceSet || []
  return instances[0] || null
}

async function lighthouseInstance(regionName) {
  const configured = String(process.env.TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID || '').trim()
  if (configured) {
    return {
      id: configured,
      name: '',
      source: 'runner.env'
    }
  }

  const metadataInstanceId = await metadata('instance-id').catch(() => '')
  if (metadataInstanceId.startsWith('lhins-')) {
    return {
      id: metadataInstanceId,
      name: '',
      source: 'metadata instance-id'
    }
  }

  const publicIp = await metadata('public-ipv4').catch(() => '')
  const privateIp = await metadata('local-ipv4').catch(() => '')
  const candidates = [
    ['public-ip-address', publicIp],
    ['private-ip-address', privateIp]
  ]

  for (const [filterName, filterValue] of candidates) {
    try {
      const instance = await describeInstancesByFilter(regionName, filterName, filterValue)
      if (instance?.InstanceId) {
        return {
          id: instance.InstanceId,
          name: instance.InstanceName || '',
          source: `${filterName}=${filterValue}`
        }
      }
    } catch {
      // Try the next metadata filter before failing with a targeted message below.
    }
  }

  userError(
    '未能自动识别轻量应用服务器实例 ID。请在腾讯云轻量应用服务器控制台复制实例 ID，' +
      '并在 runner.env 设置 TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=lhins-...。' +
      ` 当前 metadata instance-id=${metadataInstanceId || '-'}，public-ip=${publicIp || '-'}。`
  )
}

async function trafficPackages(instanceIdValue, regionName) {
  const response = await requestLighthouse(
    'DescribeInstancesTrafficPackages',
    {
      InstanceIds: [instanceIdValue],
      Limit: 1
    },
    regionName
  )
  const item = (response.InstanceTrafficPackageSet || []).find(
    (entry) => entry.InstanceId === instanceIdValue
  )
  const packages = item?.TrafficPackageSet || []
  return {
    totalCount: response.TotalCount || 0,
    packages,
    usedBytes: packages.reduce((total, pkg) => total + Number(pkg.TrafficUsed || 0), 0),
    totalBytes: packages.reduce((total, pkg) => total + Number(pkg.TrafficPackageTotal || 0), 0),
    remainingBytes: packages.reduce(
      (total, pkg) => total + Number(pkg.TrafficPackageRemaining || 0),
      0
    ),
    overflowBytes: packages.reduce((total, pkg) => total + Number(pkg.TrafficOverflow || 0), 0),
    startTime: packages[0]?.StartTime || '',
    endTime: packages[0]?.EndTime || '',
    status: packages[0]?.Status || ''
  }
}

async function getMonitorDataFlexible(options) {
  const candidates = ['InstanceId', 'unInstanceId']
  const attempts = []
  for (const dimensionName of candidates) {
    const data = await requestMonitorData({ ...options, dimensionName })
    attempts.push({ dimensionName, ...data })
    if (data.ok) {
      return { response: data.response, dimensionName, attempts }
    }
  }
  const firstError = attempts.find((attempt) => attempt.error)?.error
  userError(
    `腾讯云监控查询失败：${firstError?.Code ? `${firstError.Code} : ` : ''}${
      firstError?.Message || '所有维度名都查询失败'
    }`
  )
}

function formatAttempt(attempt) {
  if (attempt.ok) {
    return `${attempt.metricName}/${attempt.dimensionName}: OK`
  }
  const err = attempt.error || {}
  return `${attempt.metricName}/${attempt.dimensionName}: ${err.Code || `HTTP ${attempt.status}`} ${
    err.Message || ''
  }`.trim()
}

function values(response) {
  const dataPoints = response?.DataPoints || []
  const first = dataPoints[0] || {}
  return Array.isArray(first.Values) ? first.Values.filter((value) => typeof value === 'number') : []
}

function sum(list) {
  return list.reduce((total, value) => total + value, 0)
}

function max(list) {
  return list.length ? Math.max(...list) : 0
}

function bytesToGb(value) {
  return value / 1024 / 1024 / 1024
}

function mbToBytes(value) {
  return value * 1024 * 1024
}

function fmt(value, digits = 2) {
  if (!Number.isFinite(value)) {
    return '-'
  }
  return value.toFixed(digits)
}

function fmtBytes(value) {
  if (!Number.isFinite(value)) {
    return '-'
  }
  return `${fmt(bytesToGb(value), 3)} GB`
}

async function monitorEstimate(instanceIdValue, regionName, startTime, endTime) {
  const period = 60
  const [outTraffic, wanOut, wanIn] = await Promise.all([
    getMonitorDataFlexible({
      metricName: 'LighthouseOuttraffic',
      period,
      startTime,
      endTime,
      instanceIdValue,
      regionName
    }),
    getMonitorDataFlexible({
      metricName: 'LighthouseOuttraffic',
      period: 300,
      startTime,
      endTime,
      instanceIdValue,
      regionName
    }),
    getMonitorDataFlexible({
      metricName: 'LighthouseIntraffic',
      period: 300,
      startTime,
      endTime,
      instanceIdValue,
      regionName
    })
  ])

  const outValues = values(outTraffic.response)
  const outBytesEstimate = mbToBytes(sum(outValues) * period)

  return {
    dimensionName: outTraffic.dimensionName,
    outBytesEstimate,
    outPeakMBytesPerSecond: max(values(wanOut.response)),
    inPeakMBytesPerSecond: max(values(wanIn.response))
  }
}

export async function getTencentCloudTrafficSnapshot() {
  const regionName = await region()
  const instance = await lighthouseInstance(regionName)
  const { date, startTime, endTime } = todayRange()

  const [packages, monitor] = await Promise.all([
    trafficPackages(instance.id, regionName),
    monitorEstimate(instance.id, regionName, startTime, endTime).catch((error) => ({
      error: error.message
    }))
  ])

  const packagePercent = packages.totalBytes
    ? (packages.usedBytes / packages.totalBytes) * 100
    : Number.NaN

  const remainingPercent = packages.totalBytes
    ? (packages.remainingBytes / packages.totalBytes) * 100
    : Number.NaN

  return {
    regionName,
    instance,
    date,
    startTime,
    endTime,
    packages,
    monitor,
    packagePercent,
    remainingPercent
  }
}

export async function getTencentCloudTrafficReport() {
  const {
    regionName,
    instance,
    date,
    startTime,
    endTime,
    packages,
    monitor,
    packagePercent
  } = await getTencentCloudTrafficSnapshot()

  const lines = [
    '腾讯云轻量服务器流量',
    `实例：${instance.id}${instance.name ? ` / ${instance.name}` : ''}`,
    `地域：${regionName}`,
    `识别方式：${instance.source}`,
    `日期：${date}`,
    '',
    '流量包：',
    `本周期已用：${fmtBytes(packages.usedBytes)} / ${fmt(packagePercent, 2)}%`,
    `本周期总量：${fmtBytes(packages.totalBytes)}`,
    `本周期剩余：${fmtBytes(packages.remainingBytes)}`,
    `超额流量：${fmtBytes(packages.overflowBytes)}`,
    `周期：${packages.startTime || '-'} - ${packages.endTime || '-'}`,
    `状态：${packages.status || '-'}`,
    ''
  ]

  if (monitor.error) {
    lines.push('今日监控估算：获取失败', monitor.error)
  } else {
    lines.push(
      '今日监控估算：',
      `公网出流量：约 ${fmtBytes(monitor.outBytesEstimate)}`,
      `公网出流量峰值：${fmt(monitor.outPeakMBytesPerSecond)} MBytes/s`,
      `公网入流量峰值：${fmt(monitor.inPeakMBytesPerSecond)} MBytes/s`,
      `监控维度：${monitor.dimensionName}`,
      `范围：${startTime} - ${endTime}`,
      '说明：今日出流量按 60 秒粒度的外网每秒出流量折算，云监控该指标为 max 统计，可能偏高；流量包已用/剩余以 Lighthouse 接口为准。'
    )
  }

  return lines.join('\n')
}

export async function getTencentCloudTrafficDebugReport() {
  const regionName = await region()
  let instance = null
  let instanceError = ''
  try {
    instance = await lighthouseInstance(regionName)
  } catch (error) {
    instanceError = error.message
  }

  const { startTime, endTime } = todayRange()
  const lines = [
    '腾讯云轻量服务器调试',
    `地域：${regionName}`,
    `Lighthouse API：${LIGHTHOUSE_ENDPOINT}`,
    `监控命名空间：${LIGHTHOUSE_NAMESPACE}`,
    `范围：${startTime} - ${endTime}`,
    ''
  ]

  if (!instance) {
    lines.push('实例识别：失败', instanceError)
    return lines.join('\n')
  }

  lines.push(`实例：${instance.id}${instance.name ? ` / ${instance.name}` : ''}`, `识别方式：${instance.source}`)

  try {
    const packages = await trafficPackages(instance.id, regionName)
    lines.push(
      '',
      'DescribeInstancesTrafficPackages: OK',
      `流量包数量：${packages.packages.length}`,
      `本周期已用：${fmtBytes(packages.usedBytes)}`,
      `本周期剩余：${fmtBytes(packages.remainingBytes)}`
    )
  } catch (error) {
    lines.push('', `DescribeInstancesTrafficPackages: ${error.message}`)
  }

  const metrics = [
    { metricName: 'QemuVcpuUsage', period: 300 },
    { metricName: 'LighthouseOuttraffic', period: 60 },
    { metricName: 'LighthouseIntraffic', period: 60 },
    { metricName: 'LighthouseOutratio', period: 300 }
  ]
  const attempts = []
  for (const metric of metrics) {
    for (const dimensionName of ['InstanceId', 'unInstanceId']) {
      const result = await requestMonitorData({
        ...metric,
        startTime,
        endTime,
        instanceIdValue: instance.id,
        regionName,
        dimensionName
      })
      attempts.push({ ...result, metricName: metric.metricName, dimensionName })
    }
  }

  lines.push('', ...attempts.map(formatAttempt))
  return lines.join('\n')
}
