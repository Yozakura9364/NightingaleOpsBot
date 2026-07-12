from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie
import json
import random
import string
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


DAOYU_BASE_URL = "https://daoyu.sdo.com"
SQMALL_BASE_URL = "https://sqmallservice.u.sdo.com"
DAOYU_APP_UA = "SdAccountKeyM/9.3.3 (iPhone; iOS 17.0; Scale/3.00)"
MALL_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 DaoYu/9.3.3"
)


@dataclass
class SqmallAccountResult:
    display_name: str
    ok: bool
    message: str
    balance: int | str | None = None


@dataclass
class SqmallSignResult:
    ok: bool
    account_results: list[SqmallAccountResult]

    @property
    def summary(self) -> str:
        if not self.account_results:
            return "未发现可签到账号。"
        lines: list[str] = []
        for item in self.account_results:
            status = "成功" if item.ok else "失败"
            balance = "" if item.balance is None else f"，当前积分余额 {item.balance}"
            lines.append(f"{item.display_name}：{status}，{item.message}{balance}")
        return "\n".join(lines)


class SqmallSignError(RuntimeError):
    pass


def _random_device_id() -> str:
    return "-".join(str(uuid4()).upper() for _ in range(5))


def _random_manuid() -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))


def _cookie_header(cookies: dict[str, str] | None) -> str:
    if not cookies:
        return ""
    return "; ".join(f"{key}={value}" for key, value in cookies.items() if value is not None)


def _response_cookies(headers) -> dict[str, str]:
    values = []
    if hasattr(headers, "get_all"):
        values = headers.get_all("Set-Cookie") or []
    else:
        value = headers.get("Set-Cookie")
        values = [value] if value else []

    parsed: dict[str, str] = {}
    for value in values:
        cookie = SimpleCookie()
        cookie.load(value)
        for key, morsel in cookie.items():
            parsed[key] = morsel.value
    return parsed


def _safe_response_text(data: bytes, limit: int = 500) -> str:
    try:
        return data.decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")[:limit]
    except Exception:
        return repr(data[:limit])


def _json_request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[dict, dict[str, str]]:
    request_url = url
    if params:
        request_url = f"{url}?{urlencode(params)}"

    body = None
    request_headers = dict(headers or {})
    if data is not None:
        body = urlencode(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    cookie = _cookie_header(cookies)
    if cookie:
        request_headers["Cookie"] = cookie

    request = Request(request_url, data=body, headers=request_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            response_headers = response.headers
    except HTTPError as error:
        raw = error.read()
        raise SqmallSignError(f"HTTP {error.code}：{_safe_response_text(raw)}") from error
    except URLError as error:
        raise SqmallSignError(f"网络请求失败：{error.reason}") from error

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise SqmallSignError(f"接口返回不是 JSON：{_safe_response_text(raw)}") from error
    return payload, _response_cookies(response_headers)


def _daoyu_headers() -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": DAOYU_APP_UA,
        "Accept-Language": "zh-Hans-CN;q=1, zh-Hant-CN;q=0.9",
    }


def _mall_headers(*, merchant_id: str = "1", deploy_platform: str = "4", mode: str = "app") -> dict[str, str]:
    if mode == "web":
        return {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Origin": "https://qu.sdo.com",
            "Referer": "https://qu.sdo.com/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15"
            ),
            "qu-deploy-platform": "1",
            "qu-hardware-platform": "3",
            "qu-merchant-id": merchant_id,
            "qu-software-platform": "1",
            "qu-web-host": "qu.sdo.com",
        }
    return {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Origin": "https://m.qu.sdo.com",
        "Referer": "https://m.qu.sdo.com/",
        "User-Agent": MALL_UA,
        "X-Requested-With": "com.sdo.sdaccountkey",
        "qu-deploy-platform": deploy_platform,
        "qu-hardware-platform": "1",
        "qu-merchant-id": merchant_id,
        "qu-software-platform": "2",
        "qu-web-host": "https://m.qu.sdo.com",
    }


def _daoyu_cookies(daoyu_key: str, identity: str, user_id: str = "") -> dict[str, str]:
    cookies = {
        "USERSESSID": daoyu_key,
        "is_login": "1",
    }
    if user_id:
        cookies["user_id"] = user_id
        cookies["nickname"] = identity
    else:
        cookies["show_username"] = identity
    return cookies


