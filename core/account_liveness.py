# -*- coding: utf-8 -*-
"""已注册账号查活：Roxy 密码+TOTP 浏览器登录，并保留共享邮箱 OTP 重登录辅助。"""
import importlib
import logging
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.session import BrowserSession
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import follow_oauth_callback, fetch_session
from core.email_provider import wait_for_otp
from core.roxybrowser_client import RoxyBrowserClient

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()
_LOGIN_URL = "https://chatgpt.com/auth/login"
_ROXY_OPEN_LOCK = threading.Lock()
_ROXY_LAST_OPEN_AT = 0.0
_ROXY_START_MAX_ATTEMPTS = 4

# Imported only when a live-check task actually opens Roxy.  Keeping these
# imports lazy avoids loading Selenium/browser helpers in status-only workers.
roxy_registration = None
roxy_codex_oauth = None

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "408", "425", "429", "500", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset", "refused", "temporarily",
    "tls", "ssl", "curl (5)", "curl (7)", "curl (28)", "curl (35)",
)

_TERMINAL_LOGIN_HINTS = (
    "wrong password", "invalid password", "password is incorrect",
    "账号密码错误", "邮箱或密码", "totp secret", "2fa(totp)",
    "invalid otp", "incorrect otp", "验证码错误", "验证码无效",
    "account_deactivated", "account_deleted", "account_banned",
    "账号已废", "账号停用", "账号封禁",
)


def _proxy_attempt_limit(value: int | None = None) -> int:
    if value is None:
        try:
            from config import proxy as proxy_config

            value = int(getattr(proxy_config, "PROXY_RETRY_MAX_ATTEMPTS", 4) or 4)
        except Exception:
            value = 4
    return max(1, min(4, int(value or 1)))


