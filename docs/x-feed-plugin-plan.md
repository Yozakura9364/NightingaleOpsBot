# X Feed Push Plugin Runbook

> 这份文档保留为 RSSHub 时代的历史方案，当前生产请改看 `docs/x-feed-twikit-runbook.md`。

## Current Status

`astrbot_plugin_x_feed` is deployed on the production AstrBot server.

Production translation is currently enabled:

```text
translate_enabled=true
translate_target_lang=简体中文
translate_provider_id=auto
translate_show_original=true
```

The working chain is:

```text
RSSHub /twitter/user/<handle>
  -> astrbot_plugin_x_feed polls RSS
  -> plugin stores last-seen state in SQLite
  -> AstrBot LLM translation if enabled
  -> QQ text/link push
  -> plugin downloads X images through proxy
  -> QQ local-image push
```

This is a best-effort, low-frequency watcher for a small number of X accounts.
It is not a real-time or guaranteed-delivery system.

## Runtime Paths

Server:

```text
/opt/nightingale/astrbot/docker-compose.yml
/opt/nightingale/astrbot/rsshub.env
/opt/nightingale/astrbot/xproxy-relay.py
/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed
/opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed
```

Local Windows repo:

```text
H:\NightingaleSilenceWeb\NightingaleOpsBot\astrbot-plugin\astrbot_plugin_x_feed
H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\start-xproxy-tunnel.ps1
H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\status-xproxy-tunnel.ps1
H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\stop-xproxy-tunnel.ps1
```

Runtime data:

```text
astrbot_plugin_x_feed/.local/x_feed.sqlite3
astrbot_plugin_x_feed/.local/images/
```

Do not commit `.local/` files.

## Auth And Proxy

RSSHub uses `TWITTER_AUTH_TOKEN` from:

```text
/opt/nightingale/astrbot/rsshub.env
```

Do not print or paste the token in chat, logs, commits, or docs. The token is a
logged-in X session token and may expire or trigger X risk checks.

The official X API key path was tested but returned HTTP 402 because paid API
access was not available. The current working path is RSSHub's web/API route
with `TWITTER_AUTH_TOKEN`.

RSSHub and image downloads need proxy access. The current proxy chain is:

```text
RSSHub / AstrBot container
  -> 172.19.0.1:7890 on Docker host
  -> nightingale-xproxy-relay.service
  -> SSH reverse tunnel 127.0.0.1:17890
  -> Windows local proxy 127.0.0.1:7890
```

Server relay:

```bash
systemctl status nightingale-xproxy-relay.service --no-pager -l
```

Local tunnel status:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\status-xproxy-tunnel.ps1
```

Start local tunnel:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\start-xproxy-tunnel.ps1
```

