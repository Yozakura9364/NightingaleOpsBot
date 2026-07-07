from __future__ import annotations

from pathlib import Path

import aiohttp


class SqmallQrClient:
    def __init__(self, endpoint: str, token: str, timeout_seconds: int = 240):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def start_session(self, *, ttl_ms: int) -> dict:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.endpoint}/sqmall/qr/start",
                headers=self._headers(),
                json={"ttlMs": ttl_ms},
            ) as response:
                return await self._read_json(response)

    async def download_image(self, session_id: str, output_path: Path) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "image/png",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.endpoint}/sqmall/qr/{session_id}/image",
                headers=headers,
            ) as response:
                if response.status >= 400:
                    payload = await self._read_json(response)
                    raise RuntimeError(payload.get("error") or f"下载二维码失败：HTTP {response.status}")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(await response.read())

    async def status(self, session_id: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{self.endpoint}/sqmall/qr/{session_id}/status",
                headers=self._headers(),
            ) as response:
                return await self._read_json(response)

    async def cancel(self, session_id: str) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(
                f"{self.endpoint}/sqmall/qr/{session_id}",
                headers=self._headers(),
            ) as response:
                if response.status >= 400:
                    await response.read()

    @staticmethod
    async def _read_json(response: aiohttp.ClientResponse) -> dict:
        try:
            payload = await response.json(content_type=None)
        except Exception:
            text = await response.text()
            payload = {"ok": False, "error": text[:500]}

        if response.status >= 400 and "status" not in payload:
            payload["status"] = response.status
        return payload
