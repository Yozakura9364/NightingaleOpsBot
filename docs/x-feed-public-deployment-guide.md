# AstrBot X 用户动态推送插件部署指南

> 本文是可公开分享的脱敏版本。所有服务器地址、目录、账号、代理地址和 X 用户名均使用
> 占位符。部署时请替换为自己的环境，不要把 Cookie、Token、QQ 号或完整生产配置发布到
> GitHub、聊天记录和日志中。

## 1. 功能概述

`astrbot_plugin_x_feed` 是一个面向少量 X 账号的低频轮询推送插件。

它可以：

- 在 QQ 私聊或群聊中订阅指定 X 用户。
- 定期读取用户时间线并判断是否出现新动态。
- 推送动态正文、发布时间和原始链接。
- 下载并发送动态中的图片。
- 可选调用 AstrBot LLM Provider 翻译正文。
- 将订阅和 last-seen 状态保存在本地 SQLite。

它不提供：

- X 官方实时流或 Webhook。
- 秒级更新保证。
- 视频文件下载和转发。
- 大规模账号监控。
- 永久有效的 X 登录态。

推荐用途是个人 Bot、少量账号、每 10 分钟或更低频率的更新提醒。

## 2. 工作原理

推荐架构：

```text
QQ 私聊或群聊
  -> AstrBot
  -> astrbot_plugin_x_feed
  -> Twikit
  -> 可选 HTTP 代理
  -> X
  -> 文本、链接和图片 URL
  -> 可选 LLM 翻译
  -> QQ 文本消息
  -> 下载图片到插件缓存
  -> QQ 图片消息
```

插件不是持续保持长连接，而是按配置周期轮询：

1. 读取所有已启用订阅。
2. 获取每个账号最近若干条动态。
3. 用该订阅保存的 `last_seen_id` 或 `last_seen_link` 判断新内容。
4. 按时间顺序发送本轮允许的动态。
5. 更新 SQLite 中的 last-seen 状态。

同一个 X 账号在不同群或私聊中订阅时，会作为多条独立订阅保存。

## 3. 部署前提

需要准备：

- 一台可运行 Docker 的 Linux 服务器。
- 已完成部署的 AstrBot。
- 已连接 QQ 平台的 AstrBot 适配器，例如 OneBot/NapCat。
- 一个用于只读抓取的 X 账号登录态。
- 服务器可以访问 X，或者可以访问一台能代理 X 流量的设备。
- Python 3 和可用的 `pip`。
- 插件源码目录 `astrbot_plugin_x_feed`。

建议使用专门的低价值只读 X 账号，不要使用重要主账号。Cookie 抓取存在登录态失效、
风控和账号验证风险。

## 4. 目录约定

下文使用以下占位符：

| 占位符 | 含义 | 示例形式 |
| --- | --- | --- |
| `<SERVER_USER>` | Linux SSH 用户 | `root` 或普通运维用户 |
| `<SERVER_HOST>` | 服务器域名或 IP | `bot.example.com` |
| `<ASTRBOT_ROOT>` | AstrBot Compose 目录 | `/srv/astrbot` |
| `<ASTRBOT_CONTAINER>` | AstrBot 容器名 | `astrbot` |
| `<PLUGIN_RUNTIME>` | AstrBot 运行时插件目录 | `/srv/astrbot/data/plugins/astrbot_plugin_x_feed` |
| `<PLUGIN_CONFIG>` | 插件配置文件 | `/srv/astrbot/data/config/astrbot_plugin_x_feed_config.json` |
| `<PROXY_URL>` | 可选代理地址 | `http://<proxy-host>:<port>` |
| `<X_HANDLE>` | 要订阅的 X 用户名 | 不含隐私信息的测试账号 |

如果 AstrBot 使用官方默认挂载方式，容器内插件路径通常类似：

```text
/AstrBot/data/plugins/astrbot_plugin_x_feed
```

请先检查自己的 `docker-compose.yml`，确认宿主机目录与容器目录的映射关系。

## 5. 获取插件源码

