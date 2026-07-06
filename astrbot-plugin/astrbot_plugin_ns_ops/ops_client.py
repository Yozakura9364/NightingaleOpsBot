import aiohttp


class NsOpsClient:
    def __init__(self, endpoint: str, token: str, timeout_seconds: int = 900):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def health(self) -> dict:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{self.endpoint}/health") as response:
                return await self._read_json(response)

    async def run_job(self, job_path: str, payload: dict | None = None) -> dict:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.endpoint}/jobs/{job_path}",
                headers=self._headers(),
                json=payload or {},
            ) as response:
                return await self._read_json(response)

    async def confirm(self, token: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self.endpoint}/confirm",
                headers=self._headers(),
                json={"token": token},
            ) as response:
                return await self._read_json(response)

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
