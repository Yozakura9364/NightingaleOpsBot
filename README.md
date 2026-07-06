# NightingaleOpsBot

NightingaleOpsBot is a private operations bridge for Nightingale Silence services.

It contains:

- `runner/`: a localhost-only Node.js job runner.
- `astrbot-plugin/astrbot_plugin_ns_ops/`: the AstrBot plugin that maps QQ `/ns` commands to runner jobs.
- `astrbot-plugin/astrbot_plugin_risingstone_sign/`: the AstrBot plugin for private Rising Stones binding and daily sign-in.
- `scripts/`: local install and scheduled-task helpers.

The runner exposes only registered jobs. QQ input selects a job id or structured
payload; it is never appended to arbitrary shell commands.

## Local Layout

Expected sibling directories on this Windows host:

```text
H:\NightingaleSilenceWeb\
├── NightingaleOpsBot\
├── NightingaleSilenceWebV2\
└── astrbot\
```

## Local Config

Create a gitignored runtime config:

```powershell
New-Item -ItemType Directory -Force .\.local
Copy-Item .\runner\runner.local.example.json .\.local\runner.local.json
```

Then edit `.local\runner.local.json` and set a long random `NS_OPS_TOKEN`.
The same token must be configured in AstrBot's plugin config:

```text
H:\NightingaleSilenceWeb\astrbot\data\config\astrbot_plugin_ns_ops_config.json
```

Do not commit `.local\runner.local.json`.

## Start Runner

```powershell
.\runner\start-runner.ps1
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:18766/health
```

## Install AstrBot Plugin

```powershell
.\scripts\install-astrbot-plugin.ps1
```

Then restart AstrBot:

```powershell
Set-Location H:\NightingaleSilenceWeb\astrbot
docker compose restart astrbot
```

## Register Auto Start

```powershell
.\scripts\register-runner-task.ps1 -StartNow
```

This creates the Windows scheduled task:

```text
NightingaleSilence NS Ops Runner
```

It starts when the current Windows user logs in.

## QQ Commands

### Ops Commands

Read-only:

```text
/ns ping
/ns status
/ns logs astrbot
/ns v2 status
/ns v2 check
/ns v2 build
/ns armoire check-store
/ns armoire audit-store
/ns git status
/ns git diff
```

Confirmation required:

```text
/ns restart astrbot
/ns v2 deploy
/ns git commit <提交说明>
/ns git push
/ns file write <文件名.md> <内容>
/ns confirm <验证码>
```

## Safety Notes

- `git.commit` commits staged files only; it never runs `git add`.
- `git.push` requires a clean worktree and an existing upstream.
- `file.write` writes only below `.local\inbox` by default.
- `v2.deploy` is disabled until `NS_OPS_DEPLOY_NPM_SCRIPT` is configured and
  the matching npm script exists in the V2 project.

### Rising Stones Sign-In

This is a separate AstrBot plugin from `/ns`. It lets QQ users bind their own
Rising Stones cookie by private message, then run manual sign-in or receive daily
automatic sign-in results.

Private binding format:

```text
绑定石之家
COOKIE: ff14risingstones=...
USER_AGENT: Mozilla/5.0 ...
```

Private commands:

```text
石之家签到
石之家状态
解绑石之家
石之家帮助
```

Group messages only return guidance and never accept cookies:

```text
石之家签到
石之家帮助
```

Credential storage:

- Stored under the installed AstrBot plugin directory in `.local/`.
- SQLite stores encrypted cookie and user-agent values.
- The encryption key is generated locally as `.local/secret.key`.
- These runtime files are not committed to this repository.

The sign-in API flow is based on
`StarHeartHunt/ff14risingstone_sign_task` (MIT).
