# FFXIV Watch Plan

本文记录 FF14 官方新闻和商城更新提醒的长期计划。当前只做方案沉淀，尚未实现插件。

## 目标

做一个可开关、低打扰、可逐步扩展的 FF14 官方情报监听系统，用 QQ bot 推送官方新闻和各服商城更新。

核心目标：

- 监听国服和国际服官方新闻。
- 监听国服、台服、日服、韩服商城商品更新。
- 群/私聊可独立订阅和关闭，避免和其他群友机器人重复推送。
- 首次启动只建立基线，不推历史。
- 先只做提醒，不自动修改 V2 或 Armoire 数据。
- 后续再把商城更新接入 Armoire 候选生成流程。
- FF14 datamining 仓库更新提醒由通用 GitHub Watch 承接，本计划不内置 GitHub 仓库监听逻辑。

## 数据源

### 官方新闻

- 国服新闻：
  - RSSHub `/ff14/zh/news`
- 国服公告：
  - RSSHub `/ff14/zh/announce`
- 日服 Lodestone：
  - RSSHub `/ff14/global/jp/all`

当前新闻源不再维护自写爬虫，统一通过服务器 RSSHub 实例读取结构化 RSS/XML。FF14 watch 仍负责 QQ 订阅、来源开关、去重、基线和推送。

新闻分类建议：

- 全部新闻
- 维护
- 更新
- 活动
- 商城/道具
- 重要公告

### 商城更新

当前关注的商城页面：

- 国服盛趣商城：
  - `https://qu.sdo.com/product-detail/0d527e640bd3ada51565`
- 台服商城：
  - `https://www.ffxiv.com.tw/web/store/product_detail.aspx?id=F0068_251120152555`
- 日服 Online Store：
  - `https://store.finalfantasyxiv.com/ffxivstore/ja-jp/product/392`
- 韩服商城：
  - `https://www.ff14.co.kr/shop/home/detail/1687`

第一阶段只要求“有更新就推”，不判断是不是衣服、坐骑、宠物或动作。后续可以再加商品分类和 Armoire 候选识别。

不同商城的抓取难度预计不同：

- 台服和韩服详情页信息相对直接，适合优先做。
- 国服盛趣商城偏动态页面，需要先找列表/详情接口。
- 日服 Online Store 页面可能依赖前端数据或接口，需要探测 API 或使用浏览器抓取。

### Datamining 仓库更新

这部分不放在 FF14 Watch 插件内实现，统一交给通用 GitHub Watch。

通用预设见：

```text
docs/github-watch-plan.md
config/github-watch-presets.json
```

当前预设分组：

```text
ffxiv-datamining
```

该分组包括：

```text
InfSein/ffxiv-datamining-mixed
Ra-Workspace/ffxiv-datamining-ko
thewakingsands/ffxiv-datamining-tc
```

群友需要 datamining 更新提醒时，应该订阅 GitHub Watch 的 `ffxiv-datamining` preset，而不是订阅 FF14 Watch。

## 架构建议

建议做成独立插件，不塞进 `/ns` 运维入口：

```text
NightingaleOpsBot/
├── runner/
│   └── ffxivWatch.mjs
├── astrbot-plugin/
│   └── astrbot_plugin_ffxiv_watch/
│       ├── main.py
│       └── _conf_schema.json
└── .local/
    └── ffxiv-watch.sqlite3
```

理由：

- `/ns` 是管理员运维入口，权限更高，不适合直接给群友使用。
- FF14 情报提醒可能在群里订阅，权限模型和 `/ns` 不一样。
- 以后可以单独关停、迁移或扩展，不影响运维插件。

Runner 负责：

- 抓取各源。
- 解析成标准事件。
- 和 SQLite 里的已见记录做 diff。
- 返回新事件或状态。

AstrBot 插件负责：

- QQ 命令。
- 群/私聊订阅关系。
- 定时轮询。
- 去重推送。
- 开关管理。

## 命令设计

建议命令前缀使用 `/ff14watch`，避免和已有插件冲突。

基础命令：

```text
/ff14watch 帮助
/ff14watch 状态
/ff14watch 开
/ff14watch 关
/ff14watch 测试
```

订阅命令：

```text
/ff14watch 订阅 新闻
/ff14watch 订阅 商城
/ff14watch 取消 新闻
/ff14watch 取消 商城
/ff14watch 订阅列表
```

来源开关：

```text
/ff14watch 源
/ff14watch 源 cn-news on
/ff14watch 源 cn-news off
/ff14watch 源 jp-news on
/ff14watch 源 jp-news off
/ff14watch 源 cn-store on
/ff14watch 源 tw-store on
/ff14watch 源 jp-store on
/ff14watch 源 kr-store on
```

可选管理命令：

