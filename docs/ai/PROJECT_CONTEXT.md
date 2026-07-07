# NightingaleOpsBot 项目上下文

本文档汇总 `AGENTS.md` 和 `PUBLICPROMPT.md` 中对本项目长期有效的规则，供后续维护和 AI 协作时快速确认项目边界。

## 项目范围

- 本机仓库根目录：`H:\NightingaleSilenceWeb\NightingaleOpsBot`。
- 服务器源码目录：`/opt/nightingale/NightingaleOpsBot`。
- 本项目是夜莺不语 QQ Bot 和运维自动化项目。
- 本项目与 `NightingaleSilenceWebV2` 分离；Bot 代码不要移动到 V2 项目中。

主要职责：

- 通过 QQ Bot 运维和观察夜莺不语相关服务与服务器状态。
- 通过 AstrBot 插件提供受控的服务器运维命令。
- 推送 FF14 新闻、数据源更新、GitHub 更新、RSSHub/X 动态和日程提醒。
- 提供石之家、盛趣积分商城等私聊账号绑定与自动签到能力。

## 运行布局

本机 Windows 目录：

```text
H:\NightingaleSilenceWeb\
├── NightingaleOpsBot\
├── NightingaleSilenceWebV2\
└── astrbot\
```

服务器目录：

```text
/opt/nightingale/
├── NightingaleOpsBot/
├── astrbot/
└── backups/
```

当前生产服务主要包括 AstrBot、NapCat、RSSHub、GsCore 和 NightingaleOpsBot runner。

## AI 工作流程

- 每次开始任务前，先确认当前 workspace、repository 和项目根目录。
- 修改文件前，先读取当前项目适用的 `AGENTS.md`。
- 如果后续子目录出现更近层级的 `AGENTS.md`，涉及该目录的任务还要读取子目录规则。
- `AGENTS.md` 是本项目长期规则来源；`PUBLICPROMPT.md` 是用户全局偏好，不覆盖项目级规则。
- 完整功能、重构、删除代码、跨文件架构修改和跨项目联调，需要先做只读计划并等待确认。
- 小范围明确修复可以在读取相关文件后直接实现。
- 不要把其他 Nightingale 项目的业务规则、目录结构或组件习惯套用到本项目。

## 编码规则

- 本仓库文本文件统一使用 UTF-8 无 BOM。
- 不要保存为 ANSI、GBK、UTF-8 with BOM 或 UTF-16。
- Windows PowerShell 5.1 下处理中文、日文、韩文文本时，必须显式设置 UTF-8 读写和管道编码。
- 手工改源码优先使用 `apply_patch`。如脚本必须写文件，也要显式写入 UTF-8 无 BOM。

## 文档规则

- 长期规则写入 `AGENTS.md` 或 `docs/ai/`。
- 可复用流程写入 `docs/` 下的专题 runbook。
- 不要把一次性 bug、临时需求、密钥、猜测或聊天中的短期决策写入长期文档。
- 文档必须符合当前仓库结构和生产部署状态。

当前重要文档：

- `docs/ns-health-runbook.md`
- `docs/tencent-cloud-traffic-runbook.md`
- `docs/ffxiv-watch-plan.md`
- `docs/github-watch-plan.md`
- `docs/x-feed-plugin-plan.md`
- `docs/share-link-resolver-runbook.md`

## 安全边界

- 不要打印或提交 token、cookie、二维码登录密钥、`secret.key`、验证码、完整运行配置等敏感信息。
- 插件运行数据通常在 `.local/` 下，部署时不要覆盖。
- 石之家和盛趣签到属于用户私有凭据流程。群聊只返回引导，不接受凭据。
- runner 只暴露已注册 job。QQ 输入不能拼接成任意 shell 命令。
- 部署、重启、git commit/push、写文件等危险操作必须保持 allowlist 和确认机制。

## Git 和部署

- 修改前检查 `git status --short`。本仓库经常有多个 Bot 功能的未提交改动。
- 只暂存当前任务相关文件或 hunk。
- 覆盖生产插件前，先在 `/opt/nightingale/backups` 下创建带时间戳的备份。
- 只同步当前任务需要的文件。
- 部署后验证语法和服务日志。
- 不要擅自 commit 或 push，除非用户明确要求或确认。

## 验证方式

按修改层级选择验证：

- Python AstrBot 插件：运行 `python -m py_compile ...`。
- runner 修改：运行 Node 语法检查、构建检查或目标 job 调用。
- 服务器插件部署：容器内运行 `python3 -m py_compile`，重启 AstrBot，再检查 `docker logs`。
- QQ 命令：涉及风控、登录、凭据时优先私聊低频测试。

如果无法验证，必须说明原因和残余风险。
