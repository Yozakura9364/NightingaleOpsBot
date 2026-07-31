# X Feed 完整实现与运维交接文档

> 本文是 `astrbot_plugin_x_feed` 的当前事实来源，供后续对话、维护和迁移使用。
> 代码、配置和生产状态发生变化时，应优先更新本文，再更新简短入口文档。

最后核对：2026-07-23

## 1. 先看结论

当前生产方案是：

```text
QQ 私聊/群聊中的 /x订阅
  -> AstrBot 保存当前会话订阅
  -> 后台低频轮询
  -> Twikit 读取 X 登录态
  -> 服务器通过 Tailscale 访问 Windows 本机代理 7890
  -> X GraphQL/Web 请求
  -> Twikit 返回账号时间线
  -> 插件解析文本、链接、发布时间和图片 URL
  -> 可选调用 AstrBot LLM 翻译
  -> QQ 文本消息
  -> 代理下载 X 图片到本地缓存
  -> QQ 本地图片消息
```

当前生产重点：

- 后端是 `twikit`，不是 RSSHub。
- 正文代理和图片代理都直接使用 Tailscale 地址 `http://100.74.24.101:7890`。
- 这不是付费 X API，也不是实时推送。
- 方案适合少量账号、低频轮询、自用。
- Windows 电脑、本机代理和 Tailscale 任意一个离线，生产推送都会受到影响。
- 默认每 10 分钟轮询一次；插件启动后第一次轮询还会先等待 20 秒。
- 当前配置最多每个账号每轮发送 3 条动态，每条最多发送 4 张图片。
- 生产翻译基线是开启、目标语言简体中文、保留原文；翻译失败回退原文。

最重要的复现原则：

1. 不要把 X API、RSSHub、Nitter 和 Twikit 混写成一条链路。
2. 不要把 cookie 放进 Git、聊天、日志或文档。
3. 不要覆盖插件 `.local/` 运行数据。
4. 不要只改源码，生产 AstrBot 实际加载的是运行时插件目录。
5. 改完代理、cookie、Python 代码或依赖后，都要重启 AstrBot 并做低频验证。

## 2. 项目边界与文件地图

本机仓库：

```text
H:\NightingaleSilenceWeb\NightingaleOpsBot
```

生产服务器：

```text
/opt/nightingale
```

X Feed 本地源码：

```text
astrbot-plugin/astrbot_plugin_x_feed/
├── main.py
├── twikit_client.py
├── feed_client.py
├── storage.py
├── _conf_schema.json
├── requirements.txt
└── metadata.yaml
```

源码职责：

| 文件 | 职责 |
| --- | --- |
| `main.py` | AstrBot 插件入口、配置读取、QQ 命令、后台轮询、判新、翻译、文本/图片发送 |
| `twikit_client.py` | Twikit 客户端初始化、cookie 读取与持久化、账号时间线请求、Tweet 转换、错误分类 |
| `feed_client.py` | `FeedItem` 数据结构、RSSHub 兼容后端、RSS/XML/Atom 解析、handle 规范化 |
| `storage.py` | SQLite 建表、订阅 CRUD、会话开关、last-seen 状态、失败状态和 seen 记录 |
| `_conf_schema.json` | AstrBot 配置页面的字段、类型、默认值和提示 |
| `requirements.txt` | Twikit Git 依赖；当前使用维护中的 `unclecode/twikit` 仓库来源 |
| `metadata.yaml` | AstrBot 插件名称、版本、作者和仓库信息 |

相关文档和脚本：

```text
docs/x-feed-twikit-runbook.md       当前文档
docs/x-feed-plugin-plan.md          RSSHub 时代的历史方案和迁移记录
scripts/start-xproxy-tunnel.ps1    旧的反向 SSH 隧道启动脚本
scripts/status-xproxy-tunnel.ps1   旧的反向 SSH 隧道状态脚本
scripts/stop-xproxy-tunnel.ps1     旧的反向 SSH 隧道停止脚本
```

生产路径：

```text
/opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed
/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
```

第一个是维护用源码目录，第二个是 AstrBot 容器实际加载的运行时目录。
生产部署时必须同步需要的源码文件到两个目录，不能只改维护仓库。

## 3. 为什么选择 Twikit

这套功能经历过几条路线，当前选择是约束下的结果。

### 3.1 官方 X API

曾经测试过官方 API 路线，但当前账号和套餐返回 HTTP 402，无法作为不付费的自用方案。
因此代码没有依赖 Consumer Key、Consumer Secret、Bearer Token，也不应在维护时误以为
需要重新配置这些值。

### 3.2 RSSHub + X 登录态

早期方案是 RSSHub 的 `/twitter/user/<handle>` 路由：

```text
RSSHub -> X 路由 -> TWITTER_AUTH_TOKEN/登录态 -> RSS XML -> x_feed
```