Stop local tunnel:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\stop-xproxy-tunnel.ps1
```

The Windows local proxy on `127.0.0.1:7890` must be running before starting the
tunnel.

## QQ Commands

Commands are scoped to the current conversation.

Private chat subscription pushes to private chat:

```text
/x订阅 @FF_XIV_EN
```

Group subscription pushes to that group:

```text
/x订阅 @FF_XIV_EN
```

Common commands:

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

If a handle was subscribed in private chat and should move to a group, run
`/x订阅 @handle` in the group. Private and group subscriptions are stored
separately.

## Plugin Config

AstrBot config schema fields and defaults:

```json
{
  "rsshub_base_url": "http://rsshub:1200",
  "enabled": true,
  "poll_interval_minutes": 10,
  "max_items_per_poll": 3,
  "initial_backfill_items": 1,
  "include_images": true,
  "max_images_per_post": 1,
  "image_proxy_url": "http://172.19.0.1:7890",
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

Keep `poll_interval_minutes` at 10 minutes or higher for cookie-based access.
Do not subscribe many X accounts at once.

Translation is disabled by schema default, but production currently has
`translate_enabled=true`. When translation is enabled, the plugin uses AstrBot
LLM providers in this order:

1. `translate_provider_id`
2. current conversation provider
3. first available provider

If translation times out or fails, the plugin sends the original post text and
continues with image sending.

To toggle translation on the production server, edit only the plugin config file
and restart AstrBot:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json')
data = json.loads(p.read_text(encoding='utf-8-sig'))
data['translate_enabled'] = True
p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
PY
cd /opt/nightingale/astrbot && docker compose restart astrbot
```

Use `False` instead of `True` to disable it. Check the live state from QQ with:

```text
/x翻译状态
```

## Validation

Check RSSHub:

```bash
curl -I http://127.0.0.1:1200/
curl -sS -L --max-time 60 http://127.0.0.1:1200/twitter/user/FF_XIV_EN | head
```

Check plugin loading:

```bash
docker logs --since 2m astrbot 2>&1 | grep astrbot_plugin_x_feed
```

Check feed parsing from AstrBot container:

```bash
docker exec astrbot python3 -m py_compile \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/main.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/feed_client.py \
  /AstrBot/data/plugins/astrbot_plugin_x_feed/storage.py
```

QQ-side smoke test:

```text
/x推送测试 @FF_XIV_EN
```

Expected result:

1. Text/link message.
2. If translation is enabled and an LLM provider is available, the text includes
   both `原文` and the configured target language.
3. A second image message if the X post contains an image.

## Troubleshooting

### RSSHub returns `ConfigNotFoundError`

`TWITTER_AUTH_TOKEN` is missing from the RSSHub container.

Check only variable names, not values:

```bash
docker exec rsshub sh -lc 'env | cut -d= -f1 | grep ^TWITTER_ | sort'
```

### RSSHub route times out

The proxy chain is probably down.

Check:

```bash
systemctl is-active nightingale-xproxy-relay.service
```

On Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File H:\NightingaleSilenceWeb\NightingaleOpsBot\scripts\status-xproxy-tunnel.ps1
```

### Text sends but image does not

The plugin should download images through `image_proxy_url` and send local image
files. Check AstrBot logs:

```bash
docker logs --since 10m astrbot 2>&1 | grep -Ei 'x_feed|image|Traceback|ERROR|WARNING' | tail -200
```

If the error mentions `pbs.twimg.com`, the image proxy/tunnel is not working.
If the error is from NapCat or OneBot after local image conversion, inspect
NapCat logs.

### Translation does not appear

Check `/x翻译状态` first. If translation is enabled but no translation appears,
verify AstrBot has at least one usable LLM provider. Translation failures are
logged as warnings and fall back to the original post text.

Relevant logs:

```bash
docker logs --since 10m astrbot 2>&1 | grep -Ei 'X feed translation|astrbot_plugin_x_feed|Traceback|ERROR|WARNING' | tail -200
```

### X login expires

Refresh `TWITTER_AUTH_TOKEN` from a logged-in X browser session and update:

```powershell
ssh -t root@100.67.17.31 /opt/nightingale/astrbot/set-rsshub-twitter-auth-token.sh
```

Use a low-value read-only X account where possible. Avoid using an important
main account if a spare account is available.

## Deployment Notes

Before changing production plugin code:

```bash
mkdir -p /opt/nightingale/backups
tar -czf /opt/nightingale/backups/x-feed-before-sync-$(date +%Y%m%d-%H%M%S).tgz \
  /opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_x_feed \
  /opt/nightingale/astrbot/data/plugins/astrbot_plugin_x_feed 2>/dev/null || true
```

After syncing:

```bash
cd /opt/nightingale/astrbot
docker compose restart astrbot
docker logs --since 2m astrbot 2>&1 | grep -Ei 'x_feed|Traceback|ERROR|WARNING' | tail -200
```

Do not overwrite `.local/x_feed.sqlite3` or `.local/images/` during sync.

Before changing production plugin config:

```bash
cp /opt/nightingale/astrbot/data/config/astrbot_plugin_x_feed_config.json \
  /opt/nightingale/backups/astrbot_plugin_x_feed_config-before-change-$(date +%Y%m%d-%H%M%S).json
```

## Risks

- `TWITTER_AUTH_TOKEN` can expire or be invalidated by X risk checks.
- Low polling intervals can increase risk of rate limits or account checks.
- The local Windows proxy and SSH tunnel are now part of the runtime chain.
- Translation uses LLM provider quota and can slow one poll cycle if the provider
  is slow or unavailable.
- Image sending depends on downloading media before QQ/NapCat sends it.
- X/RSSHub behavior can change without code changes in this repository.
