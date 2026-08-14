# -*- coding: utf-8 -*-
"""账号 2FA 补跑后台队列。

对既有账号在后台补跑或重置 2FA：
  1. 本地密码和旧 TOTP 齐全时，优先在同一 Roxy 窗口完成登录和设置；
  2. 缺少 TOTP 或浏览器明确进入邮箱验证码页时，保留协议 OTP 兼容链路；
  3. 登录、设置和确认节点各自有限重试，写回新 secret 和 accessToken。

与查活共用同一份日志文件（注册日志/live-check-{邮箱}.log），每条日志带 [2FA] 前缀。
"""
from __future__ import annotations

import logging
import random
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_export import (
    _trigger_reauth, _follow_reauth, _validate_reauth_otp,
    _exchange_new_token, _enroll_totp, _activate_totp,
    trigger_password_reset, submit_new_password,
)
from core.account_liveness import log_path, relogin_account
from core.account_liveness import (
    _account_proxy_routes,
    _is_retryable_network_error,
    _load_roxy_helpers,
    _proxy_attempt_limit,
    _redacted_browser_logs,
    _staggered_open_profile,
    _totp_redaction_values,
)
from core.email_provider import wait_for_otp, wait_for_reset_link
from core.humanize import delay as human_delay
from core.openai_auth import AccountUnusableError, EmailOtpInvalidError, send_email_otp
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)

_WORKERS = 2
_QUEUE_LIMIT = 200
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="twofa")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()

_REAUTH_MAX_OTP_ATTEMPTS = 3
_PASSWORD_MAX_ATTEMPTS = 3
_STAGE_MAX_ATTEMPTS = 3
_LOGIN_URL = "https://chatgpt.com/auth/login"


class _EmailOtpFallbackRequired(RuntimeError):
    """Roxy reached an email-code page; the protocol OTP path owns that flow."""


def _stage_attempt_limit(value: int | None = None) -> int:
    if value is None:
        try:
            from config import twofa as twofa_config

            value = int(getattr(twofa_config, "TWOFA_MAX_ATTEMPTS", _STAGE_MAX_ATTEMPTS) or _STAGE_MAX_ATTEMPTS)
        except Exception:
            value = _STAGE_MAX_ATTEMPTS
    return max(1, min(5, int(value or 1)))


def _is_terminal_stage_error(exc: BaseException) -> bool:
    if isinstance(exc, (AccountUnusableError, EmailOtpInvalidError, ValueError, _EmailOtpFallbackRequired)):
        return True
    if type(exc).__name__ == "StopRequested":
        return True
    text = str(exc or "").lower()
    return any(marker in text for marker in (
        "账号密码错误",
        "wrong password",
        "invalid password",
        "password is incorrect",
        "邮箱或密码",
        "2fa(totp) 验证码连续错误",
        "totp secret",
        "account_deactivated",
        "account deleted",
        "account disabled",
        "账号已废",
        "账号停用",
        "账号封禁",
        "手动停止",
    ))


def _run_stage_with_recovery(
    stage: str,
    operation,
    *,
    recover=None,
    max_attempts: int | None = None,
):
    """Retry one resumable node without replaying already completed nodes."""
    attempts = _stage_attempt_limit(max_attempts)
    last_exc: BaseException | None = None
    history: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_exc = exc
            history.append(f"{attempt}:{type(exc).__name__}:{str(exc)[:180]}")
            if _is_terminal_stage_error(exc) or attempt >= attempts:
                break
            logger.warning(
                "[2FA][%s] 节点失败（%s/%s），从当前节点恢复：%s: %s",
                stage,
                attempt,
                attempts,
                type(exc).__name__,
                str(exc)[:220],
            )
            if recover is not None:
                try:
                    recover(attempt, exc)
                except Exception as recovery_exc:
                    history.append(
                        f"recover{attempt}:{type(recovery_exc).__name__}:{str(recovery_exc)[:180]}"
                    )
                    if _is_terminal_stage_error(recovery_exc):
                        raise
            time.sleep(min(2.0, 0.5 * attempt))
    if last_exc is not None and _is_terminal_stage_error(last_exc):
        raise last_exc
    raise RuntimeError(
        f"2FA 节点 {stage} 连续 {attempts} 次未完成；"
        f"last={type(last_exc).__name__ if last_exc else 'unknown'}: {str(last_exc or '')[:240]}; "
        f"history={' | '.join(history)}"
    ) from last_exc