RSSHub 方案的优点是协议简单、解析器容易写；缺点是 RSSHub 路由、X 登录态、代理、
容器网络和上游实现同时存在，任一环节改变都可能失效。`docs/x-feed-plugin-plan.md`
保留了这段历史配置和故障记录，但不能当作当前生产架构。

### 3.3 Nitter

Nitter 需要额外维护实例、访客会话或账号会话，实例可用性和上游适配也不稳定。它没有
进入当前生产链路。

### 3.4 Twikit

当前选择 Twikit 的理由：

- 少量账号、低频轮询时实现直接。
- 可以用浏览器导出的登录态，不需要付费 X API。
- 能直接得到用户对象和用户 Tweet 时间线。
- Tweet 对象中可以拿到正文、ID、发布时间、链接和媒体 URL。
- 可以在插件内把正文和图片拆开处理，图片失败不影响正文。

代价也明确存在：cookie 会过期，X 页面协议变化会导致 Twikit 断裂，且仍依赖本机在线
代理。因此它适合本项目的低频自用场景，不是通用的高可靠公共服务。

## 4. 当前网络架构

### 4.1 正在使用的链路

```text
AstrBot 容器
  -> Twikit / urllib 图片下载
  -> http://100.74.24.101:7890
  -> Tailscale 到 Windows 本机
  -> Windows 本地代理 127.0.0.1:7890
  -> X
```

`100.74.24.101` 是当前这台 Windows 设备的 Tailscale 地址。迁移到其他机器时，必须
替换为新设备的 Tailscale IP 或稳定主机名，并确保本机代理允许来自 Tailscale 网卡的
连接。

### 4.2 本机要求

Windows 本机必须同时满足：

- X 浏览器登录态有效。
- 代理客户端正在监听本地 `7890`。
- 代理客户端允许局域网/Tailscale 接入，而不是只绑定回环地址。
- Windows 防火墙允许来自 Tailscale 网卡的 7890 入站连接。
- Tailscale 已登录且设备在线。
- Tailscale 地址没有发生变化，或插件配置已同步更新。

服务器访问 X 的能力不等于本机浏览器能访问 X。真正的关键是服务器能否访问
`100.74.24.101:7890`，以及代理是否接受该连接。

### 4.3 旧的反向 SSH 隧道

历史方案是：

```text
AstrBot/RSSHub 容器
  -> Docker host 172.19.0.1:7890
  -> nightingale-xproxy-relay.service
  -> 服务器 127.0.0.1:17890
  -> Windows 反向 SSH 隧道
  -> Windows 127.0.0.1:7890
```

本机 `scripts/start-xproxy-tunnel.ps1` 默认建立：

```text
服务器 127.0.0.1:17890 -> 本机 127.0.0.1:7890
```

脚本还会在本地 `.local/xproxy-tunnel.pid`、`.log` 和 `.err.log` 保存进程状态。
它能检测本机 7890，但不能保证 Tailscale/SSH 长期在线；状态脚本出现 stale PID
时只能说明本地 PID 文件不再对应运行中的 SSH 进程。

当前生产图片问题就是旧链路失效：服务仍可能显示 active，但它转发的服务器
`127.0.0.1:17890` 没有真正监听。后来把正文和图片统一改成 Tailscale 直连，绕过
`nightingale-xproxy-relay.service` 和旧隧道。

旧服务可能仍被健康检查列出，但它不是当前 X Feed 正常工作的必要条件。不要因为它
显示 active 就断定 X 代理可用，也不要在没有确认其他任务依赖前擅自删除它。

2026-07-23 生产只读核对时，`nightingale-xproxy-relay.service` 状态仍是 `active`；
这只代表遗留进程存活，不改变当前正文和图片都走 Tailscale 直连的事实。

## 5. 插件启动与对象初始化

`main.py` 注册 `astrbot_plugin_x_feed` 后，`XFeedPlugin.__init__` 会：

1. 保存 AstrBot `Context` 和配置。
2. 将插件数据目录设为源码目录下的 `.local/`。
3. 创建 `XFeedStore`，初始化 SQLite 表。
4. 创建 `.local/images/` 图片缓存目录。
5. 创建后台任务句柄和轮询锁。
6. 不会在初始化时立刻请求 X，也不会在初始化时读取 cookie 创建 Twikit 客户端。

`initialize()` 只有在 `enabled` 为真时创建 `_poll_loop()`。轮询任务先等待 20 秒，
随后执行一次轮询，之后按 `poll_interval_minutes` 循环等待。

Twikit 客户端是懒加载的：第一次真正需要抓取时，`_fetch_feed()` 才创建
`TwikitFeedClient`。同一个插件进程内会复用该客户端；cookie 文件修改时间发生变化时，
下次抓取会重新创建客户端并读取新 cookie。

## 6. QQ 命令和订阅模型

