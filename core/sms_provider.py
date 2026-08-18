# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - SMSBower：sms-activate 兼容文本接口
    - VAK：GET JSON 接口（getNumber/getSmsCode/setStatus）
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活；VAK 将 6 映射为 bad（号码已使用）。
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}


@dataclass(frozen=True)
class SmsProviderBinding:
    """Immutable provider configuration captured when an activation is taken.

    WebUI configuration reloads mutate ``config.codex`` in place.  An
    activation, however, belongs to the platform/account that issued its
    number; switching ``SMS_PROVIDER`` while the OTP is pending must not send
    the later poll or lifecycle update to a different platform.  ``values``
    is a shallow immutable snapshot of the uppercase config values plus the
    effective service/country overrides used for this activation.
    """

    provider: str
    values: Mapping[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


# Activation id -> the exact provider/config snapshot used by acquire_number.
# IDs are globally unique on the SMS platforms in normal operation.  Keeping
# this map local also preserves the historical tuple return value.
_ACTIVATION_BINDINGS: dict[str, SmsProviderBinding] = {}


def _capture_binding(
    provider: str | None = None,
    *,
    service: str | None = None,
    country: str | None = None,
) -> SmsProviderBinding:
    """Capture current Codex SMS settings before an activation is acquired."""
    current_provider = str(provider or _provider()).strip().lower()
    if current_provider in {"vaksms", "vak_sms", "vak-sms", "vakapi", "vak_api"}:
        current_provider = "vak"
    # Keep all uppercase settings so a future provider-specific option is
    # still frozen without requiring another lifecycle change.
    values = {
        key: value
        for key, value in vars(_cfg).items()
        if str(key).isupper()
    }
    if current_provider == "vak":
        effective_service = str(service or values.get("VAK_SMS_SERVICE") or "dr").strip()
        effective_country = str(
            country or values.get("VAK_SMS_COUNTRY") or values.get("SMS_COUNTRY") or "us"
        ).strip()
    elif current_provider == "smsbower":
        # SMSBower has its own service setting.  Do not accidentally freeze
        # the generic Grizzly/L service when the two fields differ.
        effective_service = str(service or values.get("SMSBOWER_SERVICE") or "dr").strip()
        effective_country = str(country or values.get("SMS_COUNTRY") or "").strip()
    elif current_provider in {"l", "h"}:
        # The local adapters intentionally require the shared SMS_SERVICE
        # value (H uses it as projectId); preserve their empty-value checks.
        effective_service = str(service or values.get("SMS_SERVICE") or "").strip()
        effective_country = str(country or values.get("SMS_COUNTRY") or "").strip()
    else:
        effective_service = str(service or values.get("SMS_SERVICE") or "openai").strip()
        effective_country = str(country or values.get("SMS_COUNTRY") or "").strip()
    values["__effective_service"] = effective_service
    values["__effective_country"] = effective_country
    return SmsProviderBinding(current_provider, MappingProxyType(values))


def _binding_for(activation_id: str | int, binding: SmsProviderBinding | None = None) -> SmsProviderBinding:
    """Resolve a lifecycle snapshot, falling back to current settings.

    The fallback keeps direct calls such as ``set_status('id', 6)`` working
    for callers that did not acquire through this module (and for old tests).
    """
    if binding is not None:
        return binding
    found = _ACTIVATION_BINDINGS.get(str(activation_id or "").strip())
    return found or _capture_binding()


def _remember_binding(activation_id: str | int, binding: SmsProviderBinding) -> None:
    key = str(activation_id or "").strip()
    if key:
        _ACTIVATION_BINDINGS[key] = binding


def _forget_binding(activation_id: str | int) -> None:
    key = str(activation_id or "").strip()
    if key:
        _ACTIVATION_BINDINGS.pop(key, None)


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsAuthenticationError(SmsProviderError):
    """API Key 无效；继续换号码不会恢复。"""


class SmsConfigurationError(SmsProviderError):
    """接码服务、国家或其他参数配置错误。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


def _http(binding: SmsProviderBinding | None = None) -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    timeout = binding.get("SMS_REQUEST_TIMEOUT", 30) if binding else getattr(_cfg, "SMS_REQUEST_TIMEOUT", 30)
    s.timeout = timeout
    return s


def _provider(binding: SmsProviderBinding | None = None) -> str:
    if binding is not None:
        return str(binding.provider or "grizzly").strip().lower()
    value = str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()
    if value in {"vaksms", "vak_sms", "vak-sms", "vakapi", "vak_api"}:
        return "vak"
    return value


def _setting(binding: SmsProviderBinding | None, key: str, default: Any = None) -> Any:
    if binding is not None:
        return binding.get(key, default)
    return getattr(_cfg, key, default)


def _sms_api_key(binding: SmsProviderBinding | None = None) -> str:
    """sms-activate 协议系 API Key：smsbower 用 SMSBOWER_API_KEY，否则用 SMS_API_KEY。"""
    if _provider(binding) == "vak":
        # VAK credentials are kept separate from the legacy sms-activate key;
        # silently sending the latter to VAK only produces a misleading
        # BAD_KEY and can make a configuration look active when it is not.
        return str(_setting(binding, "VAK_SMS_API_KEY", "") or "").strip()
    if _provider(binding) == "smsbower":
        return str(_setting(binding, "SMSBOWER_API_KEY", "") or "").strip() or str(_setting(binding, "SMS_API_KEY", "") or "").strip()
    return str(_setting(binding, "SMS_API_KEY", "") or "").strip()


def _sms_api_base(binding: SmsProviderBinding | None = None) -> str:
    """sms-activate 协议系基址：smsbower 用 SMSBOWER_API_BASE，否则用 SMS_API_BASE。"""
    if _provider(binding) == "vak":
        return str(_setting(binding, "VAK_SMS_API_BASE", "") or "https://vak-sms.com").strip()
    if _provider(binding) == "smsbower":
        return str(_setting(binding, "SMSBOWER_API_BASE", "") or "https://smsbower.page/stubs/handler_api.php").strip()
    return str(_setting(binding, "SMS_API_BASE", "") or "https://api.grizzlysms.com/stubs/handler_api.php").strip()


def _sms_service(default: str = "", binding: SmsProviderBinding | None = None) -> str:
    """服务码：smsbower 用 SMSBOWER_SERVICE（默认 dr=OpenAI），否则用 SMS_SERVICE。"""
    effective = _setting(binding, "__effective_service", "")
    if effective:
        return str(effective).strip()
    if _provider(binding) == "vak":
        return str(_setting(binding, "VAK_SMS_SERVICE", "") or "dr").strip() or default
    if _provider(binding) == "smsbower":
        return str(_setting(binding, "SMSBOWER_SERVICE", "") or "dr").strip() or default
    return str(_setting(binding, "SMS_SERVICE", "") or "openai").strip() or default


def _sms_country(binding: SmsProviderBinding | None = None) -> str:
    effective = _setting(binding, "__effective_country", "")
    if effective:
        return str(effective).strip()
    return str(_setting(binding, "SMS_COUNTRY", "") or "").strip()


def _sms_provider_ids(binding: SmsProviderBinding | None = None) -> str:
    """渠道号（providerIds，逗号分隔）。GrizzlySMS / SMSBower 都支持。"""
    return str(_setting(binding, "SMS_PROVIDER_IDS", "") or "").strip()


def _vak_client(
    http,
    *,
    service: str | None = None,
    country: str | None = None,
    binding: SmsProviderBinding | None = None,
):
    """Create a VAK client around the caller's existing HTTP session."""
    from core.vak_sms import VakSmsClient

    return VakSmsClient(
        api_key=str(_setting(binding, "VAK_SMS_API_KEY", "") or "").strip(),
        base_url=str(_setting(binding, "VAK_SMS_API_BASE", "https://vak-sms.com") or "https://vak-sms.com").strip(),
        service=str(
            service
            or _setting(binding, "__effective_service", "")
            or _setting(binding, "VAK_SMS_SERVICE", "dr")
            or "dr"
        ).strip(),
        country=str(
            country
            or _setting(binding, "__effective_country", "")
            or _setting(binding, "VAK_SMS_COUNTRY", "")
            or _setting(binding, "SMS_COUNTRY", "")
            or "us"
        ).strip(),
        operator=str(_setting(binding, "VAK_SMS_OPERATOR", "") or "").strip(),
        soft_id=str(_setting(binding, "VAK_SMS_SOFT_ID", "") or "").strip(),
        request_timeout=float(_setting(binding, "SMS_REQUEST_TIMEOUT", 30) or 30),
        poll_interval=float(_setting(binding, "VAK_SMS_POLL_INTERVAL", _setting(binding, "SMS_POLL_INTERVAL", 5)) or 5),
        success_status=str(_setting(binding, "VAK_SMS_SUCCESS_STATUS", "bad") or "bad"),
        cancel_status=str(_setting(binding, "VAK_SMS_CANCEL_STATUS", "end") or "end"),
        transport=http,
    )


def _vak_status(status: int | str, binding: SmsProviderBinding | None = None) -> str:
    """Translate the project's legacy activation states to VAK states.

    The shared provider API historically exposes sms-activate-style numeric
    values (1/3/6/8).  VAK's API intentionally uses ``send`` (request another
    SMS), ``end`` (release an unused number), and ``bad`` (mark a used number)
    instead; passing the numeric values through makes every Codex
    mark/complete/cancel request fail with ``badStatus``.
    """
    value = str(status or "").strip().lower()
    return {
        "1": "send",       # SMS was sent; wait for the first code
        "3": "send",       # request another SMS
        "6": str(_setting(binding, "VAK_SMS_SUCCESS_STATUS", "bad") or "bad").strip().lower(),
        "8": str(_setting(binding, "VAK_SMS_CANCEL_STATUS", "end") or "end").strip().lower(),
        "send": "send",
        "end": "end",
        "bad": "bad",
    }.get(value, value)


def _wrap_vak_error(exc: Exception) -> SmsProviderError:
    """Map VAK's classified errors to the long-standing provider API."""
    from core.vak_sms import (
        VakSmsAuthenticationError,
        VakSmsBalanceError,
        VakSmsCodeTimeout,
        VakSmsConfigurationError,
        VakSmsNoNumbersError,
        VakSmsProtocolError,
    )

    if isinstance(exc, VakSmsAuthenticationError):
        return SmsAuthenticationError(str(exc))
    if isinstance(exc, VakSmsBalanceError):
        return SmsNoBalanceError(str(exc))
    if isinstance(exc, VakSmsNoNumbersError):
        return SmsNoNumbersError(str(exc))
    if isinstance(exc, VakSmsCodeTimeout):
        return SmsCodeTimeout(str(exc))
    if isinstance(exc, VakSmsConfigurationError):
        return SmsConfigurationError(str(exc))
    if isinstance(exc, VakSmsProtocolError) and str(getattr(exc, "code", "") or "").upper() in {
        "BAD_SERVICE", "BAD_COUNTRY", "BAD_OPERATOR", "BAD_DATA",
    }:
        return SmsConfigurationError(str(exc))
    if isinstance(exc, SmsProviderError):
        return exc
    code = str(getattr(exc, "code", "") or "").strip().upper()
    suffix = f"（{code}）" if code else ""
    return SmsProviderError(f"VAK 接码失败{suffix}：{type(exc).__name__}: {exc}")


def is_fatal_error(exc: BaseException) -> bool:
    """Return True when another number/retry cannot repair the failure."""
    if isinstance(exc, (SmsAuthenticationError, SmsConfigurationError, SmsNoBalanceError)):
        return True
    text = str(exc or "").upper()
    return any(token in text for token in (
        "BAD_KEY", "NO_BALANCE", "CONFIGURATION_ERROR",
        "BAD_SERVICE", "BAD_COUNTRY", "BAD_OPERATOR", "BAD_DATA",
    ))


def _request_grizzly(
    http: CurlSession,
    params: dict,
    *,
    binding: SmsProviderBinding | None = None,
) -> str:
    """
    发一个 sms-activate 协议系（GrizzlySMS / SMSBower）API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    """
    base_params = {"api_key": _sms_api_key(binding)}
    base_params.update(params)
    resp = http.get(_sms_api_base(binding), params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"GrizzlySMS HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"该服务被平台禁售：{text}")

    return text


def list_countries() -> list[dict]:
    """调 getCountries 拉国家列表（sms-activate 协议系：GrizzlySMS / SMSBower）。

    返回 [{id, rus, eng, chn}]，按 id 排序。API Key 无效或解析失败返回 []。
    """
    if _provider() == "vak":
        http = _http()
        try:
            client = _vak_client(http)
            data = client.get_country_list()
            result = []
            items = data if isinstance(data, list) else None
            if items is None and isinstance(data, dict):
                for key in ("countries", "data", "items", "countryList"):
                    candidate = data.get(key)
                    if isinstance(candidate, list):
                        items = candidate
                        break
                if items is None and data:
                    # A few VAK gateways return an object keyed by country
                    # code instead of an array.
                    items = [
                        {"countryCode": key, **value}
                        for key, value in data.items()
                        if isinstance(value, dict)
                    ]
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("countryCode") or item.get("code") or item.get("id") or "").strip()
                if not cid:
                    continue
                operators = item.get("operatorList") or item.get("operators")
                result.append({
                    "id": cid,
                    "rus": str(item.get("countryName") or item.get("name") or ""),
                    "eng": str(item.get("countryName") or item.get("name") or ""),
                    "chn": str(item.get("countryName") or item.get("name") or ""),
                    "operators": operators if isinstance(operators, list) else [],
                })
            result.sort(key=lambda item: str(item.get("id") or "").lower())
            return result
        except Exception as exc:
            logger.warning("[SMS:VAK] getCountryList 拉取失败：%s", exc)
            return []
        finally:
            try:
                http.close()
            except Exception:
                pass

    http = _http()
    try:
        text = _request_grizzly(http, {"action": "getCountries"})
    except Exception as exc:
        logger.warning("[SMS] getCountries 拉取失败（可能 API Key 无效）：%s", exc)
        return []
    finally:
        try:
            http.close()
        except Exception:
            pass

    try:
        data = json.loads(text or "")
    except Exception:
        return []

    countries: list[dict] = []

    def _push(cid, info):
        if isinstance(info, dict):
            countries.append({
                "id": str(cid),
                "rus": str(info.get("rus") or info.get("name") or ""),
                "eng": str(info.get("eng") or info.get("en") or ""),
                "chn": str(info.get("chn") or info.get("zh") or info.get("cn") or ""),
            })

    if isinstance(data, dict):
        # 结构 A：{ "1": {"rus":..,"eng":..,"chn":..}, ... }
        for cid, info in data.items():
            _push(cid, info)
        # 结构 B：{"countries": [...]} 或 {"data": [...]}
        if not countries:
            for key in ("countries", "data", "items"):
                lst = data.get(key)
                if isinstance(lst, list):
                    for it in lst:
                        if isinstance(it, dict):
                            cid = it.get("id") or it.get("country_id") or it.get("country")
                            if cid is not None:
                                _push(cid, it)
                    break
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, dict):
                cid = it.get("id") or it.get("country_id") or it.get("country")
                if cid is not None:
                    _push(cid, it)

    # 去重并按 id 排序
    seen: set[str] = set()
    out = []
    for c in countries:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        out.append(c)
    out.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0)
    return out


