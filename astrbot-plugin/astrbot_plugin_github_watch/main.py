from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Iterable

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .delivery import deliver_once
from .github_client import GitHubClient, GitHubCommit, GitHubRelease, GitHubTag
from .storage import GitHubWatchStore, Subscription


COMMANDS = ("ghwatch", "githubwatch")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EVENT_ALIASES = {
    "push": "push",
    "commit": "push",
    "commits": "push",
    "提交": "push",
    "release": "release",
    "releases": "release",
    "版本": "release",
    "发布": "release",
    "tag": "tag",
    "tags": "tag",
    "标签": "tag",
}
ON_WORDS = {"on", "开", "开启", "启用", "true", "1"}
OFF_WORDS = {"off", "关", "关闭", "停用", "false", "0"}


def _split_ids(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip()}


def _event_origin(event: AstrMessageEvent) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "").strip()


def _target_kind(event: AstrMessageEvent) -> str:
    return "group" if str(event.get_group_id() or "").strip() else "private"


def _strip_command(text: str, commands: tuple[str, ...]) -> str:
    first_line = str(text or "").strip().splitlines()[0].strip() if str(text or "").strip() else ""
    if first_line.startswith("/"):
        first_line = first_line[1:].lstrip()
    for command in commands:
        if first_line == command:
            return ""
        if first_line.startswith(command + " "):
            return first_line[len(command) :].strip()
    return first_line


def _clamp(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 16)] + "\n...[已截断]"


def _normalize_repo(value: str) -> str:
    repo = str(value or "").strip().strip("/")
    if repo.startswith("https://github.com/"):
        repo = repo.removeprefix("https://github.com/").strip("/")
        repo = "/".join(repo.split("/")[:2])
    if not REPO_RE.match(repo):
        raise ValueError("仓库格式不对，请使用 owner/repo，例如 InfSein/ffxiv-datamining-mixed。")
    owner, name = repo.split("/", 1)
    return f"{owner}/{name}"


def _parse_action(value: str) -> bool:
    action = str(value or "").strip().lower()
    if action in ON_WORDS:
        return True
    if action in OFF_WORDS:
        return False
    raise ValueError("开关只能写 on/off、开/关。")


def _short_error(error: Exception | str) -> str:
    text = str(error).replace("\n", " ").strip()
    text = re.sub(r'\{"message":"([^"]+)".*', r"\1", text)
    text = text.replace("GitHub API HTTP 404: ", "404 ")
    text = text.replace("GitHub API HTTP 403: ", "403 ")
    text = text.replace("GitHub API request failed: ", "")
    if len(text) > 140:
        return text[:137] + "..."
    return text or "未知错误"


def _state_key(event_type: str, repo: str, branch: str = "") -> str:
    if event_type == "push":
        return f"push:{repo}:{branch}"
    return f"{event_type}:{repo}"


def _help_text() -> str:
    return "\n".join(
        [
            "GitHub 仓库更新提醒",
            "",
            "/ghwatch preset",
            "/ghwatch preset show ffxiv-datamining",
            "/ghwatch preset ffxiv-datamining",
            "/ghwatch 订阅 owner/repo [branch]  （不写 branch 时自动使用默认分支）",
            "/ghwatch 取消 owner/repo",
            "/ghwatch 列表",
            "/ghwatch 状态",
            "/ghwatch 检查",
            "/ghwatch 测试 owner/repo",
            "/ghwatch 事件 owner/repo push on",
            "/ghwatch 事件 owner/repo release off",
            "/ghwatch 事件 owner/repo tag off",
            "/ghwatch 开",
            "/ghwatch 关",
            "",
            "首次订阅只建立基线，不会推历史提交。默认监听 push / release / tag。",
        ]
    )