### 6.1 命令

```text
/x帮助
/x订阅 @handle
/x取消订阅 @handle
/x订阅列表
/x推送测试 @handle
/x推送关
/x推送开
/x翻译状态
```

`normalize_handle()` 支持：

- `@FF_XIV_EN`
- `FF_XIV_EN`
- `https://x.com/FF_XIV_EN`
- `https://twitter.com/FF_XIV_EN`

最终只接受 1-15 位 ASCII 字母、数字和下划线，并统一转为小写。

### 6.2 会话范围

AstrBot 的 `unified_msg_origin` 是订阅的真正目标标识：

- 私聊执行 `/x订阅 @handle`，推送回该私聊。
- 群聊执行 `/x订阅 @handle`，推送回该群。
- 同一个 handle 在私聊和群聊中是两条独立订阅。
- `/x推送开` 和 `/x推送关` 只影响当前会话。
- `/x取消订阅` 只删除当前会话对应的 handle。

管理权限由 `admin_user_ids` 控制：

- 配置为空时，代码视为不限制。
- 配置为逗号或换行分隔的 QQ 号时，只有这些发送者能执行订阅、取消、开关命令。
- `/x订阅列表`、`/x推送测试` 和 `/x翻译状态` 不额外要求管理员权限。

### 6.3 新增订阅的行为

执行 `/x订阅 @handle` 时，不是直接写入数据库，而是先抓取一次账号：

1. handle 规范化。
2. 调用当前后端抓取。
3. 抓取失败则返回“订阅前测试失败”，不创建订阅。
4. 抓取成功后，以第一条结果作为最新基线写入订阅。
5. 这样下一次轮询不会把订阅前已有的旧动态当作新动态。
6. `initial_backfill_items` 大于 0 时，会把最新条目作为“当前最新”发给执行命令的会话。
7. 如果最新条目有图片且启用图片，会继续单独发送图片。

因此新增订阅默认是“从当前最新开始”，不是把账号历史全部补发。

## 7. 后台轮询和判新逻辑

### 7.1 轮询顺序

每轮轮询由 `_poll_once()` 完成：

1. 从 SQLite 读取所有 `enabled = 1` 的订阅。
2. 按 `handle, target_origin` 排序。
3. 用同一个 `XFeedStore` 锁保护数据库访问。
4. 按订阅逐条串行执行 `_process_subscription()`。
5. 每个订阅抓取、判新、发送、写状态完成后，才处理下一个订阅。

这意味着：

- 一个慢代理、慢 Twikit 请求或慢翻译会拖慢同一轮后面的订阅。
- 同一个账号被多个会话订阅时，目前会重复抓取多次；代码没有按 handle 做本轮抓取缓存。
- 没有单独的每订阅超时，Twikit 和图片下载超时由各自库/配置控制。

### 7.2 当前判新算法

订阅保存两个游标：`last_seen_id` 和 `last_seen_link`。

当前抓取结果通常是从最新到较旧的顺序。`_new_items()` 从头遍历：

- 遇到 `item_id == last_seen_id`，停止。
- 或遇到非空 `link == last_seen_link`，停止。
- 停止前的结果视为新动态。
- 如果订阅没有任何游标，返回空列表，防止首次轮询补发一批旧动态。

判新后，代码取 `max_items_per_poll` 条，再反转顺序发送，使同一轮多条动态尽量按旧到新
发送。发送完成后把最新抓取结果的第一条写入 `last_seen_id/link`。

### 7.3 重要的边界行为

`seen_items` 表目前只由 `record_seen()` 写入，没有被 `_new_items()` 查询。因此真正的
去重依据是每条订阅自身的 `last_seen_id/link`，不是 `seen_items`。

如果两次轮询之间的新动态数量超过 `twikit_timeline_count`，旧游标可能已经不在本次返回
窗口里。此时插件会把当前窗口都认为是新内容，但最多发送 `max_items_per_poll` 条，随后
把第一条设为新基线，窗口中剩余的旧动态可能被跳过。这是低频小规模方案的已知取舍。

如果发送文本时抛出异常，异常会回到轮询循环，可能影响本轮后续订阅；图片发送异常则在
图片函数内部记录 warning，不会阻止正文和 seen 状态继续完成。

## 8. 后端和数据转换

### 8.1 Twikit 后端

`main.py` 的 `_backend()` 只把配置值精确为 `rsshub` 时切换 RSSHub；其他值全部按
`twikit` 处理。因此拼写错误不会自动报错，而是回到 Twikit 路径。

`TwikitFeedClient.fetch_user_feed()` 的实际步骤：

