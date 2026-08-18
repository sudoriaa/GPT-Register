# -*- coding: utf-8 -*-
"""PayPal BA 协议支付队列与 SMSBower 自动验证码适配。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from config import paypal_payment as cfg
from core import db
from core.paypal_smsbower import PayPalSmsBowerClient, SmsBowerCodeTimeout, SmsBowerError

logger = logging.getLogger(__name__)

_BA_TOKEN_RE = re.compile(r"\bBA-[A-Za-z0-9_-]{6,100}\b", re.IGNORECASE)
_TERMINAL_PROTOCOL_STATUSES = {"completed", "failed", "cancelled", "stopped"}


class PaymentConfigurationError(RuntimeError):
    """支付配置或本地协议项目不可用。"""


class ProtocolPaymentError(RuntimeError):
    """外部 PayPal BA 协议任务失败。"""


class ManualInterventionRequired(ProtocolPaymentError):
    """协议服务进入需要人工验证的状态。"""


def _runtime_setting(name: str, default=None):
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


def auto_payment_enabled() -> bool:
    return _bool_setting("PAYPAL_PAYMENT_AUTO", False)


def _country(value: str | None = None) -> str:
    country = str(value or _runtime_setting("PAYPAL_PAYMENT_COUNTRY", "GB") or "GB").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise PaymentConfigurationError("协议支付账单国家必须是两位国家代码")
    return country


def _sms_api_key() -> str:
    return str(_runtime_setting("PAYPAL_PAYMENT_SMS_API_KEY", "") or "").strip()


def public_settings() -> dict:
    """只返回页面可展示的设置，不回显 API Key 或代理认证。"""
    project = Path(str(_runtime_setting("PAYPAL_PAYMENT_PROJECT_PATH", "") or "").strip()).expanduser()
    return {
        "auto_payment": auto_payment_enabled(),
        "payment_country": _country(),
        "payment_proxy_configured": bool(str(_runtime_setting("PAYPAL_PAYMENT_PROXY", "") or "").strip()),
        "sms_api_key_configured": bool(_sms_api_key()),
        "sms_country": str(_runtime_setting("PAYPAL_PAYMENT_SMS_COUNTRY", "16") or "16").strip(),
        "sms_provider_ids": str(_runtime_setting("PAYPAL_PAYMENT_SMS_PROVIDER_IDS", "") or "").strip(),
        "sms_service": str(_runtime_setting("PAYPAL_PAYMENT_SMS_SERVICE", "paypal") or "paypal").strip(),
        "sms_timeout": _int_setting("PAYPAL_PAYMENT_SMS_TIMEOUT", 120, 20, 3600),
        "payment_retries": _int_setting("PAYPAL_PAYMENT_MAX_RETRIES", 2, 0, 20),
        "service_base": str(_runtime_setting("PAYPAL_PAYMENT_SERVICE_BASE", "http://127.0.0.1:18097") or "").strip().rstrip("/"),
        "service_autostart": _bool_setting("PAYPAL_PAYMENT_AUTOSTART_SERVICE", True),
        "protocol_project_exists": project.exists(),
    }


_WORKERS = _int_setting("PAYPAL_PAYMENT_WORKERS", 2, 1, 16)
_QUEUE_LIMIT = _int_setting("PAYPAL_PAYMENT_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="paypal-payment")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_QUEUE_STATE_LOCK = threading.Lock()
_QUEUE_STATE = {"queued": 0, "running": 0}


def _queue_delta(*, queued: int = 0, running: int = 0) -> None:
    with _QUEUE_STATE_LOCK:
        _QUEUE_STATE["queued"] = max(0, int(_QUEUE_STATE.get("queued") or 0) + int(queued))
        _QUEUE_STATE["running"] = max(0, int(_QUEUE_STATE.get("running") or 0) + int(running))


def queue_settings() -> dict:
    with _QUEUE_STATE_LOCK:
        snapshot = dict(_QUEUE_STATE)
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT, **snapshot}


def extract_ba_token(value: object) -> str:
    """从完整 PayPal approve URL 或纯 BA Token 中提取协议 token。"""
    text = unquote(str(value or "").strip())
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        for key in ("ba_token", "token", "billingAgreementId"):
            candidate = str((parse_qs(parsed.query).get(key) or [""])[0] or "")
            match = _BA_TOKEN_RE.search(candidate)
            if match:
                return match.group(0).upper()
    except Exception:
        pass
    match = _BA_TOKEN_RE.search(text)
    return match.group(0).upper() if match else ""


def _normalize_proxy(value: str | None) -> str:
    # Keep one proxy grammar across extraction, payment and account copy.
    from core.extract_link_service import _normalize_proxy as normalize_extract_proxy
    return normalize_extract_proxy(value)


def _account_proxy(account_id: int) -> str:
    row = db.get_account(int(account_id)) or {}
    return _normalize_proxy(row.get("registration_proxy") or row.get("proxy_used"))


def resolve_payment_proxy(account_id: int, override: str | None = None) -> tuple[str, str]:
    raw_override = str(override or "").strip()
    override_value = _normalize_proxy(raw_override)
    if raw_override and not override_value:
        raise PaymentConfigurationError("本次协议支付代理格式无效")
    raw_global = str(_runtime_setting("PAYPAL_PAYMENT_PROXY", "") or "").strip()
    global_value = _normalize_proxy(raw_global)
    if raw_global and not global_value:
        raise PaymentConfigurationError("全局协议支付代理格式无效")
    for source, value in (
        ("custom", override_value),
        ("global", global_value),
        ("registration", _account_proxy(account_id)),
    ):
        if value:
            return value, source
    return "", "none"


def _redact(value: object, *secrets: object, limit: int = 1200) -> str:
    text = str(value or "")
    for secret in sorted({str(item) for item in secrets if str(item or "")}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@", r"\1://***:***@", text)
    text = re.sub(r"\bBA-[A-Za-z0-9_-]{6,100}\b", "BA-[REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<!\d)\+?\d{8,20}(?!\d)", "[PHONE_REDACTED]", text)
    return text[:limit]


_SERVICE_LOCK = threading.RLock()
_SERVICE_PROCESS: subprocess.Popen | None = None
_SERVICE_LOG_HANDLE = None


def _service_base() -> str:
    base = str(_runtime_setting("PAYPAL_PAYMENT_SERVICE_BASE", "http://127.0.0.1:18097") or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PaymentConfigurationError("PAYPAL_PAYMENT_SERVICE_BASE 地址无效")
    return base


def _health_ok(base: str, timeout: float = 1.5) -> bool:
    try:
        response = httpx.get(base + "/api/health", timeout=timeout, trust_env=False)
        return response.status_code == 200 and bool((response.json() or {}).get("ok", True))
    except Exception:
        return False


def _find_protocol_script(project: Path) -> Path | None:
    candidates = [
        project / "web.py",
        project / "paypal-agreement-protocol" / "web.py",
        project / ".integration-web.py",
    ]
    candidates.extend(sorted(project.glob("*/web.py")) if project.exists() else [])
    return next((item for item in candidates if item.exists() and item.is_file()), None)


def _protocol_python(script: Path, project: Path) -> str:
    configured = str(_runtime_setting("PAYPAL_PAYMENT_PYTHON", "") or "").strip()
    candidates = (
        configured,
        str(script.parent / ".venv" / "Scripts" / "python.exe"),
        str(script.parent / ".venv" / "bin" / "python"),
        str(project / ".venv" / "Scripts" / "python.exe"),
        str(project / ".venv" / "bin" / "python"),
        sys.executable,
    )
    return next((item for item in candidates if item and Path(item).exists()), sys.executable)


def ensure_protocol_service() -> str:
    """健康检查并按需启动本地 paypal-agreement-protocol Web 服务。"""
    global _SERVICE_PROCESS, _SERVICE_LOG_HANDLE
    base = _service_base()
    if _health_ok(base):
        return base
    if not _bool_setting("PAYPAL_PAYMENT_AUTOSTART_SERVICE", True):
        raise PaymentConfigurationError(f"PayPal 协议支付服务未启动: {base}")
    parsed = urlsplit(base)
    if (parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
        raise PaymentConfigurationError("远程协议支付服务不可自动启动，请先启动服务或关闭自动启动")
    project = Path(str(_runtime_setting("PAYPAL_PAYMENT_PROJECT_PATH", "") or "").strip()).expanduser()
    script = _find_protocol_script(project)
    if script is None:
        raise PaymentConfigurationError("协议支付项目缺少 web.py/.integration-web.py")
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    with _SERVICE_LOCK:
        if _health_ok(base):
            return base
        if _SERVICE_PROCESS is not None and _SERVICE_PROCESS.poll() is None:
            pass
        else:
            python_exe = _protocol_python(script, project)
            log_dir = Path(__file__).resolve().parent.parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _SERVICE_LOG_HANDLE = open(log_dir / "paypal-payment-service.log", "a", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            _SERVICE_PROCESS = subprocess.Popen(
                [python_exe, str(script), "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(script.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=_SERVICE_LOG_HANDLE,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        timeout = _int_setting("PAYPAL_PAYMENT_SERVICE_START_TIMEOUT", 20, 2, 120)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _health_ok(base):
                logger.info("[协议支付] 本地 PP协议服务已启动: %s", base)
                return base
            if _SERVICE_PROCESS is not None and _SERVICE_PROCESS.poll() is not None:
                break
            time.sleep(0.25)
    raise PaymentConfigurationError(
        "本地 PP协议服务启动失败；请确认完整 paypal-agreement-protocol 源码与依赖已放入配置目录，"
        "详情见 logs/paypal-payment-service.log"
    )


class PaypalProtocolHttpRunner:
    """驱动外部 PP协议 Web API，并在 awaiting_otp 时自动提交验证码。"""

    def run(
        self,
        *,
        ba_token: str,
        phone: str,
        country: str,
        proxy: str,
        otp_supplier: Callable[[], str],
        progress: Callable[[str, str], None] | None = None,
    ) -> dict:
        base = ensure_protocol_service()
        timeout_seconds = _int_setting("PAYPAL_PAYMENT_PROTOCOL_TIMEOUT", 600, 60, 3600)
        poll_interval = _int_setting("PAYPAL_PAYMENT_POLL_INTERVAL", 2, 1, 15)
        progress = progress or (lambda _message, _job_id="": None)
        headers = {"X-Internal-Auto-Channel": "1"}
        with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False, headers=headers) as client:
            response = client.post(base + "/api/jobs", json={
                "ba_token": ba_token,
                "phone": phone,
                "country": country,
                "buyer_mode": str(_runtime_setting("PAYPAL_PAYMENT_BUYER_MODE", "identity_elevation") or "identity_elevation"),
                "agreement_only": _bool_setting("PAYPAL_PAYMENT_AGREEMENT_ONLY", False),
                "proxies": [proxy],
            })
            try:
                payload = response.json()
            except Exception:
                payload = {}
            if response.status_code not in {200, 201, 202}:
                raise ProtocolPaymentError(str(payload.get("error") or response.text or f"HTTP {response.status_code}")[:1000])
            job = payload.get("job") if isinstance(payload, dict) else None
            job = job if isinstance(job, dict) else payload
            job_id = str((job or {}).get("id") or (job or {}).get("job_id") or "").strip()
            if not job_id:
                raise ProtocolPaymentError("PP协议服务未返回 job id")
            progress("PP协议任务已创建", job_id)
            deadline = time.monotonic() + timeout_seconds
            otp_submitted = False
            last_stage = ""
            try:
                while time.monotonic() < deadline:
                    snapshot_response = client.get(base + f"/api/jobs/{job_id}")
                    try:
                        snapshot = snapshot_response.json()
                    except Exception:
                        snapshot = {}
                    if snapshot_response.status_code != 200:
                        raise ProtocolPaymentError(str(snapshot.get("error") or snapshot_response.text or f"HTTP {snapshot_response.status_code}")[:1000])
                    status = str(snapshot.get("status") or "").strip().lower()
                    stage = str(snapshot.get("stage") or snapshot.get("message") or status or "协议支付运行中")
                    if stage != last_stage:
                        progress(stage[:500], job_id)
                        last_stage = stage
                    if status == "awaiting_otp":
                        if otp_submitted:
                            raise ProtocolPaymentError("PayPal 验证码未通过，协议服务再次请求验证码")
                        code = str(otp_supplier() or "").strip()
                        if not code:
                            raise SmsBowerCodeTimeout("SMSBower 未返回验证码")
                        otp_response = client.post(base + f"/api/jobs/{job_id}/otp", json={"value": code})
                        if otp_response.status_code not in {200, 201, 202}:
                            try:
                                otp_payload = otp_response.json()
                            except Exception:
                                otp_payload = {}
                            raise ProtocolPaymentError(str(otp_payload.get("error") or otp_response.text or "提交验证码失败")[:1000])
                        otp_submitted = True
                        progress("SMSBower 验证码已提交", job_id)
                    elif status == "awaiting_captcha":
                        raise ManualInterventionRequired("PP协议进入人工验证状态，请在失败记录中人工重试/处理")
                    elif status == "completed":
                        result = snapshot.get("result") if isinstance(snapshot.get("result"), dict) else {}
                        result_status = str(result.get("status") or "success").strip().lower()
                        if result_status != "success":
                            raise ProtocolPaymentError(str(result.get("error") or result.get("message") or "协议支付结果失败")[:1000])
                        return {"protocol_job_id": job_id, "result": result}
                    elif status in {"failed", "cancelled", "stopped"}:
                        raise ProtocolPaymentError(str(snapshot.get("error") or snapshot.get("stage") or f"协议支付状态 {status}")[:1000])
                    time.sleep(poll_interval)
                # Treat a polling deadline like every other terminal error so
                # the protocol service receives a cancellation request before
                # the worker releases the SMS activation and retries.
                raise ProtocolPaymentError(f"PP协议任务超时（>{timeout_seconds}s）")
            except Exception:
                try:
                    client.post(base + f"/api/jobs/{job_id}/cancel", json={})
                except Exception:
                    pass
                raise


def _sms_client():
    """Create the shared, dependency-injected SMSBower client.

    Keeping this behind a factory lets tests replace it without touching the
    payment worker and keeps registration's global SMS settings isolated.
    """
    return PayPalSmsBowerClient(
        api_key=_sms_api_key(),
        base_url=str(_runtime_setting("PAYPAL_PAYMENT_SMS_API_BASE", "https://smsbower.page/stubs/handler_api.php") or ""),
        service=str(_runtime_setting("PAYPAL_PAYMENT_SMS_SERVICE", "paypal") or "paypal"),
        country=str(_runtime_setting("PAYPAL_PAYMENT_SMS_COUNTRY", "16") or "16"),
        provider_ids=str(_runtime_setting("PAYPAL_PAYMENT_SMS_PROVIDER_IDS", "") or ""),
        request_timeout=float(_int_setting("PAYPAL_PAYMENT_SMS_REQUEST_TIMEOUT", 30, 5, 120)),
        poll_interval=float(_int_setting("PAYPAL_PAYMENT_SMS_POLL_INTERVAL", 3, 1, 30)),
        # A worker should not block for the full provider cancellation grace
        # period. The client still retries according to its bounded policy;
        # un-cancellable activations remain tracked for later cleanup.
        cancel_grace_period=float(_int_setting("PAYPAL_PAYMENT_SMS_CANCEL_GRACE", 5, 0, 60)),
        cancel_retry_delay=5.0,
        cancel_retries=1,
    )


def _safe_result_summary(result: dict) -> dict:
    source = result if isinstance(result, dict) else {}
    allowed = (
        "status", "settlement_status", "redirect_status", "payment_action",
        "billing_country", "stage", "retryable", "paypal_authorized",
        "grok_subscription_active", "grok_subscription_count",
    )
    summary = {key: source.get(key) for key in allowed if source.get(key) not in (None, "")}
    # Correlate successes without persisting replayable BA/EC/Payer tokens.
    fingerprint_source = "|".join(str(source.get(key) or "") for key in ("ba_token", "ec_token", "user_id"))
    if fingerprint_source.strip("|"):
        summary["result_fingerprint"] = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:20]
    return summary


def _run_payment(
    *,
    account_id: int,
    email: str,
    ba_token: str,
    trigger: str,
    country: str,
    proxy: str,
    proxy_source: str,
) -> dict:
    _queue_delta(queued=-1, running=1)
    retry_count = _int_setting("PAYPAL_PAYMENT_MAX_RETRIES", 2, 0, 20)
    max_attempts = retry_count + 1
    try:
        if not db.mark_account_paypal_payment_running(account_id):
            return {"ok": False, "status": "failed", "error": "账号已删除或支付状态已重置"}
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            sms: PayPalSmsBowerClient | None = None
            activation_id = ""
            activation = None
            phone = ""
            protocol_job_id = ""
            try:
                db.update_account_paypal_payment(account_id, {
                    "ok": False,
                    "status": "running",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "country": country,
                    "proxy_source": proxy_source,
                    "message": f"第 {attempt}/{max_attempts} 轮：正在从 SMSBower 取号",
                })
                sms = _sms_client()
                activation = sms.acquire()
                activation_id = str(getattr(activation, "activation_id", "") or "")
                phone = str(getattr(activation, "phone_number", "") or "")
                if not activation_id or not phone:
                    raise SmsBowerError("SMSBower 返回的号码信息不完整")
                activation_fingerprint = hashlib.sha256(activation_id.encode("utf-8")).hexdigest()[:16]
                phone_digits = re.sub(r"\D", "", phone)
                db.update_account_paypal_payment(account_id, {
                    "ok": False,
                    "status": "running",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "country": country,
                    "phone_country": str(_runtime_setting("PAYPAL_PAYMENT_SMS_COUNTRY", "") or ""),
                    "phone_last4": phone_digits[-4:],
                    "activation_fingerprint": activation_fingerprint,
                    "message": f"第 {attempt}/{max_attempts} 轮：号码已取得，正在启动 PP协议",
                })

                def progress(message: str, job_id: str = "") -> None:
                    nonlocal protocol_job_id
                    if job_id:
                        protocol_job_id = job_id
                    db.update_account_paypal_payment(account_id, {
                        "ok": False,
                        "status": "running",
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "protocol_job_id": protocol_job_id or None,
                        "message": f"第 {attempt}/{max_attempts} 轮：{message}",
                    })

                runner = PaypalProtocolHttpRunner()
                outcome = runner.run(
                    ba_token=ba_token,
                    phone=phone,
                    country=country,
                    proxy=proxy,
                    otp_supplier=lambda: sms.get_code(
                        activation,
                        timeout=_int_setting("PAYPAL_PAYMENT_SMS_TIMEOUT", 120, 20, 3600),
                    ),
                    progress=progress,
                )
                protocol_job_id = str(outcome.get("protocol_job_id") or protocol_job_id)
                raw_result = outcome.get("result") if isinstance(outcome.get("result"), dict) else {}
                safe_result = _safe_result_summary(raw_result)
                if sms:
                    sms.complete(activation)
                final = {
                    "ok": True,
                    "status": "success",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "protocol_job_id": protocol_job_id,
                    "country": country,
                    "proxy_source": proxy_source,
                    "settlement_status": safe_result.get("settlement_status"),
                    "redirect_status": safe_result.get("redirect_status"),
                    "payment_action": safe_result.get("payment_action"),
                    "result": safe_result,
                    "message": "协议支付成功",
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                }
                db.update_account_paypal_payment(account_id, final)
                logger.info("[协议支付] 成功: %s attempt=%s/%s country=%s", email, attempt, max_attempts, country)
                return final
            except PaymentConfigurationError:
                raise
            except Exception as exc:
                last_error = _redact(f"{type(exc).__name__}: {exc}", ba_token, phone, proxy)
                if sms and activation_id:
                    try:
                        sms.cancel(activation)
                    except Exception:
                        pass
                logger.warning("[协议支付] 第 %s/%s 轮失败: %s: %s", attempt, max_attempts, email, last_error)
                db.update_account_paypal_payment(account_id, {
                    "ok": False,
                    "status": "running" if attempt < max_attempts else "failed",
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "protocol_job_id": protocol_job_id or None,
                    "country": country,
                    "proxy_source": proxy_source,
                    "error": last_error,
                    "message": (
                        f"第 {attempt}/{max_attempts} 轮失败，正在更换号码重试：{last_error}"
                        if attempt < max_attempts else last_error
                    ),
                })
                if attempt < max_attempts:
                    continue
            finally:
                if sms:
                    sms.close()
        final = {
            "ok": False,
            "status": "failed",
            "attempt": max_attempts,
            "max_attempts": max_attempts,
            "country": country,
            "proxy_source": proxy_source,
            "error": last_error or "协议支付失败",
            "message": last_error or "协议支付失败",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_paypal_payment(account_id, final)
        return final
    except Exception as exc:
        reason = _redact(f"{type(exc).__name__}: {exc}", ba_token, proxy)
        final = {
            "ok": False,
            "status": "failed",
            "attempt": 0,
            "max_attempts": max_attempts,
            "country": country,
            "proxy_source": proxy_source,
            "error": reason,
            "message": reason,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_paypal_payment(account_id, final)
        logger.exception("[协议支付] 配置/启动失败: %s", email)
        return final
    finally:
        _queue_delta(running=-1)
        _QUEUE_SLOTS.release()


def enqueue_account_payment(
    *,
    account_id: int,
    trigger: str = "manual",
    proxy: str | None = None,
    country: str | None = None,
) -> dict:
    """把一个已有有效 BA 提链的账号放入协议支付队列。"""
    account = db.get_account(int(account_id)) or {}
    if not account:
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    if str(account.get("extract_link_status") or "").strip().lower() != "success":
        return {"accepted": False, "busy": False, "error": "账号尚未提链成功"}
    if not db.account_extract_link_is_fresh(int(account_id)):
        return {"accepted": False, "busy": False, "error": "PayPal 提链已过期，请先重新提链"}
    link = str(account.get("extract_link_long_url") or account.get("extract_link_copy_paste") or "").strip()
    ba_token = extract_ba_token(link)
    if not ba_token:
        return {"accepted": False, "busy": False, "error": "提链结果不含有效 BA Token"}
    if not _sms_api_key():
        return {"accepted": False, "busy": False, "error": "请先配置协议支付 SMSBower API Key"}
    payment_country = _country(country)
    selected_proxy, proxy_source = resolve_payment_proxy(int(account_id), proxy)
    if not selected_proxy:
        return {"accepted": False, "busy": False, "error": "没有可用协议支付代理：请配置自定义代理或保存注册代理"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "协议支付队列已满"}
    try:
        if not db.claim_account_paypal_payment(
            int(account_id),
            trigger=trigger,
            country=payment_country,
            proxy_source=proxy_source,
        ):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在协议支付中"}
        _queue_delta(queued=1)
        future = _EXECUTOR.submit(
            _run_payment,
            account_id=int(account_id),
            email=str(account.get("email") or ""),
            ba_token=ba_token,
            trigger=trigger,
            country=payment_country,
            proxy=selected_proxy,
            proxy_source=proxy_source,
        )
        return {
            "accepted": True,
            "busy": False,
            "future": future,
            "country": payment_country,
            "proxy_source": proxy_source,
        }
    except Exception:
        _QUEUE_SLOTS.release()
        raise
