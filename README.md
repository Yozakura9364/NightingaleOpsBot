# NightingaleOpsBot

NightingaleOpsBot 是夜莺不语相关服务的私有 QQ Bot 和运维自动化项目。

本项目包含：

- `runner/`：只监听本机/内网地址的 Node.js job runner。
- `astrbot-plugin/astrbot_plugin_ns_ops/`：把 QQ `/ns` 命令映射到 runner job 的 AstrBot 插件。
- `astrbot-plugin/astrbot_plugin_risingstone_sign/`：石之家私聊绑定和每日签到插件。
- `astrbot-plugin/astrbot_plugin_sqmall_sign/`：盛趣积分商城私聊绑定和每日签到插件。
- `astrbot-plugin/astrbot_plugin_x_feed/`：低频 X 账号动态推送插件。
- `astrbot-plugin/astrbot_plugin_deadline_reminder/`：日程和 deadline 每日提醒插件。
- `astrbot-plugin/astrbot_plugin_github_watch/`：GitHub 仓库更新推送和 preset 订阅插件。
- `astrbot-plugin/astrbot_plugin_github_cards/`：GitHub 链接卡片、issue/PR/readme 查询插件。
- `astrbot-plugin/astrbot_plugin_share_link_resolver/`：QQ 手机分享卡片原始链接解析插件。
- `scripts/`：本机安装、隧道和计划任务辅助脚本。

项目规则和上下文：

- `AGENTS.md`：本仓库 AI 协作和开发维护的长期规则。
- `PUBLICPROMPT.md`：用户全局 AI 助手偏好；本仓库内以 `AGENTS.md` 为更高优先级。
- `docs/ai/PROJECT_CONTEXT.md`：项目边界、AI 工作流、编码规则和维护注意事项。

运维文档：

- `docs/ffxiv-watch-plan.md`：FF14 官方新闻和商城更新提醒方案。
- `docs/github-watch-plan.md`：通用 GitHub 仓库更新提醒方案和 preset。
- `docs/ns-health-runbook.md`：`/ns health`、每日状态报告和自动告警维护文档。
- `docs/share-link-resolver-runbook.md`：QQ 分享卡片原始链接解析维护文档。
- `docs/tencent-cloud-traffic-runbook.md`：腾讯云轻量应用服务器流量日报维护文档。
- `docs/x-feed-twikit-runbook.md`：X/Twikit 推送插件维护文档。

本仓库与 `NightingaleSilenceWebV2` 分离。Bot 代码本机放在 `H:\NightingaleSilenceWeb\NightingaleOpsBot`，服务器放在 `/opt/nightingale/NightingaleOpsBot`，不要移动回 V2。

runner 只暴露已注册 job。QQ 输入只用于选择 job id 或结构化参数，不能拼接成任意 shell 命令。

## 本机目录

Windows 本机预期同级目录：

```text
H:\NightingaleSilenceWeb\
├── NightingaleOpsBot\
├── NightingaleSilenceWebV2\
└── astrbot\
```

## 本机配置

创建被 git 忽略的运行配置：

```powershell
New-Item -ItemType Directory -Force .\.local
Copy-Item .\runner\runner.local.example.json .\.local\runner.local.json
```

然后编辑 `.local\runner.local.json`，设置足够长的随机 `NS_OPS_TOKEN`。同一个 token 也要写入 AstrBot 插件配置：

```text
H:\NightingaleSilenceWeb\astrbot\data\config\astrbot_plugin_ns_ops_config.json
```

不要提交 `.local\runner.local.json`。

## 启动 Runner

```powershell
.\runner\start-runner.ps1
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18766/health
```

## 安装 AstrBot 插件

```powershell
.\scripts\install-astrbot-plugin.ps1
```

然后重启 AstrBot：

```powershell
Set-Location H:\NightingaleSilenceWeb\astrbot
docker compose restart astrbot
```

## 注册开机自启

```powershell
.\scripts\register-runner-task.ps1 -StartNow
```

该脚本会创建 Windows 计划任务：

```text
NightingaleSilence NS Ops Runner
```

当前 Windows 用户登录后会自动启动 runner。