1. 规范化 handle。
2. `_ensure_client()` 检查依赖、cookie 文件和 `auth_token`。
3. 创建 `twikit.Client(language=locale, proxy=proxy_url)`。
4. 使用 `client.set_cookies(cookies, clear_cookies=True)` 注入登录态。
5. 调用 `get_user_by_screen_name(handle)` 获取用户 ID。
6. 调用 `get_user_tweets(user.id, "Tweets", count=timeline_count)`。
7. 将每个 Tweet 转成内部统一的 `FeedItem`。
8. 成功后尝试把 Twikit 当前 cookie 持久化回文件。

Tweet 转换字段：

| Tweet 字段 | `FeedItem` 字段 | 说明 |
| --- | --- | --- |
| `id` / `rest_id` | `item_id` | 判新主键；没有时回退到链接或正文前 80 字符 |
| `full_text` / `text` | `title`、`summary` | 当前文本输出来源 |
| `created_at_datetime` / `created_at` | `published_at` | 尽量转为本地带时区 ISO 时间 |
| `url` 或拼接 URL | `link` | 没有显式链接时拼成 `https://x.com/<handle>/status/<id>` |
| `media` 及部分 `legacy` | `image_urls` | 只提取 HTTP/HTTPS 媒体 URL |

当前代码没有单独获取作者头像、显示名称、引用推文正文、视频文件或回复树；推送标题
使用订阅 handle。

### 8.2 RSSHub 后端

RSSHub 仍保留作兼容和实验后端，地址默认为：

```text
http://rsshub:1200/twitter/user/<handle>
```

`feed_client.py` 使用标准库 `urllib` 请求，并解析 RSS 或 Atom：

- RSS 使用 `guid`、`link`、`title`、`pubDate`、`description`。
- Atom 使用 `id`、`link`、`title`、`published/updated`、`summary/content`。
- 从 HTML `<img src>`、`content`、`thumbnail` 和 `enclosure` 中提取图片。
- 解析结果也统一为 `FeedItem`，后面的判新、翻译、发送逻辑不区分来源。

RSSHub 路径还需要 RSSHub 自己的 X 路由配置、登录态和代理；当前生产不是这条路径。

## 9. Cookie 登录态

### 9.1 文件位置

生产运行时：

```text
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/x_cookies.json
```

源码目录中的相对路径 `.local/x_cookies.json` 会按插件目录解析。不要把它放到仓库跟踪
文件，也不要在聊天中粘贴内容。

### 9.2 支持格式

`twikit_client.py` 支持：

1. Twikit 保存的 JSON object。
2. 浏览器导出的 cookie JSON array，每项有 `name` 和 `value`。
3. 纯文本 Cookie header，例如：

```text
auth_token=<已隐藏>; ct0=<已隐藏>
```

代码会把 JSON object 或数组整理为 `{cookie_name: cookie_value}`。纯文本会按分号和换行
拆分。代码硬性检查 `auth_token`；实际 X 请求通常还需要有效的 `ct0`，所以复现时建议
导出完整 X 域 cookie，而不是只手抄一个字段。

### 9.3 Cookie 生命周期

- cookie 文件不存在：直接失败。
- 文件为空或格式无法识别：直接失败。
- 缺少 `auth_token`：直接失败。
- 第一次使用或文件修改时间改变：重建 Twikit 客户端并重新注入 cookie。
- 请求成功后，插件尝试用 `client.get_cookies()` 将当前 cookie 写回 JSON 文件。
- 写回失败不会让已成功的抓取失败。

重新登录时只更新 `x_cookies.json`，不要删除 SQLite、不要清空订阅、不要重建整个插件。
建议使用只读小号；即使账号有历史付费记录，也不能保证不会触发 X 风控。

### 9.4 Cookie 安全

服务器上至少限制为运行用户可读；root 部署时也不要把文件放入公共下载目录。
任何调试只输出“文件存在、字段是否存在、请求是否成功”，不能输出 cookie 值、完整配置或
HTTP 请求头。

## 10. 翻译流程

翻译不是 Twikit 的一部分，而是正文已经拿到后的可选步骤。

`_format_item()` 先取 `item.title or item.summary`，再调用 `_translate_text()`：

1. 翻译关闭或正文为空：直接返回原文。
2. 指定 `translate_provider_id` 且 Provider 存在：使用它。
3. 否则尝试当前会话 Provider。
4. 再否则使用 AstrBot 第一个可用 Provider。
5. 使用配置的 system prompt 和目标语言调用 `context.llm_generate()`。
6. 单次超时由 `translate_timeout_seconds` 控制，默认 45 秒。
7. 最多尝试两次，第一次失败后等待 1 秒。
8. 仍失败或返回空文本：返回原文，不阻断推送。

翻译成功且 `translate_show_original=true` 时，文本结构是：

```text
X 更新：@handle
原文：
...

简体中文：
...
（由 <model> 翻译）
<发布时间>
<链接>
```

翻译会按订阅逐条串行执行，因此 Provider 慢时会明显增加一轮推送耗时。

验证命令：

```text
/x翻译状态
```