def _l_url(path: str, binding: SmsProviderBinding | None = None) -> str:
    base = str(_setting(binding, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers(binding: SmsProviderBinding | None = None) -> dict:
    token = str(_setting(binding, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(
    http: CurlSession,
    path: str,
    payload: dict,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    resp = http.post(_l_url(path, binding), headers=_l_headers(binding), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str, binding: SmsProviderBinding | None = None) -> str:
    base = str(_setting(binding, "H_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers(binding: SmsProviderBinding | None = None) -> dict:
    token = str(_setting(binding, "H_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(
    http: CurlSession,
    path: str,
    payload: dict,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    resp = http.post(_h_url(path, binding), headers=_h_headers(binding), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    own_http = http is None
    http = http or _http(binding)
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"id": activation_id}, binding=binding)
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(
    ids: list[str],
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    own_http = http is None
    http = http or _http(binding)
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids}, binding=binding)
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http(binding)
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id}, binding=binding)
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(
    ids: list[str],
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http(binding)
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids}, binding=binding)
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str, binding: SmsProviderBinding | None = None) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(_setting(binding, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str, binding: SmsProviderBinding | None = None) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(_setting(binding, "H_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _h_phone_acquire_mode(binding: SmsProviderBinding | None = None) -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = str(_setting(binding, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable").strip().lower()
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    # Freeze the provider/config *before* making the request.  The resulting
    # tuple remains backwards compatible; the binding is kept privately by
    # activation id for all later lifecycle calls.
    binding = _capture_binding(service=service, country=country)
    provider = binding.provider
    own_http = http is None
    http = http or _http(binding)
    try:
        if provider == "vak":
            client = _vak_client(http, service=service, country=country, binding=binding)
            try:
                activation = client.acquire()
            except Exception as exc:
                raise _wrap_vak_error(exc) from exc
            _ACQUIRED_AT[activation.activation_id] = time.time()
            _remember_binding(activation.activation_id, binding)
            logger.info(
                "[SMS:VAK] 取号成功：id=%s, country=%s, service=%s, phone=+%s",
                activation.activation_id,
                activation.country,
                activation.service,
                activation.phone_number,
            )
            return activation.activation_id, activation.phone_number

        if provider == "l":
            payload = {
                "service": _sms_service(binding=binding),
                "country": _sms_country(binding),
            }
            max_price = _setting(binding, "SMS_MAX_PRICE", "")
            if max_price:
                payload["maxPrice"] = max_price

            data = _post_l_json(http, "/api/admin/l/take-phone", payload, binding=binding)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(_setting(binding, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone, binding)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            _remember_binding(activation_id, binding)
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if provider == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = _sms_service(binding=binding)
            h_country = _sms_country(binding)
            if not project_id:
                raise SmsProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode(binding)
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload, binding=binding)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(_setting(binding, "H_PHONE_PREFIX", "") or "")
            phone = _normalize_h_phone(raw_phone, binding)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            _remember_binding(activation_id, binding)
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return activation_id, phone

        params = {
            "action": "getNumber",
            "service": _sms_service(binding=binding),
            "country": _sms_country(binding),
        }
        max_price = _setting(binding, "SMS_MAX_PRICE", "")
        if max_price:
            params["maxPrice"] = max_price
        provider_ids = _sms_provider_ids(binding)
        if provider_ids:
            params["providerIds"] = provider_ids

        text = _request_grizzly(http, params, binding=binding)
        # 成功格式：ACCESS_NUMBER:激活ID:号码
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsProviderError(f"getNumber 非预期响应：{text[:200]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsProviderError(f"getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = parts[2].strip()
        _ACQUIRED_AT[activation_id] = time.time()
        _remember_binding(activation_id, binding)
        logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
        return activation_id, phone
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    binding = _binding_for(activation_id, binding)
    provider = binding.provider
    own_http = http is None
    http = http or _http(binding)
    configured_wait = int(
        (binding.get("SMS_CODE_WAIT", 120) if binding else getattr(_cfg, "SMS_CODE_WAIT", 120))
        or 120
    )
    deadline = time.time() + (max_wait or configured_wait)
    interval = poll_interval or (
        _setting(binding, "VAK_SMS_POLL_INTERVAL", _setting(binding, "SMS_POLL_INTERVAL", 5))
        if provider == "vak" else _setting(binding, "SMS_POLL_INTERVAL", 5)
    )
    try:
        total_wait = max_wait or configured_wait
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "vak":
                client = _vak_client(http, binding=binding)
                try:
                    code = client.get_code(
                        activation_id,
                        timeout=max(0, int(deadline - time.time())),
                        poll_interval=interval,
                        mark_sent=False,
                    )
                    logger.info(f"[SMS:VAK] 第 {round_no} 轮收到验证码：{code}")
                    return code
                except Exception as exc:
                    raise _wrap_vak_error(exc) from exc

            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id}, binding=binding)
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id}, binding=binding)
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id}, binding=binding)

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(
    activation_id: str,
    status: int | str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    binding = _binding_for(activation_id, binding)
    provider = binding.provider
    own_http = http is None
    http = http or _http(binding)
    try:
        if provider == "vak":
            try:
                return _vak_client(http, binding=binding).set_status(activation_id, _vak_status(status, binding))
            except Exception as exc:
                raise _wrap_vak_error(exc) from exc
        if provider == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status}")
            return "OK"
        return _request_grizzly(
            http,
            {"action": "setStatus", "status": str(status), "id": activation_id},
            binding=binding,
        )
    finally:
        if own_http:
            http.close()