def _get_flowid(manuid: str, device_id: str, daoyu_key: str, identity: str, user_id: str = "") -> str:
    payload, _ = _json_request(
        "GET",
        f"{DAOYU_BASE_URL}/api/thirdPartyAuth/initialize",
        params={
            "media_channel": "AppStore",
            "device_os": "iOS17.0",
            "idfa": "00000000-0000-0000-0000-000000000000",
            "circle_id": "854742",
            "app_version": "i.9.3.3",
            "device_manuid": manuid,
            "src_code": "8",
            "clientId": "qu_shop",
            "appId": "6666",
            "scope": "get_account_profile",
            "extend": "",
            "device_id": device_id,
        },
        headers=_daoyu_headers(),
        cookies=_daoyu_cookies(daoyu_key, identity, user_id),
    )
    if payload.get("return_code") != 0:
        raise SqmallSignError(f"获取叨鱼 flowId 失败：{payload.get('return_message', payload)}")
    flowid = (payload.get("data") or {}).get("flowId")
    if not flowid:
        raise SqmallSignError("获取叨鱼 flowId 失败：返回缺少 flowId")
    return str(flowid)


def _get_account_id_list(
    flowid: str,
    manuid: str,
    device_id: str,
    daoyu_key: str,
    identity: str,
    user_id: str = "",
) -> list[dict]:
    payload, _ = _json_request(
        "GET",
        f"{DAOYU_BASE_URL}/api/thirdPartyAuth/queryAccountList",
        params={
            "flowId": flowid,
            "idfa": "00000000-0000-0000-0000-000000000000",
            "device_manuid": manuid,
            "src_code": "8",
            "circle_id": "854742",
            "device_os": "iOS17.0",
            "media_channel": "AppStore",
            "app_version": "i.9.3.3",
            "device_id": device_id,
        },
        headers=_daoyu_headers(),
        cookies=_daoyu_cookies(daoyu_key, identity, user_id),
    )
    if payload.get("return_message") != "success":
        raise SqmallSignError(f"拉取子账号列表失败：{payload.get('return_message', payload)}")
    account_list = (payload.get("data") or {}).get("accountList") or []
    return account_list if isinstance(account_list, list) else []


def _make_confirm(
    account_id: str,
    flowid: str,
    manuid: str,
    device_id: str,
    daoyu_key: str,
    identity: str,
    user_id: str = "",
) -> bool:
    payload, _ = _json_request(
        "GET",
        f"{DAOYU_BASE_URL}/api/thirdPartyAuth/chooseAccount",
        params={
            "idfa": "00000000-0000-0000-0000-000000000000",
            "flowId": flowid,
            "device_os": "iOS17.0",
            "media_channel": "AppStore",
            "accountId": account_id,
            "device_manuid": manuid,
            "app_version": "i.9.3.3",
            "src_code": "8",
            "device_id": device_id,
            "circle_id": "854742",
        },
        headers=_daoyu_headers(),
        cookies=_daoyu_cookies(daoyu_key, identity, user_id),
    )
    return payload.get("return_message") == "success"


def _get_sub_account_key(
    flowid: str,
    manuid: str,
    device_id: str,
    daoyu_key: str,
    identity: str,
    user_id: str = "",
) -> str:
    payload, _ = _json_request(
        "GET",
        f"{DAOYU_BASE_URL}/api/thirdPartyAuth/confirm",
        params={
            "app_version": "i.9.3.3",
            "flowId": flowid,
            "idfa": "00000000-0000-0000-0000-000000000000",
            "circle_id": "854742",
            "device_manuid": manuid,
            "media_channel": "AppStore",
            "device_os": "iOS17.0",
            "device_id": device_id,
            "src_code": "8",
        },
        headers=_daoyu_headers(),
        cookies=_daoyu_cookies(daoyu_key, identity, user_id),
    )
    if payload.get("return_code") != 0:
        raise SqmallSignError(f"获取子账号票据失败：{payload.get('return_message', payload)}")
    authorization = (payload.get("data") or {}).get("authorization")
    if not authorization:
        raise SqmallSignError("获取子账号票据失败：返回缺少 authorization")
    return str(authorization)


def _get_temp_sessionid(daoyu_key: str) -> str:
    _, cookies = _json_request(
        "GET",
        f"{SQMALL_BASE_URL}/api/us/daoyu/account/getMallLoginStatus",
        params={"USERSESSID": daoyu_key},
        headers=_mall_headers(merchant_id=""),
    )
    session_id = cookies.get("sessionId")
    if not session_id:
        raise SqmallSignError("获取商城临时 sessionId 失败")
    return session_id


def _get_sub_account_session(sub_account_key: str, temp_session_id: str) -> str:
    _, cookies = _json_request(
        "GET",
        f"{SQMALL_BASE_URL}/api/us/daoyu/account/switch",
        params={"daoyuTicket": sub_account_key},
        headers=_mall_headers(deploy_platform="8"),
        cookies={"sessionId": temp_session_id},
    )
    session_id = cookies.get("sessionId")
    if not session_id:
        raise SqmallSignError("切换商城子账号 sessionId 失败")
    return session_id