## 11. 图片下载与发送

Twikit 只负责提供图片 URL，`main.py` 的 `_download_image_sync()` 负责实际下载：

1. 只接受 HTTP/HTTPS URL。
2. 用图片 URL 的 SHA-256 前 24 位作为缓存名。
3. 缓存目录是 `.local/images/`。
4. 已存在且大小合法时直接复用。
5. 通过 `image_proxy_url` 建立 HTTP/HTTPS `ProxyHandler`。
6. 请求带 X Referer 和 Bot User-Agent。
7. 只接受 `Content-Type: image/*`。
8. 最多读取 `max_image_bytes + 1` 字节。
9. 超限、空响应、非图片响应或 URL 错误时抛出异常。
10. 下载成功后通过 `Comp.Image.fromFileSystem()` 发送本地图片。

自动轮询时先发文本，再逐张发图片；命令测试也是先发文本，再发图片。图片默认最多一张，
图片失败会记录 warning，但不影响文本。

缓存没有内置按天清理任务。长期运行应关注磁盘：删除旧缓存不会破坏订阅，但相同图片
再次推送时会重新下载；清理前不要误删 `x_feed.sqlite3` 或 cookie。

## 12. SQLite 状态和数据保护

数据库位置：

```text
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/x_feed.sqlite3
```

### 12.1 `subscriptions`

核心字段：

| 字段 | 含义 |
| --- | --- |
| `id` | 订阅自增 ID |
| `handle` | 规范化后的小写 X handle |
| `target_origin` | AstrBot 的私聊/群聊会话来源，决定消息发往哪里 |
| `target_kind` | `private` 或 `group` |
| `created_by` | 创建订阅的发送者 ID |
| `created_at` | 创建时间 |
| `enabled` | 是否参加后台轮询 |
| `last_seen_id` | 当前订阅的最后已知动态 ID |
| `last_seen_link` | 当前订阅的最后已知动态链接，ID 异常时的备用游标 |
| `last_success_at` | 最近一次抓取成功并更新状态的时间 |
| `failure_count` | 连续抓取失败次数 |
| `last_error` | 最近一次错误，最多保存 1000 字符 |

唯一约束是 `(handle, target_origin)`，所以同一账号可以发往多个会话，但同一会话不会有
重复订阅行。

### 12.2 `seen_items`

字段是 `handle`、`item_id`、`link`、`published_at`、`pushed_at`，主键是
`(handle, item_id)`。它目前用于保存“曾经发送过”的审计记录，但当前判新算法不查询它。
不要把它误解为全局去重表，也不要仅清理这张表来修复 last-seen 状态。

### 12.3 备份原则

修改源码前备份源码和运行时目录；修改配置前单独备份配置；任何同步都跳过：

```text
.local/x_feed.sqlite3
.local/x_cookies.json
.local/images/
```

数据库损坏或需要回滚时，先停止 AstrBot，再恢复备份，最后启动并检查订阅列表。不要用
空数据库覆盖生产运行数据。

## 13. 配置基线

配置文件：

```text
/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
```

以下是字段语义和当前基线。实际生产值始终以服务器配置文件为准，示例不包含任何秘密。

| 字段 | 当前/默认值 | 作用和注意事项 |
| --- | --- | --- |
| `backend` | `twikit` | 只有精确写 `rsshub` 才走 RSSHub，其他值回退 Twikit |
| `rsshub_base_url` | `http://rsshub:1200` | 仅 RSSHub 后端使用 |
| `twikit_cookies_file` | `.local/x_cookies.json` | 相对插件目录解析 |
| `twikit_locale` | `en-US` | Twikit Client 语言参数 |
| `twikit_proxy_url` | `http://100.74.24.101:7890` | X 正文抓取代理；留空表示直连 |
| `twikit_timeline_count` | `5` | 每次从 X 请求的时间线条数，影响漏推和请求量 |
| `enabled` | `true` | 是否启动后台轮询任务 |
| `poll_interval_minutes` | `10` | 代码强制不低于 5 分钟；不建议为追求实时而压低 |
| `max_items_per_poll` | `3` | 每个账号每轮最多发送动态数 |
| `initial_backfill_items` | `1` | 新订阅时立即显示最新条数；不代表历史补发 |
| `include_images` | `true` | 是否发送正文图片 |
| `max_images_per_post` | `4` | 每条动态最多发送图片数；代码强制限制在 0-4 |
| `image_proxy_url` | `http://100.74.24.101:7890` | 图片下载代理，当前与正文统一 |
| `image_download_timeout_seconds` | `20` | 单张图片下载超时 |
| `max_image_bytes` | `8000000` | 单张图片大小上限 |
| `admin_user_ids` | 空 | 空表示不限制订阅管理；否则逗号/换行分隔 QQ 号 |
| `failure_notice_threshold` | `6` | 连续失败达到此次数才升级为 warning |
| `translate_enabled` | 生产开启，schema 默认关闭 | 是否调用 AstrBot LLM |
| `translate_target_lang` | `简体中文` | 翻译目标语言 |
| `translate_provider_id` | 空/自动选择 | 指定 Provider；空时按当前会话再到第一个可用 Provider |
| `translate_show_original` | `true` | 翻译时是否保留原文 |
| `translate_prompt` | 空 | 空时使用内置翻译提示词 |
| `translate_timeout_seconds` | `45` | 单次翻译超时 |
| `max_output_chars` | `1800` | 单条文本消息截断长度 |