def is_running(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("twofa_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _is_retryable_network(exc: BaseException) -> bool:
    return _is_retryable_network_error(exc)


def _reauth_otp_with_retry(session, email: str, otp_after_ts: float) -> str:
    """等待重认证 OTP 并提交验证；无效/过期自动重发重试，返回 continue_url。"""
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, _REAUTH_MAX_OTP_ATTEMPTS + 1):
        try:
            if current_otp is None:
                logger.info("[2FA] 等待重认证 OTP：%s（第 %s/%s 次）", email, attempt, _REAUTH_MAX_OTP_ATTEMPTS)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts)
            return _validate_reauth_otp(session, current_otp)
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= _REAUTH_MAX_OTP_ATTEMPTS:
                break
            logger.warning(
                "[2FA] 重认证 OTP 无效/过期，重新发送后再取（%s/%s）：%s",
                attempt, _REAUTH_MAX_OTP_ATTEMPTS, str(exc)[:180],
            )
            send_email_otp(session, referer="https://auth.openai.com/email-verification")
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            if attempt >= _REAUTH_MAX_OTP_ATTEMPTS or not _is_retryable_network(exc):
                raise
            last_exc = exc
            logger.warning(
                "[2FA] 重认证 OTP 网络抖动，重新发送后再取（%s/%s）：%s",
                attempt, _REAUTH_MAX_OTP_ATTEMPTS, str(exc)[:180],
            )
            send_email_otp(session, referer="https://auth.openai.com/email-verification")
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("重认证 OTP 验证失败")


