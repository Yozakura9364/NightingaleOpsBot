from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable

from .feed_client import FeedItem, normalize_handle

try:
    from twikit import Client
except Exception:  # pragma: no cover - runtime dependency
    Client = None


@dataclass(frozen=True)
class TwikitSettings:
    cookies_file: Path
    locale: str
    proxy_url: str
    timeline_count: int


class TwikitFeedClient:
    def __init__(self, settings: TwikitSettings):
        self.settings = settings
        self._client: Any | None = None
        self._cookie_mtime_ns: int | None = None
        self._lock = asyncio.Lock()

    async def fetch_user_feed(self, handle: str) -> list[FeedItem]:
        normalized = normalize_handle(handle)
        client = await self._ensure_client()

        try:
            user = await client.get_user_by_screen_name(normalized)
            tweets = await client.get_user_tweets(user.id, "Tweets", count=self.settings.timeline_count)
            items = [self._tweet_to_feed_item(normalized, tweet) for tweet in self._coerce_items(tweets)]
            await self._persist_cookies(client)
            return [item for item in items if item.item_id or item.link]
        except KeyError as error:
            # Twikit occasionally raises KeyError on empty/odd timelines.
            if str(error).strip("'\"") in {"entries", "moduleItems", "moduleItem"}:
                return []
            raise RuntimeError(f"Twikit 返回了不完整时间线数据：{error}") from error
        except Exception as error:
            raise RuntimeError(self._describe_fetch_error(error)) from error

    async def _ensure_client(self):
        if Client is None:
            raise RuntimeError("Twikit 依赖未安装。")

        cookies_file = self.settings.cookies_file
        if not cookies_file.exists():
            raise RuntimeError(f"Twikit cookies 文件不存在：{cookies_file}")

        mtime_ns = cookies_file.stat().st_mtime_ns
        async with self._lock:
            if self._client is not None and self._cookie_mtime_ns == mtime_ns:
                return self._client

            client = self._create_client()
            cookies = self._read_cookies(cookies_file)
            if not cookies:
                raise RuntimeError("Twikit cookies 文件为空或格式无法识别。")
            if not str(cookies.get("auth_token", "")).strip():
                raise RuntimeError("Twikit cookies 缺少 auth_token，当前登录态不可用。")
            client.set_cookies(cookies, clear_cookies=True)
            self._client = client
            self._cookie_mtime_ns = mtime_ns
            return self._client

    def _create_client(self):
        kwargs: dict[str, Any] = {"language": self.settings.locale or "en-US"}
        if self.settings.proxy_url:
            kwargs["proxy"] = self.settings.proxy_url
        return Client(**kwargs)

    @staticmethod
    def _read_cookies(path: Path) -> dict[str, str]:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return TwikitFeedClient._parse_cookie_header(text)

        if isinstance(payload, dict):
            return {str(key): str(value) for key, value in payload.items() if str(value).strip()}

        if isinstance(payload, list):
            cookies: dict[str, str] = {}
            for item in payload:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                value = str(item.get("value", "")).strip()
                if name and value:
                    cookies[name] = value
            return cookies

        return {}

    @staticmethod
    def _parse_cookie_header(text: str) -> dict[str, str]:
        cookies: dict[str, str] = {}
        for part in text.replace("\n", ";").split(";"):
            segment = part.strip()
            if not segment or "=" not in segment:
                continue
            name, value = segment.split("=", 1)
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies[name] = value
        return cookies

    async def _persist_cookies(self, client) -> None:
        try:
            cookies = client.get_cookies()
            if not isinstance(cookies, dict) or not cookies:
                return
            self.settings.cookies_file.parent.mkdir(parents=True, exist_ok=True)
            self.settings.cookies_file.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._cookie_mtime_ns = self.settings.cookies_file.stat().st_mtime_ns
        except Exception:
            return

    @staticmethod
    def _coerce_items(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
            return list(value)
        return [value]

    def _tweet_to_feed_item(self, handle: str, tweet: Any) -> FeedItem:
        item_id = self._pick_first(tweet, "id", "rest_id", default="")
        text = self._pick_first(tweet, "full_text", "text", default="")
        created_at = self._format_datetime(
            self._pick_first(tweet, "created_at_datetime", "created_at", default="")
        )
        link = self._tweet_link(handle, item_id, tweet)
        summary = str(text or "").strip()

        return FeedItem(
            item_id=str(item_id or link or summary[:80]).strip(),
            title=summary,
            link=link,
            published_at=created_at,
            summary=summary,
            image_urls=self._extract_image_urls(tweet),
        )

    @staticmethod
    def _pick_first(source: Any, *names: str, default: Any = "") -> Any:
        for name in names:
            value = getattr(source, name, None)
            if value not in (None, ""):
                return value
            if isinstance(source, dict):
                value = source.get(name)
                if value not in (None, ""):
                    return value
        return default

    @staticmethod
    def _tweet_link(handle: str, item_id: Any, tweet: Any) -> str:
        explicit = TwikitFeedClient._pick_first(tweet, "url", default="")
        if explicit:
            return str(explicit).strip()
        item = str(item_id or "").strip()
        return f"https://x.com/{handle}/status/{item}" if item else ""

    def _extract_image_urls(self, tweet: Any) -> list[str]:
        media_items = self._pick_first(tweet, "media", default=[])
        if not media_items:
            return []

        urls: list[str] = []
        for media in self._coerce_items(media_items):
            candidates = [
                self._pick_first(media, "media_url_https", "media_url", "url", "expanded_url", default=""),
            ]
            if isinstance(media, dict):
                legacy_url = media.get("legacy", {}).get("media_url_https") if isinstance(media.get("legacy"), dict) else ""
                if legacy_url:
                    candidates.append(legacy_url)
            for candidate in candidates:
                url = str(candidate or "").strip()
                if url.startswith(("http://", "https://")) and url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _format_datetime(value: Any) -> str:
        if isinstance(value, datetime):
            return value.astimezone().isoformat(timespec="seconds")

        raw = str(value or "").strip()
        if not raw:
            return ""

        for parser in (
            lambda text: datetime.fromisoformat(text.replace("Z", "+00:00")),
            lambda text: datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y"),
        ):
            try:
                return parser(raw).astimezone().isoformat(timespec="seconds")
            except ValueError:
                continue
        return raw

    def _describe_fetch_error(self, error: Exception) -> str:
        details = f"{type(error).__name__}: {error}"
        lowered = details.lower()

        if "couldn't get key_byte indices" in lowered:
            return (
                "Twikit 与当前 X 页面协议不兼容（KEY_BYTE 解析失败）。"
                "这通常不是 cookie 问题，而是 Twikit 版本断了，需要更新服务端 Twikit。"
            )

        if any(
            token in lowered
            for token in (
                "401",
                "403",
                "unauthorized",
                "forbidden",
                "csrf",
                "auth_token",
                "x-csrf-token",
                "login",
                "cookie",
                "cookies",
            )
        ):
            return "X 登录态失效或 cookies 不可用，请重新导出并更新 x_cookies.json。"

        if any(
            token in lowered
            for token in (
                "proxyerror",
                "remoteprotocolerror",
                "readtimeout",
                "connecttimeout",
                "timed out",
                "server disconnected without sending a response",
                "all connection attempts failed",
                "connection refused",
                "connection reset",
                "network is unreachable",
                "no route to host",
                "temporary failure in name resolution",
                "name or service not known",
                "cannot connect to host",
            )
        ):
            if self.settings.proxy_url:
                return (
                    "Twikit 无法通过代理连到 X。"
                    f"请检查代理地址 {self.settings.proxy_url}、本机 7890 端口和 Tailscale 是否在线。"
                )
            return "Twikit 直连 X 失败，请检查服务器网络或改为可用代理。"

        return f"Twikit 抓取失败：{details}"