def mark_sms_sent(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> None:
    """记录"短信已发出"钩子；VAK 不在这里发送重发请求。

    竞态说明：OpenAI 提交手机号后几秒内就把短信发出，验证码往往在代码进验证码页之前
    就已到达平台。此时平台会自动把激活状态推进到 STATUS_OK（码已就绪），再调 setStatus(1)
    会被平台拒绝并返回 BAD_STATUS——这是良性竞态，不是错误，绝不能当失败换号（会烧钱）。

    VAK 的 ``send`` 状态语义是“请求再次发送短信”，并非普通的已发送标记。因此首次
    提交号码后对 VAK 只记录日志，避免无意触发第二条短信；需要重发时调用
    :func:`request_sms_resend`。

    无论标记成败都继续走 getStatus 轮询（wait_for_sms_code）：码已到达时会立刻返回；
    真正确认"没码"的是 wait_for_sms_code 超时，由上层负责换号。这里任何失败都只告警。
    """
    binding = _binding_for(activation_id, binding)
    if binding.provider == "vak":
        logger.info("[SMS:VAK] 已记录短信发送，不触发 send 重发：activation_id=%s", activation_id)
        return
    try:
        set_status(activation_id, 1, http=http, binding=binding)
        logger.info(f"[SMS] 已标记短信已发出 activation_id={activation_id}")
    except SmsProviderError as exc:
        if "BAD_STATUS" in str(exc):
            logger.info("[SMS] setStatus(1) 返回 BAD_STATUS（验证码可能已提前到达），直接进入轮询")
        else:
            logger.warning("[SMS] setStatus(1) 标记失败，仍继续轮询：%s", str(exc)[:200])
    except Exception as exc:  # noqa: BLE001 —— 标记步骤是尽力而为，任何失败都不阻断轮询
        logger.warning("[SMS] setStatus(1) 标记异常，仍继续轮询：%s", str(exc)[:200])


def request_sms_resend(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> str:
    """显式请求当前激活再次发送短信。

    对 VAK 调用 ``setStatus(send)``；其他兼容平台使用历史状态 3。这个
    API 只应在页面明确要求重新发送时使用。
    """
    binding = _binding_for(activation_id, binding)
    if binding.provider != "vak":
        return set_status(activation_id, 3, http=http, binding=binding)
    own_http = http is None
    client_http = http or _http(binding)
    try:
        try:
            return _vak_client(client_http, binding=binding).request_resend(activation_id)
        except Exception as exc:
            raise _wrap_vak_error(exc) from exc
    finally:
        if own_http:
            client_http.close()


# Short alias for callers that use the provider client's terminology.
resend_sms = request_sms_resend


def complete(
    activation_id: str,
    http: CurlSession | None = None,
    *,
    binding: SmsProviderBinding | None = None,
) -> None:
    """标记激活完成（status=6；VAK 映射为 bad）。失败只告警不抛。"""
    binding = _binding_for(activation_id, binding)
    provider = binding.provider
    if provider == "vak":
        try:
            own_http = http is None
            client_http = http or _http(binding)
            try:
                completed = _vak_client(client_http, binding=binding).complete(activation_id)
                if completed:
                    logger.info(f"[SMS:VAK] 已结束激活 id={activation_id}")
                    _ACQUIRED_AT.pop(activation_id, None)
                    _forget_binding(activation_id)
                else:
                    logger.warning(f"[SMS:VAK] 结束激活未确认 id={activation_id}")
            finally:
                if own_http:
                    client_http.close()
        except Exception as exc:
            logger.warning(f"[SMS:VAK] 结束激活失败（不影响结果）：{exc}")
        return
    if provider == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        _forget_binding(activation_id)
        return
    if provider == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        _forget_binding(activation_id)
        return
    try:
        set_status(activation_id, 6, http=http, binding=binding)
        logger.info(f"[SMS] 已标记完成 activation_id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        _forget_binding(activation_id)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")


def _do_cancel_sync(
    activation_id: str,
    http_factory,
    binding: SmsProviderBinding | None = None,
) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS] 取消等待 GrizzlySMS 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = http_factory()
    try:
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http, binding=binding)
                logger.info(f"[SMS] 已取消 activation_id={activation_id}")
                _ACQUIRED_AT.pop(activation_id, None)
                _forget_binding(activation_id)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS] 取消最终失败（不影响结果，需到平台手动取消）：activation_id={activation_id}, {exc}"
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(
    activation_id: str,
    http: CurlSession | None = None,
    background: bool = True,
    *,
    bad: bool = False,
    binding: SmsProviderBinding | None = None,
) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    ``bad=True`` 用于已经收到验证码但业务校验失败的号码；VAK 会直接使用
    ``bad`` 状态，其他兼容平台映射为完成状态。失败只告警不抛，不影响主流程。
    """
    binding = _binding_for(activation_id, binding)
    provider = binding.provider
    if provider == "vak":
        cancelled = False
        try:
            own_http = http is None
            client_http = http or _http(binding)
            try:
                cancelled = _vak_client(client_http, binding=binding).cancel(activation_id, bad=bad)
                if cancelled:
                    logger.info(f"[SMS:VAK] 已取消激活%s id={activation_id}", "（bad）" if bad else "")
                else:
                    logger.warning(f"[SMS:VAK] 号码状态仍未结束%s id={activation_id}", "（bad）" if bad else "")
            finally:
                if own_http:
                    client_http.close()
        except Exception as exc:
            logger.warning(f"[SMS:VAK] 取消号码失败（不影响主流程）：id={activation_id}, {exc}")
        # Keep the snapshot and acquisition timestamp when VAK did not
        # confirm the transition.  A later cleanup/reconciliation call must
        # still target the original VAK account, even if WebUI settings have
        # since selected another provider.
        if cancelled:
            _ACQUIRED_AT.pop(activation_id, None)
            _forget_binding(activation_id)
        return

    if bad:
        try:
            set_status(activation_id, 6, http=http, binding=binding)
            logger.info(f"[SMS] 已将收到验证码的激活标记完成 activation_id={activation_id}")
        except Exception as exc:
            logger.warning(f"[SMS] 已收码号码标记完成失败（不影响主流程）：activation_id={activation_id}, {exc}")
        _ACQUIRED_AT.pop(activation_id, None)
        _forget_binding(activation_id)
        return

    if provider == "l":
        try:
            _release_l_number(activation_id, http=http, binding=binding)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
            _forget_binding(activation_id)
        return
    if provider == "h":
        try:
            _release_h_number(activation_id, http=http, binding=binding)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
            _forget_binding(activation_id)
        return

    if not background:
        _do_cancel_sync(activation_id, lambda: _http(binding), binding)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, lambda: _http(binding), binding),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")