将完整插件目录放入 AstrBot 的运行时插件目录：

```text
astrbot_plugin_x_feed/
├── __init__.py
├── main.py
├── twikit_client.py
├── feed_client.py
├── storage.py
├── _conf_schema.json
├── requirements.txt
└── metadata.yaml
```

不要只复制 `main.py`。Twikit 客户端、RSS 兼容解析、SQLite 存储和配置 schema 都是运行
所需文件。

通过 SCP 上传的示例：

```bash
scp -r astrbot_plugin_x_feed \
  <SERVER_USER>@<SERVER_HOST>:<ASTRBOT_ROOT>/data/plugins/
```

如果是从 Git 仓库部署，建议在服务器保留一份维护用源码，再把经过验证的文件同步到
AstrBot 运行时目录。不要让部署命令覆盖插件的 `.local/`。

## 6. 安装 Twikit 依赖

插件的 `requirements.txt` 声明 Twikit 依赖。进入 AstrBot 容器安装：

```bash
docker exec -it <ASTRBOT_CONTAINER> sh
python3 -m pip install -r \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/requirements.txt
exit
```

检查安装结果：

```bash
docker exec <ASTRBOT_CONTAINER> python3 -m pip show twikit
docker exec <ASTRBOT_CONTAINER> python3 -c \
  'import twikit; print(twikit.__file__)'
```

如果 requirements 使用 Git URL，容器中还需要 `git`，并且服务器需要能访问对应 Git
托管服务。

生产部署建议把 Twikit 固定到经过验证的 tag 或 commit。直接跟随仓库默认分支会导致不同
时间重新安装得到不同代码，不利于复现和回滚。

## 7. 准备 X Cookie

### 7.1 获取方式

1. 在浏览器中登录用于抓取的 X 账号。
2. 使用浏览器开发者工具或可信 Cookie 导出工具导出 X 域 Cookie。
3. 在本机检查导出内容，不要通过群聊或公开网盘传输。
4. 将 Cookie 文件安全上传到插件 `.local/` 目录。

插件支持三种格式：

### JSON object

```json
{
  "auth_token": "<REDACTED>",
  "ct0": "<REDACTED>"
}
```

### 浏览器导出的 JSON array

```json
[
  {
    "name": "auth_token",
    "value": "<REDACTED>"
  },
  {
    "name": "ct0",
    "value": "<REDACTED>"
  }
]
```

### Cookie header 文本

```text
auth_token=<REDACTED>; ct0=<REDACTED>
```

代码会硬性检查 `auth_token`。实际使用建议导出完整 X 域 Cookie，不要只保留一个字段。

### 7.2 放置位置

```bash
mkdir -p <PLUGIN_RUNTIME>/.local
chmod 700 <PLUGIN_RUNTIME>/.local
```

将文件上传为：

```text
<PLUGIN_RUNTIME>/.local/x_cookies.json
```

然后限制权限：

```bash
chmod 600 <PLUGIN_RUNTIME>/.local/x_cookies.json
```

不要执行以下操作：

- 不要把 Cookie 提交进 Git。
- 不要把 Cookie 写进 `docker-compose.yml`。
- 不要在日志中打印完整 Cookie。
- 不要在求助时直接粘贴 Cookie 内容。
- 不要把 `.local/` 作为源码目录整体同步。

## 8. 配置网络访问

### 8.1 服务器可以直接访问 X

如果服务器可以稳定访问 X，可以将代理字段留空：

```json
{
  "twikit_proxy_url": "",
  "image_proxy_url": ""
}
```

正文和图片都需要可访问 X 相关域名。只验证 `x.com` 首页不够，还应确认 API 请求和
`pbs.twimg.com` 图片域名可用。

### 8.2 使用同一网络中的代理

如果服务器不能直接访问 X，可通过 Tailscale、WireGuard 或可信局域网连接到另一台代理
设备：

```text
AstrBot 容器
  -> http://<proxy-device-address>:<proxy-port>
  -> 代理设备
  -> X
```

配置：

