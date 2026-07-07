# NS Health Runbook

本文记录 `/ns health`、每日服务器状态报告和自动告警的维护方式。

## 功能范围

Runner 模块：

- `runner/systemHealth.mjs`
- `runner/tencentCloudTraffic.mjs`
- `runner/jobs.mjs`

AstrBot 入口：

- `astrbot-plugin/astrbot_plugin_ns_ops/main.py`
- `astrbot-plugin/astrbot_plugin_ns_ops/formatters.py`
- `astrbot-plugin/astrbot_plugin_ns_ops/_conf_schema.json`

QQ 命令：

```text
/ns health
/ns daily
/ns alerts
/ns traffic bind
/ns traffic status
```

说明：

- `/ns health`：即时综合健康检查。
- `/ns daily`：手动生成每日状态报告。
- `/ns alerts`：只返回当前是否有需要主动推送的告警；正常时为 `OK`。
- `/ns traffic bind`：绑定私聊接收窗口。每日状态和自动告警都复用这个绑定，不会推到群里。

## 检查项

当前健康检查覆盖：

- 腾讯云轻量应用服务器流量包和今日公网流量估算。
- 磁盘剩余空间。
- 当前内存、今日内存峰值。
- 当前负载、今日 CPU 峰值。
- Docker 容器状态。
- systemd 服务状态。
- HTTP 站点可用性。
- HTTPS 证书到期天数。
- 最近 24 小时异常日志。

默认 Docker 容器：

```text
astrbot, napcat, gsuid_core, rsshub
```

默认 systemd 服务：

```text
nightingale-ops-runner, nsglamour, nightingale-xproxy-relay, docker
```

默认 HTTP 检查：

```text
AstrBot=http://127.0.0.1:6185/
NapCat=http://127.0.0.1:6099/
GsCore=http://127.0.0.1:8764/
NSGlamour=http://127.0.0.1:8765/
```

HTTP 状态码 `< 500` 视为服务可达；例如 `301`、`302`、`404` 不视为服务挂掉。`502`、`503`、连接失败和超时会触发告警。

## 自动告警

AstrBot 插件启动后会循环执行：

1. 到点后推送一次 `/ns daily` 等价的每日状态。
2. 每隔 `health_alert_interval_seconds` 秒执行一次 `system/alerts`。

默认配置：

```json
{
  "traffic_report_enabled": true,
  "traffic_report_hour": 12,
  "traffic_report_minute": 0,
  "health_alert_enabled": true,
  "health_alert_interval_seconds": 300
}
```

自动告警复用 `/ns traffic bind` 绑定的私聊窗口。同一批异常只推一次；恢复正常时会推一条恢复通知。

默认会触发主动告警的情况：

- Docker 容器没有运行，或 healthcheck 非 healthy。
- systemd 服务不是 active。
- HTTP 检查返回 5xx、连接失败或超时。
- 磁盘使用率超过阈值。
- 内存使用率超过阈值。
- 今日 CPU 峰值超过阈值。
- 腾讯云流量包剩余比例低于阈值。
- 证书剩余天数低于阈值。

最近 24 小时异常日志默认只展示在 `/ns health` 和 `/ns daily`，不主动告警。需要把日志也纳入主动告警时，设置：

```bash
NS_HEALTH_ERROR_LOG_ALERT=true
```

## Runner 环境变量

可选配置写在：

```bash
/opt/nightingale/NightingaleOpsBot/.local/runner.env
```

可配置项：

```bash
NS_HEALTH_DOCKER_CONTAINERS=astrbot,napcat,gsuid_core,rsshub
NS_HEALTH_SYSTEMD_SERVICES=nightingale-ops-runner,nsglamour,nightingale-xproxy-relay,docker
NS_HEALTH_HTTP_URLS=AstrBot=http://127.0.0.1:6185/,NapCat=http://127.0.0.1:6099/,GsCore=http://127.0.0.1:8764/,NSGlamour=http://127.0.0.1:8765/
NS_HEALTH_TLS_HOSTS=example.com,www.example.com
NS_HEALTH_DISK_PATHS=/
NS_HEALTH_DISK_WARN_PERCENT=85
NS_HEALTH_MEMORY_WARN_PERCENT=90
NS_HEALTH_CPU_WARN_PERCENT=95
NS_HEALTH_TRAFFIC_REMAINING_WARN_PERCENT=20
NS_HEALTH_CERT_WARN_DAYS=14
NS_HEALTH_ERROR_LOG_ALERT=false
```

如果没有配置 `NS_HEALTH_TLS_HOSTS`，且 HTTP 检查里没有 HTTPS URL，则证书检查显示“未配置 HTTPS 检查”。

## Docker 权限

`nightingale-ops-runner` 以 `nightingale` 用户运行。为了检查 Docker 容器状态和执行已有的容器运维任务，systemd 服务增加了：

```ini
[Service]
SupplementaryGroups=docker
```

文件位置：

```bash
/etc/systemd/system/nightingale-ops-runner.service.d/10-docker-group.conf
```

修改后需要：

```bash
systemctl daemon-reload
systemctl restart nightingale-ops-runner
```

注意：Docker socket 权限等价于较高的宿主机操作能力。继续保持 runner 只监听内网地址，并确保 `/ns` 插件只允许管理员调用。

## 验证

本地语法检查：

```powershell
Set-Location H:\NightingaleSilenceWeb\NightingaleOpsBot
npm run check
python -m py_compile astrbot-plugin\astrbot_plugin_ns_ops\main.py astrbot-plugin\astrbot_plugin_ns_ops\formatters.py
```

服务器语法检查：

```bash
cd /opt/nightingale/NightingaleOpsBot
npm run check
docker exec astrbot python3 -m py_compile /AstrBot/data/plugins/astrbot_plugin_ns_ops/main.py /AstrBot/data/plugins/astrbot_plugin_ns_ops/formatters.py
```

Runner 直接验证：

```bash
cd /opt/nightingale/NightingaleOpsBot
set -a
. ./.local/runner.env
set +a

curl -sS \
  -H "Authorization: Bearer ${NS_OPS_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST "http://${NS_OPS_HOST:-127.0.0.1}:${NS_OPS_PORT:-18766}/jobs/system/health" \
  -d '{}'

curl -sS \
  -H "Authorization: Bearer ${NS_OPS_TOKEN}" \
  -H "Content-Type: application/json" \
  -X POST "http://${NS_OPS_HOST:-127.0.0.1}:${NS_OPS_PORT:-18766}/jobs/system/alerts" \
  -d '{}'
```

正常情况下 `system/alerts` 返回：

```text
OK
```

QQ 侧验证：

```text
/ns health
/ns daily
/ns alerts
```

## 常见问题

### 容器全部显示 missing

通常是 runner 进程没有 Docker socket 权限。

检查：

```bash
id nightingale
ls -l /var/run/docker.sock
systemctl show nightingale-ops-runner -p SupplementaryGroups --no-pager
```

处理：

```bash
mkdir -p /etc/systemd/system/nightingale-ops-runner.service.d
cat > /etc/systemd/system/nightingale-ops-runner.service.d/10-docker-group.conf <<'CONF'
[Service]
SupplementaryGroups=docker
CONF
systemctl daemon-reload
systemctl restart nightingale-ops-runner
```

### 最近异常日志导致状态看起来不干净

`/ns health` 会展示最近 24 小时错误日志，包括重启期间 NapCat 短暂断开 AstrBot 的记录。这类日志默认不触发主动告警。

如果要清查当前是否真的需要告警，看：

```text
/ns alerts
```

返回 `OK` 就表示当前没有主动告警项。
