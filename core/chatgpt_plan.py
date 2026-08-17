# -*- coding: utf-8 -*-
"""ChatGPT 账号套餐/试用资格查询。"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import random
import re
import socket
import time
import urllib.request as urllib_request
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlparse
from urllib.request import getproxies

from core.session import BrowserSession

logger = logging.getLogger(__name__)

ACCOUNTS_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
CANCEL_SUBSCRIPTION_PATH = "/backend-api/subscriptions/cancel"
PROTOCOL_IMPERSONATE = "chrome"
PROTOCOL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)^(?:access[_-]?token|refresh[_-]?token|token|authorization|"
    r"password|passwd|secret|client[_-]?secret|cookie|set-cookie|"
    r"proxy(?:[_-](?:url|username|user|password|pass|credentials))?)$"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PROXY_CREDENTIAL_RE = re.compile(r"(?i)(https?|socks5h?)://[^\s/@:]+:[^\s/@]+@")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_token(token: str) -> str:
    token = (token or "").strip().strip('"').strip("'")
    if token.lower().startswith("authorization:"):
        token = token.split(":", 1)[1].strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _mask_proxy(proxy: str) -> str:
    """返回可用于日志/API 结果的代理摘要，不泄露用户名和密码。"""
    value = str(proxy or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        scheme = f"{parsed.scheme}://" if parsed.scheme else ""
        auth = "***:***@" if parsed.username or parsed.password else ""
        return f"{scheme}{auth}{host}{port}" or "***"
    except Exception:
        return "***"


def _local_proxy_status(proxy: str) -> tuple[bool, bool, str | None]:
    """检查回环代理端口；非本地代理不做预探测，避免额外网络请求。"""
    value = str(proxy or "").strip()
    if not value:
        return False, False, None
    try:
        parsed = urlparse(value if "://" in value else f"//{value}")
        host = parsed.hostname or ""
        is_loopback = host.lower() == "localhost"
        if not is_loopback:
            try:
                is_loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                is_loopback = False
        if not is_loopback:
            return False, True, None
        if not parsed.port:
            return True, False, "本地代理未配置端口"
        try:
            with socket.create_connection((host, parsed.port), timeout=0.5):
                return True, True, None
        except OSError as exc:
            return True, False, f"本地代理 {host}:{parsed.port} 未监听（{type(exc).__name__}）"
    except Exception as exc:
        return False, False, f"代理地址解析失败（{type(exc).__name__}）"


def _proxy_endpoint(proxy: str) -> tuple[str, int] | None:
    """Return a credential/scheme-insensitive endpoint for proxy deduplication."""
    value = str(proxy or "").strip()
    if not value:
        return None
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        host = str(parsed.hostname or "").strip().lower()
        if not host:
            return None
        try:
            if host == "localhost" or ipaddress.ip_address(host).is_loopback:
                host = "loopback"
        except ValueError:
            pass
        default_port = {
            "http": 80,
            "https": 443,
            "socks": 1080,
            "socks5": 1080,
            "socks5h": 1080,
        }.get(str(parsed.scheme or "http").lower(), 0)
        return host, int(parsed.port or default_port)
    except Exception:
        return None


def _same_proxy_endpoint(left: str, right: str) -> bool:
    left_endpoint = _proxy_endpoint(left)
    return bool(left_endpoint and left_endpoint == _proxy_endpoint(right))


def _is_loopback_proxy(proxy: str) -> bool:
    endpoint = _proxy_endpoint(proxy)
    return bool(endpoint and endpoint[0] == "loopback")


def _system_proxy_url() -> str:
    """Resolve the OS HTTPS proxy without exposing it in logs."""
    try:
        configured = getproxies() or {}
    except Exception:
        configured = {}
    # On Windows, urllib prefers environment variables whenever it sees any
    # name ending in _PROXY. Project settings such as PROXY_PRE_PROXY then hide
    # the real Internet Settings entries, so consult the registry directly.
    if not any(configured.get(key) for key in ("https", "all", "http")):
        registry_reader = getattr(urllib_request, "getproxies_registry", None)
        if callable(registry_reader):
            try:
                registry_values = registry_reader() or {}
            except Exception:
                registry_values = {}
            configured = {**registry_values, **configured}
    for key in ("https", "all", "http"):
        value = str(configured.get(key) or "").strip()
        if not value:
            continue
        if "://" not in value:
            value = f"http://{value}"
        if _proxy_endpoint(value) is None:
            continue
        is_local, available, _ = _local_proxy_status(value)
        if is_local and not available:
            continue
        return value.rstrip("/")
    return ""


def _route_pre_proxy_policy(selected: str, pre_proxy: str, *, force_disable: bool = False) -> tuple[bool, str | None]:
    """Choose whether a route may inherit PROXY_PRE_PROXY.

    Local/system proxies are already complete egress routes. Chaining a local
    proxy through another local proxy can recurse back into the same listener.
    """
    disable = bool(
        force_disable
        or not str(selected or "").strip()
        or _is_loopback_proxy(selected)
        or _same_proxy_endpoint(selected, pre_proxy)
    )
    return disable, None if disable else (_mask_proxy(pre_proxy) or None)


def _system_proxy_route(system_proxy: str, *, mode: str) -> dict:
    return {
        "proxy": system_proxy,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(system_proxy) or None,
        "proxy_fallback_reason": "using available system proxy",
        "proxy_pre_proxy": None,
        "proxy_source": "system",
        "_disable_pre_proxy": True,
    }


def resolve_plan_check_route(explicit_proxy: Optional[str] = None) -> dict:
    """解析套餐查询的实际网络路径。

    explicit_proxy 不是 None 时表示 API 调用方明确覆盖配置；空字符串代表直连。
    """
    from config import proxy as proxy_cfg

    pre_proxy = str(getattr(proxy_cfg, "PROXY_PRE_PROXY", "") or "").strip()

    if explicit_proxy is not None:
        selected = str(explicit_proxy or "").strip()
        disable_pre_proxy, pre_proxy_meta = _route_pre_proxy_policy(selected, pre_proxy)
        return {
            "proxy": selected,
            "proxy_mode": "request",
            "network_route": "proxy" if selected else "direct",
            "proxy_used": _mask_proxy(selected) or None,
            "proxy_fallback_reason": None,
            "proxy_pre_proxy": pre_proxy_meta,
            "_disable_pre_proxy": disable_pre_proxy,
        }

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if mode not in {"auto", "proxy", "direct"}:
        raise ValueError(f"PLAN_CHECK_PROXY_MODE={mode!r} 无效，可选 auto / proxy / direct")
    if mode == "direct":
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": None,
            "proxy_pre_proxy": None,
            "_disable_pre_proxy": True,
        }

    selected = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    if not selected:
        selected = str(proxy_cfg.pick_proxy() or "").strip()
    if not selected:
        if mode == "proxy":
            raise ValueError("套餐查询网络模式为 proxy，但未配置 PLAN_CHECK_PROXY 或 PROXY_POOL")
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct",
            "proxy_used": None,
            "proxy_fallback_reason": "未配置套餐查询代理或代理池",
            "proxy_pre_proxy": None,
            "_disable_pre_proxy": True,
        }

    is_local, available, reason = _local_proxy_status(selected)
    if mode == "auto" and is_local and not available:
        return {
            "proxy": "",
            "proxy_mode": mode,
            "network_route": "direct_fallback",
            "proxy_used": _mask_proxy(selected),
            "proxy_fallback_reason": reason,
            "proxy_pre_proxy": None,
            "_disable_pre_proxy": True,
        }
    disable_pre_proxy, pre_proxy_meta = _route_pre_proxy_policy(selected, pre_proxy)
    return {
        "proxy": selected,
        "proxy_mode": mode,
        "network_route": "proxy",
        "proxy_used": _mask_proxy(selected),
        "proxy_fallback_reason": None,
        "proxy_pre_proxy": pre_proxy_meta,
        "_disable_pre_proxy": disable_pre_proxy,
    }


def _plan_check_routes(explicit_proxy: Optional[str], max_attempts: int) -> list[dict]:
    """Build an ordered list of distinct routes for one plan-check task."""
    explicit_value = None if explicit_proxy is None else str(explicit_proxy or "").strip()
    # An explicitly empty proxy means direct-only. Configured direct mode has the
    # same meaning, so neither case silently switches back to the proxy pool.
    if explicit_value == "":
        return [resolve_plan_check_route(explicit_proxy)]

    from config import proxy as proxy_cfg

    mode = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY_MODE", "auto") or "auto").strip().lower()
    if explicit_value is None and mode == "direct":
        return [resolve_plan_check_route(explicit_proxy)]

    system_proxy = _system_proxy_url() if explicit_value is None else ""
    # auto 模式优先走当前系统代理。这个出口已经被桌面环境实际使用，
    # 比随机住宅代理更适合作为短时套餐查询的第一条路径。
    if mode == "auto" and system_proxy:
        first_route = _system_proxy_route(system_proxy, mode=mode)
    else:
        try:
            first_route = resolve_plan_check_route(explicit_proxy)
        except ValueError:
            if not system_proxy:
                raise
            first_route = _system_proxy_route(system_proxy, mode=mode)
    if (
        system_proxy
        and not str(first_route.get("proxy") or "").strip()
        and first_route.get("network_route") in {"direct", "direct_fallback"}
    ):
        first_route = _system_proxy_route(system_proxy, mode=mode)
    elif system_proxy and _same_proxy_endpoint(str(first_route.get("proxy") or ""), system_proxy):
        first_route.update(_system_proxy_route(str(first_route.get("proxy") or system_proxy), mode=mode))

    routes = [first_route]
    if max_attempts <= 1:
        return routes

    configured_pool = getattr(proxy_cfg, "PROXY_POOL", []) or []
    if isinstance(configured_pool, str):
        configured_pool = [configured_pool]
    pool = [str(item or "").strip() for item in configured_pool]
    pool = [item for item in pool if item]
    random.shuffle(pool)

    candidates: list[str] = []
    configured_proxy = str(getattr(proxy_cfg, "PLAN_CHECK_PROXY", "") or "").strip()
    if configured_proxy:
        candidates.append(configured_proxy)
    candidates.extend(pool)

    seen_candidates = {explicit_value} if explicit_value else set()
    first_proxy = str(first_route.get("proxy") or "").strip()
    if first_proxy:
        seen_candidates.add(first_proxy)
    first_proxy_mask = str(first_route.get("proxy_used") or "").strip()
    seen_routes = {
        (first_proxy, str(first_route.get("network_route") or ""))
    }

    system_already_used = bool(
        system_proxy and _same_proxy_endpoint(first_proxy, system_proxy)
    )
    reserve_system_slot = bool(system_proxy and not system_already_used)
    normal_route_limit = max_attempts - (1 if reserve_system_slot else 0)

    for candidate in candidates:
        if candidate in seen_candidates:
            continue
        if system_proxy and _same_proxy_endpoint(candidate, system_proxy):
            seen_candidates.add(candidate)
            continue
        # In auto mode an unavailable local proxy is represented as a direct
        # fallback, with only its masked value retained in route metadata.
        if not first_proxy and first_proxy_mask and _mask_proxy(candidate) == first_proxy_mask:
            seen_candidates.add(candidate)
            continue
        seen_candidates.add(candidate)
        candidate_route = resolve_plan_check_route(candidate)
        route_proxy = str(candidate_route.get("proxy") or "").strip()
        route_key = (route_proxy, str(candidate_route.get("network_route") or ""))
        if route_key in seen_routes:
            continue
        seen_routes.add(route_key)
        routes.append(candidate_route)
        if len(routes) >= normal_route_limit:
            break

    if reserve_system_slot and len(routes) < max_attempts:
        routes.append(_system_proxy_route(system_proxy, mode=mode))
    elif system_already_used and len(routes) == 1 and max_attempts > 1:
        # A lone system route still benefits from bounded transient retries.
        routes.extend(
            _system_proxy_route(system_proxy, mode=mode)
            for _ in range(max_attempts - 1)
        )
    return routes


def decode_jwt_payload_unverified(token: str) -> dict:
    """仅本地解析 JWT payload，不校验签名。"""
    token = normalize_token(token)
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def token_claims(token: str) -> dict:
    payload = decode_jwt_payload_unverified(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    profile = payload.get("https://api.openai.com/profile") or {}
    exp = payload.get("exp")
    exp_iso = None
    expired = None
    if isinstance(exp, (int, float)):
        exp_iso = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        expired = datetime.now(tz=timezone.utc).timestamp() >= float(exp)
    return {
        "payload": payload,
        "email": profile.get("email"),
        "user_name": profile.get("name"),
        "user_id": auth.get("chatgpt_user_id") or auth.get("user_id"),
        "account_id": auth.get("chatgpt_account_id"),
        "claim_plan_type": auth.get("chatgpt_plan_type"),
        "exp": exp,
        "token_expires_at": exp_iso,
        "token_expired": expired,
    }


def _optional_bool(value: Any) -> bool | None:
    """Normalize an API boolean without turning a missing value into False."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _first_present(mapping: dict, *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def classify_subscription_status(result: dict) -> str:
    """Return a stable subscription state from parsed or raw account data.

    Missing booleans intentionally stay unknown.  A cancellation timestamp is
    stronger evidence than ``will_renew`` because some responses omit the
    latter after a cancellation has already been scheduled.
    """
    if not isinstance(result, dict):
        return "unknown"

    entitlement = result.get("entitlement")
    if not isinstance(entitlement, dict):
        entitlement = {}
    last_subscription = result.get("last_active_subscription")
    if not isinstance(last_subscription, dict):
        last_subscription = {}

    active_value = _first_present(result, "has_active_subscription")
    if active_value is None and "has_active_subscription" in entitlement:
        active_value = entitlement.get("has_active_subscription")
    active = _optional_bool(active_value)

    will_renew_value = _first_present(result, "last_will_renew", "will_renew")
    if will_renew_value is None and "will_renew" in last_subscription:
        will_renew_value = last_subscription.get("will_renew")
    will_renew = _optional_bool(will_renew_value)

    cancels_at = _first_present(
        result,
        "cancels_at",
        "plan_cancels_at",
        "subscription_cancels_at",
    )
    if not cancels_at:
        cancels_at = entitlement.get("cancels_at")

    if active is False:
        return "none"
    if active is not True:
        return "unknown"
    if cancels_at:
        return "cancel_scheduled"
    if will_renew is True:
        return "renewing"
    if will_renew is False:
        return "active_nonrenewing"
    return "unknown"


