# GitHub Watch Plan

本文记录通用 GitHub 仓库更新监听方案。它不限定 FF14，可用于任意 GitHub 仓库；FF14 datamining 只是一个预设分组。

## 当前状态

已落地第一版自写轮询插件：

```text
astrbot-plugin/astrbot_plugin_github_watch/
```

第一版没有使用 GitHub App / webhook，也不需要公网回调。插件通过 GitHub 公共 REST API 轮询公开仓库；如需监听私有仓库或提高限流额度，可以在 AstrBot 插件配置中填写 `github_token`。

当前命令：

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

运行数据保存在插件目录的 `.local/github_watch.sqlite3`，部署时不要覆盖 `.local/`。

已加固：

- 不写 branch 时自动读取 GitHub 仓库默认分支。
- `/ghwatch preset show <id>` 可查看 preset 展开清单。
- `/ghwatch 检查` 可诊断当前会话订阅的 repo / branch / release / tag API 状态。

## 目标

- 优先复用现成 AstrBot 插件或 GitHub webhook 插件，少写自定义代码。
- 支持群聊和私聊订阅任意 GitHub 仓库。
- 支持 commit / release / tag 更新提醒；issue / PR 可作为后续可选功能。
- 支持预设分组，一条命令订阅一组常用仓库。
- 首次订阅只建立基线，不推历史。
- 每个群/私聊独立去重、独立开关。

## 候选方案

### 现成插件优先

优先在 AstrBot 插件市场和 GitHub 搜索现成插件。当前已发现：

- `xunxiing/astrbot_plugin_githubapp-adapter`
  - 偏 GitHub App / webhook / issue-PR 交互。
  - 功能可能比“repo 更新提醒”更重。
  - 后续安装前需要确认配置复杂度、鉴权方式、是否支持普通 commit/release 推送。

如果现成插件满足以下能力，就优先使用：

- 按群/私聊订阅 repo。
- 支持 push / release / tag 事件。
- 有去重。
- 有开关。
- 不要求把 GitHub secret 暴露到群聊。

### 轻量适配层

如果现成插件不支持 preset，但支持订阅命令，可以写一个很薄的适配层：

- 读取 `config/github-watch-presets.json`。
- 把 `/ghwatch preset ffxiv-datamining` 展开成多条现成插件订阅命令。
- 不自己处理 GitHub API。

如果现成插件不合适，最后再考虑自写轮询版。

## 预设清单

预设文件：

```text
config/github-watch-presets.json
```

当前预设：

```text
ffxiv-datamining
- InfSein/ffxiv-datamining-mixed
- Ra-Workspace/ffxiv-datamining-ko
- thewakingsands/ffxiv-datamining-tc

nightingale-projects
- Yozakura9364/NightingaleOpsBot
```

后续如果 V2 或其他项目有公开仓库，再追加到 `nightingale-projects`。

## 命令设计

如果最终插件由我们控制，建议命令：

```text
/ghwatch 帮助
/ghwatch 状态
/ghwatch 订阅 owner/repo
/ghwatch 取消 owner/repo
/ghwatch 列表
/ghwatch 开
/ghwatch 关
/ghwatch preset
/ghwatch preset ffxiv-datamining
/ghwatch preset nightingale-projects
```

来源细分：

```text
/ghwatch 事件 owner/repo push on
/ghwatch 事件 owner/repo release on
/ghwatch 事件 owner/repo tag on
/ghwatch 事件 owner/repo issue off
/ghwatch 事件 owner/repo pr off
```

## 推送格式

Commit 更新：

```text
GitHub 更新
仓库：InfSein/ffxiv-datamining-mixed
分支：master
提交：abcdef1 Update 7.x data
作者：...
链接：https://github.com/InfSein/ffxiv-datamining-mixed/commit/abcdef1
```

Release 更新：

```text
GitHub Release
仓库：owner/repo
版本：v1.2.3
标题：...
链接：...
```

群聊默认不展示维护建议。私聊管理员可以附加：

```text
关联预设：ffxiv-datamining
可能影响：NSGlamour / V2 Armoire 数据
建议：需要时手动执行数据 rebuild/check。
```

## 去重和基线

- 首次订阅只保存当前最新 commit/release/tag，不推历史。
- 每个聊天窗口独立记录已推送事件。
- Commit event key：`github:push:<owner>/<repo>:<branch>:<sha>`。
- Release event key：`github:release:<owner>/<repo>:<release-id-or-tag>`。
- Tag event key：`github:tag:<owner>/<repo>:<tag>`。

## 与 FF14 Watch 的边界

GitHub Watch 承接所有 GitHub 仓库更新提醒，包括 FF14 datamining。

FF14 Watch 只保留 FF14 专属解析：

- 国服/国际服官方新闻。
- 国服/台服/日服/韩服商城更新。
- 后续 Armoire 候选生成。

FF14 Watch 不再内置 GitHub 仓库监听逻辑。

## 实施阶段

### Phase 0: 插件调研

- 在 AstrBot 插件市场搜索 GitHub / webhook / release / repository 相关插件。
- 验证 `astrbot_plugin_githubapp-adapter` 是否适合简单 repo 更新提醒。
- 确认是否需要公网 webhook 入口，或能否只用轮询。

### Phase 1: preset 文件落地

- 保留 `config/github-watch-presets.json`。
- 若使用现成插件，记录 preset 到插件命令的映射。

### Phase 2: 轻量适配

仅在现成插件不支持 preset 时实现：

- `/ghwatch preset <id>`
- `/ghwatch preset list`
- `/ghwatch preset show <id>`

### Phase 3: 自写轮询版

仅在没有合适现成插件时考虑：

- GitHub REST API 轮询 commits/releases/tags。
- SQLite 存 baseline 和 delivery records。
- 支持群/私聊订阅。

## 风险点

- GitHub API 未认证时限流较低；高频轮询多个仓库可能触发限制。
- Webhook 需要公网入口和 secret 校验，部署复杂度更高。
- 群聊订阅公开仓库一般安全；私有仓库需要额外鉴权，不放入第一版。
- 现成插件如果权限过大，要单独评估配置和 token 风险。