## QQ 命令

### 运维命令

只读命令：

```text
/ns ping
/ns status
/ns health
/ns daily
/ns logs astrbot
/ns traffic today
/ns traffic debug
/ns traffic status
/ns traffic bind
/ns v2 status
/ns v2 check
/ns v2 build
/ns armoire check-store
/ns armoire audit-store
/ns armoire audit-store-latest
/ns git status
/ns git diff
```

需要确认的命令：

```text
/ns restart astrbot
/ns v2 deploy
/ns git commit <提交说明>
/ns git push
/ns file write <文件名.md> <内容>
/ns armoire sync-catalog
/ns confirm <验证码>
```

## 安全说明

- 开始维护本仓库前先读 `AGENTS.md`。
- 所有文本文件统一保存为 UTF-8 无 BOM，避免 ANSI、GBK、UTF-8 with BOM 和 UTF-16。
- 不要默认当前项目就是上一轮聊天的项目；先确认 workspace 和仓库根目录。
- `git.commit` 只提交已经 staged 的文件，不会自动执行 `git add`。
- `git.push` 要求工作区干净且当前分支已有 upstream。
- `file.write` 默认只能写入 `.local\inbox` 下。
- `v2.deploy` 在未配置 `NS_OPS_DEPLOY_NPM_SCRIPT` 且 V2 项目不存在对应 npm script 时保持禁用。

### 石之家签到

石之家签到是独立 AstrBot 插件，不属于 `/ns` 运维命令。它允许 QQ 用户在私聊中绑定自己的石之家登录态，然后手动签到或接收每日自动签到结果。

私聊二维码绑定：

```text
/石之家绑定
/石之家绑定 小号1
```

二维码绑定流程会让 runner 打开盛趣官方登录页，切换到二维码登录页，并把临时二维码截图返回给 AstrBot。用户扫码并在手机端确认后，插件只保存 `ff14risingstones` cookie 和 user-agent，并写入现有加密 SQLite 存储。

手动 cookie 绑定仍可用：

```text
/绑定石之家
/绑定石之家 小号1
COOKIE: ff14risingstones=...
USER_AGENT: Mozilla/5.0 ...
```

私聊命令：

```text
/石之家绑定
/石之家状态
/石之家签到
/石之家房屋
/石之家签到 小号1
/石之家房屋 小号1
/石之家状态 小号1
/石之家改名 默认 莺歌
/石之家改名 小号1 新名字
/石之家解绑 默认
/石之家解绑 小号1
/石之家帮助
/石之家扫码绑定
```

每个 QQ 用户可以绑定多个石之家账号，每个账号使用一个短槽位名。不提供槽位名时使用 `默认`，兼容旧的单账号绑定。`/石之家签到` 不带槽位名会签到所有槽位；`/石之家签到 默认` 或 `/石之家签到 小号1` 只签到指定槽位。`/石之家改名 旧槽位 新槽位` 可以在不重新扫码、不改变凭据的情况下改名。解绑需要明确槽位名，避免误删默认账号。`/石之家房屋` 只检查保存账号的房屋自动撤除提醒，不执行签到。

启用 `show_account_summary` 时，私聊绑定、状态和签到结果会显示简短账号摘要：

```text
绑定角色：大区 / 服务器 / 角色名
签到角色：大区 / 服务器 / 角色名
```

群聊只返回引导，不接受 cookie：

```text
/石之家签到
/石之家帮助
```

凭据存储：

- 存在已安装 AstrBot 插件目录的 `.local/` 下。
- SQLite 中加密保存 cookie 和 user-agent。
- 加密密钥本地生成为 `.local/secret.key`。
- 这些运行文件不提交到仓库。
- 二维码截图是临时文件，视为敏感信息，只能在请求绑定的私聊中使用。

签到 API 流程参考 `StarHeartHunt/ff14risingstone_sign_task`（MIT）。

### 盛趣积分商城签到

盛趣积分商城签到是独立 AstrBot 插件。它允许 QQ 用户在私聊中绑定自己的叨鱼凭据，然后手动执行盛趣积分商城签到或接收每日自动签到结果。