```json
{
  "twikit_proxy_url": "<PROXY_URL>",
  "image_proxy_url": "<PROXY_URL>"
}
```

必须同时配置正文和图片代理。常见故障是正文已经走新代理，但图片仍指向旧代理，导致
只发文字不发图片。

代理设备需要：

- 允许来自 VPN/局域网接口的入站连接。
- 防火墙放行代理端口。
- 代理程序不要只绑定 `127.0.0.1`。
- 保持设备、代理程序和组网客户端在线。

不要把无认证的代理端口直接暴露到公网。优先使用 Tailscale/WireGuard 等私有组网。

### 8.3 连通性检查

在服务器上测试代理：

```bash
curl -x <PROXY_URL> -I --max-time 15 https://x.com/
```

再测试图片域名：

```bash
curl -x <PROXY_URL> -I --max-time 15 https://pbs.twimg.com/
```

HTTP 状态不一定是 200，但不应出现连接超时、拒绝连接、无路由或代理握手失败。

## 9. 配置插件

可以通过 AstrBot 管理界面填写插件配置，也可以安全地编辑插件配置文件。

推荐起始配置：

```json
{
  "backend": "twikit",
  "rsshub_base_url": "http://rsshub:1200",
  "twikit_cookies_file": ".local/x_cookies.json",
  "twikit_locale": "en-US",
  "twikit_proxy_url": "<PROXY_URL>",
  "twikit_timeline_count": 5,
  "enabled": true,
  "poll_interval_minutes": 10,
  "max_items_per_poll": 3,
  "initial_backfill_items": 1,
  "include_images": true,
  "max_images_per_post": 4,
  "image_proxy_url": "<PROXY_URL>",
  "image_download_timeout_seconds": 20,
  "max_image_bytes": 8000000,
  "admin_user_ids": "",
  "failure_notice_threshold": 6,
  "translate_enabled": false,
  "translate_target_lang": "简体中文",
  "translate_provider_id": "",
  "translate_show_original": true,
  "translate_prompt": "",
  "translate_timeout_seconds": 45,
  "max_output_chars": 1800
}
```

如果不使用代理，将两个 `<PROXY_URL>` 替换为空字符串。

### 关键字段

| 字段 | 建议 | 说明 |
| --- | --- | --- |
| `backend` | `twikit` | 推荐后端；`rsshub` 是兼容路线 |
| `twikit_timeline_count` | `5-10` | 太小可能漏过高频账号动态，太大增加请求量 |
| `poll_interval_minutes` | `10` 或更高 | 代码强制不低于 5 分钟，不建议高频抓取 |
| `max_items_per_poll` | `1-3` | 防止断线恢复后刷屏 |
| `initial_backfill_items` | `0-1` | 新订阅时显示当前最新，不是历史补发 |
| `max_images_per_post` | 默认 `4`，可设 `0-4` | 图片越多，下载和 QQ 发送压力越大 |
| `admin_user_ids` | 建议设置 | 公开群 Bot 应限制订阅管理权限 |
| `translate_enabled` | 按需 | 会消耗 LLM 配额并增加轮询耗时 |

`admin_user_ids` 可以用逗号或换行分隔多个管理员 QQ 号。公开分享配置示例时必须移除真实
QQ 号。

## 10. 启动和验证

### 10.1 Python 语法检查

```bash
docker exec <ASTRBOT_CONTAINER> python3 -m py_compile \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/main.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/twikit_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/feed_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/storage.py
```

### 10.2 重启 AstrBot

```bash
cd <ASTRBOT_ROOT>
docker compose restart <ASTRBOT_CONTAINER>
```

如果 Compose service 名和容器名不同，应使用 Compose service 名。

### 10.3 检查日志

```bash
docker logs --since 2m <ASTRBOT_CONTAINER> 2>&1 \
  | grep -Ei 'x_feed|twikit|Traceback|ERROR|WARNING' \
  | tail -200
```

正常情况下应看到插件加载和轮询任务启动，不应出现依赖导入错误、Cookie 缺失或代理连接
错误。

### 10.4 QQ 侧测试

