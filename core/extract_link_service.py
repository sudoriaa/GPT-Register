# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _bool_setting(name: str, default: bool = False) -> bool:
    value = _runtime_setting(name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def backend_name() -> str:
    value = str(_runtime_setting("EXTRACT_LINK_BACKEND", "local") or "local").strip().lower()
    if value in {"cdk", "1k50", "web", "cdk-web"}:
        value = "cdk_web"
    if value not in {"local", "remote", "cdk_web"}:
        raise ValueError("EXTRACT_LINK_BACKEND 仅支持 local / remote / cdk_web")
    return value


def auto_extract_enabled() -> bool:
    return _bool_setting("EXTRACT_LINK_AUTO", False)


SUPPORTED_LINK_TYPES = {"paypal", "pix", "upi", "kakao_pay", "ideal"}


def _link_type(value: str | None = None) -> str:
    default = "paypal" if backend_name() in {"local", "cdk_web"} else "pix"
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", default) or default).strip().lower()
    if t not in SUPPORTED_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 paypal / pix / upi / kakao_pay / ideal")
    if backend_name() in {"local", "cdk_web"}:
        return "paypal"
    if t == "paypal":
        # The legacy CDK service has no PayPal payment-method type; retain
        # its historical PIX default when that backend is selected.
        return "pix"
    return t


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    cdk = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


def public_settings() -> dict:
    """返回前端可展示的提链设置，绝不返回代理认证或 CDK。"""
    backend = backend_name()
    proxy_setting_name = "CDK_WEB_PROXY" if backend == "cdk_web" else "EXTRACT_LINK_PROXY"
    result = {
        "backend": backend,
        "auto_extract": auto_extract_enabled(),
        "custom_proxy_configured": bool(str(_runtime_setting(proxy_setting_name, "") or "").strip()),
        "country": str(_runtime_setting("EXTRACT_LINK_COUNTRY", "GB") or "GB").strip().upper(),
        "payment_method": str(_runtime_setting("EXTRACT_LINK_PAYMENT_METHOD", "paypal") or "paypal").strip().lower(),
        "expiry_minutes": _int_setting("EXTRACT_LINK_EXPIRY_MINUTES", 60, 1, 24 * 60),
    }
    if backend == "cdk_web":
        try:
            from core import cdk_web_service
            result.update(cdk_web_service.public_settings())
            result["backend"] = "cdk_web"
        except Exception:
            result.update({"cdk_web_enabled": False, "cdk_pool_total": 0, "cdk_pool_available": 0})
    return result


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def _normalize_proxy(value: str | None) -> str:
    """Convert registration copy format to a proxy URL accepted by extractor."""
    text = str(value or "").strip().splitlines()[0].strip() if str(value or "").strip() else ""
    if not text:
        return ""
    if "://" in text:
        try:
            parsed = urlsplit(text)
            scheme = parsed.scheme.lower()
            if scheme == "socks":
                scheme = "socks5"
            if scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                return ""
            host = parsed.hostname
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            auth = ""
            if parsed.username is not None:
                auth = quote(unquote(parsed.username), safe="")
                if parsed.password is not None:
                    auth += ":" + quote(unquote(parsed.password), safe="")
                auth += "@"
            port = f":{parsed.port}" if parsed.port else ""
            return urlunsplit((scheme, auth + host + port, parsed.path, parsed.query, ""))
        except (TypeError, ValueError):
            return ""

    # Account list copy format is host:port:username:password. Password may
    # contain colons, so split only three times.
    parts = text.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit() and parts[0]:
        host, port, username, password = parts
        scheme = "socks5h" if any(x in host.lower() for x in ("kookeey", "iproyal", "iprocket")) else "http"
        return (
            f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
            f"@{host}:{port}"
        )
    # Also accept host:port with no authentication for custom gateways.
    if len(parts) == 2 and parts[1].isdigit() and parts[0]:
        return f"http://{parts[0]}:{parts[1]}"
    return ""


def _account_proxy(account_id: int) -> str:
    try:
        row = db.get_account(int(account_id)) or {}
    except Exception:
        row = {}
    return _normalize_proxy(row.get("registration_proxy") or row.get("proxy_used"))


def resolve_extract_proxy(account_id: int, override: str | None = None) -> tuple[str, str]:
    """Resolve proxy precedence: request override > global > registration."""
    raw_override = str(override or "").strip()
    override_value = _normalize_proxy(raw_override)
    if raw_override and not override_value:
        raise ValueError("本次提链代理格式无效")
    global_name = "CDK_WEB_PROXY" if backend_name() == "cdk_web" else "EXTRACT_LINK_PROXY"
    raw_global = str(_runtime_setting(global_name, "") or "").strip()
    global_value = _normalize_proxy(raw_global)
    if raw_global and not global_value:
        raise ValueError("全局提链代理格式无效")
    candidates = (
        ("custom", override_value),
        ("global", global_value),
        ("registration", _account_proxy(account_id)),
    )
    for source, value in candidates:
        if value:
            return value, source
    return "", "none"


def _expiry_iso() -> str:
    minutes = _int_setting("EXTRACT_LINK_EXPIRY_MINUTES", 60, 1, 24 * 60)
    return (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _redact_text(value: object, *, access_token: str = "", proxy: str = "") -> str:
    text = str(value or "")
    for secret in (str(access_token or ""), str(proxy or "")):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@", r"\1://***:***@", text)
    # Worker errors can contain a full token in a response preview; keep only
    # a bounded diagnostic and never persist a raw credential.
    return text[:500]


def _is_paypal_approval_url(value: object) -> bool:
    """Accept only the final PayPal billing-agreement approval URL."""
    try:
        parsed = urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        if not (host == "paypal.com" or host.endswith(".paypal.com")):
            return False
        if parsed.path.rstrip("/").lower() != "/agreements/approve":
            return False
        ba_token = (parse_qs(parsed.query).get("ba_token") or [""])[0]
        return str(ba_token).startswith("BA-")
    except Exception:
        return False


_LOCAL_WORKER = r'''
import json, sys

def main():
    payload = json.load(sys.stdin)
    try:
        from payment_link_extractor.application import extract_payment_link
        from payment_link_extractor.models import ExtractionConfig
        cfg = ExtractionConfig(
            access_token=str(payload.get("access_token") or ""),
            checkout_proxy=str(payload.get("checkout_proxy") or ""),
            update_proxy=str(payload.get("update_proxy") or payload.get("checkout_proxy") or ""),
            stripe_hcaptcha_token=str(payload.get("stripe_hcaptcha_token") or ""),
            country=str(payload.get("country") or "GB"),
            payment_method=str(payload.get("payment_method") or "paypal"),
            apply_checkout_update=bool(payload.get("apply_checkout_update", True)),
            verbose=False,
            oaics_only=False,
        )
        result = extract_payment_link(cfg)
        data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        print(json.dumps({"ok": True, "result": data}, ensure_ascii=False))
    except BaseException as exc:
        print(json.dumps({
            "ok": False,
            "status_code": getattr(exc, "status_code", None),
            "error_type": type(exc).__name__,
            "error": str(exc)[:1200],
        }, ensure_ascii=False))

if __name__ == "__main__":
    main()
'''


def _local_python(project_path: Path) -> str:
    configured = str(_runtime_setting("EXTRACT_LINK_PYTHON", "") or "").strip()
    candidates = [
        configured,
        str(project_path / ".venv" / "Scripts" / "python.exe"),
        str(project_path / ".venv" / "bin" / "python"),
        sys.executable,
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return sys.executable


def _normalize_local_result(raw: dict, *, link_type: str) -> dict:
    """Map the standalone extractor result to the account-safe result shape."""
    source = raw if isinstance(raw, dict) else {}
    paypal_field = str(source.get("paypal_url") or "").strip()
    link = paypal_field or source.get("provider_url") or ""
    # New extractor builds expose paypal_url and are strict-checked here as a
    # final guard. Older/fixture adapters may expose only provider_url; the
    # standalone project has already resolved and validated that field.
    if backend_name() == "local" and str(link_type or "").lower() == "paypal" and paypal_field and not _is_paypal_approval_url(paypal_field):
        raise RuntimeError("PayPal 审批链接未返回或格式无效")
    # Legacy remote providers may return non-PayPal links; preserve their
    # historical fallback behavior when the local PayPal backend is not used.
    if backend_name() == "local" and not str(link or "").strip():
        raise RuntimeError("PayPal 审批链接未返回")
    if not link:
        link = source.get("stripe_redirect_url") or source.get("long_url") or source.get("copy_paste") or ""
    normalized_link = str(link or "")
    payload = {
        # The local backend is accepted only after the PayPal approval URL
        # check above; keep both copy fields pointed at that validated URL.
        "long_url": normalized_link if backend_name() == "local" else str(source.get("long_url") or normalized_link or ""),
        "copy_paste": normalized_link if backend_name() == "local" else str(source.get("copy_paste") or normalized_link or ""),
        "image_url_png": str(source.get("image_url_png") or ""),
        "image_url_svg": str(source.get("image_url_svg") or ""),
        "payment_method": str(source.get("payment_method") or "paypal"),
        "payment_link_type": str(source.get("payment_link_type") or link_type or "paypal"),
        "provider_url": normalized_link if backend_name() == "local" else str(source.get("provider_url") or source.get("paypal_url") or normalized_link or ""),
        "stripe_redirect_url": str(source.get("stripe_redirect_url") or ""),
        "checkout_session_id": str(source.get("checkout_session_id") or ""),
        "session_kind": str(source.get("session_kind") or ""),
        "billing_country": str(source.get("billing_country") or ""),
        "currency": str(source.get("currency") or ""),
        "amount_due": source.get("amount_due"),
        "amount_due_minor": source.get("amount_due_minor"),
        "cdk_remaining": source.get("cdk_remaining"),
        "expires_at": _expiry_iso(),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _run_local_extract(*, access_token: str, link_type: str, proxy: str) -> dict:
    project_path = Path(str(_runtime_setting("EXTRACT_LINK_PROJECT_PATH", "") or "").strip()).expanduser()
    if not project_path.exists():
        raise ValueError("EXTRACT_LINK_PROJECT_PATH 不存在")
    python_exe = _local_python(project_path)
    payload = {
        "access_token": access_token,
        "checkout_proxy": proxy,
        "update_proxy": proxy,
        "country": str(_runtime_setting("EXTRACT_LINK_COUNTRY", "GB") or "GB").strip().upper(),
        "payment_method": str(_runtime_setting("EXTRACT_LINK_PAYMENT_METHOD", "paypal") or "paypal").strip().lower(),
        "apply_checkout_update": _bool_setting("EXTRACT_LINK_APPLY_CHECKOUT_UPDATE", True),
    }
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    env = os.environ.copy()
    # The standalone project defaults to routing public proxies through its
    # optional bridge. Direct in-process integration should use the account's
    # selected proxy as-is unless the user explicitly configures a bridge.
    env.setdefault("OPLL_CHAIN_ALL_PROXIES", "false")
    env["OPLL_CHAIN_ALL_PROXIES"] = str(env.get("EXTRACT_LINK_CHAIN_ALL_PROXIES", "false"))
    try:
        completed = subprocess.run(
            [python_exe, "-c", _LOCAL_WORKER],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            cwd=str(project_path),
            env=env,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"本地 PayPal 提链超时（>{timeout}s）") from exc
    stdout = (completed.stdout or "").splitlines()
    response = None
    for line in reversed(stdout):
        try:
            candidate = json.loads(line)
        except Exception:
            continue
        if isinstance(candidate, dict) and ("ok" in candidate or "error" in candidate):
            response = candidate
            break
    if not isinstance(response, dict):
        detail = (completed.stderr or completed.stdout or "本地提链进程无 JSON 输出").strip()
        raise RuntimeError(_redact_text(detail, access_token=access_token, proxy=proxy))
    if not response.get("ok"):
        status_code = response.get("status_code")
        error = str(response.get("error") or "本地 PayPal 提链失败")
        if str(status_code) == "401" or re.search(r"(?i)(?:http|status|code)[^\d]{0,8}401|unauthori[sz]ed|token.{0,20}(?:expired|invalid)", error):
            raise RuntimeError("AT失效")
        raise RuntimeError(_redact_text(error, access_token=access_token, proxy=proxy))
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("本地 PayPal 提链未返回结果")
    return _normalize_local_result(result, link_type=link_type)


def query_cdk(*, cdk: str | None = None) -> dict:
    if backend_name() == "cdk_web":
        from core import cdk_pool
        items = cdk_pool.get_pool().list_public()
        if cdk:
            # Never echo the supplied code.  Return the matching masked record
            # when it is already in the local pool.
            fp = cdk_pool.fingerprint(cdk)
            match = next((item for item in items if item.get("fingerprint") == fp), None)
            return {"backend": "cdk_web", "configured": bool(match), "cdk_required": True, "item": match}
        return {
            "backend": "cdk_web", "configured": bool(items), "cdk_required": True,
            "remaining": None, "pool_total": len(items),
            "pool_available": sum(1 for item in items if item.get("status") == "available"),
            "items": items,
        }
    if backend_name() == "local":
        return {"backend": "local", "configured": True, "cdk_required": False, "remaining": None}
    base = _api_base()
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_extract_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extract",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"提链服务未返回 job_id: {data}")
            return data
        resp = s.post(f"{base}/api/extract", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(url, timeout=timeout, stream=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _maybe_enqueue_paypal_payment(account_id: int, *, trigger: str) -> None:
    """提链成功后按独立开关自动进入协议支付队列。"""
    try:
        from core import paypal_payment_service
        if not paypal_payment_service.auto_payment_enabled():
            return
        queued = paypal_payment_service.enqueue_account_payment(
            account_id=int(account_id),
            trigger=f"extract_{trigger}"[:80],
        )
        if queued.get("accepted"):
            logger.info("[提链] 已自动入协议支付队列: account_id=%s", account_id)
        else:
            logger.warning(
                "[提链] 自动协议支付入队失败: account_id=%s reason=%s",
                account_id,
                _redact_text(queued.get("error") or "unknown"),
            )
    except Exception as exc:
        # 提链本身已经成功，支付入队异常只记录给人工处理，不回滚提链结果。
        logger.exception("[提链] 自动协议支付触发异常: account_id=%s: %s", account_id, type(exc).__name__)


def _run_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    link_type: str,
    cdk: str | None,
    trigger: str,
    proxy: str | None = None,
    proxy_source: str = "none",
) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        if backend_name() == "cdk_web":
            # The 1K50 backend owns its own CDK rotation, stable visitor and
            # optional protocol-payment continuation.  Keep this dispatch in
            # the existing queue so plan-check and WebUI callers retain one
            # API surface.
            from core import cdk_web_backend
            return cdk_web_backend.run_extract(
                account_id=account_id,
                email=email,
                access_token=access_token,
                trigger=trigger,
                proxy=str(proxy or ""),
                proxy_source=proxy_source,
            )
        if backend_name() == "local":
            selected_proxy = str(proxy or "").strip()
            if not selected_proxy:
                raise ValueError("没有可用提链代理：请配置自定义代理或先保存注册代理")
            job_id = f"local-{uuid.uuid4().hex}"
            db.update_account_extract(account_id, {
                "ok": False,
                "status": "running",
                "job_id": job_id,
                "link_type": link_type,
                "message": "本地 PayPal 提链执行中",
                "proxy_source": proxy_source,
            })
            payload = _run_local_extract(access_token=access_token, link_type=link_type, proxy=selected_proxy)
            final = {
                "ok": True,
                "status": "success",
                "job_id": job_id,
                "link_type": link_type,
                "result": payload,
                "message": "提链成功",
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "proxy_source": proxy_source,
            }
            db.update_account_extract(account_id, final)
            _maybe_enqueue_paypal_payment(account_id, trigger=trigger)
            logger.info("[提链] 本地成功: %s type=%s", email, link_type)
            return final

        # Legacy remote CDK/SSE backend.
        code = _cdk(cdk)
        job = _create_extract_job(token=access_token, link_type=link_type, cdk=code)
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
            "proxy_source": proxy_source,
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=code):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                result = _normalize_local_result(result, link_type=link_type)
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs, "proxy_source": proxy_source}
                db.update_account_extract(account_id, final)
                _maybe_enqueue_paypal_payment(account_id, trigger=trigger)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        reason = _redact_text(reason, access_token=access_token, proxy=proxy or "")
        if getattr(exc, "status_code", None) == 401 or re.search(r"(?i)(?:http|status|code)[^\d]{0,8}401|unauthori[sz]ed|token.{0,20}(?:expired|invalid)", reason):
            reason = "AT失效"
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str = "manual",
    link_type: str | None = None,
    cdk: str | None = None,
    proxy: str | None = None,
) -> dict:
    if backend_name() == "cdk_web":
        from core import cdk_web_backend
        return cdk_web_backend.enqueue_extract(
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=trigger,
            proxy=proxy,
        )
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        lt = _link_type(link_type)
        code = None if backend_name() == "local" else _cdk(cdk)
        selected_proxy, proxy_source = resolve_extract_proxy(account_id, proxy)
        if backend_name() == "local" and not selected_proxy:
            _QUEUE_SLOTS.release()
            return {
                "accepted": False,
                "busy": False,
                "error": "没有可用提链代理：请填写本次/全局代理，或先保存注册代理",
            }
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id,
            email=email,
            access_token=access_token,
            link_type=lt,
            cdk=code,
            trigger=trigger,
            proxy=selected_proxy,
            proxy_source=proxy_source,
        )
        return {
            "accepted": True,
            "busy": False,
            "future": fut,
            "link_type": lt,
            "backend": backend_name(),
            "proxy_source": proxy_source,
        }
    except Exception:
        _QUEUE_SLOTS.release()
        raise
