# QQ Share Link Resolver Runbook

## Current Status

`astrbot_plugin_share_link_resolver` is deployed on the production AstrBot server.

The plugin's primary purpose is narrow:

```text
QQ mobile share card that is hard to open/read on desktop
  -> extract the original URL from the QQ JSON card
  -> clean noisy share parameters where safe
  -> send a normal clickable link back to QQ
```

Image/card rendering is a convenience layer. It should stay best-effort and
must not turn this plugin into a full RSS subscription system, crawler, or
high-fidelity screenshot service.

## Runtime Paths

Server:

```text
/opt/nightingale/astrbot/data/config/astrbot_plugin_share_link_resolver_config.json
/opt/nightingale/astrbot/data/plugins/astrbot_plugin_share_link_resolver
/opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_share_link_resolver
```

Local Windows repo:

```text
H:\NightingaleSilenceWeb\NightingaleOpsBot\astrbot-plugin\astrbot_plugin_share_link_resolver
```

Runtime data:

```text
astrbot_plugin_share_link_resolver/.local/cards/
astrbot_plugin_share_link_resolver/.local/emoji/
astrbot_plugin_share_link_resolver/.local/fonts/
astrbot_plugin_share_link_resolver/.local/nga_cookies.json
```

Do not commit `.local/` files. Treat `nga_cookies.json` as a logged-in browser
session and do not print it in chat or logs.

## Commands

Common commands:

```text
/分享链接帮助
/链接解析状态
/转图 <链接>
/链接转图 <链接>
/nga状态
```

Automatic mode handles QQ JSON share card messages. By default it does not parse
plain text links, to avoid duplicate replies when someone simply posts a normal
URL.

## Configuration

Important options in AstrBot plugin config:

```text
enabled=true
allow_private=true
allow_groups=true
include_plain_text=false
max_links_per_message=1
clean_share_params=true
send_images=true
render_card_images=true
card_width=760
card_max_height=2200
exclude_media_links=true
stop_after_resolved=true
debug_log=false
```

Keep `include_plain_text=false` unless there is a specific reason to parse
ordinary text URLs. Enabling it globally can make the bot reply to too many
normal messages.

`stop_after_resolved=true` is intentional. It reduces duplicate handling and
prevents later plugins from complaining about unsupported JSON-card messages.

## Platform Handling

The plugin extracts URLs from nested QQ JSON payloads using field-name hints
such as `url`, `link`, `jump`, `target`, `source`, `web`, `page`, and `share`.
It filters obvious media/CDN links by default.

Current platform-specific behavior:

- Xiaohongshu: rewrites mobile discovery item links into a PC-share style link
  while preserving required `xsec_token` data.
- Miyoushe, TapTap, Xiaoheihe, and KuroBBS: rich article extraction. The
  plugin tries lightweight public web/API routes to extract the main post text,
  author metadata, and several inline images, then renders a main-post card.
  Failure falls back to the generic QQ share-card image.
- NGA: can fetch the linked thread page and generate a readable preview card.
  Current rendering supports main post, page-highlighted hot replies when
  present, basic metadata, NGA smiley images, and attachment images.
- Miyoushe: rewrites mobile article links whose article id is stored in the
  `#/article/<id>` fragment into PC article links such as
  `https://www.miyoushe.com/ys/article/<id>`.
- Weibo and common web pages: best-effort title/description/cover extraction.
- Other platforms: falls back to a generic link card or plain link.

If card fetching or rendering fails, the plugin should degrade to a normal link
instead of producing noisy user-facing errors.

Text emoji in rendered cards are detected and drawn as cached Twemoji images
under `.local/emoji/`. If the emoji CDN is unavailable or a glyph is not covered,
the renderer falls back to normal font drawing.

## Rich Card Plan

The rich-card plan keeps this plugin focused on QQ share-card recovery, but
allows cheap main-post extraction for platforms where a stable public web/API
route is available.

First batch:

- Miyoushe: `getPostFull` article payload.
- TapTap: topic detail payload.
- Xiaoheihe: signed share-data payload when the share URL exposes a usable
  `link_id`.

Current first-batch verification:

- Miyoushe is verified on the production container with a real article link.
- TapTap is wired as best-effort, but the current production container receives
  HTTP 405 from the tested topic detail endpoint and therefore falls back to the
  generic card.
- Xiaoheihe uses the current web `link/tree` route with generated request
  signing. It also cleans App-local optimizer image paths and maps common
  `[cube_...]` markers to simple emoji.
- KuroBBS / 库街区 uses the anonymous `getPostDetail` route to render the main
  post title, author, metadata, text, and several images.

