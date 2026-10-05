"""AutoDL instance queries, conservative decisions, and explicit start commands.

The endpoint is an internal web-console API, so unexpected responses are treated
as unknown/error. Start commands never retry an uncertain outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
import math
import socket
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .config import AutoDLSettings, Target


class APIError(Exception):
    """A safe, user-facing API failure with no response body or credentials."""


class AuthenticationError(APIError):
    """The AutoDL login token is missing, invalid, or no longer authorized."""


class RateLimitError(APIError):
    """The service asks the caller to reduce its request rate."""

    def __init__(self, message: str = "AutoDL 请求过于频繁，请稍后重试。", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class StartRejectedError(APIError):
    """A start was definitely not accepted; retry requires explicit evidence."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class StartOutcomeUnknownError(APIError):
    """A start may have reached AutoDL; retain pending state and only query."""


@dataclass(frozen=True)
class StartCandidate:
    instance_uuid: str
    label: str
    required_gpus: int
    idle_gpus: int


@dataclass(frozen=True)
class Observation:
    target_key: str
    label: str
    available: bool | None
    idle_gpus: int | None
    required_gpus: int | None
    detail: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the Authorization header, even to another AutoDL URL.
        return None


def _integer(value: Any) -> int | None:
    """Accept nonnegative integral counts, including decimal API strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 and value.is_integer() else None
    if isinstance(value, str) and value and value.isascii() and value.isdecimal():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return None


def _display(value: Any) -> str:
    # Instance names originate outside the program; keep terminal/mail output
    # on one line and do not allow terminal escape sequences.
    return "".join(char if char.isprintable() else " " for char in str(value))[:200]


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _header_value(value: str) -> bool:
    return isinstance(value, str) and all(32 <= ord(char) < 127 for char in value)


def _ordinary_uuid(value: Any) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 128
        and not value.lower().startswith("pro-")
        and all(char.isascii() and (char.isalnum() or char in "-_") for char in value)
    )


def _row_uuid(row: dict) -> str | None:
    identities = [row[key] for key in ("uuid", "instance_uuid") if row.get(key) not in (None, "")]
    if not identities or any(not _ordinary_uuid(value) for value in identities):
        return None
    return identities[0] if all(value == identities[0] for value in identities) else None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class AutoDLClient:
    """Query owned instances; only an explicit start_instance call can start one."""

    PAGE_SIZE = 100
    MAX_PAGES = 1000
    MAX_ROWS = 100000
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, token: str, settings: AutoDLSettings, timeout: float = 20):
        if not _header_value(token) or not token.strip():
            raise AuthenticationError("AutoDL token 为空或格式无效，请检查所配置的环境变量。")
        if not _header_value(settings.app_version):
            raise APIError("AutoDL app_version 格式无效。")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise APIError("AutoDL 请求超时必须是正数。")
        self._token = token.strip()
        self._settings = settings
        self._timeout = timeout
        suffix = "/api/v1/sub_user/instance" if settings.sub_account else "/api/v1/instance"
        self._endpoint = "https://www.autodl.com" + suffix
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "autodl-gpu-watch/1",
        }
        if self._settings.app_version:
            headers["AppVersion"] = self._settings.app_version
        return headers

    def _fetch_page(self, page_index: int) -> dict:
        body = {
            "page_index": page_index,
            "page_size": self.PAGE_SIZE,
            "status": [],
            "charge_type": [],
            "name": "",
            "sub_name": self._settings.sub_account,
        }
        request = Request(self._endpoint, data=json.dumps(body).encode("utf-8"), headers=self._request_headers(), method="POST")
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                if status != 200:
                    self._http_error(status, response.headers.get("Retry-After"))
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            retry = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            self._http_error(status, retry)
            raise AssertionError("unreachable") from None
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError, HTTPException):
            raise APIError("AutoDL 请求失败，请检查网络、代理及 TLS 连接后重试。") from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise APIError("AutoDL 响应超过安全大小限制，本轮结果未采用。")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            raise APIError("AutoDL 未返回有效 JSON；登录可能失效或接口发生变化。") from None
        if not isinstance(payload, dict):
            raise APIError("AutoDL 响应结构无效，本轮结果未采用。")
        if payload.get("code") != "Success":
            code = payload.get("code")
            normalized = "".join(char for char in str(code).lower() if char.isalnum())
            if code in (401, 403) or any(part in normalized for part in ("authorizefailed", "unauthor", "unauthentic", "notlogin", "loginexpired", "tokenexpired", "tokeninvalid", "invalidtoken", "tokenexpire", "forbidden")):
                raise AuthenticationError("AutoDL 登录已失效或无访问权限，请更新 token 并核对主账号/子账号设置。")
            if code == 429 or any(part in normalized for part in ("ratelimit", "toomanyrequest", "requesttoofrequent")):
                raise RateLimitError()
            # Server messages can echo credentials or HTML; never expose them.
            raise APIError("AutoDL 拒绝了实例查询；请检查登录状态、账号设置或接口变化。")
        return payload

    def start_instance(self, instance_uuid: str) -> None:
        """Submit one ordinary-container GPU start; success means accepted only.

        Verified against the public console bundles on 2026-10-05:
        https://www.autodl.com/assets/instance.4daa8be5.js
        https://www.autodl.com/assets/index.bdc86294.js
        The ordinary start sends only instance_uuid. The Pro developer API's
        payload="gpu" parameter must not be added to this different endpoint.
        The caller must persist pending intent BEFORE this call, and confirm
        the later running GPU state by querying; this method never retries.
        """
        if self._settings.sub_account:
            raise StartRejectedError("自动开机暂不支持子账号设置，请使用主账号或在网页手动开机。")
        if not _ordinary_uuid(instance_uuid):
            raise StartRejectedError("自动开机需要普通容器的有效实例 UUID。")
        request = Request(
            "https://www.autodl.com/api/v1/instance/power_on",
            data=json.dumps({"instance_uuid": instance_uuid}).encode("utf-8"),
            headers=self._request_headers(), method="POST",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.getcode()
                if status != 200:
                    self._start_http_error(status, response.headers.get("Retry-After"))
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            retry = exc.headers.get("Retry-After") if exc.headers else None
            try:
                exc.close()
            except OSError:
                pass
            self._start_http_error(status, retry)
            raise AssertionError("unreachable") from None
        except (URLError, TimeoutError, socket.timeout, ssl.SSLError, OSError, HTTPException):
            raise StartOutcomeUnknownError("开机请求的结果暂不确定，已停止自动重发；将只查询实例状态，请勿重复开机。") from None
        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise StartOutcomeUnknownError("开机响应过大，无法确认结果；已停止自动重发，请检查实例状态。")
        try:
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            raise StartOutcomeUnknownError("开机响应无效，无法确认结果；已停止自动重发，请检查实例状态。") from None
        if not isinstance(payload, dict):
            raise StartOutcomeUnknownError("开机响应结构未知，已停止自动重发，请检查实例状态。")
        code = payload.get("code")
        if code == "Success":
            return
        if code == "AuthorizeFailed":
            raise AuthenticationError("AutoDL 登录已失效或无开机权限；已停止自动开机，请重新登录后手动确认。")
        if code == "TORecharge":
            raise StartRejectedError("AutoDL 因账户余额不足拒绝开机；已停止本轮自动开机，请在网页处理。")
        if code == "TORealName":
            raise StartRejectedError("AutoDL 要求完成实名认证后再开机；已停止本轮自动开机，请在网页处理。")
        # No public ordinary-console capacity rejection code has been verified.
        # Do not infer a retryable result from free-form server text or Pro codes.
        raise StartOutcomeUnknownError("AutoDL 返回了未识别的开机结果，已停止自动重发，请在网页确认实例状态。")

    @staticmethod
    def _start_http_error(status: int, retry: str | None = None) -> None:
        if status in (401, 403):
            raise AuthenticationError("AutoDL 登录已失效或无开机权限；已停止自动开机，请重新登录后手动确认。") from None
        if status == 429:
            raise RateLimitError("AutoDL 限制了开机请求频率，已停止本轮自动开机，请稍后检查实例状态。", _retry_after(retry)) from None
        if 300 <= status < 400:
            raise StartOutcomeUnknownError("开机接口返回重定向，已阻止携带凭据跳转及自动重发；请在网页确认实例状态。") from None
        raise StartOutcomeUnknownError("开机请求未返回可确认的结果，已停止自动重发，请在网页确认实例状态。") from None

    @staticmethod
    def _http_error(status: int, retry: str | None = None) -> None:
        if status in (401, 403):
            raise AuthenticationError("AutoDL 登录已失效或无访问权限，请更新 token 并核对主账号/子账号设置。") from None
        if status == 429:
            raise RateLimitError(retry_after=_retry_after(retry)) from None
        if 300 <= status < 400:
            raise AuthenticationError("AutoDL 返回了重定向，已阻止携带凭据跳转；请检查登录状态或接口地址。") from None
        raise APIError(f"AutoDL 请求返回 HTTP {status}，本轮结果未采用。") from None

    @staticmethod
    def _unpack(payload: dict, page_index: int) -> tuple[list[dict], int | None, bool | None]:
        data = payload.get("data")
        metadata = data if isinstance(data, dict) else {}
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            candidates = [data[key] for key in ("list", "result") if key in data]
            if len(candidates) != 1:
                raise APIError("AutoDL 实例列表字段缺失或有歧义，本轮结果未采用。")
            rows = candidates[0]
        else:
            raise APIError("AutoDL 实例列表结构无效，本轮结果未采用。")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise APIError("AutoDL 实例列表包含无效记录，本轮结果未采用。")
        totals = []
        for source in (metadata, payload):
            for key in ("result_total", "total", "total_count"):
                if key in source:
                    total = _integer(source[key])
                    if total is None:
                        raise APIError("AutoDL 分页总量无效，本轮结果未采用。")
                    totals.append(total)
        if totals and len(set(totals)) != 1:
            raise APIError("AutoDL 分页总量不一致，本轮结果未采用。")
        if "page_index" in metadata and _integer(metadata["page_index"]) != page_index:
            raise APIError("AutoDL 返回了错误的页码，本轮结果未采用。")
        has_more = metadata.get("has_more")
        if "has_more" in metadata and not isinstance(has_more, bool):
            raise APIError("AutoDL 分页标记无效，本轮结果未采用。")
        return rows, totals[0] if totals else None, has_more

    def fetch_instances(self) -> list[dict]:
        rows: list[dict] = []
        total: int | None = None
        seen_pages: set[bytes] = set()
        seen_uuids: set[str] = set()
        for page_index in range(1, self.MAX_PAGES + 1):
            page, page_total, has_more = self._unpack(self._fetch_page(page_index), page_index)
            if page_total is not None:
                if total is not None and total != page_total:
                    raise APIError("AutoDL 分页期间实例总量发生变化，本轮结果未采用。")
                total = page_total
                if total > self.MAX_ROWS:
                    raise APIError("AutoDL 实例数量超过安全上限，本轮结果未采用。")
            if not page:
                if (total is not None and len(rows) != total) or has_more is True:
                    raise APIError("AutoDL 分页提前结束，本轮结果未采用。")
                return rows
            signature = hashlib.sha256(json.dumps(page, sort_keys=True, ensure_ascii=True).encode("utf-8")).digest()
            if signature in seen_pages:
                raise APIError("AutoDL 重复返回相同分页，无法确认完整列表。")
            seen_pages.add(signature)
            for row in page:
                uuid = _identifier(row.get("uuid")) or _identifier(row.get("instance_uuid"))
                if uuid is not None:
                    if uuid in seen_uuids:
                        raise APIError("AutoDL 分页出现重复实例，本轮结果未采用。")
                    seen_uuids.add(uuid)
            rows.extend(page)
            if len(rows) > self.MAX_ROWS or (total is not None and len(rows) > total):
                raise APIError("AutoDL 分页数量超出预期，本轮结果未采用。")
            if total is not None and len(rows) == total:
                if has_more is True:
                    raise APIError("AutoDL 分页总量与后续页标记冲突，本轮结果未采用。")
                return rows
            if has_more is False:
                if total is not None:
                    raise APIError("AutoDL 分页提前结束，本轮结果未采用。")
                return rows
            # A short page alone is not proof of completion: the server might
            # impose a smaller page size than the one requested.
        raise APIError("AutoDL 分页超过安全上限，未返回不完整列表。")


def _matching_uuid_rows(instance_uuid: str, rows: list[dict]) -> list[dict]:
    return [row for row in rows if instance_uuid in (row.get("uuid"), row.get("instance_uuid"))]


def resolve_start_candidate(target: Target, rows: list[dict]) -> StartCandidate | None:
    """Resolve exactly one stopped ordinary instance with enough actual GPUs.

    Unlike notification thresholds, a smaller target.min_gpus must never reduce
    the instance's real GPU requirement. Ambiguous identities/counts fail closed.
    """
    if target.kind != "instance" or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    matches = _matching_uuid_rows(target.value, rows)
    if not matches:
        matches = [row for row in rows if row.get("name") == target.value]
    if len(matches) != 1:
        return None
    row = matches[0]
    instance_uuid = _row_uuid(row)
    if instance_uuid is None or len(_matching_uuid_rows(instance_uuid, rows)) != 1 or row.get("status") != "shutdown":
        return None
    actual = _integer(row.get("req_gpu_amount"))
    idle, total = _integer(row.get("gpu_idle_num")), _integer(row.get("gpu_all_num"))
    minimum = 0 if target.min_gpus is None else _integer(target.min_gpus)
    if actual is None or actual == 0 or minimum is None or (target.min_gpus is not None and minimum == 0):
        return None
    required = max(actual, minimum)
    if idle is None or total is None or not required <= idle <= total:
        return None
    machine_id = _identifier(row.get("machine_id"))
    if machine_id is not None:
        for other in rows:
            if _identifier(other.get("machine_id")) == machine_id:
                if (_integer(other.get("gpu_idle_num")), _integer(other.get("gpu_all_num"))) != (idle, total):
                    return None
    label = row.get("name")
    return StartCandidate(instance_uuid, _display(label if isinstance(label, str) and label else instance_uuid), required, idle)


def instance_start_status(instance_uuid: str, rows: list[dict]) -> str:
    """Confirm pending starts by UUID only, never by a mutable display name."""
    if not _ordinary_uuid(instance_uuid) or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return "unknown"
    matches = _matching_uuid_rows(instance_uuid, rows)
    if len(matches) != 1 or _row_uuid(matches[0]) != instance_uuid:
        return "unknown"
    row = matches[0]
    if row.get("status") == "running":
        return "running" if row.get("start_mode") in ("gpu", "normal") else "unknown"
    if row.get("status") == "starting":
        return "starting"
    if row.get("status") == "shutdown":
        return "shutdown"
    return "unknown"


def evaluate_target(target: Target, rows: list[dict]) -> Observation:
    """Use exact identities and fail closed on missing or contradictory data."""
    label = _display(target.value)
    required = _integer(target.min_gpus)

    def observation(available: bool | None, detail: str, idle: int | None = None) -> Observation:
        return Observation(target.key, label, available, idle, required, detail)

    if target.min_gpus is not None and (required is None or required == 0):
        return observation(None, "GPU 需求必须是正整数。")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return observation(None, "实例列表结构无效。")
    if target.kind == "machine":
        label = "机器 " + label
        if required is None:
            return observation(None, "机器监控必须显式设置 min_gpus。")
        matches = [row for row in rows if _identifier(row.get("machine_id")) == target.value]
        if not matches:
            return observation(None, "账号现有实例中未找到该 machine_id。")
        idle_values = [_integer(row.get("gpu_idle_num")) for row in matches]
        if any(value is None for value in idle_values):
            return observation(None, "该机器的空闲 GPU 数据缺失或无效。")
        if len(set(idle_values)) != 1:
            return observation(None, "同一机器的空闲 GPU 快照不一致，等待下次查询。")
        idle = idle_values[0]
        assert idle is not None
        totals = [_integer(row.get("gpu_all_num")) for row in matches if "gpu_all_num" in row]
        if any(value is None or value < idle for value in totals) or len(set(totals)) > 1:
            return observation(None, "该机器的 GPU 总量数据无效或不一致。")
        return observation(idle >= required, f"空闲 {idle} 张，需要 {required} 张。", idle)
    if target.kind != "instance":
        return observation(None, "不支持的监控目标类型。")
    matches = [row for row in rows if target.value in (_identifier(row.get("uuid")), _identifier(row.get("instance_uuid")))]
    if not matches:
        matches = [row for row in rows if row.get("name") == target.value]
    if not matches:
        return observation(None, "未找到实例，请使用完整名称或 UUID。")
    if len(matches) != 1:
        return observation(None, "实例标识匹配多条记录，请使用唯一 UUID。")
    row = matches[0]
    if isinstance(row.get("name"), str) and row["name"]:
        label = _display(row["name"])
    if target.min_gpus is None:
        required = _integer(row.get("req_gpu_amount"))
    if required is None or required == 0:
        return observation(None, "实例 GPU 需求缺失或无效。")
    status = row.get("status")
    if status == "running":
        if row.get("start_mode") in ("gpu", "normal"):
            return observation(False, "实例正在使用 GPU 运行，无需启动提醒。")
        if row.get("start_mode") != "non_gpu":
            return observation(None, "运行实例的启动模式未知。")
    elif status in ("starting", "stopping"):
        return observation(False, "实例正在开机或关机，暂不符合提醒条件。")
    elif status != "shutdown":
        return observation(None, "实例状态未知或暂不支持评估。")
    idle = _integer(row.get("gpu_idle_num"))
    if idle is None:
        return observation(None, "空闲 GPU 数量缺失或无效。")
    if "gpu_all_num" in row:
        total = _integer(row["gpu_all_num"])
        if total is None or idle > total:
            return observation(None, "GPU 总量缺失、无效或小于空闲数量。")
    return observation(idle >= required, f"空闲 {idle} 张，需要 {required} 张。", idle)