@register(
    "astrbot_plugin_github_watch",
    "NightingaleSilence",
    "Generic GitHub repository update watcher with preset groups.",
    "0.1.0",
)
class GitHubWatchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(__file__).resolve().parent / ".local"
        self.store = GitHubWatchStore(self.data_dir)
        self.plugin_dir = Path(__file__).resolve().parent
        self.presets = self._load_presets()
        self.max_output_chars = int(self.config.get("max_output_chars", 3000) or 3000)
        self._poll_task: asyncio.Task | None = None
        self._poll_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._poll_task = asyncio.create_task(self._poll_loop())
            logger.info("GitHub watch poll loop started.")

    async def terminate(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    def _poll_interval_seconds(self) -> int:
        return max(10, int(self.config.get("poll_interval_minutes", 60) or 60)) * 60

    def _startup_delay_seconds(self) -> int:
        return max(5, int(self.config.get("startup_delay_seconds", 45) or 45))

    def _request_timeout_seconds(self) -> int:
        return max(5, int(self.config.get("request_timeout_seconds", 20) or 20))

    def _failure_notice_threshold(self) -> int:
        return max(1, int(self.config.get("failure_notice_threshold", 3) or 3))

    def _client(self) -> GitHubClient:
        return GitHubClient(
            token=str(self.config.get("github_token", "") or ""),
            timeout_seconds=self._request_timeout_seconds(),
            proxy_url=str(self.config.get("proxy_url", "") or ""),
        )

    def _load_presets(self) -> list[dict]:
        path = self.plugin_dir / "presets.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            logger.warning("GitHub watch presets.json not found.")
            return []
        except Exception as error:
            logger.warning("GitHub watch presets.json load failed: %s", error)
            return []
        presets = payload.get("presets", []) if isinstance(payload, dict) else []
        return [preset for preset in presets if isinstance(preset, dict)]

    def _preset_by_id(self, preset_id: str) -> dict | None:
        wanted = str(preset_id or "").strip()
        return next((preset for preset in self.presets if str(preset.get("id") or "") == wanted), None)

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id())
        manager_ids = _split_ids(self.config.get("manager_user_ids", ""))
        if sender in manager_ids:
            return True
        try:
            astrobot_config = self.context.get_config(event.unified_msg_origin)
            admin_ids = {str(item) for item in astrobot_config.get("admins_id", [])}
            if sender in admin_ids:
                return True
        except Exception:
            pass
        if not self.config.get("manage_requires_admin", True):
            return True
        return bool(event.is_admin())

    async def _poll_loop(self) -> None:
        await asyncio.sleep(self._startup_delay_seconds())
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("GitHub watch poll loop error: %s", error)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _poll_once(self) -> None:
        async with self._poll_lock:
            subscriptions = self.store.list_enabled_subscriptions()
            by_repo: dict[str, list[Subscription]] = {}
            for subscription in subscriptions:
                by_repo.setdefault(subscription.repo, []).append(subscription)

            client = self._client()
            for repo, repo_subscriptions in by_repo.items():
                branches = sorted(
                    {
                        branch
                        for subscription in repo_subscriptions
                        if subscription.watch_push
                        for branch in subscription.branches
                    }
                )
                for branch in branches:
                    await self._process_push(client, repo, branch, repo_subscriptions)
                if any(subscription.watch_release for subscription in repo_subscriptions):
                    await self._process_release(client, repo, repo_subscriptions)
                if any(subscription.watch_tag for subscription in repo_subscriptions):
                    await self._process_tag(client, repo, repo_subscriptions)

    async def _process_push(
        self,
        client: GitHubClient,
        repo: str,
        branch: str,
        subscriptions: list[Subscription],
    ) -> None:
        state_key = _state_key("push", repo, branch)
        try:
            item = await asyncio.to_thread(client.latest_commit, repo, branch)
        except Exception as error:
            self._record_failure(state_key, error)
            return
        if not item:
            self.store.record_state_success(state_key=state_key, event_key="", baseline_done=True)
            return
        await self._handle_detected_event(
            state_key=state_key,
            event_key=item.event_key(),
            repo=repo,
            event_type="push",
            title=item.message,
            url=item.url,
            payload=item.payload(),
            text=self._format_commit(item, subscriptions),
            subscriptions=[
                subscription
                for subscription in subscriptions
                if subscription.watch_push and branch in subscription.branches
            ],
        )

    async def _process_release(
        self,
        client: GitHubClient,
        repo: str,
        subscriptions: list[Subscription],
    ) -> None:
        state_key = _state_key("release", repo)
        try:
            item = await asyncio.to_thread(client.latest_release, repo)
        except Exception as error:
            self._record_failure(state_key, error)
            return
        if not item:
            self.store.record_state_success(state_key=state_key, event_key="", baseline_done=True)
            return
        await self._handle_detected_event(
            state_key=state_key,
            event_key=item.event_key(),
            repo=repo,
            event_type="release",
            title=item.title,
            url=item.url,
            payload=item.payload(),
            text=self._format_release(item, subscriptions),
            subscriptions=[subscription for subscription in subscriptions if subscription.watch_release],
        )

    async def _process_tag(
        self,
        client: GitHubClient,
        repo: str,
        subscriptions: list[Subscription],
    ) -> None:
        state_key = _state_key("tag", repo)
        try:
            item = await asyncio.to_thread(client.latest_tag, repo)
        except Exception as error:
            self._record_failure(state_key, error)
            return
        if not item:
            self.store.record_state_success(state_key=state_key, event_key="", baseline_done=True)
            return
        await self._handle_detected_event(
            state_key=state_key,
            event_key=item.event_key(),
            repo=repo,
            event_type="tag",
            title=item.tag_name,
            url=item.url,
            payload=item.payload(),
            text=self._format_tag(item, subscriptions),
            subscriptions=[subscription for subscription in subscriptions if subscription.watch_tag],
        )

    async def _handle_detected_event(
        self,
        *,
        state_key: str,
        event_key: str,
        repo: str,
        event_type: str,
        title: str,
        url: str,
        payload: dict,
        text: str,
        subscriptions: list[Subscription],
    ) -> None:
        state = self.store.get_state(state_key)
        if not state.baseline_done:
            self.store.upsert_event(
                event_key=event_key,
                repo=repo,
                event_type=event_type,
                title=title,
                url=url,
                payload=payload,
            )
            self.store.record_state_success(state_key=state_key, event_key=event_key, baseline_done=True)
            logger.info("GitHub watch baseline recorded for %s.", state_key)
            return
        if event_key == state.last_event_key:
            self.store.record_state_success(state_key=state_key, event_key=event_key, baseline_done=True)
            return

        self.store.upsert_event(
            event_key=event_key,
            repo=repo,
            event_type=event_type,
            title=title,
            url=url,
            payload=payload,
        )
        for subscription in subscriptions:
            try:
                delivered = await deliver_once(
                    store=self.store,
                    event_key=event_key,
                    target_origin=subscription.target_origin,
                    send=lambda: self._send_to_origin(subscription.target_origin, text),
                )
            except Exception as error:
                logger.error(
                    "GitHub watch delivery failed for %s (%s): %s",
                    event_key,
                    subscription.target_kind,
                    error,
                )
                raise
            if delivered:
                logger.info(
                    "GitHub watch delivered %s to %s target.",
                    event_key,
                    subscription.target_kind,
                )
        self.store.record_state_success(state_key=state_key, event_key=event_key, baseline_done=True)

    def _record_failure(self, state_key: str, error: Exception) -> None:
        failure_count = self.store.record_state_failure(state_key=state_key, error=str(error))
        if failure_count >= self._failure_notice_threshold():
            logger.warning("GitHub watch %s failed %s times: %s", state_key, failure_count, error)
        else:
            logger.info("GitHub watch %s failed: %s", state_key, error)

    async def _send_to_origin(self, origin: str, text: str) -> None:
        await self.context.send_message(origin, MessageChain([Comp.Plain(_clamp(text, self.max_output_chars))]))

    def _format_commit(self, item: GitHubCommit, subscriptions: Iterable[Subscription]) -> str:
        lines = [
            "GitHub 更新",
            f"仓库：{item.repo}",
            f"分支：{item.branch}",
            f"提交：{item.sha[:7]} {item.message}",
        ]
        if item.author:
            lines.append(f"作者：{item.author}")
        if item.committed_at:
            lines.append(f"时间：{item.committed_at}")
        self._append_preset_hint(lines, subscriptions)
        lines.append(item.url)
        return "\n".join(lines)

    def _format_release(self, item: GitHubRelease, subscriptions: Iterable[Subscription]) -> str:
        lines = [
            "GitHub Release",
            f"仓库：{item.repo}",
            f"版本：{item.tag_name or item.release_id}",
        ]
        if item.title and item.title != item.tag_name:
            lines.append(f"标题：{item.title}")
        if item.published_at:
            lines.append(f"时间：{item.published_at}")
        self._append_preset_hint(lines, subscriptions)
        lines.append(item.url)
        return "\n".join(lines)

    def _format_tag(self, item: GitHubTag, subscriptions: Iterable[Subscription]) -> str:
        lines = [
            "GitHub Tag",
            f"仓库：{item.repo}",
            f"标签：{item.tag_name}",
            f"提交：{item.sha[:7]}",
        ]
        self._append_preset_hint(lines, subscriptions)
        lines.append(item.url)
        return "\n".join(lines)

    @staticmethod
    def _append_preset_hint(lines: list[str], subscriptions: Iterable[Subscription]) -> None:
        preset_ids = sorted({subscription.preset_id for subscription in subscriptions if subscription.preset_id})
        if not preset_ids:
            return
        lines.append(f"关联预设：{', '.join(preset_ids)}")
        if "ffxiv-datamining" in preset_ids:
            lines.append("可能影响：NSGlamour / V2 Armoire 数据")

    def _format_presets(self) -> str:
        if not self.presets:
            return "当前没有可用 preset。"
        lines = ["GitHub Watch preset"]
        for preset in self.presets:
            items = preset.get("items", [])
            count = len(items) if isinstance(items, list) else 0
            lines.append(f"- {preset.get('id')}：{preset.get('label', '')}（{count} 个仓库）")
        lines.append("")
        lines.append("使用：/ghwatch preset ffxiv-datamining")
        return "\n".join(lines)

    def _format_preset_detail(self, preset_id: str) -> str:
        preset = self._preset_by_id(preset_id)
        if not preset:
            return f"没有找到 preset：{preset_id}\n\n{self._format_presets()}"
        items = preset.get("items", [])
        lines = [
            f"GitHub Watch preset：{preset.get('id')}",
            str(preset.get("label") or "").strip(),
        ]
        description = str(preset.get("description") or "").strip()
        if description:
            lines.append(description)
        if not isinstance(items, list) or not items:
            lines.append("这个 preset 里还没有仓库。")
            return "\n".join(line for line in lines if line)
        lines.append("")
        for item in items:
            repo = str(item.get("repo") or "").strip()
            label = str(item.get("label") or "").strip()
            branches = [str(branch).strip() for branch in item.get("branches", []) if str(branch).strip()]
            branch_text = ",".join(branches) if branches else "默认分支"
            suffix = f" / {label}" if label else ""
            lines.append(f"- {repo}{suffix}：{branch_text}")
        return "\n".join(line for line in lines if line)

    def _format_subscriptions(self, origin: str) -> str:
        subscriptions = self.store.list_subscriptions(origin)
        if not subscriptions:
            return "当前会话还没有 GitHub Watch 订阅。"
        lines = ["当前会话 GitHub Watch 订阅："]
        for subscription in subscriptions:
            status = "开启" if subscription.enabled else "关闭"
            events = []
            if subscription.watch_push:
                events.append("push")
            if subscription.watch_release:
                events.append("release")
            if subscription.watch_tag:
                events.append("tag")
            preset = f"，preset={subscription.preset_id}" if subscription.preset_id else ""
            label = f" / {subscription.label}" if subscription.label else ""
            lines.append(
                f"- {subscription.repo}{label}：{status}，分支={','.join(subscription.branches)}，事件={','.join(events) or '无'}{preset}"
            )
        return "\n".join(lines)

    def _format_status(self, origin: str) -> str:
        target = self.store.get_target(origin)
        target_enabled = target.enabled if target else True
        lines = [
            "GitHub Watch 状态",
            f"插件后台：{'开启' if self.config.get('enabled', True) else '关闭'}",
            f"当前会话：{'开启' if target_enabled else '关闭'}",
            f"轮询间隔：{self._poll_interval_seconds() // 60} 分钟",
            f"全局订阅会话数：{self.store.count_enabled_targets()}",
            "",
            self._format_subscriptions(origin),
        ]
        return "\n".join(lines)

    async def _resolve_branches(self, client: GitHubClient, repo: str, branches: list[str]) -> tuple[list[str], str]:
        clean = [str(branch).strip() for branch in branches if str(branch).strip()]
        if clean:
            return clean, ""
        info = await asyncio.to_thread(client.repository, repo)
        return [info.default_branch or "main"], info.default_branch or "main"

    async def _test_repo(self, repo: str) -> str:
        client = self._client()
        lines = [f"GitHub Watch 测试：{repo}"]
        default_branch = ""
        try:
            info = await asyncio.to_thread(client.repository, repo)
            default_branch = info.default_branch or "main"
            lines.append(f"默认分支：{default_branch}")
        except Exception as error:
            lines.append(f"仓库信息：失败：{_short_error(error)}")
        try:
            commit = await asyncio.to_thread(client.latest_commit, repo, default_branch or "main")
        except Exception:
            commit = None
        if not commit:
            try:
                commit = await asyncio.to_thread(client.latest_commit, repo, "master")
            except Exception as error:
                lines.append(f"commit：失败：{_short_error(error)}")
                commit = None
        if commit:
            lines.append(f"commit：{commit.branch} / {commit.sha[:7]} {commit.message}")
        try:
            release = await asyncio.to_thread(client.latest_release, repo)
            lines.append(f"release：{release.tag_name if release else '无'}")
        except Exception as error:
            lines.append(f"release：失败：{_short_error(error)}")
        try:
            tag = await asyncio.to_thread(client.latest_tag, repo)
            lines.append(f"tag：{tag.tag_name if tag else '无'}")
        except Exception as error:
            lines.append(f"tag：失败：{_short_error(error)}")
        return "\n".join(lines)

    async def _check_subscriptions(self, origin: str) -> str:
        subscriptions = [item for item in self.store.list_subscriptions(origin) if item.enabled]
        if not subscriptions:
            return "当前会话还没有开启中的 GitHub Watch 订阅。"

        client = self._client()
        lines = ["GitHub Watch 检查"]
        ok_count = 0
        issue_count = 0
        for subscription in subscriptions:
            repo_lines = [f"- {subscription.repo}"]
            repo_ok = True
            try:
                info = await asyncio.to_thread(client.repository, subscription.repo)
                repo_lines.append(f"  默认分支：{info.default_branch}")
                if info.archived:
                    repo_lines.append("  提醒：仓库已归档")
            except Exception as error:
                repo_ok = False
                repo_lines.append(f"  仓库信息：失败：{_short_error(error)}")

            if subscription.watch_push:
                for branch in subscription.branches:
                    try:
                        commit = await asyncio.to_thread(client.latest_commit, subscription.repo, branch)
                        repo_lines.append(
                            f"  push/{branch}：OK {commit.sha[:7] if commit else '无提交'}"
                        )
                    except Exception as error:
                        repo_ok = False
                        repo_lines.append(f"  push/{branch}：失败：{_short_error(error)}")
            if subscription.watch_release:
                try:
                    release = await asyncio.to_thread(client.latest_release, subscription.repo)
                    repo_lines.append(f"  release：OK {release.tag_name if release else '无 release'}")
                except Exception as error:
                    repo_ok = False
                    repo_lines.append(f"  release：失败：{_short_error(error)}")
            if subscription.watch_tag:
                try:
                    tag = await asyncio.to_thread(client.latest_tag, subscription.repo)
                    repo_lines.append(f"  tag：OK {tag.tag_name if tag else '无 tag'}")
                except Exception as error:
                    repo_ok = False
                    repo_lines.append(f"  tag：失败：{_short_error(error)}")

            if repo_ok:
                ok_count += 1
            else:
                issue_count += 1
            lines.extend(repo_lines)

        lines.insert(1, f"结果：{ok_count} 个仓库正常，{issue_count} 个仓库有问题")
        return "\n".join(lines)

    async def _baseline_subscription(self, subscription: Subscription) -> None:
        client = self._client()
        for branch in subscription.branches:
            try:
                item = await asyncio.to_thread(client.latest_commit, subscription.repo, branch)
                event_key = item.event_key() if item else ""
                if item:
                    self.store.upsert_event(
                        event_key=event_key,
                        repo=subscription.repo,
                        event_type="push",
                        title=item.message,
                        url=item.url,
                        payload=item.payload(),
                    )
                    self.store.mark_delivered(event_key=event_key, target_origin=subscription.target_origin)
                self.store.record_state_success(
                    state_key=_state_key("push", subscription.repo, branch),
                    event_key=event_key,
                    baseline_done=True,
                )
            except Exception as error:
                self._record_failure(_state_key("push", subscription.repo, branch), error)

        for event_type, loader in (
            ("release", client.latest_release),
            ("tag", client.latest_tag),
        ):
            try:
                item = await asyncio.to_thread(loader, subscription.repo)
                event_key = item.event_key() if item else ""
                if item:
                    title = getattr(item, "title", "") or getattr(item, "tag_name", "")
                    self.store.upsert_event(
                        event_key=event_key,
                        repo=subscription.repo,
                        event_type=event_type,
                        title=title,
                        url=item.url,
                        payload=item.payload(),
                    )
                    self.store.mark_delivered(event_key=event_key, target_origin=subscription.target_origin)
                self.store.record_state_success(
                    state_key=_state_key(event_type, subscription.repo),
                    event_key=event_key,
                    baseline_done=True,
                )
            except Exception as error:
                self._record_failure(_state_key(event_type, subscription.repo), error)

    async def _subscribe(
        self,
        *,
        origin: str,
        target_kind: str,
        repo: str,
        label: str,
        branches: list[str],
        preset_id: str,
        created_by: str,
    ) -> bool:
        created = self.store.upsert_subscription(
            target_origin=origin,
            target_kind=target_kind,
            repo=repo,
            label=label,
            branches=branches,
            preset_id=preset_id,
            created_by=created_by,
        )
        subscription = next(
            (item for item in self.store.list_subscriptions(origin) if item.repo == repo),
            None,
        )
        if subscription:
            await self._baseline_subscription(subscription)
        return created

    async def _handle_preset(self, event: AstrMessageEvent, origin: str, target_kind: str, args: str):
        raw_args = str(args or "").strip()
        parts = raw_args.split(maxsplit=1)
        action = parts[0].strip().lower() if parts else ""
        if action in {"show", "查看", "详情", "detail"}:
            preset_id = parts[1].strip() if len(parts) > 1 else ""
            if not preset_id:
                yield event.plain_result("用法：/ghwatch preset show ffxiv-datamining")
                return
            yield event.plain_result(_clamp(self._format_preset_detail(preset_id), self.max_output_chars))
            return

        preset_id = raw_args
        if not preset_id or preset_id in {"list", "列表"}:
            yield event.plain_result(_clamp(self._format_presets(), self.max_output_chars))
            return

        preset = self._preset_by_id(preset_id)
        if not preset:
            yield event.plain_result(f"没有找到 preset：{preset_id}\n\n{self._format_presets()}")
            return
        if not self._can_manage(event):
            yield event.plain_result("权限不足：GitHub Watch preset 订阅仅限管理员使用。")
            return

        items = preset.get("items", [])
        if not isinstance(items, list):
            yield event.plain_result(f"preset {preset_id} 配置不正确。")
            return

        created = 0
        restored = 0
        errors: list[str] = []
        client = self._client()
        for item in items:
            try:
                repo = _normalize_repo(str(item.get("repo") or ""))
                raw_branches = [str(branch).strip() for branch in item.get("branches", []) if str(branch).strip()]
                branches, _ = await self._resolve_branches(client, repo, raw_branches)
                was_created = await self._subscribe(
                    origin=origin,
                    target_kind=target_kind,
                    repo=repo,
                    label=str(item.get("label") or ""),
                    branches=branches,
                    preset_id=preset_id,
                    created_by=str(event.get_sender_id()),
                )
                if was_created:
                    created += 1
                else:
                    restored += 1
            except Exception as error:
                errors.append(str(error))

        lines = [
            f"已应用 GitHub Watch preset：{preset_id}",
            f"新增：{created}，恢复/更新：{restored}",
            "首次订阅已建立基线，不会推历史提交。",
        ]
        if errors:
            lines.append("")
            lines.append("失败：")
            lines.extend(errors[:5])
        yield event.plain_result(_clamp("\n".join(lines), self.max_output_chars))

    async def _handle(self, event: AstrMessageEvent):
        origin = _event_origin(event)
        if not origin:
            yield event.plain_result("当前会话来源无法记录，请稍后重试。")
            return

        target_kind = _target_kind(event)
        remainder = _strip_command(event.message_str or "", COMMANDS)
        parts = remainder.split(maxsplit=1)
        sub = parts[0].strip().lower() if parts else ""
        args = parts[1].strip() if len(parts) > 1 else ""

        if not sub or sub in {"help", "帮助", "菜单"}:
            yield event.plain_result(_help_text())
            return

        if sub in {"status", "状态"}:
            yield event.plain_result(_clamp(self._format_status(origin), self.max_output_chars))
            return

        if sub in {"check", "检查", "诊断"}:
            yield event.plain_result(_clamp(await self._check_subscriptions(origin), self.max_output_chars))
            return

        if sub in {"list", "列表", "订阅列表"}:
            yield event.plain_result(_clamp(self._format_subscriptions(origin), self.max_output_chars))
            return

        if sub in {"preset", "预设"}:
            async for result in self._handle_preset(event, origin, target_kind, args):
                yield result
            return

        if sub in {"test", "测试"}:
            try:
                repo = _normalize_repo(args)
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            yield event.plain_result(_clamp(await self._test_repo(repo), self.max_output_chars))
            return

        if not self._can_manage(event):
            yield event.plain_result("权限不足：GitHub Watch 管理命令仅限管理员使用。")
            return

        if sub in {"subscribe", "订阅"}:
            values = args.split()
            if not values:
                yield event.plain_result("用法：/ghwatch 订阅 owner/repo [branch]")
                return
            try:
                repo = _normalize_repo(values[0])
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            client = self._client()
            try:
                branches, detected_branch = await self._resolve_branches(client, repo, values[1:])
            except Exception as error:
                yield event.plain_result(f"获取仓库默认分支失败：{_short_error(error)}")
                return
            created = await self._subscribe(
                origin=origin,
                target_kind=target_kind,
                repo=repo,
                label="",
                branches=branches,
                preset_id="",
                created_by=str(event.get_sender_id()),
            )
            yield event.plain_result(
                "\n".join(
                    [
                        f"{'已订阅' if created else '已恢复/更新订阅'}：{repo}",
                        f"分支：{', '.join(branches)}" + (f"（自动探测）" if detected_branch else ""),
                        "首次订阅已建立基线，不会推历史提交。",
                    ]
                )
            )
            return

        if sub in {"unsubscribe", "取消", "取消订阅"}:
            try:
                repo = _normalize_repo(args)
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            removed = self.store.remove_subscription(target_origin=origin, repo=repo)
            yield event.plain_result(f"已取消订阅：{repo}" if removed else f"当前会话没有订阅：{repo}")
            return

        if sub in {"event", "事件"}:
            values = args.split()
            if len(values) < 3:
                yield event.plain_result("用法：/ghwatch 事件 owner/repo push on")
                return
            try:
                repo = _normalize_repo(values[0])
                event_type = EVENT_ALIASES.get(values[1].lower())
                if not event_type:
                    raise ValueError("事件类型只能是 push / release / tag。")
                enabled = _parse_action(values[2])
            except ValueError as error:
                yield event.plain_result(str(error))
                return
            ok = self.store.set_event_enabled(
                target_origin=origin,
                repo=repo,
                event_type=event_type,
                enabled=enabled,
            )
            yield event.plain_result(
                f"已{'开启' if enabled else '关闭'} {repo} 的 {event_type} 事件。"
                if ok
                else f"当前会话没有订阅：{repo}"
            )
            return

        if sub in {"on", "开", "开启"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=True)
            yield event.plain_result("已开启当前会话的 GitHub Watch 推送。")
            return

        if sub in {"off", "关", "关闭"}:
            self.store.set_target_enabled(target_origin=origin, target_kind=target_kind, enabled=False)
            yield event.plain_result("已关闭当前会话的 GitHub Watch 推送；订阅记录不会删除。")
            return

        yield event.plain_result(_help_text())

    @filter.command("ghwatch")
    async def ghwatch(self, event: AstrMessageEvent):
        async for result in self._handle(event):
            yield result

    @filter.command("githubwatch")
    async def githubwatch(self, event: AstrMessageEvent):
        async for result in self._handle(event):
            yield result