建议先在 Bot 私聊中测试：

```text
/x帮助
/x翻译状态
/x推送测试 @<X_HANDLE>
```

预期结果：

1. 返回最新动态正文。
2. 包含动态发布时间和原始链接。
3. 开启翻译时显示译文或原文加译文。
4. 动态包含图片时，随后发送图片消息。

`/x推送测试` 会真实请求 X 并发送 QQ 消息。不要在群聊中连续执行。

## 11. 使用命令

### 订阅当前会话

```text
/x订阅 @<X_HANDLE>
```

在私聊执行，推送到该私聊；在群聊执行，推送到该群。

### 取消当前会话订阅

```text
/x取消订阅 @<X_HANDLE>
```

### 查看当前会话订阅

```text
/x订阅列表
```

### 暂停和恢复当前会话

```text
/x推送关
/x推送开
```

### 查看翻译状态

```text
/x翻译状态
```

私聊和群聊订阅互相独立。如果需要把私聊订阅迁移到群聊，应在目标群重新订阅，再取消
私聊订阅。

## 12. 运行数据

插件会在运行时创建：

```text
<PLUGIN_RUNTIME>/.local/
├── x_feed.sqlite3
├── x_cookies.json
└── images/
```

### `x_feed.sqlite3`

保存：

- X handle。
- QQ 会话来源。
- 私聊或群聊类型。
- 是否启用。
- 最近一次动态 ID 和链接。
- 最近成功时间。
- 连续失败次数和最后错误。

不要用空数据库覆盖生产文件。删除数据库会丢失全部订阅和 last-seen 状态，并可能导致
重新订阅或错误补发。

### `images/`

保存已下载的 X 图片缓存。当前插件不会自动清理该目录，应定期监控磁盘空间。

删除旧图片缓存不会删除订阅，但相同图片再次需要发送时会重新下载。清理时不要误删
SQLite 和 Cookie。

## 13. 备份和升级

### 13.1 部署前备份

```bash
mkdir -p <ASTRBOT_ROOT>/backups
tar -czf <ASTRBOT_ROOT>/backups/x-feed-before-sync-$(date +%Y%m%d-%H%M%S).tgz \
  <PLUGIN_RUNTIME> \
  <PLUGIN_CONFIG>
```

备份中包含 Cookie 时必须限制备份目录权限，不要上传到公开对象存储。

### 13.2 只同步源码

更新插件时，覆盖这些源码文件即可：

```text
main.py
twikit_client.py
feed_client.py
storage.py
_conf_schema.json
requirements.txt
metadata.yaml
```

保留：

```text
.local/x_feed.sqlite3
.local/x_cookies.json
.local/images/
```

### 13.3 更新 Twikit

只有遇到明确的 X 协议兼容问题时才优先升级 Twikit。升级前记录：

- 当前包版本。
- 当前 requirements 来源。
- 当前 Git commit 或 tag。
- 需要解决的具体错误。

升级后重新执行语法检查、Cookie 测试、正文测试和图片测试。

## 14. 常见故障

### 14.1 Cookie 文件不存在或缺少 `auth_token`

表现：

```text
Twikit cookies 文件不存在
Twikit cookies 文件为空或格式无法识别
Twikit cookies 缺少 auth_token
```

处理：

1. 检查文件路径和权限。
2. 重新导出完整 X Cookie。
3. 只替换 `x_cookies.json`。
4. 重启 AstrBot 或等待插件检测到文件修改。

不要删除 SQLite。

### 14.2 登录态失效

表现：

```text
X 登录态失效或 cookies 不可用
```

可能伴随 401、403、CSRF 或登录相关错误。重新登录 X 并导出 Cookie。

### 14.3 代理连接失败

表现可能包括：

```text
ConnectTimeout
ReadTimeout
Connection refused
Network is unreachable
No route to host
RemoteProtocolError
```

检查：

- 代理设备是否在线。
- 代理端口是否监听。
- 防火墙和私有组网是否允许连接。
- 正文和图片代理是否一致。
- 服务器和容器是否都能访问代理地址。

