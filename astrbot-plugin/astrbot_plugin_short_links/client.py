from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ShortLinkClientError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status


class ShortLinkClient:
    def __init__(self, *, endpoint: str, token: str, timeout_seconds: int = 12):
        self.endpoint = str(endpoint or "").rstrip("/")
        self.token = str(token or "").strip()
        self.timeout_seconds = max(3, int(timeout_seconds))

    def _request(self, method: str, path: str, payload: dict | None = None):
        if not self.endpoint or not self.token:
            raise ShortLinkClientError("短链服务尚未配置。")
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.endpoint}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
                return json.loads(body.decode("utf-8")) if body else None
        except HTTPError as error:
            try:
                payload = json.loads(error.read().decode("utf-8"))
                detail = str(payload.get("error") or "")
            except Exception:
                detail = ""
            messages = {
                400: "链接或短码格式不正确。",
                401: "短链服务认证失败。",
                404: "没有找到这个短链。",
                409: "这个短码已经被使用。",
                503: "短链管理服务尚未配置。",
            }
            raise ShortLinkClientError(
                messages.get(error.code, detail or "短链服务请求失败。"),
                status=error.code,
            ) from error
        except (URLError, TimeoutError) as error:
            raise ShortLinkClientError("暂时无法连接短链服务。") from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ShortLinkClientError("短链服务返回了无效数据。") from error

    def create(self, target_url: str, code: str = "") -> dict:
        payload = {"target_url": target_url}
        if code:
            payload["code"] = code
        return self._request("POST", "/internal/short-links", payload)

    def list(self) -> list[dict]:
        payload = self._request("GET", "/internal/short-links") or {}
        links = payload.get("links") if isinstance(payload, dict) else []
        return links if isinstance(links, list) else []

    def update(
        self,
        code: str,
        *,
        target_url: str | None = None,
        enabled: bool | None = None,
    ) -> dict:
        payload: dict[str, object] = {}
        if target_url is not None:
            payload["target_url"] = target_url
        if enabled is not None:
            payload["enabled"] = enabled
        return self._request(
            "PATCH",
            f"/internal/short-links/{quote(code, safe='')}",
            payload,
        )

    def delete(self, code: str) -> None:
        self._request("DELETE", f"/internal/short-links/{quote(code, safe='')}")