```text
/ff14watch 基线
/ff14watch 最近
/ff14watch 重扫
```

`基线` 用于只记录当前最新内容，不推历史。`重扫` 只建议管理员使用。

## 开关设计

为避免和其他群友机器人重复，开关需要分三层。

### 全局开关

控制插件是否工作：

```text
enabled: true | false
```

### 聊天窗口开关

每个群或私聊独立开关：

```text
scope_type: private | group
scope_id: QQ号或群号
enabled: true | false
```

### 来源开关

每个聊天窗口可以单独开启/关闭来源：

```text
cn-news
cn-notice
jp-news
cn-store
tw-store
jp-store
kr-store
```

示例场景：

- 某群已有 Lodestone 推送：关闭 `jp-news`，保留商城。
- 某群只想看商城：订阅 `商城`，不订阅 `新闻`。
- 私聊管理员想看全部：所有来源开启。
- Datamining 仓库更新提醒用 GitHub Watch 的 preset，不占用 FF14 Watch 来源开关。

## 去重策略

必须避免刷历史和重复推送。

规则：

- 首次启动只建立基线，不推历史。
- 每个来源生成稳定 `event_key`。
- 同一个 `event_key` 在同一个聊天窗口只推一次。
- 新闻标题修改但链接不变，默认不再次推送。
- 商城价格、上下架、分类变化可以生成独立变更事件。

事件 key 示例：

```text
news:cn:<article-id-or-url-hash>
news:jp:<article-id-or-url-hash>
store:cn:<product-id>
store:tw:F0068_251120152555
store:jp:392
store:kr:1687
store-change:jp:392:price:<hash>
```

SQLite 建议表：

```text
sources
events
subscriptions
deliveries
source_state
```

其中：

- `events` 保存源侧发现的事件。
- `deliveries` 保存每个聊天窗口已推送事件，防止重复。
- `source_state` 保存每个数据源最新抓取时间、错误状态和基线状态。

## 推送格式

新闻推送：

```text
FF14 新闻更新｜国际服
[维护] 全ワールド メンテナンス作業のお知らせ
日期：2026-xx-xx
链接：...
```

商城推送：

```text
FF14 商城更新｜韩服
新增商品：별빛 로브
分类：의상실 / 의상
价格：3,600 크리스탈
链接：...
```

变更推送：

```text
FF14 商城变更｜日服
商品：Far Eastern Schoolgirl's Attire
变化：价格 1,980 JPY -> 1,386 JPY
链接：...
```

第一版可以只发纯文本。图片和翻译等增强功能后续再加。

## 轮询和限频

建议默认：

- 新闻：每 30-60 分钟检查一次。
- 商城：每 3-6 小时检查一次。
- 单个来源失败后指数退避。
- 单次推送最多 5-10 条，超出部分合并摘要。

不要高频抓商城，避免给官方站点造成压力，也降低被风控概率。

## 实施阶段

### Phase 0: 数据源探针

先写探针，不写插件：

```text
tools/probe-ffxiv-watch-sources.mjs
```

当前探针命令：

```text
npm run probe:ffxiv-watch
npm run probe:ffxiv-watch -- --source=cn-news
npm run probe:ffxiv-watch -- --source=jp-news
npm run probe:ffxiv-watch -- --source=cn-store
npm run probe:ffxiv-watch -- --source=tw-store
npm run probe:ffxiv-watch -- --source=jp-store
npm run probe:ffxiv-watch -- --source=kr-store
```

目标：

- 确认每个新闻/商城源能抓到什么字段。
- 判断是否需要浏览器环境。
- 找到列表页、详情页或 API。
- 输出统一 JSON 样例。

截至 2026-07-07 的探针结果：

| 来源 | 当前状态 | 已确认字段/入口 | 后续处理 |
| ---- | -------- | -------------- | -------- |
| `cn-news` | 部分可用 | 官网 SPA 背后存在 `https://cqnews.web.sdo.com/api/news/newsList?gameCode=ff&CategoryCode=...`；`CategoryCode=7187&pageIndex=0&pageSize=10` 可返回置顶/头图新闻。 | 继续补普通新闻列表的 `CategoryCode`，或用浏览器探针确认页面实际请求。 |
| `jp-news` | 可用 | Lodestone HTML 可解析 `/lodestone/news/detail/...` 链接、标题和 URL。 | 后续增强发布时间和分类解析。 |
| `cn-store` | 详情 API 可用 | 盛趣商城详情页可从 `https://sqmallservice.u.sdo.com/api/ps/product/allInOne?skuId=...` 拿到商品名、价格、货币、图片、描述、上架状态。 | 继续寻找商品列表页或列表 API，才能判断“新商品”。 |
| `tw-store` | 详情 HTML 可用 | 详情页 HTML 可解析商品名和图片候选；当前样例可识别 `星芒長袍`。 | 继续寻找列表页或列表 API；价格字段需要增强。 |
| `jp-store` | 详情 HTML 可用 | 详情页 `<title>` 和 og 信息可解析商品名、描述、图片；当前样例可识别 `スターライトローブ`。 | 优先探测 `https://api.store.finalfantasyxiv.com/ffxivcatalog/api/` 相关接口，补列表和价格。 |
| `kr-store` | 暂不可直连 | 本机 Node fetch 失败，服务器 `curl` 20 秒超时。 | 后续考虑代理、浏览器环境或替代数据源；第一版可先不启用韩服源。 |