推荐低频基线：少量账号、10 分钟或更长间隔、时间线 5-10 条、每轮最多 1-3 条。
增加账号数量会线性增加请求、翻译和图片下载压力。

2026-07-23 已对生产配置做过只读核对：表中列出的 backend、locale、两个代理地址、
时间线条数、轮询间隔、每轮上限、图片开关和上限、失败阈值、翻译开关/目标语言/原文
模式、超时及输出上限均与服务器配置一致。核对过程没有读取或输出 cookie、Provider ID
和管理员 QQ 号。

## 14. 从零复现和部署

### 14.1 准备条件

需要：

- Linux 服务器上的 AstrBot 和 NapCat 已运行。
- Windows 或其他可稳定访问 X 的设备上运行本地代理。
- 服务器可以通过 Tailscale 访问该设备的代理端口。
- AstrBot 容器能读取插件运行目录。
- Python 运行时可安装 `requirements.txt` 中的 Twikit Git 依赖。

先确认仓库文件和当前分支，不要在含有其他未提交功能改动的工作区执行大范围覆盖：

```powershell
git -c safe.directory=H:/NightingaleSilenceWeb/NightingaleOpsBot status --short
```

### 14.2 安装插件依赖

依赖来源在：

```text
astrbot-plugin/astrbot_plugin_x_feed/requirements.txt
```

当前是：

```text
git+https://github.com/unclecode/twikit.git
```

2026-07-23 生产 `pip show twikit` 显示：

```text
Version: 1.7.6
Location: /AstrBot/data/site-packages
Home-page: https://github.com/d60/twikit
```

仓库 requirements 指向 `unclecode/twikit`，但 fork 可能沿用原项目的包 metadata，
所以 `Home-page` 仍显示 `d60/twikit`。`pip show` 不能证明实际安装的 Git commit；当前
requirements 也没有固定 commit，重新安装可能取得不同代码。维护时应同时记录安装来源、
commit/tag 和包版本，条件允许时把依赖固定到已验证 commit，提升可复现性。

生产更新 Twikit 时必须先做备份，并在容器内确认：

```bash
docker exec astrbot python3 -m pip show twikit
docker exec astrbot python3 -c 'import twikit; print(twikit.__file__)'
```

不要因为普通的 cookie 失效就先升级依赖；只有出现协议兼容错误，例如
`Couldn't get KEY_BYTE indices`，才优先核查 Twikit 版本和上游兼容性。

### 14.3 写入配置和 cookie

使用 AstrBot 配置界面或安全脚本设置非敏感配置。生产最小关键配置类似：

```json
{
  "backend": "twikit",
  "twikit_cookies_file": ".local/x_cookies.json",
  "twikit_proxy_url": "http://<tailscale-ip>:7890",
  "image_proxy_url": "http://<tailscale-ip>:7890",
  "poll_interval_minutes": 10,
  "include_images": true,
  "max_images_per_post": 4
}
```

将浏览器导出的完整 X cookie 写入运行时 `.local/x_cookies.json`，并限制权限。不要把
cookie 文件同步到维护仓库，也不要通过 QQ 传递。

### 14.4 备份并同步源码

生产目录是 `/opt/nightingale`，覆盖前执行：

```bash
mkdir -p /opt/nightingale/backups
tar -czf /opt/nightingale/backups/x-feed-before-sync-$(date +%Y%m%d-%H%M%S).tgz \
  /opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed \
  /opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed \
  /opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
```

只同步需要的源码文件，例如：

```bash
install -m 0644 main.py \
  /opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed/main.py
install -m 0644 main.py \
  /opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/main.py
```

实际部署时还要按改动同步 `twikit_client.py`、`feed_client.py`、`storage.py`、schema
或 requirements；不要把本地 `.local/` 一起覆盖过去。

### 14.5 重启和验证

```bash
docker exec astrbot python3 -m py_compile \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/main.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/feed_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/twikit_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/storage.py

cd /opt/nightingale/astrbot
docker compose restart astrbot

docker logs --since 2m astrbot 2>&1 \
  | grep -Ei 'x_feed|twikit|Traceback|ERROR|WARNING' \
  | tail -200
```

然后在私聊中低频测试：

