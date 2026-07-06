from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
from uuid import uuid4


DEFAULT_BASE_URL = "https://apiff14risingstones.web.sdo.com"


@dataclass
class SignOptions:
    get_sign_reward: bool = True
    check_house_remain: bool = False
    base_url: str = DEFAULT_BASE_URL


@dataclass
class SignResult:
    ok: bool
    lines: list[str]

    @property
    def summary(self) -> str:
        return "\n".join(self.lines)


class RisingstoneSignError(RuntimeError):
    pass


def _safe_response_text(text: str, limit: int = 500) -> str:
    return str(text or "").replace("\r", " ").replace("\n", " ")[:limit]


def _json_response(response, action: str):
    try:
        return response.json()
    except JSONDecodeError as error:
        raise RisingstoneSignError(
            f"{action} 返回不是 JSON：HTTP {response.status_code}，{_safe_response_text(response.text)}"
        ) from error


def _headers(cookie: str, user_agent: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "Referer": "https://ff14risingstones.web.sdo.com/",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "User-Agent": user_agent,
        "Cookie": cookie,
    }


def _current_month() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


def run_risingstone_sign(cookie: str, user_agent: str, options: SignOptions | None = None) -> SignResult:
    # API flow based on StarHeartHunt/ff14risingstone_sign_task (MIT).
    from curl_cffi import requests

    opts = options or SignOptions()
    base_url = opts.base_url.rstrip("/")
    headers = _headers(cookie, user_agent)
    lines: list[str] = []

    sign_response = requests.post(
        f"{base_url}/api/home/sign/signIn",
        params={"tempsuid": str(uuid4())},
        data={"tempsuid": str(uuid4())},
        headers=headers,
        impersonate="chrome124",
        timeout=30,
    )
    sign_data = _json_response(sign_response, "签到")
    code = sign_data.get("code")
    message = sign_data.get("msg") or sign_data.get("message") or sign_data
    if code is None or (isinstance(code, int) and code > 10000 and code != 10001):
        raise RisingstoneSignError(f"签到失败：{message}")
    lines.append(f"签到结果：{message}")

    if opts.check_house_remain:
        user_response = requests.get(
            f"{base_url}/api/home/userInfo/getUserInfo",
            params={"page": 1},
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            impersonate="chrome124",
            timeout=30,
        )
        user_data = _json_response(user_response, "用户信息")
        house_remain_day = (
            user_data.get("data", {})
            .get("characterDetail", [{}])[0]
            .get("house_remain_day")
        )
        if house_remain_day:
            lines.append(f"房屋拆除倒计时：{house_remain_day}")
        else:
            lines.append("房屋拆除倒计时：未发现风险")

    if opts.get_sign_reward:
        month = _current_month()
        reward_response = requests.get(
            f"{base_url}/api/home/sign/signRewardList",
            params={"month": month, "tempsuid": str(uuid4())},
            headers=headers,
            impersonate="chrome124",
            timeout=30,
        )
        reward_data = _json_response(reward_response, "签到奖励列表")
        rewards = reward_data.get("data") or []
        available = [item for item in rewards if item.get("is_get") == 0]
        if not available:
            lines.append("签到奖励：暂无可领取奖励")
        for item in available:
            item_name = item.get("item_name") or f"奖励 {item.get('id', '-')}"
            get_response = requests.post(
                f"{base_url}/api/home/sign/getSignReward",
                params={"tempsuid": str(uuid4())},
                data={"id": item.get("id"), "month": month, "tempsuid": str(uuid4())},
                headers=headers,
                impersonate="chrome124",
                timeout=30,
            )
            get_data = _json_response(get_response, f"领取奖励 {item_name}")
            if get_data.get("code") is not None and get_data.get("code") > 10000:
                lines.append(f"领取奖励失败：{item_name}，{get_data.get('msg', get_data)}")
            else:
                lines.append(f"领取奖励：{item_name}")

    return SignResult(ok=True, lines=lines)
