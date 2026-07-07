from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


API_BASE = "https://api.github.com"
USER_AGENT = "NightingaleOpsBot-GitHubWatch/0.1"


@dataclass(frozen=True)
class GitHubRepo:
    repo: str
    default_branch: str
    private: bool
    archived: bool
    url: str


@dataclass(frozen=True)
class GitHubCommit:
    repo: str
    branch: str
    sha: str
    message: str
    author: str
    committed_at: str
    url: str

    def event_key(self) -> str:
        return f"github:push:{self.repo}:{self.branch}:{self.sha}"

    def payload(self) -> dict[str, str]:
        return {
            "type": "push",
            "repo": self.repo,
            "branch": self.branch,
            "sha": self.sha,
            "message": self.message,
            "author": self.author,
            "committed_at": self.committed_at,
            "url": self.url,
        }


@dataclass(frozen=True)
class GitHubRelease:
    repo: str
    release_id: str
    tag_name: str
    title: str
    published_at: str
    url: str

    def event_key(self) -> str:
        key = self.release_id or self.tag_name
        return f"github:release:{self.repo}:{key}"

    def payload(self) -> dict[str, str]:
        return {
            "type": "release",
            "repo": self.repo,
            "release_id": self.release_id,
            "tag_name": self.tag_name,
            "title": self.title,
            "published_at": self.published_at,
            "url": self.url,
        }


@dataclass(frozen=True)
class GitHubTag:
    repo: str
    tag_name: str
    sha: str
    url: str

    def event_key(self) -> str:
        return f"github:tag:{self.repo}:{self.tag_name}:{self.sha}"

    def payload(self) -> dict[str, str]:
        return {
            "type": "tag",
            "repo": self.repo,
            "tag_name": self.tag_name,
            "sha": self.sha,
            "url": self.url,
        }


class GitHubClient:
    def __init__(self, *, token: str = "", timeout_seconds: int = 20, proxy_url: str = ""):
        self.token = str(token or "").strip()
        self.timeout_seconds = max(5, int(timeout_seconds or 20))
        self.proxy_url = str(proxy_url or "").strip()

    def repository(self, repo: str) -> GitHubRepo:
        payload = self._get_json(f"/repos/{repo}")
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub API returned invalid repository payload.")
        return GitHubRepo(
            repo=repo,
            default_branch=str(payload.get("default_branch") or "main"),
            private=bool(payload.get("private")),
            archived=bool(payload.get("archived")),
            url=str(payload.get("html_url") or f"https://github.com/{repo}"),
        )

    def latest_commit(self, repo: str, branch: str) -> GitHubCommit | None:
        payload = self._get_json(f"/repos/{repo}/commits", {"sha": branch, "per_page": "1"})
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        commit = row.get("commit") if isinstance(row, dict) else {}
        author = commit.get("author") if isinstance(commit, dict) else {}
        sha = str(row.get("sha") or "")
        message = _first_line(str(commit.get("message") or ""))
        return GitHubCommit(
            repo=repo,
            branch=branch,
            sha=sha,
            message=message,
            author=str(author.get("name") or row.get("author", {}).get("login") or ""),
            committed_at=str(author.get("date") or ""),
            url=str(row.get("html_url") or f"https://github.com/{repo}/commit/{sha}"),
        )

    def latest_release(self, repo: str) -> GitHubRelease | None:
        payload = self._get_json(f"/repos/{repo}/releases", {"per_page": "1"})
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        if not isinstance(row, dict):
            return None
        tag_name = str(row.get("tag_name") or "")
        return GitHubRelease(
            repo=repo,
            release_id=str(row.get("id") or ""),
            tag_name=tag_name,
            title=str(row.get("name") or tag_name),
            published_at=str(row.get("published_at") or row.get("created_at") or ""),
            url=str(row.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag_name}"),
        )

    def latest_tag(self, repo: str) -> GitHubTag | None:
        payload = self._get_json(f"/repos/{repo}/tags", {"per_page": "1"})
        if not isinstance(payload, list) or not payload:
            return None
        row = payload[0]
        if not isinstance(row, dict):
            return None
        commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
        tag_name = str(row.get("name") or "")
        sha = str(commit.get("sha") or "")
        return GitHubTag(
            repo=repo,
            tag_name=tag_name,
            sha=sha,
            url=f"https://github.com/{repo}/releases/tag/{tag_name}",
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        url = f"{API_BASE}{path}{query}"
        request = Request(url, headers=self._headers())
        opener = self._opener()
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                data = response.read().decode("utf-8", errors="replace")
                return json.loads(data)
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"GitHub API HTTP {error.code}: {body}") from error
        except URLError as error:
            raise RuntimeError(f"GitHub API request failed: {error.reason}") from error

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _opener(self):
        if not self.proxy_url:
            return build_opener()
        return build_opener(ProxyHandler({"http": self.proxy_url, "https": self.proxy_url}))


def _first_line(value: str) -> str:
    return str(value or "").strip().splitlines()[0].strip()