```text
/x翻译状态
/x推送测试 @FF_XIV_EN
```

测试命令会抓取最新一条并可能发送图片，是真实 QQ 侧测试，不是无副作用的纯函数测试。
不要在群里反复刷测试命令。

## 15. 日常使用

### 订阅一个账号

私聊：

```text
/x订阅 @FF_XIV_EN
```

群聊：

```text
/x订阅 @FF_XIV_EN
```

消息会发回执行命令的私聊或群聊。私聊订阅不会自动转移到群聊；需要在目标群重新执行。

### 查看和控制

```text
/x订阅列表
/x推送关
/x推送开
/x取消订阅 @FF_XIV_EN
```

### 单次测试

```text
/x推送测试 @FF_XIV_EN
```

它只发送最新条目，不会修改订阅的 last-seen 游标，也不会创建订阅。

## 16. 故障排查矩阵

### 16.1 订阅前测试失败：cookie

典型文案：

```text
X 登录态失效或 cookies 不可用，请重新导出并更新 x_cookies.json。
```

检查：

```bash
test -s /opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/x_cookies.json
docker exec astrbot python3 -m pip show twikit
```

处理：

1. 在本机确认 X 浏览器仍能正常登录和浏览。
2. 重新导出完整 X 域 cookie。
3. 只更新服务器 `.local/x_cookies.json`。
4. 重启 AstrBot，或等待下次抓取触发 mtime 检查。
5. 用 `/x推送测试 @handle` 验证。

不要在 cookie 错误时删除 SQLite；订阅状态与登录态是两个独立问题。

### 16.2 代理不通

典型文案：

```text
Twikit 无法通过代理连到 X。请检查代理地址、本机 7890 端口和 Tailscale 是否在线。
```

排查顺序：

1. Windows 本机 X 浏览器是否可访问。
2. 本机代理是否监听 7890。
3. 代理是否允许 Tailscale 入站。
4. Tailscale 客户端是否在线、地址是否变化。
5. 服务器是否能连到 `<tailscale-ip>:7890`。
6. 正文代理和图片代理是否都写成同一可用地址。

图片单独失败时，优先检查 `image_proxy_url`。历史上正文已经能抓取，但图片仍使用
`172.19.0.1:7890` 旧链路，结果日志反复出现 `Remote end closed connection without response`。
现在应统一使用 Tailscale 直连。

### 16.3 `KEY_BYTE` 或协议解析失败

典型错误：

```text
Couldn't get KEY_BYTE indices
```

这是 X 页面协议变化或 Twikit 版本兼容问题，不是普通 cookie 过期。处理顺序：

1. 记录当前 Twikit 版本和安装来源。
2. 阅读上游仓库变更说明。
3. 备份生产插件和运行时。
4. 更新 requirements 对应的维护版本。
5. 容器内执行 `py_compile` 和低频 `/x推送测试`。
6. 确认新版本不会改写或删除 `.local/`。

### 16.4 抓到了正文但没有图片

可能原因：

- Tweet 没有图片，只有视频、引用卡片或媒体字段未被 Twikit 暴露。
- `include_images=false`。
- `max_images_per_post=0`。
- X 图片 URL 下载被代理阻断。
- 响应 `Content-Type` 不是 `image/*`。
- 图片超过 `max_image_bytes`。
- QQ/NapCat 拒绝本地文件消息。

检查日志：

```bash
docker logs --since 10m astrbot 2>&1 \
  | grep -Ei 'X feed image|pbs.twimg.com|image|Traceback|ERROR|WARNING' \
  | tail -200
```

先确认文件是否写入 `.local/images/`，再区分“下载失败”和“QQ 发送失败”。

### 16.5 订阅后没有立即推送

这是默认行为：订阅命令先把当前最新条目写成基线，避免把旧动态当新动态。只有
`initial_backfill_items` 大于 0 才会在订阅命令中显示最新条目。

要验证运行链路，用 `/x推送测试 @handle`，不要反复取消再订阅。

### 16.6 有新动态但延迟或少发

这是低频轮询模型的预期边界：

- 轮询不是实时订阅。
- 每轮开头可能等待 20 秒，随后按间隔运行。
- 多个订阅串行处理。
- Twikit、代理、翻译和图片下载都会增加耗时。
- `max_items_per_poll` 会限制一轮发送数量。
- 如果新动态超出 `twikit_timeline_count` 窗口，较旧动态可能被跳过。

不建议简单把间隔压到 5 分钟以下来解决延迟；这会增加 X 请求频率和风控风险。

### 16.7 翻译没有出现

执行：

```text
/x翻译状态
```

如果状态开启但仍只有原文：

- 检查 AstrBot 是否有可用 LLM Provider。
- 检查指定 Provider ID 是否存在。
- 检查翻译超时和 Provider 配额。
- 查看 `X feed translation failed` 或 `translation skipped` 日志。

