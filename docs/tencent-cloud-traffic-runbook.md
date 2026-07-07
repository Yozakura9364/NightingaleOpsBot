# Tencent Cloud Lighthouse Traffic Runbook

本文记录 NS 运维插件的腾讯云流量日报维护方式。当前生产服务器是腾讯云轻量应用服务器 Lighthouse，不是 CVM。

## 功能范围

- QQ 命令入口在 `astrbot-plugin/astrbot_plugin_ns_ops/`。
- Runner 实现在 `runner/tencentCloudTraffic.mjs`。
- Runner job 注册在 `runner/jobs.mjs`：
  - `cloud.tencent.traffic.today`
  - `cloud.tencent.traffic.debug`
- QQ 命令：
  - `/ns traffic today`
  - `/ns traffic debug`
  - `/ns traffic bind`
  - `/ns traffic status`
  - `/ns traffic unbind`
- 日报由 AstrBot 插件内的循环任务发送，默认每天 `12:00`，需要先私聊执行 `/ns traffic bind` 绑定接收窗口。

## 数据源

轻量应用服务器不能按 CVM 查询。不要使用 `QCE/CVM`、`WanOuttraffic`、`AccOuttraffic` 或底层 `ins-...` 实例 ID 来查日报。

当前实现使用两类接口：

- Lighthouse API：
  - endpoint: `lighthouse.tencentcloudapi.com`
  - version: `2020-03-24`
  - action: `DescribeInstancesTrafficPackages`
  - 用途：读取流量包本周期已用、总量、剩余、超额流量。
- Cloud Monitor API：
  - endpoint: `monitor.tencentcloudapi.com`
  - version: `2018-07-24`
  - namespace: `QCE/LIGHTHOUSE`
  - metrics: `LighthouseOuttraffic`、`LighthouseIntraffic`、`QemuVcpuUsage`、`LighthouseOutratio`
  - dimension: `InstanceId`
  - 用途：读取今日公网出流量估算和出入流量峰值。

流量包已用和剩余以 Lighthouse API 为准。今日出流量是用云监控每秒出流量按时间粒度折算的估算值，云监控指标是 max 统计，可能偏高。

## Runner 环境变量

服务器配置文件：

```bash
/opt/nightingale/NightingaleOpsBot/.local/runner.env
```

需要配置：

```bash
TENCENTCLOUD_SECRET_ID=...
TENCENTCLOUD_SECRET_KEY=...
TENCENTCLOUD_REGION=ap-shanghai
TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=lhins-...
```

说明：

- `TENCENTCLOUD_SECRET_ID` 和 `TENCENTCLOUD_SECRET_KEY` 不要打印、截图或提交。
- `TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID` 必须是轻量应用服务器实例 ID，通常以 `lhins-` 开头。
- 轻量服务器 metadata 可能只能返回底层 CVM ID，例如 `ins-...`，这不能用于 Lighthouse 流量查询。
- 当前生产值已经写在服务器 `runner.env` 中；维护文档不记录完整凭据。

修改实例 ID 时用：

```bash
ENV_FILE=/opt/nightingale/NightingaleOpsBot/.local/runner.env
cp "$ENV_FILE" "$ENV_FILE.bak.$(date +%Y%m%d-%H%M%S)"

if grep -q '^TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=' "$ENV_FILE"; then
  sed -i 's/^TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=.*/TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=lhins-.../' "$ENV_FILE"
else
  printf '\nTENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=%s\n' 'lhins-...' >> "$ENV_FILE"
fi

chown nightingale:nightingale "$ENV_FILE"
systemctl restart nightingale-ops-runner
```

## CAM 权限

子用户至少需要能读轻量应用服务器和云监控：

- 云监控只读，例如 `QcloudMonitorReadOnlyAccess`。
- 轻量应用服务器只读，例如 `QcloudLighthouseReadOnlyAccess`。

只给 CVM 只读不够。CVM 只读权限不能读取 Lighthouse 流量包。

## 验证流程

本地代码语法检查：

```powershell
Set-Location H:\NightingaleSilenceWeb\NightingaleOpsBot
npm run check
python -m py_compile astrbot-plugin\astrbot_plugin_ns_ops\main.py
```

服务器语法检查：

```bash
cd /opt/nightingale/NightingaleOpsBot
npm run check
```

重启 runner：

```bash
systemctl restart nightingale-ops-runner
systemctl show nightingale-ops-runner -p ActiveState -p MainPID -p ExecMainStartTimestamp --no-pager
```

QQ 侧验证：

```text
/ns traffic debug
/ns traffic today
/ns traffic status
```

直接调用 runner 验证时，不要把 token 写进命令行日志。推荐在服务器上 source `runner.env`：

```bash
cd /opt/nightingale/NightingaleOpsBot
set -a
. ./.local/runner.env
set +a

curl -sS \
  -H "Authorization: Bearer ${NS_OPS_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST "http://${NS_OPS_HOST:-127.0.0.1}:${NS_OPS_PORT:-18766}/jobs/cloud/tencent/traffic/debug" \
  -d '{}'
```

正常 debug 结果应包含：

```text
DescribeInstancesTrafficPackages: OK
QemuVcpuUsage/InstanceId: OK
LighthouseOuttraffic/InstanceId: OK
LighthouseIntraffic/InstanceId: OK
```

`unInstanceId` 维度失败可以忽略。它只是兼容探测；正式查询会使用成功的 `InstanceId`。

## 常见问题

### InvalidParameterValue: unauthorized operation or the instance has been destroyed

常见原因：

- 还在用 CVM 逻辑查询轻量服务器。
- 用了 metadata 返回的底层 `ins-...`，而不是 `lhins-...`。
- 子用户缺少 Lighthouse 只读权限。

处理：

1. 确认 `runner/tencentCloudTraffic.mjs` 使用 `QCE/LIGHTHOUSE`。
2. 确认 `runner.env` 里有 `TENCENTCLOUD_LIGHTHOUSE_INSTANCE_ID=lhins-...`。
3. 给 CAM 子用户补轻量应用服务器只读权限。
4. 重启 `nightingale-ops-runner`。
5. 运行 `/ns traffic debug`。

### debug 仍显示 CVM 指标

如果输出里还有 `WanOuttraffic`、`WanIntraffic`、`AccOuttraffic`，说明 runner 进程仍加载旧代码。

处理：

```bash
grep -nE 'LIGHTHOUSE|WanOuttraffic|DescribeInstancesTrafficPackages' \
  /opt/nightingale/NightingaleOpsBot/runner/tencentCloudTraffic.mjs

systemctl restart nightingale-ops-runner
systemctl show nightingale-ops-runner -p ActiveState -p MainPID -p ExecMainStartTimestamp --no-pager
```

### PowerShell 调 SSH 命令被拆坏

Windows PowerShell 里复杂的 SSH 内联命令容易被引号、`$()`、管道和 UTF-8 BOM 影响。涉及 token、heredoc、正则或多行脚本时，优先写临时 `.sh` 文件，用 UTF-8 no BOM 上传后执行。

### 日报没有推送

检查：

- AstrBot 插件配置 `traffic_report_enabled` 是否为 `true`。
- 私聊是否执行过 `/ns traffic bind`。
- `/ns traffic status` 里的绑定状态和上次推送日期。
- Runner 是否在线：`/ns ping`。
- Runner 今日报告是否能返回：`/ns traffic today`。