def _sign_account(sub_session_id: str, account_id: str) -> tuple[bool, str]:
    payload, _ = _json_request(
        "PUT",
        f"{SQMALL_BASE_URL}/api/us/integration/checkIn",
        data={"merchantId": 1},
        headers=_mall_headers(mode="web"),
        cookies={"sessionId": sub_session_id, "direbmemllam": account_id},
    )
    result_msg = str(payload.get("resultMsg") or payload.get("message") or payload)
    if result_msg == "SUCCESS":
        return True, "签到成功"
    if result_msg == "今日已签到，请勿重复签到":
        return True, "今日已签到"
    return False, f"签到失败：{result_msg}"


def _get_balance(sub_session_id: str) -> int | str:
    payload, _ = _json_request(
        "GET",
        f"{SQMALL_BASE_URL}/api/rs/member/integral/balance",
        params={"merchantId": 1},
        headers=_mall_headers(mode="web"),
        cookies={"sessionId": sub_session_id},
    )
    data = payload.get("data") or {}
    return data.get("balance", "-")


def run_sqmall_sign(daoyu_key: str, identity: str, user_id: str = "") -> SqmallSignResult:
    key = str(daoyu_key or "").strip()
    name = str(identity or "").strip()
    normalized_user_id = str(user_id or "").strip()
    if not key.startswith("DY"):
        raise SqmallSignError("DaoyuKey 格式不正确，应以 DY 开头。")
    if len(name) < 2:
        raise SqmallSignError("身份字段过短，请填写 SHOW_USERNAME 或 NICKNAME。")
    if normalized_user_id and not normalized_user_id.isdigit():
        raise SqmallSignError("USER_ID 格式不正确，应为纯数字。")

    device_id = _random_device_id()
    manuid = _random_manuid()
    flowid = _get_flowid(manuid, device_id, key, name, normalized_user_id)
    accounts = _get_account_id_list(flowid, manuid, device_id, key, name, normalized_user_id)
    if not accounts:
        raise SqmallSignError("没有发现叨鱼子账号。")

    temp_session_id = _get_temp_sessionid(key)
    results: list[SqmallAccountResult] = []

    for index, account in enumerate(accounts):
        account_id = str(account.get("accountId") or "")
        display_name = str(account.get("displayName") or account_id or f"账号{index + 1}")
        try:
            if not account_id:
                raise SqmallSignError("子账号缺少 accountId")
            if not _make_confirm(account_id, flowid, manuid, device_id, key, name, normalized_user_id):
                raise SqmallSignError("与叨鱼服务器握手失败")

            sub_account_key = _get_sub_account_key(flowid, manuid, device_id, key, name, normalized_user_id)
            sub_session_id = _get_sub_account_session(sub_account_key, temp_session_id)
            ok, message = _sign_account(sub_session_id, account_id)
            balance = _get_balance(sub_session_id)
            results.append(
                SqmallAccountResult(
                    display_name=display_name,
                    ok=ok,
                    message=message,
                    balance=balance,
                )
            )
        except Exception as error:
            results.append(
                SqmallAccountResult(
                    display_name=display_name,
                    ok=False,
                    message=str(error),
                )
            )

        if index + 1 < len(accounts):
            flowid = _get_flowid(manuid, device_id, key, name, normalized_user_id)

    return SqmallSignResult(ok=any(item.ok for item in results), account_results=results)


def validate_sqmall_daoyu_credentials(daoyu_key: str, identity: str, user_id: str = "") -> int:
    key = str(daoyu_key or "").strip()
    name = str(identity or "").strip()
    normalized_user_id = str(user_id or "").strip()
    if not key.startswith("DY"):
        raise SqmallSignError("DaoyuKey 格式不正确，应以 DY 开头。")
    if len(name) < 2:
        raise SqmallSignError("身份字段过短，请填写 SHOW_USERNAME 或 NICKNAME。")
    if normalized_user_id and not normalized_user_id.isdigit():
        raise SqmallSignError("USER_ID 格式不正确，应为纯数字。")

    device_id = _random_device_id()
    manuid = _random_manuid()
    flowid = _get_flowid(manuid, device_id, key, name, normalized_user_id)
    accounts = _get_account_id_list(flowid, manuid, device_id, key, name, normalized_user_id)
    if not accounts:
        raise SqmallSignError("没有发现叨鱼子账号。")
    return len(accounts)


def run_sqmall_session_sign(session_id: str, member_id: str, display_name: str = "") -> SqmallSignResult:
    session = str(session_id or "").strip()
    account_id = str(member_id or "").strip()
    name = str(display_name or "").strip() or account_id or "盛趣商城账号"
    if not session:
        raise SqmallSignError("商城 sessionId 为空，请重新扫码绑定。")
    if not account_id:
        raise SqmallSignError("商城会员标识为空，请重新扫码绑定。")

    ok, message = _sign_account(session, account_id)
    balance = None
    try:
        balance = _get_balance(session)
    except Exception as error:
        message = f"{message}；积分余额获取失败：{error}"

    result = SqmallAccountResult(
        display_name=name,
        ok=ok,
        message=message,
        balance=balance,
    )
    return SqmallSignResult(ok=ok, account_results=[result])
