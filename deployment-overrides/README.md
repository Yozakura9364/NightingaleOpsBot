# 生产覆盖补丁

这里保存 AstrBot 插件市场安装的第三方插件所需的生产定制。第三方插件源码不直接纳入本仓库；插件更新后，在对应插件根目录执行补丁即可恢复定制。

`astrbot_plugin_parser/bilibili-title-intro.patch`：B站视频链接只发送标题和简介，不下载视频、不发送解析卡片，也不请求 AI 总结。

```bash
cd /opt/nightingale/astrbot/data/plugins/astrbot_plugin_parser
git apply -p1 /opt/nightingale/NightingaleOpsBot/deployment-overrides/astrbot_plugin_parser/bilibili-title-intro.patch
```