翻译失败会回退原文，不应因此认为 X 抓取失败。

### 16.8 只停某个群的推送

在目标群执行 `/x推送关`。不要修改全局 `enabled`，后者会停止插件后台任务。恢复使用
`/x推送开`；删除单个账号使用 `/x取消订阅 @handle`。

## 17. 维护、升级和回滚

### 日常检查

建议按以下顺序：

1. `/x订阅列表` 查看目标会话的状态和连续失败次数。
2. `/x翻译状态` 确认翻译配置。
3. 查看 AstrBot 中 `x_feed`、`twikit` 和图片错误日志。
4. 确认本机代理和 Tailscale 在线。
5. 检查 `.local/images/` 是否异常增长。

### 更新 cookie

只备份并替换：

```text
.local/x_cookies.json
```

不要同时更新代码、删除数据库或修改轮询参数。更新后做一次私聊测试即可。

### 更新代码

1. 读取本文件和当前 `git status`。
2. 只修改相关插件文件。
3. 备份生产源码、运行时和配置。
4. 同步源码，不覆盖 `.local/`。
5. 容器内 `py_compile`。
6. 重启 AstrBot。
7. 看启动日志和失败计数。
8. 私聊低频 `/x推送测试`。

### 更新 Twikit

Twikit 是上游协议适配层，升级风险高于普通插件代码。每次升级都应记录：

- 原版本和新版本。
- requirements 来源和 commit/tag。
- 是否解决了具体 X 协议错误。
- cookie 是否仍能加载。
- 文本、时间线和图片是否均能工作。

如果新版本失败，优先恢复备份的运行时包和插件代码，不要重置数据库。

### 回滚

```bash
cd /opt/nightingale/astrbot
docker compose stop astrbot
# 从带时间戳的备份恢复需要的源码/配置，保留或单独恢复 .local
docker compose start astrbot
docker logs --since 2m astrbot 2>&1 | tail -200
```

回滚时必须确认：

- `x_feed.sqlite3` 没被空文件覆盖。
- cookie 没被旧备份覆盖成过期版本。
- `twikit_proxy_url` 和 `image_proxy_url` 没回退到旧的 `172.19.0.1:7890`。

## 18. 安全与隐私边界

X cookie 等价于登录态，应按密码处理：

- 不进 Git。
- 不写文档。
- 不发 QQ。
- 不打印完整 JSON、请求头或异常上下文中的 cookie。
- 不在公开日志中保存原始请求。
- 优先使用低权限、低价值、只读用途账号。

Tailscale 地址、服务器路径、群聊来源和 `target_origin` 也不应作为公开网页内容输出。
本文保存在私有 Bot 仓库，复制到其他项目时应替换具体地址和路径为占位符。

## 19. 已知限制和后续改进方向

当前故意保持实现简单，以下不是“配置没开”，而是代码层限制：

- 不是实时推送，没有 X webhook/stream。
- 同一个 handle 被多个会话订阅时重复抓取。
- `seen_items` 没参与实际判重。
- 时间线窗口不足时可能跳过旧动态。
- 推送文本发送异常可能影响本轮后续订阅。
- 翻译和订阅处理都是串行的。
- 只处理 Twikit 暴露的图片 URL，不保证引用媒体、视频、GIF 或卡片媒体完整。
- 当前没有图片缓存自动清理策略。
- X 页面协议或 Twikit 上游变化可能随时破坏抓取。
- Twikit Git 依赖当前未固定 commit，重新安装不保证得到与生产完全相同的代码。
- cookie 过期和账号风控无法由插件彻底消除。

合理的后续改进顺序：

1. 为每轮同 handle 增加抓取结果缓存，减少重复请求。
2. 用 `seen_items` 辅助判重，但先设计清理策略和迁移逻辑。
3. 为每个订阅增加单独超时与失败退避，避免一个账号拖住整轮。
4. 增加图片缓存按时间/总容量清理。
5. 记录抓取耗时、推送耗时和图片失败原因，但继续屏蔽 cookie/token。
6. 增加离线单元测试，覆盖 cookie 三种格式、时间线判新、窗口溢出和错误分类。

不要为了追求“秒发”直接把轮询改成高频；先解决批量调度、缓存和退避，再评估频率。

## 20. 交接给后续对话的最短摘要

后续对话接手时，先读本文件，然后确认：

```text
生产插件：/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed
生产配置：/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
运行数据：/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed/.local/
后端：Twikit
正文代理：http://<Tailscale-IP>:7890
图片代理：http://<Tailscale-IP>:7890
测试命令：/x推送测试 @handle
```

先判断问题属于 cookie、代理、Twikit 版本、判新游标、翻译、图片下载还是 QQ 发送，
不要一上来清数据库或重装整个插件。任何生产改动先备份，任何验证优先私聊低频进行。
