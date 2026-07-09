# X Feed Twikit 运维说明

## 当前架构

`astrbot_plugin_x_feed` 生产环境已经切到 `Twikit`，不再依赖 RSSHub 抓 X 时间线。

当前链路：

```text
AstrBot x_feed 插件
  -> Twikit
  -> 服务器通过 Tailscale 访问你的 Windows 本机代理
  -> http://100.74.24.101:7890
  -> X
```

图片发送链路与正文一致，AstrBot 会先下载图片到插件本地缓存，再发到 QQ。

这是一套“少量账号、低频轮询”的自用方案，不是实时推送，也不适合大量订阅。

## 为什么必须借本机代理

当前服务器本身对 X 的直连不稳定，Twikit 直连容易超时。

现在能稳定工作的原因不是服务器单独能抓，而是：

1. 你的 Windows 本机能正常访问 X。
2. 本机本地代理监听在 `7890`。
3. 服务器通过 Tailscale 访问到你的本机地址 `100.74.24.101`。
4. Twikit 在服务器侧把请求转发到 `http://100.74.24.101:7890`。

所以只要下面任意一项掉了，X 推送就会跟着失效：

- 你的电脑关机
- 本机代理没开
- Tailscale 断开
- 本机 Tailscale IP 变了但插件配置没更新

## 关键路径

本地仓库：

```text
H:\NightingaleSilenceWeb\NightingaleOpsBot\astrbot-plugin\astrbot_plugin_x_feed
```

服务器插件源码：

```text
/opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed
```

AstrBot 运行时插件目录：

```text
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed
```

AstrBot 插件配置：

```text
/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
```

运行时数据：

```text
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/x_feed.sqlite3
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/images/
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/x_cookies.json
```

注意：

- 不要覆盖 `.local/x_feed.sqlite3`
- 不要覆盖 `.local/images/`
- 不要把 `x_cookies.json`、cookie、token 打到聊天、日志、提交里

## 当前配置重点

生产环境关键项：

```json
{
  "backend": "twikit",
  "twikit_cookies_file": ".local/x_cookies.json",
  "twikit_proxy_url": "http://100.74.24.101:7890"
}
```

含义：

- `backend=twikit`：正文抓取走 Twikit
- `twikit_cookies_file`：读取本地导出的 X 登录态
- `twikit_proxy_url`：明确走你本机代理，不走服务器直连

## cookies 文件支持格式

`twikit_cookies_file` 现在支持三种格式：

1. Twikit 自己保存的 JSON dict
2. 浏览器导出的 cookie JSON 数组
3. 纯文本 cookie 头，例如：

```text
auth_token=...; ct0=...
```

最少要有 `auth_token`。没有它时，插件会直接报 cookies 不可用。

## 常用 QQ 命令

```text
/x帮助
/x推送测试 @FF_XIV_EN
/x订阅 @FF_XIV_EN
/x取消订阅 @FF_XIV_EN
/x订阅列表
/x推送关
/x推送开
/x翻译状态
```

说明：

- 私聊里订阅，推送发私聊
- 群里订阅，推送发群
- 私聊和群订阅是分开的

## 当前报错含义

### 1. cookie 失效

典型提示：

```text
X 登录态失效或 cookies 不可用，请重新导出并更新 x_cookies.json。
```

说明：

- 浏览器登录态过期
- 导出的 cookie 不完整
- `auth_token` 丢了

处理：

1. 在本机重新登录 X
2. 重新导出 cookie
3. 覆盖服务器上的 `.local/x_cookies.json`
4. 重启 AstrBot

### 2. 代理不通

典型提示：

```text
Twikit 无法通过代理连到 X。请检查代理地址 http://100.74.24.101:7890、本机 7890 端口和 Tailscale 是否在线。
```

说明：

- 你的电脑没开
- 本机代理没开
- 7890 没监听
- Tailscale 掉线
- 服务器访问不到 `100.74.24.101:7890`

处理顺序：

1. 先确认你本机现在能访问 X
2. 确认本机代理还在 `7890`
3. 确认本机 Tailscale 在线
4. 在服务器上测试是否能连通 `100.74.24.101:7890`

### 3. Twikit 版本断了

典型提示：

```text
Twikit 与当前 X 页面协议不兼容（KEY_BYTE 解析失败）。这通常不是 cookie 问题，而是 Twikit 版本断了，需要更新服务端 Twikit。
```

说明：

- 这是 X 页面协议变化导致的上游兼容问题
- 重新登录、重填 cookie 通常没用

当前生产已经从旧版 `d60/twikit` 切到维护中的 fork，原因就是这里。

## 当前已知运行依赖

服务端 AstrBot 运行时需要能 import 到可用的 `twikit` 包。

之前旧版包报过：

```text
Couldn't get KEY_BYTE indices
```

因此生产环境已经把服务端运行时 `twikit` 替换为维护中的 fork。后续如果这个错误再出现，优先考虑上游兼容问题，不要先怀疑 cookie。

## 基本排障命令

### 看 AstrBot 日志

```bash
docker logs --since 10m astrbot 2>&1 | grep -Ei 'x_feed|twikit|Traceback|ERROR|WARNING' | tail -200
```

### 看插件配置

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json')
data = json.loads(p.read_text(encoding='utf-8-sig'))
print('backend =', data.get('backend'))
print('twikit_proxy_url =', data.get('twikit_proxy_url'))
print('twikit_cookies_file =', data.get('twikit_cookies_file'))
PY
```

只看字段状态，不要把 cookie 内容打印出来。

### 重启 AstrBot

```bash
cd /opt/nightingale/astrbot && docker compose restart astrbot
```

### 语法检查

```bash
docker exec astrbot python3 -m py_compile \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/main.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/feed_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/twikit_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/storage.py
```

### QQ 侧烟雾测试

```text
/x推送测试 @FF_XIV_EN
```

## 更新 cookies 的原则

- 只在你本机已确认登录有效时更新
- 只覆盖 `x_cookies.json`
- 不要顺手删数据库
- 不要顺手清 `.local/images`

如果只是 cookie 失效，不需要重建订阅，也不需要清空 `.local/x_feed.sqlite3`。

## 部署注意事项

部署前先备份：

```bash
mkdir -p /opt/nightingale/backups
tar -czf /opt/nightingale/backups/x-feed-before-sync-$(date +%Y%m%d-%H%M%S).tgz \
  /opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed \
  /opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed 2>/dev/null || true
```

同步时只覆盖源码文件，不覆盖运行时 `.local/`。

## 残余风险

- 这套方案仍然依赖你的本机在线，不是纯服务器自治
- X 改协议后，Twikit 未来还可能再断
- cookie 登录态天生会过期
- 轮询频率拉太低风险更高，不建议压到 5 分钟以下