Second batch candidates:

- Weibo: possible through mobile APIs, but cookie and risk-control behavior are
  less stable.
- Skland: the web app appears to use `https://zonai.skland.com/web/v2/item`,
  but direct anonymous requests currently return a generic request error from
  the server. Treat it as signed/request-constrained for now unless a stable
  lightweight route is confirmed.

Generic-only for now:

- Xiaohongshu, Skland, Mihuashi, and Huajia should remain
  QQ-card/title/cover based unless a stable lightweight route is confirmed.

Rich cards must remain best-effort:

- Limit text length, image count, and final image height.
- Preserve the normal clickable link response.
- Degrade to the generic card when extraction, parsing, image loading, or
  platform APIs fail.
- Do not require logged-in cookies for the first-batch extractors.

## Scope Boundary

Keep this plugin focused on passive share-card recovery.

In scope:

- Recover original URLs from QQ share cards.
- Make links usable on desktop QQ.
- Clean obvious tracking/share parameters.
- Generate a lightweight preview image when the data is already available or
  cheap to fetch.
- Preserve readable fallbacks when a platform blocks fetching.

Out of scope:

- RSS or scheduled subscription polling.
- Real-time update push.
- Full social-platform crawling.
- High-fidelity screenshots that must match another bot pixel-for-pixel.
- Solving anti-bot systems, CAPTCHA flows, or unstable private APIs.
- Long-term authenticated scraping for Xiaohongshu, Weibo, or NGA beyond small
  best-effort helpers.

For scheduled updates, use or build a separate feed/watch plugin. RSSHub can be
used as an upstream feed generator, but it should not be folded into this
share-card resolver.

## NGA Notes

NGA support is intentionally best-effort.

Current useful behavior:

- Reads normal thread HTML with optional logged-in cookie.
- Uses `hightlight_for_0` hot reply blocks when NGA includes them in the page.
- Falls back to floor replies when hot replies are unavailable.
- Maps common `[s:ac:...]` and `[s:a2:...]` smileys to NGA static smiley images.
- Extracts `[img]...[/img]` and attachment loader image URLs.

Known limitations:

- Some HTML responses expose only anonymous `UID:<id>` names and no avatar.
- Hot-reply score fields may not equal the visible like count used by other
  clients or bots.
- Reply selection and metadata depend on what the fetched HTML contains.
- The image is a readable card, not a screenshot-equivalent page render.

Use `/nga状态` to check whether the cookie file exists and whether a simple NGA
fetch still looks logged in. The command prints only status, not cookie values.

## Deployment And Validation

Before overwriting production plugin files, create a backup:

```bash
mkdir -p /opt/nightingale/backups
tar -czf /opt/nightingale/backups/share-link-resolver-before-sync-$(date +%Y%m%d-%H%M%S).tgz \
  /opt/nightingale/NightingaleOpsBot/astrbot-plugin/astrbot_plugin_share_link_resolver \
  /opt/nightingale/astrbot/data/plugins/astrbot_plugin_share_link_resolver
```

After syncing changed files:

```bash
docker exec astrbot python3 -m py_compile \
  /AstrBot/data/plugins/astrbot_plugin_share_link_resolver/main.py \
  /AstrBot/data/plugins/astrbot_plugin_share_link_resolver/web_card.py \
  /AstrBot/data/plugins/astrbot_plugin_share_link_resolver/card_renderer.py

cd /opt/nightingale/astrbot
docker compose restart astrbot

docker logs --since 2m astrbot 2>&1 | grep -Ei \
  'share_link_resolver|Traceback|ERROR|aiocqhttp|适配器已连接' | tail -200
```

For a QQ-side smoke test, send a mobile QQ share card in private chat. The bot
should send a normal text link first and, if rendering succeeds, a preview image
second.

## Troubleshooting

No reply:

- Check whether the message reached AstrBot as `[ComponentType.Json]`.
- If the user sent a plain URL, remember `include_plain_text=false` ignores it.
- Check `enabled`, `allow_private`, `allow_groups`, group allow/block lists, and
  `max_links_per_message`.

Only text link, no image:

- Check `render_card_images` and `send_images`.
- Check whether the card image was created under `.local/cards/`.
- Check NapCat logs for image send failures.

NGA card lacks names or avatars:

- This is usually a source-data limitation. The HTML may only expose `UID:<id>`
  and no avatar field.
- Do not expand this plugin into a heavy browser crawler just to fill profile
  data.

NGA hot replies missing:

- The fetched HTML may not contain `hightlight_for_0`.
- The plugin should fall back to ordinary floor preview in that case.
