from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError
from uuid import uuid4


DEFAULT_BASE_URL = "https://apiff14risingstones.web.sdo.com"


@dataclass
class SignOptions:
    get_sign_reward: bool = True
    check_house_remain: bool = False
    report_account_summary: bool = True
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


def _clean_text(value) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "none" else text


def _account_summary_from_user_data(data: dict) -> str:
    details = data.get("characterDetail") or []
    first_detail = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
    area_name = _clean_text(data.get("area_name"))
    group_name = _clean_text(data.get("group_name"))
    character_name = _clean_text(data.get("character_name")) or _clean_text(
        first_detail.get("character_name")
    )

    parts = [part for part in (area_name, group_name, character_name) if part]
    if parts:
        return " / ".join(parts)

    uuid = _clean_text(data.get("uuid"))
    return f"UUID {uuid}" if uuid else "未获取到角色名"


def _get_user_info_data(requests, base_url: str, headers: dict[str, str]) -> dict:
    user_response = requests.get(
        f"{base_url}/api/home/userInfo/getUserInfo",
        params={"page": 1},
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome124",
        timeout=30,
    )
    user_data = _json_response(user_response, "用户信息")
    code = user_data.get("code")
    if code is not None and isinstance(code, int) and code > 10000:
        raise RisingstoneSignError(f"用户信息获取失败：{user_data.get('msg', user_data)}")
    return user_data.get("data") or {}


def _get_character_bind_info_data(requests, base_url: str, headers: dict[str, str]) -> dict:
    character_response = requests.get(
        f"{base_url}/api/home/groupAndRole/getCharacterBindInfo",
        params={"platform": 2},
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome124",
        timeout=30,
    )
    character_data = _json_response(character_response, "绑定角色信息")
    code = character_data.get("code")
    if code is not None and isinstance(code, int) and code > 10000:
        raise RisingstoneSignError(
            f"绑定角色信息获取失败：{character_data.get('msg', character_data)}"
        )
    data = character_data.get("data") or {}
    if not _clean_text(data.get("character_name")):
        raise RisingstoneSignError("请先在石之家选择并绑定角色")
    return data


def _get_login_data(requests, base_url: str, headers: dict[str, str]) -> dict:
    login_response = requests.get(
        f"{base_url}/api/home/GHome/isLogin",
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome124",
        timeout=30,
    )
    login_data = _json_response(login_response, "登录信息")
    code = login_data.get("code")
    if code is not None and isinstance(code, int) and code > 10000 and code not in (10002,):
        raise RisingstoneSignError(f"登录信息获取失败：{login_data.get('msg', login_data)}")
    return login_data.get("data") or {}


def get_risingstone_account_summary(
    cookie: str,
    user_agent: str,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    from curl_cffi import requests

    normalized_base_url = base_url.rstrip("/")
    headers = _headers(cookie, user_agent)
    errors: list[Exception] = []
    for loader in (_get_character_bind_info_data, _get_login_data, _get_user_info_data):
        try:
            return _account_summary_from_user_data(loader(requests, normalized_base_url, headers))
        except Exception as error:
            errors.append(error)
    raise RisingstoneSignError(f"账号角色获取失败：{errors[-1]}")


def _get_report_account_data(requests, base_url: str, headers: dict[str, str]) -> tuple[dict | None, Exception | None]:
    try:
        return _get_character_bind_info_data(requests, base_url, headers), None
    except Exception as character_error:
        try:
            return _get_login_data(requests, base_url, headers), None
        except Exception:
            return None, character_error


def _first_character_detail(user_info_data: dict) -> dict:
    details = user_info_data.get("characterDetail") or []
    if isinstance(details, list) and details and isinstance(details[0], dict):
        return details[0]
    if isinstance(details, dict):
        return details
    return {}


def _house_status_line(user_info_data: dict) -> str:
    detail = _first_character_detail(user_info_data)
    if not detail:
        return "房屋拆除倒计时：未获取到角色房屋信息"

    house_remain_day = _clean_text(detail.get("house_remain_day"))
    if not house_remain_day:
        return "房屋拆除倒计时：未发现风险"
    if "*" in house_remain_day:
        return "房屋拆除倒计时：房屋状态已隐藏"
    return f"房屋拆除倒计时：{house_remain_day}"


def run_risingstone_house_check(
    cookie: str,
    user_agent: str,
    options: SignOptions | None = None,
) -> SignResult:
    from curl_cffi import requests

    opts = options or SignOptions(get_sign_reward=False, check_house_remain=True)
    base_url = opts.base_url.rstrip("/")
    headers = _headers(cookie, user_agent)
    lines: list[str] = []

    account_data, account_error = _get_report_account_data(requests, base_url, headers)
    if opts.report_account_summary:
        if account_data is not None:
            lines.append(f"绑定角色：{_account_summary_from_user_data(account_data)}")
        elif account_error is not None:
            lines.append(f"绑定角色：获取失败（{account_error}）")

    try:
        user_info_data = _get_user_info_data(requests, base_url, headers)
    except Exception as error:
        raise RisingstoneSignError(f"用户信息获取失败：{error}") from error
    lines.append(_house_status_line(user_info_data))
    return SignResult(ok=True, lines=lines)


def run_risingstone_sign(cookie: str, user_agent: str, options: SignOptions | None = None) -> SignResult:
    # API flow based on StarHeartHunt/ff14risingstone_sign_task (MIT).
    from curl_cffi import requests

    opts = options or SignOptions()
    base_url = opts.base_url.rstrip("/")
    headers = _headers(cookie, user_agent)
    lines: list[str] = []
    account_data = None
    account_error = None

    if opts.report_account_summary or opts.check_house_remain:
        account_data, account_error = _get_report_account_data(requests, base_url, headers)
        if opts.check_house_remain and account_error is not None:
            raise RisingstoneSignError(f"绑定角色信息获取失败：{account_error}") from account_error

    sign_response = requests.post(
        f"{base_url}/api/home/sign/signIn",
        params={"tempsuid": str(uuid4())},
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

    if opts.report_account_summary:
        if account_data is not None:
            lines.append(f"签到角色：{_account_summary_from_user_data(account_data)}")
        elif account_error is not None:
            lines.append(f"签到角色：获取失败（{account_error}）")

    if opts.check_house_remain:
        try:
            user_info_data = _get_user_info_data(requests, base_url, headers)
        except Exception as error:
            raise RisingstoneSignError(f"用户信息获取失败：{error}") from error
        lines.append(_house_status_line(user_info_data))

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