### 14.4 `KEY_BYTE` 解析失败

表现：

```text
Couldn't get KEY_BYTE indices
```

这是 Twikit 与当前 X 页面协议不兼容，通常不是 Cookie 问题。检查 Twikit 上游更新，并在
备份后升级到已验证版本。

### 14.5 有正文但没有图片

检查：

- `include_images` 是否开启。
- `max_images_per_post` 是否大于 0。
- `image_proxy_url` 是否可用。
- `pbs.twimg.com` 是否可访问。
- 图片是否超过大小上限。
- AstrBot/NapCat 是否能发送本地文件图片。

日志：

```bash
docker logs --since 10m <ASTRBOT_CONTAINER> 2>&1 \
  | grep -Ei 'X feed image|pbs.twimg.com|image|Traceback|ERROR|WARNING' \
  | tail -200
```

### 14.6 翻译没有出现

检查：

- `/x翻译状态` 是否显示开启。
- AstrBot 是否配置了 LLM Provider。
- 指定 Provider ID 是否存在。
- Provider 是否超时或额度不足。

翻译失败会回退原文，不影响 X 抓取和图片发送。

### 14.7 推送延迟

这是轮询架构的正常边界：

- 插件启动后会先等待约 20 秒。
- 默认每 10 分钟轮询。
- 所有订阅串行处理。
- 慢代理、翻译和图片会拖长一轮耗时。

不要为了秒级推送盲目降低轮询间隔。过高请求频率会增加账号风控和上游限流风险。

### 14.8 少发或漏发

如果账号在两轮之间发布的动态数量超过 `twikit_timeline_count`，旧游标可能离开抓取窗口。
插件只会处理当前窗口，并受 `max_items_per_poll` 限制，因此较旧动态可能被跳过。

对高频账号应适度增加时间线条数，而不是只降低轮询间隔。

## 15. 安全建议

- 使用专用低价值 X 账号。
- Cookie 文件权限设为 `600`。
- `.local/` 加入 `.gitignore`。
- 代理只通过私有组网访问，不暴露公网。
- 给订阅管理命令设置管理员 QQ 白名单。
- 日志中只记录错误类别，不打印 Cookie 和请求头。
- 公开求助时使用 `<SERVER_HOST>`、`<PROXY_URL>`、`<X_HANDLE>` 等占位符。
- 备份文件如果包含 Cookie，应加密或放在权限受控目录。
- 定期检查 X 登录活动，发现异常立即撤销会话并更换 Cookie。

## 16. 已知限制

- 同一个 X handle 被多个 QQ 会话订阅时会重复抓取。
- SQLite 中的 `seen_items` 目前只用于记录，不参与真正判重。
- 抓取窗口不足时可能跳过旧动态。
- 所有订阅和翻译串行执行。
- 图片缓存没有自动清理。
- 只处理 Twikit 返回的图片 URL，不保证引用媒体、视频和 GIF 完整。
- X 页面协议变化可能导致 Twikit 突然失效。
- Cookie 登录态会过期，也可能触发账号验证。

这些限制决定了该插件适合少量账号的个人用途，不适合公共大规模推送平台。

## 17. 发布前脱敏检查

把本指南、配置截图或故障日志发给别人前，检查以下内容：

- [ ] 没有服务器公网 IP、域名和 SSH 用户名。
- [ ] 没有 Tailscale/WireGuard 私网地址。
- [ ] 没有本机用户名、盘符和绝对路径。
- [ ] 没有 QQ 号、群号和 AstrBot `target_origin`。
- [ ] 没有 X Cookie、`auth_token`、`ct0` 和请求头。
- [ ] 没有 LLM Provider ID、API Key 和模型平台密钥。
- [ ] 没有完整生产配置文件。
- [ ] 没有数据库、Cookie 文件和图片缓存附件。
- [ ] 示例账号已替换为 `<X_HANDLE>`。
- [ ] 代理地址已替换为 `<PROXY_URL>`。

完成这些检查后，本文可以作为公开部署说明分享。