def _generate_account_password(length: int = 14) -> str:
    """生成账号随机密码：大小写字母 + 数字混合（不含符号），各字符类至少 1 个。"""
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    chars = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
    ]
    pool = upper + lower + digits
    chars.extend(random.choice(pool) for _ in range(max(0, length - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


def _backfill_password_enabled() -> bool:
    """是否在 2FA 补跑时顺带设置密码（config/register.py BACKFILL_SET_PASSWORD）。"""
    try:
        from config import register as _cfg
        return bool(getattr(_cfg, "BACKFILL_SET_PASSWORD", True))
    except Exception:
        return True


def _set_password_after_twofa(session, email: str, password: str) -> dict:
    """尽力而为：发重置邮件 → 取链接 → 提交新密码。带重发重试（最多 3 次）。

    返回 dict：{password, password_status, password_error, password_done_at}
    - success：三步全过
    - failed：任一步失败（非致命，外层 2FA result 仍为 success）
    内部吞掉所有异常，最后一次错误写进 password_error。
    """
    last_exc: Exception | None = None
    for attempt in range(1, _PASSWORD_MAX_ATTEMPTS + 1):
        try:
            logger.info("[2FA] [PWD] 尝试设置密码（第 %s/%s 次）", attempt, _PASSWORD_MAX_ATTEMPTS)
            trigger_password_reset(session, email)  # 重发重置邮件
            reset_url = wait_for_reset_link(
                email,
                after_ts=time.time(),
                max_wait=120,
            )
            submit_new_password(session, reset_url, password)
            return {
                "password": password,
                "password_status": "success",
                "password_error": None,
                "password_done_at": datetime.now().isoformat(timespec="seconds"),
            }
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[2FA] [PWD] 密码步骤失败（第 %s/%s 次，继续重试）: %s",
                attempt, _PASSWORD_MAX_ATTEMPTS, str(exc)[:240],
            )
            if attempt < _PASSWORD_MAX_ATTEMPTS:
                time.sleep(2)
    err = f"{type(last_exc).__name__}: {str(last_exc)[:300]}"
    return {
        "password": password,
        "password_status": "failed",
        "password_error": err,
        "password_done_at": None,
    }


def _protocol_twofa_flow(email: str, proxy: str | None) -> dict:
    """Compatibility flow for accounts without a usable password+TOTP pair."""
    session, session_info = _run_stage_with_recovery(
        "protocol_login",
        # ``relogin_account`` owns the four-route rotation.  Node recovery
        # must not multiply that budget on the same logical login step.
        lambda: relogin_account(email, proxy, _max_proxy_attempts=_proxy_attempt_limit()),
        max_attempts=1,
    )
    logger.info("[2FA] 协议 OTP 重新登录成功，开始 reauth 2FA：%s", email)

    otp_after_ts = time.time()
    auth_url = _run_stage_with_recovery(
        "reauth_trigger",
        lambda: _trigger_reauth(session, email),
    )

    def follow_reauth() -> str:
        _follow_reauth(session, auth_url)
        return auth_url

    human_delay("api")
    _run_stage_with_recovery("reauth_follow", follow_reauth)
    human_delay("navigate")
    continue_url = _reauth_otp_with_retry(session, email, otp_after_ts)
    human_delay("otp_input")
    new_token = _run_stage_with_recovery(
        "token_exchange",
        lambda: _exchange_new_token(session, continue_url),
    )
    human_delay("api")
    secret, session_id = _run_stage_with_recovery(
        "totp_enroll",
        lambda: _enroll_totp(session, new_token),
    )
    human_delay("form")

    def activate() -> bool:
        if not _activate_totp(session, new_token, secret, session_id):
            raise RuntimeError("TOTP 激活接口未确认 success=true")
        return True

    _run_stage_with_recovery("totp_confirm", activate)
    return {
        "transport": "protocol_otp",
        "secret": secret,
        "access_token": new_token,
        "session": session_info,
        "protocol_session": session,
        "device_id": getattr(session, "device_id", None),
        "proxy_used": getattr(session, "proxy", None),
    }


def _recover_roxy_page(driver, registration, stage: str, attempt: int) -> None:
    """Return to the nearest stable page for a failed browser node."""
    if stage == "login" and attempt == 1:
        driver.refresh()
        return
    url = _LOGIN_URL if stage == "login" else "https://chatgpt.com/"
    registration._safe_get(
        driver,
        url,
        timeout=45,
        attempts=1,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
        script_timeout=90 if stage != "login" else 35,
    )


def _roxy_login_once(driver, registration, codex_oauth, email: str, password: str, totp_secret: str) -> dict:
    """Complete or resume the browser login node without using mailbox OTP."""
    try:
        if codex_oauth._has_access_token(driver):
            return registration._fetch_chatgpt_session(driver, timeout=45, auto_jump_wait=3)
    except Exception:
        pass

    registration._safe_get(
        driver,
        _LOGIN_URL,
        timeout=45,
        attempts=1,
        accept_hosts=("chatgpt.com", "auth.openai.com"),
        script_timeout=35,
    )
    try:
        if codex_oauth._has_access_token(driver):
            return registration._fetch_chatgpt_session(driver, timeout=45, auto_jump_wait=3)
    except Exception:
        pass
    registration._type_email_address(driver, email, timeout=20)
    registration._submit_email_step(driver, email)
    outcome = codex_oauth._login_with_password_and_2fa(
        driver,
        email,
        password,
        totp_secret,
        timeout=60,
    )
    if outcome == "email_otp":
        raise _EmailOtpFallbackRequired("Roxy 密码登录明确进入邮箱验证码节点")
    if outcome != "done":
        raise RuntimeError(f"Roxy 密码+TOTP 登录流程未完成: {outcome or 'unknown'}")
    session_info = registration._fetch_chatgpt_session(driver, timeout=90)
    if not isinstance(session_info, dict) or not str(session_info.get("accessToken") or "").strip():
        raise RuntimeError("Roxy 密码+TOTP 登录后未读取到 accessToken")
    return session_info


def _explicit_mfa_state(session_info: dict) -> bool | None:
    user = session_info.get("user") if isinstance(session_info, dict) else None
    user = user if isinstance(user, dict) else {}
    if "mfa" not in user:
        return None
    value = user.get("mfa")
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "enabled", "totp"}:
        return True
    if text in {"false", "0", "no", "disabled", "none", ""}:
        return False
    return None


def _run_roxy_twofa_flow_once(email: str, password: str, totp_secret: str, proxy: str | None) -> dict:
    """Login and replace 2FA in one disposable Roxy driver."""
    secrets = [password, totp_secret, *_totp_redaction_values(totp_secret)]
    client = None
    opened = None
    driver = None
    new_secret = ""
    new_token = ""
    with _redacted_browser_logs(secrets) as scrubber:
        try:
            registration, codex_oauth = _load_roxy_helpers()
            client = RoxyBrowserClient(preferred_proxy=proxy)
            opened = _staggered_open_profile(client)
            driver = registration._build_driver(opened)

            session_info = _run_stage_with_recovery(
                "roxy_login",
                lambda: _roxy_login_once(
                    driver,
                    registration,
                    codex_oauth,
                    email,
                    password,
                    totp_secret,
                ),
                recover=lambda attempt, _exc: _recover_roxy_page(
                    driver,
                    registration,
                    "login",
                    attempt,
                ),
            )
            login_token = str(session_info.get("accessToken") or "").strip()
            if login_token:
                scrubber.add_secret(login_token)
            logger.info("[2FA] Roxy 密码+TOTP 登录成功，继续当前窗口设置 2FA：%s", email)

            def setup_totp() -> tuple[str, str]:
                result = registration._enable_2fa_with_retry(driver, email, max_attempts=1)
                if not result or len(result) != 2:
                    raise RuntimeError("Roxy TOTP 设置节点未返回 secret/accessToken")
                return str(result[0] or ""), str(result[1] or "")

            new_secret, new_token = _run_stage_with_recovery(
                "roxy_totp_setup",
                setup_totp,
                recover=lambda attempt, _exc: _recover_roxy_page(
                    driver,
                    registration,
                    "setup",
                    attempt,
                ),
            )
            if not new_secret or not new_token:
                raise RuntimeError("Roxy TOTP 设置结果缺少 secret/accessToken")
            scrubber.add_secret(new_secret)
            scrubber.add_secret(new_token)

            def confirm_totp() -> dict:
                confirmed = registration._fetch_chatgpt_session(driver, timeout=45, auto_jump_wait=5)
                if not isinstance(confirmed, dict) or not str(confirmed.get("accessToken") or "").strip():
                    raise RuntimeError("2FA 激活后 session 确认缺少 accessToken")
                if _explicit_mfa_state(confirmed) is False:
                    raise RuntimeError("2FA 激活后 session 暂未显示 MFA 已启用")
                return confirmed

            confirmed_session = _run_stage_with_recovery(
                "roxy_totp_confirm",
                confirm_totp,
                recover=lambda attempt, _exc: _recover_roxy_page(
                    driver,
                    registration,
                    "confirm",
                    attempt,
                ),
            )
            confirmed_token = str(confirmed_session.get("accessToken") or "").strip()
            if confirmed_token:
                scrubber.add_secret(confirmed_token)
                new_token = confirmed_token
            return {
                "transport": "roxy_password_totp",
                "secret": new_secret,
                "access_token": new_token,
                "session": confirmed_session,
                "protocol_session": None,
                "device_id": None,
                "proxy_used": None,
                "browser_profile_id": str(getattr(opened, "profile_id", "") or ""),
            }
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception as exc:
                    logger.warning("[2FA] Roxy driver 关闭异常：%s", type(exc).__name__)
            if client is not None:
                try:
                    client.cleanup_profile(opened)
                except Exception as exc:
                    logger.warning("[2FA] Roxy profile 清理异常：%s", type(exc).__name__)
                close_http = getattr(getattr(client, "http", None), "close", None)
                if callable(close_http):
                    try:
                        close_http()
                    except Exception:
                        pass


def _run_roxy_twofa_flow(email: str, password: str, totp_secret: str, proxy: str | None) -> dict:
    """Run 2FA in a fresh Roxy profile after each transient proxy failure."""
    routes = _account_proxy_routes(proxy, _proxy_attempt_limit())
    last_exc: BaseException | None = None
    for attempt, route in enumerate(routes, start=1):
        selected = route.get("proxy")
        try:
            result = _run_roxy_twofa_flow_once(
                email,
                password,
                totp_secret,
                selected,
            )
            result.setdefault("attempt_count", attempt)
            result.setdefault("max_attempts", len(routes))
            result.setdefault("proxy_used", route.get("proxy_used"))
            return result
        except Exception as exc:
            last_exc = exc
            if isinstance(exc, _EmailOtpFallbackRequired):
                raise
            if attempt >= len(routes) or not _is_retryable_network(exc):
                raise
            logger.warning(
                "[2FA] Roxy 网络失败，清理环境并切换代理（%s/%s）：%s: %s",
                attempt,
                len(routes),
                type(exc).__name__,
                str(exc)[:220],
            )
            time.sleep(1.5)
    raise last_exc if last_exc else RuntimeError("2FA Roxy 代理重试失败")


def _execute_twofa_flow(
    email: str,
    *,
    password: str,
    totp_secret: str,
    proxy: str | None,
) -> dict:
    """Select Roxy only for a complete password+TOTP login credential pair."""
    password = str(password or "").strip()
    totp_secret = str(totp_secret or "").strip()
    if password and totp_secret:
        try:
            return _run_roxy_twofa_flow(email, password, totp_secret, proxy)
        except _EmailOtpFallbackRequired:
            logger.info("[2FA] Roxy 明确进入邮箱验证码节点，回退协议 OTP 兼容流程：%s", email)
            return _protocol_twofa_flow(email, proxy)

    missing = "TOTP" if not totp_secret else "密码"
    logger.info("[2FA] 本地缺少%s，使用协议 OTP 兼容流程：%s", missing, email)
    return _protocol_twofa_flow(email, proxy)


def _run_twofa(
    *,
    account_id: int,
    email: str,
    proxy: str | None,
    trigger: str,
    password: str = "",
    totp_secret: str = "",
) -> dict:
    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    protocol_session = None
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_twofa_running(account_id):
            _append_log(email, "[2FA] 账号已删除或 2FA 状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或 2FA 状态已被重置"}

        # 与查活共用同一账号日志文件：本线程所有 logger 记录都落盘，方便查看详情。
        path = log_path(email)
        path.parent.mkdir(parents=True, exist_ok=True)
        thread_name = threading.current_thread().name
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info(
            "[2FA] 开始补跑 2FA：%s trigger=%s proxy=%s",
            email,
            trigger,
            "已指定" if proxy else "配置随机/直连",
        )

        password = str(password or "").strip()
        totp_secret = str(totp_secret or "").strip()
        flow = _execute_twofa_flow(
            email,
            password=password,
            totp_secret=totp_secret,
            proxy=proxy,
        )

        secret = str(flow.get("secret") or "")
        new_token = str(flow.get("access_token") or "")
        session_info = flow.get("session") if isinstance(flow.get("session"), dict) else {}
        protocol_session = flow.get("protocol_session")
        if not secret or not new_token:
            raise RuntimeError("2FA 流程完成但缺少 secret/accessToken")

        # ---- 阶段三（可选、尽力而为）：顺带设置 ChatGPT 登录密码 ----
        # 密码失败不影响 2FA 结果；_set_password_after_twofa 内部吞异常，只返回状态。
        pwd_fields: dict = {"password_status": "skipped", "password": password}
        if not password and protocol_session is not None and _backfill_password_enabled():
            pwd_fields = _set_password_after_twofa(
                protocol_session,
                email,
                _generate_account_password(),
            )

        result = {
            "ok": True,
            "status": "success",
            "totp_secret": secret,
            "access_token": new_token,
            "session": session_info,
            "device_id": flow.get("device_id"),
            "proxy_used": flow.get("proxy_used"),
            "twofa_transport": flow.get("transport"),
            **({"browser_profile_id": flow.get("browser_profile_id")} if flow.get("browser_profile_id") else {}),
            "done_at": datetime.now().isoformat(timespec="seconds"),
            **pwd_fields,
        }
        db.update_account_twofa(account_id, result)
        try:
            if pwd_fields.get("password_status") != "skipped":
                db.update_account_password(account_id, result)  # 独立 writer，只写密码字段
        except Exception as exc:
            logger.warning("[2FA] 密码字段落库失败（不影响 2FA）: %s", str(exc)[:160])
        logger.info(
            "[2FA] 完成：secret=%s...%s，已刷新 accessToken，password_status=%s",
            secret[:4], secret[-4:], pwd_fields.get("password_status"),
        )
        return result
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or "account_deactivated"
        result = {
            "ok": False,
            "status": "deactivated",
            "error": code,
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            db.update_account_twofa(account_id, result)
        except Exception:
            logger.exception("[2FA] 写入已废状态失败: account_id=%s", account_id)
        logger.warning("[2FA] 账号已废：%s %s", email, code)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            db.update_account_twofa(account_id, result)
        except Exception:
            logger.exception("[2FA] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[2FA] 后台异常：%s", email)
        return result
    finally:
        raw_session = getattr(protocol_session, "session", None)
        close_session = getattr(raw_session, "close", None)
        if callable(close_session):
            try:
                close_session()
            except Exception:
                pass
        if fh is not None:
            try:
                root_logger.removeHandler(fh)
                fh.close()
            except Exception:
                pass
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_twofa(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "2FA 补跑队列已满，请稍后重试"}
    acc = db.get_account(account_id) or {}
    if not acc:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": False, "error": "账号不存在"}
    if str(acc.get("live_check_status") or "") in {"queued", "running"}:
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活，请稍后再补跑 2FA"}
    if not db.claim_account_twofa(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在补跑 2FA"}

    _append_log(email, f"[2FA] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_twofa,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
            password=str(acc.get("chatgpt_password") or acc.get("password") or "").strip(),
            totp_secret=str(acc.get("totp_secret") or "").strip(),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "error": f"2FA 补跑入队失败: {type(exc).__name__}: {str(exc)[:160]}",
            "done_at": datetime.now().isoformat(timespec="seconds"),
        }
        db.update_account_twofa(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