def _redact_diagnostic_value(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Redact credentials while preserving enough of an API error to diagnose it."""
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _SENSITIVE_KEY_RE.search(str(key))
                else _redact_diagnostic_value(item, secrets=secrets)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_diagnostic_value(item, secrets=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_diagnostic_value(item, secrets=secrets) for item in value)
    if not isinstance(value, str):
        return value

    text = value
    for secret in secrets:
        normalized = str(secret or "").strip()
        if normalized:
            text = text.replace(normalized, "[REDACTED]")
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _PROXY_CREDENTIAL_RE.sub(r"\1://[REDACTED]@", text)
    return _JWT_RE.sub("[REDACTED_JWT]", text)


def _response_text(response: Any) -> str:
    value = getattr(response, "text", "")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _safe_response_preview(
    response: Any,
    *,
    secrets: tuple[str, ...] = (),
    limit: int = 500,
) -> str:
    text = _response_text(response).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        sanitized = _redact_diagnostic_value(text, secrets=secrets)
    else:
        sanitized = json.dumps(
            _redact_diagnostic_value(parsed, secrets=secrets),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return str(sanitized).replace("\r", " ").replace("\n", " ")[: max(1, int(limit))]


def _safe_exception_detail(exc: BaseException, *, secrets: tuple[str, ...] = ()) -> str:
    detail = str(_redact_diagnostic_value(str(exc or ""), secrets=secrets)).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _http_failure_fields(
    response: Any,
    *,
    phase: str,
    label: str,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    status = int(getattr(response, "status_code", 0) or 0)
    preview = _safe_response_preview(response, secrets=secrets)
    error = f"{label} HTTP {status}"
    if preview:
        error = f"{error}: {preview}"
    result: dict[str, Any] = {
        "phase": phase,
        "http_status": status,
        "error": error,
    }
    if preview:
        result["response_preview"] = preview
    headers = getattr(response, "headers", None)
    if headers is not None and callable(getattr(headers, "get", None)):
        request_id = headers.get("x-request-id") or headers.get("cf-ray")
        if request_id:
            result["request_id"] = str(request_id)[:200]
    return result


def _response_json_object(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        text = _response_text(response).strip()
        data = json.loads(text) if text else None
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    return data


def _common_headers(
    env: BrowserSession,
    token: str,
    target_path: str = ACCOUNTS_CHECK_PATH,
) -> dict[str, str]:
    # Keep this byte-for-byte aligned with the proven standalone protocol
    # client. BrowserSession is still used for its proxy/pre-proxy transport.
    return {
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "authorization": f"Bearer {normalize_token(token)}",
        "content-type": "application/json",
        "oai-device-id": env.device_id,
        "oai-language": "zh-CN",
        "origin": "https://chatgpt.com",
        "referer": "https://chatgpt.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": PROTOCOL_USER_AGENT,
        "x-openai-target-path": target_path,
        "x-openai-target-route": target_path,
    }


def _new_protocol_session(proxy: str, *, pre_proxy: str | None = None) -> BrowserSession:
    env = BrowserSession(proxy=proxy, pre_proxy=pre_proxy, detect_exit_geo=False)
    # The standalone reference client constructs curl-cffi with
    # Session(impersonate="chrome"). Keep the protocol-only transport aligned
    # while retaining BrowserSession's proxy and pre-proxy wiring.
    env.session.impersonate = PROTOCOL_IMPERSONATE
    return env


def _protocol_session_for_route(route: dict) -> BrowserSession:
    proxy = str(route.get("proxy") or "")
    disable_pre_proxy = bool(route.get("_disable_pre_proxy")) or not proxy
    return _new_protocol_session(
        proxy,
        pre_proxy="" if disable_pre_proxy else None,
    )


def _public_route_meta(route: dict) -> dict:
    return {
        key: value
        for key, value in route.items()
        if key != "proxy" and not str(key).startswith("_")
    }


def _select_remote_account(data: dict, requested_account_id: str = "") -> tuple[str, dict]:
    """Select one account while refusing an ambiguous or mismatched response."""
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("response is missing accounts")

    requested = str(requested_account_id or "").strip()
    if requested:
        direct = accounts.get(requested)
        if isinstance(direct, dict):
            remote_id = str((direct.get("account") or {}).get("account_id") or requested).strip()
            if remote_id != requested:
                raise ValueError("remote account_id does not match token account_id")
            return remote_id, direct
        for key, item in accounts.items():
            if not isinstance(item, dict):
                continue
            remote_id = str((item.get("account") or {}).get("account_id") or key).strip()
            if remote_id == requested:
                return remote_id, item
        raise ValueError("response has no account matching token account_id")

    candidates: dict[str, dict] = {}
    for key, item in accounts.items():
        if not isinstance(item, dict):
            continue
        remote_id = str((item.get("account") or {}).get("account_id") or "").strip()
        if not remote_id and key != "default":
            remote_id = str(key).strip()
        if remote_id:
            candidates[remote_id] = item
    if len(candidates) != 1:
        raise ValueError("response does not identify one remote account_id")
    return next(iter(candidates.items()))


def _parse_remote_plan(data: dict, token: str, requested_account_id: str = "") -> dict:
    remote_id, item = _select_remote_account(data, requested_account_id)
    parsed = parse_accounts_check({"accounts": {remote_id: item}}, token=token)
    # parse_accounts_check also exposes JWT claims; restore the ID proven by
    # this response so a local DB id or a mismatched claim can never be posted.
    parsed["account_id"] = remote_id
    parsed["remote_account_id"] = remote_id
    return parsed


def verify_account_subscription_protocol(
    token: str,
    *,
    expected_email: str = "",
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
) -> dict:
    """Read subscription state for exactly the account identified by the AT."""
    token = normalize_token(token)
    claims = token_claims(token) if token else {}
    token_email = str(claims.get("email") or "").strip()
    local_email = str(expected_email or "").strip()
    base = {
        "ok": False,
        "checked_at": now_iso(),
        "protocol": "protocol",
        "phase": "validate",
    }
    if not token:
        return {**base, "reason": "missing_token", "error": "token is empty"}
    if local_email and token_email and local_email.casefold() != token_email.casefold():
        return {
            **base,
            "reason": "account_email_mismatch",
            "error": "local email does not match token email",
        }
    if claims.get("token_expired") is True:
        return {
            **base,
            "reason": "token_expired",
            "token_expired": True,
            "needs_live_check": True,
            "error": "access token is expired",
        }

    env = None
    response = None
    route_meta: dict[str, Any] = {}
    phase = "route"
    secrets = (token, str(proxy or ""))
    try:
        route = resolve_plan_check_route(proxy)
        route_meta = _public_route_meta(route)
        secrets = (token, str(route.get("proxy") or ""))
        timeout_seconds, _, _ = _plan_check_settings(timeout, 1, 0)
        env = _protocol_session_for_route(route)
        query_path = f"{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
        phase = "query"
        response = env.session.get(
            f"https://chatgpt.com{query_path}",
            headers=_common_headers(env, token, ACCOUNTS_CHECK_PATH),
            allow_redirects=False,
            timeout=timeout_seconds,
        )
        http_status = int(response.status_code)
        if not 200 <= http_status < 300:
            is_auth_expired = http_status == 401
            return {
                **base,
                "reason": "protocol_check_failed",
                "retryable": _retryable_plan_error(http_status),
                "token_expired": True if is_auth_expired else claims.get("token_expired"),
                "needs_live_check": is_auth_expired,
                **_http_failure_fields(
                    response,
                    phase="query",
                    label="subscription verification",
                    secrets=secrets,
                ),
                **route_meta,
            }
        requested_id = str(claims.get("account_id") or "").strip()
        phase = "query_parse"
        parsed = _parse_remote_plan(_response_json_object(response), token, requested_id)
        parsed.update({
            "protocol": "protocol",
            "phase": "query",
            "http_status": http_status,
            "retryable": False,
            **route_meta,
        })
        return parsed
    except Exception as exc:
        result = {
            **base,
            "reason": "protocol_exception",
            "phase": phase,
            "retryable": True,
            "error": f"subscription verification failed: {_safe_exception_detail(exc, secrets=secrets)}",
            **route_meta,
        }
        preview = _safe_response_preview(response, secrets=secrets) if response is not None else ""
        if preview:
            result["response_preview"] = preview
        if response is not None and getattr(response, "status_code", None) is not None:
            result["http_status"] = int(response.status_code)
        return result
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def cancel_account_subscription_protocol(
    token: str,
    *,
    expected_email: str = "",
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    verification_delays: tuple[float, ...] = (0.8, 1.5, 3.0),
) -> dict:
    """Run a fresh GET -> one POST -> verification GETs in one session."""
    token = normalize_token(token)
    claims = token_claims(token) if token else {}
    token_email = str(claims.get("email") or "").strip()
    local_email = str(expected_email or "").strip()
    base = {
        "ok": False,
        "checked_at": now_iso(),
        "protocol": "protocol",
        "phase": "validate",
        "posted": False,
        "confirmed": False,
    }
    if not token:
        return {**base, "reason": "missing_token", "error": "token is empty"}
    if local_email and token_email and local_email.casefold() != token_email.casefold():
        return {
            **base,
            "reason": "account_email_mismatch",
            "error": "local email does not match token email",
        }
    if claims.get("token_expired") is True:
        return {**base, "reason": "token_expired", "error": "access token is expired"}

    env = None
    route_meta: dict[str, Any] = {}
    phase = "route"
    current_response = None
    secrets = (token, str(proxy or ""))
    try:
        route = resolve_plan_check_route(proxy)
        route_meta = _public_route_meta(route)
        secrets = (token, str(route.get("proxy") or ""))
        timeout_seconds, _, _ = _plan_check_settings(timeout, 1, 0)
        env = _protocol_session_for_route(route)
        query_path = f"{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
        requested_id = str(claims.get("account_id") or "").strip()

        phase = "precheck"
        before_response = env.session.get(
            f"https://chatgpt.com{query_path}",
            headers=_common_headers(env, token, ACCOUNTS_CHECK_PATH),
            allow_redirects=False,
            timeout=timeout_seconds,
        )
        current_response = before_response
        before_status = int(before_response.status_code)
        if not 200 <= before_status < 300:
            is_auth_expired = before_status == 401
            return {
                **base,
                "reason": "protocol_check_failed",
                "retryable": _retryable_plan_error(before_status),
                "token_expired": True if is_auth_expired else claims.get("token_expired"),
                "needs_live_check": is_auth_expired,
                **_http_failure_fields(
                    before_response,
                    phase="precheck",
                    label="pre-cancel plan check",
                    secrets=secrets,
                ),
                **route_meta,
            }
        phase = "precheck_parse"
        before = _parse_remote_plan(_response_json_object(before_response), token, requested_id)
        remote_id = str(before.get("remote_account_id") or "").strip()
        if not remote_id:
            return {
                **base,
                "phase": "precheck_parse",
                "reason": "missing_remote_account_id",
                "http_status": before_status,
                "error": "missing remote account_id",
                **route_meta,
            }

        active = _optional_bool(before.get("has_active_subscription"))
        if active is False or classify_subscription_status(before) == "none":
            return {
                **base,
                "ok": True,
                "confirmed": True,
                "phase": "precheck",
                "reason": "none",
                "http_status": before_status,
                "before": before,
                **route_meta,
            }
        if before.get("cancels_at") or _optional_bool(before.get("last_will_renew")) is False:
            return {
                **base,
                "ok": True,
                "confirmed": True,
                "phase": "precheck",
                "reason": "already_cancelled",
                "http_status": before_status,
                "before": before,
                **route_meta,
            }
        origin = str(before.get("last_purchase_origin_platform") or "").strip().lower().replace("-", "_")
        if origin in {"apple", "app_store", "appstore", "ios", "google", "google_play", "googleplay", "android"}:
            return {
                **base,
                "ok": True,
                "phase": "precheck",
                "reason": "mobile_store",
                "http_status": before_status,
                "before": before,
                **route_meta,
            }
        if active is not True:
            return {
                **base,
                "phase": "precheck",
                "reason": "protocol_status_unknown",
                "http_status": before_status,
                "error": "active subscription state is unknown",
                "before": before,
                **route_meta,
            }

        post_status: int | None = None
        post_error = ""
        post_preview = ""
        post_response = None
        phase = "cancel_request"
        try:
            post_response = env.session.post(
                f"https://chatgpt.com{CANCEL_SUBSCRIPTION_PATH}",
                headers=_common_headers(env, token, CANCEL_SUBSCRIPTION_PATH),
                json={"account_id": remote_id},
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            current_response = post_response
            post_status = int(post_response.status_code)
            post_preview = _safe_response_preview(post_response, secrets=secrets)
        except Exception as exc:
            # A transport error can happen after the server accepted the POST.
            # Never retry this mutation; verify current state instead.
            post_error = _safe_exception_detail(exc, secrets=secrets)

        posted_base = {
            **base,
            "posted": True,
            "http_status": before_status,
            "before": before,
            **({"cancel_http_status": post_status} if post_status is not None else {}),
            **route_meta,
        }
        after: dict | None = None
        last_verify_status: int | None = None
        last_verify_error = ""
        last_verify_preview = ""
        delays = verification_delays or (0.0,)
        for delay in delays:
            if delay > 0:
                time.sleep(float(delay))
            phase = "verify"
            after_response = None
            try:
                after_response = env.session.get(
                    f"https://chatgpt.com{query_path}",
                    headers=_common_headers(env, token, ACCOUNTS_CHECK_PATH),
                    allow_redirects=False,
                    timeout=timeout_seconds,
                )
                current_response = after_response
                last_verify_status = int(after_response.status_code)
                if not 200 <= last_verify_status < 300:
                    verify_fields = _http_failure_fields(
                        after_response,
                        phase="verify",
                        label="post-cancel verification",
                        secrets=secrets,
                    )
                    last_verify_error = str(verify_fields.get("error") or "")
                    last_verify_preview = str(verify_fields.get("response_preview") or "")
                    continue
                candidate = _parse_remote_plan(
                    _response_json_object(after_response),
                    token,
                    remote_id,
                )
                after = candidate
                last_verify_error = ""
                last_verify_preview = ""
                if (
                    _optional_bool(candidate.get("has_active_subscription")) is False
                    or candidate.get("cancels_at")
                    or _optional_bool(candidate.get("last_will_renew")) is False
                ):
                    break
            except Exception as exc:
                last_verify_error = (
                    "post-cancel verification failed: "
                    f"{_safe_exception_detail(exc, secrets=secrets)}"
                )
                last_verify_preview = _safe_response_preview(
                    after_response,
                    secrets=secrets,
                ) if after_response is not None else ""
                continue

        confirmed = bool(
            isinstance(after, dict)
            and (
                _optional_bool(after.get("has_active_subscription")) is False
                or after.get("cancels_at")
                or _optional_bool(after.get("last_will_renew")) is False
            )
        )
        post_accepted = post_status is not None and 200 <= post_status < 300
        if confirmed:
            failure_reason = "cancel_confirmed"
            failure_error = None
            failure_phase = "verify"
            failure_preview = ""
        elif post_error:
            failure_reason = "protocol_cancel_failed"
            failure_error = f"cancel request transport error: {post_error}"
            failure_phase = "cancel_request"
            failure_preview = last_verify_preview
        elif not post_accepted:
            failure_reason = "protocol_cancel_failed"
            failure_error = f"cancel subscription HTTP {post_status}"
            if post_preview:
                failure_error = f"{failure_error}: {post_preview}"
            failure_phase = "cancel_request"
            failure_preview = post_preview
        else:
            failure_reason = "protocol_cancel_unconfirmed"
            failure_error = last_verify_error or "cancel accepted but renewal state was not confirmed"
            failure_phase = "verify"
            failure_preview = last_verify_preview
        return {
            **posted_base,
            "ok": confirmed,
            "confirmed": confirmed,
            "phase": failure_phase,
            "reason": failure_reason,
            **({"after": after} if isinstance(after, dict) else {}),
            **({"verify_http_status": last_verify_status} if last_verify_status is not None else {}),
            **({"response_preview": failure_preview} if failure_preview else {}),
            **({"error": failure_error} if failure_error else {}),
        }
    except Exception as exc:
        result = {
            **base,
            "reason": "protocol_exception",
            "phase": phase,
            "error": f"protocol request failed: {_safe_exception_detail(exc, secrets=secrets)}",
            **route_meta,
        }
        preview = _safe_response_preview(current_response, secrets=secrets) if current_response is not None else ""
        if preview:
            result["response_preview"] = preview
        if current_response is not None and getattr(current_response, "status_code", None) is not None:
            result["http_status"] = int(current_response.status_code)
        return result
    finally:
        if env is not None:
            try:
                env.session.close()
            except Exception:
                pass


def parse_accounts_check(
    data: dict,
    *,
    token: str = "",
    include_subscription: bool = True,
) -> dict:
    """从 accounts/check 响应提取套餐和 Plus 试用资格。"""
    claims = token_claims(token) if token else {}
    claim_account_id = claims.get("account_id")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, dict):
        raise ValueError("响应缺少 accounts 对象")

    item = None
    account_key = None
    if claim_account_id and isinstance(accounts.get(claim_account_id), dict):
        item = accounts.get(claim_account_id)
        account_key = claim_account_id
    elif isinstance(accounts.get("default"), dict):
        item = accounts.get("default")
        account = item.get("account") or {}
        account_key = account.get("account_id") or "default"
    else:
        for k, v in accounts.items():
            if k != "default" and isinstance(v, dict):
                item = v
                account_key = k
                break
    if not isinstance(item, dict):
        raise ValueError("未找到可解析的账号条目")

    account = item.get("account") or {}
    entitlement = item.get("entitlement") or {}
    last_sub = item.get("last_active_subscription") or {}
    eligible_promo_campaigns = item.get("eligible_promo_campaigns") or {}
    plus_campaign = eligible_promo_campaigns.get("plus") if isinstance(eligible_promo_campaigns, dict) else None
    plus_meta = (plus_campaign or {}).get("metadata") or {}
    discount = plus_meta.get("discount") or {}
    duration = plus_meta.get("duration") or {}

    plan_type = account.get("plan_type") or claims.get("claim_plan_type") or ""
    subscription_plan = entitlement.get("subscription_plan") or ""
    has_active_subscription = _optional_bool(entitlement.get("has_active_subscription"))
    is_active_subscription_gratis = _optional_bool(entitlement.get("is_active_subscription_gratis"))
    last_will_renew = _optional_bool(last_sub.get("will_renew"))
    subscription_id = entitlement.get("subscription_id") or last_sub.get("subscription_id")
    is_free = str(plan_type).lower() == "free" or str(subscription_plan).lower() == "chatgptfreeplan"
    plus_trial_eligible = bool(is_free and plus_campaign)

    offers = ((item.get("eligible_offers") or {}).get("offers") or [])
    eligible_offer_ids = [o.get("id") for o in offers if isinstance(o, dict) and o.get("id")]

    result = {
        "ok": True,
        "checked_at": now_iso(),
        "account_id": account.get("account_id") or account_key or claim_account_id,
        "account_user_role": account.get("account_user_role"),
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "expires_at": entitlement.get("expires_at"),
        "renews_at": entitlement.get("renews_at"),
        "billing_period": entitlement.get("billing_period"),
        "billing_currency": entitlement.get("billing_currency"),
        "discount_type": (entitlement.get("discount") or {}).get("discount_type"),
        "discount_amount": (entitlement.get("discount") or {}).get("amount"),
        "discount_duration_num_periods": (entitlement.get("discount") or {}).get("duration_num_periods"),
        "discount_expires_at": (entitlement.get("discount") or {}).get("discount_expires_at"),
        "discount_cancellation_policy": (entitlement.get("discount") or {}).get("cancellation_policy"),
        "discount_promo_campaign_id": (entitlement.get("discount") or {}).get("promo_campaign_id"),
        "plus_trial_eligible": plus_trial_eligible,
        "plus_trial_campaign_id": (plus_campaign or {}).get("id"),
        "plus_trial_title": plus_meta.get("title"),
        "plus_trial_summary": plus_meta.get("summary"),
        "plus_trial_discount_percentage": discount.get("percentage"),
        "plus_trial_duration_num_periods": duration.get("num_periods"),
        "plus_trial_duration_period": duration.get("period"),
        "plus_trial_promotion_type_label": plus_meta.get("promotion_type_label"),
        "eligible_offer_ids": eligible_offer_ids,
        "features_count": len(item.get("features") or []),
        "can_access_with_session": bool(item.get("can_access_with_session")),
        "raw_account_plan_type": account.get("plan_type"),
    }
    if include_subscription:
        result.update({
            "subscription_id": subscription_id,
            "has_active_subscription": has_active_subscription,
            "is_active_subscription_gratis": is_active_subscription_gratis,
            "cancels_at": entitlement.get("cancels_at"),
            "is_delinquent": bool(entitlement.get("is_delinquent")),
            "last_purchase_origin_platform": last_sub.get("purchase_origin_platform"),
            "last_will_renew": last_will_renew,
        })
        result["subscription_status"] = classify_subscription_status(result)
    result.update({k: v for k, v in claims.items() if k != "payload" and v is not None})
    return result


def _plan_check_settings(
    timeout: float | None,
    max_attempts: int | None,
    retry_delay: float | None,
) -> tuple[float, int, float]:
    from config import proxy as proxy_cfg

    timeout_value = timeout if timeout is not None else getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 8.0)
    # 套餐查询使用自己的短重试预算，避免继承注册等长任务的全局代理重试次数。
    attempts_value = max_attempts if max_attempts is not None else getattr(
        proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 2
    )
    delay_value = retry_delay if retry_delay is not None else getattr(proxy_cfg, "PLAN_CHECK_RETRY_DELAY", 0.5)
    return (
        max(1.0, min(60.0, float(timeout_value or 8.0))),
        max(1, min(4, int(attempts_value or 1))),
        max(0.0, min(30.0, float(delay_value or 0.0))),
    )


def _retryable_plan_error(http_status: int | None) -> bool:
    if http_status is None:
        return True
    return http_status in {408, 409, 425, 429} or http_status >= 500


def _terminal_invalid_at_result(
    response: Any,
    *,
    claims: dict,
    attempt: int,
    max_attempts: int,
    request_timeout: float,
    route_meta: dict,
    secrets: tuple[str, ...],
) -> dict:
    """Build the terminal result for an account endpoint HTTP 401."""
    failure_fields = _http_failure_fields(
        response,
        phase="query",
        label="套餐查询",
        secrets=secrets,
    )
    detail = str(failure_fields.get("response_preview") or "")
    error = "AT失效（HTTP 401），请手动查活刷新"
    if detail:
        error = f"{error}: {detail}"
    return {
        "ok": False,
        "checked_at": now_iso(),
        "protocol": "protocol",
        **{k: v for k, v in claims.items() if k != "payload"},
        **failure_fields,
        "reason": "token_expired",
        "error": error,
        "retryable": False,
        "token_expired": True,
        "needs_live_check": True,
        "attempt_count": attempt,
        "max_attempts": max_attempts,
        "request_timeout": request_timeout,
        **route_meta,
    }


def _is_plan_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    name = type(exc).__name__.lower()
    detail = str(exc).lower()
    return "timeout" in name or "timed out" in detail or "operation timeout" in detail


def _retry_wait_seconds(resp: Any, base_delay: float, attempt: int) -> float:
    try:
        retry_after = (getattr(resp, "headers", {}) or {}).get("retry-after")
        if retry_after is not None:
            return max(0.0, min(30.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(30.0, base_delay * attempt))


def check_account_plan(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
    include_subscription: bool = False,
) -> dict:
    token = normalize_token(token)
    if not token:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "protocol": "protocol",
            "phase": "validate",
            "reason": "missing_token",
            "error": "token 为空",
        }
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "protocol": "protocol",
            "phase": "validate",
            "reason": "token_expired",
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    url = f"https://chatgpt.com{ACCOUNTS_CHECK_PATH}?timezone_offset_min={quote(str(timezone_offset_min))}"
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "protocol": "protocol",
            "phase": "configure",
            "reason": "protocol_config_error",
            "http_status": None,
            "error": f"套餐查询重试配置错误: {_safe_exception_detail(exc, secrets=(token, str(proxy or '')))}",
            "retryable": False,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        routes = _plan_check_routes(proxy, attempts)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "protocol": "protocol",
            "phase": "route",
            "reason": "protocol_route_error",
            "http_status": None,
            "error": f"套餐查询网络配置错误: {_safe_exception_detail(exc, secrets=(token, str(proxy or '')))}",
            "retryable": False,
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    effective_attempts = min(attempts, len(routes))

    last_result: dict | None = None
    for attempt, route in enumerate(routes[:effective_attempts], start=1):
        env = None
        resp = None
        secrets = (token, str(route.get("proxy") or ""))
        route_meta = _public_route_meta(route)
        phase = "query"
        try:
            # 套餐查询只需要稳定的请求头，不需要额外访问 IP 地理信息接口。
            env = _protocol_session_for_route(route)
            resp = env.session.get(
                url,
                headers=_common_headers(env, token),
                allow_redirects=False,
                timeout=timeout_seconds,
            )
            http_status = int(resp.status_code)
            if not (200 <= http_status < 300):
                is_auth_expired = http_status == 401
                if is_auth_expired:
                    # 401 belongs to the account AT, not to the selected route.
                    # Return immediately so this task never rotates to another proxy.
                    return _terminal_invalid_at_result(
                        resp,
                        claims=claims,
                        attempt=attempt,
                        max_attempts=effective_attempts,
                        request_timeout=timeout_seconds,
                        route_meta=route_meta,
                        secrets=secrets,
                    )
                is_timeout = http_status in {408, 504, 524}
                failure_fields = _http_failure_fields(
                    resp,
                    phase="query",
                    label="套餐查询",
                    secrets=secrets,
                )
                last_result = {
                    "ok": False,
                    "checked_at": now_iso(),
                    "protocol": "protocol",
                    "reason": (
                        "protocol_timeout"
                        if is_timeout
                        else "protocol_check_failed"
                    ),
                    **failure_fields,
                    "retryable": _retryable_plan_error(http_status),
                    "token_expired": claims.get("token_expired"),
                    "needs_live_check": False,
                }
            else:
                phase = "query_parse"
                try:
                    data = _response_json_object(resp)
                except Exception as exc:
                    preview = _safe_response_preview(resp, secrets=secrets)
                    last_result = {
                        "ok": False,
                        "checked_at": now_iso(),
                        "protocol": "protocol",
                        "phase": "query_parse",
                        "reason": "protocol_invalid_response",
                        "http_status": http_status,
                        "error": f"套餐查询响应解析失败: {_safe_exception_detail(exc, secrets=secrets)}",
                        **({"response_preview": preview} if preview else {}),
                        "retryable": True,
                    }
                else:
                    parsed = parse_accounts_check(
                        data,
                        token=token,
                        include_subscription=include_subscription,
                    )
                    parsed["protocol"] = "protocol"
                    parsed["phase"] = "query"
                    parsed["http_status"] = http_status
                    parsed["attempt_count"] = attempt
                    parsed["max_attempts"] = effective_attempts
                    parsed["request_timeout"] = timeout_seconds
                    parsed["retryable"] = False
                    parsed.update(route_meta)
                    return parsed
        except Exception as exc:
            safe_error = _safe_exception_detail(exc, secrets=secrets)
            logger.debug("套餐查询失败: %s", safe_error)
            failure_response = resp if resp is not None else getattr(exc, "response", None)
            failure_status = None
            if failure_response is not None:
                try:
                    failure_status = int(getattr(failure_response, "status_code", 0) or 0) or None
                except (TypeError, ValueError):
                    failure_status = None
            if failure_status == 401:
                # Some HTTP clients raise status errors instead of returning the
                # response. Treat the attached 401 exactly like a normal 401.
                return _terminal_invalid_at_result(
                    failure_response,
                    claims=claims,
                    attempt=attempt,
                    max_attempts=effective_attempts,
                    request_timeout=timeout_seconds,
                    route_meta=route_meta,
                    secrets=secrets,
                )
            preview = _safe_response_preview(failure_response, secrets=secrets) if failure_response is not None else ""
            is_timeout = _is_plan_timeout_exception(exc)
            last_result = {
                "ok": False,
                "checked_at": now_iso(),
                "protocol": "protocol",
                "phase": phase,
                "reason": "protocol_timeout" if is_timeout else "protocol_exception",
                "http_status": failure_status,
                "error": safe_error,
                **({"response_preview": preview} if preview else {}),
                "retryable": True,
            }
        finally:
            if env is not None:
                try:
                    env.session.close()
                except Exception:
                    pass

        last_result = last_result or {
            "ok": False,
            "checked_at": now_iso(),
            "protocol": "protocol",
            "phase": phase,
            "reason": "protocol_unknown_error",
            "error": "未知错误",
            "retryable": True,
        }
        last_result.update({
            "attempt_count": attempt,
            "max_attempts": effective_attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        })
        if not last_result.get("retryable") or attempt >= effective_attempts:
            return last_result

        wait_seconds = _retry_wait_seconds(resp, base_delay, attempt)
        next_route = routes[attempt]
        current_route_label = route_meta.get("proxy_used") or route_meta.get("network_route") or "direct"
        next_route_label = (
            next_route.get("proxy_used")
            or next_route.get("network_route")
            or "direct"
        )
        logger.warning(
            "套餐查询临时失败，第 %s/%s 次，%.1fs 后切换代理 %s -> %s: %s",
            attempt,
            effective_attempts,
            wait_seconds,
            current_route_label,
            next_route_label,
            last_result.get("error"),
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "protocol": "protocol",
        "phase": "query",
        "reason": "protocol_not_executed",
        "http_status": None,
        "error": "套餐查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