私聊扫码绑定：

```text
/盛趣商城绑定
/盛趣商城绑定 小号1
```

扫码绑定会优先保存可复用的叨鱼登录态；如果官方登录只返回短期商城 session，会保存短期 session 作为兜底，过期后需要重新扫码或改用手工绑定。

手工绑定兜底格式：

```text
/盛趣商城绑定
/盛趣商城绑定 小号1
SESSION_ID: login-xxxxxxxx
MEMBER_ID: 1795361933

或

DAOYU_KEY: DY_...
USER_ID: 807483
NICKNAME: sdo807483

或

DAOYU_KEY: DY_...
SHOW_USERNAME: 138****1234
```

私聊命令：

```text
/盛趣商城帮助
/盛趣商城绑定
/盛趣商城状态
/盛趣商城签到
/盛趣商城签到 小号1
/盛趣商城改名 小号1 新名字
/盛趣商城解绑 小号1
```

短别名：

```text
/盛趣绑定
/盛趣状态
/盛趣签到
/盛趣解绑
/商城绑定
/商城状态
/商城签到
/商城解绑
```

每个 QQ 用户可以绑定多个盛趣商城账号，每个账号使用一个短槽位名。不提供槽位名时使用 `默认`。`/盛趣商城签到` 不带槽位名会签到所有槽位。

凭据存储：

- 存在 `astrbot_plugin_sqmall_sign` 插件目录的 `.local/` 下。
- SQLite 中加密保存 `DAOYU_KEY` 或 `SESSION_ID`，并保存对应的身份字段（`MEMBER_ID`、`SHOW_USERNAME` 或 `NICKNAME/USER_ID`）。
- 加密密钥本地生成为 `.local/secret.key`。
- 这些运行文件不提交到仓库。
- 群聊只返回引导，不接受 `DAOYU_KEY`。

签到 API 流程参考 `FF14CN/Sarean-arsenal`（AGPL-3.0）。

### X 动态推送

X 动态推送是低频更新插件。它通过 Twikit 读取 X 时间线，把 last-seen 状态保存到插件本地 SQLite，并推送到执行订阅命令的 QQ 私聊或群聊。

当前生产链路：

```text
astrbot_plugin_x_feed -> Twikit -> 可选 LLM 翻译 -> QQ 文本/链接
X 图片 URL -> 代理下载 -> 本地图片文件 -> QQ 图片
```

常用命令：

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

订阅范围：

- 在私聊中执行 `/x订阅 @handle` 会推送到私聊。
- 在群聊中执行 `/x订阅 @handle` 会推送到该群。
- 私聊和群聊订阅分开保存。

运行注意事项：

- 当前可用 X 链路使用 Twikit + 本地导出的 cookies，不是付费 X API。
- 服务器通过 Tailscale 访问你的 Windows 本机代理 `http://100.74.24.101:7890`。
- 只要你的电脑关机、本机代理没开、Tailscale 掉线，X 推送就会一起失效。
- 生产环境当前启用了翻译。翻译使用 AstrBot LLM provider，目标语言为简体中文，保留原文；翻译失败时回退到原文。可用 `/x翻译状态` 验证。

完整维护流程见 `docs/x-feed-twikit-runbook.md`。

### 日程提醒

日程提醒是轻量 deadline 插件。它把 deadline 存在插件本地 SQLite，并每天推送当前聊天的有效日程。默认推送时间是 `Asia/Shanghai` 时区的 09:00。

常用命令：

```text
/ddl 帮助
/ddl 添加 国服活动 2026-08-01 23:59 活动名称
/ddl 添加 国际服活动 2026-08-01 活动名称
/ddl 列表
/ddl 今日
/ddl 删除 3
/ddl 暂停 3
/ddl 恢复 3
/ddl 关
/ddl 开
/ddl 广播加入
/ddl 广播添加 国服活动 2026-08-01 23:59 活动名称
/ddl 广播列表
/ddl 广播今日
/ddl 广播删除 3
```

`/日程` 也是 `/ddl` 的别名。

提醒格式：