def _account_proxy_routes(proxy: str | None, max_attempts: int | None = None) -> list[dict]:
    """Return distinct account-operation routes, with the system proxy last."""
    attempts = _proxy_attempt_limit(max_attempts)
    try:
        from core.chatgpt_plan import _plan_check_routes

        routes = _plan_check_routes(proxy, attempts)
    except Exception:
        # Preserve the caller's explicit direct/proxy semantics even when route
        # discovery itself is unavailable during a lightweight worker import.
        routes = [{
            "proxy": proxy,
            "network_route": "configured" if proxy is None else ("proxy" if proxy else "direct"),
            "proxy_used": None,
        }]
    return list(routes[:attempts]) or [{"proxy": proxy, "network_route": "configured", "proxy_used": None}]


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (AccountUnusableError, EmailOtpInvalidError, ValueError)):
        return False
    text = str(exc or "").lower()
    if any(hint in text for hint in _TERMINAL_LOGIN_HINTS):
        return False
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _network_preflight_with_retry(email: str, proxy: str | None, max_attempts: int = 4) -> tuple[BrowserSession, str]:
    """Providers → CSRF → Signin 网络预检；失败换新 IP 重试（每轮新会话新代理）。"""
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    routes = _account_proxy_routes(proxy, max_attempts)
    for attempt, route in enumerate(routes, start=1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass
        session = BrowserSession(proxy=route.get("proxy"))
        logger.info(
            "[查活] 会话创建完成：route=%s proxy=%s device_id=%s（网络预检第 %s/%s 次）",
            route.get("network_route") or "-",
            route.get("proxy_used") or ("direct" if not session.proxy else "configured"),
            session.device_id,
            attempt,
            len(routes),
        )
        try:
            get_providers(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= len(routes) or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），换新 IP 重试：%s",
                attempt, len(routes), str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _load_roxy_helpers():
    global roxy_registration, roxy_codex_oauth
    if roxy_registration is None:
        roxy_registration = importlib.import_module("core.roxy_registration")
    if roxy_codex_oauth is None:
        roxy_codex_oauth = importlib.import_module("core.roxy_codex_oauth")
    return roxy_registration, roxy_codex_oauth


def _roxy_open_stagger_seconds() -> float:
    from config import register as register_config

    try:
        return max(0.0, float(getattr(register_config, "BATCH_STAGGER", 2.0) or 0.0))
    except (TypeError, ValueError):
        return 2.0


def _staggered_open_profile(client: RoxyBrowserClient):
    """Space concurrent live-check browser starts using the batch interval."""
    global _ROXY_LAST_OPEN_AT
    interval = _roxy_open_stagger_seconds()
    with _ROXY_OPEN_LOCK:
        now = time.monotonic()
        wait_seconds = max(0.0, _ROXY_LAST_OPEN_AT + interval - now)
        if wait_seconds:
            logger.info("[查活] 并发窗口错峰等待 %.1f 秒", wait_seconds)
            time.sleep(wait_seconds)
        _ROXY_LAST_OPEN_AT = now + wait_seconds
    return client.open_profile()


def _redact(value: object, secrets=(), *, limit: int = 800) -> str:
    text = str(value or "")
    for secret in sorted(
        {str(item) for item in secrets if item is not None and len(str(item)) >= 4},
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)\b((?:https?|socks(?:4a?|5h?))://)[^/\s:@]+:[^@\s/]+@",
        r"\1[REDACTED]@",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[a-z0-9._~+\-/=]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(['\"]?(?:access[_-]?token|refresh[_-]?token)['\"]?\s*[:=]\s*['\"]?)[^\s,'\"}]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"\beyJ[a-zA-Z0-9_-]{12,}\.[a-zA-Z0-9_-]{8,}(?:\.[a-zA-Z0-9_-]+)?\b",
        "[REDACTED]",
        text,
    )
    return text[:max(0, int(limit))]


def _totp_redaction_values(totp_secret: str) -> list[str]:
    """Cover the TOTP values that Selenium may emit in debug command logs."""
    try:
        import pyotp
        generator = pyotp.TOTP(totp_secret)
        now = time.time()
        return [generator.at(now + offset * 30) for offset in range(-2, 9)]
    except Exception:
        return []


class _SensitiveLogFilter(logging.Filter):
    def __init__(self, secrets=()):
        super().__init__()
        self._thread_ident = threading.get_ident()
        self._secrets = list(secrets)

    def add_secret(self, value: object) -> None:
        if value:
            self._secrets.append(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if threading.get_ident() != self._thread_ident:
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        if record.exc_info:
            rendered = f"{rendered}: {type(record.exc_info[1]).__name__}"
            record.exc_info = None
            record.exc_text = None
        record.msg = _redact(rendered, self._secrets)
        record.args = ()
        return True


@contextmanager
def _redacted_browser_logs(secrets=()):
    scrubber = _SensitiveLogFilter(secrets)
    targets = [
        logging.getLogger(__name__),
        logging.getLogger("core.roxybrowser_client"),
        logging.getLogger("core.roxy_registration"),
        logging.getLogger("core.roxy_codex_oauth"),
    ]
    handlers = list(logging.getLogger().handlers)
    for target in targets:
        target.addFilter(scrubber)
    for handler in handlers:
        handler.addFilter(scrubber)
    try:
        yield scrubber
    finally:
        for handler in handlers:
            handler.removeFilter(scrubber)
        for target in targets:
            target.removeFilter(scrubber)


def _cleanup_roxy_environment(client, opened, driver, *, secrets=(), access_token: str = "") -> None:
    if driver is not None:
        try:
            driver.quit()
        except Exception as exc:
            logger.warning("[查活] Roxy driver 关闭异常：%s", _redact(exc, [*secrets, access_token]))
    if client is None:
        return
    try:
        client.cleanup_profile(opened)
    except Exception as exc:
        logger.warning("[查活] Roxy profile 清理异常：%s", _redact(exc, [*secrets, access_token]))
    raw_http = getattr(client, "http", None)
    close_http = getattr(raw_http, "close", None)
    if callable(close_http):
        try:
            close_http()
        except Exception as exc:
            logger.warning("[查活] Roxy API session 关闭异常：%s", _redact(exc, [*secrets, access_token]))


def _open_roxy_environment_with_retry(
    registration,
    *,
    secrets=(),
    proxy: str | None = None,
    max_attempts: int = _ROXY_START_MAX_ATTEMPTS,
):
    """Create a usable temporary Roxy environment before entering login stages."""
    max_attempts = max(1, int(max_attempts or 1))
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        client = None
        opened = None
        driver = None
        try:
            client = RoxyBrowserClient(preferred_proxy=proxy)
            opened = _staggered_open_profile(client)
            driver = registration._build_driver(opened)
            if driver is None:
                raise RuntimeError("Roxy WebDriver 创建后为空")
            return client, opened, driver
        except Exception as exc:
            last_exc = exc
            _cleanup_roxy_environment(client, opened, driver, secrets=secrets)
            if attempt >= max_attempts:
                break
            logger.warning(
                "[查活][恢复][浏览器环境] 启动失败（%s/%s），清理后重新创建：%s",
                attempt,
                max_attempts,
                _redact(f"{type(exc).__name__}: {exc}", secrets, limit=260),
            )
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Roxy 浏览器环境启动失败")


def _relogin_account_with_password_totp_once(
    email: str,
    password: str,
    totp_secret: str,
    proxy: str | None = None,
) -> dict:
    """Log in through a disposable Roxy browser using password and TOTP only.

    The returned session is detached data.  The Selenium driver and temporary
    Roxy profile are always cleaned before this function returns or raises.
    ``proxy`` remains accepted for the live-check API; the Roxy profile owns
    its configured proxy selection.
    """
    email = str(email or "").strip()
    password = str(password or "").strip()
    totp_secret = str(totp_secret or "").strip()
    missing = []
    if not email:
        missing.append("email")
    if not password:
        missing.append("chatgpt_password/password")
    if not totp_secret:
        missing.append("totp_secret")
    if missing:
        raise ValueError(f"查活缺少本地登录凭据: {', '.join(missing)}")

    secrets = [password, totp_secret, *_totp_redaction_values(totp_secret)]
    client = None
    opened = None
    driver = None
    access_token = ""
    with _redacted_browser_logs(secrets) as scrubber:
        try:
            registration, codex_oauth = _load_roxy_helpers()
            client, opened, driver = _open_roxy_environment_with_retry(
                registration,
                secrets=secrets,
                proxy=proxy,
                # The public wrapper owns route rotation; this helper only
                # retries local Roxy/API startup when called directly.
                max_attempts=1,
            )

            def resume_open_stage(state, _report):
                ready = state in {"email", "login_password", "otp", "logged_in", "chatgpt"}
                return ready, state

            def open_login_stage():
                registration._safe_get(
                    driver,
                    _LOGIN_URL,
                    timeout=45,
                    attempts=2,
                    accept_hosts=("chatgpt.com", "auth.openai.com"),
                    script_timeout=35,
                )
                return "email"

            registration._run_stage_with_recovery(
                driver,
                "查活打开登录页",
                open_login_stage,
                stage_url=_LOGIN_URL,
                resume_from_state=resume_open_stage,
            )

            def submit_email_operation():
                registration._type_email_address(driver, email, timeout=20)
                registration._submit_email_step(driver, email)
                state = registration._wait_email_submit_next_state(driver, email, timeout=30)
                if state in {"email_page", "unknown"}:
                    raise RuntimeError(f"邮箱提交后页面未推进到密码/验证阶段：state={state}")
                if state == "password":
                    raise RuntimeError("邮箱提交后进入注册密码页而非登录密码页")
                return state

            def resume_email_stage(state, _report):
                advanced = state in {"login_password", "otp", "logged_in", "chatgpt"}
                return advanced, state

            def run_email_stage():
                return registration._run_stage_with_recovery(
                    driver,
                    "查活提交邮箱",
                    submit_email_operation,
                    stage_url=_LOGIN_URL,
                    resume_from_state=resume_email_stage,
                )

            def password_totp_operation():
                outcome = codex_oauth._login_with_password_and_2fa(
                    driver,
                    email,
                    password,
                    totp_secret,
                    timeout=60,
                )
                if outcome == "email_otp":
                    raise RuntimeError("密码+2FA 登录后页面要求邮箱验证码；未读取邮箱取码")
                if outcome != "done":
                    raise RuntimeError(f"密码+2FA 登录流程未完成: {outcome or 'unknown'}")
                return outcome

            def resume_password_stage(state, _report):
                done = state in {"logged_in", "chatgpt"}
                return done, "done"

            def run_password_totp_stage():
                stage_url = str(getattr(driver, "current_url", "") or "").strip() or _LOGIN_URL
                return registration._run_stage_with_recovery(
                    driver,
                    "查活密码和2FA登录",
                    password_totp_operation,
                    stage_url=stage_url,
                    previous_url=_LOGIN_URL,
                    replay_previous=run_email_stage,
                    resume_from_state=resume_password_stage,
                )

            email_state = run_email_stage()
            if email_state not in {"logged_in", "chatgpt"}:
                run_password_totp_stage()

            def replay_login_from_email():
                replay_state = run_email_stage()
                if replay_state not in {"logged_in", "chatgpt"}:
                    run_password_totp_stage()

            def fetch_valid_session():
                nonlocal access_token
                info = registration._fetch_chatgpt_session(driver, timeout=90)
                if not isinstance(info, dict):
                    raise RuntimeError("浏览器登录返回的 session 格式异常")
                token = str(info.get("accessToken") or "").strip()
                if not token:
                    raise RuntimeError("密码+2FA 登录后未读取到 accessToken")
                access_token = token
                scrubber.add_secret(access_token)
                return info

            session_info = registration._run_stage_with_recovery(
                driver,
                "查活获取Session和AT",
                fetch_valid_session,
                stage_url="https://chatgpt.com/",
                previous_url=_LOGIN_URL,
                replay_previous=replay_login_from_email,
            )
            scrubber.add_secret(access_token)
            logger.info("[查活] Roxy 密码+2FA 登录成功：%s", email)
            return {
                "access_token": access_token,
                "session": session_info,
                "browser_profile_id": str(getattr(opened, "profile_id", "") or ""),
                "network_route": "roxy_browser",
            }
        except AccountUnusableError as exc:
            code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
            raise AccountUnusableError(
                _redact(str(exc), [*secrets, access_token]),
                error_code=code,
            ) from None
        except Exception as exc:
            safe = _redact(f"{type(exc).__name__}: {exc}", [*secrets, access_token])
            code = detect_account_unusable_text(safe)
            if code:
                raise AccountUnusableError(safe, error_code=code) from None
            raise RuntimeError(safe) from None
        finally:
            _cleanup_roxy_environment(
                client,
                opened,
                driver,
                secrets=secrets,
                access_token=access_token,
            )


def relogin_account_with_password_totp(
    email: str,
    password: str,
    totp_secret: str,
    proxy: str | None = None,
) -> dict:
    """Password+TOTP login with a fresh Roxy environment per proxy failure."""
    routes = _account_proxy_routes(proxy, _proxy_attempt_limit())
    last_exc: BaseException | None = None
    for attempt, route in enumerate(routes, start=1):
        selected = route.get("proxy")
        try:
            result = _relogin_account_with_password_totp_once(
                email,
                password,
                totp_secret,
                proxy=selected,
            )
            result.setdefault("attempt_count", attempt)
            result.setdefault("max_attempts", len(routes))
            result.setdefault("proxy_used", route.get("proxy_used"))
            return result
        except Exception as exc:
            last_exc = exc
            if attempt >= len(routes) or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] Roxy 登录网络失败，清理环境并切换代理（%s/%s）：%s",
                attempt,
                len(routes),
                _redact(f"{type(exc).__name__}: {exc}", [password, totp_secret], limit=260),
            )
            time.sleep(1.5)
    raise last_exc if last_exc else RuntimeError("Roxy 登录代理重试失败")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts)
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def _relogin_account_once(email: str, proxy: str | None = None) -> tuple[BrowserSession, dict]:
    """
    重新邮箱 OTP 登录已注册账号，返回 (session, session_info)。

    这是取消套餐与补跑 2FA 复用的协议辅助（Providers → CSRF → Signin →
    Authorize → 邮箱 OTP → OAuth callback → Session/AT）。它不参与账号查活，
    不写日志文件、不记录查活状态；返回的 session 带 chatgpt.com 登录态 cookie，
    可继续用于 2FA 重认证（reauth）等后续链路。

    失败抛异常：
        - AccountUnusableError: 账号已废（删除/停用/封禁），再试无意义
        - RuntimeError / EmailOtpInvalidError: 其它可重试或需上层处理的错误
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    # The caller supplies one already-selected route here.  Keeping the
    # preflight bounded to one attempt ensures a failed route is replaced by
    # the outer rotation loop instead of being retried on the same proxy.
    session, authorize_url = _network_preflight_with_retry(email, proxy, max_attempts=1)
    try:
        otp_after_ts = time.time()
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            raise AccountUnusableError(f"账号已废弃（{dead_code}），邮箱不可再用", error_code=dead_code)

        validate_result = _validate_with_retry(session, email, otp_after_ts)
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        if not continue_url:
            raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
        if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
            raise RuntimeError(f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}")

        follow_oauth_callback(session, str(continue_url), referer="https://auth.openai.com/email-verification")
        session_info = fetch_session(session)
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")
        logger.info("[查活] 重新登录成功：%s", email)
        return session, session_info
    except BaseException:
        try:
            session.session.close()
        except Exception:
            pass
        raise


def relogin_account(
    email: str,
    proxy: str | None = None,
    *,
    _max_proxy_attempts: int | None = None,
) -> tuple[BrowserSession, dict]:
    """Re-login with bounded proxy rotation for transient network failures."""
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    routes = _account_proxy_routes(
        proxy,
        _proxy_attempt_limit(_max_proxy_attempts),
    )
    last_exc: BaseException | None = None
    for attempt, route in enumerate(routes, start=1):
        selected = route.get("proxy")
        logger.info(
            "[查活] 协议登录网络节点 %s/%s：route=%s proxy=%s",
            attempt,
            len(routes),
            route.get("network_route") or "-",
            route.get("proxy_used") or ("direct" if not selected else "configured"),
        )
        try:
            return _relogin_account_once(email, selected)
        except Exception as exc:
            last_exc = exc
            if attempt >= len(routes) or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 协议登录网络失败，切换代理重试（%s/%s）：%s",
                attempt,
                len(routes),
                str(exc)[:220],
            )
            time.sleep(1.5)
    raise last_exc if last_exc else RuntimeError("协议登录代理重试失败")


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    password: str | None = None,
    totp_secret: str | None = None,
    clear_log: bool = True,
) -> dict:
    """
    使用 Roxy 浏览器通过邮箱、密码和 TOTP 查活并刷新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    key = email.lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    fh: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始 Roxy 密码+2FA 登录：%s", email)
        logger.info("[查活] 流程：Roxy 浏览器 → 邮箱+密码 → TOTP → Session/AT（不读取邮箱验证码）")
        login_result = relogin_account_with_password_totp(
            email,
            str(password or ""),
            str(totp_secret or ""),
            proxy=proxy,
        )
        session_info = login_result.get("session") or {}
        access_token = str(login_result.get("access_token") or session_info.get("accessToken") or "")
        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "browser_profile_id": login_result.get("browser_profile_id"),
            "network_route": "roxy_browser",
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