探针输出建议：

```json
{
  "source": "jp-news",
  "ok": true,
  "items": [
    {
      "id": "...",
      "title": "...",
      "url": "...",
      "publishedAt": "...",
      "category": "maintenance"
    }
  ]
}
```

### Phase 1: 新闻提醒 MVP

实现：

- 独立 AstrBot 插件。
- 订阅/取消/状态/测试命令。
- 国服新闻和 Lodestone 新闻监听。
- 首次启动基线，不推历史。

### Phase 2: 商城提醒 MVP

优先实现：

- 台服商城。
- 韩服商城。

然后补：

- 国服盛趣商城。
- 日服 Online Store。

第一版只推新增商品，不做商品属性识别。

### Phase 3: 商城变更检测

增加：

- 商品下架。
- 价格变化。
- 限时折扣。
- 分类变化。
- 商品详情变化。

### Phase 4: 接入 Armoire 候选

商城新增商品后，生成候选清单：

```text
/ff14watch armoire candidates
/ff14watch armoire preview <event-id>
```

候选只供管理员私聊使用，不自动写 V2 数据。

后续稳定后再考虑：

```text
/ff14watch armoire confirm <event-id>
```

确认后生成 `armoire-store-catalog` 或 corrections 修改，并运行校验。

## 风险点

- 官方页面结构变化会导致抓取失败。
- SPA 页面可能需要 Playwright 或找接口。
- 商城页面可能有地区、语言、Cookie、风控限制。
- 群里已有类似机器人，必须默认可关闭、可按源关闭。
- 首次部署如果没有基线保护，容易刷历史。
- 商城商品翻译和分类识别容易误判，第一版不要自动写入 V2 数据。
- Datamining 更新提醒由 GitHub Watch 承接；FF14 Watch 不重复实现，避免两套订阅系统冲突。

## 当前决策

- 石之家论坛不作为新闻源。
- 新闻源聚焦国服官网新闻页和国际服 Lodestone。
- 商城源覆盖国服、台服、日服、韩服商城。
- 商城更新不限定衣服，有更新就推。
- Datamining 更新提醒由通用 GitHub Watch 承接，预设分组为 `ffxiv-datamining`。
- 第一阶段以“提醒和开关”为核心，不做 Armoire 自动写入。
- 下一步优先做数据源探针，确认每个源的可抓字段和稳定入口。

## 2026-07-08 落地记录

已新增 AstrBot 插件：

```text
astrbot-plugin/astrbot_plugin_ffxiv_watch/
```

当前命令：

```text
/ff14watch 帮助
/ff14watch 订阅 新闻
/ff14watch 订阅 商城
/ff14watch 取消 新闻
/ff14watch 取消 商城
/ff14watch 订阅列表
/ff14watch 状态
/ff14watch 测试
/ff14watch 测试 jp-news
/ff14watch 源
/ff14watch 源 jp-news off
/ff14watch 源 jp-news on
/ff14watch 开
/ff14watch 关
/ff14watch 基线
```

当前启用源：

```text
cn-news
cn-notice
jp-news
cn-store
tw-store
jp-store
```

韩服商城暂不启用。原因是服务器和本机探测都不稳定，且用户已确认先不管韩服。

实现边界：

- 首次轮询只建立基线，不推历史。
- 群聊和私聊可分别订阅新闻、商城，并可单独关闭某个源。
- 新闻源只保留 `cn-news`、`cn-notice`、`jp-news`，分别映射 RSSHub `/ff14/zh/news`、`/ff14/zh/announce`、`/ff14/global/jp/all`。
- `rsshub_base_url` 默认 `http://rsshub:1200`，用于 AstrBot 与 RSSHub 同 Docker 网络部署。
- 商城第一版只监控当前已确认详情页的内容变更，尚未实现完整商品列表 / 新品发现。后续需要继续找国服、台服、日服的稳定列表 API。
- `/yoine 推送` 已加入 FF14 watch 菜单入口。
- 2026-07-08 已加入配图推送：默认 `include_images=true`，每条更新最多发送 1 张图片。图片由数据源返回的图片 URL 提供，发送失败不影响文字推送。