```text
日程提醒

国服活动：
#1 xxx的xx
结束时间：2026-08-01 23:59
距离结束还有 12天3小时
```

范围和存储：

- 在私聊中执行命令，管理私聊日程。
- 在群聊中执行命令，管理该群日程。
- 群聊执行 `/ddl 广播加入` 后，会接收全局广播日程。
- 广播日程通过 `/ddl 广播添加 ...` 管理，并推送给所有已加入广播的群。
- 日程保存在 `astrbot_plugin_deadline_reminder/.local/`。
- 当前聊天执行 `/ddl 关` 不会删除已有日程。

### GitHub Watch

GitHub Watch 是通用 GitHub 仓库更新推送插件。它轮询 GitHub REST API，把基线和投递记录保存在插件本地 SQLite，并推送到执行订阅命令的 QQ 私聊或群聊。

常用命令：

```text
/ghwatch 帮助
/ghwatch 状态
/ghwatch 列表
/ghwatch 订阅 owner/repo [branch]
/ghwatch 取消 owner/repo
/ghwatch preset
/ghwatch preset show ffxiv-datamining
/ghwatch preset ffxiv-datamining
/ghwatch preset nightingale-projects
/ghwatch 检查
/ghwatch 测试 owner/repo
/ghwatch 事件 owner/repo push on
/ghwatch 事件 owner/repo release off
/ghwatch 事件 owner/repo tag off
/ghwatch 开
/ghwatch 关
```

当前内置 preset：

```text
ffxiv-datamining
- InfSein/ffxiv-datamining-mixed
- Ra-Workspace/ffxiv-datamining-ko
- thewakingsands/ffxiv-datamining-tc

nightingale-projects
- Yozakura9364/NightingaleOpsBot
```

运行注意事项：

- 首次订阅会把当前最新 commit/release/tag 记录为基线，不推送历史更新。
- `/ghwatch 订阅 owner/repo` 不写 branch 时，插件会从 GitHub 读取仓库默认分支，不假设 `main` 或 `master`。
- `/ghwatch 检查` 会检查当前聊天订阅的仓库、分支、release 和 tag API 状态，不改变基线。
- 默认监听事件是 `push`、`release`、`tag`。
- 公开仓库不配置 token 也可用。仓库较多时建议在插件配置中设置 `github_token` 提高 GitHub API 限额；不要在 QQ 中发送 token。
- 运行数据保存在 `astrbot_plugin_github_watch/.local/`，部署时不要提交或覆盖。

### GitHub Cards

GitHub Cards 是外部 AstrBot 插件，来源为 `Soulter/astrbot_plugin_github_cards`。它和 GitHub Watch 同时安装，但用途不同：

- `astrbot_plugin_github_watch`：主动仓库更新推送，带 Nightingale preset。
- `astrbot_plugin_github_cards`：被动解析 GitHub 链接卡片，以及 issue/PR/readme 查询。

常用命令：

```text
/ghlink on
/ghlink off
/ghissue owner/repo#123
/ghpr owner/repo#123
/ghreadme owner/repo
/ghlimit
```

运行注意事项：

- 发送 `https://github.com/owner/repo` 链接可能触发 OpenGraph 卡片。
- 插件也提供 `/ghsub`、`/ghunsub`、`/ghlist`，但 Nightingale 当前优先使用 `/ghwatch` 做仓库更新订阅，避免重复推送逻辑。
- Webhook 模式默认关闭；除非另行制定 webhook 方案，否则不要暴露 webhook 端口。
- 上游插件许可证为 GPL-3.0。

### 石之家二维码登录探针

本仓库包含一个实验探针，用于检查后端是否能打开官方石之家登录页并看到二维码/登录候选：

```powershell
npm run probe:risingstone-login
```

如需手动测试完整二维码流程，可以打开可见 Chrome 窗口并在超时前扫码：

```powershell
npm run probe:risingstone-login -- --headed --accept-login-terms --wait-for-login-ms=180000
```

探针会优先使用本地或全局 Playwright。它不会打印 cookie。截图写入 `.local/probes/`，该目录被 git 忽略。如果截图包含登录二维码，应视为敏感信息。
