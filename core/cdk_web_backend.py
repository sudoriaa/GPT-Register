# -*- coding: utf-8 -*-
"""把 1K50 CDK 网页客户端接入现有账号提链/协议支付队列。"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from config import cdk_web as cfg
from core import cdk_pool, db
from core.cdk_web_service import (
    CdkAuthError,
    CdkInvalidError,
    CdkProtocolError,
    CdkTaskError,
    CdkWebClient,
    CdkWebError,
    DEFAULT_BASE_URL,
)

logger = logging.getLogger(__name__)


def _setting(name: str, default=None):
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    value = os.getenv(name)
    return str(value).strip() if value is not None and str(value).strip() else getattr(cfg, name, default)


def _bool(name: str, default: bool = False) -> bool:
    value = _setting(name, default)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    return _bool("CDK_WEB_ENABLED", False)


def public_settings() -> dict:
    rows = cdk_pool.get_pool().list_public()
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "available")
        counts[status] = counts.get(status, 0) + 1
    return {
        "cdk_web_enabled": enabled(),
        "cdk_web_base_url": str(_setting("CDK_WEB_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        "cdk_web_password_configured": bool(str(_setting("CDK_WEB_WORKBENCH_PASSWORD", "") or "").strip()),
        "cdk_web_country": str(_setting("CDK_WEB_COUNTRY", "GB") or "GB").upper(),
        "cdk_web_protocol_country": str(_setting("CDK_WEB_PROTOCOL_COUNTRY", "GB") or "GB").upper(),
        "cdk_web_auto_payment": _bool("CDK_WEB_AUTO_PAYMENT", True),
        "cdk_web_proxy_configured": bool(str(_setting("CDK_WEB_PROXY", "") or "").strip()),
        "cdk_web_sms_mode": str(_setting("CDK_WEB_SMS_MODE", "server-auto") or "server-auto"),
        "cdk_web_sms_provider": str(_setting("CDK_WEB_SMS_PROVIDER", "") or ""),
        "cdk_web_sms_api_key_configured": bool(str(_setting("CDK_WEB_SMS_API_KEY", "") or "").strip()),
        "cdk_web_sms_country": str(_setting("CDK_WEB_SMS_COUNTRY", "GB") or "GB"),
        "cdk_web_max_retries": _int("CDK_WEB_MAX_RETRIES", 2, 0, 20),
        "cdk_pool_total": len(rows),
        "cdk_pool_available": counts.get("available", 0),
        "cdk_pool_counts": counts,
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _new_client(visitor_id: str = "", cookies: dict | None = None) -> CdkWebClient:
    return CdkWebClient(
        str(_setting("CDK_WEB_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL),
        password=str(_setting("CDK_WEB_WORKBENCH_PASSWORD", "") or ""),
        visitor_id=visitor_id,
        cookies=cookies or {},
        timeout=float(_int("CDK_WEB_REQUEST_TIMEOUT", 30, 5, 300)),
    )


def _proxy(account_id: int, override: str | None = None) -> tuple[str, str]:
    # Keep format/precedence identical to the existing local extractor.
    from core.extract_link_service import _account_proxy, _normalize_proxy
    raw_custom = str(override or "").strip()
    custom = _normalize_proxy(raw_custom)
    if raw_custom and not custom:
        raise ValueError("本次 CDK 代理格式无效")
    raw_global = str(_setting("CDK_WEB_PROXY", "") or "").strip()
    global_proxy = _normalize_proxy(raw_global)
    if raw_global and not global_proxy:
        raise ValueError("CDK 默认代理格式无效")
    for source, value in (("custom", custom), ("global", global_proxy), ("registration", _account_proxy(account_id))):
        if value:
            return value, source
    return "", "none"


def _redact(value: object, *secrets: str) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r"(?i)(https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@", r"\1://***:***@", text)
    return text[:1000]


def _task_id(payload: dict) -> str:
    task = payload.get("task") if isinstance(payload.get("task"), dict) else payload
    return str(task.get("task_id") or task.get("payment_task_id") or task.get("protocol_payment_id") or task.get("job_id") or task.get("id") or "").strip()


def _result(payload: dict) -> dict:
    value = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        return dict(value)
    return dict(payload) if isinstance(payload, dict) else {}


def _remaining(session) -> int | None:
    try:
        value = int(getattr(session, "remaining_uses", 0))
        return max(0, value)
    except (TypeError, ValueError):
        return None


def _link_result(raw: dict, remaining: int | None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    link = str(source.get("paypal_url") or source.get("provider_url") or source.get("long_url") or source.get("copy_paste") or "").strip()
    if not link:
        raise CdkTaskError("CDK网页成功响应未包含 PayPal 链接", code="LINK_MISSING")
    result = {
        "long_url": link,
        "copy_paste": link,
        "provider_url": link,
        "payment_method": str(source.get("payment_method") or "paypal"),
        "payment_link_type": "paypal",
        "session_kind": source.get("session_kind"),
        "billing_country": source.get("billing_country"),
        "currency": source.get("currency"),
        "amount_due": source.get("amount_due"),
        "amount_due_minor": source.get("amount_due_minor"),
        "cdk_remaining": remaining,
        "expires_at": (datetime.now() + timedelta(minutes=60)).isoformat(timespec="seconds"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _payment_payload(proxy: str) -> dict:
    country = str(_setting("CDK_WEB_PROTOCOL_COUNTRY", "") or _setting("CDK_WEB_COUNTRY", "GB") or "GB").upper()
    sms_key = str(_setting("CDK_WEB_SMS_API_KEY", "") or "")
    provider = str(_setting("CDK_WEB_SMS_PROVIDER", "") or "")
    payload = {
        "checkout_proxy": proxy,
        "sms_mode": str(_setting("CDK_WEB_SMS_MODE", "server-auto") or "server-auto"),
        "sms_provider": provider,
        "sms_api_key": sms_key,
        "smsbower_api_key": sms_key if provider.strip().lower() == "smsbower" else "",
        "phone": "",
        "protocol_country": country,
        "country": country,
        "sms_country": str(_setting("CDK_WEB_SMS_COUNTRY", country) or country),
        "buyer_mode": str(_setting("CDK_WEB_BUYER_MODE", "identity_elevation") or "identity_elevation"),
        "agreement_only": _bool("CDK_WEB_AGREEMENT_ONLY", True),
        "preconfig_override": False,
    }
    return payload


def _safe_payment_result(payload: dict) -> dict:
    source = _result(payload)
    allowed = ("status", "settlement_status", "redirect_status", "payment_action", "billing_country", "stage", "retryable", "paypal_authorized")
    return {key: source.get(key) for key in allowed if source.get(key) not in (None, "")}


def _payment_success(account_id: int, task_id: str, snapshot: dict, *, attempt: int, max_attempts: int, country: str, proxy_source: str, message: str) -> dict:
    safe = _safe_payment_result(snapshot)
    final = {
        "ok": True, "status": "success", "attempt": attempt,
        "max_attempts": max_attempts, "protocol_job_id": task_id,
        "country": country, "proxy_source": proxy_source,
        "backend": "cdk_web", "result": safe,
        "settlement_status": safe.get("settlement_status"),
        "redirect_status": safe.get("redirect_status"),
        "payment_action": safe.get("payment_action"),
        "message": message,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.update_account_paypal_payment(account_id, final)
    return final


def _wait_payment(client: CdkWebClient, task_id: str, account_id: int, attempt: int, max_attempts: int) -> dict:
    deadline = time.monotonic() + _int("CDK_WEB_PAYMENT_TIMEOUT", 900, 30, 3600)
    interval = _int("CDK_WEB_PAYMENT_POLL_INTERVAL", 2, 1, 30)
    last = ""
    while time.monotonic() < deadline:
        snapshot = client.get_protocol_payment(task_id)
        status = str(snapshot.get("status") or snapshot.get("stage") or "").lower()
        stage = str(snapshot.get("stage") or snapshot.get("message") or status)
        if stage and stage != last:
            db.update_account_paypal_payment(account_id, {
                "ok": False, "status": "running", "attempt": attempt,
                "max_attempts": max_attempts, "protocol_job_id": task_id,
                "backend": "cdk_web", "message": stage,
            })
            last = stage
        if status in {"awaiting_otp", "awaiting_captcha", "manual", "needs_intervention"}:
            raise CdkProtocolError(f"等待人工处理：{stage or status}", code=status.upper())
        if status in {"completed", "succeeded", "success", "paid"}:
            return snapshot
        if status in {"failed", "cancelled", "canceled", "stopped", "error"}:
            raise CdkProtocolError(str(snapshot.get("error") or snapshot.get("message") or status), code="PAYMENT_FAILED")
        time.sleep(interval)
    try:
        client.cancel_protocol_payment(task_id)
    except Exception:
        pass
    raise CdkProtocolError("CDK协议支付超时", code="PAYMENT_TIMEOUT", retryable=True)


def _run_payment(*, account_id: int, client: CdkWebClient, source_task_id: str, proxy: str, proxy_source: str, trigger: str) -> dict:
    country = str(_setting("CDK_WEB_PROTOCOL_COUNTRY", "") or _setting("CDK_WEB_COUNTRY", "GB") or "GB").upper()
    if not db.claim_account_paypal_payment(account_id, trigger=trigger, country=country, proxy_source=proxy_source):
        return {"ok": False, "status": "failed", "error": "该账号正在协议支付中"}
    db.mark_account_paypal_payment_running(account_id)
    values = _payment_payload(proxy)
    max_attempts = _int("CDK_WEB_MAX_RETRIES", 2, 0, 20) + 1
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        payment_id = ""
        try:
            db.update_account_paypal_payment(account_id, {
                "ok": False, "status": "running", "attempt": attempt,
                "max_attempts": max_attempts, "country": country,
                "proxy_source": proxy_source, "backend": "cdk_web",
                "message": f"CDK网页协议支付第 {attempt}/{max_attempts} 轮",
            })
            preconfig = dict(values)
            preconfig.pop("checkout_proxy", None)
            preconfig.pop("preconfig_override", None)
            client.register_protocol_preconfig(source_task_id, **preconfig)
            created = client.create_protocol_payment(source_task_id, **values)
            payment_id = _task_id(created)
            if not payment_id:
                raise CdkProtocolError("CDK网页未返回协议支付 task_id", code="PAYMENT_TASK_MISSING")
            db.update_account_paypal_payment(account_id, {
                "ok": False, "status": "running", "attempt": attempt,
                "max_attempts": max_attempts, "protocol_job_id": payment_id,
                "country": country, "proxy_source": proxy_source,
                "backend": "cdk_web", "message": "CDK网页协议支付运行中",
            })
            snapshot = _wait_payment(client, payment_id, account_id, attempt, max_attempts)
            return _payment_success(
                account_id, payment_id, snapshot,
                attempt=attempt, max_attempts=max_attempts,
                country=country, proxy_source=proxy_source,
                message="CDK网页协议支付成功",
            )
        except Exception as exc:
            last_error = _redact(exc, proxy)
            code = str(getattr(exc, "code", "") or "").lower()
            # Keep an awaiting-OTP/CAPTCHA task alive.  The remote workbench
            # owns the waiting state; cancelling here would make the manual
            # submit endpoint reject the value and lose the task session.
            if code in {"awaiting_otp", "awaiting_captcha", "manual", "needs_intervention"}:
                final = {
                    "ok": False, "status": "failed", "attempt": attempt,
                    "max_attempts": max_attempts, "protocol_job_id": payment_id,
                    "country": country, "proxy_source": proxy_source,
                    "backend": "cdk_web", "payment_action": code,
                    "error": last_error, "message": "等待人工验证码/验证后可重新支付",
                    "result": {"visitor_id": client.visitor_id, "manual_stage": code},
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                }
                db.update_account_paypal_payment(account_id, final)
                return final
            if payment_id:
                try:
                    client.cancel_protocol_payment(payment_id)
                except Exception:
                    pass
            db.update_account_paypal_payment(account_id, {
                "ok": False, "status": "running" if attempt < max_attempts else "failed",
                "attempt": attempt, "max_attempts": max_attempts,
                "protocol_job_id": payment_id or None, "country": country,
                "proxy_source": proxy_source, "backend": "cdk_web", "error": last_error,
                "message": f"第 {attempt}/{max_attempts} 轮失败" if attempt < max_attempts else last_error,
            })
            if attempt < max_attempts:
                continue
    final = {
        "ok": False, "status": "failed", "attempt": max_attempts,
        "max_attempts": max_attempts, "country": country,
        "proxy_source": proxy_source, "backend": "cdk_web",
        "error": last_error or "CDK网页协议支付失败", "message": last_error or "CDK网页协议支付失败",
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    db.update_account_paypal_payment(account_id, final)
    return final


def run_extract(*, account_id: int, email: str, access_token: str, trigger: str, proxy: str, proxy_source: str) -> dict:
    pool = cdk_pool.get_pool()
    max_attempts = _int("CDK_WEB_MAX_RETRIES", 2, 0, 20) + 1
    last_error = ""
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或提链状态已重置"}
        for attempt in range(1, max_attempts + 1):
            lease = pool.lease(task_id=f"account-{account_id}-{uuid.uuid4().hex}")
            if not lease:
                last_error = "CDK 池没有可用条目"
                break
            lease_id = str(lease.get("id") or lease.get("fingerprint") or "")
            code = str(lease.get("code") or "")
            client = _new_client()
            session = None
            task_id = ""
            try:
                db.update_account_extract(account_id, {
                    "ok": False, "status": "running", "link_type": "paypal",
                    "backend": "cdk_web", "visitor_id": client.visitor_id,
                    "proxy_source": proxy_source,
                    "message": f"CDK网页提链第 {attempt}/{max_attempts} 轮：激活 CDK",
                })
                session = client.activate_lease(pool, lease)
                if not session.valid:
                    raise CdkInvalidError("CDK 无效或已耗尽", code="CDK_USAGE_LIMIT")
                created = client.create_task(
                    access_token,
                    country=str(_setting("CDK_WEB_COUNTRY", "GB") or "GB"),
                    payment_method="paypal",
                    checkout_proxy=proxy,
                    update_proxy=proxy,
                    apply_checkout_update=True,
                    oaics_only=True,
                    protocol_country=str(_setting("CDK_WEB_PROTOCOL_COUNTRY", "GB") or "GB"),
                    sms_country=str(_setting("CDK_WEB_SMS_COUNTRY", "GB") or "GB"),
                    auto_start_protocol=False,
                    window_id=client.visitor_id,
                    window_concurrency=1,
                )
                task_id = _task_id(created)
                if not task_id:
                    raise CdkTaskError("CDK网页未返回提链 task_id", code="TASK_ID_MISSING")
                db.update_account_extract(account_id, {
                    "ok": False, "status": "running", "job_id": task_id,
                    "link_type": "paypal", "backend": "cdk_web", "visitor_id": client.visitor_id,
                    "session_state": client.session_state(),
                    "cdk_remaining": _remaining(session), "proxy_source": proxy_source,
                    "message": "CDK网页提链任务运行中",
                })
                snapshot = client.poll_task(
                    task_id,
                    timeout=_int("CDK_WEB_TASK_TIMEOUT", 900, 30, 3600),
                    interval=_int("CDK_WEB_POLL_INTERVAL", 2, 1, 30),
                    callback=lambda item: db.update_account_extract(account_id, {
                        "ok": False, "status": "running", "job_id": task_id,
                        "backend": "cdk_web", "visitor_id": client.visitor_id,
                        "session_state": client.session_state(),
                        "message": str(item.get("stage") or item.get("message") or item.get("status") or "任务运行中")[:500],
                    }),
                    raise_on_failure=True,
                )
                result = _link_result(_result(snapshot), _remaining(session))
                pool.mark_used(lease_id, remaining_uses=_remaining(session))
                final = {
                    "ok": True, "status": "success", "job_id": task_id,
                    "link_type": "paypal", "backend": "cdk_web", "visitor_id": client.visitor_id,
                    "session_state": client.session_state(),
                    "cdk_remaining": _remaining(session), "proxy_source": proxy_source,
                    "result": result, "message": "CDK网页提链成功",
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                }
                db.update_account_extract(account_id, final)
                if _bool("CDK_WEB_AUTO_PAYMENT", True):
                    final["payment"] = _run_payment(
                        account_id=account_id, client=client, source_task_id=task_id,
                        proxy=proxy, proxy_source=proxy_source,
                        trigger=f"cdk_extract_{trigger}"[:80],
                    )
                return final
            except CdkAuthError as exc:
                # Authentication failure from the task endpoint is treated as
                # AT failure; the current CDK remains reusable.
                message = str(exc)
                is_at = bool(task_id) and ("token" in message.lower() or "access" in message.lower() or getattr(exc, "status_code", 0) == 401)
                pool.release(lease_id, status="available", error="AT失效" if is_at else _redact(exc, code))
                last_error = "AT失效" if is_at else _redact(exc, code)
                break
            except CdkInvalidError as exc:
                last_error = _redact(exc, code)
                if str(getattr(exc, "code", "") or "").upper() in {"CDK_USAGE_LIMIT", "CDK_AT_LIMIT"}:
                    pool.release(lease_id, status="exhausted", remaining_uses=0, error=last_error)
                else:
                    pool.mark_invalid(lease_id, last_error)
                if attempt < max_attempts:
                    continue
            except Exception as exc:
                last_error = _redact(exc, code, access_token, proxy)
                # A failed task can encode an HTTP 401 in its terminal
                # snapshot instead of raising CdkAuthError.  Treat that as
                # account AT expiry and keep the CDK reusable; do not rotate
                # through the whole pool for one invalid account.
                if task_id and re.search(r"(?i)(?:\b401\b|unauthori[sz]ed|access.?token|token.{0,20}(?:expired|invalid)|at失效)", last_error):
                    pool.release(lease_id, status="available", error="AT失效")
                    last_error = "AT失效"
                    break
                if task_id:
                    try:
                        client.cancel_task(task_id)
                    except Exception:
                        pass
                # A transient task failure can consume a use; keep the latest
                # known count, otherwise make the CDK available for a later run.
                remaining = _remaining(session)
                pool.mark_used(lease_id, remaining_uses=remaining, error=last_error)
                if attempt < max_attempts:
                    continue
            finally:
                client.close()
        final = {
            "ok": False, "status": "failed", "link_type": "paypal", "backend": "cdk_web",
            "proxy_source": proxy_source, "error": last_error or "CDK网页提链失败",
            "message": last_error or "CDK网页提链失败", "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_extract(account_id, final)
        return final
    except Exception as exc:
        reason = _redact(exc, access_token, proxy)
        final = {"ok": False, "status": "failed", "backend": "cdk_web", "proxy_source": proxy_source, "error": reason, "message": reason}
        db.update_account_extract(account_id, final)
        return final
    finally:
        _QUEUE_SLOTS.release()


_WORKERS = _int("CDK_WEB_WORKERS", 2, 1, 16)
_QUEUE_LIMIT = _int("CDK_WEB_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="cdk-web")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def enqueue_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", proxy: str | None = None, **_kwargs) -> dict:
    if not enabled():
        return {"accepted": False, "busy": False, "error": "CDK 网页后端尚未启用"}
    if cdk_pool.get_pool().available_count() <= 0:
        return {"accepted": False, "busy": False, "error": "CDK 池没有可用条目"}
    selected, source = _proxy(int(account_id), proxy)
    if not selected:
        return {"accepted": False, "busy": False, "error": "没有可用 CDK 代理：请填写本次/默认代理或保存注册代理"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "CDK 网页队列已满"}
    try:
        if not db.claim_account_extract(int(account_id), trigger=trigger, link_type="paypal", backend="cdk_web"):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        future = _EXECUTOR.submit(run_extract, account_id=int(account_id), email=str(email or ""), access_token=str(access_token or ""), trigger=trigger, proxy=selected, proxy_source=source)
        return {"accepted": True, "busy": False, "future": future, "backend": "cdk_web", "link_type": "paypal", "proxy_source": source}
    except Exception:
        _QUEUE_SLOTS.release()
        raise


def _visitor_from_account(account: dict) -> str:
    value = str(account.get("extract_link_cdk_visitor") or "").strip()
    if value:
        return value
    try:
        payload = json.loads(str(account.get("paypal_payment_result_json") or "{}"))
    except Exception:
        payload = {}
    return str(payload.get("visitor_id") or "").strip() if isinstance(payload, dict) else ""


def _session_from_account(account: dict) -> tuple[str, dict[str, str]]:
    """Restore the private CDK visitor/cookie session for a payment resume."""
    visitor = ""
    cookies: dict[str, str] = {}
    raw = str(account.get("extract_link_cdk_session_json") or "").strip()
    if raw:
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            state = {}
        if isinstance(state, dict):
            visitor = str(state.get("visitor_id") or state.get("visitor") or "").strip()[:128]
            raw_cookies = state.get("cookies")
            if isinstance(raw_cookies, dict):
                for name, value in raw_cookies.items():
                    name_text = str(name or "").strip()[:80]
                    value_text = str(value or "").strip()[:512]
                    if name_text and value_text:
                        cookies[name_text] = value_text
    if not visitor:
        visitor = _visitor_from_account(account)
    # Older rows only stored the stable visitor header.  The workbench uses
    # the same value for opl_visitor, so this fallback keeps a resumed task
    # bound to its original session after a process restart.
    if visitor and "opl_visitor" not in cookies:
        cookies["opl_visitor"] = visitor
    return visitor, cookies


def enqueue_payment(*, account_id: int, trigger: str = "manual", proxy: str | None = None, country: str | None = None) -> dict:
    account = db.get_account(int(account_id)) or {}
    if not account:
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    if str(account.get("extract_link_backend") or "").lower() != "cdk_web":
        return {"accepted": False, "busy": False, "error": "该账号不是 CDK 网页提链记录"}
    source_task_id = str(account.get("extract_link_job_id") or "").strip()
    if not source_task_id:
        return {"accepted": False, "busy": False, "error": "缺少 CDK 网页提链任务 ID"}
    if not db.account_extract_link_is_fresh(int(account_id)):
        return {"accepted": False, "busy": False, "error": "PayPal 提链已过期，请先重新提链"}
    selected, source = _proxy(int(account_id), proxy)
    if not selected:
        return {"accepted": False, "busy": False, "error": "没有可用 CDK 支付代理"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "CDK 网页队列已满"}
    visitor, cookies = _session_from_account(account)

    def worker():
        try:
            client = _new_client(visitor, cookies)
            try:
                return _run_payment(account_id=int(account_id), client=client, source_task_id=source_task_id, proxy=selected, proxy_source=source, trigger=trigger)
            finally:
                client.close()
        finally:
            _QUEUE_SLOTS.release()

    future = _EXECUTOR.submit(worker)
    return {"accepted": True, "busy": False, "future": future, "backend": "cdk_web", "country": str(country or _setting("CDK_WEB_PROTOCOL_COUNTRY", "GB")).upper(), "proxy_source": source}


def _resume_payment(*, account_id: int, client: CdkWebClient, task_id: str, proxy: str, proxy_source: str) -> dict:
    """Continue polling a remote payment task after manual OTP/CAPTCHA input."""
    account = db.get_account(int(account_id)) or {}
    try:
        attempt = max(1, int(account.get("paypal_payment_attempt") or 1))
    except (TypeError, ValueError):
        attempt = 1
    try:
        max_attempts = max(1, int(account.get("paypal_payment_max_attempts") or 0))
    except (TypeError, ValueError):
        max_attempts = 0
    if not max_attempts:
        max_attempts = _int("CDK_WEB_MAX_RETRIES", 2, 0, 20) + 1
    country = str(account.get("paypal_payment_country") or _setting("CDK_WEB_PROTOCOL_COUNTRY", "GB") or "GB").upper()
    db.update_account_paypal_payment(account_id, {
        "ok": False, "status": "running", "attempt": attempt,
        "max_attempts": max_attempts, "protocol_job_id": task_id,
        "country": country, "proxy_source": proxy_source,
        "backend": "cdk_web", "message": "人工值已提交，恢复 CDK 网页协议支付轮询",
    })
    try:
        snapshot = _wait_payment(client, task_id, account_id, attempt, max_attempts)
        return _payment_success(
            account_id, task_id, snapshot,
            attempt=attempt, max_attempts=max_attempts,
            country=country, proxy_source=proxy_source,
            message="CDK网页协议支付成功",
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "").lower()
        error = _redact(exc, proxy)
        if code in {"awaiting_otp", "awaiting_captcha", "manual", "needs_intervention"}:
            final = {
                "ok": False, "status": "failed", "attempt": attempt,
                "max_attempts": max_attempts, "protocol_job_id": task_id,
                "country": country, "proxy_source": proxy_source,
                "backend": "cdk_web", "payment_action": code,
                "error": error, "message": "仍等待人工验证码/验证，可继续提交",
                "result": {"manual_stage": code},
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
        else:
            final = {
                "ok": False, "status": "failed", "attempt": attempt,
                "max_attempts": max_attempts, "protocol_job_id": task_id,
                "country": country, "proxy_source": proxy_source,
                "backend": "cdk_web", "error": error, "message": error,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
        db.update_account_paypal_payment(account_id, final)
        return final


def submit_intervention(*, account_id: int, value: str, kind: str = "otp") -> dict:
    account = db.get_account(int(account_id)) or {}
    if not account:
        raise ValueError("账号不存在")
    kind = str(kind or "otp").strip().lower()
    if kind not in {"otp", "captcha"}:
        raise ValueError("人工提交类型仅支持 otp/captcha")
    value = str(value or "").strip()
    if not value:
        raise ValueError("验证码/验证结果不能为空")
    task_id = str(account.get("paypal_payment_protocol_job_id") or "").strip()
    if not task_id:
        raise ValueError("账号没有等待人工处理的协议支付任务")
    visitor, cookies = _session_from_account(account)
    # The original payment task already carries its checkout proxy on the
    # workbench.  Manual OTP/CAPTCHA submission only touches that existing
    # task, so it remains resumable even if a process restart temporarily
    # leaves no local proxy setting.  Use the saved source for redaction/UI.
    try:
        proxy, proxy_source = _proxy(int(account_id), None)
    except Exception:
        proxy = ""
        proxy_source = str(account.get("paypal_payment_proxy_source") or "none").strip().lower() or "none"
    if not _QUEUE_SLOTS.acquire(blocking=False):
        raise ValueError("CDK 网页队列已满")
    client = None
    try:
        client = _new_client(visitor, cookies)
        submitted = client.submit_captcha(task_id, value) if kind == "captcha" else client.submit_otp(task_id, value)
        db.update_account_paypal_payment(account_id, {
            "ok": False, "status": "running", "attempt": account.get("paypal_payment_attempt") or 1,
            "max_attempts": account.get("paypal_payment_max_attempts") or (_int("CDK_WEB_MAX_RETRIES", 2, 0, 20) + 1),
            "protocol_job_id": task_id, "proxy_source": proxy_source, "backend": "cdk_web",
            "message": "人工值已提交，正在恢复协议支付",
        })
        def resume_worker():
            try:
                return _resume_payment(
                    account_id=int(account_id), client=client, task_id=task_id,
                    proxy=proxy, proxy_source=proxy_source,
                )
            finally:
                client.close()
                _QUEUE_SLOTS.release()

        future = _EXECUTOR.submit(resume_worker)
        return {
            "accepted": True,
            "status": str(submitted.get("status") or "running") if isinstance(submitted, dict) else "running",
            "protocol_job_id": task_id,
            "kind": kind,
            "future": future,
        }
    except Exception:
        _QUEUE_SLOTS.release()
        if client is not None:
            client.close()
        raise


__all__ = ["enabled", "public_settings", "queue_settings", "enqueue_extract", "enqueue_payment", "submit_intervention", "run_extract"]
